#!/usr/bin/env bash
# ORK3D · V2 · despliegue controlado de la API admin (sin frontend). Se ejecuta desde EC2.
#
# Crea o verifica, en este orden: copias de rollback, log group, rol IAM, Lambda admin, Function URL AWS_IAM,
# dos permisos para CloudFront (OAC), OAC, origin request policy, origin y behavior /admin/api/* en CloudFront,
# y un smoke sin efectos. Después se detiene.
#
# NO despliega: frontend admin, la Lambda pública, RESENAS_NOTIFY_ENABLED, notificaciones, /opiniones ni main.
# Recursos existentes: si coinciden exactamente se tratan como idempotentes; si difieren, no se escribe nada.
#
# Escribe en AWS SOLO con ALLOW_ADMIN_DEPLOY=1. Sin esa variable sale con 3 antes de llamar a AWS.
# Variables obligatorias (del entorno, nunca del repo): RESENAS_FROM_EMAIL y RESENAS_ADMIN_EMAIL. Solo en una
# actualización de código (ALLOW_ADMIN_CODE_UPDATE=1) pueden faltar: se toman de la Lambda existente, sin imprimirlas.
# AWS_PROFILE (default) y AWS_REGION (us-east-1): solo esos valores. RESENAS_BUCKET se lee de la Lambda pública.
# ADMIN_DEPLOY_ESPERA: segundos entre reintentos y sondeos (30 por defecto).
# ADMIN_DEPLOY_RUTA: carpeta de rollback (/tmp/ork3d-v2-deploy por defecto; solo para pruebas).
#
# Códigos: 0 completo · 1 precheck o paso falló · 2 variable inválida · 3 falta ALLOW_ADMIN_DEPLOY=1 ·
#          4 recurso existente distinto (no se escribe nada) · 5 conflicto de patrones en CloudFront ·
#          6 el smoke no coincide con lo esperado.
set -uo pipefail

# ── Guardas: antes de cualquier llamada a AWS ─────────────────────────────────────────────
if [[ "${ALLOW_ADMIN_DEPLOY:-}" != "1" ]]; then
  echo "ABORTA: este script escribe en AWS (Lambda, IAM, CloudFront). Para ejecutarlo: ALLOW_ADMIN_DEPLOY=1 infra/admin-deploy.sh" >&2
  exit 3
fi

PERFIL="${AWS_PROFILE:-default}"
REGION="${AWS_REGION:-us-east-1}"
[[ "$PERFIL" == "default" ]] || { echo "ERROR: AWS_PROFILE solo puede ser default en este despliegue." >&2; exit 2; }
[[ "$REGION" == "us-east-1" ]] || { echo "ERROR: AWS_REGION solo puede ser us-east-1 en este despliegue." >&2; exit 2; }
export AWS_PROFILE="$PERFIL" AWS_REGION="$REGION" AWS_PAGER=""

[[ -z "${RESENAS_BUCKET:-}" ]] || { echo "ERROR: no definas RESENAS_BUCKET: se lee de la Lambda pública." >&2; exit 2; }

AWS="${AWS_CLI:-aws}"
GIT="${GIT_CMD:-git}"
CURL="${CURL_CMD:-curl}"
ESPERA="${ADMIN_DEPLOY_ESPERA:-30}"
RAMA_ESPERADA="claude/ork3d-landing-page-b2fQr"
FN_ADMIN="ork3d-resenas-admin"
FN_PUBLICA="ork3d-resenas"
FN_REDIRECT="ork3d-redirect-www"
ROL="ork3d-resenas-admin-lambda"
POLITICA_ROL="ork3d-resenas-admin-permisos"
LOG_GRUPO="/aws/lambda/ork3d-resenas-admin"
BASICA_ARN="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
DIST="ECOW0H6A48HVP"
SITIO="ork3d.com"
BASE="https://ork3d.com"
ORIGEN="resenas-admin-lambda"
PATRON="/admin/api/*"
SID_URL="ork3d-admin-cf-url"
SID_INVOCA="ork3d-admin-cf-invoke"
OAC_NOMBRE="ork3d-resenas-admin-oac"
ORP_NOMBRE="ork3d-resenas-admin-orp"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AYUDA="$ROOT/infra/admin_deploy_ayuda.py"
RUTA="${ADMIN_DEPLOY_RUTA:-/tmp/ork3d-v2-deploy}"   # override solo para pruebas
RB="$RUTA/rollback"
STAMP="$(date -u +%Y%m%dT%H%M%S%6NZ)"   # microsegundos: dos ejecuciones no comparten copias
export RESENAS_DISTRIBUTION_ID="$DIST"
T="$(mktemp -d)"
chmod 700 "$T"
trap 'rm -rf "$T"' EXIT

fallo()    { echo "ERROR: $*" >&2; exit 1; }
distinto() { echo "ERROR: $*" >&2; exit 4; }
detalle()  { python3 "$AYUDA" detalle "$1"; }   # código, operación y mensaje de la CLI, sanitizados
codigo()   { python3 -c 'import re, sys; m = re.search(r"An error occurred \((\w+)\)", open(sys.argv[1], encoding="utf-8", errors="replace").read()); print(m.group(1) if m else "desconocido")' "$1"; }
valor()    { python3 "$AYUDA" get "$1" "$2"; }
enmascarar() { python3 "$AYUDA" mascarar "$1"; }
paso()     { echo; echo "── $* ──"; }

# Correos: del entorno. Solo en una actualización de código (ALLOW_ADMIN_CODE_UPDATE=1) pueden faltar: se leen de la
# Lambda admin existente, que ya los tiene. Es una lectura; se exportan para el resto del script y nunca se imprimen.
# Sin Lambda (creación desde cero) no hay de dónde leerlos y siguen siendo obligatorios del entorno.
PATRON_CORREO='^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'
if [[ "${ALLOW_ADMIN_CODE_UPDATE:-}" == "1" && ( -z "${RESENAS_FROM_EMAIL:-}" || -z "${RESENAS_ADMIN_EMAIL:-}" ) ]]; then
  if "$AWS" lambda get-function-configuration --function-name "$FN_ADMIN" --output json > "$T/correos.json" 2> /dev/null; then
    RESENAS_FROM_EMAIL="${RESENAS_FROM_EMAIL:-$(valor "$T/correos.json" Environment.Variables.RESENAS_FROM_EMAIL)}"
    RESENAS_ADMIN_EMAIL="${RESENAS_ADMIN_EMAIL:-$(valor "$T/correos.json" Environment.Variables.RESENAS_ADMIN_EMAIL)}"
  fi
