#!/usr/bin/env bash
# ORK3D · panel admin: verificación del rediseño en producción. SOLO LECTURA: GET al sitio y lecturas de AWS.
# No hace login, no envía POST, no crea reseñas, no envía correos y no escribe en AWS.
#
# pre  (antes de publicar): lectura de producción (infra/prod-lectura-v2.sh: /opiniones lanzada, /opinar, www, panel,
#      API sin cookie 401, export, notificaciones ON, logs sin cookies); panel actual (cabeceras, CSP, noindex, sin estilos
#      ni scripts inline); assets que la página actual referencia (200); ninguna reseña de prueba en el export; navegador
#      de solo lectura si Playwright está disponible.
#      Único hueco permitido en pre: /assets/css/admin.css?v=1 responde 404 porque el archivo nunca se publicó. Se acepta
#      solo si se cumplen TODAS las condiciones de evaluar_hueco_css, y se informa como KNOWN GAP (nunca como PASS silencioso).
# post (después de publicar): lo anterior, y además: HTML y assets servidos = commit 590ed7d byte a byte; assets con
#      ?v=2 y sus Content-Type y Cache-Control; sección, indicador de sesión, "Publicada", slug distinto del nombre
#      visible; innerHTML y scripts inline; API 401.
#
# Variables: SMOKE_NOTIFY_ID (obligatoria): id de la reseña de prueba de notificaciones.
#            SMOKE_IDS (opcional, separadas por coma; por defecto, las dos históricas).
#            ADMIN_UI_REINTENTOS (3) y ADMIN_UI_ESPERA (10 s): reintentos de la comparación con el commit (caché de CloudFront).
# Códigos: 0 todo PASS · 1 algún FAIL · 2 uso o entorno inválido.
# Playwright ausente no es un FAIL: se informa como INFO (código 3 de admin-ui-navegador.js).
set -uo pipefail

MODO="${1:-}"
case "$MODO" in
  pre|post) ;;
  *) echo "uso: $0 pre|post" >&2; exit 2 ;;
esac
[[ -n "${SMOKE_NOTIFY_ID:-}" ]] || { echo "ERROR: falta SMOKE_NOTIFY_ID: sin el id de la reseña de prueba de notificaciones no se puede confirmar que no esté publicada." >&2; exit 2; }
[[ "$SMOKE_NOTIFY_ID" =~ ^[0-9a-f]{32}$ ]] || { echo "ERROR: SMOKE_NOTIFY_ID debe tener 32 caracteres hexadecimales." >&2; exit 2; }
SMOKE_IDS="${SMOKE_IDS:-633729550934477689fafb8f576dd851,d1cc45e8f15a425890e2a3a8cbbc9ebd}"
REINTENTOS="${ADMIN_UI_REINTENTOS:-3}"
ESPERA="${ADMIN_UI_ESPERA:-10}"

source "$(cd "$(dirname "$0")" && pwd)/admin-ui-comun.sh"
LECTURA="$ROOT/infra/prod-lectura-v2.sh"
LECTURA_AYUDA="$ROOT/infra/prod_lectura_ayuda.py"
T="$(mktemp -d)"
chmod 700 "$T"
trap 'rm -rf "$T"' EXIT
FALLOS=0
CSS_REFERENCIADO=0      # 1 si una página actual referencia /assets/css/admin.css?v=1
CSS_URL=""
CSS_CODIGO=""
CSS_MANIFIESTO_OK=0     # el manifiesto contiene assets/css/admin.css exactamente una vez
CSS_BLOB_OK=0           # el CSS local es el blob de REVISION_REVISADA
HUECO_ACEPTADO=0

paso() { echo; echo "── $* ──"; }

control() {  # NOMBRE CODIGO [DETALLE]: 0 = PASS
  if [[ "$2" == 0 ]]; then
    echo "PASS  $1"
  else
    echo "FAIL  $1${3:+ — $3}"
    FALLOS=$((FALLOS + 1))
  fi
}

# Comando de admin_ui_ayuda.py: su salida ya trae PASS: o FAIL:; aquí se muestra igual, con dos espacios.
ayuda_control() {
  local salida rc
  salida="$(python3 "$AYUDA" "$@" 2>&1)"
  rc=$?
  printf '%s\n' "$salida" | sed -e 's/^PASS: /PASS  /' -e 's/^FAIL: /FAIL  /'
  (( rc == 0 )) || FALLOS=$((FALLOS + 1))
}

