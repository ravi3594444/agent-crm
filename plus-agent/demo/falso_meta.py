"""El Graph API de Meta, de mentira, que en vez de mandar GUARDA.

Cada envío queda en un buzón en memoria y se puede leer después: es la
transcripción de lo que el cliente y el equipo HABRÍAN recibido por WhatsApp.
Nada sale de la máquina.

Devuelve exactamente lo que app/whatsapp.py exige para dar un envío por bueno
—un {"messages": [{"id": …}]}— y sabe fallar a pedido, con la forma real de un
error de Meta (código en el cuerpo, x-fb-request-id, retry-after), para poder
ejercitar la clasificación permanente/transitorio y la cola de avisos caídos.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Envio:
    """Un mensaje que habría salido."""

    n: int
    a: str
    tipo: str
    texto: str
    plantilla: str = ""
    parametros: list[str] = field(default_factory=list)
    botones: list[str] = field(default_factory=list)
    crudo: dict = field(default_factory=dict)


@dataclass
class Falla:
    """Una falla programada: la próxima llamada que matchee se rechaza."""

    estado: int
    codigo: int | None
    veces: int = 1
    para: str = ""  # teléfono, o "" para cualquiera


class Buzon:
    def __init__(self) -> None:
        self.envios: list[Envio] = []
        self.fallas: list[Falla] = []
        self.candado = threading.RLock()

    # -- programación de fallas

    def programar_falla(
        self, estado: int, codigo: int | None, *, veces: int = 1, para: str = ""
    ) -> None:
        with self.candado:
            self.fallas.append(Falla(estado, codigo, veces, para))

    def _falla_para(self, telefono: str) -> Falla | None:
        for f in self.fallas:
            if f.veces <= 0:
                continue
            if f.para and f.para != telefono:
                continue
            f.veces -= 1
            return f
        return None

    # -- lectura

    def para(self, telefono: str) -> list[Envio]:
        with self.candado:
            return [e for e in self.envios if e.a == telefono]

    def textos(self, telefono: str = "") -> list[str]:
        with self.candado:
            return [
                e.texto for e in self.envios
                if (not telefono or e.a == telefono) and e.texto
            ]

    def limpiar(self) -> None:
        with self.candado:
            self.envios.clear()
            self.fallas.clear()


def _texto_de(payload: dict) -> tuple[str, str, list[str], list[str], str]:
    """(tipo, texto, parámetros, botones, plantilla) de un payload de Graph."""
    tipo = str(payload.get("type") or "")
    if tipo == "text":
        return tipo, str((payload.get("text") or {}).get("body") or ""), [], [], ""
    if tipo == "interactive":
        inter = payload.get("interactive") or {}
        cuerpo = str(((inter.get("body") or {}).get("text")) or "")
        botones = [
            str(((b.get("reply") or {}).get("title")) or "")
            for b in ((inter.get("action") or {}).get("buttons") or [])
        ]
        return tipo, cuerpo, [], botones, ""
    if tipo == "template":
        plantilla = payload.get("template") or {}
        nombre = str(plantilla.get("name") or "")
        parametros: list[str] = []
        botones: list[str] = []
        for comp in plantilla.get("components") or []:
            if comp.get("type") == "body":
                parametros += [
                    str(p.get("text") or "") for p in comp.get("parameters") or []
                ]
            if str(comp.get("type") or "").lower() == "button":
                botones += [
                    str(p.get("payload") or "") for p in comp.get("parameters") or []
                ]
        # La plantilla no lleva texto libre: el "texto" es su nombre y sus
        # parámetros, que es lo que el destinatario terminaría leyendo.
        return tipo, f"[plantilla {nombre}] " + " | ".join(parametros), parametros, botones, nombre
    return tipo, "", [], [], ""


def manejar(
    buzon: Buzon,
    metodo: str,
    ruta: str,
    consulta: dict[str, list[str]],
    cuerpo: dict,
    phone_id: str,
) -> tuple[int, dict, dict[str, str]]:
    """(estado, json, headers). Nunca levanta."""
    partes = [p for p in ruta.split("/") if p]
    # /{version}/{phone_id}/messages  ó  /{version}/{phone_id}
    if len(partes) >= 3 and partes[-1] == "messages":
        if partes[-2] != phone_id:
            return 404, {"error": {"code": 100, "message": "phone id desconocido"}}, {}
        return _enviar(buzon, cuerpo)
    if len(partes) >= 2 and partes[-1] == phone_id:
        return 200, {"id": phone_id, "quality_rating": "GREEN",
                     "display_phone_number": "+54 9 351 000-0000"}, {}
    if partes and partes[-1] == "debug_token":
        return 200, {"data": {"type": "SYSTEM_USER", "is_valid": True,
                              "expires_at": 0,
                              "scopes": ["whatsapp_business_messaging",
                                         "whatsapp_business_management"]}}, {}
    if partes and partes[-1] == "message_templates":
        nombre = (consulta.get("name") or [""])[0]
        return 200, {"data": [{"name": nombre, "status": "APPROVED",
                               "language": "es_AR"}] if nombre else []}, {}
    return 404, {"error": {"code": 100, "message": f"el doble no implementa {ruta}"}}, {}


def _enviar(buzon: Buzon, payload: dict) -> tuple[int, dict, dict[str, str]]:
    telefono = str(payload.get("to") or "")
    with buzon.candado:
        falla = buzon._falla_para(telefono)
        if falla is not None:
            cabeceras = {"x-fb-request-id": f"demo-req-{len(buzon.envios) + 1}"}
            if falla.estado == 429:
                cabeceras["retry-after"] = "2"
            cuerpo: dict[str, Any] = {
                "error": {
                    "message": "falla programada por el banco de pruebas",
                    "type": "OAuthException",
                }
            }
            if falla.codigo is not None:
                cuerpo["error"]["code"] = falla.codigo
            return falla.estado, cuerpo, cabeceras

        if str(payload.get("messaging_product") or "") != "whatsapp":
            return 400, {"error": {"code": 100,
                                   "message": "falta messaging_product"}}, {}
        tipo, texto, parametros, botones, plantilla = _texto_de(payload)
        n = len(buzon.envios) + 1
        buzon.envios.append(
            Envio(n=n, a=telefono, tipo=tipo, texto=texto, plantilla=plantilla,
                  parametros=parametros, botones=botones, crudo=payload)
        )
        return 200, {
            "messaging_product": "whatsapp",
            "contacts": [{"input": telefono, "wa_id": telefono}],
            "messages": [{"id": f"wamid.DEMO{n:06d}"}],
        }, {}
