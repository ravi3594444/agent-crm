#!/usr/bin/env bash
# Respaldo nocturno del agente: Redis + la base del sitio de ERPNext.
#
# POR QUÉ EXISTE
# Redis no es una caché en este producto. Ahí viven LOS LÍMITES QUE FIJÓ EL
# DUEÑO —y que le GANAN al .env del servidor—, las conversaciones, la agenda
# durable, las marcas de idempotencia y las colas de salida. Un `docker rm` sin
# volumen, un disco lleno o un dedo equivocado y el sistema vuelve a los valores
# de arranque del .env, que pueden ser MÁS FLOJOS que los que él puso.
# app/limites.py lo detecta y falla cerrado, pero deja todo pendiente hasta que
# alguien restaure. Hasta hoy no había nada que restaurar.
#
# QUÉ GUARDA (y qué NO): ver docs/INFRA.md. En una línea: guarda los DATOS
# (Redis + la base de ERPNext) y NO guarda los SECRETOS (.env) ni la máquina.
#
# CÓMO FALLA: fuerte y temprano. Un respaldo a medias que no avisa es peor que
# no tener respaldo, porque se lo cree el día que hace falta. Todo paso que no
# pueda probar que salió bien corta el script con exit != 0 (y el cron manda el
# error por mail). Se puede correr dos veces sin romper nada.
#
# Uso:  deploy/respaldo.sh [--simulacro] [--con-archivos] [--dias N] [--ayuda]
set -euo pipefail

# ---------------------------------------------------------------- parámetros
# Todo por variable de entorno para que el cron no tenga que editar el script.
# Ningún secreto acá adentro: `bench` saca las credenciales de la base del
# site_config.json del propio sitio, y el Redis de este stack no tiene clave
# (está publicado sólo en loopback, ver deploy/migrar_redis_a_volumen.sh).
RESPALDO_DIR=${RESPALDO_DIR:-/srv/respaldos/plus-agent}
RESPALDO_DIAS=${RESPALDO_DIAS:-30}
RESPALDO_GCS_BUCKET=${RESPALDO_GCS_BUCKET:-}
APP_DIR=${APP_DIR:-/srv/agent-crm/plus-agent}
ERPNEXT_COMPOSE=${ERPNEXT_COMPOSE:-$HOME/gitops/erpnext.yml}
ERPNEXT_PROYECTO=${ERPNEXT_PROYECTO:-erpnext}
ERPNEXT_SITIO=${ERPNEXT_SITIO:-agentcrm4.duckdns.org}
# El contenedor de Redis. Vacío = lo busca solo (servicio `redis` del compose
# del agente y, si no está, el `agent-redis` suelto que deja
# deploy/migrar_redis_a_volumen.sh). Se puede fijar para no depender de eso.
REDIS_CONTENEDOR=${REDIS_CONTENEDOR:-}

SIMULACRO=0
CON_ARCHIVOS=0

ayuda() {
  cat <<'FIN'
Respaldo del agente: un snapshot de Redis + un dump de la base de ERPNext.

  deploy/respaldo.sh [opciones]

  --simulacro, --dry-run   Verifica todo (contenedores, permisos, herramientas)
                           y muestra el plan. NO escribe nada, no toca Redis ni
                           ERPNext. Es lo que hay que correr al instalar el cron.
  --con-archivos           Agrega los adjuntos del sitio (--with-files de bench).
                           Apagado por default: son lentos y grandes, y este
                           despliegue casi no los usa.
  --dias N                 Cuántos días de respaldos locales conservar (default 30).
  --ayuda, --help

Variables (todas opcionales, con estos valores por default):
  RESPALDO_DIR=/srv/respaldos/plus-agent   dónde se guardan
  RESPALDO_DIAS=30                         retención local
  RESPALDO_GCS_BUCKET=                     si está, sube ahí (gs://ESE-BUCKET/...)
  APP_DIR=/srv/agent-crm/plus-agent        compose del agente (proyecto plus-agent)
  ERPNEXT_COMPOSE=$HOME/gitops/erpnext.yml compose de ERPNext (proyecto erpnext)
  ERPNEXT_SITIO=agentcrm4.duckdns.org      el sitio de Frappe
  REDIS_CONTENEDOR=                        fijar sólo si la autodetección falla

Restaurar es OTRO script y a propósito: deploy/restaurar.sh
FIN
}

while [ $# -gt 0 ]; do
  case "$1" in
    --simulacro|--dry-run) SIMULACRO=1 ;;
    --con-archivos|--with-files) CON_ARCHIVOS=1 ;;
    --dias) shift; RESPALDO_DIAS=${1:-30} ;;
    --ayuda|--help|-h) ayuda; exit 0 ;;
    *) echo "[respaldo] opción desconocida: $1 (probá --ayuda)" >&2; exit 2 ;;
  esac
  shift
