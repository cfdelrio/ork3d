# Panel admin · deploy controlado del rediseño (UX/UI)

Commit revisado: `590ed7df180202390fda8a523b712b1c4e04c8cf` (rediseño aprobado).
Commit anterior: `e8052cde6e89a11595c15e7e13a299c51bc1ac7a` (lanzamiento de `/opiniones`).
Branch: `claude/ork3d-landing-page-b2fQr`. El despliegue se ejecuta **desde EC2**. Este documento no ejecuta nada.

## Qué se publica

Solo los estáticos del panel que cambiaron. La lista sale de `git diff --name-only e8052cd..590ed7d` cruzada con los estáticos del panel:

| Archivo (= clave S3) | Content-Type | Cache-Control | Invalida |
|---|---|---|---|
| `admin-web/entrar.html` | `text/html; charset=utf-8` | `no-store, max-age=0` | `/admin/entrar`, `/admin/entrar.html`, `/admin-web/entrar.html` |
| `admin-web/resenas.html` | `text/html; charset=utf-8` | `no-store, max-age=0` | `/admin/resenas`, `/admin/resenas/*`, `/admin/resenas.html`, `/admin-web/resenas.html` |
| `assets/css/admin.css` | `text/css; charset=utf-8` | `no-store, max-age=0` | `/assets/css/admin.css` |
| `assets/js/admin-comun.js` | `application/javascript; charset=utf-8` | `no-store, max-age=0` | `/assets/js/admin-comun.js` |
| `assets/js/admin-panel.js` | `application/javascript; charset=utf-8` | `no-store, max-age=0` | `/assets/js/admin-panel.js` |

Manifiesto: `infra/admin-ui-archivos.tsv`. Las cabeceras son las mismas que usa `infra/admin-frontend-deploy.sh` para el panel.

**`assets/js/admin-entrar.js` no se sube.** El diff `e8052cd..590ed7d` no lo incluye y su blob es idéntico en ambos commits (`b766729397c35095bcb2722f50dd2f7e79d01285`). Solo cambió la referencia en `entrar.html` (`?v=1` → `?v=2`). Ese `?v=2` apunta al objeto actual, que tiene el mismo contenido. La verificación post comprueba que lo servido en `?v=2` es byte a byte el de `590ed7d`.

**No se toca:** Lambdas, IAM, SES, la configuración de CloudFront, la función CloudFront, DNS, el bucket privado, reseñas, export público, notificaciones, `/opiniones`, `/opinar`, API admin, sesiones, auth y CSRF. En CloudFront solo se crea una invalidación con las rutas listadas.

## Guardas (`publicar` y `diff` se niegan si…)

- el repo tiene cambios sin commitear;
- HEAD no es igual a `origin/claude/ork3d-landing-page-b2fQr` (`ls-remote`);
- el manifiesto no es exactamente la intersección de `git diff e8052cd..590ed7d` con los estáticos del panel;
- falta algún archivo del manifiesto, o su contenido en HEAD no es el de `590ed7d`.

**Decisión sobre "HEAD == 590ed7d":** la regla literal no sirve después del commit de este tooling, porque HEAD pasa a ser ese commit y además tiene que ser igual a origin. Por eso la guarda exige que **el contenido** de los 5 archivos sea el revisado (mismo blob) y que `590ed7d` sea ancestro de HEAD. Así se publica exactamente lo revisado, sin bloquear el tooling.

## Bloque 1 · preflight (EC2, solo lectura)

```bash
cd /ruta/al/repo                       # checkout limpio del branch
git status --porcelain                 # debe salir vacío
git fetch origin claude/ork3d-landing-page-b2fQr
git merge --ff-only origin/claude/ork3d-landing-page-b2fQr

infra/admin-ui-publicar.sh diff        # guardas + diferencias local ↔ S3. No escribe.
SMOKE_NOTIFY_ID=<id de la reseña de prueba de notificaciones> infra/admin-ui-verify.sh pre
```

`SMOKE_NOTIFY_ID` es obligatorio: sin él no se puede confirmar que la reseña de prueba no esté publicada. `pre` no hace login, no envía POST, no envía correos y no crea reseñas.