fi
for variable in RESENAS_FROM_EMAIL RESENAS_ADMIN_EMAIL; do
  dato="${!variable:-}"
  [[ -n "$dato" ]] || { echo "ERROR: falta $variable (debe venir del entorno)." >&2; exit 2; }
  [[ "$dato" =~ $PATRON_CORREO ]] || { echo "ERROR: $variable no tiene formato de correo." >&2; exit 2; }
done
export RESENAS_FROM_EMAIL RESENAS_ADMIN_EMAIL

guardar_rollback() {  # NOMBRE ARCHIVO: copia nueva con la marca de la ejecución; nunca sobrescribe
  local nombre="$1" origen="$2"
  local base="${nombre%.*}" ext="${nombre##*.}"
  local destino="$RB/$base.$STAMP.$ext"
  [[ ! -e "$destino" ]] || fallo "ya existe $destino; no se sobrescribe"
  cp "$origen" "$destino" && chmod 600 "$destino" || fallo "no se pudo guardar $nombre"
  echo "  rollback: $(basename "$destino") (600)"
}

identidad_ok() {  # CORREO ROL: EMAIL_ADDRESS verificada y habilitada para enviar
  local correo="$1" rol="$2"
  if ! "$AWS" sesv2 get-email-identity --email-identity "$correo" --output json > "$T/$rol-id.json" 2> "$T/$rol-id.err"; then
    fallo "$rol: no es una identidad SES en $AWS_REGION (código $(detalle "$T/$rol-id.err"))"
  fi
  [[ "$(valor "$T/$rol-id.json" IdentityType)" == EMAIL_ADDRESS ]] || fallo "$rol: debe ser EMAIL_ADDRESS"
  [[ "$(valor "$T/$rol-id.json" VerificationStatus)" == SUCCESS ]] || fallo "$rol: no está verificada"
  [[ "$(valor "$T/$rol-id.json" VerifiedForSendingStatus)" == true ]] || fallo "$rol: no está habilitada para enviar"
  echo "  $rol: $(enmascarar "$correo") · EMAIL_ADDRESS verificada y habilitada"
}

# Registro de la ejecución: todo lo que se imprime queda también en un archivo privado (600). CHECKPOINT 8 lo revisa.
mkdir -p "$RUTA" "$RB" && chmod 700 "$RUTA" "$RB" || { echo "ERROR: no se pudo crear $RB" >&2; exit 1; }
LOG="$RUTA/ejecucion.$STAMP.log"
touch "$LOG" && chmod 600 "$LOG" || { echo "ERROR: no se pudo crear el registro $LOG" >&2; exit 1; }
exec > >(tee -a "$LOG") 2>&1

# ── 1. Repositorio ────────────────────────────────────────────────────────────────────────
paso "1. Repositorio"
cd "$ROOT" || fallo "no se pudo entrar al repositorio"
RAMA="$($GIT rev-parse --abbrev-ref HEAD 2> /dev/null)" || fallo "no es un repositorio git"
[[ "$RAMA" == "$RAMA_ESPERADA" ]] || fallo "branch $RAMA; se esperaba $RAMA_ESPERADA"
[[ -z "$($GIT status --porcelain)" ]] || fallo "árbol no limpio (revisá git status)"
HEAD_LOCAL="$($GIT rev-parse HEAD)"
HEAD_REMOTO="$($GIT ls-remote origin "refs/heads/$RAMA_ESPERADA" 2> /dev/null | cut -f1)"
[[ -n "$HEAD_REMOTO" ]] || fallo "no se pudo leer el remoto de $RAMA_ESPERADA"
[[ "$HEAD_LOCAL" == "$HEAD_REMOTO" ]] || fallo "HEAD local ${HEAD_LOCAL:0:7} distinto del remoto ${HEAD_REMOTO:0:7}"
echo "  HEAD ${HEAD_LOCAL:0:7} · $RAMA · árbol limpio · remoto = local"

# ── 2. Identidad AWS ──────────────────────────────────────────────────────────────────────
paso "2. Identidad AWS"
"$AWS" sts get-caller-identity --output json > "$T/id.json" 2> "$T/id.err" \
  || fallo "no se pudo autenticar con el perfil $AWS_PROFILE (código $(detalle "$T/id.err"))"
CUENTA="$(valor "$T/id.json" Account)"
[[ -n "$CUENTA" ]] || fallo "sts no devolvió la cuenta"
echo "  cuenta ****${CUENTA: -4} · perfil=$AWS_PROFILE · región=$AWS_REGION"
ROL_ARN="arn:aws:iam::$CUENTA:role/$ROL"
DIST_ARN="arn:aws:cloudfront::$CUENTA:distribution/$DIST"

# ── 3. Lambda pública (solo lectura): de aquí sale RESENAS_BUCKET, en memoria ─────────────
paso "3. Lambda pública (solo lectura)"
"$AWS" lambda get-function-configuration --function-name "$FN_PUBLICA" --output json > "$T/pub.json" 2> "$T/pub.err" \
  || fallo "no se pudo leer $FN_PUBLICA (código $(detalle "$T/pub.err"))"
[[ "$(valor "$T/pub.json" State)" == Active ]] || fallo "$FN_PUBLICA no está Active"
[[ "$(valor "$T/pub.json" LastUpdateStatus)" == Successful ]] || fallo "$FN_PUBLICA no tiene LastUpdateStatus Successful"
RESENAS_BUCKET="$(python3 "$AYUDA" bucket-privado "$T/pub.json")"
[[ -n "$RESENAS_BUCKET" ]] || fallo "$FN_PUBLICA no tiene RESENAS_BUCKET"
export RESENAS_BUCKET
ROL_PUB="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("Role", "").rsplit("/", 1)[-1])' "$T/pub.json")"
echo "  $FN_PUBLICA: Active · Successful · bucket privado: $RESENAS_BUCKET · rol: $ROL_PUB"

