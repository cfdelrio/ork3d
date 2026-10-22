#!/usr/bin/env bash
# ORK3D · /opiniones: verificación de producción. SOLO LECTURA: GET al sitio y lecturas de AWS.
#
# pre  (antes de publicar): /opiniones oculta (noindex), home sin enlace, sitemap sin entrada, notificaciones ON,
#      export sano y ninguna reseña de prueba visible. Si una reseña de prueba aparece en el export: DETENER.
# post (después de publicar): /opiniones lanzada (indexable, canonical, enlazada desde el footer, en el sitemap),
#      título y description, assets con su tipo, export sano, ninguna reseña de prueba visible, notificaciones ON
#      y logs limpios.
#
# Variables: SMOKE_NOTIFY_ID (obligatoria): id de la reseña de prueba de notificaciones (no se puede verificar sin ella).
#            SMOKE_IDS (opcional): ids de prueba separadas por coma. Por defecto, las dos históricas.
#            PROD_LECTURA_BASE (opcional): sitio a verificar; por defecto https://ork3d.com.
# Códigos: 0 todo PASS · 1 algún FAIL · 2 uso o entorno inválido.
set -uo pipefail

MODO="${1:-}"
case "$MODO" in
  pre|post) ;;
  *) echo "uso: $0 pre|post" >&2; exit 2 ;;
esac
[[ -n "${SMOKE_NOTIFY_ID:-}" ]] || { echo "ERROR: falta SMOKE_NOTIFY_ID: sin el id de la reseña de prueba de notificaciones no se puede confirmar que no esté publicada." >&2; exit 2; }
[[ "$SMOKE_NOTIFY_ID" =~ ^[0-9a-f]{32}$ ]] || { echo "ERROR: SMOKE_NOTIFY_ID debe tener 32 caracteres hexadecimales." >&2; exit 2; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AYUDA="$ROOT/infra/prod_lectura_ayuda.py"
BASE="${PROD_LECTURA_BASE:-https://ork3d.com}"
SMOKE_IDS="${SMOKE_IDS:-633729550934477689fafb8f576dd851,d1cc45e8f15a425890e2a3a8cbbc9ebd}"
export AWS_PROFILE="${AWS_PROFILE:-default}" AWS_REGION="${AWS_REGION:-us-east-1}" AWS_PAGER=""
T="$(mktemp -d)"
chmod 700 "$T"
trap 'rm -rf "$T"' EXIT
FALLOS=0

control() {  # NOMBRE CONDICION(0 = PASS): una línea
  if [[ "$2" == 0 ]]; then echo "PASS  $1"; else echo "FAIL  $1"; FALLOS=$((FALLOS + 1)); fi
}

echo "Verificación ${MODO} de /opiniones contra $BASE (solo lectura)"
echo

# 1) Todos los controles de lectura de producción, con el estado esperado para este momento.
if [[ "$MODO" == pre ]]; then
  ESTADO=oculta
else
  ESTADO=lanzada
fi
echo "── Lectura de producción (PROD_LECTURA_OPINIONES=$ESTADO) ──"
if PROD_LECTURA_OPINIONES="$ESTADO" PROD_LECTURA_NOTIFY_ESPERADO=true PROD_LECTURA_BASE="$BASE" \
    "$ROOT/infra/prod-lectura-v2.sh" > "$T/lectura.txt" 2>&1; then
  control "lectura de producción completa" 0
else
  control "lectura de producción completa" 1
fi
sed 's/^/  /' "$T/lectura.txt"
echo

# 2) Export: reseñas de prueba fuera. Se usan solo ids, no contenido.
echo "── Export público y reseñas de prueba ──"
if curl -sS --max-time 20 -o "$T/export.json" "$BASE/resenas/publicas.json"; then
  control "export público descargado" 0
  python3 "$AYUDA" smoke-fuera "$T/export.json" "$SMOKE_NOTIFY_ID" $(echo "$SMOKE_IDS" | tr ',' ' ') > "$T/smoke.txt"
  smoke_rc=$?
  sed 's/^/  /' "$T/smoke.txt"
  control "ninguna reseña de prueba aparece en el export" "$smoke_rc"
else
  control "export público descargado" 1
fi
echo

# 3) Página y assets (solo post: el HTML nuevo y sus recursos).
if [[ "$MODO" == post ]]; then
  echo "── Página y assets del lanzamiento ──"
  HTML="$(curl -sS --max-time 20 "$BASE/opiniones" || true)"
  printf '%s' "$HTML" | grep -q '<title>Opiniones de clientes | ORK3D</title>'; control "title correcto" $?
  printf '%s' "$HTML" | grep -q 'name="description" content="Opiniones de clientes de ORK3D'; control "description presente" $?
  printf '%s' "$HTML" | grep -q 'id="resenas-listado"'; control "contenedor de opiniones en la página" $?
  for ruta in /assets/css/opiniones.css?v=2 /assets/js/resenas-publicas.js?v=2; do
    cabeceras="$(curl -sS --max-time 20 -o /dev/null -w '%{http_code} %{content_type}' "$BASE$ruta" || true)"
    case "$cabeceras" in
      "200 text/css"*|"200 application/javascript"*|"200 text/javascript"*) control "asset $ruta" 0 ;;
      *) control "asset $ruta ($cabeceras)" 1 ;;
    esac
  done
  echo
fi

echo "── Resultado ──"
if (( FALLOS == 0 )); then
  echo "Verificación $MODO: todo PASS."
  exit 0
fi
echo "Verificación $MODO: $FALLOS control(es) en FAIL. Si una reseña de prueba aparece en el export: DETENER."
exit 1
