---
name: cambios-seguros-ork3d
description: Guía para hacer cambios en el repo de ORK3D (sitio web, landings, estilos, tests) sin romper lo que el dueño del proyecto (CEO) ya aprobó. Úsala cuando se pida modificar páginas, textos, estilos, scripts, tests o el flujo de publicación de ork3d.com.
---

# Cambios seguros en ORK3D

Tenés libertad para proponer y hacer cambios en el código y el contenido del sitio, siempre dentro de tu propia rama y con un PR. Lo que sí necesita aviso previo está en `referencias/temas-sensibles.md`.

## Libertad (no hace falta preguntar)

- Cambios de código, estilos, estructura HTML, tests, scripts de validación y correcciones técnicas.
- Ajustes de diseño que no toquen identidad, textos aprobados ni publicación.
- Nuevas páginas o secciones, siempre que no toquen los temas sensibles.

## Antes de tocar algo sensible: avisá y esperá

Si el cambio toca un tema de `referencias/temas-sensibles.md`, escribí primero:

> ⚠️ Tema sensible: <nombre del tema>. Qué cambiaría: <descripción concreta>. ¿Lo hago?

No modifiques ese punto hasta recibir confirmación.

## Lo que nunca hacés

- Hacer `push` ni `merge` a `main`.
- Ejecutar deploy, comandos de AWS ni cambios en la infraestructura.
- Borrar contenido del bucket, de `resenas/` o del panel admin.
- Commitear credenciales, claves, tokens ni archivos `.env`.
- Publicar material real de clientes (video, fotos, nombres, montos) fuera de lo que ya está publicado.

## Flujo de trabajo

1. Partí de `main` actualizado:
   ```
   git fetch origin
   git switch -c claude/<tema-corto> origin/main
   ```
2. Hacé los cambios en esa rama.
3. Validá antes de abrir el PR:
   ```
   python3 dev/validar.py
   ```
   Si tocaste una página, corré también el E2E que le corresponda (`dev/e2e/*.e2e.js`, con `NODE_PATH=/opt/node22/lib/node_modules`). Para cambios grandes, corré `dev/suite.sh` completo.
4. Subí solo tu rama (`git push -u origin claude/<tema-corto>`).
5. Abrí un PR contra `main` con esta información:
   - Qué cambió y por qué.
   - **Temas sensibles tocados:** "ninguno" o la lista, con el aviso que diste y la confirmación recibida.
   - Resultado de las validaciones.
6. El dueño revisa y decide el merge. Vos no mergeás.

## Señales de que algo se rompió

Si notás desborde horizontal, CTA invisibles, enlaces rotos o un test que falla y no sabés por qué, no lo "arregles a ciegas": reportalo en el PR o avisá antes de seguir.
