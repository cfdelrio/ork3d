# Reseñas de clientes · arquitectura, seguridad, costos y runbook

Estado: **infraestructura de AWS creada desde la máquina del operador; frontend sin desplegar.** Los hallazgos de esa
ejecución están en el runbook (`docs/resenas/RUNBOOK-DESPLIEGUE-MANUAL.md`, sección 17). Pendientes: `config.js` con la
Function URL real, deploy del frontend, smoke test de cliente y merge a `main`. El código está en la rama
`claude/ork3d-landing-page-b2fQr`.

## Qué hay en el repo

| Parte | Dónde | Verificación |
| --- | --- | --- |
| Página del cliente `/opinar/<token>` | `opinar.html`, `assets/js/opinar.js`, `assets/css/opinar.css` | E2E en Chromium (24 casos del total) |
| API (Lambda) | `api/resenas/app.py` | 76 tests con `test_app.py` y `test_contrato.py` |
| Moderación (CLI) | `admin/resenas.sh`, `admin/README.md` | 97 casos del CLI, más un recorrido manual de 25 comandos |
| Listado público `/opiniones` | `opiniones.html`, `assets/js/resenas-publicas.js` | Construido, **sin enlazar y con noindex** hasta aprobar el diseño |
| Infraestructura | `infra/resenas-setup.sh`, `infra/resenas/*.json`, `infra/tests` | 18 tests del script y las políticas, sin AWS. Dry-run: 19 llamadas simuladas, 0 reales. Ejecutada en AWS desde la máquina del operador (runbook, sección 17) |
| Validaciones estáticas | `dev/validar.py` | 65 comprobaciones (sintaxis, HTML, JSON, políticas, IAM, script de despliegue) |

Suite completa: `dev/suite.sh`. Sale con 1 si alguna capa falla. Resultado actual: **280 casos, 0 fallos, 0 omitidos**.

## Flujo

1. Después de entregar un pedido, el equipo crea un token con `admin/resenas.sh token crear` y manda el link por WhatsApp.
2. El cliente elige estrellas (obligatorio), aspectos, comentario, una foto opcional y cómo figurar. Marca el permiso de publicación y envía.
3. La reseña queda en **pending**. Nunca se publica sola: alguien la aprueba desde el CLI.
4. `aprobar` publica la reseña y regenera `resenas/publicas.json` en el bucket del sitio.
5. Después de enviar se muestra el link a la reseña de Google, si está configurado, con cualquier calificación. Las reseñas negativas no se filtran: entran a la misma cola.

## Arquitectura

```
cliente ──► ork3d.com/opinar/<token>      CloudFront → S3 público (opinar.html, sin terceros)
   │
   ├─ GET  /tokens/<token> ─┐
   ├─ POST /presign         ├─► Lambda (Python 3.12, Function URL)  ─► bucket PRIVADO
   └─ POST /resenas         ┘        api/resenas/app.py                tokens/ claims/ resenas/ pendientes/
                                                                              ▲
moderador ── admin/resenas.sh (AWS CLI, jq, openssl) ─────────────────────────┘
      │ aprobar / exportar
      └─► ork3d.com/resenas/publicas.json y resenas/fotos/<id>.jpg
          (bucket del sitio; deploy.sh excluye resenas/ del sync y del --delete)
```

| Prefijo en el bucket privado | Contenido | Escribe | Lee |
| --- | --- | --- | --- |
| `tokens/<sha256>.json` | producto, categoría, vencimiento, revocado, verificado | CLI | Lambda |
| `claims/<sha256>.json` | marca de uso: un solo envío por token | Lambda (`IfNoneMatch`) | Lambda |
| `resenas/<id>.json` | reseña completa, incluidos contacto, feedback privado y pedido interno | Lambda (pending), CLI (estado) | CLI |
| `pendientes/<sha256>/<uuid>.jpg` | foto subida por el navegador; vence a los 2 días | navegador (POST firmado) | Lambda, CLI |

Lo público vive solo en `resenas/publicas.json` y `resenas/fotos/` del bucket del sitio. El export
copia campos explícitos: contacto, feedback privado, pedido interno y hashes no pasan nunca.

**IDs**: todas las reseñas tienen 32 caracteres hex (`uuid4().hex`). Lambda y CLI usan el mismo formato;
`test_contrato.py` y el recorrido manual lo verifican.

**Sin base de datos**: el volumen es de decenas o cientos de reseñas por mes, y el CLI lee S3 directamente.

## Decisiones tomadas

