#!/usr/bin/env bash
# ORK3D · panel admin: constantes y guardas compartidas por infra/admin-ui-publicar.sh e infra/admin-ui-verify.sh.
# Se carga con "source". No se ejecuta sola. No escribe nada.
#
# Guarda del repo (verificar_repo): el contenido de los estáticos del manifiesto tiene que ser el de REVISION_REVISADA.
# No se exige HEAD == REVISION_REVISADA a secas: este mismo tooling vive en un commit posterior. Lo que importa es que
# el contenido desplegado sea el revisado (mismo blob) y que HEAD esté igual a origin.

REVISION_REVISADA="590ed7df180202390fda8a523b712b1c4e04c8cf"   # rediseño del panel, UX/UI aprobada
REVISION_BASE="e8052cde6e89a11595c15e7e13a299c51bc1ac7a"       # estado anterior: el diff de estáticos sale de aquí
RAMA="claude/ork3d-landing-page-b2fQr"
BUCKET="ork3d.com"
DIST="ECOW0H6A48HVP"
BASE="https://ork3d.com"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFIESTO="$ROOT/infra/admin-ui-archivos.tsv"
AYUDA="$ROOT/infra/admin_ui_ayuda.py"
AWS="${AWS_CLI:-aws}"
GIT="${GIT_CMD:-git}"
CURL="${CURL_CMD:-curl}"

PERFIL="${AWS_PROFILE:-default}"
REGION="${AWS_REGION:-us-east-1}"
[[ "$PERFIL" == default ]] || { echo "ERROR: AWS_PROFILE solo puede ser default en este despliegue." >&2; exit 2; }
[[ "$REGION" == us-east-1 ]] || { echo "ERROR: AWS_REGION solo puede ser us-east-1 en este despliegue." >&2; exit 2; }
export AWS_PROFILE="$PERFIL" AWS_REGION="$REGION" AWS_PAGER=""

fallo()    { echo "ERROR: $*" >&2; exit 1; }
no_listo() { echo "NO LISTO: $*" >&2; exit 4; }

# Filas del manifiesto sin comentarios: archivo, clave, content-type, cache-control, rutas.
# Sin argumento lee el del repo; con una ruta, el manifiesto guardado en una copia de rollback.
leer_manifiesto() {
  grep -v '^#' "${1:-$MANIFIESTO}" | grep -v '^[[:space:]]*$'
}

# Guardas del repo. Solo lectura: git local y ls-remote (sin fetch, sin escribir refs).
verificar_repo() {
  python3 "$AYUDA" validar-manifiesto "$MANIFIESTO" > /dev/null || no_listo "el manifiesto no es válido (python3 $AYUDA validar-manifiesto)"
  "$GIT" -C "$ROOT" rev-parse --is-inside-work-tree > /dev/null 2>&1 || no_listo "no es un repositorio git"
  local rama head remoto esperado declarado linea archivo clave ct cc rutas hash_head hash_rev
  rama="$("$GIT" -C "$ROOT" rev-parse --abbrev-ref HEAD)"
  [[ "$rama" == "$RAMA" ]] || no_listo "rama $rama; se esperaba $RAMA"
  [[ -z "$("$GIT" -C "$ROOT" status --porcelain)" ]] || no_listo "el repo tiene cambios sin commitear: se publica solo lo commiteado"
  head="$("$GIT" -C "$ROOT" rev-parse HEAD)"
  remoto="$("$GIT" -C "$ROOT" ls-remote origin "refs/heads/$RAMA" 2> /dev/null | cut -f1)"
  [[ -n "$remoto" ]] || no_listo "no se pudo leer origin/$RAMA (ls-remote)"
  [[ "$head" == "$remoto" ]] || no_listo "HEAD $head no coincide con origin/$RAMA $remoto: hacé push antes de publicar"
  "$GIT" -C "$ROOT" cat-file -e "$REVISION_REVISADA^{commit}" 2> /dev/null || no_listo "falta el commit $REVISION_REVISADA en el repo"
  "$GIT" -C "$ROOT" merge-base --is-ancestor "$REVISION_REVISADA" HEAD 2> /dev/null \
    || no_listo "HEAD no contiene el commit revisado $REVISION_REVISADA"

  # El manifiesto tiene que ser exactamente la intersección entre el diff de estáticos y los estáticos del panel.
  esperado="$("$GIT" -C "$ROOT" diff --name-only "$REVISION_BASE" "$REVISION_REVISADA" | python3 "$AYUDA" estaticos-admin | sort)"
  declarado="$(leer_manifiesto | cut -f1 | sort)"
  [[ "$esperado" == "$declarado" ]] \
    || no_listo "el manifiesto no es la intersección de git diff $REVISION_BASE..$REVISION_REVISADA con los estáticos del panel"

  # Cada archivo del manifiesto existe y su contenido en HEAD es el de la revisión revisada.
  while IFS=$'\t' read -r archivo clave ct cc rutas; do
    [[ -f "$ROOT/$archivo" ]] || no_listo "falta el archivo local $archivo"
    hash_head="$("$GIT" -C "$ROOT" rev-parse "HEAD:$archivo" 2> /dev/null)" || no_listo "$archivo no existe en HEAD"
    hash_rev="$("$GIT" -C "$ROOT" rev-parse "$REVISION_REVISADA:$archivo" 2> /dev/null)" || no_listo "$archivo no existe en $REVISION_REVISADA"
    [[ "$hash_head" == "$hash_rev" ]] || no_listo "$archivo en HEAD no es el de $REVISION_REVISADA (contenido distinto)"
  done < <(leer_manifiesto)

  echo "  repo: rama $RAMA · HEAD $head = origin · contiene $REVISION_REVISADA"
  echo "  manifiesto = intersección de git diff $REVISION_BASE..$REVISION_REVISADA con los estáticos del panel ($(wc -l <<< "$declarado" | tr -d ' ') archivos)"
}

