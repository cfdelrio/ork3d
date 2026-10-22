#!/usr/bin/env bash
# ORK3D · V2 · activación controlada del aviso de reseña nueva (RESENAS_NOTIFY_ENABLED) en la Lambda pública.
#
# Modos:
#   preflight (por defecto)  SOLO LECTURA. Comprueba lo que el aviso necesita: Lambda sana, código desplegado con el
#                            aviso e idéntico a HEAD, política del rol con SES y bucket de avisos, identidades SES
#                            verificadas y SES de la cuenta habilitado.
#   activar                  Exige ALLOW_NOTIFY_ENABLE=1. Vuelve a correr preflight; si no está LISTO, no escribe.
#                            Guarda la configuración actual (600) y escribe UNA variable, RESENAS_NOTIFY_ENABLED=true,
#                            con --revision-id: si la Lambda cambió entre medio, la escritura se rechaza.
#   revertir                 Exige ALLOW_NOTIFY_ENABLE=1. Restaura exactamente la configuración guardada antes de activar.
#   observar                 SOLO LECTURA. Cuenta los eventos del aviso en los logs de la Lambda pública y, con REVIEW_ID,
#                            comprueba que exista notificaciones/<id>.json. No muestra mensajes de los logs ni correos.
#
# No toca: el código de la Lambda pública, la Lambda admin, /opiniones, CloudFront, IAM, DNS, SES, el bucket, ni
# ninguna otra variable de la Lambda pública. No ejecuta resenas-setup.sh ni setup.sh.
# Variables: AWS_PROFILE=default y AWS_REGION=us-east-1 (los únicos admitidos).
# NOTIFY_RUTA: carpeta privada de copias para revertir (~/ork3d-notify por defecto).
# NOTIFY_ANTES (revertir, opcional): ruta concreta de la copia a restaurar; por defecto, la más reciente.
# MINUTOS (observar): ventana de logs, 60 por defecto. REVIEW_ID (observar, opcional): id de 32 hex de una reseña.
#
# Códigos: 0 correcto · 1 verificación o escritura falló · 2 uso o entorno inválido ·
#          3 falta ALLOW_NOTIFY_ENABLE=1 (antes de llamar a AWS) · 4 no está LISTO (no se escribió nada).
set -uo pipefail

MODO="${1:-preflight}"
case "$MODO" in
  preflight|activar|revertir|observar) ;;
  *) echo "uso: $0 [preflight|activar|revertir|observar]" >&2; exit 2 ;;
esac

if [[ "$MODO" == activar || "$MODO" == revertir ]] && [[ "${ALLOW_NOTIFY_ENABLE:-}" != "1" ]]; then
  echo "ABORTA: $MODO escribe la configuración de la Lambda pública. Para ejecutarlo: ALLOW_NOTIFY_ENABLE=1 infra/notify-activacion.sh $MODO" >&2
  exit 3
fi

PERFIL="${AWS_PROFILE:-default}"
REGION="${AWS_REGION:-us-east-1}"
[[ "$PERFIL" == "default" ]] || { echo "ERROR: AWS_PROFILE solo puede ser default en este paso." >&2; exit 2; }
[[ "$REGION" == "us-east-1" ]] || { echo "ERROR: AWS_REGION solo puede ser us-east-1 en este paso." >&2; exit 2; }
export AWS_PROFILE="$PERFIL" AWS_REGION="$REGION" AWS_PAGER=""

AWS="${AWS_CLI:-aws}"
CURL="${CURL_CMD:-curl}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AYUDA="$ROOT/infra/notify_activacion_ayuda.py"
AYUDA_ADMIN="$ROOT/infra/admin_deploy_ayuda.py"
PLANTILLA="$ROOT/infra/resenas/lambda-permisos.json"
FN="ork3d-resenas"
LOG_GRUPO="/aws/lambda/ork3d-resenas"
POLITICA="resenas-bucket-privado"
RUTA="${NOTIFY_RUTA:-$HOME/ork3d-notify}"
RB="$RUTA/rollback"
PATRON_CORREO='^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'
FALLOS=0
ESTADO_FLAG=""

