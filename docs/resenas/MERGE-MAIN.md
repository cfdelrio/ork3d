# Integración de la rama a main: auditoría y plan

**Estado (2026-10-23): integrado en `main` (`d616cc2`).** Este documento es el registro de la auditoría previa al merge. Las referencias a la rama de feature y los comandos de merge se conservan como registro. El estado vigente está en `ESTADO-PRODUCCION.md`.

Rama: `claude/ork3d-landing-page-b2fQr`. Destino: `main`. Este documento no integra nada ni despliega nada.

## 1. Estado de las refs

| Ref | Valor |
|---|---|
| `origin/main` | `e622ecf9f5bd6b78a2122a3481adea0b14acff6c` |
| HEAD de la rama (antes de este cambio) | `0c07eb8189791b4143a2433435a22245a5d2d16d` |
| merge-base | `e622ecf` (= origin/main) |
| commits exclusivos de main | **0** |
| commits exclusivos de la rama | **37** |
| `origin/main` es ancestro de HEAD | sí: el merge sería un **fast-forward** |
| archivos en `main...rama` | 110 (89 añadidos, 21 modificados) |

Como main no tiene commits propios, no hay conflictos de merge en git. El riesgo está en lo que el push a main dispara (secciones 2 a 4), no en el merge.

Commits exclusivos de la rama (de más reciente a más antiguo):

```
0c07eb8 chore(resenas): modela el hueco conocido de admin.css en el preflight
96a8774 chore(resenas): prepara deploy seguro del admin ui
590ed7d feat(resenas): mejora experiencia visual del panel admin
e8052cd feat(resenas): lanza pagina publica de opiniones
9b244ac fix(resenas): tolera concurrencia no configurada en snapshot
12fd38d feat(resenas): prepara despliegue seguro del notifier publico
97fd1dc feat(resenas): prepara la activación controlada del aviso de reseña nueva
06011b6 fix(resenas): corrige auditoria de secretos en CloudWatch
7f8df26 test(resenas): verifica logs de CloudWatch en la lectura de produccion
1451fd4 test(resenas): cierra seguridad y regresion V2 admin
a04d96b fix(resenas): registro de sesion o login corrupto responde 401, no 500
bc58135 fix(resenas): el registro descarta valores con forma de token
b63fcb2 fix(resenas): evita moderacion accidental desde el panel admin
ffb86ed fix(resenas): usa etag development al publicar CloudFront Function
582405a fix(resenas): publica ork3d-redirect-www como LIVE más bloque admin
20ca3cf feat(resenas): agrega publicación controlada del panel admin
9c890ff fix(resenas): responde 400 a los rechazos CSRF de la API admin
5d6b45c fix(resenas): diagnostica el 404 de la API admin en el borde de CloudFront
1dc51b5 fix(resenas): corrige routing real de API admin
a266140 fix(resenas): completa cache behavior admin de CloudFront
29a691e fix(resenas): limita comentario de ORP para CloudFront
a230d92 fix(resenas): corrige ORP para OAC de Lambda admin
f4f0459 chore(resenas): prepara deploy controlado del admin V2
202ecb7 chore(resenas): prepara smoke SES de V2
291b1d0 fix(resenas): corrige preflight AWS real
2f979c2 ci: limita deploy productivo a main
0aaf812 chore(resenas): prepara preflight AWS de V2
dbd91d4 docs(resenas): OAC, hash del body y gate de If-Match
29657dc fix(infra): dos permisos de OAC, política de origen propia y métodos de CloudFront
f804627 fix(resenas): x-amz-content-sha256 en cada POST del panel y emulación de OAC en dev
b91b09d test(resenas): cubre 430 px en el diseño del panel admin
84061e6 docs(resenas): contrato V2, README y runbook con el plan sin ejecutar
334d7fc test(resenas): privacidad y paridad con el CLI sobre el mismo almacén
6fc8b70 feat(resenas): panel admin web y servidor de desarrollo con /admin/api
0a44672 ops(resenas): scripts del panel en DRY_RUN y políticas mínimas
b0c3332 feat(resenas): aviso por correo al moderador desde la Lambda pública
483d6fa feat(resenas): núcleo reusable y API admin V2 con tests
```

## 2. Auditoría del workflow (`.github/workflows/deploy.yml`)

