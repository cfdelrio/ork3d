#!/usr/bin/env node
/* E2E de la landing evergreen /reconocimientos-empresariales, en Chromium real contra el sitio del repo servido por dev/serve.py.
 *
 * Qué prueba (lo que no cubre caso-real.e2e.js: video, orden de lectura y responsive del caso):
 *   - la página existe con URL limpia, title, description, canonical y H1 únicos, indexable;
 *   - está en el sitemap con el mismo formato que el resto de las páginas;
 *   - enlaces entrantes: la home y la landing de regalos apuntan a ella, y ella vuelve a la de regalos;
 *   - las preguntas frecuentes visibles coinciden con su JSON-LD;
 *   - no aparecen nombres de personas, montos ni marca del cliente en el texto, los alt, las etiquetas ni el JSON-LD;
 *   - la foto nueva usa los formatos optimizados y no hay MP4 duplicados en el repo.
 *
 * Uso (desde la raíz del repo):  SHOTS_DIR=<carpeta para capturas> node dev/e2e/reconocimientos.e2e.js
 * Variables opcionales: PORT (8801 por defecto), CHROME_PATH, PLAYWRIGHT_PATH.
 */
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const PORT = Number(process.env.PORT || 8801);
const BASE = `http://127.0.0.1:${PORT}`;
const RUTA = '/reconocimientos-empresariales';
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined);
// Lo que la página no debe repetir en texto, alt, etiquetas ni datos estructurados (privacidad del proyecto real).
const DATOS_SENSIBLES = /Escobar|Gonzalo|Emprendimiento|PROHIT|\bHIT\b|50k|\$\s?50|SOY CAPAZ/i;

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

async function abrir(browser, ruta, ancho = 1440) {
  const ctx = await browser.newContext({ viewport: { width: ancho, height: 900 }, locale: 'es-AR' });
  const page = await ctx.newPage();
  const errores = [];
  page.on('pageerror', (e) => errores.push('pageerror: ' + e.message));
  page.on('console', (m) => {
    if (m.type() === 'error' && (m.location().url || BASE).startsWith(BASE)) errores.push('consola: ' + m.text());
  });
  await page.goto(BASE + ruta, { waitUntil: 'load' });
  return { ctx, page, errores };
}

