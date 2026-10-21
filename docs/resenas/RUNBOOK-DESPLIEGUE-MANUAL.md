# Runbook de despliegue manual · reseñas ORK3D (V1, prueba controlada)

**Estado: listo para ejecutar desde tu máquina. No ejecutado.** Este documento no crea ni modifica nada por sí solo.
Cada comando se corre a mano, en orden, y cada paso tiene una salida esperada. Si una salida no coincide, **parás**.

Qué crea el despliegue de infraestructura: un bucket S3 privado, un rol IAM mínimo, una Lambda con Function URL,
su concurrencia reservada, un log group con retención de 30 días y una nueva versión de la CloudFront Function
`ork3d-redirect-www` con la regla `/opinar/*`. **No toca** la distribución de CloudFront, el bucket del sitio, DNS,
certificados, `config.js`, `main` ni la home.

Reglas:

- Nada se borra automáticamente. Los rollbacks son comandos manuales (sección 15).
- `DRY_RUN=1` antes que `DRY_RUN=0`, siempre.
- Ningún account ID, distribution ID, URL real ni credencial se commitea. Van en `infra/.env` (ignorado por git)
  o en tu shell.
- Los comandos de AWS usan `--profile "$AWS_PROFILE"`. No hay access keys en archivos ni en comandos.
- Si algo no coincide con lo esperado: parar, no reintentar a ciegas.

---

## 1. Revisión del script `infra/resenas-setup.sh`

| Punto | Resultado |
| --- | --- |
| Compatible con macOS y Linux | Sí. No usa features de bash 4+ ni flags exclusivos de GNU. Probado con bash 5.2 y con un `aws` de prueba local. **No probado contra AWS real.** |
| AWS CLI v2 | El modo real lo verifica: `aws --version` debe empezar con `aws-cli/2`. |
| `jq` y `zip` | Obligatorios. Se verifican al arrancar. |
| Dry-run por defecto | Sí: `DRY_RUN=1`. El dry-run no llama a AWS (verificado: 0 llamadas reales). |
| Credenciales | El script no lee credenciales. Usa el profile de AWS CLI (`AWS_PROFILE`). |
| Confirmación antes de cambiar nada | Sí, en modo real: hay que escribir `CREAR`. |
| Chequeos previos de solo lectura | Sí, en modo real: bucket del sitio accesible, distribución con alias `ork3d.com`, función de redirección en LIVE. Si falla uno, corta sin cambios. |
| Borrados | Ninguno. El script no contiene `delete-*`, `--recursive` ni `s3 rm` (verificado en `dev/validar.py`). |

Cambios aplicados en esta revisión:

1. El bucket privado **ya no tiene un nombre fijo**. Si no definís `RESENAS_BUCKET`, se deriva del account ID:
   `ork3d-resenas-privado-<ACCOUNT_ID>`. Se valida el formato de nombre S3.
2. Chequeos previos de solo lectura antes de cualquier cambio (punto anterior).
3. Confirmación interactiva con `CREAR`.
4. La retención del log group (30 días) se fija **después** de la invocación de verificación. Antes podía fallar: Lambda
   crea el grupo de logs recién en la primera invocación.
5. La descripción de la Lambda quedó en ASCII (algunos validadores de AWS rechazan caracteres no ASCII).
6. Corregido un bug de la verificación que mostraba `HTTP 000000` cuando no había respuesta.

Limitaciones conocidas:

- `put-function-concurrency` puede fallar en cuentas nuevas con poco margen de concurrencia. El script lo avisa y
  sigue. Hay que fijarla a mano (comando en la sección 9).
- Si la verificación de la API devuelve `Forbidden`, falta un permiso de invocación pública. Ver sección 11.

---

## 2. Requisitos locales (A)

| Herramienta | Mínimo | Cómo verificar | Notas |
| --- | --- | --- | --- |
| macOS o Linux | — | `uname -s` | |
| bash | 3.2 (probado en 5.2) | `bash --version` | El script de infra corre con bash 3.2 (el de macOS). Si algún otro script falla, `brew install bash`. |
| AWS CLI | **v2** | `aws --version` → `aws-cli/2.x` | Recomendado: versión reciente, para `s3api put-object --if-none-match` (sección 13). |
| jq | 1.6 | `jq --version` | macOS: `brew install jq`. Debian/Ubuntu: `sudo apt install jq`. |
| zip | cualquiera | `zip -v` | Viene en macOS y en la mayoría de las distros. |
| curl | cualquiera | `curl --version` | |
| dig | bind 9 | `dig -v` | Solo para la sección 12. macOS lo trae. |
| git | 2.x | `git --version` | |
| Python | 3.9+ | `python3 --version` | Tests de API y validaciones. |
| Node.js y Playwright | Node 18+ | `node --version` | Solo para el E2E de `dev/suite.sh`. |

Instalación de Playwright (una vez):

```
npm install -g playwright
npx playwright install chromium
export NODE_PATH="$(npm root -g)"
```

Instalación de AWS CLI v2: el paquete oficial (`https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html`)
o `brew install awscli` en macOS. **No** uses `pip install awscli` (es v1).

---

## 3. Variables (B)

Fuente única: `infra/.env` en la raíz del repo. Está ignorado por git (`.gitignore`). El script lo carga con `source`,
así que solo tiene que contener asignaciones simples.

```
# infra/.env — LOCAL, ignorado por git. No commitear. Se carga con `source`: valores entre comillas.
# Un placeholder sin reemplazar (<...>) hace que el script corte en el chequeo previo, sin cambios en AWS.
BUCKET="ork3d.com"
DISTRIBUTION_ID="<DISTRIBUTION_ID>"                        # sección 4
AWS_REGION="us-east-1"
RESENAS_BUCKET="ork3d-resenas-privado-<TU_ACCOUNT_ID>"    # sección 3.1
ALLOWED_ORIGIN="https://ork3d.com"
GOOGLE_REVIEW_URL=""
RESENAS_LAMBDA_CONCURRENCY="5"
```

En la shell, antes de cada sesión (sin credenciales):

```
export AWS_PROFILE=ork3d
```