Se leyó completo. En `origin/main` el archivo es distinto: no tiene la guarda `if:` del job ni `admin-web/**` en los paths.

- **Triggers:** `push` a `main` con paths, y `workflow_dispatch`. Sin otros.
- **Paths del push:** `index.html`, `torneos.html`, `regalos-empresariales-fin-de-ano.html`, `error.html`, `opinar.html`, `opiniones.html`, `config.js`, `sitemap.xml`, `robots.txt`, `assets/**`, `admin-web/**`, `deploy.sh`, `.github/workflows/deploy.yml`.
- **Guarda:** `if: github.ref == 'refs/heads/main'` a nivel de job. Un `workflow_dispatch` desde otra rama se salta el job. Sin esa guarda en `main`, el job corre en cualquier push que cumpla los paths.
- **Entorno:** `environment: production`. Si ese entorno tiene revisores requeridos, no se puede verificar desde el repo: hay que revisarlo en Settings → Environments.
- **Concurrencia:** `group: deploy-prod`, `cancel-in-progress: false`.
- **Permisos:** `contents: read`.
- **Secretos:** `AWS_ACCESS_KEY_ID` y `AWS_SECRET_ACCESS_KEY` (claves de larga duración, no OIDC). Variables: `BUCKET`, `DISTRIBUTION_ID`.
- **Comandos:** checkout, `configure-aws-credentials` a us-east-1, y `./deploy.sh`.
- **¿Llama a deploy.sh?** Sí, es el único paso que escribe.
- **¿Puede ejecutar `sync --delete`?** Con el deploy.sh de `main` (y con el de la rama antes de este cambio), **sí**: en cada corrida del job. El deploy.sh nuevo de esta rama no tiene `--delete` (§6).
- **¿Publica admin?** En `main` no hay `admin-web/` ni JS/CSS admin. En la rama sí los hay, y el deploy.sh actual los subiría (ver §3).
- **¿Publica /opiniones?** Sí (`opiniones.html`, y `assets/**`).
- **¿Toca `resenas/`, `admin/`, `api/`?** No, están excluidos del sync. Ver §3 sobre la fragilidad de esa exclusión.
- **¿Modifica CloudFront Function?** No. Solo hace `create-invalidation`.
- **Invalidación:** `/*` en cada corrida.

## 3. Auditoría de `deploy.sh`

`deploy.sh` es idéntico en `main` y en la rama (`git diff origin/main HEAD -- deploy.sh` vacío). Se leyó completo.

**Qué sube:** un `aws s3 sync . s3://$BUCKET --delete` con exclusiones; después `cp` explícitos de HTML, `config.js`, `opinar.html`, `opiniones.html`; y `cp --recursive` de `assets/` (tres veces: todo, AVIF y WebP).

**Qué excluye el sync** (en el original): `.git`, `.github`, `infra`, `deploy.sh`, `verify.sh`, `README.md`, `.gitignore`, `claude.md`, `*.sh`, `*.md`, `api/*`, `admin/*`, `dev/*`, `docs/*`, `resenas/*`.

**Qué NO excluye y sube con cabeceras públicas de 300 s:** `admin-web/*` y `assets/**` (incluido el panel admin). El `cp --recursive` de `assets/` no tiene exclusiones.

**Qué podría borrar con `--delete`:** cualquier objeto del bucket que (a) no esté en el repo y (b) no caiga en una exclusión. Según la semántica de la CLI (§5), las exclusiones también aplican a los objetos del destino, así que `resenas/`, `api/`, `admin/`, `dev/`, `docs/` e `infra/` quedan protegidos por el filtro. Lo que no está protegido es la raíz del sitio, `assets/**` y `admin-web/**`.

**Cabeceras (problema independiente del borrado):** el deploy de `main` dejaría `admin-web/*` y los estáticos admin con `Cache-Control: public, max-age=300`, cuando el panel exige `no-store`. Como `main` no tiene esos archivos, el primer merge los introduciría con las cabeceras equivocadas. Esto bloquea el merge con el script actual.

**`sitemap.xml`:** el sync lo sube con `public, max-age=300`, sin `must-revalidate`. El lanzamiento de `/opiniones` lo publicó con `must-revalidate`. Pequeña regresión de cabecera.

## 4. Inventario de riesgo S3

