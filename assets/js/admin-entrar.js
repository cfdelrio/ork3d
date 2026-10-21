/* ORK3D · Confirmación de acceso al panel admin (V2).
 * El código llega en el fragmento (#c=...), que no viaja al servidor. Se lee y se borra de la
 * URL al cargar. Cargar la página no consume nada: solo el clic en "Confirmar acceso" llama a la API.
 */
(function () {
  'use strict';

  var A = window.OrkAdmin;
  var CODIGO_RE = /^[A-Za-z0-9_-]{43}$/;
  var MSG_INVALIDO = 'El enlace no es válido o venció. Pedí uno nuevo desde el panel.';

  var boton = document.getElementById('confirmar-acceso');
  var aviso = document.getElementById('aviso-acceso');

  var codigo = null;
  var enviando = false;

  function leerCodigo() {
    var coincidencia = /^#c=([A-Za-z0-9_-]+)$/.exec(window.location.hash || '');
    return coincidencia && CODIGO_RE.test(coincidencia[1]) ? coincidencia[1] : null;
  }

  codigo = leerCodigo();

  // Sacar el fragmento de la barra de direcciones y del historial, sin recargar la página.
  if (window.location.hash && window.history && window.history.replaceState) {
    window.history.replaceState(null, '', window.location.pathname);
  }

  if (codigo) {
    boton.disabled = false;
  } else {
    boton.disabled = true;
    A.avisar(aviso, MSG_INVALIDO, 'error');
  }

  boton.addEventListener('click', function () {
    if (enviando || !codigo) {
      return;
    }
    enviando = true;
    boton.disabled = true;
    A.avisar(aviso, '', '');

    A.llamar('POST', '/sesion/confirmar', { codigo: codigo }).then(function (resultado) {
      if (resultado.status === 200) {
        codigo = null;
        window.location.replace('/admin/resenas');
        return;
      }
      if (resultado.status === 401) {
        // Código inválido, vencido o ya usado: no se reintenta con el mismo.
        codigo = null;
        A.avisar(aviso, MSG_INVALIDO, 'error');
        return;
      }
      // Otro error del servidor: el código puede seguir vigente, así que se deja reintentar.
      A.avisar(aviso, 'No pudimos confirmar el acceso. Probá de nuevo.', 'error');
      boton.disabled = false;
    }, function () {
      // Error de red: el servidor no recibió nada, el código sigue sirviendo.
      A.avisar(aviso, A.MSG_RED, 'error');
      boton.disabled = false;
    }).then(function () {
      enviando = false;
    });
  });
})();
