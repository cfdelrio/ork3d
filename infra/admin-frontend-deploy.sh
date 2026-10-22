#!/usr/bin/env bash
# ORK3D · V2 · publicación del panel admin: páginas, JS y reglas de /admin/entrar y /admin/resenas. Se ejecuta desde EC2.
#
# Escribe SOLO estas claves del bucket del sitio: admin-web/entrar.html, admin-web/resenas.html y assets/js/admin-*.js.
# Función CloudFront ork3d-redirect-www: se publica el repo solo si su código LIVE es el del repo sin el bloque admin
# (ver infra/admin_frontend_ayuda.py). Si difiere en otra cosa, no se escribe nada. Antes de cualquier escritura guarda
# copias de rollback de la función, de la metadata y de las claves que ya existían.
# Invalida solo las rutas del panel. Smoke de lectura al final: hash de cada página, no-store, API 401 y sitio público.
#
# Escribe en AWS SOLO con ALLOW_ADMIN_FRONTEND=1. Sin esa variable sale con 3 antes de llamar a AWS.
# Variables opcionales: ADMIN_DEPLOY_RUTA (rollback, /tmp/ork3d-v2-deploy), FRONT_REINTENTOS (6), FRONT_ESPERA (10 s).
# Códigos: 0 completo · 1 precheck o paso falló · 2 variable inválida · 3 falta ALLOW_ADMIN_FRONTEND=1 ·
#          4 la función LIVE difiere del repo en más que el bloque admin · 6 el smoke no coincide.
set -uo pipefail

[[ "${ALLOW_ADMIN_FRONTEND:-}" == "1" ]] || {
  echo "ABORTA: este script publica el panel en el sitio y actualiza una función de CloudFront. Para ejecutarlo: ALLOW_ADMIN_FRONTEND=1 infra/admin-frontend-deploy.sh" >&2
  exit 3
}

PERFIL="${AWS_PROFILE:-default}"; REGION="${AWS_REGION:-us-east-1}"
[[ "$PERFIL" == default ]] || { echo "ERROR: AWS_PROFILE solo puede ser default en este despliegue." >&2; exit 2; }
[[ "$REGION" == us-east-1 ]] || { echo "ERROR: AWS_REGION solo puede ser us-east-1 en este despliegue." >&2; exit 2; }
export AWS_PROFILE="$PERFIL" AWS_REGION="$REGION" AWS_PAGER=""

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AWS="${AWS_CLI:-aws}"
GIT="${GIT_CMD:-git}"
CURL="${CURL_CMD:-curl}"
AYUDA="$ROOT/infra/admin_frontend_ayuda.py"
AYUDA_DEPLOY="$ROOT/infra/admin_deploy_ayuda.py"
RAMA_ESPERADA="claude/ork3d-landing-page-b2fQr"
SITIO="ork3d.com"
BUCKET="ork3d.com"
DIST="ECOW0H6A48HVP"
FN="ork3d-redirect-www"
BASE="https://$SITIO"
RUTA="${ADMIN_DEPLOY_RUTA:-/tmp/ork3d-v2-deploy}"
RB="$RUTA/rollback"
REINTENTOS="${FRONT_REINTENTOS:-6}"
ESPERA="${FRONT_ESPERA:-10}"
STAMP="$(date -u +%Y%m%dT%H%M%S%6NZ)"

umask 077
mkdir -p "$RB" && chmod 700 "$RUTA" "$RB"
T="$(mktemp -d)"
chmod 700 "$T"
trap 'rm -rf "$T"' EXIT
LOG="$RUTA/frontend.$STAMP.log"
touch "$LOG" && chmod 600 "$LOG"
exec > >(tee -a "$LOG") 2>&1

fallo()    { echo "ERROR: $*" >&2; exit 1; }
distinto() { echo "ERROR: $*" >&2; exit 4; }
detalle()  { python3 "$AYUDA_DEPLOY" detalle "$1"; }
codigo()   { python3 -c 'import re, sys; m = re.search(r"An error occurred \((\w+)\)", open(sys.argv[1], encoding="utf-8", errors="replace").read()); print(m.group(1) if m else "desconocido")' "$1"; }
valor()    { python3 "$AYUDA_DEPLOY" get "$1" "$2"; }
paso()     { echo; echo "── $* ──"; }

guardar_rollback() {  # NOMBRE ARCHIVO: copia nueva con la marca de la ejecución; nunca sobrescribe
  local nombre="$1" origen="$2"
  local base="${nombre%.*}" ext="${nombre##*.}"
  local destino="$RB/$base.$STAMP.$ext"
  [[ ! -e "$destino" ]] || fallo "ya existe $destino; no se sobrescribe"
  cp "$origen" "$destino" && chmod 600 "$destino" || fallo "no se pudo guardar $nombre"
  echo "  rollback: $(basename "$destino") (600)"
}