| Variable | Obligatoria | Valor | De dónde sale |
| --- | --- | --- | --- |
| `BUCKET` | sí | `ork3d.com` | Ya existe: lo creó `infra/setup.sh`. |
| `DISTRIBUTION_ID` | sí | `<DISTRIBUTION_ID>` | Sección 4. |
| `AWS_PROFILE` | sí (en la shell) | `ork3d` | Tu configuración local de AWS CLI (sección 5). |
| `AWS_REGION` | sí | `us-east-1` | La misma región del sitio original. Todo el despliegue va en esta región. |
| `RESENAS_BUCKET` | recomendada | `ork3d-resenas-privado-<TU_ACCOUNT_ID>` | Ver 3.1. Si no la definís, el script la deriva con el mismo patrón. |
| `ALLOWED_ORIGIN` | sí | `https://ork3d.com` | Default del script. |
| `GOOGLE_REVIEW_URL` | no | vacía | Cuando tengas el enlace de la reseña en Google. |
| `RESENAS_LAMBDA_CONCURRENCY` | no | `5` | Default. Límite de concurrencia: freno a abuso y costo. |
| `RESENAS_FUNCTION` / `RESENAS_ROLE` | no | `ork3d-resenas` / `ork3d-resenas-lambda` | Defaults. No cambiarlos salvo conflicto. |

### 3.1 Nombre del bucket

Los nombres de bucket S3 son **globales**: si otra cuenta ya usa el nombre, falla. Por eso el patrón es
`ork3d-resenas-privado-<ACCOUNT_ID>`, con el ID de tu cuenta. Para obtenerlo (lectura, no crea nada):

```
aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text
```

Reemplazá `<TU_ACCOUNT_ID>` por ese valor **en tu `infra/.env` local**. No lo escribas en el repo, ni en
`admin/.env.example`, ni en ningún documento que se commitee.

Reglas del nombre: minúsculas, números, puntos y guiones, de 3 a 63 caracteres. El script lo valida.

---

## 4. CloudFront Distribution ID del sitio (C)

No inventes el valor. Se obtiene de AWS. Tres caminos, del más simple al más completo.

**Opción 1: si ya existe `infra/.env` de la configuración original** (`infra/setup.sh` lo escribe):

```
grep '^DISTRIBUTION_ID=' infra/.env
```

Anotá el valor y verificalo con la opción 2.

**Opción 2: consulta por CLI (solo lectura):**

```
aws cloudfront list-distributions --profile "$AWS_PROFILE" \
  --query 'DistributionList.Items[].{Id:Id,Domain:DomainName,Aliases:Aliases.Items}' \
  --output json \
  | jq -r '.[] | select(.Aliases != null) | select(.Aliases | index("ork3d.com")) | "\(.Id)  \(.Domain)"'
```

Salida esperada: **una sola línea**, con un ID que empieza con `E` y un dominio `dXXXXXXXX.cloudfront.net`.

- Ninguna línea → la distribución no tiene `ork3d.com` como alias. Parar.
- Dos o más líneas → ambigüedad. Parar y revisar en la consola.

**Opción 3: consola web (solo lectura):** CloudFront → Distributions → la distribución cuyo *Alternate domain names*
incluye `ork3d.com` → columna **ID**.

**Confirmación (lectura):** el origen debe ser el bucket del sitio, y la función de redirección debe estar asociada:

```
DIST_ID="<DISTRIBUTION_ID>"
aws cloudfront get-distribution-config --id "$DIST_ID" --profile "$AWS_PROFILE" \
  --query 'DistributionConfig.Origins.Items[].DomainName' --output text
```

Esperado: un dominio que contiene `ork3d.com` (el bucket del sitio). Si apunta a otro bucket: parar.

Guardá el ID en `infra/.env`. No lo commitees.

---

## 5. Profile de AWS (sin access keys en el repo)

El runbook usa un profile nombrado, `ork3d`. Configuración recomendada: SSO o un rol asumido, sin claves de larga duración.

```
aws configure sso --profile ork3d       # interactivo: nombre de la sesión, URL SSO, cuenta y rol
aws sso login --profile ork3d            # renueva la sesión cuando expira
```

No pegues access keys en `infra/.env`, en comandos ni en archivos del repo. Si tu configuración usa claves
estáticas, usá el profile en `~/.aws/credentials` y **no** las copies a ningún lado.

Validación de identidad (muestra el account ID enmascarado, sin credenciales):

```
aws sts get-caller-identity --profile "$AWS_PROFILE" --output json \
  | jq -r '"Cuenta: " + (.Account | .[0:4] + "********") + "   Principal: " + (.Arn | sub("[0-9]{12}"; "************"))'
```

Esperado: `Cuenta: ` con los primeros 4 dígitos y asteriscos, y el ARN de tu rol o usuario. Si falla con
`could not be found`, el profile no existe. Si falla con `ExpiredToken`, corré `aws sso login --profile ork3d`.

---

## 6. Pre-flight (copiar y pegar, desde tu máquina)

Antes de empezar, definí las variables de la sección 3 en `infra/.env` y en la shell:

```
REPO="/ruta/a/ork3d"            # ajustá la ruta de tu clon
cd "$REPO"
set -a; . infra/.env; set +a    # carga BUCKET, DISTRIBUTION_ID, RESENAS_BUCKET, etc. (local, ignorado por git)
export AWS_PROFILE=ork3d
```

### Bloque 1: herramientas

```
echo "aws:  $(aws --version 2>&1 | head -n 1)"        # esperado: aws-cli/2.x
echo "jq:   $(jq --version)"                            # esperado: jq-1.6 o superior
echo "zip:  $(zip -v 2>/dev/null | sed -n 2p)"          # esperado: Zip 3.x
echo "bash: $BASH_VERSION"                              # esperado: 3.2 o superior
echo "curl: $(curl --version | head -n 1)"
case "$(aws --version 2>&1)" in
  aws-cli/2*) echo "OK: AWS CLI v2" ;;
  *) echo "ERROR: se requiere AWS CLI v2. Parar." ;;
esac
```

### Bloque 2: identidad y región

```
aws sts get-caller-identity --profile "$AWS_PROFILE" --output json \
  | jq -r '"Cuenta: " + (.Account | .[0:4] + "********")'
aws configure get region --profile "$AWS_PROFILE"
```

Esperado: la cuenta que **vos esperás** (la de tu organización ORK3D) y la región `us-east-1`. Si la cuenta no es la
esperada: parar. Esa comprobación es la barrera principal contra desplegar en la cuenta equivocada.

### Bloque 3: existencia y permisos básicos (solo lectura)

