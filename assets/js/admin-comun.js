/* ORK3D · Panel admin de reseñas (V2) · helpers comunes.
 * Sin frameworks. Nada de innerHTML: el contenido de clientes entra solo con textContent.
 * Ningún token ni dato del panel se guarda en localStorage ni sessionStorage.
 */
(function () {
  'use strict';

  var BASE = '/admin/api';

  var ESTADOS = {
    pending: { texto: 'Pendiente', clase: 'pildora--pendiente' },
    approved: { texto: 'Aprobada', clase: 'pildora--aprobada' },
    rejected: { texto: 'Rechazada', clase: 'pildora--rechazada' },
    hidden: { texto: 'Oculta', clase: 'pildora--oculta' }
  };

  var ASPECTOS = {
    diseno: 'Diseño',
    personalizacion: 'Personalización',
    calidad: 'Calidad',
    fecha: 'Fecha de entrega',
    atencion: 'Atención',
    resultado: 'Resultado final'
  };

  var MSG_RED = 'No pudimos conectar con el panel. Revisá tu conexión y probá de nuevo.';

  // Llama a la API admin (siempre mismo origen). Resuelve {status, datos}.
  // Solo rechaza ante error de red. Los GET no llevan cabeceras de mutación.
  function llamar(metodo, ruta, cuerpo) {
    var cabeceras = { Accept: 'application/json' };
    var opciones = {
      method: metodo,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: cabeceras
    };
    if (metodo !== 'GET') {
      cabeceras['Content-Type'] = 'application/json';
      cabeceras['X-Admin-Request'] = '1';
      opciones.body = JSON.stringify(cuerpo || {});
    }
    return fetch(BASE + ruta, opciones).then(function (respuesta) {
      return respuesta.text().then(function (texto) {
        var datos = {};
        if (texto) {
          try {
            datos = JSON.parse(texto);
          } catch (error) {
            datos = {};
          }
        }
        return { status: respuesta.status, datos: datos };
      });
    });
  }

  // Mensaje que manda el servidor (ya es texto para humanos) o un texto de respaldo.
  function mensajeDe(resultado, alternativo) {
    var datos = resultado && resultado.datos;
    if (datos && typeof datos.mensaje === 'string' && datos.mensaje) {
      return datos.mensaje;
    }
    return alternativo;
  }

  // Crea un elemento con atributos fijos y texto. Nunca interpreta HTML.
  function crear(etiqueta, atributos, texto) {
    var nodo = document.createElement(etiqueta);
    if (atributos) {
      Object.keys(atributos).forEach(function (nombre) {
        nodo.setAttribute(nombre, atributos[nombre]);
      });
    }
    if (texto !== undefined && texto !== null) {
      nodo.textContent = String(texto);
    }
    return nodo;
  }

  function vaciar(nodo) {
    while (nodo.firstChild) {
      nodo.removeChild(nodo.firstChild);
    }
  }

  // Estrellas como texto (★ y ☆) con nombre accesible. Rating fuera de 1..5 se acota.
  function estrellas(rating) {
    var n = Math.max(0, Math.min(5, Number(rating) || 0));
    var texto = '★'.repeat(n) + '☆'.repeat(5 - n);
    return crear('span', { 'class': 'estrellas', role: 'img', 'aria-label': n + ' de 5 estrellas' }, texto);
  }

  function fecha(iso) {
    if (!iso) {
      return '';
    }
    var d = new Date(iso);
    if (isNaN(d.getTime())) {
      return '';
    }
    return d.toLocaleDateString('es-AR', { day: '2-digit', month: 'short', year: 'numeric' });
  }

  // Muestra un aviso en una región aria-live. Texto vacío = oculto.
  function avisar(nodo, texto, tipo) {
    if (!nodo) {
      return;
    }
    nodo.textContent = texto || '';
    nodo.hidden = !texto;
    if (tipo) {
      nodo.setAttribute('data-tipo', tipo);
    }
  }

  window.OrkAdmin = {
    llamar: llamar,
    mensajeDe: mensajeDe,
    MSG_RED: MSG_RED,
    crear: crear,
    vaciar: vaciar,
    estrellas: estrellas,
    fecha: fecha,
    avisar: avisar,
    ESTADOS: ESTADOS,
    ASPECTOS: ASPECTOS
  };
})();