caso('la página existe con URL limpia, title, description, canonical y H1 únicos, e indexable', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, RUTA);
  try {
    const r = await page.request.get(BASE + RUTA);
    afirmar(r.status() === 200, `${RUTA} responde ${r.status()}`);
    afirmar((await page.title()) === 'Reconocimientos empresariales personalizados | ORK3D', `title: ${await page.title()}`);
    const description = await page.locator('meta[name="description"]').getAttribute('content');
    afirmar(/reconocimientos empresariales personalizados/i.test(description), 'la description debe nombrar la intención');
    afirmar(description.length <= 160, `description de ${description.length} caracteres`);
    afirmar((await page.locator('link[rel="canonical"]').getAttribute('href')) === 'https://ork3d.com/reconocimientos-empresariales',
      'canonical incorrecto');
    afirmar((await page.locator('meta[name="robots"]').count()) === 0
      || !/noindex/i.test(await page.locator('meta[name="robots"]').getAttribute('content')), 'la página no debe ser noindex');
    afirmar(await page.locator('h1').count() === 1, 'debe haber un único H1');
    afirmar((await page.locator('h1').innerText()).trim() === 'Reconocimientos diseñados para representar logros de verdad.', 'texto del H1');
    afirmar((await page.locator('.hero__badge').innerText()).trim() === 'RECONOCIMIENTOS EMPRESARIALES PERSONALIZADOS', 'texto del eyebrow');
    afirmar(await page.locator('h2').count() >= 4, 'la página debe tener secciones con H2');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('está en el sitemap con el formato del resto de las páginas', async () => {
  const xml = fs.readFileSync(path.join(ROOT, 'sitemap.xml'), 'utf-8');
  const bloque = xml.match(/<url>\s*<loc>https:\/\/ork3d\.com\/reconocimientos-empresariales<\/loc>[\s\S]*?<\/url>/);
  afirmar(bloque, 'falta la entrada en sitemap.xml');
  afirmar(/<changefreq>monthly<\/changefreq>/.test(bloque[0]) && /<priority>0\.8<\/priority>/.test(bloque[0]),
    'la entrada del sitemap debe tener changefreq y priority como las demás');
});

caso('la home y la landing de regalos enlazan a la página; ella vuelve a la de regalos', async (browser) => {
  for (const ruta of ['/', '/regalos-empresariales-fin-de-ano.html']) {
    const { ctx, page, errores } = await abrir(browser, ruta);
    try {
      const enlaces = await page.locator(`a[href="${RUTA}"]`).count();
      afirmar(enlaces >= 1, `${ruta} no enlaza ${RUTA}`);
      afirmar(errores.length === 0, errores.join(' | '));
    } finally {
      await ctx.close();
    }
  }
  const { ctx, page, errores } = await abrir(browser, RUTA);
  try {
    afirmar(await page.locator('a[href="/regalos-empresariales-fin-de-ano"]').count() >= 1,
      'la página evergreen debe enlazar a la landing de regalos');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('las preguntas frecuentes visibles coinciden con su JSON-LD', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, RUTA);
  try {
    const datos = await page.evaluate(() => {
      const ld = [...document.querySelectorAll('script[type="application/ld+json"]')].map((s) => JSON.parse(s.textContent))
        .find((j) => j['@type'] === 'FAQPage');
      const visibles = [...document.querySelectorAll('#faq .faq__item')].map((d) => ({
        pregunta: d.querySelector('.faq__question').textContent.replace(/\s+/g, ' ').trim(),
        respuesta: d.querySelector('.faq__answer p').textContent.replace(/\s+/g, ' ').trim(),
      }));
      return { jsonld: ld.mainEntity.map((q) => ({ pregunta: q.name, respuesta: q.acceptedAnswer.text })), visibles };
    });
    afirmar(JSON.stringify(datos.jsonld) === JSON.stringify(datos.visibles), 'el FAQ visible y el JSON-LD no coinciden');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('no aparecen nombres, montos ni marca del cliente en texto, alt, etiquetas ni datos estructurados', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, RUTA);
  try {
    const textos = await page.evaluate(() => [
      document.body.innerText,
      document.title,
      document.querySelector('meta[name="description"]').content,
      ...[...document.querySelectorAll('[alt], [aria-label]')].flatMap((e) => [e.getAttribute('alt'), e.getAttribute('aria-label')]),
      ...[...document.querySelectorAll('script[type="application/ld+json"]')].map((s) => s.textContent),
    ].filter(Boolean).join(' | '));
    const hallazgo = textos.match(DATOS_SENSIBLES);
    afirmar(!hallazgo, `dato sensible en la página: ${hallazgo}`);
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('la foto usa los formatos optimizados y el repo tiene un solo MP4 del caso', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, RUTA);
  try {
    const srcs = await page.evaluate(() => [...document.querySelectorAll('.caso-real__foto source')].map((s) => s.getAttribute('srcset')));
    afirmar(srcs.length === 2, `la foto debe tener AVIF y WebP, hay ${srcs.length} fuentes`);
    afirmar(srcs.every((s) => s.includes('480w') && s.includes('800w')), 'srcset con 480 y 800 px');
    const img = await page.locator('.caso-real__foto img').evaluate((i) => ({ w: i.getAttribute('width'), h: i.getAttribute('height'), loading: i.loading }));
    afirmar(img.w === '800' && img.h === '1067' && img.loading === 'lazy', `dimensiones y carga diferida de la foto: ${JSON.stringify(img)}`);
    const mp4 = [];
    const recorrer = (d) => fs.readdirSync(d, { withFileTypes: true }).forEach((e) => {
      const p = path.join(d, e.name);
      if (e.isDirectory()) recorrer(p); else if (e.name.endsWith('.mp4')) mp4.push(path.relative(ROOT, p));
    });
    recorrer(path.join(ROOT, 'assets'));
    afirmar(mp4.length === 1, `MP4 en assets: ${mp4.join(', ')}`);
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
  console.log(`\nReconocimientos E2E: ${casos.length - fallos} PASS, ${fallos} FAIL`);
  process.exit(fallos ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
