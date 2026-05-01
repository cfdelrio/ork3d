#!/usr/bin/env bash
# ORK3D · Verifica que el sitio responde correctamente.
# Uso: ./verify.sh [www.ork3d.com]
set -euo pipefail

HOST="${1:-www.ork3d.com}"
URLS=(
  "https://${HOST}/"
  "https://${HOST}/assets/css/styles.css"
  "https://${HOST}/assets/js/main.js"
  "https://${HOST}/config.js"
)

ok=0
fail=0

for url in "${URLS[@]}"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" -L --max-time 10 "$url" || echo "000")
  if [[ "$code" =~ ^(200|301|302)$ ]]; then
    echo "✓ ${url} → ${code}"
    ok=$((ok+1))
  else
    echo "✗ ${url} → ${code}"
    fail=$((fail+1))
  fi
done

echo ""
echo "Resultado: ${ok} OK, ${fail} fallidos"
exit "${fail}"
