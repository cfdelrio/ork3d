---
name: ork3d-web-safe-changes
description: Protege el SEO, la conversión y la arquitectura técnica de ork3d.com al modificar páginas públicas, home, landings, textos, títulos, navegación, enlaces internos, imágenes o video, FAQ, datos estructurados, sitemap, CTA, CSS/JS públicos, páginas nuevas o reglas de publicación. Usala para cualquier cambio en el sitio público, sin frenar la evolución del sitio.
---

# ORK3D Web Safe Changes

El objetivo no es impedir cambios. Es hacer el cambio pedido sin destruir por accidente la intención de búsqueda, los enlaces internos, los activos de conversión o la infraestructura que ya funciona.

## 1. Posicionamiento

ORK3D no se presenta primero como "impresión 3D". La impresión 3D y la fabricación digital son tecnologías de producción. La propuesta es más amplia: **objetos personalizados diseñados alrededor de lo que una persona, una empresa o un evento quiere representar.**

Conceptos ya establecidos: diseño personalizado, premios personalizados, reconocimientos, regalos empresariales, identidad de marca, personalización individual, producción escalable, objetos con significado y proyectos especiales.

- Para B2B: no partimos de un premio de catálogo. Partimos de lo que el cliente quiere reconocer.
- Mensaje central: **"Una identidad común. Un detalle único para cada persona."**
- No reemplaces este posicionamiento por lenguaje genérico de imprenta o de fabricación.

## 2. Antes de tocar nada

Siempre inspeccioná la implementación actual. No edites desde suposiciones.

```
git fetch origin
git status --short
git branch --show-current
git rev-parse HEAD
git rev-parse origin/main
```

- Para trabajo nuevo, partí de `origin/main` actualizado, en una rama nueva:
  ```
  git switch --detach origin/main
  git switch -c feature/<descripcion-corta>
  ```
  Si la sesión te asignó un nombre de rama, usá ese.
- **No reutilices una rama de feature vieja solo porque el nombre es cómodo.**
- **Si el árbol de trabajo está sucio, PARÁ.** Reportá los archivos antes de modificar cualquier cosa.

## 3. Leé antes de editar

Antes de cambiar una página, revisá:

1. el HTML existente;
2. el `<title>`;
3. la meta description;
4. el canonical;
5. el H1;
6. los demás encabezados;
7. el JSON-LD;
8. los enlaces internos;
9. el sitemap;
10. las landings relacionadas;
11. el CSS compartido con otras páginas;
12. los tests que cubren esa funcionalidad.

**Si editás copy de FAQ, revisá las dos cosas:** la FAQ visible y el JSON-LD FAQ. Tienen que quedar consistentes.

## 4. Invariantes de SEO

Salvo que la tarea pida cambiarlos explícitamente, conservá:

- la URL canónica;
- la indexabilidad;
- un H1 significativo y único;
- la intención de búsqueda de la página;
- los enlaces internos relevantes;
- la presencia en el sitemap;
- un title y una meta description válidos;
- la consistencia de los datos estructurados.

Reglas duras:

- **No agregues `noindex` a una página comercial indexable.**
- No cambies canonicals ni URLs por casualidad.
- No crees varias páginas apuntando esencialmente a la misma consulta sin analizar canibalización.

## 5. Title, H1, meta, canonical y robots no son cosméticos

Cambiar `<title>`, H1, meta description, canonical, URL o directivas robots es un cambio de SEO. Antes de hacerlo, reportá:

```
BEFORE:
...

AFTER:
...

SEARCH INTENT:
...

REASON:
...
```

Si el cambio visual pedido no requiere cambio de SEO, no toques esos elementos.

## 6. Keywords

- Usá las keywords de forma natural. Nada de keyword stuffing.
- La claridad comercial tiene prioridad sobre repetir frases exactas.
- Ejemplo: el title puede ser "Reconocimientos empresariales personalizados | ORK3D" mientras el H1 para humanos es "Reconocimientos diseñados para representar logros de verdad.". Cada uno cumple su función.
- La intención se comunica de forma semántica en title, eyebrow, encabezados, cuerpo y enlaces internos. No fuerces la misma keyword en todos los encabezados.

## 7. Los enlaces internos son arquitectura

Antes de borrar o cambiar un enlace, determiná qué función cumple. Relaciones que hoy existen:

```
HOME
  ↓
/reconocimientos-empresariales

/regalos-empresariales-fin-de-ano
  ↓
/reconocimientos-empresariales
```

- Las páginas relevantes pueden enlazar a `/opiniones` como prueba de confianza.
- No elimines estas relaciones por accidente al simplificar secciones.
- Al crear una landing pública nueva, revisá si necesita **enlaces desde** páginas existentes, no solo enlaces hacia ellas.

## 8. Casos reales

Los casos reales son activos de conversión fuertes (fotos, videos, prototipos, productos terminados). Usá solo material real.

No inventes:

- clientes, proyectos ni testimonios;
- cantidades, resultados o métricas de ventas;
- nombres de empresas.

Si una imagen o video real muestra un nombre de persona, marca de cliente o información privada:

- no lo repitas innecesariamente en alt, SEO, schema ni copy;
- la autorización de publicación es un requisito de negocio aparte. **No asumas autorización.**

## 9. Mapa de intenciones

Cada página tiene una intención distinta. No las hagas competir. Detalle en `referencias/mapa-de-intenciones.md`.

- `/` — hub general. Puede presentar varias verticales, pero no reproducir landings completas.
- `/torneos` — deportes y competencias. No convertirla en landing B2B genérica.
- `/regalos-empresariales-fin-de-ano` — estacional. No la conviertas en la página permanente de reconocimientos.
- `/reconocimientos-empresariales` — evergreen. Debe seguir siendo evergreen; no la conviertas en tema navideño o de fin de año.
- `/opiniones` — solo reseñas reales publicadas. Nunca generes reseñas falsas.
- `/opinar/*` — flujo de recolección de reseñas. No lo alteres en trabajo ordinario del sitio.

## 10. Lo que nunca hacés

- Hacer `push` ni `merge` a `main`.
- Ejecutar deploy, comandos de AWS ni cambios de infraestructura.
- Borrar contenido del bucket, de `resenas/` o del panel admin.
- Commitear credenciales, claves, tokens o archivos `.env`.
- Publicar material real de clientes sin autorización explícita.
- Cambiar `/opinar/*` o el flujo de reseñas en un trabajo de contenido.

Los temas que requieren aviso previo y confirmación están en `referencias/temas-sensibles.md`. Antes de tocar uno, escribí:

> ⚠️ Tema sensible: <nombre>. Qué cambiaría: <descripción concreta>. ¿Lo hago?

## 11. Validación antes del PR

```
python3 dev/validar.py
```

- Para cambios en páginas, corré el E2E que corresponda (`dev/e2e/*.e2e.js`, con `NODE_PATH=/opt/node22/lib/node_modules`).
- Para cambios grandes, corré `dev/suite.sh` completo.
- En el PR, incluí: qué cambió y por qué, el bloque BEFORE/AFTER si tocaste SEO, los temas sensibles tocados ("ninguno" o la lista con la confirmación recibida) y el resultado de las validaciones.
- Subí solo tu rama (`git push -u origin <rama>`). El merge lo decide el dueño del proyecto.

Si notás desborde horizontal, CTA invisibles, enlaces rotos o un test que falla y no sabés por qué, no lo "arregles a ciegas": reportalo.
