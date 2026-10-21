# V2 · Panel admin de reseñas (contrato de implementación)

**Estado: implementación local. No desplegada. AWS y producción no modificados.**

Este documento es el contrato entre las piezas de la V2: núcleo Python, API administrativa,
UI, notificaciones y scripts de infraestructura. Si una pieza necesita cambiar un contrato
de aquí, se cambia este documento primero.

## 1. Alcance

- Crear invitaciones de reseña desde un panel web (`/admin/resenas`).
- Moderar reseñas desde el panel: aprobar, rechazar, ocultar y restaurar.
- Recibir un correo al moderador cuando un cliente deja una reseña pending.
- Acceso del moderador por enlace de correo (magic link) y sesión opaca de 8 h.
- La CLI `admin/resenas.sh` sigue siendo la herramienta de administración y recuperación.
  No cambia en esta etapa.
- `/opiniones` sigue sin lanzar: noindex, fuera del sitemap y sin enlaces desde la home.

No incluye: passwords propios, WhatsApp API, Google Reviews, reescritura de la CLI,
cambios de DNS, cambios de IAM reales ni despliegue.

## 2. Decisiones aprobadas y desviaciones

| Tema | Decisión | Nota |
|---|---|---|
| Autenticación | Magic link por correo + sesión opaca de 8 h en cookie | Sin passwords propios |
| Arquitectura | Lambda admin separada `ork3d-resenas-admin`. API bajo `/admin/api/*`, UI en `/admin/resenas` | La Lambda pública no gana permisos sobre el sitio |
| CLI | Opción A: sin cambios incompatibles, núcleo Python reusable y tests de paridad | La CLI se alinea después de validar el panel |
| SES | Diseño con AWS SES, implementado con una abstracción y `FakeSES` | Sin SES real ni DNS en esta etapa |
| Buzón | Requisito de 2FA documentado para producción, sin código asociado | |
| Foto | Bytes servidos por la Lambda admin, con sesión y `no-store` | Nunca URL pública ni bucket privado expuesto |
| Function URL admin | Diseño objetivo AWS_IAM + OAC de CloudFront | No se depende de eso localmente. Verificar antes del deploy |
| Transiciones | Más estrictas que la CLI (ver §6) | Diferencia cubierta con tests |

### Desviaciones y decisiones que surgieron en la implementación

1. **`approved → rejected` sí está permitido** en la web: el pedido de moderación dice
   "si venía de approved, retirar foto pública y regenerar export". La CLI lo soporta y
   tiene test (`rechazar approved: sale del export`).
2. **`rejected → approved` no está permitido** en la web. La CLI lo acepta, pero no hay test
   que lo cubra. La regla del pedido es "solo si existe una razón funcional ya testeada".
   Se devuelve 409 y se documenta la diferencia.
3. **`hidden → rejected` no está permitido** en la web (la CLI lo acepta, sin test).
4. **`pending → hidden` no está permitido** (la CLI lo acepta, sin test).
5. **Listado y export de fotos:** la copia pública se hace leyendo y escribiendo bytes
   (`GetObject` + `PutObject`), no con `CopyObject`, para no ampliar permisos de la Lambda.
6. **Estado de notificación** no se guarda en la reseña. Se guarda en
   `notificaciones/<id>.json`, así que el esquema de reseña V1 no cambia.
7. **Sesiones vencidas:** la lectura (incluido `GET /admin/api/sesion`) no borra nada. Las sesiones
   vencidas las limpia la regla de ciclo de vida de `admin/` (2 días). Así los GET no escriben.
8. **Export de la CLI:** la CLI produce el mismo conjunto de claves y el mismo orden. Los tests de
   `test_paridad_cli.py` lo comprueban escribiendo y leyendo con las dos herramientas sobre el mismo almacén.

## 3. Rutas de la UI (estáticas, sin datos)

| Ruta pública | Archivo en el sitio (CloudFront la reescribe) |
|---|---|
| `/admin/resenas` | `admin-web/resenas.html` |
| `/admin/resenas/<id>` | `admin-web/resenas.html` (la página lee el id de la URL) |
| `/admin/entrar` | `admin-web/entrar.html` |

