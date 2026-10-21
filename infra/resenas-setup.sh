#!/usr/bin/env bash
# ORK3D · Reseñas: crea o actualiza la infraestructura de AWS del sistema de reseñas.
#
# Crea (idempotente):
#   - Bucket privado (RESENAS_BUCKET): tokens, claims, reseñas y fotos pendientes.
#     Sin acceso público, versionado, cifrado, pendientes/ vence a los 2 días.
#   - Rol de la Lambda con permisos mínimos por prefijo, más s3:ListBucket sobre el bucket
#     (sin ListBucket, S3 responde AccessDenied a claves inexistentes en vez de NotFound).
#   - Log group /aws/lambda/<FUNCTION> con retención de 30 días, creado ANTES de la Lambda.
#   - Lambda (Python 3.12, api/resenas/app.py) con Function URL AuthType NONE, sin CORS propio.
#     La Function URL necesita DOS permisos: lambda:InvokeFunctionUrl (con AuthType NONE) y
#     lambda:InvokeFunction con lambda:InvokedViaFunctionUrl = true. Sin el segundo responde 403.
#   - Concurrencia reservada: OPCIONAL. Solo se configura si la cuenta tiene margen.
#   - CORS del bucket para la subida firmada desde ALLOWED_ORIGIN.
#   - Regla /opinar/* en la CloudFront Function ork3d-redirect-www (infra/redirect-www.js).
#
# Uso (desde la raíz del repo, con AWS CLI v2 y un profile con permisos de administrador):
#   export AWS_PROFILE=ork3d                # profile de la cuenta ORK3D; nunca access keys en el repo
#   DRY_RUN=1 infra/resenas-setup.sh        # valor por defecto: imprime los comandos, no llama a AWS
#   DRY_RUN=0 infra/resenas-setup.sh        # ejecuta, después de chequeos de solo lectura y de escribir CREAR.
#                                           # Es idempotente: reutiliza lo que ya existe.
#
# Procedimiento completo, verificaciones y rollback: docs/resenas/RUNBOOK-DESPLIEGUE-MANUAL.md
#
# Variables (o en infra/.env, donde ya están BUCKET y DISTRIBUTION_ID de infra/setup.sh):
#   AWS_PROFILE                 profile de AWS CLI (ej. ork3d)
#   AWS_REGION                  us-east-1
#   RESENAS_BUCKET              opcional. Default: ork3d-resenas-privado-<ACCOUNT_ID>
#   RESENAS_FUNCTION            ork3d-resenas
#   RESENAS_ROLE                ork3d-resenas-lambda
#   ALLOWED_ORIGIN              https://ork3d.com
#   GOOGLE_REVIEW_URL           opcional: enlace a la reseña en Google
#   RESENAS_LAMBDA_CONCURRENCY  5 (solo si la cuenta tiene margen: runbook, sección 3.2)
#   IAM_PROPAGATION_SECONDS     10 (espera tras crear el rol; las pruebas la ponen en 0)
#   BUCKET                      bucket del sitio (ork3d.com)
#   DISTRIBUTION_ID             ID de la distribución de CloudFront del sitio
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f infra/.env ]]; then
  # shellcheck disable=SC1091
  source infra/.env
fi

DRY_RUN="${DRY_RUN:-1}"
REGION="${AWS_REGION:-us-east-1}"
RESENAS_BUCKET="${RESENAS_BUCKET:-}"   # si queda vacío, se deriva del ACCOUNT_ID más abajo
FUNCTION="${RESENAS_FUNCTION:-ork3d-resenas}"
ROLE="${RESENAS_ROLE:-ork3d-resenas-lambda}"
ALLOWED_ORIGIN="${ALLOWED_ORIGIN:-https://ork3d.com}"
GOOGLE_REVIEW_URL="${GOOGLE_REVIEW_URL:-}"
CONCURRENCY="${RESENAS_LAMBDA_CONCURRENCY:-5}"
IAM_ESPERA="${IAM_PROPAGATION_SECONDS:-10}"
MIN_NO_RESERVADA=10                    # mínimo de concurrencia no reservada que exige AWS en la cuenta
LOG_GROUP="/aws/lambda/${FUNCTION}"
FN_NAME="ork3d-redirect-www"           # la misma CloudFront Function que crea infra/setup.sh

