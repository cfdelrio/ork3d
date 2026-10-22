#!/usr/bin/env node
/* E2E de /opiniones en Chromium real, contra el sitio del repo servido por dev/serve.py.
 *
 * El export (/resenas/publicas.json) y las fotos se interceptan con page.route: cada caso fija su propio
 * contenido y el resultado no depende del S3 falso. La página, el JS y el CSS son los reales.
 *
 * Uso (desde la raíz del repo):
 *   SHOTS_DIR=<carpeta para capturas> node dev/e2e/opiniones.e2e.js
 * Variables opcionales: PORT (8796), CHROME_PATH, PLAYWRIGHT_PATH.
 */
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const PORT = Number(process.env.PORT || 8796);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR || path.join(ROOT, 'dev', 'e2e', 'capturas');
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined);
const ANCHOS = [320, 375, 390, 768, 1280];
// JPEG real de 8×6 px (287 bytes). El panel valida JPEG sin metadatos; el sitio solo lo muestra.
const JPEG = Buffer.from('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCAAGAAgDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAb/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAgX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwC5AQBf/9k=', 'base64');
const LLAVE = 'b'.repeat(32);   // id hexadecimal válido (32 caracteres)

fs.mkdirSync(SHOTS, { recursive: true });

function id(n) { return n.toString(16).padStart(32, '0'); }

// Reseña pública tal como la exporta publicacion.construir_export (allowlist).
function resena(n, cambios = {}) {
  return Object.assign({
    id: id(n), rating: 5, comment: 'Comentario de prueba ' + n, aspects: ['calidad'],
    display_name: 'Cliente ' + n, category: 'trofeos', product_label: 'Trofeo ' + n, verified: true,
    photo_url: null, published_at: '2026-10-' + String(n).padStart(2, '0') + 'T12:00:00Z',
  }, cambios);
}
function export_(items) {
  return { generated_at: '2026-10-22T12:00:00Z', count: items.length, items };
}

function arrancarServidor() {
  return new Promise((resolver, rechazar) => {
    const proc = spawn('python3', [path.join(ROOT, 'dev', 'serve.py'), String(PORT)], { cwd: ROOT });
    let salida = '';
    proc.stdout.on('data', (d) => { salida += d; if (salida.includes('ORK3D dev')) resolver(proc); });
    proc.stderr.on('data', (d) => { salida += d; });
    proc.on('exit', (code) => rechazar(new Error(`dev/serve.py salió con ${code}: ${salida}`)));
  });
}

function esExterno(url) {
  return !url.startsWith(BASE);
}

// Abre /opiniones con un contexto nuevo. opciones: export (array de items), retraso (ms), exportStatus,
// fotos ('ok' | 'fallar'), ancho, alto.
async function abrir(browser, opciones = {}) {
  const context = await browser.newContext({ viewport: { width: opciones.ancho || 1280, height: opciones.alto || 800 },
    locale: 'es-AR' });
  const page = await context.newPage();
  const errores = [];
  const fallas = [];
  const fotosPedidas = [];
  page.on('pageerror', (e) => errores.push('excepción: ' + e.message));
  page.on('console', (m) => {
    if (m.type() === 'error' && !esExterno(m.location().url || BASE)) errores.push('consola: ' + m.text());
  });
  page.on('response', (r) => {
    if (r.status() >= 400 && !esExterno(r.url())) fallas.push(r.status() + ' ' + r.url());
  });
  page.on('request', (r) => { if (r.url().includes('/resenas/fotos/')) fotosPedidas.push(r.url()); });
  await page.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.abort());
  await page.route('**/resenas/fotos/**', (r) => (opciones.fotos === 'fallar'
    ? r.fulfill({ status: 404, contentType: 'text/plain', body: 'no' })
    : r.fulfill({ status: 200, contentType: 'image/jpeg', body: JPEG })));
  await page.route('**/resenas/publicas.json', async (r) => {
    if (opciones.retraso) await new Promise((res) => setTimeout(res, opciones.retraso));
    if (opciones.exportStatus) return r.fulfill({ status: opciones.exportStatus, contentType: 'text/plain', body: 'error' });
    return r.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify(opciones.exportDoc || export_(opciones.export || [])) });
  });
  await page.addInitScript(() => {
    window.__cls = 0;
    try {
      new PerformanceObserver((lista) => {
        lista.getEntries().forEach((e) => { if (!e.hadRecentInput) window.__cls += e.value; });
      }).observe({ type: 'layout-shift', buffered: true });
    } catch (e) { window.__cls = -1; }
  });
  return { context, page, errores, fallas, fotosPedidas };
}

