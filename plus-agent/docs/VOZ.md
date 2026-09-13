# VOZ — el agente de clientes atendiendo el teléfono

Es el **mismo** agente de clientes de WhatsApp con otra puerta de entrada. Las
herramientas son `TOOLS_CLIENTES` sin agregar ni sacar una, las reglas son
`app/prompts.py` palabra por palabra, y las credenciales de ERPNext son las de
siempre. Lo único propio del canal es cómo se habla y quién resulta ser el que
llama.

---

## Cómo correrlo

El relay y la interfaz del navegador viven en el repo `the-calling-agent`. Este
repo aporta el agente; aquél lo sirve.

En el servidor, es un servicio más del compose, con su propio perfil:

```bash
docker compose --profile voz up -d --build voz     # http://localhost:8082
```

En local:

```bash
pip install -r requirements-voz.txt      # requirements.txt + el relay, pinneado
export ASSEMBLYAI_API_KEY=...            # el único secreto que agrega la voz
export AGENT_FACTORY=app.voz.agente:desde_navegador
export PYTHONPATH=$PWD
python -m calling_agent.main
```

**El relay va aparte de la imagen del `agente`, a propósito.** Esa imagen
atiende el webhook de WhatsApp y corre los cuatro hilos de barrido; no tiene por
qué cargar el relay ni tener `git` para instalarlo desde un pin de commit. Por
eso `requirements-voz.txt` existe y `requirements.txt` no lo menciona.

Abrí la página, poné tu número si querés que te tome un pedido, y tocá
**Llamar**. `localhost` es el único origen que
los navegadores eximen de HTTPS para el micrófono; en cualquier otro lado hace
falta un certificado de verdad.

`AGENT_FACTORY` es un `módulo:función` que el relay llama **una vez por
conexión**. Por eso cada llamada tiene su identidad y su id de idempotencia, y
por eso dos pestañas abiertas a la vez no se pisan.

| Variable | |
|---|---|
| `ASSEMBLYAI_API_KEY` | Obligatoria. |
| `AGENT_FACTORY` | `app.voz.agente:desde_navegador`. Vacío = el agente de restaurante del otro repo. |
| `VOZ_AGENTE` | Voz de AssemblyAI. Default `diego` (multilingüe, español rioplatense). |
| `NOMBRE_NEGOCIO` | Sale en el saludo. |
| `VOZ_NUMERO_POR_PARAMETRO` | **Demo. Apagado por default.** Ver abajo. |
| `VOZ_CONFIA_EN_CALLER_ID` | Si el número de la telefonía alcanza para SER un cliente. **Apagado por default.** Ver abajo. |

---

## La página

`plus-agent/static_voz/index.html`, y el `Dockerfile.voz` la pone en lugar de la
del relay. La del relay es el sitio de un producto de restaurantes —«tableline»,
«Busy tables», «Ask for a table»—: servírsela a un cliente de una distribuidora
es el mismo error que el agente de restaurante atendiendo el teléfono, una capa
más arriba.

Sólo se reemplaza el `index.html`. El núcleo —websocket, reconecte, audio— sigue
siendo `js/call-session.js` y `js/audio.js` del relay, pinneados con él. Lo que
no se reusa es `js/app.js`, que es el controlador de esa landing.

El nombre del negocio lo trae `/experience` (el compose le pasa `NOMBRE_NEGOCIO`
al relay como `RESTAURANT_NAME`), así que la misma página sirve para cualquier
cliente sin tocar el HTML.

`scripts/test-voz-ui.mjs` la corre contra un DOM falso, sin navegador. Prueba lo
que falla en silencio: que el número tipeado viaje en el `passthrough` y no en
la conversación, que sin número no se mande un parámetro vacío, y que la
conversación se vea venga el texto en `text` o en `transcript` —el proveedor usa
uno u otro según el mensaje, y contemplar sólo uno deja la pantalla en blanco
mientras la llamada anda perfecto—.

## Qué puede hacer y qué no

Las once herramientas de `TOOLS_CLIENTES`: catálogo, stock, estado de un pedido,
pedido habitual, alta de cliente, alta de lead, crear pedido, escalar a una
persona, pedir una excepción de entrega, anotar un seguimiento y darse de baja un
borrador propio.

