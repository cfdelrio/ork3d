#!/usr/bin/env bash
# ORK3D · panel admin: publicación controlada de los 5 estáticos del rediseño (commit 590ed7d). Se ejecuta desde EC2.
#
# Sube SOLO los archivos de infra/admin-ui-archivos.tsv, uno por uno con s3api put-object. No usa sync, ni --delete, ni
# directorios completos. No toca Lambdas, IAM, SES, DNS, S3 privado, reseñas, export, notificaciones, /opiniones,
# /opinar, la API, sesiones, auth ni CSRF. En CloudFront solo crea la invalidación de las rutas del manifiesto.
# admin-entrar.js no se sube: su contenido no cambió respecto de e8052cd (el manifiesto lo excluye).
#
# Modos:
#   diff (por defecto)  SOLO LECTURA. Verifica el repo y compara cada archivo con su objeto en S3. No escribe nada.
#   publicar            Exige ALLOW_ADMIN_UI_PUBLISH=1. Guarda la copia previa (bytes y cabeceras, 600), sube los 5
#                       archivos, verifica sus cabeceras, invalida las rutas del manifiesto y espera.
#   revertir            Exige ALLOW_ADMIN_UI_PUBLISH=1. Restaura exactamente la copia más reciente (bytes, Content-Type,
#                       Cache-Control, metadata); quita los objetos que no existían antes; invalida las mismas rutas.
#
# Variables: ADMIN_UI_RUTA (carpeta de copias, /tmp/ork3d-admin-ui), ADMIN_UI_COPIA (revertir: copia concreta).
# Las copias no se borran nunca.
# Códigos: 0 correcto · 1 falló · 2 uso o entorno inválido · 3 falta ALLOW_ADMIN_UI_PUBLISH (sin llamar a AWS) ·
#          4 no listo: el repo no cumple las guardas (sin escritura).
set -uo pipefail
umask 077

MODO="${1:-diff}"
case "$MODO" in
  diff|publicar|revertir) ;;
  *) echo "uso: $0 [diff|publicar|revertir]" >&2; exit 2 ;;
esac

if [[ "$MODO" != diff && "${ALLOW_ADMIN_UI_PUBLISH:-}" != "1" ]]; then
  echo "ABORTA: $MODO escribe en S3 e invalida CloudFront. Para ejecutarlo: ALLOW_ADMIN_UI_PUBLISH=1 infra/admin-ui-publicar.sh $MODO" >&2
  exit 3
fi

source "$(cd "$(dirname "$0")" && pwd)/admin-ui-comun.sh"

RUTA="${ADMIN_UI_RUTA:-/tmp/ork3d-admin-ui}"
COPIAS="$RUTA/copias"
PUNTERO="$RUTA/ULTIMA-COPIA"
T="$(mktemp -d)"
chmod 700 "$T"
trap 'rm -rf "$T"' EXIT
REMOTO_SHA=""
INV=""

paso() { echo; echo "── $* ──"; }

diff_publicacion() {
  local n=0 archivo clave ct cc rutas tam sha_loc estado
  paso "Diferencias local ↔ S3 (solo lectura)"
  while IFS=$'\t' read -r archivo clave ct cc rutas; do
    n=$((n + 1))
    tam="$(wc -c < "$ROOT/$archivo")"
    sha_loc="$(sha256sum "$ROOT/$archivo" | cut -d' ' -f1)"
    remoto_sha "$clave" "$T/r$n"
    if [[ "$REMOTO_SHA" == AUSENTE ]]; then
      estado="NUEVO: no existe en S3"
    elif [[ "$REMOTO_SHA" == "$sha_loc" ]]; then
      if python3 "$AYUDA" cabeceras "$T/r$n.head" "$ct" "$cc" > /dev/null; then
        estado="IGUAL"
      else
        estado="IGUAL CONTENIDO · CABECERAS DISTINTAS (se corrigen al publicar)"
      fi
    else
      estado="DISTINTO: se reemplaza"
    fi
    echo "  $archivo"
    echo "    tamaño local: $tam bytes"
    echo "    sha256 local: $sha_loc"
    if [[ "$REMOTO_SHA" == AUSENTE ]]; then
      echo "    sha256 S3:    (no existe)"
    else
      echo "    sha256 S3:    $REMOTO_SHA"
    fi
    echo "    estado:       $estado"
    echo "    destino:      s3://$BUCKET/$clave"
  done < <(leer_manifiesto)
  echo
  echo "  rutas a invalidar en CloudFront: $(python3 "$AYUDA" rutas "$MANIFIESTO" | paste -sd' ' -)"
  echo "  no se toca: Lambdas, IAM, SES, CloudFront (salvo invalidación), DNS, S3 privado, reseñas, export,"
  echo "              notificaciones, /opiniones, /opinar, API admin, sesiones, auth, CSRF, admin-entrar.js"
}

