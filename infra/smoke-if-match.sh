#!/usr/bin/env bash
# Smoke de escrituras condicionales de S3 (If-Match). Es el ÚNICO script de infra que escribe en AWS.
#
# Requiere ALLOW_SMOKE_WRITE=1. Sin esa variable aborta antes de llamar a AWS (exit 3).
# Crea UNA key aleatoria bajo admin/smoke-if-match/<uuid>.json en el bucket privado configurado
# (RESENAS_BUCKET, o el bucket que tiene la Lambda pública si no se define). Nunca toca tokens/, claims/,
# pendientes/ ni resenas/. Al terminar borra TODAS las versiones y delete markers de esa key, también si
# una prueba falla. Registra resultados, códigos de error y conteo de ganadores. No imprime ETags.
#
# Variables: AWS_PROFILE (default: default), AWS_REGION (default: us-east-1), RESENAS_BUCKET (opcional),
#            RESENAS_FUNCTION (default: ork3d-resenas), SMOKE_OUT (default: /tmp/ork3d-v2-preflight).
# Códigos de salida: 0 OK · 1 prueba fallida · 2 sin bucket · 3 falta ALLOW_SMOKE_WRITE ·
#                    4 ambos writers ganaron (violación de If-Match) · 5 la limpieza no quedó completa.
set -uo pipefail

if [[ "${ALLOW_SMOKE_WRITE:-}" != "1" ]]; then
  echo "ABORTA: este smoke escribe en S3. Para ejecutarlo: ALLOW_SMOKE_WRITE=1 infra/smoke-if-match.sh" >&2
  exit 3
fi

AWS="${AWS_CLI:-aws}"
export AWS_PROFILE="${AWS_PROFILE:-default}" AWS_REGION="${AWS_REGION:-us-east-1}" AWS_PAGER=""
PREFIJO="admin/smoke-if-match/"
OUT="${SMOKE_OUT:-/tmp/ork3d-v2-preflight}"
FN="${RESENAS_FUNCTION:-ork3d-resenas}"
if ! mkdir -p "$OUT" || ! chmod 700 "$OUT"; then
  echo "ERROR: no se pudo preparar la carpeta de salida $OUT" >&2
  exit 1
fi
LOG="$OUT/smoke-if-match-$(date -u +%Y%m%dT%H%M%SZ).log"
T="$(mktemp -d)"
chmod 700 "$T"
ARMADO=0  # 1 antes del primer intento de crear la key: la limpieza busca sus versiones aunque ese intento falle
BUCKET=""
KEY=""

say() { echo "$*" | tee -a "$LOG"; }

# Ayudas de parseo: solo leen la salida de la CLI, nunca la imprimen.
cat > "$T/codigo.py" <<'PY'
import re, sys
texto = open(sys.argv[1], encoding="utf-8", errors="replace").read()
m = re.search(r"An error occurred \((\w+)\)", texto)
print(m.group(1) if m else "desconocido")
PY
cat > "$T/lista.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
clave = sys.argv[2]
for tipo in ("Versions", "DeleteMarkers"):
    for v in d.get(tipo) or []:
        if v.get("Key") == clave:
            print(tipo, v["VersionId"])
PY
cat > "$T/cuenta.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
clave = sys.argv[2]
print(sum(1 for tipo in ("Versions", "DeleteMarkers") for v in (d.get(tipo) or []) if v.get("Key") == clave))
PY
cat > "$T/writer.py" <<'PY'
import json, sys
print(json.load(open(sys.argv[1])).get("writer", "?"))
PY

