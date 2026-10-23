#!/usr/bin/env bash
# ORK3D · /opiniones: publicación acotada de los archivos del lanzamiento.
#
# Solo sube los archivos de infra/opiniones-archivos.tsv. No usa sync, ni --delete, ni infra/setup.sh, ni
# infra/resenas-setup.sh, ni deploy.sh. No toca resenas/, el export, las fotos, la API, la Lambda, IAM, CloudFront
# (salvo la invalidación de las rutas de esos archivos) ni DNS.
#
# Modos:
#   diff (por defecto)  SOLO LECTURA. Lista los archivos exactos, compara el SHA-256 local con el de S3, y verifica
#                       que el repo esté limpio y en el mismo commit que origin.
#   publicar            Exige ALLOW_OPINIONES_PUBLISH=1 y OPINIONES_REPUBLICAR=1 (/opiniones ya está lanzada). Guarda los objetos actuales y sus cabeceras (600) en una
#                       carpeta de copia, sube solo los archivos del manifiesto, invalida sus rutas y espera.
#   revertir            Exige ALLOW_OPINIONES_PUBLISH=1. Restaura los objetos de la copia más reciente con sus cabeceras
#                       originales, e invalida las mismas rutas.
#
# Variables: AWS_PROFILE=default y AWS_REGION=us-east-1 (los únicos admitidos).
# OPINIONES_RUTA: carpeta privada de copias (por defecto /tmp/ork3d-opiniones).
# OPINIONES_COPIA (revertir): carpeta concreta de la copia; por defecto, la más reciente.
#
# Códigos: 0 correcto · 1 falló · 2 entorno inválido · 3 falta la guarda (antes de AWS) · 4 no está listo (no se escribió nada).
set -uo pipefail

MODO="${1:-diff}"
case "$MODO" in
  diff|publicar|revertir) ;;
  *) echo "uso: $0 [diff|publicar|revertir]" >&2; exit 2 ;;
esac

if [[ "$MODO" == publicar || "$MODO" == revertir ]] && [[ "${ALLOW_OPINIONES_PUBLISH:-}" != "1" ]]; then
  echo "ABORTA: $MODO escribe archivos del sitio en S3 e invalida CloudFront. Para ejecutarlo: ALLOW_OPINIONES_PUBLISH=1 infra/opiniones-publicar.sh $MODO" >&2
  exit 3
fi

# /opiniones ya está lanzada en producción: publicar vuelve a subir la misma página. Solo tiene sentido si el repo
# cambió, así que exige una segunda marca explícita. revertir no cambia: sigue con ALLOW_OPINIONES_PUBLISH=1.
if [[ "$MODO" == publicar && "${OPINIONES_REPUBLICAR:-}" != "1" ]]; then
  echo "ABORTA: /opiniones ya está lanzada en producción. publicar vuelve a subir la misma página: solo tiene sentido si el repo cambió. Para republicar a propósito: ALLOW_OPINIONES_PUBLISH=1 OPINIONES_REPUBLICAR=1 infra/opiniones-publicar.sh publicar" >&2
  exit 3
fi

PERFIL="${AWS_PROFILE:-default}"
REGION="${AWS_REGION:-us-east-1}"
[[ "$PERFIL" == "default" ]] || { echo "ERROR: AWS_PROFILE solo puede ser default en este paso." >&2; exit 2; }
[[ "$REGION" == "us-east-1" ]] || { echo "ERROR: AWS_REGION solo puede ser us-east-1 en este paso." >&2; exit 2; }
export AWS_PROFILE="$PERFIL" AWS_REGION="$REGION" AWS_PAGER=""

AWS="${AWS_CLI:-aws}"
GIT="${GIT_CMD:-git}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MANIFIESTO="$ROOT/infra/opiniones-archivos.tsv"
BUCKET="${OPINIONES_BUCKET:-ork3d.com}"
DIST="${OPINIONES_DIST:-ECOW0H6A48HVP}"
RAMA="main"
RUTA="${OPINIONES_RUTA:-/tmp/ork3d-opiniones}"
RB="$RUTA/rollback"
T="$(mktemp -d)"
chmod 700 "$T"
trap 'rm -rf "$T"' EXIT