# ── 4. Artefactos locales (sin AWS) ───────────────────────────────────────────────────────
paso "4. Artefactos locales"
python3 "$AYUDA" zip "$ROOT" "$T/admin.zip" > "$T/sha.txt" || fallo "no se pudo empaquetar la Lambda admin"
SHA="$(cat "$T/sha.txt")"
echo "  paquete: admin_app.py + nucleo/*.py · CodeSha256 ${SHA:0:12}…"
python3 "$AYUDA" politica "$ROOT/infra/resenas/admin-lambda-permisos.json" "$T/politica-admin.json" \
  "$CUENTA" "$AWS_REGION" "$DIST" "$RESENAS_BUCKET" "$SITIO" "$RESENAS_FROM_EMAIL" "$RESENAS_ADMIN_EMAIL" \
  || fallo "no se pudo renderizar la política del rol admin"
echo "  política del rol admin (mínimo privilegio, sids y acciones):"
python3 "$AYUDA" sids "$T/politica-admin.json" | sed 's/^/    /'
python3 "$AYUDA" env "$T/env.json" > /dev/null || fallo "faltan variables de entorno de la Lambda admin"
echo "  variables de la Lambda admin (nombres): $(python3 -c 'import json, sys; print(", ".join(sorted(json.load(open(sys.argv[1]))["Variables"])))' "$T/env.json")"

# ── 5. Copias de rollback (antes de cualquier escritura) ──────────────────────────────────
paso "5. Copias de rollback (solo lectura)"
mkdir -p "$RB" && chmod 700 "$RUTA" "$RB" || fallo "no se pudo crear $RB"

if "$AWS" lambda get-policy --function-name "$FN_PUBLICA" --output json > "$T/pub-policy.json" 2> "$T/pp.err"; then
  :
else
  [[ "$(codigo "$T/pp.err")" == ResourceNotFoundException ]] || fallo "no se pudo leer la política de $FN_PUBLICA"
  echo '{}' > "$T/pub-policy.json"
fi
guardar_rollback lambda-publica-policy.json "$T/pub-policy.json"

"$AWS" iam get-role --role-name "$ROL_PUB" --output json > "$T/rolpub.json" 2> "$T/rp.err" || fallo "no se pudo leer el rol $ROL_PUB"
"$AWS" iam list-attached-role-policies --role-name "$ROL_PUB" --output json > "$T/rolpub-adj.json" 2> "$T/rpa.err" \
  || fallo "no se pudo listar las políticas administradas de $ROL_PUB"
"$AWS" iam list-role-policies --role-name "$ROL_PUB" --output json > "$T/rolpub-inl.json" 2> "$T/rpi.err" \
  || fallo "no se pudo listar las políticas inline de $ROL_PUB"
: > "$T/rolpub-inl-nombres.txt"
python3 -c 'import json, sys; [print(n) for n in json.load(open(sys.argv[1])).get("PolicyNames", [])]' "$T/rolpub-inl.json" \
  > "$T/rolpub-inl-nombres.txt"
while read -r nombre; do
  [[ -n "$nombre" ]] || continue
  "$AWS" iam get-role-policy --role-name "$ROL_PUB" --policy-name "$nombre" --output json > "$T/rolpub-$nombre.json" 2> /dev/null \
    || fallo "no se pudo leer la política inline $nombre de $ROL_PUB"
done < "$T/rolpub-inl-nombres.txt"
python3 - "$T" "$ROL_PUB" > "$T/rolpub-completo.json" <<'PY' || fallo "no se pudo armar la copia del rol público"
import json, os, sys
t, rol = sys.argv[1], sys.argv[2]
nombres = [l.strip() for l in open(os.path.join(t, 'rolpub-inl-nombres.txt')) if l.strip()]
inline = [{'PolicyName': n, 'PolicyDocument': json.load(open(os.path.join(t, f'rolpub-{n}.json'))).get('PolicyDocument')}
          for n in nombres]
out = {'RoleName': rol,
       'Role': json.load(open(os.path.join(t, 'rolpub.json'))).get('Role'),
       'AttachedManagedPolicies': json.load(open(os.path.join(t, 'rolpub-adj.json'))).get('AttachedPolicies', []),
       'InlinePolicies': inline}
print(json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True))
PY
guardar_rollback iam-rol-publico.json "$T/rolpub-completo.json"

"$AWS" cloudfront get-distribution --id "$DIST" --output json > "$T/dist.json" 2> "$T/d.err" \
  || fallo "no se pudo leer la distribución $DIST (código $(detalle "$T/d.err"))"
[[ "$(valor "$T/dist.json" Distribution.Status)" == Deployed ]] || fallo "la distribución $DIST no está Deployed"
[[ ",$(valor "$T/dist.json" Distribution.DistributionConfig.Aliases.Items)," == *",$SITIO,"* ]] \
  || fallo "la distribución $DIST no tiene el alias $SITIO"
guardar_rollback cloudfront-distribucion.json "$T/dist.json"
"$AWS" cloudfront get-distribution-config --id "$DIST" --output json > "$T/dist-config.json" 2> "$T/dc.err" \
  || fallo "no se pudo leer la configuración de $DIST"
ETAG="$(valor "$T/dist-config.json" ETag)"
[[ -n "$ETAG" ]] || fallo "no se pudo leer el ETag de $DIST"
guardar_rollback cloudfront-distribucion-config.json "$T/dist-config.json"

"$AWS" cloudfront describe-function --name "$FN_REDIRECT" --stage LIVE --output json > "$T/fn-meta.json" 2> "$T/fm.err" \
  || fallo "no se pudo leer la función $FN_REDIRECT (código $(detalle "$T/fm.err"))"
guardar_rollback cloudfront-funcion-redirect-meta.json "$T/fn-meta.json"
"$AWS" cloudfront get-function --name "$FN_REDIRECT" --stage LIVE --output json "$T/fn-live.js" > /dev/null 2> "$T/fg.err" \
  || fallo "no se pudo leer el código LIVE de $FN_REDIRECT"
guardar_rollback cloudfront-funcion-redirect-LIVE.js "$T/fn-live.js"

