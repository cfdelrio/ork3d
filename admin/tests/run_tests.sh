#!/usr/bin/env bash
# Tests de admin/resenas.sh contra el shim local (admin/tests/aws_shim.py).
# No toca AWS: todo ocurre en un directorio temporal con datos sintéticos.
# Uso: bash admin/tests/run_tests.sh   (sale con 0 solo si todos los casos pasan)
set -euo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${TESTS_DIR}/../.." && pwd)"
CLI="${REPO}/admin/resenas.sh"

for dep in jq openssl python3; do
  command -v "$dep" >/dev/null 2>&1 || { echo "Falta la dependencia: ${dep}" >&2; exit 1; }
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FX="${WORK}/fx"
mkdir -p "$FX" "${WORK}/bin"

# Entorno aislado: nada de esto apunta a AWS real.
export SHIM_ROOT="${WORK}/root"
export AWS_CMD="python3 ${TESTS_DIR}/aws_shim.py"
export RESENAS_BUCKET=test-privado
export SITE_BUCKET=test-sitio
export CF_DISTRIBUTION_ID=TESTDIST
export SITE_URL=https://ork3d.com
export TOKEN_DAYS=60
export ADMIN_OUT_DIR="${WORK}/out"
mkdir -p "$SHIM_ROOT"

# Si hay ImageMagick real, usamos un stub que copia: el resultado queda determinista.
# Sin ImageMagick, la CLI corre su camino de fallback (copia con aviso).
if command -v magick >/dev/null 2>&1; then
  cat > "${WORK}/bin/magick" <<'EOF'
#!/usr/bin/env bash
# Stub de tests: "procesa" la foto copiando entrada -> salida.
cp "$1" "${@: -1}"
EOF
  chmod +x "${WORK}/bin/magick"
  export PATH="${WORK}/bin:${PATH}"
fi

# shellcheck source=fixtures.sh
source "${TESTS_DIR}/fixtures.sh"
fx_cargar

# ── Utilidades de test ──────────────────────────────────────────────────────
PASS=0
FAIL=0
pasa() { PASS=$((PASS+1)); printf 'PASS  %s\n' "$1"; }
falla() { FAIL=$((FAIL+1)); printf 'FAIL  %s  (%s)\n' "$1" "$2"; }
seccion() { printf '\n== %s\n' "$1"; }

# afirma NOMBRE MENSAJE_SI_FALLA CONDICION...   (la condición es cualquier comando)
afirma() {
  local nombre="$1" msg="$2"
  shift 2
  if "$@"; then pasa "$nombre"; else falla "$nombre" "$msg"; fi
}

# Ejecuta la CLI capturando código, salida y error. Deja RC, OUT y ERR.
cli() {
  RC=0
  OUT="$("$CLI" "$@" 2>"${WORK}/stderr.txt")" || RC=$?
  ERR="$(cat "${WORK}/stderr.txt")"
}

contiene() { grep -qF -- "$1" <<< "$2"; }
no_contiene() { ! grep -qF -- "$1" <<< "$2"; }
contar() {  # contar TEXTO CADENA -> apariciones
  local n
  n="$(grep -o -F -- "$1" <<< "$2" | wc -l)" || true
  printf '%s' "${n//[[:space:]]/}"
}
empieza_con() { [[ "$2" == "$1"* ]]; }
es_token() { [[ "$1" =~ ^[A-Za-z0-9_-]{32}$ ]]; }
epoch() { date -u -d "$1" +%s 2>/dev/null || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s; }
permisos() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }
jpeg_ok() { [[ "$(head -c 3 "$1" | od -An -tx1 | tr -d ' \n')" == "ffd8ff" ]]; }