Lo que hay en el bucket del sitio (`ork3d.com`) se infiere del repo y del código de la aplicación. **No se listó el bucket**: no se ejecutó AWS desde este entorno. El listado real (solo lectura) es un requisito del merge (§8).

El bucket del sitio es distinto del privado (`ork3d-resenas-privado-<cuenta>`, con `tokens/`, `claims/`, `resenas/`, `pendientes/`, `admin/locks/`). El deploy del sitio no lo toca.

| Objeto / prefijo | ¿En producción? | ¿En el repo? | ¿Lo sube el deploy? | ¿Excluido? | Riesgo con `--delete` | Riesgo con la rama |
|---|---|---|---|---|---|---|
| `resenas/publicas.json` | sí (lo escribe la app) | no | no | sí (`resenas/*`) | protegido por filtro | ninguno |
| `resenas/fotos/*.jpg` | sí (la app) | no | no | sí (`resenas/*`) | protegido por filtro | ninguno |
| `admin-web/entrar.html`, `admin-web/resenas.html` | sí (admin-ui) | sí | antes sí, con `max-age=300` | **sí (nuevo)** | ninguno | cabeceras preservadas |
| `assets/css/admin.css` | sí (admin-ui, 2026-10-22) | sí | antes sí, con `max-age=300` | **sí (nuevo)** | ninguno | preservado |
| `assets/js/admin-comun.js`, `admin-entrar.js`, `admin-panel.js` | sí (admin-ui) | sí | antes sí, con `max-age=300` | **sí (nuevo)** | ninguno | preservado |
| `opiniones.html`, `assets/css/opiniones.css`, `assets/js/resenas-publicas.js` | sí (lanzamiento) | sí | sí | no | no (están en el repo) | sí, con cabeceras del lanzamiento |
| Raíz: `index.html`, `torneos.html`, `regalos-…html`, `error.html`, `opinar.html`, `config.js`, `robots.txt`, `sitemap.xml` | sí | sí | sí | no | no (están en el repo) | `sitemap.xml` con `must-revalidate` |
| `assets/**` del repo, sin admin (30 archivos en total, incluidos los de /opiniones) | sí | sí | sí | no | no | sin cambios |
| **Legado**: 26 archivos borrados del repo en el historial de main | **desconocido** | no | no | no | **sí, si siguen en producción** | no se tocan |
| Código del repo (`api/`, `admin/`, `dev/`, `docs/`, `infra/`, `.github/`, `*.sh`, `*.md`) | no debería haber | sí | no | sí | protegido | ninguno |

Los 26 de la fila de legado son: `assets/img/feature-torneo.svg`, 16 variantes `.avif`/`.webp` de `assets/img/fotos/` (identidad, produccion, series), 7 `g-*.svg`, `hero-trophies.svg` y `printer.svg`. Ninguno existe en el HEAD actual ni está referenciado fuera de `docs/`, `infra/`, `dev/`, `api/` y `admin/` (verificado con `git grep`). Si siguen en el bucket, son objetos sin uso, pero un `--delete` los borraría sin revisión.

**Veredicto del inventario:** con el deploy.sh de `main`, un `--delete` puede borrar el legado y cualquier otro objeto del bucket fuera de las exclusiones. No es posible demostrar que no existe ninguno sin listar el bucket. **Con el deploy.sh de esta rama no hay ningún borrado posible** (§6).

## 5. Semántica verificada de `aws s3 sync --delete`

No se asumió. Se leyó el código fuente de awscli 1.46.2 (descargado de PyPI a un directorio temporal, sin ejecutarlo):

- `syncstrategy/delete.py`: el texto de ayuda de `--delete` dice literalmente *"Note that files excluded by filters are excluded from deletion."*
- `filters.py`, `Filter.call`: para cada regla, se aplica el patrón de origen y el de destino. Gana la última regla que coincide. Los objetos del destino pasan por el mismo filtro.

Conclusión: las exclusiones sí protegen de `--delete`. Pero esa protección depende de que cada prefijo que queremos conservar esté en una exclusión. Un prefijo nuevo que no esté en la lista queda expuesto. No es una garantía de allowlist.

El runner de GitHub trae AWS CLI v2, y su código no se leyó. Por eso la estrategia no depende de esta semántica: sin `--delete` no hay nada que filtrar.

