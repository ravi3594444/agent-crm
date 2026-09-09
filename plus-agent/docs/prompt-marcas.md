# Session opener — los marcadores durables (hallazgo 4 de la auditoría)

Fresh session in `plus-agent/` of `agent-crm`. Tu tarea es el issue **#24**:
https://github.com/ravi3594444/agent-crm/issues/24 — leelo entero, lleva las mediciones.

**Primer commit:** guardá este mensaje como `docs/prompt-marcas.md` en una rama nueva desde `main`, y pushealo.

Antes de tocar nada, leé `docs/AUDITORIA.md`. Importa una cosa en particular: **el radio de explosión 0 es la razón por la que este hallazgo está en la lista, no evidencia de que sea seguro tocarlo.**

## El estado

Once marcadores durables escritos como comentarios en documentos de ERPNext:

`[limite]` · `[entrega]` · `[idioma]` (`limites.py`) · `[accion]` (`acciones.py`) · `[solicitud]` (`solicitudes.py`) · `[sombra]` (`sombra.py`) · `[confirmado-por-agente]` (`confirmacion.py`) · `[remito-preparado-por-agente]` (`decisiones.py`) · `[pendiente-aviso]` y `[pendiente-cerrado]` (`pendientes.py`) · más dos en prosa: `Requiere revisión humana:` y `Rechazado manualmente por` (`autonomia.py`).

**Ningún lector compartido.** Cada módulo trae el suyo: `pendientes._tiene_marca`, `limites._consultar_marca`, `autonomia._comentarios`, y `sombra`/`confirmacion` con su propia consulta y su propio parseo.

| | |
|---|---|
| copias | 11 marcadores, 0 lector compartido |
| radio de explosión | **0** — medido: le cambié el texto a `pendientes.MARCA_AVISO` y no se rompió un solo test |
| costo | ~3 medios días |

## Por qué el radio 0 es lo grave

El texto de un marcador es el formato durable con el que el sistema **se entiende consigo mismo entre reinicios**. Un marcador renombrado significa que el barrido de mañana no ve los avisos que escribió el de hoy, y vuelve a avisarle al cliente — o no cierra lo que había que cerrar. Radio 0 no es «bajo acoplamiento»: es que la parte del contrato que sobrevive al proceso no está afirmada en ninguna parte.

Es la misma lección que #9 y que la auditoría estática de #18: **un guard que no se puede romper no está guardando.**

## Tres cosas que el issue NO decide, y vos sí

1. **`sombra.MAX_MARCAS = 5` contra `confirmacion.MAX_MARCAS = 20`.** Averiguá si la diferencia es real antes de unificar. Si es real, **escribila** — hoy no está en ninguna parte y eso es el problema, no el número. Si es un accidente, unificá y decilo.
2. **Los dos marcadores en prosa** (`Requiere revisión humana:`, `Rechazado manualmente por`). Decidí si entran al mismo registro que los once entre corchetes. No son iguales: los de corchetes son un formato que el sistema eligió, los de prosa son texto que alguien escribió y que además **está en español**, así que tocarlos roza el catálogo de idiomas. Justificá la decisión en el PR.
3. **`confirmacion.py` está excluido** por el brief de la auditoría (núcleo de seguridad), aunque su marcador aparezca en el censo. El conteo es honesto; la consolidación no lo incluye. Su `MAX_MARCAS` se puede *leer* para responder el punto 1, no cambiar.

## El test que tiene que existir cuando termines

El que hoy no existe: **uno que falle si el texto de un marcador durable cambia.** Ése es el punto entero. Comprobalo por mutación — cambiale el texto a un marcador y mostrá qué test se cae, en el cuerpo del PR.

Un registro `nombre → (texto, techo, parser)` y un lector único (`marcas.leer(...)` / `marcas.escribir(...)`) hace ese test posible y hoy no lo es.

## Bordes

- **No toques el núcleo de seguridad**: las tres identidades de ERPNext, la lista lineal de reglas de `policy.evaluar`, los scripts de idempotencia de la cola durable, `confirmacion.py`.
- Los marcadores ya escritos en ERPNext de un cliente vivo **no se pueden renombrar**: si el registro cambia un texto, hay que leer los dos o el sistema pierde su propia historia. Decidí y decilo.
- Cadena por módulo, no un big bang: cada módulo migrado con sus tests pasando antes del siguiente.

## Done when

- `ruff check app demo tests`; `pytest -q` (Redis Stack en `REDIS_URL`, db 0) sin regresión sobre el total actual.
- Un test que falla si un marcador cambia de texto, y la mutación que lo demuestra en el PR.
- Los dos techos (5 y 20) unificados **o** con el motivo de su diferencia escrito.
- El PR dice qué se decidió sobre los marcadores en prosa y por qué.
