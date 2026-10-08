#!/usr/bin/env bash
# Comprueba la configuración local y la sube como secreto MONITOR_CONFIG.
# Uso: ./subir-config.sh [fichero]   (por defecto monitor_config.json)
set -euo pipefail
cd "$(dirname "$0")"
file="${1:-monitor_config.json}"
[ -s "$file" ] || { echo "No existe o está vacío: $file" >&2; exit 1; }
MONITOR_CONFIG="$(cat "$file")" python3 monitor.py --check-config
gh secret set MONITOR_CONFIG < "$file"