**Qué esperar en `pre`:** `PASS` en la lectura de producción (`/opiniones` indexable, `/opinar` sano, `www` con 301, API admin sin cookie con 401, notificaciones encendidas, logs sin cookies), `PASS` en CSP, noindex, sin `style=` y sin scripts inline del panel actual, y `PASS` en los assets que la página actual referencia (`?v=1`).

El estado real de producción hoy incluye un hueco conocido, modelado explícitamente (ver siguiente sección): `/assets/css/admin.css?v=1` responde 404 porque el archivo nunca fue publicado. El resultado esperado de `pre` es entonces:

```
KNOWN GAP  assets/css/admin.css?v=1 no existe en producción; será creado por este deploy
Verificación pre: todo PASS, con 1 KNOWN GAP explícito (assets/css/admin.css: lo crea este deploy).
```

### Hueco conocido: `assets/css/admin.css`

El único 404 aceptable en `pre` es `/assets/css/admin.css?v=1`, y solo si se cumplen **todas** estas condiciones. Cada una tiene su propio caso negativo en los tests:

1. el HTML actual de `/admin/resenas` o `/admin/entrar` referencia `/assets/css/admin.css?v=1`;
2. `head-object` confirma que `assets/css/admin.css` no existe en el bucket, con 404 explícito (un 403 o cualquier otro error **no** cuenta como ausencia);
3. los demás assets del panel responden 200 y el resto del preflight pasa;
4. el manifiesto contiene `assets/css/admin.css` exactamente una vez;
5. el CSS local es exactamente el blob revisado en `590ed7d`;
6. la respuesta de `/assets/css/admin.css?v=1` es exactamente 404.

Además, en `pre` cada asset del panel que referencia el HTML actual tiene que ser `?v=1`: otra versión es FAIL. Si el CSS **existe** y responde 200, no hay hueco y el control es un PASS normal. En `post` no existe excepción: `admin.css?v=2` tiene que responder 200 con el blob revisado.

Lo que sigue siendo FAIL en `pre`: falta cualquier otro asset (`admin-comun.js`, `admin-panel.js`, `admin-entrar.js`), el HTML pide una versión inesperada, el CSS existe pero responde algo distinto de 200, el manifiesto no contiene el CSS, el contenido local no es el revisado, o hay cualquier otra regresión del preflight (en ese caso el hueco tampoco se acepta y se informa por separado).

El hueco se **informa**, no se oculta. Después del deploy, `admin.css` queda creado con sus cabeceras y el hueco deja de existir; `post` verifica que el asset responde 200 con los bytes revisados.

## Bloque 2 · deploy (EC2)

Solo después de un `pre` con todo en PASS y de revisar la salida de `diff`:

```bash
cd /ruta/al/repo
infra/admin-ui-publicar.sh diff                              # revisar: 5 archivos, IGUAL/DISTINTO/NUEVO
ALLOW_ADMIN_UI_PUBLISH=1 infra/admin-ui-publicar.sh publicar # copia previa (600) → 5 put-object → invalidación
SMOKE_NOTIFY_ID=<id> infra/admin-ui-verify.sh post           # verificación de lo servido
```

`publicar` guarda la copia previa **antes** de subir nada, muestra su ruta y la registra en `ULTIMA-COPIA`. Si una subida falla a mitad, el script corta, informa que la publicación quedó parcial y dice cómo revertir. Las copias anteriores no se borran.

**Salida esperada de `publicar` (forma):**

```
── Guardas del repo ──
  repo: rama claude/ork3d-landing-page-b2fQr · HEAD <sha> = origin · contiene 590ed7df…
  manifiesto = intersección de git diff e8052cde…..590ed7df… con los estáticos del panel (5 archivos)
── Copia previa (600) ──
  admin-web/entrar.html: existía (copiado con sus cabeceras)  … (una línea por archivo)
  copia de rollback: /tmp/ork3d-admin-ui/copias/<stamp> (archivos 600)
── Subida ──
  subido: admin-web/entrar.html (Content-Type y Cache-Control verificados)  … (5 líneas)
── Invalidación de CloudFront ──
  invalidación: <id> (10 rutas, completada)
Publicado. Siguiente: infra/admin-ui-verify.sh post
```

