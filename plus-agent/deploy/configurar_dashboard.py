"""Enable the dashboard in an existing agent .env without touching other keys."""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shlex
import tempfile
from pathlib import Path

TOKEN_LINE = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?DASHBOARD_API_TOKEN[ \t]*=.*$")


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
    descriptor, temp = tempfile.mkstemp(prefix=".dashboard-env-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(result)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates mode 0600; the token must not become world-readable.
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return token


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    args = parser.parse_args()
    try:
        token = configure(args.env_file)
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    if token:
        print("Dashboard access enabled. Save this token in your password manager:")
        print(token)
        print("Restart the agent, open /dashboard/, and enter that token to sign in.")
    else:
        print("Dashboard access is already configured. The existing token was preserved.")


if __name__ == "__main__":
    main()