# Estado del bucket: las versiones vigentes son archivos del shim (objeto = archivo).
ruta_priv() { printf '%s/%s/%s' "$SHIM_ROOT" "$RESENAS_BUCKET" "$1"; }
ruta_sitio() { printf '%s/%s/%s' "$SHIM_ROOT" "$SITE_BUCKET" "$1"; }
existe_priv() { test -f "$(ruta_priv "$1")"; }
no_existe_priv() { ! existe_priv "$1"; }
existe_sitio() { test -f "$(ruta_sitio "$1")"; }
campo_priv() { jq -r --arg c "$2" '.[$c] | tostring' "$(ruta_priv "$1")"; }
estado() { campo_priv "resenas/$1.json" status; }
meta_sitio() {  # meta_sitio CLAVE CAMPO  (metadatos del objeto en el bucket del sitio)
  $AWS_CMD s3api head-object --bucket "$SITE_BUCKET" --key "$1" | jq -r --arg c "$2" '.[$c] // "ausente"'
}
EXPORT_FILE="$(ruta_sitio resenas/publicas.json)"
en_export() { jq -e --arg id "$1" '[.items[].id] | index($id) != null' "$EXPORT_FILE" >/dev/null; }
item_export() { jq -r --arg id "$1" --arg c "$2" '.items[] | select(.id == $id) | .[$c] | tostring' "$EXPORT_FILE"; }
aprobada_ok() { [[ "$(estado "$1")" == approved && "$(campo_priv "resenas/$1.json" published_at)" != null ]]; }
invalidacion_registrada() { grep -qF '/resenas/*' "${SHIM_ROOT}/_calls.log"; }
puts_export() { grep -c 'resenas/publicas.json' "${SHIM_ROOT}/_aws_calls.log" || true; }

# Export: solo claves permitidas y sin ningún campo o dato privado.
claves_permitidas() {
  jq -e '[.items[] | keys[]] | unique
    | all(IN("id","rating","comment","aspects","display_name","category","product_label","verified","photo_url","published_at"))' \
    "$EXPORT_FILE" >/dev/null
}
export_limpio() {
  ! grep -Eq 'token_hash|order_ref|"contact|private_feedback|photo_key|"status"|email|tel[eé]fono|FEEDBACK-PRIVADO|example\.invalid|PEDIDO-FICTICIO' "$EXPORT_FILE"
}
orden_desc() { jq -e '[.items[].published_at] as $p | $p == ($p | sort | reverse)' "$EXPORT_FILE" >/dev/null; }
count_ok() { jq -e '.count == (.items | length)' "$EXPORT_FILE" >/dev/null; }
antes_ok() {  # antes_ok A B: A aparece antes que B en el export
  jq -e --arg a "$1" --arg b "$2" \
    '[.items[].id] as $ids | ($ids | index($a)) != null and ($ids | index($b)) != null and (($ids | index($a)) < ($ids | index($b)))' \
    "$EXPORT_FILE" >/dev/null
}

# Ningún token completo de fixtures ni de creaciones dentro del texto dado.
sin_tokens_completos() {
  local t
  for t in "${FX_TOKENS[@]}"; do
    if contiene "$t" "$1"; then return 1; fi
  done
  return 0
}

lista_pendientes_ok() {
  contiene "$RP_FOTO" "$OUT" && contiene "$RP_SIN_PERMISO" "$OUT" && contiene "$RP_TEXTO" "$OUT" \
    && no_contiene "$RA_OLD" "$OUT" && no_contiene "$RH_OCULTA" "$OUT" \
    && no_contiene "$RR_NUEVA" "$OUT" && no_contiene "$RJ_VIEJA" "$OUT"
}
pendientes_sin_privado() {
  no_contiene "Texto sintético" "$OUT" && no_contiene "FEEDBACK-PRIVADO" "$OUT"
}
mensaje_ok() { [[ "$(contar "/opinar/" "$OUT")" -eq 1 ]] && contiene "Hola, dejá tu opinión:" "$OUT"; }
fila_usada() { grep -F -- "$1" <<< "$OUT" | grep -q 'sí$'; }
sin_env() { ! find "$1" -name '.env' | grep -q .; }
link_de() { grep -oE 'https://[A-Za-z0-9./:_-]+/opinar/[A-Za-z0-9_-]+' <<< "$1" | sed -n '1p' || true; }
mensaje_envtest_ok() { [[ "$OUT" == *"https://envtest.example/opinar/"* ]]; }

# ── 1. token crear ──────────────────────────────────────────────────────────
seccion "1. token crear"
cli token crear --pedido PEDIDO-FICTICIO-100 --producto trofeos --categoria trofeos --producto-label "Trofeo de torneo"
afirma "token crear: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
ENLACE="$(link_de "$OUT")"
TOKEN="${ENLACE##*/}"
FX_TOKENS+=("$TOKEN")
TH_NUEVO="$(fx_hash "$TOKEN")"
afirma "token crear: el token tiene 32 caracteres base64url" "token inválido" es_token "$TOKEN"
afirma "token crear: el link empieza con SITE_URL/opinar/" "link=${ENLACE}" empieza_con "${SITE_URL}/opinar/" "$ENLACE"
afirma "token crear: el link aparece UNA sola vez en la salida" "apariciones=$(contar "/opinar/" "$OUT")" \
  test "$(contar "/opinar/" "$OUT")" -eq 1
