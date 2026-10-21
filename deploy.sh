#!/usr/bin/env bash
# ORK3D · Deploy: sync a S3 + invalidación de CloudFront.
# Requiere haber corrido infra/setup.sh al menos una vez.
# Variables: BUCKET, DISTRIBUTION_ID (se leen de infra/.env si existe, o del entorno).

set -euo pipefail

cd "$(dirname "$0")"

if [[ -f infra/.env ]]; then
  # shellcheck disable=SC1091
  source infra/.env
fi

: "${BUCKET:?Falta BUCKET. Ejecutá infra/setup.sh o exportá BUCKET=ork3d.com}"
: "${DISTRIBUTION_ID:?Falta DISTRIBUTION_ID. Ejecutá infra/setup.sh o exportalo.}"

echo "▶ Sync a s3://${BUCKET} ..."

# IMPORTANTE: resenas/ lo escribe el CLI de moderación (publicas.json y fotos aprobadas).
# Está excluido del sync y del --delete: si se quita la exclusión, cada deploy borra las reseñas publicadas.
# Assets con cache larga (hash en path no, así que mantenemos cache moderado)
aws s3 sync . "s3://${BUCKET}" \
  --delete \
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
  --cache-control "public, max-age=300" \
  --metadata-directive REPLACE

# HTML: cache corta para que cambios se vean rápido
aws s3 cp index.html "s3://${BUCKET}/index.html" \
  --cache-control "public, max-age=60, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --metadata-directive REPLACE

aws s3 cp error.html "s3://${BUCKET}/error.html" \
  --cache-control "public, max-age=60, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --metadata-directive REPLACE

aws s3 cp torneos.html "s3://${BUCKET}/torneos.html" \
  --cache-control "public, max-age=60, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --metadata-directive REPLACE

aws s3 cp regalos-empresariales-fin-de-ano.html "s3://${BUCKET}/regalos-empresariales-fin-de-ano.html" \
  --cache-control "public, max-age=60, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --metadata-directive REPLACE

# CSS / JS / SVG: cache larga
aws s3 cp assets/ "s3://${BUCKET}/assets/" \
  --recursive \
  --cache-control "public, max-age=300, must-revalidate" \
  --metadata-directive REPLACE

# AVIF y WebP: Content-Type explícito por extensión; no depender del mimetypes del runner
aws s3 cp assets/ "s3://${BUCKET}/assets/" \
  --recursive \
  --exclude "*" --include "*.avif" \
  --content-type "image/avif" \
  --cache-control "public, max-age=300, must-revalidate" \
  --metadata-directive REPLACE

aws s3 cp assets/ "s3://${BUCKET}/assets/" \
  --recursive \
  --exclude "*" --include "*.webp" \
  --content-type "image/webp" \
  --cache-control "public, max-age=300, must-revalidate" \
  --metadata-directive REPLACE

aws s3 cp opinar.html "s3://${BUCKET}/opinar.html" \
  --cache-control "public, max-age=60, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --metadata-directive REPLACE

aws s3 cp opiniones.html "s3://${BUCKET}/opiniones.html" \
  --cache-control "public, max-age=60, must-revalidate" \
  --content-type "text/html; charset=utf-8" \
  --metadata-directive REPLACE

aws s3 cp config.js "s3://${BUCKET}/config.js" \
  --cache-control "public, max-age=300, must-revalidate" \
  --content-type "application/javascript; charset=utf-8" \
  --metadata-directive REPLACE

echo "▶ Invalidando CloudFront ${DISTRIBUTION_ID} ..."
INV_ID=$(aws cloudfront create-invalidation \
  --distribution-id "${DISTRIBUTION_ID}" \
  --paths "/*" \
  --query 'Invalidation.Id' --output text)

echo "✓ Invalidación creada: ${INV_ID}"
echo "✓ Deploy completo."
