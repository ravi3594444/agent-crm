#!/bin/bash
# El supervisor del agente: quién es, cómo se lanza y cómo se lo reconoce.
#
# Lo carga start.sh con `source`. Acá no corre nada al cargar: son funciones.
#
# EL PROBLEMA QUE RESUELVE
# start.sh guarda el pid del supervisor en un archivo y, al volver de un
# reinicio, pregunta «¿sigue vivo?» antes de lanzar otro. Preguntarlo con
# `kill -0` no alcanza: tras un reboot el pid puede ser de cualquier proceso
# que el SO recicló. Y preguntar «¿su línea de comando dice uvicorn?» tampoco:
# cualquier otro Uvicorn de la máquina —otro proyecto, una prueba— pasaba por
# nuestro supervisor, start.sh se quedaba esperando su /health y el agente de
# WhatsApp no arrancaba nunca.
#
# LA MARCA
# El supervisor se lanza con un argumento propio, `plus-agent-supervisor=<pidfile>`,
# que queda en su línea de comando (/proc/<pid>/cmdline) como un elemento
# entero. Sólo un proceso lanzado por supervisor_lanzar con ESE pidfile lo
# lleva: ni otro uvicorn, ni otro bash, ni otra instancia de este mismo agente
# con otro pidfile. supervisor_vivo exige el elemento exacto, no un substring.
#
# EL PIDFILE
# Sólo dígitos; cualquier otra cosa se trata como inválido. Lo escribe el
# propio bucle ($$) y lo borra él al salir, también cuando lo matan: la señal
# corta el `wait`, el trap mata al hijo y limpia.

supervisor_marca() {
  # $1 = pidfile absoluto. Una marca por instancia, legible en `ps`.
  printf 'plus-agent-supervisor=%s' "$1"
}

supervisor_vivo() {
  # $1 = pidfile. 0 si ESE archivo apunta a NUESTRO supervisor, vivo.
  local pidfile=$1 contenido pid marca
  [ -f "$pidfile" ] || return 1
  contenido=$(cat "$pidfile" 2>/dev/null) || return 1
  pid=$(printf '%s' "$contenido" | tr -d '[:space:]')
  case $pid in
    ''|*[!0-9]*) return 1 ;;
  esac
  kill -0 "$pid" 2>/dev/null || return 1
  marca=$(supervisor_marca "$pidfile")
  tr '\0' '\n' <"/proc/$pid/cmdline" 2>/dev/null | grep -qxF -- "$marca"
}

supervisor_lanzar() {
  # $1 = APP, $2 = PORT, $3 = pidfile, $4 = log, $5... = comando a supervisar.
  # Sin comando, el agente de verdad: el uvicorn del venv de APP.
  local app=$1 port=$2 pidfile=$3 log=$4
  shift 4
  if [ $# -eq 0 ]; then
    set -- "$app/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port "$port" --no-access-log
  fi
  # setsid: sesión propia, sobrevive al shell que lo lanzó. La marca viaja
  # como argumento para que quede en la línea de comando del supervisor. El
  # pid lo escribe el propio bucle ($$): `$!` sería el de setsid, que muere
  # enseguida al re-forkear, y supervisor_vivo no lo encontraría.
  # PYTHONUNBUFFERED: los print() del agente van a un archivo; sin esto quedan
  # en el buffer y el log parece vacío justo cuando hace falta leerlo.
  # El comando corre en segundo plano y se lo espera con `wait`: así una señal
  # al supervisor interrumpe la espera, el trap mata al hijo, borra el pidfile
  # y sale, en vez de dejar un uvicorn huérfano y un pidfile que miente.
  setsid nohup bash -c '
    cd "$1" || exit 1
    pidfile=$3
    shift 4
    echo $$ >"$pidfile"
    hijo=
    limpiar() { [ -n "$hijo" ] && kill "$hijo" 2>/dev/null; rm -f "$pidfile"; }
    trap '"'"'limpiar; exit 0'"'"' TERM INT HUP
    trap limpiar EXIT
    export PYTHONUNBUFFERED=1
    while true; do
      "$@" & hijo=$!
      wait "$hijo"; codigo=$?
      hijo=
      echo "[start] el agente terminó (exit $codigo); reinicio en 3 s"
      sleep 3 & hijo=$!
      wait "$hijo"
      hijo=
    done' _ "$app" "$port" "$pidfile" "$(supervisor_marca "$pidfile")" "$@" >"$log" 2>&1 </dev/null &
}