# SHA-256 del objeto en S3. Deja el resultado en REMOTO_SHA: AUSENTE si no existe, o el hash. Solo lectura.
# Un error distinto de 404 detiene todo: no se confunde con "no existe".
# Deja el head-object en "$2.head" para comparar cabeceras.
remoto_sha() {
  local clave="$1" destino="$2"
  if ! "$AWS" s3api head-object --bucket "$BUCKET" --key "$clave" --output json > "$destino.head" 2> "$destino.err" < /dev/null; then
    if grep -q -E '\(404\)|NoSuchKey|Not Found' "$destino.err"; then
      REMOTO_SHA=AUSENTE
      return 0
    fi
    fallo "head-object $clave: $(cat "$destino.err")"
  fi
  "$AWS" s3api get-object --bucket "$BUCKET" --key "$clave" "$destino.cuerpo" --output json > /dev/null 2> "$destino.gerr" < /dev/null \
    || fallo "get-object $clave: $(cat "$destino.gerr")"
  REMOTO_SHA="$(sha256sum "$destino.cuerpo" | cut -d' ' -f1)"
}

# Existencia de un objeto en S3, en una sola palabra: si | no | error: <detalle>. Solo "no" con 404 explícito; cualquier
# otro error (403, 500, red) es "error" y no se interpreta como ausencia. Solo lectura.
existencia_s3() {
  local clave="$1" salida
  if salida="$("$AWS" s3api head-object --bucket "$BUCKET" --key "$clave" --output json 2>&1 < /dev/null)"; then
    echo si
  elif grep -q -E '\(404\)|NoSuchKey' <<< "$salida"; then
    echo no
  else
    echo "error: $(tr '\n' ' ' <<< "$salida" | cut -c1-200)"
  fi
}

# Verdadero si el archivo local y su blob en HEAD son exactamente el blob de REVISION_REVISADA. Solo lectura de git.
blob_coincide() {  # ARCHIVO
  local archivo="$1" local_h head_h rev_h
  [[ -f "$ROOT/$archivo" ]] || return 1
  local_h="$("$GIT" -C "$ROOT" hash-object -- "$ROOT/$archivo" 2> /dev/null)" || return 1
  head_h="$("$GIT" -C "$ROOT" rev-parse "HEAD:$archivo" 2> /dev/null)" || return 1
  rev_h="$("$GIT" -C "$ROOT" rev-parse "$REVISION_REVISADA:$archivo" 2> /dev/null)" || return 1
  [[ "$local_h" == "$rev_h" && "$head_h" == "$rev_h" ]]
}

