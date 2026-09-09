# Auditoría de rigidez — dónde está el mismo concepto escrito más de una vez

**La consolidación de mayor valor es un solo reloj del negocio (`app/reloj.py`): «qué hora es acá» está escrito 11 veces en `app/` y 6 más en `tests/`, ya divergió tres veces en producción, y cuesta ~3 medios días con los tests del lado incluido.**

## Lo que la medición dijo, y contradice la premisa del brief

El brief dice que la duplicación hace que «un cambio de una línea rompa diez tests en cuatro archivos». **No se reproduce.** Cambié cada concepto en UNA copia y corrí la suite entera:

| mutación | archivos de test rotos |
|---|---:|
| `pendientes._zona()` devuelve UTC | **1** |
| `inventario._momento` lee el sello como UTC (el bug de #16) | **1** |
| `pendientes.en_silencio` devuelve siempre False | **1** |
| `pendientes.MARCA_AVISO` cambia de texto | **0** |
| `aprobacion._leer_doc` pierde la identidad de política | **0** |

El radio de explosión es **1, no 10**, para todo lo que medí — porque la suite está bien aislada: cada archivo de test maneja un módulo con dobles, así que sólo ejercita SU copia.

Eso no debilita el caso de consolidar: lo invierte y lo hace más fuerte. **Acá la duplicación no encarece el cambio — hace invisible la divergencia.** Si cambio el concepto en una copia, las otras diez siguen con el comportamiento viejo y ningún test puede notar que discrepan, porque ningún test mira dos copias a la vez. Así pasaron #16 (tres lecturas del mismo sello, dos interpretándolo distinto) y #19 (cuatro techos, tres informados mal). Es exactamente lo que se dijo de los fixtures duplicados —hacen clases enteras de bug **infalsificables**— y la medición dice que vale igual para el código.

Consecuencia para la fórmula: con radio ≈ 1 en todas las filas, `copias × radio ÷ costo` se degenera en `copias ÷ costo`. Así que la tabla lleva el radio medido **y** el término que sí discrimina: **divergencias**, o sea cuántas copias ya se separaron y costaron un bug.

## Tabla ordenada

| # | concepto | copias | radio | divergencias | costo | recomendación |
|---|---|---:|---:|---:|---:|---|
| 1 | reloj del negocio: `_zona`/`_ahora`/`_hoy` + el default escrito a mano | 11 archivos, 13 literales, 1 copia literal (`_hoy_del_negocio`) | 1 | **3** | 2 | `app/reloj.py`, un solo módulo |
| 2 | el mismo reloj del lado de los tests: `AHORA`/`ZONA`/`HOY`/`epoch()` | 6 archivos | — | **2** | 1 | fixture compartido; hacer junto con #1 |
| 3 | sello naive de ERPNext → zona del negocio | 4 | 1 | **2** | 1 | una función; es la mitad de #1 |
| 4 | marcador durable en un comentario de ERPNext: escribir, leer, y el techo de cuántos | 11 marcadores, 0 lector compartido, `MAX_MARCAS` dos veces con valores distintos (5 y 20) | **0** | — | 3 | un módulo `marcas.py` con registro |
| 5 | agenda-durable-más-barrido (`solicitudes/pendientes/digest::tick`) | 3 (+1 planeada) | 1 | — | 5 | `agenda.py`, **después** de 1–4 |
| — | a mano donde hay librería: `formato.pesos`, `telefono.py`, `_MESES`/`_DIAS`, `_ORDEN_DIAS` | 4 | 1 | 0 | 2 | ver «no marcado», con motivo |
| — | `_leer_doc` copiado textual (`aprobacion.py`, `decisiones.py`) | 2 | **0** | — | — | núcleo de seguridad: excluido |

**Guarda de amplitud cumplida:** tres de las cinco (#2, #4, #5) son código que no toqué en #16–#19. La #5 sale última **a propósito** y con la medición en la mano: es la que el brief anterior daba por decidida.

## Los cinco

**1 — Un solo reloj del negocio.** `os.getenv("BUSINESS_TIMEZONE", "America/Argentina/Buenos_Aires").strip()` + `ZoneInfo(...)` está escrito 13 veces en 11 archivos (`acciones`, `briefing`, `conversacion`, `digest`, `inventario`, `limites`, `notificar`, `pendientes`, `policy`, `solicitudes`, `tools/pedidos`), cada uno con su propio envoltorio: `_zona()` dos veces, `_ahora()` siete, `_hoy()` cinco. `policy._hoy_del_negocio` y `tools/pedidos._hoy_del_negocio` son **la misma función copiada**, hasta el mensaje de error. Ya divergió tres veces: #12 (once tests leían el reloj vivo), #14 (la zona entraba desde el entorno), #16 (la zona de ERPNext y la del negocio nunca se comparaban). Consolidar es `app/reloj.py` con `zona()`, `ahora()`, `hoy()` y `de_erpnext(sello)`, y los 11 archivos importándolo. El default hardcodeado queda en un lugar, que es lo que permite cambiarlo para un cliente que no está en Buenos Aires.

**2 — El mismo reloj, del lado de los tests.** `test_autonomia.py` define `AHORA`, `test_inventario.py` define `ZONA` **y** `AHORA`, `test_pendientes.py` define `ZONA` y `epoch()`, y `test_digest.py`, `test_fechas_entrega.py` y `test_policy_reglas.py` cada uno su `HOY`. Seis archivos re-derivaron la suposición del código, así que ninguno puede discrepar con ella: por eso el bug de zona de #16 era invisible a los tests que tocaban justo ese campo. Un fixture en `conftest.py` que dé el momento y la zona, y los seis archivos pidiéndolo. Es **el más barato de los cinco** y no tiene sentido hacerlo separado de #1.

**3 — El sello naive de ERPNext.** `replace(tzinfo=...)` sobre un campo que ERPNext escribió sin zona aparece en `pendientes.edad_horas`, `autonomia._creacion`, `inventario._momento` y `solicitudes._momento_del_negocio` — y en `confirmacion.py:81` con UTC a propósito y documentado. Dos de las cuatro ya costaron un bug (#16, y la tercera copia quedó sin documentar hasta ayer). Es una función de tres líneas, `reloj.de_erpnext(sello)`, y entra dentro de #1: sale casi gratis si se hace ahí.

**4 — Los marcadores durables.** Hay once —`[limite]`, `[entrega]`, `[idioma]`, `[accion]`, `[solicitud]`, `[sombra]`, `[confirmado-por-agente]`, `[remito-preparado-por-agente]`, `[pendiente-aviso]`, `[pendiente-cerrado]`, más dos en prosa (`Requiere revisión humana:`, `Rechazado manualmente por`)— y **ningún lector compartido**: `pendientes._tiene_marca`, `limites._consultar_marca`, `autonomia._comentarios` y `sombra`/`confirmacion` cada uno con su consulta y su parseo. El techo de cuántos leer está escrito dos veces con valores distintos (`sombra.MAX_MARCAS = 5`, `confirmacion.MAX_MARCAS = 20`) y nada dice por qué difieren. Lo que lo pone en la lista es el **radio 0**: cambié el texto de un marcador y no se rompió un solo test, así que el nombre del marcador —que es el formato durable con el que el sistema se entiende consigo mismo entre reinicios— no está protegido por nada. Un `marcas.py` con un registro `nombre → (texto, techo, parser)` y un lector único.

**5 — Agenda durable más barrido.** `solicitudes.py::tick`, `pendientes.py::tick` y `digest.py::tick` re-implementan durabilidad, exactamente-una-vez, horas de silencio, re-leer antes de actuar y fallar cerrado. Es la duplicación más grande por volumen de código y la más chica por número de copias, y es la más cara: 5 medios días, contra 1 de las dos primeras. `pendientes.en_silencio` dice en su propio docstring que el criterio es «el mismo de `digest.tick`». Vale hacerla — es el `agenda.py` del brief anterior — pero **después** de 1 a 4, por dos razones medidas: es la que peor ordena en `copias ÷ costo`, y las cuatro de arriba son cosas que un `agenda.py` va a usar. Construir la agenda sobre cuatro conceptos todavía duplicados es escribirlos una quinta vez.

## Lo que deliberadamente NO marqué, con el motivo por línea

- **`policy.evaluar`** — excluido por el brief, y correcto: una lista lineal de reglas verificadas por separado es el diseño; partirla es cómo se pierde una regla.
- **Las tres identidades de ERPNext, `confirmacion.py`, los scripts de idempotencia de la cola** — excluidos por el brief. Costo de la exclusión, medido y dicho para que quede: `aprobacion._leer_doc` y `decisiones._leer_doc` son copias textuales y cambiar una no rompe ningún test.
- **`limites.py`** — 1636 líneas y parece el candidato obvio a «módulo que debería ser una fila». **Ya lo es:** `Definicion` + `_numero`/`_dias`/`_hora` es exactamente la tabla que este audit recomienda en otras partes. Es el modelo a copiar, no un hallazgo.
- **`formato.pesos`** — no es deuda por sí solo. `locale` es global al proceso y `setlocale` no es thread-safe, y esta app corre hilos de barrido, así que la stdlib está descartada con razón. `babel` sí sirve (`format_currency(v, "ARS", locale=...)`, sin estado de proceso), y ahí el argumento a favor es el segundo idioma, no la limpieza.
- **`telefono.py`** — 108 líneas de reglas argentinas a mano. Se queda hasta que haya un segundo país: `phonenumbers` es más correcto, y los tests actuales son la suite de aceptación que probaría la equivalencia, pero cambiarlo sin necesidad mueve el código que decide a quién se le manda un mensaje.
- **`_MESES`/`_DIAS`/`_ORDEN_DIAS`** — tres mapas de español a mano. Se van con `babel` en el mismo movimiento que `pesos`, no antes: sueltos no pagan.
- **El digest y `solicitudes.py`** — no migrar, por el brief. Funcionan y tienen tests.
- **Correctitud, estilo, cobertura** — fuera de la lente por definición.

## Visto al pasar, fuera de alcance

- CI corre la suite en un solo idioma; el hueco entre #17 y #18 sólo se vio con los dos en `main` (ver PR #20).
- `readiness.py` lee `BUSINESS_TIMEZONE` cuatro veces por su cuenta.
- `excepciones.HORIZONTE_DIAS = 7` y el horizonte de `recordar` del brief de agenda son el mismo número en dos lados.
- Nada asegura que `aprobacion` lea con la identidad de política.
- `digest.MARCA_TTL_SEGUNDOS` es un TTL, no un marcador; el nombre choca con los once `MARCA*` que sí lo son.
