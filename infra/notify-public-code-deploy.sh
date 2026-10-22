#!/usr/bin/env bash
# ORK3D · V2 · Fase A del aviso: código y variables de la Lambda pública ork3d-resenas, con RESENAS_NOTIFY_ENABLED=false.
#
# Modos:
#   diff (por defecto)  SOLO LECTURA. Descarga el paquete desplegado, lo compara con el propuesto (app.py y su cierre de
#                       imports) y dice qué archivos cambian y qué variables se agregarían.
#   deploy              Exige ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY=1. Antes de escribir guarda las copias (600): configuración,
#                       código, Environment, y una foto de lo que no debe cambiar. Escribe el Environment completo
#                       (actuales + nuevas) y después el código. Después verifica. No toca IAM, CloudFront, DNS ni la Lambda admin.
#   verify              SOLO LECTURA. Compara la Lambda con la foto guardada antes del despliegue.
#   revertir            Exige ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY=1. Restaura el Environment y el código guardados.
#
# Variables: AWS_PROFILE=default y AWS_REGION=us-east-1 (los únicos admitidos).
# NOTIFY_PUBLIC_RUTA: carpeta privada de copias (por defecto /tmp/ork3d-notify-public). NOTIFY_PUBLIC_COPIA (revertir):
# carpeta concreta de la copia; por defecto, la más reciente.
#
# Códigos: 0 correcto · 1 verificación o escritura falló · 2 entorno inválido · 3 falta la guarda (antes de AWS) ·
#          4 no está listo (no se escribió nada).
set -uo pipefail

MODO="${1:-diff}"
case "$MODO" in
  diff|deploy|verify|revertir) ;;
  *) echo "uso: $0 [diff|deploy|verify|revertir]" >&2; exit 2 ;;
esac

if [[ "$MODO" == deploy || "$MODO" == revertir ]] && [[ "${ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY:-}" != "1" ]]; then
  echo "ABORTA: $MODO escribe el código y el Environment de la Lambda pública. Para ejecutarlo: ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY=1 infra/notify-public-code-deploy.sh $MODO" >&2
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
FN="ork3d-resenas"
FN_ADMIN="ork3d-resenas-admin"
DIST="ECOW0H6A48HVP"
EXPORT_URL="https://ork3d.com/resenas/publicas.json"
RUTA="${NOTIFY_PUBLIC_RUTA:-/tmp/ork3d-notify-public}"
RB="$RUTA/rollback"
FALLOS=0
T="$(mktemp -d)"
chmod 700 "$T"
trap 'rm -rf "$T"' EXIT

fallo()  { echo "ERROR: $*" >&2; exit 1; }
detalle() { python3 "$AYUDA_ADMIN" detalle "$1"; }
valor()  { python3 "$AYUDA_ADMIN" get "$1" "$2"; }
codigo_error() { python3 -c 'import re, sys; m = re.search(r"An error occurred \((\w+)\)", open(sys.argv[1], encoding="utf-8", errors="replace").read()); print(m.group(1) if m else "desconocido")' "$1"; }

ayuda() {  # ORDEN ARGS...: salida de infra/notify_activacion_ayuda.py con sangría; un código distinto de 0 cuenta
  local salida rc
  salida="$(python3 "$AYUDA" "$@" 2>&1)"; rc=$?
  printf '%s\n' "$salida" | sed 's/^/  /'
  [[ $rc -eq 0 ]] || FALLOS=$((FALLOS + 1))
}

leer_config() {  # SALIDA: configuración de la Lambda pública (sin código ni URLs firmadas)
  "$AWS" lambda get-function-configuration --function-name "$FN" --output json > "$1" 2> "$T/cfg.err" \
    || fallo "no se pudo leer $FN ($(detalle "$T/cfg.err"))"
}

descargar_codigo() {  # CONFIG SALIDA: paquete desplegado; su SHA-256 debe ser el CodeSha256 de la configuración
  "$AWS" lambda get-function --function-name "$FN" --output json > "$T/fn.json" 2> "$T/fn.err" \
    || fallo "no se pudo leer el código de $FN ($(detalle "$T/fn.err"))"
  "$CURL" -sS -o "$2" "$(valor "$T/fn.json" Code.Location)" 2> "$T/cod.err" \
    || fallo "no se pudo descargar el código desplegado (URL firmada no mostrada)"
  [[ "$(python3 "$AYUDA_ADMIN" sha-archivo "$2")" == "$(valor "$1" CodeSha256)" ]] \
    || fallo "el código descargado no coincide con su CodeSha256: no se sigue"
}

