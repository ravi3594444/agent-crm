"""¿Se puede entregar en esta dirección sin que lo mire una persona?

POR QUÉ NO LO DECIDE EL MODELO
Si el LLM pudiera opinar sobre si una dirección "está cerca", un cliente lo
convencería con un mensaje: "es acá al lado, mandámelo igual". Acá no hay
criterio, hay comparaciones contra las zonas que configuró el negocio. El
modelo puede PEDIR la dirección y repetir lo que se decidió; no puede decidir.

LA REGLA (release gate RC1)
  * Con los códigos postales y las localidades configurados, la dirección
    tiene que traer LOS DOS datos y LOS DOS tienen que estar permitidos.
  * Con una sola lista configurada, manda esa lista sobre su dato.
  * Sin ninguna lista, nada se entrega solo.
  * Cualquier contradicción (un dato dentro y el otro fuera), cualquier dato
    exigido que falte y cualquier valor que no se pueda interpretar dejan el
    pedido en BORRADOR para que lo mire una persona.
Un pedido anterior a la misma dirección ya NO habilita nada por sí solo: la
zona la definen las listas, y si el dueño quiere entregar ahí, agrega el
código postal o la localidad a la lista. Así la regla es la misma para todos.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app import erpnext, limites

# Todos los motivos de esta capa arrancan igual, para que el resto del sistema
# sepa que ESTE pedido está esperando por la entrega y no por otra regla: el
# aviso al equipo lo dice y al cliente se le habla distinto.
MOTIVO = "entrega a revisar"

# Por qué NO se entrega, en categorías que el resto del sistema puede leer.
OK = "ok"
SIN_ZONAS = "sin_zonas"
FALTA = "falta"
FUERA = "fuera"
CONTRADICCION = "contradiccion"
ERROR = "error"


@dataclass(frozen=True)
class EvaluacionZona:
    dentro: bool
    motivo: str
    categoria: str


def _sin_tildes(texto: str) -> str:
    return "".join(
        char
        for char in unicodedata.normalize("NFD", texto.lower())
        if unicodedata.category(char) != "Mn"
    )


def normalizar_localidad(texto: object) -> str:
    """Minúsculas, sin tildes y sin puntuación: "Villa Allende" == "villa allende"."""
    limpio = _sin_tildes(str(texto or ""))
    return re.sub(r"[^a-z0-9]+", " ", limpio).strip()


def normalizar_cp(texto: object) -> str:
    """Sólo los caracteres alfanuméricos, en mayúsculas: "X5000" == "x 5000"."""
    return re.sub(r"[^A-Z0-9]+", "", str(texto or "").upper())


def zonas_configuradas() -> tuple[frozenset[str], frozenset[str]]:
    """(códigos postales, localidades) donde el negocio reparte.

    Salen de app/limites.py, no del entorno: son una regla de entrega como los
    días y la hora, y el dueño las cambia por WhatsApp con el mismo código de
    cuatro dígitos. El entorno sigue siendo el valor de ARRANQUE (lo resuelve
    `limites.zonas()`), así que un .env existente sigue funcionando igual.

    Se leen en cada llamada: cambiar una zona no necesita reiniciar nada.
    """
    codigos_crudos, localidades_crudas = limites.zonas()
    codigos = frozenset(normalizar_cp(cp) for cp in codigos_crudos)
    localidades = frozenset(
        normalizar_localidad(loc) for loc in localidades_crudas
    )
    return (
        frozenset(cp for cp in codigos if cp),
        frozenset(loc for loc in localidades if loc),
    )


def texto_direccion(direccion: dict) -> str:
    """La dirección como la leería una persona, para el aviso al equipo."""
    partes = [
        str(direccion.get("address_line1") or "").strip(),
        str(direccion.get("address_line2") or "").strip(),
        str(direccion.get("city") or "").strip(),
    ]
    escrito = ", ".join(parte for parte in partes if parte)
    cp = str(direccion.get("pincode") or "").strip()
    if cp:
        escrito = f"{escrito} (CP {cp})" if escrito else f"CP {cp}"
    return escrito or "sin datos de dirección"


def _requerido(nombre: str, crudo: str, normalizado: str) -> str | None:
    """Motivo si un dato exigido falta o no se puede interpretar; None si sirve."""
    if not crudo:
        return f"la dirección no tiene {nombre}"
    if not normalizado:
        return f"{nombre} «{crudo}» no se pudo interpretar"
    return None


def evaluar_zona(direccion: object) -> EvaluacionZona:
    """Determinista y sin red: sólo compara texto contra las listas configuradas."""
    if not isinstance(direccion, dict):
        return EvaluacionZona(False, "la dirección no se pudo interpretar", ERROR)
    codigos, localidades = zonas_configuradas()
    if not codigos and not localidades:
        return EvaluacionZona(False, "no hay zonas de reparto configuradas", SIN_ZONAS)

    cp_crudo = str(direccion.get("pincode") or "").strip()
    loc_cruda = str(direccion.get("city") or "").strip()
    try:
        cp = normalizar_cp(cp_crudo)
        localidad = normalizar_localidad(loc_cruda)
    except Exception:
        return EvaluacionZona(False, "no pude interpretar el código postal o la localidad", ERROR)

    faltantes: list[str] = []
    ilegibles: list[str] = []
    if codigos:
        problema = _requerido("código postal", cp_crudo, cp)
        if problema and "no se pudo interpretar" in problema:
            ilegibles.append(problema)
        elif problema:
            faltantes.append("código postal")
    if localidades:
        problema = _requerido("localidad", loc_cruda, localidad)
        if problema and "no se pudo interpretar" in problema:
            ilegibles.append(problema)
        elif problema:
            faltantes.append("localidad")
    if ilegibles:
        return EvaluacionZona(False, "; ".join(ilegibles), ERROR)
    if faltantes:
        return EvaluacionZona(
            False,
            "la dirección no tiene " + " ni ".join(faltantes) + ", y las zonas de reparto lo exigen",
            FALTA,
        )

    cp_ok = (cp in codigos) if codigos else None
    loc_ok = (localidad in localidades) if localidades else None
    if codigos and localidades:
        if cp_ok and loc_ok:
            return EvaluacionZona(True, "", OK)
        if cp_ok != loc_ok:
            return EvaluacionZona(
                False,
                f"el código postal {cp} y la localidad «{loc_cruda}» se contradicen: "
                "uno está en las zonas de reparto y el otro no",
                CONTRADICCION,
            )
        return EvaluacionZona(
            False,
            f"el código postal {cp} y la localidad «{loc_cruda}» no están en las zonas de reparto",
            FUERA,
        )
    if codigos:
        if cp_ok:
            return EvaluacionZona(True, "", OK)
        return EvaluacionZona(False, f"el código postal {cp} no está en las zonas de reparto", FUERA)
    if loc_ok:
        return EvaluacionZona(True, "", OK)
    return EvaluacionZona(
        False, f"la localidad «{loc_cruda}» no está en las zonas de reparto", FUERA
    )


def en_zona(direccion: dict) -> tuple[bool, str]:
    """(en_zona, motivo). Compatibilidad: la evaluación completa es evaluar_zona."""
    evaluacion = evaluar_zona(direccion)
    return evaluacion.dentro, evaluacion.motivo


def nombre_direccion(sales_order: dict) -> str:
    """La dirección de ENTREGA del pedido, que es la que importa."""
    for campo in ("shipping_address_name", "customer_address"):
        valor = str(sales_order.get(campo) or "").strip()
        if valor:
            return valor
    return ""


def autorizada(sales_order: dict) -> tuple[bool, str]:
    """(se puede entregar sin revisión humana, motivo para el equipo).

    Nunca levanta: cualquier duda vuelve como False, que es lo que la política
    necesita para dejar el pedido en borrador en vez de prometer una entrega.
    """
    nombre = nombre_direccion(sales_order)
    if not nombre:
        return False, f"{MOTIVO}: el pedido no tiene dirección cargada"
    try:
        direccion = erpnext.policy_get_doc("Address", nombre)
    except erpnext.ERPNextError as exc:
        print(f"[entrega] no pude leer la dirección {nombre}: {exc}")
        return False, f"{MOTIVO}: no pude leer la dirección {nombre}"
    if not isinstance(direccion, dict):
        return False, f"{MOTIVO}: no pude leer la dirección {nombre}"

    evaluacion = evaluar_zona(direccion)
    if evaluacion.dentro:
        return True, ""
    return False, f"{MOTIVO}: {texto_direccion(direccion)} — {evaluacion.motivo}"
