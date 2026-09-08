"""Cuánto se confirma solo, cuánto lo confirmás vos, y qué lo está frenando.

POR QUÉ EXISTE
La única vista de lo que hizo la política era `make decisiones`, que es un grep
sobre los logs de un contenedor — se borran al recrearlo, hay que tener SSH, y
no es una respuesta que el dueño pueda pedir por WhatsApp. Así, la decisión de
subir un límite se tomaba sin ningún número.

Todo lo que se cuenta acá sale de hechos DURABLES en ERPNext, no de logs:

  * `[confirmado-por-agente] … fuente=…` — quién confirmó cada pedido. El
    `fuente` se escribía desde el principio y NADIE lo leía de vuelta
    (`confirmacion._SELLO` sólo captura el sello de tiempo), así que el parser
    es nuevo. Tres orígenes distintos, y la diferencia es el punto: la política
    sola, una persona a mano, o el cliente aceptando una oferta.
  * `[sombra] {…}` — qué habrían dicho las reglas (W1). Trae los motivos ya
    SEPARADOS en lista, así que los frenos se cuentan sobre datos y no sobre
    prosa. Es la fuente preferida.
  * «Requiere revisión humana: …» — el comentario que deja `_after_create`
    cuando no se auto-confirma. Es prosa interpolada, así que sólo se puede
    agrupar por fragmentos estables; se usa para los pedidos que no tienen
    registro de sombra.
  * «Rechazado manualmente por…» — el rechazo, que ya es un hecho contable.

EL NÚMERO QUE NADIE VEÍA
`policy._borradores_que_reservan` levanta pasados `MAX_BORRADORES` borradores
vivos, y a partir de ahí NO SE AUTO-CONFIRMA NADA, de ningún producto, con un
motivo («no se pudo verificar stock de X») que se lee igual que una caída de
ERPNext. Ese contador se lee ACÁ, directo, contando los borradores — no
sacándolo de los motivos de `evaluar`, que son deliberadamente uniformes y no
distinguen un techo lleno de una caída.

Cuenta los borradores de TODOS los orígenes, porque el que cargó una persona a
mano retiene stock y ocupa lugar en ese techo exactamente igual: `policy` no
filtra por origen.

CADA NÚMERO PUEDE SER «NO SÉ»
None nunca es 0. Un resumen que informa una autonomía de cero porque no pudo
leer ERPNext es peor que uno que dice que no pudo leer: con el primero, el
dueño baja un límite que no hacía falta bajar.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from app import entrega, erpnext, idioma, policy

DIAS_DEFAULT = 7
# Techo por lectura. Un pedido deja varios comentarios, así que esto es un
# techo de COMENTARIOS y no de pedidos; si se llena, el resumen lo dice en vez
# de informar un número recortado como si fuera el total.
MAX_COMENTARIOS = 500
# Cuántos (producto, depósito) se miran para la frescura de los conteos.
MAX_PRODUCTOS = 20

MARCA_REVISION = "Requiere revisión humana:"
MARCA_RECHAZO = "Rechazado manualmente por"

_FUENTE = re.compile(r"fuente=(?P<fuente>.+?)\s*$", re.MULTILINE)

# La tabla de agrupación, en UN solo lugar. Gana el primero que coincide, así
# que el orden importa: «descuento» va después de los que lo mencionan de paso.
#
# Son fragmentos y no anclas: los motivos de `policy` son prosa interpolada
# («monto $8.450 supera el tope de $0»), distinta en cada pedido. Lo que NO
# coincide con nada cae en «otros» CON su cuenta — un freno que nadie agrupó
# tiene que verse, porque es la señal de que falta una fila en esta tabla.
_GRUPOS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("auto-confirmación apagada", ("auto-confirmación desactivada",)),
    ("límites ilegibles", ("límites sin verificar",)),
    ("inventario apagado", ("inventario no marcado como confiable",)),
    ("tope del pedido", ("supera el tope de",)),
    (
        "cliente nuevo",
        ("cliente con solo", "cliente nuevo:", "cliente sin historial"),
    ),
    ("muy por encima de su promedio", ("su promedio",)),
    ("historial ilegible", ("no se pudo verificar el historial",)),
    ("deuda vencida", ("vencidos", "no se pudo verificar la deuda")),
    (
        "sin conteo de stock",
        ("sin verificar", "nadie confirmó un conteo", "el último conteo"),
    ),
    ("sin stock", ("stock insuficiente", "no se pudo verificar stock")),
    ("zona de entrega", (entrega.MOTIVO,)),
    ("fecha de entrega", ("fecha de entrega", "fecha del pedido", "fecha del negocio")),
    ("cantidad por producto", ("por producto",)),
    ("descuento", ("descuento",)),
    (
        "lista o moneda",
        ("lista de precios", "moneda", "precio fuera de lista", "lista estándar"),
    ),
    ("pedido incompleto", ("sin productos", "cliente ausente", "total ")),
)

OTROS = "otros"


def grupo(motivo: object) -> str:
    """En qué cubeta cae un motivo. `otros` cuando no coincide con ninguna."""
    texto_motivo = str(motivo or "").lower()
    if not texto_motivo.strip():
        return OTROS
    for nombre, fragmentos in _GRUPOS:
        if any(f.lower() in texto_motivo for f in fragmentos):
            return nombre
    return OTROS


# ------------------------------------------------------------- las lecturas


def _desde(dias: int) -> datetime:
    """El corte de la ventana, en la hora del NEGOCIO.

    No en UTC: el string que se le manda a ERPNext se compara contra sus
    `creation` sin zona, que están en la hora de su propio sistema — la del
    negocio. Un corte en UTC movía la ventana el offset entero (tres horas
    acá), así que los comentarios del borde entraban o salían por error.
    """
    from app import pendientes

    return datetime.now(pendientes._zona()) - timedelta(
        days=max(1, int(dias or DIAS_DEFAULT))
    )


def _creacion(fila: dict) -> datetime | None:
    """El `creation` de un comentario, interpretado como lo hace `pendientes`.

    ERPNext guarda sin zona, en la hora de su propio sistema. `edad_horas` ya
    lo lee así para decidir la edad de un borrador; leerlo como UTC acá hacía
    que los dos módulos discreparan por el offset sobre el MISMO campo.
    """
    from app import pendientes

    crudo = str(fila.get("creation") or "").strip()
    if not crudo:
        return None
    try:
        momento = datetime.fromisoformat(crudo)
    except (TypeError, ValueError):
        return None
    if momento.tzinfo is None:
        return momento.replace(tzinfo=pendientes._zona())
    return momento


def _comentarios(marca: str, desde: datetime) -> tuple[list[dict], bool] | None:
    """(comentarios de la ventana, se llenó el techo). None si no se pudo leer.

    El filtro de fecha va también en la consulta —para no traer un año de
    historia— pero se vuelve a aplicar en Python: así el resultado es el mismo
    con o sin un ERPNext que respete el operador.
    """
    try:
        filas = erpnext.policy_get_list(
            "Comment",
            filters=[
                ["reference_doctype", "=", "Sales Order"],
                ["content", "like", f"%{marca}%"],
                ["creation", ">=", desde.strftime("%Y-%m-%d %H:%M:%S")],
            ],
            fields=["content", "reference_name", "creation"],
            limit=MAX_COMENTARIOS + 1,
            order_by="creation desc",
        )
    except Exception as exc:
        print(f"[autonomia] no pude leer {marca}: {type(exc).__name__}: {exc}")
        return None
    truncado = len(filas) > MAX_COMENTARIOS
    dentro = []
    for fila in filas[:MAX_COMENTARIOS]:
        momento = _creacion(fila)
        if momento is None or momento >= desde:
            # Un `creation` ilegible se cuenta: descartarlo bajaría el número
            # sin decir que lo bajó.
            dentro.append(fila)
    return dentro, truncado


def confirmaciones(dias: int = DIAS_DEFAULT) -> dict | None:
    """Quién confirmó cada pedido de la ventana, por origen.

    Un pedido con dos marcas cuenta una vez: se toma la más vieja, igual que
    `confirmacion._desde_erpnext`, que es la que fija el plazo de cancelación.
    """
    from app import confirmacion

    leido = _comentarios(confirmacion.MARCA, _desde(dias))
    if leido is None:
        return None
    filas, truncado = leido
    por_pedido: dict[str, str] = {}
    for fila in sorted(filas, key=lambda f: str(f.get("creation") or "")):
        pedido = str(fila.get("reference_name") or "").strip()
        if not pedido or pedido in por_pedido:
            continue
        encontrado = _FUENTE.search(str(fila.get("content") or ""))
        por_pedido[pedido] = (encontrado.group("fuente") if encontrado else "").lower()
    cuenta = {"solos": 0, "por_vos": 0, "acepto_el_cliente": 0, "sin_fuente": 0}
    for fuente in por_pedido.values():
        if "autom" in fuente:
            cuenta["solos"] += 1
        elif "manual" in fuente:
            cuenta["por_vos"] += 1
        elif "solicitud" in fuente:
            cuenta["acepto_el_cliente"] += 1
        else:
            cuenta["sin_fuente"] += 1
    cuenta["truncado"] = truncado
    cuenta["total"] = len(por_pedido)
    return cuenta


def rechazos(dias: int = DIAS_DEFAULT) -> int | None:
    """Pedidos que una persona rechazó a mano en la ventana."""
    leido = _comentarios(MARCA_RECHAZO, _desde(dias))
    if leido is None:
        return None
    filas, _truncado = leido
    return len({str(f.get("reference_name") or "") for f in filas if f.get("reference_name")})


def sombras(dias: int = DIAS_DEFAULT) -> dict | None:
    """Lo que las reglas habrían dicho, contado sobre los registros de W1.

    Los motivos vienen ya separados en lista dentro del JSON, así que estos
    frenos se agrupan sobre DATOS y no sobre prosa. Es la diferencia entre
    contar y adivinar.
    """
    from app import sombra as sombra_mod

    leido = _comentarios(sombra_mod.MARCA, _desde(dias))
    if leido is None:
        return None
    filas, truncado = leido
    vistos: dict[str, dict] = {}
    for fila in filas:  # creation desc: el primero de cada pedido es el más nuevo
        pedido = str(fila.get("reference_name") or "").strip()
        if not pedido or pedido in vistos:
            continue
        datos = sombra_mod._parsear(str(fila.get("content") or ""))
        if datos is not None:
            vistos[pedido] = datos
    pasan = 0
    postura: dict[str, int] = {}
    reglas: dict[str, int] = {}
    for datos in vistos.values():
        if datos.get("pasa_reglas"):
            pasan += 1
        for motivo in datos.get("motivos_postura") or []:
            postura[str(motivo)] = postura.get(str(motivo), 0) + 1
        # Un pedido cuenta UNA vez por cubeta aunque tenga tres motivos de la
        # misma: si no, «sin stock» sumaría uno por renglón y el número diría
        # más frenos que pedidos.
        for nombre in {grupo(m) for m in (datos.get("motivos_reglas") or [])}:
            reglas[nombre] = reglas.get(nombre, 0) + 1
    return {
        "con_registro": len(vistos),
        "pasan": pasan,
        "frenados": len(vistos) - pasan,
        "postura": postura,
        "reglas": reglas,
        "truncado": truncado,
    }


def revisiones(dias: int = DIAS_DEFAULT) -> dict | None:
    """Los frenos que dejó `_after_create`, agrupados por fragmentos.

    Prosa interpolada: menos preciso que `sombras`, y la única fuente cuando el
    modo sombra está apagado.

    OJO, y es un bug abierto: esto NO excluye los pedidos que sí tienen
    registro de sombra, y `texto` elige una fuente O la otra con un `or`. Así,
    un solo freno de sombra en la ventana tapa TODOS los frenos que sólo
    figuran acá. El desglose de motivos todavía no es confiable para decidir
    subir un límite; los conteos de confirmados, rechazos y sombra sí lo son.
    Se arregla en el PR siguiente, antes de que haya datos de sombra que mirar.
    """
    leido = _comentarios(MARCA_REVISION, _desde(dias))
    if leido is None:
        return None
    filas, truncado = leido
    por_pedido: dict[str, str] = {}
    for fila in filas:
        pedido = str(fila.get("reference_name") or "").strip()
        if pedido and pedido not in por_pedido:
            por_pedido[pedido] = str(fila.get("content") or "")
    grupos: dict[str, int] = {}
    for contenido in por_pedido.values():
        cuerpo = contenido.split(MARCA_REVISION, 1)[-1]
        for nombre in {grupo(m) for m in cuerpo.split(";") if m.strip()}:
            grupos[nombre] = grupos.get(nombre, 0) + 1
    return {"pedidos": len(por_pedido), "grupos": grupos, "truncado": truncado}


def borradores_vivos() -> dict | None:
    """Cuántos borradores compiten por stock, contra el techo de `policy`.

    Se cuenta contando, no interpretando motivos. Pasado el techo, `policy`
    levanta y NADA se auto-confirma: es la falla más grande que este sistema
    puede tener y hoy no se ve en ninguna parte hasta que ya pasó.

    Incluye los cargados a mano: `policy._borradores_que_reservan` no filtra por
    origen, así que ocupan lugar en el mismo techo.
    """
    from app import pendientes

    try:
        filas = pendientes.listar_esperando(limite=policy.MAX_BORRADORES + 1)
    except Exception as exc:
        print(f"[autonomia] no pude contar los borradores: {type(exc).__name__}: {exc}")
        return None
    vivos = len(filas)
    del_bot = sum(1 for f in filas if pendientes.del_agente(f))
    return {
        "vivos": vivos,
        "del_bot": del_bot,
        "a_mano": vivos - del_bot,
        "tope": policy.MAX_BORRADORES,
        "pasado": vivos > policy.MAX_BORRADORES,
        "pct": (vivos * 100.0 / policy.MAX_BORRADORES) if policy.MAX_BORRADORES else 0.0,
    }


def conteos(dias: int = DIAS_DEFAULT) -> dict | None:
    """Frescura de los conteos sobre los productos que se pidieron en la ventana.

    «Productos clave» no está configurado en ninguna parte, así que se define
    por lo que el negocio efectivamente vendió: los que aparecieron en pedidos.
    Un producto que nadie pidió no es un conteo que falte.
    """
    from app import inventario

    desde = _desde(dias)
    try:
        renglones = erpnext.policy_get_list(
            "Sales Order Item",
            filters=[["creation", ">=", desde.strftime("%Y-%m-%d %H:%M:%S")]],
            fields=["item_code", "warehouse", "creation"],
            limit=MAX_COMENTARIOS,
            parent="Sales Order",
            order_by="creation desc",
        )
    except Exception as exc:
        print(f"[autonomia] no pude leer los productos pedidos: {type(exc).__name__}")
        return None
    pares: list[tuple[str, str]] = []
    for fila in renglones:
        code = str(fila.get("item_code") or "").strip()
        deposito = str(fila.get("warehouse") or "").strip()
        if code and deposito and (code, deposito) not in pares:
            pares.append((code, deposito))
        if len(pares) >= MAX_PRODUCTOS:
            break
    if not pares:
        return {"mirados": 0, "frescos": 0, "faltan": []}
    frescos = 0
    faltan: list[str] = []
    for code, deposito in pares:
        try:
            fresco, _motivo = inventario.confiable(code, deposito)
        except Exception:
            faltan.append(code)
            continue
        if fresco:
            frescos += 1
        else:
            faltan.append(code)
    return {"mirados": len(pares), "frescos": frescos, "faltan": faltan[:10]}


# ------------------------------------------------------------- el resumen


def resumen(dias: int = DIAS_DEFAULT) -> dict:
    """Todos los números de la ventana. Cada uno puede faltar por separado.

    Nunca levanta: una sección que no se pudo leer vale None y el texto lo dice.
    """
    return {
        "dias": max(1, int(dias or DIAS_DEFAULT)),
        "confirmaciones": confirmaciones(dias),
        "rechazos": rechazos(dias),
        "sombras": sombras(dias),
        "revisiones": revisiones(dias),
        "borradores": borradores_vivos(),
        "conteos": conteos(dias),
    }


def _linea_grupos(grupos: dict[str, int] | None) -> str:
    if not grupos:
        return ""
    ordenados = sorted(grupos.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{nombre} {cuenta}" for nombre, cuenta in ordenados[:6])


def texto(datos: dict, lengua: str | None = None) -> str:
    """El resumen como lo lee el dueño. Constructor puro: sin reloj y sin red."""
    conf = datos.get("confirmaciones")
    som = datos.get("sombras")
    rev = datos.get("revisiones")
    bor = datos.get("borradores")
    cue = datos.get("conteos")
    ilegible = idioma.t("gerencia.autonomia_ilegible", lengua)

    def numero(valor: object) -> str:
        return ilegible if valor is None else str(valor)

    frenos = _linea_grupos((som or {}).get("reglas")) or _linea_grupos(
        (rev or {}).get("grupos")
    )
    cuerpo = idioma.t(
        "gerencia.autonomia",
        lengua,
        dias=datos.get("dias", DIAS_DEFAULT),
        confirmados=numero(None if conf is None else conf.get("total")),
        solos=numero(None if conf is None else conf.get("solos")),
        por_vos=numero(None if conf is None else conf.get("por_vos")),
        acepto=numero(None if conf is None else conf.get("acepto_el_cliente")),
        rechazados=numero(datos.get("rechazos")),
        sombra_pasan=numero(None if som is None else som.get("pasan")),
        sombra_frenados=numero(None if som is None else som.get("frenados")),
        postura=_linea_grupos((som or {}).get("postura")) or "—",
        frenos=frenos or "—",
        borradores=numero(None if bor is None else bor.get("vivos")),
        tope=numero(None if bor is None else bor.get("tope")),
        conteos=(
            ilegible
            if cue is None
            else f"{cue.get('frescos')} de {cue.get('mirados')}"
        ),
        falto=", ".join((cue or {}).get("faltan") or []) or "—",
    )
    # Un techo de lectura lleno significa que estos números son un PISO, no el
    # total. Decirlo es la diferencia entre un número y un número engañoso.
    if any((fuente or {}).get("truncado") for fuente in (conf, som, rev)):
        cuerpo += "\n" + idioma.t("gerencia.autonomia_truncado", lengua)
    return cuerpo


# ------------------------------------------------------------- la escalera


NIVELES = ("0 — lanzamiento", "1 — sombra", "2 — lo habitual", "3 — clientes conocidos")


def nivel(cfg: object, datos: dict) -> dict:
    """En qué escalón está la CONFIGURACIÓN, y qué dicen los números.

    Determinista y en un solo lugar, para que el resumen de las 18:00 y la
    herramienta de gerencia no puedan contestar cosas distintas.

    No propone nada: decir «subilo a 25 mil» es una frase del dueño, no del
    sistema. Devuelve el escalón y los hechos.
    """
    from app import inventario

    tope = float(getattr(cfg, "tope", 0) or 0)
    stock = inventario.maestra_encendida()
    som = datos.get("sombras") or {}
    sombra_encendida = bool(som.get("con_registro"))

    if tope <= 0:
        actual = NIVELES[1] if sombra_encendida else NIVELES[0]
    elif not stock:
        # Un tope sin stock confiable no auto-confirma nada: la postura sigue
        # siendo de lanzamiento aunque el número esté puesto.
        actual = NIVELES[0]
    else:
        actual = NIVELES[2]
    return {
        "nivel": actual,
        "tope": tope,
        "stock_confiable": stock,
        "sombra_encendida": sombra_encendida,
        "habrian_pasado": som.get("pasan"),
        "con_registro": som.get("con_registro"),
    }


def main(argv: list[str] | None = None) -> int:
    """`python -m app.autonomia` — reemplaza el grep de `make decisiones`."""
    import app  # noqa: F401  (carga .env)

    argumentos = list(argv or [])
    dias = DIAS_DEFAULT
    for arg in argumentos:
        if arg.startswith("--dias="):
            try:
                dias = int(arg.split("=", 1)[1])
            except ValueError:
                print(f"[autonomia] {arg}: días inválidos; uso {DIAS_DEFAULT}")
    datos = resumen(dias)
    print(texto(datos, idioma.gerencia()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(main(sys.argv[1:]))