async function cargado(page) {
  await page.goto(BASE + '/opiniones', { waitUntil: 'load' });
  await page.waitForFunction(() => document.getElementById('resenas-listado').getAttribute('data-estado') !== 'cargando',
    null, { timeout: 15000 });
}

function afirmar(condicion, mensaje) {
  if (!condicion) throw new Error(mensaje);
}

const CASOS = [];
function caso(nombre, fn) { CASOS.push({ nombre, fn }); }

caso('indexable: sin noindex, canonical exacto sin querystring, title y description', async (browser) => {
  const { context, page } = await abrir(browser);
  try {
    await cargado(page);
    const robots = await page.locator('meta[name="robots"]').count();
    if (robots) afirmar(!/noindex/.test(await page.locator('meta[name="robots"]').getAttribute('content')), 'meta robots con noindex');
    const canonical = await page.locator('link[rel="canonical"]').getAttribute('href');
    afirmar(canonical === 'https://ork3d.com/opiniones', 'canonical es ' + canonical);
    afirmar(!canonical.includes('?'), 'canonical con querystring');
    afirmar(await page.title() === 'Opiniones de clientes | ORK3D', 'title: ' + await page.title());
    const desc = await page.locator('meta[name="description"]').getAttribute('content');
    afirmar(desc && desc.length >= 80 && desc.length <= 170, 'description de ' + (desc || '').length + ' caracteres');
  } finally { await context.close(); }
});

caso('sitemap incluye /opiniones (desde el servidor de prueba)', async () => {
  const texto = await (await fetch(BASE + '/sitemap.xml')).text();
  afirmar(texto.includes('<loc>https://ork3d.com/opiniones</loc>'), 'sitemap sin /opiniones');
  afirmar(!/<lastmod>/.test(texto.split('/opiniones')[1].split('</url>')[0]), 'lastmod en la entrada de /opiniones');
});

caso('home, torneos, regalos y /opiniones enlazan a /opiniones desde el footer', async (browser) => {
  const { context, page } = await abrir(browser);
  try {
    for (const ruta of ['/', '/torneos', '/regalos-empresariales-fin-de-ano', '/opiniones']) {
      await page.goto(BASE + ruta, { waitUntil: 'load' });
      const enlace = page.locator('footer a[href="/opiniones"]');
      afirmar(await enlace.count() === 1, 'sin enlace en footer de ' + ruta);
      afirmar(await enlace.isVisible(), 'enlace no visible en ' + ruta);
    }
  } finally { await context.close(); }
});

caso('export: solo lo que trae el export; campos internos no aparecen en la página', async (browser) => {
  const interno = { order_ref: 'PEDIDO-SECRETO-999', contact: 'contacto-secreto@ejemplo.test',
    private_feedback: 'FEEDBACK-SECRETO', token_hash: 'c'.repeat(64), status: 'approved', permission_to_publish: true };
  const { context, page, errores } = await abrir(browser, { export: [resena(1, interno), resena(2)] });
  try {
    await cargado(page);
    const texto = await page.locator('main').innerText();
    for (const marca of ['PEDIDO-SECRETO-999', 'contacto-secreto@ejemplo.test', 'FEEDBACK-SECRETO', 'c'.repeat(64)]) {
      afirmar(!texto.includes(marca), 'se muestra un campo interno: ' + marca.slice(0, 12));
    }
    afirmar(await page.locator('#resenas-lista > li').count() === 2, 'cantidad de tarjetas');
    afirmar(errores.length === 0, 'errores: ' + errores.join(' | '));
  } finally { await context.close(); }
});