fallo() { echo "ERROR: $*" >&2; exit 1; }
no_listo() { echo "NO LISTO: $*" >&2; exit 4; }

# Entradas del manifiesto, sin comentarios: archivo, clave, content-type, cache-control, rutas.
leer_manifiesto() {
  grep -v '^#' "$MANIFIESTO" | grep -v '^[[:space:]]*$'
}

sha_local() { sha256sum "$1" | cut -c1-16; }

sha_remoto() {  # CLAVE: SHA-256 del objeto en S3; vacío si el objeto no existe (sin head-object, sha256 de vacío engañaría)
  "$AWS" s3api head-object --bucket "$BUCKET" --key "$1" > /dev/null 2>&1 || return 0
  "$AWS" s3 cp "s3://$BUCKET/$1" - 2> /dev/null | sha256sum | cut -c1-16
}

verificar_repo() {
  [[ -z "$("$GIT" -C "$ROOT" status --porcelain)" ]] || no_listo "el repo tiene cambios sin commitear: publicar solo lo commiteado"
  local local_head remoto_head
  local_head="$("$GIT" -C "$ROOT" rev-parse HEAD)"
  remoto_head="$("$GIT" -C "$ROOT" rev-parse "origin/$RAMA" 2> /dev/null || echo '')"
  [[ -n "$remoto_head" ]] || no_listo "no existe origin/$RAMA: hacer git fetch"
  [[ "$local_head" == "$remoto_head" ]] || no_listo "HEAD $local_head no coincide con origin/$RAMA $remoto_head"
  echo "  repo limpio en $local_head (igual a origin/$RAMA)"
}

rutas_a_invalidar() {  # lista única de rutas de todo el manifiesto
  leer_manifiesto | awk -F'\t' '{print $5}' | tr ',' '\n' | sort -u
}

diff_publicacion() {
  echo "── Archivos del lanzamiento (manifiesto: infra/opiniones-archivos.tsv) ──"
  local linea archivo clave ct cc rutas tamano estado remoto
  while IFS=$'\t' read -r archivo clave ct cc rutas; do
    [[ -f "$ROOT/$archivo" ]] || no_listo "falta el archivo local $archivo"
    tamano="$(wc -c < "$ROOT/$archivo")"
    remoto="$(sha_remoto "$clave")"
    if [[ -z "$remoto" ]]; then estado="NUEVO"
    elif [[ "$remoto" == "$(sha_local "$ROOT/$archivo")" ]]; then estado="IGUAL en S3"
    else estado="DISTINTO: se reemplaza"
    fi
    printf '  %-40s %7s bytes  sha %s  %s\n' "$archivo" "$tamano" "$(sha_local "$ROOT/$archivo")" "$estado"
  done < <(leer_manifiesto)
  echo "  rutas a invalidar en CloudFront: $(rutas_a_invalidar | paste -sd' ' -)"
  echo "  no se toca: resenas/, admin/, api/, infra/, Lambdas, IAM, CloudFront (salvo invalidación), DNS"
}

if [[ "$MODO" == diff ]]; then
  verificar_repo
  diff_publicacion
  echo
  echo "Diff listo. Para publicar: ALLOW_OPINIONES_PUBLISH=1 infra/opiniones-publicar.sh publicar"
  exit 0
fi

