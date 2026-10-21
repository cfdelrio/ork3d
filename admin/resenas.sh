#!/usr/bin/env bash
# ORK3D · CLI de moderación de reseñas.
# Uso: admin/resenas.sh <comando> [args]   (ver ayuda: admin/resenas.sh ayuda)
#
# Bucket privado (RESENAS_BUCKET): tokens, claims, reseñas y fotos pendientes.
# Bucket público (SITE_BUCKET): export público y fotos aprobadas.
# Configuración: admin/.env (si existe) o variables de entorno.
#
# Códigos de salida: 0 ok · 1 uso incorrecto · 2 estado inválido o no encontrado · 3 error de AWS.

set -euo pipefail
# Archivos nuevos solo para el dueño: backups y fotos privadas no deben ser legibles por otros.
umask 077

ADMIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ────────────────────────────────────────────────────────────
# Configuración
# ────────────────────────────────────────────────────────────
# Las variables que ya vienen del entorno tienen prioridad sobre admin/.env.
if [[ -f "${ADMIN_DIR}/.env" ]]; then
  while IFS= read -r linea || [[ -n "$linea" ]]; do
    [[ "$linea" =~ ^[[:space:]]*(#|$) ]] && continue
    if [[ "$linea" =~ ^[[:space:]]*([A-Z_][A-Z0-9_]*)=(.*)$ ]]; then
      clave="${BASH_REMATCH[1]}"
      valor="${BASH_REMATCH[2]}"
      valor="${valor%\"}"; valor="${valor#\"}"
      valor="${valor%\'}"; valor="${valor#\'}"
      if [[ -z "${!clave+x}" ]]; then
        printf -v "$clave" '%s' "$valor"
        export "$clave"
      fi
    fi
  done < "${ADMIN_DIR}/.env"
fi

RESENAS_BUCKET="${RESENAS_BUCKET:-ork3d-resenas-privado}"
SITE_BUCKET="${SITE_BUCKET:-ork3d.com}"
CF_DISTRIBUTION_ID="${CF_DISTRIBUTION_ID:-}"
SITE_URL="${SITE_URL:-https://ork3d.com}"
TOKEN_DAYS="${TOKEN_DAYS:-60}"
AWS_CMD="${AWS_CMD:-aws}"
ADMIN_OUT_DIR="${ADMIN_OUT_DIR:-${ADMIN_DIR}/out}"

# ────────────────────────────────────────────────────────────
# Utilidades
# ────────────────────────────────────────────────────────────
err() { printf 'Error: %s\n' "$*" >&2; }
aviso() { printf 'Aviso: %s\n' "$*" >&2; }

# Ejecuta el comando AWS configurado (puede tener espacios: word splitting a propósito).
aws_cli() {
  # shellcheck disable=SC2086
  $AWS_CMD "$@"
}

ahora() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Suma (o resta, con signo) N días a una fecha ISO UTC. Prueba GNU y BSD/macOS.
# Uso: sumar_dias 2026-10-21T16:36:43Z +60   ·   sumar_dias "$(ahora)" -90
sumar_dias() {
  local base="$1" n="$2" out
  if out=$(date -u -d "${base} ${n} days" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null); then
    printf '%s' "$out"
  elif out=$(date -u -v"${n}"d -j -f '%Y-%m-%dT%H:%M:%SZ' "$base" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null); then
    printf '%s' "$out"
  else
    err "no pude calcular una fecha con este comando date."
    exit 1
  fi
}

# Token base64url de 32 caracteres (24 bytes aleatorios).
generar_token() {
  openssl rand -base64 24 | tr '+/' '-_' | tr -d '='
}

token_valido() {
  [[ "$1" =~ ^[A-Za-z0-9_-]{32}$ ]]
}

hash_token() {
  printf '%s' "$1" | openssl dgst -sha256 -r | cut -d' ' -f1
}

# Validaciones de identificadores (evitan inyección de rutas en las claves S3).
validar_id() {
  if [[ ! "$1" =~ ^[a-f0-9]{24}$ ]]; then
    err "ID de reseña inválido: '$1'."
    exit 1
  fi
}

validar_hash() {
  if [[ ! "$1" =~ ^[a-f0-9]{64}$ ]]; then
    err "hash de token inválido."
    exit 1
  fi
}

validar_slug() {
  if [[ ! "$1" =~ ^[a-z0-9-]+$ ]]; then
    err "slug inválido '$1' (usá minúsculas, números y guiones)."
    exit 1
  fi
}

# Directorio temporal propio de esta ejecución; se borra al salir.
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# Helpers de S3. Convención: "no existe" en head/get = código de salida != 0.
# Los errores de put/list los decide el llamador (normalmente exit 3).

# Descarga un objeto a un archivo temporal. Imprime la ruta; falla si no existe.
descargar_objeto() {  # descargar_objeto BUCKET KEY
  local destino
  destino="$(mktemp "${TMP_DIR}/obj.XXXXXX")"
  if aws_cli s3api get-object --bucket "$1" --key "$2" "$destino" >/dev/null 2>&1; then
    printf '%s' "$destino"
  else
    return 1
  fi
}

# Sube un archivo como objeto. Opciones: --crear (create-only), --cache C.
subir_objeto() {  # subir_objeto BUCKET KEY ARCHIVO TIPO [--crear] [--cache C]
  local bucket="$1" key="$2" archivo="$3" tipo="$4" crear="" cache=""
  shift 4
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --crear) crear=1; shift ;;
      --cache) cache="${2:-}"; shift 2 ;;
      *) return 1 ;;
    esac
  done
  local -a extra=()
  if [[ -n "$crear" ]]; then extra+=(--if-none-match '*'); fi
  if [[ -n "$cache" ]]; then extra+=(--cache-control "$cache"); fi
  aws_cli s3api put-object --bucket "$bucket" --key "$key" --body "$archivo" \
    --content-type "$tipo" ${extra[@]+"${extra[@]}"} >/dev/null 2>&1
}

