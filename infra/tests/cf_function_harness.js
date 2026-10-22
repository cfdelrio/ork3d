// Ejecuta el handler de una CloudFront Function (cloudfront-js) con eventos de prueba. Solo para tests.
//
// Entrada (stdin, JSON): { "funciones": [ruta, ...], "casos": [{ "host", "uri", "qs" }, ...] }
// Salida (stdout, JSON): para cada función, la respuesta del handler a cada caso, en el mismo orden.
// Cada función corre en su propio contexto vm: no ve variables globales de este proceso.
const fs = require('fs');
const vm = require('vm');

const entrada = JSON.parse(fs.readFileSync(0, 'utf8'));
const resultados = entrada.funciones.map((ruta) => {
  const contexto = {};
  vm.createContext(contexto);
  vm.runInContext(fs.readFileSync(ruta, 'utf8') + '\n;this.handler = handler;', contexto);
  return entrada.casos.map((caso) => {
    const evento = {
      request: {
        method: 'GET',
        uri: caso.uri,
        querystring: caso.qs || {},
        headers: { host: { value: caso.host } },
      },
    };
    return contexto.handler(evento);
  });
});
process.stdout.write(JSON.stringify(resultados));