limpiar() {
  if [[ "$ARMADO" != "1" ]]; then
    say "limpieza: nada que limpiar (todavía no se intentó crear la key)."
    return 0
  fi
  if ! "$AWS" s3api list-object-versions --bucket "$BUCKET" --prefix "$KEY" --output json > "$T/versiones.json" 2> "$T/lv.err"; then
    say "limpieza: no se pudieron listar las versiones de la key de prueba."
    return 1
  fi
  python3 "$T/lista.py" "$T/versiones.json" "$KEY" > "$T/lista.txt"
  local eliminadas=0 marcas=0 fallos=0 tipo vid
  while read -r tipo vid; do
    [[ -n "${vid:-}" ]] || continue
    if "$AWS" s3api delete-object --bucket "$BUCKET" --key "$KEY" --version-id "$vid" --output json > /dev/null 2> "$T/del.err"; then
      if [[ "$tipo" == "Versions" ]]; then eliminadas=$((eliminadas + 1)); else marcas=$((marcas + 1)); fi
    else
      fallos=$((fallos + 1))
    fi
  done < "$T/lista.txt"
  # Verificación: ninguna versión ni delete marker de esa key, y la key no se ve.
  "$AWS" s3api list-object-versions --bucket "$BUCKET" --prefix "$KEY" --output json > "$T/versiones2.json" 2> /dev/null || true
  local restantes existe=0
  restantes="$(python3 "$T/cuenta.py" "$T/versiones2.json" "$KEY" 2> /dev/null || echo 99)"
  if "$AWS" s3api head-object --bucket "$BUCKET" --key "$KEY" > /dev/null 2>&1; then existe=1; fi
  say "limpieza: versiones eliminadas=$eliminadas · delete markers eliminados=$marcas · errores de borrado=$fallos · restantes=$restantes · key visible=$existe"
  [[ "$fallos" == "0" && "$restantes" == "0" && "$existe" == "0" ]]
}

finalizar() {
  local rc=$?
  if ! limpiar; then
    say "ERROR: la limpieza no quedó completa. Revisar la key $KEY en el bucket."
    [[ $rc -eq 0 ]] && rc=5
  fi
  rm -rf "$T"
  exit "$rc"
}
trap finalizar EXIT

# Comprobación offline: la CLI instalada conoce el parámetro IfMatch de PutObject.
if ! "$AWS" s3api put-object --generate-cli-skeleton input 2>/dev/null | grep -q 'IfMatch'; then
  say "ERROR: la AWS CLI instalada no conoce el parámetro IfMatch de PutObject. Actualizá la CLI."
  exit 1
fi

if ! "$AWS" sts get-caller-identity --output json > /dev/null 2> "$T/id.err"; then
  say "ERROR: no se pudo autenticar con el perfil $AWS_PROFILE."
  exit 1
fi

BUCKET="${RESENAS_BUCKET:-}"
if [[ -z "$BUCKET" ]]; then
  BUCKET="$("$AWS" lambda get-function-configuration --function-name "$FN" \
    --query 'Environment.Variables.RESENAS_BUCKET' --output text 2>/dev/null || true)"
fi
if [[ -z "$BUCKET" || "$BUCKET" == "None" ]]; then
  say "ERROR: no hay bucket privado configurado (define RESENAS_BUCKET, o revisá la Lambda $FN)."
  exit 2
fi
say "Bucket privado: ${BUCKET:0:4}… (enmascarado). Prefijo de prueba: $PREFIJO"

UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
KEY="${PREFIJO}${UUID}.json"
case "$KEY" in
  tokens/*|claims/*|pendientes/*|resenas/*) say "ERROR: clave prohibida: $KEY"; exit 4 ;;
esac
say "Key de prueba: $KEY"

# Cuerpos exactos (sin salto de línea final).
printf '%s' '{"version":1}' > "$T/v1.json"
printf '%s' '{"version":2}' > "$T/v2.json"
printf '%s' '{"version":3}' > "$T/v3.json"
printf '%s' '{"writer":"A"}' > "$T/wa.json"
printf '%s' '{"writer":"B"}' > "$T/wb.json"

# 1. Crear. ARMADO va antes: si la escritura falla a mitad de camino, la limpieza igual busca versiones.
ARMADO=1
if "$AWS" s3api put-object --bucket "$BUCKET" --key "$KEY" --body "$T/v1.json" \
     --content-type application/json --output json > "$T/p1.json" 2> "$T/p1.err"; then
  say "1. crear {\"version\":1}: OK"
else
  say "1. crear: FALLO ($(python3 "$T/codigo.py" "$T/p1.err"))"
  exit 1
fi

# 2. ETag inicial (no se imprime).
if ! ETAG1="$("$AWS" s3api head-object --bucket "$BUCKET" --key "$KEY" --query ETag --output text 2> "$T/h1.err")"; then
  say "2. ETag inicial: FALLO ($(python3 "$T/codigo.py" "$T/h1.err"))"
  exit 1
fi
say "2. ETag inicial obtenido (no se imprime)"

# 3. Actualizar con el ETag correcto: debe funcionar.
if "$AWS" s3api put-object --bucket "$BUCKET" --key "$KEY" --body "$T/v2.json" \
     --content-type application/json --if-match "$ETAG1" --output json > "$T/p3.json" 2> "$T/p3.err"; then
  say "3. If-Match con el ETag vigente: OK (escritura aceptada)"
else
  say "3. If-Match con el ETag vigente: FALLO ($(python3 "$T/codigo.py" "$T/p3.err"))"
  exit 1
fi

# 4. Intentar con el ETag viejo: debe fallar con 412 (PreconditionFailed).
if "$AWS" s3api put-object --bucket "$BUCKET" --key "$KEY" --body "$T/v3.json" \
     --content-type application/json --if-match "$ETAG1" --output json > "$T/p4.json" 2> "$T/p4.err"; then
  say "4. ETag viejo: la escritura NO fue rechazada (inesperado): FALLO"
  exit 1
fi
C4="$(python3 "$T/codigo.py" "$T/p4.err")"
if [[ "$C4" == "PreconditionFailed" ]]; then
  say "4. ETag viejo: rechazado con PreconditionFailed (HTTP 412, derivado del código): OK"
else
  say "4. ETag viejo: código inesperado $C4: FALLO"
  exit 1
fi

# 5. ETag vigente (no se imprime).
if ! ETAG3="$("$AWS" s3api head-object --bucket "$BUCKET" --key "$KEY" --query ETag --output text 2> "$T/h3.err")"; then
  say "5. ETag vigente: FALLO ($(python3 "$T/codigo.py" "$T/h3.err"))"
  exit 1
fi
say "5. ETag vigente obtenido (no se imprime)"

# 6. Dos escritores simultáneos con exactamente ese ETag.
( "$AWS" s3api put-object --bucket "$BUCKET" --key "$KEY" --body "$T/wa.json" \
    --content-type application/json --if-match "$ETAG3" --output json > "$T/wa.out" 2> "$T/wa.err"; echo $? > "$T/wa.rc" ) &
( "$AWS" s3api put-object --bucket "$BUCKET" --key "$KEY" --body "$T/wb.json" \
    --content-type application/json --if-match "$ETAG3" --output json > "$T/wb.out" 2> "$T/wb.err"; echo $? > "$T/wb.rc" ) &
wait
RA="$(cat "$T/wa.rc" 2> /dev/null || echo 99)"
RB="$(cat "$T/wb.rc" 2> /dev/null || echo 99)"

# 7. Conteo de ganadores: exactamente uno.
GANADORES=0
[[ "$RA" == "0" ]] && GANADORES=$((GANADORES + 1))
[[ "$RB" == "0" ]] && GANADORES=$((GANADORES + 1))
if [[ $GANADORES -eq 2 ]]; then
  say "6-7. ambos writers ganaron con el mismo ETag: violación de If-Match. ABORTA."
  exit 4
fi
if [[ $GANADORES -ne 1 ]]; then
  say "6-7. ganadores=$GANADORES (se esperaba exactamente 1): FALLO"
  exit 1
fi
if [[ "$RA" == "0" ]]; then
  GANADOR=A
  PERDEDOR_ERR="$T/wb.err"
else
  GANADOR=B
  PERDEDOR_ERR="$T/wa.err"
fi

# 8. El perdedor debe fallar con 412 o 409.
C8="$(python3 "$T/codigo.py" "$PERDEDOR_ERR")"
case "$C8" in
  PreconditionFailed) HTTP=412 ;;
  ConditionalRequestConflict) HTTP=409 ;;
  *) say "8. el perdedor falló con código inesperado $C8: FALLO"; exit 1 ;;
esac
say "6-8. concurrencia: ganadores=1 · perdedores=1 · código del perdedor=$C8 (HTTP $HTTP, derivado del código)"

# 9. Leer solo ese objeto y confirmar el ganador.
if ! "$AWS" s3api get-object --bucket "$BUCKET" --key "$KEY" "$T/final.json" --output json > "$T/g.json" 2> "$T/g.err"; then
  say "9. lectura del objeto final: FALLO ($(python3 "$T/codigo.py" "$T/g.err"))"
  exit 1
fi
VISTO="$(python3 "$T/writer.py" "$T/final.json")"
if [[ "$VISTO" == "$GANADOR" ]]; then
  say "9. objeto final escrito por el writer $VISTO, coincide con el ganador: OK"
else
  say "9. objeto final: writer=$VISTO, se esperaba $GANADOR: FALLO"
  exit 1
fi
say "Pruebas 1 a 9: OK. La limpieza (10 y 11) se verifica al terminar."
