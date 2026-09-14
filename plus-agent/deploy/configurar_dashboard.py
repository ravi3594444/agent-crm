"""Enable the dashboard in an existing agent .env without touching other keys.

Dos modos, y la diferencia decide qué puede hacer quien entra:

  * sin `--persona`: escribe `DASHBOARD_API_TOKEN`, el token COMPARTIDO. Sirve
    para mirar y **nunca** para decidir: no nombra a nadie, y una confirmación
    sin nombre no se puede auditar (`dashboard.puede_decidir`).
  * con `--persona <teléfono>`: agrega una entrada a `DASHBOARD_TOKENS`, que es
    una forma de probar «soy este teléfono» sin WhatsApp. Lo que ese teléfono
    puede hacer lo siguen decidiendo `router.es_equipo` y las mismas guardas
    que valen sobre el webhook firmado — el token no inventa permisos nuevos.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shlex
import tempfile
from pathlib import Path

TOKEN_LINE = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?DASHBOARD_API_TOKEN[ \t]*=.*$")
TOKENS_LINE = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?DASHBOARD_TOKENS[ \t]*=.*$")
EQUIPO_LINE = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?TELEFONOS_EQUIPO[ \t]*=.*$")
# El mismo mínimo que `dashboard.TOKEN_MINIMO`. No se importa `app` sólo para
# esto: este script corre en el host, antes de que el agente exista, y
# `secrets.token_urlsafe(48)` da 64 caracteres — el margen no es estrecho.
MINIMO = 32


def configure(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Configure the agent's .env first; refusing a missing file or symlink.")
    source = path.read_text()
    matches = list(TOKEN_LINE.finditer(source))
    if len(matches) > 1:
        raise ValueError("Multiple DASHBOARD_API_TOKEN entries exist; resolve them first.")
    if matches:
        parts = shlex.split(matches[0].group().split("=", 1)[1], comments=True)
        if len(parts) > 1:
            raise ValueError("The dashboard token must be one value; check its quoting.")
        value = parts[0] if parts else ""
        if len(value) >= 32:
            return None
        if value:
            raise ValueError("The existing dashboard token is too short; refusing to replace it silently.")
    token = secrets.token_urlsafe(48)
    line = f"DASHBOARD_API_TOKEN={token}"
    result = TOKEN_LINE.sub(line, source) if matches else source.rstrip("\n") + "\n" + line + "\n"
    _escribir(path, result)
    return token


def _escribir(path: Path, contenido: str) -> None:
    """Reemplazo atómico, con el `.env` en 0600 y nunca a medio escribir."""
    descriptor, temp = tempfile.mkstemp(prefix=".dashboard-env-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(contenido)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates mode 0600; the token must not become world-readable.
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _valor_de(patron: re.Pattern[str], fuente: str, clave: str) -> str:
    """El valor de una clave del `.env`, o "" si no está. Levanta si hay dos."""
    encontrados = list(patron.finditer(fuente))
    if len(encontrados) > 1:
        raise ValueError(f"Multiple {clave} entries exist; resolve them first.")
    if not encontrados:
        return ""
    partes = shlex.split(encontrados[0].group().split("=", 1)[1], comments=True)
    if len(partes) > 1:
        raise ValueError(f"{clave} must be one value; check its quoting.")
    return partes[0] if partes else ""


def _normalizar(numero: str) -> str:
    """El MISMO `telefono.normalizar` que usa el webhook y que usa el panel.

    Escribir acá una normalización propia haría que el `.env` guardara un número
    escrito de una forma y `quien()` buscara otro: el token entraría y no sería
    nadie. La importación es tardía a propósito — el script tiene que poder
    fallar con un mensaje y no con un ImportError si se lo corre fuera del repo.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import telefono

    return telefono.normalizar(numero)


