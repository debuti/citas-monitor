#!/usr/bin/env bash
# Instala citas-monitor como servicio de systemd: arranca solo al encender la máquina
# y se reinicia solo si se cae. Uso: sudo ./instalar.sh
set -euo pipefail
cd "$(dirname "$0")"
dir="$(pwd)"
[ "$(id -u)" = 0 ] || { echo "Ejecútalo con sudo: sudo ./instalar.sh" >&2; exit 1; }
docker="$(command -v docker)" || { echo "No encuentro docker." >&2; exit 1; }
"$docker" compose version >/dev/null || { echo "Falta el plugin 'docker compose'." >&2; exit 1; }
[ -s .env ] || { echo "Falta .env: cp .env.example .env y rellénalo." >&2; exit 1; }
grep -q '^ADMIN_CHAT_ID=[0-9-]\+$' .env || { echo "Falta ADMIN_CHAT_ID en .env." >&2; exit 1; }
# Si no hay vigilancias fijas, un fichero vacío para que el volumen no cree una carpeta.
[ -e monitor_config.json ] || echo '[]' > monitor_config.json
mkdir -p data
sed -e "s#@DIR@#$dir#g" -e "s#@DOCKER@#$docker#g" citas-monitor.service \
  > /etc/systemd/system/citas-monitor.service
systemctl daemon-reload
systemctl enable docker.service
systemctl enable --now citas-monitor.service
systemctl restart citas-monitor.service
echo "Instalado. Estado:  systemctl status citas-monitor"
echo "Registro:           journalctl -u citas-monitor -f"
