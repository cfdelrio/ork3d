#!/usr/bin/env bash
# ORK3D · Deploy del sitio público: sube archivos a S3 + invalidación de CloudFront.
# Requiere haber corrido infra/setup.sh al menos una vez.
# Variables: BUCKET, DISTRIBUTION_ID (se leen de infra/.env si existe, o del entorno).
#
# Reglas de este script (ver docs/resenas/MERGE-MAIN.md):
#   1. No borra nada en S3: no hay --delete ni rm. Un objeto que ya no está en el repo queda en el bucket hasta que
#      alguien lo quite a mano, con revisión. Así no se borra contenido que genera la aplicación ni legado que siga
#      publicado.
#   2. Cada prefijo tiene un solo dueño. Este script es dueño del sitio público. Quedan FUERA:
#        resenas/                          export y fotos públicas: las escribe la aplicación (Lambda admin y CLI).
#        admin-web/, assets/css/admin.css, assets/js/admin-*
#                                          panel admin: lo publica infra/admin-ui-publicar.sh con cabeceras no-store.
#        api/, admin/, dev/, docs/, infra/, .github/, *.sh, *.md
#                                          código del repo, no sitio.
#   3. DEPLOY_DRY_RUN=1 lista lo que haría con --dryrun (solo lectura en S3) y no invalida CloudFront. Es el chequeo
#      previo a cualquier merge a main.

set -euo pipefail

cd "$(dirname "$0")"

if [[ -f infra/.env ]]; then
  # shellcheck disable=SC1091
  source infra/.env
fi

: "${BUCKET:?Falta BUCKET. Ejecutá infra/setup.sh o exportá BUCKET=ork3d.com}"
: "${DISTRIBUTION_ID:?Falta DISTRIBUTION_ID. Ejecutá infra/setup.sh o exportalo.}"
DEPLOY_DRY_RUN="${DEPLOY_DRY_RUN:-0}"

# Todas las llamadas a S3 pasan por aquí. Con DEPLOY_DRY_RUN=1 solo listan y comparan: no escriben.
s3_sitio() {
  if [[ "$DEPLOY_DRY_RUN" == 1 ]]; then
    aws s3 "$@" --dryrun
  else
    aws s3 "$@"
  fi
}

if [[ "$DEPLOY_DRY_RUN" == 1 ]]; then
  echo "▶ DRY RUN: ninguna escritura en S3 ni invalidación. Se listan las operaciones que haría."
fi

echo "▶ Sync a s3://${BUCKET} (sin --delete) ..."

# IMPORTANTE: sin --delete. Ver la regla 1 del encabezado: el sync sube, no reconcilia.
# Exclusiones: código del repo y dueños de otros prefijos (regla 2 del encabezado).
# Las exclusiones también aplican a los objetos del bucket: son la protección de resenas/ y del panel admin.
s3_sitio sync . "s3://${BUCKET}" \
  --exclude ".git/*" \
  --exclude ".github/*" \
  --exclude "infra/*" \
  --exclude "deploy.sh" \
  --exclude "verify.sh" \
  --exclude "README.md" \
  --exclude ".gitignore" \
  --exclude "claude.md" \
  --exclude "*.sh" \
  --exclude "*.md" \
  --exclude "api/*" \
  --exclude "admin/*" \
  --exclude "dev/*" \
  --exclude "docs/*" \
  --exclude "resenas/*" \
  --exclude "admin-web/*" \
  --exclude "assets/css/admin.css" \
  --exclude "assets/js/admin-*" \
  --cache-control "public, max-age=300" \
  --metadata-directive REPLACE

# HTML: cache corta para que cambios se vean rápido
s3_sitio cp index.html "s3://${BUCKET}/index.html" \
  --cache-control "public, max-age=60, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --metadata-directive REPLACE

s3_sitio cp error.html "s3://${BUCKET}/error.html" \
  --cache-control "public, max-age=60, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --metadata-directive REPLACE

s3_sitio cp torneos.html "s3://${BUCKET}/torneos.html" \
  --cache-control "public, max-age=60, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --metadata-directive REPLACE

s3_sitio cp regalos-empresariales-fin-de-ano.html "s3://${BUCKET}/regalos-empresariales-fin-de-ano.html" \
  --cache-control "public, max-age=60, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --metadata-directive REPLACE

s3_sitio cp reconocimientos-empresariales.html "s3://${BUCKET}/reconocimientos-empresariales.html" \
  --cache-control "public, max-age=60, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --metadata-directive REPLACE

# CSS / JS / SVG: cache larga. Mismas exclusiones del panel admin que el sync (rutas relativas a assets/).
s3_sitio cp assets/ "s3://${BUCKET}/assets/" \
  --recursive \
  --exclude "css/admin.css" \
  --exclude "js/admin-*" \
  --cache-control "public, max-age=300, must-revalidate" \
  --metadata-directive REPLACE

# AVIF y WebP: Content-Type explícito por extensión; no depender del mimetypes del runner
s3_sitio cp assets/ "s3://${BUCKET}/assets/" \
  --recursive \
  --exclude "*" --include "*.avif" \
  --content-type "image/avif" \
  --cache-control "public, max-age=300, must-revalidate" \
  --metadata-directive REPLACE

s3_sitio cp assets/ "s3://${BUCKET}/assets/" \
  --recursive \
  --exclude "*" --include "*.webp" \
  --content-type "image/webp" \
  --cache-control "public, max-age=300, must-revalidate" \
  --metadata-directive REPLACE

s3_sitio cp opinar.html "s3://${BUCKET}/opinar.html" \
  --cache-control "public, max-age=60, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --metadata-directive REPLACE

s3_sitio cp opiniones.html "s3://${BUCKET}/opiniones.html" \
  --cache-control "public, max-age=60, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --metadata-directive REPLACE

# Sitemap: el sync lo subiría sin must-revalidate. Se fija la cabecera del lanzamiento de /opiniones.
s3_sitio cp sitemap.xml "s3://${BUCKET}/sitemap.xml" \
  --cache-control "public, max-age=300, must-revalidate" \
  --content-type "application/xml; charset=utf-8" \
  --metadata-directive REPLACE

s3_sitio cp config.js "s3://${BUCKET}/config.js" \
  --cache-control "public, max-age=300, must-revalidate" \
  --content-type "application/javascript; charset=utf-8" \
  --metadata-directive REPLACE

if [[ "$DEPLOY_DRY_RUN" == 1 ]]; then
  echo "▶ DRY RUN: invalidación de CloudFront omitida (la real sería sobre /*)."
  echo "✓ DRY RUN completo. No se escribió nada."
  exit 0
fi

echo "▶ Invalidando CloudFront ${DISTRIBUTION_ID} ..."
INV_ID=$(aws cloudfront create-invalidation \
  --distribution-id "${DISTRIBUTION_ID}" \
  --paths "/*" \
  --query 'Invalidation.Id' --output text)

echo "✓ Invalidación creada: ${INV_ID}"
echo "✓ Deploy completo."
