#!/bin/sh
# Arranca el bot sin Docker, desacoplado, y no hace nada si ya está corriendo.
# Lo lanza el bot de Telegram (.tg-service) al arrancar el contenedor y cada 5 minutos.
cd "$(dirname "$0")" || exit 1
mkdir -p data
PID_FILE=data/bot.pid
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null \
   && grep -q bot.py "/proc/$(cat "$PID_FILE")/cmdline" 2>/dev/null; then
    exit 0
fi
set -a; . ./.env; set +a
export PYTHONUNBUFFERED=1 TZ="${TZ:-Europe/Madrid}"
setsid nohup python3 bot.py >> data/bot.log 2>&1 < /dev/null &
echo $! > "$PID_FILE"
