#!/usr/bin/env node
/* E2E del hero de la landing de regalos empresariales, en Chromium real contra el sitio del repo servido por dev/serve.py.
 *
 * Qué prueba:
 *   - textos del hero (eyebrow, H1 único, descripción, complemento, beneficios, microcopy);
 *   - los dos CTA (principal "Ver ideas de regalos" → #regalos; secundario "Solicitar propuesta personalizada" → #cotizacion)
 *     y la barra fija de móvil, con su evento de medición;
 *   - en 375, 390, 430, 768, 1024 y 1440 px no hay desborde horizontal, las fotos del hero cargan y el CTA principal
 *     queda visible en el primer pantallazo de móvil;
 *   - contraste AA del texto del hero, calculado sobre los fondos reales (lila, crema y negro);
 *   - el botón flotante de WhatsApp no aparece en móvil (la barra fija cubre el contacto) y sí en escritorio;
 *   - la transición lila → crema existe al pie del hero.
 *
 * Uso (desde la raíz del repo):  SHOTS_DIR=<carpeta para capturas> node dev/e2e/regalos-hero.e2e.js
 * Variables opcionales: PORT (8808), CHROME_PATH, PLAYWRIGHT_PATH.
 */
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const PORT = Number(process.env.PORT || 8808);
const BASE = `http://127.0.0.1:${PORT}`;
const PAGINA = '/regalos-empresariales-fin-de-ano.html';
const SHOTS = process.env.SHOTS_DIR || path.join(ROOT, 'dev', 'e2e', 'capturas');
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined);
const VISTAS = [[375, 667], [390, 844], [430, 932], [768, 1024], [1024, 768], [1440, 900]];

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

function esExterno(url) {
  return !url.startsWith(BASE);
}

async function abrir(browser, ancho, alto = 800) {
  const ctx = await browser.newContext({ viewport: { width: ancho, height: alto }, locale: 'es-AR' });
  const page = await ctx.newPage();
  const errores = [];
  page.on('pageerror', (e) => errores.push('pageerror: ' + e.message));
  page.on('console', (m) => {
    if (m.type() === 'error' && !esExterno((m.location() && m.location().url) || BASE)) errores.push('consola: ' + m.text());
  });
  await page.goto(BASE + PAGINA, { waitUntil: 'load' });
  await page.waitForTimeout(400);
  return { ctx, page, errores };
}

// Contraste WCAG 2.x entre el color de texto (con alfa) y el fondo efectivo del elemento.
const contrasteEnPagina = (page, selector) => page.evaluate((sel) => {
  const parse = (c) => {
    const m = c.match(/rgba?\(([^)]+)\)/);
    const v = m[1].split(',').map((x) => parseFloat(x));
    return { r: v[0], g: v[1], b: v[2], a: v.length > 3 ? v[3] : 1 };
  };
  const mezclar = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a), b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1,
  });
  const lum = (c) => {
    const f = (x) => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const el = document.querySelector(sel);
  let fondo = null;
  for (let n = el; n && !fondo; n = n.parentElement) {
    const bg = parse(getComputedStyle(n).backgroundColor);
    if (bg.a > 0) fondo = bg;
  }
  const texto = mezclar(parse(getComputedStyle(el).color), fondo);
  const [l1, l2] = [lum(texto), lum(fondo)].sort((a, b) => b - a);
  return (l1 + 0.05) / (l2 + 0.05);
}, selector);

