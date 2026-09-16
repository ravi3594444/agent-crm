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
    avisar_al_cliente,
    confirmar_entrega,
    contar_stock,
    registrar_venta_offline,
)
from app.tools.catalogo import (
    buscar_producto,
    consultar_stock,
    estado_pedido,
    pedido_habitual,
)
from app.tools.configuracion import (
    proponer_limite,
    ver_ajustes,
)
from app.tools.crm import (
    actualizar_cliente,
    actualizar_producto,
    anotar_en_ficha,
    armar_presupuesto,
    cambiar_precio,
    editar_borrador,
)
from app.tools.entrega import (
    condiciones_de_entrega,
)
from app.tools.gerencia import (
    ejecutar_reporte,
    ficha_cliente,
    informe,
)
from app.tools.gestion import (
    detalle_de_pedido,
    proponer_accion,
)
from app.tools.memoria import (
    anotar_dato,
    ver_memoria,
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
    # Las condiciones de entrega que configuró el dueño (app/limites.py, grupo
    # ENTREGA), en una sola herramienta. SÓLO LECTURA y sin un solo dato del
    # cliente que termine escrito en ninguna parte. Es lo que hace el negocio
    # EN GENERAL: no promete la entrega de un pedido —eso sigue siendo
    # `pedir_excepcion_de_entrega` más la decisión de una persona— y un ajuste
    # que falta sale como faltante, nunca como un «no repartimos».
    condiciones_de_entrega,
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
    # UNA herramienta para los cinco informes (pendientes, ventas, stock bajo,
    # cobranzas, autonomía). Eran cinco, y `cobranzas_vencidas` era literalmente
    # una de las siete consultas que `ejecutar_reporte` ya corre: dos
    # herramientas plausibles para «¿cuánto me deben?». Lo que degrada la
    # elección es el solapamiento, no la cantidad.
    informe, ficha_cliente, ejecutar_reporte,
    buscar_producto, consultar_stock, estado_pedido,
    escalar_a_humano,
    # offline capture — how reality gets back into the system
    registrar_venta_offline, contar_stock, confirmar_entrega,
    # ...y el mensaje al cliente, que ahora SALE —con el botón del dueño— en
    # vez de devolver un borrador con un hueco para copiar a mano.
    avisar_al_cliente,
    # the owner's own limits: read them out and PROPOSE a change. There is no
    # tool that confirms one, deliberately — the four-digit code never enters
    # this agent's context and the deterministic router in app/main.py is what
    # applies the change. An agent that could call both steps is one step.
    # NEVER in TOOLS_CLIENTES — a customer cannot be allowed near these.
    # ver_ajustes reads all THREE (limits, delivery rules, history) behind one
    # closed Literal; proponer_limite is the only one that writes, and it stays
    # its own tool — a read and a write behind one enum is one where the wrong
    # branch writes.
    ver_ajustes, proponer_limite,
    # ...y las treinta cosas que repite todo el tiempo y no quiere volver a
    # explicar (app/memoria.py). `ver_memoria` LEE detrás de un Literal;
    # `anotar_dato` ESCRIBE, así que va suelta: misma línea que ver_ajustes y
    # proponer_limite. Un dato no es un ajuste y NO lleva código de cuatro
    # dígitos — hacerle tipear un código para anotar «la panadería paga los
    # viernes» es exactamente la fricción de la que se queja.
    ver_memoria, anotar_dato,
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
    # ...y lo que el dueño puede CAMBIAR (app/tools/crm.py). La línea no es
    # leer-contra-escribir —era demasiado ancha— sino IRREVERSIBLE × PLATA:
    # estas cinco se deshacen escribiendo de nuevo y no le cobran un peso a
    # nadie. Lo irreversible sigue afuera y sigue sin ser alcanzable: emitir usa
    # la credencial de política, cancelar un emitido no existe como herramienta,
    # los límites piden su código de cuatro dígitos, y `Item Price` no está en
    # `erpnext.DOCTYPES_EDITABLES` —la negativa es del cliente HTTP, no de la
    # buena conducta de un archivo—.
    #
    # NINGUNA pone un precio: `LineaSimple` no tiene `rate`, igual que
    # `pedidos.LineaPedido`. El precio lo resuelve ERPNext y lo verifica
    # `policy._precio_autorizado`. Una herramienta donde el precio es un
    # argumento del modelo convierte al modelo en la autoridad de precios.
    #
    # LA EXCEPCIÓN, Y LA DECIDIÓ EL DUEÑO: `cambiar_precio` escribe el precio de
    # LISTA. No es el precio de un renglón, pero tampoco es inocente —
    # `policy._precio_estandar` auto-confirma cuando el renglón coincide con la
    # lista, así que quien escribe la lista influye en lo que se confirma solo—.
    # Lo pidió explícitamente («no one can confirm everytime i need automated»)
    # y es su negocio.
    #
    # Lo que sí queda acotado, porque no depende de su permiso sino de cómo se
    # comporta un modelo: el único valor que el modelo aporta es el NÚMERO
    # —lista, moneda y unidad salen de `policy` y del `stock_uom` leído de
    # ERPNext—; ese número tiene que caer adentro de `PRECIO_CAMBIO_MAX_PCT`; y
    # hay UN cambio por producto por día, porque una banda por llamada no acota
    # una serie y el modelo puede llamar cinco veces en el mismo turno. Con la
    # banda en 0, que es el default, no escribe nada. La puerta genérica sigue
    # cerrada: `Item Price` no está en `erpnext.DOCTYPES_EDITABLES`.
    actualizar_cliente, anotar_en_ficha, armar_presupuesto,
    editar_borrador, actualizar_producto, cambiar_precio,
]

HERRAMIENTA_INEXISTENTE = (
    "Esa herramienta no existe para esta conversación y no la vas a conseguir "
    "pidiéndola de nuevo. NO le muestres al cliente este mensaje, ni nombres "
    "herramientas, sistemas ni errores. Si lo que pide lo tiene que ver una "
    "persona, usá escalar_a_humano; si no, seguí con lo que sí podés hacer."
)

ERROR_DE_HERRAMIENTA = (
    "Esa herramienta falló y no devolvió nada. No inventes un resultado. Llamá a "
    "escalar_a_humano y decile al cliente, en UNA línea y con UNA sola disculpa, "
    "que eso lo va a ver el encargado. No le hables de herramientas, de sistemas "
    "ni de errores técnicos."
)
