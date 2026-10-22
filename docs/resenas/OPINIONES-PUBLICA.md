# /opiniones: lanzamiento público

Estado: preparado. Ningún archivo de esta fase se publicó todavía. La publicación la ejecuta el operador con el
bloque EC2 de abajo, después del preflight.

## 1. Auditoría de la página antes del lanzamiento

Qué existía en `opiniones.html` (commit 9b244ac):

- HTML con `<meta name="robots" content="noindex, follow">`, canonical correcto, title y OG. Único h1.
- JS (`assets/js/resenas-publicas.js`) que lee `/resenas/publicas.json`, arma tarjetas con `textContent` (nunca HTML),
  muestra estrellas con `role="img"` y texto, sello de pedido verificado, chips de aspectos, foto con `loading="lazy"`
  y `alt`, y fecha. Maneja 404 del export como lista vacía y otros errores como mensaje.
- CSS con los tokens de `styles.css`. Sin estado de carga, sin resumen visual, fotos sin relación de aspecto fija.
- Sin enlaces internos desde el sitio. Fuera del sitemap. Las demás páginas públicas no tienen meta robots.

Qué faltaba para considerarla página pública:

- Indexación: quitar el `noindex` (el resto del sitio no usa meta robots, así que la convención es su ausencia).
- Sitemap y enlace desde el footer de home, torneos y regalos. La home no tiene sección de testimonios: no hay
  contexto de confianza donde engancharlo sin saturarla.
- Estado de carga, vacío, error y datos en un solo lugar, sin saltos de diseño (altura reservada).
- Resumen con promedio y cantidad calculados del export, con coma decimal y una cifra. Sin "0,0" si no hay reseñas.
- Orden explícito de más recientes a más antiguas en el cliente (el export ya lo hace; el cliente no depende de eso).
- Fallback de fotos que no cargan, sin hueco roto.
- Skip link, foco visible, contraste medido (no supuesto), textos de estrellas.
- Copy honesto: la descripción dice lo que el sitio verifica (cada opinión llega con el enlace de un pedido y se revisa).

## 2. Diseño propuesto

- Cabecera: kicker, h1 "Opiniones de quienes eligieron ORK3D", lead sobre la revisión previa, CTA "Pedir presupuesto".
- Resumen: un bloque con estrellas redondeadas, "5,0 de 5 · 2 opiniones", calculado del export. Mientras carga, dice
  "Cargando opiniones…"; si no hay reseñas, un mensaje vacío; si falla el export, un mensaje de error sin tocar el resto.
- Listado: tarjetas con estrellas, sello "Pedido verificado" si corresponde, producto, comentario entre comillas, chips
  de aspectos, foto con relación 4:3 y alt, nombre público y fecha. Más recientes primero.
- Sin CTA debajo de la lista: lo que está debajo no se mueve al cargar (CLS medido < 0,1).
- Mobile: 320, 375, 390, 768 y escritorio sin scroll horizontal, con textos largos sin espacios.

## 3. Structured data: decisión

**No se agrega** `Product`, `AggregateRating` ni `Review`. Razones:

- La página reúne reseñas de varios productos. Un `Product` único o un `AggregateRating` de la página representaría
  mal las reseñas: el promedio de un trofeo no es el de una medalla.
- Son reseñas de productos que vende el mismo sitio. Eso es el caso de "reseña propia" que las guías de reseñas de
  Google no admiten como rich result.
- Sin una página de producto con sus propias reseñas, no hay un contenido fiel que marcar.

Cuando exista una página de producto con reseñas propias, se evalúa por página, con esa misma regla.

## 4. Archivos del lanzamiento

Fuente única: `infra/opiniones-archivos.tsv`. Solo estos 7 se suben:

| Archivo | Clave en S3 | Caché | Invalidación |
| --- | --- | --- | --- |
| `opiniones.html` | `opiniones.html` | 60 s | `/opiniones`, `/opiniones.html` |
| `index.html` | `index.html` | 60 s | `/`, `/index.html` |
| `torneos.html` | `torneos.html` | 60 s | `/torneos`, `/torneos.html` |
| `regalos-empresariales-fin-de-ano.html` | igual | 60 s | ruta limpia y `.html` |
| `sitemap.xml` | `sitemap.xml` | 300 s | `/sitemap.xml` |
| `assets/css/opiniones.css` | igual | 300 s | su ruta |
| `assets/js/resenas-publicas.js` | igual | 300 s | su ruta |