"$AWS" s3api head-bucket --bucket "$RESENAS_BUCKET" 2> "$T/hb.err" \
  || fallo "el bucket privado no existe o no es accesible (código $(detalle "$T/hb.err"))"
if "$AWS" s3api get-bucket-lifecycle-configuration --bucket "$RESENAS_BUCKET" --output json > "$T/lc.json" 2> "$T/lc.err"; then
  :
else
  [[ "$(codigo "$T/lc.err")" == NoSuchLifecycleConfiguration ]] || fallo "no se pudo leer el lifecycle del bucket privado"
  echo '{"Rules": [], "nota": "sin lifecycle configurado"}' > "$T/lc.json"
fi
guardar_rollback s3-lifecycle-privado.json "$T/lc.json"

echo
echo "CHECKPOINT 1 — backups OK"

# ── 6. SES (solo lectura) ─────────────────────────────────────────────────────────────────
paso "6. SES (solo lectura)"
"$AWS" sesv2 get-account --output json > "$T/ses.json" 2> "$T/ses.err" || fallo "no se pudo leer la cuenta SES"
[[ "$(valor "$T/ses.json" SendingEnabled)" == true ]] || fallo "SES: el envío está deshabilitado"
SANDBOX=si
[[ "$(valor "$T/ses.json" ProductionAccessEnabled)" == true ]] && SANDBOX=no
echo "  SES: envío habilitado · sandbox=$SANDBOX"
identidad_ok "$RESENAS_FROM_EMAIL" remitente
identidad_ok "$RESENAS_ADMIN_EMAIL" destinatario

# ── 7. Recursos V2: estado actual (solo lectura) ──────────────────────────────────────────
paso "7. Recursos V2 existentes"

"$AWS" logs describe-log-groups --log-group-name-prefix "$LOG_GRUPO" --output json > "$T/logs.json" 2> "$T/lgd.err" \
  || fallo "no se pudo consultar CloudWatch Logs"
LOG_ESTADO="$(python3 -c 'import json, sys
d = json.load(open(sys.argv[1])); n = sys.argv[2]
for g in d.get("logGroups", []):
    if g.get("logGroupName") == n:
        print("si:" + str(g.get("retentionInDays", "sin")))
        break
else:
    print("no")' "$T/logs.json" "$LOG_GRUPO")"
case "$LOG_ESTADO" in
  no) LOG_EXISTE=no ;;
  si:30) LOG_EXISTE=si ;;
  *) distinto "el log group $LOG_GRUPO existe con retención ${LOG_ESTADO#si:} (se esperaba 30)" ;;
esac
echo "  log group $LOG_GRUPO: $( [[ $LOG_EXISTE == si ]] && echo 'existe, retención 30' || echo 'no existe' )"

ROL_EXISTE=no
ADJ_OK=falta
POL_EXISTE=no
if "$AWS" iam get-role --role-name "$ROL" --output json > "$T/rol.json" 2> "$T/rol.err"; then
  ROL_EXISTE=si
  python3 "$AYUDA" extraer "$T/rol.json" Role.AssumeRolePolicyDocument "$T/rol-trust.json" || fallo "rol: no se pudo leer la trust policy"
  python3 "$AYUDA" politica-coincide "$T/rol-trust.json" "$ROOT/infra/resenas/lambda-trust.json" > /dev/null \
    || distinto "el rol $ROL existe con otra trust policy"
  "$AWS" iam list-attached-role-policies --role-name "$ROL" --output json > "$T/rol-adj.json" 2> /dev/null || fallo "no se pudo listar las políticas del rol"
  ADJUNTAS="$(python3 -c 'import json, sys; print(",".join(p["PolicyArn"] for p in json.load(open(sys.argv[1])).get("AttachedPolicies", [])))' "$T/rol-adj.json")"
  case "$ADJUNTAS" in
    "$BASICA_ARN") ADJ_OK=si ;;
    "") ADJ_OK=falta ;;
    *) distinto "el rol $ROL tiene políticas administradas distintas: $ADJUNTAS" ;;
  esac
  "$AWS" iam list-role-policies --role-name "$ROL" --output json > "$T/rol-inl.json" 2> /dev/null || fallo "no se pudo listar las políticas inline del rol"
  INLINE="$(python3 -c 'import json, sys; print(",".join(json.load(open(sys.argv[1])).get("PolicyNames", [])))' "$T/rol-inl.json")"
  case "$INLINE" in
    "$POLITICA_ROL")
      "$AWS" iam get-role-policy --role-name "$ROL" --policy-name "$POLITICA_ROL" --output json > "$T/rol-pol.json" 2> /dev/null \
        || fallo "no se pudo leer la política $POLITICA_ROL"
      python3 "$AYUDA" extraer "$T/rol-pol.json" PolicyDocument "$T/rol-pol-doc.json" || fallo "política: no se pudo leer el documento"
      python3 "$AYUDA" politica-coincide "$T/rol-pol-doc.json" "$T/politica-admin.json" > /dev/null \
        || distinto "la política $POLITICA_ROL existe con otro contenido"
      POL_EXISTE=si ;;
    "") POL_EXISTE=no ;;
    *) distinto "el rol $ROL tiene políticas inline inesperadas: $INLINE" ;;
  esac
  echo "  rol $ROL: existe y coincide (trust, políticas adjuntas e inline)"
else
  [[ "$(codigo "$T/rol.err")" == NoSuchEntity ]] || fallo "no se pudo leer el rol $ROL (código $(detalle "$T/rol.err"))"
  echo "  rol $ROL: no existe"
fi

LAMBDA_EXISTE=no
CODIGO_ACTUALIZAR=no
if "$AWS" lambda get-function-configuration --function-name "$FN_ADMIN" --output json > "$T/admin.json" 2> "$T/admin.err"; then
  LAMBDA_EXISTE=si
  [[ "$ROL_EXISTE" == si ]] || distinto "la Lambda $FN_ADMIN existe sin el rol $ROL: estado inconsistente"
  python3 "$AYUDA" lambda-coincide "$T/admin.json" "$SHA" "$T/env.json" \
    || distinto "la Lambda $FN_ADMIN existe y no coincide con el repo (motivos arriba)"
  if python3 "$AYUDA" codigo-coincide "$T/admin.json" "$SHA" > /dev/null; then
    echo "  Lambda $FN_ADMIN: existe y coincide (idempotente)"
  else
    [[ "${ALLOW_ADMIN_CODE_UPDATE:-}" == "1" ]] \
      || distinto "el código de $FN_ADMIN difiere del paquete del repo: para actualizar solo el código, ALLOW_ADMIN_CODE_UPDATE=1"
    CODIGO_ACTUALIZAR=si
    echo "  Lambda $FN_ADMIN: configuración coincide; el código difiere (CodeSha256 distinto). Se actualiza solo el código."
  fi