caso('orden: más recientes primero, sin ranking por calificación', async (browser) => {
  const { context, page } = await abrir(browser, { export: [resena(3, { rating: 5 }), resena(5, { rating: 1 }),
    resena(1, { rating: 5 }), resena(4, { rating: 2 }), resena(2, { rating: 4 })] });
  try {
    await cargado(page);
    const fechas = await page.locator('#resenas-lista time').evaluateAll((els) => els.map((e) => e.getAttribute('datetime')));
    const ordenadas = [...fechas].sort().reverse();
    afirmar(JSON.stringify(fechas) === JSON.stringify(ordenadas), 'orden: ' + fechas.join(', '));
  } finally { await context.close(); }
});

async function promedioDe(browser, ratings) {
  const items = ratings.map((r, i) => resena(i + 1, { rating: r }));
  const { context, page } = await abrir(browser, { export: items });
  try {
    await cargado(page);
    return {
      texto: await page.locator('#resumen-nota .resumen__texto').textContent(),
      etiqueta: await page.locator('#resumen-nota [role="img"]').getAttribute('aria-label'),
    };
  } finally { await context.close(); }
}

caso('promedio y cantidad reales, con coma decimal y una sola cifra decimal', async (browser) => {
  let r = await promedioDe(browser, [5, 4, 3, 4]);
  afirmar(r.texto === '4,0 de 5 · 4 opiniones', 'promedio 4 de 4: ' + r.texto);
  afirmar(r.etiqueta === 'Promedio 4,0 de 5 estrellas', 'etiqueta accesible: ' + r.etiqueta);
  r = await promedioDe(browser, [5, 4]);
  afirmar(r.texto === '4,5 de 5 · 2 opiniones', 'promedio 2 de 2: ' + r.texto);
  r = await promedioDe(browser, [5]);
  afirmar(r.texto === '5,0 de 5 · 1 opinión', 'singular: ' + r.texto);
});

caso('rating inválido no entra al promedio ni a la cantidad', async (browser) => {
  const r = await promedioDe(browser, [5, 'x', null, 9]);
  afirmar(r.texto === '5,0 de 5 · 1 opinión', 'rating inválido contado: ' + r.texto);
});

caso('sin reseñas: estado vacío amigable, sin 0,0 ni resumen', async (browser) => {
  const { context, page } = await abrir(browser, { export: [] });
  try {
    await cargado(page);
    const estado = await page.locator('#resumen-nota').textContent();
    afirmar(/Todavía no hay opiniones publicadas/.test(estado), 'estado vacío: ' + estado);
    afirmar(await page.locator('#resenas-resumen').getAttribute('data-estado') === 'vacio', 'data-estado no es vacio');
    afirmar(!(await page.locator('main').innerText()).includes('0,0'), 'aparece 0,0');
    afirmar(await page.locator('#resenas-lista').isHidden(), 'lista visible sin reseñas');
    afirmar(await page.locator('#resumen-nota .resumen__texto').count() === 0, 'hay promedio sin reseñas');
  } finally { await context.close(); }
});

caso('error del export: mensaje, la página sigue y el título se mantiene', async (browser) => {
  const { context, page, fallas } = await abrir(browser, { exportStatus: 500 });
  try {
    await cargado(page);
    const estado = page.locator('#resumen-nota');
    afirmar(await page.locator('#resenas-resumen').getAttribute('data-estado') === 'error', 'data-estado no es error');
    afirmar(/No pudimos cargar las opiniones/.test(await estado.textContent()), 'mensaje de error');
    afirmar(await page.locator('h1').isVisible(), 'el h1 desapareció');
    afirmar(await page.locator('#resenas-lista').isHidden(), 'la lista quedó visible con error');
    afirmar(await page.locator('footer').isVisible(), 'el footer desapareció');
    afirmar(await page.locator('#resumen-nota').isVisible(), 'el mensaje de error no se ve');
    afirmar(fallas.length === 1 && fallas[0].startsWith('500 '), 'fallas de red: ' + fallas.join(' | '));
  } finally { await context.close(); }
});

