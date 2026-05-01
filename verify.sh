#!/usr/bin/env bash
# ORK3D · Verifica que el sitio responde correctamente.
set -euo pipefail

DOMAIN="${1:-ork3d.com}"
URLS=(
  "https://${DOMAIN}/"
  "https://${DOMAIN}/assets/css/styles.css"
  "https://${DOMAIN}/assets/js/main.js"
  "https://www.${DOMAIN}/"
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
