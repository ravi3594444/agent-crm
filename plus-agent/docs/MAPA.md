# MAPA — qué hace cada archivo grande, para no volver a leerlo entero

Este archivo existe por una razón medible: `app/solicitudes.py` tiene 2731
líneas, `app/idioma.py` 2135, `app/limites.py` 1798 y `app/main.py` 1775. Una
sesión que empieza de cero y los lee enteros gasta la mitad de su contexto antes
de escribir una línea, y vuelve a descubrir las mismas trampas que descubrió la
sesión anterior.

**Cómo se usa:** leé la fila del módulo que vas a tocar y las trampas de abajo.
Andá al archivo sólo por lo que la fila no conteste.

**Cómo se mantiene:** es un acuerdo de trabajo en `CLAUDE.md` — el que cambia un
módulo actualiza su fila en el mismo commit. Un mapa desactualizado es peor que
no tenerlo, porque se le cree.

> Esto NO reemplaza a `CLAUDE.md` (las reglas duras), a `docs/AUDITORIA.md` (la
> deuda medida) ni a los docstrings de módulo, que siguen siendo donde vive el
> *porqué* largo. Esto es el índice.

---

## Los módulos, por tamaño

| archivo | líneas | qué es | lo que hay que saber antes de tocarlo |
|---|---:|---|---|
| `app/solicitudes.py` | 2731 | El almacén de **solicitudes de decisión**: un registro durable por pedido, escrito como comentarios `[solicitud] {json}` append-only donde **cada evento lleva la foto entera**. Redis es índice y caché, reconstruible desde ERPNext. | Leer es «el evento más nuevo que parsea gana», nunca un fold. `_escribir` devuelve False = *esto no pasó*. Dos conjuntos de estado: `ABIERTOS` (alguien debe contestar) y el más ancho `CON_PLAZO` (tiene plazo vivo); el índice y el barrido van por el segundo. **Es el patrón que `agenda.py` generaliza — leelo antes de escribir cualquier cosa durable.** |
| `app/idioma.py` | 2135 | El catálogo ES/EN entero y `t(clave, idioma, **params)`. | `t()` **nunca levanta**: una clave que no existe se loguea y se devuelve *tal cual*, así que un olvido viaja a WhatsApp como `pedido.algo` en vez de romper. `tests/test_idioma_cobertura.py` es lo que lo atrapa. Claves nuevas **al final del bloque**, que es como dos sesiones lo editan sin pisarse. |
| `app/limites.py` | 1798 | Todo lo que el dueño cambia por WhatsApp: tres registros de `Definicion` (`LIMITES`, `ENTREGA`, `IDIOMAS`) unidos en `TODOS`. | Un límite nuevo es **una entrada de dict y nada más** — alias, validación, código de cuatro dígitos y auditoría durable salen gratis. `maximo` **default 0.0**: si no lo declarás, todo valor menos 0 se rechaza. `opcional=True` es lo único que permite `NINGUNO`. `vigente()` **levanta** si Redis no está: nunca es una lectura segura. |
| `app/main.py` | 1775 | El webhook de Meta, el router determinista del equipo y los **cuatro hilos** de fondo. | El barrido es `_solicitudes_scheduler` (línea ~1566, cada 60 s): `solicitudes.tick()`, `pendientes.tick()`, `agenda.tick()`, **cada uno en su propio try/except**. El `from app import ...` está **fuera** del `while`: un error de import en un módulo nuevo mata el hilo entero en silencio. Códigos de 4 dígitos (ajustes) y 6 (acciones) se matchean **antes** del modelo y en orden. |
| `app/readiness.py` | 1205 | El preflight: valida `.env`, Meta, ERPNext, Redis y los límites sin mostrar valores. | Un aviso de barrido que sale **fuera de la ventana de 24 h** va en `PLANTILLAS` *y* en `PLANTILLAS_BARRIDO_SIEMPRE` o `PLANTILLAS_BARRIDO_OPCIONAL`. Olvidarlo es un test rojo, a propósito. |
| `app/decisiones.py` | 1111 | Lo que el equipo decide sobre un pedido y el remito. | `telefono_del_cliente(pedido)` es de acá. |
| `app/acciones.py` | 1006 | Una orden en prosa del dueño convertida en UNA acción, confirmada con código de 6 dígitos. | Escribe `[accion]`, el único rastro de que una persona autorizó algo. |
| `app/policy.py` | 968 | `evaluar()`: la lista lineal de reglas de auto-confirmación. | **No se parte** — es el diseño, y partirla es cómo se pierde una regla. `_precio_autorizado` filtra Item Prices por `price_list`, `currency` **y** `uom`. |
| `app/tools/pedidos.py` | ~990 | Las herramientas de escritura del cliente: `crear_pedido`, `crear_cliente`, `escalar_a_humano`, `recordar`. | Los pedidos se crean en **un solo lugar** (`erpnext.create_doc("Sales Order", …)`). `_after_create` tiene **tres salidas** y `crear_pedido` **cuatro** que ni la tocan (idempotencia y recuperación): nada que se escriba ahí puede asumirse escrito una sola vez. |
| `app/notificar.py` | ~720 | El embudo único de todo lo que lee una persona del equipo. | `avisar_dueno` avisa un fallo con un **retorno falso**, no con excepción. `NOTIFICAR_SOLO_PRIMERO` manda al **primer teléfono alfabético**, que puede no ser el dueño: para el dueño, `avisar_dueno`. Cuerpo capado a 1024 por Meta. |
| `app/autonomia.py` | 673 | Cuánto se confirma solo y qué lo frena. | Lee `pendientes._zona()` (un privado). |
| `app/marcas.py` | ~700 | **El registro tipado de los 13 marcadores durables** y el único lector/escritor. | Toda marca nueva es una fila de `_FILAS`: el `order_by` **se deriva** de `lectura` y el parser de `texto`, así que no se pueden separar. `porque_el_techo` es obligatorio. Agregar una fila rompe **3 tests escritos a mano** — actualizarlos a mano es el punto, generarlos los mata. |
| `app/pendientes.py` | ~620 | El barrido de borradores esperando a una persona: sombra, recordatorio y cierre. | `en_silencio()` es la **única** implementación de las horas de silencio (22:00–07:00, ventana que cruza la medianoche). `_tiene_marca` tiene **tres** respuestas y hay que comparar con `is not False`. El cierre delega en `agenda.ejecutar_ahora`. Todo apagado por default (`NINGUNO`). |
| `app/erpnext.py` | 604 | El cliente REST. **Tres identidades**: cliente, gerencia y política. | Un hilo de fondo **no tiene scope** y cae en la clave de *cliente*: por eso todo lector de fondo llama `policy_get_*` explícitamente. Un 404 y una caída son **la misma excepción**. `_list` levanta en vez de devolver `[]`. `get_list` **default limit=20**. |
| `app/agenda.py` | ~700 | **La lista durable de cosas que vencen más tarde.** Una fila = `(id, sobre, tipo, vence, estado, params)`; un barrido; handlers de ~10 líneas. | Los cinco conceptos —exactamente-una-vez, horas de silencio, re-leer, fallar cerrado, lote acotado— viven acá y **sólo** acá. Un comportamiento nuevo es una fila, no un módulo. `sobre` es **siempre** un Sales Order. |
| `app/avisos.py` | 501 | La cola durable de salida al **cliente** (ZSET, score = cuándo vence). | `encolar` es **fail-closed**: si Redis no toma la escritura, **levanta**; no devuelve False. `False` significa *ya lo cubrió otro*. Deduplica por `(evento, pedido)` **30 días**: dos filas del mismo tipo sobre el mismo pedido necesitan el id en el evento. |
| `app/digest.py` | 475 | El resumen de las 18:00. Hilo propio, gateado por `DIGEST_ACTIVO`. | No migrar. |
| `app/reloj.py` | 181 | **El reloj del negocio, uno solo.** | `zona()`/`ahora()`/`hoy()` **levantan** y son para código que DECIDE. `zona_con_respaldo(quien)`/`ahora_con_respaldo` no levantan y son para el que **informa, barre o sella** — un barrido usa éstas. Los sellos sin zona de ERPNext se leen **sólo** con `de_erpnext()`. El literal `"America/Argentina/Buenos_Aires"` no puede aparecer en ningún otro archivo de `app/`. |

