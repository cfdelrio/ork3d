"""Transiciones de moderación de la web V2 (más estrictas que la CLI, ver docs/resenas/V2-ADMIN.md §6).

Pares (acción, estado de origen) → estado de destino. Cualquier otro par es 409.
"""
from . import errores

ESTADOS = ('pending', 'approved', 'rejected', 'hidden')
ACCIONES = ('aprobar', 'rechazar', 'ocultar', 'restaurar')

DESTINO = {
    ('aprobar', 'pending'): 'approved',
    ('rechazar', 'pending'): 'rejected',
    ('rechazar', 'approved'): 'rejected',
    ('ocultar', 'approved'): 'hidden',
    ('restaurar', 'hidden'): 'approved',
}

# Pares que cambian lo que ve el público (exigen regenerar el export).
CAMBIA_EXPORT = {('aprobar', 'pending'), ('rechazar', 'approved'), ('ocultar', 'approved'),
                 ('restaurar', 'hidden')}

REQUIERE_PERMISO = {'aprobar', 'restaurar'}


def destino(accion, estado_actual):
    nuevo = DESTINO.get((accion, estado_actual))
    if nuevo is None:
        raise errores.ErrorEstado(accion, estado_actual)
    return nuevo


def exige_permiso(accion, resena):
    if accion in REQUIERE_PERMISO and resena.get('permission_to_publish') is not True:
        raise errores.ErrorSinPermiso('permission_to_publish')


def cambia_export(accion, estado_actual):
    return (accion, estado_actual) in CAMBIA_EXPORT