## 6. Estrategia elegida

**Opción D: subida sin `--delete`, con un solo dueño por prefijo, y un dry run obligatorio antes del merge.**

Alternativas evaluadas:

- **A. Manifiesto allowlist** (como `admin-ui-archivos.tsv`): la mejor garantía de reproducibilidad, pero reescribe todo el deploy del sitio. Queda como siguiente paso, no como cambio de este merge.
- **B. Ampliar exclusiones:** frágil. Cada prefijo nuevo queda expuesto hasta que alguien lo agrega. Es la causa raíz del problema actual.
- **C. Separar deploy público y admin:** adoptado en parte, con un dueño por prefijo. El admin se publica con `admin-ui-publicar.sh`.
- **D. Reemplazar el `sync --delete` por subida explícita:** adoptado. Sin `--delete` ni `rm`, no hay borrados posibles.
- **E. Otra:** el dry run (`DEPLOY_DRY_RUN=1`) es el chequeo de seguridad previo al merge, y se incluye en esta opción.

Prioridades:

1. **No borrar contenido generado:** cumplido. Ningún comando borra.
2. **Despliegue reproducible:** el mismo commit produce el mismo plan de `--dryrun`. Lo verifican los tests.
3. **Rollback claro:** volver a desplegar el commit anterior sobrescribe las claves del sitio. Un objeto que el deploy nuevo añadió no se borra: quitarlo es manual y revisado.
4. **No duplicar scripts:** `deploy.sh` sigue siendo el único script del sitio.
5. **main como source of truth:** el sitio sale de `main`. El panel admin también, pero lo publica su propio script, con su manifiesto.

### Cambios en `deploy.sh`

- Se eliminó `--delete`.
- Se agregaron exclusiones: `admin-web/*`, `assets/css/admin.css`, `assets/js/admin-*` (en el sync) y `css/admin.css`, `js/admin-*` (en el `cp --recursive` de `assets/`).
- Se agregó un `cp` explícito de `sitemap.xml` con `public, max-age=300, must-revalidate`.
- Se agregó `DEPLOY_DRY_RUN=1`: cada llamada a S3 lleva `--dryrun`, y no hay invalidación.
- Las exclusiones existentes, el orden de las subidas y las cabeceras de los demás archivos no cambian. `validar.py` y `test_deploy_workflow.py` siguen pasando.

El `workflow` no cambia: la guarda `if:` y los paths ya están en la rama.

## 7. Pruebas

`infra/tests/test_deploy_sitio.py`, 23 casos, sin AWS:

- **Simulación** de la semántica de la CLI sobre un inventario de 194 objetos: los 165 archivos del repo, más lo generado, más el legado, más código hipotético.
- **Auditoría** de `deploy.sh`: pasa sobre el script real.
- **Sin borrados:** con el script real, el conjunto de objetos que se borrarían es vacío, en todas las clases.
- **Si se reintroduce `--delete`:** lo único que se borraría es exactamente el legado de 26 archivos. Las exclusiones protegen generados, admin, /opiniones, la raíz y el código.
- **Mutaciones** (cada una debe ser detectada con su motivo): quitar `resenas/*`, quitar `admin-web/*`, quitar `css/admin.css` del cp, reintroducir `--delete`, quitar `api/*`, dejar de subir `/opiniones`, quitar el cache del sitemap.
- **Dry run y modo real** ejecutan `deploy.sh` con un `aws` falso que registra cada llamada: el dry run solo lista (`--dryrun` en todo) y no invalida; el modo real no usa `--dryrun` y crea una única invalidación sobre `/*`; ambos planean las mismas operaciones.

**Mutación sobre el archivo real:** se quitó la exclusión de `resenas/*` de `deploy.sh`, y la suite falló con 8 casos (incluidos `resenas/publicas.json` y las fotos). Se reintrodujo `--delete`, y falló con 4. Al restaurar, el archivo quedó idéntico (`cmp`) y la suite volvió a 23/23.

## 8. Plan para llegar a main

**PR o push directo.** Conviene PR, aunque no se crea sin pedido: deja la auditoría y el diff a la vista, y la protección de `main` puede exigirla.

**Tipo de integración: fast-forward (`--ff-only`), no squash.** Razones:

