// CloudFront Function:
//   1. Redirige www.ork3d.com → ork3d.com (apex canónico).
//   2. Reescribe paths sin extensión → .html (ej: /torneos → /torneos.html).
// Asociar a la distribution como event "viewer-request".
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

  // Extensionless path → append .html (except root /)
  var uri = request.uri;
  if (uri !== '/' && uri.indexOf('.') === -1) {
    request.uri = uri + '.html';
  }

  return request;
}
