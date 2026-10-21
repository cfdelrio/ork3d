#!/usr/bin/env node
/* E2E del panel admin de reseñas (V2) contra dev/serve.py: API admin, S3 falso y FakeSES.
 *
 * Uso (desde la raíz del repo):
 *   NODE_PATH=/opt/node22/lib/node_modules node dev/e2e/admin.e2e.js
 *
 * Variables opcionales: PORT (8798), CHROME_PATH (si no, el Chromium de la sandbox),
 * PLAYWRIGHT_PATH (si Playwright no está en el NODE_PATH), SHOTS_DIR.
 *
 * Lo que este E2E asume del servidor de desarrollo (docs/resenas/V2-ADMIN.md, §13):
 * - /admin/api/* montado, con las rutas, cabeceras y códigos de la tabla de §7.
 * - GET /dev/correos: lista de correos simulados {to, from, subject, text, html}.
 * - GET /dev/state: {resenas: [...]} con id y comment (como ya lo usa el E2E de /opinar).
 * - /admin/resenas, /admin/resenas/<id> y /admin/entrar servidos desde admin-web/*.html.
 *
 * Límites a tener en cuenta:
 * - El login admite 3 solicitudes cada 15 min (límite global). El E2E pide un solo enlace: el caso
 *   de login guarda la sesión y los demás casos la reutilizan.
 * - Los 202, 401, 409 y 503 que el servidor no produce a voluntad se simulan con page.route. Esos
 *   casos prueban cómo reacciona el panel, no el servidor.
 */
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const crypto = require('crypto');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const PORT = Number(process.env.PORT || 8798);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR || path.join(ROOT, 'dev', 'e2e', 'capturas');
// Chromium: CHROME_PATH si se define; si no, el de la sandbox; si no existe, el que trae Playwright.
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined);

const MSG_INVALIDO = 'El enlace no es válido o venció. Pedí uno nuevo desde el panel.';
const ASUNTO_ACCESO = 'Acceso al panel de reseñas ORK3D';
const CODIGO_RE = /^[A-Za-z0-9_-]{43}$/;
const ID_RE = /^[a-f0-9]{32}$/;

fs.mkdirSync(SHOTS, { recursive: true });

// Estado que pasa de un caso al siguiente (los casos corren en orden).
let tokens = {};
const resenas = {};        // letra -> id de la reseña de prueba
let estadoSesion = null;   // storageState con la cookie ork3d_admin (del primer login)
let codigoLogin = null;    // código del primer enlace: después de usarlo debe devolver 401
let correoAcceso = null;   // destinatario del correo de acceso (para revisar logs)
const tokensInvitacion = []; // tokens de las invitaciones creadas (para revisar logs)
let logServidor = '';      // salida del servidor, para revisar que no imprima datos sensibles
const casos = [];
// POST del panel que tienen que llevar x-amz-content-sha256 con el hash del body (OAC; ver V2-ADMIN.md §5).
const RUTAS_POST_REQUERIDAS = [
  '/sesion/solicitar', '/sesion/confirmar', '/sesion/cerrar', '/invitaciones',
  '/resenas/{id}/aprobar', '/resenas/{id}/rechazar', '/resenas/{id}/ocultar', '/resenas/{id}/restaurar',
  '/resenas/{id}/reenviar-aviso', '/exportar'
];
// Todos los pedidos al API admin de todos los casos: el caso final verifica cada POST.
const pedidosApi = [];

function caso(nombre, fn) { casos.push({ nombre, fn }); }

function arrancarServidor() {
  return new Promise((resolver, rechazar) => {
    const proc = spawn('python3', [path.join(ROOT, 'dev', 'serve.py'), String(PORT)], { cwd: ROOT });
    let arranque = '';
    let listo = false;
    const recoger = (d) => {
      logServidor += d;
      if (listo) return;
      arranque += d;
      if (arranque.includes('ORK3D dev')) {
        listo = true;
        resolver(proc);
      }
    };
    proc.stdout.on('data', recoger);
    proc.stderr.on('data', recoger);
    proc.on('exit', (code) => {
      if (!listo) rechazar(new Error(`dev/serve.py salió con ${code}: ${arranque}`));
    });
  });
}

function esperar(condicion, mensaje) {
  if (!condicion) throw new Error(mensaje);
}

async function getJson(ruta) {
  const r = await fetch(BASE + ruta);
  return r.json();
}