if [[ "$MODO" == publicar ]]; then
  verificar_repo
  diff_publicacion > "$T/diff.txt" || true
  cat "$T/diff.txt"
  echo
  COPIA="$RB/$(date -u +%Y%m%dT%H%M%S%6NZ)"
  mkdir -p "$COPIA/objetos" && chmod 700 "$RUTA" "$RB" "$COPIA" "$COPIA/objetos"
  : > "$COPIA/cabeceras.tsv"
  echo "── Copia de los objetos actuales (600) ──"
  while IFS=$'\t' read -r archivo clave ct cc rutas; do
    if "$AWS" s3api head-object --bucket "$BUCKET" --key "$clave" --output json > "$T/h.json" 2> "$T/h.err"; then
      mkdir -p "$COPIA/objetos/$(dirname "$clave")"
      "$AWS" s3 cp "s3://$BUCKET/$clave" "$COPIA/objetos/$clave" > /dev/null 2> "$T/cp.err" \
        || fallo "no se pudo copiar $clave: $(cat "$T/cp.err")"
      python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sys.argv[2]+"\t"+(d.get("ContentType") or "")+"\t"+(d.get("CacheControl") or "")+"\tsi")' \
        "$T/h.json" "$clave" >> "$COPIA/cabeceras.tsv"
    else
      printf '%s\t\t\tno\n' "$clave" >> "$COPIA/cabeceras.tsv"   # no existía: revertir lo quitaría
    fi
  done < <(leer_manifiesto)
  chmod 600 "$COPIA/cabeceras.tsv"
  find "$COPIA/objetos" -type f -exec chmod 600 {} +
  echo "  copia: $COPIA"

  echo "── Subida (solo el manifiesto) ──"
  while IFS=$'\t' read -r archivo clave ct cc rutas; do
    "$AWS" s3 cp "$ROOT/$archivo" "s3://$BUCKET/$clave" --content-type "$ct" --cache-control "$cc" > /dev/null 2> "$T/up.err" \
      || fallo "no se pudo subir $clave: $(cat "$T/up.err")"
    echo "  subido: $clave"
  done < <(leer_manifiesto)

  echo "── Invalidación de CloudFront (solo las rutas del manifiesto) ──"
  mapfile -t RUTAS < <(rutas_a_invalidar)
  INV="$("$AWS" cloudfront create-invalidation --distribution-id "$DIST" --paths "${RUTAS[@]}" \
    --query 'Invalidation.Id' --output text 2> "$T/inv.err")" || fallo "create-invalidation: $(cat "$T/inv.err")"
  echo "  invalidación: $INV (${#RUTAS[@]} rutas)"
  "$AWS" cloudfront wait invalidation-completed --distribution-id "$DIST" --id "$INV" 2> "$T/wait.err" \
    || fallo "la invalidación $INV no se completó: $(cat "$T/wait.err")"
  echo
  echo "Publicado. Siguiente: infra/opiniones-verify.sh post"
  echo "Para volver atrás: ALLOW_OPINIONES_PUBLISH=1 infra/opiniones-publicar.sh revertir"
  exit 0
fi

# revertir
COPIA="${OPINIONES_COPIA:-$(ls -td "$RB"/*/ 2>/dev/null | head -1)}"
COPIA="${COPIA%/}"
[[ -n "$COPIA" && -f "$COPIA/cabeceras.tsv" ]] || fallo "no hay copia para revertir en $RB (OPINIONES_COPIA indica una concreta)"
while IFS=$'\t' read -r clave ct cc existia; do
  if [[ "$existia" == si ]]; then
    args=(--content-type "$ct")
    [[ -n "$cc" ]] && args+=(--cache-control "$cc")
    "$AWS" s3 cp "$COPIA/objetos/$clave" "s3://$BUCKET/$clave" "${args[@]}" > /dev/null 2> "$T/rv.err" \
      || fallo "no se pudo restaurar $clave: $(cat "$T/rv.err")"
    echo "  restaurado: $clave"
  else
    fallo "$clave no existía antes del lanzamiento: revertir no lo borra; revisar a mano"
  fi
done < "$COPIA/cabeceras.tsv"
mapfile -t RUTAS < <(rutas_a_invalidar)
INV="$("$AWS" cloudfront create-invalidation --distribution-id "$DIST" --paths "${RUTAS[@]}" \
  --query 'Invalidation.Id' --output text 2> "$T/inv.err")" || fallo "create-invalidation: $(cat "$T/inv.err")"
"$AWS" cloudfront wait invalidation-completed --distribution-id "$DIST" --id "$INV" 2> "$T/wait.err" \
  || fallo "la invalidación $INV no se completó"
echo "Revertido desde $(basename "$COPIA"). Reseñas, export, fotos y backend no cambiaron."
exit 0
