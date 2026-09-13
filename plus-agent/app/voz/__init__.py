"""El canal de voz: el mismo agente de clientes, atendiendo el teléfono.

NO ES UN AGENTE NUEVO. Es el de siempre, con otra puerta de entrada. Las
herramientas son `TOOLS_CLIENTES` (app/tools/registro.py) sin agregar ni sacar
una sola, las reglas son las de `app/prompts.py` palabra por palabra, y la
identidad se resuelve contra el mismo ERPNext. Lo único propio de este canal es
CÓMO SE HABLA —en voz, a alguien que está esperando en la línea— y eso vive en
`app/voz/prompt.py`, abajo de las reglas y sin tocarlas.

POR QUÉ NO PASA POR `app/graph.py`
El relay de voz (la API de agentes de AssemblyAI) hospeda el modelo y el turno:
manda `tool.call` y espera `tool.result`. No hay forma de meterle un texto ya
generado, así que `responder_cliente()` no se puede usar desde acá: no existe el
mensaje del protocolo que hablaría su respuesta. Lo que sí se comparte —y es lo
que importa— son las herramientas, sus credenciales y las reglas.

LO QUE ESTE CANAL AGREGA Y NINGUNA REGLA CUBRÍA
Una fuente de error que WhatsApp no tiene: el cliente dijo «quince» y el
reconocimiento escuchó «cincuenta». El texto del cliente no existe —lo único que
queda es una transcripción que puede estar mal, y nadie ve el original—. El
control no es la cola de borradores: es que el agente REPITA producto, cantidad,
unidad y fecha antes de llamar a `crear_pedido`, con el cliente en la línea para
desmentirlo en el segundo en que pasa. Está en el bloque de voz del prompt.

QUÉ NO ENTRA ACÁ, NUNCA
Gerencia. Un ajuste del dueño se confirma con un código de cuatro dígitos que
—regla dura 3 de CLAUDE.md— no puede entrar al contexto del modelo. Por teléfono
el dueño tendría que decirlo en voz alta y el reconocimiento lo pondría adentro
de la transcripción, que es exactamente el contexto del modelo. Lo mismo con los
seis dígitos de `app/acciones.py`. Mientras el código viaje por la voz del que
llama, este canal es sólo de clientes.
"""
