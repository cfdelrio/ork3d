"""Núcleo reusable de reseñas ORK3D (V2).

Lo usan la Lambda pública (app.py), la Lambda admin (admin_app.py) y los tests.
No importa boto3: los clientes reales se inyectan desde afuera, y los tests usan fakes.
Las reglas de token, de transición, de export y de sesión viven aquí una sola vez.
"""