obtener() {  # NOMBRE RUTA_CON_QUERY: GET de producción. Cabeceras en $T/NOMBRE.h, cuerpo en $T/NOMBRE.b. Imprime el código.
  local codigo
  codigo="$("$CURL" -sS --max-time 20 -D "$T/$1.h" -o "$T/$1.b" -w '%{http_code}' "$BASE$2" 2> "$T/$1.err" < /dev/null)" || codigo=000
  echo "${codigo:-000}"
}

tipo_de() {  # NOMBRE: Content-Type sin parámetros, en minúsculas
  grep -i '^content-type:' "$T/$1.h" 2> /dev/null | tail -1 | cut -d: -f2- | cut -d';' -f1 | tr -d ' \r' | tr '[:upper:]' '[:lower:]'
}

cache_de() {  # NOMBRE: Cache-Control tal como llega
  grep -i '^cache-control:' "$T/$1.h" 2> /dev/null | tail -1 | cut -d: -f2- | tr -d '\r' | sed 's/^ //'
}

# Content-Type que el manifiesto declara para un archivo, sin parámetros.
tipo_manifiesto() {  # ARCHIVO
  leer_manifiesto | awk -F'\t' -v a="$1" '$1 == a {print $3}' | cut -d';' -f1 | tr -d ' ' | tr '[:upper:]' '[:lower:]'
}

# Compara el cuerpo servido con el blob de ARCHIVO en la revisión. En post reintenta: CloudFront puede tardar en servir
# la copia nueva tras la invalidación. Deja el cuerpo en $T/NOMBRE.b.
comparar_con_revision() {  # NOMBRE RUTA_CON_QUERY ARCHIVO
  local nombre="$1" url="$2" archivo="$3" intento=0 codigo esperado real
  esperado="$("$GIT" -C "$ROOT" show "$REVISION_REVISADA:$archivo" 2> /dev/null | sha256sum | cut -d' ' -f1)"
  while :; do
    codigo="$(obtener "$nombre" "$url")"
    real="$(sha256sum "$T/$nombre.b" 2> /dev/null | cut -d' ' -f1)"
    if [[ "$codigo" == 200 && "$real" == "$esperado" ]]; then
      echo "PASS  $url = $archivo en $REVISION_REVISADA (byte a byte)"
      return 0
    fi
    intento=$((intento + 1))
    if (( intento >= REINTENTOS )); then
      echo "FAIL  $url: código $codigo, sha ${real:0:12} distinto del ${esperado:0:12} de $archivo en $REVISION_REVISADA"
      FALLOS=$((FALLOS + 1))
      return 1
    fi
    sleep "$ESPERA"
  done
}

# Único hueco permitido en pre. Cada condición es explícita; si una falla, es FAIL con su motivo. Sin bypass, sin --force.
#   1. el HTML actual de /admin/resenas o /admin/entrar referencia /assets/css/admin.css?v=1      (CSS_REFERENCIADO)
#   2. head-object confirma que assets/css/admin.css no existe en el bucket (404 explícito)      (existencia_s3 = no)
#   3. los demás assets actuales responden 200, y el resto del preflight pasa                    (FALLOS = 0)
#   4. el manifiesto contiene assets/css/admin.css exactamente una vez                            (CSS_MANIFIESTO_OK)
#   5. el CSS local es exactamente el blob revisado en 590ed7d                                    (CSS_BLOB_OK)
#   6. la respuesta de /assets/css/admin.css?v=1 es 404, no otro código                           (CSS_CODIGO)
evaluar_hueco_css() {
  local clave=assets/css/admin.css existencia
  if (( CSS_REFERENCIADO == 0 )); then
    control "el HTML actual referencia /assets/css/admin.css?v=1" 1 "ninguna de las páginas lo referencia"
    return 0
  fi
  if [[ "$CSS_CODIGO" == 200 ]]; then
    control "$CSS_URL responde 200: el archivo existe, no hay hueco" 0
    return 0
  fi
  existencia="$(existencia_s3 "$clave")"
  if [[ "$existencia" == error:* ]]; then
    control "head-object $clave no es concluyente" 1 "${existencia#error: }"
    return 0
  fi
  if [[ "$existencia" == si ]]; then
    control "$CSS_URL responde $CSS_CODIGO pero $clave existe en S3" 1
    return 0
  fi
  if [[ "$CSS_CODIGO" != 404 ]]; then
    control "$CSS_URL responde $CSS_CODIGO: sin archivo en S3 solo se acepta 404" 1
    return 0
  fi
  if (( CSS_MANIFIESTO_OK != 1 )); then
    control "$CSS_URL: hueco no aceptado, el manifiesto no contiene $clave exactamente una vez" 1
    return 0
  fi
  if (( CSS_BLOB_OK != 1 )); then
    control "$CSS_URL: hueco no aceptado, el CSS local no es el de $REVISION_REVISADA" 1
    return 0
  fi
  if (( FALLOS > 0 )); then
    control "$CSS_URL: hueco no aceptado mientras haya otros FAIL" 1
    return 0
  fi
  HUECO_ACEPTADO=1
  echo "KNOWN GAP  assets/css/admin.css?v=1 no existe en producción; será creado por este deploy"
}

