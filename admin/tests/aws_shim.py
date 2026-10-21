#!/usr/bin/env python3
"""Shim local que imita el subconjunto de AWS CLI que usa admin/resenas.sh.

Solo para tests: no toca AWS. Raíz de datos: variable de entorno SHIM_ROOT.

Diseño:
  - Bucket = subdirectorio de SHIM_ROOT.
  - Objeto actual = archivo dentro del bucket (SHIM_ROOT/<bucket>/<clave>). Si otro proceso
    escribe o borra archivos directamente (p. ej. los tests de contrato con la Lambda), el shim
    lo registra como versión nueva o borrado al primer uso del bucket (ver reconciliar()).
  - Cada put crea una versión: contenido en SHIM_ROOT/.versions/<bucket>/<VersionId>
    e índice JSON por objeto en SHIM_ROOT/.meta/<bucket>/<sha256(clave)>.json
    (lista de versiones; la última es la vigente; un marcador de borrado tiene Deleted=true).
  - SHIM_ROOT/_calls.log: invalidaciones de CloudFront (una línea por llamada).
  - SHIM_ROOT/_aws_calls.log: todas las llamadas, solo argumentos (nunca contenido).

Códigos de salida:
  - 254: objeto inexistente (head-object / get-object).
  - 255: PreconditionFailed (put-object con --if-none-match '*' y la clave ya existe).
  - 1: uso incorrecto o subcomando no implementado en el shim.
"""
import hashlib
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone

EXIT_USO = 1
EXIT_NO_EXISTE = 254
EXIT_PRECONDICION = 255


def fail(msg, code=EXIT_USO):
    sys.stderr.write(msg + "\n")
    sys.exit(code)


def ahora_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def quitar_file(ruta):
    return ruta[len("file://"):] if ruta.startswith("file://") else ruta


def parsear(argv):
    """Devuelve (servicio, operación, flags, posicionales). Flags repetibles: --paths."""
    if len(argv) < 2:
        fail("shim: uso: aws_shim.py <servicio> <operación> [opciones]")
    servicio, operacion = argv[0], argv[1]
    flags, pos = {}, []
    i = 2
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            nombre = a[2:]
            if nombre == "paths":
                valores = []
                i += 1
                while i < len(argv) and not argv[i].startswith("--"):
                    valores.append(argv[i])
                    i += 1
                flags[nombre] = valores
                continue
            if i + 1 >= len(argv):
                fail(f"shim: falta el valor de --{nombre}")
            flags[nombre] = argv[i + 1]
            i += 2
        else:
            pos.append(a)
            i += 1
    return servicio, operacion, flags, pos


def requerido(flags, nombre):
    if nombre not in flags:
        fail(f"shim: falta --{nombre}")
    return flags[nombre]


def validar_clave(clave):
    partes = clave.split("/")
    if not clave or clave.startswith("/") or any(p in ("", ".", "..") for p in partes):
        fail(f"shim: clave inválida: {clave!r}")
    return partes


# ── Almacenamiento ──────────────────────────────────────────────────────────

def raiz():
    r = os.environ.get("SHIM_ROOT")
    if not r:
        fail("shim: falta SHIM_ROOT en el entorno.")
    os.makedirs(r, exist_ok=True)
    return r


def dir_versiones(r, bucket):
    return os.path.join(r, ".versions", bucket)


def dir_meta(r, bucket):
    return os.path.join(r, ".meta", bucket)


def ruta_meta(r, bucket, clave):
    return os.path.join(dir_meta(r, bucket), hashlib.sha256(clave.encode("utf-8")).hexdigest() + ".json")


def cargar_versiones(r, bucket, clave):
    p = ruta_meta(r, bucket, clave)
    if not os.path.isfile(p):
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)["versions"]


def guardar_versiones(r, bucket, clave, versiones):
    p = ruta_meta(r, bucket, clave)
    if not versiones:
        if os.path.isfile(p):
            os.remove(p)
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"key": clave, "versions": versiones}, f)


