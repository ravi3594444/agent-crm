// Exercise application logic and event handlers without a browser or network.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../plus-agent/app/dashboard_ui/app.js', import.meta.url), 'utf8');

function workspace() {
  const listeners = {}, nodes = {}, copied = [], downloads = [], requests = [];
  let document;
  function element(id) {
    const handlers = {};
    return {
      id, innerHTML: '', textContent: '', open: false, disabled: false, value: '',
      selectionStart: 0, tagName: 'DIV', dataset: {},
      classList: { add() {}, remove() {}, toggle() {} },
      addEventListener(event, callback) { handlers[event] = callback; },
      showModal() { this.open = true; },
      close() { this.open = false; handlers.close?.(); },
      focus() { document.activeElement = this; },
      setSelectionRange(start) { this.selectionStart = start; },
      click() { downloads.push(this.download); },
      querySelector() { return this.button; },
      reset() { this.fields = {}; },
    };
  }
  for (const id of ['app', 'toast', 'detail-dialog', 'connection-dialog', 'search', 'connection-error', 'chart-detail']) nodes[id] = element(id);
  nodes.search.tagName = 'INPUT';
  const dialogs = [nodes['detail-dialog'], nodes['connection-dialog']];
  document = {
    activeElement: null, visibilityState: 'visible',
    querySelector(selector) { return selector === 'dialog[open]' ? dialogs.find(d => d.open) : nodes[selector.slice(1)] || null; },
    querySelectorAll(selector) { return selector === 'dialog' ? dialogs : selector === 'dialog[open]' ? dialogs.filter(d => d.open) : []; },
    addEventListener(event, callback) { listeners[event] = callback; },
    createElement: element,
  };
  const context = vm.createContext({
    document, location: { pathname: '/', search: '', hash: '', origin: 'https://dashboard.example' },
    history: { replaceState() {} }, window: { addEventListener() {}, scrollTo() {} },
    navigator: { clipboard: { async writeText(value) { copied.push(value); } } },
    URL: class extends URL {
      static createObjectURL(blob) { downloads.push(blob); return 'blob:test'; }
      static revokeObjectURL() {}
    },
    URLSearchParams, Intl, Date, Blob, AbortSignal,
    FormData: class { constructor(form) { this.fields = form.fields; } get(key) { return this.fields[key]; } },
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 1; },
    fetch: async (...args) => { requests.push(args); throw new Error('No network fixture configured'); },
  });
  vm.runInContext(source, context);
  const run = code => vm.runInContext(code, context);
  const click = dataset => listeners.click({ target: { closest: () => ({ dataset }) }, preventDefault() {} });
  const fixture = () => JSON.parse(run("JSON.stringify({...makeDemo(),mode:'live',pendingOrders:makeDemo().orders.filter(o=>o.status==='pending')})"));
  const live = () => {
    context.snapshotFixture = fixture();
    run("data=validateSnapshot(snapshotFixture);state.connection={base:'https://agent.example',token:'dashboard-fixture-token-at-least-32-characters'};state.session++;render()");
  };
  const form = () => Object.assign(element('connect-form'), {
    fields: { url: 'https://agent.example', token: 'dashboard-fixture-token-at-least-32-characters' },
    button: element('submit'),
  });
  const submit = target => listeners.submit({ target, preventDefault() {} });
  return { context, run, click, fixture, live, nodes, copied, downloads, requests, listeners, form, submit };
}

const response = value => ({ ok: true, json: async () => value });
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }

test('starts signed out and every navigation view renders after explicit demo selection', async () => {
  const w = workspace();
  assert.equal(w.run('data.mode'), 'disconnected');
  assert.equal(w.run('data.orders'), null);
  assert.match(w.nodes.app.innerHTML, /Open your live CRM workspace/);
  await w.click({ action: 'demo' });
  assert.equal(w.run('data.mode'), 'demo');
  for (const view of ['overview', 'orders', 'inventory', 'customers', 'agents', 'settings']) {
    await w.click({ view });
    assert.equal(w.run('state.view'), view);
    assert.ok(w.nodes.app.innerHTML.length > 1000);
  }
  await w.click({ action: 'menu' });
  assert.equal(w.run('state.menu'), true);
  await w.click({ action: 'close-menu' });
  assert.equal(w.run('state.menu'), false);
});

test('search, status and stock filters, pagination, charts, details, copy and CSV work', async () => {
  const w = workspace();
  await w.click({ action: 'demo' });
  await w.click({ view: 'orders' });
  await w.click({ action: 'next' });
  assert.equal(w.run('state.page'), 2);
  await w.click({ action: 'prev' });
  assert.equal(w.run('state.page'), 1);
  await w.click({ filter: 'pending' });
  assert.equal(w.run("selectedOrders().every(o=>o.status==='pending')"), true);
  w.nodes.search.value = 'La Buena';
  w.nodes.search.selectionStart = 3;
  w.listeners.input({ target: w.nodes.search });
  assert.equal(w.run("selectedOrders().every(o=>o.customer.includes('La Buena'))"), true);
  assert.equal(w.nodes.search.selectionStart, 3);
  await w.click({ view: 'overview' });
  const day = w.run('data.today');
  await w.click({ chartDate: day });
  assert.match(w.nodes['chart-detail'].textContent, /9 orders/);
  const id = w.run('data.orders[0].id');
  await w.run('showOrder(data.orders[0].id)');
  assert.match(w.nodes['detail-dialog'].innerHTML, /Order items/);
  await w.click({ copy: id });
  assert.deepEqual(w.copied, [id]);
  await w.click({ close: 'detail-dialog' });
  assert.equal(w.nodes['detail-dialog'].open, false);
  w.run('showProduct(data.products[0].id)');
  assert.match(w.nodes['detail-dialog'].innerHTML, /400/);
  w.run('showCustomer(data.customers[0].id)');
  assert.match(w.nodes['detail-dialog'].innerHTML, /Recent orders/);
  await w.click({ view: 'inventory' });
  w.listeners.change({ target: { id: 'stock-filter', value: 'low' } });
  assert.match(w.nodes.app.innerHTML, /Creamy cheese/);
  assert.doesNotMatch(w.nodes.app.innerHTML, /Whole milk/);
  await w.click({ view: 'orders' });
  w.run("data.orders[0].customer='=SUM(1,2)'");
  await w.click({ action: 'export' });
  const csv = await w.downloads.find(value => value instanceof Blob).text();
  assert.match(csv, /Order ID/);
  assert.match(csv, /"'=SUM\(1,2\)"/);
  assert.ok(w.downloads.some(value => typeof value === 'string' && value.endsWith('.csv')));
});

