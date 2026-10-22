#!/usr/bin/env bash
# ORK3D · V2 · publicación del panel admin: páginas, JS y reglas de /admin/entrar y /admin/resenas. Se ejecuta desde EC2.
#
# Escribe SOLO estas claves del bucket del sitio: admin-web/entrar.html, admin-web/resenas.html y assets/js/admin-*.js.
# Función CloudFront ork3d-redirect-www: se publica LIVE + bloque admin (la regla del panel insertada antes del rewrite
# de extensionless). Nunca se reemplaza LIVE por el repo: la propuesta se verifica (solo se agrega el bloque, no se quita
# ni cambia nada de LIVE) antes de cualquier escritura. Si LIVE no admite el bloque, no se escribe nada.
# Antes de escribir guarda rollback de la función LIVE, su metadata, la propuesta y las páginas que ya existían.
# Invalida solo las rutas del panel. Smoke de lectura al final: hash de cada página, no-store, API 401 y sitio público.
#
# Escribe en AWS SOLO con ALLOW_ADMIN_FRONTEND=1. Sin esa variable sale con 3 antes de llamar a AWS.
# Variables opcionales: ADMIN_DEPLOY_RUTA (rollback, /tmp/ork3d-v2-deploy), FRONT_REINTENTOS (6), FRONT_ESPERA (10 s).
# FRONT_SOLO_DIAGNOSTICO=1: solo lee LIVE, compara con el repo (diff semántico y textual) y sale sin escribir nada.
# Códigos: 0 completo o diagnóstico · 1 precheck o paso falló · 2 variable inválida · 3 falta ALLOW_ADMIN_FRONTEND=1 ·
#          4 LIVE no admite el bloque admin (o la propuesta cambia algo de LIVE) · 6 el smoke no coincide.
set -uo pipefail

SOLO_DIAGNOSTICO="${FRONT_SOLO_DIAGNOSTICO:-}"
if [[ "$SOLO_DIAGNOSTICO" != 1 ]]; then
  [[ "${ALLOW_ADMIN_FRONTEND:-}" == "1" ]] || {
    echo "ABORTA: este script publica el panel en el sitio y actualiza una función de CloudFront. Para ejecutarlo: ALLOW_ADMIN_FRONTEND=1 infra/admin-frontend-deploy.sh (solo lectura: FRONT_SOLO_DIAGNOSTICO=1 infra/admin-frontend-deploy.sh)" >&2
    exit 3
  }
fi

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

paso "2. Función CloudFront $FN: LIVE como fuente, estado, diferencias y propuesta"
# LIVE es la fuente: el código que sirve producción. Su ETag no se usa para escribir: UpdateFunction exige el de DEVELOPMENT.
"$AWS" cloudfront describe-function --name "$FN" --stage LIVE --output json > "$T/fn-meta-live.json" 2> "$T/fm.err" \
  || fallo "no se pudo leer la metadata de LIVE de $FN (código $(detalle "$T/fm.err"))"
"$AWS" cloudfront get-function --name "$FN" --stage LIVE "$T/fn-live.js" > /dev/null 2> "$T/fg.err" \
  || fallo "no se pudo leer el código LIVE de $FN (código $(detalle "$T/fg.err"))"
guardar_rollback cloudfront-funcion-admin-LIVE.js "$T/fn-live.js"
guardar_rollback cloudfront-funcion-admin-meta-LIVE.json "$T/fn-meta-live.json"
SHA_LIVE="$(sha256sum "$T/fn-live.js" | cut -d' ' -f1)"
ESTADO_FN="$(python3 "$AYUDA" estado "$T/fn-live.js" "$ROOT/infra/redirect-www.js")"
echo "  estado de LIVE frente al repo: $ESTADO_FN"
echo "  diferencias LIVE vs repo (semánticas; el diff textual completo queda en el archivo indicado):"
python3 "$AYUDA" diff "$T/fn-live.js" "$ROOT/infra/redirect-www.js" "$RB/cloudfront-funcion-admin-diferencias.$STAMP.txt" \
  || fallo "no se pudo comparar LIVE con el repo"
echo "  historial del repo frente a LIVE:"
python3 "$AYUDA" historial "$T/fn-live.js" "$ROOT" "$GIT" || true
case "$ESTADO_FN" in
  PUBLICADA) echo "  $FN ya contiene el bloque admin del repo: no hay nada que publicar en la función" ;;
  SIN_BLOQUE) echo "  $FN: se publica LIVE + bloque admin (ninguna regla de LIVE se quita ni cambia)" ;;
  INCOMPATIBLE:*) distinto "$FN no admite el bloque admin (${ESTADO_FN#INCOMPATIBLE: }): no se escribe nada" ;;
  *) distinto "estado inesperado de $FN: $ESTADO_FN" ;;
esac
if [[ "$ESTADO_FN" == SIN_BLOQUE ]]; then
  python3 "$AYUDA" inyectar "$T/fn-live.js" "$ROOT/infra/redirect-www.js" "$T/fn-propuesta.js" || fallo "no se pudo construir la propuesta"
  python3 "$AYUDA" verificar "$T/fn-live.js" "$T/fn-propuesta.js" "$ROOT/infra/redirect-www.js" \
    || distinto "la propuesta cambia algo de LIVE además del bloque admin: no se escribe nada"
  guardar_rollback cloudfront-funcion-admin-propuesta.js "$T/fn-propuesta.js"
  echo "  propuesta verificada: LIVE + bloque admin, sin eliminaciones"
fi

if [[ "$SOLO_DIAGNOSTICO" == 1 ]]; then
  echo
  echo "Diagnóstico de solo lectura: no se escribió nada en AWS ni en el sitio."
  echo "Diff completo: $RB/cloudfront-funcion-admin-diferencias.$STAMP.txt"
  exit 0