// Contexto con colectores: errores de página, pedidos a terceros y pedidos a la API admin.
async function abrirPagina(browser, { ancho = 390, conSesion = false } = {}) {
  if (conSesion) esperar(estadoSesion, 'este caso necesita la sesión del caso de login, que falló');
  const ctx = await browser.newContext({
    viewport: { width: ancho, height: 844 },
    locale: 'es-AR',
    storageState: conSesion ? estadoSesion : undefined
  });
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: BASE });
  const page = await ctx.newPage();
  const s = { page, ctx, errores: [], externos: [], api: [] };
  page.on('pageerror', (e) => s.errores.push(`pageerror: ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error') s.errores.push(`console: ${m.text()}`); });
  page.on('request', (r) => {
    if (!r.url().startsWith(BASE)) s.externos.push(r.url());
    if (r.url().startsWith(`${BASE}/admin/api/`)) {
      // cuerpoBytes son los bytes exactos que sale por la red (sin reserializar): el hash se recalcula sobre ellos.
      const pedido = {
        metodo: r.method(),
        ruta: r.url().slice(`${BASE}/admin/api`.length),
        cabeceras: {},
        cuerpo: r.postData(),
        cuerpoBytes: r.postDataBuffer()
      };
      // Chromium agrega Origin después del evento 'request': headers() no lo trae y allHeaders() sí.
      // Quien necesite las cabeceras espera pedido.listo.
      pedido.listo = r.allHeaders().then((h) => { pedido.cabeceras = h; }).catch(() => {});
      s.api.push(pedido);
      pedidosApi.push(pedido);
    }
  });
  return s;
}

function pedidosA(s, metodo, ruta) {
  return s.api.filter((p) => p.metodo === metodo && p.ruta.startsWith(ruta));
}

// OAC exige x-amz-content-sha256 = SHA-256 hex exacto del body. Se recalcula sobre los bytes capturados
// y se compara con la cabecera que salió por la red. No basta con que la cabecera exista.
async function verificarHashDelBody(pedido) {
  esperar(pedido, 'debe haber un POST para verificar su hash');
  await pedido.listo;
  esperar(pedido.cuerpoBytes && pedido.cuerpoBytes.length > 0,
    `POST ${pedido.ruta} sin body capturado: no se puede verificar su hash`);
  const esperado = crypto.createHash('sha256').update(pedido.cuerpoBytes).digest('hex');
  const recibido = pedido.cabeceras['x-amz-content-sha256'];
  esperar(recibido !== undefined, `POST ${pedido.ruta} sin cabecera x-amz-content-sha256`);
  esperar(recibido === esperado,
    `POST ${pedido.ruta}: x-amz-content-sha256 ${recibido} no coincide con el SHA-256 del body (${esperado})`);
}

function sinErroresNiExternos(s, permitidos = []) {
  const inesperados = s.errores.filter((e) => !permitidos.some((re) => re.test(e)));
  esperar(inesperados.length === 0, `errores de consola o de página: ${inesperados.join(' | ')}`);
  esperar(s.externos.length === 0, `pedidos a terceros: ${s.externos.join(' | ')}`);
}

// Reseña por la API pública (sin navegador). El marcador en el comentario permite encontrar su id.
async function crearResena(token, datos) {
  const r = await fetch(`${BASE}/api/resenas/resenas`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, display_mode: 'inicial', permission_to_publish: true, ...datos })
  });
  esperar(r.ok, `la API pública rechazó la reseña "${datos.comment}": ${r.status}`);
}

async function idDeResena(marcador) {
  const estado = await getJson('/dev/state');
  const r = estado.resenas.find((x) => (x.comment || '').includes(marcador));
  esperar(r && ID_RE.test(r.id || ''), `no encontré la reseña ${marcador} en /dev/state`);
  return r.id;
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

// Reseña con foto por el formulario público, como la completaría una clienta.
async function resenaConFoto(browser, token, marcador) {
  const s = await abrirPagina(browser);
  try {
    const p = s.page;
    await p.goto(`${BASE}/opinar/${token}`);
    await p.locator('#formulario').waitFor({ state: 'visible' });
    const png = await pngDeGradiente(p);
    await p.setInputFiles('#foto', { name: 'pedido.png', mimeType: 'image/png', buffer: png });
    await p.locator('#foto-estado').filter({ hasText: 'Foto lista.' }).waitFor({ timeout: 15000 });
    await p.locator('label[for="rating-5"]').click();
    await p.fill('#nombre', 'Bruno Pérez');
    await p.locator('#permiso').check();
    await p.fill('#comentario', `Con foto. ${marcador}`);
    await p.click('#enviar');
    await p.locator('#gracias').waitFor({ state: 'visible', timeout: 8000 });
  } finally {
    await s.ctx.close();
  }
}

async function abrirDetalle(browser, id, { ancho = 390 } = {}) {
  const s = await abrirPagina(browser, { conSesion: true, ancho });
  await s.page.goto(`${BASE}/admin/resenas/${id}`);
  await s.page.locator('#detalle .detalle__bloque').first().waitFor({ state: 'visible', timeout: 8000 });
  return s;
}

async function llenarInvitacion(p) {
  await p.fill('#campo-pedido', 'ORK-E2E-0001');
  await p.fill('#campo-producto', 'trofeo-personalizado');
  await p.fill('#campo-etiqueta', 'Trofeo personalizado');
  await p.fill('#campo-categoria', 'torneos');
  await p.fill('#campo-dias', '15');
}

// ── Datos de prueba ─────────────────────────────────────────────────────────

caso('datos: reseñas de prueba por la API pública (una con foto, por el formulario)', async (browser) => {
  await crearResena(tokens.ok1, { rating: 5, aspects: ['diseno'], name: 'Ana Gómez', comment: 'Flujo completo. marca-admin-A' });
  await resenaConFoto(browser, tokens.ok2, 'marca-admin-B');
  await crearResena(tokens.ok3, {
    rating: 4,
    aspects: ['calidad', 'fecha'],
    name: 'Carla Ruiz',
    comment: '<img src=x onerror="window.__xss=1">Hola. marca-admin-C',
    private_feedback: 'Interno: revisar envío',
    contact_requested: true,
    contact: 'carla@example.com'
  });
  await crearResena(tokens.ok4, { rating: 3, aspects: ['atencion'], name: 'Diego Ros', comment: 'Export pendiente. marca-admin-D' });
  await crearResena(tokens.ok5, { rating: 2, aspects: ['diseno'], name: 'Elena Torres', comment: 'Errores simulados. marca-admin-E' });
  await crearResena(tokens.ok6, { rating: 5, aspects: ['resultado'], name: 'Fede Lago', comment: 'Doble clic. marca-admin-F' });
  await crearResena(tokens.ok7, { rating: 1, aspects: ['calidad'], name: 'Gastón Mir', comment: 'Sesión vencida. marca-admin-G' });
  for (const letra of 'ABCDEFG') {
    resenas[letra] = await idDeResena(`marca-admin-${letra}`);
  }
  esperar(new Set(Object.values(resenas)).size === 7, 'cada reseña de prueba debe tener su propio id');
});

// ── Acceso ──────────────────────────────────────────────────────────────────

caso('acceso: sin sesión se ve solo el acceso y no hay datos en pantalla', async (browser) => {
  const s = await abrirPagina(browser);
  try {
    await s.page.goto(`${BASE}/admin/resenas`);
    await s.page.locator('#seccion-acceso').waitFor({ state: 'visible', timeout: 8000 });
    esperar(await s.page.locator('#seccion-panel').isHidden(), 'el panel no debe verse sin sesión');
    esperar(await s.page.locator('#seccion-detalle').isHidden(), 'el detalle no debe verse sin sesión');
    esperar((await s.page.locator('.tarjeta').count()) === 0, 'no debe haber reseñas sin sesión');
    esperar(await s.page.locator('#cerrar-sesion').isHidden(), 'Cerrar sesión no debe verse sin sesión');
    sinErroresNiExternos(s, [/status of 401/]);
  } finally {
    await s.ctx.close();
  }
});

caso('login: pedir el enlace, confirmarlo desde el correo y entrar al panel', async (browser) => {
  const s = await abrirPagina(browser);
  try {
    const p = s.page;
    await p.goto(`${BASE}/admin/resenas`);
    await p.locator('#pedir-acceso').waitFor({ state: 'visible', timeout: 8000 });
    await p.click('#pedir-acceso');
    await p.locator('#aviso-acceso').filter({ hasText: 'Si corresponde, te llegó un correo con el enlace.' })
      .waitFor({ state: 'visible' });
    esperar(await p.locator('#pedir-acceso').isDisabled(), 'el botón debe enfriarse después de pedir el enlace');

    const correos = await getJson('/dev/correos');
    const acceso = correos.filter((c) => c.subject === ASUNTO_ACCESO).pop();
    esperar(acceso, 'no llegó el correo de acceso');
    correoAcceso = acceso.to;
    const prefijo = `${BASE}/admin/entrar#c=`;
    const cuerpo = `${acceso.text || ''}\n${acceso.html || ''}`;
    const posicion = cuerpo.indexOf(prefijo);
    esperar(posicion >= 0, 'el correo debe traer el enlace con el código en el fragmento');
    const codigo = cuerpo.slice(posicion + prefijo.length, posicion + prefijo.length + 43);
    esperar(CODIGO_RE.test(codigo), `código con formato inesperado: ${codigo}`);

    // Cargar la página no consume el enlace: solo el clic lo hace.
    await p.goto(`${BASE}/admin/entrar#c=${codigo}`);
    await p.locator('#confirmar-acceso').waitFor({ state: 'visible' });
    esperar(await p.locator('#confirmar-acceso').isEnabled(), 'con código, el botón debe estar habilitado');
    esperar(!p.url().includes('#c='), `el código debe salir de la URL: ${p.url()}`);
    esperar(pedidosA(s, 'POST', '/sesion/confirmar').length === 0, 'cargar la página no debe confirmar el acceso');

    await p.click('#confirmar-acceso');
    await p.waitForURL(`${BASE}/admin/resenas`);
    await p.locator('#seccion-panel').waitFor({ state: 'visible', timeout: 8000 });
    esperar(await p.locator('#cerrar-sesion').isVisible(), 'con sesión debe verse Cerrar sesión');

    const cookie = (await s.ctx.cookies()).find((c) => c.name === 'ork3d_admin');
    esperar(cookie && cookie.httpOnly && cookie.secure && cookie.path === '/admin' && cookie.sameSite === 'Lax',
      `cookie de sesión: ${JSON.stringify(cookie)}`);
    const ochoHoras = 8 * 60 * 60 * 1000;
    esperar(Math.abs(cookie.expires * 1000 - Date.now() - ochoHoras) < 5 * 60 * 1000, 'la sesión dura 8 h');

    estadoSesion = await s.ctx.storageState();
    codigoLogin = codigo;
    // La primera visita, sin sesión, consulta GET /admin/api/sesion y recibe 401: es el estado esperado.
    sinErroresNiExternos(s, [/status of 401/]);
  } finally {
    await s.ctx.close();
  }
});