- **Solo JPEG**. El navegador recodifica cada foto a JPEG, y el export público es `.jpg`. Aceptar PNG y WebP
  obligaba a convertir en la CLI y ampliaba la superficie de parseo. Se eliminaron.
- **Metadatos fuera**. La Lambda valida la estructura del JPEG segmento por segmento y rechaza EXIF (con GPS),
  XMP, IPTC y comentarios. Acepta solo JFIF y el perfil de color ICC. Ver "Seguridad", fila EXIF.
- **noindex en /opiniones** hasta que se apruebe el diseño y el flujo de moderación.
- **Google CTA** independiente de la calificación: no se filtra por nota.

## Seguridad: revisión punto por punto

Cada fila dice qué se verificó y con qué. ✔ = verificado con test, E2E o validación del repo.
◐ = parcial. ✖ = pendiente.

| Control | Evidencia | Estado |
| --- | --- | --- |
| Ningún token en analytics | Ninguna página HTML carga analytics (`dev/validar.py`). `/opinar` carga solo `config.js` y `opinar.js`. Ningún recurso de infra configura logs de acceso (grep sobre `infra/`). | ✔ / ◐ (ver pendientes: Cloudflare y logs de consola) |
| `/opinar/<token>` con noindex | `<meta name="robots" content="noindex, nofollow">`: validación y E2E "seguridad: /opinar lleva noindex…". robots.txt no bloquea `/opinar` a propósito: el crawler tiene que poder leer el meta. | ✔ |
| no-referrer | `<meta name="referrer" content="no-referrer">`: validación y E2E. | ✔ |
| Feedback privado nunca en el JSON público | `assertSinDatosPrivados` en `test_contrato.py`; export con campos explícitos en `exportar_publicas`. | ✔ |
| `order_ref` nunca público | El pedido interno (`PEDIDO-SECRETO-777` en el test) no aparece en el export; no está en la lista de campos del export. | ✔ |
| Uploads solo formatos permitidos | `PHOTO_EXT = {'jpg'}`. Test: presign con png, webp, gif, JPG y listas se rechaza con 400. Clave de foto validada con regex en la Lambda y en la CLI. | ✔ |
| Tamaño máximo | Política firmada 15 KB–2 MB (`content-length-range`) y chequeo del servidor sobre el objeto real. Test: 14.999 y 2.000.001 bytes se rechazan. | ✔ |
| Nombres seguros | Solo letras y separadores, 2–40 caracteres. El público muestra nombre e inicial, o el nombre guardado en modo anónimo. | ✔ |
| EXIF eliminado | Tres capas: (1) el navegador recodifica en canvas: E2E con un canario EXIF en el original, y el objeto guardado no contiene `Exif`. (2) La Lambda rechaza cualquier segmento de metadatos y estructura rota: 14 casos de rechazo y 5 de aceptación en `JpegSinMetadatos`. (3) La CLI publica sin copiar a ciegas: `magick -strip` si está, y el JPEG ya validado. | ✔ en Chromium. ✖ Safari/iOS no probado. |
| Sanitización de texto | Caracteres de control fuera; el HTML del cliente se muestra como texto (test y E2E con `<img onerror>`); la página pública y `/opinar` usan `textContent`. | ✔ |
| Honeypot | Campo `website` oculto: la respuesta es 202 y no se guarda nada. Test y E2E. | ✔ |
| Token de un solo uso | `claims/<hash>` con `IfNoneMatch='*'`. Test de carrera simulada, E2E de doble envío desde dos pestañas (la segunda recibe 409). Atomicidad validada en S3 real (runbook, 13.1). | ✔ |
| Token guardado solo como hash | Test: el token en claro no aparece en ninguna clave ni objeto. Vence a los 60 días y se puede revocar. | ✔ |
| Rate limiting | Concurrencia reservada de 5 en la Lambda (`infra/resenas-setup.sh`). **No hay límite por IP** ni regla de WAF. | ◐ |
| Bucket privado | Block Public Access total, versionado, cifrado SSE-S3, política que niega HTTP sin TLS. `validar.py` verifica la política. No se aplicó en AWS. | ◐ (solo dry-run) |
| IAM mínimo privilegio | La Lambda lee `tokens/` y escribe solo `claims/`, `resenas/`, `pendientes/`. No puede escribir `tokens/`. Sin `s3:*` ni `Resource: "*"`. `validar.py` lo verifica. | ✔ (en el JSON) |