```
# Nombre de bucket libre (esperado: "Not Found" o 404 la primera vez; 403 = nombre tomado por otra cuenta: cambiá RESENAS_BUCKET)
aws s3api head-bucket --bucket "$RESENAS_BUCKET" --profile "$AWS_PROFILE" 2>&1 | tail -n 1

# Rol y Lambda: primera vez, "no existe"
aws iam get-role --role-name ork3d-resenas-lambda --profile "$AWS_PROFILE" >/dev/null 2>&1 \
  && echo "rol: YA EXISTE (el script lo reutiliza)" || echo "rol: no existe (se creará)"
aws lambda get-function --function-name ork3d-resenas --profile "$AWS_PROFILE" >/dev/null 2>&1 \
  && echo "lambda: YA EXISTE (se actualiza)" || echo "lambda: no existe (se creará)"

# Log group: primera vez, lista vacía
aws logs describe-log-groups --log-group-name-prefix /aws/lambda/ork3d-resenas \
  --profile "$AWS_PROFILE" --query 'logGroups[].logGroupName' --output text

# Función de redirección que vamos a actualizar (debe existir en LIVE)
aws cloudfront describe-function --name ork3d-redirect-www --stage LIVE \
  --profile "$AWS_PROFILE" --query 'FunctionSummary.Name' --output text

# Permisos básicos de lectura (no crean nada). Si alguno da AccessDenied: parar.
aws cloudfront list-distributions --profile "$AWS_PROFILE" --query 'DistributionList.Quantity' --output text
aws iam list-roles --max-items 1 --profile "$AWS_PROFILE" --query 'Roles[0].RoleName' --output text
aws lambda list-functions --max-items 1 --profile "$AWS_PROFILE" --query 'Functions[0].FunctionName' --output text
```

Esperado: el bucket libre; el rol y la Lambda "no existe" (primera vez); el log group vacío; el nombre
`ork3d-redirect-www`; los tres listados devuelven un valor o `None` (sin error).

### Bloque 4: respaldo de la CloudFront Function actual (para el rollback)

```
BK="$HOME/ork3d-backup-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BK"
aws cloudfront get-function --name ork3d-redirect-www --stage LIVE --profile "$AWS_PROFILE" "$BK/redirect-www.LIVE.js"
diff "$BK/redirect-www.LIVE.js" <(git -C "$REPO" show b6c9fb2:infra/redirect-www.js) \
  && echo "LIVE = infra/redirect-www.js de main (b6c9fb2)"
echo "$BK" > "$HOME/.ork3d-ultimo-backup"
```

Esperado: `LIVE = infra/redirect-www.js de main (b6c9fb2)`. Si `diff` muestra diferencias: alguien cambió la función en
AWS. **Parar** y revisar qué cambió antes de seguir. El respaldo queda en `$BK`: guardalo.

### Bloque 5: repo y tests

```
cd "$REPO"
git status --short                    # esperado: vacío (sin cambios sin commitear)
git rev-parse --short HEAD            # el commit que vas a desplegar
git rev-parse --short origin/main     # esperado: b6c9fb2 (hasta que se autorice el merge)
dev/suite.sh                          # esperado: "Suite: todas las capas en verde."
```

Esperado de `dev/suite.sh` (todas las capas en verde, 0 fallos, 0 skipped):

- API y contrato: `Ran 73 tests` → `OK`.
- CLI de moderación: `Resultado: 97 PASS, 0 FAIL`.
- Navegador: `24/24 casos OK`.
- Validaciones estáticas: `Validaciones: 63 PASS, 0 FAIL`.

Si cualquier capa falla: **no desplegar**.

### Bloque 6: dry-run (obligatorio)

```
cd "$REPO"
DRY_RUN=1 infra/resenas-setup.sh > "$HOME/ork3d-dryrun.log" 2>&1
echo "exit=$?"
echo "llamadas reales a AWS (esperado 0): $(grep -c '^  \$ aws' "$HOME/ork3d-dryrun.log")"
echo "llamadas simuladas (esperado 18): $(grep -c '\[dry-run\] aws' "$HOME/ork3d-dryrun.log")"
```

Esperado: `exit=0`, `llamadas reales a AWS: 0`, `llamadas simuladas: 18`.

---

## 7. Dry-run: qué tiene que mostrar (D)

Leé el log completo: `less "$HOME/ork3d-dryrun.log"`. Las 18 llamadas simuladas se agrupan así:

| Paso | Llamadas simuladas | Qué crea o cambia |
| --- | --- | --- |
| 1. Bucket | 7: `create-bucket`, `put-public-access-block`, `put-bucket-versioning`, `put-bucket-encryption`, `put-bucket-lifecycle-configuration`, `put-bucket-cors`, `put-bucket-policy` | Bucket privado con todas sus propiedades |
| 2. Rol | 3: `create-role`, `put-role-policy`, `attach-role-policy` | Rol IAM mínimo |
| 3. Lambda | 3: `create-function`, `wait function-active-v2`, `put-function-concurrency` | Lambda, 256 MB, timeout 10 s, concurrencia 5 |
| 4. Function URL | 2: `create-function-url-config`, `add-permission` | URL pública sin CORS propio |
| 5. CloudFront | 2: `update-function`, `publish-function` | Nueva versión de `ork3d-redirect-www` |
| 6. Logs | 1: `put-retention-policy` | Retención de 30 días |

Y el resumen al principio del log: 7 recursos, costo fijo USD 0, costo variable potencial menor a USD 0,30 al mes.

Si aparece cualquier otro servicio, cualquier `delete-*`, o cualquier `update-distribution`: **parar**.

---

## 8. Checklist de revisión del dry-run (E)

Marcá cada ítem antes de la ejecución real. Si alguno no coincide: parar.

- [ ] Región: `us-east-1` (o la que definiste en `AWS_REGION`).
- [ ] Bucket: `ork3d-resenas-privado-<tu account>`, en minúsculas, sin el placeholder `<ACCOUNT_ID>` si la ejecución es real.
- [ ] Hay exactamente 7 recursos en el resumen (bucket, rol, Lambda, Function URL, permiso de invocación, log group, CloudFront Function).
- [ ] Servicios en el log, solo estos: `grep -o 'aws [a-z0-9-]*' "$HOME/ork3d-dryrun.log" | sort | uniq -c` debe dar `cloudfront`, `iam`, `lambda`, `logs` y `s3api`.
- [ ] Ningún servicio prohibido: `grep -i -E 'aws (ec2|rds|elasticache|apigateway|apigatewayv2|ecs|eks|elb|elbv2|nat)' "$HOME/ork3d-dryrun.log"` no devuelve nada.
- [ ] Ningún borrado: `grep -E 'delete|--recursive|s3 rm' "$HOME/ork3d-dryrun.log"` no devuelve nada.
- [ ] Bloqueo de acceso público: los cuatro flags en `true`.
- [ ] Versionado: `Status=Enabled`.
- [ ] Cifrado: `AES256`.
- [ ] Lifecycle: `pendientes/` vence a los 2 días y las versiones no actuales a los 30 días (`infra/resenas/bucket-lifecycle.json`).
- [ ] CORS: origen `https://ork3d.com`, método `POST` (`infra/resenas/bucket-cors.json`).
- [ ] Política del bucket: niega tráfico sin TLS (`infra/resenas/bucket-policy.json`).
- [ ] Rol: lee `tokens/`, `claims/`, `resenas/`, `pendientes/`; escribe `claims/`, `resenas/`, `pendientes/`; borra `claims/`. Ningún comodín (`infra/resenas/lambda-permisos.json`).
- [ ] Lambda: `python3.12`, 256 MB, timeout 10 s, concurrencia reservada 5.
- [ ] Variables de entorno de la Lambda: `RESENAS_BUCKET`, `ALLOWED_ORIGIN`, `GOOGLE_REVIEW_URL`. Ningún secreto.
- [ ] Log group con retención de 30 días.
- [ ] CloudFront: solo `update-function` y `publish-function` sobre `ork3d-redirect-www`. No aparece `update-distribution`.
- [ ] El log no contiene ningún account ID real (solo el placeholder `<ACCOUNT_ID>`, o tu bucket local si lo definiste).
- [ ] Bloques 1 a 6 de la sección 6 completos, con las salidas esperadas.

