# ORK3D — Auditoría estado actual

## Stack
- HTML + CSS + JS vanilla (sin frameworks, sin build step)
- S3 privado (OAC) + CloudFront + ACM
- GitHub Actions CI/CD: push a `main` → deploy.sh → S3 sync + invalidación CF
- DNS: Cloudflare (CNAME flattening en apex)

## Arquitectura de archivos
```
index.html          # Única página actual
error.html          # 404/403
config.js           # WhatsApp number (deployado con cache corta)
assets/css/styles.css
assets/js/main.js
assets/img/         # SVG placeholders + PNG/WebP reales (trofeos3D, printer)
infra/              # Setup AWS (one-time)
deploy.sh           # Sync + invalidación
```

## Rutas existentes
- `/` → index.html
- Navegación por anchors: #hero, #fabricamos, #ideal-para, #productos, #proceso, #torneos, #empresas, #contacto
- No existe `/torneos` como página dedicada (sólo `#torneos` anchor en home)

## CTAs actuales
| Ubicación | Tipo | Mensaje WA |
|---|---|---|
| Nav | `[data-wa]` | mensaje genérico de config.js |
| Hero | `[data-wa]` | mensaje genérico |
| Hero | `href="#productos"` | anchor (target real es #fabricamos) |
| `#torneos` feature | `[data-wa-msg]` | "quiero armar kit para mi torneo" |
| CTA final | `[data-wa-msg]` | "tengo una idea" |
| Footer | `[data-wa]` | mensaje genérico |
| WA FAB | `[data-wa]` | mensaje genérico |

## Tracking existente
**NINGUNO.** Cero analytics: sin GA, sin Plausible, sin eventos custom.

## SEO actual
- `<title>`, `<meta description>`, `<canonical>` ✓
- OG tags + Twitter card ✓
- Schema.org LocalBusiness ✓
- `lang="es"` ✓
- Sin sitemap.xml
- Sin robots.txt
- `og:image` es SVG (algunos crawlers no lo procesan)
- `/torneos` no existe → sin indexación SEO para esa keyword

## Componentes reutilizables
- `.btn`, `.btn--primary`, `.btn--ghost`, `.btn--lg`
- `.section`, `.section--dark`, `.section--feature`, `.section--cta`
- `.section__title`, `.section__sub`, `.section__lead`
- `.grid`, `.grid--3`, `.grid--4`
- `.card-ideal`, `.card-fab`, `.card-product`
- `.process`, `.process__step`
- `.wa-fab`
- `buildWaUrl(msg)` en main.js — puede extenderse con `source`

## Problemas técnicos
1. `href="#productos"` en hero apunta a sección que ya fue renombrada a `#fabricamos`
2. Sin analytics de ningún tipo
3. `/torneos` no existe; sólo anchor `#torneos`
4. URL rewrite para paths sin extensión no configurado en CloudFront
5. Sin sitemap.xml ni robots.txt
6. Sección `#productos` usa SVG placeholders ilustrativos (no son renders reales)
7. Sin sticky CTA mobile

## Oportunidades detectadas
- `/torneos` tiene potencial SEO alto ("trofeos personalizados torneo" tiene búsqueda)
- `buildWaUrl` ya existe; agregar `source` no requiere refactor mayor
- FAQ con `<details>/<summary>` nativo: sin JS, accesible, rápido
- Sticky mobile CTA: conversion directa sin fricción
- CloudFront Function actual sólo maneja www→apex; se puede extender para URL rewrite
