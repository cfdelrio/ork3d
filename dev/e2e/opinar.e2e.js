#!/usr/bin/env node
/* E2E de /opinar/<token> contra dev/serve.py (API real de app.py + S3 falso).
 *
 * Uso (desde la raíz del repo):
 *   SHOTS_DIR=<carpeta para capturas> node dev/e2e/opinar.e2e.js
 *
 * Variables opcionales: PORT (8799), CHROME_PATH (si no, el Chromium de Playwright),
 * PLAYWRIGHT_PATH (si Playwright no está en el NODE_PATH).
 */
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const PORT = Number(process.env.PORT || 8799);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR || path.join(ROOT, 'dev', 'e2e', 'capturas');
// Chromium: CHROME_PATH si se define; si no, el de la sandbox; si no existe, el que trae Playwright.
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined);

fs.mkdirSync(SHOTS, { recursive: true });

function arrancarServidor() {
  return new Promise((resolver, rechazar) => {
    const proc = spawn('python3', [path.join(ROOT, 'dev', 'serve.py'), String(PORT)], { cwd: ROOT });
    let salida = '';
    proc.stdout.on('data', (d) => {
      salida += d;
      if (salida.includes('ORK3D dev')) resolver(proc);
    });
    proc.stderr.on('data', (d) => { salida += d; });
    proc.on('exit', (code) => rechazar(new Error(`dev/serve.py salió con ${code}: ${salida}`)));
  });
}

function esperar(condicion, mensaje) {
  if (!condicion) throw new Error(mensaje);
}

async function getJson(ruta) {
  const r = await fetch(BASE + ruta);
  return r.json();
}