Todas las páginas admin llevan `<meta name="robots" content="noindex, nofollow">` y
`<meta name="referrer" content="no-referrer">`, no tienen datos estáticos y no van al sitemap.
No deben ser enlazadas desde la home ni desde la navegación pública.

## 4. Autenticación

### 4.1 Login por correo (magic link)

1. `POST /admin/api/sesion/solicitar` (sin sesión). Siempre responde `202 {"ok": true}`.
   - Rate limit global: 3 solicitudes cada 15 minutos (`admin/limites/acceso.json`).
     Si se supera, no se envía nada y la respuesta sigue siendo 202.
   - Genera un código de 32 bytes aleatorios (`secrets.token_urlsafe(32)`).
   - Guarda solo `sha256(codigo)` en `admin/logins/<sha256>.json` con `expira = creado + 10 min`.
   - Envía el correo de acceso a `RESENAS_ADMIN_EMAIL` (destinatario fijo, configurado en
     el servidor). El navegador no puede indicar otro destinatario.
   - Enlace: `<RESENAS_ADMIN_BASE_URL>/admin/entrar#c=<codigo>`. El código va en el fragmento.
2. `GET /admin/entrar` solo muestra la página "Confirmar acceso". No consume nada.
3. El clic en "Confirmar acceso" hace `POST /admin/api/sesion/confirmar` con `{"codigo": "..."}`.
   Después de leer el fragmento, la página lo borra con `history.replaceState`.
4. El código es de un solo uso: se consume con un claim `IfNoneMatch: *` sobre
   `admin/logins-usados/<sha256>.json`. Si ya existe, el código está usado.
5. Éxito: se crea una sesión opaca de 32 bytes, se guarda `sha256(sesion)` en
   `admin/sesiones/<sha256>.json` con `expira = creada + 8 h`, y se responde `200` con
   `Set-Cookie`.

Un escáner de correo que hace GET al enlace no inicia sesión ni consume el código.

### 4.2 Cookie de sesión

`ork3d_admin=<sesion>; HttpOnly; Secure; SameSite=Lax; Path=/admin; Max-Age=28800`

- No hay renovación: la sesión vence a las 8 h desde su creación.
- `POST /admin/api/sesion/cerrar` borra el objeto de sesión y envía la cookie con `Max-Age=0`.

### 4.3 Requisito de producción (no implementado)

El buzón de administración debe tener 2FA. Es un requisito operativo, no hay código asociado.

## 5. CSRF y reglas de cabeceras

Para toda mutación administrativa (todos los `POST`):

- Cookie de sesión válida (salvo `solicitar` y `confirmar`).
- `Content-Type: application/json` (exacto, con o sin `; charset=utf-8`). Si no, `415`.
- `Origin` exactamente igual a `ALLOWED_ORIGIN`. Si no, `403`.
- Header `X-Admin-Request: 1`. Si falta, `403`.
- Un `POST` sin sesión responde `401`. Con sesión pero sin cabeceras, responde `403`.

Los `GET` nunca producen efectos secundarios. Se testea con snapshot del almacén.

## 6. Transiciones de moderación (web V2)

| Acción | Desde | Hacia | Efectos |
|---|---|---|---|
| `aprobar` | pending | approved | exige `permission_to_publish`; copia foto pública; publica; regenera export |
| `rechazar` | pending | rejected | sin cambios en el export |
| `rechazar` | approved | rejected | retira foto pública; regenera export |
| `ocultar` | approved | hidden | retira foto pública; regenera export |
| `restaurar` | hidden | approved | exige permiso; repone foto pública; conserva `published_at`; regenera export |

Cualquier otra combinación responde `409 estado_incompatible` sin efectos.

Orden de escritura para aprobar y restaurar: copiar foto, luego escribir la reseña con
`If-Match`, luego regenerar el export. Si la escritura falla por conflicto, se relee la
reseña. Si la foto copiada ya no está referenciada por la reseña, se borra para no dejar
una foto pública huérfana.

