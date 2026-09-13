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
| `app/solicitudes.py` | 2731 | El almacén de **solicitudes de decisión**: un registro durable por pedido, escrito como comentarios `[solicitud] {json}` append-only donde **cada evento lleva la foto entera**. Redis es índice y caché, reconstruible desde ERPNext. | Leer es «el evento más nuevo que parsea gana», nunca un fold. `_escribir` devuelve False = *esto no pasó*. Dos conjuntos de estado: `ABIERTOS` (alguien debe contestar) y el más ancho `CON_PLAZO` (tiene plazo vivo); el índice y el barrido van por el segundo. Para preguntar «¿hay algo en curso?» la condición es `not in TERMINALES` y NUNCA un subconjunto: `ABIERTOS` deja `REVISION_HUMANA` afuera a propósito, así que filtrar por ahí rechaza los dos estados donde el cliente NO aceptó y permite el único donde sí. `soltar_reserva` re-lee DESPUÉS de escribir y devuelve `(probado, frase)`: es la única prueba de que un borrador dejó de tomar stock. Sus DOS éxitos no son el mismo hecho y hay constantes para distinguirlos: `LO_CERRO_ESTA_LLAMADA` es una transición que hizo esta llamada, `YA_ESTABA_CERRADO` es un borrador que cerró **cualquiera** — quien le atribuya el cierre a alguien tiene que mirar cuál de las dos vino, o termina escribiendo «lo dio de baja su cliente» sobre un rechazo del equipo. **Es el patrón que `agenda.py` generaliza — leelo antes de escribir cualquier cosa durable.** |
| `app/idioma.py` | 2135 | El catálogo ES/EN entero y `t(clave, idioma, **params)`. | `t()` **nunca levanta**: una clave que no existe se loguea y se devuelve *tal cual*, así que un olvido viaja a WhatsApp como `pedido.algo` en vez de romper. `tests/test_idioma_cobertura.py` es lo que lo atrapa. Claves nuevas **al final del bloque**, que es como dos sesiones lo editan sin pisarse. |
| `app/limites.py` | 1798 | Todo lo que el dueño cambia por WhatsApp: tres registros de `Definicion` (`LIMITES`, `ENTREGA`, `IDIOMAS`) unidos en `TODOS`. | Un límite nuevo es **una entrada de dict y nada más** — alias, validación, código de cuatro dígitos y auditoría durable salen gratis. `maximo` **default 0.0**: si no lo declarás, todo valor menos 0 se rechaza. `opcional=True` es lo único que permite `NINGUNO`. `vigente()` **levanta** si Redis no está: nunca es una lectura segura. |
| `app/main.py` | 1775 | El webhook de Meta, el router determinista del equipo y los **cuatro hilos** de fondo. | El barrido es `_solicitudes_scheduler` (línea ~1566, cada 60 s): `solicitudes.tick()`, `pendientes.tick()`, `agenda.tick()`, **cada uno en su propio try/except**. El `from app import ...` está **fuera** del `while`: un error de import en un módulo nuevo mata el hilo entero en silencio. Códigos de 4 dígitos (ajustes) y 6 (acciones) se matchean **antes** del modelo y en orden. |
| `app/readiness.py` | 1205 | El preflight: valida `.env`, Meta, ERPNext, Redis y los límites sin mostrar valores. | Un aviso de barrido que sale **fuera de la ventana de 24 h** va en `PLANTILLAS` *y* en `PLANTILLAS_BARRIDO_SIEMPRE` o `PLANTILLAS_BARRIDO_OPCIONAL`. Olvidarlo es un test rojo, a propósito. |
| `app/decisiones.py` | 1111 | Lo que el equipo decide sobre un pedido y el remito. | `telefono_del_cliente(pedido)` es de acá. `_marcar_sin_reserva` **delega en `solicitudes.soltar_reserva`**: re-leía antes de escribir pero nunca después, así que «la escritura no levantó» se informaba como «el stock está libre» y el rastro durable del pedido lo afirmaba. Devuelve «liberado PROBADO», no «la llamada salió». |
| `app/acciones.py` | 1006 | Una orden en prosa del dueño convertida en UNA acción, confirmada con código de 6 dígitos. | Escribe `[accion]`, el único rastro de que una persona autorizó algo. |
| `app/policy.py` | 968 | `evaluar()`: la lista lineal de reglas de auto-confirmación. | **No se parte** — es el diseño, y partirla es cómo se pierde una regla. `_precio_autorizado` filtra Item Prices por `price_list`, `currency` **y** `uom`. |
| `app/tools/pedidos.py` | ~1180 | Las herramientas de escritura del cliente: `crear_pedido`, `crear_cliente`, `escalar_a_humano`, `recordar`, `dar_de_baja_pedido`. | Los pedidos se crean en **un solo lugar** (`erpnext.create_doc("Sales Order", …)`). `_after_create` tiene **tres salidas** y `crear_pedido` **cuatro** que ni la tocan (idempotencia y recuperación): nada que se escriba ahí puede asumirse escrito una sola vez. **Definir el `@tool` no lo hace alcanzable**: el agente se arma de `TOOLS_CLIENTES` en `app/graph.py` al importar, así que una herramienta sin registrar se contesta con «esa herramienta no existe» y TODOS sus tests de comportamiento pueden estar verdes igual. «No encontré el pedido» es UNA sola frase para «no existe» y «no es tuyo», construida en un solo `return`: dos tokens distintos dejan enumerar pedidos ajenos. |
| `app/notificar.py` | ~720 | El embudo único de todo lo que lee una persona del equipo. | `avisar_dueno` avisa un fallo con un **retorno falso**, no con excepción. `NOTIFICAR_SOLO_PRIMERO` manda al **primer teléfono alfabético**, que puede no ser el dueño: para el dueño, `avisar_dueno`. Cuerpo capado a 1024 por Meta. |
| `app/autonomia.py` | 673 | Cuánto se confirma solo y qué lo frena. | Lee `pendientes._zona()` (un privado). |
| `app/marcas.py` | ~700 | **El registro tipado de los 14 marcadores durables** y el único lector/escritor. | Toda marca nueva es una fila de `_FILAS`: el `order_by` **se deriva** de `lectura` y el parser de `texto`, así que no se pueden separar. `porque_el_techo` es obligatorio. Agregar una fila rompe **3 tests escritos a mano** — actualizarlos a mano es el punto, generarlos los mata. Los tres son: la tabla `TEXTOS_DURABLES` y el censo `== 12` de `tests/test_marcas.py`, y el censo `== 14` de `tests/test_idioma_cobertura.py` — cuyo `set(...) == {...}` SALE del registro y por eso no se toca. `escribir` **no** deduplica (es un `add_comment` pelado): lo exactamente-una-vez lo pone el que llama. |
| `app/pendientes.py` | ~620 | El barrido de borradores esperando a una persona: sombra, recordatorio y cierre. | `en_silencio()` es la **única** implementación de las horas de silencio (22:00–07:00, ventana que cruza la medianoche). **La guarda nocturna tapa `_avisar` y NO `_cerrar`**: soltar una reserva no le habla a nadie, así que no espera — taparlo dejaba el stock tomado hasta las 07:00. `_tiene_marca` tiene **tres** respuestas y hay que comparar con `is not False`. El cierre delega en `agenda.ejecutar_ahora`. Todo apagado por default (`NINGUNO`). |
| `app/erpnext.py` | 604 | El cliente REST. **Tres identidades**: cliente, gerencia y política. | Un hilo de fondo **no tiene scope** y cae en la clave de *cliente*: por eso todo lector de fondo llama `policy_get_*` explícitamente. Un 404 y una caída son **la misma excepción**. `_list` levanta en vez de devolver `[]`. `get_list` **default limit=20**. |
| `app/agenda.py` | ~1700 | **La lista durable de cosas que vencen más tarde.** Una fila = `(id, sobre, tipo, vence, estado, params)`; un barrido; handlers de ~10 líneas. | Los cinco conceptos —exactamente-una-vez, horas de silencio, re-leer, fallar cerrado, lote acotado— viven acá y **sólo** acá. Un comportamiento nuevo es una fila, no un módulo. `sobre` es **siempre** un Sales Order. **Un hecho con dos consumidores son DOS filas**: la baja que pide un cliente suelta la reserva (`baja_de_pedido`, fuera de `_HABLAN_CON_ALGUIEN`, corre a las 3 AM) y recién con la reserva PROBADA crea la fila que le habla al dueño (`aviso_baja_al_dueno`, adentro, espera a las 07:00). Una sola fila tendría que elegir, y las dos respuestas son correctas para su mitad. Un handler que devuelve `Resultado` termina la fila; `None` la deja viva y se reintenta; `reprogramar`+`params` es lo único que persiste estado entre intentos — y es lo que hace reanudable un efecto irreversible: `baja_de_pedido` guarda `fase="soltada"` apenas suelta la reserva, así que la marca y el aviso se reintentan SIN volver a cerrar nada y la fila no termina hasta que los dos quedaron durables. **El único orden de locks anidados del repo vive acá**: `_despachar` toma `pendiente:{sobre}` o `agenda:{sobre}`, y `baja_de_pedido` pide `solicitud:{sobre}` ADENTRO — porque re-leer la solicitud sin ese lock sólo achica la ventana en la que una aceptación hace Submit sobre el pedido que se está cerrando. No se traba porque va en una sola dirección: `pendiente:` se toma sólo en este módulo y ninguna función que tome `solicitud:` llega a despachar una fila. **Si alguna vez una lo hace, esto se abraza.** **Nada que venga después de un efecto irreversible puede ser «best effort» si la fila termina igual**: el reintento ya no puede distinguirse de una baja nueva — y cada evento gasta del techo 80 de `[agenda]`, así que un reintento por minuto se come la historia del pedido. **«¿Sigue vivo?» son DOS preguntas, no una**: `docstatus` 1/2, *y* un borrador cerrado (`docstatus=0` + `status` en `ESTADOS_SIN_RESERVA`, que incluye «On Hold»). `por_que_ya_no_vive(doc)` es la única definición y todo handler pasa por ella; mirar sólo `docstatus` fue un bug real que le avisaba a un cliente la hora de entrega de un pedido rechazado. **Un efecto y el mensaje que lo cuenta son DOS filas** también en el cierre: `cierre_borrador` suelta la reserva y corre a las 3 de la mañana, `aviso_cierre` habla y espera a las 07:00 — sólo el segundo está en `_HABLAN_CON_ALGUIEN`. `ejecutar_ahora` despacha también la fila que su fila dejó atrás, **la que dice `_HIJAS` y ninguna otra, y por id**; sin eso barría toda fila vencida del pedido, fuera de turno y fuera de `POR_RONDA`. La fila `aviso_cierre` vence en **0.0** para que su id sea estable: así `_asegurar_aviso_de_cierre` reintenta el `crear` sin dejar una gemela, que serían dos mensajes al mismo cliente. **Un handler sólo pone `ya_paso` si hizo algo irreversible** — `_aviso_cierre` no lo pone, porque lo único que aporta es el mensaje. Una fila que se reprograma compara **su propio** momento con `ahora`, nunca el plazo del que se deriva. `_cachear` es un **compare-and-set en Lua**: comparación, índice y blob en un script, con desempate por `sello` —y con sellos IGUALES lo terminal le gana a lo pendiente, porque una fila creada y despachada en el mismo acto tiene sus dos eventos con el mismo sello—. El doble de `tests/fakes.py` **replica** ese script en Python y por eso no puede discrepar con él: lo que lo prueba es `tests/test_agenda_redis.py`, contra un Redis de verdad. |
| `app/voz/` | 440 | **El segundo canal: el mismo agente de clientes atendiendo el teléfono.** Cuatro archivos: `herramientas.py` traduce `TOOLS_CLIENTES` a JSON Schema y despacha; `prompt.py` usa `SYSTEM_ES_AR` ENTERO y le agrega un bloque abajo; `identidad.py` decide quién llama; `agente.py` arma un `AgentDefinition` **por llamada**. | **No es un agente nuevo**: si te dan ganas de agregarle una herramienta «sólo para voz», eso es la deriva que el módulo existe para impedir — la lista sale de `app/tools/registro.py` y el test que lo exige es `test_la_voz_expone_exactamente_el_registro_de_clientes`. **Un `AgentDefinition` por llamada, no uno por proceso**: `run_tool` recibe `(nombre, argumentos)` y nada más, así que la identidad va en una clausura; uno construido al importar le sirve el contexto del primer cliente del día a todos los demás, y en una demo de una llamada se ve perfecto. **El nombre del cliente NO entra al prompt** — el relay manda un solo `system_prompt` y no acepta mensajes previos, así que no existe el lugar de menor prioridad donde `conversacion.mensaje_perfil` lo pone en WhatsApp. **La identidad de voz es más débil que la de WhatsApp**: el `caller_id` lo pone la red del que llama y se falsifica, así que el alcance es SIEMPRE `customer` y un número del equipo se rechaza (`LlamadaDeEquipo`). **Gerencia no entra por acá nunca**: los cuatro dígitos de un ajuste no pueden entrar al contexto del modelo y por teléfono habría que decirlos en voz alta. La dependencia `calling_agent` se importa **adentro de la función**: el resto del módulo se testea sin ella. **El factory NUNCA levanta**: el relay trata un factory que falla como «servime el agente de fábrica», y el de fábrica es el de RESTAURANTE — con ERPNext caído un segundo, el que llamaba a la distribuidora escuchaba a una recepcionista ofreciéndole mesa. Lo encontró un arranque de verdad; ningún test lo veía porque todos mockean el lookup. **Un ERPNext caído NO es «no tiene cuenta»**: son dos respuestas con costos opuestos según quién llama, y por eso `_ficha_y_si_se_pudo` devuelve las dos cosas. `VOZ_NUMERO_POR_PARAMETRO` es de DEMO y va apagado: un número por URL da de alta y pide para sí mismo, y **nunca abre una cuenta que ya existe**. `verificar.py` (`python -m app.voz.verificar`) mira lo que `/healthz` no puede: healthz contesta 200 con el agente de restaurante servido. **Un `caller_id` no es nadie sin `VOZ_CONFIA_EN_CALLER_ID`** (apagado por default): no alcanza con negarle el `customer_code`, porque `_cuenta_del_remitente` resuelve la cuenta POR TELÉFONO cuando no hay código — son las dos cosas o ninguna. **El `inbound_message_id` se arma por TOOL CALL y no por llamada**: `crear_pedido` lo hashea en `po_no`, y con un id por llamada el SEGUNDO pedido del cliente devolvía el primero, antes de mirar las líneas nuevas. |
| `app/tools/registro.py` | 148 | **QUIÉN puede llamar a QUÉ**: `TOOLS_CLIENTES`, `TOOLS_GERENCIA` y los dos textos que se le dicen al modelo cuando una herramienta no está o falló. | Vivía en `app/graph.py` y se movió al aparecer el segundo canal: **dos canales que arman su propia lista se desincronizan en la primera herramienta nueva y nadie se entera**. `graph.py` las re-exporta, así que media suite las sigue importando de ahí. Importarlo **no** arrastra Redis, que es lo que deja testear `app/voz/` sin RediSearch. Los comentarios de cada fila son el punto: dicen por qué esa herramienta es segura del lado del cliente. |
| `app/avisos.py` | 501 | La cola durable de salida al **cliente** (ZSET, score = cuándo vence). | `encolar` es **fail-closed**: si Redis no toma la escritura, **levanta**; no devuelve False. `False` significa *ya lo cubrió otro*. `encolar_equipo` usa la misma escala: True = al menos un destinatario **cubierto** (encolado ahora o ya encolado antes), False = *nadie se enteró y nadie se va a enterar*. Contar sólo lo recién escrito hacía que todo reintento reportara fracaso. Deduplica por `(evento, pedido)` **30 días**: dos filas del mismo tipo sobre el mismo pedido necesitan el id en el evento. |
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
11. **Un canal nuevo hereda las reglas sólo si NO las copia.** `app/voz/prompt.py`
    usa `SYSTEM_ES_AR` entero y agrega abajo. Una copia editada a mano habría
    quedado desactualizada en el primer cambio de una regla, y el test que lo
    ata (`test_el_prompt_de_voz_lleva_las_reglas_enteras_y_sin_editar`) compara
    el texto, no una frase suelta.
12. **Lo que en WhatsApp es la constancia, por teléfono no existe.** El texto que
    escribió el cliente queda; lo que dijo, no — queda una transcripción que
    puede haber oído «cincuenta» donde dijo «quince», y el original no lo ve
    nadie. Por eso el bloque de voz obliga a repetir producto, cantidad, unidad
    y fecha ANTES de `crear_pedido`. No contradice la regla 3: la regla 3
    prohíbe pedir permiso para cargar un borrador; esto comprueba que se oyó
    bien, con el cliente todavía en la línea para desmentirlo.
