"""Un mensaje al cliente que el dueño todavía no aprobó.

POR QUÉ EXISTE
`redactar_mensaje_cliente` (app/tools/captura.py) devuelve un BORRADOR con un
hueco —«[Redactá el mensaje acá…]»— para que una persona lo copie y lo mande a
mano desde su propio WhatsApp. O sea que la frase del dueño «yo le digo
cualquier cosa al manager y él lo hace, por sí mismo o por el agente de
ventas» terminaba, para todo lo que sale hacia un cliente, en copiar y pegar.

Este módulo guarda el mensaje YA REDACTADO mientras espera el visto bueno, y
nada más. No manda: eso lo hace `app/avisos.py`, que ya tiene reintentos, cola
durable, ventana de 24 h y plantilla.

POR QUÉ NO ES UN HANDOFF ENTRE AGENTES
Un handoff de LangGraph traspasa el control DENTRO de un hilo. «Avisale a la
panadería que llegó el queso» cruza dos teléfonos y dos hilos distintos: el del
dueño y el del cliente. Lo que corresponde es un TRABAJO DE SALIDA —esto—, y
además así el agente de gerencia nunca toca las herramientas del de clientes ni
su credencial de ERPNext.

POR QUÉ BOTONES Y NO UN CÓDIGO
Los ajustes se confirman con cuatro dígitos y las acciones sobre un pedido con
seis, y el router de `app/main.py` los distingue POR EL LARGO. Un tercer largo
sería un tercer camino de confirmación sobre el mismo mecanismo, y además el
dueño se queja justamente de tener que tipear códigos. El botón ya existe
(`app/aprobacion.py::manejar_boton`), ya comprueba `es_equipo`, y le muestra el
texto EXACTO que va a salir antes de que lo toque.

DE UN SOLO USO, Y ATÓMICO
`consumir` es un `GETDEL`: el segundo toque del botón no encuentra nada. No
alcanza con borrar después de mandar —entre leer y borrar entran dos toques— y
tampoco alcanza con la idempotencia de `avisos.encolar`, aunque quede de
segunda red: esa es por (evento, pedido), y acá el «pedido» es justamente el id
que este módulo entrega.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass

from app import locks

PREFIJO = "salida:"

# Una hora. No es un número redondo por gusto: un mensaje a un cliente envejece
# —«ya llegó el queso» deja de ser cierto—, y si el dueño no lo aprobó en una
# hora es mejor que el agente lo vuelva a redactar con lo que sepa entonces,
# antes que mandar algo que se escribió sobre otra realidad.
TTL_SEGUNDOS = 3600

# Meta corta el cuerpo de un interactivo en 1024 caracteres y hay que dejarle
# lugar al encabezado con el nombre del cliente.
LARGO_MAXIMO = 700


class SalidaError(Exception):
    """No se pudo guardar el mensaje. Nunca se traga: si esto falla, el dueño
    NO tiene que recibir un botón que no va a funcionar."""


@dataclass(frozen=True)
class Salida:
    id: str
    cliente: str
    telefono: str
    texto: str
    pedida_por: str
    creada: float

    def como_dict(self) -> dict:
        return {
            "id": self.id,
            "cliente": self.cliente,
            "telefono": self.telefono,
            "texto": self.texto,
            "pedida_por": self.pedida_por,
            "creada": self.creada,
        }


def _clave(id_salida: str) -> str:
    return f"{PREFIJO}{id_salida}"


def proponer(cliente: str, telefono: str, texto: str, pedida_por: str) -> Salida:
    """Guarda un mensaje a la espera del visto bueno y devuelve su id.

    Levanta si no pudo guardarlo. El que llama NO tiene que mandar un botón
    cuando esto falla: sería un botón que al tocarlo no encuentra nada.
    """
    texto = (texto or "").strip()
    if not texto:
        raise SalidaError("no hay texto para mandar")
    if not telefono:
        raise SalidaError("no sé a qué número mandarlo")
    salida = Salida(
        # 8 bytes de urandom en hexa (16 caracteres). No es un contador ni el nombre del
        # cliente: el id viaja en el payload de un botón, y un id adivinable
        # deja que un número del equipo apruebe el mensaje de otro sin haberlo
        # visto nunca.
        id=secrets.token_hex(8),
        cliente=cliente,
        telefono=telefono,
        texto=texto[:LARGO_MAXIMO],
        pedida_por=pedida_por,
        creada=time.time(),
    )
    try:
        locks.conexion().set(_clave(salida.id), _cuerpo(salida), ex=TTL_SEGUNDOS)
    except Exception as exc:
        raise SalidaError(f"no pude guardar el mensaje ({type(exc).__name__})") from exc
    return salida


def _cuerpo(salida: Salida) -> str:
    return json.dumps(salida.como_dict(), ensure_ascii=False, separators=(",", ":"))


def devolver(salida: Salida) -> bool:
    """Vuelve a guardar una salida que se consumió y NO salió. True si volvió.

    `consumir` es un GETDEL, así que si lo de después falla —encolar, por
    ejemplo— la propuesta ya no está: el dueño lee «no salió», toca el mismo
    botón y recibe «ya no está». Puede pedir otra, pero no puede REINTENTAR la
    que ya miró y aprobó, que es lo único que quería hacer.

    VUELVE CON SU VENCIMIENTO ORIGINAL, no con uno nuevo. Un TTL fresco
    convertiría cada fallo de encolado en una hora más de vida para una promesa
    que el cliente todavía no recibió, y una propuesta aprobada hace 59 minutos
    que revive por otra hora es un mensaje que sale cuando ya no viene a cuento.
    Lo que queda se calcula desde `creada` y NO se lee de Redis: leerlo habría
    que hacerlo antes del GETDEL, y ahí `consumir` deja de ser una sola
    operación, que es todo su punto.

    Si ya venció, no se restaura y devuelve False: el que llama sabe que esa
    propuesta no vuelve, y el dueño tiene que pedirla de nuevo.

    El `id` NO cambia, y eso es lo que hace segura la reintentada: es la clave de
    idempotencia de la cola, así que un encolado que falló de forma incierta
    —falló al contestar, no al encolar— no manda el mensaje dos veces.
    """
    restante = int(TTL_SEGUNDOS - (time.time() - salida.creada))
    if restante <= 0:
        return False
    try:
        locks.conexion().set(_clave(salida.id), _cuerpo(salida), ex=restante)
        return True
    except Exception as exc:
        print(f"[salidas] no pude devolver {salida.id} ({type(exc).__name__})")
        return False


def _desde_json(crudo: object) -> Salida | None:
    if not crudo:
        return None
    try:
        d = json.loads(crudo if isinstance(crudo, str) else crudo.decode())
        return Salida(
            id=str(d["id"]), cliente=str(d.get("cliente", "")),
            telefono=str(d["telefono"]), texto=str(d["texto"]),
            pedida_por=str(d.get("pedida_por", "")), creada=float(d.get("creada", 0)),
        )
    except Exception:
        return None


def leer(id_salida: str) -> Salida | None:
    """Mira sin consumir. Para mostrar, nunca para mandar."""
    if not id_salida:
        return None
    try:
        return _desde_json(locks.conexion().get(_clave(id_salida)))
    except Exception:
        return None


def consumir(id_salida: str) -> Salida | None:
    """Lee y borra de una. El segundo toque del botón devuelve None.

    `GETDEL` es una sola operación en el servidor: un `get` y un `delete`
    separados dejan pasar dos toques que llegan juntos, que es exactamente lo
    que hace un dedo impaciente en un teléfono.
    """
    if not id_salida:
        return None
    try:
        return _desde_json(locks.conexion().getdel(_clave(id_salida)))
    except Exception:
        return None
