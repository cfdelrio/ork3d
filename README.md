# ORK3D · Landing

Landing estática de **ork3d.com** + **www.ork3d.com** (impresión 3D de trofeos, medallas, regalos corporativos y piezas personalizadas en Buenos Aires). Sin backend, sin DB, sin frameworks.

```
HTML + CSS + JS vanilla → S3 (privado) → CloudFront (OAC) → ork3d.com
                                                          ↑ www → 301 → apex
```

> DNS gestionado en **Cloudflare** (free). El DNS de Donweb no soporta `ANAME`/`ALIAS` en apex, así que migramos los nameservers del dominio a Cloudflare para usar CNAME flattening.

---

## 1. Estructura

```
.
├── index.html              # Landing
├── error.html              # Página 404
├── config.js               # WHATSAPP_NUMBER (variable)
├── assets/
│   ├── css/styles.css
│   ├── js/main.js
│   └── img/                # SVG placeholders (reemplazables por renders reales)
├── deploy.sh               # Sync a S3 + invalidación CloudFront
├── verify.sh               # Smoke test del sitio público
└── infra/
    ├── setup.sh                   # One-time: crea bucket, OAC, cert, distributions
    ├── teardown.sh                # Borra todo (destructivo)
    ├── redirect-www.js            # CloudFront Function www → apex
    └── bucket-policy.template.json
```

---

## 2. Editar el sitio

### Cambiar el número de WhatsApp
Editá `config.js`:
```js
WHATSAPP_NUMBER: "5491141843591"
```
Formato internacional sin `+`, sin espacios. Argentina móvil = `549` + área + número.

### Reemplazar imágenes
Los archivos en `assets/img/` son SVG placeholders. Cuando tengas renders reales (PNG/JPG/WEBP) sustituilos manteniendo el mismo nombre, o actualizá los `src` en `index.html`. Recomendado: WEBP < 200 KB.

### Cambiar copy
Todo el texto está en `index.html`. Buscá la sección por `id` (`#hero`, `#productos`, `#torneos`, etc.).

### Probar local
```bash
python3 -m http.server 8000
# abrir http://localhost:8000
```

---

## 3. Deploy (recurrente)

Después del setup inicial alcanza con:
```bash
./deploy.sh
```
Sincroniza a S3 con cache headers correctos e invalida CloudFront.

Verificación:
```bash
./verify.sh ork3d.com
```

---

## 4. Setup AWS inicial (one-time)

> **Antes:** confirmá que estás en la cuenta correcta con `aws sts get-caller-identity`.

Requisitos:
- AWS CLI v2 (`aws --version`)
- `jq` (`apt install jq` / `brew install jq`)
- Credenciales con permisos en S3, CloudFront y ACM

```bash
cd infra
./setup.sh
```

Crea, en orden:

| # | Recurso | Notas |
|---|---|---|
| 1 | Bucket S3 `ork3d.com` | Privado, AES256, versionado on |
| 2 | Certificado ACM en `us-east-1` | `ork3d.com` + `www.ork3d.com`, validación DNS |
| 3 | Origin Access Control (OAC) | CloudFront firma su acceso a S3 |
| 4 | CloudFront Function `ork3d-redirect-www` | 301 www → apex |
| 5 | Distribution **apex** | sirve el sitio |
| 6 | Distribution **www** | redirige a apex via Function |
| 7 | Bucket policy | sólo permite las dos distributions vía OAC |

Al terminar deja `infra/.env` con las variables que `deploy.sh` usa.

### Pausa para validar ACM
Cuando ACM emita el cert, el script imprime los **CNAMEs de validación** y pausa. Tenés que crearlos en Donweb (paso 5).

---

## 5. DNS en Donweb

Panel: **Mis dominios → ork3d.com → Editor de zona DNS**.

