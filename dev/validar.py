#!/usr/bin/env python3
"""Validaciones estáticas de ORK3D: sintaxis, HTML, JSON y políticas de reseñas.

Uso (desde la raíz del repo):  python3 dev/validar.py
Sale con 1 si alguna validación falla. No hay validaciones que se salteen: si falta una
herramienta (node, bash) la validación falla y lo dice.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fallos = []
aprobadas = 0


def ok(nombre):
    global aprobadas
    aprobadas += 1
    print(f'PASS  {nombre}')


def falla(nombre, detalle):
    fallos.append(nombre)
    print(f'FAIL  {nombre}\n      {detalle}')


def leer(ruta):
    with open(os.path.join(ROOT, ruta), encoding='utf-8') as f:
        return f.read()


def comprobar(nombre, condicion, detalle=''):
    if condicion:
        ok(nombre)
    else:
        falla(nombre, detalle or 'condición no cumplida')


def correr(comando):
    return subprocess.run(comando, cwd=ROOT, capture_output=True, text=True)


# ── Sintaxis ───────────────────────────────────────────────────────────────
def validar_javascript():
    node = shutil.which('node')
    archivos = sorted(glob.glob(os.path.join(ROOT, 'assets/js/*.js')) +
                      glob.glob(os.path.join(ROOT, 'dev/e2e/*.js')) + [os.path.join(ROOT, 'config.js')])
    if not node:
        falla('JavaScript: node disponible', 'no se encontró node; no se puede validar la sintaxis')
        return
    for ruta in archivos:
        r = correr([node, '--check', ruta])
        comprobar(f'JavaScript: sintaxis de {os.path.relpath(ruta, ROOT)}', r.returncode == 0, r.stderr.strip())


def validar_shell():
    archivos = ['deploy.sh', 'verify.sh', 'infra/setup.sh', 'infra/teardown.sh', 'infra/resenas-setup.sh',
                'admin/resenas.sh', 'admin/tests/run_tests.sh', 'admin/tests/fixtures.sh']
    for rel in archivos:
        r = correr(['bash', '-n', rel])
        comprobar(f'Shell: sintaxis de {rel}', r.returncode == 0, r.stderr.strip())


def validar_python():
    archivos = glob.glob(os.path.join(ROOT, 'api/resenas/*.py')) + glob.glob(os.path.join(ROOT, 'dev/*.py')) + \
        glob.glob(os.path.join(ROOT, 'admin/tests/*.py'))
    for ruta in archivos:
        r = correr([sys.executable, '-m', 'py_compile', ruta])
        comprobar(f'Python: compila {os.path.relpath(ruta, ROOT)}', r.returncode == 0, r.stderr.strip())


def validar_json():
    for ruta in sorted(glob.glob(os.path.join(ROOT, 'infra/resenas/*.json'))):
        try:
            with open(ruta, encoding='utf-8') as f:
                json.load(f)
            ok(f'JSON: {os.path.relpath(ruta, ROOT)}')
        except ValueError as exc:
            falla(f'JSON: {os.path.relpath(ruta, ROOT)}', str(exc))


# ── HTML: etiquetas balanceadas ────────────────────────────────────────────
VACIAS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
CIERRE_OPCIONAL = {'li', 'p', 'dt', 'dd', 'option'}


class Balance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.pila = []
        self.errores = []

    def handle_starttag(self, tag, attrs):
        if tag not in VACIAS:
            self.pila.append((tag, self.getpos()))

    def handle_startendtag(self, tag, attrs):
        pass  # <path/>, <img/>: no abren nada

    def handle_endtag(self, tag):
        if tag in VACIAS:
            return
        for i in range(len(self.pila) - 1, -1, -1):
            if self.pila[i][0] == tag:
                for abierta, pos in self.pila[i + 1:]:
                    if abierta not in CIERRE_OPCIONAL:
                        self.errores.append(f'<{abierta}> (línea {pos[0]}) queda sin cerrar antes de </{tag}>')
                del self.pila[i:]
                return
        self.errores.append(f'</{tag}> (línea {self.getpos()[0]}) no tiene apertura')

    def cerrar(self):
        for tag, pos in self.pila:
            if tag not in CIERRE_OPCIONAL:
                self.errores.append(f'<{tag}> (línea {pos[0]}) nunca se cierra')


def validar_html():
    paginas = ['index.html', 'torneos.html', 'regalos-empresariales-fin-de-ano.html', 'error.html',
               'opinar.html', 'opiniones.html']
    for rel in paginas:
        parser = Balance()
        parser.feed(leer(rel))
        parser.cerrar()
        comprobar(f'HTML: etiquetas balanceadas en {rel}', not parser.errores, '; '.join(parser.errores[:5]))


# ── Políticas de privacidad y publicación ──────────────────────────────────
def validar_politicas():
    opinar = leer('opinar.html')
    comprobar('Reseñas: /opinar tiene noindex', re.search(r'<meta name="robots" content="[^"]*noindex', opinar) is not None)
    comprobar('Reseñas: /opinar tiene referrer no-referrer',
              '<meta name="referrer" content="no-referrer"' in opinar)
    terceros = ['googleapis', 'gstatic', 'gtag', 'googletagmanager', 'plausible', 'main.js', 'analytics']
    encontrados = [t for t in terceros if t in opinar]
    comprobar('Reseñas: /opinar no carga analytics ni terceros', not encontrados, f'aparece: {encontrados}')

    opiniones = leer('opiniones.html')
    comprobar('Reseñas: /opiniones está en noindex hasta aprobar el diseño',
              re.search(r'<meta name="robots" content="[^"]*noindex', opiniones) is not None)

    # /opiniones no debe estar enlazada en ninguna parte del sitio público.
    enlazada = []
    for rel in ['index.html', 'torneos.html', 'regalos-empresariales-fin-de-ano.html', 'error.html',
                'sitemap.xml', 'robots.txt', 'assets/js/main.js', 'assets/css/styles.css']:
        if 'opiniones' in leer(rel):
            enlazada.append(rel)
    comprobar('Reseñas: /opiniones no está enlazada en home, navegación, footer ni sitemap',
              not enlazada, f'aparece en: {enlazada}')

    config = leer('config.js')
    comprobar('Config: RESENAS_API_URL y GOOGLE_REVIEW_URL existen',
              'RESENAS_API_URL' in config and 'GOOGLE_REVIEW_URL' in config)

    robots = leer('robots.txt')
    comprobar('SEO: robots.txt no permite /api/', 'Disallow: /api/' in robots)

    deploy = leer('deploy.sh')
    for patron in ['api/*', 'admin/*', 'dev/*', 'docs/*', 'resenas/*']:
        comprobar(f'Deploy: sync excluye {patron}', f'--exclude "{patron}"' in deploy)
    comprobar('Deploy: sube opinar.html y opiniones.html',
              'opinar.html' in deploy and 'opiniones.html' in deploy)

    redir = leer('infra/redirect-www.js')
    comprobar("CloudFront: regla /opinar/* antes de la de extensionless",
              "'/opinar/'" in redir and "'/opinar/'" in redir and
              redir.index("'/opinar/'") < redir.index('Extensionless'))


# ── IAM: mínimo privilegio ────────────────────────────────────────────────
def validar_iam():
    def sentencias(nombre):
        with open(os.path.join(ROOT, 'infra/resenas', nombre), encoding='utf-8') as f:
            return json.load(f)['Statement']

    for nombre in ['lambda-permisos.json', 'admin-permisos.json']:
        for st in sentencias(nombre):
            acciones = st['Action'] if isinstance(st['Action'], list) else [st['Action']]
            comprobar(f'IAM {nombre}: sin s3:* ni recursos "*"',
                      'Resource' in st and st['Resource'] != '*' and 's3:*' not in acciones,
                      f'{st.get("Sid")}')

    lam = sentencias('lambda-permisos.json')
    puts = [r for st in lam if st['Action'] == 's3:PutObject' for r in st['Resource']]
    comprobar('IAM lambda: no puede escribir tokens/ (solo el CLI crea tokens)',
              not any('/tokens/' in r for r in puts), f'escribe en: {puts}')
    gets_tokens = any('/tokens/' in r for st in lam for r in (st['Resource'] if isinstance(st['Resource'], list) else [st['Resource']]))
    comprobar('IAM lambda: lee tokens/ para validarlos', gets_tokens)

    politica = json.load(open(os.path.join(ROOT, 'infra/resenas/bucket-policy.json'), encoding='utf-8'))
    niegan_http = [st for st in politica['Statement'] if st.get('Effect') == 'Deny' and
                   'aws:SecureTransport' in json.dumps(st.get('Condition', {}))]
    comprobar('Bucket privado: niega tráfico sin TLS', bool(niegan_http))


def main():
    validar_javascript()
    validar_shell()
    validar_python()
    validar_json()
    validar_html()
    validar_politicas()
    validar_iam()
    print(f'\nValidaciones: {aprobadas} PASS, {len(fallos)} FAIL')
    sys.exit(1 if fallos else 0)


if __name__ == '__main__':
    main()
