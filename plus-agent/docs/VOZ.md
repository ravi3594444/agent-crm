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

```bash
pip install -r requirements.txt          # trae `calling-agent`, pinneado
export ASSEMBLYAI_API_KEY=...            # el único secreto que agrega la voz
export AGENT_FACTORY=app.voz.agente:desde_navegador
export PYTHONPATH=/srv/agent-crm/plus-agent
python -m calling_agent.main
```

`http://localhost:8080` y **Start conversation**. `localhost` es el único origen
que los navegadores eximen de HTTPS para el micrófono; en cualquier otro lado
hace falta un certificado de verdad.

`AGENT_FACTORY` es un `módulo:función` que el relay llama **una vez por
conexión**. Por eso cada llamada tiene su identidad y su id de idempotencia, y
por eso dos pestañas abiertas a la vez no se pisan.

| Variable | |
|---|---|
| `ASSEMBLYAI_API_KEY` | Obligatoria. |
| `AGENT_FACTORY` | `app.voz.agente:desde_navegador`. Vacío = el agente de restaurante del otro repo. |
| `VOZ_AGENTE` | Voz de AssemblyAI. Default `diego` (multilingüe, español rioplatense). |
| `NOMBRE_NEGOCIO` | Sale en el saludo. |

---

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
y en telefonía se falsifica sin equipo especial. De ahí las dos reglas de
`app/voz/identidad.py`:

* el alcance es **siempre** `customer`, sin parámetro para cambiarlo. Un
  `caller_id` falsificado consigue, como mucho, lo que consigue un cliente;
* un número del equipo **no entra**: `LlamadaDeEquipo`. Al equipo lo atiende
  WhatsApp, con el router determinista y los códigos.

En el navegador no hay número de ninguna clase. `de_navegador()` no le da cuenta
a nadie: catálogo, stock y alta, que es exactamente lo que puede hacer un
desconocido por WhatsApp. **Dar cuenta por un número tipeado sería identidad
declarada por el que llama**, que es el único tipo que este sistema nunca aceptó
—cualquiera escribiría el número de otro y leería sus pedidos—.

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