leer_opcional() {  # NOMBRE SALIDA AUSENCIA COMANDO...: recurso opcional; AUSENCIA es el único código de error que
  # significa "no existe" ("" si ninguno). Se acepta como ausencia y nada más:
  #   - exit 0 con stdout vacío: AWS no devolvió nada (p. ej. get-function-concurrency sin concurrencia reservada);
  #   - exit distinto de 0 con el código AUSENCIA (p. ej. ResourceNotFoundException).
  # Todo lo demás es un fallo: JSON inválido, AccessDenied, throttling, credenciales rotas, cualquier otro error.
  local nombre="$1" salida="$2" ausencia="$3"; shift 3
  local crudo
  crudo="$(mktemp "$T/opc.XXXXXX")"
  if "$@" > "$crudo" 2> "$T/opt.err"; then
    python3 "$AYUDA" normalizar-opcional "$crudo" "$salida" \
      || fallo "$nombre: la respuesta de AWS no es JSON válido; no se toma como ausencia"
    return 0
  fi
  if [[ -n "$ausencia" && "$(codigo_error "$T/opt.err")" == "$ausencia" ]]; then
    : > "$crudo"
    python3 "$AYUDA" normalizar-opcional "$crudo" "$salida" || fallo "$nombre: no se pudo registrar la ausencia"
    return 0
  fi
  fallo "$nombre: error de AWS, no se toma como ausencia ($(detalle "$T/opt.err"))"
}

instantanea() {  # CARPETA: foto de lo que la Fase A no debe cambiar (solo lectura)
  local carpeta="$1"
  mkdir -p "$carpeta" && chmod 700 "$carpeta"
  leer_config "$T/i-config.json"
  descargar_codigo "$T/i-config.json" "$T/i-codigo.zip"
  leer_opcional "get-function-url-config" "$T/i-url.json" ResourceNotFoundException \
    "$AWS" lambda get-function-url-config --function-name "$FN" --output json
  leer_opcional "get-policy" "$T/i-recurso.json" ResourceNotFoundException \
    "$AWS" lambda get-policy --function-name "$FN" --output json
  leer_opcional "get-function-concurrency" "$T/i-conc.json" "" \
    "$AWS" lambda get-function-concurrency --function-name "$FN" --output json
  "$AWS" cloudfront get-distribution --id "$DIST" --output json > "$T/i-cf.json" 2> "$T/cf.err" \
    || fallo "no se pudo leer la distribución $DIST ($(detalle "$T/cf.err"))"
  "$CURL" -sS -o "$T/i-export.json" "$EXPORT_URL" 2> "$T/exp.err" || fallo "no se pudo leer el export público"
  python3 "$AYUDA" instantanea "$T/i-config.json" "$T/i-codigo.zip" "$T/i-url.json" "$T/i-recurso.json" \
    "$T/i-conc.json" "$T/i-cf.json" "$(python3 "$AYUDA_ADMIN" sha-archivo "$T/i-export.json")" "$carpeta" \
    || fallo "no se pudo guardar la foto en $carpeta"
}

preparar() {  # FOTO: foto actual + paquete propuesto + Environment nuevo, en $T. Cuenta FAIL en $FALLOS.
  instantanea "$1"
  CODESHA="$(python3 "$AYUDA" zip-aviso "$ROOT" "$T/propuesto.zip")" || fallo "no se pudo armar el paquete propuesto"
  "$AWS" lambda get-function-configuration --function-name "$FN_ADMIN" --output json > "$T/admin.json" 2> "$T/admin.err" \
    || fallo "no se pudo leer $FN_ADMIN para tomar los correos ($(detalle "$T/admin.err"))"
  ayuda env-nuevo "$1/config.json" "$T/admin.json" "$T/env-nuevo.json"
}

esperar() {  # MENSAJE: espera a que la Lambda termine el último cambio
  "$AWS" lambda wait function-updated-v2 --function-name "$FN" 2> "$T/w.err" || fallo "$1 ($(detalle "$T/w.err"))"
}

if [[ "$MODO" == diff ]]; then
  echo "── Paquete desplegado contra el propuesto (solo lectura) ──"
  preparar "$T/antes"
  echo "  INFO  SHA-256 del paquete propuesto: $CODESHA"
  ayuda comparar-paquetes "$T/antes/desplegado.zip" "$T/propuesto.zip"
  echo
  echo "── Variables (solo lectura) ──"
  echo "  INFO  RESENAS_NOTIFY_ENABLED se deja en false"
  echo
  if (( FALLOS > 0 )); then
    echo "NO LISTO para desplegar. No se cambió nada: revisá cada FAIL."
    exit 1
  fi
  echo "LISTO para desplegar con ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY=1 (Fase A: notificaciones apagadas)."
  exit 0
fi