---

## Los tests: cómo está armado el harness

- `tests/conftest.py` es **el único conftest**. Estampa un entorno falso *antes*
  de importar `app`, y deja **sin fijar** `BUSINESS_TIMEZONE`, `IDIOMA_*` y
  `LOCALE` — porque CI corre **cinco celdas** con esos valores cambiados.
- **Cinco fixtures, todas autouse:** `limites_sin_redis`, `marcas_sin_redis`,
  `_idioma_declarado`, `_locale_declarado`, `el_barrido_no_lee_el_reloj_real`.
- `RelojDePrueba("2026-09-08")`: un archivo **nombra su momento** y arma las
  horas desde ahí (`a_las`, `epoch`, `sello`, `sello_partido`), resolviendo la
  zona en el momento de la llamada. Por eso los mismos tests pasan en Buenos
  Aires y en Kolkata.
- `tests/fakes.py::listar` es el doble de consulta de ERPNext y **honra**
  filtros, `order_by`, `limit` y `start`. Es a propósito: un doble que ignorara
  el orden no podría discrepar con el código sobre «leé el extremo más nuevo».
- `el_barrido_no_lee_el_reloj_real` hace que `pendientes._ahora()` llame a
  `pytest.fail` **en toda la suite**. Usa `pytest.fail` y no `assert` porque
  `Failed` hereda de `BaseException` y un `except Exception` de producción no
  puede tragárselo.