: "${BUCKET:?Falta BUCKET (infra/.env o entorno). Lo crea infra/setup.sh.}"
: "${DISTRIBUTION_ID:?Falta DISTRIBUTION_ID. Ver docs/resenas/RUNBOOK-DESPLIEGUE-MANUAL.md, sección 4.}"

for herramienta in jq zip; do
  command -v "$herramienta" >/dev/null || { echo "Falta ${herramienta}."; exit 1; }
done
if [[ "$DRY_RUN" == "0" ]]; then
  command -v aws >/dev/null || { echo "Falta AWS CLI v2."; exit 1; }
  case "$(aws --version 2>&1)" in
    aws-cli/2*) ;;
    *) echo "Se requiere AWS CLI v2 (aws --version debe empezar con aws-cli/2)."; exit 1 ;;
  esac
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log() { printf '\n▶ %s\n' "$*"; }

# Ejecuta un comando de AWS, o solo lo imprime si DRY_RUN=1.
aws_run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '  [dry-run] aws %s\n' "$*"
    return 0
  fi
  printf '  $ aws %s\n' "$*"
  aws "$@"
}

# Chequeo de existencia. En dry-run no se consulta AWS y se asume que no existe.
existe() {
  [[ "$DRY_RUN" == "0" ]] && "$@" >/dev/null 2>&1
}

# Reemplaza los placeholders de una plantilla de infra/resenas/ y deja el resultado en $WORK.
render() {
  if [[ "$1" == "lambda-permisos.json" && ( -z "${RESENAS_FROM_EMAIL:-}" || -z "${RESENAS_ADMIN_EMAIL:-}" ) ]]; then
    echo "  ⚠ RESENAS_FROM_EMAIL o RESENAS_ADMIN_EMAIL vacíos: la política de la Lambda pública queda sin correo (requerido desde la V2)."
  fi
  sed \
    -e "s|__RESENAS_BUCKET__|${RESENAS_BUCKET}|g" \
    -e "s|__SITE_BUCKET__|${BUCKET}|g" \
    -e "s|__ALLOWED_ORIGIN__|${ALLOWED_ORIGIN}|g" \
    -e "s|__ACCOUNT_ID__|${ACCOUNT_ID}|g" \
    -e "s|__DISTRIBUTION_ID__|${DISTRIBUTION_ID}|g" \
    -e "s|__AWS_REGION__|${REGION}|g" \
    -e "s|__RESENAS_FROM_EMAIL__|${RESENAS_FROM_EMAIL:-}|g" \
    -e "s|__RESENAS_ADMIN_EMAIL__|${RESENAS_ADMIN_EMAIL:-}|g" \
    "infra/resenas/$1" > "$WORK/$1"
}

if [[ "$DRY_RUN" == "1" ]]; then
  ACCOUNT_ID="<ACCOUNT_ID>"
  RESENAS_BUCKET="${RESENAS_BUCKET:-ork3d-resenas-privado-<ACCOUNT_ID>}"
  echo "DRY_RUN=1: no se llama a AWS. Se imprimen los comandos que correría DRY_RUN=0."