def agregar_persona(path: Path, numero: str, *, solo_lectura: bool = False) -> tuple[str, str]:
    """Un token nuevo para UNA persona. Devuelve (token, teléfono normalizado).

    Lo que este modo protege, y por qué no alcanza con generar un token:

      * **el teléfono se normaliza con el del webhook**, así que `+54 9 351 …` y
        `54935 1…` son la misma persona de los dos lados. Un `.env` con el
        número escrito de otra forma da un token que entra y no es nadie.
      * **se rechaza un número que no está en TELEFONOS_EQUIPO** salvo que se
        pida `--solo-lectura`. Ese token sirve para mirar y nunca para
        confirmar (`puede_decidir` exige `router.es_equipo`), así que sin la
        bandera el caso abrumadoramente más probable es un dígito mal tipeado y
        un panel que "no anda" sin que nada lo explique.
      * **se rechaza el choque con `DASHBOARD_API_TOKEN`**. `quien()` recorre
        los tokens por persona ANTES del compartido, así que dos valores iguales
        convierten al token que tiene todo el equipo en esa persona, con su
        derecho a confirmar pedidos. Con `token_urlsafe(48)` no va a pasar
        nunca; se comprueba igual porque el costo es una comparación y lo que
        está del otro lado es una escalada de privilegios silenciosa.
      * **se rechaza pisar a alguien que ya tiene token.** Dos tokens vivos para
        la misma persona es uno que nadie sabe que existe.
    """
    if not path.is_file() or path.is_symlink():
        raise ValueError("Configure the agent's .env first; refusing a missing file or symlink.")
    telefono = _normalizar(numero)
    if not telefono:
        raise ValueError(f"No entiendo el teléfono {numero!r}; escribilo con el código de país.")

    source = path.read_text()
    compartido = _valor_de(TOKEN_LINE, source, "DASHBOARD_API_TOKEN")
    actuales = _valor_de(TOKENS_LINE, source, "DASHBOARD_TOKENS")
    equipo = {
        _normalizar(t)
        for t in _valor_de(EQUIPO_LINE, source, "TELEFONOS_EQUIPO").split(",")
        if _normalizar(t)
    }

    if telefono not in equipo and not solo_lectura:
        raise ValueError(
            "Ese número no está en TELEFONOS_EQUIPO: el token entraría al panel y no "
            "podría confirmar nada. Si es a propósito, repetilo con --solo-lectura."
        )

    entradas = [e.strip() for e in actuales.split(",") if e.strip()]
    for entrada in entradas:
        if ":" in entrada and _normalizar(entrada.partition(":")[2]) == telefono:
            raise ValueError(
                "Ese teléfono ya tiene un token en DASHBOARD_TOKENS. Borrá esa entrada "
                "primero: dos tokens vivos para la misma persona es uno que nadie sabe "
                "que existe."
            )

    token = secrets.token_urlsafe(48)
    if compartido and token == compartido:  # pragma: no cover - 2**288 contra
        raise ValueError("El token generado chocó con DASHBOARD_API_TOKEN; volvé a correrlo.")

    entradas.append(f"{token}:{telefono}")
    linea = "DASHBOARD_TOKENS=" + ",".join(entradas)
    result = (
        TOKENS_LINE.sub(linea.replace("\\", "\\\\"), source)
        if TOKENS_LINE.search(source)
        else source.rstrip("\n") + "\n" + linea + "\n"
    )
    _escribir(path, result)
    return token, telefono


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    parser.add_argument(
        "--persona",
        metavar="TELEFONO",
        help="Teléfono del equipo al que pertenece este token. Sin esto se "
             "configura el token COMPARTIDO, que mira y nunca decide.",
    )
    parser.add_argument(
        "--solo-lectura",
        action="store_true",
        help="Permitir un token para un número que NO está en TELEFONOS_EQUIPO. "
             "Entra y mira; no puede confirmar nada.",
    )
    args = parser.parse_args()

    if args.persona:
        try:
            token, telefono = agregar_persona(
                args.env_file, args.persona, solo_lectura=args.solo_lectura
            )
        except ValueError as exc:
            parser.exit(1, f"{exc}\n")
        print(f"Token nuevo para {telefono}. Guardalo en su gestor de contraseñas:")
        print(token)
        print("Es de UNA persona: no se reenvía ni se comparte, porque con él se")
        print("firman decisiones a su nombre en el historial del pedido.")
        print("Después: docker compose up -d --force-recreate agente "
              "(restart NO relee el .env).")
        return

    if args.solo_lectura:
        parser.exit(1, "--solo-lectura sólo tiene sentido junto con --persona.\n")
    try:
        token = configure(args.env_file)
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    if token:
        print("Dashboard access enabled. Save this token in your password manager:")
        print(token)
        print("Restart the agent, open /dashboard/, and enter that token to sign in.")
        print("This shared token can READ. To confirm orders from the panel, mint a")
        print("per-person token with --persona <phone>.")
    else:
        print("Dashboard access is already configured. The existing token was preserved.")


if __name__ == "__main__":
    main()