done

case "$RESPALDO_DIAS" in
  ''|*[!0-9]*) echo "[respaldo] --dias tiene que ser un número entero, vino '$RESPALDO_DIAS'" >&2; exit 2 ;;
esac

log() { printf '[respaldo] %s\n' "$*"; }
morir() { printf '[respaldo] !! %s\n' "$*" >&2; exit 1; }

# `cd` adentro de un subshell: el nombre de proyecto de compose sale del
# DIRECTORIO (plus-agent), igual que en todos los comandos de CLAUDE.md. Si
# alguien corre esto desde otro lado sin el cd, compose no encuentra el stack.
dc()  { ( cd "$APP_DIR" && docker compose "$@" ); }
dce() { docker compose --project-name "$ERPNEXT_PROYECTO" -f "$ERPNEXT_COMPOSE" "$@"; }

MOMENTO=$(date -u +%Y%m%dT%H%M%SZ)
DIA=$(date -u +%Y-%m-%d)
DESTINO="$RESPALDO_DIR/$DIA"

# ------------------------------------------------------------------ preflight
# Todo lo que puede faltar, antes de tocar nada. Un respaldo que descubre a la
# mitad que no puede escribir deja un directorio con medio dump adentro.
command -v docker >/dev/null 2>&1 || morir "no encuentro docker en el PATH"
docker info >/dev/null 2>&1 || morir "docker no responde (¿el usuario está en el grupo docker?)"
[ -d "$APP_DIR" ] || morir "no existe APP_DIR=$APP_DIR"
[ -f "$ERPNEXT_COMPOSE" ] || morir "no existe el compose de ERPNext: $ERPNEXT_COMPOSE"
command -v sha256sum >/dev/null 2>&1 || morir "falta sha256sum (coreutils)"

# El contenedor de Redis, en el orden en que puede existir en este servidor.
if [ -z "$REDIS_CONTENEDOR" ]; then
  REDIS_CONTENEDOR=$(dc ps -q redis 2>/dev/null || true)
fi
if [ -z "$REDIS_CONTENEDOR" ]; then
  # deploy/migrar_redis_a_volumen.sh deja un contenedor SUELTO con este nombre:
  # ahí el `docker compose ps` del agente no lo ve y la autodetección tenía que
  # saberlo o el respaldo de Redis se saltaba en silencio.
  REDIS_CONTENEDOR=$(docker ps -q --filter 'name=^/agent-redis$' || true)
fi
[ -n "$REDIS_CONTENEDOR" ] || morir \
  "no encuentro el contenedor de Redis (probé el servicio 'redis' de $APP_DIR y 'agent-redis'). Fijá REDIS_CONTENEDOR=<nombre>."

BACKEND=$(dce ps -q backend 2>/dev/null || true)
[ -n "$BACKEND" ] || morir "no encuentro el contenedor 'backend' de ERPNext (proyecto $ERPNEXT_PROYECTO, archivo $ERPNEXT_COMPOSE)"

if [ -n "$RESPALDO_GCS_BUCKET" ]; then
  if command -v gcloud >/dev/null 2>&1; then SUBIDOR=gcloud
  elif command -v gsutil >/dev/null 2>&1; then SUBIDOR=gsutil
  else
    # Configurado y sin herramienta: eso es una falla, no un aviso. Un respaldo
    # que se cree remoto y es sólo local es la clase de mentira que se descubre
    # el día del incendio.
    morir "RESPALDO_GCS_BUCKET=$RESPALDO_GCS_BUCKET pero no hay ni gcloud ni gsutil en el PATH"
  fi
else
  SUBIDOR=ninguno
fi

BACKUPS_EN_CONTENEDOR="/home/frappe/frappe-bench/sites/$ERPNEXT_SITIO/private/backups"

