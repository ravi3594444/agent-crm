// La página del canal de voz, sin navegador y sin red.
//
// Lo que se prueba es lo que falla EN SILENCIO: la página carga, el botón
// anda, y sin embargo el número no viaja o la conversación se ve vacía. Nada
// de eso levanta un error, así que sin estos tests se descubre en la demo.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(
  new URL('../plus-agent/static_voz/index.html', import.meta.url),
  'utf8',
);

// El módulo se ejecuta sin su import: `CallSession` se inyecta en el contexto,
// que es como el doble puede registrar con qué lo construyeron.
const guion = html
  .match(/<script type="module">([\s\S]*?)<\/script>/)[1]
  .replace(/^\s*import .*$/m, '');

function pagina({ busqueda = '', experiencia = { restaurant: 'Lácteos Plus' } } = {}) {
  const nodos = {};
  const construidas = [];

  function elemento(id = '') {
    const manejadores = {};
    return {
      id,
      textContent: '',
      value: '',
      hidden: false,
      disabled: false,
      className: '',
      scrollTop: 0,
      scrollHeight: 100,
      hijos: [],
      classList: { toggle() {}, add() {}, remove() {} },
      addEventListener(evento, callback) {
        manejadores[evento] = callback;
      },
      disparar(evento) {
        return manejadores[evento]?.();
      },
      append(...nuevos) {
        this.hijos.push(...nuevos);
      },
      // El texto que ve una persona: lo que la página realmente escribió.
      get texto() {
        return this.hijos.map((h) => h.texto ?? h.textContent ?? String(h)).join(' ');
      },
    };
  }

  for (const id of [
    'negocio', 'telefono', 'llamar', 'cortar', 'silenciar',
    'aviso', 'punto', 'estado', 'charla', 'vacio', 'ayuda-numero',
  ]) {
    nodos[id] = elemento(id);
  }

  const document = {
    getElementById: (id) => nodos[id] || null,
    createElement: () => elemento(),
    createTextNode: (texto) => ({ texto }),
  };

  class CallSessionFalsa {
    constructor(opciones) {
      this.opciones = opciones;
      this.arrancada = false;
      this.silenciada = false;
      construidas.push(this);
    }
    async start() {
      this.arrancada = true;
    }
    stop() {
      this.arrancada = false;
    }
    setMuted(valor) {
      this.silenciada = valor;
    }
  }

  const contexto = vm.createContext({
    document,
    location: { search: busqueda, origin: 'https://voz.example' },
    URLSearchParams,
    CallSession: CallSessionFalsa,
    fetch: async () => ({ ok: true, json: async () => experiencia }),
    console,
  });
  vm.runInContext(guion, contexto);
  return { nodos, construidas, contexto };
}

const proximoTick = () => new Promise((resolve) => setImmediate(resolve));

test('el número tipeado viaja en el passthrough, no en la conversación', async () => {
  // Es la única vía por la que el agente sabe quién llama. Si no viaja, la
  // demo atiende a todo el mundo como desconocido y no puede tomar un pedido
  // — sin ningún error a la vista.
  const { nodos, construidas } = pagina();
  nodos.telefono.value = '+54 9 351 123 4567';
  await nodos.llamar.disparar('click');
  assert.equal(construidas.length, 1);
  // Copiado a este realm: un objeto creado adentro del `vm` tiene otro
  // prototipo y `deepEqual` los compara, así que compararlo crudo falla aunque
  // el contenido sea idéntico.
  assert.deepEqual({ ...construidas[0].opciones.passthrough }, {
    telefono: '+54 9 351 123 4567',
  });
  assert.equal(construidas[0].arrancada, true);
});

test('sin número no se manda un parámetro vacío', async () => {
  const { nodos, construidas } = pagina();
  nodos.telefono.value = '   ';
  await nodos.llamar.disparar('click');
  assert.deepEqual({ ...construidas[0].opciones.passthrough }, {});
});

test('?telefono= en la URL llena el campo', async () => {
  const { nodos } = pagina({ busqueda: '?telefono=5493511234567' });
  assert.equal(nodos.telefono.value, '5493511234567');
});

test('la conversación se muestra venga el texto en text o en transcript', async () => {
  // El proveedor usa uno u otro según el mensaje. Contemplar sólo `text` deja
  // la pantalla vacía mientras la llamada funciona perfecto.
  const { nodos, construidas } = pagina();
  await nodos.llamar.disparar('click');
  const emitir = construidas[0].opciones.emit;

  emitir({ type: 'transcript.user', text: 'quiero quince kilos de muzzarella' });
  emitir({ type: 'transcript.agent', transcript: 'quince kilos, ¿para el jueves?' });

  assert.match(nodos.charla.texto, /quince kilos de muzzarella/);
  assert.match(nodos.charla.texto, /¿para el jueves\?/);
});

test('un transcript vacío no agrega un turno en blanco', async () => {
  const { nodos, construidas } = pagina();
  await nodos.llamar.disparar('click');
  construidas[0].opciones.emit({ type: 'transcript.user', text: '' });
  assert.equal(nodos.charla.hijos.length, 0);
});

test('el nombre del negocio sale del servidor y no del HTML', async () => {
  const { nodos } = pagina({ experiencia: { restaurant: 'Distribuidora Sur' } });
  await proximoTick();
  assert.equal(nodos.negocio.textContent, 'Distribuidora Sur');
});

test('sin clave en el servidor, la página lo dice antes de que alguien llame', async () => {
  const { nodos } = pagina({
    experiencia: { restaurant: 'Lácteos Plus', live_configured: false },
  });
  await proximoTick();
  assert.equal(nodos.aviso.hidden, false);
  assert.match(nodos.aviso.textContent, /ASSEMBLYAI_API_KEY/);
});

test('silenciar y cortar llegan a la sesión', async () => {
  const { nodos, construidas } = pagina();
  await nodos.llamar.disparar('click');
  nodos.silenciar.disparar('click');
  assert.equal(construidas[0].silenciada, true);
  nodos.silenciar.disparar('click');
  assert.equal(construidas[0].silenciada, false);
  nodos.cortar.disparar('click');
  assert.equal(construidas[0].arrancada, false);
});

test('un error de la llamada se ve en pantalla', async () => {
  const { nodos, construidas } = pagina();
  await nodos.llamar.disparar('click');
  construidas[0].opciones.emit({ type: 'error', message: 'No se pudo conectar.' });
  assert.equal(nodos.aviso.hidden, false);
  assert.match(nodos.aviso.textContent, /No se pudo conectar/);
});
