// CloudFront Function:
//   1. Redirige www.ork3d.com → ork3d.com (apex canónico).
//   2. /opinar/<token> → /opinar.html (el token va en el path; ver docs/resenas/README.md).
//   3. Panel admin V2: /admin/resenas y /admin/resenas/<id> → /admin-web/resenas.html;
//      /admin/entrar → /admin-web/entrar.html. /admin/api/* no se reescribe: va a su propia behavior.
//   4. Reescribe paths sin extensión → .html (ej: /torneos → /torneos.html).
// Asociar a la distribution como event "viewer-request", solo en el default behavior.
function handler(event) {
  var request = event.request;
  var host = request.headers.host && request.headers.host.value;

  // www → apex redirect
  if (host && host.toLowerCase().indexOf('www.') === 0) {
    var newHost = host.substring(4);
    var qs = '';
    if (request.querystring && Object.keys(request.querystring).length) {
      var parts = [];
      for (var k in request.querystring) {
        parts.push(k + '=' + request.querystring[k].value);
      }
      qs = '?' + parts.join('&');
    }
    return {
      statusCode: 301,
      statusDescription: 'Moved Permanently',
      headers: {
        'location': { value: 'https://' + newHost + request.uri + qs }
      }
    };
  }

  var uri = request.uri;

  // /opinar/<token> → opinar.html. Va antes de la regla de extensionless: el token no tiene punto,
  // y sin esta regla quedaría como /opinar/<token>.html.
  if (uri.indexOf('/opinar/') === 0) {
    request.uri = '/opinar.html';
    return request;
  }

  // Panel admin V2. /admin/api/* no se reescribe: es la API y tiene su propia behavior.
  if (uri.indexOf('/admin/api/') === 0) {
    return request;
  }
  // /admin/resenas y /admin/resenas/<id> → la página del panel (el id lo lee la página).
  if (uri === '/admin/resenas' || uri.indexOf('/admin/resenas/') === 0) {
    request.uri = '/admin-web/resenas.html';
    return request;
  }
  // /admin/entrar → página de confirmación del acceso por correo.
  if (uri === '/admin/entrar') {
    request.uri = '/admin-web/entrar.html';
    return request;
  }

  // Extensionless path → append .html (except root /)
  if (uri !== '/' && uri.indexOf('.') === -1) {
    request.uri = uri + '.html';
  }

  return request;
}