---

## 9. Ejecución real (F)

Solo después de completar la checklist. Desde la raíz del repo y con el profile activo:

```
cd "$REPO"
export AWS_PROFILE=ork3d
DRY_RUN=0 infra/resenas-setup.sh 2>&1 | tee "$HOME/ork3d-deploy-$(date -u +%Y%m%dT%H%M%SZ).log"
```

El log queda en tu máquina. Contiene tu account ID: no lo compartas en público.

### Qué tiene que pasar, paso por paso

| # | Salida esperada | Si no pasa |
| --- | --- | --- |
| 1 | `Cuenta AWS: <tu ID> · región: us-east-1 · profile: ork3d` | Si la cuenta no es la esperada: `Ctrl-C` ahora. |
| 2 | El resumen de 7 recursos. | — |
| 3 | `▶ Chequeos previos (solo lectura)` con tres líneas `OK`: bucket del sitio, alias `ork3d.com`, función en LIVE. | Cualquier `ERROR` corta sin cambios. Corregir la causa y volver a empezar. |
| 4 | El aviso de qué se va a crear, y el prompt: `Escribí CREAR para continuar`. | Escribir `CREAR` (exacto, en mayúsculas). Cualquier otra cosa cancela: `Cancelado. No se modificó AWS.` |
| 5 | `▶ Bucket privado` con `$ aws s3api create-bucket` (solo la primera vez) y seis `$ aws s3api put-…`. Sin errores. | `BucketAlreadyExists`: el nombre lo tiene otra cuenta. Cambiá `RESENAS_BUCKET` en `infra/.env` y empezá de nuevo. |
| 6 | `▶ Rol ork3d-resenas-lambda`, después `esperando la propagación de IAM…` (10 s). | `The role defined for the function cannot be assumed`: esperá un minuto y relanzá. |
| 7 | `▶ Lambda ork3d-resenas`: `create-function` (primera vez), `wait function-active-v2` (30 s a 1 min), `put-function-concurrency`. | Si ves `⚠ No se pudo fijar la concurrencia`: ver el comando manual abajo. |
| 8 | `▶ Function URL`: `create-function-url-config` y `add-permission` (o `el permiso ya existía`). | — |
| 9 | `▶ CloudFront Function ork3d-redirect-www`: `update-function` y `publish-function`, sin salida. | Si falla `publish-function`: revisar el ETag. No reintentar en bucle. |
| 10 | `▶ Verificación de la API`: `GET /tokens/<falso> → HTTP 200 {"ok": false, "motivo": "invalido"}` y `preflight → access-control-allow-origin: https://ork3d.com`. | `HTTP 403` o `Forbidden`: sección 11. Cualquier otra respuesta: parar. |
| 11 | `▶ Retención de logs: 30 días`, sin salida. | `⚠`: correr el comando manual abajo. |
| 12 | `▶ Listo. Próximos pasos` con la línea `Function URL: https://…`. | — |

Comando manual de concurrencia, si el paso 7 avisó:

```
aws lambda put-function-concurrency --function-name ork3d-resenas \
  --reserved-concurrent-executions 5 --profile "$AWS_PROFILE"
```

Comando manual de retención, si el paso 11 avisó:

```
aws logs put-retention-policy --log-group-name /aws/lambda/ork3d-resenas \
  --retention-in-days 30 --profile "$AWS_PROFILE"
```

Si el script se corta con error en medio: **no lo relances a ciegas**. Leé el último comando del log, corregí la causa,
y volvé a correr el script. Es idempotente: reutiliza lo que ya existe.

---

## 10. Verificación post-creación (G)

Con la misma sesión, cada comando tiene su salida esperada. Todos son de solo lectura.

### S3

```
aws s3api head-bucket --bucket "$RESENAS_BUCKET" --profile "$AWS_PROFILE" && echo "bucket: existe"
aws s3api get-public-access-block --bucket "$RESENAS_BUCKET" --profile "$AWS_PROFILE" \
  --query PublicAccessBlockConfiguration --output json
#   esperado: BlockPublicAcls, IgnorePublicAcls, BlockPublicPolicy y RestrictPublicBuckets en true
aws s3api get-bucket-policy-status --bucket "$RESENAS_BUCKET" --profile "$AWS_PROFILE" \
  --query PolicyStatus.IsPublic --output text
#   esperado: False
aws s3api get-bucket-versioning --bucket "$RESENAS_BUCKET" --profile "$AWS_PROFILE" --query Status --output text
#   esperado: Enabled
aws s3api get-bucket-encryption --bucket "$RESENAS_BUCKET" --profile "$AWS_PROFILE" \
  --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' --output text
#   esperado: AES256
aws s3api get-bucket-lifecycle-configuration --bucket "$RESENAS_BUCKET" --profile "$AWS_PROFILE" --output json
#   esperado: pendientes-vencen-a-los-2-dias (Expiration Days 2) y versiones-anteriores-30-dias (NoncurrentDays 30)
aws s3api get-bucket-cors --bucket "$RESENAS_BUCKET" --profile "$AWS_PROFILE" --output json
#   esperado: AllowedOrigins ["https://ork3d.com"], AllowedMethods ["POST"]
aws s3api get-bucket-policy --bucket "$RESENAS_BUCKET" --profile "$AWS_PROFILE" --query Policy --output text | jq '.Statement[] | {Sid, Effect}'
#   esperado: SinTransporteSeguroNoPermitido, Effect Deny
```

### IAM