### 5.1. Validación ACM (obligatorios)
ACM te va a dar 2 valores tipo:
```
_xxxx.ork3d.com.       CNAME   _yyyy.acm-validations.aws.
_xxxx.www.ork3d.com.   CNAME   _yyyy.acm-validations.aws.
```
- En Donweb el **nombre** se carga sin el `.ork3d.com.` final (sólo la parte antes).
- No los borres después: ACM los usa para renovar el cert automáticamente.

### 5.2. Registros del sitio
Cuando termine `setup.sh`, imprime los dos `*.cloudfront.net` reales. Cargá:

| Tipo | Nombre | Valor |
|---|---|---|
| `ANAME` / `ALIAS` (si Donweb lo permite) | `@` (apex) | `dxxxxxx.cloudfront.net` |
| `CNAME` | `www` | `dyyyyyy.cloudfront.net` |

⚠️ **Sobre el apex (`@`):** DNS estándar no permite `CNAME` en el apex. Donweb suele tener `ANAME`/"Registro alias" que lo resuelve. Si tu plan no lo soporta:

1. **Opción A** (recomendada si Donweb no tiene ANAME): usar el **servicio de redirección de dominio** de Donweb: redirigí `ork3d.com` → `https://www.ork3d.com`, y dejá `www` como canónico. Editá `infra/redirect-www.js` para invertir la lógica (apex → www).
2. **Opción B**: migrar el dominio a Route53 (USD 0.50/mes) y usar `A ALIAS`.

Preguntale a soporte de Donweb si tenés `ANAME`/`ALIAS`. Si sí, adelante con el plan original.

### 5.3. Verificar propagación
```bash
dig +short ork3d.com
dig +short www.ork3d.com
# Deben resolver a IPs de CloudFront
```

---

## 6. Costos estimados

Tráfico bajo (< 1 GB/mes salida, < 100k requests):

| Recurso | Costo mensual |
|---|---|
| S3 storage (~5 MB) | < USD 0.01 |
| S3 requests | ~ USD 0.01 |
| CloudFront salida | **gratis** (1 TB free tier) |
| CloudFront requests | **gratis** (10M free tier) |
| CloudFront Functions | gratis hasta 2M invocaciones/mes |
| ACM certificate | **gratis** |
| Route53 *(sólo si lo usás)* | USD 0.50/mes/zone + USD 0.40/M consultas |

**Total realista: USD 0–1/mes.** Sin tráfico → casi 0.

---

## 7. Comandos AWS CLI útiles

```bash
# Status de la distribution
aws cloudfront get-distribution --id <DISTRIBUTION_ID> --query 'Distribution.Status'

# Invalidar manualmente
aws cloudfront create-invalidation --distribution-id <DISTRIBUTION_ID> --paths "/*"

# Ver contenido del bucket
aws s3 ls s3://ork3d.com/ --recursive --human-readable --summarize

# Ver bucket policy
aws s3api get-bucket-policy --bucket ork3d.com --query Policy --output text | jq
```

---

## 8. Decisiones tomadas

- **www → apex (301).** `ork3d.com` es canónico.
- **Cache policy:** managed `CachingOptimized` (`658327ea-f89d-4fab-a63d-7e88639e58f6`). HTML con `max-age=60` (deploys se ven rápido); assets con `max-age=31536000, immutable`.
- **No se usa Lambda@Edge.** Redirect www→apex con CloudFront Functions (gratis bajo el límite).
- **No se usa Route53** salvo migración explícita. DNS queda en Donweb.
- **No hay backend.** CTAs apuntan a WhatsApp (`wa.me`).
- **404** servido desde `/error.html` para 403/404 del bucket.

---

## 9. Próximos pasos sugeridos

- Reemplazar SVG placeholders por renders reales o fotos de productos.
- Agregar Plausible / GA4 si querés tracking.
- Sumar link real de Instagram en `index.html` (footer).
- Crear `sitemap.xml` y `robots.txt` cuando el sitio esté indexable.
