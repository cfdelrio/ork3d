#!/usr/bin/env bash
# ORK3D · Reseñas V2 · panel admin: plan de infraestructura en DRY_RUN.
#
# Esta etapa NO ejecuta AWS. Con DRY_RUN=1 (valor por defecto) el script solo imprime los comandos
# que correría. No llama a aws, curl ni a ninguna herramienta de red. DRY_RUN=0 queda bloqueado
# hasta una autorización explícita (docs/resenas/V2-ADMIN.md §14).
#
# Variables (de infra/.env o del entorno):
#   RESENAS_BUCKET            bucket privado de reseñas (lo creó la V1)
#   BUCKET                    bucket del sitio (ork3d.com)
#   DISTRIBUTION_ID           distribución de CloudFront del sitio
#   RESENAS_ADMIN_EMAIL       destinatario fijo de moderación. No se hardcodea.
#   RESENAS_FROM_EMAIL        remitente SES. No se hardcodea.
#   RESENAS_ADMIN_BASE_URL    base pública (por defecto https://ork3d.com)
#   ALLOWED_ORIGIN            origen exacto permitido (por defecto https://ork3d.com)
#   AWS_REGION                región (por defecto us-east-1)
#   DRY_RUN                   1 por defecto. 0 queda bloqueado en esta etapa.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f infra/.env ]]; then
  # shellcheck disable=SC1091
  source infra/.env
fi

DRY_RUN="${DRY_RUN:-1}"
if [[ "$DRY_RUN" != "1" ]]; then
  echo "DRY_RUN=${DRY_RUN}: este script no ejecuta AWS en esta etapa." >&2
  echo "El modo real requiere una autorización explícita (docs/resenas/V2-ADMIN.md §14)." >&2
  exit 2
fi

fallo() { echo "ERROR: $*" >&2; exit 1; }

REGION="${AWS_REGION:-us-east-1}"
RESENAS_BUCKET="${RESENAS_BUCKET:-}"
SITE_BUCKET="${BUCKET:-}"
DISTRIBUTION_ID="${DISTRIBUTION_ID:-}"
ADMIN_EMAIL="${RESENAS_ADMIN_EMAIL:-}"
FROM_EMAIL="${RESENAS_FROM_EMAIL:-}"
BASE_URL="${RESENAS_ADMIN_BASE_URL:-https://ork3d.com}"
ORIGEN="${ALLOWED_ORIGIN:-https://ork3d.com}"
ACCOUNT_ID="<ACCOUNT_ID>"   # no se consulta en dry-run: no hay llamadas a STS
FN="ork3d-resenas-admin"
ROL="ork3d-resenas-admin-lambda"
POLITICA="resenas-admin-privado"
LOG_GROUP="/aws/lambda/${FN}"
FN_REDIRECT="ork3d-redirect-www"