caso('entrar: un enlace ya usado devuelve 401, muestra el mensaje y no se reintenta solo', async (browser) => {
  esperar(codigoLogin, 'este caso depende del login');
  const s = await abrirPagina(browser);
  try {
    await s.page.goto(`${BASE}/admin/entrar#c=${codigoLogin}`);
    await s.page.locator('#confirmar-acceso').waitFor({ state: 'visible' });
    await s.page.click('#confirmar-acceso');
    await s.page.locator('#aviso-acceso').filter({ hasText: MSG_INVALIDO }).waitFor({ state: 'visible', timeout: 8000 });
    esperar(await s.page.locator('#confirmar-acceso').isDisabled(), 'con un código inválido no debe quedar listo para reintentar');
    esperar(pedidosA(s, 'POST', '/sesion/confirmar').length === 1, 'debe haber un solo intento');
    sinErroresNiExternos(s, [/status of 401/]);
  } finally {
    await s.ctx.close();
  }
});

caso('entrar: sin código en el enlace deshabilita la confirmación y avisa', async (browser) => {
  const s = await abrirPagina(browser);
  try {
    await s.page.goto(`${BASE}/admin/entrar`);
    await s.page.locator('#aviso-acceso').filter({ hasText: MSG_INVALIDO }).waitFor({ state: 'visible' });
    esperar(await s.page.locator('#confirmar-acceso').isDisabled(), 'sin código el botón debe estar deshabilitado');
    esperar(pedidosA(s, 'POST', '/sesion/confirmar').length === 0, 'sin código no debe haber llamadas');
    sinErroresNiExternos(s);
  } finally {
    await s.ctx.close();
  }
});

caso('entrar: un código inventado devuelve 401 y no se reintenta solo', async (browser) => {
  const s = await abrirPagina(browser);
  try {
    await s.page.goto(`${BASE}/admin/entrar#c=${'A'.repeat(43)}`);
    await s.page.locator('#confirmar-acceso').waitFor({ state: 'visible' });
    await s.page.click('#confirmar-acceso');
    await s.page.locator('#aviso-acceso').filter({ hasText: MSG_INVALIDO }).waitFor({ state: 'visible', timeout: 8000 });
    esperar(pedidosA(s, 'POST', '/sesion/confirmar').length === 1, 'debe haber un solo intento');
    esperar(await s.page.locator('#confirmar-acceso').isDisabled(), 'el botón no debe quedar listo para reintentar');
    sinErroresNiExternos(s, [/status of 401/]);
  } finally {
    await s.ctx.close();
  }
});