PAGINAS=(
  "admin-web/entrar.html text/html; charset=utf-8"
  "admin-web/resenas.html text/html; charset=utf-8"
  "assets/js/admin-comun.js application/javascript; charset=utf-8"
  "assets/js/admin-entrar.js application/javascript; charset=utf-8"
  "assets/js/admin-panel.js application/javascript; charset=utf-8"
)

paso "1. Repositorio"
cd "$ROOT" || fallo "no se pudo entrar a la raíz del repo"
RAMA="$($GIT rev-parse --abbrev-ref HEAD 2> /dev/null)" || fallo "no es un repositorio git"
[[ "$RAMA" == "$RAMA_ESPERADA" ]] || fallo "rama $RAMA; se esperaba $RAMA_ESPERADA"
[[ -z "$($GIT status --porcelain)" ]] || fallo "árbol no limpio (revisá git status)"
[[ "$($GIT rev-parse HEAD)" == "$($GIT ls-remote origin "refs/heads/$RAMA_ESPERADA" 2> /dev/null | cut -f1)" ]] \
  || fallo "HEAD local distinto del remoto: hacé push o pull antes de publicar"
for f in "${PAGINAS[@]}"; do
  ruta="${f%% *}"
  [[ -s "$ROOT/$ruta" ]] || fallo "falta $ruta en el repo"
done
for pagina in admin-web/entrar.html admin-web/resenas.html; do
  grep -q 'name="robots" content="noindex, nofollow"' "$ROOT/$pagina" || fallo "$pagina no declara noindex"
done
python3 "$AYUDA" bloque-admin "$ROOT/infra/redirect-www.js" > /dev/null || fallo "infra/redirect-www.js no tiene el bloque admin completo"
echo "  rama $RAMA_ESPERADA · HEAD = remoto · páginas noindex · función del repo con bloque admin"

paso "2. Función CloudFront $FN (copias de rollback antes de escribir)"
"$AWS" cloudfront describe-function --name "$FN" --stage LIVE --output json > "$T/fn-meta.json" 2> "$T/fm.err" \
  || fallo "no se pudo leer la metadata de $FN (código $(detalle "$T/fm.err"))"
"$AWS" cloudfront get-function --name "$FN" --stage LIVE "$T/fn-live.js" > /dev/null 2> "$T/fg.err" \
  || fallo "no se pudo leer el código LIVE de $FN (código $(detalle "$T/fg.err"))"
guardar_rollback cloudfront-funcion-admin-LIVE.js "$T/fn-live.js"
guardar_rollback cloudfront-funcion-admin-meta.json "$T/fn-meta.json"
COMPARACION="$(python3 "$AYUDA" funcion "$T/fn-live.js" "$ROOT/infra/redirect-www.js")"
case "$COMPARACION" in
  IGUAL) echo "  $FN LIVE = repo: sin cambios" ;;
  ACTUALIZAR) echo "  $FN LIVE = repo sin el bloque admin: se publica el repo (solo se agregan las reglas del panel)" ;;
  *) distinto "$FN LIVE difiere del repo en más que el bloque admin: no se escribe nada (revisar el código publicado)" ;;
esac

paso "3. Copias de rollback de las páginas que ya existen"
for f in "${PAGINAS[@]}"; do
  clave="${f%% *}"
  nombre="s3-previo-$(echo "$clave" | tr '/' '_')"
  if "$AWS" s3 cp "s3://$BUCKET/$clave" "$T/previo" --only-show-errors > /dev/null 2> "$T/prev.err"; then
    guardar_rollback "$nombre" "$T/previo"
  else
    [[ "$(codigo "$T/prev.err")" == 404 ]] || fallo "no se pudo leer $clave para la copia de rollback (código $(detalle "$T/prev.err"))"
    echo "  $clave no existía: no hay copia previa"
  fi
done

