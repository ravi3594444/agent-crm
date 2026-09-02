#!/bin/bash
# Levanta TODO el stack y lo deja listo. Idempotente: se puede correr N veces.
#
# Por qué existe: un Codespace se apaga solo cuando queda inactivo. Al volver:
#   - los contenedores de ERPNext con restart=on-failure NO vuelven (los mató
#     el daemon, no una falla) -> el CRM queda caído
#   - la visibilidad de los puertos vuelve a "private" -> Meta no llega al webhook
#   - el agente (un uvicorn suelto) desaparece
# Se ejecuta solo desde .devcontainer/devcontainer.json (postStartCommand), o a mano.
set -u
REPO=/workspaces/agent-crm
APP=$REPO/plus-agent
PORT=${AGENTE_PUERTO:-8081}
LOG=${AGENTE_LOG:-$APP/agente.log}
cd "$APP"

echo "[start] ERPNext stack"
docker compose -f frappe_docker/pwd.yml up -d >/dev/null 2>&1 || echo "[start]   (compose up devolvió error, sigo)"

echo "[start] Redis Stack (RedisJSON + RediSearch: el checkpointer no arranca con un Redis pelado)"
# El volumen y el AOF no son un detalle de infraestructura: desde la etapa 2b
# los límites de auto-confirmación que fija el dueño viven en este Redis. Sin
# volumen, un `docker rm` los borra y el sistema volvería a los valores de
# arranque del .env, que pueden ser MÁS FLOJOS que los que él puso.
if docker ps -a --format '{{.Names}}' | grep -qx agent-redis; then
  docker update --restart unless-stopped agent-redis >/dev/null 2>&1
  docker start agent-redis >/dev/null 2>&1
  if ! docker inspect -f '{{range .Mounts}}{{.Name}} {{end}}' agent-redis 2>/dev/null | grep -q agent-redis-data; then
    echo "[start] !! agent-redis corre SIN volumen: los límites del dueño no sobreviven un docker rm."
    echo "[start]    Migralo sin perder nada:  $APP/deploy/migrar_redis_a_volumen.sh"
  fi
else
  docker run -d --name agent-redis --restart unless-stopped \
    -p 6379:6379 \
    -v agent-redis-data:/data \
    -e REDIS_ARGS="--appendonly yes --appendfsync everysec --maxmemory-policy noeviction" \
    redis/redis-stack-server:latest >/dev/null
fi
until docker exec agent-redis redis-cli ping 2>/dev/null | grep -q PONG; do sleep 2; echo "[start]   redis..."; done

echo "[start] esperando ERPNext"
for i in $(seq 1 60); do
  curl -sf -m 4 http://localhost:8080/api/method/ping >/dev/null 2>&1 && break
  sleep 5; echo "[start]   erpnext... ($((i*5))s)"
done
curl -sf -m 4 http://localhost:8080/api/method/ping >/dev/null 2>&1 || echo "[start] !! ERPNext no respondió en 300s; el agente arranca igual y va a fallar hasta que vuelva"

# Visibilidad del puerto: se pierde en cada reinicio del Codespace y Meta necesita
# llegar desde afuera. gh viene autenticado en Codespaces.
if [ -n "${CODESPACE_NAME:-}" ] && command -v gh >/dev/null 2>&1; then
  gh codespace ports visibility "$PORT:public" -c "$CODESPACE_NAME" >/dev/null 2>&1 \
    && echo "[start] puerto $PORT público" \
    || echo "[start] !! no pude poner el puerto $PORT público (hacelo desde la pestaña PORTS)"
fi

if curl -sf -m 3 "http://localhost:$PORT/health" >/dev/null 2>&1; then
  echo "[start] el agente ya está corriendo en :$PORT"
else
  echo "[start] agente -> :$PORT  (log: $LOG)"
  # setsid: sesión propia, sobrevive al shell que lo lanzó. Ruta absoluta al venv:
  # `uvicorn` a secas asumía el venv activado, y en un arranque frío no lo está.
  setsid nohup "$APP/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port "$PORT" >"$LOG" 2>&1 </dev/null &
  for i in $(seq 1 30); do curl -sf -m 2 "http://localhost:$PORT/health" >/dev/null 2>&1 && break; sleep 1; done
fi
curl -sf -m 3 "http://localhost:$PORT/health" >/dev/null 2>&1 && echo "[start] LISTO" || { echo "[start] !! el agente no levantó; mirá $LOG"; tail -20 "$LOG"; exit 1; }
