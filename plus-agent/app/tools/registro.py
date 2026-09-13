"""QUIÉN puede llamar a QUÉ. Un registro, dos canales.

Esto vivía adentro de `app/graph.py`, que construye el checkpointer de Redis al
importarse. Mientras WhatsApp era el único canal eso daba igual; con un segundo
canal (`app/voz/`) dejó de darlo, por dos razones:

  * la que importa: un canal que arma su PROPIA lista de herramientas se
    desincroniza del otro en la primera herramienta nueva, y nadie se entera.
    `dar_de_baja_pedido` se agregó a un solo lado y el cliente que llama por
    teléfono no puede darse de baja lo que sí puede por WhatsApp. Una lista, dos
    canales, y un test que lo exige (`tests/test_voz.py`);
  * la incidental: importar la lista ya no arrastra un Redis.

`app/graph.py` las sigue exportando, así que todo lo que las importaba de ahí
—tests incluidos— no cambia.

LOS COMENTARIOS DE ABAJO SON EL PUNTO. Cada uno dice por qué esa herramienta es
segura del lado del cliente, o por qué no puede estarlo. Se movieron enteros: un
registro sin ellos es una lista de nombres y la próxima sesión vuelve a decidir
desde cero qué puede tocar un desconocido.
"""

from app.tools.captura import (
    confirmar_entrega,
    contar_stock,
    redactar_mensaje_cliente,
    registrar_venta_offline,
)
from app.tools.catalogo import (
    buscar_producto,
    consultar_stock,
    estado_pedido,
    pedido_habitual,
)
from app.tools.configuracion import (
    historial_limites,
    proponer_limite,
    ver_limites,
    ver_reglas_de_entrega,
)
from app.tools.gerencia import (
    cobranzas_vencidas,
    ejecutar_reporte,
    ficha_cliente,
    pedidos_pendientes,
    resumen_autonomia,
    stock_bajo,
    ventas_del_periodo,
)
from app.tools.gestion import (
    detalle_de_pedido,
    proponer_accion,
)
from app.tools.operaciones import (
    estado_del_sistema,
    ver_avisos_fallidos,
)
from app.tools.pedidos import (
    crear_cliente,
    crear_lead,
    crear_pedido,
    dar_de_baja_pedido,
    escalar_a_humano,
    pedir_excepcion_de_entrega,
    recordar,
)


TOOLS_CLIENTES = [
    buscar_producto, consultar_stock, estado_pedido, pedido_habitual,
    # crear_cliente da de alta al REMITENTE con el teléfono del webhook: no
    # acepta un teléfono como argumento, así que ningún mensaje puede pedir
    # el alta de otra persona.
    crear_cliente, crear_lead, crear_pedido, escalar_a_humano,
    # Pide una excepción de entrega. NO decide: o el dueño la dejó autorizada
    # de antemano, o abre una solicitud para una persona (app/solicitudes.py).
    pedir_excepcion_de_entrega,
    # Anota una fila en app/agenda.py para volver sobre un pedido más tarde.
    # PROPONE: el tipo lo fuerza Python a `seguimiento`, la fecha va acotada al
    # horizonte y el motivo se guarda como DATO que lee una persona del equipo.
    # Lo peor que puede causar es un mensaje al equipo que no hacía falta —
    # nunca una confirmación, un submit, una cancelación ni plata.
    recordar,
    # El cliente se da de baja su propio BORRADOR. No escribe nada privilegiado:
    # comprueba que el pedido es suyo, que es un borrador y que no hay una
    # decisión en curso, y anota una fila de app/agenda.py con la credencial de
    # CLIENTE. Quien cierra el borrador es el barrido, con la de política —
    # ninguna herramienta la alcanza. NUNCA en TOOLS_GERENCIA: el equipo cancela
    # por el router determinista, con código, y sobre pedidos confirmados.
    dar_de_baja_pedido,
]

TOOLS_GERENCIA = [
    pedidos_pendientes, ventas_del_periodo, stock_bajo,
    cobranzas_vencidas, ficha_cliente, ejecutar_reporte,
    buscar_producto, consultar_stock, estado_pedido,
    escalar_a_humano,
    # offline capture — how reality gets back into the system
    registrar_venta_offline, contar_stock, confirmar_entrega,
    redactar_mensaje_cliente,
    # the owner's own limits: read them out and PROPOSE a change. There is no
    # tool that confirms one, deliberately — the four-digit code never enters
    # this agent's context and the deterministic router in app/main.py is what
    # applies the change. An agent that could call both steps is one step.
    # NEVER in TOOLS_CLIENTES — a customer cannot be allowed near these.
    ver_limites, proponer_limite, historial_limites,
    # ...and his delivery rules, through the SAME propose/confirm pair. Reading
    # them is its own tool; changing one is proponer_limite like everything else.
    ver_reglas_de_entrega,
    # read-only operational status. No writes, no retries, no secrets, and
    # NEVER in TOOLS_CLIENTES: these count queues and name the provider.
    estado_del_sistema, ver_avisos_fallidos,
    # ...and the owner's prose about ONE order, turned into ONE action that
    # already exists (app/acciones.py). Reading is done on the spot; anything
    # that writes is only PREPARED here and confirmed by him with a six-digit
    # code this agent never sees — the same shape as proponer_limite, and for
    # the same reason. NEVER in TOOLS_CLIENTES: a customer near these is a
    # customer deciding his own order.
    detalle_de_pedido, proponer_accion,
    # ...and the numbers he needs to decide whether to loosen anything
    # (app/autonomia.py). Read-only, and it reports rather than advises.
    resumen_autonomia,
]


# Lo que se le dice al MODELO cuando una herramienta no está o falló. Vive acá
# por lo mismo que las listas: los dos canales tienen que decir lo mismo. Un
# canal con su propia frase es un canal donde el modelo, ante el mismo fallo,
# elige otra salida — y la salida que importa es «llamá a escalar_a_humano y no
# le hables al cliente de sistemas».
HERRAMIENTA_INEXISTENTE = (
    "Esa herramienta no existe para esta conversación y no la vas a conseguir "
    "pidiéndola de nuevo. NO le muestres al cliente este mensaje, ni nombres "
    "herramientas, sistemas ni errores. Si lo que pide lo tiene que ver una "
    "persona, usá escalar_a_humano; si no, seguí con lo que sí podés hacer."
)

# Una herramienta que levanta deja un AIMessage sin su ToolMessage y rompe el
# hilo para siempre: en WhatsApp, ese cliente no se puede volver a contestar
# hasta que alguien limpie Redis a mano. Por teléfono el daño es menor —la
# llamada se corta— pero la respuesta correcta es la misma, así que es el mismo
# texto. Un fallo de herramienta se convierte SIEMPRE en un resultado normal.
ERROR_DE_HERRAMIENTA = (
    "Esa herramienta falló y no devolvió nada. No inventes un resultado. Llamá a "
    "escalar_a_humano y decile al cliente, en UNA línea y con UNA sola disculpa, "
    "que eso lo va a ver el encargado. No le hables de herramientas, de sistemas "
    "ni de errores técnicos."
)