```
aws iam get-role --role-name ork3d-resenas-lambda --profile "$AWS_PROFILE" \
  --query 'Role.{Name:RoleName,Arn:Arn}' --output json
aws iam get-role-policy --role-name ork3d-resenas-lambda --policy-name resenas-bucket-privado \
  --profile "$AWS_PROFILE" --query PolicyDocument --output json | jq '.Statement[] | {Sid, Actions: .Action, Resources: .Resource}'
#   esperado: 3 sentencias (LeerEstadoReseniasYFotos, EscribirClaimsResenasYFirmarFotos, LiberarClaimSiFallaLaEscritura)
#   ningún Resource "*"; ningún s3:* ; ningún PutObject sobre tokens/
aws iam list-attached-role-policies --role-name ork3d-resenas-lambda --profile "$AWS_PROFILE" --output text
#   esperado: arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

### Lambda

```
aws lambda get-function-configuration --function-name ork3d-resenas --profile "$AWS_PROFILE" \
  --query '{State:State,Runtime:Runtime,Handler:Handler,Memory:MemorySize,Timeout:Timeout}' --output json
#   esperado: State Active, Runtime python3.12, Handler app.handler, Memory 256, Timeout 10
aws lambda get-function-configuration --function-name ork3d-resenas --profile "$AWS_PROFILE" \
  --query 'Environment.Variables' --output json
#   esperado: solo RESENAS_BUCKET, ALLOWED_ORIGIN, GOOGLE_REVIEW_URL (sin secretos)
aws lambda get-function-concurrency --function-name ork3d-resenas --profile "$AWS_PROFILE" --output json
#   esperado: {"ReservedConcurrentExecutions": 5}
aws lambda get-function-url-config --function-name ork3d-resenas --profile "$AWS_PROFILE" --output json
#   esperado: AuthType NONE, y sin bloque Cors (o Cors vacío)
aws lambda get-policy --function-name ork3d-resenas --profile "$AWS_PROFILE" --query Policy --output text \
  | jq '.Statement[] | {Sid, Action, Principal, Condition}'
#   esperado: FunctionURLAllowPublicAccess, lambda:InvokeFunctionUrl, condición FunctionUrlAuthType NONE
```

### CloudWatch Logs

```
aws logs describe-log-groups --log-group-name-prefix /aws/lambda/ork3d-resenas --profile "$AWS_PROFILE" \
  --query 'logGroups[].{Name:logGroupName,RetentionDays:retentionInDays}' --output table
#   esperado: /aws/lambda/ork3d-resenas con RetentionDays 30
```

### CloudFront

```
aws cloudfront describe-function --name ork3d-redirect-www --stage LIVE --profile "$AWS_PROFILE" \
  --query 'FunctionSummary.FunctionMetadata.{Stage:Stage,LastModified:LastModifiedTime}' --output table
#   esperado: Stage LIVE, fecha de hoy
aws cloudfront get-function --name ork3d-redirect-www --stage LIVE --profile "$AWS_PROFILE" "$HOME/ork3d-live-post.js" >/dev/null
grep -c '/opinar/' "$HOME/ork3d-live-post.js"
#   esperado: 1 o más
aws cloudfront get-distribution-config --id "$DISTRIBUTION_ID" --profile "$AWS_PROFILE" \
  --query 'DistributionConfig.Aliases.Items' --output text
#   esperado: ork3d.com (sin cambios en la distribución)
```

Lo que no se verifica todavía, porque depende del deploy del frontend: que `https://ork3d.com/opinar/<token>` se
reescriba bien. Eso va en la sección 14.

---

## 11. Function URL (H)

La URL pública de la Lambda. Se obtiene así (solo lectura):

```
aws lambda get-function-url-config --function-name ork3d-resenas --profile "$AWS_PROFILE" \
  --query FunctionUrl --output text
```

Esperado: `https://<id>.lambda-url.us-east-1.on.aws/` (con barra final). Al pasarla a `config.js` se quita la barra
final: `https://<id>.lambda-url.us-east-1.on.aws`.

Esta URL también aparece en la línea `Function URL:` al final de la ejecución real.

**No la pegues en `config.js` todavía.** Pasamela a mí. El cambio de `config.js` es un commit aparte, que se hace
después de autorizar el deploy del frontend.

Verificación con la URL (lectura: solo consulta el token falso, no crea nada):

```
API="https://<id>.lambda-url.us-east-1.on.aws"
curl -s -w '\nHTTP %{http_code}\n' "$API/tokens/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
#   esperado: {"ok": false, "motivo": "invalido"} y HTTP 200
curl -s -o /dev/null -D - -X OPTIONS -H "Origin: https://ork3d.com" \
  -H "Access-Control-Request-Method: POST" "$API/resenas" | grep -i '^access-control'
#   esperado: access-control-allow-origin: https://ork3d.com
#             access-control-allow-methods: GET,POST,OPTIONS
```

Si la primera consulta devuelve `HTTP 403` o `{"Message":"Forbidden"}`: la invocación pública no está permitida. La
Function URL con `AuthType NONE` necesita el permiso `lambda:InvokeFunctionUrl` con principal `*`. El script ya lo
agrega. Si aun así falla, la alternativa es agregar también `lambda:InvokeFunction` con principal `*`:

```
aws lambda add-permission --function-name ork3d-resenas --statement-id FunctionInvokePublicAccess \
  --action lambda:InvokeFunction --principal "*" --profile "$AWS_PROFILE"
```

Ese permiso deja invocar la función con credenciales de cualquier cuenta AWS. La API sigue exigiendo un token válido
para escribir. Es una decisión de seguridad: **no la apliques sin mi confirmación.**

---

## 12. Cloudflare y DNS: superficie de privacidad (6)

Objetivo: saber si `ork3d.com` (y `www`) pasan por el proxy de Cloudflare. Si pasan, Cloudflare ve la URL completa
`/opinar/<token>` (el token va en el path de la página). **No bloquea el proyecto**, pero es una superficie de
privacidad que hay que dejar documentada. Esta sección no modifica DNS.

### Método 1: DNS (solo lectura)

```
dig +short ork3d.com A
dig +short ork3d.com AAAA
dig +short www.ork3d.com A
dig +short ork3d.com NS          # *.ns.cloudflare.com indica que el DNS está hospedado en Cloudflare
```

Interpretación:

- Las IPs A están en rangos de **Cloudflare** → el proxy está activo. Lista oficial: `https://www.cloudflare.com/ips-v4` (y `ips-v6`).
- Las IPs A están en rangos de **CloudFront** → el DNS apunta directo a CloudFront, sin proxy. Lista oficial:
  `https://ip-ranges.amazonaws.com/ip-ranges.json` (servicio `CLOUDFRONT`).
- Los NS son de Cloudflare pero las IPs son de CloudFront → DNS en Cloudflare **sin proxy** (nube gris).

### Método 2: headers HTTP (solo lectura)

```
curl -sI https://ork3d.com/ | grep -i -E '^(server|cf-ray|via|x-amz-cf-id|x-cache):'
```