afirma "token crear: el token completo aparece UNA sola vez en la salida" "apariciones=$(contar "$TOKEN" "$OUT")" \
  test "$(contar "$TOKEN" "$OUT")" -eq 1
afirma "token crear: el link NO contiene el pedido" "el link incluye el pedido" no_contiene "PEDIDO-FICTICIO-100" "$ENLACE"
afirma "token crear: tokens/<sha>.json existe en el bucket privado" "falta el registro" existe_priv "tokens/${TH_NUEVO}.json"
CREADO="$(campo_priv "tokens/${TH_NUEVO}.json" created_at)"
VENCE="$(campo_priv "tokens/${TH_NUEVO}.json" expires_at)"
afirma "token crear: expires_at = created_at + 60 días" "creado=${CREADO} vence=${VENCE}" \
  test "$(( $(epoch "$VENCE") - $(epoch "$CREADO") ))" -eq $((60*86400))
afirma "token crear: el registro guarda el pedido (solo interno)" "order_ref distinto" \
  test "$(campo_priv "tokens/${TH_NUEVO}.json" order_ref)" = PEDIDO-FICTICIO-100

cli token crear --pedido PEDIDO-FICTICIO-101 --producto trofeos --categoria trofeos --mensaje "Hola, dejá tu opinión: {link}"
afirma "token crear con --mensaje: {link} se reemplaza y el link aparece una vez" "mensaje mal armado" mensaje_ok
FX_TOKENS+=("$(link_de "$OUT" | sed 's|.*/opinar/||')")

cli token crear --pedido PEDIDO-FICTICIO-102 --producto trofeos
afirma "token crear sin --categoria: sale con 1" "RC=${RC}" test "$RC" -eq 1
cli token crear --pedido PEDIDO-FICTICIO-102 --producto trofeos --categoria trofeos --dias 0
afirma "token crear con --dias fuera de rango: sale con 1" "RC=${RC}" test "$RC" -eq 1

# ── 2. token revocar ────────────────────────────────────────────────────────
seccion "2. token revocar"
cli token revocar "$TOKEN"
afirma "token revocar: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "token revocar: revoked=true en el registro" "revoked=$(campo_priv "tokens/${TH_NUEVO}.json" revoked)" \
  test "$(campo_priv "tokens/${TH_NUEVO}.json" revoked)" = true
afirma "token revocar: revoked_at queda registrado" "revoked_at=null" \
  test "$(campo_priv "tokens/${TH_NUEVO}.json" revoked_at)" != null
cli token revocar "$(fx_tk 999)"
afirma "token revocar de un token inexistente: sale con 2" "RC=${RC}" test "$RC" -eq 2
cli token revocar "no-es-un-token"
afirma "token revocar con formato inválido: sale con 1" "RC=${RC}" test "$RC" -eq 1

# ── 3. token listar ─────────────────────────────────────────────────────────
seccion "3. token listar"
cli token listar
afirma "token listar: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "token listar: muestra los tokens registrados" "no aparece ningún hash" \
  test "$(contar "${TH1:0:12}" "$OUT")" -ge 1
afirma "token listar: marca USADO=sí para el token con claim" "fila del token con claim sin sí" fila_usada "${TH3:0:12}"
afirma "token listar: no imprime tokens completos" "se imprimió un token completo" sin_tokens_completos "$OUT"

# ── 4. pendientes ───────────────────────────────────────────────────────────
seccion "4. pendientes"
cli pendientes
afirma "pendientes: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "pendientes: lista solo las pending (con y sin permiso)" "listado incorrecto" lista_pendientes_ok
afirma "pendientes: no muestra comentarios ni feedback privado" "se filtró texto de la reseña" pendientes_sin_privado

