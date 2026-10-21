# Reseñas de clientes · arquitectura y runbook

Sistema de reseñas de ORK3D (opción C). Lo que está en el repo:

| Parte | Dónde | Estado |
|---|---|---|
| Página del cliente `/opinar/<token>` | `opinar.html`, `assets/js/opinar.js`, `assets/css/opinar.css` | Lista, probada en el navegador (19 casos E2E) |
| API (Lambda) | `api/resenas/app.py` | Lista, 59 tests unitarios |
| Moderación (CLI) | `admin/resenas.sh`, `admin/README.md` | En el repo |
| Infraestructura AWS | `infra/resenas-setup.sh`, `infra/resenas/*.json` | Solo dry-run desde el sandbox |
| Listado público `/opiniones` y sección del home | — | Etapa 4: se construye cuando se apruebe el diseño |

## Flujo

1. Después de entregar un pedido, el equipo crea un token y manda el enlace `https://ork3d.com/opinar/<token>` por WhatsApp.
2. El cliente elige estrellas (obligatorio), aspectos, comentario, foto opcional y cómo figurar: nombre con inicial, solo nombre o anónimo. Con el permiso marcado, envía.
3. La reseña queda en **pending**. Nunca se publica sola: alguien la aprueba desde el CLI.
4. Aprobar publica y reescribe `resenas/publicas.json` y las fotos aprobadas en el sitio.
5. Después de enviar se muestra el enlace a la reseña de Google (si `GOOGLE_REVIEW_URL` está configurado). Se muestra con cualquier calificación: no se filtra por nota.

Las reseñas negativas no se ocultan ni se filtran: entran a la misma cola de moderación.

## Arquitectura

```
cliente ──► ork3d.com/opinar/<token>        CloudFront → S3 público (opinar.html, sin terceros)
   │
   ├─ GET  /tokens/<token> ─┐
   ├─ POST /presign         ├─► Lambda (Python 3.12, Function URL)  ─► bucket PRIVADO ork3d-resenas-privado
   └─ POST /resenas         ┘        api/resenas/app.py                tokens/  claims/  resenas/  pendientes/
                                                                              ▲
moderador ── admin/resenas.sh (AWS CLI + jq + openssl) ───────────────────────┘
      │ aprobar / exportar
      └─► ork3d.com/resenas/publicas.json y resenas/fotos/<id>.jpg
          (bucket del sitio; prefijo excluido del deploy: el --delete nunca lo toca)
```

| Prefijo en el bucket privado | Contenido | Escribe | Lee |
|---|---|---|---|
| `tokens/<sha256>.json` | producto, categoría, vencimiento, revocado | CLI | Lambda |
| `claims/<sha256>.json` | marca de uso (un solo envío por token) | Lambda, con `IfNoneMatch` | Lambda |
| `resenas/<id>.json` | reseña completa: contacto y feedback privado incluidos | Lambda (pending), CLI (estado) | CLI |
| `pendientes/<sha256>/<uuid>.jpg` | foto subida por el navegador; vence a los 2 días | navegador (POST firmado) | Lambda, CLI |

Lo público vive solo en `resenas/publicas.json` y `resenas/fotos/` del bucket del sitio, y solo tiene reseñas aprobadas con permiso. El contacto y el feedback privado nunca salen de S3 privado.

No hay base de datos: el volumen es de decenas de reseñas por mes y el CLI lee S3 directamente.

## Seguridad y privacidad

- **Token**: 24 bytes aleatorios (192 bits), 32 caracteres base64url. El bucket guarda solo su SHA-256. Vence a los 60 días y se puede revocar. No lleva número de pedido en la URL.
- **Un solo uso**: el envío crea `claims/<hash>.json` con escritura condicional (`IfNoneMatch='*'`). Dos envíos simultáneos: gana uno, el otro recibe 409. Si falla la escritura de la reseña, el claim se borra para que el cliente pueda reintentar.
- **Fotos**: el navegador las reduce a 1600 px y las recodifica como JPEG (así se quitan los metadatos EXIF). La subida va firmada con condiciones de tamaño (15 KB a 2 MB) y de tipo. El servidor vuelve a validar tamaño y firma binaria. Ninguna foto sale del bucket privado sin aprobación.
- **Anti-spam**: campo trampa (un bot recibe la misma confirmación y no se guarda nada), límite de concurrencia de 5 en la Lambda, cuerpo máximo de 8 KB, validación estricta de cada campo.
- **Logs**: no se registran tokens completos, IPs ni textos de reseña. Retención de 30 días.
- **Nombre real**: no se guarda. Solo se guarda el nombre mostrado (`Ana G.`, `Ana` o `Cliente de ORK3D`).
- **Página `/opinar`**: `noindex`, `referrer: no-referrer` (el token no sale en el Referer) y sin fuentes ni scripts de terceros. No carga `main.js`, porque ese script envía la ruta a analytics.
- **Retención**: las rechazadas se borran a los 90 días (`mantenimiento`). Las ocultas se quedan hasta que las borres con `borrar`. Fotos pendientes, 2 días (lifecycle del bucket). Versiones anteriores, 30 días.