No se suben: `resenas/` (export, fotos), `api/`, `admin/`, `admin-web/`, `infra/`, `dev/`, `docs/`, `styles.css`,
`main.js`, `config.js`, ni CloudFront, IAM, Lambdas o DNS. Sin `sync`, sin `--delete`.

## 5. Tests

- `infra/tests/test_opiniones_publica.py` (41): robots, canonical, title/description/OG, un h1, landmarks, skip link,
  sin schema, CTA en la cabecera, sitemap sin `lastmod`, enlaces del footer, JS sin HTML dinámico, sin campos privados,
  peso, manifiesto exacto, scripts sin `sync`/`--delete`/scripts peligrosos, verificación solo de lectura, export real
  (`construir_export`: solo aprobadas, orden, allowlist, smoke ocultas o rechazadas fuera), y publicación/reversión
  contra el simulador (sube solo el manifiesto, invalida solo sus rutas, copia privada con cabeceras, reversión exacta).
- `infra/tests/test_prod_lectura.py` (37): lectura de producción en modo `oculta` y `lanzada`, indexable, canonical,
  enlace, sitemap y smoke fuera del export.
- `dev/e2e/opiniones.e2e.js` (19, Chromium real): indexable, sitemap, enlaces, export sin campos privados, orden,
  promedio, rating inválido, vacío, error, carga, fotos (lazy, alt, fallback), CLS, móvil 320–1280, accesibilidad,
  contraste WCAG AA medido, teclado, enlaces, consola y red, resize.

## 6. Preflight, publicación, verificación y reversión

- `infra/opiniones-verify.sh pre`: lectura de producción con `/opiniones` oculta, notificaciones ON, export sano y
  ninguna reseña de prueba visible. Si una aparece: DETENER.
- `infra/opiniones-publicar.sh diff`: lista exacta de archivos, SHA-256 local contra S3, repo limpio y HEAD = origin.
- `ALLOW_OPINIONES_PUBLISH=1 infra/opiniones-publicar.sh publicar`: copia privada (600), sube el manifiesto, invalida y espera.
- `infra/opiniones-verify.sh post`: lectura de producción con `/opiniones` lanzada, title, assets y export.
- `infra/opiniones-navegador.js`: Chromium contra producción (solo lectura): el promedio y la cantidad que se ven
  coinciden con el export real, una tarjeta por reseña, sin errores de consola ni 4xx/5xx.
- Reversión: `ALLOW_OPINIONES_PUBLISH=1 infra/opiniones-publicar.sh revertir`. Restaura bytes y cabeceras de la
  copia más reciente. No toca reseñas, export, fotos ni backend.

## 7. Reseñas de prueba

Las reseñas de prueba no se borran físicamente. Se ocultan o rechazan desde el panel, con los estados soportados:

- las dos históricas (`633729550934477689fafb8f576dd851`, `d1cc45e8f15a425890e2a3a8cbbc9ebd`) → `hidden` (ocultar);
- la de notificaciones → `rejected` (rechazar).

Al ocultar o rechazar, el panel regenera el export. Ese mismo proceso borra la copia pública de la foto de una reseña
que ya no está aprobada (`reparar_fotos_publicas`). Es el comportamiento existente; los scripts no borran nada en S3.

## 8. Riesgos y pendientes

- La página lanzada depende del contenido: con las reseñas de prueba ocultas y sin reseñas reales aprobadas, se ve el
  estado vacío. Lanzarla sin opiniones reales es un riesgo de imagen, no técnico.
- La imagen social es `og-cover.svg`, la misma de todo el sitio. Algunas plataformas no renderizan SVG en previews;
  cambiarla exige un PNG o JPG nuevo, que no se fabricó.
- El skip link está solo en esta página (las demás no lo tienen). Agregarlo al resto es un cambio aparte.
- La indexación depende de Google: después del deploy, la página puede tardar en aparecer en resultados.
- No hay enlace en la navegación principal, solo en los footers, para no saturar la home.