// ── Bandeja ─────────────────────────────────────────────────────────────────

caso('bandeja: pestañas con flechas, Inicio y Fin; cada pestaña pide su estado', async (browser) => {
  const s = await abrirPagina(browser, { conSesion: true });
  try {
    const p = s.page;
    await p.goto(`${BASE}/admin/resenas`);
    await p.locator('#lista .tarjeta, #lista .lista__vacia').first().waitFor({ state: 'visible', timeout: 8000 });
    esperar((await p.locator('#lista .tarjeta').count()) >= 1, 'Pendientes debe listar las reseñas de prueba');
    esperar((await p.locator('#tab-pending').getAttribute('aria-selected')) === 'true', 'la pestaña inicial es Pendientes');

    const enlace = await p.locator('#lista .tarjeta a.btn').first().getAttribute('href');
    esperar(/^\/admin\/resenas\/[a-f0-9]{32}$/.test(enlace || ''), `enlace "Ver": ${enlace}`);

    await p.focus('#tab-pending');
    let pedido = p.waitForRequest((r) => r.url().includes('/admin/api/resenas?estado=approved'));
    await p.keyboard.press('ArrowRight');
    await pedido;
    esperar((await p.evaluate(() => document.activeElement.id)) === 'tab-approved', 'la flecha derecha mueve el foco');
    esperar((await p.locator('#tab-approved').getAttribute('aria-selected')) === 'true', 'la pestaña enfocada queda activa');
    esperar((await p.locator('#tab-pending').getAttribute('tabindex')) === '-1', 'la pestaña inactiva sale del orden de tabulación');

    pedido = p.waitForRequest((r) => r.url().includes('/admin/api/resenas?estado=all'));
    await p.keyboard.press('End');
    await pedido;
    esperar((await p.evaluate(() => document.activeElement.id)) === 'tab-all', 'Fin va a la última pestaña');

    pedido = p.waitForRequest((r) => r.url().includes('/admin/api/resenas?estado=pending'));
    await p.keyboard.press('Home');
    await pedido;
    esperar((await p.evaluate(() => document.activeElement.id)) === 'tab-pending', 'Inicio va a la primera pestaña');

    // "Ver" abre el detalle de esa reseña.
    await p.locator('#lista .tarjeta a.btn').first().click();
    await p.locator('#detalle .detalle__bloque').first().waitFor({ state: 'visible', timeout: 8000 });
    esperar(/\/admin\/resenas\/[a-f0-9]{32}$/.test(p.url()), `URL del detalle: ${p.url()}`);
    sinErroresNiExternos(s);
  } finally {
    await s.ctx.close();
  }
});

const rutaBandeja = (url) => url.pathname === '/admin/api/resenas';

caso('bandeja: si la carga falla muestra el aviso y Reintentar vuelve a cargar', async (browser) => {
  const s = await abrirPagina(browser, { conSesion: true });
  try {
    const p = s.page;
    await p.route(rutaBandeja, (r) => r.abort());
    await p.goto(`${BASE}/admin/resenas`);
    await p.locator('#aviso-bandeja').filter({ hasText: 'No pudimos cargar la bandeja.' }).waitFor({ state: 'visible', timeout: 8000 });
    esperar(await p.locator('#reintentar-bandeja').isVisible(), 'debe ofrecer Reintentar');
    await p.unroute(rutaBandeja);
    await p.click('#reintentar-bandeja');
    await p.locator('#lista .tarjeta, #lista .lista__vacia').first().waitFor({ state: 'visible', timeout: 8000 });
    esperar(await p.locator('#reintentar-bandeja').isHidden(), 'Reintentar se oculta al cargar');
    sinErroresNiExternos(s, [/net::ERR_FAILED/]);
  } finally {
    await s.ctx.close();
  }
});

// ── Crear solicitud (invitación) ────────────────────────────────────────────

caso('invitación: la validación del navegador marca los campos, enfoca el primero y no envía', async (browser) => {
  const s = await abrirPagina(browser, { conSesion: true });
  try {
    const p = s.page;
    await p.goto(`${BASE}/admin/resenas`);
    await p.locator('#form-invitacion').waitFor({ state: 'visible', timeout: 8000 });
    await p.click('#crear-link');
    esperar(await p.locator('#err-pedido').isVisible(), 'falta el error de pedido');
    esperar(await p.locator('#err-producto').isVisible(), 'falta el error de producto');
    esperar(await p.locator('#err-etiqueta').isVisible(), 'falta el error de nombre');
    esperar(await p.locator('#err-categoria').isVisible(), 'falta el error de categoría');
    esperar((await p.locator('#campo-pedido').getAttribute('aria-invalid')) === 'true', 'el pedido debe quedar con aria-invalid');
    esperar((await p.evaluate(() => document.activeElement.id)) === 'campo-pedido', 'el foco debe ir al primer error');

    await p.fill('#campo-dias', '400');
    await p.click('#crear-link');
    esperar(await p.locator('#err-dias').isVisible(), 'los días fuera de rango deben marcar error');
    esperar(pedidosA(s, 'POST', '/invitaciones').length === 0, 'con errores no debe haber ningún POST');
    sinErroresNiExternos(s);
  } finally {
    await s.ctx.close();
  }
});