- `server: cloudflare` y `cf-ray` → la respuesta pasó por Cloudflare.
- `x-amz-cf-id` y `x-cache: ... from cloudfront` → la respuesta pasó por CloudFront.
- Ambos grupos → cadena **Cloudflare → CloudFront**: proxy activo.
- Solo el grupo de CloudFront → sin proxy.

### Método 3: configuración conocida (solo lectura, en el dashboard)

El README del repo dice que el DNS está en Cloudflare, con CNAME flattening en el apex. En el dashboard de
Cloudflare, DNS → Records, mirá los registros de `ork3d.com` y `www`: **nube naranja** = proxied; **nube gris** = DNS only.

### Qué hacer con el resultado

- **Sin proxy**: no hay superficie adicional. Documentar y seguir.
- **Con proxy**: documentar que Cloudflare ve las URLs de `/opinar/<token>` (y la IP del cliente). Revisar en el
  dashboard qué analíticas o logs guarda el plan actual. No cambiar DNS en esta prueba.
- Opciones futuras, sin tomar decisión ahora: publicar la página de reseñas en un subdominio DNS-only, o cambiar el
  esquema para que el token no viaje en el path. Ambas implican cambio de arquitectura: otra aprobación.

Nota: la API (`*.lambda-url.us-east-1.on.aws`) **no** pasa por Cloudflare. El token viaja a la Lambda directo.
Solo la navegación a `/opinar/<token>` pasa por el DNS del sitio.

---

## 13. Prueba de atomicidad IfNoneMatch (I). No ejecutar todavía

Objetivo: confirmar contra S3 real que dos claims concurrentes sobre el mismo token dan **un solo éxito**, y que
se crea **una sola reseña**. Esta prueba crea datos reales en producción. Se ejecuta solo con autorización explícita.

### Requisitos

- Function URL conocida (sección 11).
- `admin/.env` con los valores reales: `RESENAS_BUCKET`, `SITE_BUCKET=ork3d.com`, `CF_DISTRIBUTION_ID`, `SITE_URL=https://ork3d.com`.
- Autorización explícita para crear datos de prueba (token `SMOKE-TEST-ORK3D`).

### 13.1 Primitiva S3 (sin reseñas): dos `put-object` con `--if-none-match`

Antes, comprobá que tu versión de AWS CLI soporta el parámetro:

```
aws s3api put-object help | grep -c -- '--if-none-match'
#   esperado: 1 o más. Si da 0: actualizá AWS CLI v2 antes de seguir.
```

Prueba (clave de prueba bajo `claims/`, que se borra al final):

```
KEY="claims/smoke-atomicidad-$(date -u +%Y%m%dT%H%M%SZ).json"
printf '{"smoke":"atomicidad"}' > "$HOME/claim-a.json"
printf '{"smoke":"atomicidad"}' > "$HOME/claim-b.json"
( aws s3api put-object --bucket "$RESENAS_BUCKET" --key "$KEY" --body "$HOME/claim-a.json" \
    --if-none-match '*' --profile "$AWS_PROFILE" >/dev/null 2>"$HOME/err-a.txt"; echo "A exit=$?" ) &
( aws s3api put-object --bucket "$RESENAS_BUCKET" --key "$KEY" --body "$HOME/claim-b.json" \
    --if-none-match '*' --profile "$AWS_PROFILE" >/dev/null 2>"$HOME/err-b.txt"; echo "B exit=$?" ) &
wait
```

Esperado: **un `exit=0` y un `exit=255`**. El perdedor dice `PreconditionFailed` en su archivo de error
(`grep PreconditionFailed "$HOME/err-a.txt" "$HOME/err-b.txt"`). Si los dos dan `exit=0`: **la atomicidad no
funciona**. Parar y no continuar con la prueba de la API.

Limpieza: con versionado activo, `delete-object` solo deja una marca de borrado. Hay que borrar todas las versiones
(procedimiento de la sección 15.4):

```
borrar_versiones_de_clave "$RESENAS_BUCKET" "$KEY"
```

### 13.2 Prueba end-to-end con la API: un token, dos envíos simultáneos

1. Crear el token de prueba con la CLI real (crea datos en producción):

```
cd "$REPO"
admin/resenas.sh token crear --pedido SMOKE-TEST-ORK3D --producto smoke-test --categoria test \
  --producto-label "Prueba técnica" --dias 1
```

Copiá el token del link `https://ork3d.com/opinar/<TOKEN>` y guardalo en una variable **sin imprimirlo en logs públicos**:

```
TOKEN="<TOKEN>"
TOKEN_HASH="$(printf '%s' "$TOKEN" | (shasum -a 256 2>/dev/null || sha256sum) | cut -d' ' -f1)"
echo "hash del token: ${TOKEN_HASH}"
```

2. Payload de prueba, sin datos personales:

```
cat > "$HOME/smoke-payload.json" <<EOF
{"token":"$TOKEN","rating":5,"comment":"Reseña técnica de prueba. No publicar.","aspects":["calidad"],"display_mode":"anonimo","name":"","permission_to_publish":true,"private_feedback":"Smoke test técnico.","contact_requested":false,"website":""}
EOF
```

3. Dos envíos prácticamente simultáneos:

```
API="https://<id>.lambda-url.us-east-1.on.aws"
( curl -s -o "$HOME/r1.json" -w 'A:%{http_code}\n' -X POST "$API/resenas" \
    -H 'Content-Type: application/json' --data @"$HOME/smoke-payload.json" ) &
( curl -s -o "$HOME/r2.json" -w 'B:%{http_code}\n' -X POST "$API/resenas" \
    -H 'Content-Type: application/json' --data @"$HOME/smoke-payload.json" ) &
wait
cat "$HOME/r1.json"; echo; cat "$HOME/r2.json"; echo
```

Esperado: **un 200 y un 409**. El 409 trae `{"ok": false, "motivo": "usado"}`. Si los dos dan 200: **parar**: el token se
usó dos veces.

4. Verificar que existe una sola reseña y un solo claim:

```
aws s3api list-objects-v2 --bucket "$RESENAS_BUCKET" --prefix resenas/ --profile "$AWS_PROFILE" \
  --query 'Contents[].Key' --output text | tr '\t' '\n' | grep '^resenas/' | while read -r k; do
    aws s3 cp "s3://$RESENAS_BUCKET/$k" - --profile "$AWS_PROFILE" \
      | jq -r --arg h "$TOKEN_HASH" 'select(.token_hash == $h) | "\(.id)  \(.status)"'
  done
#   esperado: exactamente UNA línea (id de 32 caracteres y status pending)

aws s3api head-object --bucket "$RESENAS_BUCKET" --key "claims/$TOKEN_HASH.json" --profile "$AWS_PROFILE" \
  --query ContentLength --output text
#   esperado: un número (el claim existe)
```