invalidar() {  # RUTAS...: una invalidación con solo esas rutas; espera a que termine. Deja el id en INV.
  local ruta
  for ruta in "$@"; do
    [[ "$ruta" != "/*" ]] || fallo "se intentó invalidar /*: no permitido"
  done
  INV="$("$AWS" cloudfront create-invalidation --distribution-id "$DIST" --paths "$@" \
    --query 'Invalidation.Id' --output text < /dev/null 2> "$T/inv.err")" \
    || fallo "create-invalidation: $(cat "$T/inv.err")"
  "$AWS" cloudfront wait invalidation-completed --distribution-id "$DIST" --id "$INV" < /dev/null 2> "$T/wait.err" \
    || fallo "la invalidación $INV no se completó: $(cat "$T/wait.err")"
}

publicar() {
  paso "Guardas del repo"
  verificar_repo
  diff_publicacion

  paso "Copia previa (600): bytes y cabeceras de los objetos actuales"
  local n=0 archivo clave ct cc rutas entrada copia stamp
  stamp="$(date -u +%Y%m%dT%H%M%S%6NZ)"
  copia="$COPIAS/$stamp"
  [[ ! -e "$copia" ]] || fallo "ya existe $copia; no se sobrescribe"
  mkdir -p "$copia/objetos" "$copia/entradas" || fallo "no se pudo crear $copia"
  chmod 700 "$RUTA" "$COPIAS" "$copia" "$copia/objetos" "$copia/entradas"
  cp "$MANIFIESTO" "$copia/manifiesto.tsv"
  while IFS=$'\t' read -r archivo clave ct cc rutas; do
    n=$((n + 1))
    entrada="$copia/entradas/$(printf '%02d' "$n").json"
    if "$AWS" s3api head-object --bucket "$BUCKET" --key "$clave" --output json > "$T/h.json" 2> "$T/h.err" < /dev/null; then
      mkdir -p "$(dirname "$copia/objetos/$clave")"
      "$AWS" s3api get-object --bucket "$BUCKET" --key "$clave" "$copia/objetos/$clave" --output json > /dev/null 2> "$T/g.err" < /dev/null \
        || fallo "no se pudo copiar $clave: $(cat "$T/g.err")"
      python3 "$AYUDA" copia-entrada "$T/h.json" "$clave" > "$entrada"
      echo "  $clave: existía (copiado con sus cabeceras)"
    else
      grep -q -E '\(404\)|NoSuchKey|Not Found' "$T/h.err" || fallo "head-object $clave: $(cat "$T/h.err")"
      python3 "$AYUDA" copia-entrada - "$clave" > "$entrada"
      echo "  $clave: no existía (revertir lo quitaría)"
    fi
  done < <(leer_manifiesto)
  find "$copia" -type f -exec chmod 600 {} +
  printf '%s\n' "$copia" > "$PUNTERO"
  chmod 600 "$PUNTERO"
  echo "  copia de rollback: $copia (archivos 600)"

  paso "Subida: solo los archivos del manifiesto, uno por uno (s3api put-object)"
  while IFS=$'\t' read -r archivo clave ct cc rutas; do
    "$AWS" s3api put-object --bucket "$BUCKET" --key "$clave" --body "$ROOT/$archivo" \
      --content-type "$ct" --cache-control "$cc" --output json > "$T/put.json" 2> "$T/put.err" < /dev/null \
      || fallo "put-object $clave falló: $(cat "$T/put.err"). Publicación parcial: revertir con ALLOW_ADMIN_UI_PUBLISH=1 infra/admin-ui-publicar.sh revertir"
    "$AWS" s3api head-object --bucket "$BUCKET" --key "$clave" --output json > "$T/post.json" 2> "$T/post.err" < /dev/null \
      || fallo "head-object tras subir $clave: $(cat "$T/post.err")"
    python3 "$AYUDA" cabeceras "$T/post.json" "$ct" "$cc" > /dev/null \
      || fallo "las cabeceras de $clave no quedaron como el manifiesto. Revertir con: ALLOW_ADMIN_UI_PUBLISH=1 infra/admin-ui-publicar.sh revertir"
    echo "  subido: $clave (Content-Type y Cache-Control verificados)"
  done < <(leer_manifiesto)

  paso "Invalidación de CloudFront (solo las rutas del manifiesto)"
  local -a rutas_inv
  mapfile -t rutas_inv < <(python3 "$AYUDA" rutas "$MANIFIESTO")
  invalidar "${rutas_inv[@]}"
  echo "  invalidación: $INV (${#rutas_inv[@]} rutas, completada)"

  echo
  echo "Publicado. Siguiente: infra/admin-ui-verify.sh post"
  echo "Para volver atrás: ALLOW_ADMIN_UI_PUBLISH=1 infra/admin-ui-publicar.sh revertir (copia: $copia)"
}