caso('textos del hero: eyebrow, H1 único, descripción, complemento, beneficios y microcopy', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, 1440, 900);
  try {
    afirmar(await page.locator('h1').count() === 1, 'debe haber un único H1');
    afirmar((await page.locator('.hero__badge').innerText()).trim() === 'FIN DE AÑO 2026 · REGALOS PARA EMPRESAS', 'eyebrow');
    afirmar((await page.locator('h1').innerText()).replace(/\s+/g, ' ').trim()
      === 'Una misma identidad. Un regalo único para cada persona.', 'H1');
    afirmar((await page.locator('.hero__sub').innerText()).trim()
      === 'Sorprendé a tu equipo con regalos que combinan la identidad de tu empresa con un detalle personalizado para cada integrante.', 'descripción');
    afirmar((await page.locator('.hero__pitch').innerText()).trim()
      === 'Te respondemos rápido y te ayudamos a elegir la cantidad, desde una pieza hasta una serie.', 'complemento');
    const beneficios = await page.locator('.hero__beneficios li').allInnerTexts();
    afirmar(JSON.stringify(beneficios) === JSON.stringify(['Respuesta rápida', 'Sin grandes pedidos mínimos', 'Personalización individual']),
      `beneficios: ${JSON.stringify(beneficios)}`);
    afirmar((await page.locator('.hero__microcopy').innerText()).trim() === 'Diseñado y fabricado en Buenos Aires.', 'microcopy');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('los CTA del hero apuntan a su destino y tienen su evento de medición', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, 1440, 900);
  try {
    const principal = page.locator('.hero__ctas a').first();
    const secundario = page.locator('.hero__ctas a').nth(1);
    afirmar((await principal.innerText()).replace(/\s+/g, ' ').trim() === 'Ver ideas de regalos →', 'texto del CTA principal');
    afirmar((await principal.getAttribute('href')) === '#regalos', 'el CTA principal debe ir a #regalos');
    afirmar((await principal.getAttribute('data-track-source')) === 'ver_ideas_regalos', 'evento del CTA principal');
    afirmar((await secundario.innerText()).trim() === 'Solicitar propuesta personalizada', 'texto del CTA secundario');
    afirmar((await secundario.getAttribute('href')) === '#cotizacion', 'el CTA secundario debe ir a #cotizacion');
    afirmar((await secundario.getAttribute('data-track-source')) === 'solicitar_propuesta_personalizada', 'evento del CTA secundario');
    const sticky = page.locator('#stickyCta a');
    afirmar((await sticky.innerText()).trim() === 'Solicitar propuesta', 'texto de la barra fija');
    afirmar((await sticky.getAttribute('href')) === '#cotizacion', 'la barra fija debe ir a #cotizacion');
    afirmar((await page.locator('#regalos').count()) === 1 && (await page.locator('#cotizacion').count()) === 1, 'faltan los destinos');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('el CTA principal navega a las ideas de regalos', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, 1440, 900);
  try {
    await page.locator('.hero__ctas a').first().click();
    await page.waitForFunction(() => location.hash === '#regalos');
    afirmar(await page.locator('#regalos').isVisible(), 'la sección de ideas debe quedar visible');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

for (const [ancho, alto] of VISTAS) {
  caso(`sin desborde horizontal, con fotos cargadas y bien proporcionadas (${ancho} px)`, async (browser) => {
    const { ctx, page, errores } = await abrir(browser, ancho, alto);
    try {
      const d = await page.evaluate(() => ({ visible: document.documentElement.clientWidth, total: document.documentElement.scrollWidth }));
      afirmar(d.total <= d.visible, `desborde horizontal de ${d.total - d.visible} px a ${ancho} px`);
      const fotos = await page.evaluate(() => [...document.querySelectorAll('.hero__pieza img')].map((i) => ({
        ok: i.complete && i.naturalWidth > 0, alt: i.alt, proporcion: i.getBoundingClientRect().width / i.getBoundingClientRect().height,
      })));
      afirmar(fotos.length === 3, `se esperaban 3 fotos, hay ${fotos.length}`);
      for (const f of fotos) {
        afirmar(f.ok, 'una foto del hero no cargó');
        afirmar(f.alt.length > 0, 'una foto del hero no tiene texto alternativo');
      }
      if (ancho === 390) {
        const cta = await page.locator('.hero__ctas a').first().boundingBox();
        afirmar(cta.y + cta.height <= alto, `el CTA principal no entra en el primer pantallazo a ${ancho}x${alto}`);
      }
      fs.mkdirSync(SHOTS, { recursive: true });
      await page.screenshot({ path: path.join(SHOTS, `regalos-hero-${ancho}.png`) });
      afirmar(errores.length === 0, errores.join(' | '));
    } finally {
      await ctx.close();
    }
  });
}

caso('el contraste de los textos del hero cumple AA (4,5:1 o más)', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, 390, 844);
  try {
    const elementos = ['.hero__badge', 'h1', '.hero__title-segunda', '.hero__sub', '.hero__pitch', '.hero__beneficios li', '.hero__microcopy', '.hero__ctas a.btn--primary', '.hero__ctas a.btn--ghost'];
    const malos = [];
    for (const sel of elementos) {
      const c = await contrasteEnPagina(page, sel);
      if (c < 4.5) malos.push(`${sel} ${c.toFixed(2)}:1`);
    }
    afirmar(malos.length === 0, `contraste insuficiente: ${malos.join(', ')}`);
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('el botón flotante de WhatsApp: oculto en móvil, visible en escritorio, con su fuente de medición', async (browser) => {
  for (const [ancho, alto, visible] of [[390, 844, false], [1440, 900, true]]) {
    const { ctx, page, errores } = await abrir(browser, ancho, alto);
    try {
      const mostrado = await page.locator('.wa-fab').evaluate((e) => getComputedStyle(e).display !== 'none');
      afirmar(mostrado === visible, `botón flotante a ${ancho} px: visible=${mostrado}, se esperaba ${visible}`);
      afirmar((await page.locator('.wa-fab').getAttribute('data-wa-source')) === 'contacto_flotante', 'fuente de medición del botón flotante');
      afirmar(errores.length === 0, errores.join(' | '));
    } finally {
      await ctx.close();
    }
  }
});

caso('el hero termina en una transición crema que se une a la sección siguiente', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, 1440, 900);
  try {
    const transicion = await page.evaluate(() => {
      const s = getComputedStyle(document.querySelector('#hero'), '::after');
      return { contenido: s.content, fondo: s.backgroundColor, radio: s.borderTopLeftRadius };
    });
    afirmar(transicion.contenido !== 'none' && transicion.fondo === 'rgb(243, 240, 233)', `transición: ${JSON.stringify(transicion)}`);
    afirmar((await page.locator('#concepto').evaluate((e) => getComputedStyle(e).backgroundColor)) === 'rgb(243, 240, 233)',
      'la sección siguiente debe ser crema');
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
  console.log(`\nHero de regalos E2E: ${casos.length - fallos} PASS, ${fallos} FAIL`);
  process.exit(fallos ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
