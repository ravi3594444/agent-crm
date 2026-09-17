"""Los permisos de ERPNext que las tres identidades necesitan para LEER.

SCRIPT DE DESPLIEGUE, NO UNA HERRAMIENTA. Igual que `deploy/cuentas_inventario.py`
y `deploy/seed_dairy.py`: lo corre una persona con credenciales de Administrator
pasadas POR PROCESO, no cuelga de ningún tool del agente y no lo puede invocar un
mensaje de WhatsApp. Nadie lo cablee después a `app/tools/`.

Se corre DESDE `plus-agent/`, en el host y con el venv del proyecto — igual que
`make seed`. La imagen de producción sólo copia `app/`, así que dentro del
contenedor no existe `/srv/deploy`.

    ERPNEXT_URL=https://tu-erpnext \
    ERPNEXT_ADMIN_API_KEY=... ERPNEXT_ADMIN_API_SECRET=... \
        .venv/bin/python deploy/permisos_erpnext.py             # mira, no escribe
    ... --aplicar                                               # otorga lo que falte
    ... --probar                                                # lo verifica de verdad

`ERPNEXT_URL` va en la línea de comandos a propósito: el `.env` dice
`http://frontend:8080`, que sólo resuelve ADENTRO de la red de docker, y desde el
host todo script muere con «Temporary failure in name resolution».
`app/__init__.py` hace `load_dotenv(override=False)`, así que lo que pasás por
proceso gana.

POR QUÉ LAS CREDENCIALES DE ADMIN SE LLAMAN DISTINTO. `ERPNEXT_API_KEY` es la
identidad del AGENTE DE CLIENTES y está en el `.env`. Si le pasáramos la de
Administrator con ese mismo nombre, `--probar` estaría comprobando el permiso de
Administrator y diciendo que el agente puede leer — que es exactamente la
pregunta que vino a contestar. Con nombres distintos, el probe usa las claves
reales de cada identidad y no hay forma de confundirlas. Si no están las
`ERPNEXT_ADMIN_*` se cae a `ERPNEXT_API_KEY` por compatibilidad con los otros
scripts, y entonces `--probar` avisa y no corre.

EL PROBLEMA QUE ARREGLA
Medido en vivo el 17/09, tres 403 seguidos, cada uno descubierto sólo cuando un
pedido real falló:

    [erpnext] la creación de Sales Order: rechazado 403 — User agente-ia@… does
      not have doctype access via role permission for document Account
    [erpnext] el reporte Accounts Receivable: rechazado 403 — You don't have
      access to Report: Accounts Receivable
    [erpnext] la confirmación de Sales Order: rechazado 403 — User politica-ia@…
      does not have doctype access via role permission for document Account

ERPNext toca `Account` al VALIDAR un Sales Order (cuenta de ingreso, cuentas de
impuestos), así que una identidad que puede crear el documento igual no puede
guardarlo. Y `policy._saldo_vencido` corre el reporte Accounts Receivable, que
tiene su propio permiso aparte del de la doctype.

Ninguno de los tres se ve hasta que un cliente pide algo. Una instalación nueva
los encuentra de a uno, en producción, con el dueño mirando.

QUÉ OTORGA, Y QUÉ NO
Sólo LECTURA. `read` sobre `Account`, y acceso al reporte Accounts Receivable.
Nada de write, create, delete ni submit: la regla de las tres identidades dice
que cliente y gerencia leen y crean borradores, y que política es la única que
confirma — y eso lo dan los permisos que YA tiene cada rol, no éste. Si alguna
vez este archivo otorga un `submit`, está mal.

Los roles NO se escriben a mano acá: se le pregunta a ERPNext quién es cada
credencial (`frappe.auth.get_logged_user`) y después qué roles tiene. Un rol
hardcodeado sería una cuarta copia de la regla de las tres identidades, y la que
se desincroniza sin que nada se ponga rojo.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from app import erpnext

# La doctype que ERPNext lee al validar un Sales Order. Las tres identidades la
# necesitan: cliente y gerencia para CREAR el borrador, política para
# confirmarlo. Las tres con `read` y nada más.
DOCTYPE_CUENTA = "Account"

# El reporte del que sale la deuda vencida (`policy._saldo_vencido`). Sólo lo
# corre la identidad de política.
REPORTE_DEUDA = "Accounts Receivable"

# Las tres identidades, con el par de variables de entorno de cada una. El orden
# es el de `CLAUDE.md`, para que el informe se lea igual que la tabla.
IDENTIDADES: tuple[tuple[str, str, str], ...] = (
    ("cliente", "ERPNEXT_API_KEY", "ERPNEXT_API_SECRET"),
    ("gerencia", "ERPNEXT_MANAGER_API_KEY", "ERPNEXT_MANAGER_API_SECRET"),
    ("politica", "ERPNEXT_POLICY_API_KEY", "ERPNEXT_POLICY_API_SECRET"),
)

# Qué identidad necesita el reporte. Sólo política: `_saldo_vencido` usa
# `policy_run_report`, y ninguna herramienta de cliente ni de gerencia lo corre.
IDENTIDADES_DEL_REPORTE = frozenset({"politica"})


class Interrumpir(Exception):
    """Algo que el script no puede decidir solo. El mensaje es para una persona."""


@dataclass
class Identidad:
    """Una de las tres credenciales, ya resuelta contra ERPNext."""

    nombre: str
    var_clave: str
    var_secreto: str
    usuario: str = ""
    roles: tuple[str, ...] = ()
    problema: str = ""

    @property
    def utilizable(self) -> bool:
        return bool(self.usuario and self.roles and not self.problema)


@dataclass
class Paso:
    """Un permiso que falta, o que ya está. Decidido antes de escribir nada."""

    rol: str
    objeto: str
    detalle: str
    hace_falta: bool
    identidades: list[str] = field(default_factory=list)


def _credenciales_admin() -> tuple[str, str, bool]:
    """(clave, secreto, son_las_propias). Ver el docstring del módulo."""
    clave = os.environ.get("ERPNEXT_ADMIN_API_KEY", "").strip()
    secreto = os.environ.get("ERPNEXT_ADMIN_API_SECRET", "").strip()
    if clave and secreto:
        return clave, secreto, True
    clave = os.environ.get("ERPNEXT_API_KEY", "").strip()
    secreto = os.environ.get("ERPNEXT_API_SECRET", "").strip()
    if not clave or not secreto:
        raise Interrumpir(
            "faltan las credenciales de Administrator: pasá "
            "ERPNEXT_ADMIN_API_KEY y ERPNEXT_ADMIN_API_SECRET por proceso"
        )
    return clave, secreto, False


def pedir(
    metodo: str,
    ruta: str,
    *,
    clave: str,
    secreto: str,
    payload: dict | None = None,
    params: dict | None = None,
) -> dict:
    """Un pedido a ERPNext con las credenciales que le pasen.

    `app/erpnext.py` no expone un request genérico NI DEBE EXPONERLO: cada
    escritura de ahí es angosta a propósito. Este script corre con
    Administrator, que es otra cosa, así que arma su propio pedido y no ensancha
    nada de lo que usa el runtime.

    EL MOTIVO DEL SERVIDOR VIENE EN EL ERROR, y no es un detalle: el 417 del
    Stock Reconciliation costó semanas justamente porque `app/erpnext.py` se
    queda con el status y tira el cuerpo. Acá no hay ningún modelo leyendo la
    excepción —la lee una persona parada frente a la terminal—, así que el
    cuerpo va entero.
    """
    url = os.environ.get("ERPNEXT_URL", "").strip().rstrip("/")
    if not url:
        raise Interrumpir("falta ERPNEXT_URL")
    try:
        respuesta = httpx.request(
            metodo,
            f"{url}{ruta}",
            headers={
                "Authorization": f"token {clave}:{secreto}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
            params=params,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise erpnext.ERPNextError(
            f"ERPNext no disponible durante {metodo} {ruta}"
        ) from exc
    if respuesta.status_code >= 400:
        raise erpnext.ERPNextError(
            f"ERPNext rechazó {metodo} {ruta} (estado {respuesta.status_code})"
            f" — {_motivo(respuesta)}"
        )
    try:
        cuerpo = respuesta.json()
    except ValueError:
        return {}
    return cuerpo if isinstance(cuerpo, dict) else {}


def _motivo(respuesta: httpx.Response) -> str:
    """El texto que ERPNext puso en el cuerpo, aplanado. Vacío si no hay."""
    try:
        cuerpo = respuesta.json()
    except ValueError:
        return (respuesta.text or "").strip()[:400] or "sin cuerpo"
    if not isinstance(cuerpo, dict):
        return str(cuerpo)[:400]
    for campo in ("_server_messages", "exception", "message", "exc"):
        crudo = cuerpo.get(campo)
        if not crudo:
            continue
        if campo == "_server_messages":
            try:
                mensajes = json.loads(crudo)
            except (TypeError, ValueError):
                return str(crudo)[:400]
            partes = []
            for mensaje in mensajes if isinstance(mensajes, list) else [mensajes]:
                try:
                    dato = json.loads(mensaje)
                except (TypeError, ValueError):
                    partes.append(str(mensaje))
                    continue
                partes.append(
                    str(dato.get("message") or dato) if isinstance(dato, dict) else str(dato)
                )
            if partes:
                return " | ".join(partes)[:400]
        else:
            return str(crudo)[:400]
    return "sin motivo en el cuerpo"


def listar(
    doctype: str, *, clave: str, secreto: str, filtros: list, campos: list[str]
) -> list[dict]:
    """Un get_list con las credenciales dadas. Devuelve [] si no hay nada."""
    cuerpo = pedir(
        "GET",
        f"/api/resource/{quote(doctype, safe='')}",
        clave=clave,
        secreto=secreto,
        params={
            "filters": json.dumps(filtros),
            "fields": json.dumps(campos),
            "limit_page_length": 0,
        },
    )
    datos = cuerpo.get("data")
    return [fila for fila in datos if isinstance(fila, dict)] if isinstance(datos, list) else []


def resolver_identidad(
    identidad: Identidad, *, clave: str, secreto: str
) -> Identidad:
    """Quién es esa credencial y qué roles tiene. Nunca levanta: deja `problema`.

    Una credencial que falta NO es un error del script: un despliegue chico
    puede no tener todavía la de gerencia. Se informa y se sigue con las otras.
    """
    mi_clave = os.environ.get(identidad.var_clave, "").strip()
    mi_secreto = os.environ.get(identidad.var_secreto, "").strip()
    if not mi_clave or not mi_secreto:
        identidad.problema = f"faltan {identidad.var_clave} / {identidad.var_secreto}"
        return identidad
    try:
        cuerpo = pedir(
            "GET",
            "/api/method/frappe.auth.get_logged_user",
            clave=mi_clave,
            secreto=mi_secreto,
        )
    except erpnext.ERPNextError as exc:
        identidad.problema = f"no pude identificarla: {exc}"
        return identidad
    usuario = str(cuerpo.get("message") or "").strip()
    if not usuario:
        identidad.problema = "ERPNext no dijo qué usuario es esa credencial"
        return identidad
    identidad.usuario = usuario
    # Los roles se leen con ADMIN, no con la credencial: `readiness` ya documenta
    # que una identidad angosta puede no poder leer su propia tabla de roles, y
    # entonces «0 roles» significaría «no pude mirar» y no «no tiene».
    filas = listar(
        "Has Role",
        clave=clave,
        secreto=secreto,
        filtros=[["parent", "=", usuario], ["parenttype", "=", "User"]],
        campos=["role"],
    )
    roles = tuple(
        sorted({str(f.get("role") or "").strip() for f in filas if f.get("role")})
    )
    if not roles:
        identidad.problema = f"{usuario} no tiene ningún rol"
        return identidad
    identidad.roles = roles
    return identidad


def tiene_permiso(
    doctype: str, rol: str, ptype: str, *, clave: str, secreto: str
) -> bool:
    """¿Ese rol tiene ese permiso sobre esa doctype, permlevel 0?

    LA REGLA DE FRAPPE, NO UNA APROXIMACIÓN: si existe UN SOLO `Custom DocPerm`
    para la doctype, los `DocPerm` estándar dejan de contar por completo. Mirar
    las dos tablas y quedarse con la unión diría que hay permiso donde no lo
    hay, y este script se usa justamente para creerle.
    """
    customs = listar(
        "Custom DocPerm",
        clave=clave,
        secreto=secreto,
        filtros=[["parent", "=", doctype]],
        campos=["role", "permlevel", ptype],
    )
    tabla = customs or listar(
        "DocPerm",
        clave=clave,
        secreto=secreto,
        filtros=[["parent", "=", doctype]],
        campos=["role", "permlevel", ptype],
    )
    for fila in tabla:
        if str(fila.get("role") or "").strip() != rol:
            continue
        if int(fila.get("permlevel") or 0) != 0:
            continue
        if int(fila.get(ptype) or 0) == 1:
            return True
    return False


def rol_del_reporte(reporte: str, rol: str, *, clave: str, secreto: str) -> bool:
    """¿Ese rol puede correr ese reporte?

    `Report.is_permitted()` junta los `Has Role` colgados del reporte con los del
    `Custom Role` que lo referencia, y deja pasar a cualquiera si la lista queda
    VACÍA. Esa última parte importa: en un ERPNext sin restringir el reporte
    puede estar abierto, y entonces no falta nada que otorgar.
    """
    propios = listar(
        "Has Role",
        clave=clave,
        secreto=secreto,
        filtros=[["parent", "=", reporte], ["parenttype", "=", "Report"]],
        campos=["role"],
    )
    permitidos = {str(f.get("role") or "").strip() for f in propios if f.get("role")}
    custom = listar(
        "Custom Role",
        clave=clave,
        secreto=secreto,
        filtros=[["report", "=", reporte]],
        campos=["name"],
    )
    for fila in custom:
        nombre = str(fila.get("name") or "").strip()
        if not nombre:
            continue
        hijos = listar(
            "Has Role",
            clave=clave,
            secreto=secreto,
            filtros=[["parent", "=", nombre], ["parenttype", "=", "Custom Role"]],
            campos=["role"],
        )
        permitidos |= {str(f.get("role") or "").strip() for f in hijos if f.get("role")}
    permitidos.discard("")
    # Sin nadie en la lista, el reporte es de todos: no falta nada.
    return (not permitidos) or rol in permitidos


def otorgar_lectura(doctype: str, rol: str, *, clave: str, secreto: str) -> str:
    """`read` sobre la doctype para ese rol. Devuelve qué hizo, en una línea.

    `add_permission` VUELVE SIN HACER NADA si ya existe una fila de Custom
    DocPerm para ese (doctype, rol, permlevel), así que no sirve para agregar un
    segundo derecho a una fila que ya está — para eso está
    `update_permission_property`. Se llaman los dos, en ese orden: el primero
    crea la fila cuando no hay, el segundo se asegura del bit. Correr los dos es
    idempotente y es la única forma de que el resultado no dependa de si la fila
    existía.
    """
    pedir(
        "POST",
        "/api/method/frappe.permissions.add_permission",
        clave=clave,
        secreto=secreto,
        payload={"doctype": doctype, "role": rol, "permlevel": 0, "ptype": "read"},
    )
    pedir(
        "POST",
        "/api/method/frappe.permissions.update_permission_property",
        clave=clave,
        secreto=secreto,
        payload={
            "doctype": doctype,
            "role": rol,
            "permlevel": 0,
            "ptype": "read",
            "value": 1,
        },
    )
    return f"otorgado: leer {doctype} para «{rol}»"


def otorgar_reporte(reporte: str, rol: str, *, clave: str, secreto: str) -> str:
    """Acceso al reporte para ese rol, vía `Custom Role`.

    `Custom Role` es el punto de extensión que Frappe tiene para esto: los
    `Has Role` que cuelgan del Report son los que trae la app y una migración
    los pisa. Si ya hay un Custom Role para el reporte se le agrega la fila; si
    no, se crea. `ref_doctype` sale del Report, no de una constante: es «Sales
    Invoice» para Accounts Receivable, pero eso es un dato de ERPNext y no algo
    que este archivo tenga por qué saber.
    """
    doc = pedir(
        "GET",
        f"/api/resource/Report/{quote(reporte, safe='')}",
        clave=clave,
        secreto=secreto,
    ).get("data")
    ref = str((doc or {}).get("ref_doctype") or "").strip()
    if not ref:
        raise Interrumpir(f"el reporte {reporte} no tiene ref_doctype; miralo a mano")

    existentes = listar(
        "Custom Role",
        clave=clave,
        secreto=secreto,
        filtros=[["report", "=", reporte]],
        campos=["name"],
    )
    if existentes:
        nombre = str(existentes[0].get("name") or "").strip()
        actual = pedir(
            "GET",
            f"/api/resource/Custom%20Role/{quote(nombre, safe='')}",
            clave=clave,
            secreto=secreto,
        ).get("data") or {}
        roles = [r for r in (actual.get("roles") or []) if isinstance(r, dict)]
        roles.append({"doctype": "Has Role", "role": rol})
        pedir(
            "PUT",
            f"/api/resource/Custom%20Role/{quote(nombre, safe='')}",
            clave=clave,
            secreto=secreto,
            payload={"roles": roles},
        )
        return f"otorgado: reporte {reporte} para «{rol}» (Custom Role {nombre})"

    pedir(
        "POST",
        "/api/resource/Custom%20Role",
        clave=clave,
        secreto=secreto,
        payload={
            "doctype": "Custom Role",
            "report": reporte,
            "ref_doctype": ref,
            "roles": [{"doctype": "Has Role", "role": rol}],
        },
    )
    return f"otorgado: reporte {reporte} para «{rol}» (Custom Role nuevo)"


def planear(identidades: list[Identidad], *, clave: str, secreto: str) -> list[Paso]:
    """Qué falta, sin escribir nada.

    Un rol puede ser el mismo para dos identidades —nada lo impide— así que los
    pasos se agrupan POR ROL y se anota cuáles identidades lo pedían. Otorgar
    dos veces lo mismo no rompe nada, pero el informe sí mentiría.
    """
    por_rol: dict[tuple[str, str], Paso] = {}

    def anotar(rol: str, objeto: str, detalle: str, falta: bool, quien: str) -> None:
        paso = por_rol.get((rol, objeto))
        if paso is None:
            paso = Paso(rol=rol, objeto=objeto, detalle=detalle, hace_falta=falta)
            por_rol[(rol, objeto)] = paso
        if quien not in paso.identidades:
            paso.identidades.append(quien)

    for identidad in identidades:
        if not identidad.utilizable:
            continue
        for rol in identidad.roles:
            falta = not tiene_permiso(
                DOCTYPE_CUENTA, rol, "read", clave=clave, secreto=secreto
            )
            anotar(rol, DOCTYPE_CUENTA, f"leer {DOCTYPE_CUENTA}", falta, identidad.nombre)
        if identidad.nombre in IDENTIDADES_DEL_REPORTE:
            for rol in identidad.roles:
                falta = not rol_del_reporte(
                    REPORTE_DEUDA, rol, clave=clave, secreto=secreto
                )
                anotar(
                    rol, REPORTE_DEUDA, f"correr el reporte {REPORTE_DEUDA}", falta,
                    identidad.nombre,
                )
    return sorted(por_rol.values(), key=lambda p: (p.objeto, p.rol))


def probar(identidades: list[Identidad], propias: bool) -> list[str]:
    """La comprobación de verdad: cada identidad LEE con SUS PROPIAS claves.

    Un informe que dice «el permiso está» mirando la tabla de permisos con
    Administrator es el mismo defecto que `readiness` ya documenta —medir con la
    credencial equivocada— y acá sería peor, porque Administrator puede todo.
    """
    if not propias:
        return [
            "probar: NO corrido. Pasaste las credenciales de admin como "
            "ERPNEXT_API_KEY, que es también la del agente de clientes: el probe "
            "estaría midiendo a Administrator. Usá ERPNEXT_ADMIN_API_KEY."
        ]
    lineas = []
    for identidad in identidades:
        if not identidad.utilizable:
            lineas.append(f"probar {identidad.nombre}: salteada ({identidad.problema})")
            continue
        mi_clave = os.environ[identidad.var_clave].strip()
        mi_secreto = os.environ[identidad.var_secreto].strip()
        try:
            listar(
                DOCTYPE_CUENTA,
                clave=mi_clave,
                secreto=mi_secreto,
                filtros=[["disabled", "=", 0]],
                campos=["name"],
            )
            lineas.append(f"probar {identidad.nombre}: lee {DOCTYPE_CUENTA} ✓")
        except erpnext.ERPNextError as exc:
            lineas.append(f"probar {identidad.nombre}: NO lee {DOCTYPE_CUENTA} — {exc}")
        if identidad.nombre not in IDENTIDADES_DEL_REPORTE:
            continue
        try:
            pedir(
                "GET",
                "/api/method/frappe.desk.query_report.run",
                clave=mi_clave,
                secreto=mi_secreto,
                params={"report_name": REPORTE_DEUDA, "ignore_prepared_report": 1},
            )
            lineas.append(f"probar {identidad.nombre}: corre {REPORTE_DEUDA} ✓")
        except erpnext.ERPNextError as exc:
            lineas.append(
                f"probar {identidad.nombre}: NO corre {REPORTE_DEUDA} — {exc}"
            )
    return lineas


def main(argv: list[str] | None = None) -> int:
    opciones = _opciones(argv if argv is not None else sys.argv[1:])
    try:
        clave, secreto, propias = _credenciales_admin()
    except Interrumpir as exc:
        print(f"ERROR: {exc}")
        return 2

    identidades = [
        resolver_identidad(Identidad(nombre, var_c, var_s), clave=clave, secreto=secreto)
        for nombre, var_c, var_s in IDENTIDADES
    ]
    print("Identidades")
    for identidad in identidades:
        if identidad.utilizable:
            print(
                f"  {identidad.nombre:9} {identidad.usuario} "
                f"— {', '.join(identidad.roles)}"
            )
        else:
            print(f"  {identidad.nombre:9} NO SE PUDO: {identidad.problema}")

    if not any(i.utilizable for i in identidades):
        print("\nNinguna identidad se pudo resolver; no hay nada que planear.")
        return 2

    try:
        pasos = planear(identidades, clave=clave, secreto=secreto)
    except (erpnext.ERPNextError, Interrumpir) as exc:
        print(f"\nERROR al revisar los permisos: {exc}")
        return 2

    print("\nPermisos")
    faltan = [p for p in pasos if p.hace_falta]
    for paso in pasos:
        estado = "FALTA " if paso.hace_falta else "ya está"
        quien = ", ".join(paso.identidades)
        print(f"  {estado} «{paso.rol}»: {paso.detalle}  ({quien})")

    if not faltan:
        print("\nNo falta ninguno.")
    elif not opciones.aplicar:
        print(f"\n{len(faltan)} permiso(s) por otorgar. Volvé a correrlo con --aplicar.")
    else:
        print()
        for paso in faltan:
            try:
                if paso.objeto == DOCTYPE_CUENTA:
                    print(
                        "  "
                        + otorgar_lectura(paso.objeto, paso.rol, clave=clave, secreto=secreto)
                    )
                else:
                    print(
                        "  "
                        + otorgar_reporte(paso.objeto, paso.rol, clave=clave, secreto=secreto)
                    )
            except (erpnext.ERPNextError, Interrumpir) as exc:
                print(f"  NO PUDE otorgar {paso.detalle} a «{paso.rol}»: {exc}")
                return 2
        # Frappe cachea los permisos por sitio. Sin esto el agente sigue viendo
        # 403 con el permiso ya escrito, que es indistinguible de no haberlo
        # escrito — y es media hora de mirar la tabla equivocada.
        print(
            "\nAHORA LIMPIÁ LA CACHÉ o los permisos nuevos no se ven:\n"
            "  docker compose --project-name erpnext -f ~/gitops/erpnext.yml "
            "exec backend bench --site <tu-sitio> clear-cache"
        )

    if opciones.probar:
        print()
        for linea in probar(identidades, propias):
            print(f"  {linea}")

    return 0


def _opciones(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Otorga los permisos de LECTURA que las tres identidades necesitan "
            "en ERPNext. Sin --aplicar sólo mira."
        )
    )
    parser.add_argument("--aplicar", action="store_true", help="escribir de verdad")
    parser.add_argument(
        "--probar",
        action="store_true",
        help="después de mirar, leer con las claves de cada identidad",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
