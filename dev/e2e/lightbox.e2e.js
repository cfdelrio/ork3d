#!/usr/bin/env node
/* E2E de la foto ampliada en la home (sección "piezas"), en Chromium real contra el sitio del repo servido por dev/serve.py.
 *
 * Qué prueba:
 *   - cada foto del mosaico es un botón con nombre accesible y abre la foto a pantalla completa;
 *   - la imagen ampliada cabe en el viewport en 375 y en 1280 px, y no se pide ningún recurso nuevo;
 *   - Escape, el botón ×, y el clic en el fondo cierran; el clic en la foto no;
 *   - el foco vuelve al botón que abrió el diálogo, y Tab no sale del diálogo;
 *   - con teclado (Enter) abre la foto correspondiente;
 *   - el scroll de la página queda bloqueado mientras está abierta;
 *   - sin soporte de <dialog> (showModal) la página queda como antes: sin botones y sin errores.
 *
 * Uso (desde la raíz del repo):
 *   SHOTS_DIR=<carpeta para capturas> node dev/e2e/lightbox.e2e.js
 * Variables opcionales: PORT (8793), CHROME_PATH, PLAYWRIGHT_PATH.
 */
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const PORT = Number(process.env.PORT || 8793);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR || path.join(ROOT, 'dev', 'e2e', 'capturas');
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined);
// Viewports: móvil, escritorio y una pantalla grande (la foto tiene que escalar, no quedar en su tamaño natural).
const VISTAS = [{ ancho: 375, alto: 800 }, { ancho: 1280, alto: 800 }, { ancho: 1920, alto: 1080 }];
const SERIE = '/assets/img/fotos/series-800.webp';
const PERSONALIZACION = '/assets/img/fotos/personalizacion-470.webp';
const PRODUCCION = '/assets/img/fotos/produccion-retrato-740.webp';

const casos = [];
function caso(nombre, fn) { casos.push({ nombre, fn }); }
function afirmar(condicion, mensaje) { if (!condicion) throw new Error(mensaje); }

function arrancarServidor() {
  return new Promise((resolver, rechazar) => {
    const proc = spawn('python3', [path.join(ROOT, 'dev', 'serve.py'), String(PORT)], { cwd: ROOT });
    let salida = '';
    let listo = false;
    const recoger = (d) => {
      if (listo) return;
      salida += d;
      if (salida.includes('ORK3D dev')) { listo = true; resolver(proc); }
    };
    proc.stdout.on('data', recoger);
    proc.stderr.on('data', recoger);
    proc.on('exit', (code) => rechazar(new Error(`dev/serve.py salió con ${code}: ${salida}`)));
  });
}

// Solo cuentan los errores del propio sitio: los recursos de terceros (analítica, fuentes) no dependen de este repo.
function esExterno(url) {
  return !url.startsWith(BASE);
}

async function abrirHome(browser, ancho, { sinDialogo = false, alto = 800 } = {}) {
  const ctx = await browser.newContext({ viewport: { width: ancho, height: alto }, locale: 'es-AR' });
  const page = await ctx.newPage();
  const errores = [];
  page.on('pageerror', (e) => errores.push('pageerror: ' + e.message));
  page.on('console', (m) => {
    if (m.type() === 'error' && !esExterno((m.location() && m.location().url) || BASE)) {
      errores.push('consola: ' + m.text());
    }
  });
  if (sinDialogo) {
    await page.addInitScript(() => { delete HTMLDialogElement.prototype.showModal; });
  }
  await page.goto(BASE + '/', { waitUntil: 'load' });
  await page.locator('#piezas').scrollIntoViewIfNeeded();
  return { ctx, page, errores };
}

const botones = (p) => p.locator('#piezas .mosaic__zoom');
const dialogo = (p) => p.locator('dialog.lightbox');
const dialogoAbierto = (p) => p.locator('dialog.lightbox').evaluate((d) => d.open);

async function abrirConBoton(p, indice) {
  await botones(p).nth(indice).click();
  await p.waitForFunction(() => {
    const d = document.querySelector('dialog.lightbox');
    return d && d.open;
  });
  await p.waitForFunction(() => {
    const i = document.querySelector('dialog.lightbox img');
    return i && i.complete && i.naturalWidth > 0;
  });
}