# Devuelve 0 si el objeto existe.
existe_objeto() {  # existe_objeto BUCKET KEY
  aws_cli s3api head-object --bucket "$1" --key "$2" >/dev/null 2>&1
}

# Imprime las claves de un prefijo, una por línea. Falla si AWS falla.
listar_claves() {  # listar_claves BUCKET PREFIJO
  local salida
  salida="$(aws_cli s3api list-objects-v2 --bucket "$1" --prefix "$2" 2>/dev/null)" || return 1
  printf '%s' "$salida" | jq -r '(.Contents // [])[].Key'
}

# Escribe un JSON (ya armado en archivo) en el bucket privado, sobrescribiendo.
guardar_json_privado() {  # guardar_json_privado KEY ARCHIVO
  subir_objeto "$RESENAS_BUCKET" "$1" "$2" "application/json; charset=utf-8"
}

# ────────────────────────────────────────────────────────────
# Comandos: token
# ────────────────────────────────────────────────────────────
uso_token() {
  cat >&2 <<'EOF'
Uso:
  resenas.sh token crear --pedido REF --producto SLUG --categoria SLUG
                         [--producto-label "Texto"] [--dias N] [--mensaje "Texto"]
  resenas.sh token revocar TOKEN
  resenas.sh token listar
EOF
}

cmd_token_crear() {
  local pedido="" producto="" categoria="" etiqueta="" dias="$TOKEN_DAYS" mensaje=""
  local token="" hash="" creado="" vence="" archivo="" intento enlace texto
  while [[ $# -gt 0 ]]; do
    if [[ $# -lt 2 ]]; then err "falta el valor de '$1'."; uso_token; exit 1; fi
    case "$1" in
      --pedido) pedido="$2" ;;
      --producto) producto="$2" ;;
      --categoria) categoria="$2" ;;
      --producto-label) etiqueta="$2" ;;
      --dias) dias="$2" ;;
      --mensaje) mensaje="$2" ;;
      *) err "opción desconocida: '$1'."; uso_token; exit 1 ;;
    esac
    shift 2
  done

  if [[ -z "$pedido" || -z "$producto" || -z "$categoria" ]]; then
    err "faltan datos: --pedido, --producto y --categoria son obligatorios."
    uso_token; exit 1
  fi
  validar_slug "$producto"
  validar_slug "$categoria"
  [[ -n "$etiqueta" ]] || etiqueta="$producto"
  if [[ ! "$dias" =~ ^[0-9]+$ ]]; then err "--dias debe ser un número entero."; exit 1; fi
  dias=$((10#$dias))
  if (( dias < 1 || dias > 365 )); then err "--dias debe estar entre 1 y 365."; exit 1; fi

  # Create-only: si la clave ya existe (colisión, o error de AWS) se reintenta con otro token.
  for intento in 1 2 3; do
    token="$(generar_token)"
    if ! token_valido "$token"; then err "no pude generar un token válido."; exit 1; fi
    hash="$(hash_token "$token")"
    creado="$(ahora)"
    vence="$(sumar_dias "$creado" "+${dias}")"
    archivo="$(mktemp "${TMP_DIR}/token.XXXXXX")"
    jq -n --arg hash "$hash" --arg creado "$creado" --arg vence "$vence" \
      --arg pedido "$pedido" --arg producto "$producto" --arg etiqueta "$etiqueta" \
      --arg categoria "$categoria" '{
        v: 1, token_hash: $hash, created_at: $creado, expires_at: $vence,
        order_ref: $pedido, product_slug: $producto, product_label: $etiqueta,
        category: $categoria, verified: false, revoked: false, revoked_at: null
      }' > "$archivo"
    if subir_objeto "$RESENAS_BUCKET" "tokens/${hash}.json" "$archivo" \
        "application/json; charset=utf-8" --crear; then
      break
    fi
    if [[ "$intento" == 3 ]]; then
      err "no pude guardar el token en el bucket privado (colisión o error de AWS)."
      exit 3
    fi
  done

  # El link aparece UNA sola vez: dentro del mensaje de WhatsApp. Nunca se vuelve a mostrar.
  enlace="${SITE_URL%/}/opinar/${token}"
  if [[ -n "$mensaje" ]]; then
    texto="${mensaje//\{link\}/$enlace}"
    if [[ "$texto" != *"$enlace"* ]]; then texto="${texto}"$'\n'"${enlace}"; fi
  else
    texto="¡Hola! Gracias por tu compra en ORK3D. ¿Nos contás cómo te fue? Dejá tu opinión acá (vence en ${dias} días): ${enlace}"
  fi

  printf 'Token creado. Vence: %s\n' "$vence"
  printf 'Mensaje para WhatsApp (el link se muestra UNA sola vez; copialo ahora):\n'
  printf '%s\n' '------------------------------------------------------------'
  printf '%s\n' "$texto"
  printf '%s\n' '------------------------------------------------------------'
}