Orden para rechazar y ocultar: escribir el estado (así sale del export), luego retirar la
foto pública y regenerar el export. Si algo falla después del estado, la respuesta es `202`
con `export: "pendiente"`, y `POST /admin/api/exportar` repara el estado.

## 7. Endpoints de la API administrativa

Todas las respuestas llevan `Content-Type: application/json; charset=utf-8`,
`Cache-Control: no-store`, `X-Content-Type-Options: nosniff` y `Referrer-Policy: no-referrer`.
No hay cabeceras CORS: la API es mismo origen.

Errores: `{"ok": false, "error": "<codigo>", "mensaje": "<texto>", ...}`.

| Código HTTP | `error` | Cuándo |
|---|---|---|
| 400 | `validacion` | campos inválidos (incluye `campos: {campo: texto}`) |
| 400 | `json` | cuerpo malformado |
| 401 | `no_autenticado` | sin sesión o sesión vencida |
| 401 | `codigo_invalido` | código de login inválido, vencido o usado (mismo mensaje en todos) |
| 403 | `csrf` | faltan cabeceras CSRF |
| 403 | `origen` | `Origin` distinto del permitido |
| 404 | `no_encontrado` | reseña inexistente o sin foto |
| 409 | `estado_incompatible` | acción no permitida en el estado actual (incluye `estado_actual`) |
| 409 | `sin_permiso` | la reseña no tiene `permission_to_publish` |
| 409 | `conflicto_concurrencia` | la reseña cambió mientras se trabajaba |
| 409 | `ya_enviado` | `reenviar-aviso` cuando el aviso ya salió |
| 413 | `demasiado_grande` | cuerpo mayor a 8 KB |
| 415 | `content_type` | no es `application/json` |
| 409 | `foto_invalida` | la foto privada no se puede publicar (no existe, no es JPEG o trae metadatos) |
| 503 | `foto_no_publicada` | la copia de la foto falló antes de escribir el estado (nada cambió) |
| 503 | `export_ocupado` | `POST /admin/api/exportar` mientras otro proceso regenera el export (reintentar) |
| 503 | `export_fallido` | `POST /admin/api/exportar` falló al regenerar o al invalidar CloudFront (reintentar) |
| 503 | `aviso_fallido` | SES falló en `reenviar-aviso` (la reseña no cambió) |
| 500 | `interno` | error sanitizado, sin detalle |

### 7.1 Autenticación

| Método | Ruta | Auth | Respuesta de éxito |
|---|---|---|---|
| POST | `/admin/api/sesion/solicitar` | origen + cabeceras | `202 {"ok": true}` (siempre) |
| POST | `/admin/api/sesion/confirmar` | origen + cabeceras | `200 {"ok": true, "expira": iso}` + `Set-Cookie` |
| GET | `/admin/api/sesion` | sesión | `200 {"ok": true, "expira": iso}` |
| POST | `/admin/api/sesion/cerrar` | sesión + cabeceras | `200 {"ok": true}` + cookie expirada |

`solicitar` acepta cuerpo vacío o `{}`. Cualquier otra clave responde `400 validacion`.
El destinatario nunca viene del cliente.

### 7.2 Invitaciones

`POST /admin/api/invitaciones` (sesión + cabeceras)

Cuerpo:
```json
{"order_ref": "ORK-2026-0182", "product_slug": "trofeo-personalizado",
 "product_label": "Trofeo personalizado", "category": "torneos", "dias": 15}
```

Reglas (las mismas que `cmd_token_crear`):
- `order_ref`: obligatorio, 1 a 80 caracteres.
- `product_slug` y `category`: `^[a-z0-9-]+$`, 1 a 40 caracteres.
- `product_label`: obligatorio, 1 a 80 caracteres. La CLI usa el slug si no hay label; la
  web lo exige (diferencia menor, documentada).
- `dias`: entero de 1 a 365. Si falta o es `null`, vale `RESENAS_TOKEN_DAYS` (60 por defecto, igual que la CLI).