if [ "$SIMULACRO" = 1 ]; then
  log "SIMULACRO — no se escribe nada"
  log "  destino          : $DESTINO"
  log "  retención        : $RESPALDO_DIAS días"
  log "  redis            : contenedor $(docker inspect -f '{{.Name}}' "$REDIS_CONTENEDOR" | sed 's|^/||')"
  log "  erpnext          : sitio $ERPNEXT_SITIO en el contenedor backend"
  log "  adjuntos         : $([ "$CON_ARCHIVOS" = 1 ] && echo 'sí (--with-files)' || echo 'no')"
  log "  copia remota     : $([ -n "$RESPALDO_GCS_BUCKET" ] && echo "gs://$RESPALDO_GCS_BUCKET ($SUBIDOR)" || echo 'no configurada')"
  # SIN `mkdir`: la línea de arriba dice «no se escribe nada» y crear el
  # directorio fechado ES escribir. Dejaba una carpeta vacía que la retención y
  # el monitoreo cuentan como un respaldo. Se comprueba el PADRE, que es lo que
  # de verdad hace falta para que el respaldo real pueda crear el suyo.
  padre="$(dirname "$DESTINO")"
  mkdir -p "$padre" 2>/dev/null || morir "no puedo crear $padre (¿permisos? probá: sudo mkdir -p $RESPALDO_DIR && sudo chown \$USER $RESPALDO_DIR)"
  [ -w "$padre" ] || morir "no puedo escribir en $padre"
  log "permisos OK. Todo lo que hace falta está."
  exit 0
fi

mkdir -p "$DESTINO" || morir "no puedo crear $DESTINO"
[ -w "$DESTINO" ] || morir "no puedo escribir en $DESTINO"

# Un solo respaldo a la vez. Sin esto, un respaldo lento y el del día siguiente
# se pisan copiando el mismo dump.rdb a medio escribir. `mkdir` es atómico en
# POSIX; flock no está en todos lados.
LOCK="$RESPALDO_DIR/.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  morir "ya hay un respaldo corriendo (o quedó $LOCK de uno que murió; borralo a mano si estás seguro)"
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

log "destino $DESTINO (momento $MOMENTO)"

# --------------------------------------------------------------------- Redis
# BGSAVE es ASINCRÓNICO: devuelve enseguida y el archivo se escribe después.
# Copiar el dump.rdb sin esperar da el de AYER con cara de ser el de hoy. La
# forma correcta de esperar es comparar LASTSAVE (el epoch del último guardado
# exitoso) antes y después; rdb_bgsave_in_progress sólo sirve de apoyo.
redis_cli() { docker exec -i "$REDIS_CONTENEDOR" redis-cli "$@"; }

ANTES=$(redis_cli LASTSAVE | tr -d '[:space:]')
case "$ANTES" in
  ''|*[!0-9]*) morir "Redis no contesta LASTSAVE (vino '$ANTES')" ;;
esac

log "pidiendo BGSAVE a Redis (LASTSAVE previo $ANTES)"
SALIDA=$(redis_cli BGSAVE 2>&1 || true)
case "$SALIDA" in
  # Un BGSAVE ya en curso NO es un error: su resultado nos sirve igual, sólo
  # hay que esperarlo. Cualquier otro (error) sí corta.
  *"already in progress"*) log "ya había un BGSAVE en curso, lo espero" ;;
  *ERR*|*error*)           morir "Redis rechazó BGSAVE: $SALIDA" ;;
esac

DESPUES="$ANTES"
for _ in $(seq 1 120); do
  DESPUES=$(redis_cli LASTSAVE | tr -d '[:space:]')
  [ "$DESPUES" != "$ANTES" ] && break
  sleep 1
done
[ "$DESPUES" != "$ANTES" ] || morir "Redis no terminó el BGSAVE en 120 s (LASTSAVE sigue en $ANTES)"

ESTADO_BGSAVE=$(redis_cli INFO persistence | tr -d '\r' | sed -n 's/^rdb_last_bgsave_status://p')
[ "$ESTADO_BGSAVE" = "ok" ] || morir "el último BGSAVE terminó en '$ESTADO_BGSAVE' (¿disco lleno?)"

CLAVES=$(redis_cli DBSIZE | tr -d '[:space:]')
RDB="$DESTINO/redis-$MOMENTO.rdb"
docker cp "$REDIS_CONTENEDOR:/data/dump.rdb" "$RDB" || morir "no pude copiar /data/dump.rdb del contenedor de Redis"
[ -s "$RDB" ] || morir "el dump.rdb copiado está vacío"
# Un RDB de verdad empieza con la firma "REDIS". Cuesta una línea y distingue
# «se copió un archivo» de «se copió EL archivo».
FIRMA=$(head -c 5 "$RDB" || true)
[ "$FIRMA" = "REDIS" ] || morir "$RDB no parece un RDB (empieza con '$FIRMA')"
log "Redis: $(du -h "$RDB" | cut -f1), $CLAVES claves"