cmd_token_revocar() {
  local token="${1:-}" hash="" ruta="" nuevo=""
  if [[ -z "$token" ]]; then err "falta el TOKEN a revocar."; uso_token; exit 1; fi
  if ! token_valido "$token"; then err "el token no tiene un formato válido."; exit 1; fi
  hash="$(hash_token "$token")"
  if ! ruta="$(descargar_objeto "$RESENAS_BUCKET" "tokens/${hash}.json")"; then
    err "token no encontrado."
    exit 2
  fi
  nuevo="$(mktemp "${TMP_DIR}/token.XXXXXX")"
  jq --arg ahora "$(ahora)" '.revoked = true | .revoked_at = $ahora' "$ruta" > "$nuevo"
  if ! guardar_json_privado "tokens/${hash}.json" "$nuevo"; then
    err "no pude guardar el registro del token (error de AWS)."
    exit 3
  fi
  printf 'Token revocado (registro %s...).\n' "${hash:0:12}"
}

cmd_token_listar() {
  local claves="" clave="" hash="" ruta="" fila="" usado=""
  local etiqueta="" creado="" vence="" revocado=""
  if ! claves="$(listar_claves "$RESENAS_BUCKET" "tokens/")"; then
    err "no pude listar los tokens (error de AWS)."
    exit 3
  fi
  if [[ -z "$claves" ]]; then echo "No hay tokens registrados."; return 0; fi

  printf '%-12s  %-24s  %-20s  %-20s  %-9s  %s\n' "HASH" "PRODUCTO" "CREADO" "VENCE" "REVOCADO" "USADO"
  while IFS= read -r clave; do
    [[ "$clave" =~ ^tokens/[a-f0-9]{64}\.json$ ]] || continue
    hash="${clave#tokens/}"; hash="${hash%.json}"
    if ! ruta="$(descargar_objeto "$RESENAS_BUCKET" "$clave")"; then
      aviso "no pude leer el registro ${hash:0:12}..."
      continue
    fi
    fila="$(jq -r '[(.product_label // "-"), (.created_at // "-"), (.expires_at // "-"),
      (if .revoked then "sí" else "no" end)] | @tsv' "$ruta")"
    IFS=$'\t' read -r etiqueta creado vence revocado <<< "$fila"
    # El claim lo crea la Lambda al usar el link: la CLI solo lo consulta.
    if existe_objeto "$RESENAS_BUCKET" "claims/${hash}.json"; then usado="sí"; else usado="no"; fi
    printf '%-12s  %-24s  %-20s  %-20s  %-9s  %s\n' \
      "${hash:0:12}" "${etiqueta:0:24}" "$creado" "$vence" "$revocado" "$usado"
  done <<< "$claves"
}

cmd_token() {
  local sub="${1:-}"
  shift || true
  case "$sub" in
    crear) cmd_token_crear "$@" ;;
    revocar) cmd_token_revocar "$@" ;;
    listar) cmd_token_listar "$@" ;;
    *) uso_token; exit 1 ;;
  esac
}

# ────────────────────────────────────────────────────────────
# Helpers de reseñas, fotos y export
# ────────────────────────────────────────────────────────────
# Imprime la ruta local de una reseña. Devuelve 2 si no existe.
cargar_resena() {  # cargar_resena ID
  descargar_objeto "$RESENAS_BUCKET" "resenas/${1}.json" \
    || { err "no existe la reseña ${1}."; return 2; }
}