if [[ "$MODO" == deploy ]]; then
  mkdir -p "$RB" && chmod 700 "$RUTA" "$RB"
  # La foto se arma en $T y la carpeta de copia solo se crea si todo salió bien: una lectura fallida no deja
  # copias a medias que verify o revertir podrían tomar como "la más reciente".
  preparar "$T/foto"
  (( FALLOS == 0 )) || { echo "NO LISTO: no se escribió nada. Revisá cada FAIL con diff."; exit 4; }
  COPIA="$RB/$(date -u +%Y%m%dT%H%M%S%6NZ)"
  mkdir -p "$COPIA" && chmod 700 "$COPIA"
  cp -pr "$T/foto" "$COPIA/foto" || fallo "no se pudo guardar la foto de antes del despliegue"
  python3 "$AYUDA" snapshot "$COPIA/foto/config.json" "$COPIA/env-antes.json" || fallo "no se pudo guardar el Environment anterior"
  echo "  copias para revertir: $COPIA (600)"

  REV="$(valor "$COPIA/foto/config.json" RevisionId)"
  "$AWS" lambda update-function-configuration --function-name "$FN" --environment "file://$T/env-nuevo.json" \
    --revision-id "$REV" --output json > "$T/upd1.json" 2> "$T/upd1.err" \
    || fallo "update-function-configuration ($(detalle "$T/upd1.err")); el código no se cambió. Revisá con diff"
  esperar "$FN no llegó a Successful tras cambiar el Environment: revisá con verify y, si hace falta, revertir"

  leer_config "$T/despues-env.json"
  REV="$(valor "$T/despues-env.json" RevisionId)"
  "$AWS" lambda update-function-code --function-name "$FN" --zip-file "fileb://$T/propuesto.zip" \
    --revision-id "$REV" --output json > "$T/upd2.json" 2> "$T/upd2.err" \
    || fallo "update-function-code ($(detalle "$T/upd2.err")). El Environment ya cambió (flag false): revertir si hace falta"
  esperar "$FN no llegó a Successful tras cambiar el código: revertir con ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY=1"

  echo "── Verificación posterior ──"
  instantanea "$T/despues"
  ayuda verificar-publico "$COPIA/foto" "$T/despues" "$CODESHA" "$T/admin.json"
  if (( FALLOS > 0 )); then
    echo "ERROR: la Lambda no quedó como se esperaba. Revertir: ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY=1 infra/notify-public-code-deploy.sh revertir" >&2
    exit 1
  fi
  echo
  echo "Fase A aplicada: código nuevo y RESENAS_NOTIFY_ENABLED=false. Siguiente: notify-activacion.sh preflight."
  exit 0
fi

if [[ "$MODO" == verify ]]; then
  COPIA="${NOTIFY_PUBLIC_COPIA:-$(ls -td "$RB"/*/ 2>/dev/null | head -1)}"
  COPIA="${COPIA%/}"
  [[ -n "$COPIA" && -f "$COPIA/foto/config.json" ]] || fallo "no hay foto de antes del despliegue en $RB"
  "$AWS" lambda get-function-configuration --function-name "$FN_ADMIN" --output json > "$T/admin.json" 2> "$T/admin.err" \
    || fallo "no se pudo leer $FN_ADMIN ($(detalle "$T/admin.err"))"
  CODESHA="$(python3 "$AYUDA" zip-aviso "$ROOT" "$T/propuesto.zip")" || fallo "no se pudo armar el paquete propuesto"
  instantanea "$T/ahora"
  ayuda verificar-publico "$COPIA/foto" "$T/ahora" "$CODESHA" "$T/admin.json"
  (( FALLOS == 0 )) && { echo "Verificación: todo como se esperaba."; exit 0; }
  echo "Verificación con FAIL: revisá las líneas marcadas."
  exit 1
fi

# revertir
COPIA="${NOTIFY_PUBLIC_COPIA:-$(ls -td "$RB"/*/ 2>/dev/null | head -1)}"
COPIA="${COPIA%/}"
[[ -n "$COPIA" && -f "$COPIA/foto/desplegado.zip" && -f "$COPIA/env-antes.json" ]] \
  || fallo "no hay copia completa para revertir en $RB"
leer_config "$T/rev-config.json"
python3 "$AYUDA" plan-revertir "$COPIA/env-antes.json" "$T/env-revertir.json" || fallo "no se pudo preparar el Environment guardado"
"$AWS" lambda update-function-configuration --function-name "$FN" --environment "file://$T/env-revertir.json" \
  --revision-id "$(valor "$T/rev-config.json" RevisionId)" --output json > "$T/rev1.json" 2> "$T/rev1.err" \
  || fallo "update-function-configuration ($(detalle "$T/rev1.err")); nada más cambió"
esperar "$FN no llegó a Successful al restaurar el Environment"

[[ "$(python3 "$AYUDA_ADMIN" sha-archivo "$COPIA/foto/desplegado.zip")" == "$(valor "$COPIA/foto/config.json" CodeSha256)" ]] \
  || fallo "la copia del código no coincide con su CodeSha256: no se restaura"
leer_config "$T/rev-config2.json"
"$AWS" lambda update-function-code --function-name "$FN" --zip-file "fileb://$COPIA/foto/desplegado.zip" \
  --revision-id "$(valor "$T/rev-config2.json" RevisionId)" --output json > "$T/rev2.json" 2> "$T/rev2.err" \
  || fallo "update-function-code ($(detalle "$T/rev2.err")). El Environment ya se restauró"
esperar "$FN no llegó a Successful al restaurar el código"

echo "── Restauración comprobada ──"
instantanea "$T/restaurada"
ayuda comparar-restauracion "$COPIA/foto" "$T/restaurada"
(( FALLOS == 0 )) || { echo "Restauración con FAIL: revisá las líneas marcadas." >&2; exit 1; }
echo "Revertido: variables y código como antes de la Fase A. Las reseñas no cambian."
exit 0