T="$(mktemp -d)"
chmod 700 "$T"
trap 'rm -rf "$T"' EXIT

fallo()    { echo "ERROR: $*" >&2; exit 1; }
detalle()  { python3 "$AYUDA_ADMIN" detalle "$1"; }   # código, operación y mensaje de la CLI, sanitizados
valor()    { python3 "$AYUDA_ADMIN" get "$1" "$2"; }
enmascarar() { python3 "$AYUDA_ADMIN" mascarar "$1"; }
codigo_error() { python3 -c 'import re, sys; m = re.search(r"An error occurred \((\w+)\)", open(sys.argv[1], encoding="utf-8", errors="replace").read()); print(m.group(1) if m else "desconocido")' "$1"; }

resultado() {  # PASS|FAIL NOMBRE: una línea; cada FAIL cuenta
  if [[ "$1" == PASS ]]; then echo "  PASS  $2"; else echo "  FAIL  $2"; FALLOS=$((FALLOS + 1)); fi
}

ayuda() {  # ORDEN ARGS...: la salida de infra/notify_activacion_ayuda.py con sangría; un código distinto de 0 cuenta
  local salida rc
  salida="$(python3 "$AYUDA" "$@" 2>&1)"; rc=$?
  printf '%s\n' "$salida" | sed 's/^/  /'
  [[ $rc -eq 0 ]] || FALLOS=$((FALLOS + 1))
}

leer_config() {  # deja la configuración de la Lambda pública en $T/config.json (sin código ni URLs)
  "$AWS" lambda get-function-configuration --function-name "$FN" --output json > "$T/config.json" 2> "$T/config.err" \
    || fallo "no se pudo leer $FN ($(detalle "$T/config.err"))"
}

comprobar() {  # preflight: todo solo lectura. Deja $ESTADO_FLAG y cuenta los FAIL en $FALLOS.
  echo "── Comprobaciones (solo lectura) ──"
  leer_config
  resultado "$([[ "$(valor "$T/config.json" State)" == Active && "$(valor "$T/config.json" LastUpdateStatus)" == Successful ]] && echo PASS || echo FAIL)" \
    "$FN activa y con la última actualización Successful"
  resultado "$([[ "$(valor "$T/config.json" Runtime)" == python3.12 && "$(valor "$T/config.json" Handler)" == app.handler ]] && echo PASS || echo FAIL)" \
    "runtime python3.12 y handler app.handler"
  echo "  INFO  timeout $(valor "$T/config.json" Timeout) s · memoria $(valor "$T/config.json" MemorySize) MB"

  ESTADO_FLAG="$(python3 "$AYUDA" estado-flag "$T/config.json" 2>/dev/null)"
  case "$ESTADO_FLAG" in
    apagado|activo) echo "  INFO  RESENAS_NOTIFY_ENABLED: $ESTADO_FLAG" ;;
    *) echo "  FAIL  RESENAS_NOTIFY_ENABLED tiene un valor no reconocido (se espera ausente, false o true)"; FALLOS=$((FALLOS + 1)) ;;
  esac

  "$AWS" lambda get-function --function-name "$FN" --output json > "$T/fn.json" 2> "$T/fn.err" \
    || fallo "no se pudo leer el código de $FN ($(detalle "$T/fn.err"))"
  "$CURL" -sS -o "$T/codigo.zip" "$(valor "$T/fn.json" Code.Location)" 2> "$T/codigo.err" \
    || fallo "no se pudo descargar el código desplegado (URL firmada no mostrada)"
  resultado "$([[ "$(python3 "$AYUDA_ADMIN" sha-archivo "$T/codigo.zip")" == "$(valor "$T/config.json" CodeSha256)" ]] && echo PASS || echo FAIL)" \
    "el código descargado coincide con su CodeSha256"
  ayuda codigo "$T/codigo.zip" "$ROOT"

  local rol_arn rol cuenta
  rol_arn="$(valor "$T/config.json" Role)"
  rol="${rol_arn##*/}"
  cuenta="$(echo "$rol_arn" | cut -d: -f5)"
  if "$AWS" iam get-role-policy --role-name "$rol" --policy-name "$POLITICA" --output json > "$T/politica.json" 2> "$T/pol.err"; then
    ayuda politica "$T/politica.json" "$PLANTILLA" "$T/config.json" "$cuenta"
  else
    resultado FAIL "política $POLITICA del rol $rol legible ($(detalle "$T/pol.err"))"
  fi

  local variable dir
  for variable in RESENAS_FROM_EMAIL RESENAS_ADMIN_EMAIL; do
    dir="$(valor "$T/config.json" "Environment.Variables.$variable")"
    if ! [[ "$dir" =~ $PATRON_CORREO ]]; then
      resultado FAIL "$variable falta o no tiene formato de correo en la configuración"
      continue
    fi
    if "$AWS" sesv2 get-email-identity --email-identity "$dir" --output json > "$T/id.json" 2> "$T/id.err"; then
      resultado "$([[ "$(valor "$T/id.json" IdentityType)" == EMAIL_ADDRESS && "$(valor "$T/id.json" VerificationStatus)" == SUCCESS && "$(valor "$T/id.json" VerifiedForSendingStatus)" == true ]] && echo PASS || echo FAIL)" \
        "$variable $(enmascarar "$dir") es EMAIL_ADDRESS verificada y habilitada para enviar"
    else
      resultado FAIL "$variable no es una identidad SES ($(detalle "$T/id.err"))"
    fi
  done

  if "$AWS" sesv2 get-account --output json > "$T/cuenta.json" 2> "$T/cuenta.err"; then
    resultado "$([[ "$(valor "$T/cuenta.json" SendingEnabled)" == true ]] && echo PASS || echo FAIL)" "SES de la cuenta habilitado para enviar"
  else
    resultado FAIL "SES: no se pudo leer la cuenta ($(detalle "$T/cuenta.err"))"
  fi
}