Si aparece más de una línea: **parar**. Eso es un fallo de atomicidad.

Después de validar, la limpieza de esta prueba va en la sección 15.5. Lo que queda y por qué está explicado allí.

---

## 14. Smoke test de cliente y moderación (pendiente de autorización)

Se ejecuta después del deploy del frontend. Es la parte del pedido original que valida la página real. Pasos y salidas esperadas:

1. **Home y páginas**: `https://ork3d.com` sin cambios: `curl -s https://ork3d.com/ | grep -c -i -E 'opiniones|reseña|resenas'` → `0`.
2. **Token B**: `admin/resenas.sh token crear --pedido SMOKE-TEST-ORK3D --producto smoke-test --categoria test --producto-label "Prueba técnica" --dias 1`. Abrí el link en escritorio y en celular.
3. **Página** `https://ork3d.com/opinar/<TOKEN_B>`: carga el formulario con el producto "Prueba técnica". Sin scripts de terceros, sin fuentes externas. `noindex` y `referrer no-referrer` en el HTML.
4. **Formulario**: 5 estrellas; nombre `Prueba ORK3D`; un aspecto; comentario `Reseña técnica de prueba. No publicar.`; feedback privado `Smoke test técnico.`; sin contacto; permiso marcado.
5. **Foto**: subir un JPEG de prueba (de tu máquina). Esperado: "Foto lista."
6. **Envío**: esperado: pantalla "Gracias por tu opinión".
7. **Moderación** (CLI real): `admin/resenas.sh pendientes` (aparece el ID de 32 caracteres). `admin/resenas.sh ver <ID>` (muestra el texto, el feedback privado, el pedido interno `SMOKE-TEST-ORK3D`). `admin/resenas.sh aprobar <ID>`.
8. **Export**: `curl -s https://ork3d.com/resenas/publicas.json | jq . > "$HOME/export-smoke.json"`. Esperado en el esquema de campos:

   ```
   jq -r '[.items[] | keys[]] | unique | join(" ")' "$HOME/export-smoke.json"
   #   esperado exacto: aspects category comment display_name id photo_url product_label published_at rating verified
   ```

   Cualquier otro campo (email, teléfono, `order_ref`, feedback privado, `contact_requested`, token, hash, clave de foto): **ERROR**. Parar.
9. **Foto en AWS**: la reseña tiene `photo_key` en `pendientes/`. Antes de aprobar, esa foto **no** está en el sitio. Después de aprobar, la copia pública está en `resenas/fotos/<ID>.jpg` y la privada sigue en `pendientes/`. Verificá sin metadatos: la Lambda ya rechaza EXIF, XMP, IPTC y comentarios al subir; si quisieras confirmarlo, `aws s3 cp` a un archivo local y buscá `Exif` con `grep -c Exif`: esperado `0`.
10. **Segundo envío**: reenviá el mismo link. Esperado: pantalla "Ya recibimos tu opinión" y ninguna reseña nueva.
11. **Limpieza**: sección 15.5.

---

## 15. Rollback (J)

### 15.1 Inventario: qué se crea y qué se modifica

**Recursos nuevos** (se pueden borrar, solo si los creó este runbook):

- Bucket S3 privado `$RESENAS_BUCKET` (con sus versiones y marcas de borrado).
- Rol IAM `ork3d-resenas-lambda`, con su política inline `resenas-bucket-privado` y la política administrada adjunta.
- Lambda `ork3d-resenas`, con su concurrencia reservada, su Function URL y el permiso `FunctionURLAllowPublicAccess`.
- Log group `/aws/lambda/ork3d-resenas`.

**Recursos existentes que se modifican:**

- CloudFront Function `ork3d-redirect-www`: se reemplaza el código de LIVE. Su rollback es restaurar el código respaldado
  en el bloque 4 del pre-flight.

**Sin cambios**: la distribución de CloudFront (su configuración), el bucket del sitio (salvo los archivos que suba un
deploy), DNS, certificados, otras funciones y otros roles.

### 15.2 Rollback de la CloudFront Function

Antes de restaurar, confirmá que LIVE es el código que se va a reemplazar:

```
BK="$(cat "$HOME/.ork3d-ultimo-backup")"
aws cloudfront get-function --name ork3d-redirect-www --stage LIVE --profile "$AWS_PROFILE" "$HOME/ork3d-live-antes-rollback.js" >/dev/null
diff "$HOME/ork3d-live-antes-rollback.js" <(git -C "$REPO" show b6c9fb2:infra/redirect-www.js) \
  && echo "LIVE = main: se puede restaurar"
```

Restauración (código del respaldo; si no tenés el respaldo, el de `b6c9fb2`):

```
cp "$BK/redirect-www.LIVE.js" "$HOME/redirect-www.rollback.js"
ETAG="$(aws cloudfront describe-function --name ork3d-redirect-www --profile "$AWS_PROFILE" --query ETag --output text)"
aws cloudfront update-function --name ork3d-redirect-www --if-match "$ETAG" \
  --function-config "Comment='Redirect www to apex',Runtime=cloudfront-js-2.0" \
  --function-code fileb://$HOME/redirect-www.rollback.js --profile "$AWS_PROFILE"
ETAG2="$(aws cloudfront describe-function --name ork3d-redirect-www --profile "$AWS_PROFILE" --query ETag --output text)"
aws cloudfront publish-function --name ork3d-redirect-www --if-match "$ETAG2" --profile "$AWS_PROFILE"
```

Verificación: `aws cloudfront describe-function --name ork3d-redirect-www --stage LIVE --profile "$AWS_PROFILE"` muestra el
código restaurado.

### 15.3 Eliminar recursos nuevos (manual, uno por uno)

Antes de cada comando, confirmá el nombre con su `get-*`. **Solo** los recursos creados por este runbook.

```
# 1. Function URL y concurrencia (se van con la función)
aws lambda delete-function-url-config --function-name ork3d-resenas --profile "$AWS_PROFILE"

# 2. Lambda
aws lambda delete-function --function-name ork3d-resenas --profile "$AWS_PROFILE"

# 3. Log group (opcional: borra los logs)
aws logs delete-log-group --log-group-name /aws/lambda/ork3d-resenas --profile "$AWS_PROFILE"

# 4. Rol: política inline, política administrada y el rol
aws iam delete-role-policy --role-name ork3d-resenas-lambda --policy-name resenas-bucket-privado --profile "$AWS_PROFILE"
aws iam detach-role-policy --role-name ork3d-resenas-lambda \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole --profile "$AWS_PROFILE"
aws iam delete-role --role-name ork3d-resenas-lambda --profile "$AWS_PROFILE"
```

