#!/usr/bin/env bash
# ORK3D · Setup AWS (one-time).
#
# Crea:
#   - Bucket S3 privado: ork3d.com
#   - OAC (Origin Access Control) para CloudFront
#   - CloudFront distribution para apex (ork3d.com)
#   - CloudFront Function que redirige www → apex
#   - Distribution secundaria para www.ork3d.com (con la function)
#   - Certificado ACM en us-east-1 para ork3d.com y www.ork3d.com
#
# Requisitos:
#   - AWS CLI v2 configurado (aws configure)
#   - jq instalado
#
# Antes de correr este script confirmá que querés crear los recursos.
# Validación ACM: hay que crear los CNAMEs que el script imprime, en Donweb.
# El script PAUSA hasta que lo confirmes.

set -euo pipefail

DOMAIN="ork3d.com"
WWW_DOMAIN="www.${DOMAIN}"
BUCKET="${DOMAIN}"
REGION="${AWS_REGION:-us-east-1}"           # Bucket en cualquier región; lo dejamos en us-east-1 por simplicidad
ACM_REGION="us-east-1"                       # Obligatorio para CloudFront

cd "$(dirname "$0")"

echo "════════════════════════════════════════════════════════════"
echo "  ORK3D · Setup de infraestructura AWS"
echo "════════════════════════════════════════════════════════════"
echo "  Dominio:     ${DOMAIN}"
echo "  www:         ${WWW_DOMAIN} → redirige a apex"
echo "  Bucket:      s3://${BUCKET}  (privado)"
echo "  Región:      ${REGION}"
echo "  ACM región:  ${ACM_REGION}  (obligatorio para CloudFront)"
echo "════════════════════════════════════════════════════════════"
read -rp "¿Continuar? (escribir 'si' para crear recursos): " ans
[[ "$ans" == "si" ]] || { echo "Abortado."; exit 1; }

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "→ Cuenta AWS: ${ACCOUNT_ID}"

# ────────────────────────────────────────────────────────────
# 1) Bucket S3 privado
# ────────────────────────────────────────────────────────────
echo ""
echo "[1/7] Creando bucket S3 privado ${BUCKET} ..."
if aws s3api head-bucket --bucket "${BUCKET}" 2>/dev/null; then
  echo "    ya existe, sigo."
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
# 2) Certificado ACM (us-east-1)
# ────────────────────────────────────────────────────────────
echo ""
echo "[2/7] Solicitando certificado ACM para ${DOMAIN} y ${WWW_DOMAIN} ..."
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
echo "    >>> CREÁ ESTOS REGISTROS CNAME EN DONWEB <<<"
aws acm describe-certificate --region "${ACM_REGION}" --certificate-arn "${CERT_ARN}" \
  --query 'Certificate.DomainValidationOptions[].ResourceRecord.[Name,Type,Value]' \
  --output table

read -rp "¿Ya creaste los CNAMEs de validación en Donweb y querés esperar a que valide? (si/skip): " v
if [[ "${v}" == "si" ]]; then
  echo "    Esperando validación (puede tardar 5-30 min) ..."
  aws acm wait certificate-validated --region "${ACM_REGION}" --certificate-arn "${CERT_ARN}"
  echo "    ✓ Certificado validado."
fi

# ────────────────────────────────────────────────────────────
# 3) Origin Access Control (OAC)
# ────────────────────────────────────────────────────────────
echo ""
echo "[3/7] Creando Origin Access Control ..."
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
echo "[4/7] Publicando CloudFront Function 'ork3d-redirect-www' ..."
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
FN_ARN=$(aws cloudfront describe-function --name "${FN_NAME}" --stage LIVE --query 'FunctionSummary.FunctionMetadata.FunctionARN' --output text)
echo "    Function ARN: ${FN_ARN}"