# ── 5. aprobar sin permiso ──────────────────────────────────────────────────
seccion "5. aprobar sin permiso"
cli aprobar "$RP_SIN_PERMISO"
afirma "aprobar sin permiso: sale con 2" "RC=${RC}" test "$RC" -eq 2
afirma "aprobar sin permiso: el status no cambia" "status=$(estado "$RP_SIN_PERMISO")" \
  test "$(estado "$RP_SIN_PERMISO")" = pending

# ── 6. aprobar con permiso (con foto) ───────────────────────────────────────
seccion "6. aprobar con permiso y foto"
cli aprobar "$RP_FOTO"
afirma "aprobar con permiso: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "aprobar: status approved y published_at fijado" "status=$(estado "$RP_FOTO")" aprobada_ok "$RP_FOTO"
afirma "aprobar: photo_public_key = resenas/fotos/<id>.jpg" "photo_public_key=$(campo_priv "resenas/${RP_FOTO}.json" photo_public_key)" \
  test "$(campo_priv "resenas/${RP_FOTO}.json" photo_public_key)" = "resenas/fotos/${RP_FOTO}.jpg"
afirma "aprobar: la foto pública existe en el bucket del sitio" "falta la foto pública" \
  existe_sitio "resenas/fotos/${RP_FOTO}.jpg"
afirma "aprobar: foto pública con Content-Type image/jpeg" "ContentType=$(meta_sitio "resenas/fotos/${RP_FOTO}.jpg" ContentType)" \
  test "$(meta_sitio "resenas/fotos/${RP_FOTO}.jpg" ContentType)" = image/jpeg
afirma "aprobar: foto pública con Cache-Control inmutable" "CacheControl=$(meta_sitio "resenas/fotos/${RP_FOTO}.jpg" CacheControl)" \
  test "$(meta_sitio "resenas/fotos/${RP_FOTO}.jpg" CacheControl)" = "public, max-age=31536000, immutable"
afirma "aprobar: el export incluye la reseña" "falta en el export" en_export "$RP_FOTO"
afirma "aprobar: el export incluye photo_url de la foto pública" "photo_url=$(item_export "$RP_FOTO" photo_url)" \
  test "$(item_export "$RP_FOTO" photo_url)" = "/resenas/fotos/${RP_FOTO}.jpg"
afirma "aprobar: el export no contiene campos ni datos privados" "hay campos o datos prohibidos" export_limpio
afirma "aprobar: el export solo tiene las claves permitidas" "hay claves no permitidas" claves_permitidas
afirma "aprobar: se registró la invalidación de /resenas/*" "no está en _calls.log" invalidacion_registrada

# ── 7. ocultar ──────────────────────────────────────────────────────────────
seccion "7. ocultar"
cli ocultar "$RP_FOTO"
afirma "ocultar: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "ocultar: status hidden" "status=$(estado "$RP_FOTO")" test "$(estado "$RP_FOTO")" = hidden
afirma "ocultar: la reseña sale del export" "sigue en el export" no_contiene "$RP_FOTO" "$(cat "$EXPORT_FILE")"
afirma "ocultar: la foto pública se borra del sitio" "la foto pública sigue" test ! -f "$(ruta_sitio "resenas/fotos/${RP_FOTO}.jpg")"
afirma "ocultar: la foto privada se conserva" "se borró la privada" existe_priv "$FOTO_1"

# ── 8. restaurar ────────────────────────────────────────────────────────────
seccion "8. restaurar"
cli restaurar "$RP_FOTO"
afirma "restaurar: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "restaurar: vuelve a approved" "status=$(estado "$RP_FOTO")" test "$(estado "$RP_FOTO")" = approved
afirma "restaurar: la foto pública vuelve a existir" "falta la foto pública" existe_sitio "resenas/fotos/${RP_FOTO}.jpg"
afirma "restaurar: reaparece en el export con su foto" "no está en el export" \
  test "$(item_export "$RP_FOTO" photo_url)" = "/resenas/fotos/${RP_FOTO}.jpg"
cli restaurar "$RP_FOTO"
afirma "restaurar sobre una reseña que no está oculta: sale con 2" "RC=${RC}" test "$RC" -eq 2