def todas_las_claves(r, bucket):
    """Devuelve [(clave, versiones)] de un bucket, ordenado por clave."""
    resultado = []
    d = dir_meta(r, bucket)
    if not os.path.isdir(d):
        return resultado
    for nombre in os.listdir(d):
        if not nombre.endswith(".json"):
            continue
        with open(os.path.join(d, nombre), encoding="utf-8") as f:
            datos = json.load(f)
        resultado.append((datos["key"], datos["versions"]))
    resultado.sort(key=lambda kv: kv[0])
    return resultado


def vigente(versiones):
    return versiones[-1] if versiones else None


def presente(versiones):
    v = vigente(versiones)
    return v is not None and not v["Deleted"]


def sincronizar_vigente(r, bucket, clave, versiones):
    """Mantiene el archivo del objeto actual (objeto = archivo) según la versión vigente."""
    destino = os.path.join(r, bucket, *validar_clave(clave))
    if presente(versiones):
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        shutil.copyfile(os.path.join(dir_versiones(r, bucket), vigente(versiones)["VersionId"]), destino)
    elif os.path.isfile(destino):
        os.remove(destino)


def contenido_version(r, bucket, version):
    return os.path.join(dir_versiones(r, bucket), version["VersionId"])


# ── Operaciones s3api ───────────────────────────────────────────────────────

def put_object(r, bucket, clave, cuerpo, tipo, cache, if_none_match):
    validar_clave(clave)
    versiones = cargar_versiones(r, bucket, clave)
    if if_none_match is not None:
        if if_none_match != "*":
            fail("shim: solo se soporta --if-none-match '*'")
        if presente(versiones):
            fail("PreconditionFailed: At least one of the pre-conditions you specified did not hold (If-None-Match: *)",
                 EXIT_PRECONDICION)
    cuerpo = quitar_file(cuerpo)
    if not os.path.isfile(cuerpo):
        fail(f"shim: no existe el archivo a subir: {cuerpo}")
    with open(cuerpo, "rb") as f:
        datos = f.read()
    vid = uuid.uuid4().hex
    os.makedirs(dir_versiones(r, bucket), exist_ok=True)
    with open(os.path.join(dir_versiones(r, bucket), vid), "wb") as f:
        f.write(datos)
    versiones.append({
        "VersionId": vid,
        "Deleted": False,
        "Size": len(datos),
        "MD5": hashlib.md5(datos).hexdigest(),
        "ContentType": tipo,
        "CacheControl": cache,
        "LastModified": ahora_iso(),
    })
    guardar_versiones(r, bucket, clave, versiones)
    sincronizar_vigente(r, bucket, clave, versiones)
    return {"ETag": '"' + hashlib.md5(datos).hexdigest() + '"', "VersionId": vid}


def metadatos(version):
    meta = {"ContentLength": version["Size"], "LastModified": version["LastModified"],
            "VersionId": version["VersionId"]}
    if version.get("ContentType"):
        meta["ContentType"] = version["ContentType"]
    if version.get("CacheControl"):
        meta["CacheControl"] = version["CacheControl"]
    return meta


def get_object(r, bucket, clave, outfile):
    versiones = cargar_versiones(r, bucket, clave)
    if not presente(versiones):
        fail(f"An error occurred (NoSuchKey) when calling the GetObject operation: The specified key does not exist. ({clave})",
             EXIT_NO_EXISTE)
    v = vigente(versiones)
    shutil.copyfile(contenido_version(r, bucket, v), outfile)
    return metadatos(v)


def head_object(r, bucket, clave):
    versiones = cargar_versiones(r, bucket, clave)
    if not presente(versiones):
        fail(f"An error occurred (404) when calling the HeadObject operation: Not Found ({clave})", EXIT_NO_EXISTE)
    return metadatos(vigente(versiones))


def list_objects_v2(r, bucket, prefijo):
    contenido = []
    for clave, versiones in todas_las_claves(r, bucket):
        if not clave.startswith(prefijo) or not presente(versiones):
            continue
        v = vigente(versiones)
        contenido.append({"Key": clave, "Size": v["Size"], "LastModified": v["LastModified"]})
    return {"Contents": contenido} if contenido else {}


