#!/usr/bin/env bash
# Fixtures SINTÉTICOS para admin/tests/run_tests.sh. Ningún dato de acá es real.
# Se cargan directo en el bucket privado (como lo haría la Lambda) usando el shim.
# Requiere definidos: AWS_CMD, RESENAS_BUCKET, SITE_BUCKET y FX (directorio temporal).

fx_hash() { printf '%s' "$1" | openssl dgst -sha256 -r | cut -d' ' -f1; }

# Token sintético n: 32 caracteres (marcador de prueba, no un token real).
fx_tk() { printf 'TKFIX%027d' "$1"; }

# ID de reseña sintético n: 32 caracteres hex (la Lambda usa uuid4().hex).
fx_id() { printf '%032x' "$1"; }

# Fecha ISO UTC a N días de hoy (N negativo = pasado). GNU y BSD.
fx_fecha_dias() {
  date -u -d "$1 days" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v"$1"d +%Y-%m-%dT%H:%M:%SZ
}

# Sube un archivo al bucket con el shim. Uso: fx_put BUCKET CLAVE ARCHIVO [TIPO] [CACHE]
fx_put() {
  local -a extra=()
  if [[ -n "${4:-}" ]]; then extra+=(--content-type "$4"); fi
  if [[ -n "${5:-}" ]]; then extra+=(--cache-control "$5"); fi
  # shellcheck disable=SC2086
  $AWS_CMD s3api put-object --bucket "$1" --key "$2" --body "$3" ${extra[@]+"${extra[@]}"} >/dev/null
}

# Foto sintética: cabecera JPEG (FF D8 FF E0) + relleno. No es una foto real.
fx_jpeg() {
  { printf '\xff\xd8\xff\xe0'; head -c 128 /dev/zero; } > "${FX}/sintetica.jpg"
}

# Token sintético: escribe tokens/<hash>.json en el bucket privado. Imprime el hash.
fx_token() {  # fx_token TOKEN PEDIDO
  local th archivo
  th="$(fx_hash "$1")"
  archivo="${FX}/token-${th}.json"
  jq -n --arg th "$th" --arg pedido "$2" --arg creado "$(fx_fecha_dias -1)" --arg vence "$(fx_fecha_dias 59)" '{
    v: 1, token_hash: $th, created_at: $creado, expires_at: $vence, order_ref: $pedido,
    product_slug: "trofeos", product_label: "Trofeo de torneo", category: "trofeos",
    verified: false, revoked: false, revoked_at: null
  }' > "$archivo"
  fx_put "$RESENAS_BUCKET" "tokens/${th}.json" "$archivo" "application/json; charset=utf-8"
  printf '%s' "$th"
}

# Claim de uso único: lo crea la Lambda; la CLI solo lo lee.
fx_claim() {  # fx_claim HASH
  local archivo="${FX}/claim-$1.json"
  jq -n --arg th "$1" --arg t "$(fx_fecha_dias -1)" '{v: 1, token_hash: $th, claimed_at: $t}' > "$archivo"
  fx_put "$RESENAS_BUCKET" "claims/$1.json" "$archivo" "application/json; charset=utf-8"
}

# Reseña sintética en estado pending (permiso true). El filtro jq ajusta estado y campos.
fx_resena() {  # fx_resena ID TOKEN_HASH FILTRO_JQ
  local archivo="${FX}/resena-$1.json" creado
  creado="$(fx_fecha_dias -10)"
  jq -n --arg id "$1" --arg th "$2" --arg creado "$creado" '{
    v: 1, id: $id, token_hash: $th, status: "pending",
    created_at: $creado, moderated_at: null, published_at: null,
    rating: 5, comment: "Texto sintético de prueba", aspects: ["calidad"],
    display_mode: "inicial", display_name: "Cliente Test", permission_to_publish: true,
    photo_key: null, photo_public_key: null, private_feedback: "FEEDBACK-PRIVADO-TEST",
    contact_requested: true, contact: "fixture@example.invalid",
    product_slug: "trofeos", product_label: "Trofeo de torneo", category: "trofeos", verified: false
  }' | jq "$3" > "$archivo"
  fx_put "$RESENAS_BUCKET" "resenas/$1.json" "$archivo" "application/json; charset=utf-8"
}