**Salida esperada de `post`:** todas las líneas `PASS`, incluidas `PASS  /admin/resenas = admin-web/resenas.html en 590ed7df…` (y lo mismo para cada página y cada asset `?v=2`), `PASS  … versión ?v=2 (cache bust)`, `PASS  … Content-Type …`, `PASS  … no-store`, `PASS  4 texto(s) presentes …` (sección, sesión, invitación con slug y nombre visible), `PASS  1 texto(s) presentes … Publicada`, `PASS  sin innerHTML …` y `Verificación post: todo PASS.` El navegador de solo lectura puede salir como `INFO` si Playwright no está instalado en EC2; no es un fallo del despliegue.

## Bloque 3 · rollback (EC2)

Restaura exactamente la copia más reciente: bytes, Content-Type, Cache-Control y metadata. Los objetos que no existían antes se quitan, solo esos. Después invalida las mismas rutas:

```bash
cd /ruta/al/repo
ALLOW_ADMIN_UI_PUBLISH=1 infra/admin-ui-publicar.sh revertir
SMOKE_NOTIFY_ID=<id> infra/admin-ui-verify.sh pre
```

Para volver a una copia concreta: `ADMIN_UI_COPIA=/tmp/ork3d-admin-ui/copias/<stamp> ALLOW_ADMIN_UI_PUBLISH=1 infra/admin-ui-publicar.sh revertir`.

`revertir` no mira el repo: restaura desde la copia, así funciona aunque el checkout haya cambiado.

## Bloque 4 · checks post-deploy

`infra/admin-ui-verify.sh post` comprueba:

- HTML de `/admin/resenas` y `/admin/entrar` byte a byte igual a `590ed7d`, con `no-store`;
- los assets que referencian, con `?v=2`, 200, Content-Type y `no-store`, y bytes iguales a `590ed7d`;
- sección actual (`id="seccion-actual"`), indicador de sesión (`id="estado-sesion"`), `Publicada`, y la invitación con slug y nombre visible separados;
- CSP presente, noindex exacto, sin atributos `style=`, sin scripts inline, sin `innerHTML` ni HTML crudo en el JS servido;
- API admin sin cookie con 401 `no_autenticado`;
- `/opiniones`, `/opinar`, `www`, export sin reseñas de prueba, notificaciones encendidas, logs sin cookies (vía `infra/prod-lectura-v2.sh`).

**Fuera de la automatización (paso humano):** iniciar sesión con un enlace real del correo y revisar la consola del navegador. El script no hace login a propósito.

## Tests

`infra/tests/test_admin_ui_publica.py` (47 casos, sin AWS ni red). Usa clones git reales, un bucket y un CloudFront simulados (`aws_fake_admin_ui.py`) y un sitio simulado (`aws_fake_curl_admin_ui.py`). Cubre:

- manifiesto exacto y su igualdad con la intersección del diff, y que `admin-entrar.js` queda fuera;
- rutas de invalidación acotadas, nunca `/*`;
- ausencia de `sync`, `--delete`, `s3 rm`, `--recursive`, `s3 cp` y directorios completos;
- `delete-object` solo en `revertir`, y CloudFront solo para invalidar;
- verificación sin escrituras y con curl solo GET; navegador sin clics y abortando lo que no sea GET al mismo origen;
- guardas: repo sucio, HEAD distinto de origin, contenido distinto de `590ed7d`, archivo ausente, manifiesto distinto de la intersección;
- `publicar` sin allow (código 3, sin llamadas a AWS); copia 600; solo los 5 `put-object`; invalidación acotada;
- `revertir` exacto (bytes, cabeceras, metadata, borrado solo de lo que no existía);
- copias que no se borran; publicación parcial informada; revertir sin copia;
- `pre` y `post` contra el sitio simulado: pasan en el estado correcto y fallan con HTML de la versión anterior, `style=`, `innerHTML`, asset sin `?v=2` y reseña de prueba visible en el export.
- el hueco de `admin.css?v=1`: se acepta solo con las seis condiciones y se informa como KNOWN GAP. Cada condición tiene un caso negativo (head-object con 403, existe en S3 pero responde 404, responde 500, falta otro asset, versión inesperada, manifiesto sin el CSS, CSS local distinto, otra regresión que lo tapa). En `post` nunca se acepta.

## Qué no se ejecutó

No se llamó a AWS desde este entorno: los tests usan simuladores. No se publicó ni se invalidó nada. Los scripts no se probaron contra el bucket real: eso ocurre en EC2, empezando por el bloque 1.