Bucket privado: solo si es el de este runbook y ya no tiene datos que conservar. Primero vaciá todas las versiones,
en lotes, y revisá el conteo antes de borrar:

```
echo "Bucket a vaciar: $RESENAS_BUCKET"          # confirmá que es el correcto
aws s3api list-object-versions --bucket "$RESENAS_BUCKET" --profile "$AWS_PROFILE" --output json \
  | jq '{Objects: ([.Versions[]?, .DeleteMarkers[]?] | map({Key, VersionId}))}' > "$HOME/versiones-bucket.json"
jq '.Objects | length' "$HOME/versiones-bucket.json"   # cuántas versiones y marcas se borrarían
```

Si el conteo es el esperado y el bucket es el correcto, borrar en lotes de hasta 1000 objetos (`delete-objects`
admite ese máximo por llamada). Si el conteo es 0, saltear. Luego:

```
aws s3api delete-bucket --bucket "$RESENAS_BUCKET" --profile "$AWS_PROFILE"
```

### 15.4 Helper: borrar todas las versiones de una clave exacta

Es un procedimiento manual para una clave concreta. **No** borra prefijos: cada llamada apunta a una sola clave.

```
borrar_versiones_de_clave() {  # uso: borrar_versiones_de_clave BUCKET CLAVE_EXACTA
  local b="$1" k="$2" tmp n
  tmp="$(mktemp)"
  aws s3api list-object-versions --bucket "$b" --prefix "$k" --profile "$AWS_PROFILE" --output json \
    | jq -c --arg k "$k" '{Objects: ([.Versions[]?, .DeleteMarkers[]?] | map(select(.Key == $k)) | map({Key, VersionId}))}' > "$tmp"
  n="$(jq '.Objects | length' "$tmp")"
  if [ "$n" -gt 0 ]; then
    aws s3api delete-objects --bucket "$b" --delete "file://$tmp" --profile "$AWS_PROFILE" >/dev/null \
      && echo "borradas $n versiones de $b/$k"
  else
    echo "sin versiones de $b/$k"
  fi
  rm -f "$tmp"
}
```

Uso: `borrar_versiones_de_clave "$BUCKET" "resenas/fotos/<ID>.jpg"` (la copia pública va en el bucket del sitio; la
privada, en `$RESENAS_BUCKET`).

### 15.5 Limpieza de datos de prueba (smoke)

Objetivo: no dejar reseñas de prueba publicadas, ni en el export, ni en versiones de los buckets.

1. Anotar las claves de prueba: el ID de la reseña, el hash del token, la foto privada (`photo_key` de la reseña).

   ```
   RID="<ID>"
   aws s3 cp "s3://$RESENAS_BUCKET/resenas/$RID.json" - --profile "$AWS_PROFILE" | jq -r '.photo_key // "sin foto"'
   ```

2. Borrar cada clave, con el helper del 15.4 (una llamada por clave):

   ```
   borrar_versiones_de_clave "$RESENAS_BUCKET" "resenas/$RID.json"
   borrar_versiones_de_clave "$RESENAS_BUCKET" "tokens/$TOKEN_HASH.json"
   borrar_versiones_de_clave "$RESENAS_BUCKET" "claims/$TOKEN_HASH.json"
   borrar_versiones_de_clave "$BUCKET" "resenas/fotos/$RID.jpg"            # solo si hubo foto aprobada
   borrar_versiones_de_clave "$RESENAS_BUCKET" "<photo_key anotada>"       # solo si hubo foto
   ```

   Si quedó algún claim de la prueba de atomicidad (13.1) o del token A (13.2), va en la misma lista.

3. Regenerar el export público y confirmar que la reseña de prueba ya no está:

   ```
   cd "$REPO" && admin/resenas.sh exportar
   curl -s https://ork3d.com/resenas/publicas.json | jq '.count, [.items[].display_name]'
   #   esperado: el count sin la reseña de prueba; "Prueba ORK3D" no aparece
   ```

**Qué queda y por qué:** nada de la prueba en los buckets (sin reseñas, tokens ni claims de prueba), y el export sin
la reseña. Los logs de CloudWatch quedan 30 días: contienen IDs y prefijos de hash, pero no tokens completos ni texto
de reseñas. Si decidís conservar el claim `claims/<hash>.json` como traza de auditoría, dejalo **solo** ese archivo,
con el token ya borrado, y documentá la decisión. Por defecto no se conserva nada.

### 15.6 Revertir el frontend

Si el deploy del frontend hay que deshacerlo: revertí los commits con un **commit nuevo** en `main`
(`git revert <sha>`). **No** uses force push. El deploy que sigue sincroniza con `--delete`: los archivos que ya no
están en el repo (`opinar.html`, `opiniones.html`, `assets/js/opinar.js`, etc.) se borran del sitio. `deploy.sh`
excluye `resenas/`, `api/`, `admin/`, `dev/` y `docs/`, así que las reseñas publicadas no se tocan.

### 15.7 Reglas para no borrar datos de ORK3D existentes

- Nunca `aws s3 rm --recursive` ni `aws s3 sync --delete` contra el bucket del sitio (`ork3d.com`).
- Para retirar archivos del sitio, solo las claves exactas de este despliegue: `opinar.html`, `opiniones.html`,
  `assets/css/opinar.css`, `assets/css/opiniones.css`, `assets/js/opinar.js`, `assets/js/resenas-publicas.js`.
- En `resenas/` del sitio, solo los objetos de prueba identificados por ID o hash. Nunca el prefijo entero.
- No tocar la distribución de CloudFront, su configuración, ni borrar la función `ork3d-redirect-www`: solo se restaura
  su código (15.2).
- Antes de cada `delete-*`, confirmar el nombre con un `get-*` o `describe-*` y que el recurso lo creó este runbook.
- Ningún comando de este runbook borra por comodín.

---

## 16. Qué no hacer

- No merge a `main` ni deploy sin autorización explícita. El orden es: infraestructura, Function URL, `config.js` en un
  commit aparte, merge, deploy, smoke test.
- No `DRY_RUN=0` sin haber leído el dry-run completo y completado la checklist.
- No access keys en archivos, en comandos ni en el repo.
- No pegar account IDs, distribution IDs ni URLs reales en el repo ni en issues públicos.
- No cambiar `config.js` con una URL ficticia. Esperá la Function URL real.
- No enlazar `/opiniones`, ni agregarla al sitemap, ni tocar la home en este despliegue.
- No crear reseñas de prueba visibles públicamente. Si una prueba aprueba una reseña, la limpieza (15.5) es obligatoria.
