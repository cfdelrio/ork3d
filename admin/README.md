# Moderación de reseñas (admin/)

CLI en bash para crear los links de opinión, moderar las reseñas y regenerar el export público
(`resenas/publicas.json`) del sitio. Todo pasa por AWS CLI v2. No hay servidor propio.

Estado: implementación lista para revisión técnica. **No está desplegada**: el CLI corre hoy sobre
un shim local, y los recursos de AWS se crean con `infra/resenas-setup.sh` (ver más abajo).

## Requisitos

- bash, jq, openssl.
- AWS CLI v2 con las credenciales de la cuenta de ORK3D (perfil o SSO). Permisos mínimos en
  `infra/resenas/admin-permisos.json`.
- ImageMagick (`magick`), opcional. Si está, vuelve a limpiar la foto al publicarla. Las fotos ya
  llegan validadas: solo JPEG, sin EXIF, XMP, IPTC ni comentarios (ver `api/resenas/app.py`).

Probado con bash 5.2 y GNU coreutils. En macOS (bash 3.2, `date` de BSD) no se probó todavía.

## Configuración

Copiá `admin/.env.example` a `admin/.env` y completá los valores. El archivo no tiene credenciales:
la CLI usa tu perfil de AWS. Las variables ya exportadas en el entorno tienen prioridad.
`admin/.env` y `admin/out/` están en `admin/.gitignore`.

| Variable | Qué es |
| --- | --- |
| `RESENAS_BUCKET` | Bucket privado: tokens, claims, reseñas y fotos pendientes. |
| `SITE_BUCKET` | Bucket del sitio: ahí se escriben `resenas/publicas.json` y las fotos aprobadas. |
| `CF_DISTRIBUTION_ID` | Distribución de CloudFront para invalidar `/resenas/*`. Vacío = se omite, con aviso. |
| `SITE_URL` | Base de los links que se mandan al cliente. |
| `TOKEN_DAYS` | Validez de cada link (60 por defecto). |
| `AWS_CMD` | Comando de AWS CLI v2 (puede tener espacios). |
| `ADMIN_OUT_DIR` | Carpeta para fotos privadas (`ver`) y backups. Vacío = `admin/out`. |

## Flujo de moderación

1. **Crear el link** para el cliente:
   `admin/resenas.sh token crear --pedido 1234 --producto trofeo-acrilico --categoria trofeos --producto-label "Trofeo acrílico"`.
   Imprime el mensaje de WhatsApp con el link. El link se muestra una sola vez: el bucket guarda solo su
   hash. Si se pierde, se crea otro token y se revoca el anterior con `token revocar`.
2. **Revisar lo que llegó**: `pendientes` lista las reseñas esperando decisión. `ver ID` muestra la
   reseña completa, con el pedido interno. Si tiene foto, la baja a `ADMIN_OUT_DIR`.
3. **Decidir**:
   - `aprobar ID` publica la reseña (exige permiso de publicación) y regenera el export.
   - `rechazar ID` la descarta. Quita la foto pública y conserva el texto.
4. **Corregir lo publicado**:
   - `ocultar ID` la saca del sitio sin borrarla. `restaurar ID` la vuelve a publicar.
   - `verificar ID` marca la reseña y su token como compra verificada.
5. **Cada cambio que afecta lo público** regenera `publicas.json` e invalida `/resenas/*` en CloudFront.

Un pedido de quitar una opinión se resuelve con `ocultar`. Borrarla del todo es `borrar`.

## Comandos