**Lo que queda abierto de seguridad**
- El token viaja en la ruta de la página y en la del API. Está protegido por el uso único, el vencimiento y la revocación. Pero si `ork3d.com` pasa por el proxy de Cloudflare, Cloudflare ve esas URLs. No está verificado desde el sandbox.
- No hay límite por IP. El tope es la concurrencia de 5, que acota el ritmo, no el total. Un atacante con muchos tokens válidos puede generar costo (ver costos).
- La moderación manual es la única barrera contra fotos con personas o datos visibles. La Lambda y la CLI cubren metadatos, no contenido.

## Costos

Objetivo: cerca de USD 0 al mes. **Costo fijo: USD 0.** No hay servidores ni recursos encendidos todo el tiempo.

Supuestos del modelo, por reseña:
- 5 invocaciones de Lambda (GET del token, preflights y POST), 150 ms, 256 MB.
- Lambda: 0,19 GB-s por reseña.
- S3 privado: 2,6 GET y 2,3 PUT por reseña (30% con foto). Cada aprobación hace 1 GET, 1 PUT, 1 LIST y **un GET por cada reseña del bucket**, porque `exportar` relee todo.
- Foto: 250 KB en promedio. 30% de las reseñas con foto.
- Tarifas a precio pleno, sin asumir capa gratuita de S3: almacenamiento USD 0,023/GB-mes; GET USD 0,0004/1.000; PUT, POST y LIST USD 0,005/1.000; Lambda USD 0,20/millón de requests y USD 0,0000166667/GB-s.
- Capas gratuitas de Lambda (1 M requests, 400.000 GB-s), CloudFront (1 TB, 10 M requests, 2 M invocaciones de Functions) y CloudWatch Logs (5 GB compartidos).

| Servicio | Costo fijo | Variable | Capa gratuita | 10 reseñas/mes | 100/mes | 1.000/mes (año 1) |
| --- | --- | --- | --- | --- | --- | --- |
| Lambda (API) | 0 | Por invocación y GB-s | 1 M requests y 400.000 GB-s/mes, siempre | 0,00 | 0,00 | 0,00 (sin capa: 0,004) |
| S3 privado (tokens, claims, reseñas, pendientes) | 0 | Requests (GET, PUT, LIST) y almacenamiento | No asumida | < 0,01 | ≈ 0,01 | ≈ 0,22 |
| S3 público (`publicas.json`, fotos aprobadas) | 0 | 1 PUT por aprobación y almacenamiento de fotos | No asumida | < 0,01 | < 0,01 | ≈ 0,02 |
| CloudFront (sitio, `/opiniones`, fotos) | 0 | USD 0,085/GB sobre 1 TB; USD 0,01/10.000 requests | 1 TB y 10 M requests/mes, siempre | 0,00 | 0,00 | 0,00 (hasta 1 TB) |
| CloudFront Functions (regla `/opinar`) | 0 | USD 0,10/millón sobre 2 M | 2 M/mes, siempre | 0,00 | 0,00 | 0,00 |
| CloudWatch Logs (30 días) | 0 | Ingesta y almacenamiento | 5 GB/mes compartidos | 0,00 | 0,00 | 0,00 |
| Otros (IAM, SSE-S3, ACM, DNS) | 0 | — | — | 0,00 | 0,00 | 0,00 |
| **Total estimado** | **0** | | | **≈ 0,00** | **≈ 0,01** | **≈ 0,25 en el primer año** |

**El punto de atención es el export.** En el segundo año, con unas 11.000 reseñas acumuladas, cada aprobación
relee todas: 1.000 aprobaciones al mes son 11 millones de GET, unos **USD 4,40 al mes**, y el export tarda
minutos. Hasta unos cientos de reseñas el problema no existe. Antes de llegar a ~1.000, conviene exportar
por lotes o mantener un índice. Está en pendientes.