# Guarda un archivo JSON como registro de reseña. Devuelve 3 si falla AWS.
guardar_resena() {  # guardar_resena ID ARCHIVO
  guardar_json_privado "resenas/${1}.json" "$2" \
    || { err "no pude guardar la reseña ${1} (error de AWS)."; return 3; }
}

# Aplica un filtro jq a una reseña y la guarda. Uso: modificar_resena ID [args jq] FILTRO
modificar_resena() {
  local id="$1" ruta="" nuevo=""
  shift
  ruta="$(cargar_resena "$id")" || return 2
  nuevo="$(mktemp "${TMP_DIR}/resena.XXXXXX")"
  jq "$@" "$ruta" > "$nuevo"
  guardar_resena "$id" "$nuevo"
}

# Pide confirmación escribiendo BORRAR. Devuelve 0 solo si la respuesta es exactamente BORRAR.
confirmar() {  # confirmar MENSAJE
  local respuesta=""
  printf '%s\nEscribí BORRAR para confirmar (cualquier otra cosa cancela): ' "$1" >&2
  IFS= read -r respuesta || true
  [[ "$respuesta" == "BORRAR" ]]
}

# Las claves de foto vienen del registro: se validan antes de usarlas en S3.
validar_clave_foto() {  # validar_clave_foto CLAVE
  if [[ ! "$1" =~ ^pendientes/[a-f0-9]{64}/[A-Za-z0-9._-]+$ ]]; then
    err "clave de foto inválida en el registro de la reseña."
    exit 1
  fi
}

# Borra todas las versiones de una clave exacta (no borra otras claves con el mismo prefijo).
# Devuelve 0 también si no había nada; 3 si falla AWS.
borrar_todas_versiones() {  # borrar_todas_versiones BUCKET KEY
  local bucket="$1" key="$2" salida="" objetos="" archivo=""
  if ! salida="$(aws_cli s3api list-object-versions --bucket "$bucket" --prefix "$key" 2>/dev/null)"; then
    err "no pude listar versiones de un objeto (error de AWS)."
    return 3
  fi
  objetos="$(printf '%s' "$salida" | jq -c --arg k "$key" \
    '[((.Versions // []) + (.DeleteMarkers // []))[] | select(.Key == $k) | {Key: .Key, VersionId: .VersionId}]')"
  if [[ "$objetos" == "[]" ]]; then return 0; fi
  archivo="$(mktemp "${TMP_DIR}/del.XXXXXX")"
  jq -n --argjson o "$objetos" '{Objects: $o}' > "$archivo"
  if ! aws_cli s3api delete-objects --bucket "$bucket" --delete "file://${archivo}" >/dev/null 2>&1; then
    err "no pude borrar un objeto (error de AWS)."
    return 3
  fi
}

# Quita la foto pública de una reseña (no toca la privada).
borrar_foto_publica() {  # borrar_foto_publica ID
  borrar_todas_versiones "$SITE_BUCKET" "resenas/fotos/${1}.jpg"
}

# Procesa la foto privada (quita metadatos) y la sube al bucket del sitio.
# Imprime la clave pública. Devuelve 1 si la foto no sirve, 3 si falla AWS.
publicar_foto() {  # publicar_foto ID CLAVE_PRIVADA
  local id="$1" origen="" destino="" cabecera=""
  origen="$(descargar_objeto "$RESENAS_BUCKET" "$2")" || { err "no pude descargar la foto privada (error de AWS)."; return 3; }
  destino="${TMP_DIR}/${id}.jpg"
  if command -v magick >/dev/null 2>&1; then
    if ! magick "$origen" -strip "$destino" 2>/dev/null; then
      err "ImageMagick no pudo procesar la foto de la reseña ${id}."
      return 1
    fi
  else
    aviso "ImageMagick (magick) no está instalado: la foto de la reseña ${id} se publica SIN quitar metadatos (EXIF)."
    cp "$origen" "$destino"
  fi
  cabecera="$(head -c 3 "$destino" | od -An -tx1 | tr -d ' \n')"
  if [[ "$cabecera" != "ffd8ff" ]]; then
    err "la foto de la reseña ${id} no es un JPEG válido."
    return 1
  fi
  if ! subir_objeto "$SITE_BUCKET" "resenas/fotos/${id}.jpg" "$destino" "image/jpeg" \
      --cache "public, max-age=31536000, immutable"; then
    err "no pude subir la foto pública de la reseña ${id} (error de AWS)."
    return 3
  fi
  printf '%s' "resenas/fotos/${id}.jpg"
}

