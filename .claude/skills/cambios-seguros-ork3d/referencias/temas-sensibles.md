# Temas sensibles de ORK3D

Esta lista la mantiene el dueño del proyecto. Cualquier cambio en estos puntos requiere el aviso de `SKILL.md` y la confirmación antes de tocarlos.

## 1. Textos aprobados

Estos textos están aprobados. Si cambian, avisá antes:

| Dónde | Texto vigente |
|---|---|
| Landing de regalos (H1) | "Una misma identidad. Un regalo único para cada persona." |
| Landing de reconocimientos (eyebrow) | "RECONOCIMIENTOS EMPRESARIALES PERSONALIZADOS" |
| Landing de reconocimientos (H1) | "Reconocimientos diseñados para representar logros de verdad." |
| Landing de reconocimientos (title) | "Reconocimientos empresariales personalizados \| ORK3D" |
| Beneficios del hero (landing de regalos y de reconocimientos) | "Respuesta rápida", "Sin grandes pedidos mínimos", "Personalización individual" |
| CTA principal | "Pedir una propuesta" |

## 2. Promesas comerciales

No agregues ni cambies estas afirmaciones sin aviso:

- Plazos de entrega o tiempos de respuesta concretos.
- Cantidades mínimas o máximas concretas.
- Precios, descuentos, garantías o políticas de devolución.
- Afirmaciones sobre clientes, cifras o resultados.
- Testimonios o reseñas. Solo se publican reseñas reales.

## 3. Material real de clientes

Video y fotos del caso real (`assets/video/caso-real/`, `assets/img/caso-real/`) muestran un proyecto real:

- No cambies, recortes ni edites la pieza ni el material.
- No generes versiones artificiales ni imágenes con IA del proyecto.
- No repitas nombres de personas, montos, marca del cliente ni la frase de la pieza en texto visible, `alt`, `aria-label`, SEO ni JSON-LD.
- No subas material nuevo de clientes sin autorización explícita del dueño.

## 4. Identidad visual

- Paleta oficial: negro `#191919`, crema `#F3F0E9`, coral `#FF704E`, lima `#D6F17C`, lila `#B5B3F4`. Se usan los tokens de `assets/css/styles.css`; no inventes colores.
- Logo y nombre comercial "ORK3D".
- Tipografía global y estilos compartidos entre páginas.
- Cambios de diseño en la home, en el menú o en el footer de todas las páginas.

## 5. Contacto

- Número de WhatsApp en `config.js` y en los enlaces `data-wa`.
- Correo de contacto `hola@ork3d.com` (ubicación definida por marketing).
- Formularios de cotización y sus destinos.

## 6. Publicación y producción

- `deploy.sh`, `.github/workflows/deploy.yml` y cualquier cambio de los `paths` del workflow.
- `infra/`, AWS, CloudFront, buckets y funciones de CloudFront.
- Cualquier cambio que, al mergearse a `main`, se publique en producción sin revisión.
- Navegación global del sitio (menú, footer, sitemap).

## 7. Reviews V2 y panel admin

- `/opiniones`, `/opinar`, `/admin` y `admin-web/`.
- `api/`, `admin/`, `resenas/` y las Lambdas.
- Nada de esto se toca como parte de un cambio de contenido o de diseño.