| Comando | Qué hace |
| --- | --- |
| `token crear --pedido REF --producto SLUG --categoria SLUG [--producto-label TXT] [--dias N] [--mensaje TXT]` | Crea el token y muestra el mensaje de WhatsApp. `{link}` en `--mensaje` marca dónde va el link. |
| `token revocar TOKEN` | Invalida un link ya enviado. |
| `token listar` | Lista hash corto, producto, fechas, si está revocado y si ya fue usado. |
| `pendientes` | Lista las reseñas pendientes. |
| `ver ID` | Muestra una reseña completa y baja la foto privada a `ADMIN_OUT_DIR`. |
| `aprobar ID` | Publica y regenera el export. |
| `rechazar ID` | Rechaza, quita la foto pública y regenera el export. |
| `ocultar ID` / `restaurar ID` | Saca del sitio / vuelve a publicar, con export regenerado. |
| `verificar ID` | Marca la reseña y el token como compra verificada. |
| `borrar ID [--foto] [--yes]` | Sin `--foto` borra la reseña y todas sus fotos. Con `--foto` solo las fotos; el texto se conserva. Pide escribir `BORRAR` salvo que pases `--yes`. |
| `mantenimiento [--dry-run] [--yes]` | Borra las rechazadas de más de 90 días. Con `--dry-run` solo lista. |
| `exportar` | Regenera `resenas/publicas.json` e invalida CloudFront. |
| `backup DIR` | Copia el bucket privado completo a `DIR` (`aws s3 sync`). |
| `ayuda` | Muestra el uso completo. |

Todos los IDs de reseña son de 32 caracteres hex (el `uuid4().hex` que genera la Lambda). La CLI
rechaza cualquier otro formato con código 1.

Códigos de salida: 0 ok · 1 uso incorrecto o error local · 2 estado inválido o no encontrado · 3 error de AWS.

## Revisar, respaldar y mantener

- **Backup**: `backup DIR` copia el bucket privado completo. Incluye datos personales (nombres,
  contactos, feedback privado). Guardalo cifrado, fuera del repo o en `admin/out/`, que git ignora.
  No incluye credenciales.
- **Mantenimiento**: siempre `mantenimiento --dry-run` primero. Lista las rechazadas de más de 90 días.
  Después, `mantenimiento --yes`. Las ocultas no se tocan: se borran a mano con `borrar`.
- **Borrado**: es irreversible. `borrar` elimina todas las versiones del objeto, también las que guarda el versionado. Usá `--yes` solo si estás seguro.

## Probar en local

```
bash admin/tests/run_tests.sh                                  # CLI contra el shim local (sin AWS)
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s api/resenas -v   # API + contrato con la CLI
dev/suite.sh                                                   # todo, incluido el navegador
```

`aws_shim.py` guarda cada objeto como archivo en un directorio temporal. `test_contrato.py` corre la CLI
real contra la lógica real de la Lambda sobre esos mismos archivos.

## Puesta en marcha (más adelante, no ejecutada)

Runbook operativo completo, con pre-flight, verificaciones y rollback: `docs/resenas/RUNBOOK-DESPLIEGUE-MANUAL.md`.

1. Revisar el dry-run: `infra/resenas-setup.sh`. Muestra los recursos y su costo, sin tocar AWS.
2. Crear los recursos con `DRY_RUN=0 infra/resenas-setup.sh`, desde una cuenta con permisos de administrador.
3. Asignar `infra/resenas/admin-permisos.json` al usuario o rol que modera, reemplazando los placeholders.
4. Completar `admin/.env` con los valores de la cuenta.
5. Probar el flujo completo con un token de prueba antes de mandar links reales. Ver `docs/resenas/README.md`.

## Límites conocidos

- **El export relee todas las reseñas** del bucket en cada aprobación (`exportar_publicas`, una descarga
  por reseña). Con cientos de reseñas tarda minutos y el costo de S3 crece. Antes de llegar a ~1.000
  reseñas conviene exportar por lotes o mantener un índice. Ver `docs/resenas/README.md`, sección "Costos".
- Los listados leen una sola respuesta de S3 (hasta 1.000 objetos). Con más reseñas hay que paginar.
- La CLI no crea reseñas: eso lo hace la Lambda.
- En modo anónimo el export muestra el `display_name` guardado por la Lambda (`Cliente de ORK3D`).
- macOS y bash 3.2 no están probados.
