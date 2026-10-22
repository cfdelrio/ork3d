#!/usr/bin/env bash
# ORK3D · V2 · verificación de lectura de producción. Se ejecuta desde EC2 (AWS_PROFILE=default).
#
# SOLO LECTURA: peticiones GET al sitio público y lecturas de AWS (el export, la configuración de la Lambda pública).
# No escribe nada en AWS, ni en el sitio, ni en el bucket. No usa tokens reales ni envía correos.
#
# Una línea por control (PASS/FAIL). Código 0 si todos pasan, 1 si alguno falla, 2 si no se pudo ejecutar.
# Variables opcionales: CURL_CMD, AWS_CLI (para pruebas), PROD_LECTURA_BASE (por defecto https://ork3d.com).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AYUDA="$ROOT/infra/prod_lectura_ayuda.py"
CURL="${CURL_CMD:-curl}"
AWS="${AWS_CLI:-aws}"
BASE="${PROD_LECTURA_BASE:-https://ork3d.com}"
export AWS_PROFILE="${AWS_PROFILE:-default}" AWS_REGION="${AWS_REGION:-us-east-1}" AWS_PAGER=""

DIR="$(mktemp -d)"
chmod 700 "$DIR"
trap 'rm -rf "$DIR"' EXIT
FALLOS=0
TOKEN_FALSO="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"   # forma de token, inexistente: no consume nada

control() {  # NOMBRE RESULTADO: PASS/FAIL en una línea
  local nombre="$1" resultado="$2"
  if [[ "$resultado" == PASS* ]]; then
    echo "PASS  $nombre — ${resultado#PASS: }"
  else
    echo "FAIL  $nombre — ${resultado#FAIL: }"
    FALLOS=$((FALLOS + 1))
  fi
}

get() {  # NOMBRE RUTA: GET y cabeceras en $DIR/NOMBRE.h; devuelve el código HTTP (000 si no hubo respuesta)
  local codigo
  codigo="$("$CURL" -sS --max-time 20 -D "$DIR/$1.h" -o "$DIR/$1.b" -w '%{http_code}' "$BASE$2" 2> "$DIR/$1.err" || true)"
  echo "${codigo:-000}"
}

tipo_de() {  # NOMBRE: content-type sin parámetros, en minúsculas
  grep -i '^content-type:' "$DIR/$1.h" | tail -1 | cut -d: -f2- | cut -d';' -f1 | tr -d ' \r' | tr '[:upper:]' '[:lower:]'
}

echo "Verificación de lectura de producción ($BASE). Solo GET y lecturas de AWS."
echo

# ── Sitio público ─────────────────────────────────────────────────────────────
CODIGO_HOME="$(get home /)"
control "home responde 200" "$([[ "$CODIGO_HOME" == 200 ]] && echo PASS || echo "FAIL: HTTP $CODIGO_HOME")"

CODIGO_OPINIONES="$(get opiniones /opiniones)"
control "/opiniones accesible directamente" "$([[ "$CODIGO_OPINIONES" == 200 ]] && echo PASS || echo "FAIL: HTTP $CODIGO_OPINIONES")"
control "/opiniones noindex, follow" "$(python3 "$AYUDA" robots "$DIR/opiniones.b" 'noindex, follow')"
control "home no enlaza /opiniones" "$(python3 "$AYUDA" sin-enlace "$DIR/home.b" /opiniones)"

CODIGO_SITEMAP="$(get sitemap /sitemap.xml)"
if [[ "$CODIGO_SITEMAP" == 200 ]]; then
  control "sitemap no lista /opiniones" "$(python3 "$AYUDA" sitemap-sin "$DIR/sitemap.b" /opiniones)"
else
  control "sitemap responde 200" "FAIL: HTTP $CODIGO_SITEMAP"
fi