- Los commits conservan su SHA. `590ed7d` sigue siendo ancestro de main: el tooling admin verifica `merge-base --is-ancestor 590ed7d HEAD`. Con squash, esa guarda fallaría en main.
- Los blobs de los estáticos admin no cambian en ningún tipo de integración.
- No hay merge commit que revisar: el historial queda lineal.

**Commits que llegan:** los 37 de la sección 1, más el commit de este documento y el de `deploy.sh`. El deploy.sh nuevo llega a `main` con la auditoría de la sección 6.

### Verificaciones previas al merge

1. `dev/suite.sh` en `SUITE_RC=0` (ya verificado para el commit de esta rama).
2. **Dry run en EC2, read-only, desde el commit a integrar:**
   ```bash
   cd /ruta/al/repo
   git fetch origin claude/ork3d-landing-page-b2fQr
   git checkout --detach origin/claude/ork3d-landing-page-b2fQr
   DEPLOY_DRY_RUN=1 BUCKET=ork3d.com DISTRIBUTION_ID=<id> ./deploy.sh
   ```
   Resultado esperado: solo líneas `(dryrun) upload:`, ninguna `(dryrun) delete:` y ninguna invalidación. Cualquier `(dryrun) delete:` detiene el merge.
   Este comando hace listados de S3 (lectura). No escribe.
3. Revisión de la protección de `main` en GitHub (no verificable desde el repo): revisor requerido, checks requeridos, sin force push, y revisar el entorno `production`.
4. Confirmar en el mismo dry run que `admin-web/*`, `assets/css/admin.css` y `assets/js/admin-*` **no** aparecen en el plan.

### Verificaciones posteriores al merge

1. El workflow corre una vez (es inevitable: el push a main lo dispara). Se revisa su log: solo `sync` y `cp`, sin `--delete`, y una invalidación `/*`.
2. `infra/admin-ui-verify.sh post` (sin `ALLOW`, solo lectura): el panel sigue con `no-store` y el CSS admin sigue en 200 con los bytes revisados.
3. `infra/prod-lectura-v2.sh`: `/opiniones`, `/opinar`, export, notificaciones, logs.
4. `verify.sh` del repo.

### ¿Deshabilitar el workflow?

No hace falta. Con el deploy.sh nuevo, la primera corrida sube archivos que ya están publicados con el mismo contenido. El dry run de §8.2 confirma eso antes del merge. Deshabilitarlo dejaría el sitio sin deploy automático y no agrega seguridad.

### Comando final de merge (NO ejecutar)

```bash
git switch main
git merge --ff-only origin/claude/ork3d-landing-page-b2fQr
git push origin main          # esto dispara deploy.yml
```

`git push origin main` es el paso que despliega. Solo se ejecuta después del dry run y de las verificaciones de la sección 8.

### Después del merge

- El tooling admin tiene la rama como constante (`RAMA` en `infra/admin-ui-comun.sh`, `RAMA_ESPERADA` en `infra/admin-frontend-deploy.sh`, `RAMA` en `infra/opiniones-publicar.sh`). Hay que cambiarla a `main` en un commit posterior. Ese commit no dispara deploy: `infra/` no está en los paths del workflow.
- Los scripts históricos (`infra/setup.sh`, `infra/resenas-setup.sh`, `infra/teardown.sh`, `infra/admin-frontend-deploy.sh`, `infra/opiniones-publicar.sh`) no se ejecutan en el flujo normal. `teardown.sh` borra el bucket: no se corre.

## 9. Riesgos residuales

- **El legado (26 objetos):** no se borra con el deploy nuevo, pero tampoco se limpia. Si producción los tiene y se quieren quitar, es una operación manual con revisión.
- **Credenciales de larga duración** en `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. Migrar a OIDC es una mejora separada.
- **`/*` en cada invalidación:** es el comportamiento actual, se mantiene para no cambiar el comportamiento de caché.
- **Un prefijo nuevo** que no esté ni en el repo ni en las exclusiones se sube sin problema, pero si se reintroduce `--delete` queda expuesto. Por eso el `--delete` está prohibido por test.
- **Versionado del bucket del sitio:** no se verificó si está habilitado. Si no lo está, un borrado manual es irreversible.
- **La semántica de la CLI v2** no fue leída: la protección del deploy nuevo no depende de ella.