# Regenera resenas/publicas.json desde las reseñas approved + permission_to_publish, y sube al sitio.
# Solo se copian campos explícitos: ningún campo privado puede colarse en el export.
exportar_publicas() {
  local claves="" clave="" ruta="" archivo="" generado="" n=0
  local -a rutas=()
  if ! claves="$(listar_claves "$RESENAS_BUCKET" "resenas/")"; then
    err "no pude listar las reseñas (error de AWS)."
    return 3
  fi
  while IFS= read -r clave; do
    [[ "$clave" =~ ^resenas/[a-f0-9]{24}\.json$ ]] || continue
    if ! ruta="$(descargar_objeto "$RESENAS_BUCKET" "$clave")"; then
      err "no pude leer una reseña (error de AWS)."
      return 3
    fi
    rutas+=("$ruta")
    n=$((n+1))
  done <<< "$claves"

  generado="$(ahora)"
  archivo="$(mktemp "${TMP_DIR}/publicas.XXXXXX")"
  if (( n == 0 )); then
    jq -n --arg generado "$generado" '{generated_at: $generado, count: 0, items: []}' > "$archivo"
  else
    jq -s --arg generado "$generado" '
      [ .[]
        | select(.status == "approved" and .permission_to_publish == true)
        | {
            id: .id,
            rating: .rating,
            comment: .comment,
            aspects: (.aspects // []),
            display_name: (if .display_mode == "anonimo" then "Cliente anónimo" else (.display_name // "Cliente") end),
            category: .category,
            product_label: .product_label,
            verified: (.verified // false),
            photo_url: (if .photo_public_key then ("/resenas/fotos/" + .id + ".jpg") else null end),
            published_at: .published_at
          }
      ]
      | sort_by(.published_at // "") | reverse
      | {generated_at: $generado, count: length, items: .}
    ' ${rutas[@]+"${rutas[@]}"} > "$archivo"
  fi

  if ! subir_objeto "$SITE_BUCKET" "resenas/publicas.json" "$archivo" \
      "application/json; charset=utf-8" --cache "public, max-age=60, must-revalidate"; then
    err "no pude subir resenas/publicas.json al bucket del sitio (error de AWS)."
    return 3
  fi
  echo "Export regenerado: $(jq -r '.count' "$archivo") reseña(s) publicada(s)."
  invalidar_cdn
}

# Invalida /resenas/* en CloudFront. Sin CF_DISTRIBUTION_ID se omite con aviso.
invalidar_cdn() {
  if [[ -z "$CF_DISTRIBUTION_ID" ]]; then
    aviso "CF_DISTRIBUTION_ID no está definido: se omite la invalidación de CloudFront (/resenas/*)."
    return 0
  fi
  if ! aws_cli cloudfront create-invalidation --distribution-id "$CF_DISTRIBUTION_ID" \
      --paths '/resenas/*' >/dev/null 2>&1; then
    err "no pude crear la invalidación de CloudFront (el export ya se subió)."
    return 3
  fi
  echo "Invalidación de CloudFront creada (/resenas/*)."
}

# ────────────────────────────────────────────────────────────
# Comandos: pendientes, ver, exportar
# ────────────────────────────────────────────────────────────
cmd_pendientes() {
  local claves="" clave="" ruta="" lista="" total=0 id="" fecha="" rating="" categoria="" foto=""
  if [[ $# -gt 0 ]]; then err "pendientes no acepta argumentos."; exit 1; fi
  if ! claves="$(listar_claves "$RESENAS_BUCKET" "resenas/")"; then
    err "no pude listar las reseñas (error de AWS)."
    exit 3
  fi
  lista="$(mktemp "${TMP_DIR}/pendientes.XXXXXX")"
  while IFS= read -r clave; do
    [[ "$clave" =~ ^resenas/[a-f0-9]{24}\.json$ ]] || continue
    if ! ruta="$(descargar_objeto "$RESENAS_BUCKET" "$clave")"; then
      aviso "no pude leer una reseña (error de AWS)."
      continue
    fi
    # Solo metadatos: el comentario no se muestra en el listado.
    jq -r 'select(.status == "pending")
      | [.id, .created_at, (.rating | tostring), .category, (if .photo_key then "sí" else "no" end)]
      | @tsv' "$ruta" >> "$lista"
  done <<< "$claves"

  total="$(wc -l < "$lista" | tr -d ' ')"
  if [[ "$total" == 0 ]]; then echo "No hay reseñas pendientes."; return 0; fi
  printf '%-24s  %-20s  %-6s  %-18s  %s\n' "ID" "FECHA" "RATING" "CATEGORÍA" "FOTO"
  sort -t$'\t' -k2,2 "$lista" | while IFS=$'\t' read -r id fecha rating categoria foto; do
    printf '%-24s  %-20s  %-6s  %-18s  %s\n' "$id" "$fecha" "$rating" "$categoria" "$foto"
  done
  echo "Total: ${total} pendiente(s)."
}

cmd_ver() {
  local id="${1:-}" ruta="" token_hash="" tok="" photo_key="" ext="" destino="" abs=""
  if [[ -z "$id" ]]; then err "falta el ID de la reseña."; uso_completo; exit 1; fi
  validar_id "$id"
  ruta="$(cargar_resena "$id")" || exit 2
  jq . "$ruta"

  token_hash="$(jq -r '.token_hash // ""' "$ruta")"
  if [[ "$token_hash" =~ ^[a-f0-9]{64}$ ]]; then
    if tok="$(descargar_objeto "$RESENAS_BUCKET" "tokens/${token_hash}.json")"; then
      printf '\nPedido asociado (solo admin, nunca va al público): %s\n' "$(jq -r '.order_ref // "-"' "$tok")"
    else
      aviso "no encontré el token asociado a esta reseña."
    fi
  fi

  photo_key="$(jq -r '.photo_key // ""' "$ruta")"
  if [[ -n "$photo_key" ]]; then
    validar_clave_foto "$photo_key"
    ext="${photo_key##*.}"
    mkdir -p "$ADMIN_OUT_DIR"
    destino="${ADMIN_OUT_DIR}/${id}-privada.${ext}"
    if ! aws_cli s3api get-object --bucket "$RESENAS_BUCKET" --key "$photo_key" "$destino" >/dev/null 2>&1; then
      err "no pude descargar la foto privada (error de AWS)."
      exit 3
    fi
    abs="$(cd "$ADMIN_OUT_DIR" && pwd)"
    printf 'Foto privada descargada en: %s/%s-privada.%s\n' "$abs" "$id" "$ext"
  else
    echo "(sin foto)"
  fi
}

cmd_exportar() {
  if [[ $# -gt 0 ]]; then err "exportar no acepta argumentos."; exit 1; fi
  exportar_publicas || exit $?
}

# ────────────────────────────────────────────────────────────
# Comandos: moderación (aprobar, rechazar, ocultar, restaurar, verificar)
# ────────────────────────────────────────────────────────────
# Sin permiso de publicación no se publica nada (aprobar y restaurar).
exigir_permiso() {  # exigir_permiso RUTA ID
  if [[ "$(jq -r '.permission_to_publish' "$1")" != "true" ]]; then
    err "la reseña ${2} no tiene permiso para publicar (permission_to_publish=false)."
    return 2
  fi
}

# Publica una reseña (aprobar o restaurar): sube la foto si hay, guarda approved y exporta.
publicar_resena() {  # publicar_resena ID RUTA MODO   (MODO: aprobar | restaurar)
  local id="$1" ruta="$2" modo="$3" photo_key="" pk="" nuevo=""
  photo_key="$(jq -r '.photo_key // ""' "$ruta")"
  if [[ -n "$photo_key" ]]; then
    validar_clave_foto "$photo_key"
    pk="$(publicar_foto "$id" "$photo_key")" || return $?
  fi
  nuevo="$(mktemp "${TMP_DIR}/resena.XXXXXX")"
  jq --arg ahora "$(ahora)" --arg pk "$pk" --arg modo "$modo" '
    .status = "approved"
    | .moderated_at = $ahora
    | .published_at = (if $modo == "restaurar" then (.published_at // $ahora) else $ahora end)
    | .photo_public_key = (if $pk == "" then null else $pk end)' "$ruta" > "$nuevo"
  guardar_resena "$id" "$nuevo" || return 3
  exportar_publicas || return $?
}

aprobar_resena() {  # aprobar_resena ID
  local id="$1" ruta=""
  ruta="$(cargar_resena "$id")" || exit 2
  exigir_permiso "$ruta" "$id" || exit 2
  case "$(jq -r '.status' "$ruta")" in
    pending|hidden|rejected) ;;
    *) err "la reseña ${id} está en estado '$(jq -r '.status' "$ruta")'; aprobar solo acepta pending, hidden o rejected."
       exit 2 ;;
  esac
  publicar_resena "$id" "$ruta" aprobar || exit $?
  echo "Reseña ${id} aprobada y publicada."
}

rechazar_resena() {  # rechazar_resena ID
  local id="$1" ruta="" status=""
  ruta="$(cargar_resena "$id")" || exit 2
  status="$(jq -r '.status' "$ruta")"
  # Solo si estaba publicada hay algo que quitar del sitio.
  if [[ "$status" == "approved" ]]; then
    borrar_foto_publica "$id" || exit 3
  fi
  modificar_resena "$id" --arg ahora "$(ahora)" \
    '.status = "rejected" | .moderated_at = $ahora | .photo_public_key = null' || exit $?
  if [[ "$status" == "approved" ]]; then
    exportar_publicas || exit $?
  fi
  echo "Reseña ${id} rechazada."
}

ocultar_resena() {  # ocultar_resena ID
  local id="$1"
  cargar_resena "$id" >/dev/null || exit 2
  borrar_foto_publica "$id" || exit 3
  modificar_resena "$id" --arg ahora "$(ahora)" \
    '.status = "hidden" | .moderated_at = $ahora | .photo_public_key = null' || exit $?
  exportar_publicas || exit $?
  echo "Reseña ${id} oculta. Su foto pública se borró; la privada se conserva."
}

restaurar_resena() {  # restaurar_resena ID
  local id="$1" ruta="" status=""
  ruta="$(cargar_resena "$id")" || exit 2
  status="$(jq -r '.status' "$ruta")"
  if [[ "$status" != "hidden" ]]; then
    err "la reseña ${id} está en estado '${status}'; restaurar solo aplica a 'hidden'."
    exit 2
  fi
  exigir_permiso "$ruta" "$id" || exit 2
  publicar_resena "$id" "$ruta" restaurar || exit $?
  echo "Reseña ${id} restaurada y publicada de nuevo."
}

verificar_resena() {  # verificar_resena ID
  local id="$1" ruta="" token_hash="" ruta_tok="" nuevo_tok="" status=""
  ruta="$(cargar_resena "$id")" || exit 2
  token_hash="$(jq -r '.token_hash // ""' "$ruta")"
  status="$(jq -r '.status' "$ruta")"
  modificar_resena "$id" '.verified = true' || exit $?
  if [[ "$token_hash" =~ ^[a-f0-9]{64}$ ]]; then
    if ruta_tok="$(descargar_objeto "$RESENAS_BUCKET" "tokens/${token_hash}.json")"; then
      nuevo_tok="$(mktemp "${TMP_DIR}/token.XXXXXX")"
      jq '.verified = true' "$ruta_tok" > "$nuevo_tok"
      if ! guardar_json_privado "tokens/${token_hash}.json" "$nuevo_tok"; then
        err "no pude guardar el token asociado (error de AWS)."
        exit 3
      fi
    else
      aviso "no encontré el token asociado; la reseña quedó verificada igual."
    fi
  fi
  if [[ "$status" == "approved" ]]; then
    exportar_publicas || exit $?
  fi
  echo "Reseña ${id} verificada."
}

# ────────────────────────────────────────────────────────────
# Comandos: borrar, mantenimiento, backup
# ────────────────────────────────────────────────────────────
cmd_borrar() {
  local id="" solo_fotos="" yes="" ruta="" photo_key="" nuevo=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --foto) solo_fotos=1 ;;
      --yes) yes=1 ;;
      -*) err "opción desconocida: '$1'."; uso_completo; exit 1 ;;
      *)
        if [[ -n "$id" ]]; then err "demasiados argumentos."; uso_completo; exit 1; fi
        id="$1" ;;
    esac
    shift
  done
  if [[ -z "$id" ]]; then err "falta el ID de la reseña."; uso_completo; exit 1; fi
  validar_id "$id"
  ruta="$(cargar_resena "$id")" || exit 2
  photo_key="$(jq -r '.photo_key // ""' "$ruta")"
  if [[ -n "$photo_key" ]]; then validar_clave_foto "$photo_key"; fi

  if [[ -z "$yes" ]]; then
    if [[ -n "$solo_fotos" ]]; then
      confirmar "Se borran las FOTOS (privada y pública) de la reseña ${id}; el texto se conserva." \
        || { echo "Cancelado."; exit 0; }
    else
      confirmar "Se borra la reseña ${id} completa (texto y fotos)." \
        || { echo "Cancelado."; exit 0; }
    fi
  fi

  borrar_foto_publica "$id" || exit 3
  if [[ -n "$photo_key" ]]; then
    borrar_todas_versiones "$RESENAS_BUCKET" "$photo_key" || exit 3
  fi
  if [[ -n "$solo_fotos" ]]; then
    nuevo="$(mktemp "${TMP_DIR}/resena.XXXXXX")"
    jq '.photo_key = null | .photo_public_key = null' "$ruta" > "$nuevo"
    guardar_resena "$id" "$nuevo" || exit 3
    exportar_publicas || exit $?
    echo "Fotos de la reseña ${id} borradas (privada y pública). El texto se conserva."
  else
    borrar_todas_versiones "$RESENAS_BUCKET" "resenas/${id}.json" || exit 3
    exportar_publicas || exit $?
    echo "Reseña ${id} borrada. Export actualizado."
  fi
}

cmd_mantenimiento() {
  local dry="" yes="" claves="" clave="" ruta="" corte="" lista="" total=0
  local id="" creado="" pk="" borradas=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry=1 ;;
      --yes) yes=1 ;;
      *) err "opción desconocida: '$1'."; uso_completo; exit 1 ;;
    esac
    shift
  done

  corte="$(sumar_dias "$(ahora)" "-90")"
  if ! claves="$(listar_claves "$RESENAS_BUCKET" "resenas/")"; then
    err "no pude listar las reseñas (error de AWS)."
    exit 3
  fi
  lista="$(mktemp "${TMP_DIR}/mant.XXXXXX")"
  while IFS= read -r clave; do
    [[ "$clave" =~ ^resenas/[a-f0-9]{24}\.json$ ]] || continue
    if ! ruta="$(descargar_objeto "$RESENAS_BUCKET" "$clave")"; then
      aviso "no pude leer una reseña (error de AWS)."
      continue
    fi
    jq -r --arg corte "$corte" '
      select(.status == "rejected" and ((.created_at // "") < $corte))
      | [.id, .created_at, (.photo_key // "")] | @tsv' "$ruta" >> "$lista"
  done <<< "$claves"

  total="$(wc -l < "$lista" | tr -d ' ')"
  if [[ "$total" == 0 ]]; then
    echo "No hay reseñas rechazadas de más de 90 días."
    return 0
  fi
  echo "Reseñas rechazadas creadas antes de ${corte}:"
  while IFS=$'\t' read -r id creado pk; do
    printf '  %s  creada %s\n' "$id" "$creado"
  done < "$lista"
  if [[ -n "$dry" ]]; then
    echo "Dry-run: no se borró nada (${total} candidata(s))."
    return 0
  fi
  if [[ -z "$yes" ]]; then
    confirmar "Se borran ${total} reseña(s) rechazadas, con sus fotos." \
      || { echo "Cancelado."; return 0; }
  fi
  while IFS=$'\t' read -r id creado pk; do
    borrar_foto_publica "$id" || exit 3
    if [[ -n "$pk" ]]; then
      validar_clave_foto "$pk"
      borrar_todas_versiones "$RESENAS_BUCKET" "$pk" || exit 3
    fi
    borrar_todas_versiones "$RESENAS_BUCKET" "resenas/${id}.json" || exit 3
    borradas=$((borradas+1))
  done < "$lista"
  exportar_publicas || exit $?
  echo "Mantenimiento: ${borradas} reseña(s) borrada(s)."
}

# Copia todo el bucket privado a DIR. Solo objetos del bucket: ninguna credencial ni .env.
cmd_backup() {
  local dir="${1:-}" cantidad=0
  if [[ -z "$dir" ]]; then err "falta DIR."; uso_completo; exit 1; fi
  mkdir -p "$dir"
  if ! aws_cli s3 sync "s3://${RESENAS_BUCKET}" "$dir" >/dev/null 2>&1; then
    err "falló el backup del bucket privado (error de AWS)."
    exit 3
  fi
  cantidad="$(find "$dir" -type f | wc -l | tr -d ' ')"
  echo "Backup completo en: $(cd "$dir" && pwd) (${cantidad} archivo(s))."
}

# ────────────────────────────────────────────────────────────
# Ayuda y despachador
# ────────────────────────────────────────────────────────────
uso_completo() {
  cat >&2 <<'EOF'
Uso: resenas.sh <comando> [argumentos]

  token crear --pedido REF --producto SLUG --categoria SLUG
              [--producto-label "Texto"] [--dias N] [--mensaje "Texto"]
  token revocar TOKEN
  token listar
  pendientes
  ver ID
  aprobar ID | rechazar ID | ocultar ID | restaurar ID | verificar ID
  borrar ID [--foto] [--yes]
  mantenimiento [--dry-run] [--yes]
  exportar
  backup DIR
  ayuda

Códigos de salida: 0 ok · 1 uso incorrecto o error local · 2 estado inválido o no encontrado · 3 error de AWS.
Configuración: admin/.env (o variables de entorno). Ver admin/README.md.
EOF
}

main() {
  local comando="${1:-}"
  if [[ $# -gt 0 ]]; then shift; fi
  case "$comando" in
    token) cmd_token "$@" ;;
    pendientes) cmd_pendientes "$@" ;;
    ver) cmd_ver "$@" ;;
    aprobar) validar_id "${1:-}"; aprobar_resena "$1" ;;
    rechazar) validar_id "${1:-}"; rechazar_resena "$1" ;;
    ocultar) validar_id "${1:-}"; ocultar_resena "$1" ;;
    restaurar) validar_id "${1:-}"; restaurar_resena "$1" ;;
    verificar) validar_id "${1:-}"; verificar_resena "$1" ;;
    borrar) cmd_borrar "$@" ;;
    mantenimiento) cmd_mantenimiento "$@" ;;
    exportar) cmd_exportar "$@" ;;
    backup) cmd_backup "$@" ;;
    ayuda|"") uso_completo; exit 1 ;;
    *) err "comando desconocido: '${comando}'."; uso_completo; exit 1 ;;
  esac
}

main "$@"