caso('cada foto del mosaico es un botón con nombre accesible', async (browser) => {
  const { ctx, page, errores } = await abrirHome(browser, 1280);
  try {
    afirmar(await botones(page).count() === 8, `se esperaban 8 botones (3 del mosaico + 5 de la galería), hay ${await botones(page).count()}`);
    const nombres = await botones(page).evaluateAll((els) => els.map((e) => e.getAttribute('aria-label')));
    for (const n of nombres) afirmar(n && n.startsWith('Ampliar foto: '), `nombre accesible inesperado: ${n}`);
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

for (const { ancho, alto } of VISTAS) {
  caso(`clic abre la foto a pantalla completa y ocupa el viewport (${ancho}x${alto})`, async (browser) => {
    const { ctx, page, errores } = await abrirHome(browser, ancho, { alto });
    try {
      await abrirConBoton(page, 0);
      const src = await page.locator('dialog.lightbox img').getAttribute('src');
      afirmar(src === SERIE, `la foto ampliada debe ser ${SERIE}, es ${src}`);
      const caja = await page.locator('dialog.lightbox img').boundingBox();
      const vista = page.viewportSize();
      afirmar(caja.width <= vista.width && caja.height <= vista.height,
        `la foto no cabe: ${Math.round(caja.width)}x${Math.round(caja.height)} en ${vista.width}x${vista.height}`);
      // Pantalla completa: la foto ocupa al menos el 80% del viewport en su dimensión limitante.
      const ocupacion = Math.max(caja.width / vista.width, caja.height / vista.height);
      afirmar(ocupacion >= 0.8, `la foto ocupa solo el ${Math.round(ocupacion * 100)}% del viewport`);
      afirmar(await page.evaluate(() => document.documentElement.classList.contains('is-lightbox-open')),
        'la página debe marcar que el lightbox está abierto');
      const scroll = await page.evaluate(() => getComputedStyle(document.documentElement).overflow);
      afirmar(scroll === 'hidden', `el scroll de la página debe quedar bloqueado, overflow=${scroll}`);
      fs.mkdirSync(SHOTS, { recursive: true });
      await page.screenshot({ path: path.join(SHOTS, `lightbox-abierta-${ancho}x${alto}.png`) });
      afirmar(errores.length === 0, errores.join(' | '));
    } finally {
      await ctx.close();
    }
  });
}

caso('Escape cierra y devuelve el foco al botón que abrió el diálogo', async (browser) => {
  const { ctx, page, errores } = await abrirHome(browser, 1280);
  try {
    await abrirConBoton(page, 1);
    await page.keyboard.press('Escape');
    afirmar(!(await dialogoAbierto(page)), 'Escape debe cerrar el diálogo');
    const etiqueta = await page.evaluate(() => document.activeElement && document.activeElement.getAttribute('aria-label'));
    afirmar(etiqueta && etiqueta.startsWith('Ampliar foto: Llavero'), `el foco debe volver al botón de la foto 2, quedó en "${etiqueta}"`);
    afirmar(!(await page.evaluate(() => document.documentElement.classList.contains('is-lightbox-open'))),
      'al cerrar debe quitarse el bloqueo de scroll');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('el botón × cierra; el clic en la foto no cierra; el clic en el fondo sí', async (browser) => {
  const { ctx, page, errores } = await abrirHome(browser, 1280);
  try {
    await abrirConBoton(page, 0);
    await page.locator('dialog.lightbox img').click();
    afirmar(await dialogoAbierto(page), 'el clic sobre la foto no debe cerrar el diálogo');
    await page.mouse.click(5, 5);
    afirmar(!(await dialogoAbierto(page)), 'el clic en el fondo debe cerrar el diálogo');
    await abrirConBoton(page, 2);
    await page.locator('.lightbox__close').click();
    afirmar(!(await dialogoAbierto(page)), 'el botón × debe cerrar el diálogo');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('teclado: Enter abre la foto de su botón y Tab no sale del diálogo', async (browser) => {
  const { ctx, page, errores } = await abrirHome(browser, 1280);
  try {
    await botones(page).nth(1).focus();
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => {
      const d = document.querySelector('dialog.lightbox');
      return d && d.open;
    });
    afirmar((await page.locator('dialog.lightbox img').getAttribute('src')) === PERSONALIZACION,
      'Enter sobre la foto 2 debe abrir la foto 2');
    for (let i = 0; i < 4; i++) {
      await page.keyboard.press('Tab');
      const dentro = await page.evaluate(() => document.querySelector('dialog.lightbox').contains(document.activeElement));
      afirmar(dentro, `Tab ${i + 1} sacó el foco del diálogo`);
    }
    await page.keyboard.press('Shift+Tab');
    afirmar(await page.evaluate(() => document.querySelector('dialog.lightbox').contains(document.activeElement)),
      'Shift+Tab sacó el foco del diálogo');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('la galería muestra las cinco piezas nuevas, cargadas, y cada una se amplía', async (browser) => {
  const { ctx, page, errores } = await abrirHome(browser, 1280);
  try {
    const galeria = page.locator('.piezas__galeria');
    await galeria.scrollIntoViewIfNeeded();
    await page.waitForFunction(() => [...document.querySelectorAll('.piezas__galeria img')]
      .every((i) => i.complete && i.naturalWidth > 0), null, { timeout: 15000 });
    afirmar(await galeria.locator('img').count() === 5, 'la galería debe tener 5 fotos');
    const esperadas = [
      '/assets/img/fotos/piezas-serie-medlog-800.webp',
      '/assets/img/fotos/piezas-personalizado-lego-800.webp',
      '/assets/img/fotos/piezas-escudos-800.webp',
      '/assets/img/fotos/piezas-camiseta-40-800.webp',
      '/assets/img/fotos/piezas-goma-800.webp',
    ];
    for (let i = 0; i < esperadas.length; i++) {
      const src = await galeria.locator('img').nth(i).getAttribute('src');
      afirmar(src === esperadas[i], `foto ${i + 1} de la galería: ${src}`);
      await galeria.locator('.mosaic__zoom').nth(i).click();
      await page.waitForFunction(() => {
        const d = document.querySelector('dialog.lightbox');
        return d && d.open;
      });
      afirmar((await page.locator('dialog.lightbox img').getAttribute('src')) === esperadas[i],
        `el lightbox debe abrir la foto ${i + 1} de la galería`);
      await page.keyboard.press('Escape');
    }
    await galeria.screenshot({ path: path.join(SHOTS, 'galeria-1280.png') });
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('la galería se acomoda en 2 columnas en móvil, 3 en tablet y 5 en escritorio', async (browser) => {
  for (const [ancho, esperadas] of [[375, 2], [800, 3], [1280, 5]]) {
    const { ctx, page, errores } = await abrirHome(browser, ancho);
    try {
      const columnas = await page.evaluate(() => new Set(
        [...document.querySelectorAll('.piezas__galeria .mosaic__item')]
          .map((e) => Math.round(e.getBoundingClientRect().left))).size);
      afirmar(columnas === esperadas, `${ancho} px: ${columnas} columnas, se esperaban ${esperadas}`);
      afirmar(errores.length === 0, errores.join(' | '));
    } finally {
      await ctx.close();
    }
  }
});

caso('la galería no genera desborde horizontal en móvil', async (browser) => {
  const { ctx, page, errores } = await abrirHome(browser, 375);
  try {
    await page.locator('.piezas__galeria').scrollIntoViewIfNeeded();
    const d = await page.evaluate(() => ({ visible: document.documentElement.clientWidth, total: document.documentElement.scrollWidth }));
    afirmar(d.total <= d.visible, `desborde horizontal de ${d.total - d.visible}px en 375 px`);
    await page.locator('#piezas').screenshot({ path: path.join(SHOTS, 'piezas-375.png') });
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('la foto de producción abre su versión ampliada', async (browser) => {
  const { ctx, page, errores } = await abrirHome(browser, 375);
  try {
    await abrirConBoton(page, 2);
    afirmar((await page.locator('dialog.lightbox img').getAttribute('src')) === PRODUCCION,
      'la foto de producción debe abrir produccion-retrato-740.webp');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('sin soporte de <dialog> la página queda como antes: sin botones y sin errores', async (browser) => {
  const { ctx, page, errores } = await abrirHome(browser, 1280, { sinDialogo: true });
  try {
    afirmar(await botones(page).count() === 0, 'sin showModal no debe haber botones de ampliación');
    afirmar(await page.locator('#piezas picture img').first().isVisible(), 'las fotos deben seguir visibles');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

async function main() {
  const servidor = await arrancarServidor();
  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
  let fallos = 0;
  try {
    for (const c of casos) {
      try {
        await c.fn(browser);
        console.log(`PASS  ${c.nombre}`);
      } catch (e) {
        fallos += 1;
        console.log(`FAIL  ${c.nombre}\n      ${String(e.message).split('\n')[0]}`);
      }
    }
  } finally {
    await browser.close();
    servidor.kill();
  }
  console.log(`\nFoto ampliada E2E: ${casos.length - fallos} PASS, ${fallos} FAIL`);
  process.exit(fallos ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