# ------------------------------------------------------------------- ERPNext
# `bench backup` es el camino soportado: resuelve solo las credenciales de la
# base desde el site_config.json del sitio, así que acá no hay ninguna clave
# escrita ni pasada por línea de comando. Corre desde /home/frappe/frappe-bench
# (el cwd por default del contenedor backend), que es donde bench espera estar:
# el cwd equivocado es el `FileNotFoundError: .../logs/database.log` de CLAUDE.md.
#
# `exec -T`: sin TTY. Desde cron no hay terminal y sin -T docker falla con
# "the input device is not a TTY".
ls_dumps() {
  dce exec -T backend bash -c \
    "ls -1t '$BACKUPS_EN_CONTENEDOR'/*-database.sql.gz 2>/dev/null | head -1" \
    | tr -d '\r' | tr -d '\n'
}

PREVIO=$(ls_dumps || true)
log "pidiendo a bench el backup de $ERPNEXT_SITIO"
if [ "$CON_ARCHIVOS" = 1 ]; then
  dce exec -T backend bench --site "$ERPNEXT_SITIO" backup --with-files >/dev/null \
    || morir "bench backup --with-files falló"
else
  dce exec -T backend bench --site "$ERPNEXT_SITIO" backup >/dev/null \
    || morir "bench backup falló"
fi

NUEVO=$(ls_dumps || true)
[ -n "$NUEVO" ] || morir "bench dijo que salió bien pero no hay ningún *-database.sql.gz en $BACKUPS_EN_CONTENEDOR"
# La comprobación que convierte «bench salió con 0» en «hay un dump NUEVO».
# Sin esto, un bench que falla sin código de error copia el dump de anteayer y
# el manifiesto lo firma como el de hoy.
[ "$NUEVO" != "$PREVIO" ] || morir "bench no generó un dump nuevo (el más reciente sigue siendo $PREVIO)"

PREFIJO=${NUEVO%-database.sql.gz}
SQL="$DESTINO/erpnext-$MOMENTO-database.sql.gz"
CFG="$DESTINO/erpnext-$MOMENTO-site_config.json"
docker cp "$BACKEND:$NUEVO" "$SQL" || morir "no pude copiar $NUEVO del contenedor backend"
[ -s "$SQL" ] || morir "el dump de ERPNext copiado está vacío"
# gzip íntegro o no sirve de nada el día de la restauración.
gzip -t "$SQL" 2>/dev/null || morir "$SQL no pasa gzip -t (copia corrupta)"

# El site_config del sitio viaja al lado del dump: trae las credenciales con las
# que el sitio habla con su base. Sin él, restaurar en una máquina nueva es
# adivinar. OJO: por eso mismo el directorio de respaldos NO es público.
if dce exec -T backend test -f "${PREFIJO}-site_config_backup.json" 2>/dev/null; then
  docker cp "$BACKEND:${PREFIJO}-site_config_backup.json" "$CFG" \
    || morir "no pude copiar el site_config del respaldo"
else
  log "aviso: bench no dejó site_config_backup.json (no es fatal, pero anotalo)"
  CFG=""
fi

ARCHIVOS_TAR=""
if [ "$CON_ARCHIVOS" = 1 ]; then
  for sufijo in files private-files; do
    origen="${PREFIJO}-${sufijo}.tar"
    if dce exec -T backend test -f "$origen" 2>/dev/null; then
      docker cp "$BACKEND:$origen" "$DESTINO/erpnext-$MOMENTO-${sufijo}.tar" \
        || morir "no pude copiar $origen"
      ARCHIVOS_TAR="$ARCHIVOS_TAR $DESTINO/erpnext-$MOMENTO-${sufijo}.tar"
    fi
  done
fi
log "ERPNext: $(du -h "$SQL" | cut -f1) de base$([ -n "$ARCHIVOS_TAR" ] && echo ' + adjuntos')"