fi

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

paso "4. Escrituras de la función (solo si LIVE admite el bloque admin)"
if [[ "$ESTADO_FN" == SIN_BLOQUE ]]; then
  # a) Concurrencia: LIVE tiene que seguir siendo el que se usó para construir la propuesta. Si no, no se escribe nada.
  "$AWS" cloudfront get-function --name "$FN" --stage LIVE "$T/fn-live-2.js" > /dev/null 2> "$T/fg2.err" \
    || fallo "no se pudo releer LIVE antes de escribir (código $(detalle "$T/fg2.err"))"
  [[ "$(sha256sum "$T/fn-live-2.js" | cut -d' ' -f1)" == "$SHA_LIVE" ]] \
    || distinto "LIVE cambió desde el diagnóstico (otra publicación): no se escribió nada. Volvé a ejecutar el script."
  # b) DEVELOPMENT: se lee su código actual. Solo puede ser LIVE (lo normal) o la propuesta (una corrida previa).
  "$AWS" cloudfront get-function --name "$FN" --stage DEVELOPMENT "$T/fn-dev-1.js" > /dev/null 2> "$T/fd1.err" \
    || fallo "no se pudo leer DEVELOPMENT de $FN (código $(detalle "$T/fd1.err"))"
  BORRADOR="$(python3 "$AYUDA" borrador "$T/fn-dev-1.js" "$T/fn-live.js" "$T/fn-propuesta.js")"
  [[ "$BORRADOR" != DISTINTO ]] \
    || distinto "DEVELOPMENT tiene un código distinto de LIVE y de la propuesta: no se escribió nada (revisar la función en la consola)"
  echo "  DEVELOPMENT parte de: $BORRADOR"
  guardar_rollback cloudfront-funcion-admin-DEV-previo.js "$T/fn-dev-1.js"
  # c) ETag y FunctionConfig de DEVELOPMENT, leídos en este momento y no de corridas anteriores. Entre esta lectura y
  #    update-function no hay otra lectura de AWS.
  "$AWS" cloudfront describe-function --name "$FN" --stage DEVELOPMENT --output json > "$T/fn-dev-meta.json" 2> "$T/fdm.err" \
    || fallo "no se pudo leer la metadata de DEVELOPMENT (código $(detalle "$T/fdm.err"))"
  guardar_rollback cloudfront-funcion-admin-DEV-meta.json "$T/fn-dev-meta.json"
  python3 "$AYUDA" config-funcion "$T/fn-dev-meta.json" "$T/fn-config.json" || fallo "configuración de DEVELOPMENT no reconocida"
  "$AWS" cloudfront update-function --name "$FN" --if-match "$(valor "$T/fn-dev-meta.json" ETag)" \
    --function-config "file://$T/fn-config.json" --function-code "fileb://$T/fn-propuesta.js" \
    --output json > "$T/fn-upd.json" 2> "$T/fu.err" \
    || fallo "update-function ($(detalle "$T/fu.err")); DEVELOPMENT previo en $RB"
  echo "  DEVELOPMENT actualizado (update-function devolvió su nuevo ETag)"
  # d) Antes de publicar: ETag de DEVELOPMENT vigente, código y config tienen que ser exactamente los enviados.
  "$AWS" cloudfront describe-function --name "$FN" --stage DEVELOPMENT --output json > "$T/fn-dev-meta-2.json" 2> "$T/fdm2.err" \
    || fallo "no se pudo releer la metadata de DEVELOPMENT (código $(detalle "$T/fdm2.err"))"
  "$AWS" cloudfront get-function --name "$FN" --stage DEVELOPMENT "$T/fn-dev-2.js" > /dev/null 2> "$T/fd2.err" \
    || fallo "no se pudo releer el código de DEVELOPMENT (código $(detalle "$T/fd2.err"))"
  python3 "$AYUDA" verificar-dev "$T/fn-dev-meta-2.json" "$T/fn-dev-2.js" "$T/fn-propuesta.js" "$T/fn-config.json" \
    || fallo "DEVELOPMENT no quedó con la propuesta y la config enviadas: no se publica (DEVELOPMENT previo en $RB)"
  # e) PublishFunction copia DEVELOPMENT a LIVE con su ETag vigente. Nunca se usa el ETag de LIVE.
  "$AWS" cloudfront publish-function --name "$FN" --if-match "$(valor "$T/fn-dev-meta-2.json" ETag)" \
    --output json > "$T/fn-pub.json" 2> "$T/fp.err" \
    || fallo "publish-function ($(detalle "$T/fp.err")); DEVELOPMENT previo en $RB"
  # f) LIVE tras publicar: el candidato exacto, y quitar el bloque admin da el LIVE anterior byte a byte.
  "$AWS" cloudfront get-function --name "$FN" --stage LIVE "$T/fn-verif.js" > /dev/null 2> "$T/fv.err" \
    || fallo "no se pudo releer LIVE tras publicar (código $(detalle "$T/fv.err"))"
  cmp -s "$T/fn-verif.js" "$T/fn-propuesta.js" || fallo "LIVE no coincide con la propuesta tras publicar; restaurar desde $RB"
  python3 "$AYUDA" verificar "$T/fn-live.js" "$T/fn-verif.js" "$ROOT/infra/redirect-www.js" \
    || fallo "LIVE publicado cambia algo de LIVE anterior; restaurar desde $RB"
  echo "  $FN publicada: LIVE = LIVE anterior + bloque admin (verificado byte a byte)"
else
  echo "  $FN: sin cambios (ya contiene el bloque admin)"
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
echo "CHECKPOINT F2 — páginas y JS publicados; función $FN = LIVE + bloque admin"

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