- Un módulo que agrega variables de entorno las agrega al `delenv` de
  `limites_sin_redis`, o el `.env` de una máquina de desarrollo configura los
  tests.

---

## Las trampas que ya costaron tiempo

1. **`erpnext.add_comment` se traga los errores; `registrar_comentario` los
   levanta.** `marcas.escribir(exigir=True)` usa el segundo. Para algo de lo que
   depende una promesa a un cliente, «no hubo excepción» tiene que seguir
   significando «quedó escrito».
2. **El `content` de un comentario vuelve envuelto en HTML y con entidades
   escapadas.** El doble de test **no** lo envuelve, así que un parseo hecho a
   mano pasa todos los tests y falla en producción. Por eso existe
   `marcas.texto_plano` y por eso `tests/test_pendientes.py:329` simula el
   destrozo.
3. **El JSON de un marcador está anclado al FINAL del comentario.** Cualquier
   texto después de la carga lo vuelve ilegible — y se saltea en silencio, no da
   error.
4. **`delivery_date` es un `Date` de ERPNext: `"YYYY-MM-DD"` pelado, sin hora y
   sin zona.** La hora de entrega es **configuración del dueño**
   (`ENTREGA_HORA`, vía `excepciones.hora_reparto()`). Todo lo que necesite una
   hora del día tiene que traérsela; el pedido no la tiene.
5. **La fecha de entrega no es final en la creación**: `policy_aplicar_terminos`
   la reescribe cuando el cliente acepta una contraoferta. Lo que dependa de
   ella **se re-lee a la hora de actuar**.
6. **Un aviso que dispara un barrido sale casi siempre fuera de la ventana de
   24 h de Meta.** Sin `plantilla_env` no falla a veces: falla **siempre**, se
   gasta los 8 reintentos y muere en la cola de descarte, en silencio.
7. **Los parámetros de una plantilla son DATOS** (número de pedido, fecha,
   monto), nunca prosa: la plantilla está registrada en **un** idioma y una
   frase traducida adentro de un parámetro no traduce nada.
8. **`policy_get_list` con la clave de cliente devuelve lo que ese usuario ve.**
   Un hilo de fondo no tiene scope: siempre `policy_get_*`.
9. **`docker compose restart` NO re-lee `.env`** — hace falta
   `up -d --force-recreate`.
10. **`LLM_PROVIDER` en blanco significa `qwen`, no gemini.** No hay fallback
    entre proveedores, por diseño.