else
  ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
  echo "Cuenta AWS: ${ACCOUNT_ID} · región: ${REGION} · profile: ${AWS_PROFILE:-(default)}"
  RESENAS_BUCKET="${RESENAS_BUCKET:-ork3d-resenas-privado-${ACCOUNT_ID}}"
  if [[ ! "$RESENAS_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]; then
    echo "RESENAS_BUCKET inválido: '${RESENAS_BUCKET}'. Usá minúsculas, números, puntos y guiones (3 a 63 caracteres)."
    exit 1
  fi
fi

# Lista exacta de lo que se crea o actualiza, y su costo. Se imprime en dry-run y en modo real.
resumen_recursos() {
  cat <<EOF
Recursos que este script crea o actualiza (región ${REGION}, cuenta ${ACCOUNT_ID}):
  1. S3 bucket privado       ${RESENAS_BUCKET}: sin acceso público, versionado, cifrado SSE-S3,
                              pendientes/ vence a los 2 días, versiones anteriores a los 30 días,
                              CORS solo POST desde ${ALLOWED_ORIGIN}, política que niega HTTP sin TLS.
  2. Rol IAM                 ${ROLE}: lee tokens/, claims/, resenas/, pendientes/ y lista solo ese bucket
                              (ListBucket); escribe claims/, resenas/, pendientes/; borra claims/ (rollback).
  3. Log group               ${LOG_GROUP}: se crea antes de la Lambda, retención de 30 días.
  4. Lambda                  ${FUNCTION}: python3.12, 256 MB, timeout 10 s.
                              Concurrencia reservada ${CONCURRENCY}: opcional, solo si la cuenta tiene margen.
  5. Function URL            AuthType NONE, sin CORS propio (la Lambda responde CORS y el preflight).
  6. Permisos de invocación  FunctionURLAllowPublicAccess (lambda:InvokeFunctionUrl, AuthType NONE) y
                              FunctionURLAllowInvokeViaUrl (lambda:InvokeFunction, InvokedViaFunctionUrl = true).
  7. CloudFront Function     ${FN_NAME}: nueva versión publicada con la regla /opinar/*.
Costo fijo: USD 0 al mes (no hay servidores ni recursos encendidos todo el tiempo).
Costo variable potencial: menos de USD 0,30 al mes con 10 a 1.000 reseñas al mes en el primer año
(Lambda, CloudFront y CloudWatch quedan en sus capas gratuitas; S3 cobra centavos). El costo de S3 crece
si el export relee todas las reseñas: ver docs/resenas/README.md, sección "Costos".
EOF
}
resumen_recursos
echo

# Chequeos previos en modo real: solo lectura. Cortan antes de cambiar nada si algo no coincide.
if [[ "$DRY_RUN" == "0" ]]; then
  log "Chequeos previos (solo lectura)"
  if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    echo "ERROR: no se pudo acceder al bucket del sitio '${BUCKET}'. Sin cambios."
    exit 1
  fi
  echo "  bucket del sitio ${BUCKET}: OK"

  if [[ ! "$DISTRIBUTION_ID" =~ ^E[A-Z0-9]{10,}$ ]]; then
    echo "ERROR: DISTRIBUTION_ID con formato inesperado. Sin cambios."
    exit 1
  fi
  aliases="$(aws cloudfront get-distribution-config --id "$DISTRIBUTION_ID" \
    --query 'DistributionConfig.Aliases.Items' --output text 2>/dev/null || true)"
  aliases_sp="${aliases//$'\t'/ }"
  case " ${aliases_sp} " in
    *" ork3d.com "*) echo "  distribución ${DISTRIBUTION_ID}: alias ork3d.com OK" ;;
    *) echo "ERROR: la distribución ${DISTRIBUTION_ID} no tiene ork3d.com como alias. Sin cambios."; exit 1 ;;
  esac

  fn_existente="$(aws cloudfront describe-function --name "$FN_NAME" --stage LIVE \
    --query 'FunctionSummary.Name' --output text 2>/dev/null || true)"
  if [[ "$fn_existente" != "$FN_NAME" ]]; then
    echo "ERROR: no existe la CloudFront Function '${FN_NAME}' en LIVE. Sin cambios."
    exit 1
  fi
  echo "  CloudFront Function ${FN_NAME} en LIVE: OK"

  ayuda_permiso="$(AWS_PAGER="" aws lambda add-permission help 2>/dev/null || true)"
  case "$ayuda_permiso" in
    *invoked-via-function-url*) echo "  AWS CLI soporta --invoked-via-function-url: OK" ;;
    *) echo "ERROR: esta AWS CLI no soporta --invoked-via-function-url. Actualizá AWS CLI v2. Sin cambios."; exit 1 ;;
  esac

  echo
  echo "Esto CREA o MODIFICA recursos en la cuenta ${ACCOUNT_ID} (región ${REGION}):"
  echo "  - bucket privado ${RESENAS_BUCKET}"
  echo "  - rol ${ROLE}, log group ${LOG_GROUP}, Lambda ${FUNCTION}, Function URL y sus permisos"
  echo "  - concurrencia reservada solo si la cuenta tiene margen"
  echo "  - CloudFront Function ${FN_NAME} (nueva versión publicada en LIVE)"
  echo "No borra nada. Si no revisaste el dry-run, cancelá."
  read -r -p "Escribí CREAR para continuar (cualquier otra cosa cancela sin tocar AWS): " confirmacion || true
  if [[ "${confirmacion:-}" != "CREAR" ]]; then
    echo "Cancelado. No se modificó AWS."
    exit 1
  fi
fi

# ── 1. Bucket privado ──────────────────────────────────────────────────────
log "Bucket privado s3://${RESENAS_BUCKET}"
if ! existe aws s3api head-bucket --bucket "$RESENAS_BUCKET"; then
  if [[ "$REGION" == "us-east-1" ]]; then
    aws_run s3api create-bucket --bucket "$RESENAS_BUCKET" --region "$REGION"
  else
    aws_run s3api create-bucket --bucket "$RESENAS_BUCKET" --region "$REGION" \
      --create-bucket-configuration "LocationConstraint=${REGION}"
  fi
fi
aws_run s3api put-public-access-block --bucket "$RESENAS_BUCKET" \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
aws_run s3api put-bucket-versioning --bucket "$RESENAS_BUCKET" \
  --versioning-configuration Status=Enabled
aws_run s3api put-bucket-encryption --bucket "$RESENAS_BUCKET" \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
render bucket-lifecycle.json
aws_run s3api put-bucket-lifecycle-configuration --bucket "$RESENAS_BUCKET" \
  --lifecycle-configuration "file://${WORK}/bucket-lifecycle.json"
render bucket-cors.json
aws_run s3api put-bucket-cors --bucket "$RESENAS_BUCKET" \
  --cors-configuration "file://${WORK}/bucket-cors.json"
render bucket-policy.json
aws_run s3api put-bucket-policy --bucket "$RESENAS_BUCKET" \
  --policy "file://${WORK}/bucket-policy.json"

# ── 2. Rol de la Lambda (mínimo: solo los prefijos que usa app.py, más ListBucket del bucket) ──
log "Rol ${ROLE}"
render lambda-trust.json
render lambda-permisos.json
if ! existe aws iam get-role --role-name "$ROLE"; then
  aws_run iam create-role --role-name "$ROLE" \
    --assume-role-policy-document "file://${WORK}/lambda-trust.json"
fi
aws_run iam put-role-policy --role-name "$ROLE" --policy-name resenas-bucket-privado \
  --policy-document "file://${WORK}/lambda-permisos.json"
aws_run iam attach-role-policy --role-name "$ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE}"
if [[ "$DRY_RUN" == "0" ]]; then
  ROLE_ARN="$(aws iam get-role --role-name "$ROLE" --query 'Role.Arn' --output text)"
  echo "  esperando la propagación de IAM (${IAM_ESPERA} s)…"
  sleep "$IAM_ESPERA"
fi

# ── 3. Log group de la Lambda (antes de la Lambda: existe desde la primera invocación) ──
log "Log group ${LOG_GROUP} (retención 30 días)"
if [[ "$DRY_RUN" == "1" ]]; then
  aws_run logs create-log-group --log-group-name "$LOG_GROUP"
else
  grupo_existente="$(aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" \
    --query "logGroups[?logGroupName=='${LOG_GROUP}'].logGroupName" --output text 2>/dev/null || true)"
  if [[ "$grupo_existente" == "$LOG_GROUP" ]]; then
    echo "  el log group ya existe: no se recrea"
  else
    aws_run logs create-log-group --log-group-name "$LOG_GROUP"
  fi
fi
aws_run logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 30

# ── 4. Lambda ──────────────────────────────────────────────────────────────
log "Lambda ${FUNCTION}"
(cd api/resenas && zip -q -j "${WORK}/resenas.zip" app.py)
# El núcleo V2 (tokens, correo, export, sesiones) va con la Lambda pública: app.py lo importa.
# Sin el núcleo la Lambda no arranca, así que su ausencia detiene el script antes de tocar AWS.
if [[ ! -d api/resenas/nucleo ]]; then
  echo "  ✗ falta api/resenas/nucleo/: la Lambda pública no arranca sin el núcleo V2." >&2
  exit 1
fi
(cd api/resenas && zip -q -r "${WORK}/resenas.zip" nucleo -x '*/__pycache__/*')
jq -n --arg b "$RESENAS_BUCKET" --arg o "$ALLOWED_ORIGIN" --arg g "$GOOGLE_REVIEW_URL" \
  '{Variables: {RESENAS_BUCKET: $b, ALLOWED_ORIGIN: $o, GOOGLE_REVIEW_URL: $g}}' > "${WORK}/env.json"
if ! existe aws lambda get-function --function-name "$FUNCTION"; then
  aws_run lambda create-function --function-name "$FUNCTION" \
    --description "ORK3D reviews API: tokens, photos and pending reviews" \
    --runtime python3.12 --handler app.handler --role "$ROLE_ARN" \
    --zip-file "fileb://${WORK}/resenas.zip" \
    --timeout 10 --memory-size 256 \
    --environment "file://${WORK}/env.json"
  aws_run lambda wait function-active-v2 --function-name "$FUNCTION"
else
  aws_run lambda update-function-code --function-name "$FUNCTION" \
    --zip-file "fileb://${WORK}/resenas.zip"
  aws_run lambda wait function-updated-v2 --function-name "$FUNCTION"
  aws_run lambda update-function-configuration --function-name "$FUNCTION" \
    --timeout 10 --memory-size 256 --environment "file://${WORK}/env.json"
  aws_run lambda wait function-updated-v2 --function-name "$FUNCTION"
fi

# ── 5. Concurrencia reservada (OPCIONAL) ───────────────────────────────────
# No es requisito funcional. Solo se reserva si queda margen por encima del mínimo que exige AWS
# (MIN_NO_RESERVADA) para la concurrencia no reservada. No se toca la concurrencia de otras funciones.
configurar_concurrencia() {
  log "Concurrencia reservada (opcional: solo con margen en la cuenta)"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [dry-run] consultar aws lambda get-account-settings; si hay margen, put-function-concurrency ${CONCURRENCY}"
    return 0
  fi
  local cuenta no_reservada reservada_actual disponible
  cuenta="$(aws lambda get-account-settings --query 'AccountLimit' --output json 2>/dev/null || echo '{}')"
  no_reservada="$(jq -r '.UnreservedConcurrentExecutions // 0' <<<"$cuenta")"
  reservada_actual="$(aws lambda get-function-concurrency --function-name "$FUNCTION" \
    --query 'ReservedConcurrentExecutions' --output text 2>/dev/null || echo 0)"
  [[ "$reservada_actual" =~ ^[0-9]+$ ]] || reservada_actual=0
  disponible=$(( no_reservada + reservada_actual ))
  if (( disponible - CONCURRENCY >= MIN_NO_RESERVADA )); then
    aws_run lambda put-function-concurrency --function-name "$FUNCTION" \
      --reserved-concurrent-executions "$CONCURRENCY"
  else
    echo "  Reserved concurrency omitida: límite de cuenta insuficiente."
    echo "  (concurrencia no reservada disponible: ${disponible}; AWS exige al menos ${MIN_NO_RESERVADA} sin reservar)"
  fi
}
configurar_concurrencia

# ── 6. Function URL pública y sus dos permisos de invocación ───────────────
# AuthType NONE necesita DOS permisos: InvokeFunctionUrl (con AuthType NONE) e InvokeFunction con
# lambda:InvokedViaFunctionUrl = true. Sin el segundo, la URL responde 403. Ambos se detectan por el
# contenido de la política (no por Sid), así que el paso es idempotente aunque el permiso exista con otro nombre.
log "Function URL (AuthType NONE, sin CORS propio) y permisos de invocación"
if ! existe aws lambda get-function-url-config --function-name "$FUNCTION"; then
  aws_run lambda create-function-url-config --function-name "$FUNCTION" --auth-type NONE
fi
politica='{}'
if [[ "$DRY_RUN" == "0" ]]; then
  politica="$(aws lambda get-policy --function-name "$FUNCTION" --query Policy --output text 2>/dev/null || echo '{}')"
fi
tiene_permiso_url() {
  jq -e 'any(.Statement[]?; .Action == "lambda:InvokeFunctionUrl" and ((.Condition.StringEquals["lambda:FunctionUrlAuthType"] // "") == "NONE"))' \
    <<<"$politica" >/dev/null 2>&1
}
tiene_permiso_invocacion() {
  jq -e 'any(.Statement[]?; .Action == "lambda:InvokeFunction" and (((.Condition.Bool["lambda:InvokedViaFunctionUrl"] // "") | tostring) == "true"))' \
    <<<"$politica" >/dev/null 2>&1
}
if tiene_permiso_url; then
  echo "  lambda:InvokeFunctionUrl con AuthType NONE: ya existe"
else
  aws_run lambda add-permission --function-name "$FUNCTION" \
    --statement-id FunctionURLAllowPublicAccess \
    --action lambda:InvokeFunctionUrl --principal "*" \
    --function-url-auth-type NONE
fi
if tiene_permiso_invocacion; then
  echo "  lambda:InvokeFunction con InvokedViaFunctionUrl = true: ya existe"
else
  aws_run lambda add-permission --function-name "$FUNCTION" \
    --statement-id FunctionURLAllowInvokeViaUrl \
    --action lambda:InvokeFunction --principal "*" \
    --invoked-via-function-url
fi
API_URL="https://<ID>.lambda-url.${REGION}.on.aws"
if [[ "$DRY_RUN" == "0" ]]; then
  API_URL="$(aws lambda get-function-url-config --function-name "$FUNCTION" --query FunctionUrl --output text)"
  API_URL="${API_URL%/}"
fi

# ── 7. CloudFront: regla /opinar/* ─────────────────────────────────────────
log "CloudFront Function ${FN_NAME}: regla /opinar/* (infra/redirect-www.js)"
ETAG="<ETAG>"
if [[ "$DRY_RUN" == "0" ]]; then
  ETAG="$(aws cloudfront describe-function --name "$FN_NAME" --query 'ETag' --output text)"
fi
aws_run cloudfront update-function --name "$FN_NAME" --if-match "$ETAG" \
  --function-config "Comment='Redirect www to apex',Runtime=cloudfront-js-2.0" \
  --function-code fileb://infra/redirect-www.js
if [[ "$DRY_RUN" == "0" ]]; then
  ETAG="$(aws cloudfront describe-function --name "$FN_NAME" --query 'ETag' --output text)"
fi
aws_run cloudfront publish-function --name "$FN_NAME" --if-match "$ETAG"

# ── 8. Verificación de la API (solo en modo real) ──────────────────────────
# Comportamiento esperado, validado en producción: GET de token inexistente → 200 {"ok": false, "motivo": "invalido"};
# OPTIONS /resenas → 204 con Access-Control-Allow-Origin del origen permitido.
if [[ "$DRY_RUN" == "0" ]]; then
  log "Verificación de la API"
  token_falso="$(printf 'A%.0s' $(seq 1 32))"
  cuerpo="$WORK/respuesta.json"
  codigo="$(curl -s -o "$cuerpo" -w '%{http_code}' --max-time 15 "${API_URL}/tokens/${token_falso}" || true)"
  [[ -n "$codigo" ]] || codigo="000"  # sin respuesta: curl ya imprime 000 con -w
  cuerpo_txt="$(cat "$cuerpo" 2>/dev/null || true)"
  echo "  GET /tokens/<falso> → HTTP ${codigo} ${cuerpo_txt}"
  if [[ "$codigo" == "200" ]] && { [[ "$cuerpo_txt" == *'"motivo": "invalido"'* ]] || [[ "$cuerpo_txt" == *'"motivo":"invalido"'* ]]; }; then
    echo "  ✓ La API responde como se espera (token inexistente: 200 invalido)."
  else
    echo "  ⚠ Respuesta inesperada. Si es 403 o Forbidden, revisá los dos permisos de la Function URL (runbook, sección 11)."
  fi
  cabeceras="$WORK/preflight.txt"
  codigo_opt="$(curl -s -o /dev/null -D "$cabeceras" -w '%{http_code}' -X OPTIONS --max-time 15 \
    -H "Origin: ${ALLOWED_ORIGIN}" -H "Access-Control-Request-Method: POST" "${API_URL}/resenas" || true)"
  [[ -n "$codigo_opt" ]] || codigo_opt="000"
  acao="$(tr -d '\r' < "$cabeceras" 2>/dev/null | grep -i '^access-control-allow-origin' || true)"
  echo "  OPTIONS /resenas → HTTP ${codigo_opt} ${acao:-sin cabecera CORS}"
fi

# ── Resumen ────────────────────────────────────────────────────────────────
log "Listo. Próximos pasos"
echo "  1. Function URL: ${API_URL}"
echo "     Guardala. Se pega en config.js (RESENAS_API_URL, sin barra final) recién cuando se autorice el deploy del frontend."
echo "  2. Moderador: asignale infra/resenas/admin-permisos.json (reemplazá __RESENAS_BUCKET__, __SITE_BUCKET__,"
echo "     __ACCOUNT_ID__ y __DISTRIBUTION_ID__). En admin/.env: RESENAS_BUCKET=${RESENAS_BUCKET}"
echo "  3. Verificación completa: docs/resenas/RUNBOOK-DESPLIEGUE-MANUAL.md, sección 10."
if [[ "$DRY_RUN" == "1" ]]; then
  echo "  (Nada se creó: era DRY_RUN=1. Para ejecutar: DRY_RUN=0 infra/resenas-setup.sh)"
fi