caso('estado de carga visible hasta que llega el export', async (browser) => {
  const { context, page } = await abrir(browser, { export: [resena(1)], retraso: 1500 });
  try {
    await page.goto(BASE + '/opiniones', { waitUntil: 'domcontentloaded' });
    const estado = page.locator('#resumen-nota');
    afirmar(await estado.isVisible(), 'el estado de carga no se ve');
    afirmar(/Cargando opiniones/.test(await estado.textContent()), 'texto de carga');
    await page.waitForFunction(() => document.getElementById('resenas-listado').getAttribute('data-estado') !== 'cargando',
      null, { timeout: 15000 });
    afirmar(await page.locator('#resenas-lista > li').count() === 1, 'la tarjeta no llegó');
  } finally { await context.close(); }
});

caso('fotos: lazy, alt útil, y fallback sin hueco roto si la foto no carga', async (browser) => {
  const items = Array.from({ length: 12 }, (_, i) => resena(i + 1, { photo_url: '/resenas/fotos/' + id(i + 1) + '.jpg' }));
  const { context, page, fotosPedidas } = await abrir(browser, { export: items });
  try {
    await cargado(page);
    const atributos = await page.locator('#resenas-lista img').evaluateAll((imgs) => imgs.map((i) => ({
      loading: i.getAttribute('loading'), alt: i.getAttribute('alt') })));
    afirmar(atributos.length === 12, 'imágenes: ' + atributos.length);
    afirmar(atributos.every((a) => a.loading === 'lazy'), 'alguna imagen sin loading=lazy');
    afirmar(atributos.every((a) => a.alt && a.alt.startsWith('Foto enviada por ')), 'alt no útil');
    afirmar(fotosPedidas.length < 12, 'se pidieron todas las fotos antes de hacer scroll: ' + fotosPedidas.length);
  } finally { await context.close(); }
  const r = await abrir(browser, { export: [resena(1, { photo_url: '/resenas/fotos/' + id(1) + '.jpg' })], fotos: 'fallar' });
  try {
    await cargado(r.page);
    await r.page.waitForFunction(() => document.querySelectorAll('#resenas-lista img').length === 0, null, { timeout: 15000 });
    afirmar(await r.page.locator('#resenas-lista li.resena--sin-foto').count() === 1, 'sin marca de fallback');
    afirmar(await r.page.locator('#resenas-lista li.resena').count() === 1, 'la tarjeta se perdió');
  } finally { await r.context.close(); }
});

caso('CLS menor a 0,1 al cargar la página con reseñas y fotos', async (browser) => {
  const items = Array.from({ length: 6 }, (_, i) => resena(i + 1, { photo_url: '/resenas/fotos/' + id(i + 1) + '.jpg' }));
  const { context, page } = await abrir(browser, { export: items });
  try {
    await cargado(page);
    await page.waitForTimeout(800);
    const cls = await page.evaluate(() => window.__cls);
    afirmar(cls >= 0 && cls < 0.1, 'CLS ' + cls);
  } finally { await context.close(); }
});