**Gerencia no entra por voz, y no es una decisión de producto.** Un ajuste del
dueño se confirma con un código de cuatro dígitos que —regla dura 3— no puede
entrar al contexto del modelo. Por teléfono habría que decirlo en voz alta y el
reconocimiento lo pondría adentro de la transcripción, que es exactamente el
contexto del modelo. Lo mismo con los seis dígitos de `app/acciones.py`. Mientras
el código viaje por la voz del que llama, esto es sólo de clientes.

---

## La identidad es más débil acá, y el código lo sabe

En WhatsApp el número lo firma Meta. En una llamada lo pone la red del que llama,
y en telefonía se falsifica sin equipo especial. De ahí las tres reglas de
`app/voz/identidad.py`:

* el alcance es **siempre** `customer`, sin parámetro para cambiarlo;
* un número del equipo **no entra**: `LlamadaDeEquipo`. Al equipo lo atiende
  WhatsApp, con el router determinista y los códigos;
* **un `caller_id` no es nadie mientras el dueño no lo diga**
  (`VOZ_CONFIA_EN_CALLER_ID`, apagado por default).

### Por qué el caller_id no alcanza solo

Este módulo decía que el número se falsifica y después lo usaba igual para
resolver la cuenta. Con eso, un desconocido que marcaba el número de la
panadería le leía los pedidos y le cargaba otros. Lo cazó una review.

Y no alcanzaba con no darle el `customer_code`:
`tools/pedidos.py::_cuenta_del_remitente` resuelve la cuenta **por teléfono**
cuando no hay código, así que entregar el número es entregar la cuenta. Son las
dos cosas o ninguna, y el default es ninguna.

Encenderlo es decidir que el `caller_id` de tu operador alcanza — una decisión
del dueño, como el tope de auto-confirmación. Lo que se gana es que un cliente
conocido pida sin repetir quién es; lo que se arriesga es que cualquiera que
sepa su número pueda hacerlo por él. Para un pedido que igual revisa una
persona puede valer la pena; con `AUTO_CONFIRM_MAX` arriba de cero, mucho
menos.

En el navegador no hay número de ninguna clase. `de_navegador()` no le da cuenta
a nadie: catálogo, stock y alta, que es exactamente lo que puede hacer un
desconocido por WhatsApp.

### El número por parámetro (`VOZ_NUMERO_POR_PARAMETRO`), que es de demo

Sin número no se puede tomar un pedido: `crear_cliente` da de alta al que llama
con su teléfono verificado, y sin teléfono no hay a quién dar de alta. Así que
para mostrar un pedido de punta a punta desde el navegador hay una sola puerta,
y está apagada por default:

```
http://localhost:8082/?telefono=5493511234567
```

El parámetro viaja en la URL del websocket. **No sale de la conversación y el
modelo no lo ve**: se fija antes de que el que llama diga una palabra, ninguna
herramienta lo acepta como argumento, y por eso nadie puede hablar para
cambiarlo. Es la misma forma que tiene el número en WhatsApp —de afuera del
mensaje— con una firma mucho peor: ahí lo firma Meta, acá lo escribió el que
abrió la página.

De ahí las tres cosas que no hace, ni encendido:

* no corre con el flag apagado, que es el default y lo que va en producción;
* no atiende un número del equipo;
* **no abre la cuenta de un cliente que ya existe.** Ésta es la que importa: si
  la abriera, cualquiera escribiría el número de la panadería y le leería los
  pedidos. Un número declarado da de alta y pide para sí mismo, nunca lee lo de
  otro. Con ERPNext caído tampoco da de alta, porque no se pudo descartar que la
  cuenta exista.

Para un cliente de verdad esto se apaga y el número lo trae el `caller_id` de la
telefonía (`agente.desde_telefono`), que es de la red y no del que llama.

---

## Lo único que la voz agrega a las reglas

Una fuente de error que WhatsApp no tiene: el cliente dijo «quince» y el
reconocimiento escuchó «cincuenta». El texto del cliente **no existe**; lo único
que queda es una transcripción que puede estar mal, y nadie ve el original.

El control no es la cola de borradores: es **repetir el pedido antes de
cargarlo**, con el cliente en la línea para desmentirlo en el segundo en que
pasa. Está en `BLOQUE_VOZ`, abajo de las reglas y sin tocar ninguna.

