#!/usr/bin/env bash
# ORK3D · V2 · IAM del aviso: agrega a la política del rol de la Lambda pública lo que falta para el aviso.
#
# Sentencias que agrega (las mismas de infra/resenas/lambda-permisos.json, con los valores reales):
#   NotificarNuevaResenaAlModerador  ses:SendEmail solo desde RESENAS_FROM_EMAIL hacia RESENAS_ADMIN_EMAIL
#   GuardarAvisosDeNotificacion      s3:PutObject solo en notificaciones/*, para reservar el aviso antes de enviarlo
# No cambia ninguna sentencia existente, ni otros roles, ni CloudFront, ni la Lambda. Va separado de la Fase A a propósito.
#
# Modos:
#   diff (por defecto)  SOLO LECTURA. Muestra qué sentencias faltan y su forma (con placeholders, sin correos).
#   aplicar             Exige ALLOW_NOTIFY_PUBLIC_IAM=1. Guarda la política anterior (600) y escribe la política fusionada.
#                       Si ya están las dos sentencias, no escribe nada.
#   verify              SOLO LECTURA. Comprueba que las dos sentencias están y que las anteriores siguen iguales.
#
# Los correos salen de la Lambda admin (fuente validada). Variables: AWS_PROFILE=default, AWS_REGION=us-east-1.
# NOTIFY_PUBLIC_RUTA: carpeta privada de copias (por defecto /tmp/ork3d-notify-public).
#
# Códigos: 0 correcto · 1 verificación o escritura falló · 2 entorno inválido · 3 falta la guarda · 4 no está listo.
set -uo pipefail

MODO="${1:-diff}"
case "$MODO" in
  diff|aplicar|verify) ;;
  *) echo "uso: $0 [diff|aplicar|verify]" >&2; exit 2 ;;
esac

if [[ "$MODO" == aplicar ]] && [[ "${ALLOW_NOTIFY_PUBLIC_IAM:-}" != "1" ]]; then
  echo "ABORTA: aplicar escribe la política del rol de la Lambda pública. Para ejecutarlo: ALLOW_NOTIFY_PUBLIC_IAM=1 infra/notify-public-iam.sh aplicar" >&2
  exit 3
fi

PERFIL="${AWS_PROFILE:-default}"
REGION="${AWS_REGION:-us-east-1}"
[[ "$PERFIL" == "default" ]] || { echo "ERROR: AWS_PROFILE solo puede ser default en este paso." >&2; exit 2; }
[[ "$REGION" == "us-east-1" ]] || { echo "ERROR: AWS_REGION solo puede ser us-east-1 en este paso." >&2; exit 2; }
export AWS_PROFILE="$PERFIL" AWS_REGION="$REGION" AWS_PAGER=""

AWS="${AWS_CLI:-aws}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AYUDA="$ROOT/infra/notify_activacion_ayuda.py"
AYUDA_ADMIN="$ROOT/infra/admin_deploy_ayuda.py"
PLANTILLA="$ROOT/infra/resenas/lambda-permisos.json"
FN="ork3d-resenas"
FN_ADMIN="ork3d-resenas-admin"
POLITICA="resenas-bucket-privado"
RUTA="${NOTIFY_PUBLIC_RUTA:-/tmp/ork3d-notify-public}"
RB="$RUTA/rollback"
FALLOS=0
T="$(mktemp -d)"
chmod 700 "$T"
trap 'rm -rf "$T"' EXIT

fallo()  { echo "ERROR: $*" >&2; exit 1; }
detalle() { python3 "$AYUDA_ADMIN" detalle "$1"; }
valor()  { python3 "$AYUDA_ADMIN" get "$1" "$2"; }
ayuda() {  # ORDEN ARGS...: salida de la ayuda con sangría; un código distinto de 0 cuenta
  local salida rc
  salida="$(python3 "$AYUDA" "$@" 2>&1)"; rc=$?
  printf '%s\n' "$salida" | sed 's/^/  /'
  [[ $rc -eq 0 ]] || FALLOS=$((FALLOS + 1))
}

"$AWS" lambda get-function-configuration --function-name "$FN" --output json > "$T/config.json" 2> "$T/cfg.err" \
  || fallo "no se pudo leer $FN ($(detalle "$T/cfg.err"))"
"$AWS" lambda get-function-configuration --function-name "$FN_ADMIN" --output json > "$T/admin.json" 2> "$T/admin.err" \
  || fallo "no se pudo leer $FN_ADMIN para tomar los correos ($(detalle "$T/admin.err"))"

