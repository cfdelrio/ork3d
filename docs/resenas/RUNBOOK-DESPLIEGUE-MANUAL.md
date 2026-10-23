# Runbook de despliegue manual · reseñas ORK3D (V1, prueba controlada)

**Estado: V1 completada en producción.** Estado vigente de V2 y comandos actuales: `docs/resenas/ESTADO-PRODUCCION.md`. Infraestructura, `config.js`, merge a `main`, deploy y smoke test productivo están cerrados. Lo que sigue abierto está en la sección 17.3. Este documento no crea ni modifica nada por sí solo.
Cada comando se corre a mano, en orden, y cada paso tiene una salida esperada. Si una salida no coincide, **parás**.

Qué crea el despliegue de infraestructura: un bucket S3 privado, un rol IAM mínimo, un log group con retención de 30 días,
una Lambda con Function URL (con sus dos permisos de invocación), concurrencia reservada solo si la cuenta tiene margen,
y una nueva versión de la CloudFront Function `ork3d-redirect-www` con la regla `/opinar/*`. **No toca** la distribución
de CloudFront, el bucket del sitio, DNS, certificados, `config.js`, `main` ni la home.

### Estado de validación

| Punto | Estado |
| --- | --- |
| `config.js` productivo (commit `768eaa0`) | ✔ COMPLETADO |
| Merge a `main` (fast-forward desde `b6c9fb2`) | ✔ COMPLETADO |
| Deploy GitHub Actions (run `39659217831`, #27: success) | ✔ COMPLETADO |
| Smoke test productivo (sección 14) | ✔ COMPLETADO |
| Flujo de foto: presign, JPEG y sin metadatos (sección 14.1) | ✔ COMPLETADO |
| Consumo del token: `motivo=usado` tras el envío y después de la limpieza (sección 14.1) | ✔ COMPLETADO |
| Moderación: pending, aprobación y CLI (sección 14.1) | ✔ COMPLETADO |
| Export público y privacidad de campos (sección 14.1) | ✔ COMPLETADO |
| Limpieza del smoke (sección 15.5) | ✔ COMPLETADO |
| `/opiniones` | ✔ Existe por URL directa, **no lanzada**: noindex, fuera del sitemap y sin enlaces desde home ni navegación |

Reglas:

- Nada se borra automáticamente. Los rollbacks son comandos manuales (sección 15).
- `DRY_RUN=1` antes que `DRY_RUN=0`, siempre. La infraestructura ya existe: `DRY_RUN=0` solo con autorización explícita.
- Ningún account ID, distribution ID, URL real ni credencial se commitea. Van en `infra/.env` (ignorado por git)
  o en tu shell.
- Los comandos de AWS usan `--profile "$AWS_PROFILE"`. No hay access keys en archivos ni en comandos.
- Si algo no coincide con lo esperado: parar, no reintentar a ciegas.

---

## 1. Revisión del script `infra/resenas-setup.sh`

| Punto | Resultado |
| --- | --- |
| Compatible con macOS y Linux | Sin features de bash 4+ ni flags exclusivos de GNU (revisado a mano). Probado con bash 5.2 y con un `aws` de prueba (`infra/tests`). Ejecutado contra AWS real desde la máquina del operador (sección 17). **Bash 3.2 y macOS: no probados.** |
| AWS CLI v2 | El modo real lo verifica: `aws --version` debe empezar con `aws-cli/2`. |
| `jq` y `zip` | Obligatorios. Se verifican al arrancar. |
| Dry-run por defecto | Sí: `DRY_RUN=1`. El dry-run no llama a AWS (verificado: 0 llamadas reales). |
| Credenciales | El script no lee credenciales. Usa el profile de AWS CLI (`AWS_PROFILE`). |
| Confirmación antes de cambiar nada | Sí, en modo real: hay que escribir `CREAR`. |
| Chequeos previos de solo lectura | Sí, en modo real: bucket del sitio accesible, distribución con alias `ork3d.com`, función de redirección en LIVE, y AWS CLI con `--invoked-via-function-url`. Si falla uno, corta sin cambios. |
| Borrados | Ninguno. El script no contiene `delete-*`, `--recursive` ni `s3 rm` (verificado en `dev/validar.py`). |

Estado actual del script (los hallazgos del despliegue real están detallados en la sección 17):

1. El bucket privado **no tiene un nombre fijo**. Si no definís `RESENAS_BUCKET`, se deriva del account ID:
   `ork3d-resenas-privado-<ACCOUNT_ID>`. Se valida el formato de nombre S3.
2. Chequeos previos de solo lectura antes de cualquier cambio (tabla de arriba).
3. Confirmación interactiva con `CREAR`.
4. **Función URL con dos permisos.** `lambda:InvokeFunctionUrl` (AuthType NONE) y `lambda:InvokeFunction` con
   `lambda:InvokedViaFunctionUrl = true`. Sin el segundo, la URL responde 403. Ambos se detectan por contenido de la
   política, no por nombre.
5. **Log group antes de la Lambda.** Se crea y recibe retención de 30 días antes de `create-function`. Así la primera
   invocación no crea el grupo sin retención.
6. **Concurrencia reservada opcional.** Solo se reserva si la cuenta tiene margen (sección 3.2).
7. **`s3:ListBucket` sobre el bucket**, sin prefijo. Sin ese permiso, una clave inexistente da `AccessDenied`, y la API
   respondía 500 en lugar de `{"ok": false, "motivo": "invalido"}`.
8. La descripción de la Lambda quedó en ASCII (algunos validadores de AWS rechazan caracteres no ASCII).
9. Corregido un bug de la verificación que mostraba `HTTP 000000` cuando no había respuesta.

Limitaciones conocidas:

- La concurrencia reservada no se configura si la cuenta no tiene margen. Es lo esperado en una cuenta con límite bajo (sección 3.2).
- Si la verificación de la API devuelve `Forbidden` o `HTTP 403`, falta uno de los dos permisos de la Function URL. Ver sección 11.

---

## 2. Requisitos locales (A)

| Herramienta | Mínimo | Cómo verificar | Notas |
| --- | --- | --- | --- |
| macOS o Linux | — | `uname -s` | |
| bash | 3.2 (probado en 5.2) | `bash --version` | Probado con bash 5.2. Bash 3.2 (el de macOS) no está probado para el script de infra. Si algún otro script falla, `brew install bash`. |
| AWS CLI | **v2** | `aws --version` → `aws-cli/2.x` | Versión reciente: el script exige `--invoked-via-function-url` (chequeo previo), y la prueba 13.1 usa `put-object --if-none-match`. |
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
RESENAS_LAMBDA_CONCURRENCY="5"        # opcional: se reserva solo con margen en la cuenta (sección 3.2)
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
| `RESENAS_LAMBDA_CONCURRENCY` | no | `5` | Default. Se reserva solo si la cuenta tiene margen (3.2). Si no, el script lo informa y sigue. |
| `IAM_PROPAGATION_SECONDS` | no | `10` | Espera tras crear el rol, para que IAM propague. Las pruebas la fijan en 0. |
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

### 3.2 Concurrencia reservada (opcional)

AWS exige que, después de reservar concurrencia para una función, queden **al menos 10** sin reservar en la cuenta.
Con `RESENAS_LAMBDA_CONCURRENCY=5`, la cuenta necesita **al menos 15** sin reservar (`UnreservedConcurrentExecutions`
en `get-account-settings`).

- Si hay margen, el script reserva la concurrencia.
- Si no hay margen, imprime `Reserved concurrency omitida: límite de cuenta insuficiente.` y **sigue**. La Lambda
  funciona igual: la reserva es un freno a costo ante abuso, no un requisito funcional.

En la ejecución real la cuenta tenía 10 sin reservar, así que la reserva de 5 se omitió (sección 17, hallazgo 3).
Para reservarla después: pedir un aumento del límite de concurrencia en Service Quotas y correr el comando manual
de la sección 9.

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

# Margen de concurrencia de la cuenta (solo lectura). Esperado: un número. Ver sección 3.2.
aws lambda get-account-settings --profile "$AWS_PROFILE" \
  --query 'AccountLimit.UnreservedConcurrentExecutions' --output text

# Función de redirección que vamos a actualizar (debe existir en LIVE)
aws cloudfront describe-function --name ork3d-redirect-www --stage LIVE \
  --profile "$AWS_PROFILE" --query 'FunctionSummary.Name' --output text

# Permisos básicos de lectura (no crean nada). Si alguno da AccessDenied: parar.
aws cloudfront list-distributions --profile "$AWS_PROFILE" --query 'DistributionList.Quantity' --output text
aws iam list-roles --max-items 1 --profile "$AWS_PROFILE" --query 'Roles[0].RoleName' --output text
aws lambda list-functions --max-items 1 --profile "$AWS_PROFILE" --query 'Functions[0].FunctionName' --output text
```

Esperado: el bucket libre; el rol y la Lambda "no existe" (primera vez); el log group vacío; el nombre
`ork3d-redirect-www`; los tres listados devuelven un valor o `None` (sin error); la concurrencia sin reservar es un
número. Si es menor que 15, la reserva se omitirá: es esperado (sección 3.2).

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

- API y contrato: `Ran 76 tests` → `OK`.
- Infra (script y políticas, sin AWS): `Ran 18 tests` → `OK`.
- CLI de moderación: `Resultado: 97 PASS, 0 FAIL`.
- Navegador: `24/24 casos OK`.
- Validaciones estáticas: `Validaciones: 65 PASS, 0 FAIL`.

Si cualquier capa falla: **no desplegar**.

### Bloque 6: dry-run (obligatorio)

```
cd "$REPO"
DRY_RUN=1 infra/resenas-setup.sh > "$HOME/ork3d-dryrun.log" 2>&1
echo "exit=$?"
echo "llamadas reales a AWS (esperado 0): $(grep -c '^  \$ aws' "$HOME/ork3d-dryrun.log")"
echo "llamadas simuladas (esperado 19): $(grep -c '\[dry-run\] aws' "$HOME/ork3d-dryrun.log")"
```

Esperado: `exit=0`, `llamadas reales a AWS: 0`, `llamadas simuladas: 19`.

---

## 7. Dry-run: qué tiene que mostrar (D)

Leé el log completo: `less "$HOME/ork3d-dryrun.log"`. Las 19 llamadas simuladas se agrupan así, en el orden del script:

| Paso | Llamadas simuladas | Qué crea o cambia |
| --- | --- | --- |
| 1. Bucket | 7: `create-bucket`, `put-public-access-block`, `put-bucket-versioning`, `put-bucket-encryption`, `put-bucket-lifecycle-configuration`, `put-bucket-cors`, `put-bucket-policy` | Bucket privado con todas sus propiedades |
| 2. Rol | 3: `create-role`, `put-role-policy`, `attach-role-policy` | Rol IAM mínimo, con `ListBucket` sobre el bucket |
| 3. Log group | 2: `create-log-group`, `put-retention-policy` | Log group con retención de 30 días, antes de la Lambda |
| 4. Lambda | 2: `create-function`, `wait function-active-v2` | Lambda, python3.12, 256 MB, timeout 10 s |
| 5. Concurrencia | 0: solo una línea `[dry-run] consultar aws lambda get-account-settings; ...` | Se decide en modo real, con margen (sección 3.2) |
| 6. Function URL | 3: `create-function-url-config`, `add-permission` (`FunctionURLAllowPublicAccess`), `add-permission` (`FunctionURLAllowInvokeViaUrl`) | URL pública sin CORS propio, con sus dos permisos |
| 7. CloudFront | 2: `update-function`, `publish-function` | Nueva versión de `ork3d-redirect-www` |

Total: 7 + 3 + 2 + 2 + 0 + 3 + 2 = **19** llamadas simuladas.

Y el resumen al principio del log: 7 recursos, costo fijo USD 0, costo variable potencial menor a USD 0,30 al mes.

Si aparece cualquier otro servicio, cualquier `delete-*`, o cualquier `update-distribution`: **parar**.

---

## 8. Checklist de revisión del dry-run (E)

Marcá cada ítem antes de la ejecución real. Si alguno no coincide: parar.

- [ ] Región: `us-east-1` (o la que definiste en `AWS_REGION`).
- [ ] Bucket: `ork3d-resenas-privado-<tu account>`, en minúsculas, sin el placeholder `<ACCOUNT_ID>` si la ejecución es real.
- [ ] Hay exactamente 7 recursos en el resumen (bucket, rol, log group, Lambda, Function URL, permisos de invocación, CloudFront Function).
- [ ] Servicios en el log, solo estos: `grep -o 'aws [a-z0-9-]*' "$HOME/ork3d-dryrun.log" | sort | uniq -c` debe dar `cloudfront`, `iam`, `lambda`, `logs` y `s3api`.
- [ ] Ningún servicio prohibido: `grep -i -E 'aws (ec2|rds|elasticache|apigateway|apigatewayv2|ecs|eks|elb|elbv2|nat)' "$HOME/ork3d-dryrun.log"` no devuelve nada.
- [ ] Ningún borrado: `grep -E 'delete|--recursive|s3 rm' "$HOME/ork3d-dryrun.log"` no devuelve nada.
- [ ] Bloqueo de acceso público: los cuatro flags en `true`.
- [ ] Versionado: `Status=Enabled`.
- [ ] Cifrado: `AES256`.
- [ ] Lifecycle: `pendientes/` vence a los 2 días y las versiones no actuales a los 30 días (`infra/resenas/bucket-lifecycle.json`).
- [ ] CORS: origen `https://ork3d.com`, método `POST` (`infra/resenas/bucket-cors.json`).
- [ ] Política del bucket: niega tráfico sin TLS (`infra/resenas/bucket-policy.json`).
- [ ] Rol: lee `tokens/`, `claims/`, `resenas/`, `pendientes/`; escribe `claims/`, `resenas/`, `pendientes/`; borra `claims/`; lista solo el bucket (`ListBucket`, sin prefijo). Ningún comodín (`infra/resenas/lambda-permisos.json`).
- [ ] Lambda: `python3.12`, 256 MB, timeout 10 s. Concurrencia reservada solo si la cuenta tiene margen (sección 3.2); su ausencia es válida.
- [ ] Function URL: dos permisos de invocación (`FunctionURLAllowPublicAccess` y `FunctionURLAllowInvokeViaUrl`).
- [ ] Variables de entorno de la Lambda: `RESENAS_BUCKET`, `ALLOWED_ORIGIN`, `GOOGLE_REVIEW_URL`. Ningún secreto.
- [ ] Log group: aparece antes que la Lambda y con retención de 30 días.
- [ ] CloudFront: solo `update-function` y `publish-function` sobre `ork3d-redirect-www`. No aparece `update-distribution`.
- [ ] El log no contiene ningún account ID real (solo el placeholder `<ACCOUNT_ID>`, o tu bucket local si lo definiste).
- [ ] Bloques 1 a 6 de la sección 6 completos, con las salidas esperadas.

---

## 9. Ejecución real (F)

**Ya se ejecutó en producción**, con una versión anterior a los cambios de la sección 17. **No volver a correrlo sin autorización explícita.** Esta sección queda como referencia para reconstruir la infraestructura en otra cuenta.

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
| 3 | `▶ Chequeos previos (solo lectura)` con cuatro líneas `OK`: bucket del sitio, alias `ork3d.com`, función en LIVE y soporte de `--invoked-via-function-url` en la CLI. | Cualquier `ERROR` corta sin cambios. Corregir la causa y volver a empezar. |
| 4 | El aviso de qué se va a crear, y el prompt: `Escribí CREAR para continuar`. | Escribir `CREAR` (exacto, en mayúsculas). Cualquier otra cosa cancela: `Cancelado. No se modificó AWS.` |
| 5 | `▶ Bucket privado` con `$ aws s3api create-bucket` (solo la primera vez) y seis `$ aws s3api put-…`. Sin errores. | `BucketAlreadyExists`: el nombre lo tiene otra cuenta. Cambiá `RESENAS_BUCKET` en `infra/.env` y empezá de nuevo. |
| 6 | `▶ Rol ork3d-resenas-lambda`, después `esperando la propagación de IAM…` (10 s). | `The role defined for the function cannot be assumed`: esperá un minuto y relanzá. |
| 7 | `▶ Log group /aws/lambda/ork3d-resenas (retención 30 días)`: `create-log-group` (solo la primera vez) y `put-retention-policy`. Ocurre **antes** que la Lambda. | Cualquier error: parar. No seguir sin este paso. |
| 8 | `▶ Lambda ork3d-resenas`: `create-function` (primera vez) y `wait function-active-v2` (30 s a 1 min). | — |
| 9 | `▶ Concurrencia reservada (opcional)`: `put-function-concurrency`, o `Reserved concurrency omitida: límite de cuenta insuficiente.` | La omisión es válida (sección 3.2). Cualquier otro error: parar. |
| 10 | `▶ Function URL`: `create-function-url-config` (primera vez) y dos `add-permission`, o `ya existe` para cada permiso. | Un `add-permission` con error: no reintentar a ciegas. Revisar con `get-policy` (sección 10). |
| 11 | `▶ CloudFront Function ork3d-redirect-www`: `update-function` y `publish-function`, sin salida. | Si falla `publish-function`: revisar el ETag. No reintentar en bucle. |
| 12 | `▶ Verificación de la API`: `GET /tokens/<falso> → HTTP 200 {"ok": false, "motivo": "invalido"}` y `OPTIONS /resenas → HTTP 204` con `access-control-allow-origin: https://ork3d.com`. | `HTTP 403` o `Forbidden`: falta un permiso de la Function URL (sección 11). Cualquier otra respuesta: parar. |
| 13 | `▶ Listo. Próximos pasos` con la línea `Function URL: https://…`. | — |

Comando manual de concurrencia. Solo si la cuenta ya tiene margen (al menos 15 sin reservar) o después de aumentar el límite en Service Quotas:

```
aws lambda put-function-concurrency --function-name ork3d-resenas \
  --reserved-concurrent-executions 5 --profile "$AWS_PROFILE"
```

Si `put-retention-policy` falla con `ResourceNotFoundException`, el log group no existe. No debería pasar: el script lo crea
antes de la Lambda. Si pasa, crearlo y fijar la retención a mano:

```
aws logs create-log-group --log-group-name /aws/lambda/ork3d-resenas --profile "$AWS_PROFILE"
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
#   esperado: 4 sentencias (LeerEstadoReseniasYFotos, EscribirClaimsResenasYFirmarFotos, LiberarClaimSiFallaLaEscritura,
#   ListarBucketParaDistinguirAusentes)
#   ningún Resource "*"; ningún s3:* ; ningún PutObject sobre tokens/; ListBucket solo sobre el ARN del bucket, sin prefijo
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
#   esperado: {} si la concurrencia se omitió (sección 3.2); {"ReservedConcurrentExecutions": 5} si se configuró.
aws lambda get-function-url-config --function-name ork3d-resenas --profile "$AWS_PROFILE" --output json
#   esperado: AuthType NONE, y sin bloque Cors (o Cors vacío)
aws lambda get-policy --function-name ork3d-resenas --profile "$AWS_PROFILE" --query Policy --output text \
  | jq '.Statement[] | {Sid, Action, Principal, Condition}'
#   esperado: dos sentencias:
#     FunctionURLAllowPublicAccess: lambda:InvokeFunctionUrl, condición FunctionUrlAuthType NONE
#     FunctionURLAllowInvokeViaUrl: lambda:InvokeFunction, condición lambda:InvokedViaFunctionUrl = true
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

La reescritura de `https://ork3d.com/opinar/<token>` quedó verificada en el smoke productivo (sección 14.1, filas 2 y 3).

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

**`config.js` ya usa esta URL** desde el commit `768eaa0`, sin barra final. Fue un commit aparte, como estaba previsto,
y se desplegó con el run `39659217831` (sección 14).

Verificación con la URL (lectura: solo consulta el token falso, no crea nada):

```
API="https://<id>.lambda-url.us-east-1.on.aws"
curl -s -w '\nHTTP %{http_code}\n' "$API/tokens/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
#   esperado: {"ok": false, "motivo": "invalido"} y HTTP 200
curl -s -o /dev/null -D - -X OPTIONS -H "Origin: https://ork3d.com" \
  -H "Access-Control-Request-Method: POST" "$API/resenas" | grep -i -E '^(HTTP|access-control)'
#   esperado: HTTP/2 204 (o HTTP/1.1 204), access-control-allow-origin: https://ork3d.com,
#             access-control-allow-methods: GET,POST,OPTIONS
```

Validado en la ejecución real (sección 17.2): el token falso responde `HTTP 200` con `invalido`, y el preflight responde
`204` con `access-control-allow-origin`.

Si el GET devuelve `HTTP 403` o `{"Message":"Forbidden"}`, falta uno de los dos permisos de invocación. Revisá el
`get-policy` de la sección 10. Solo si falta alguno, agregarlo a mano (sin tocar los existentes):

```
# Permiso 1: invocar la URL con AuthType NONE
aws lambda add-permission --function-name ork3d-resenas --statement-id FunctionURLAllowPublicAccess \
  --action lambda:InvokeFunctionUrl --principal "*" --function-url-auth-type NONE --profile "$AWS_PROFILE"

# Permiso 2: invocación que entra por la Function URL (sin este, 403)
aws lambda add-permission --function-name ork3d-resenas --statement-id FunctionURLAllowInvokeViaUrl \
  --action lambda:InvokeFunction --principal "*" --invoked-via-function-url --profile "$AWS_PROFILE"
```

Los dos usan principal `*` porque la URL es pública (AuthType NONE). El segundo solo aplica a invocaciones que entran
por la URL (`lambda:InvokedViaFunctionUrl = true`). La seguridad de escritura la da la API: token válido de un solo uso,
validación del contenido y límites. **No agregar ningún permiso de invocación sin esa condición**, sin autorización explícita.

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

## 13. Atomicidad con If-None-Match (I): validada en S3 real (13.1). 13.2 pendiente

Objetivo: confirmar que dos escrituras concurrentes sobre el mismo claim dan **un solo éxito** (a nivel S3, 13.1), y que
dos envíos concurrentes por API crean **una sola reseña** (a nivel HTTP, 13.2). **13.1 quedó validada en S3 real**: dos escrituras simultáneas con `If-None-Match: *` sobre la misma clave dieron exactamente una escritura exitosa y una `PreconditionFailed`. **13.2 no se ejecutó**: dos submits HTTP simultáneos contra `/resenas` crean datos reales en producción y requieren autorización explícita. El consumo del token (`motivo=usado` tras el envío) se verificó en el smoke productivo (sección 14.1).

### Requisitos

- Function URL conocida (sección 11).
- `admin/.env` con los valores reales: `RESENAS_BUCKET`, `SITE_BUCKET=ork3d.com`, `CF_DISTRIBUTION_ID`, `SITE_URL=https://ork3d.com`.
- Autorización explícita para crear datos de prueba (token `SMOKE-TEST-ORK3D`).

### 13.1 Primitiva S3 (sin reseñas): dos `put-object` con `--if-none-match`

**Resultado real (S3 de producción):** dos escrituras simultáneas con `--if-none-match '*'` sobre la misma clave produjeron
exactamente una escritura exitosa y una `PreconditionFailed`. Esta es la primitiva de exclusión atómica validada contra AWS real.
No se ejecutaron dos submits HTTP simultáneos contra `/resenas` (13.2). Ver sección 17.2.

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

### 13.2 Prueba end-to-end con la API: un token, dos envíos simultáneos (pendiente de autorización)

1. Crear el token de prueba con la CLI real (crea datos en producción):

```
cd "$REPO"
admin/resenas.sh token crear --pedido SMOKE-TEST-ORK3D --producto smoke-test --categoria test \
  --producto-label "Prueba técnica ORK3D" --dias 1
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

## 14. Smoke test de cliente y moderación (COMPLETADO)

Resultados de la ejecución y verificación operativa realizada desde el entorno EC2 de producción, después del deploy de `768eaa0` (run `39659217831`, #27: success). No es una reverificación desde otro entorno. Pedido de prueba `SMOKE-TEST-ORK3D`, producto "Prueba técnica ORK3D", categoría `test`, con datos de prueba. No se documentan el token, su hash, la Function URL ni el ID de la reseña.

### 14.1 Resultados

| # | Verificación | Resultado |
| --- | --- | --- |
| 1 | Token de prueba creado con la CLI | ✔ |
| 2 | `GET` del token válido | ✔ `ok=true`, producto correcto, `categoria=test` |
| 3 | `/opinar/<token>` | ✔ HTTP 200, `noindex`, `no-referrer` |
| 4 | Envío desde navegador real | ✔ rating 5; aspectos `calidad` y `resultado`; comentario técnico; feedback privado; permiso sí; contacto no; foto JPEG de prueba |
| 5 | Foto subida con presign | ✔ subida firmada correcta |
| 6 | `POST /resenas` | ✔ HTTP 200 |
| 7 | Pantalla de gracias | ✔ apareció, sin errores de consola |
| 8 | Estado del token después del envío | ✔ `GET /tokens/<token>` devuelve `motivo=usado`. No se ejecutó un segundo `POST /resenas`: ni HTTP 409 ni el texto "Ya recibimos tu opinión" fueron verificados |
| 9 | Reseña en `pending` | ✔ exactamente una. Rating 5, categoría `test`, `product_label` correcto, comentario y aspectos correctos, `display_name` generado, `private_feedback` solo interno, `permission_to_publish=true`, foto privada válida. El ID (32 caracteres) no se documenta |
| 10 | Intentos fallidos con foto | ✔ dos objetos temporales en `pendientes/`, identificados por key exacta y eliminados sin tocar la foto asociada (sección 17.4, hallazgo A) |
| 11 | Aprobación temporal | ✔ `approved`, `published_at` definido, `photo_public_key` creada, export regenerado, invalidación de CloudFront creada |
| 12 | Export público (`resenas/publicas.json`) | ✔ items con la allowlist exacta (abajo) y sin campos privados |
| 13 | Foto pública | ✔ HTTP 200, `Content-Type: image/jpeg`, tamaño correcto, `Cache-Control: public,max-age=31536000,immutable`, SHA-256 igual al de la foto privada, sin EXIF, XMP, IPTC ni GPS detectables |
| 14 | Limpieza | ✔ reseña, foto privada y foto pública eliminadas; export regenerado; 0 `pending`. Estado de consumo del token preservado: una consulta posterior devolvió `ok=false`, `motivo=usado` (sección 15.5) |
| 15 | Verificación final | ✔ foto pública del smoke: HTTP 404. Export: `count=0`, `items=[]`. `/opiniones`: `noindex, follow`. Sitemap: 0 referencias a opiniones. Home: 0 referencias a opiniones |
| 16 | `/opiniones` sin lanzar | ✔ accesible por URL directa, con noindex, fuera del sitemap, sin enlaces y sin reseñas ficticias |
| 17 | Recursos AWS | ✔ no se crearon recursos nuevos durante el smoke final |

**Allowlist del item público** (`resenas/publicas.json`): `id`, `rating`, `comment`, `aspects`, `display_name`, `category`, `product_label`, `verified`, `photo_url`, `published_at`. Confirmado que **no** expone `token`, `token_hash`, `order_ref`, `private_feedback`, `contact_requested`, `contact`, la clave privada de la foto, datos AWS ni datos internos.

### 14.2 Procedimiento (referencia)

Pasos y salidas esperadas:

1. **Home y páginas**: `https://ork3d.com` sin cambios: `curl -s https://ork3d.com/ | grep -c -i -E 'opiniones|reseña|resenas'` → `0`.
2. **Token B**: `admin/resenas.sh token crear --pedido SMOKE-TEST-ORK3D --producto smoke-test --categoria test --producto-label "Prueba técnica ORK3D" --dias 1`. Abrí el link en escritorio y en celular.
3. **Página** `https://ork3d.com/opinar/<TOKEN_B>`: carga el formulario con el producto "Prueba técnica ORK3D". Sin scripts de terceros, sin fuentes externas. `noindex` y `referrer no-referrer` en el HTML.
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
10. **Segundo envío**: reenviá el mismo link. Esperado: pantalla "Ya recibimos tu opinión" y ninguna reseña nueva. *En el smoke productivo este paso no se verificó: solo se verificó `motivo=usado` por GET (ver 14.1, fila 8).*
11. **Limpieza**: sección 15.5.

---

## 15. Rollback (J)

### 15.1 Inventario: qué se crea y qué se modifica

**Recursos nuevos** (se pueden borrar, solo si los creó este runbook):

- Bucket S3 privado `$RESENAS_BUCKET` (con sus versiones y marcas de borrado).
- Rol IAM `ork3d-resenas-lambda`, con su política inline `resenas-bucket-privado` y la política administrada adjunta.
- Lambda `ork3d-resenas`, con su Function URL y sus dos permisos (`FunctionURLAllowPublicAccess` y `FunctionURLAllowInvokeViaUrl`). La concurrencia reservada existe solo si se configuró (sección 3.2).
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

**Qué quedó en el smoke productivo:** la reseña y sus fotos fueron eliminadas, mientras que el estado de consumo del token se preservó; una consulta posterior continuó devolviendo `motivo=usado`. Además se borraron los objetos temporales de intentos fallidos (sección 17.4), y el export se regeneró con `count=0`. No se inspeccionó a nivel S3 si el objeto de `tokens/` sigue existiendo o si el estado `usado` se resuelve solo mediante `claims/`: ese punto no está verificado.

Los logs de CloudWatch quedan 30 días: contienen IDs y prefijos de hash, pero no tokens completos ni texto de reseñas.

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
- No volver a ejecutar `infra/resenas-setup.sh` con `DRY_RUN=0` sin autorización explícita: la infraestructura ya existe.
- No access keys en archivos, en comandos ni en el repo.
- No pegar account IDs, distribution IDs ni URLs reales en el repo ni en issues públicos.
- No cambiar `config.js` con una URL ficticia. La Function URL real ya está en `config.js` desde el commit `768eaa0`.
- No enlazar `/opiniones`, ni agregarla al sitemap, ni tocar la home en este despliegue.
- No crear reseñas de prueba visibles públicamente. Si una prueba aprueba una reseña, la limpieza (15.5) es obligatoria.

---

## 17. Hallazgos del despliegue real

Estos puntos salieron al crear la infraestructura en AWS desde la máquina del operador. Cada uno quedó corregido en el
script, en las políticas, en los tests o en este runbook. Los hallazgos del smoke productivo están en la sección 17.4.

### 17.1 Hallazgos y correcciones

| # | Hallazgo | Causa | Corrección en el repo | Dónde se verifica |
| --- | --- | --- | --- | --- |
| 1 | La Function URL respondía 403. | Solo existía `lambda:InvokeFunctionUrl`. Con AuthType NONE hace falta también `lambda:InvokeFunction` con `lambda:InvokedViaFunctionUrl = true`. | El script crea los dos permisos y los detecta por contenido de la política: es idempotente. | `infra/tests`: `test_script_declara_los_dos_permisos_de_function_url`, `test_modo_real_crea_los_dos_permisos_de_function_url`. Sección 11. |
| 2 | Un token inexistente respondía 500 en lugar de `invalido`. | Sin `s3:ListBucket`, S3 responde `AccessDenied` a claves que no existen, en lugar de `NotFound`. La API lo trataba como error interno. | El rol suma `s3:ListBucket` sobre el ARN del bucket, sin prefijo. Un `AccessDenied` real sigue siendo 500. | `infra/tests`: `test_lambda_permisos_incluye_listbucket_solo_sobre_el_bucket_de_resenas`. `api/resenas`: `test_contrato_token_inexistente_responde_200_invalido`, `test_error_de_permiso_en_s3_no_se_disfraza_de_token_invalido`. |
| 3 | `put-function-concurrency` con 5 fallaba. | La cuenta tenía 10 sin reservar. AWS exige que queden al menos 10 después de reservar. | Concurrencia opcional: se reserva solo con margen (sección 3.2). Si no hay margen, se informa y se sigue. | `infra/tests`: `test_concurrencia_sin_margen_no_aborta_ni_toca_la_funcion`, `test_concurrencia_con_margen_la_configura_solo_para_la_funcion`. |
| 4 | `put-retention-policy` fallaba con `ResourceNotFoundException`. | Lambda crea el log group en su primera invocación. La retención se fijaba después de la verificación, cuando el grupo todavía no existía. | El log group se crea **antes** de la Lambda, con retención de 30 días. | `infra/tests`: `test_log_group_se_crea_antes_de_la_lambda_con_retencion_30`, `test_log_group_existente_no_se_recrea`. |

### 17.2 Comportamiento validado en AWS real

| Prueba | Resultado |
| --- | --- |
| `GET /tokens/<token inexistente>` (sección 11) | `HTTP 200` con `{"ok": false, "motivo": "invalido"}`. No es 404 ni 500. |
| `OPTIONS /resenas` con `Origin: https://ork3d.com` | `HTTP 204` con `access-control-allow-origin: https://ork3d.com`. CORS lo responde la Lambda: la Function URL no tiene CORS propio. |
| Dos escrituras simultáneas con `--if-none-match '*'` sobre la misma clave (13.1) | Una escritura exitosa y una `PreconditionFailed`. Exclusión atómica confirmada en S3 real. No son dos submits HTTP contra `/resenas`. |

### 17.3 Pendiente

- **13.2**: dos submits HTTP simultáneos contra `/resenas`. No se ejecutaron: crean datos de producción y requieren autorización. La exclusión atómica en S3 sí está validada (13.1).
- **Sección 12 (no bloqueante)**: si `ork3d.com` pasa por el proxy de Cloudflare, no está verificado. La arquitectura productiva validada usa S3 + CloudFront.
- **Safari/iOS**: confirmar que el canvas no agrega metadatos.
- **Límite por IP**: no implementado (`docs/resenas/README.md`, pendientes).
- **Escala del export (consideración futura, no bloqueante)**: relee todas las reseñas. Con el volumen inicial no es un problema; conviene revisarla antes de ~1.000 reseñas.
- **Script de infraestructura en macOS y bash 3.2**: no probado.

### 17.4 Hallazgos del smoke productivo

**A. Objetos temporales en `pendientes/` tras un intento fallido.** Un envío que falla después de subir la foto puede dejar
un objeto temporal en `pendientes/`. El lifecycle de `pendientes/` lo elimina a los 2 días (sección 10, S3). En pruebas
manuales se puede limpiar por **key exacta** con el helper de la sección 15.4, nunca por prefijo. En el smoke se borraron
dos objetos de este tipo sin tocar la foto asociada al envío exitoso.

**B. Selección de estrellas en tests automatizados.** Las estrellas son radios superpuestos por el estilo. Para seleccionar
una en un test, hacer click sobre el `label` asociado al rating (`label[for="rating-N"]`) en lugar de llamar `check()` sobre
el radio. Esto solo cambia la automatización: el diseño y el comportamiento no cambian.

---

## 18. V2 · panel admin (implementada localmente; no desplegada)

**Estado: V2 admin implementada y validada localmente. AWS y producción todavía no modificados.**
Esta sección es un plan. Ningún comando de esta sección se ejecutó contra AWS. El contrato está en
`docs/resenas/V2-ADMIN.md`.

Qué agrega la V2: una Lambda admin `ork3d-resenas-admin` (Python 3.12, Function URL con `AWS_IAM`, detrás
de CloudFront por OAC en `/admin/api/*`), las páginas `/admin/resenas` y `/admin/entrar`, el envío de un
aviso por SES cuando entra una reseña pending (detrás de `RESENAS_NOTIFY_ENABLED=false`), una regla de
2 días para `admin/`, y el paquete `nucleo/` que la Lambda pública también necesita.

### 18.1 Antes de cualquier `DRY_RUN=0` (todo pendiente)

| # | Punto | Por qué bloquea | Cómo se verifica |
| --- | --- | --- | --- |
| 1 | 2FA en el buzón de `RESENAS_ADMIN_EMAIL` | El acceso al panel depende de ese buzón | Confirmar en la cuenta de correo del moderador |
| 2 | Smoke de `If-Match` en la cuenta (gate) | S3 soporta `If-Match` por ETag. El smoke confirma los códigos reales de esta cuenta antes de producción | Especificado en §18.5.1. No se ejecuta en esta etapa |
| 3 | OAC con Function URL `AWS_IAM`: dos permisos, reenvío y hash | Sin esto `/admin/api/*` no llega a la Lambda admin. Los dos permisos (`InvokeFunctionUrl` e `InvokeFunction`) son el requisito de la revisión. La confirmación en la página primaria no fue posible desde el sandbox | Verificar la guía de CloudFront y la cuenta según §18.5.2 (política de la función, POST con hash, cookie). Si no es viable, parar y reportar: no cambiar el diseño en silencio |
| 4 | Identidad SES de `RESENAS_FROM_EMAIL` | La política de `ses:SendEmail` usa `identity/<correo>`; si se verifica un dominio, cambia el ARN | Decisión explícita. Salir del sandbox de SES si hace falta |
| 5 | DKIM y DMARC | Sin ellos el aviso puede caer en spam | Revisar la configuración DNS antes de activar el aviso. No se toca DNS en esta etapa |

### 18.2 Dry-run (sin AWS)

```bash
# Desde la raíz del repo. Con DRY_RUN=1 (valor por defecto) el script solo imprime lo que correría.
DRY_RUN=1 infra/admin-setup.sh
python3 -m unittest discover -s infra/tests -v      # 51 casos: 18 de V1, 33 de V2
python3 dev/validar.py                               # 138 comprobaciones
```

El script no llama a `aws`, `curl` ni `wget` en dry-run. El test de infra lo comprueba con binarios falsos que
registran cada llamada.

### 18.3 Orden propuesto (NO ejecutado)

Cada paso queda en el script como `PLAN` o como `dry-run`. Los pasos marcados PLAN necesitan una autorización
explícita, y su sintaxis exacta se verifica en la cuenta antes del deploy.

| Paso | Recurso | Comando de referencia | Estado |
| --- | --- | --- | --- |
| 1 | Rol y política de la Lambda admin | `aws iam create-role` y `put-role-policy` con `infra/resenas/admin-lambda-permisos.json` | dry-run |
| 2 | Log group `/aws/lambda/ork3d-resenas-admin` (30 días) | `aws logs create-log-group` y `put-retention-policy` | dry-run |
| 3 | Lambda `ork3d-resenas-admin` (python3.12, 256 MB, 10 s) | `aws lambda create-function` con el zip de `admin_app.py` y `nucleo/` | dry-run |
| 4 | Function URL con `AWS_IAM` | `aws lambda create-function-url-config --auth-type AWS_IAM` | PLAN |
| 5 | OAC y dos permisos de invocación para CloudFront | `aws cloudfront create-origin-access-control`, y dos `aws lambda add-permission`: `AllowCloudFrontOACInvokeFunctionUrl` (`lambda:InvokeFunctionUrl`) y `AllowCloudFrontOACInvokeFunction` (`lambda:InvokeFunction`), ambos con principal `cloudfront.amazonaws.com` y `SourceArn` de la distribución | PLAN |
| 6 | Política de origen propia y behavior `/admin/api/*` | `create-origin-request-policy` con `infra/resenas/admin-origin-request-policy.json`; luego `get-distribution-config` (guardar copia y ETag) y `update-distribution` con `infra/resenas/admin-cloudfront-behavior.json`, con el Id de la política en `__ADMIN_ORP_ID__` | PLAN |
| 7 | Lifecycle de `admin/` (2 días) | `put-bucket-lifecycle-configuration` con `infra/resenas/bucket-lifecycle.json`. **Reemplaza toda la configuración**: el archivo incluye las reglas de V1 | PLAN |
| 8 | Función `ork3d-redirect-www` | `update-function` y `publish-function` con `infra/redirect-www.js` (reglas `/admin/resenas` y `/admin/entrar`) | PLAN |
| 9 | Lambda pública: política y código | `put-role-policy` con `infra/resenas/lambda-permisos.json` (agrega SES y `notificaciones/*`) y `update-function-code` con `app.py` y `nucleo/` | PLAN |
| 10 | Frontend admin | `deploy.yml` (agrega `admin-web/**`) | PLAN |
| 11 | Activar el aviso | `update-function-configuration` con `RESENAS_NOTIFY_ENABLED=true`, solo después de la identidad SES verificada. Procedimiento y comprobaciones en `V2-ADMIN.md` §19 (`infra/notify-activacion.sh`) | PREPARADO, no activado |

Antes de cada paso PLAN: copia de la configuración actual, en un archivo fuera del repo.

### 18.4 Verificación después de cada paso

- Paso 3: `get-function-configuration`: Runtime python3.12, Handler `admin_app.handler`, Memory 256, Timeout 10,
  y solo las variables sin secretos (`RESENAS_BUCKET`, `RESENAS_SITE_BUCKET`, `RESENAS_DISTRIBUTION_ID`,
  `RESENAS_ADMIN_EMAIL`, `RESENAS_FROM_EMAIL`, `RESENAS_ADMIN_BASE_URL`, `ALLOWED_ORIGIN`).
- Paso 5: `aws lambda get-policy --function-name ork3d-resenas-admin` muestra exactamente dos sentencias, con principal
  `cloudfront.amazonaws.com` y `SourceArn` de la distribución: `lambda:InvokeFunctionUrl` e `lambda:InvokeFunction`.
- Paso 6: la política de origen tiene las cabeceras, la cookie y la query de `infra/resenas/admin-origin-request-policy.json`, sin `Host`.
  La behavior `/admin/api/*` apunta al origen admin, no cachea (`CachingDisabled`) y usa esa política.
- Paso 7: `get-bucket-lifecycle-configuration` muestra las tres reglas (pendientes, admin y versiones).
- Paso 8: `GET /admin/entrar` sirve `admin-web/entrar.html`. `GET /admin/api/sesion` no se reescribe.

### 18.5 Smoke de producción (plan)

Ejecutar solo después de la autorización de 18.3. Todo dato de prueba se identifica con `order_ref` que empieza con `SMOKE-V2`.

1. Sin sesión: `GET /admin/resenas` responde 200 con `noindex, nofollow`. `GET /admin/api/sesion` responde 401.
   `POST /admin/api/sesion/solicitar` sin `Origin` responde 400 y no envía correo (V2-ADMIN.md §16.8).
2. Acceso: `POST /admin/api/sesion/solicitar` con las cabeceras CSRF. Llega **un** correo al buzón de moderación,
   con el enlace `…/admin/entrar#c=…`. Abrir el enlace sin confirmar: no consume el código. Confirmar: 200 y
   cookie `ork3d_admin` con `HttpOnly; Secure; SameSite=Lax; Path=/admin`. Reusar el enlace: 401.
3. Invitación: crear una con `order_ref` `SMOKE-V2-…`. El enlace `…/opinar/<token>` abre el formulario y
   `GET /tokens/<token>` responde `ok: true`.
4. Reseña real de prueba: enviarla. Llega el aviso (`Nueva reseña ORK3D · ★…`) con el enlace al panel. La
   reseña aparece en la bandeja como pending, con `aviso: enviado`.
5. Moderación: aprobar, ver el export (`resenas/publicas.json`) con el item y sin campos privados. Ocultar y
   restaurar. Comprobar que la CLI `admin/resenas.sh pendientes` y `ver` siguen funcionando.
6. Concurrencia: dos `put-object --if-match` sobre la misma reseña (requisito 2 de 18.1).
7. Logs: buscar en CloudWatch el token de la invitación, un código de acceso y un correo: no deben aparecer.
8. Limpieza: borrar las reseñas y tokens `SMOKE-V2` con el procedimiento de la sección 15.5. Revocar la sesión
   con `POST /admin/api/sesion/cerrar`.

### 18.5.1 Gate: smoke de `If-Match` en S3 (especificado, NO ejecutar en esta etapa)

S3 soporta `If-Match` con el ETag en `PutObject`. Este smoke confirma los códigos reales de la cuenta antes de
producción. Usa el bucket privado (`RESENAS_BUCKET`) y una clave de prueba que empieza con `admin/`, así que la regla
de dos días la limpia si algo queda a medias.

| Paso | Acción | Resultado esperado |
|---|---|---|
| 1 | Escritura inicial: `aws s3api put-object` de `{"v":1,"escritor":"inicial"}` a `admin/smoke/if-match-<uuid>.json` | HTTP 200 |
| 2 | Obtener el ETag: `aws s3api head-object --query ETag --output text` | ETag `E1` |
| 3 | Escritura condicionada con el ETag vigente: `put-object --if-match "E1"` | HTTP 200; el ETag cambia a `E2` |
| 4 | Dos escritores con el mismo `E2`, en paralelo, con cuerpos distintos (`w1.json`, `w2.json`) | Exactamente uno gana (200). El otro recibe `412 PreconditionFailed` o `409 ConditionalRequestConflict`, según la carrera. Registrar el código real |
| 5 | `get-object` de la clave | Contenido del ganador. El perdedor no sobrescribió |
| 6 | Limpieza exacta: `aws s3api delete-object` solo de esa clave, y luego `head-object` | `delete-object` sin error; `head-object` devuelve 404 |

Criterio de paso: los pasos 1 a 3 dan 200; el paso 4 da exactamente un 200 y un 412 o 409; el paso 5 coincide con
el ganador; el paso 6 deja la clave inexistente. Antes de correrlo, confirmar que la AWS CLI instalada soporta
`--if-match` en `put-object` (`aws s3api put-object help`). Si el paso 4 devuelve 404 en lugar de 409 (carrera
con borrado), `Almacen` ya lo trata como conflicto. Ajustar la documentación, no el código.

### 18.5.2 Gate: OAC, hash y cookie (especificado, NO ejecutar en esta etapa)

1. Verificar en la documentación primaria de AWS (CloudFront, Function URL con OAC) que las dos sentencias
   coinciden con `infra/resenas/admin-function-url-permisos.json`. Si la documentación pide otra cosa, parar.
2. Tras el paso 5: `aws lambda get-policy --function-name ork3d-resenas-admin` muestra las dos sentencias y ninguna otra.
3. Un POST al panel por la distribución, con el hash correcto: el API responde según su estado (200, 202, 401 o 409).
   El mismo POST sin `x-amz-content-sha256` y con un hash cambiado: el rechazo debe llegar antes de la Lambda.
   Registrar el código real de cada caso.
4. Con sesión abierta, `GET /admin/api/sesion` devuelve 200. Eso confirma que la cookie `ork3d_admin` llega a la Lambda.
5. `/admin/api/*` no cachea: las respuestas no traen indicios de caché (`X-Cache` distinto de `Hit from cloudfront`).

Si alguno de los puntos no se cumple: parar y reportar. No cambiar el diseño en silencio.

### 18.6 Rollback (por paso, en orden inverso)

| Paso revertido | Cómo volver atrás | Efecto sobre los datos |
| --- | --- | --- |
| 11. Aviso | `update-function-configuration` con `RESENAS_NOTIFY_ENABLED=false` | Ninguno. Las reseñas siguen guardadas |
| 10. Frontend | Redeploy del commit anterior de `admin-web/` (o borrar los objetos `admin-web/*` del bucket del sitio) | Ninguno |
| 9. Lambda pública | `update-function-code` con el zip anterior (sin `nucleo/`) y `put-role-policy` con la política de V1 | Ninguno |
| 8. Redirección | `publish-function` con la versión de la copia guardada en 18.3 | Ninguno |
| 7. Lifecycle | `put-bucket-lifecycle-configuration` con la copia guardada (la de V1) | Las sesiones y locks de `admin/` ya no vencen en 2 días |
| 6. Behavior | `update-distribution` con la copia guardada y su ETag | Ninguno |
| 6b. Política de origen | `delete-origin-request-policy` cuando la distribución ya no la usa | Ninguno |
| 5. OAC y permisos | `remove-permission` de `AllowCloudFrontOACInvokeFunctionUrl` y de `AllowCloudFrontOACInvokeFunction`, y después `delete-origin-access-control` | Ninguno |
| 4. Function URL | `delete-function-url-config` | Ninguno |
| 1 a 3. Rol, log group, Lambda | Borrar la Lambda admin y su rol (los datos siguen en el bucket) | Ninguno. Las sesiones abiertas dejan de servir |

Ningún rollback borra reseñas, tokens ni fotos. El panel no tiene estado propio fuera del bucket privado.

### 18.7 Costo

- Costo fijo: 0 USD. No hay recursos con cargo fijo: no hay servidores, bases de datos ni NAT.
- Costo variable: invocaciones y tiempo de la Lambda (capa gratuita de Lambda), solicitudes de S3, solicitudes de
  CloudFront, ingestión y retención de 30 días en CloudWatch Logs, y correos de SES (uno por reseña nueva y uno
  por cada acceso al panel). La tarifa vigente de SES debe confirmarse en la página de precios de AWS antes del deploy.
- Límites que frenan el gasto: 3 accesos al panel cada 15 minutos (global) y 8 KB por cuerpo.
