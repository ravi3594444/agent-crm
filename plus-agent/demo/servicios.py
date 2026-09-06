"""Los tres dobles, en un proceso: ERPNext, Meta y el endpoint del modelo.

Corre DENTRO de la red aislada del banco de pruebas, al lado del agente. Los
tres escuchan en puertos distintos para que un error de ruteo se vea como un
404 y no como una respuesta de otro servicio:

    8000  http   ERPNext        (app/erpnext.py no exige https)
    8443  https  Graph de Meta  (app/whatsapp.py exige https salvo loopback)
    8444  https  el modelo      (app/modelos.py exige https, sin excepción)
    8999  http   control        para que el piloto lea el buzón y siembre

El certificado se genera afuera y se monta: el agente confía en él por
SSL_CERT_FILE, que es un mecanismo de httpx y no un cambio en la app.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from demo import datos, falso_erpnext, falso_meta, falso_modelo

ALMACEN = falso_erpnext.Almacen()
BUZON = falso_meta.Buzon()
REGLAS: list[falso_modelo.Regla] = []
RELEVO: falso_modelo.Relevo | None = None
PHONE_ID = os.getenv("DEMO_PHONE_ID", "demo-phone-id")
LLAMADAS_MODELO: list[dict] = []
_CANDADO = threading.Lock()


class _Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, formato: str, *args: object) -> None:
        """Silencio: el access log de un doble no aporta y puede traer datos."""

    def _leer(self) -> bytes:
        largo = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(largo) if largo else b""

    def _responder(self, estado: int, cuerpo: bytes, tipo: str,
                   cabeceras: dict[str, str] | None = None) -> None:
        self.send_response(estado)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        for k, v in (cabeceras or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(cuerpo)

    def _json(self, estado: int, datos_: object,
              cabeceras: dict[str, str] | None = None) -> None:
        self._responder(
            estado, json.dumps(datos_, ensure_ascii=False).encode("utf-8"),
            "application/json", cabeceras,
        )


class ERPNext(_Base):
    def _hacer(self, metodo: str) -> None:
        partes = urlparse(self.path)
        estado, cuerpo = falso_erpnext.manejar(
            ALMACEN, metodo, partes.path, parse_qs(partes.query),
            self._leer(), self.headers.get("Authorization", ""),
        )
        self._json(estado, cuerpo)

    do_GET = lambda self: self._hacer("GET")          # noqa: E731
    do_POST = lambda self: self._hacer("POST")        # noqa: E731
    do_PUT = lambda self: self._hacer("PUT")          # noqa: E731
    do_DELETE = lambda self: self._hacer("DELETE")    # noqa: E731


class Meta(_Base):
    def _hacer(self, metodo: str) -> None:
        partes = urlparse(self.path)
        cuerpo = self._leer()
        try:
            payload = json.loads(cuerpo) if cuerpo else {}
        except ValueError:
            payload = {}
        estado, respuesta, cabeceras = falso_meta.manejar(
            BUZON, metodo, partes.path, parse_qs(partes.query),
            payload if isinstance(payload, dict) else {}, PHONE_ID,
        )
        self._json(estado, respuesta, cabeceras)

    do_GET = lambda self: self._hacer("GET")     # noqa: E731
    do_POST = lambda self: self._hacer("POST")   # noqa: E731


class Modelo(_Base):
    def do_POST(self) -> None:
        cuerpo = self._leer()
        if RELEVO is not None:
            estado, respuesta, tipo = RELEVO(
                urlparse(self.path).path, cuerpo, dict(self.headers)
            )
            self._responder(estado, respuesta, tipo)
            return
        try:
            payload = json.loads(cuerpo)
        except ValueError:
            self._json(400, {"error": {"message": "cuerpo no JSON"}})
            return
        with _CANDADO:
            LLAMADAS_MODELO.append(
                {"modelo": payload.get("model"),
                 "mensajes": len(payload.get("messages") or []),
                 "herramientas": len(payload.get("tools") or [])}
            )
        self._json(200, falso_modelo.responder(REGLAS, payload))


class Control(_Base):
    """Lo que el piloto necesita: leer el buzón, sembrar, programar fallas."""

    def do_GET(self) -> None:
        partes = urlparse(self.path)
        if partes.path == "/salud":
            self._json(200, {"ok": True, "modo": "relevo" if RELEVO else "guion"})
        elif partes.path == "/buzon":
            self._json(200, {"envios": [
                {"n": e.n, "a": e.a, "tipo": e.tipo, "texto": e.texto,
                 "plantilla": e.plantilla, "parametros": e.parametros,
                 "botones": e.botones}
                for e in BUZON.envios
            ]})
        elif partes.path == "/documentos":
            doctype = (parse_qs(partes.query).get("doctype") or [""])[0]
            self._json(200, {"data": ALMACEN.listar(
                doctype, limite=500,
                parent=falso_erpnext.HIJAS.get(doctype, (None,))[0],
            )})
        elif partes.path == "/instantanea":
            # El estado que le importa al informe, por documento. El piloto lo
            # pide antes y después de cada paso y muestra la diferencia.
            foto: dict[str, dict] = {}
            for doctype, tabla in ALMACEN.docs.items():
                if doctype in falso_erpnext.HIJAS:
                    continue
                for nombre, doc in tabla.items():
                    interesa = {
                        c: doc.get(c) for c in (
                            "docstatus", "status", "customer", "grand_total",
                            "delivery_date", "additional_discount_percentage",
                            "outstanding_amount", "content", "reference_name",
                            "against_sales_order", "po_no", "total_qty",
                        ) if doc.get(c) not in (None, "")
                    }
                    foto[f"{doctype}/{nombre}"] = interesa
            self._json(200, {"documentos": foto})
        elif partes.path == "/modelo":
            self._json(200, {"llamadas": LLAMADAS_MODELO,
                             "relevos": RELEVO.llamadas if RELEVO else 0})
        elif partes.path == "/http":
            self._json(200, {"pedidos": ALMACEN.pedidos_http[-500:]})
        else:
            self._json(404, {"error": partes.path})

    def do_POST(self) -> None:
        partes = urlparse(self.path)
        cuerpo = self._leer()
        try:
            payload = json.loads(cuerpo) if cuerpo else {}
        except ValueError:
            payload = {}
        if partes.path == "/sembrar":
            datos.sembrar(ALMACEN)
            self._json(200, {"ok": True,
                             "items": len(ALMACEN.tabla("Item")),
                             "clientes": len(ALMACEN.tabla("Customer"))})
        elif partes.path == "/falla-meta":
            BUZON.programar_falla(
                int(payload.get("estado") or 400),
                payload.get("codigo"),
                veces=int(payload.get("veces") or 1),
                para=str(payload.get("para") or ""),
            )
            self._json(200, {"ok": True})
        elif partes.path == "/limpiar-buzon":
            BUZON.limpiar()
            self._json(200, {"ok": True})
        elif partes.path == "/crear":
            doc = ALMACEN.crear(str(payload["doctype"]), payload["doc"],
                                puede_confirmar=True)
            self._json(200, {"data": doc})
        else:
            self._json(404, {"error": partes.path})


def _servir(clase: type[BaseHTTPRequestHandler], puerto: int,
            cert: str = "", clave: str = "") -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("0.0.0.0", puerto), clase)
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, clave)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> None:
    global RELEVO
    ap = argparse.ArgumentParser(description="Los dobles del banco de pruebas")
    ap.add_argument("--cert", required=True)
    ap.add_argument("--clave", required=True)
    ap.add_argument("--relevar-a", default="",
                    help="URL del proveedor real; si está, el modelo releva")
    ap.add_argument("--clave-en", default="",
                    help="nombre de la variable con la clave real (sólo el relevo la ve)")
    ap.add_argument("--sembrar", action="store_true")
    args = ap.parse_args()

    if args.relevar_a:
        clave = os.environ.get(args.clave_en, "") if args.clave_en else ""
        if args.clave_en and not clave:
            raise SystemExit(
                f"[servicios] falta {args.clave_en} en el entorno del relevo: "
                "sin la clave real no tiene sentido relevar"
            )
        RELEVO = falso_modelo.Relevo(args.relevar_a, clave=clave)
    else:
        from demo import escenarios
        REGLAS.extend(escenarios.reglas())

    if args.sembrar:
        datos.sembrar(ALMACEN)

    _servir(ERPNext, 8000)
    _servir(Meta, 8443, args.cert, args.clave)
    _servir(Modelo, 8444, args.cert, args.clave)
    _servir(Control, 8999)
    print(
        f"[servicios] erpnext :8000  meta :8443  modelo :8444 "
        f"({'relevo -> proveedor real' if RELEVO else 'guion offline'})  control :8999",
        flush=True,
    )
    threading.Event().wait()


if __name__ == "__main__":
    main()
