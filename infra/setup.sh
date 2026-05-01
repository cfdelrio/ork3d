#!/usr/bin/env bash
# ORK3D · Setup AWS (one-time, simplificado para www-only).
#
# Sirve sólo www.ork3d.com. El apex (ork3d.com) NO se configura en este flujo
# porque Donweb no soporta ANAME/ALIAS en el apex.
#
# Crea:
#   - Bucket S3 privado: ork3d.com
#   - OAC (Origin Access Control) para CloudFront
#   - Certificado ACM en us-east-1 sólo para www.ork3d.com
#   - 1 CloudFront distribution para www.ork3d.com
#
# Requisitos:
#   - AWS CLI v2 (en CloudShell ya viene)
#   - jq (en CloudShell ya viene)
#
# El script PAUSA después de pedir el cert ACM para que cargues el CNAME
# de validación en Donweb antes de seguir.

set -euo pipefail

DOMAIN="ork3d.com"
WWW_DOMAIN="www.${DOMAIN}"
BUCKET="${DOMAIN}"
REGION="${AWS_REGION:-us-east-1}"
ACM_REGION="us-east-1"

cd "$(dirname "$0")"

echo "════════════════════════════════════════════════════════════"
echo "  ORK3D · Setup de infraestructura AWS (www-only)"
echo "════════════════════════════════════════════════════════════"
echo "  Sitio:       https://${WWW_DOMAIN}"
echo "  Apex:        ${DOMAIN} → NO se configura (limitación Donweb)"
echo "  Bucket:      s3://${BUCKET}  (privado)"
echo "  Región S3:   ${REGION}"
echo "  Región ACM:  ${ACM_REGION}"
echo "════════════════════════════════════════════════════════════"
read -rp "¿Continuar? (escribir 'si' para crear recursos): " ans
[[ "$ans" == "si" ]] || { echo "Abortado."; exit 1; }

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "→ Cuenta AWS: ${ACCOUNT_ID}"

# ────────────────────────────────────────────────────────────
# 1) Bucket S3 privado
# ────────────────────────────────────────────────────────────
echo ""
echo "[1/5] Bucket S3 privado ${BUCKET} ..."
if aws s3api head-bucket --bucket "${BUCKET}" 2>/dev/null; then
  echo "    ya existe."