**Fuentes y advertencias.** Los precios salen de búsquedas web: la de Lambda trae a la vez el precio por
GB-s y un tramo que una fuente contradice; la de S3 tiene una fuente que cobra la lectura por 10.000 en vez
de por 1.000. Verificar en la página oficial antes de aprobar.
- Lambda: [AWS Lambda pricing](https://aws.amazon.com/lambda/pricing/); [guía de precios de Lambda (Eon)](https://www.eon.io/blog/aws-lambda-pricing)
- S3: [AWS S3 pricing](https://aws.amazon.com/s3/pricing/); [precios de S3 2026 (Upstash)](https://upstash.com/blog/aws-s3-pricing-explained-2026); [precios de S3 2026 (Filebase)](https://filebase.com/blog/aws-s3-pricing-in-2026-what-youll-actually-pay/)
- CloudFront: [guía de clases de precio (AWS Developer Guide)](https://docs.aws.amazon.com/ko_kr/AmazonCloudFront/latest/DeveloperGuide/PriceClass.html); [guía de precios (Wring)](https://wring.co/blog/aws-cloudfront-pricing-guide); [precios de CloudFront (Egresscost)](https://egresscost.com/aws/cloudfront-pricing)
- CloudWatch: [AWS CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/)

## Despliegue (infraestructura ejecutada; frontend pendiente)

Procedimiento operativo completo (pre-flight, dry-run, ejecución real, verificación, smoke test y rollback): `docs/resenas/RUNBOOK-DESPLIEGUE-MANUAL.md`.

1. `infra/resenas-setup.sh`: dry-run por defecto. Imprime los 7 recursos, su costo y cada comando, sin tocar AWS.
2. `DRY_RUN=0 infra/resenas-setup.sh`, desde tu máquina con credenciales de administrador. Ya se ejecutó: no volver a correrlo sin autorización. Es idempotente.
   Crea el bucket privado, el rol, el log group, la Lambda, la Function URL con sus dos permisos, la concurrencia (solo si la cuenta tiene margen) y la regla `/opinar/*`.
3. Pegar la URL que imprime en `config.js` → `RESENAS_API_URL`, y deployar con `./deploy.sh` o el push a `main`.
4. Asignar `infra/resenas/admin-permisos.json` al usuario que modera, reemplazando los placeholders.
5. Configurar `admin/.env`. Ver `admin/README.md`.
6. **Smoke test de cliente y moderación** (runbook, sección 14): crear un token de prueba, enviar la reseña, reenviar el
   mismo link (debe responder 409), aprobar, y confirmar el export. Pendiente de autorización y del deploy del frontend.
   La atomicidad de `IfNoneMatch` ya quedó validada en S3 real (runbook, sección 13.1). La invocación pública funciona
   con los dos permisos de la Function URL. Ambos usan principal `*` porque la URL es pública; la API sigue exigiendo un
   token válido para escribir.

## Pruebas

```
dev/suite.sh
```

Cinco capas, todas obligatorias:

| Capa | Comando | Resultado actual |
| --- | --- | --- |
| API y contrato CLI/API | `python3 -m unittest discover -s api/resenas -v` | 76 OK, 0 fallos, 0 omitidos |
| Infra: script y políticas (sin AWS) | `python3 -m unittest discover -s infra/tests -v` | 18 OK, 0 fallos, 0 omitidos |
| CLI de moderación | `bash admin/tests/run_tests.sh` | 97 PASS, 0 FAIL |
| Navegador (Chromium) | `node dev/e2e/opinar.e2e.js` | 24/24 |
| Validaciones estáticas | `python3 dev/validar.py` | 65 PASS, 0 FAIL |

`test_contrato.py` corre el CLI real contra el mismo directorio de archivos que usa la lógica real de la
Lambda por `app.handler`. No mockea el contrato. El almacenamiento es un adaptador de archivos, que imita
el comportamiento de S3 que usamos.

`dev/serve.py` es el servidor local del E2E: sirve el sitio con las reglas de CloudFront y usa `app.py` con
un S3 en memoria. `dev/` está excluido del deploy.

## Pendientes reales

Bloqueantes para producción:
1. **Revisión de Cloudflare**: confirmar si `ork3d.com` usa el proxy. Si sí, decidir si las URLs con token pueden pasar por Cloudflare.
2. **Smoke test de cliente y moderación** (paso 6; runbook, sección 14). Pendiente de autorización y del deploy del frontend.
3. **Safari/iOS**: confirmar que el canvas no agrega metadatos. Si los agregara, la foto de esos clientes se rechazaría.
4. **Escala del export**: antes de ~1.000 reseñas, exportar por lotes o mantener un índice.
5. **Decisión de producto**: texto del permiso de publicación y aviso de privacidad. Revisión legal pendiente.

Decisiones de diseño abiertas:
6. Fotos de Variedad para la home (pendiente desde antes de este trabajo, opciones 1 a 4).
7. Etapa 4: enlazar `/opiniones`, sumarla al sitemap y agregar la sección del home. Requiere aprobar diseño y flujo.
8. Límite por IP (WAF o reglas de CloudFront) si el tráfico lo pide.
9. Google: completar `GOOGLE_REVIEW_URL`.

No probado:
10. CLI y script de infraestructura en macOS (bash 3.2 y `date` de BSD).
11. Safari y Firefox en el navegador.
12. `shellcheck` (no disponible en el sandbox; se validó sintaxis con `bash -n`).
13. Validador HTML oficial (se usó un chequeo propio de etiquetas balanceadas).