ROL_ARN="$(valor "$T/config.json" Role)"
ROL="${ROL_ARN##*/}"
CUENTA="$(echo "$ROL_ARN" | cut -d: -f5)"
BUCKET="$(valor "$T/config.json" Environment.Variables.RESENAS_BUCKET)"
ORIGEN="$(valor "$T/admin.json" Environment.Variables.RESENAS_FROM_EMAIL)"
DESTINO="$(valor "$T/admin.json" Environment.Variables.RESENAS_ADMIN_EMAIL)"
[[ -n "$BUCKET" && -n "$ORIGEN" && -n "$DESTINO" ]] || fallo "falta el bucket o un correo en la configuración (no se cambió nada)"
python3 "$AYUDA" config-iam "$T/config.json" "$T/admin.json" "$T/iam-config.json" || fallo "no se pudo armar la configuración de verificación"

"$AWS" iam get-role-policy --role-name "$ROL" --policy-name "$POLITICA" --output json > "$T/politica.json" 2> "$T/pol.err" \
  || fallo "no se pudo leer la política $POLITICA del rol $ROL: no se crea una nueva ($(detalle "$T/pol.err"))"

fusionar() {  # FUSIÓN: la política con lo que falta; escribe $T/fusion.json solo si hay algo que agregar
  ayuda politica-fusion "$T/politica.json" "$PLANTILLA" "$BUCKET" "$ORIGEN" "$DESTINO" "$CUENTA" "$T/fusion.json"
}

if [[ "$MODO" == diff ]]; then
  echo "── Política $POLITICA del rol $ROL (solo lectura) ──"
  fusionar
  if [[ -f "$T/fusion.json" ]]; then
    echo
    echo "── Forma de las sentencias que se agregarían (placeholders, sin correos) ──"
    python3 "$AYUDA" muestra-sentencias "$PLANTILLA" | sed 's/^/  /'
  fi
  echo
  if (( FALLOS > 0 )); then
    echo "NO LISTO: revisá cada FAIL. No se cambió nada."
    exit 4
  fi
  if [[ -f "$T/fusion.json" ]]; then
    echo "Faltan sentencias: ALLOW_NOTIFY_PUBLIC_IAM=1 infra/notify-public-iam.sh aplicar"
  else
    echo "IAM listo: el rol ya tiene las dos sentencias del aviso."
  fi
  exit 0
fi

if [[ "$MODO" == aplicar ]]; then
  echo "── Política $POLITICA del rol $ROL ──"
  fusionar
  (( FALLOS == 0 )) || { echo "NO LISTO: no se escribió nada."; exit 4; }
  if [[ ! -f "$T/fusion.json" ]]; then
    echo "IAM sin cambios: el rol ya tiene las dos sentencias del aviso."
    exit 0
  fi
  mkdir -p "$RB" && chmod 700 "$RUTA" "$RB"
  COPIA="$RB/politica-antes.$(date -u +%Y%m%dT%H%M%S%6NZ).json"
  cp "$T/politica.json" "$COPIA" && chmod 600 "$COPIA" || fallo "no se pudo guardar la política anterior"
  echo "  copia de la política anterior: $COPIA (600)"
  "$AWS" iam put-role-policy --role-name "$ROL" --policy-name "$POLITICA" --policy-document "file://$T/fusion.json" \
    > "$T/put.json" 2> "$T/put.err" \
    || fallo "put-role-policy ($(detalle "$T/put.err")); la política no cambió"
  "$AWS" iam get-role-policy --role-name "$ROL" --policy-name "$POLITICA" --output json > "$T/despues.json" 2> "$T/desp.err" \
    || fallo "no se pudo releer la política tras escribirla: revisá con verify y la copia anterior"
  echo "── Verificación posterior ──"
  ayuda comparar-politica "$COPIA" "$T/despues.json"
  ayuda politica "$T/despues.json" "$PLANTILLA" "$T/iam-config.json" "$CUENTA"
  if (( FALLOS > 0 )); then
    echo "ERROR: la política no quedó como se esperaba. Copia anterior: $COPIA" >&2
    exit 1
  fi
  echo "IAM aplicado: el rol tiene las dos sentencias del aviso y las anteriores siguen iguales."
  exit 0
fi

# verify
echo "── Verificación de la política (solo lectura) ──"
ayuda politica "$T/politica.json" "$PLANTILLA" "$T/iam-config.json" "$CUENTA"
(( FALLOS == 0 )) && { echo "IAM listo."; exit 0; }
echo "IAM con FAIL: revisá las líneas marcadas."
exit 1