else
  [[ "$(codigo "$T/admin.err")" == ResourceNotFoundException ]] || fallo "no se pudo leer $FN_ADMIN (código $(detalle "$T/admin.err"))"
  echo "  Lambda $FN_ADMIN: no existe"
fi

URL_EXISTE=no
PERM=ninguno
if [[ "$LAMBDA_EXISTE" == si ]]; then
  if "$AWS" lambda get-function-url-config --function-name "$FN_ADMIN" --output json > "$T/url.json" 2> "$T/url.err"; then
    [[ "$(valor "$T/url.json" AuthType)" == AWS_IAM ]] || distinto "la Function URL existe con AuthType distinto de AWS_IAM"
    URL_EXISTE=si
  else
    [[ "$(codigo "$T/url.err")" == ResourceNotFoundException ]] || fallo "no se pudo leer la Function URL"
  fi
  if "$AWS" lambda get-policy --function-name "$FN_ADMIN" --output json > "$T/admin-policy.json" 2> "$T/apol.err"; then
    :
  else
    [[ "$(codigo "$T/apol.err")" == ResourceNotFoundException ]] || fallo "no se pudo leer la resource policy de $FN_ADMIN"
    echo '{}' > "$T/admin-policy.json"
  fi
  PERM="$(python3 "$AYUDA" permisos "$T/admin-policy.json" "$DIST_ARN")" \
    || distinto "permisos de CloudFront en $FN_ADMIN: $PERM"
fi

# Código anterior de la Lambda: copia de rollback ANTES de cualquier escritura. La URL firmada (Code.Location) no se imprime.
if [[ $CODIGO_ACTUALIZAR == si ]]; then
  "$AWS" lambda get-function --function-name "$FN_ADMIN" --output json > "$T/fn-codigo.json" 2> "$T/fn-codigo.err" \
    || fallo "no se pudo leer el código actual de $FN_ADMIN (código $(detalle "$T/fn-codigo.err"))"
  "$CURL" -sS -o "$T/codigo-anterior.zip" "$(valor "$T/fn-codigo.json" Code.Location)" 2> "$T/codigo-dl.err" \
    || fallo "no se pudo descargar el código actual de $FN_ADMIN para la copia de rollback (URL firmada no mostrada)"
  [[ "$(python3 "$AYUDA" sha-archivo "$T/codigo-anterior.zip")" == "$(valor "$T/admin.json" CodeSha256)" ]] \
    || fallo "el código descargado de $FN_ADMIN no coincide con su CodeSha256: no se actualiza"
  guardar_rollback lambda-admin-codigo-anterior.zip "$T/codigo-anterior.zip"
fi

"$AWS" cloudfront list-origin-access-controls --output json > "$T/oac-list.json" 2> /dev/null || fallo "no se pudo listar los OAC"
OAC_SALIDA="$(python3 "$AYUDA" recurso-coincide oac "$T/oac-list.json" "$OAC_NOMBRE")"; OAC_RC=$?
case "$OAC_RC" in
  0) OAC_EXISTE=si; OAC_ID="$OAC_SALIDA" ;;
  4) OAC_EXISTE=no; OAC_ID="" ;;
  *) distinto "el OAC $OAC_NOMBRE existe con otra configuración" ;;
esac
echo "  OAC $OAC_NOMBRE: $( [[ $OAC_EXISTE == si ]] && echo "existe ($OAC_ID)" || echo 'no existe' )"

"$AWS" cloudfront list-origin-request-policies --type custom --output json > "$T/orp-list.json" 2> /dev/null \
  || fallo "no se pudo listar las origin request policies"
ORP_SALIDA="$(python3 "$AYUDA" recurso-coincide orp "$T/orp-list.json" "$ORP_NOMBRE")"; ORP_RC=$?
case "$ORP_RC" in
  0) ORP_EXISTE=si; ORP_ID="$ORP_SALIDA" ;;
  4) ORP_EXISTE=no; ORP_ID="" ;;
  *) distinto "la origin request policy $ORP_NOMBRE existe con otra configuración" ;;
esac
if [[ $ORP_EXISTE == si ]]; then
  echo "  origin request policy $ORP_NOMBRE: existe y coincide (ID $ORP_ID); no se vuelve a crear"
else
  echo "  origin request policy $ORP_NOMBRE: no existe"
fi

python3 "$AYUDA" dist-analizar "$T/dist-config.json" > "$T/dist-analisis.txt"
DIST_RC=$?
cat "$T/dist-analisis.txt" | sed 's/^/  /'
[[ $DIST_RC -eq 0 ]] || { echo "ERROR: conflicto de patrones en CloudFront; no se escribe nada." >&2; exit 5; }
ORIGEN_ADMIN_EXISTE="$(grep '^ORIGEN_ADMIN=' "$T/dist-analisis.txt" | cut -d= -f2)"
BEH_EXISTE="$(grep '^BEHAVIOR_ADMIN=' "$T/dist-analisis.txt" | cut -d= -f2)"