caso('móvil: sin scroll horizontal en 320, 375, 390, 768 y 1280 px, con textos largos', async (browser) => {
  const palabraLarga = 'a'.repeat(120);
  for (const ancho of ANCHOS) {
    const items = [resena(1, { comment: palabraLarga + ' ' + palabraLarga, product_label: 'Producto ' + palabraLarga,
      display_name: palabraLarga, photo_url: '/resenas/fotos/' + id(1) + '.jpg' }), resena(2)];
    const { context, page } = await abrir(browser, { export: items, ancho });
    try {
      await cargado(page);
      const exceso = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      afirmar(exceso <= 0, `${ancho}px: scroll horizontal de ${exceso}px`);
      const visibles = await page.locator('#resenas-lista > li').evaluateAll((lis) => lis.every((li) => {
        const r = li.getBoundingClientRect(); return r.width > 0 && r.right <= window.innerWidth + 0.5; }));
      afirmar(visibles, `${ancho}px: alguna tarjeta sale del viewport`);
      if (ancho === 375 || ancho === 1280) {
        await page.screenshot({ path: path.join(SHOTS, `opiniones-${ancho}.png`), fullPage: true });
      }
    } finally { await context.close(); }
  }
});

caso('accesibilidad: un h1, landmarks, alt en imágenes, estrellas con texto, idioma', async (browser) => {
  const { context, page } = await abrir(browser, { export: [resena(1, { photo_url: '/resenas/fotos/' + id(1) + '.jpg' }), resena(2)] });
  try {
    await cargado(page);
    afirmar(await page.locator('h1').count() === 1, 'debe haber un solo h1');
    afirmar(await page.locator('html').getAttribute('lang') === 'es', 'idioma');
    for (const [rol, n] of [['banner', 1], ['main', 1], ['contentinfo', 1], ['navigation', 1]]) {
      afirmar(await page.getByRole(rol).count() === n, `landmark ${rol}: ${await page.getByRole(rol).count()}`);
    }
    const sinAlt = await page.locator('img:not([alt]), img[alt=""]').count();
    afirmar(sinAlt === 0, 'imágenes sin alt: ' + sinAlt);
    const estrellas = await page.locator('#resenas-lista [role="img"]').evaluateAll((els) => els.map((e) => e.getAttribute('aria-label')));
    afirmar(estrellas.length === 2 && estrellas.every((t) => /^\d de 5 estrellas$/.test(t)), 'estrellas sin texto: ' + estrellas.join(' | '));
    afirmar(await page.locator('#resenas-lista').getAttribute('aria-label') === 'Opiniones publicadas', 'lista sin nombre');
  } finally { await context.close(); }
});

caso('contraste de texto WCAG AA (4,5:1) en los textos de la página', async (browser) => {
  const { context, page } = await abrir(browser, { export: [resena(1), resena(2)] });
  try {
    await cargado(page);
    const resultados = await page.evaluate(() => {
      function parse(c) {
        const m = c.match(/rgba?\(([^)]+)\)/);
        if (!m) return null;
        const p = m[1].split(',').map((s) => parseFloat(s));
        return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
      }
      function mezclar(fg, bg) {
        return { r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a),
          b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1 };
      }
      function lum(c) {
        const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
        return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
      }
      const base = parse(getComputedStyle(document.body).backgroundColor) || { r: 25, g: 25, b: 25, a: 1 };
      function fondo(el) {
        for (let e = el; e; e = e.parentElement) {
          const c = parse(getComputedStyle(e).backgroundColor);
          if (c && c.a > 0) return c.a < 1 ? mezclar(c, base) : c;
        }
        return base;
      }
      const selectores = ['.opiniones__kicker', '.opiniones__lead', '.opiniones__estado', '.resumen__texto',
        '.resena__producto', '.resena__texto', '.resena__chips li', '.resena__sello', '.resena__autor span',
        '.resena__autor time', '.opiniones__cierre-titulo', 'h1', '.opiniones__listado-titulo'];
      const salida = [];
      for (const sel of selectores) {
        for (const el of document.querySelectorAll(sel)) {
          if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') continue;
          if (!el.textContent.trim()) continue;
          let fg = parse(getComputedStyle(el).color);
          const bg = fondo(el);
          if (fg.a < 1) fg = mezclar(fg, bg);
          const a = lum(fg), b = lum(bg);
          salida.push({ sel, texto: el.textContent.trim().slice(0, 30), ratio: (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) });
        }
      }
      return salida;
    });
    afirmar(resultados.length > 5, 'pocos textos medidos: ' + resultados.length);
    const malos = resultados.filter((r) => r.ratio < 4.5);
    afirmar(malos.length === 0, 'contraste insuficiente: ' + malos.map((m) => `${m.sel} "${m.texto}" ${m.ratio.toFixed(2)}`).join(' | '));
  } finally { await context.close(); }
});

