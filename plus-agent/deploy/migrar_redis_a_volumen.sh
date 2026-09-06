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
# volumen nuevo, y recrea el contenedor apuntando al volumen, EN LOOPBACK
# (127.0.0.1:6379: Redis Stack no pide contraseña, y publicado en 0.0.0.0
# cualquier vecino de red podría subir un tope escribiendo el hash) y en las
# MISMAS redes de Docker que tenía (un Redis de compose recreado en la red
# default dejaría al agente sin resolver su nombre).
#
# ES TRANSACCIONAL: si cualquier paso falla después de renombrar el viejo —el
# `docker run`, la espera, el PONG— se vuelve al contenedor anterior tal como
# estaba. Con `set -e`, un `docker run` que falla cortaba el script ANTES del
# bloque de vuelta atrás y dejaba a Redis renombrado y parado. Ahora la vuelta
# atrás está registrada en un trap desde antes del rename y sólo se desarma
# cuando el nuevo contestó.
set -euo pipefail
NOMBRE=${1:-agent-redis}
VOLUMEN=${2:-agent-redis-data}
IMAGEN=$(docker inspect -f '{{.Config.Image}}' "$NOMBRE")
# Las redes del contenedor original, tal como están (compose crea la suya).
REDES=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$NOMBRE")
PRIMERA_RED=${REDES%% *}
VIEJO="${NOMBRE}-sin-volumen"

echo "[migrar] $NOMBRE -> volumen $VOLUMEN (imagen $IMAGEN, redes: ${REDES:-default})"
docker exec "$NOMBRE" redis-cli SAVE >/dev/null
docker exec "$NOMBRE" redis-cli BGREWRITEAOF >/dev/null || true
sleep 2

TMP=$(mktemp -d)
LISTO=0
RENOMBRADO=0

volver_atras() {
  rm -rf "$TMP"
  if [ "$LISTO" = 1 ] || [ "$RENOMBRADO" = 0 ]; then
    return
  fi
  echo "[migrar] !! algo falló; vuelvo al contenedor anterior"
  docker rm -f "$NOMBRE" >/dev/null 2>&1 || true
  docker rename "$VIEJO" "$NOMBRE" >/dev/null 2>&1 || true
  docker start "$NOMBRE" >/dev/null 2>&1 || true
  echo "[migrar] $NOMBRE es otra vez el contenedor original, sin volumen"
}
trap volver_atras EXIT

docker cp "$NOMBRE:/data/." "$TMP/"
docker volume create "$VOLUMEN" >/dev/null
docker run --rm -v "$VOLUMEN:/destino" -v "$TMP:/origen:ro" alpine \
  sh -c 'cp -a /origen/. /destino/ 2>/dev/null || true'

# Desde acá cualquier fallo deshace: el trap ya está armado.
docker rename "$NOMBRE" "$VIEJO"
RENOMBRADO=1
docker stop "$VIEJO" >/dev/null

RED_ARGS=()
if [ -n "$PRIMERA_RED" ]; then
  RED_ARGS=(--network "$PRIMERA_RED")
fi
docker run -d --name "$NOMBRE" --restart unless-stopped \
  "${RED_ARGS[@]}" \
  -p 127.0.0.1:6379:6379 \
  -v "$VOLUMEN:/data" \
  -e REDIS_ARGS="--appendonly yes --appendfsync everysec --maxmemory-policy noeviction" \
  "$IMAGEN" >/dev/null
# El resto de las redes, si tenía más de una.
for red in $REDES; do
  [ "$red" = "$PRIMERA_RED" ] || docker network connect "$red" "$NOMBRE"
done

for _ in $(seq 1 30); do
  docker exec "$NOMBRE" redis-cli ping 2>/dev/null | grep -q PONG && break
  sleep 1
done
docker exec "$NOMBRE" redis-cli ping | grep -q PONG || {
  echo "[migrar] !! el contenedor nuevo no responde PONG"
  exit 1  # el trap vuelve al anterior
}
LISTO=1
echo "[migrar] listo. Claves: $(docker exec "$NOMBRE" redis-cli DBSIZE); publicado sólo en 127.0.0.1:6379"
echo "[migrar] el viejo quedó parado como $VIEJO; borralo cuando estés tranquilo:"
echo "[migrar]   docker rm $VIEJO"