[[ -n "$RESENAS_BUCKET" ]] || fallo "falta RESENAS_BUCKET (bucket privado de reseñas, creado por la V1)."
[[ -n "$SITE_BUCKET" ]] || fallo "falta BUCKET (bucket del sitio, ork3d.com)."
[[ -n "$DISTRIBUTION_ID" ]] || fallo "falta DISTRIBUTION_ID (distribución de CloudFront del sitio)."
[[ "$DISTRIBUTION_ID" =~ ^E[A-Z0-9]{10,}$ ]] || fallo "DISTRIBUTION_ID con formato inesperado."
[[ -n "$ADMIN_EMAIL" ]] || fallo "falta RESENAS_ADMIN_EMAIL (correo de moderación). Definila en infra/.env o en el entorno. El script no inventa direcciones."
[[ -n "$FROM_EMAIL" ]] || fallo "falta RESENAS_FROM_EMAIL (remitente SES). Definila en infra/.env o en el entorno. El script no inventa direcciones."
correo_valido() { [[ "$1" =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; }
correo_valido "$ADMIN_EMAIL" || fallo "RESENAS_ADMIN_EMAIL no tiene formato de correo."
correo_valido "$FROM_EMAIL" || fallo "RESENAS_FROM_EMAIL no tiene formato de correo."
[[ "$BASE_URL" =~ ^https://[A-Za-z0-9.-]+$ ]] || fallo "RESENAS_ADMIN_BASE_URL debe ser https://<host>, sin barra final."
[[ "$ORIGEN" =~ ^https://[A-Za-z0-9.-]+$ ]] || fallo "ALLOWED_ORIGIN debe ser https://<host>, sin barra final."

enmascarar() { echo "$1" | sed -E 's/^(.)[^@]*(@.*)$/\1***\2/'; }

WORK="$(mktemp -d)"
# KEEP_WORK=1 conserva los archivos renderizados para revisarlos. No imprime sus contenidos.
if [[ "${KEEP_WORK:-0}" != "1" ]]; then
  trap 'rm -rf "$WORK"' EXIT
fi

# Renderiza una plantilla de infra/resenas/ en $WORK. No toca AWS.
render() {  # render PLANTILLA SALIDA
  sed \
    -e "s|__RESENAS_BUCKET__|${RESENAS_BUCKET}|g" \
    -e "s|__SITE_BUCKET__|${SITE_BUCKET}|g" \
    -e "s|__DISTRIBUTION_ID__|${DISTRIBUTION_ID}|g" \
    -e "s|__ACCOUNT_ID__|${ACCOUNT_ID}|g" \
    -e "s|__AWS_REGION__|${REGION}|g" \
    -e "s|__RESENAS_FROM_EMAIL__|${FROM_EMAIL}|g" \
    -e "s|__RESENAS_ADMIN_EMAIL__|${ADMIN_EMAIL}|g" \
    "infra/resenas/$1" > "$WORK/$2"
}
render admin-lambda-permisos.json admin-permisos.json

# Variables de entorno de la Lambda admin. Los valores ya están validados (sin comillas ni barras).
printf '{"Variables":{"RESENAS_BUCKET":"%s","RESENAS_SITE_BUCKET":"%s","RESENAS_DISTRIBUTION_ID":"%s","RESENAS_ADMIN_EMAIL":"%s","RESENAS_FROM_EMAIL":"%s","RESENAS_ADMIN_BASE_URL":"%s","ALLOWED_ORIGIN":"%s","RESENAS_SESION_HORAS":"8","RESENAS_LOGIN_MINUTOS":"10","RESENAS_LOGIN_LIMITE":"3","RESENAS_LOGIN_VENTANA_MIN":"15","RESENAS_TOKEN_DAYS":"60"}}\n' \
  "$RESENAS_BUCKET" "$SITE_BUCKET" "$DISTRIBUTION_ID" "$ADMIN_EMAIL" "$FROM_EMAIL" "$BASE_URL" "$ORIGEN" \
  > "$WORK/admin-env.json"

SIMULADAS=0
PLANES=0
# Comando que correría en modo real. En dry-run solo se imprime.
aws_dry() { SIMULADAS=$((SIMULADAS + 1)); echo "  [dry-run] aws $*"; }
# Paso planificado: requiere aprobación y no se ejecuta en esta etapa.
plan() { PLANES=$((PLANES + 1)); echo "  PLAN $*"; }

echo "ORK3D · Panel admin V2 · plan de infraestructura (DRY_RUN=1)"
echo "Modo: DRY_RUN=1. Este script no llama a AWS ni a ninguna herramienta de red."
echo "Región: ${REGION} · Cuenta: ${ACCOUNT_ID} (no se consulta en dry-run)"
echo "Bucket privado: ${RESENAS_BUCKET} · Bucket del sitio: ${SITE_BUCKET} · Distribución: ${DISTRIBUTION_ID}"
echo "Correo (desde variables, no hardcodeado): remitente $(enmascarar "$FROM_EMAIL") · moderación $(enmascarar "$ADMIN_EMAIL")"
echo
echo "Recursos (8 pasos). Ejecutables en dry-run: 1 a 3. PLAN (requieren aprobación): 4 a 8."
echo "  1. Rol IAM ${ROL} y política ${POLITICA}"
echo "  2. Log group ${LOG_GROUP} (retención 30 días)"
echo "  3. Lambda ${FN} (python3.12, 256 MB, timeout 10 s)"
echo "  4. Function URL con AWS_IAM"
echo "  5. OAC de CloudFront y permiso para cloudfront.amazonaws.com"
echo "  6. Behavior /admin/api/* en la distribución existente"
echo "  7. Lifecycle admin/ de 2 días en el bucket privado"
echo "  8. ${FN_REDIRECT}: reglas /admin/resenas y /admin/entrar"
echo "Costo fijo: USD 0. Costo variable: docs/resenas/RUNBOOK-DESPLIEGUE-MANUAL.md, sección 18.7."

echo
echo "▶ 1. Rol IAM y política"
aws_dry iam create-role --role-name "$ROL" --assume-role-policy-document file://infra/resenas/lambda-trust.json
aws_dry iam put-role-policy --role-name "$ROL" --policy-name "$POLITICA" --policy-document "file://$WORK/admin-permisos.json"
aws_dry iam attach-role-policy --role-name "$ROL" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
echo "  [dry-run] esperar la propagación de IAM (10 s) antes de create-function"

echo
echo "▶ 2. Log group (retención de 30 días, antes de la Lambda)"
aws_dry logs create-log-group --log-group-name "$LOG_GROUP"
aws_dry logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 30

echo
echo "▶ 3. Lambda ${FN}"
echo "  [dry-run] (cd api/resenas && zip -q -r ${WORK}/resenas-admin.zip admin_app.py nucleo -x '*/__pycache__/*')"
aws_dry lambda create-function --function-name "$FN" --runtime python3.12 --handler admin_app.handler \
  --role "arn:aws:iam::${ACCOUNT_ID}:role/${ROL}" --zip-file "fileb://${WORK}/resenas-admin.zip" \
  --timeout 10 --memory-size 256 --environment "file://${WORK}/admin-env.json"
aws_dry lambda wait function-active-v2 --function-name "$FN"
echo "  Variables de entorno (solo nombres): RESENAS_BUCKET, RESENAS_SITE_BUCKET, RESENAS_DISTRIBUTION_ID,"
echo "    RESENAS_ADMIN_EMAIL, RESENAS_FROM_EMAIL, RESENAS_ADMIN_BASE_URL, ALLOWED_ORIGIN, RESENAS_SESION_HORAS,"
echo "    RESENAS_LOGIN_MINUTOS, RESENAS_LOGIN_LIMITE, RESENAS_LOGIN_VENTANA_MIN, RESENAS_TOKEN_DAYS"
echo "  (los valores van en un archivo temporal de la ejecución y no se imprimen)"

echo
echo "▶ 4. Function URL con AWS_IAM (PLAN: depende de la OAC del paso 5)"
plan "aws lambda create-function-url-config --function-name ${FN} --auth-type AWS_IAM"

echo
echo "▶ 5. OAC de CloudFront y permiso de invocación (PLAN)"
plan "aws cloudfront create-origin-access-control --origin-access-control-config Name=ork3d-resenas-admin-oac,SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=lambda"
plan "aws lambda add-permission --function-name ${FN} --statement-id AllowCloudFrontOACAdmin --action lambda:InvokeFunctionUrl --principal cloudfront.amazonaws.com --source-arn arn:aws:cloudfront::${ACCOUNT_ID}:distribution/${DISTRIBUTION_ID}"
echo "  La sintaxis exacta se verifica en la cuenta antes del deploy."

echo
echo "▶ 6. Behavior /admin/api/* en la distribución existente (PLAN: requiere aprobación)"
plan "aws cloudfront get-distribution-config --id ${DISTRIBUTION_ID}   # guardar copia y ETag antes de cambiar"
echo "  PLAN aplicar infra/resenas/admin-cloudfront-behavior.json: origen de la Lambda admin y CacheBehaviors /admin/api/*"
plan "aws cloudfront update-distribution --id ${DISTRIBUTION_ID} --if-match <ETAG> --distribution-config file://<distribucion-modificada.json>"

echo
echo "▶ 7. Lifecycle del bucket privado (PLAN: reemplaza toda la configuración)"
plan "aws s3api put-bucket-lifecycle-configuration --bucket ${RESENAS_BUCKET} --lifecycle-configuration file://infra/resenas/bucket-lifecycle.json"
echo "  El archivo incluye también las reglas de pendientes/ y de versiones anteriores."

echo
echo "▶ 8. ${FN_REDIRECT}: reglas /admin/resenas y /admin/entrar (PLAN)"
plan "aws cloudfront update-function --name ${FN_REDIRECT} --if-match <ETAG> --function-config \"Comment='Redirect www to apex',Runtime=cloudfront-js-2.0\" --function-code fileb://infra/redirect-www.js"
plan "aws cloudfront publish-function --name ${FN_REDIRECT} --if-match <ETAG-NUEVO>"

echo
echo "Resumen: ${SIMULADAS} llamadas simuladas (dry-run), ${PLANES} comandos PLAN (no ejecutables en esta etapa), 0 llamadas reales."
echo "No se creó ningún recurso. Para el modo real hace falta una autorización explícita (V2-ADMIN.md §14)."
if [[ "${KEEP_WORK:-0}" == "1" ]]; then
  echo "KEEP_WORK=1: archivos renderizados conservados en ${WORK}"
fi