## Despliegue (una vez)

Requisitos: AWS CLI v2 con credenciales de administrador de la cuenta, `jq`, `zip`. Se corre desde tu máquina, no desde el sandbox.

1. `infra/resenas-setup.sh` (dry-run por defecto): imprime cada comando sin tocar AWS. Revisalo.
2. `DRY_RUN=0 infra/resenas-setup.sh`: crea el bucket privado, el rol, la Lambda, la Function URL, la regla `/opinar/*` en CloudFront y la verificación. Es idempotente.
3. Pegá la URL que imprime en `config.js` → `RESENAS_API_URL`, y deployá (push a `main` o `./deploy.sh`).
4. Asigná `infra/resenas/admin-permisos.json` al usuario o rol que modera. Reemplazá los placeholders (`__RESENAS_BUCKET__`, `__SITE_BUCKET__`, `__ACCOUNT_ID__`, `__DISTRIBUTION_ID__`).
5. Configurá `admin/.env` (ver `admin/README.md`).
6. **Smoke test** (valida lo que el sandbox no puede probar):
   - Creá un token de prueba con `admin/resenas.sh token crear …` y abrí el enlace.
   - Enviá la reseña. Enviá el mismo enlace otra vez: debe responder 409 ("ya recibimos tu opinión"). Eso confirma que el runtime de Lambda soporta `IfNoneMatch`.
   - Aprobala con `admin/resenas.sh aprobar <id>` y confirmá que aparece en `https://ork3d.com/resenas/publicas.json`.
   - Si la verificación devuelve `Forbidden`: agregá el permiso de invocación pública que pide Lambda, `lambda:InvokeFunction` con principal `*`, y volvé a verificar. Ese permiso también deja invocar la función con credenciales AWS de cualquier cuenta. La API sigue exigiendo un token válido para escribir.

## Moderación (runbook del CLI)

```
admin/resenas.sh token crear --pedido REF --producto SLUG --categoria SLUG [--producto-label "Texto"] [--dias N]
admin/resenas.sh token revocar TOKEN
admin/resenas.sh token listar
admin/resenas.sh pendientes                 # cola de moderación
admin/resenas.sh ver ID                     # reseña completa, incluido contacto
admin/resenas.sh aprobar ID                 # publica y exporta
admin/resenas.sh rechazar ID | ocultar ID | restaurar ID | verificar ID
admin/resenas.sh borrar ID [--foto] [--yes]
admin/resenas.sh mantenimiento [--dry-run] [--yes]
admin/resenas.sh exportar                   # regenera publicas.json y la CDN
admin/resenas.sh backup DIR
```

Códigos de salida: 0 ok · 1 uso incorrecto o error local · 2 estado inválido o no encontrado · 3 error de AWS.

- **Ocultar** saca una reseña aprobada del sitio sin borrarla. Es la respuesta a un pedido de quitarla.
- **Revocar** invalida un enlace que ya se mandó.
- **Mantenimiento** borra las rechazadas de más de 90 días. Las ocultas no se tocan: se borran a mano con `borrar`. Corré `--dry-run` antes de `--yes`.
- **Backup** copia el bucket privado completo a un directorio local. Hacelo antes de cambios grandes. Incluye datos de contacto y feedback privado: guardalo cifrado y fuera del repo.

## Costos

Con decenas de reseñas por mes, Lambda, S3 y CloudFront quedan en centavos de dólar por mes. El límite de concurrencia de 5 evita sorpresas por abuso.

## Pruebas

```
python3 -m unittest discover -s api/resenas -v          # API: 59 casos, fake de S3
admin/tests/run_tests.sh                                # CLI
SHOTS_DIR=<carpeta> node dev/e2e/opinar.e2e.js          # página + API reales, en Chromium
```

`dev/serve.py` es el servidor local de la E2E: sirve el sitio con las mismas reglas que CloudFront y usa `app.py` con un S3 en memoria. No se despliega: `dev/` está excluido del deploy.

## Límites de la V1

- Sin panel web de moderación: solo CLI.
- Sin correo automático: el enlace lo manda el equipo por WhatsApp.
- Safari y Firefox no se probaron en el sandbox (solo Chromium).
- La verificación de `IfNoneMatch` depende de la versión de boto3 del runtime. El smoke test del paso 6 la valida.
- Etapa 4 pendiente: listado `/opiniones` y sección del home, que requieren aprobación de diseño.
