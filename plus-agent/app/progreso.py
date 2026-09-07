"""Un aviso de avance por turno, y sólo cuando hay algo de qué avisar.

LO QUE ESTO REEMPLAZA
Hasta acá, TODO mensaje de texto recibía «Recibido, dame un momento mientras
lo verifico» antes de que nadie mirara nada: lo mandaba el webhook en cuanto
encolaba, y el worker otra vez por si el webhook no había llegado. Un «hola»
producía dos mensajes —el aviso de que se estaba verificando algo que nunca se
verificó, y la respuesta— y un código de cuatro dígitos, que Python atiende en
un segundo, también. El aviso no describía lo que pasaba: describía lo que el
código suponía que iba a pasar.

QUÉ HACE AHORA
Es un callback de LangChain (``BaseCallbackHandler``), el punto de extensión
documentado para observar lo que hace un agente, y viaja en la config de la
invocación (app/graph.py). El agente lo llama cuando de verdad EMPIEZA una
herramienta (``on_tool_start``). Recién ahí se programa UN aviso, que sale sólo
si pasados ``demora`` segundos el turno sigue abierto Y TODAVÍA hay una
herramienta corriendo. Si la herramienta contesta rápido, el aviso no sale: no
importa cuánto tarde después el modelo. Una segunda o tercera herramienta en el
mismo turno —en serie o en paralelo— no programa nada más mientras haya un
plazo esperando.

LA ESPERA DEL MODELO NO ES UNA CONSULTA
El aviso arrancaba con la primera herramienta y nada lo cancelaba cuando ésa
terminaba, así que un turno de una herramienta de 0,1 s y una segunda llamada
al modelo de diez segundos —el caso normal de un pedido, y lo que el log de
producción mostraba: ``modelo=2x10.3s herramientas=1x0.1s progreso=si``— le
mandaba a la persona «estoy consultando el sistema» cuando ya no se estaba
consultando nada. Ahora el plazo mira si el trabajo sigue en vuelo: la latencia
del modelo se mide y se escribe en el log, y no se le cuenta a nadie como si
fuera una consulta. Un plazo que vence sin trabajo en vuelo no gasta el único
aviso del turno: si más adelante otra herramienta tarda de verdad, se arma de
nuevo.

Un turno en que el modelo contesta directo, sin herramientas, no produce
ningún aviso, tarde lo que tarde el proveedor. Eso no esconde la latencia: la
mide (``resumen()``) y el worker la escribe en el log, que es donde se puede
leer sin inventarle a la persona que se está consultando algo.

LO QUE NO ES
No lee el mensaje. No decide si hace falta una herramienta. No clasifica
intenciones. Eso lo hace el modelo; esto sólo reacciona a lo que el modelo
hizo de verdad.

EL ORDEN ES UNA GARANTÍA, NO UNA PROBABILIDAD
``terminar()`` se llama antes de mandar la respuesta final. Cancela el
temporizador y toma el MISMO candado que sostiene el envío del aviso mientras
está en vuelo, así que cuando devuelve, o el aviso ya salió o ya no va a
salir. Un «estoy consultando» después de la respuesta es imposible por
construcción, no por suerte con los tiempos.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler


class Progreso(BaseCallbackHandler):
    """Observa un turno del agente; programa a lo sumo un aviso de avance.

    ``enviar`` es quien manda el aviso (y decide su idioma y su idempotencia);
    devuelve verdadero si el destinatario lo recibió. ``demora`` son los
    segundos entre la primera herramienta y el aviso; negativa, no se manda
    nunca. Es best effort de punta a punta: una excepción al enviar se anota y
    el turno sigue exactamente igual.
    """

    # Un callback que levanta no puede cortar el turno de nadie.
    raise_error = False

    def __init__(self, enviar: Callable[[], object], demora: float) -> None:
        super().__init__()
        self._enviar = enviar
        self._demora = float(demora)
        # Dos candados a propósito. El de envío ordena «aviso» y «final»; el de
        # métricas es barato y no espera a Meta: una herramienta que termina
        # mientras el aviso está en vuelo no tiene por qué quedarse esperando.
        # El orden de toma es SIEMPRE _candado_envio -> _candado, y no se
        # invierte en ningún camino: terminar() los toma uno después del otro,
        # nunca anidados, así que no hay ciclo posible.
        self._candado_envio = threading.Lock()
        self._candado = threading.Lock()
        self._timer: threading.Timer | None = None
        # Cuántas herramientas están corriendo AHORA, y si hay un temporizador
        # esperando su plazo. Las dos cosas juntas son lo que distingue «se
        # está consultando algo» de «estamos esperando al modelo».
        self._en_vuelo = 0
        self._pendiente = False
        self._terminado = False
        self._intentado = False
        self._enviado = False
        # Lo que se mide, para el log del turno.
        self.herramientas: list[str] = []
        self.llamadas_modelo = 0
        self.segundos_modelo = 0.0
        self.segundos_herramientas = 0.0
        self.error_modelo = ""
        self._inicio_modelo: dict[UUID, float] = {}
        self._inicio_herramienta: dict[UUID, float] = {}

    # ------------------------------------------------------------ el modelo

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        with self._candado:
            self.llamadas_modelo += 1
            self._inicio_modelo[run_id] = time.monotonic()

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: Any, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self.on_chat_model_start(serialized, prompts, run_id=run_id, **kwargs)

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        with self._candado:
            inicio = self._inicio_modelo.pop(run_id, None)
            if inicio is not None:
                self.segundos_modelo += time.monotonic() - inicio

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        with self._candado:
            self.error_modelo = type(error).__name__
            inicio = self._inicio_modelo.pop(run_id, None)
            if inicio is not None:
                self.segundos_modelo += time.monotonic() - inicio

    # ------------------------------------------------------ las herramientas

    def on_tool_start(
        self, serialized: dict[str, Any], input_str: str, *, run_id: UUID, **kwargs: Any
    ) -> None:
        nombre = str((serialized or {}).get("name") or kwargs.get("name") or "?")
        with self._candado:
            self.herramientas.append(nombre)
            self._inicio_herramienta[run_id] = time.monotonic()
            self._en_vuelo += 1
            # `_pendiente` dice que ya hay un plazo corriendo: una segunda o
            # tercera herramienta —en serie o en paralelo— no programa nada más.
            if (
                self._terminado
                or self._demora < 0
                or self._intentado
                or self._pendiente
            ):
                return
            # Acá nace el aviso: empezó a haber trabajo y no hay ningún plazo
            # esperando. Sólo puede haber UNO PENDIENTE, porque `_pendiente` se
            # limpia recién cuando el anterior disparó —el que ya disparó puede
            # seguir mandando, y cancelarlo no haría nada—. Daemon: un proceso
            # que se apaga no espera por él.
            self._pendiente = True
            self._timer = threading.Timer(self._demora, self._disparar)
            self._timer.daemon = True
            self._timer.start()

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._cerrar_herramienta(run_id)

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._cerrar_herramienta(run_id)

    def _cerrar_herramienta(self, run_id: UUID) -> None:
        with self._candado:
            inicio = self._inicio_herramienta.pop(run_id, None)
            if inicio is not None:
                self.segundos_herramientas += time.monotonic() - inicio
                self._en_vuelo = max(0, self._en_vuelo - 1)

    # ------------------------------------------------------------- el aviso

    def _disparar(self) -> None:
        """Lo que corre el temporizador. Manda el aviso si el turno sigue abierto.

        Sostiene el candado de envío MIENTRAS manda: si la respuesta final llega
        justo ahora, ``terminar()`` espera acá a que el aviso termine de salir.
        """
        with self._candado_envio:
            if self._terminado or self._intentado:
                return
            with self._candado:
                self._pendiente = False
                hay_trabajo = self._en_vuelo > 0
            if not hay_trabajo:
                # La herramienta contestó antes del plazo y lo que falta es el
                # modelo. Eso NO es «estoy consultando el sistema»: era el aviso
                # que salía igual en un turno de una herramienta de 0,1 s y una
                # segunda llamada al modelo de diez segundos, describiendo una
                # consulta que ya había terminado.
                # No se marca intentado: si otra herramienta de ESTE turno tarda
                # de verdad, ese aviso sí corresponde y vuelve a armarse.
                return
            self._intentado = True
            try:
                self._enviado = bool(self._enviar())
            except Exception as exc:
                # Es UX opcional: nunca afecta al turno.
                print(f"[progreso] aviso de avance falló type={type(exc).__name__}")

    def terminar(self) -> None:
        """El turno cerró: ningún aviso puede empezar a salir después de esto.

        Idempotente. Al volver, cualquier aviso en vuelo ya terminó de salir
        (o de fallar), y uno que todavía no empezó ya no va a empezar.
        """
        with self._candado:
            timer = self._timer
        if timer:
            # Cancelar uno que ya disparó no hace nada, así que no hace falta
            # distinguir.
            timer.cancel()
        with self._candado_envio:
            self._terminado = True

    # --------------------------------------------------------- lo que se vio

    @property
    def aviso_enviado(self) -> bool:
        return self._enviado

    @property
    def aviso_intentado(self) -> bool:
        return self._intentado

    def resumen(self) -> str:
        """Las partes de la latencia, por separado y en una línea de log.

        ``modelo=1x16.6s`` es lo que tardó el proveedor; ``herramientas=0x0.0s``
        dice si se consultó algo; ``progreso=no`` si la persona recibió un aviso
        antes de la respuesta. Con esto se lee un turno lento sin adivinar a
        quién le tocó la espera.
        """
        with self._candado:
            error = f" error_modelo={self.error_modelo}" if self.error_modelo else ""
            return (
                f"modelo={self.llamadas_modelo}x{self.segundos_modelo:.1f}s "
                f"herramientas={len(self.herramientas)}x{self.segundos_herramientas:.1f}s "
                f"progreso={'si' if self._enviado else 'no'}{error}"
            )
