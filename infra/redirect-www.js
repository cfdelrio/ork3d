// CloudFront Function: redirige www.ork3d.com → ork3d.com (apex canónico).
// Asociar a la distribution como event "viewer-request".
function handler(event) {
  var request = event.request;
  var host = request.headers.host && request.headers.host.value;
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
  return request;
}
