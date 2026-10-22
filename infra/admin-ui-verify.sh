#!/usr/bin/env bash
# ORK3D · panel admin: verificación del rediseño en producción. SOLO LECTURA: GET al sitio y lecturas de AWS.
# No hace login, no envía POST, no crea reseñas, no envía correos y no escribe en AWS.
#
# pre  (antes de publicar): lectura de producción (infra/prod-lectura-v2.sh: /opiniones lanzada, /opinar, www, panel,
#      API sin cookie 401, export, notificaciones ON, logs sin cookies); panel actual (cabeceras, CSP, noindex, sin estilos
#      ni scripts inline); assets que la página actual referencia (200); ninguna reseña de prueba en el export; navegador
#      de solo lectura si Playwright está disponible.
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

echo "Verificación $MODO del panel admin contra $BASE (solo lectura)"

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

echo
echo "── Resultado ──"
if (( FALLOS == 0 )); then
  echo "Verificación $MODO: todo PASS."
  exit 0
fi
echo "Verificación $MODO: $FALLOS control(es) en FAIL. No se cambió nada."
if [[ "$MODO" == post ]]; then
  echo "Si el panel no corresponde a $REVISION_REVISADA: revertir con ALLOW_ADMIN_UI_PUBLISH=1 infra/admin-ui-publicar.sh revertir"
fi
exit 1