# Carga el conjunto completo. Deja en variables los IDs (RP_*, RA_*, RH_*, RR_*, RJ_*),
# los hashes de token (TH1..TH12), las claves de foto (FOTO_*) y FX_TOKENS con los tokens
# completos, para verificar que nunca se impriman.
fx_cargar() {
  fx_jpeg
  FX_TOKENS=()
  local n
  for n in 1 2 3 4 5 6 7 8 9 10 11 12; do FX_TOKENS+=("$(fx_tk "$n")"); done
  TH1="$(fx_token "$(fx_tk 1)" PEDIDO-FICTICIO-001)"
  TH2="$(fx_token "$(fx_tk 2)" PEDIDO-FICTICIO-002)"
  TH3="$(fx_token "$(fx_tk 3)" PEDIDO-FICTICIO-003)"
  TH4="$(fx_token "$(fx_tk 4)" PEDIDO-FICTICIO-004)"
  TH5="$(fx_token "$(fx_tk 5)" PEDIDO-FICTICIO-005)"
  TH6="$(fx_token "$(fx_tk 6)" PEDIDO-FICTICIO-006)"
  TH7="$(fx_token "$(fx_tk 7)" PEDIDO-FICTICIO-007)"
  TH8="$(fx_token "$(fx_tk 8)" PEDIDO-FICTICIO-008)"
  TH9="$(fx_token "$(fx_tk 9)" PEDIDO-FICTICIO-009)"
  TH10="$(fx_token "$(fx_tk 10)" PEDIDO-FICTICIO-010)"
  TH11="$(fx_token "$(fx_tk 11)" PEDIDO-FICTICIO-011)"
  TH12="$(fx_token "$(fx_tk 12)" PEDIDO-FICTICIO-012)"
  fx_claim "$TH3"

  RP_FOTO="$(fx_id 1)"; RP_SIN_PERMISO="$(fx_id 2)"; RP_TEXTO="$(fx_id 3)"
  RA_OLD="$(fx_id 4)"; RA_NEW="$(fx_id 5)"; RA_FOTO="$(fx_id 6)"
  RH_OCULTA="$(fx_id 7)"; RR_NUEVA="$(fx_id 8)"; RJ_VIEJA="$(fx_id 9)"; RA_ANON="$(fx_id 10)"
  RP_WEBP="$(fx_id 11)"; RP_CLAVE_MALA="$(fx_id 12)"

  # Claves de foto con el formato del Lambda: pendientes/<sha256 hex>/<uuid hex>.<ext>
  FOTO_1="pendientes/${TH1}/$(fx_id 101).jpg"
  FOTO_2="pendientes/${TH6}/$(fx_id 102).jpg"
  FOTO_3="pendientes/${TH9}/$(fx_id 103).jpg"
  FOTO_WEBP="pendientes/${TH11}/$(fx_id 104).webp"

  # pending con foto y permiso
  fx_resena "$RP_FOTO" "$TH1" ".photo_key = \"${FOTO_1}\""
  fx_put "$RESENAS_BUCKET" "$FOTO_1" "${FX}/sintetica.jpg" "image/jpeg"

  # pending SIN permiso de publicación
  fx_resena "$RP_SIN_PERMISO" "$TH2" '.permission_to_publish = false'

  # pending de texto (con claim: el link ya fue usado)
  fx_resena "$RP_TEXTO" "$TH3" '.display_name = "Cliente Test C"'

  # pending con clave WebP (el Lambda solo acepta JPEG: ver debe rechazarla) y otra con clave fuera de formato
  fx_resena "$RP_WEBP" "$TH11" ".photo_key = \"${FOTO_WEBP}\""
  fx_put "$RESENAS_BUCKET" "$FOTO_WEBP" "${FX}/sintetica.jpg" "image/webp"
  fx_resena "$RP_CLAVE_MALA" "$TH12" ".photo_key = \"pendientes/${TH12}/foto-invalida.jpeg\""

  # approved, con fechas distintas para probar el orden del export
  fx_resena "$RA_OLD" "$TH4" '.status = "approved" | .published_at = "2026-09-01T10:00:00Z"
    | .moderated_at = "2026-09-01T10:00:00Z" | .display_name = "Ana Test"'
  fx_resena "$RA_NEW" "$TH5" '.status = "approved" | .published_at = "2026-10-01T10:00:00Z"
    | .moderated_at = "2026-10-01T10:00:00Z" | .display_mode = "nombre" | .display_name = "Lucía Test"'

  # approved con foto privada y foto pública ya publicada (más nueva que RA_NEW)
  fx_resena "$RA_FOTO" "$TH6" ".status = \"approved\" | .published_at = \"2026-10-10T10:00:00Z\"
    | .photo_key = \"${FOTO_2}\" | .photo_public_key = \"resenas/fotos/${RA_FOTO}.jpg\""
  fx_put "$RESENAS_BUCKET" "$FOTO_2" "${FX}/sintetica.jpg" "image/jpeg"
  fx_put "$SITE_BUCKET" "resenas/fotos/${RA_FOTO}.jpg" "${FX}/sintetica.jpg" "image/jpeg" \
    "public, max-age=31536000, immutable"

  # hidden, rejected reciente y rejected viejo (120 días, con foto privada)
  fx_resena "$RH_OCULTA" "$TH7" '.status = "hidden" | .moderated_at = "2026-09-15T10:00:00Z"'
  fx_resena "$RR_NUEVA" "$TH8" '.status = "rejected" | .moderated_at = "2026-10-20T10:00:00Z"'
  fx_resena "$RJ_VIEJA" "$TH9" ".status = \"rejected\" | .created_at = \"$(fx_fecha_dias -120)\"
    | .moderated_at = \"$(fx_fecha_dias -119)\" | .photo_key = \"${FOTO_3}\""
  fx_put "$RESENAS_BUCKET" "$FOTO_3" "${FX}/sintetica.jpg" "image/jpeg"

  # approved en modo anónimo: el Lambda guarda el nombre genérico (ANON_NAME en app.py)
  fx_resena "$RA_ANON" "$TH10" '.status = "approved" | .published_at = "2026-07-01T10:00:00Z"
    | .moderated_at = "2026-07-01T10:00:00Z" | .display_mode = "anonimo" | .display_name = "Cliente de ORK3D"'
}