No contradice la regla 3. La regla 3 prohíbe pedir permiso para cargar un
borrador («¿te lo cargo?») porque cargarlo no compromete nada. Repetir no es
pedir permiso: es comprobar que se oyó bien. Por eso se repiten los números y la
fecha, y no se pregunta si se carga.

Vale la pena decirlo al derecho: en esto la voz es **mejor** que WhatsApp, donde
nadie le lee el pedido de vuelta a nadie. Es el único canal donde hay una persona
escuchando en el momento exacto en que se comete el error.

---

## La postura de auto-confirmación no cambia por ser voz

`AUTO_CONFIRM_MAX` y `STOCK_CONFIABLE` valen para los dos canales, y la voz no
pide una excepción. Las reglas de `policy._evaluar` protegen contra un problema
de precio, stock, monto o historial; ninguna modela el error de transcripción.
La que lo atrapa de costado es el tope: cincuenta kilos en vez de quince se pasa
del tope y lo mira una persona. Un error que **achica** el pedido pasa igual —y
por eso la repetición es obligatoria, no un lujo de prolijidad.

---

## Después de un deploy

```bash
docker compose --profile voz exec voz python -m app.voz.verificar
```

`/healthz` contesta 200 aunque `AGENT_FACTORY` no resuelva, porque el relay
atiende igual —con SU agente de fábrica, el de restaurante—. O sea que el modo
de falla que importa es invisible para un healthcheck. Esto lo mira: que el
agente sea el del CRM, que el prompt tenga sus cuatro bloques, que las
herramientas sean el registro de clientes y que la demo del número por parámetro
no se haya quedado encendida. No llama a AssemblyAI ni gasta una llamada. Lo
corre también el job `imagen-voz` de CI, contra la imagen recién construida.

## Un reconecte es la misma llamada

El relay llama al factory en CADA conexión, reconectes incluidos, y de su
`id_llamada` sale la clave de idempotencia de `crear_pedido`. Con un id nuevo,
el socket que se cae y vuelve convierte un reintento del mismo pedido en un
pedido nuevo: el cliente pide una vez y le entran dos. Por eso el id sale de
`?resume=` cuando viene —el id de la sesión upstream que el navegador
reanuda—, que es exactamente «la misma llamada».

## Una caída de ERPNext no cambia de agente

Lo encontró un arranque de verdad del servidor, no un test: con ERPNext apagado
un segundo, la excepción del lookup subía hasta el relay, que trata un factory
que falla como «servime el agente de fábrica» — **y el de fábrica es el de
restaurante**. El que llamaba a una distribuidora de lácteos escuchaba a una
recepcionista ofreciéndole mesa para dos.

Ahora una caída degrada, no cambia de agente: el que llama queda sin cuenta —
pregunta precios, y lo suyo lo ve una persona. Con `caller_id` conserva además
su teléfono, porque ése lo puso la red y sigue valiendo; lo único que se pierde
es el `customer_code`, que es lo que ERPNext no pudo contestar.

## Cómo se piden los datos que se dictan

Un nombre, una calle y un código postal dichos por teléfono son lo que peor se
entiende: la línea va en 8 kHz y «Laprida» y «la brida» suenan igual. El bloque
`DATOS QUE TE DICTAN` del prompt sigue lo que hace la industria para esto: un
dato por turno, repetir cada uno antes de seguir, el número de la calle aparte
del nombre, el código postal dígito por dígito y despacio, y deletrear la
palabra dudosa en vez de volver a pedir la dirección entera.

## Lo que sigue

**Telefonía.** Hoy el transporte es el navegador. `transport/base.py` en el repo
del relay existe para esto: µ-law de 8 kHz es byte-compatible con Telnyx y Twilio,
así que el camino no transcodifica. `agente.desde_telefono(numero)` ya está
escrito y es el que decide qué significa el `caller_id`; falta el transporte que
lo llame.

**Latencia.** Cada llamada a herramienta es una consulta a ERPNext, y en una
llamada el silencio se oye. El bloque de voz prohíbe el «dejame ver» seguido de
nada; lo que no puede arreglar el prompt es una clave de Gemini en free tier
(punto 1 de los abiertos en `CLAUDE.md`) — acá se nota más que en WhatsApp,
porque un chat espera y un teléfono no.