Generación del token: `secrets.token_bytes(24)` en base64url sin padding. Equivale a
`openssl rand -base64 24 | tr '+/' '-_' | tr -d '='`. Son 32 caracteres. Se escribe
`tokens/<sha256>.json` con create-only y hasta 3 reintentos ante colisión.

Registro: los mismos campos que la CLI: `v`, `token_hash`, `created_at`, `expires_at`,
`order_ref`, `product_slug`, `product_label`, `category`, `verified: false`, `revoked: false`,
`revoked_at: null`. El mensaje opcional **no se guarda**. Vive solo en el frontend.

Respuesta `201`:
```json
{"ok": true, "enlace": "https://.../opinar/<token>", "vence": "<iso>",
 "producto_label": "...", "categoria": "torneos"}
```
El token no se loguea. El frontend no lo persiste en `localStorage` ni en `sessionStorage`.

### 7.3 Lectura

`GET /admin/api/resenas?estado=pending|approved|rejected|hidden|all` (por defecto `all`)

- Orden: pending primero. Dentro de cada grupo, `created_at` descendente. Con `all`, el orden
  de grupos es pending, approved, hidden, rejected.
- Fila (`resenas[]`):
  `id, rating, display_name, product_label, category, created_at, status, verified, tiene_foto, aviso`.
- `aviso`: `"enviado"` si existe `notificaciones/<id>.json`; `"pendiente"` si la reseña está
  pending y no hay registro; `"no_aplica"` en otros casos.
- Nunca incluye `token_hash`, `photo_key`, `private_feedback`, `contact` ni datos AWS.

`GET /admin/api/resenas/{id}`

- Incluye: `id, status, rating, comment, aspects, display_name, display_mode, product_label,
  product_slug, category, verified, created_at, moderated_at, published_at,
  permission_to_publish, private_feedback, contact_requested, contact, pedido, tiene_foto, aviso`.
- `pedido` (la referencia interna de pedido) se resuelve en el servidor leyendo
  `tokens/<review.token_hash>.json`. El hash nunca se devuelve.
- Nunca incluye `token_hash`, `photo_key`, `photo_public_key` ni claves S3.

`GET /admin/api/resenas/{id}/foto`

- Requiere sesión. Valida que `photo_key` cumpla `^pendientes/[a-f0-9]{64}/[a-f0-9]{32}\.jpg$`
  y que el hash del medio sea el `token_hash` de la reseña.
- Responde los bytes con `Content-Type: image/jpeg`, `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`. La clave nunca viaja al frontend.
- Sin foto o sin objeto: `404`.

### 7.4 Moderación

Todas son `POST` con sesión y cabeceras (§5). El cuerpo puede estar vacío.

| Ruta | Éxito |
|---|---|
| `/admin/api/resenas/{id}/aprobar` | `200 {"ok": true, "estado": "approved", "export": "actualizado"}` |
| `/admin/api/resenas/{id}/rechazar` | `200 {"ok": true, "estado": "rejected", "export": "..."}` |
| `/admin/api/resenas/{id}/ocultar` | `200 {"ok": true, "estado": "hidden", "export": "actualizado"}` |
| `/admin/api/resenas/{id}/restaurar` | `200 {"ok": true, "estado": "approved", "export": "actualizado"}` |
| `/admin/api/resenas/{id}/reenviar-aviso` | `200 {"ok": true, "aviso": "enviado"}` |
| `/admin/api/exportar` | `200 {"ok": true, "export": "actualizado", "publicadas": n}` |

`"export": "pendiente"` con `202 {"ok": true, "estado": ..., "export": "pendiente", "reintentar": true}`
significa que el estado ya cambió pero el export no se pudo regenerar. La UI ofrece
"Regenerar export".

`reenviar-aviso` solo aplica a reseñas `pending` (en otro estado responde `409 estado_incompatible`)
y solo envía si no hay `notificaciones/<id>.json`. Si ya existe responde `409 ya_enviado`. Si SES
falla responde `503 aviso_fallido` y no cambia nada.

`"export": "actualizado"` también aparece cuando la transición no cambia lo público (por ejemplo,
rechazar una reseña pending): el export ya refleja el estado.

## 8. Modelo de datos