escribir_configuracion() {  # ARCHIVO-ENV REVISION: update-function-configuration con revisión; nada más cambia
  "$AWS" lambda update-function-configuration --function-name "$FN" --environment "file://$1" \
    --revision-id "$2" --output json > "$T/upd.json" 2> "$T/upd.err" \
    || fallo "update-function-configuration ($(detalle "$T/upd.err")); la Lambda no cambió: revisá con preflight"
  "$AWS" lambda wait function-updated-v2 --function-name "$FN" 2> "$T/w.err" \
    || fallo "$FN no llegó a Successful tras el cambio ($(detalle "$T/w.err")). Estado incierto: revisá con preflight y, si hace falta, revertir"
  "$AWS" lambda get-function-configuration --function-name "$FN" --output json > "$T/despues.json" 2> /dev/null \
    || fallo "no se pudo releer $FN tras el cambio"
}

if [[ "$MODO" == preflight ]]; then
  comprobar
  echo
  if (( FALLOS > 0 )); then
    echo "NO LISTO: revisá cada FAIL. No se cambió nada."
    exit 1
  fi
  if [[ "$ESTADO_FLAG" == activo ]]; then
    echo "YA ACTIVO: RESENAS_NOTIFY_ENABLED está en true. No hay nada que activar."
    exit 0
  fi
  echo "LISTO PARA ACTIVAR. Siguiente: ALLOW_NOTIFY_ENABLE=1 infra/notify-activacion.sh activar"
  exit 0
fi