# ── 8. Plan ────────────────────────────────────────────────────────────────────────────────
paso "8. Plan (lo que se escribe)"
[[ $LOG_EXISTE == si ]] || echo "  crear log group $LOG_GRUPO (retención 30)"
[[ $ROL_EXISTE == si ]] || echo "  crear rol $ROL + AWSLambdaBasicExecutionRole + política $POLITICA_ROL"
[[ $ROL_EXISTE == si && $ADJ_OK == falta ]] && echo "  adjuntar AWSLambdaBasicExecutionRole a $ROL"
[[ $ROL_EXISTE == si && $POL_EXISTE == no ]] && echo "  agregar política $POLITICA_ROL a $ROL"
[[ $LAMBDA_EXISTE == no ]] && echo "  crear Lambda $FN_ADMIN (python3.12, admin_app.handler, 256 MB, 10 s)"
[[ $CODIGO_ACTUALIZAR == si ]] && echo "  actualizar solo el código de $FN_ADMIN (update-function-code; copia de rollback del código anterior)"
[[ $URL_EXISTE == no ]] && echo "  crear Function URL AWS_IAM en $FN_ADMIN"
[[ $PERM == ninguno ]] && echo "  agregar 2 permisos de CloudFront (InvokeFunctionUrl e InvokeFunction)"
[[ $OAC_EXISTE == no ]] && echo "  crear OAC $OAC_NOMBRE (sigv4, always, lambda)"
[[ $ORP_EXISTE == no ]] && echo "  crear origin request policy $ORP_NOMBRE"
[[ $ORP_EXISTE == si ]] && echo "  origin request policy $ORP_NOMBRE: existe y coincide (no se crea)"
if [[ $ORIGEN_ADMIN_EXISTE == no || $BEH_EXISTE == no ]]; then
  echo "  update-distribution $DIST: origen $ORIGEN y behavior $PATRON (If-Match ${ETAG:0:8}…)"
else
  echo "  distribución $DIST: origen y behavior ya existen y coinciden: sin cambios"
fi
echo "  no se toca: default behavior, origen S3, /opinar, /opiniones, Lambda pública, frontend admin"

# ── 9. Escrituras ──────────────────────────────────────────────────────────────────────────
paso "9. Escrituras"

if [[ $LOG_EXISTE == no ]]; then
  "$AWS" logs create-log-group --log-group-name "$LOG_GRUPO" 2> "$T/lg.err" || fallo "create-log-group ($(detalle "$T/lg.err"))"
  "$AWS" logs put-retention-policy --log-group-name "$LOG_GRUPO" --retention-in-days 30 2> "$T/lr.err" \
    || fallo "put-retention-policy ($(detalle "$T/lr.err"))"
  echo "  log group creado (30 días)"
fi

if [[ $ROL_EXISTE == no ]]; then
  "$AWS" iam create-role --role-name "$ROL" --assume-role-policy-document "file://$ROOT/infra/resenas/lambda-trust.json" \
    --output json > /dev/null 2> "$T/cr.err" || fallo "create-role ($(detalle "$T/cr.err"))"
  echo "  rol $ROL creado"
  ROL_NUEVO=si
fi
if [[ $ADJ_OK == falta ]]; then
  "$AWS" iam attach-role-policy --role-name "$ROL" --policy-arn "$BASICA_ARN" 2> "$T/at.err" \
    || fallo "attach-role-policy ($(detalle "$T/at.err"))"
  echo "  AWSLambdaBasicExecutionRole adjuntada"
fi
if [[ $POL_EXISTE == no ]]; then
  "$AWS" iam put-role-policy --role-name "$ROL" --policy-name "$POLITICA_ROL" \
    --policy-document "file://$T/politica-admin.json" 2> "$T/pp2.err" || fallo "put-role-policy ($(detalle "$T/pp2.err"))"
  echo "  política $POLITICA_ROL aplicada"
fi
if [[ "${ROL_NUEVO:-no}" == si ]]; then
  echo "  esperando propagación IAM (${ESPERA} s)"
  sleep "$ESPERA"
fi

echo "CHECKPOINT 2 — IAM OK"

if [[ $LAMBDA_EXISTE == no ]]; then
  intentos=0
  until "$AWS" lambda create-function --function-name "$FN_ADMIN" --runtime python3.12 --role "$ROL_ARN" \
      --handler admin_app.handler --zip-file "fileb://$T/admin.zip" --memory-size 256 --timeout 10 \
      --environment "file://$T/env.json" --output json > "$T/create.json" 2> "$T/create.err"; do
    intentos=$((intentos + 1))
    if [[ $intentos -ge 6 || "$(codigo "$T/create.err")" != InvalidParameterValueException ]]; then
      fallo "create-function ($(detalle "$T/create.err"))"
    fi
    echo "  el rol todavía no es asumible; reintento $intentos/5"
    sleep "$ESPERA"
  done
  "$AWS" lambda wait function-active-v2 --function-name "$FN_ADMIN" 2> "$T/w.err" \
    || fallo "$FN_ADMIN no llegó a Active ($(detalle "$T/w.err"))"
  "$AWS" lambda get-function-configuration --function-name "$FN_ADMIN" --output json > "$T/admin.json" 2> /dev/null \
    || fallo "no se pudo releer $FN_ADMIN"
  python3 "$AYUDA" lambda-coincide "$T/admin.json" "$SHA" "$T/env.json" || fallo "$FN_ADMIN creada no coincide con lo esperado"
  python3 "$AYUDA" codigo-coincide "$T/admin.json" "$SHA" > /dev/null || fallo "$FN_ADMIN creada tiene otro CodeSha256 que el paquete del repo"
  echo "  $FN_ADMIN creada: Active · Successful"
fi

# Actualización solo de código (ALLOW_ADMIN_CODE_UPDATE=1, ya verificada la copia de rollback). No toca rol, Function URL,
# permisos, variables ni CloudFront. Si falla después de update-function-code, el código anterior está en la copia de rollback.
if [[ $CODIGO_ACTUALIZAR == si ]]; then
  "$AWS" lambda update-function-code --function-name "$FN_ADMIN" --zip-file "fileb://$T/admin.zip" --output json \
    > "$T/upd.json" 2> "$T/upd.err" || fallo "update-function-code ($(detalle "$T/upd.err")); código anterior en la copia de rollback"
  "$AWS" lambda wait function-updated-v2 --function-name "$FN_ADMIN" 2> "$T/w2.err" \
    || fallo "$FN_ADMIN no llegó a Successful tras actualizar el código ($(detalle "$T/w2.err")); código anterior en la copia de rollback"
  "$AWS" lambda get-function-configuration --function-name "$FN_ADMIN" --output json > "$T/admin.json" 2> /dev/null \
    || fallo "no se pudo releer $FN_ADMIN tras actualizar el código"
  python3 "$AYUDA" codigo-coincide "$T/admin.json" "$SHA" > /dev/null \
    || fallo "$FN_ADMIN no tiene el CodeSha256 del paquete del repo tras la actualización"
  python3 "$AYUDA" lambda-coincide "$T/admin.json" "$SHA" "$T/env.json" \
    || fallo "$FN_ADMIN no coincide con lo esperado tras actualizar el código"
  echo "  código de $FN_ADMIN actualizado: CodeSha256 ${SHA:0:12}… · Successful"