# ── 9. rechazar ─────────────────────────────────────────────────────────────
seccion "9. rechazar"
EXPORTS_ANTES="$(puts_export)"
cli rechazar "$RP_TEXTO"
afirma "rechazar pending: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "rechazar pending: status rejected" "status=$(estado "$RP_TEXTO")" test "$(estado "$RP_TEXTO")" = rejected
afirma "rechazar pending: no regenera el export" "se subió el export" test "$(puts_export)" -eq "$EXPORTS_ANTES"
afirma "export: el modo anónimo muestra 'Cliente de ORK3D'" "display_name=$(item_export "$RA_ANON" display_name)" \
  test "$(item_export "$RA_ANON" display_name)" = "Cliente de ORK3D"
cli rechazar "$RA_ANON"
afirma "rechazar approved: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "rechazar approved: sale del export" "sigue en el export" no_contiene "$RA_ANON" "$(cat "$EXPORT_FILE")"

# ── 10. verificar ───────────────────────────────────────────────────────────
seccion "10. verificar"
cli verificar "$RA_NEW"
afirma "verificar: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "verificar: verified=true en la reseña" "verified=$(campo_priv "resenas/${RA_NEW}.json" verified)" \
  test "$(campo_priv "resenas/${RA_NEW}.json" verified)" = true
afirma "verificar: verified=true en el token asociado" "verified=$(campo_priv "tokens/${TH5}.json" verified)" \
  test "$(campo_priv "tokens/${TH5}.json" verified)" = true
afirma "verificar: el export refleja verified=true" "verified en export distinto" \
  test "$(item_export "$RA_NEW" verified)" = true

# ── 11. borrar --foto --yes ─────────────────────────────────────────────────
seccion "11. borrar --foto"
cli borrar "$RA_FOTO" --foto --yes
afirma "borrar --foto --yes: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "borrar --foto: quita la foto privada" "la foto privada sigue" no_existe_priv "$FOTO_2"
afirma "borrar --foto: quita la foto pública" "la foto pública sigue" test ! -f "$(ruta_sitio "resenas/fotos/${RA_FOTO}.jpg")"
afirma "borrar --foto: photo_key y photo_public_key quedan en null" "campos no nulos" \
  test "$(campo_priv "resenas/${RA_FOTO}.json" photo_key)$(campo_priv "resenas/${RA_FOTO}.json" photo_public_key)" = nullnull
afirma "borrar --foto: el texto de la reseña se conserva" "se borró la reseña" existe_priv "resenas/${RA_FOTO}.json"

# ── 12. borrar --yes ────────────────────────────────────────────────────────
seccion "12. borrar"
printf 'no\n' | "$CLI" borrar "$RP_TEXTO" >/dev/null 2>&1 || true
afirma "borrar sin BORRAR como confirmación: cancela y no borra" "se borró igual" existe_priv "resenas/${RP_TEXTO}.json"
cli borrar "$RA_OLD" --yes
afirma "borrar --yes: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "borrar --yes: elimina resenas/<id>.json" "el registro sigue" no_existe_priv "resenas/${RA_OLD}.json"
afirma "borrar --yes: no queda rastro en el export" "quedó rastro en el export" \
  no_contiene "$RA_OLD" "$(cat "$EXPORT_FILE")"

# ── 13. mantenimiento ───────────────────────────────────────────────────────
seccion "13. mantenimiento"
cli mantenimiento --dry-run
afirma "mantenimiento --dry-run: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "mantenimiento --dry-run: lista la rechazada de 120 días" "no aparece la vieja" contiene "$RJ_VIEJA" "$OUT"
afirma "mantenimiento --dry-run: no lista la rechazada reciente" "aparece la reciente" no_contiene "$RR_NUEVA" "$OUT"
afirma "mantenimiento --dry-run: no borra nada" "se borró en dry-run" existe_priv "resenas/${RJ_VIEJA}.json"
cli mantenimiento --yes
afirma "mantenimiento --yes: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "mantenimiento --yes: borra la rechazada vieja" "sigue existiendo" no_existe_priv "resenas/${RJ_VIEJA}.json"
afirma "mantenimiento --yes: borra su foto privada" "la foto sigue" no_existe_priv "$FOTO_3"
afirma "mantenimiento --yes: conserva la rechazada reciente" "se borró la reciente" existe_priv "resenas/${RR_NUEVA}.json"

# ── 14. exportar ────────────────────────────────────────────────────────────
seccion "14. exportar"
cli exportar
afirma "exportar: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "exportar: ordena por published_at descendente" "orden incorrecto" orden_desc
afirma "exportar: count coincide con la cantidad de ítems" "count incorrecto" count_ok
afirma "exportar: la reseña más nueva va antes que la más vieja" "orden entre reseñas incorrecto" \
  antes_ok "$RA_FOTO" "$RA_NEW"