if [[ "$MODO" == activar ]]; then
  mkdir -p "$RB" && chmod 700 "$RUTA" "$RB"
  comprobar
  if (( FALLOS > 0 )); then
    echo
    echo "NO LISTO: no se escribió nada. Revisá cada FAIL con preflight."
    exit 4
  fi
  if [[ "$ESTADO_FLAG" == activo ]]; then
    echo
    echo "YA ACTIVO: sin cambios."
    exit 0
  fi
  echo
  echo "── Activación ──"
  COPIA="$RB/notify-antes.$(date -u +%Y%m%dT%H%M%S%6NZ).json"
  python3 "$AYUDA" snapshot "$T/config.json" "$COPIA" || fallo "no se pudo guardar la copia para revertir"
  echo "  copia para revertir: $COPIA (600)"
  python3 "$AYUDA" plan-activar "$T/config.json" "$T/env-nuevo.json" || fallo "no se pudo preparar la configuración nueva"
  escribir_configuracion "$T/env-nuevo.json" "$(valor "$T/config.json" RevisionId)"

  echo "── Verificación posterior ──"
  ayuda comparar "$T/config.json" "$T/despues.json" activar
  if (( FALLOS > 0 )); then
    echo "ERROR: la configuración no quedó como se esperaba. Revertir: ALLOW_NOTIFY_ENABLE=1 infra/notify-activacion.sh revertir" >&2
    exit 1
  fi
  echo
  echo "Aviso activado: RESENAS_NOTIFY_ENABLED=true. Para volver atrás: ALLOW_NOTIFY_ENABLE=1 infra/notify-activacion.sh revertir"
  echo "Siguiente: infra/notify-activacion.sh observar, y una reseña de prueba aprobada por el dueño."
  exit 0
fi

if [[ "$MODO" == revertir ]]; then
  COPIA="${NOTIFY_ANTES:-$(ls -t "$RB"/notify-antes.*.json 2>/dev/null | head -1)}"
  [[ -n "$COPIA" && -f "$COPIA" ]] || fallo "no hay copia para revertir en $RB (NOTIFY_ANTES indica una ruta concreta)"
  leer_config
  python3 "$AYUDA" plan-revertir "$COPIA" "$T/env-revertir.json" || fallo "no se pudo preparar la configuración guardada"
  escribir_configuracion "$T/env-revertir.json" "$(valor "$T/config.json" RevisionId)"
  ayuda comparar "$COPIA" "$T/despues.json" revertir
  if (( FALLOS > 0 )); then
    echo "ERROR: la configuración no coincide con la copia $(basename "$COPIA")." >&2
    exit 1
  fi
  echo
  echo "Revertido: configuración igual a $(basename "$COPIA"). Las reseñas guardadas no cambian."
  exit 0
fi

# observar: solo lectura
MINUTOS="${MINUTOS:-60}"
[[ "$MINUTOS" =~ ^[0-9]+$ ]] || fallo "MINUTOS debe ser un número de minutos"
leer_config
echo "── Observación (solo lectura, últimos $MINUTOS min) ──"
echo "  INFO  RESENAS_NOTIFY_ENABLED: $(python3 "$AYUDA" estado-flag "$T/config.json" 2>/dev/null || echo desconocido)"
INICIO_MS="$(python3 -c 'import sys, time; print(int((time.time() - int(sys.argv[1]) * 60) * 1000))' "$MINUTOS")"
"$AWS" logs filter-log-events --log-group-name "$LOG_GRUPO" --start-time "$INICIO_MS" --output json \
  > "$T/logs.json" 2> "$T/logs.err" || fallo "no se pudieron leer los logs ($(detalle "$T/logs.err"))"
ayuda logs "$T/logs.json"
if [[ -n "${REVIEW_ID:-}" ]]; then
  [[ "$REVIEW_ID" =~ ^[0-9a-f]{32}$ ]] || fallo "REVIEW_ID debe ser un id de 32 caracteres hexadecimales"
  BUCKET="$(valor "$T/config.json" Environment.Variables.RESENAS_BUCKET)"
  if "$AWS" s3api head-object --bucket "$BUCKET" --key "notificaciones/$REVIEW_ID.json" > /dev/null 2> "$T/head.err"; then
    echo "  PASS  notificaciones/$REVIEW_ID.json existe: el aviso quedó registrado"
  elif [[ "$(codigo_error "$T/head.err")" == 404 ]]; then
    echo "  INFO  notificaciones/$REVIEW_ID.json no existe todavía"
  else
    fallo "no se pudo consultar notificaciones/$REVIEW_ID.json ($(detalle "$T/head.err"))"
  fi
fi
if (( FALLOS > 0 )); then
  echo "Observación con FAIL: revisá las líneas marcadas."
  exit 1
fi
exit 0
