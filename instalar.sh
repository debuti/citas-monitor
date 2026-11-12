#!/usr/bin/env bash
# Instala citas-monitor como servicio de usuario de systemd (sin sudo): arranca solo al
# encender la máquina y se reinicia solo si se cae. Uso: ./instalar.sh
# Requisitos: estar en el grupo docker y tener "linger" (loginctl enable-linger).
set -euo pipefail
cd "$(dirname "$0")"
dir="$(pwd)"
docker="$(command -v docker)" || { echo "No encuentro docker." >&2; exit 1; }
"$docker" info >/dev/null 2>&1 || { echo "No puedo usar docker (¿grupo docker?)." >&2; exit 1; }
"$docker" compose version >/dev/null || { echo "Falta el plugin 'docker compose'." >&2; exit 1; }
[ -s .env ] || { echo "Falta .env: cp .env.example .env y rellénalo." >&2; exit 1; }
grep -q '^ADMIN_CHAT_ID=[0-9-]\+$' .env || { echo "Falta ADMIN_CHAT_ID en .env." >&2; exit 1; }
[ "$(loginctl show-user "$USER" -p Linger --value)" = yes ] \
  || echo "Aviso: sin linger no arrancará al encender: sudo loginctl enable-linger $USER" >&2
# Si no hay vigilancias fijas, un fichero vacío para que el volumen no cree una carpeta.
[ -e monitor_config.json ] || echo '[]' > monitor_config.json
mkdir -p data ~/.config/systemd/user
sed -e "s#@DIR@#$dir#g" -e "s#@DOCKER@#$docker#g" citas-monitor.service \
  > ~/.config/systemd/user/citas-monitor.service
systemctl --user daemon-reload
systemctl --user enable citas-monitor.service
systemctl --user restart citas-monitor.service
echo "Instalado. Estado:  systemctl --user status citas-monitor"
echo "Registro:           journalctl --user -u citas-monitor -f"
