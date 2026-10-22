#!/usr/bin/env bash
# ORK3D · V2 · diagnóstico del 404 de POST /admin/api/sesion/solicitar sin Origin. Se ejecuta desde EC2.
#
# Solo lectura de AWS: CloudFront (configuración), la Function URL de la Lambda admin, los logs de la Lambda admin
# y, si se indica RESENAS_SITE_BUCKET, la política del bucket del sitio. Cuatro peticiones HTTP sin efectos: ninguna
# envía correo ni escribe datos. Ninguna llamada a AWS modifica nada.
#
# Escribe en DIAG_RUTA (por defecto /tmp/ork3d-v2-diagnostico, carpeta 700, archivos 600) y termina con una conclusión.
# Resumible: un paso ya hecho se reutiliza. DIAG_REPETIR=1 lo vuelve a hacer.
#
# Ejecución:  ALLOW_ADMIN_DIAGNOSTICO=1 infra/admin-diagnostico.sh
# Variables opcionales: RESENAS_SITE_BUCKET, DIAG_ESPERA (segundos antes de leer logs, 60), DIAG_PAUSA (entre
#   peticiones, 4), DIAG_RUTA, DIAG_REPETIR.
# Códigos: 0 concluido · 2 lectura fallida o variable inválida · 3 falta ALLOW_ADMIN_DIAGNOSTICO=1
set -uo pipefail

[[ "${ALLOW_ADMIN_DIAGNOSTICO:-}" == "1" ]] || {
  echo "ABORTA: el diagnóstico lee AWS y hace peticiones. Para ejecutarlo: ALLOW_ADMIN_DIAGNOSTICO=1 infra/admin-diagnostico.sh" >&2
  exit 3
}
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AYUDA="$ROOT/infra/admin_diagnostico_ayuda.py"
AWS="${AWS_CLI:-aws}"
CURL="${CURL_CMD:-curl}"
export AWS_PROFILE=default AWS_REGION=us-east-1 AWS_PAGER=""
DIST="ECOW0H6A48HVP"
FN_ADMIN="ork3d-resenas-admin"
LOG_GRUPO="/aws/lambda/ork3d-resenas-admin"
BASE="https://ork3d.com"
DIR="${DIAG_RUTA:-/tmp/ork3d-v2-diagnostico}"
ESPERA="${DIAG_ESPERA:-60}"
PAUSA="${DIAG_PAUSA:-4}"
REPETIR="${DIAG_REPETIR:-}"

mkdir -p "$DIR" && chmod 700 "$DIR"
STAMP="$(date -u +%Y%m%dT%H%M%S%6NZ)"
LOG="$DIR/diagnostico.$STAMP.log"
touch "$LOG" && chmod 600 "$LOG"
exec > >(tee -a "$LOG") 2>&1

paso() { echo; echo "── $* ──"; }
hecho() { [[ -s "$1" && -z "$REPETIR" ]]; }   # paso ya hecho y no se pidió repetir

ro_aws() {  # SERVICIO OPERACION ARGUMENTOS...: única puerta a AWS del diagnóstico. Solo lectura.
  case "$1 $2" in
    "cloudfront get-distribution-config"|"lambda get-function-url-config"|"logs filter-log-events"|"s3api get-bucket-policy") ;;
    *) echo "ERROR: operación fuera del diagnóstico de solo lectura: $1 $2" >&2; exit 2 ;;
  esac
  "$AWS" "$@" --output json
}

peticion() {  # NOMBRE METODO RUTA [CABECERA...]: una petición sin efectos. Guarda estado, cabeceras visibles y tipo.
  local nombre="$1" metodo="$2" ruta="$3"; shift 3
  local t0 t1 codigo
  t0="$(python3 -c 'import time; print(int(time.time() * 1000))')"
  codigo="$("$CURL" -sS --max-time 20 -D "$DIR/$nombre.h" -o "$DIR/$nombre.b" -w '%{http_code}' \
    -X "$metodo" "$@" "$BASE$ruta" 2> "$DIR/$nombre.err")" || codigo=0
  t1="$(python3 -c 'import time; print(int(time.time() * 1000))')"
  python3 "$AYUDA" peticion "$DIR/peticiones.json" "$nombre" "$codigo" "$DIR/$nombre.h" "$DIR/$nombre.b" "$t0" "$t1" \
    || { echo "ERROR: no se pudo registrar $nombre" >&2; exit 2; }
  echo "  $nombre: HTTP $codigo"
  sleep "$PAUSA"
}

