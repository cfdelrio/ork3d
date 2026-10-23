# Reseñas V2 · estado de producción y comandos vigentes

**Rama canónica: `main`.** Todo lo que se despliega sale de `main`. La rama de feature de V2 ya está integrada en `main` y no se usa en ningún comando.

Último commit de `main` validado en producción: `d616cc2` (`chore(deploy): protege despliegue de main`). Commit del panel admin revisado: `590ed7d`.

## Estado validado

| Componente | Estado |
| --- | --- |
| `/opiniones` | lanzada: indexable, canonical `https://ork3d.com/opiniones`, enlazada desde home, en el sitemap |
| `/opinar/<token>` | sano (token inexistente: página pública, sin efectos) |
| Panel admin `/admin/resenas`, `/admin/entrar` | desplegado: CSP, `noindex, nofollow`, sin `style=` ni scripts inline |
| CSS y JS del panel (`admin.css`, `admin-comun.js`, `admin-panel.js`, `admin-entrar.js`) | v2, con `Cache-Control: no-store` |
| API admin sin sesión | `401 no_autenticado` |
| Export público `resenas/publicas.json` | 0 reseñas visibles; las reseñas de prueba quedan fuera |
| Notificaciones | `RESENAS_NOTIFY_ENABLED=true` |
| Logs de la Lambda | sin cookies ni `Set-Cookie` en las últimas 24 h |

## Verificación vigente (solo lectura)

```bash
cd /home/ec2-user/ork3d
git fetch origin && git switch main && git pull --ff-only origin main

PROD_LECTURA_OPINIONES=lanzada PROD_LECTURA_NOTIFY_ESPERADO=true infra/prod-lectura-v2.sh
SMOKE_NOTIFY_ID=<id de la reseña de prueba de notificaciones> infra/admin-ui-verify.sh post
SMOKE_NOTIFY_ID=<id de la reseña de prueba de notificaciones> infra/opiniones-verify.sh post
```

- `prod-lectura-v2.sh` exige **las dos variables**. Sin una de ellas sale con código 2 antes de cualquier petición. No tiene valores por defecto: el estado esperado se declara siempre.
- `admin-ui-verify.sh` y `opiniones-verify.sh` pasan su estado explícitamente.
- Ninguno de estos comandos hace login, envía correos ni escribe en AWS.

## Reglas de despliegue

- `main` es la fuente de verdad. El workflow `deploy.yml` corre en cada push a `main` que toque: `index.html`, `torneos.html`, `regalos-empresariales-fin-de-ano.html`, `error.html`, `opinar.html`, `opiniones.html`, `config.js`, `sitemap.xml`, `robots.txt`, `assets/**`, `admin-web/**`, `deploy.sh` o `.github/workflows/deploy.yml`. Los cambios solo en `infra/` o `docs/` no despliegan.
- Antes de un push a `main`, revisar los paths: `git diff --name-only origin/main..HEAD`.
- `deploy.sh` no borra nada en S3 (no usa `--delete`). El panel admin y `resenas/` quedan fuera de su alcance.
- Dry run antes de un deploy del sitio (solo lectura, sin invalidación):
  `DEPLOY_DRY_RUN=1 BUCKET=ork3d.com DISTRIBUTION_ID=<id> ./deploy.sh`
  La salida debe tener solo `(dryrun) upload:`. Cualquier `(dryrun) delete:` detiene el deploy.

## Comandos que no se ejecutan sin una razón nueva

| Comando | Por qué |
| --- | --- |
| `infra/setup.sh`, `infra/resenas-setup.sh`, `infra/admin-setup.sh` | Crean infraestructura. Ya está creada. |
| `infra/teardown.sh` | Borra el bucket. No se ejecuta. |
| `infra/opiniones-publicar.sh publicar` | `/opiniones` ya está lanzada. Requiere `ALLOW_OPINIONES_PUBLISH=1` **y** `OPINIONES_REPUBLICAR=1`. |
| `infra/opiniones-verify.sh pre` | Es histórico (antes del lanzamiento). Exige `OPINIONES_PRE_HISTORICO=1`. Usar `post`. |
| `infra/notify-activacion.sh activar` | Las notificaciones ya están activas. |
| `infra/notify-public-iam.sh aplicar`, `infra/notify-public-code-deploy.sh deploy` | Ya aplicados. |
| `infra/admin-deploy.sh`, `infra/admin-frontend-deploy.sh` | Históricos. Los cambios del panel se publican con `infra/admin-ui-publicar.sh`. |

## Panel admin: cambios futuros

- El panel se publica con `infra/admin-ui-publicar.sh` (manifiesto de 5 archivos: `infra/admin-ui-archivos.tsv`). Antes de subir, el script exige que el contenido de esos archivos sea el del commit `590ed7d`, comparando blobs de git.
- Un cambio a esos archivos exige, a propósito: actualizar el manifiesto y la revisión de referencia en `infra/admin-ui-comun.sh`, y revisar de nuevo.
- Modo de publicación: `ALLOW_ADMIN_UI_PUBLISH=1 infra/admin-ui-publicar.sh publicar`.
- Rollback del panel: `ALLOW_ADMIN_UI_PUBLISH=1 infra/admin-ui-publicar.sh revertir`. Restaura la copia más reciente con bytes y cabeceras exactos, y quita solo los objetos que no existían antes.

## Rollback de /opiniones

`ALLOW_OPINIONES_PUBLISH=1 infra/opiniones-publicar.sh revertir` restaura los objetos que existían antes del lanzamiento. Si encuentra un archivo que no existía, se detiene con un error y lo revisa una persona: no lo borra automáticamente.

## Documentación histórica

Estos documentos conservan el registro de cada fase. Sus comandos declaran el estado de su fecha:

- `docs/resenas/V2-ADMIN.md`: contrato de la V2 y fases (incluye Fase A y la activación de notificaciones).
- `docs/resenas/ADMIN-UI-DEPLOY.md`: deploy del panel admin, con su manifiesto, su guarda y el hueco conocido de `admin.css`.
- `docs/resenas/OPINIONES-PUBLICA.md`: auditoría y procedimiento del lanzamiento de `/opiniones`.
- `docs/resenas/MERGE-MAIN.md`: auditoría del merge a `main`, con las referencias a la rama de feature conservadas.
- `docs/resenas/RUNBOOK-DESPLIEGUE-MANUAL.md`: runbook manual de la V1.