revertir() {
  local copia n=0 archivo clave ct cc rutas entrada
  copia="${ADMIN_UI_COPIA:-}"
  [[ -n "$copia" ]] || copia="$(cat "$PUNTERO" 2> /dev/null || true)"
  [[ -n "$copia" && -f "$copia/manifiesto.tsv" ]] \
    || fallo "no hay copia para revertir (ADMIN_UI_COPIA indica una concreta; la más reciente está en $PUNTERO)"
  python3 "$AYUDA" validar-manifiesto "$copia/manifiesto.tsv" > /dev/null || fallo "la copia tiene un manifiesto no válido"

  paso "Restauración exacta desde $copia (sin consultar el repo)"
  while IFS=$'\t' read -r archivo clave ct cc rutas; do
    n=$((n + 1))
    entrada="$copia/entradas/$(printf '%02d' "$n").json"
    [[ -f "$entrada" ]] || fallo "falta la entrada de rollback $entrada"
    if [[ "$(python3 "$AYUDA" existia "$entrada")" == si ]]; then
      [[ -f "$copia/objetos/$clave" ]] || fallo "falta el cuerpo de $clave en la copia"
      local -a argumentos
      mapfile -t argumentos < <(python3 "$AYUDA" args-put "$entrada")
      "$AWS" s3api put-object --bucket "$BUCKET" --key "$clave" --body "$copia/objetos/$clave" "${argumentos[@]}" \
        --output json > /dev/null 2> "$T/rv.err" < /dev/null || fallo "no se pudo restaurar $clave: $(cat "$T/rv.err")"
      "$AWS" s3api head-object --bucket "$BUCKET" --key "$clave" --output json > "$T/rh.json" 2> "$T/rh.err" < /dev/null \
        || fallo "head-object tras restaurar $clave: $(cat "$T/rh.err")"
      python3 "$AYUDA" cabeceras-entrada "$T/rh.json" "$entrada" > /dev/null \
        || fallo "las cabeceras de $clave no son las de la copia"
      "$AWS" s3api get-object --bucket "$BUCKET" --key "$clave" "$T/rv.cuerpo" --output json > /dev/null 2> "$T/rg.err" < /dev/null \
        || fallo "get-object tras restaurar $clave: $(cat "$T/rg.err")"
      [[ "$(sha256sum "$T/rv.cuerpo" | cut -d' ' -f1)" == "$(sha256sum "$copia/objetos/$clave" | cut -d' ' -f1)" ]] \
        || fallo "el cuerpo de $clave no coincide con la copia"
      echo "  restaurado: $clave (bytes y cabeceras de la copia)"
    else
      # No existía antes de la publicación: revertir es quitar ese objeto, y solo ese.
      "$AWS" s3api delete-object --bucket "$BUCKET" --key "$clave" --output json > /dev/null 2> "$T/rd.err" < /dev/null \
        || fallo "no se pudo quitar $clave: $(cat "$T/rd.err")"
      if "$AWS" s3api head-object --bucket "$BUCKET" --key "$clave" --output json > /dev/null 2>&1 < /dev/null; then
        fallo "$clave sigue existiendo tras quitarlo"
      fi
      echo "  quitado: $clave (no existía antes de la publicación)"
    fi
  done < <(leer_manifiesto "$copia/manifiesto.tsv")

  paso "Invalidación de CloudFront (las mismas rutas)"
  local -a rutas_inv
  mapfile -t rutas_inv < <(python3 "$AYUDA" rutas "$copia/manifiesto.tsv")
  invalidar "${rutas_inv[@]}"
  echo "  invalidación: $INV (${#rutas_inv[@]} rutas, completada)"

  echo
  echo "Revertido desde $copia. Ningún dato, reseña, Lambda ni configuración de CloudFront cambió."
  echo "Siguiente: infra/admin-ui-verify.sh pre"
}

case "$MODO" in
  diff)
    paso "Guardas del repo"
    verificar_repo
    diff_publicacion
    echo
    echo "Diff listo: no se escribió nada. Para publicar: ALLOW_ADMIN_UI_PUBLISH=1 infra/admin-ui-publicar.sh publicar"
    exit 0
    ;;
  publicar)
    publicar
    exit 0
    ;;
  revertir)
    revertir
    exit 0
    ;;
esac