paso "1. Peticiones sin efectos (cuatro; ninguna envía correo)"
if hecho "$DIR/peticiones.json"; then
  echo "  peticiones ya registradas en $DIR (DIAG_REPETIR=1 para repetirlas)"
else
  rm -f "$DIR/peticiones.json" "$DIR/logs.json"
  printf '{}' > "$DIR/vacio.json"
  HASH="$(sha256sum "$DIR/vacio.json" | cut -d' ' -f1)"
  peticion get-sin-cookie GET /admin/api/sesion
  peticion put-control PUT /admin/api/sesion -H 'Content-Type: application/json' -H 'X-Admin-Request: 1' \
    -H "x-amz-content-sha256: $HASH" --data-binary "@$DIR/vacio.json"
  peticion post-sin-origin POST /admin/api/sesion/solicitar -H 'Content-Type: application/json' \
    -H 'X-Admin-Request: 1' -H "x-amz-content-sha256: $HASH" --data-binary "@$DIR/vacio.json"
  peticion post-ruta-inexistente POST /admin/api/no-existe -H 'Content-Type: application/json' \
    -H 'X-Admin-Request: 1' -H "x-amz-content-sha256: $HASH" --data-binary "@$DIR/vacio.json"
fi

paso "2. Configuración (solo lectura)"
if hecho "$DIR/dist-config.json"; then
  echo "  distribución ya leída"
else
  ro_aws cloudfront get-distribution-config --id "$DIST" > "$DIR/dist-config.json" 2> "$DIR/dist.err" \
    || { python3 "$AYUDA" limpiar "$DIR/dist.err" >&2; echo "ERROR: no se pudo leer la distribución" >&2; exit 2; }
fi
ro_aws lambda get-function-url-config --function-name "$FN_ADMIN" > "$DIR/url.json" 2> "$DIR/url.err" \
  || { : > "$DIR/url.json"; echo "  Function URL: no se pudo leer (se informa sin AuthType)"; }
if [[ -n "${RESENAS_SITE_BUCKET:-}" ]]; then
  ro_aws s3api get-bucket-policy --bucket "$RESENAS_SITE_BUCKET" > "$DIR/bucket-policy.json" 2> "$DIR/bucket.err" \
    || { : > "$DIR/bucket-policy.json"; echo "  política del bucket: no se pudo leer"; }
else
  echo "  política del bucket: RESENAS_SITE_BUCKET no definido; se omite"
  rm -f "$DIR/bucket-policy.json"
fi
python3 "$AYUDA" config "$DIR/config-resumen.json" "$DIR/dist-config.json" "$DIR/url.json" \
  $( [[ -s "$DIR/bucket-policy.json" ]] && echo "$DIR/bucket-policy.json" ) || exit 2

paso "3. Invocaciones de la Lambda admin en la ventana de las peticiones"
if hecho "$DIR/logs.json"; then
  echo "  logs ya leídos"
else
  sleep "$ESPERA"
  read -r INICIO FIN < <(python3 "$AYUDA" ventana "$DIR/peticiones.json") || exit 2
  ro_aws logs filter-log-events --log-group-name "$LOG_GRUPO" --start-time "$INICIO" --end-time "$FIN" \
    --filter-pattern REPORT > "$DIR/logs.json" 2> "$DIR/logs.err" \
    || { python3 "$AYUDA" limpiar "$DIR/logs.err" >&2; echo "ERROR: no se pudieron leer los logs" >&2; exit 2; }
fi
python3 "$AYUDA" invocaciones "$DIR/peticiones.json" "$DIR/logs.json" || exit 2

paso "4. Conclusión"
python3 "$AYUDA" clasificar "$DIR/peticiones.json" "$DIR/config-resumen.json" "$DIR/conclusion.txt" || exit 2
echo
echo "Registro: $LOG"
echo "Conclusión: $DIR/conclusion.txt"
exit 0