echo "Verificación $MODO del panel admin contra $BASE (solo lectura)"

paso "0. Repo local: manifiesto y contenido revisado (ambos modos)"
n_css="$(leer_manifiesto | awk -F'\t' -v a=assets/css/admin.css '$1 == a' | wc -l | tr -d ' ')"
control "el manifiesto contiene assets/css/admin.css exactamente una vez" "$([[ "$n_css" == 1 ]]; echo $?)" "aparece $n_css veces"
[[ "$n_css" == 1 ]] && CSS_MANIFIESTO_OK=1
while IFS=$'\t' read -r archivo clave ct cc rutas; do
  if blob_coincide "$archivo"; then
    control "$archivo: contenido local = $REVISION_REVISADA" 0
    [[ "$archivo" == assets/css/admin.css ]] && CSS_BLOB_OK=1
  else
    control "$archivo: contenido local distinto de $REVISION_REVISADA" 1
  fi
done < <(leer_manifiesto)

paso "1. Lectura de producción (infra/prod-lectura-v2.sh: /opiniones, /opinar, www, panel, API, export, notificaciones, logs)"
if PROD_LECTURA_OPINIONES=lanzada PROD_LECTURA_NOTIFY_ESPERADO=true "$LECTURA" > "$T/lectura.txt" 2>&1; then
  control "lectura de producción completa" 0
else
  control "lectura de producción completa" 1 "revisá las líneas FAIL de abajo"
fi
sed 's/^/  /' "$T/lectura.txt"

paso "2. Export: ninguna reseña de prueba visible (solo se usan ids)"
IFS=',' read -r -a ids_prueba <<< "$SMOKE_IDS"
if [[ "$(obtener export /resenas/publicas.json)" == 200 ]]; then
  if python3 "$LECTURA_AYUDA" smoke-fuera "$T/export.b" "$SMOKE_NOTIFY_ID" "${ids_prueba[@]}" > "$T/smoke.txt"; then
    control "ninguna reseña de prueba aparece en el export" 0
  else
    control "ninguna reseña de prueba aparece en el export" 1
  fi
  sed 's/^/  /' "$T/smoke.txt"
else
  control "export público responde 200" 1
fi

paso "3. Panel: HTML servido, cabeceras, CSP, noindex, sin estilos ni scripts inline"
control "commit revisado $REVISION_REVISADA disponible en el repo" \
  "$("$GIT" -C "$ROOT" cat-file -e "$REVISION_REVISADA^{commit}" > /dev/null 2>&1; echo $?)"
for par in "resenas /admin/resenas admin-web/resenas.html" "entrar /admin/entrar admin-web/entrar.html"; do
  read -r nombre ruta archivo <<< "$par"
  codigo="$(obtener "$nombre" "$ruta")"
  control "$ruta responde 200" "$([[ "$codigo" == 200 ]]; echo $?)" "código $codigo"
  control "$ruta: text/html" "$([[ "$(tipo_de "$nombre")" == text/html ]]; echo $?)"
  control "$ruta: no-store (no se cachea)" "$([[ "$(cache_de "$nombre")" == *no-store* ]]; echo $?)"
  ayuda_control csp "$T/$nombre.b"
  ayuda_control noindex "$T/$nombre.b"
  ayuda_control sin-estilos "$T/$nombre.b"
  ayuda_control sin-scripts-inline "$T/$nombre.b"
  if [[ "$MODO" == post ]]; then
    comparar_con_revision "$nombre" "$ruta" "$archivo"
  fi
done

