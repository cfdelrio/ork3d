#!/usr/bin/env bash
# ORK3D · Setup AWS (one-time, apex + www con DNS en Cloudflare).
#
# Sirve ork3d.com (canónico) y www.ork3d.com (redirige al apex con 301).
#
# Crea:
#   - Bucket S3 privado: ork3d.com
#   - OAC (Origin Access Control)
#   - Cert ACM en us-east-1 para ork3d.com + www.ork3d.com
#   - CloudFront Function: redirige www → apex
#   - 1 CloudFront distribution con ambos aliases (función asociada)
#
# Requiere DNS en Cloudflare (CNAME flattening en apex).
# El script PAUSA tras pedir el cert para que cargues los CNAMEs en Cloudflare.

set -euo pipefail

DOMAIN="ork3d.com"
WWW_DOMAIN="www.${DOMAIN}"
BUCKET="${DOMAIN}"
REGION="${AWS_REGION:-us-east-1}"
ACM_REGION="us-east-1"

cd "$(dirname "$0")"

echo "════════════════════════════════════════════════════════════"
echo "  ORK3D · Setup de infraestructura AWS"
echo "════════════════════════════════════════════════════════════"
echo "  Apex:        https://${DOMAIN}        (canónico)"
echo "  www:         https://${WWW_DOMAIN} → 301 → apex"
echo "  Bucket:      s3://${BUCKET}  (privado)"
echo "  Región S3:   ${REGION}"
echo "  Región ACM:  ${ACM_REGION}"
echo "  DNS:         Cloudflare (CNAME flattening)"
echo "════════════════════════════════════════════════════════════"
read -rp "¿Continuar? (escribir 'si' para crear recursos): " ans
[[ "$ans" == "si" ]] || { echo "Abortado."; exit 1; }

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "→ Cuenta AWS: ${ACCOUNT_ID}"

# ────────────────────────────────────────────────────────────
# 1) Bucket S3 privado
# ────────────────────────────────────────────────────────────
echo ""
echo "[1/6] Bucket S3 privado ${BUCKET} ..."
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
# 2) Certificado ACM (us-east-1) apex + www
# ────────────────────────────────────────────────────────────
echo ""
echo "[2/6] Certificado ACM para ${DOMAIN} y ${WWW_DOMAIN} ..."
CERT_ARN=$(aws acm list-certificates --region "${ACM_REGION}" \
  --query "CertificateSummaryList[?DomainName=='${DOMAIN}'].CertificateArn | [0]" \
  --output text)

if [[ "${CERT_ARN}" == "None" || -z "${CERT_ARN}" ]]; then
  CERT_ARN=$(aws acm request-certificate --region "${ACM_REGION}" \
    --domain-name "${DOMAIN}" \
    --subject-alternative-names "${WWW_DOMAIN}" \
    --validation-method DNS \
    --query CertificateArn --output text)
  echo "    Cert ARN: ${CERT_ARN}"
  echo "    Esperando que ACM publique los registros DNS de validación ..."
  sleep 8
fi

echo "    Cert ARN: ${CERT_ARN}"
echo ""
echo "    >>> CARGÁ ESTOS CNAMES EN CLOUDFLARE (Proxy: DNS only / nube gris) <<<"
aws acm describe-certificate --region "${ACM_REGION}" --certificate-arn "${CERT_ARN}" \
  --query 'Certificate.DomainValidationOptions[].ResourceRecord.[Name,Type,Value]' \
  --output table

read -rp "¿Cargaste los CNAMEs en Cloudflare y querés esperar la validación? (si/skip): " v
if [[ "${v}" == "si" ]]; then
  echo "    Esperando validación (con Cloudflare suele ser 2-5 min) ..."
  aws acm wait certificate-validated --region "${ACM_REGION}" --certificate-arn "${CERT_ARN}"
  echo "    ✓ Certificado validado."
else
  echo "    Saltando espera. Re-corré el script cuando valide."
  exit 0
fi

# ────────────────────────────────────────────────────────────
# 3) Origin Access Control (OAC)
# ────────────────────────────────────────────────────────────
echo ""
echo "[3/6] Origin Access Control ..."
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
# 4) CloudFront Function (www → apex)
# ────────────────────────────────────────────────────────────
echo ""
echo "[4/6] CloudFront Function 'ork3d-redirect-www' ..."
FN_NAME="ork3d-redirect-www"
FN_EXISTS=$(aws cloudfront list-functions \
  --query "FunctionList.Items[?Name=='${FN_NAME}'].Name | [0]" --output text 2>/dev/null || echo "None")

if [[ "${FN_EXISTS}" == "None" || -z "${FN_EXISTS}" ]]; then
  aws cloudfront create-function \
    --name "${FN_NAME}" \
    --function-config "Comment='Redirect www to apex',Runtime=cloudfront-js-2.0" \
    --function-code fileb://redirect-www.js >/dev/null
fi

FN_ETAG=$(aws cloudfront describe-function --name "${FN_NAME}" --query 'ETag' --output text)
aws cloudfront update-function \
  --name "${FN_NAME}" \
  --if-match "${FN_ETAG}" \
  --function-config "Comment='Redirect www to apex',Runtime=cloudfront-js-2.0" \
  --function-code fileb://redirect-www.js >/dev/null
FN_ETAG=$(aws cloudfront describe-function --name "${FN_NAME}" --query 'ETag' --output text)
aws cloudfront publish-function --name "${FN_NAME}" --if-match "${FN_ETAG}" >/dev/null
FN_ARN=$(aws cloudfront describe-function --name "${FN_NAME}" --stage LIVE \
  --query 'FunctionSummary.FunctionMetadata.FunctionARN' --output text)
echo "    Function ARN: ${FN_ARN}"

# ────────────────────────────────────────────────────────────
# 5) CloudFront distribution (sirve ambos aliases)
# ────────────────────────────────────────────────────────────
echo ""
echo "[5/6] CloudFront distribution para ${DOMAIN} + ${WWW_DOMAIN} ..."
CFG=$(mktemp)
cat > "${CFG}" <<JSON
{
  "CallerReference": "ork3d-$(date +%s)",
  "Comment": "ORK3D apex + www",
  "Enabled": true,
  "Aliases": { "Quantity": 2, "Items": ["${DOMAIN}", "${WWW_DOMAIN}"] },
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
    "ResponseHeadersPolicyId": "67f7725c-6f97-4210-82d7-5512b31e9d03",
    "FunctionAssociations": {
      "Quantity": 1,
      "Items": [{ "FunctionARN": "${FN_ARN}", "EventType": "viewer-request" }]
    }
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
# 6) Bucket policy (sólo CloudFront vía OAC)
# ────────────────────────────────────────────────────────────
echo ""
echo "[6/6] Bucket policy ..."
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
FN_ARN=${FN_ARN}
ENV

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  ✓ Setup completo"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "DNS final que tenés que cargar en CLOUDFLARE (Proxy: DNS only / nube gris):"
echo ""
echo "  1) Apex:"
echo "     Type: CNAME"
echo "     Name: @"
echo "     Target: ${DIST_DOMAIN}"
echo "     Proxy: DNS only (gris)"
echo ""
echo "  2) www:"
echo "     Type: CNAME"
echo "     Name: www"
echo "     Target: ${DIST_DOMAIN}"
echo "     Proxy: DNS only (gris)"
echo ""
echo "(Borrá el A 'ork3d.com → 192.0.2.1' antes de crear el CNAME apex.)"
echo ""
echo "Cuando los DNS propaguen, desde la raíz del repo corré:"
echo "  ./deploy.sh"
echo "  ./verify.sh"
echo ""