caso('invitación: crea el link, lo copia y no lo guarda en el navegador', async (browser) => {
  const s = await abrirPagina(browser, { conSesion: true });
  try {
    const p = s.page;
    await p.goto(`${BASE}/admin/resenas`);
    await p.locator('#form-invitacion').waitFor({ state: 'visible', timeout: 8000 });
    await llenarInvitacion(p);
    await p.click('#crear-link');
    await p.locator('#resultado').waitFor({ state: 'visible', timeout: 8000 });

    const post = pedidosA(s, 'POST', '/invitaciones')[0];
    esperar(post, 'debe haber un POST de invitación');
    await post.listo;
    esperar(post.cabeceras['x-admin-request'] === '1', 'falta la cabecera X-Admin-Request');
    esperar(post.cabeceras['content-type'] === 'application/json', 'el cuerpo debe ser application/json');
    esperar(post.cabeceras.origin === BASE, `Origin: ${post.cabeceras.origin}`);
    await verificarHashDelBody(post);
    const cuerpo = JSON.parse(post.cuerpo);
    esperar(cuerpo.order_ref === 'ORK-E2E-0001' && cuerpo.dias === 15, `cuerpo de la invitación: ${post.cuerpo}`);

    const enlace = await p.locator('#resultado-enlace').inputValue();
    esperar(/\/opinar\/[A-Za-z0-9_-]{32}$/.test(enlace), `enlace: ${enlace}`);
    tokensInvitacion.push(enlace.split('/opinar/')[1]);
    esperar((await p.locator('#resultado-vence').innerText()).startsWith('Vence el '), 'falta la fecha de vencimiento');

    await p.click('#copiar-enlace');
    await p.locator('#aviso-resultado').filter({ hasText: 'Copiado.' }).waitFor({ state: 'visible' });
    esperar((await p.evaluate(() => navigator.clipboard.readText())) === enlace, 'el portapapeles debe tener el enlace');

    const guardado = await p.evaluate(() => ({ local: localStorage.length, sesion: sessionStorage.length }));
    esperar(guardado.local === 0 && guardado.sesion === 0, `no se guarda nada en el navegador: ${JSON.stringify(guardado)}`);

    await p.reload();
    await p.locator('#form-invitacion').waitFor({ state: 'visible', timeout: 8000 });
    esperar(await p.locator('#resultado').isHidden(), 'el enlace no debe sobrevivir a una recarga');
    sinErroresNiExternos(s);
  } finally {
    await s.ctx.close();
  }
});

caso('invitación: un error de validación del servidor marca el campo que indica', async (browser) => {
  const s = await abrirPagina(browser, { conSesion: true });
  try {
    const p = s.page;
    await p.route((url) => url.pathname === '/admin/api/invitaciones', (r) => r.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: false,
        error: 'validacion',
        mensaje: 'Revisá los datos.',
        campos: { product_slug: 'Ese producto ya tiene un link activo.' }
      })
    }));
    await p.goto(`${BASE}/admin/resenas`);
    await p.locator('#form-invitacion').waitFor({ state: 'visible', timeout: 8000 });
    await llenarInvitacion(p);
    await p.click('#crear-link');
    await p.locator('#err-producto').filter({ hasText: 'Ese producto ya tiene un link activo.' })
      .waitFor({ state: 'visible', timeout: 8000 });
    esperar((await p.evaluate(() => document.activeElement.id)) === 'campo-producto', 'el foco debe ir al campo marcado');
    esperar(await p.locator('#resultado').isHidden(), 'no debe mostrarse ningún link');
    sinErroresNiExternos(s, [/status of 400/]);
  } finally {
    await s.ctx.close();
  }
});

caso('invitación: si la sesión venció, vuelve al acceso con aviso', async (browser) => {
  const s = await abrirPagina(browser, { conSesion: true });
  try {
    const p = s.page;
    await p.route((url) => url.pathname === '/admin/api/invitaciones', (r) => r.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ ok: false, error: 'no_autenticado', mensaje: 'Sesión vencida.' })
    }));
    await p.goto(`${BASE}/admin/resenas`);
    await p.locator('#form-invitacion').waitFor({ state: 'visible', timeout: 8000 });
    await llenarInvitacion(p);
    await p.click('#crear-link');
    await p.locator('#seccion-acceso').waitFor({ state: 'visible', timeout: 8000 });
    await p.locator('#aviso-global').filter({ hasText: 'Tu sesión venció. Iniciá acceso de nuevo.' })
      .waitFor({ state: 'visible' });
    sinErroresNiExternos(s, [/status of 401/]);
  } finally {
    await s.ctx.close();
  }
});

// ── Detalle ─────────────────────────────────────────────────────────────────

caso('detalle: sin foto muestra "Sin foto." y no pide ninguna imagen', async (browser) => {
  const s = await abrirDetalle(browser, resenas.A);
  try {
    esperar((await s.page.locator('img.detalle__foto').count()) === 0, 'no debe haber imagen sin foto');
    esperar((await s.page.locator('#detalle').innerText()).includes('Sin foto.'), 'falta el texto "Sin foto."');
    sinErroresNiExternos(s);
  } finally {
    await s.ctx.close();
  }
});

caso('detalle: el texto del cliente se ve como texto; el interno solo para el equipo', async (browser) => {
  const s = await abrirDetalle(browser, resenas.C);
  try {
    const p = s.page;
    const texto = await p.locator('#detalle').innerText();
    esperar(texto.includes('<img src=x onerror="window.__xss=1">Hola.'), 'el comentario debe verse como texto');
    esperar(await p.evaluate(() => window.__xss === undefined), 'no debe ejecutarse HTML del cliente');
    esperar((await p.locator('#detalle .detalle__texto img').count()) === 0, 'el comentario no debe crear imágenes');
    esperar(texto.includes('Calidad') && texto.includes('Fecha de entrega'), 'los aspectos deben verse con su nombre');
    esperar(texto.includes('Interno: revisar envío') && texto.includes('carla@example.com'),
      'el feedback privado y el contacto deben verse en el detalle');
    sinErroresNiExternos(s);
  } finally {
    await s.ctx.close();
  }
});