paso "4. Assets que referencia cada página"
python3 "$AYUDA" referencias "$T/resenas.b" > "$T/refs.txt"
python3 "$AYUDA" referencias "$T/entrar.b" >> "$T/refs.txt"
sort -u "$T/refs.txt" > "$T/refs-unicas.txt"
[[ -s "$T/refs-unicas.txt" ]] || control "las páginas referencian sus assets" 1 "ninguna referencia /assets/ encontrada"
while IFS= read -r url; do
  [[ -n "$url" ]] || continue
  ruta="${url%%\?*}"
  archivo="${ruta#/}"
  nombre="a_$(basename "$ruta")"
  if [[ "$MODO" == pre ]]; then
    # Baseline de producción: todo asset del panel que el HTML actual referencia tiene que ser ?v=1. Otra versión es FAIL.
    if [[ "$ruta" == /assets/css/admin* || "$ruta" == /assets/js/admin-* ]] && [[ "$url" != *'?v=1' ]]; then
      control "$url: versión inesperada en el HTML actual (pre espera ?v=1)" 1
      continue
    fi
    # admin.css?v=1 se evalúa aparte, en evaluar_hueco_css: su 404 puede ser el único hueco permitido.
    if [[ "$ruta" == /assets/css/admin.css ]]; then
      CSS_REFERENCIADO=1
      CSS_URL="$url"
      CSS_CODIGO="$(obtener "$nombre" "$url")"
      continue
    fi
  fi
  codigo="$(obtener "$nombre" "$url")"
  control "$url responde 200" "$([[ "$codigo" == 200 ]]; echo $?)" "código $codigo"
  if [[ "$MODO" == post ]]; then
    control "$url: versión ?v=2 (cache bust)" "$([[ "$url" == *'?v=2' ]]; echo $?)"
    tipo_esperado="$(tipo_manifiesto "$archivo")"
    if [[ -n "$tipo_esperado" ]]; then
      control "$url: Content-Type $tipo_esperado" "$([[ "$(tipo_de "$nombre")" == "$tipo_esperado" ]]; echo $?)"
    else
      # Solo admin-entrar.js: no está en el manifiesto porque no cambió; su tipo se comprueba igual.
      control "$url: Content-Type application/javascript" "$([[ "$(tipo_de "$nombre")" == application/javascript ]]; echo $?)"
    fi
    control "$url: no-store" "$([[ "$(cache_de "$nombre")" == *no-store* ]]; echo $?)"
    comparar_con_revision "$nombre" "$url" "$archivo"
  fi
done < "$T/refs-unicas.txt"

paso "5. Contenido servido (sección, sesión, etiqueta, invitación, innerHTML, API)"
if [[ "$MODO" == post ]]; then
  ayuda_control contiene "$T/resenas.b" 'id="seccion-actual"' 'id="estado-sesion"' \
    'Identificador del producto' 'Nombre visible del producto'
  ayuda_control contiene "$T/a_admin-comun.js.b" 'Publicada'
  shopt -s nullglob
  js_servidos=("$T"/a_admin-*.js.b)
  shopt -u nullglob
  if (( ${#js_servidos[@]} > 0 )); then
    ayuda_control sin-innerhtml "${js_servidos[@]}"
  else
    control "JS del panel servido para revisar innerHTML" 1
  fi
fi
codigo_api="$(obtener api /admin/api/sesion)"
control "API admin sin cookie responde 401 no_autenticado" \
  "$([[ "$codigo_api" == 401 ]] && grep -q no_autenticado "$T/api.b"; echo $?)" "código $codigo_api"

paso "6. Navegador de solo lectura (infra/admin-ui-navegador.js)"
if ! command -v node > /dev/null 2>&1; then
  echo "INFO  navegador omitido: node no está instalado (no es un fallo del despliegue)"
else
  node "$ROOT/infra/admin-ui-navegador.js" > "$T/nav.txt" 2>&1
  rc_nav=$?
  sed 's/^/  /' "$T/nav.txt"
  case "$rc_nav" in
    0) echo "PASS  navegador de solo lectura: sin errores, CSP ni desborde" ;;
    3) echo "INFO  navegador omitido: Playwright no está disponible (código 3; no es un fallo del despliegue)" ;;
    *) control "navegador de solo lectura" 1 "código $rc_nav" ;;
  esac
fi

if [[ "$MODO" == pre ]]; then
  paso "7. Hueco conocido: assets/css/admin.css?v=1"
  evaluar_hueco_css
fi

echo
echo "── Resultado ──"
if (( FALLOS == 0 )); then
  if (( HUECO_ACEPTADO == 1 )); then
    echo "Verificación $MODO: todo PASS, con 1 KNOWN GAP explícito (assets/css/admin.css: lo crea este deploy)."
  else
    echo "Verificación $MODO: todo PASS."
  fi
  exit 0
fi
echo "Verificación $MODO: $FALLOS control(es) en FAIL. No se cambió nada."
if [[ "$MODO" == post ]]; then
  echo "Si el panel no corresponde a $REVISION_REVISADA: revertir con ALLOW_ADMIN_UI_PUBLISH=1 infra/admin-ui-publicar.sh revertir"
fi
exit 1