def list_object_versions(r, bucket, prefijo):
    versiones_out, marcas_out = [], []
    for clave, versiones in todas_las_claves(r, bucket):
        if not clave.startswith(prefijo):
            continue
        for pos, v in enumerate(versiones):
            base = {"Key": clave, "VersionId": v["VersionId"], "IsLatest": pos == len(versiones) - 1}
            if v["Deleted"]:
                marcas_out.append(base)
            else:
                base["Size"] = v["Size"]
                versiones_out.append(base)
    salida = {}
    if versiones_out:
        salida["Versions"] = versiones_out
    if marcas_out:
        salida["DeleteMarkers"] = marcas_out
    return salida


def borrar_una(r, bucket, clave, version_id):
    """Sin version_id crea un marcador de borrado (como S3 versionado). Con version_id quita esa versión."""
    versiones = cargar_versiones(r, bucket, clave)
    if version_id is None:
        marca = uuid.uuid4().hex
        versiones.append({"VersionId": marca, "Deleted": True, "Size": 0, "LastModified": ahora_iso()})
        guardar_versiones(r, bucket, clave, versiones)
        sincronizar_vigente(r, bucket, clave, versiones)
        return {"DeleteMarker": True, "VersionId": marca}
    restantes = []
    for v in versiones:
        if v["VersionId"] == version_id:
            ruta = os.path.join(dir_versiones(r, bucket), version_id)
            if not v["Deleted"] and os.path.isfile(ruta):
                os.remove(ruta)
        else:
            restantes.append(v)
    guardar_versiones(r, bucket, clave, restantes)
    sincronizar_vigente(r, bucket, clave, restantes)
    return {"VersionId": version_id}


def delete_objects(r, bucket, archivo):
    with open(quitar_file(archivo), encoding="utf-8") as f:
        pedido = json.load(f)
    borrados = []
    for obj in pedido.get("Objects", []):
        clave = obj["Key"]
        version = obj.get("VersionId")
        borrar_una(r, bucket, clave, version)
        borrados.append({"Key": clave, "VersionId": version} if version else {"Key": clave})
    return {"Deleted": borrados}


def s3api(r, op, flags, pos):
    if op == "put-object":
        bucket = requerido(flags, "bucket")
        return put_object(r, bucket, requerido(flags, "key"), requerido(flags, "body"),
                          flags.get("content-type"), flags.get("cache-control"), flags.get("if-none-match"))
    if op == "get-object":
        if not pos:
            fail("shim: get-object necesita el archivo de salida como argumento posicional")
        return get_object(r, requerido(flags, "bucket"), requerido(flags, "key"), pos[0])
    if op == "head-object":
        return head_object(r, requerido(flags, "bucket"), requerido(flags, "key"))
    if op == "list-objects-v2":
        return list_objects_v2(r, requerido(flags, "bucket"), flags.get("prefix", ""))
    if op == "list-object-versions":
        return list_object_versions(r, requerido(flags, "bucket"), flags.get("prefix", ""))
    if op == "delete-object":
        return borrar_una(r, requerido(flags, "bucket"), requerido(flags, "key"), flags.get("version-id"))
    if op == "delete-objects":
        return delete_objects(r, requerido(flags, "bucket"), requerido(flags, "delete"))
    fail(f"shim: s3api {op} no está implementado en el shim")


# ── Operaciones s3 (cp, sync) y cloudfront ──────────────────────────────────

def s3(r, op, flags, pos):
    if op == "cp":
        if len(pos) != 2:
            fail("shim: uso: s3 cp SRC DST")
        origen, destino = pos
        if origen.startswith("s3://") and not destino.startswith("s3://"):
            bucket, clave = origen[5:].split("/", 1)
            if os.path.isdir(destino):
                destino = os.path.join(destino, os.path.basename(clave))
            get_object(r, bucket, clave, destino)
            print(f"download: {origen} to {destino}")
        elif destino.startswith("s3://") and not origen.startswith("s3://"):
            bucket, clave = destino[5:].split("/", 1)
            put_object(r, bucket, clave, origen, flags.get("content-type"), flags.get("cache-control"), None)
            print(f"upload: {origen} to {destino}")
        else:
            fail("shim: s3 cp solo soporta local→s3:// y s3://→local")
        return None
    if op == "sync":
        if len(pos) != 2 or not pos[0].startswith("s3://") or pos[1].startswith("s3://"):
            fail("shim: s3 sync solo soporta: s3://BUCKET DIR (backup)")
        bucket = pos[0][5:].strip("/")
        destino_dir = pos[1]
        for clave, versiones in todas_las_claves(r, bucket):
            if not presente(versiones):
                continue
            destino = os.path.join(destino_dir, *validar_clave(clave))
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            shutil.copyfile(contenido_version(r, bucket, vigente(versiones)), destino)
            print(f"download: s3://{bucket}/{clave} to {destino}")
        return None
    fail(f"shim: s3 {op} no está implementado en el shim")