paso "4. Escrituras"
if [[ "$COMPARACION" == ACTUALIZAR ]]; then
  python3 "$AYUDA" config-funcion "$T/fn-meta.json" "$T/fn-config.json" || fallo "configuración de $FN no reconocida"
  "$AWS" cloudfront update-function --name "$FN" --if-match "$(valor "$T/fn-meta.json" ETag)" \
    --function-config "file://$T/fn-config.json" --function-code "fileb://$ROOT/infra/redirect-www.js" \
    --output json > "$T/fn-upd.json" 2> "$T/fu.err" || fallo "update-function ($(detalle "$T/fu.err")); copia de rollback en $RB"
  "$AWS" cloudfront publish-function --name "$FN" --if-match "$(valor "$T/fn-upd.json" ETag)" \
    --output json > "$T/fn-pub.json" 2> "$T/fp.err" || fallo "publish-function ($(detalle "$T/fp.err")); copia de rollback en $RB"
  "$AWS" cloudfront get-function --name "$FN" --stage LIVE "$T/fn-verif.js" > /dev/null 2> "$T/fv.err" \
    || fallo "no se pudo releer el código LIVE tras publicar"
  [[ "$(python3 "$AYUDA" funcion "$T/fn-verif.js" "$ROOT/infra/redirect-www.js")" == IGUAL ]] \
    || fallo "LIVE no coincide con el repo tras publicar; restaurar desde $RB"
  echo "  $FN publicada: LIVE = repo"
fi
for f in "${PAGINAS[@]}"; do
  clave="${f%% *}"; tipo="${f#* }"
  "$AWS" s3 cp "$ROOT/$clave" "s3://$BUCKET/$clave" --content-type "$tipo" --cache-control 'no-store, max-age=0' \
    --only-show-errors > /dev/null 2> "$T/cp.err" || fallo "s3 cp $clave ($(detalle "$T/cp.err"))"
  echo "  publicado: $clave"
done
python3 "$AYUDA" invalidacion "$T/invalidacion.json" "$STAMP" || fallo "no se pudo armar la invalidación"
"$AWS" cloudfront create-invalidation --distribution-id "$DIST" --invalidation-batch "file://$T/invalidacion.json" \
  --output json > "$T/inv.json" 2> "$T/inv.err" || fallo "create-invalidation ($(detalle "$T/inv.err"))"
echo "  invalidación del panel: $(valor "$T/inv.json" Invalidation.Id)"
echo "CHECKPOINT F2 — páginas y JS publicados; función $FN coincide con el repo"

paso "5. Smoke (solo lectura)"
comprueba_pagina() {  # RUTA ARCHIVO_DEL_REPO: 200, cuerpo idéntico al repo y no-store
  local ruta="$1" archivo="$2" intento=0 codigo
  while (( intento < REINTENTOS )); do
    codigo="$("$CURL" -sS --max-time 20 -D "$T/h.txt" -o "$T/cuerpo" -w '%{http_code}' "$BASE$ruta")" || codigo=0
    if [[ "$codigo" == 200 ]] \
       && [[ "$(sha256sum "$T/cuerpo" | cut -d' ' -f1)" == "$(sha256sum "$ROOT/$archivo" | cut -d' ' -f1)" ]] \
       && grep -qi '^cache-control: .*no-store' "$T/h.txt"; then
      echo "  $ruta: 200 · cuerpo = repo · no-store"
      return 0
    fi
    intento=$((intento + 1))
    sleep "$ESPERA"
  done
  echo "ERROR: $ruta → $codigo; no coincide con $archivo o falta no-store tras $REINTENTOS intentos" >&2
  return 1
}
comprueba_pagina /admin/entrar admin-web/entrar.html || exit 6
comprueba_pagina /admin/resenas admin-web/resenas.html || exit 6
comprueba_pagina /assets/js/admin-panel.js assets/js/admin-panel.js || exit 6
CODIGO_API="$("$CURL" -sS --max-time 20 -o "$T/api.body" -w '%{http_code}' "$BASE/admin/api/sesion")" || CODIGO_API=0
if [[ "$CODIGO_API" != 401 ]] || ! grep -q no_autenticado "$T/api.body"; then
  echo "ERROR: /admin/api/sesion sin cookie → $CODIGO_API (se esperaba 401 de la API)." >&2
  exit 6
fi
echo "  /admin/api/sesion: 401 de la API"
CODIGO_SITIO="$("$CURL" -sS --max-time 20 -o "$T/sitio.body" -w '%{http_code}' "$BASE/")" || CODIGO_SITIO=0
[[ "$CODIGO_SITIO" == 200 ]] || { echo "ERROR: / → $CODIGO_SITIO (el sitio público debe seguir en 200)." >&2; exit 6; }
echo "  /: 200 (sitio público sin cambios)"
echo "CHECKPOINT F3 — smoke del panel, de la API y del sitio público"
echo
echo "Pendiente, fuera de este script: iniciar sesión con el enlace del correo (acceso humano) y revisar la consola del navegador."
echo "Registro: $LOG"
exit 0