# ---------------------------------------------------------------- manifiesto
# Lo que hace verificable a un respaldo: qué hay, de cuándo, de qué versión del
# código y con qué sha256. deploy/restaurar.sh lo LEE y se niega a restaurar un
# archivo cuyo hash no coincide, así que esto no es documentación: es la prueba.
MANIFIESTO="$DESTINO/MANIFIESTO-$MOMENTO.txt"
SHA_REPO=$(git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo desconocido)
{
  echo "respaldo         : $MOMENTO (UTC)"
  echo "host             : $(hostname)"
  echo "repo             : $SHA_REPO"
  echo "sitio erpnext    : $ERPNEXT_SITIO"
  echo "redis: claves    : $CLAVES"
  echo "redis: lastsave  : $DESPUES"
  echo "adjuntos         : $([ "$CON_ARCHIVOS" = 1 ] && echo sí || echo no)"
  echo
  echo "NO INCLUYE: el .env (los seis pares de claves de ERPNext, el token de"
  echo "Meta y la clave de Gemini), el docker-compose.override.yml, la VM, los"
  echo "certificados ni el DNS. Ver docs/INFRA.md."
  echo
  echo "sha256:"
  ( cd "$DESTINO" && sha256sum "$(basename "$RDB")" "$(basename "$SQL")" \
      $([ -n "$CFG" ] && basename "$CFG") \
      $(for t in $ARCHIVOS_TAR; do basename "$t"; done) )
} > "$MANIFIESTO"
log "manifiesto $MANIFIESTO"

# ------------------------------------------------------------------ copia GCS
# Un respaldo en el mismo disco que los datos no es un respaldo: el modo de
# falla más común de una VM sola es perder el disco.
if [ -n "$RESPALDO_GCS_BUCKET" ]; then
  RUTA_REMOTA="gs://$RESPALDO_GCS_BUCKET/plus-agent/$DIA/"
  log "subiendo a $RUTA_REMOTA con $SUBIDOR"
  if [ "$SUBIDOR" = gcloud ]; then
    gcloud storage cp "$DESTINO"/* "$RUTA_REMOTA" >/dev/null || morir "la subida a GCS falló"
    gcloud storage ls "$RUTA_REMOTA$(basename "$MANIFIESTO")" >/dev/null \
      || morir "subí sin error pero el manifiesto no está en el bucket"
  else
    gsutil -m cp "$DESTINO"/* "$RUTA_REMOTA" >/dev/null || morir "la subida a GCS falló"
    gsutil ls "$RUTA_REMOTA$(basename "$MANIFIESTO")" >/dev/null \
      || morir "subí sin error pero el manifiesto no está en el bucket"
  fi
  log "copia remota verificada"
else
  log "sin RESPALDO_GCS_BUCKET: la única copia está en este disco (ver docs/INFRA.md)"
fi

# -------------------------------------------------------------------- limpieza
# Retención local. El patrón del nombre acota el borrado a los directorios que
# creó ESTE script: un `rm -rf` guiado sólo por -mtime en un directorio que
# alguien reutilizó es cómo se borra otra cosa.
if [ "$RESPALDO_DIAS" -gt 0 ]; then
  BORRADOS=$(find "$RESPALDO_DIR" -mindepth 1 -maxdepth 1 -type d \
    -name '20[0-9][0-9]-[0-1][0-9]-[0-3][0-9]' -mtime +"$RESPALDO_DIAS" -print | wc -l)
  find "$RESPALDO_DIR" -mindepth 1 -maxdepth 1 -type d \
    -name '20[0-9][0-9]-[0-1][0-9]-[0-3][0-9]' -mtime +"$RESPALDO_DIAS" -exec rm -rf {} +
  [ "$BORRADOS" -gt 0 ] && log "retención: borré $BORRADOS día(s) de más de $RESPALDO_DIAS días"
fi

# Y la copia que queda ADENTRO del contenedor de ERPNext, que si no crece para
# siempre contra los 50 GB del disco. Se borra sólo lo que bench genera y sólo
# lo más viejo que la retención — nunca lo que se acaba de copiar.
dce exec -T backend bash -c \
  "find '$BACKUPS_EN_CONTENEDOR' -maxdepth 1 -type f \\( -name '*-database.sql.gz' -o -name '*-site_config_backup.json' -o -name '*-files.tar' \\) -mtime +$RESPALDO_DIAS -delete" \
  >/dev/null 2>&1 || log "aviso: no pude limpiar los backups viejos adentro del contenedor"

log "listo. $DESTINO"
log "probá la restauración de vez en cuando: un respaldo que nadie restauró es un rumor."