caso('teclado: skip link visible al foco, activable, y foco visible en el footer', async (browser) => {
  const { context, page } = await abrir(browser, { export: [resena(1)] });
  try {
    await cargado(page);
    await page.keyboard.press('Tab');
    const primero = await page.evaluate(() => document.activeElement.textContent.trim());
    afirmar(primero === 'Saltar al contenido', 'primer foco: ' + primero);
    const caja = await page.locator('.sr-only--foco').boundingBox();
    afirmar(caja && caja.width > 0 && caja.height > 0, 'el skip link no se ve al recibir foco');
    await page.keyboard.press('Enter');
    afirmar(await page.evaluate(() => location.hash) === '#contenido', 'el skip link no lleva a #contenido');
    let llego = false;
    for (let i = 0; i < 60 && !llego; i++) {
      await page.keyboard.press('Tab');
      llego = await page.evaluate(() => {
        const a = document.activeElement;
        return a.tagName === 'A' && a.getAttribute('href') === '/opiniones' && a.closest('footer') !== null;
      });
    }
    afirmar(llego, 'el teclado no llega al enlace del footer');
    afirmar(await page.evaluate(() => document.activeElement.matches(':focus-visible')), 'foco no visible en el footer');
  } finally { await context.close(); }
});

caso('enlaces internos de la página resuelven sin 404', async (browser) => {
  const { context, page } = await abrir(browser, { export: [resena(1)] });
  try {
    await cargado(page);
    const rutas = await page.locator('a[href^="/"]').evaluateAll((as) => [...new Set(as.map((a) => a.getAttribute('href').split('#')[0] || '/'))]);
    for (const ruta of rutas) {
      const r = await page.request.get(BASE + ruta);
      afirmar(r.status() < 400, `${ruta} → ${r.status()}`);
    }
  } finally { await context.close(); }
});

caso('consola y red limpias en la página con reseñas y fotos', async (browser) => {
  const { context, page, errores, fallas } = await abrir(browser, { export: [resena(1, { photo_url: '/resenas/fotos/' + id(1) + '.jpg' }), resena(2)] });
  try {
    await cargado(page);
    await page.waitForTimeout(500);
    afirmar(errores.length === 0, 'consola/excepciones: ' + errores.join(' | '));
    afirmar(fallas.length === 0, 'respuestas de error: ' + fallas.join(' | '));
  } finally { await context.close(); }
});

caso('resize: de escritorio a móvil no deja scroll horizontal', async (browser) => {
  const { context, page } = await abrir(browser, { export: [resena(1, { comment: 'x'.repeat(200) }), resena(2)] });
  try {
    await cargado(page);
    await page.setViewportSize({ width: 375, height: 800 });
    await page.waitForTimeout(200);
    const exceso = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    afirmar(exceso <= 0, 'tras redimensionar: ' + exceso + 'px');
  } finally { await context.close(); }
});

(async () => {
  const servidor = await arrancarServidor();
  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
  let fallos = 0;
  try {
    for (const { nombre, fn } of CASOS) {
      try {
        await fn(browser);
        console.log('PASS  ' + nombre);
      } catch (e) {
        fallos++;
        console.log('FAIL  ' + nombre + ': ' + e.message);
      }
    }
  } finally {
    await browser.close();
    servidor.kill();
  }
  console.log(`${CASOS.length - fallos}/${CASOS.length} casos OK · capturas en ${path.relative(ROOT, SHOTS) || SHOTS}`);
  process.exit(fallos ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