caso('detalle: la foto llega desde la API admin; si no carga, avisa y el resto sigue', async (browser) => {
  const s = await abrirDetalle(browser, resenas.B);
  try {
    const img = s.page.locator('img.detalle__foto');
    esperar((await img.getAttribute('src')) === `/admin/api/resenas/${resenas.B}/foto`, 'la foto debe pedirse a la API admin');
    await s.page.evaluate(() => document.querySelector('img.detalle__foto').scrollIntoView());
    await s.page.waitForFunction(() => {
      const i = document.querySelector('img.detalle__foto');
      return Boolean(i && i.complete && i.naturalWidth > 0);
    }, null, { timeout: 8000 });
    sinErroresNiExternos(s);
  } finally {
    await s.ctx.close();
  }

  const f = await abrirPagina(browser, { conSesion: true });
  try {
    await f.page.route((url) => url.pathname === `/admin/api/resenas/${resenas.B}/foto`,
      (r) => r.fulfill({ status: 404, body: '' }));
    await f.page.goto(`${BASE}/admin/resenas/${resenas.B}`);
    await f.page.locator('#detalle .detalle__bloque').first().waitFor({ state: 'visible', timeout: 8000 });
    await f.page.evaluate(() => document.querySelector('img.detalle__foto').scrollIntoView());
    await f.page.locator('#detalle .campo__error').filter({ hasText: 'No pudimos mostrar la foto.' })
      .waitFor({ state: 'visible', timeout: 8000 });
    esperar(await f.page.locator('img.detalle__foto').isHidden(), 'la imagen que no carga debe ocultarse');
    sinErroresNiExternos(f, [/status of 404/]);
  } finally {
    await f.ctx.close();
  }
});

caso('detalle: una reseña inexistente muestra el aviso y ningún dato', async (browser) => {
  const s = await abrirPagina(browser, { conSesion: true });
  try {
    await s.page.goto(`${BASE}/admin/resenas/${'0'.repeat(32)}`);
    await s.page.locator('#aviso-detalle').filter({ hasText: 'No encontramos esa reseña.' })
      .waitFor({ state: 'visible', timeout: 8000 });
    esperar((await s.page.locator('#detalle .detalle__bloque').count()) === 0, 'no debe mostrar datos');
    sinErroresNiExternos(s, [/status of 404/]);
  } finally {
    await s.ctx.close();
  }
});

// ── Moderación ──────────────────────────────────────────────────────────────

caso('moderación: aprobar y ocultar piden confirmación; restaurar no; el aviso sobrevive a la recarga', async (browser) => {
  const s = await abrirDetalle(browser, resenas.A);
  try {
    const p = s.page;
    const botonAccion = (accion) => p.locator(`#acciones-detalle button[data-accion="${accion}"]`);

    await botonAccion('aprobar').click();
    await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
    esperar((await p.locator('#dialogo-titulo').innerText()) === '¿Aprobar esta reseña?', 'título de la confirmación');
    await p.click('#dialogo-cancelar');
    esperar(await p.locator('#dialogo').evaluate((d) => !d.open), 'cancelar cierra el diálogo');
    esperar(pedidosA(s, 'POST', `/resenas/${resenas.A}`).length === 0, 'cancelar no debe enviar nada');

    await botonAccion('aprobar').click();
    await p.click('#dialogo-confirmar');
    await p.locator('#aviso-detalle').filter({ hasText: 'Reseña aprobada.' }).waitFor({ state: 'visible', timeout: 8000 });
    esperar((await p.locator('#detalle .pildora').textContent()).trim() === 'Aprobada', 'la píldora debe decir Aprobada');
    esperar(await botonAccion('ocultar').isVisible(), 'una reseña aprobada ofrece Ocultar');
    const post = pedidosA(s, 'POST', `/resenas/${resenas.A}/aprobar`)[0];
    await post.listo;
    esperar(post && post.cabeceras.origin === BASE && post.cabeceras['x-admin-request'] === '1',
      'la acción lleva Origin y X-Admin-Request');
    await verificarHashDelBody(post);

    await botonAccion('ocultar').click();
    await p.click('#dialogo-confirmar');
    await p.locator('#aviso-detalle').filter({ hasText: 'Reseña oculta.' }).waitFor({ state: 'visible', timeout: 8000 });
    esperar(await botonAccion('restaurar').isVisible(), 'una reseña oculta ofrece Restaurar');

    await botonAccion('restaurar').click();
    esperar((await p.locator('#dialogo[open]').count()) === 0, 'restaurar no pide confirmación');
    await p.locator('#aviso-detalle').filter({ hasText: 'Reseña restaurada.' }).waitFor({ state: 'visible', timeout: 8000 });
    esperar((await p.locator('#detalle .pildora').textContent()).trim() === 'Aprobada', 'una reseña restaurada queda aprobada');
    sinErroresNiExternos(s);
  } finally {
    await s.ctx.close();
  }
});

caso('moderación: rechazar una reseña con foto la deja sin acciones', async (browser) => {
  const s = await abrirDetalle(browser, resenas.B);
  try {
    const p = s.page;
    await p.locator('#acciones-detalle button[data-accion="rechazar"]').click();
    await p.click('#dialogo-confirmar');
    await p.locator('#aviso-detalle').filter({ hasText: 'Reseña rechazada.' }).waitFor({ state: 'visible', timeout: 8000 });
    esperar((await p.locator('#detalle .pildora').textContent()).trim() === 'Rechazada', 'la píldora debe decir Rechazada');
    esperar((await p.locator('#acciones-detalle').innerText()).includes('No hay acciones disponibles para esta reseña.'),
      'una reseña rechazada no ofrece acciones');
    sinErroresNiExternos(s);
  } finally {
    await s.ctx.close();
  }
});