fi
"$AWS" lambda get-function-concurrency --function-name "$FN_ADMIN" --output json > "$T/conc.json" 2> /dev/null
[[ -s "$T/conc.json" ]] || echo '{}' > "$T/conc.json"
[[ -z "$(valor "$T/conc.json" ReservedConcurrentExecutions)" ]] || fallo "$FN_ADMIN tiene concurrencia reservada"
echo "  $FN_ADMIN: sin concurrencia reservada"

echo "CHECKPOINT 3 — Lambda admin OK"

if [[ $URL_EXISTE == no ]]; then
  "$AWS" lambda create-function-url-config --function-name "$FN_ADMIN" --auth-type AWS_IAM --output json \
    > "$T/url.json" 2> "$T/u.err" || fallo "create-function-url-config ($(detalle "$T/u.err"))"
  echo "  Function URL creada (AWS_IAM)"
fi
URL="$(valor "$T/url.json" FunctionUrl)"
HOST="$(python3 -c 'import sys; from urllib.parse import urlparse; print(urlparse(sys.argv[1]).netloc)' "$URL")"
[[ -n "$HOST" ]] || fallo "no se obtuvo el host de la Function URL"

if [[ $PERM == ninguno ]]; then
  "$AWS" lambda add-permission --function-name "$FN_ADMIN" --statement-id "$SID_URL" --action lambda:InvokeFunctionUrl \
    --principal cloudfront.amazonaws.com --source-arn "$DIST_ARN" --function-url-auth-type AWS_IAM \
    --output json > /dev/null 2> "$T/p1.err" || fallo "add-permission InvokeFunctionUrl ($(detalle "$T/p1.err"))"
  "$AWS" lambda add-permission --function-name "$FN_ADMIN" --statement-id "$SID_INVOCA" --action lambda:InvokeFunction \
    --principal cloudfront.amazonaws.com --source-arn "$DIST_ARN" --invoked-via-function-url \
    --output json > /dev/null 2> "$T/p2.err" || fallo "add-permission InvokeFunction ($(detalle "$T/p2.err"))"
  echo "  dos permisos de CloudFront agregados"
fi
"$AWS" lambda get-policy --function-name "$FN_ADMIN" --output json > "$T/admin-policy2.json" 2> "$T/ap2.err" \
  || fallo "no se pudo verificar la resource policy final"
[[ "$(python3 "$AYUDA" permisos "$T/admin-policy2.json" "$DIST_ARN")" == ok ]] \
  || fallo "la resource policy final no tiene las dos sentencias esperadas"
echo "  resource policy verificada: 2 sentencias (AWS_IAM, InvokedViaFunctionUrl, SourceArn de la distribución)"

if [[ $OAC_EXISTE == no ]]; then
  cat > "$T/oac.json" <<JSON
{"Name": "$OAC_NOMBRE", "Description": "OAC de la API admin V2 (Lambda Function URL)", "SigningProtocol": "sigv4", "SigningBehavior": "always", "OriginAccessControlOriginType": "lambda"}
JSON
  "$AWS" cloudfront create-origin-access-control --origin-access-control-config "file://$T/oac.json" --output json \
    > "$T/oac-create.json" 2> "$T/oc.err" || fallo "create-origin-access-control ($(detalle "$T/oc.err"))"
  OAC_ID="$(valor "$T/oac-create.json" OriginAccessControl.Id)"
  echo "  OAC creado: $OAC_ID"
fi

if [[ $ORP_EXISTE == no ]]; then
  "$AWS" cloudfront create-origin-request-policy \
    --origin-request-policy-config "file://$ROOT/infra/resenas/admin-origin-request-policy.json" \
    --output json > "$T/orp-create.json" 2> "$T/orc.err" || fallo "create-origin-request-policy ($(detalle "$T/orc.err"))"
  ORP_ID="$(valor "$T/orp-create.json" OriginRequestPolicy.Id)"
  echo "  origin request policy creada: $ORP_ID"
fi

echo "CHECKPOINT 4 — Function URL/OAC OK"

if [[ $ORIGEN_ADMIN_EXISTE == no || $BEH_EXISTE == no ]]; then
  "$AWS" cloudfront get-distribution-config --id "$DIST" --output json > "$T/dist-ahora.json" 2> /dev/null \
    || fallo "no se pudo releer la configuración de $DIST"
  [[ "$(valor "$T/dist-ahora.json" ETag)" == "$ETAG" ]] \
    || fallo "la distribución cambió desde la copia de rollback (ETag distinto): no se escribe"
  python3 "$AYUDA" dist-nueva "$T/dist-ahora.json" "$T/dist-nueva.json" "$HOST" "$OAC_ID" "$ORP_ID"
  case $? in
    0) ;;
    5) echo "ERROR: conflicto de patrones en CloudFront; no se escribe nada." >&2; exit 5 ;;
    *) distinto "la configuración existente de CloudFront no coincide con lo esperado" ;;
  esac
  ETAG_UPDATE="$(valor "$T/dist-ahora.json" ETag)"   # ETag leído justo antes del update
  [[ -n "$ETAG_UPDATE" ]] || fallo "no se pudo leer el ETag inmediatamente antes del update"
  "$AWS" cloudfront update-distribution --id "$DIST" --if-match "$ETAG_UPDATE" \
    --distribution-config "file://$T/dist-nueva.json" --output json > "$T/du.json" 2> "$T/du.err" \
    || fallo "update-distribution ($(detalle "$T/du.err"))"
  echo "  update-distribution enviado (If-Match ${ETAG_UPDATE:0:8}…)"
  for _ in $(seq 1 60); do
    "$AWS" cloudfront get-distribution --id "$DIST" --output json > "$T/dist-poll.json" 2> /dev/null \
      || fallo "no se pudo consultar el estado de $DIST"
    [[ "$(valor "$T/dist-poll.json" Distribution.Status)" == Deployed ]] && break
    sleep "$ESPERA"
  done
  [[ "$(valor "$T/dist-poll.json" Distribution.Status)" == Deployed ]] || fallo "la distribución no quedó Deployed"
  "$AWS" cloudfront get-distribution-config --id "$DIST" --output json > "$T/dist-despues.json" 2> /dev/null \
    || fallo "no se pudo leer la configuración final de $DIST"
  python3 "$AYUDA" dist-verificar "$T/dist-ahora.json" "$T/dist-despues.json" "$ORP_ID" \
    || fallo "la configuración final de CloudFront no coincide con lo esperado"
  echo "  distribución $DIST: Deployed · default behavior y origen S3 sin cambios · behavior admin verificado"