# ── 15. backup ──────────────────────────────────────────────────────────────
seccion "15. backup"
BACKUP_DIR="${WORK}/backup"
cli backup "$BACKUP_DIR"
afirma "backup: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "backup: copia las reseñas del bucket privado" "falta resenas/<id>.json" \
  test -f "${BACKUP_DIR}/resenas/${RA_NEW}.json"
afirma "backup: copia los tokens" "falta tokens/<hash>.json" test -f "${BACKUP_DIR}/tokens/${TH5}.json"
afirma "backup: no incluye archivos .env" "se copió un .env" sin_env "$BACKUP_DIR"

# ── 16. seguridad y ver ─────────────────────────────────────────────────────
seccion "16. seguridad"
cli ver "$RP_FOTO"
afirma "ver: sale con 0" "RC=${RC}: ${ERR}" test "$RC" -eq 0
afirma "ver: descarga la foto privada a ADMIN_OUT_DIR" "no se descargó la foto" \
  test -f "${ADMIN_OUT_DIR}/${RP_FOTO}-privada.jpg"
afirma "ver: la foto descargada tiene cabecera JPEG" "cabecera inválida" jpeg_ok "${ADMIN_OUT_DIR}/${RP_FOTO}-privada.jpg"
afirma "ver: la foto descargada tiene permisos 600" "permisos=$(permisos "${ADMIN_OUT_DIR}/${RP_FOTO}-privada.jpg")" \
  test "$(permisos "${ADMIN_OUT_DIR}/${RP_FOTO}-privada.jpg")" = 600

cli ver "$RP_WEBP"
afirma "ver: rechaza una clave WebP (el Lambda solo acepta JPEG)" "RC=${RC}" test "$RC" -eq 1
cli ver "$RP_CLAVE_MALA"
afirma "ver: rechaza una clave fuera del formato del Lambda" "RC=${RC}" test "$RC" -eq 1

TODA_LA_SALIDA=""
for comando in "pendientes" "token listar" "exportar" "mantenimiento --dry-run" "ver ${RA_NEW}"; do
  cli $comando
  TODA_LA_SALIDA+="${OUT}"$'\n'"${ERR}"$'\n'
done
afirma "seguridad: ningún comando imprime un token completo (salvo token crear)" "se filtró un token" \
  sin_tokens_completos "$TODA_LA_SALIDA"
afirma "seguridad: el log de llamadas AWS no contiene tokens" "un token llegó a AWS" \
  sin_tokens_completos "$(cat "${SHIM_ROOT}/_aws_calls.log")"
afirma "seguridad: el export final no contiene campos prohibidos" "hay campos o datos prohibidos" export_limpio

# ── 17. uso, códigos de salida y .env ───────────────────────────────────────
seccion "17. uso y configuración"
cli
afirma "sin argumentos: muestra uso y sale con 1" "RC=${RC}" test "$RC" -eq 1
cli ayuda
afirma "ayuda: sale con 1" "RC=${RC}" test "$RC" -eq 1
cli aprobar no-es-un-id
afirma "ID inválido: sale con 1" "RC=${RC}" test "$RC" -eq 1
cli ver "$(fx_id 9999)"
afirma "reseña inexistente: sale con 2" "RC=${RC}" test "$RC" -eq 2
cli comando-inexistente
afirma "comando desconocido: sale con 1" "RC=${RC}" test "$RC" -eq 1

mkdir -p "${WORK}/envtest"
cp "$CLI" "${WORK}/envtest/resenas.sh"
printf 'SITE_URL=https://envtest.example\n' > "${WORK}/envtest/.env"
OUT="$(env -u SITE_URL "${WORK}/envtest/resenas.sh" token crear --pedido PEDIDO-FICTICIO-200 --producto trofeos --categoria trofeos)" || true
afirma "admin/.env: se usa SITE_URL del archivo cuando no está en el entorno" "salida: ${OUT}" mensaje_envtest_ok

# ── Resumen ─────────────────────────────────────────────────────────────────
echo
echo "Resultado: ${PASS} PASS, ${FAIL} FAIL"
[[ "$FAIL" -eq 0 ]]