Sin cambios en `tokens/`, `claims/`, `resenas/`, `pendientes/` ni `resenas/fotos/` ni en el
export. Claves nuevas, todas en el bucket privado salvo `resenas/` del sitio:

| Clave | Contenido | Vence |
|---|---|---|
| `admin/sesiones/<sha256>.json` | `{v, creada, expira}` | por tiempo (8 h). Las vencidas las borra la regla de 2 días, no la lectura |
| `admin/logins/<sha256>.json` | `{v, creado, expira}` | por tiempo (10 min) y lifecycle 2 días |
| `admin/logins-usados/<sha256>.json` | `{v, usado}` (claim one-use) | lifecycle 2 días |
| `admin/limites/acceso.json` | `{v, ventana_inicio, cantidad}` | lifecycle 2 días |
| `admin/locks/export.json` | `{v, duenio, expira}` | por tiempo (60 s) |
| `notificaciones/<review_id>.json` | `{v, enviado}` | no vence |

## 9. Export público

La allowlist **no cambia**. El export contiene exactamente estas claves por ítem:

`id, rating, comment, aspects, display_name, category, product_label, verified, photo_url, published_at`

El test `test_privacidad` compara el conjunto exacto de claves. Cualquier campo nuevo hace
fallar el test. Se incluyen solo reseñas con `status == "approved"` y
`permission_to_publish == true`. Se ordenan por `published_at` descendente.

Se regenera leyendo todas las reseñas, como hoy. Se invalida `/resenas/*`.

## 10. Concurrencia y lock

- **Optimistic concurrency:** cada mutación lee la reseña con su ETag y escribe con
  `IfMatch=<etag>`. Si la escritura falla por precondición, responde `409 conflicto_concurrencia`
  y no sobrescribe. El fake de S3 de los tests simula dos mutaciones: gana una, la otra
  recibe conflicto.
- **Verificación de S3 real:** el comportamiento de `If-Match` en la cuenta no está verificado.
  Antes del deploy hay que hacer un smoke real de dos escrituras concurrentes con `If-Match`.
  Hasta entonces, la implementación depende de la abstracción y de los tests locales.
- **Lock del export:** `admin/locks/export.json` con create-only. Si existe y no venció,
  el lock está ocupado (se reintenta 3 veces con espera). Si venció, se toma con `IfMatch`
  sobre su ETag. Se libera solo si el dueño coincide. Si la regeneración falla, el lock se
  libera igual en `finally`.
- **Reintento seguro:** regenerar el export es idempotente, porque se calcula desde el estado
  completo.

## 11. Correo (SES)

Abstracción `ServicioCorreo.enviar(Correo)`. Implementaciones: `FakeSES` (tests y dev) y
`SESCorreo` (boto3, sin ejecutar en esta etapa).

**Correo de reseña**
- Asunto: `Nueva reseña ORK3D · ★★★★★` (estrellas según el rating).
- Destinatario: `RESENAS_ADMIN_EMAIL`. Remitente: `RESENAS_FROM_EMAIL`.
- Contenido, allowlist: rating, display_name, product_label, category, comentario truncado a
  280 caracteres, "tiene foto: sí/no", y el enlace `<base>/admin/resenas/<review_id>`.
- No incluye: `order_ref`, `private_feedback`, `contact`, `contact_requested`, token,
  `token_hash`, claves S3 ni datos AWS.
- HTML con escape y versión `text/plain`.

**Correo de acceso**
- Asunto: `Acceso al panel de reseñas ORK3D`.
- Contenido: explicación breve, enlace con fragmento, vencimiento de 10 minutos y aviso de
  ignorar si no fue solicitado. Sin datos del sistema.

**Hook en la Lambda pública**
- Solo si `RESENAS_NOTIFY_ENABLED == "true"` (por defecto `false`).
- Se ejecuta después de guardar la reseña pending. Si SES falla: no hay rollback, no se libera
  el claim, la respuesta sigue siendo `200`, se registra `aviso_fallido` sanitizado y la
  reseña queda sin registro en `notificaciones/`, lo que la bandeja muestra como "pendiente".

