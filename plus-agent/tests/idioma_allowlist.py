"""Lo que se deja EN ESPAÑOL a propósito, y por qué cada cosa.

Esta lista es la mitad del trabajo. Traducir todo lo que parece texto es tan
malo como no traducir nada: un estado de ERPNext traducido deja de matchear, un
comando traducido deja de parsear, y una marca de auditoría traducida rompe la
reconstrucción después de un flush de Redis.

La regla es una sola: se traduce lo que LEE UNA PERSONA en WhatsApp. Todo lo
demás es un valor, y un valor no tiene idioma.

Cada entrada de acá tiene que poder explicarse en una línea. Si no se puede,
probablemente sea un texto que había que traducir.
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# 1. LOGS. Los lee quien opera el sistema, en la consola, no un cliente.
#    Traducirlos sólo lograría que la mitad de un archivo de log esté en un
#    idioma y la otra mitad en otro.
# --------------------------------------------------------------------------
LOGS = "toda cadena dentro de print(...)"

# --------------------------------------------------------------------------
# 2. VALORES CANÓNICOS DE ERPNEXT. Son claves, no prosa: se comparan, se
#    filtran y se guardan. "To Deliver and Bill" traducido no matchea nada.
# --------------------------------------------------------------------------
ERPNEXT_CANONICO = (
    "Sales Order", "Delivery Note", "Sales Invoice", "Stock Reconciliation",
    "Customer", "Address", "Item", "Item Price", "Bin", "Company", "Comment",
    "Lead", "ToDo", "Dynamic Link",
    "To Deliver and Bill", "To Bill", "To Deliver", "Completed", "Closed",
    "Draft", "Cancelled", "Submitted",
)

# --------------------------------------------------------------------------
# 3. MARCAS DE AUDITORÍA. Las lee el propio código para distinguir «nunca se
#    configuró» de «se perdió el almacén». Cambiar una rompe la reconstrucción
#    durable, que es justamente lo que evita que algo se auto-confirme después
#    de perder Redis.
# --------------------------------------------------------------------------
MARCAS_DURABLES = ("[limite]", "[entrega]", "[idioma]", "[confirmado-por-agente]")

# --------------------------------------------------------------------------
# 4. COMANDOS. El payload que parsea el router determinista. Siguen en español
#    incluso dentro de un mensaje en inglés: son lo que la gente ya escribe y
#    lo que dicen los mensajes que ya salieron. Los equivalentes en inglés se
#    AGREGARON al parser (app/main.py::_ACEPTA_RE / _RECHAZA_RE y
#    app/acciones.py::ACCIONES), no reemplazaron a los de siempre.
# --------------------------------------------------------------------------
COMANDOS_ES = (
    "confirmar", "rechazar", "cancelar", "ver", "preparar", "despachar",
    "despreparar", "acepto", "no acepto", "ok",
)
COMANDOS_EN_QUE_TAMBIEN_PARSEAN = (
    "accept", "reject", "decline", "yes", "agreed", "deal", "no thanks",
    "not interested",
)

# --------------------------------------------------------------------------
# 5. NOMBRES PROPIOS. No se traducen en ningún idioma.
# --------------------------------------------------------------------------
NOMBRES_PROPIOS = (
    "Redis", "ERPNext", "WhatsApp", "Meta", "Gemini", "Qwen", "DashScope",
)

# --------------------------------------------------------------------------
# 6. COMENTARIOS ESCRITOS SOBRE UN DOCUMENTO DE ERPNEXT. Son el rastro de
#    auditoría que alguien lee EN ERPNext, no un mensaje de WhatsApp. Quedan en
#    el idioma del sistema contable, que es uno solo.
# --------------------------------------------------------------------------
COMENTARIOS_ERPNEXT = "add_comment(...) y registrar_comentario(...)"

# --------------------------------------------------------------------------
# 7. TEXTO QUE LEE EL MODELO, NO UNA PERSONA. Lo que devuelve una @tool de
#    app/tools/ vuelve al modelo, que después redacta la respuesta en el idioma
#    del destinatario — eso ya lo gobierna el prompt. Traducir el valor de
#    retorno no cambiaría lo que recibe nadie, y duplicaría el trabajo.
#    EXCEPCIÓN: los informes que el modelo suele repetir tal cual (estado del
#    sistema, avisos caídos) SÍ se migraron, para que no dependan de que el
#    modelo los repita bien.
# --------------------------------------------------------------------------
RETORNOS_DE_HERRAMIENTA = "app/tools/*.py, salvo los informes ya migrados"

# --------------------------------------------------------------------------
# 8. EL TEXTO DEL CLIENTE, CITADO. Nunca se toca: es un dato, y reescribirlo
#    sería inventar lo que dijo. Ver app/formato.py::sin_citas y
#    solicitudes.texto_para_equipo.
# --------------------------------------------------------------------------
CITAS_DEL_CLIENTE = "el mensaje del cliente, citado con «>»"

# --------------------------------------------------------------------------
# 9. MENSAJES DE EXCEPCIÓN QUE SÓLO VAN AL LOG. Si alguna vez una de éstas
#    empieza a mostrarse a una persona, hay que darle una clave del catálogo:
#    LimiteError ya soporta `clave` justamente para eso.
# --------------------------------------------------------------------------
EXCEPCIONES_INTERNAS = "las que ningún handler interpola en una respuesta"


# Lo que un texto EN INGLÉS puede contener en español sin que sea un error.
# Se usa en el guard de cobertura.
PERMITIDO_EN_SALIDA_INGLESA = (
    ERPNEXT_CANONICO + MARCAS_DURABLES + COMANDOS_ES + NOMBRES_PROPIOS
)