else
  if [[ "${REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}"
  else
    aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" \
      --create-bucket-configuration LocationConstraint="${REGION}"
  fi
fi

aws s3api put-public-access-block --bucket "${BUCKET}" \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

aws s3api put-bucket-encryption --bucket "${BUCKET}" \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

aws s3api put-bucket-versioning --bucket "${BUCKET}" \
  --versioning-configuration Status=Enabled

# ────────────────────────────────────────────────────────────
# 2) Certificado ACM (us-east-1) sólo para www
# ────────────────────────────────────────────────────────────
echo ""
echo "[2/5] Certificado ACM para ${WWW_DOMAIN} ..."
CERT_ARN=$(aws acm list-certificates --region "${ACM_REGION}" \
  --query "CertificateSummaryList[?DomainName=='${WWW_DOMAIN}'].CertificateArn | [0]" \
  --output text)

if [[ "${CERT_ARN}" == "None" || -z "${CERT_ARN}" ]]; then
  CERT_ARN=$(aws acm request-certificate --region "${ACM_REGION}" \
    --domain-name "${WWW_DOMAIN}" \
    --validation-method DNS \
    --query CertificateArn --output text)
  echo "    Cert ARN: ${CERT_ARN}"
  echo "    Esperando que ACM publique los registros DNS de validación ..."
  sleep 8
fi

echo "    Cert ARN: ${CERT_ARN}"
echo ""
echo "    >>> CARGÁ ESTE CNAME EN DONWEB <<<"
aws acm describe-certificate --region "${ACM_REGION}" --certificate-arn "${CERT_ARN}" \
  --query 'Certificate.DomainValidationOptions[].ResourceRecord.[Name,Type,Value]' \
  --output table

read -rp "¿Cargaste el CNAME en Donweb y querés esperar la validación? (si/skip): " v
if [[ "${v}" == "si" ]]; then
  echo "    Esperando validación (puede tardar 5-30 min) ..."
  aws acm wait certificate-validated --region "${ACM_REGION}" --certificate-arn "${CERT_ARN}"
  echo "    ✓ Certificado validado."
else
  echo "    Saltando espera. Vas a tener que correr el script de nuevo cuando valide,"
  echo "    o crear la distribution manualmente."
  exit 0
fi

# ────────────────────────────────────────────────────────────
# 3) Origin Access Control (OAC)
# ────────────────────────────────────────────────────────────
echo ""
echo "[3/5] Origin Access Control ..."
OAC_ID=$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='ork3d-oac'].Id | [0]" \
  --output text 2>/dev/null || echo "None")

if [[ "${OAC_ID}" == "None" || -z "${OAC_ID}" ]]; then
  OAC_ID=$(aws cloudfront create-origin-access-control --origin-access-control-config '{
    "Name": "ork3d-oac",
    "Description": "OAC for ork3d.com static site",
    "OriginAccessControlOriginType": "s3",
    "SigningBehavior": "always",
    "SigningProtocol": "sigv4"
  }' --query 'OriginAccessControl.Id' --output text)
fi
echo "    OAC: ${OAC_ID}"

# ────────────────────────────────────────────────────────────
# 4) CloudFront distribution para www
# ────────────────────────────────────────────────────────────
echo ""
echo "[4/5] CloudFront distribution para ${WWW_DOMAIN} ..."
CFG=$(mktemp)
cat > "${CFG}" <<JSON
{
  "CallerReference": "ork3d-www-$(date +%s)",
  "Comment": "ORK3D www site",
  "Enabled": true,
  "Aliases": { "Quantity": 1, "Items": ["${WWW_DOMAIN}"] },
  "DefaultRootObject": "index.html",
  "PriceClass": "PriceClass_100",
  "HttpVersion": "http2",
  "IsIPV6Enabled": true,
  "Origins": {
    "Quantity": 1,
    "Items": [{
      "Id": "s3-${BUCKET}",
      "DomainName": "${BUCKET}.s3.${REGION}.amazonaws.com",
      "OriginAccessControlId": "${OAC_ID}",
      "S3OriginConfig": { "OriginAccessIdentity": "" },
      "ConnectionAttempts": 3,
      "ConnectionTimeout": 10
    }]
  },
  "DefaultCacheBehavior": {
    "TargetOriginId": "s3-${BUCKET}",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": { "Quantity": 2, "Items": ["GET","HEAD"], "CachedMethods": { "Quantity": 2, "Items": ["GET","HEAD"] } },
    "Compress": true,
    "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
    "ResponseHeadersPolicyId": "67f7725c-6f97-4210-82d7-5512b31e9d03"
  },
  "CustomErrorResponses": {
    "Quantity": 2,
    "Items": [
      { "ErrorCode": 403, "ResponseCode": "404", "ResponsePagePath": "/error.html", "ErrorCachingMinTTL": 60 },
      { "ErrorCode": 404, "ResponseCode": "404", "ResponsePagePath": "/error.html", "ErrorCachingMinTTL": 60 }
    ]
  },
  "ViewerCertificate": {
    "ACMCertificateArn": "${CERT_ARN}",
    "SSLSupportMethod": "sni-only",
    "MinimumProtocolVersion": "TLSv1.2_2021"
  }
}
JSON

DIST_JSON=$(aws cloudfront create-distribution --distribution-config "file://${CFG}")
DIST_ID=$(echo "${DIST_JSON}" | jq -r '.Distribution.Id')
DIST_DOMAIN=$(echo "${DIST_JSON}" | jq -r '.Distribution.DomainName')
rm -f "${CFG}"
echo "    Distribution ID:    ${DIST_ID}"
echo "    Distribution domain: ${DIST_DOMAIN}"

# ────────────────────────────────────────────────────────────
# 5) Bucket policy (sólo CloudFront vía OAC)
# ────────────────────────────────────────────────────────────
echo ""
echo "[5/5] Bucket policy ..."
POL=$(mktemp)
sed -e "s|__BUCKET__|${BUCKET}|g" \
    -e "s|__ACCOUNT_ID__|${ACCOUNT_ID}|g" \
    -e "s|__DISTRIBUTION_ID__|${DIST_ID}|g" \
    bucket-policy.template.json > "${POL}"

aws s3api put-bucket-policy --bucket "${BUCKET}" --policy "file://${POL}"
rm -f "${POL}"

# ────────────────────────────────────────────────────────────
# Guardar variables
# ────────────────────────────────────────────────────────────
cat > .env <<ENV
BUCKET=${BUCKET}
DISTRIBUTION_ID=${DIST_ID}
DIST_DOMAIN=${DIST_DOMAIN}
CERT_ARN=${CERT_ARN}
OAC_ID=${OAC_ID}
ENV

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  ✓ Setup completo"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "DNS final que tenés que cargar en Donweb (Zona DNS):"
echo ""
echo "  Tipo: CNAME"
echo "  Nombre: www"
echo "  Contenido: ${DIST_DOMAIN}"
echo "  TTL: 14400"
echo ""
echo "(Si ya existe un CNAME para www apuntando a ork3d.com, EDITALO con este valor.)"
echo ""
echo "Cuando el DNS propague, desde la raíz del repo corré:"
echo "  ./deploy.sh"
echo "  ./verify.sh www.ork3d.com"
echo ""
