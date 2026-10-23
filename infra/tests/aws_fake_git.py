#!/usr/bin/env python3
"""Simulador de git para probar infra/admin-deploy.sh. Solo responde las consultas de solo lectura del script.

Variables: FAKE_GIT_RAMA (branch), FAKE_GIT_HEAD (commit local), FAKE_GIT_REMOTO (commit del remoto),
FAKE_GIT_SUCIO=1 (árbol con cambios).
"""
import os
import sys

RAMA = os.environ.get('FAKE_GIT_RAMA', 'main')
HEAD = os.environ.get('FAKE_GIT_HEAD', '202ecb7ed9016a607eb2854f3d5a92ccf8a5466c')
REMOTO = os.environ.get('FAKE_GIT_REMOTO', HEAD)
args = sys.argv[1:]
if args == ['rev-parse', '--abbrev-ref', 'HEAD']:
    print(RAMA)
elif args == ['status', '--porcelain']:
    if os.environ.get('FAKE_GIT_SUCIO') == '1':
        print(' M dev/validar.py')
elif args == ['rev-parse', 'HEAD']:
    print(HEAD)
elif args[:2] == ['ls-remote', 'origin']:
    ref = args[2]
    print(f'{REMOTO}\t{ref}')
else:
    sys.stderr.write(f'fake git: no simulado: {args}\n')
    sys.exit(2)
