"""Logs sanitizados: solo campos de una lista permitida y valores simples.

Nunca se loguean tokens, hashes (ni prefijos), comentarios, private_feedback, contacto,
correos, URLs firmadas, cookies ni códigos de login. Un valor que no es un identificador
simple se reemplaza por "[omitido]", aunque lo pida el código que llama.
"""
import datetime as _dt
import json
import logging
import re

log = logging.getLogger('resenas')
log.setLevel(logging.INFO)

CAMPOS = frozenset({'review_id', 'op', 'status', 'estado', 'resultado', 'error', 'ruta',
                    'intento', 'foto', 'count'})
_VALOR = re.compile(r'[A-Za-z0-9_./:{}\-]{0,120}')
_HEX32 = re.compile(r'[a-f0-9]{32}')
_SECRETO = re.compile(r'[A-Za-z0-9_-]{20,}')


def _ts():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')


def _valor(v):
    if v is None or isinstance(v, (bool, int)):
        return v
    if isinstance(v, str) and _VALOR.fullmatch(v):
        return v
    return '[omitido]'


def evento(nombre, **campos):
    """Escribe una línea JSON sanitizada y la devuelve (para tests)."""
    limpio = {k: _valor(v) for k, v in campos.items() if k in CAMPOS}
    registro = {'ev': nombre, 'ts': _ts(), **limpio}
    log.info(json.dumps(registro, ensure_ascii=False))
    return registro


def ruta_plantilla(ruta):
    """/tokens/<token> → /tokens/{token}; /resenas/<id> → /resenas/{id}."""
    segmentos = []
    for seg in (ruta or '/').split('/'):
        if _HEX32.fullmatch(seg):
            segmentos.append('{id}')
        elif _SECRETO.fullmatch(seg):
            segmentos.append('{token}')
        else:
            segmentos.append(seg)
    return '/'.join(segmentos) or '/'
