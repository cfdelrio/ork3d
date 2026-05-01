#!/usr/bin/env bash
# ORK3D · Teardown · Eliminá toda la infra creada por setup.sh.
# Útil si querés reiniciar o si decidís no usar AWS.
# DESTRUCTIVO. Pide confirmación.
set -euo pipefail

cd "$(dirname "$0")"
[[ -f .env ]] && source .env || { echo "No hay infra/.env, nada para borrar."; exit 1; }

echo "Esto va a eliminar:"
echo "  - Distribution:       ${DISTRIBUTION_ID:-?}"
echo "  - Bucket S3:          ${BUCKET:-?}  (y todo su contenido)"
echo "  - Cert ACM:           ${CERT_ARN:-?}"
read -rp "Escribí 'BORRAR' para confirmar: " ans
[[ "$ans" == "BORRAR" ]] || { echo "Abortado."; exit 1; }

disable_and_delete() {
  local id="$1"
  [[ -z "$id" ]] && return
  echo "→ Deshabilitando distribution ${id} ..."
  local etag cfg
  cfg=$(aws cloudfront get-distribution-config --id "$id")
  etag=$(echo "$cfg" | jq -r '.ETag')
  echo "$cfg" | jq '.DistributionConfig.Enabled=false' | jq '.DistributionConfig' \
    > /tmp/cfg.json
  aws cloudfront update-distribution --id "$id" --if-match "$etag" \
    --distribution-config file:///tmp/cfg.json >/dev/null
  echo "→ Esperando que se deshabilite (puede tardar ~10 min) ..."
  aws cloudfront wait distribution-deployed --id "$id"
  etag=$(aws cloudfront get-distribution-config --id "$id" --query ETag --output text)
  aws cloudfront delete-distribution --id "$id" --if-match "$etag"
  echo "✓ Distribution ${id} eliminada."
}

disable_and_delete "${WWW_DISTRIBUTION_ID:-}"  # legacy, may not exist
disable_and_delete "${DISTRIBUTION_ID:-}"

if [[ -n "${BUCKET:-}" ]]; then
  echo "→ Vaciando y borrando bucket ${BUCKET} ..."
  aws s3 rm "s3://${BUCKET}" --recursive || true
  aws s3api delete-bucket --bucket "${BUCKET}" || true
fi

if [[ -n "${CERT_ARN:-}" ]]; then
  echo "→ Borrando certificado ACM ..."
  aws acm delete-certificate --region us-east-1 --certificate-arn "${CERT_ARN}" || true
fi

rm -f .env
echo "✓ Teardown completo."