caso('export: si el export falla tras la acción, avisa y ofrece Regenerar export', async (browser) => {
  const s = await abrirDetalle(browser, resenas.D);
  try {
    const p = s.page;
    // La acción responde 202: el estado cambió pero el export no se pudo regenerar.
    await p.route((url) => url.pathname === `/admin/api/resenas/${resenas.D}/aprobar`, (r) => r.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, estado: 'approved', export: 'pendiente', reintentar: true })
    }));
    await p.locator('#acciones-detalle button[data-accion="aprobar"]').click();
    await p.click('#dialogo-confirmar');
    await p.locator('#aviso-detalle').filter({ hasText: 'Export pendiente' }).waitFor({ state: 'visible', timeout: 8000 });
    esperar(await p.locator('#regenerar-export').isVisible(), 'con export pendiente debe verse Regenerar export');

    await p.click('#regenerar-export');
    await p.locator('#aviso-detalle').filter({ hasText: 'Export actualizado.' }).waitFor({ state: 'visible', timeout: 8000 });
    esperar(await p.locator('#regenerar-export').isHidden(), 'tras regenerar, el botón se oculta');
    esperar(pedidosA(s, 'POST', '/exportar').length === 1, 'Regenerar export debe llamar a /exportar una vez');
    sinErroresNiExternos(s);
  } finally {
    await s.ctx.close();
  }
});

caso('acciones: un 409 explica el motivo y recarga; un 503 no cambia nada y deja reintentar', async (browser) => {
  const s = await abrirDetalle(browser, resenas.E);
  try {
    const p = s.page;
    await p.route((url) => url.pathname === `/admin/api/resenas/${resenas.E}/aprobar`, (r) => r.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({ ok: false, error: 'estado_incompatible', mensaje: 'La reseña ya cambió. La recargamos.', estado_actual: 'approved' })
    }));
    await p.route((url) => url.pathname === `/admin/api/resenas/${resenas.E}/rechazar`, (r) => r.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ ok: false, error: 'foto_no_publicada', mensaje: 'No se pudo publicar la foto.' })
    }));

    await p.locator('#acciones-detalle button[data-accion="aprobar"]').click();
    await p.click('#dialogo-confirmar');
    await p.locator('#aviso-detalle').filter({ hasText: 'La reseña ya cambió. La recargamos.' })
      .waitFor({ state: 'visible', timeout: 8000 });

    await p.locator('#acciones-detalle button[data-accion="rechazar"]').click();
    await p.click('#dialogo-confirmar');
    await p.locator('#aviso-detalle').filter({ hasText: 'No se pudo publicar la foto. No cambió nada: probá de nuevo.' })
      .waitFor({ state: 'visible', timeout: 8000 });
    esperar(await p.locator('#acciones-detalle button[data-accion="rechazar"]').isEnabled(),
      'tras el 503 la acción vuelve a estar disponible');
    sinErroresNiExternos(s, [/status of 409/, /status of 503/]);
  } finally {
    await s.ctx.close();
  }
});

caso('acciones: mientras se envía, los botones quedan bloqueados y la acción sale una sola vez', async (browser) => {
  const s = await abrirDetalle(browser, resenas.F);
  try {
    const p = s.page;
    let liberar;
    const pausa = new Promise((resolver) => { liberar = resolver; });
    await p.route((url) => url.pathname === `/admin/api/resenas/${resenas.F}/aprobar`, async (r) => {
      await pausa;
      await r.continue();
    });

    await p.locator('#acciones-detalle button[data-accion="aprobar"]').click();
    await p.click('#dialogo-confirmar');
    await p.locator('#aviso-detalle').filter({ hasText: 'Enviando…' }).waitFor({ state: 'visible', timeout: 8000 });
    esperar(await p.locator('#acciones-detalle button[data-accion="aprobar"]').isDisabled(),
      'mientras se envía, Aprobar debe estar bloqueado');
    liberar();

    await p.locator('#aviso-detalle').filter({ hasText: 'Reseña aprobada.' }).waitFor({ state: 'visible', timeout: 8000 });
    esperar(pedidosA(s, 'POST', `/resenas/${resenas.F}/aprobar`).length === 1, 'la acción debe salir una sola vez');
    sinErroresNiExternos(s);
  } finally {
    await s.ctx.close();
  }
});

caso('acciones: si la sesión venció durante una acción, vuelve al acceso', async (browser) => {
  const s = await abrirDetalle(browser, resenas.G);
  try {
    const p = s.page;
    await p.route((url) => url.pathname === `/admin/api/resenas/${resenas.G}/rechazar`, (r) => r.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ ok: false, error: 'no_autenticado', mensaje: 'Sesión vencida.' })
    }));
    await p.locator('#acciones-detalle button[data-accion="rechazar"]').click();
    await p.click('#dialogo-confirmar');
    await p.locator('#seccion-acceso').waitFor({ state: 'visible', timeout: 8000 });
    await p.locator('#aviso-global').filter({ hasText: 'Tu sesión venció. Iniciá acceso de nuevo.' })
      .waitFor({ state: 'visible' });
    sinErroresNiExternos(s, [/status of 401/]);
  } finally {
    await s.ctx.close();
  }
});

// ── Diseño, seguridad y logs ────────────────────────────────────────────────

caso('avisos: "Reenviar aviso" aparece si el correo sigue pendiente y muestra el resultado', async (browser) => {
  const s = await abrirPagina(browser, { conSesion: true });
  try {
    const p = s.page;
    // El estado del aviso lo decide el servidor; aquí se fuerza "pendiente" para mostrar el botón.
    await p.route((url) => url.pathname === `/admin/api/resenas/${resenas.G}`, async (r) => {
      const real = await (await r.fetch()).json();
      real.resena.aviso = 'pendiente';
      await r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(real) });
    });
    await p.goto(`${BASE}/admin/resenas/${resenas.G}`);
    await p.locator('#acciones-detalle button[data-accion="reenviar-aviso"]').waitFor({ state: 'visible', timeout: 8000 });

    // Si el aviso ya salió, el servidor responde 409 ya_enviado y no cambia nada.
    await p.route((url) => url.pathname === `/admin/api/resenas/${resenas.G}/reenviar-aviso`, (r) => r.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({ ok: false, error: 'ya_enviado', mensaje: 'Ese aviso ya salió.' })
    }));
    await p.click('#acciones-detalle button[data-accion="reenviar-aviso"]');
    await p.locator('#aviso-detalle').filter({ hasText: 'Ese aviso ya salió.' }).waitFor({ state: 'visible', timeout: 8000 });
    esperar(pedidosA(s, 'POST', `/resenas/${resenas.G}/reenviar-aviso`).length === 1, 'reenviar debe llamar una sola vez');
    sinErroresNiExternos(s, [/status of 409/]);
  } finally {
    await s.ctx.close();
  }
});