test('pending review includes old drafts independently of reporting dates or recent-data outages', async () => {
  const w = workspace();
  w.live();
  w.run("data.pendingOrders=[{...data.orders[0],id:'OLD-DRAFT',status:'pending',date:dateShift(data.today,-90)}]");
  await w.click({ action: 'pending' });
  w.listeners.change({ target: { id: 'range', value: '7' } });
  assert.equal(w.run('selectedOrders()[0].id'), 'OLD-DRAFT');
  w.run('data.orders=null;render()');
  assert.match(w.nodes.app.innerHTML, /data-order="OLD-DRAFT"/);
  w.run('data.pendingOrders=null;render()');
  assert.match(w.nodes.app.innerHTML, /Order data is unavailable/);
  await w.click({ action: 'export' });
  assert.equal(w.downloads.length, 0);
  assert.match(w.nodes.toast.textContent, /unavailable/);
});

test('live order detail uses authenticated API and an old response cannot replace another detail', async () => {
  const w = workspace();
  w.live();
  const order = w.fixture().orders[0];
  order.items = [{ name: 'Actual ERP item', qty: 2, rate: 10, amount: 20 }];
  w.context.fetch = async (url, options) => {
    assert.equal(url, 'https://agent.example/api/dashboard/orders/' + order.id);
    assert.match(options.headers.Authorization, /^Bearer /);
    assert.equal(options.credentials, 'omit');
    assert.equal(options.redirect, 'error');
    return response(order);
  };
  await w.run('showOrder(data.orders[0].id)');
  assert.match(w.nodes['detail-dialog'].innerHTML, /Actual ERP item/);
  const pending = deferred();
  w.context.fetch = () => pending.promise;
  const request = w.run('showOrder(data.orders[0].id)');
  w.run('showProduct(data.products[0].id)');
  const productDetail = w.nodes['detail-dialog'].innerHTML;
  pending.resolve(response(order));
  await request;
  assert.equal(w.nodes['detail-dialog'].innerHTML, productDetail);
});

test('agent controls and queue status show real values and clear failed reads', async () => {
  const w = workspace();
  w.live();
  w.context.fetch = async url => response(url.endsWith('/controls')
    ? { policies: [{ name: 'Owner limit', value: '75000', source: 'Owner', unit: '$' }] }
    : { redis: 'Connected', worker: 'Active lease', queuedMessages: 3, queuedNotices: 4, failedReplies: 0, failedNotices: 1 });
  w.run("state.view='agents'");
  await w.run('loadExtras(true)');
  assert.match(w.nodes.app.innerHTML, /75000/);
  assert.match(w.nodes.app.innerHTML, /Active lease/);
  w.context.fetch = async () => ({ ok: false, status: 502 });
  await w.run('loadExtras(true)');
  assert.equal(w.run('data.policies'), null);
  assert.equal(w.run('data.operations'), null);
  assert.match(w.nodes.app.innerHTML, /Current limits could not be read/);
  assert.doesNotMatch(w.nodes.app.innerHTML, /75000/);
});

test('refresh errors preserve a visibly stale snapshot; sign-out invalidates pending refresh', async () => {
  const w = workspace();
  w.live();
  w.context.fetch = async () => ({ ok: false, status: 502 });
  await w.run('refresh()');
  assert.equal(w.run('state.stale'), true);
  assert.equal(w.run('data.mode'), 'live');
  assert.match(w.nodes.app.innerHTML, /Connection interrupted/);
  const pending = deferred();
  w.context.fetch = () => pending.promise;
  const refresh = w.run('refresh()');
  await w.click({ action: 'disconnect' });
  pending.resolve(response(w.fixture()));
  await refresh;
  assert.equal(w.run('data.mode'), 'disconnected');
  assert.equal(w.run('data.orders'), null);
  assert.equal(w.run('state.connection'), null);
});

test('sign-in rejects unsafe origins and closing its dialog cancels an unfinished connection', async () => {
  const w = workspace();
  w.run('openConnection()');
  const invalid = w.form();
  invalid.fields.url = 'http://remote.example';
  await w.submit(invalid);
  assert.match(w.nodes['connection-error'].textContent, /HTTPS service origin/);
  const pending = deferred();
  w.context.fetch = () => pending.promise;
  const attempt = w.submit(w.form());
  await w.click({ close: 'connection-dialog' });
  pending.resolve(response(w.fixture()));
  await attempt;
  assert.equal(w.run('state.connection'), null);
  w.run('openConnection()');
  w.context.fetch = async () => response(w.fixture());
  await w.submit(w.form());
  assert.equal(w.run('data.mode'), 'live');
  assert.equal(w.nodes['connection-dialog'].open, false);
  assert.doesNotMatch(w.nodes.app.innerHTML, /dashboard-fixture-token/);
});
