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
  # Redis Stack no pide contraseña. Publicado en 0.0.0.0, cualquier vecino de
  # red puede escribir plus-agent:limites y subir un tope sin pasar por el
  # código del dueño. `docker start` conserva el mapeo con que se creó, así que
  # un contenedor viejo hay que recrearlo: el script de migración lo hace en
  # loopback.
  # Se miran TODOS los bindings y el modo de red: vale sólo 127.0.0.1 o ::1;
  # vacío, 0.0.0.0, ::, una IP de la LAN o NetworkMode=host son «alcanzable».
  redis_expuesto=$(docker inspect -f '{{.HostConfig.NetworkMode}} {{range $p, $bs := .HostConfig.PortBindings}}{{range $bs}}[{{.HostIp}}]{{end}}{{end}}' agent-redis 2>/dev/null \
    | awk '{ if ($1 == "host") { print "host"; exit }
             n = split($0, partes, "[][]"); for (i = 2; i <= n; i += 2) { ip = partes[i]; if (ip != "127.0.0.1" && ip != "::1") { print (ip == "" ? "0.0.0.0" : ip); exit } } }')
  if [ -n "$redis_expuesto" ]; then
    echo "[start] !! agent-redis está alcanzable desde fuera de esta máquina (${redis_expuesto}:6379) y sin contraseña."
    echo "[start]    Recrealo en loopback sin perder nada:  $APP/deploy/migrar_redis_a_volumen.sh"
  fi
else
  docker run -d --name agent-redis --restart unless-stopped \
    -p 127.0.0.1:6379:6379 \
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

PIDFILE=${AGENTE_PIDFILE:-$APP/agente.pid}
# Quién es el supervisor y cómo se lo reconoce vive en deploy/supervisor.sh,
# para poder probarlo: un pid reciclado por el SO, o cualquier otro Uvicorn de
# la máquina, no pueden pasar por el nuestro (la marca exacta en su línea de
# comando es la que decide), y el pidfile se borra cuando el supervisor sale.
# shellcheck source=plus-agent/deploy/supervisor.sh
. "$APP/deploy/supervisor.sh"

if curl -sf -m 3 "http://localhost:$PORT/health" >/dev/null 2>&1; then
  echo "[start] el agente ya está corriendo en :$PORT"
elif supervisor_vivo "$PIDFILE"; then
  echo "[start] el supervisor del agente ya está vivo (pid $(cat "$PIDFILE")); espero que levante"
  for i in $(seq 1 30); do curl -sf -m 2 "http://localhost:$PORT/health" >/dev/null 2>&1 && break; sleep 1; done
else
  echo "[start] agente -> :$PORT  (log: $LOG)"
  # Ruta absoluta al venv: `uvicorn` a secas asumía el venv activado, y en un
  # arranque frío no lo está. Bucle de reinicio: si uvicorn muere (excepción,
  # OOM, kill), vuelve solo en 3 s. --no-access-log: el access log imprimía el
  # META_VERIFY_TOKEN de la query string. Todo eso está en supervisor_lanzar.
  supervisor_lanzar "$APP" "$PORT" "$PIDFILE" "$LOG"
  for i in $(seq 1 30); do curl -sf -m 2 "http://localhost:$PORT/health" >/dev/null 2>&1 && break; sleep 1; done
fi
curl -sf -m 3 "http://localhost:$PORT/health" >/dev/null 2>&1 && echo "[start] LISTO" || { echo "[start] !! el agente no levantó; mirá $LOG"; tail -20 "$LOG"; exit 1; }