caso('diseño: bandeja y detalle sin scroll horizontal a 375, 390, 430, 768 y 1440 px', async (browser) => {
  for (const ancho of [375, 390, 430, 768, 1440]) {
    for (const ruta of ['/admin/resenas', `/admin/resenas/${resenas.C}`]) {
      const s = await abrirPagina(browser, { conSesion: true, ancho });
      try {
        const listo = ruta === '/admin/resenas' ? '#lista .tarjeta, #lista .lista__vacia' : '#detalle .detalle__bloque';
        await s.page.goto(`${BASE}${ruta}`);
        await s.page.locator(listo).first().waitFor({ state: 'visible', timeout: 8000 });
        const exceso = await s.page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        esperar(exceso <= 0, `desborda ${exceso}px en ${ruta} a ${ancho}px`);
        if (ancho === 390) {
          const nombre = ruta === '/admin/resenas' ? 'bandeja' : 'detalle';
          await s.page.screenshot({ path: path.join(SHOTS, `admin-${nombre}-390.png`), fullPage: true });
        }
        sinErroresNiExternos(s);
      } finally {
        await s.ctx.close();
      }
    }
  }
});

caso('seguridad: noindex, CSP y sin scripts ni estilos inline en las dos páginas', async (browser) => {
  for (const ruta of ['/admin/resenas', '/admin/entrar']) {
    const s = await abrirPagina(browser);
    try {
      await s.page.goto(`${BASE}${ruta}`);
      const robots = (await s.page.locator('meta[name="robots"]').getAttribute('content')) || '';
      const referrer = await s.page.locator('meta[name="referrer"]').getAttribute('content');
      const csp = (await s.page.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute('content')) || '';
      esperar(/noindex/.test(robots) && /nofollow/.test(robots), `robots en ${ruta}: ${robots}`);
      esperar(referrer === 'no-referrer', `referrer en ${ruta}: ${referrer}`);
      esperar(/script-src 'self'/.test(csp) && !/unsafe-inline/.test(csp), `CSP en ${ruta}: ${csp}`);
      esperar((await s.page.locator('script:not([src])').count()) === 0, `hay un script inline en ${ruta}`);
      esperar((await s.page.locator('[style]').count()) === 0, `hay atributos style en ${ruta}`);
      sinErroresNiExternos(s, [/status of 401/]);
    } finally {
      await s.ctx.close();
    }
  }
});

caso('logs: el servidor no imprime códigos de acceso, correos ni tokens de invitación', async () => {
  esperar(codigoLogin && correoAcceso, 'este caso depende del login');
  const sensibles = [codigoLogin, correoAcceso, ...tokensInvitacion];
  // También los prefijos: el registro nunca debe dejar un fragmento reconocible.
  const prefijos = sensibles.map((v) => v.slice(0, 12));
  for (const valor of [...sensibles, ...prefijos]) {
    esperar(!logServidor.includes(valor), `el log del servidor contiene un dato sensible (${valor.slice(0, 4)}…)`);
  }
});

// ── Cerrar sesión (último: invalida la sesión que usan los demás casos) ─────

caso('sesión: cerrar sesión vuelve al acceso y la sesión anterior deja de servir', async (browser) => {
  const s = await abrirPagina(browser, { conSesion: true });
  try {
    const p = s.page;
    await p.goto(`${BASE}/admin/resenas`);
    await p.locator('#cerrar-sesion').waitFor({ state: 'visible', timeout: 8000 });
    await p.click('#cerrar-sesion');
    await p.locator('#aviso-global').filter({ hasText: 'Cerraste la sesión.' }).waitFor({ state: 'visible', timeout: 8000 });
    esperar(await p.locator('#seccion-acceso').isVisible(), 'tras cerrar sesión se ve el acceso');

    // Una sesión copiada antes de cerrar ya no vale en el servidor.
    const anterior = await browser.newContext({ storageState: estadoSesion });
    try {
      const r = await anterior.request.get(`${BASE}/admin/api/sesion`);
      esperar(r.status() === 401, `la sesión cerrada sigue sirviendo: ${r.status()}`);
    } finally {
      await anterior.close();
    }
    sinErroresNiExternos(s);
  } finally {
    await s.ctx.close();
  }
});

caso('seguridad: cada POST al API admin lleva el SHA-256 exacto de su body y todas las rutas requeridas se ejercitaron', async () => {
  const posts = pedidosApi.filter((p) => p.metodo === 'POST');
  esperar(posts.length > 0, 'no hubo POST al API admin: el caso no puede verificar nada');
  for (const p of posts) {
    await verificarHashDelBody(p);
    esperar(p.cabeceras['content-type'] === 'application/json', `POST ${p.ruta}: Content-Type ${p.cabeceras['content-type']}`);
    esperar(p.cabeceras['x-admin-request'] === '1', `POST ${p.ruta}: falta X-Admin-Request`);
    esperar(p.cabeceras.origin === BASE, `POST ${p.ruta}: Origin ${p.cabeceras.origin}`);
  }
  const vistas = new Set(posts.map((p) => p.ruta.replace(/^\/resenas\/[a-f0-9]{32}/, '/resenas/{id}')));
  for (const requerida of RUTAS_POST_REQUERIDAS) {
    esperar(vistas.has(requerida), `falta un POST a ${requerida} en la verificación del hash`);
  }
});

async function main() {
  const servidor = await arrancarServidor();
  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
  const fallidos = [];
  try {
    tokens = await getJson('/dev/tokens');
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