fi

# ── 10. Checkpoint 5 y smoke de la API admin ─────────────────────────────────────────────
paso "10. CloudFront y smoke de la API admin"
"$AWS" cloudfront get-distribution --id "$DIST" --output json > "$T/dist-final.json" 2> /dev/null \
  || fallo "no se pudo consultar el estado final de $DIST"
[[ "$(valor "$T/dist-final.json" Distribution.Status)" == Deployed ]] || fallo "CHECKPOINT 5: $DIST no está Deployed"
echo "CHECKPOINT 5 — CloudFront Deployed"

# Negativo 1: GET sin cookie. La API responde 401 con no_autenticado. No hay efectos.
CODIGO_A="$($CURL -sS -o "$T/a.body" -w '%{http_code}' "$BASE/admin/api/sesion")" \
  || fallo "no se pudo llamar a $BASE/admin/api/sesion"
if [[ "$CODIGO_A" != 401 ]] || ! grep -q no_autenticado "$T/a.body"; then
  if grep -q '"ok": false' "$T/a.body"; then ORIGEN_A="con formato de la app"; else ORIGEN_A="sin formato de la app (probable CloudFront o Function URL)"; fi
  echo "ERROR: GET /admin/api/sesion sin cookie → $CODIGO_A, respuesta $ORIGEN_A (se esperaba 401 de la API, con no_autenticado)." >&2
  exit 6
fi
echo "CHECKPOINT 6 — GET sesión 401"

# Negativo 2: POST de magic link SIN Origin. CSRF lo rechaza antes de tocar SES: 400 de la API, sin correo.
printf '{}' > "$T/vacio.json"
HASH="$(sha256sum "$T/vacio.json" | cut -d' ' -f1)"
CODIGO_C="$($CURL -sS -o "$T/c.body" -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' -H 'X-Admin-Request: 1' -H "x-amz-content-sha256: $HASH" \
  --data-binary "@$T/vacio.json" "$BASE/admin/api/sesion/solicitar")" \
  || fallo "no se pudo llamar a $BASE/admin/api/sesion/solicitar"
if [[ "$CODIGO_C" != 400 ]] || ! grep -q '"ok": false' "$T/c.body"; then
  if grep -q '"ok": false' "$T/c.body"; then ORIGEN_C="con formato de la app"; else ORIGEN_C="sin formato de la app (probable CloudFront o Function URL)"; fi
  echo "ERROR: POST /admin/api/sesion/solicitar sin Origin → $CODIGO_C, respuesta $ORIGEN_C (se esperaba 400 de la API)." >&2
  exit 6
fi
echo "CHECKPOINT 6B — CSRF negativo 400 (rechazado antes de enviar correo)"

# Positivo: UN solo POST de magic link, solo con ALLOW_ADMIN_MAGIC_EMAIL=1. Sin reintentos.
if [[ "${ALLOW_ADMIN_MAGIC_EMAIL:-}" == "1" ]]; then
  printf '{}' > "$T/magic-cuerpo.json"
  HASH_MAGIC="$(sha256sum "$T/magic-cuerpo.json" | cut -d' ' -f1)"
  CODIGO_P="$($CURL -sS -o "$T/magic.body" -w '%{http_code}' -X POST \
    -H "Origin: $BASE" -H 'Content-Type: application/json' -H 'X-Admin-Request: 1' \
    -H "x-amz-content-sha256: $HASH_MAGIC" --data-binary "@$T/magic-cuerpo.json" \
    "$BASE/admin/api/sesion/solicitar")" \
    || fallo "el POST de magic link no se pudo completar (no se reintenta)"
  if [[ "$CODIGO_P" != 202 ]]; then
    echo "ERROR: POST /admin/api/sesion/solicitar válido → $CODIGO_P (se esperaba 202). No se reintenta." >&2
    exit 6
  fi
  echo "CHECKPOINT 7 — POST solicitar 202 · email de acceso solicitado"
  echo "Revisá la bandeja del moderador y confirmá manualmente la recepción."
else
  echo "Smoke positivo de magic email omitido; requiere ALLOW_ADMIN_MAGIC_EMAIL=1"
fi

# CHECKPOINT 8: el registro no contiene correos completos ni material de credenciales.
for correo in "$RESENAS_FROM_EMAIL" "$RESENAS_ADMIN_EMAIL"; do
  grep -qF "$correo" "$LOG" && { echo "ERROR: el registro contiene un correo completo." >&2; exit 6; }
done
grep -qE 'SessionToken|AccessKeyId|SecretAccessKey|AKIA|x-amz-security-token' "$LOG" \
  && { echo "ERROR: el registro contiene material de credenciales." >&2; exit 6; }
echo "CHECKPOINT 8 — logs sanitizados"

# ── 11. Cierre ────────────────────────────────────────────────────────────────────────────
paso "11. Cierre"
(
  cd "$RB" && sha256sum ./*."$STAMP".* > "manifest.$STAMP.sha256" && chmod 600 "manifest.$STAMP.sha256"
) || fallo "no se pudo escribir el manifest de rollback"
echo "  rollback y manifest en $RB (ejecución $STAMP)"
echo
echo "API admin V2 desplegada. Smoke: 401 y 400 verificados."
echo "Detenido aquí: frontend admin NO desplegado; Lambda pública NO modificada; RESENAS_NOTIFY_ENABLED sin cambios."
exit 0
