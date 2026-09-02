#!/bin/bash
# Mueve el Redis del agente a un volumen con AOF, SIN perder lo que ya tiene.
#
# POR QUÉ: desde la etapa 2b los límites de auto-confirmación que fija el dueño
# viven en Redis. Un contenedor creado sin volumen los pierde en el primer
# `docker rm`, y el sistema volvería a los valores de arranque del .env, que
# pueden ser MÁS FLOJOS que los que él puso. app/limites.py lo detecta (cruza
# contra la auditoría en ERPNext) y deja todo pendiente, pero es mejor no
# llegar a eso.
#
# QUÉ HACE: fuerza un guardado a disco, se copia /data del contenedor viejo al
# volumen nuevo, y recrea el contenedor apuntando al volumen. No borra nada
# hasta que el contenedor nuevo contesta PONG.
set -euo pipefail
NOMBRE=${1:-agent-redis}
VOLUMEN=${2:-agent-redis-data}
IMAGEN=$(docker inspect -f '{{.Config.Image}}' "$NOMBRE")

echo "[migrar] $NOMBRE -> volumen $VOLUMEN (imagen $IMAGEN)"
docker exec "$NOMBRE" redis-cli SAVE >/dev/null
docker exec "$NOMBRE" redis-cli BGREWRITEAOF >/dev/null || true
sleep 2

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
docker cp "$NOMBRE:/data/." "$TMP/"
docker volume create "$VOLUMEN" >/dev/null
docker run --rm -v "$VOLUMEN:/destino" -v "$TMP:/origen:ro" alpine \
  sh -c 'cp -a /origen/. /destino/ 2>/dev/null || true'

docker rename "$NOMBRE" "${NOMBRE}-sin-volumen"
docker stop "${NOMBRE}-sin-volumen" >/dev/null

docker run -d --name "$NOMBRE" --restart unless-stopped \
  -p 6379:6379 \
  -v "$VOLUMEN:/data" \
  -e REDIS_ARGS="--appendonly yes --appendfsync everysec --maxmemory-policy noeviction" \
  "$IMAGEN" >/dev/null

for _ in $(seq 1 30); do
  docker exec "$NOMBRE" redis-cli ping 2>/dev/null | grep -q PONG && break
  sleep 1
done
docker exec "$NOMBRE" redis-cli ping | grep -q PONG || {
  echo "[migrar] !! el contenedor nuevo no responde; volviendo al anterior"
  docker rm -f "$NOMBRE" >/dev/null
  docker rename "${NOMBRE}-sin-volumen" "$NOMBRE"
  docker start "$NOMBRE" >/dev/null
  exit 1
}
echo "[migrar] listo. Claves: $(docker exec "$NOMBRE" redis-cli DBSIZE)"
echo "[migrar] el viejo quedó como ${NOMBRE}-sin-volumen; borralo cuando estés tranquilo:"
echo "[migrar]   docker rm ${NOMBRE}-sin-volumen"
