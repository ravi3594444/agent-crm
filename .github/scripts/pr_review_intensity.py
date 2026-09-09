#!/usr/bin/env python3
"""Decide cuánto esfuerzo merece la revisión de UN pull request.

Una sola intensidad fija está mal en las dos direcciones a la vez. Revisar un
README de dos líneas con el modelo más caro es pagar de más en cada push; y
revisar `app/router.py` —cuyo propio docstring dice que ese split «is a SECURITY
boundary, not a convenience»— con el más barato es PEOR que no revisarlo, porque
un tilde verde que no miró nada se lee exactamente igual que uno que miró en
serio. Así que el costo de la revisión sale de lo que el PR toca de verdad,
antes de arrancar ningún modelo.

QUÉ VARÍA REALMENTE

La revisión es el comando `code-review` del plugin, y su procedimiento es fijo:
lanza sus propios agentes y valida sus propios hallazgos. La intensidad tiene
que venir de AFUERA, y acá afuera sólo hay cuatro cosas que cambian el
resultado de verdad:

  1. si la revisión corre o no
  2. si los hallazgos se comentan en el PR o sólo se imprimen en el log
  3. qué modelo la orquesta
  4. cuánto tiempo tiene

más una directiva que se le agrega al comando, y que mueve el listón de
confianza y el alcance en vez de pretender redefinir el procedimiento. Esa
distinción ES el diseño. Decirle «usá ocho agentes en vez de cuatro»
contradice su propio texto y no compra nada confiable; decirle «en esta
superficie un defecto de autorización entra aunque confirmarlo pida contexto
fuera del diff» entra en un hueco que el comando ya deja abierto.

A PROPÓSITO NO ES una palanca: `--max-turns`. Recortar los turnos no compra una
revisión más barata, compra una que se corta por la mitad e informa el pedazo
que alcanzó a mirar — o sea, un resultado limpio que no lo es. Es la misma forma
del skip silencioso que ya costó una corrida entera de CI en rojo habiendo
corrido CERO tests. El techo de costo es el timeout del job, que falla fuerte.

CÓMO SE ELIGE EL NIVEL

Gana la PRIMERA regla que matchea; no hay puntaje. Es a propósito: cuando
alguien pregunta por qué su PR cayó en el nivel que cayó, una lista de
precedencia contesta en un renglón («toca app/main.py»), y un total de puntos
contesta «47, y el umbral era 40» — que no explica nada y encima invita a
tocar los umbrales para siempre.

CI no linta ni colecta este archivo: el job `lint` corre `ruff check app demo
tests` con working-directory `plus-agent`, y `testpaths = tests` en
`plus-agent/pytest.ini` deja la colección adentro de `plus-agent/tests/`. Nada
de `.github/` entra por ninguno de los dos. `--selftest` es el reemplazo, y el
workflow lo corre en cada PR antes de dejar que el clasificador decida nada:
un guard que no se puede romper no está guardando (#9, #18).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from fnmatch import fnmatchcase

# ---------------------------------------------------------------------------
# Las tablas que hay que editar cuando cambia la forma del repo.
#
# Los patrones matchean el path del archivo cambiado, estilo POSIX y relativo a
# la raíz del repo. Tres formas, y la diferencia entre las dos últimas importa:
#
#   "dir/"        prefijo de directorio
#   "*.log"       glob; sin "/" Y con comodín, así que también matchea el nombre
#                 pelado y por eso agarra a cualquier profundidad
#   "start.sh"    un path literal, matcheado entero y sólo entero
#
# Un literal A PROPÓSITO no cae al nombre del archivo. Antes sí, y entonces
# "start.sh" se llevaba puesto a cualquier "start.sh" anidado, que es otro
# archivo con otras consecuencias. Cuando un literal se quiere de verdad a
# cualquier profundidad, se escribe con comodín ("*package-lock.json") y queda
# dicho.
# ---------------------------------------------------------------------------

# Donde un defecto es caro, silencioso, o sale del PR que lo trajo. Cada entrada
# tiene un motivo concreto, no una intuición.
CRITICO = (
    # El webhook de Meta: firma HMAC, límite de tamaño, idempotencia y la cola
    # durable con lease. Es la única puerta por la que entra texto de afuera.
    "plus-agent/app/main.py",
    "plus-agent/app/whatsapp.py",
    # El límite de privilegio. Su docstring lo dice mejor que este comentario:
    # si el bot de clientes tuviera lectura completa, UNA inyección le entrega a
    # un desconocido la lista de clientes, los márgenes y los precios de
    # proveedor. `telefono.py` es la mitad silenciosa del mismo límite: un bug
    # ahí convierte al dueño en cliente, o al revés, sin que nada avise.
    "plus-agent/app/router.py",
    "plus-agent/app/telefono.py",
    "plus-agent/app/aprobacion.py",
    # El split TOOLS_CLIENTES / TOOLS_GERENCIA («NEVER in TOOLS_CLIENTES»
    # aparece tres veces en sus comentarios), las tres identidades de ERPNext, y
    # la evaluación fail-closed del auto-confirm bajo lock distribuido. Un
    # defecto acá manda un pedido con nadie mirando.
    "plus-agent/app/graph.py",
    "plus-agent/app/erpnext.py",
    "plus-agent/app/policy.py",
    "plus-agent/app/locks.py",
    # Qué significa «verde». Este repo ya tuvo CI en rojo habiendo corrido CERO
    # tests, y el rojo se leía como «falla un test» (ver #13, #28).
    ".github/workflows/",
    # El aislamiento de los tests. Una regresión acá reabre el bug que este
    # archivo documenta en su propio docstring: la suite corriendo contra el
    # .env REAL y «pasando» por eso.
    "plus-agent/tests/conftest.py",
    # Pineado a propósito: el header de requirements.txt cuenta que un rango
    # `>=` una vez despachó dos apps distintas, y la que fallaba lo hacía en el
    # primer mensaje de un cliente. Cualquier bump es alto riesgo por la propia
    # regla del repo.
    "plus-agent/requirements.txt",
    "plus-agent/requirements-dev.txt",
    # Arranque y despliegue. El Dockerfile corre sin root y con --no-access-log
    # porque el access log de uvicorn incluye el query string, y la
    # verificación única del webhook de Meta trae el verify token ahí adentro.
    # start.sh pone el puerto en PUBLIC.
    "plus-agent/Dockerfile",
    "plus-agent/docker-compose.yml",
    "start.sh",
    "plus-agent/deploy/",
    # La superficie documentada de cada secreto, y el guard de red.
    "plus-agent/.env.example",
    "guard/netguard.py",
)

# Rompe el producto, pero se queda adentro.
#
# ESTA LISTA ERA `plus-agent/app/` A SECAS, y esa versión no clasificaba: con
# todo `app/` acá adentro, TODO cambio de código salía standard o más y el nivel
# `light` no lo podía alcanzar ningún PR. Un nivel inalcanzable es una fila de la
# tabla que miente, igual que la celda de zona horaria que ci.yml marca como
# vacua. Lo que quedó es la lista de los módulos que DECIDEN algo — un monto, un
# permiso, un aviso, un estado durable — y afuera quedaron los chicos y puros
# (`formato`, `prompts`, `progreso`, `reloj`, `excepciones`, `idioma`), que con
# un cambio de cinco líneas ahora sí caen en light.
SENSIBLE = (
    "plus-agent/app/limites.py",
    "plus-agent/app/acciones.py",
    "plus-agent/app/decisiones.py",
    "plus-agent/app/solicitudes.py",
    "plus-agent/app/confirmacion.py",
    "plus-agent/app/autonomia.py",
    "plus-agent/app/inventario.py",
    "plus-agent/app/notificar.py",
    "plus-agent/app/outbound_status.py",
    "plus-agent/app/pendientes.py",
    "plus-agent/app/digest.py",
    "plus-agent/app/avisos.py",
    "plus-agent/app/entrega.py",
    "plus-agent/app/clientes.py",
    "plus-agent/app/sombra.py",
    "plus-agent/app/readiness.py",
    "plus-agent/app/runtime_context.py",
    "plus-agent/app/modelos.py",
    "plus-agent/app/ajustes.py",
    "plus-agent/app/conversacion.py",
    "plus-agent/app/briefing.py",
    # Cada herramienta que el modelo puede llamar.
    "plus-agent/app/tools/",
    # Palanca sobre la suite entera (conftest.py ya está en CRITICO).
    "plus-agent/tests/fakes.py",
    "plus-agent/pyproject.toml",
    "plus-agent/pytest.ini",
    "plus-agent/Makefile",
    ".coderabbit.yaml",
)

# Lo escribe una herramienta y no lo lee nadie. Se le muestra igual al revisor,
# pero no cuenta para el tamaño del cambio: refrescar un lock son cinco mil
# líneas y merece menos atención que doce de `router.py`.
GENERADO = (
    "*.lock",
    "*package-lock.json",
    # No es configuración: es la SALIDA del guard, `{"seen": ..., "violations":
    # ...}`, commiteada como evidencia. Nada la lee — `grep -rn netguard` sólo
    # matchea `guard/netguard.py` — y se pone vieja sola.
    "netguard.json",
    "*.log",
    "plus-agent/agente.log",
)

# Prosa. Un PR hecho sólo de esto no lo revisa ningún modelo; el que lo lee ES
# la revisión. Precedente: #27 («La auditoría de rigidez… (sólo docs)»), cinco
# archivos .md y nada más.
PROSA = (
    "*.md",
    "plus-agent/docs/",
    ".devcontainer/",
    "LICENSE",
    "*.gitignore",
    "*.dockerignore",
)

# ---------------------------------------------------------------------------
# Los niveles.
# ---------------------------------------------------------------------------

# Alias en vez de identificadores pineados, para que esta tabla siga eligiendo el
# modelo actual de cada clase en vez de envejecer callada y gastarle al nivel más
# caro el presupuesto en el modelo del año pasado.
#
# `model` es SÓLO EL ORQUESTADOR, y decirlo importa: el comando del plugin nombra
# un modelo por subagente en su propio texto («Opus bug agent», «sonnet agents
# for CLAUDE.md violations»), así que --model NO TOCA a los agentes que revisan.
# Moverlos a ellos es lo que hace la directiva del nivel light, que es el único
# canal que les llega.
#
# Todos los niveles que corren comentan. Sacarle `--comment` al nivel barato se
# probó y está mal por dos lados: los hallazgos quedan en el resumen de un job
# VERDE que nadie abre, y el paso 1 del comando —que corta si Claude ya comentó
# en el PR— nunca se dispara, así que el nivel más barato pasa a ser el ÚNICO que
# se re-corre entero en cada push, para siempre.
NIVELES = {
    "skip": {"model": "", "timeout_minutes": "5", "comment": "false"},
    "light": {"model": "sonnet", "timeout_minutes": "15", "comment": "true"},
    "standard": {"model": "sonnet", "timeout_minutes": "25", "comment": "true"},
    "deep": {"model": "opus", "timeout_minutes": "40", "comment": "true"},
}

# Se le agrega al comando tal cual, como una constante elegida por nivel.
#
# Son constantes y NADA derivado del pull request se interpola acá adentro. En un
# PR desde un fork el título, el cuerpo, las etiquetas y hasta los nombres de
# archivo los escribe quien lo abrió; nada de eso llega al prompt. Llega al
# resumen del job, que es texto y no una instrucción.
#
# Lo que estas directivas pueden y no pueden hacer: el procedimiento del comando
# está numerado y es explícito, así que una instrucción que lo CONTRADICE («usá
# ocho agentes y no cuatro») es cara o ceca. Una que mueve un DEFAULT que el
# comando deja abierto, o que vuelve a especificar una elección que el comando le
# delega al orquestador —como en qué modelo corre cada subagente—, sí pega. Acá
# sólo está la segunda clase.
#
# En inglés porque es el idioma del prompt que las recibe, no del repo.
DIRECTIVAS = {
    "skip": "",
    "light": (
        "This is a small change on a low-risk surface and it is being reviewed "
        "cheaply on purpose. Two adjustments for this review only. Run the two "
        "bug-finding agents in step 4, and their validators in step 5, on Sonnet "
        "rather than Opus: the diff is small enough to hold in one pass and the "
        "extra depth is not worth its cost here. And hold the highest confidence "
        "bar: report only a defect that will certainly misbehave, and prefer "
        "reporting nothing at all over reporting something you are unsure of."
    ),
    "standard": "",
    "deep": (
        "This pull request touches a surface where a defect is expensive, "
        "silent, or reaches past this pull request: the Meta webhook and its "
        "signature check, the customer/staff privilege split, the three ERPNext "
        "credential identities, the fail-closed auto-confirm path, deployment, "
        "or the CI definition itself. Two adjustments, for this review only. "
        "First, give the changed hunks on those surfaces a second independent "
        "pass before you conclude. Second, on those surfaces a privilege-"
        "escalation, prompt-injection, lost-notification or fail-open defect is "
        "in scope even when confirming it needs context from outside the diff: "
        "go read that context and settle it, rather than dropping the finding "
        "for being unconfirmable from the diff alone. Everywhere else in this "
        "pull request the usual bar stands."
    ),
}

ORDEN = ("skip", "light", "standard", "deep")

# Cualquiera puede pasar por encima del clasificador desde el propio PR.
ETIQUETAS = {
    "review:skip": "skip",
    "review:light": "light",
    "review:standard": "standard",
    "review:deep": "deep",
}

# QUIÉN LO ABRIÓ NO ES UN INSUMO ACÁ, y es una decisión, no un olvido. La regla
# obvia —«subile el nivel a un fork o a alguien que nunca metió nada»— NO PUEDE
# DISPARAR: claude-code-action se niega a correr para un actor sin permiso de
# escritura (tira «Actor does not have write permissions»), y un PR desde un
# fork no recibe los secretos del repo. El workflow filtra las dos cosas antes
# de consultar este archivo, así que al clasificador sólo llega alguien que
# puede pushear una rama acá — o sea, exactamente el conjunto que la regla iba
# a eximir.
#
# La pregunta de confianza se contesta UNA vez, en el workflow, que es donde
# equivocarse se ve como una cruz roja sobre la contribución de alguien de
# afuera. Una segunda copia acá sería una rama que ningún PR puede tomar: lo
# mismo que la celda que no puede fallar, y que este repo ya avisa a gritos en
# su propio CI.

# 50 líneas es un cambio que entra en una pantalla. 400 es más o menos donde la
# tasa de detección de defectos de quien lee se cae, que es también donde «lo leí
# todo con cuidado» deja de describir lo que alguien hace de verdad. Más arriba
# el nivel SÍ sigue subiendo, a deep: un diff que nadie puede sostener entero es
# justo el que merece la revisión más capaz, aunque ésa tampoco lo termine de
# leer. Las formas de edición masiva que antes eran el argumento para poner un
# techo acá —renombres, drops vendorizados, refrescar un lock— se manejan donde
# corresponde: el descuento de archivos generados y la regla de cero líneas.
CHICO_LINEAS, CHICO_ARCHIVOS = 50, 5
MEDIO_LINEAS, MEDIO_ARCHIVOS = 400, 25


def matchea(path: str, patrones: tuple[str, ...]) -> bool:
    """True cuando `path` lo cubre alguno de `patrones`. Ver la nota de arriba."""
    nombre = path.rsplit("/", 1)[-1]
    for patron in patrones:
        if patron.endswith("/"):
            if path.startswith(patron):
                return True
        elif fnmatchcase(path, patron) or (
            "/" not in patron
            and ("*" in patron or "?" in patron)
            and fnmatchcase(nombre, patron)
        ):
            return True
    return False


def clasificar(pr, archivos):
    """Devuelve (nivel, motivo). `archivos` es una lista de (path, mas, menos).

    Leelo de arriba hacia abajo: la primera regla que matchea ES la respuesta, y
    el motivo que devuelve es la explicación entera.
    """
    etiquetas = {str(n).lower() for n in pr.get("labels", ())}
    for etiqueta, nivel in ETIQUETAS.items():
        if etiqueta in etiquetas:
            return nivel, f"está la etiqueta {etiqueta}, que pisa al clasificador"

    if pr.get("draft"):
        return "skip", "el pull request es un borrador"

    if not archivos:
        return "skip", "no cambió ningún archivo"

    revisables = [a for a in archivos if not matchea(a[0], GENERADO)]
    if not revisables:
        return "skip", "todo lo que cambió es generado o vendorizado"

    # El riesgo se resuelve ANTES que el tamaño, que la prosa y que quién lo
    # abrió: así tres líneas de `router.py` le ganan a cuatro mil de código
    # común, y así un bot que sube la versión de una action en
    # `.github/workflows/` NO pasa de largo por la regla de más abajo. El orden
    # no es un detalle: este archivo llama a esos paths «qué significa verde», y
    # el único PR de bot que hay que leer es el que los toca.
    criticos = [p for p, _, _ in revisables if matchea(p, CRITICO)]
    if criticos:
        return "deep", f"toca {len(criticos)} path(s) crítico(s), primero: {criticos[0]}"

    sensibles = [p for p, _, _ in revisables if matchea(p, SENSIBLE)]
    if sensibles:
        return "standard", f"toca {len(sensibles)} path(s) sensible(s), primero: {sensibles[0]}"

    # Los bots de dependencias abren un goteo constante de PRs cuyo diff entero
    # es un número de versión y un lockfile. Revisar todos es una suscripción, no
    # un resguardo. Lo que de ellos tocaba algo riesgoso ya se contestó arriba;
    # el resto lo vuelve a prender una etiqueta, de a uno.
    if str(pr.get("author_type", "")).lower() == "bot":
        return "skip", "lo abrió un bot y no toca nada riesgoso"

    sustanciales = [a for a in revisables if not matchea(a[0], PROSA)]
    if not sustanciales:
        return "skip", "sólo prosa; el que la lee es la revisión (como #27)"

    lineas = sum(mas + menos for _, mas, menos in sustanciales)
    cuantos = len(sustanciales)

    # Ninguna línea cambiada, en la cantidad de archivos que sea: un barrido de
    # renombres, una mudanza, un cambio de permisos. GitHub informa un renombre
    # puro como 0 y 0, y la regla de tamaño de abajo leía eso como «0 líneas en
    # 30 archivos, demasiado para hojear» y le compraba el nivel más profundo:
    # cuarenta minutos de la revisión más cara para mirar NADA.
    if lineas == 0:
        return "skip", f"{cuantos} archivo(s) cambiado(s), ninguno por una sola línea"

    if lineas <= CHICO_LINEAS and cuantos <= CHICO_ARCHIVOS:
        return "light", f"{lineas} línea(s) en {cuantos} archivo(s), ningún path riesgoso"
    if lineas <= MEDIO_LINEAS and cuantos <= MEDIO_ARCHIVOS:
        return "standard", f"{lineas} línea(s) en {cuantos} archivo(s)"
    return "deep", f"{lineas} línea(s) en {cuantos} archivo(s), demasiado para hojear"


def decidir(pr, archivos):
    nivel, motivo = clasificar(pr, archivos)
    ajustes = dict(NIVELES[nivel])
    ajustes["tier"] = nivel
    ajustes["reason"] = motivo
    ajustes["directive"] = DIRECTIVAS[nivel]
    return ajustes


def leer_archivos(path):
    """Lee la lista de cambios: `path<TAB>mas<TAB>menos` por renglón.

    GitHub informa los dos contadores como "-" en un archivo binario; eso se lee
    como cero, que es lo correcto — un blob binario no tiene hunks que leer.
    """
    salida = []
    with open(path, encoding="utf-8") as handle:
        for renglon in handle:
            partes = renglon.rstrip("\n").split("\t")
            if not partes[0]:
                continue
            mas = int(partes[1]) if len(partes) > 1 and partes[1].isdigit() else 0
            menos = int(partes[2]) if len(partes) > 2 and partes[2].isdigit() else 0
            salida.append((partes[0], mas, menos))
    return salida


def leer_evento(path):
    """Aplana el payload del webhook pull_request a lo que leen las reglas."""
    with open(path, encoding="utf-8") as handle:
        evento = json.load(handle)
    pr = evento.get("pull_request") or {}
    return {
        "draft": bool(pr.get("draft")),
        "labels": [e.get("name", "") for e in pr.get("labels") or ()],
        "author_type": (pr.get("user") or {}).get("type", ""),
    }


DELIMITADOR = "PR_REVIEW_INTENSITY_EOF"


def emitir(ajustes, stream):
    for clave in ("tier", "model", "timeout_minutes", "comment", "reason", "directive"):
        valor = str(ajustes[clave])
        if "\n" in valor or "\r" in valor:
            # Sólo `directive` es multilínea y sus valores son constantes de este
            # archivo — pero un heredoc cuyo cuerpo contiene su propio
            # delimitador se traga en silencio el resto de los outputs del step.
            # El guard no cuesta nada y la falla que evita no se ve.
            if DELIMITADOR in valor:
                raise ValueError(f"el output {clave!r} contiene el delimitador")
            stream.write(f"{clave}<<{DELIMITADOR}\n{valor}\n{DELIMITADOR}\n")
        else:
            stream.write(f"{clave}={valor}\n")


def selftest():
    def pr(**kw):
        base = {
            "draft": False,
            "labels": [],
            "author_type": "User",
        }
        base.update(kw)
        return base

    def nivel(archivos, **kw):
        return decidir(pr(**kw), archivos)["tier"]

    chico = [("plus-agent/demo/escenarios.py", 5, 2)]
    medio = [("plus-agent/demo/escenarios.py", 200, 100)]
    enorme = [("plus-agent/demo/escenarios.py", 4000, 10)]

    # La etiqueta le gana a todas las reglas de abajo, en las dos direcciones.
    assert nivel(enorme, labels=["review:skip"]) == "skip"
    assert nivel(chico, labels=["review:deep"]) == "deep"
    assert nivel([("plus-agent/app/main.py", 3, 0)], labels=["review:light"]) == "light"
    # Las etiquetas de GitHub no distinguen mayúsculas, así que el match tampoco.
    assert nivel(enorme, labels=["Review:Skip"]) == "skip"

    # Un borrador todavía se está escribiendo y el bump de un bot no espera la
    # opinión de nadie. Los dos ceden ante una etiqueta explícita.
    assert nivel(chico, draft=True) == "skip"
    assert nivel(chico, author_type="Bot") == "skip"
    assert nivel(chico, draft=True, labels=["review:deep"]) == "deep"
    assert nivel(chico, author_type="Bot", labels=["review:standard"]) == "standard"

    # Nada que un modelo tenga que leer.
    assert nivel([]) == "skip"
    assert nivel([("netguard.json", 5000, 4000)]) == "skip"
    # #27 entero: cinco .md bajo plus-agent/docs/ y nada más.
    assert nivel([
        ("plus-agent/docs/AUDITORIA.md", 66, 0),
        ("plus-agent/docs/prompt-agenda.md", 95, 0),
        ("plus-agent/docs/prompt-auditoria.md", 83, 0),
        ("plus-agent/docs/prompt-marcas.md", 53, 0),
        ("plus-agent/docs/prompt-relojes-en-tests.md", 60, 0),
    ]) == "skip"
    # Prosa al lado de código es un cambio de código con una nota adjunta.
    assert nivel([("README.md", 40, 3), ("plus-agent/demo/datos.py", 5, 1)]) == "light"

    # El riesgo le gana al tamaño en las dos direcciones.
    assert nivel([("plus-agent/app/main.py", 2, 1)]) == "deep"
    assert nivel([("plus-agent/app/router.py", 1, 1)]) == "deep"
    assert nivel([("plus-agent/app/telefono.py", 1, 1)]) == "deep"
    assert nivel([("plus-agent/requirements.txt", 1, 1)]) == "deep"
    assert nivel([(".github/workflows/ci.yml", 2, 1)]) == "deep"
    assert nivel([("start.sh", 2, 1)]) == "deep"
    assert nivel([("plus-agent/tests/conftest.py", 2, 1)]) == "deep"
    # Los módulos que DECIDEN algo son sensibles; los chicos y puros no, y por
    # eso `light` vuelve a ser alcanzable (ver la nota sobre SENSIBLE).
    assert nivel([("plus-agent/app/limites.py", 2, 2)]) == "standard"
    assert nivel([("plus-agent/app/tools/pedidos.py", 2, 2)]) == "standard"
    assert nivel([("plus-agent/tests/fakes.py", 2, 2)]) == "standard"
    assert nivel([("plus-agent/app/formato.py", 2, 2)]) == "light"
    assert nivel([("plus-agent/app/reloj.py", 3, 1)]) == "light"
    assert nivel([("plus-agent/tests/test_idioma.py", 2, 2)]) == "light"
    assert nivel(chico) == "light"
    assert nivel(medio) == "standard"
    assert nivel(enorme) == "deep"

    # Refrescar un lock no puede inflar el nivel del cambio que tiene al lado.
    assert nivel([("plus-agent/demo/datos.py", 5, 2), ("netguard.json", 6000, 5000)]) == "light"

    # Un bot llega a la tabla de riesgo ANTES que a la regla de bots. El único
    # PR de bot que hay que leer es justamente el que edita el CI.
    assert nivel([(".github/workflows/ci.yml", 2, 1)], author_type="Bot") == "deep"
    assert nivel([("plus-agent/requirements.txt", 1, 1)], author_type="Bot") == "deep"
    assert nivel([("plus-agent/app/limites.py", 1, 1)], author_type="Bot") == "standard"
    assert nivel([("netguard.json", 900, 800)], author_type="Bot") == "skip"

    # Un barrido de renombres: GitHub informa 0 y 0, y treinta archivos de nada
    # son nada para leer, no un diff «demasiado para hojear».
    assert nivel([(f"plus-agent/demo/m{i}.py", 0, 0) for i in range(30)]) == "skip"
    assert nivel([("plus-agent/demo/m.py", 0, 0), ("plus-agent/demo/n.py", 1, 0)]) == "light"
    # ...pero un renombre que toca un path crítico se lee igual.
    assert nivel([("plus-agent/app/router.py", 0, 0)]) == "deep"

    # Un patrón literal es un path entero y no se filtra al mismo nombre en otro
    # lado; uno con comodín a propósito sí.
    assert matchea("start.sh", CRITICO)
    assert not matchea("otro/start.sh", CRITICO)
    assert matchea("plus-agent/frontend/package-lock.json", GENERADO)

    # Todos los niveles que corren comentan; ver la nota sobre NIVELES.
    for nombre_nivel in ("light", "standard", "deep"):
        assert NIVELES[nombre_nivel]["comment"] == "true"
    assert NIVELES["skip"]["comment"] == "false"

    # Prefijos, globs a cualquier profundidad, y ningún vecino agarrado de más.
    assert matchea("plus-agent/deploy/supervisor.sh", CRITICO)
    assert matchea("plus-agent/app/policy.py", CRITICO)
    # Un vecino con el mismo prefijo de nombre no se cuela en ninguna tabla:
    # `policy.py` es un literal, no un prefijo.
    assert not matchea("plus-agent/app/policy_reglas.py", CRITICO)
    assert not matchea("plus-agent/app/policy_reglas.py", SENSIBLE)
    assert not matchea("plus-agent/docs/AUDITORIA.md", CRITICO)

    # El parser: GitHub informa "-" en los dos contadores de un binario, y una
    # línea vacía al final del archivo es lo normal, no un caso raro.
    with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False) as tmp:
        tmp.write("plus-agent/app/logo.png\t-\t-\nplus-agent/app/main.py\t3\t1\n\n")
        ruta = tmp.name
    try:
        assert leer_archivos(ruta) == [
            ("plus-agent/app/logo.png", 0, 0),
            ("plus-agent/app/main.py", 3, 1),
        ]
    finally:
        os.unlink(ruta)

    # Un typo en alguna de las tablas tiene que aparecer acá, y no a las 3am en
    # un job.
    for nombre in ORDEN:
        assert nombre in NIVELES
        assert nombre in DIRECTIVAS
    for ajustes in NIVELES.values():
        assert set(ajustes) == {"model", "timeout_minutes", "comment"}
        assert ajustes["comment"] in ("true", "false")

    print("pr_review_intensity: pasaron todos los self-tests")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Elige la intensidad de revisión de un PR.")
    parser.add_argument("--event", help="path al JSON del evento de GitHub")
    parser.add_argument("--files", help="path a una lista `nombre<TAB>mas<TAB>menos`")
    parser.add_argument(
        "--github-output",
        default=os.environ.get("GITHUB_OUTPUT"),
        help="dónde agregar los outputs del step (default: $GITHUB_OUTPUT)",
    )
    parser.add_argument("--selftest", action="store_true", help="corre las aserciones internas")
    args = parser.parse_args(argv)

    if args.selftest:
        selftest()
        return 0
    if not args.event or not args.files:
        parser.error("--event y --files son obligatorios salvo con --selftest")

    ajustes = decidir(leer_evento(args.event), leer_archivos(args.files))
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            emitir(ajustes, handle)
    emitir(ajustes, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