def cloudfront(r, op, flags):
    if op != "create-invalidation":
        fail(f"shim: cloudfront {op} no está implementado en el shim")
    distribucion = requerido(flags, "distribution-id")
    rutas = flags.get("paths", [])
    if isinstance(rutas, str):
        rutas = [rutas]
    with open(os.path.join(r, "_calls.log"), "a", encoding="utf-8") as f:
        f.write(f"create-invalidation distribution={distribucion} paths={' '.join(rutas)}\n")
    return {"Invalidation": {"Id": "TESTINV"}}


def reconciliar(r, bucket):
    """Pone el historial al día con los archivos del bucket.

    Un archivo cuyo contenido no coincide con la última versión se registra como versión nueva
    (escritura externa). Un objeto con versión vigente que ya no tiene archivo se registra como
    borrado (borrado externo). Así el shim ve lo mismo que ve el código que escribe directo al disco.
    """
    base = os.path.join(r, bucket)
    if not os.path.isdir(base):
        return
    vistos = set()
    for dirpath, _dirs, nombres in os.walk(base):
        for nombre in nombres:
            ruta = os.path.join(dirpath, nombre)
            clave = os.path.relpath(ruta, base).replace(os.sep, "/")
            vistos.add(clave)
            with open(ruta, "rb") as f:
                datos = f.read()
            md5 = hashlib.md5(datos).hexdigest()
            versiones = cargar_versiones(r, bucket, clave)
            vig = vigente(versiones)
            if vig is not None and not vig["Deleted"] and vig.get("MD5") == md5:
                continue
            vid = uuid.uuid4().hex
            os.makedirs(dir_versiones(r, bucket), exist_ok=True)
            with open(os.path.join(dir_versiones(r, bucket), vid), "wb") as f:
                f.write(datos)
            versiones.append({"VersionId": vid, "Deleted": False, "Size": len(datos),
                              "MD5": md5, "LastModified": ahora_iso()})
            guardar_versiones(r, bucket, clave, versiones)
    for clave, versiones in todas_las_claves(r, bucket):
        vig = vigente(versiones)
        if vig is not None and not vig["Deleted"] and clave not in vistos:
            versiones.append({"VersionId": uuid.uuid4().hex, "Deleted": True, "Size": 0,
                              "LastModified": ahora_iso()})
            guardar_versiones(r, bucket, clave, versiones)


def buckets_de(flags, pos):
    """Buckets que toca una llamada: --bucket y los argumentos s3://BUCKET/..."""
    nombres = set()
    if "bucket" in flags:
        nombres.add(flags["bucket"])
    for p in pos:
        if p.startswith("s3://"):
            nombres.add(p[5:].split("/", 1)[0])
    return sorted(nombres)


def main(argv):
    servicio, op, flags, pos = parsear(argv)
    r = raiz()
    with open(os.path.join(r, "_aws_calls.log"), "a", encoding="utf-8") as f:
        f.write(" ".join(argv) + "\n")
    if servicio in ("s3api", "s3"):
        for bucket in buckets_de(flags, pos):
            reconciliar(r, bucket)
    if servicio == "s3api":
        resultado = s3api(r, op, flags, pos)
    elif servicio == "s3":
        resultado = s3(r, op, flags, pos)
    elif servicio == "cloudfront":
        resultado = cloudfront(r, op, flags)
    else:
        fail(f"shim: servicio no implementado: {servicio}")
    if resultado is not None:
        sys.stdout.write(json.dumps(resultado, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