async function abrir(browser, token, { ancho = 390, sinApi = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width: ancho, height: 844 }, locale: 'es-AR' });
  const page = await ctx.newPage();
  const sesion = { page, ctx, errores: [], externos: [], posts: 0, uploads: [] };
  page.on('pageerror', (e) => sesion.errores.push(`pageerror: ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error') sesion.errores.push(`console: ${m.text()}`); });
  page.on('request', (r) => {
    // blob: es la vista previa local de la foto; no sale de la página.
    if (!r.url().startsWith(BASE) && !r.url().startsWith('blob:')) sesion.externos.push(r.url());
    if (r.method() === 'POST' && r.url().endsWith('/api/resenas/resenas')) sesion.posts++;
  });
  page.on('response', (r) => { if (r.url().includes('/s3-fake/')) sesion.uploads.push(r.status()); });
  if (sinApi) {
    await page.route('**/config.js', (route) => route.fulfill({
      contentType: 'application/javascript',
      body: 'window.ORK3D_CONFIG = { WHATSAPP_NUMBER: "5491132543998", RESENAS_API_URL: "", GOOGLE_REVIEW_URL: "" };',
    }));
  }
  await page.goto(`${BASE}/opinar/${token}`);
  return sesion;
}

async function cerrar(sesion) {
  await sesion.ctx.close();
}

function sinErroresNiExternos(sesion, permitidos = []) {
  const inesperados = sesion.errores.filter((e) => !permitidos.some((re) => re.test(e)));
  esperar(inesperados.length === 0, `errores de consola o de página: ${inesperados.join(' | ')}`);
  esperar(sesion.externos.length === 0, `pedidos a terceros: ${sesion.externos.join(' | ')}`);
}

// El título del estado va en el h1 y la explicación en #estado.
async function estadoConTexto(page, titulo) {
  await page.locator('h1').filter({ hasText: titulo }).waitFor({ state: 'visible', timeout: 8000 });
  await page.locator('#estado').waitFor({ state: 'visible' });
  esperar(await page.locator('#formulario').isHidden(), 'el formulario no debería verse en este estado');
}

async function pngDeGradiente(page) {
  const dataUrl = await page.evaluate(() => {
    const c = document.createElement('canvas');
    c.width = 1200;
    c.height = 900;
    const x = c.getContext('2d');
    const img = x.createImageData(1200, 900);
    for (let y = 0; y < 900; y++) {
      for (let i = 0; i < 1200; i++) {
        const k = (y * 1200 + i) * 4;
        img.data[k] = (i / 1200) * 255 + Math.random() * 20;
        img.data[k + 1] = (y / 900) * 255 + Math.random() * 20;
        img.data[k + 2] = 180 + Math.random() * 20;
        img.data[k + 3] = 255;
      }
    }
    x.putImageData(img, 0, 0);
    return c.toDataURL('image/png');
  });
  return Buffer.from(dataUrl.split(',')[1], 'base64');
}

async function resenaCon(texto) {
  const estado = await getJson('/dev/state');
  return estado.resenas.find((r) => (r.comment || '').includes(texto)) || null;
}

async function llenarMinimo(page, { estrellas = 5, nombre = 'Ana Gómez' } = {}) {
  await page.locator(`label[for="rating-${estrellas}"]`).click();
  if (nombre !== null) await page.fill('#nombre', nombre);
  await page.locator('#permiso').check();
}

const casos = [];
function caso(nombre, fn) { casos.push({ nombre, fn }); }

async function main() {
  const servidor = await arrancarServidor();
  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
  const fallidos = [];
  try {
    const tokens = await getJson('/dev/tokens');

    // ── Estados del enlace ────────────────────────────────────────────
    const estados = [
      ['token inválido', 'A'.repeat(32), 'Este enlace no es válido'],
      ['sin token en la ruta', '', 'Este enlace no es válido'],
      ['token vencido', () => tokens.vencido, 'Este enlace venció'],
      ['token revocado', () => tokens.revocado, 'Este enlace ya no está disponible'],
      ['token ya usado', () => tokens.usado, 'Ya recibimos tu opinión'],
    ];
    for (const [nombre, tok, texto] of estados) {
      caso(`estado: ${nombre}`, async (browser) => {
        const sesion = await abrir(browser, typeof tok === 'function' ? tok() : tok);
        try {
          await estadoConTexto(sesion.page, texto);
          sinErroresNiExternos(sesion);
          if (nombre === 'token ya usado') {
            await sesion.page.screenshot({ path: path.join(SHOTS, 'estado-usado-390.png'), fullPage: true });
          }
        } finally { await cerrar(sesion); }
      });
    }

    caso('estado: sin API configurada muestra error y no carga el formulario', async (browser) => {
      const sesion = await abrir(browser, tokens.ok1, { sinApi: true });
      try {
        await estadoConTexto(sesion.page, 'No pudimos verificar tu enlace');
        sinErroresNiExternos(sesion);
      } finally { await cerrar(sesion); }
    });

    caso('formulario: enlace válido muestra producto y vencimiento', async (browser) => {
      const sesion = await abrir(browser, tokens.ok1);
      try {
        await sesion.page.locator('#formulario').waitFor({ state: 'visible' });
        const texto = await sesion.page.locator('#producto').innerText();
        esperar(texto.includes('Trofeo acrílico'), `producto: ${texto}`);
        esperar(texto.includes('hasta el'), `falta el vencimiento: ${texto}`);
        esperar((await sesion.page.locator('h1').innerText()) === 'Contanos cómo te llegó', 'título del formulario');
        sinErroresNiExternos(sesion);
      } finally { await cerrar(sesion); }
    });

    // ── Validación en el navegador (el servidor valida igual) ─────────
    caso('validación: enviar vacío marca los errores y no hace POST', async (browser) => {
      const sesion = await abrir(browser, tokens.ok1);
      try {
        await sesion.page.locator('#formulario').waitFor({ state: 'visible' });
        await sesion.page.click('#enviar');
        esperar(await sesion.page.locator('#err-rating').isVisible(), 'falta el error de estrellas');
        esperar(await sesion.page.locator('#err-name').isVisible(), 'falta el error de nombre');
        esperar(await sesion.page.locator('#err-permission_to_publish').isVisible(), 'falta el error de permiso');
        esperar(sesion.posts === 0, 'no debería haber POST con errores de validación');
        const enfocado = await sesion.page.evaluate(() => document.activeElement && document.activeElement.id);
        esperar(enfocado === 'rating-5', `el foco debería ir al primer error, fue ${enfocado}`);
        await sesion.page.screenshot({ path: path.join(SHOTS, 'errores-390.png'), fullPage: true });
        sinErroresNiExternos(sesion);
      } finally { await cerrar(sesion); }
    });

    // ── Camino feliz: una estrella, sin foto; el CTA de Google aparece igual ─
    caso('envío: 1 estrella sin foto queda pendiente, sin nombre real, y muestra el CTA de Google', async (browser) => {
      const sesion = await abrir(browser, tokens.ok2);
      try {
        await sesion.page.locator('#formulario').waitFor({ state: 'visible' });
        await llenarMinimo(sesion.page, { estrellas: 1, nombre: 'Ana Gómez' });
        await sesion.page.locator('label.chip', { hasText: 'Calidad' }).click();
        await sesion.page.fill('#comentario', 'Llegó roto, lo cambiaron rápido. frase-feliz-1');
        await sesion.page.click('#enviar');
        await sesion.page.locator('#gracias').waitFor({ state: 'visible', timeout: 8000 });
        esperar((await sesion.page.locator('h1').innerText()) === 'Gracias por tu opinión', 'título de agradecimiento');
        const cta = sesion.page.locator('#google-cta');
        esperar(await cta.isVisible(), 'el CTA de Google debería verse incluso con 1 estrella');
        esperar((await cta.getAttribute('href')) === 'https://g.page/r/dev-ork3d/review', 'href del CTA');
        const r = await resenaCon('frase-feliz-1');
        esperar(r, 'la reseña no quedó guardada');
        esperar(r.status === 'pending' && r.rating === 1, `estado/rating: ${r.status}/${r.rating}`);
        esperar(r.display_name === 'Ana G.', `nombre mostrado: ${r.display_name}`);
        esperar(!('name' in r), 'el nombre real no debe guardarse');
        esperar(JSON.stringify(r.aspects) === '["calidad"]', `aspectos: ${JSON.stringify(r.aspects)}`);
        esperar(r.permission_to_publish === true, 'falta el permiso');
        esperar(r.photo_key === null, 'no se subió foto');
        sinErroresNiExternos(sesion);
        await sesion.page.screenshot({ path: path.join(SHOTS, 'gracias-390.png'), fullPage: true });
      } finally { await cerrar(sesion); }

      // Después del envío el mismo enlace ya no sirve.
      const otra = await abrir(browser, tokens.ok2);
      try {
        await estadoConTexto(otra.page, 'Ya recibimos tu opinión');
      } finally { await cerrar(otra); }
    });

    // ── Foto: subida firmada, validada y sin metadatos ───────────────────
    caso('foto: se sube firmada, el servidor la acepta como JPEG y queda en pendientes', async (browser) => {
      const sesion = await abrir(browser, tokens.ok3);
      try {
        await sesion.page.locator('#formulario').waitFor({ state: 'visible' });
        const png = await pngDeGradiente(sesion.page);
        await sesion.page.setInputFiles('#foto', { name: 'pedido.png', mimeType: 'image/png', buffer: png });
        await sesion.page.locator('#foto-estado').filter({ hasText: 'Foto lista.' }).waitFor({ timeout: 15000 });
        esperar(await sesion.page.locator('#foto-preview').isVisible(), 'falta la vista previa');
        esperar(sesion.uploads.length === 1 && sesion.uploads[0] === 204, `subidas: ${JSON.stringify(sesion.uploads)}`);
        await llenarMinimo(sesion.page, { estrellas: 5, nombre: 'Bruno Pérez' });
        await sesion.page.fill('#comentario', 'Se ve igual que en la foto. frase-foto-3');
        await sesion.page.click('#enviar');
        await sesion.page.locator('#gracias').waitFor({ state: 'visible', timeout: 8000 });
        const r = await resenaCon('frase-foto-3');
        esperar(r && r.photo_key && r.photo_key.startsWith('pendientes/'), `photo_key: ${r && r.photo_key}`);
        esperar(r.foto_cabecera === 'ffd8ff', `la foto guardada no es JPEG: ${r.foto_cabecera}`);
        esperar(r.display_name === 'Bruno P.', `nombre mostrado: ${r.display_name}`);
        sinErroresNiExternos(sesion);
      } finally { await cerrar(sesion); }
    });

    // ── Modo anónimo, contacto obligatorio y quitar foto ─────────────────
    caso('anónimo: sin nombre, contacto obligatorio si se pide, y quitar foto', async (browser) => {
      const sesion = await abrir(browser, tokens.ok4);
      try {
        const p = sesion.page;
        await p.locator('#formulario').waitFor({ state: 'visible' });
        const png = await pngDeGradiente(p);
        await p.setInputFiles('#foto', { name: 'pedido.png', mimeType: 'image/png', buffer: png });
        await p.locator('#foto-estado').filter({ hasText: 'Foto lista.' }).waitFor({ timeout: 15000 });
        await p.locator('#foto-quitar').click();
        esperar(await p.locator('#foto-bloque').isHidden(), 'la vista previa debería quitarse');
        esperar((await p.locator('#foto').inputValue()) === '', 'el input de foto debería vaciarse');
        await p.locator('label.opcion', { hasText: 'Anónimo' }).click();
        esperar(await p.locator('#bloque-nombre').isHidden(), 'con anónimo no se pide nombre');
        await p.locator('label[for="rating-3"]').click();
        await p.locator('#permiso').check();
        await p.locator('#pide-contacto').check();
        await p.click('#enviar');
        esperar(await p.locator('#err-contact').isVisible(), 'falta el error de contacto');
        esperar(sesion.posts === 0, 'no debería enviarse sin contacto');
        await p.fill('#contacto', 'ana@example.com');
        await p.fill('#comentario', 'Todo bien, gracias. frase-anon-4');
        await p.click('#enviar');
        await p.locator('#gracias').waitFor({ state: 'visible', timeout: 8000 });
        const r = await resenaCon('frase-anon-4');
        esperar(r && r.display_mode === 'anonimo', `modo: ${r && r.display_mode}`);
        esperar(r.display_name === 'Cliente de ORK3D', `nombre mostrado: ${r.display_name}`);
        esperar(r.contact === 'ana@example.com' && r.contact_requested === true, 'contacto guardado');
        esperar(r.photo_key === null, 'la foto quitada no debería enviarse');
        sinErroresNiExternos(sesion);
      } finally { await cerrar(sesion); }
    });

    // ── Campo trampa: un bot recibe la misma respuesta, sin guardar ──────
    caso('honeypot: un bot recibe la confirmación pero no se guarda nada', async (browser) => {
      const antes = (await getJson('/dev/state')).resenas.length;
      const sesion = await abrir(browser, tokens.ok5);
      try {
        const p = sesion.page;
        await p.locator('#formulario').waitFor({ state: 'visible' });
        await llenarMinimo(p, { estrellas: 4, nombre: 'Carla Ruiz' });
        await p.evaluate(() => { document.querySelector('input[name=website]').value = 'http://spam.example'; });
        await p.click('#enviar');
        await p.locator('#gracias').waitFor({ state: 'visible', timeout: 8000 });
        const despues = (await getJson('/dev/state')).resenas.length;
        esperar(despues === antes, `no debería guardarse nada: ${antes} → ${despues}`);
        await p.reload();
        await p.locator('#formulario').waitFor({ state: 'visible' });
        sinErroresNiExternos(sesion);
      } finally { await cerrar(sesion); }
    });

    // ── Doble envío desde dos pestañas: solo una gana ────────────────────
    caso('doble envío: la segunda pestaña recibe "ya recibimos tu opinión"', async (browser) => {
      const a = await abrir(browser, tokens.ok6);
      const b = await abrir(browser, tokens.ok6);
      try {
        for (const s of [a, b]) await s.page.locator('#formulario').waitFor({ state: 'visible' });
        await llenarMinimo(a.page, { estrellas: 5, nombre: 'Diego Ros' });
        await a.page.fill('#comentario', 'Primera pestaña. frase-doble-6');
        await a.page.click('#enviar');
        await a.page.locator('#gracias').waitFor({ state: 'visible', timeout: 8000 });
        await llenarMinimo(b.page, { estrellas: 2, nombre: 'Diego Ros' });
        await b.page.fill('#comentario', 'Segunda pestaña. frase-doble-6b');
        await b.page.click('#enviar');
        await estadoConTexto(b.page, 'Ya recibimos tu opinión');
        const estado = await getJson('/dev/state');
        const cuantas = estado.resenas.filter((r) => (r.comment || '').includes('frase-doble-6')).length;
        esperar(cuantas === 1, `debería haber una sola reseña, hay ${cuantas}`);
        // El 409 de la segunda pestaña es esperado y el navegador lo registra en consola.
        const esperado = [/status of 409/];
        sinErroresNiExternos(a, esperado);
        sinErroresNiExternos(b, esperado);
      } finally { await cerrar(a); await cerrar(b); }
    });

    // ── Diseño: sin scroll horizontal en los anchos de referencia ───────
    for (const ancho of [375, 390, 430, 768, 1440]) {
      caso(`diseño: sin scroll horizontal a ${ancho}px`, async (browser) => {
        const sesion = await abrir(browser, tokens.ok1, { ancho });
        try {
          await sesion.page.locator('#formulario').waitFor({ state: 'visible' });
          const exceso = await sesion.page.evaluate(() =>
            document.documentElement.scrollWidth - document.documentElement.clientWidth);
          esperar(exceso <= 0, `desborda ${exceso}px a ${ancho}px`);
          await sesion.page.screenshot({ path: path.join(SHOTS, `formulario-${ancho}.png`), fullPage: true });
          sinErroresNiExternos(sesion);
        } finally { await cerrar(sesion); }
      });
    }

    caso('diseño: la pantalla de estado tampoco desborda a 375px', async (browser) => {
      const sesion = await abrir(browser, tokens.usado, { ancho: 375 });
      try {
        await estadoConTexto(sesion.page, 'Ya recibimos tu opinión');
        const exceso = await sesion.page.evaluate(() =>
          document.documentElement.scrollWidth - document.documentElement.clientWidth);
        esperar(exceso <= 0, `desborda ${exceso}px`);
      } finally { await cerrar(sesion); }
    });

    // ── Ejecución ────────────────────────────────────────────────────────
    // ── Seguridad: metadatos de la foto y meta tags de /opinar ─────────
    caso('foto: el EXIF del original no llega al almacenamiento y el JPEG guardado pasa la validación', async (browser) => {
      const sesion = await abrir(browser, tokens.ok7);
      try {
        const p = sesion.page;
        await p.locator('#formulario').waitFor({ state: 'visible' });
        // Ruido: la imagen pesa lo suficiente para pasar el mínimo de 15 KB.
        const base64 = await p.evaluate(() => {
          const c = document.createElement('canvas');
          c.width = 1200;
          c.height = 900;
          const x = c.getContext('2d');
          const im = x.createImageData(1200, 900);
          for (let i = 0; i < im.data.length; i += 4) {
            im.data[i] = Math.random() * 255;
            im.data[i + 1] = Math.random() * 255;
            im.data[i + 2] = Math.random() * 255;
            im.data[i + 3] = 255;
          }
          x.putImageData(im, 0, 0);
          return c.toDataURL('image/jpeg', 0.85).split(',')[1];
        });
        const original = Buffer.from(base64, 'base64');
        // APP1 Exif con un marcador reconocible, justo después del SOI (FFD8).
        const carga = Buffer.from('Exif\u0000\u0000ORK3D-EXIF-CANARY-GPS', 'binary');
        const app1 = Buffer.concat([Buffer.from([0xff, 0xe1, 0, carga.length + 2]), carga]);
        const conExif = Buffer.concat([original.subarray(0, 2), app1, original.subarray(2)]);
        esperar(conExif.includes(Buffer.from('ORK3D-EXIF-CANARY')), 'el original debe llevar el canario');
        await p.setInputFiles('#foto', { name: 'con-exif.jpg', mimeType: 'image/jpeg', buffer: conExif });
        await p.locator('#foto-estado').filter({ hasText: 'Foto lista.' }).waitFor({ timeout: 15000 });
        await llenarMinimo(p, { estrellas: 5, nombre: 'Elena Torres' });
        await p.fill('#comentario', 'Foto con metadatos en el original. frase-exif-7');
        await p.click('#enviar');
        await p.locator('#gracias').waitFor({ state: 'visible', timeout: 8000 });
        const r = await resenaCon('frase-exif-7');
        esperar(r && r.photo_key, 'la reseña debe tener foto');
        esperar(r.foto_jpeg_limpio === true, 'la foto guardada debe pasar la validación de JPEG limpio');
        esperar(r.foto_tiene_exif === false, 'el EXIF del original no debe llegar al almacenamiento');
        sinErroresNiExternos(sesion);
      } finally { await cerrar(sesion); }
    });

    caso('seguridad: /opinar lleva noindex y no-referrer, y no carga nada de terceros', async (browser) => {
      const sesion = await abrir(browser, tokens.ok1);
      try {
        const robots = await sesion.page.locator('meta[name="robots"]').getAttribute('content');
        const referrer = await sesion.page.locator('meta[name="referrer"]').getAttribute('content');
        esperar(/noindex/.test(robots || ''), `robots: ${robots}`);
        esperar(referrer === 'no-referrer', `referrer: ${referrer}`);
        await sesion.page.locator('#formulario').waitFor({ state: 'visible' });
        sinErroresNiExternos(sesion);
      } finally { await cerrar(sesion); }
    });

    // ── Listado público /opiniones (datos de prueba vía route; sin tocar el servidor) ──
    const publicasVacio = async (browser) => {
      const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, locale: 'es-AR' });
      const page = await ctx.newPage();
      const errores = [];
      page.on('pageerror', (e) => errores.push(`pageerror: ${e.message}`));
      page.on('console', (m) => { if (m.type() === 'error') errores.push(`console: ${m.text()}`); });
      await page.route(/fonts\.(googleapis|gstatic)\.com/, (r) => r.fulfill({ contentType: 'text/css', body: '' }));
      return { ctx, page, errores };
    };

    caso('opiniones: sin exportar todavía muestra el estado vacío', async (browser) => {
      const s = await publicasVacio(browser);
      try {
        await s.page.goto(`${BASE}/opiniones`);
        await s.page.locator('#resumen-nota').filter({ hasText: 'Todavía no hay opiniones publicadas.' })
          .waitFor({ state: 'visible', timeout: 8000 });
        esperar(await s.page.locator('#resenas-lista').isHidden(), 'la lista debería estar oculta');
        // El 404 de publicas.json es el caso real "todavía no se exportó nada".
        sinErroresNiExternos({ errores: s.errores, externos: [] }, [/status of 404/]);
      } finally { await s.ctx.close(); }
    });

    caso('opiniones: muestra solo campos públicos, escapa el texto y filtra fotos ajenas', async (browser) => {
      const s = await publicasVacio(browser);
      try {
        const p = s.page;
        await p.goto(`${BASE}/opiniones`);
        const jpeg = Buffer.from((await p.evaluate(() => {
          const c = document.createElement('canvas');
          c.width = 400; c.height = 300;
          const x = c.getContext('2d');
          x.fillStyle = '#B5B3F4'; x.fillRect(0, 0, 400, 300);
          x.fillStyle = '#FF704E'; x.fillRect(60, 60, 280, 180);
          return c.toDataURL('image/jpeg', 0.85);
        })).split(',')[1], 'base64');
        const fotoOk = '/resenas/fotos/' + 'a'.repeat(32) + '.jpg';
        const publicas = {
          generated_at: '2026-10-21T18:00:00Z', count: 2,
          items: [
            { id: 'a'.repeat(32), rating: 5, comment: 'Quedó perfecto para nuestro torneo.',
              aspects: ['calidad', 'fecha', 'no-existe'], display_name: 'Ana G.', category: 'trofeos',
              product_label: 'Trofeo acrílico', verified: true, photo_url: fotoOk,
              published_at: '2026-10-20T12:00:00Z',
              contact: 'ana@x.example', private_feedback: 'SECRETO-PRIVADO', contact_requested: true },
            { id: 'b'.repeat(32), rating: 4,
              comment: '<img src=x onerror="window.__xss=1">Muy buena atención.',
              aspects: ['atencion'], display_name: 'Cliente de ORK3D', category: 'medallas',
              product_label: 'Medalla', verified: false, photo_url: 'https://evil.example/x.jpg',
              published_at: '2026-10-19T12:00:00Z' },
          ],
        };
        await p.route('**/resenas/publicas.json', (r) => r.fulfill({ contentType: 'application/json', body: JSON.stringify(publicas) }));
        await p.route('**/resenas/fotos/*.jpg', (r) => r.fulfill({ contentType: 'image/jpeg', body: jpeg }));
        await p.reload();
        await p.locator('#resenas-lista').waitFor({ state: 'visible', timeout: 8000 });

        const resumen = await p.locator('#resumen-nota').innerText();
        esperar(resumen.includes('4,5 de 5 · 2 opiniones'), `resumen: ${resumen}`);
        esperar(await p.locator('.resena').count() === 2, 'deberían verse 2 tarjetas');
        const cuerpo = await p.locator('#resenas-lista').innerText();
        esperar(!cuerpo.includes('ana@x.example'), 'no debe mostrarse el contacto');
        esperar(!cuerpo.includes('SECRETO-PRIVADO'), 'no debe mostrarse el feedback privado');
        esperar(cuerpo.includes('<img src=x'), 'el HTML del cliente debe verse como texto');
        esperar(await p.evaluate(() => window.__xss === undefined), 'no debe ejecutarse HTML del cliente');
        esperar(await p.locator('.resena').first().locator('.resena__chips li').count() === 2,
          'solo los aspectos válidos deben verse como chips');
        esperar(await p.locator('.resena__sello').count() === 1, 'el sello de pedido verificado va una vez');
        esperar(await p.locator('.resena__foto').count() === 1, 'solo la foto con ruta válida se muestra');
        esperar((await p.locator('.resena__foto').getAttribute('loading')) === 'lazy', 'la foto debe cargar lazy');
        const exceso = await p.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        esperar(exceso <= 0, `desborda ${exceso}px a 390px`);
        await p.screenshot({ path: path.join(SHOTS, 'opiniones-390.png'), fullPage: true });
        // La primera carga, antes de registrar las rutas, pide publicas.json y recibe 404.
        sinErroresNiExternos({ errores: s.errores, externos: [] }, [/status of 404/]);
      } finally { await s.ctx.close(); }
    });

    caso('opiniones: si la carga falla, muestra el mensaje y no rompe la página', async (browser) => {
      const s = await publicasVacio(browser);
      try {
        await s.page.route('**/resenas/publicas.json', (r) => r.fulfill({ status: 500, body: 'error' }));
        await s.page.goto(`${BASE}/opiniones`);
        await s.page.locator('#resumen-nota').filter({ hasText: 'No pudimos cargar las opiniones.' })
          .waitFor({ state: 'visible', timeout: 8000 });
        // El 500 que simulamos aparece en consola; lo único esperado.
        sinErroresNiExternos({ errores: s.errores, externos: [] }, [/status of 500/]);
      } finally { await s.ctx.close(); }
    });

    for (const { nombre, fn } of casos) {
      try {
        await fn(browser);
        console.log(`ok    ${nombre}`);
      } catch (err) {
        fallidos.push(nombre);
        console.log(`FAIL  ${nombre}\n      ${err.message.split('\n')[0]}`);
      }
    }
  } finally {
    await browser.close();
    servidor.kill();
  }

  const total = casos.length;
  console.log(`\n${total - fallidos.length}/${total} casos OK · capturas en ${SHOTS}`);
  if (fallidos.length) process.exit(1);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
