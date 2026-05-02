#!/usr/bin/env bash
# ORK3D · Verifica que el sitio responde correctamente.
# Uso: ./verify.sh
set -euo pipefail

URLS=(
  "https://ork3d.com/"
  "https://ork3d.com/assets/css/styles.css"
  "https://ork3d.com/assets/js/main.js"
  "https://ork3d.com/config.js"
  "https://www.ork3d.com/"
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
# Verificá que www redirige al apex
echo -n "Redirect www → apex: "
loc=$(curl -s -o /dev/null -w "%{redirect_url}" "https://www.ork3d.com/" || echo "")
if [[ "$loc" == "https://ork3d.com/" ]]; then
  echo "✓ ${loc}"
else
  echo "✗ ${loc:-sin redirect}"
fi

echo ""
echo "Resultado: ${ok} OK, ${fail} fallidos"
exit "${fail}"
