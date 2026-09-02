#!/bin/bash
# ¿Sobreviven los límites del dueño a un reinicio REAL de Redis?
#
# No alcanza con abrir otra conexión: eso prueba que dos procesos ven lo mismo,
# no que el dato esté en disco. Esto levanta un Redis con la MISMA
# configuración que producción (AOF + volumen), fija un límite pasando por el
# código de verdad (app/limites.aplicar), y después:
#
#   1. docker restart          -> el proceso muere y vuelve
#   2. docker rm -f + docker run con el MISMO volumen  -> el contenedor
#      desaparece por completo; sólo el volumen queda
#
# En los dos casos tienen que estar el valor Y la auditoría. Si el paso 2 pasa,
# el dato está en el volumen y no en el sistema de archivos del contenedor.
#
# Corre aislado: contenedor y volumen propios, puerto 6380, y los borra al
# terminar. No toca el Redis del agente ni ERPNext.
set -euo pipefail
cd "$(dirname "$0")/.."

CONT=limites-persistencia-test
VOL=limites-persistencia-test-data
PUERTO=6380
IMAGEN=${IMAGEN_REDIS:-redis/redis-stack-server:latest}
PY=${PY:-/workspaces/agent-crm/plus-agent/.venv/bin/python}
export REDIS_URL="redis://localhost:$PUERTO/0"

limpiar() { docker rm -f "$CONT" >/dev/null 2>&1 || true; docker volume rm "$VOL" >/dev/null 2>&1 || true; }
trap limpiar EXIT
limpiar

arrancar() {
  docker run -d --name "$CONT" -p "$PUERTO:6379" -v "$VOL:/data" \
    -e REDIS_ARGS="--appendonly yes --appendfsync everysec --maxmemory-policy noeviction" \
    "$IMAGEN" >/dev/null
  for _ in $(seq 1 45); do
    docker exec "$CONT" redis-cli ping 2>/dev/null | grep -q PONG && return 0
    sleep 1
  done
  echo "FALLO: el Redis de prueba no arrancó"; exit 1
}

leer() {  # imprime  <valor del tope>|<cantidad de entradas de auditoría>
  "$PY" - <<'EOF'
import os
os.environ.setdefault("ERPNEXT_URL", "http://erpnext.invalid")
for k in ("ERPNEXT_API_KEY", "ERPNEXT_API_SECRET", "ERPNEXT_POLICY_API_KEY",
          "ERPNEXT_POLICY_API_SECRET", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TOKEN"):
    os.environ.setdefault(k, "x")
os.environ.pop("AUTO_CONFIRM_MAX", None)
from app import limites
# Sin cruce contra ERPNext: acá se prueba el disco de Redis, no ERPNext.
limites._hubo_cambios_durables = lambda: False
print(f"{limites.vigente('AUTO_CONFIRM_MAX')}|{len(limites.auditoria(50))}")
EOF
}

echo "== 1. Redis con AOF + volumen, y un límite fijado por el código real =="
arrancar
"$PY" - <<'EOF'
import os
os.environ.setdefault("ERPNEXT_URL", "http://erpnext.invalid")
for k in ("ERPNEXT_API_KEY", "ERPNEXT_API_SECRET", "ERPNEXT_POLICY_API_KEY",
          "ERPNEXT_POLICY_API_SECRET", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TOKEN"):
    os.environ.setdefault(k, "x")
from app import limites
limites._hubo_cambios_durables = lambda: False
limites._auditar_en_erpnext = lambda entrada: None  # ERPNext no participa acá
limites._codigo = lambda: "4242"
limites.proponer("tope", "31337", "5493511111111")
print("   aplicado:", limites.aplicar("4242", "5493511111111"))
EOF
ANTES=$(leer); echo "   valor|auditoría = $ANTES"
[ "$ANTES" = "31337|1" ] || { echo "FALLO: no quedó guardado de entrada"; exit 1; }

echo "== 2. docker restart (el proceso muere y vuelve) =="
docker restart "$CONT" >/dev/null
for _ in $(seq 1 45); do docker exec "$CONT" redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 1; done
DESPUES_RESTART=$(leer); echo "   valor|auditoría = $DESPUES_RESTART"
[ "$DESPUES_RESTART" = "31337|1" ] || { echo "FALLO: no sobrevivió al restart"; exit 1; }

echo "== 3. docker rm -f y docker run con el MISMO volumen =="
docker rm -f "$CONT" >/dev/null
arrancar
DESPUES_RM=$(leer); echo "   valor|auditoría = $DESPUES_RM"
[ "$DESPUES_RM" = "31337|1" ] || { echo "FALLO: no sobrevivió a recrear el contenedor"; exit 1; }
echo "   AOF en el volumen: $(docker exec "$CONT" redis-cli CONFIG GET appendonly | tail -1)"

echo "== 4. y si el volumen SÍ se pierde, no se vuelve a un default más flojo =="
"$PY" - <<'EOF'
import os
os.environ.setdefault("ERPNEXT_URL", "http://erpnext.invalid")
for k in ("ERPNEXT_API_KEY", "ERPNEXT_API_SECRET", "ERPNEXT_POLICY_API_KEY",
          "ERPNEXT_POLICY_API_SECRET", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TOKEN"):
    os.environ.setdefault(k, "x")
os.environ["AUTO_CONFIRM_MAX"] = "999999"   # el arranque, más flojo que 31337
from app import limites, locks
locks.conexion().delete(limites.CLAVE_VALORES)      # simula el volumen perdido
limites._hubo_cambios_durables = lambda: True        # ERPNext sí recuerda el cambio
try:
    limites.configuracion()
    print("   FALLO: aceptó el default de arranque"); raise SystemExit(1)
except limites.LimiteError as exc:
    print(f"   falla cerrada, como debe: {exc}")
EOF

echo
echo "OK: el límite y su auditoría sobreviven al restart y a recrear el contenedor,"
echo "    y un volumen perdido deja todo pendiente en vez de aflojar el límite."