# ────────────────────────────────────────────────────────────
# 5) Distribution APEX (ork3d.com)
# ────────────────────────────────────────────────────────────
echo ""
echo "[5/7] Creando CloudFront distribution para ${DOMAIN} ..."
APEX_CONFIG=$(mktemp)
cat > "${APEX_CONFIG}" <<JSON
{
  "CallerReference": "ork3d-apex-$(date +%s)",
  "Comment": "ORK3D apex (ork3d.com)",
  "Enabled": true,
  "Aliases": { "Quantity": 1, "Items": ["${DOMAIN}"] },
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

DIST_JSON=$(aws cloudfront create-distribution --distribution-config "file://${APEX_CONFIG}")
DIST_ID=$(echo "${DIST_JSON}" | jq -r '.Distribution.Id')
DIST_DOMAIN=$(echo "${DIST_JSON}" | jq -r '.Distribution.DomainName')
rm -f "${APEX_CONFIG}"
echo "    Distribution ID:    ${DIST_ID}"
echo "    Distribution domain: ${DIST_DOMAIN}"

# ────────────────────────────────────────────────────────────
# 6) Distribution WWW (con redirect function)
# ────────────────────────────────────────────────────────────
echo ""
echo "[6/7] Creando CloudFront distribution para ${WWW_DOMAIN} (redirect a apex) ..."
WWW_CONFIG=$(mktemp)
cat > "${WWW_CONFIG}" <<JSON
{
  "CallerReference": "ork3d-www-$(date +%s)",
  "Comment": "ORK3D www → apex redirect",
  "Enabled": true,
  "Aliases": { "Quantity": 1, "Items": ["${WWW_DOMAIN}"] },
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
    "FunctionAssociations": {
      "Quantity": 1,
      "Items": [{ "FunctionARN": "${FN_ARN}", "EventType": "viewer-request" }]
    }
  },
  "ViewerCertificate": {
    "ACMCertificateArn": "${CERT_ARN}",
    "SSLSupportMethod": "sni-only",
    "MinimumProtocolVersion": "TLSv1.2_2021"
  }
}
JSON

WWW_DIST_JSON=$(aws cloudfront create-distribution --distribution-config "file://${WWW_CONFIG}")
WWW_DIST_ID=$(echo "${WWW_DIST_JSON}" | jq -r '.Distribution.Id')
WWW_DIST_DOMAIN=$(echo "${WWW_DIST_JSON}" | jq -r '.Distribution.DomainName')
rm -f "${WWW_CONFIG}"
echo "    WWW Distribution ID:    ${WWW_DIST_ID}"
echo "    WWW Distribution domain: ${WWW_DIST_DOMAIN}"

# ────────────────────────────────────────────────────────────
# 7) Bucket policy (sólo CloudFront vía OAC)
# ────────────────────────────────────────────────────────────
echo ""
echo "[7/7] Aplicando bucket policy ..."
POL=$(mktemp)
sed -e "s|__BUCKET__|${BUCKET}|g" \
    -e "s|__ACCOUNT_ID__|${ACCOUNT_ID}|g" \
    -e "s|__DISTRIBUTION_ID__|${DIST_ID}|g" \
    bucket-policy.template.json > "${POL}"

# Permitir también la distribution de www
jq --arg arn2 "arn:aws:cloudfront::${ACCOUNT_ID}:distribution/${WWW_DIST_ID}" '
  .Statement[0].Condition.StringEquals."AWS:SourceArn" |= [.,$arn2]
' "${POL}" > "${POL}.2" && mv "${POL}.2" "${POL}"

aws s3api put-bucket-policy --bucket "${BUCKET}" --policy "file://${POL}"
rm -f "${POL}"

# ────────────────────────────────────────────────────────────
# Guardar variables
# ────────────────────────────────────────────────────────────
cat > .env <<ENV
BUCKET=${BUCKET}
DISTRIBUTION_ID=${DIST_ID}
WWW_DISTRIBUTION_ID=${WWW_DIST_ID}
DIST_DOMAIN=${DIST_DOMAIN}
WWW_DIST_DOMAIN=${WWW_DIST_DOMAIN}
CERT_ARN=${CERT_ARN}
OAC_ID=${OAC_ID}
ENV

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  ✓ Setup completo"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "DNS records que tenés que crear en Donweb:"
echo ""
echo "  Tipo CNAME    Nombre: ork3d.com         Valor: ${DIST_DOMAIN}"
echo "    (si Donweb no permite CNAME en apex, usar ALIAS o ANAME)"
echo "    (alternativa: redireccionar el A record desde el panel)"
echo ""
echo "  Tipo CNAME    Nombre: www.ork3d.com     Valor: ${WWW_DIST_DOMAIN}"
echo ""
echo "Más los CNAMEs de validación ACM que ya viste arriba."
echo ""
echo "Cuando los DNS estén propagados, corré:"
echo "  ./deploy.sh"
echo "  ./verify.sh ork3d.com"
echo ""
