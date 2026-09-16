"""¿Este proceso atendería una llamada, y la atendería el agente correcto?

    python -m app.voz.verificar

Existe porque `/healthz` del relay contesta 200 igual cuando `AGENT_FACTORY` no
resuelve: el relay trata un factory que falla como «servime el agente de
fábrica», y el de fábrica es el de RESTAURANTE. O sea que el modo de falla que
importa —el que llama a la distribuidora y escucha a una recepcionista
ofreciéndole mesa para dos— es invisible para un healthcheck. Esto lo mira.

No llama a AssemblyAI, no gasta una llamada y no necesita ERPNext arriba: con
ERPNext caído el agente se arma igual, sin cuenta, que es la degradación
correcta. Lo que comprueba es lo que no puede degradar.

Sale 0 si todo bien, 1 si no. Lo corre el job `imagen-voz` de CI y sirve en el
server después de un deploy, igual que `python -m app.readiness`.
"""
from __future__ import annotations

import os
import sys

ESPERADO = "plus-clientes"


def verificar() -> list[str]:
    """Devuelve la lista de problemas. Vacía es que está bien."""
    problemas: list[str] = []

    if not os.getenv("ASSEMBLYAI_API_KEY", "").strip():
        # No es fatal para armar el agente, pero sin esto el relay contesta
        # «ASSEMBLYAI_API_KEY is not set» y cierra: la llamada se corta sola.
        problemas.append("ASSEMBLYAI_API_KEY vacía: ninguna llamada va a conectar")

    factory = os.getenv("AGENT_FACTORY", "").strip()
    if factory != "app.voz.agente:desde_navegador":
        problemas.append(
            f"AGENT_FACTORY es {factory!r}: con esto el relay sirve su agente de "
            "fábrica, que es el de restaurante"
        )

    try:
        from calling_agent.main import _build_agent
    except ImportError as exc:
        problemas.append(f"el relay no está instalado: {exc}")
        return problemas

    agente = _build_agent({})
    if agente is None:
        problemas.append(
            "AGENT_FACTORY no resolvió: el relay atendería con el agente de "
            "restaurante. Mirá el log del arranque, que dice por qué falló"
        )
        return problemas
    if agente.name != ESPERADO:
        problemas.append(f"el agente es {agente.name!r} y tendría que ser {ESPERADO!r}")

    prompt = agente.build_prompt()
    for marca in (
        "REGLAS QUE NO PODÉS ROMPER",
        "POR TELÉFONO",
        "ANTES DE CARGAR UN PEDIDO, REPETILO",
        "DATOS QUE TE DICTAN",
    ):
        if marca not in prompt:
            problemas.append(f"al prompt le falta el bloque «{marca}»")

    from app.tools.registro import TOOLS_CLIENTES

    declaradas = [herramienta["name"] for herramienta in agente.tools]
    esperadas = [herramienta.name for herramienta in TOOLS_CLIENTES]
    if declaradas != esperadas:
        # La comparación es de LISTAS, así que también falla si el conjunto es
        # el mismo y cambió el orden — y ahí la diferencia simétrica es vacía y
        # el mensaje terminaba en «[]»: el job se ponía rojo sin decir qué. Lo
        # cazó una review de CodeRabbit.
        faltan_o_sobran = sorted(set(esperadas) ^ set(declaradas))
        detalle = (
            f"{faltan_o_sobran}"
            if faltan_o_sobran
            else f"mismas herramientas, otro orden: {declaradas} vs {esperadas}"
        )
        problemas.append(
            f"las herramientas declaradas no son el registro de clientes: {detalle}"
        )

    from app.voz import identidad

    if identidad.numero_por_parametro_habilitado():
        # No es un error: es una demo. Pero que nadie lo descubra en producción.
        problemas.append(
            "VOZ_NUMERO_POR_PARAMETRO está ENCENDIDO: cualquiera que abra la "
            "página elige su propio número. Es para una demo, no para un cliente"
        )

    return problemas


def main() -> int:
    problemas = verificar()
    if not problemas:
        print("[voz] listo: el relay atendería con el agente de clientes")
        return 0
    for problema in problemas:
        print(f"[voz] PROBLEMA: {problema}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
