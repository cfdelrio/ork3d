#!/usr/bin/env bash
# Suite completa de ORK3D: API unitaria + contrato CLI/API, CLI, E2E en Chromium y validaciones estáticas.
# Uso (desde la raíz del repo):  dev/suite.sh
# Sale con 1 si alguna capa falla. Ninguna capa se saltea: si falta una herramienta, la capa falla.
set -uo pipefail

cd "$(dirname "$0")/.."

if [[ -z "${NODE_PATH:-}" && -d /opt/node22/lib/node_modules ]]; then
  export NODE_PATH=/opt/node22/lib/node_modules
fi
SHOTS_DIR="${SHOTS_DIR:-dev/e2e/capturas}"
export SHOTS_DIR

fallos=0
resumen=()

capa() {  # capa NOMBRE COMANDO...
  local nombre="$1"; shift
  echo
  echo "════ ${nombre} ════"
  if "$@"; then
    resumen+=("PASS  ${nombre}")
  else
    resumen+=("FAIL  ${nombre}")
    fallos=$((fallos + 1))
  fi
}

capa "API unitaria y contrato CLI/API" python3 -m unittest discover -s api/resenas -v
capa "Infra: script y políticas (sin AWS)" python3 -m unittest discover -s infra/tests -v
capa "CLI de moderación (admin/tests)" bash admin/tests/run_tests.sh
capa "Browser E2E (Chromium)" node dev/e2e/opinar.e2e.js
capa "Validaciones HTML/JS/shell/JSON/IAM" python3 dev/validar.py

rm -rf api/resenas/__pycache__ dev/__pycache__ admin/tests/__pycache__ infra/tests/__pycache__

echo
echo "════ Resumen ════"
printf '%s\n' "${resumen[@]}"
if (( fallos > 0 )); then
  echo "Suite: ${fallos} capa(s) fallando."
  exit 1
fi
echo "Suite: todas las capas en verde."