CODIGO_OPINAR="$(get opinar "/opinar/$TOKEN_FALSO")"
control "/opinar/<token> responde la página pública (token inexistente, sin efectos)" \
  "$([[ "$CODIGO_OPINAR" == 200 && "$(tipo_de opinar)" == text/html ]] && echo PASS || echo "FAIL: HTTP $CODIGO_OPINAR $(tipo_de opinar)")"

REDIRECCION_WWW="$("$CURL" -sS --max-time 20 -o /dev/null -w '%{http_code} %{redirect_url}' "https://www.ork3d.com/" 2>/dev/null || true)"
REDIRECCION_WWW="${REDIRECCION_WWW:-000 }"
control "www redirige al apex con 301" \
  "$([[ "$REDIRECCION_WWW" == "301 https://ork3d.com/"* ]] && echo PASS || echo "FAIL: $REDIRECCION_WWW")"

# ── Panel admin: solo lo que es público por diseño ────────────────────────────
CODIGO_PANEL="$(get panel /admin/resenas)"
control "/admin/resenas responde 200 con noindex" \
  "$([[ "$CODIGO_PANEL" == 200 ]] && python3 "$AYUDA" robots "$DIR/panel.b" 'noindex, nofollow' || echo "FAIL: HTTP $CODIGO_PANEL")"
CODIGO_ENTRAR="$(get entrar /admin/entrar)"
control "/admin/entrar responde 200" "$([[ "$CODIGO_ENTRAR" == 200 ]] && echo PASS || echo "FAIL: HTTP $CODIGO_ENTRAR")"
CODIGO_API="$(get api /admin/api/sesion)"
control "API admin sin cookie responde 401 no_autenticado" \
  "$([[ "$CODIGO_API" == 401 ]] && grep -q no_autenticado "$DIR/api.b" && echo PASS || echo "FAIL: HTTP $CODIGO_API")"

# ── Export público y fotos ────────────────────────────────────────────────────
CODIGO_EXPORT="$(get export /resenas/publicas.json)"
if [[ "$CODIGO_EXPORT" == 200 ]]; then
  control "export público: solo la allowlist" "$(python3 "$AYUDA" exportar "$DIR/export.b")"
  CONTADOR=0
  while IFS= read -r url; do
    [[ -z "$url" ]] && continue
    CONTADOR=$((CONTADOR + 1))
    case "$url" in
      /*) ruta="$url" ;;
      *) ruta="/${url#https://ork3d.com/}" ;;
    esac
    codigo="$(get "foto$CONTADOR" "$ruta")"
    control "foto pública $CONTADOR responde 200 image/jpeg" \
      "$([[ "$codigo" == 200 && "$(tipo_de "foto$CONTADOR")" == image/jpeg ]] && echo PASS || echo "FAIL: HTTP $codigo $(tipo_de "foto$CONTADOR")")"
  done < <(python3 "$AYUDA" fotos "$DIR/export.b")
  [[ "$CONTADOR" -gt 0 ]] || echo "INFO  no hay reseñas con foto pública en el export: el control de fotos no aplica"
else
  control "export público responde 200" "FAIL: HTTP $CODIGO_EXPORT"
fi

# ── Notificaciones: lectura de la configuración de la Lambda pública (sin imprimir el resto) ──
if "$AWS" lambda get-function-configuration --function-name ork3d-resenas --output json > "$DIR/lambda.json" 2> "$DIR/lambda.err"; then
  control "RESENAS_NOTIFY_ENABLED apagado" "$(python3 "$AYUDA" flag-notificaciones "$DIR/lambda.json")"
else
  control "configuración de la Lambda pública legible" "FAIL: no se pudo leer la configuración (lectura, sin cambios)"
fi

echo
if [[ "$FALLOS" -eq 0 ]]; then
  echo "Resultado: todos los controles de lectura pasan."
  exit 0
fi
echo "Resultado: $FALLOS control(es) fallan. No se cambió nada: revisá cada FAIL antes de seguir."
exit 1
