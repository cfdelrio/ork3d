# ORK3D — Eventos de analytics

## Implementación
`ORK3D.track(event, props)` en `assets/js/main.js`.
- Si `gtag` está disponible → dispara GA4.
- Si `plausible` está disponible → dispara Plausible.
- En desarrollo → `console.debug`.

Para activar GA4 agregar en `<head>` de cada página:
```html
<script async src="https://www.googletagmanager.com/gtag/js?id=G-XXXXXXXXXX"></script>
<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}gtag('js',new Date());gtag('config','G-XXXXXXXXXX');</script>
```

---

## Eventos

### `view_tournaments`
Disparo: DOMContentLoaded en `/torneos` o `/torneos.html`.

| Propiedad | Tipo | Ejemplo |
|---|---|---|
| `page` | string | `/torneos` |

---

### `click_whatsapp`
Disparo: click en cualquier CTA que abre WhatsApp.

| Propiedad | Tipo | Valores posibles |
|---|---|---|
| `source` | string | ver tabla abajo |
| `page` | string | `/` o `/torneos` |

#### Sources
| Source | Ubicación |
|---|---|
| `tournament_hero` | Hero de /torneos |
| `tournament_awards` | Sección premios de /torneos |
| `tournament_process` | Sección proceso de /torneos |
| `tournament_deadline` | Sección "¿Cuándo es la final?" |
| `tournament_footer` | CTA final de /torneos |
| `tournament_nav` | Botón nav en /torneos |
| `homepage_tournaments` | Sección torneos de / |
| `sticky_mobile` | Sticky CTA mobile (cualquier página) |
| `generic` | FAB, footer WA, nav home |

---

### `start_quote`
Disparo: usuario ingresa fecha en el campo de /torneos antes de abrir WhatsApp.

| Propiedad | Tipo | Ejemplo |
|---|---|---|
| `source` | string | `tournament_deadline` |
| `page` | string | `/torneos` |

---

## Implementación via data-attributes
```html
<a data-wa data-wa-source="tournament_hero" data-wa-msg="...">Cotizar</a>
```
El atributo `data-wa-source` es leído por `wireWaLinks()` en main.js.
El botón de deadline (`id="deadlineCta"`) NO usa `data-wa`: es cableado
manualmente por `setupDeadlineCta()` para incluir la fecha en el mensaje.