## 12. Logs

Formato: una línea JSON por evento: `{"ev": "...", "ts": "<iso>", ...campos}`.

Campos permitidos (lista exacta en `nucleo/registro.py`, y el test la fija): `review_id`, `op`,
`status`, `estado`, `resultado`, `error` (nombre de la clase de excepción), `ruta` (plantilla, por
ejemplo `/tokens/{token}`), `intento`, `foto` (booleano) y `count` (entero). Cualquier otro valor,
o un valor que no sea un identificador simple, se reemplaza por `[omitido]`.

Prohibidos, sin excepción: token, token_hash y cualquier prefijo suyo, comentario,
private_feedback, contact, correos, URLs firmadas, cookies, códigos de login y secretos.

Se elimina el campo `th` de la Lambda pública. Los tests capturan los logs de los flujos y
verifican que no aparecen esos valores.

## 13. Desarrollo local

`dev/serve.py` monta:
- La API pública en `/api/resenas/*` (igual que antes).
- La API admin en `/admin/api/*`, con el mismo S3 en memoria y `FakeSES`.
- Las páginas admin con las mismas reglas de reescritura que CloudFront.
- `GET /dev/correos`: lista los correos simulados (`to, from, subject, text, html`). Solo dev.

Variables de dev: `RESENAS_ADMIN_EMAIL=admin@ejemplo.invalid`,
`RESENAS_FROM_EMAIL=noreply@ejemplo.invalid`, `RESENAS_NOTIFY_ENABLED=true`,
`RESENAS_ADMIN_BASE_URL=http://127.0.0.1:<puerto>`, `ALLOWED_ORIGIN=http://127.0.0.1:<puerto>`.

## 14. Infraestructura (solo DRY_RUN)

Archivos previstos, sin ejecutar AWS:
- `infra/admin-setup.sh`: `DRY_RUN=1` por defecto. Imprime los comandos y no llama a AWS.
- `infra/resenas/admin-lambda-permisos.json`: política del rol de la Lambda admin.
- `infra/resenas/admin-cloudfront-behavior.json`: plan de la behavior `/admin/api/*`.
- `infra/resenas/lambda-permisos.json`: agrega SES y `notificaciones/*` a la Lambda pública.
- `infra/resenas/bucket-lifecycle.json`: regla de 2 días para `admin/`.
- `infra/redirect-www.js`: reglas `/admin/resenas` y `/admin/entrar` (antes de la regla
  extensionless). La función se asocia solo al default behavior.
- `infra/resenas-setup.sh`: empaqueta `nucleo/` además de `app.py`.
- `.github/workflows/deploy.yml`: agrega `admin-web/**` a las rutas que disparan deploy.

Los tests de infraestructura verifican que `DRY_RUN` es el valor por defecto, que no hay
llamadas reales a AWS y que no aparecen comandos de borrado.

## 15. Seguridad revisada (resumen)

- Acceso al panel: solo con sesión válida. No se confía en ocultar la URL.
- Login: código aleatorio de 32 bytes, solo hash guardado, un solo uso, 10 min, destinatario fijo.
- Cookie: `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/admin`, 8 h.
- CSRF: cabeceras obligatorias, `Origin` exacto, `SameSite=Lax`.
- XSS: el frontend no usa `innerHTML` con datos de clientes. Usa `textContent` y DOM seguro.
  Hay CSP por `<meta>` sin scripts inline.
- Fotos: solo por la Lambda admin, con sesión y `no-store`. El bucket privado no se expone.
- Logs: sin datos sensibles (ver §12).
- Correo: sin datos privados. El HTML se escapa.
- Scanners: los GET no producen efectos. Las acciones son POST con confirmación en la UI.
- Riesgo de DoS o spam: rate limit global en el login. El costo de SES es marginal.
- Pendiente antes del deploy: 2FA en el buzón, verificación de `If-Match` en S3 real, verificación
  de OAC con Function URL AWS_IAM, y la identidad SES de producción (dirección o dominio, y salida
  del sandbox). Ninguno está verificado en esta etapa.
