// Exercise application logic and event handlers without a browser or network.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../plus-agent/app/dashboard_ui/app.js', import.meta.url), 'utf8');

function workspace(options = {}) {
  const listeners = {}, nodes = {}, copied = [], downloads = [], requests = [];
  const preferences = new Map(options.preferences || []), timeouts = new Map(), decorations = [];
  // MONOTÓNICO, no `timeouts.size + 1`: el tamaño BAJA con cada clearTimeout,
  // así que el id se reusaba y el registro del timer vivo se pisaba. Pasa en
  // el camino real de exportar: toast() toma el 1, el cleanup de 1000 ms toma
  // el 2, toast() borra el 1 y el siguiente vuelve a ser 2.
  let ultimoTimer = 0;
  let document;
  function element(id) {
    const handlers = {};
    return {
      id, innerHTML: '', textContent: '', open: false, disabled: false, value: '',
      selectionStart: 0, tagName: 'DIV', dataset: {},
      setAttribute(name, value) { this[name] = value; },
      remove() { this.removed = true; },
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
  // `querySelector` sólo resuelve los ids de esta lista: uno que falte devuelve
  // null y el primer render que lo lea se cae con un mensaje que no dice nada.
  for (const id of ['app', 'toast', 'detail-dialog', 'connection-dialog', 'setting-dialog', 'search', 'connection-error', 'chart-detail', 'setting-error']) nodes[id] = element(id);
  nodes.search.tagName = 'INPUT';
  // Los TRES diálogos. Esta lista es la que ve `document.querySelectorAll('dialog')`,
  // que es donde se enganchan los listeners de cerrar: uno que no esté acá
  // existe en index.html y no se cierra nunca en los tests.
  const dialogs = [nodes['detail-dialog'], nodes['connection-dialog'], nodes['setting-dialog']];
  document = {
    activeElement: null, visibilityState: 'visible', documentElement: { dataset: {} },
    body: { append(node) { decorations.push(node); } },
    querySelector(selector) { return selector === 'dialog[open]' ? dialogs.find(d => d.open) : nodes[selector.slice(1)] || null; },
    querySelectorAll(selector) { return selector === 'dialog' ? dialogs : selector === 'dialog[open]' ? dialogs.filter(d => d.open) : []; },
    addEventListener(event, callback) { listeners[event] = callback; },
    createElement: element,
  };
  const context = vm.createContext({
    document, location: { pathname: '/', search: options.search || '', hash: options.hash || '', origin: 'https://dashboard.example' },
    history: { replaceState() {} }, window: { addEventListener() {}, scrollTo() {} },
    navigator: { clipboard: { async writeText(value) { copied.push(value); } } },
    URL: class extends URL {
      static createObjectURL(blob) { downloads.push(blob); return 'blob:test'; }
      static revokeObjectURL() {}
    },
    URLSearchParams, Intl, Date, Blob, AbortSignal,
    localStorage: {
      getItem: key => preferences.get(key) || null,
      setItem: (key, value) => preferences.set(key, value),
      removeItem: key => preferences.delete(key),
    },
    FormData: class { constructor(form) { this.fields = form.fields; } get(key) { return this.fields[key]; } },
    setTimeout(callback, ms) { const id = ++ultimoTimer; timeouts.set(id, { callback, ms }); return id; }, clearTimeout(id) { timeouts.delete(id); }, setInterval() { return 1; },
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
  // El formulario de un ajuste. `fields` es lo que lee el `FormData` de arriba.
  const settingForm = (setting, value) => Object.assign(element('setting-form'), {
    fields: { setting, value }, button: element('submit'),
  });
  return { context, run, click, fixture, live, nodes, copied, downloads, requests, listeners, form, submit, settingForm, preferences, timeouts, decorations };
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

// Deliberately artificial rates make unit direction and amount conversion testable.
const fxFixture = (target = 'INR', rates = { INR: 1, ARS: 20, USD: 0.01 }) => ({
  result: 'success', base_code: target, rates,
  time_last_update_unix: Math.floor(Date.now() / 1000),
});

test('currency selection converts monetary values, preserves original records and exports both', async () => {
  const w = workspace();
  w.live();
  const before = w.run('JSON.stringify(data.orders)');
  let requests = 0;
  w.context.fetch = async (url, options) => {
    requests++;
    assert.equal(url, 'https://open.er-api.com/v6/latest/INR');
    assert.equal(options.credentials, 'omit');
    assert.equal(options.headers?.Authorization, undefined);
    assert.equal(options.body, undefined);
    return response(fxFixture());
  };
  await w.listeners.change({ target: { id: 'display-currency', value: 'INR' } });
  assert.equal(w.run('state.displayCurrency'), 'INR');
  assert.equal(w.run("displayAmount(1000,'ARS').value"), 50);
  assert.equal(w.run("displayAmount(5,'USD').value"), 500);
  assert.equal(w.run("displayAmount(50,'INR').value"), 50);
  assert.equal(w.run("displayAmount(null,'ARS').value"), null);
  assert.match(w.run("moneyFor(1000,'ARS')"), /INR.*50\.00/);
  assert.match(w.nodes.app.innerHTML, /Display estimates in/);
  assert.match(w.nodes.app.innerHTML, /Rates By Exchange Rate API/);
  assert.equal(w.run('JSON.stringify(data.orders)'), before);
  assert.deepEqual([...w.preferences.entries()], [['plus.dashboard.displayCurrency', 'INR']]);
  w.run('renderOrder(data.orders[0])');
  assert.match(w.nodes['detail-dialog'].innerHTML, /Original order total/);
  assert.match(w.nodes['detail-dialog'].innerHTML, /ARS/);
  assert.match(w.nodes['detail-dialog'].innerHTML, /INR/);
  await w.click({ action: 'export' });
  const csv = await w.downloads.find(v => v instanceof Blob).text();
  assert.match(csv, /Original amount/);
  assert.match(csv, /Display amount/);
  assert.match(csv, /ARS/);
  assert.match(csv, /INR/);
  assert.match(csv, /Display estimate/);
  await w.run("setDisplayCurrency('')");
  assert.equal(w.run("displayAmount(1000,'ARS').value"), 1000);
  assert.equal(w.preferences.size, 0);
  await w.run("setDisplayCurrency('INR')");
  assert.equal(requests, 1, 'recent public rates are reused');
});

test('failed and invalid rates preserve the last currency; unsupported records stay original', async () => {
  const w = workspace();
  w.live();
  w.context.fetch = async () => response(fxFixture());
  await w.run("setDisplayCurrency('INR')");
  w.context.fetch = async () => ({ ok: false, status: 429 });
  await w.run("setDisplayCurrency('EUR')");
  assert.equal(w.run('state.displayCurrency'), 'INR');
  assert.match(w.nodes.app.innerHTML, /displayed currency has been kept/);
  assert.equal(w.run("displayAmount(1000,'XYZ').value"), 1000);
  assert.match(w.run("moneyFor(1000,'XYZ')"), /original/);
  for (const invalid of [
    { ...fxFixture('EUR', { EUR: 1, ARS: 0 }), time_last_update_unix: 0 },
    fxFixture('EUR', { EUR: 1, ARS: -5 }),
    fxFixture('USD'),
    fxFixture('EUR', { EUR: 2, ARS: 20 }),
  ]) {
    w.context.fetch = async () => response(invalid);
    await w.run("setDisplayCurrency('EUR')");
    assert.equal(w.run('state.displayCurrency'), 'INR');
    assert.equal(w.run('state.fxLoading'), false);
  }
});

test('a rate response after sign-out cannot apply to another session', async () => {
  const w = workspace();
  w.live();
  const pending = deferred();
  w.context.fetch = () => pending.promise;
  const changing = w.run("setDisplayCurrency('INR')");
  await w.click({ action: 'disconnect' });
  pending.resolve(response(fxFixture()));
  await changing;
  assert.equal(w.run('state.displayCurrency'), '');
  assert.equal(w.run('state.fxLoading'), false);
  assert.equal(w.preferences.size, 0);
});

test('a revoked token during refresh ends the session instead of leaving CRM data on screen', async () => {
  // A 502 is transient: the snapshot stays, marked stale (covered above). A 401
  // is not — the token stopped working — and keeping orders, customers and
  // inventory visible until someone reloads by hand is the security half.
  const w = workspace();
  w.live();
  assert.equal(w.run('data.mode'), 'live');
  w.context.fetch = async () => ({ ok: false, status: 401 });
  await w.run('refresh()');
  assert.equal(w.run('data.mode'), 'disconnected');
  assert.equal(w.run('data.orders'), null);
  assert.equal(w.run('data.customers'), null);
  assert.equal(w.run('state.connection'), null);
  // And NOT the stale path: a stale flag with no data is a different screen.
  assert.equal(w.run('state.stale'), false);
});

test('the sidebar names the connected workspace, never a hard-coded person', async () => {
  // Every deployment showed "Ravi / Workspace owner / ADMIN". No token and no
  // snapshot field supports that, so it was a false claim about who is looking.
  const w = workspace();
  w.live();
  const sidebar = w.nodes.app.innerHTML;
  assert.doesNotMatch(sidebar, /Ravi/);
  assert.doesNotMatch(sidebar, /Workspace owner/);
  // What it says instead comes from the snapshot, and is escaped.
  const company = w.run('data.company');
  assert.ok(company);
  assert.match(sidebar, new RegExp(company.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
});

const activityFixture = () => ({
  date: '2026-09-13',
  conversations: [
    { customerId: 'CUST-001', customerName: 'Ordered shop', turns: 4, lastAt: '2026-09-13T15:00:00Z', lastLine: 'Please deliver tomorrow.', orderId: 'ORDER-PLACED' },
    { customerId: 'CUST-OUTSIDE-SNAPSHOT', customerName: 'Bakery <North>', turns: 2, lastAt: '2026-09-13T16:00:00Z', lastLine: '<img src=x onerror=alert(1)> & maybe tomorrow', orderId: null },
  ],
  newCustomers: [{ customerId: 'CUST-001', customerName: 'Ordered shop', turns: 4, lastAt: '2026-09-13T15:00:00Z', lastLine: 'My first order.', orderId: 'ORDER-PLACED' }],
  truncated: ['conversations'],
});
const queueFixture = () => ({
  upcoming: [
    { id: 'FUTURE-2', type: 'a_new_server_type', orderId: 'ORDER-LATER', customer: 'Later shop', dueAt: '2026-09-14T20:00:00Z', what: 'Ask whether the delivery arrived safely.' },
    { id: 'FUTURE-1', type: 'another_new_type', orderId: 'ORDER-SOON', customer: 'Soon shop', dueAt: '2026-09-13T18:00:00Z', what: 'Remind the customer <tomorrow> & check their address.' },
  ],
  waitingOnAPerson: [{ orderId: 'ORDER-REVIEW', customer: 'Waiting shop', since: '2026-09-13T12:00:00Z', what: 'Price above the auto-confirm limit.' }],
  undelivered: { replies: 3, notices: 1 },
});
const operationsFixture = () => ({ redis: 'Connected', worker: 'Active lease', queuedMessages: 4, queuedNotices: null, failedReplies: 7, failedNotices: 2 });
const transcriptFixture = (id, text = 'Retained customer message') => ({
  customerId: id, customerName: `Customer ${id}`, reachable: true, truncated: true, retentionDays: 14,
  mobile_no: 'PHONE-FIELD-MUST-NOT-RENDER', systemPrompt: 'SYSTEM-PROMPT-MUST-NOT-RENDER',
  messages: [
    { role: 'customer', text, at: '2026-09-13T12:00:00Z' },
    { role: 'note', text: 'The agent looked up the catalogue.', at: '2026-09-13T12:01:00Z', tool: 'PRIVATE-TOOL-NAME', args: { private: 'PRIVATE-ARGUMENT' } },
    { role: 'agent', text: 'I can prepare a draft for you.', at: '2026-09-13T12:02:00Z' },
  ],
});
function apiFixture(w, routes) {
  w.context.fetch = async (url, options) => {
    w.requests.push([url, options]);
    assert.equal(options.credentials, 'omit');
    assert.equal(options.redirect, 'error');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.headers.Authorization, 'Bearer dashboard-fixture-token-at-least-32-characters');
    assert.equal(options.body, undefined);
    const path = new URL(url).pathname;
    assert.ok(Object.hasOwn(routes, path), `Unexpected read: ${path}`);
    return response(typeof routes[path] === 'function' ? routes[path](path) : routes[path]);
  };
}

test('demo starts on Today and supplies all new panels and conversation empty states', async () => {
  const w = workspace({ search: '?demo=1' });
  assert.equal(w.run('state.view'), 'today');
  assert.match(w.nodes.app.innerHTML, /No order yet/);
  assert.match(w.nodes.app.innerHTML, /First orders today/);
  const nav = JSON.parse(w.run('JSON.stringify(nav)'));
  assert.deepEqual(nav[0], ['today', 'Today']);
  assert.ok(!nav.some(([id]) => id === 'conversations'));
  assert.equal(w.requests.length, 0);
  await w.click({ view: 'queue' });
  assert.match(w.nodes.app.innerHTML, /orders waiting on a person/);
  assert.match(w.nodes.app.innerHTML, /Remind the customer their order arrives/);
  await w.run("showCustomer('CUST-002')");
  assert.match(w.nodes['detail-dialog'].innerHTML, /Earlier messages were omitted/);
  assert.match(w.nodes['detail-dialog'].innerHTML, /The agent looked up the catalogue/);
  await w.run("showCustomer('CUST-004')");
  assert.match(w.nodes['detail-dialog'].innerHTML, /No WhatsApp number on file for this customer/);
  await w.run("showCustomer('CUST-005')");
  assert.match(w.nodes['detail-dialog'].innerHTML, /No retained messages/);
  assert.equal(w.requests.length, 0);
  const linked = workspace({ search: '?demo=1', hash: '#queue' });
  assert.match(linked.nodes.app.innerHTML, /Scheduled work/);
});

test('Today reads its own contract and opens customers absent from the capped snapshot', async () => {
  const w = workspace();
  w.live();
  apiFixture(w, {
    '/api/dashboard/today': activityFixture(),
    '/api/dashboard/customers/CUST-OUTSIDE-SNAPSHOT/conversation': transcriptFixture('CUST-OUTSIDE-SNAPSHOT'),
  });
  await w.click({ view: 'today' });
  assert.equal(w.requests.length, 1);
  const html = w.nodes.app.innerHTML;
  assert.match(html, /without-order/);
  assert.match(html, /Bakery &lt;North&gt;/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt; &amp; maybe tomorrow/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /lists are incomplete/);
  assert.match(html, /data-order="ORDER-PLACED"/);
  assert.ok(html.indexOf('Bakery &lt;North&gt;') < html.indexOf('Ordered shop'));
  await w.run("showCustomer('CUST-OUTSIDE-SNAPSHOT')");
  assert.match(w.nodes['detail-dialog'].innerHTML, /Retained customer message/);
  assert.match(w.nodes['detail-dialog'].innerHTML, /Bakery &lt;North&gt;/);
  assert.equal(w.requests.length, 2);
});

test('Overview and Coming up show server prose, waiting decisions, and independent delivery counts', async () => {
  const w = workspace();
  w.live();
  apiFixture(w, { '/api/dashboard/queue': queueFixture(), '/api/dashboard/operations': operationsFixture() });
  await w.click({ view: 'overview' });
  let html = w.nodes.app.innerHTML;
  assert.match(html, /1 order waiting on a person/);
  assert.match(html, /<strong>7<\/strong> replies never reached a customer/);
  assert.match(html, /<strong>2<\/strong> notices were not delivered/);
  assert.match(html, /Remind the customer &lt;tomorrow&gt; &amp; check their address/);
  assert.ok(html.indexOf('ORDER-SOON') < html.indexOf('ORDER-LATER'));
  assert.doesNotMatch(html, /a_new_server_type|another_new_type/);
  assert.deepEqual(w.requests.map(([url]) => new URL(url).pathname).sort(), ['/api/dashboard/operations', '/api/dashboard/queue']);
  await w.click({ view: 'queue' });
  html = w.nodes.app.innerHTML;
  assert.match(html, /1 order waiting on a person/);
  assert.match(html, /Remind the customer &lt;tomorrow&gt; &amp; check their address/);
  assert.ok(html.indexOf('ORDER-SOON') < html.indexOf('ORDER-LATER'));
  assert.match(html, /Price above the auto-confirm limit/);
  assert.match(html, /data-order="ORDER-REVIEW"/);
  assert.match(html, /<strong>3<\/strong> replies never reached a customer/);
  assert.equal(w.requests.length, 2, 'navigation reuses a recent read');
  apiFixture(w, { '/api/dashboard/queue': { upcoming: [], waitingOnAPerson: [], undelivered: { replies: 0, notices: 0 } } });
  await w.click({ read: 'queue' });
  assert.match(w.nodes.app.innerHTML, /Nothing scheduled/);
  assert.match(w.nodes.app.innerHTML, /No decisions waiting/);
  assert.doesNotMatch(w.nodes.app.innerHTML, /ORDER-REVIEW/);
});

test('transcripts display only permitted fields, escape text, and state retention honestly', async () => {
  const w = workspace();
  w.live();
  apiFixture(w, { '/api/dashboard/customers/CUST-001/conversation': transcriptFixture('CUST-001', '<script>bad()</script> & hello') });
  await w.run("showCustomer('CUST-001')");
  const html = w.nodes['detail-dialog'].innerHTML;
  assert.match(html, /&lt;script&gt;bad\(\)&lt;\/script&gt; &amp; hello/);
  assert.doesNotMatch(html, /<script>|PHONE-FIELD|SYSTEM-PROMPT|PRIVATE-TOOL|PRIVATE-ARGUMENT/);
  assert.ok(html.indexOf('Recent orders') < html.indexOf('WhatsApp conversation'));
  assert.ok(html.indexOf('bad()') < html.indexOf('The agent looked up'));
  assert.ok(html.indexOf('The agent looked up') < html.indexOf('I can prepare'));
  assert.match(html, /retained for 14 days/);
  assert.match(html, /Earlier messages were omitted/);
  const unreachable = { ...transcriptFixture('CUST-001'), reachable: false, messages: [] };
  apiFixture(w, { '/api/dashboard/customers/CUST-001/conversation': unreachable });
  await w.run("showCustomer('CUST-001')");
  assert.match(w.nodes['detail-dialog'].innerHTML, /No WhatsApp number on file/);
  assert.match(w.nodes['detail-dialog'].innerHTML, /retained for 14 days/);
});

test('new validators reject malformed fields independently and accept a long retained transcript', async () => {
  const w = workspace();
  w.live();
  const ordersBefore = w.run('JSON.stringify(data.orders)');
  const invalids = [
    ['validateActivity', { ...activityFixture(), date: '2026-02-31' }],
    ['validateActivity', { ...activityFixture(), conversations: [{ ...activityFixture().conversations[0], orderId: undefined }] }],
    ['validateActivity', { ...activityFixture(), newCustomers: [{ ...activityFixture().newCustomers[0], lastAt: 'yesterday' }] }],
    ['validateQueue', { ...queueFixture(), upcoming: [{ ...queueFixture().upcoming[0], dueAt: '2026-02-31T12:00:00Z' }] }],
    ['validateQueue', { ...queueFixture(), undelivered: { replies: -1, notices: 0 } }],
    ['validateQueue', { ...queueFixture(), waitingOnAPerson: [{ ...queueFixture().waitingOnAPerson[0], since: 'invalid' }] }],
    ['validateQueue', { ...queueFixture(), upcoming: [{ ...queueFixture().upcoming[0], what: { unsafe: true } }] }],
    ['validateOperations', { ...operationsFixture(), failedReplies: '7' }],
  ];
  for (const [validator, fixture] of invalids) {
    w.context.invalidFixture = fixture;
    assert.throws(() => w.run(`${validator}(invalidFixture)`), /invalid/);
  }
  for (const fixture of [
    { ...transcriptFixture('WRONG-CUSTOMER') },
    { ...transcriptFixture('CUST-001'), messages: [{ role: 'tool', text: 'secret', at: '2026-09-13T12:00:00Z' }] },
    { ...transcriptFixture('CUST-001'), messages: [{ role: 'system', text: 'secret', at: '2026-09-13T12:00:00Z' }] },
    { ...transcriptFixture('CUST-001'), retentionDays: 'forever' },
    { ...transcriptFixture('CUST-001'), reachable: false },
  ]) {
    w.context.invalidFixture = fixture;
    assert.throws(() => w.run("validateConversation(invalidFixture,'CUST-001')"), /invalid/);
  }
  w.context.longTranscript = { ...transcriptFixture('CUST-001'), messages: Array.from({ length: 300 }, (_, i) => ({ role: 'customer', text: `Turn ${i}`, at: '2026-09-13T12:00:00Z' })) };
  assert.equal(w.run("validateConversation(longTranscript,'CUST-001').messages.length"), 300);
  assert.equal(w.run('JSON.stringify(data.orders)'), ordersBefore);
});

test('late transcripts cannot replace another customer, an order, or a closed dialog', async () => {
  for (const destination of ['customer', 'order', 'close']) {
    const w = workspace();
    w.live();
    const pending = deferred();
    w.context.fetch = () => pending.promise;
    const reading = w.run("showCustomer('CUST-001')");
    if (destination === 'customer') {
      apiFixture(w, { '/api/dashboard/customers/CUST-002/conversation': transcriptFixture('CUST-002', 'The second customer') });
      await w.run("showCustomer('CUST-002')");
    } else if (destination === 'order') {
      apiFixture(w, { '/api/dashboard/orders/ORDER-NEXT': { ...w.fixture().orders[0], id: 'ORDER-NEXT' } });
      await w.run("showOrder('ORDER-NEXT')");
    } else {
      await w.click({ close: 'detail-dialog' });
    }
    const expected = w.nodes['detail-dialog'].innerHTML;
    pending.resolve(response(transcriptFixture('CUST-001', 'LATE PRIVATE MESSAGE')));
    await reading;
    assert.equal(w.nodes['detail-dialog'].innerHTML, expected, destination);
    assert.doesNotMatch(w.nodes['detail-dialog'].innerHTML, /LATE PRIVATE MESSAGE/);
  }
});

test('every authenticated detail or lazy read handles a revoked token with the single session reset', async () => {
  for (const [action, revokedPath] of [
    ["loadRead('activity',true)", '/today'], ["loadRead('queue',true)", '/queue'],
    ["loadRead('operations',true)", '/operations'], ["showCustomer('CUST-001')", '/customers/CUST-001/conversation'],
    ["showOrder('REVOKED-ORDER')", '/orders/REVOKED-ORDER'], ['loadExtras(true)', '/controls'],
  ]) {
    const w = workspace();
    w.live();
    w.context.activity = activityFixture();
    w.context.queue = queueFixture();
    w.run('data.activity=activity;data.queue=queue');
    // Revoke only this endpoint: a second 401 must not conceal a missing guard.
    w.context.fetch = async url => {
      const path = new URL(url).pathname;
      if (path === '/api/dashboard' + revokedPath) return { ok: false, status: 401 };
      assert.equal(path, '/api/dashboard/operations');
      return response(operationsFixture());
    };
    await w.run(action);
    assert.equal(w.run('data.mode'), 'disconnected', action);
    assert.equal(w.run('data.activity'), null);
    assert.equal(w.run('data.queue'), null);
    assert.equal(w.run('data.operations'), null);
    assert.equal(w.run('state.connection'), null);
    assert.equal(w.run('Object.values(state.reads).some(r=>r.busy||r.error||r.loadedAt||r.pending)'), false);
    assert.equal(w.nodes['detail-dialog'].innerHTML, '');
    assert.doesNotMatch(w.nodes.app.innerHTML, /Bakery|ORDER-REVIEW/);
  }
});

test('snapshot timer preserves explicit reads while manual refresh updates only the visible view', async () => {
  const w = workspace();
  w.live();
  apiFixture(w, { '/api/dashboard/queue': queueFixture(), '/api/dashboard/operations': operationsFixture() });
  await w.click({ view: 'overview' });
  const cached = w.run('JSON.stringify([data.queue,data.operations,state.reads])');
  for (const view of ['today', 'overview', 'queue', 'agents']) {
    w.run(`state.view='${view}'`);
    w.requests.length = 0;
    apiFixture(w, { '/api/dashboard/snapshot': w.fixture() });
    await w.run('refresh(true)');
    assert.deepEqual(w.requests.map(([url]) => new URL(url).pathname), ['/api/dashboard/snapshot']);
    assert.equal(w.run('JSON.stringify([data.queue,data.operations,state.reads])'), cached);
  }
  w.run("state.view='today'");
  w.requests.length = 0;
  apiFixture(w, { '/api/dashboard/snapshot': w.fixture(), '/api/dashboard/today': activityFixture() });
  await w.run('refresh()');
  assert.deepEqual(w.requests.map(([url]) => new URL(url).pathname), ['/api/dashboard/snapshot', '/api/dashboard/today']);
  assert.match(w.nodes.app.innerHTML, /Bakery &lt;North&gt;/);
});

test('missing reads stay unavailable, retry works, and a signed-out session ignores late results', async () => {
  const w = workspace();
  w.live();
  const original = w.run('JSON.stringify(data.orders)');
  for (const [key, view] of [['activity', 'today'], ['queue', 'queue']]) {
    w.run(`state.view='${view}'`);
    w.context.fetch = async () => ({ ok: false, status: 404 });
    await w.click({ read: key });
    assert.match(w.nodes.app.innerHTML, /not available on this agent yet/);
    assert.equal(w.run(`data.${key}`), null);
    assert.equal(w.run('data.mode'), 'live');
    assert.doesNotMatch(w.nodes.app.innerHTML, /Sample data for exploring/);
  }
  apiFixture(w, { '/api/dashboard/queue': queueFixture() });
  await w.click({ read: 'queue' });
  assert.match(w.nodes.app.innerHTML, /Price above the auto-confirm limit/);
  assert.equal(w.run('JSON.stringify(data.orders)'), original);
  for (const [action, fixture] of [
    ["loadRead('activity',true)", activityFixture()],
    ["loadRead('queue',true)", queueFixture()],
    ["loadRead('operations',true)", operationsFixture()],
    ["showCustomer('CUST-001')", transcriptFixture('CUST-001')],
  ]) {
    w.live();
    const pending = deferred();
    w.context.fetch = () => pending.promise;
    const reading = w.run(action);
    await w.click({ action: 'disconnect' });
    pending.resolve(response(fixture));
    await reading;
    assert.equal(w.run('data.mode'), 'disconnected', action);
    assert.equal(w.run('data.activity'), null);
    assert.equal(w.run('data.queue'), null);
    assert.equal(w.run('data.operations'), null);
    assert.equal(w.nodes['detail-dialog'].innerHTML, '');
  }
});

test('record caps are visible beside each affected list and inside customer details', async () => {
  const w = workspace();
  w.live();
  apiFixture(w, { '/api/dashboard/queue': queueFixture(), '/api/dashboard/operations': operationsFixture(), '/api/dashboard/customers/CUST-001/conversation': transcriptFixture('CUST-001') });
  w.run("data.truncated=['orders','pending orders','inventory','product names','customers']");
  for (const view of ['orders', 'inventory', 'customers']) {
    await w.click({ view });
    assert.match(w.nodes.app.innerHTML, /<p class="list-notice">This list is limited to 250/, view);
  }
  await w.click({ view: 'overview' });
  assert.match(w.nodes.app.innerHTML.split('Recent orders')[1], /<p class="list-notice">This list is limited to 250/);
  await w.run("showCustomer('CUST-001')");
  assert.match(w.nodes['detail-dialog'].innerHTML, /<p class="list-notice">This list is limited to 250/);
  assert.match(w.nodes['detail-dialog'].innerHTML, /not a complete order history/);
});

test('agent controls wait for a shared operations read already started by Overview', async () => {
  const w = workspace();
  w.live();
  const pending = deferred();
  w.context.fetch = async url => {
    w.requests.push(url);
    if (url.endsWith('/operations')) return pending.promise;
    assert.equal(url, 'https://agent.example/api/dashboard/controls');
    return response({ policies: [{ name: 'Saved order ceiling', value: '0', note: 'Owner setting' }] });
  };
  const reading = w.run("loadRead('operations')");
  w.run("state.view='agents'");
  const extras = w.run('loadExtras()');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(w.run('state.extrasBusy'), true, 'controls wait for the pending operations read');
  pending.resolve(response(operationsFixture()));
  await Promise.all([reading, extras]);
  assert.equal(w.requests.filter(url => url.endsWith('/operations')).length, 1);
  assert.equal(w.run('state.extrasError'), '');
  assert.match(w.nodes.app.innerHTML, /Saved order ceiling/);
  assert.match(w.nodes.app.innerHTML, /Active lease/);
});

test('fresh operations do not disguise old controls when the agents view is reopened', async () => {
  const w = workspace();
  w.live();
  apiFixture(w, { '/api/dashboard/controls': { policies: [{ name: 'Old ceiling', value: '10' }] }, '/api/dashboard/operations': operationsFixture() });
  await w.click({ view: 'agents' });
  w.run('state.extrasLoadedAt=Date.now()-120000');
  w.requests.length = 0;
  apiFixture(w, { '/api/dashboard/controls': { policies: [{ name: 'Updated ceiling', value: '20' }] } });
  await w.click({ view: 'agents' });
  assert.deepEqual(w.requests.map(([url]) => new URL(url).pathname), ['/api/dashboard/controls']);
  assert.match(w.nodes.app.innerHTML, /Updated ceiling/);
  assert.doesNotMatch(w.nodes.app.innerHTML, /Old ceiling/);
});

// Independent contracts: these numbers do not come from makeDemo or snapshot totals.
const salesFixture = () => ({
  currency: 'ARS', since: '2026-09-09', until: '2026-09-15', total: 1234500, orders: 42, averageOrder: 29392,
  daily: [{ date: '2026-09-10', total: 60000, orders: 2 }, { date: '2026-09-09', total: 120000, orders: 4 }],
  topProducts: [{ id: 'MILK', name: 'Whole milk', quantity: 320, total: 400000 }],
  topCustomers: [{ id: 'CUST-RANKED', name: 'Ranked shop', orders: 6, total: 180000 }], errors: [], truncated: [],
});
const adviceFixture = () => ({
  generatedAt: '2026-09-15T13:50:00+00:00', enabled: false, currency: 'ARS',
  items: [
    { id: 'sleep', kind: 'dormido', title: 'Quiet account', body: 'A regular stopped ordering.', about: 'Quiet shop', assumption: 'Weekly purchases.', amount: null, customerId: 'CUST-ADVISED', orderId: null, productId: null },
    { id: 'loss', kind: 'perdida', title: 'Sold below cost', body: 'Two lines were sold below cost.', about: 'ORDER-LOSS', assumption: 'Purchase price list.', amount: -18400, customerId: null, orderId: 'ORDER-LOSS', productId: null },
    { id: 'debt', kind: 'deuda', title: 'Overdue balance', body: 'Payment is overdue.', about: 'A balance', assumption: 'Fourteen day tolerance.', amount: 0, customerId: null, orderId: null, productId: null },
    { id: 'stock', kind: 'quiebre', title: 'Stock may run out', body: 'Demand exceeds available stock.', about: 'MILK', assumption: 'Next delivery in two days.', customerId: null, orderId: null, productId: 'MILK' },
  ], errors: [], truncated: [],
});
function reports(w, sales = salesFixture(), advice = adviceFixture()) {
  w.context.salesPayload = sales; w.context.advicePayload = advice;
  w.run('data.sales=validateSales(salesPayload);data.advice=validateAdvice(advicePayload);state.reads.sales.range=state.range');
}
function reportApi(w, handler) {
  w.context.fetch = async (url, options) => {
    w.requests.push([url, options]);
    assert.equal(options.headers.Authorization, 'Bearer dashboard-fixture-token-at-least-32-characters');
    assert.equal(options.body, undefined);
    assert.ok(!options.method || options.method === 'GET');
    return handler(new URL(url), options);
  };
}

test('Sales and Advice demo fixtures work offline for both reporting periods', async () => {
  const w = workspace({ search: '?demo=1' });
  for (const view of ['sales', 'advice']) {
    await w.click({ view });
    assert.equal(w.run('state.view'), view);
    assert.match(w.nodes.app.innerHTML, new RegExp(`data-view="${view}" class="nav-item active"`));
  }
  assert.equal(w.run('data.advice.items.length'), 4);
  assert.equal(w.run('data.advice.enabled'), false);
  assert.equal(w.run('data.advice.items.every(i=>!i.orderId||data.orders.some(o=>o.id===i.orderId))'), true);
  await w.click({ view: 'sales' });
  assert.match(w.nodes.app.innerHTML, /id="range"/);
  await w.listeners.change({ target: { id: 'range', value: '30' } });
  assert.equal(w.run('data.sales.daily.length'), 30);
  assert.equal(w.run('data.sales.since'), w.run('dateShift(data.today,-29)'));
  assert.equal(w.run('data.sales.daily.reduce((sum,row)=>sum+row.total,0)'), w.run('data.sales.total'));
  await w.listeners.change({ target: { id: 'range', value: '7' } });
  assert.equal(w.run('data.sales.daily.length'), 7);
  assert.equal(w.requests.length, 0);
});

test('Sales distinguishes unavailable fields from zero and intentional empty lists', () => {
  const w = workspace({ search: '?demo=1' });
  const unavailable = Object.fromEntries(Object.keys(salesFixture()).map(key => [key, null]));
  reports(w, unavailable);
  let html = w.run('salesView()');
  assert.equal((html.match(/unavailable-value/g) || []).length, 3);
  for (const text of ['Daily sales unavailable', 'Top products unavailable', 'Top customers unavailable', 'Start date unavailable', 'End date unavailable', 'Error details unavailable', 'List completeness unavailable']) assert.ok(html.includes(text), text);
  assert.doesNotMatch(html, /ARS|>0<|NaN/);
  reports(w, { ...salesFixture(), total: 0, orders: 0, averageOrder: 0, daily: [], topProducts: [], topCustomers: [] });
  html = w.run('salesView()');
  assert.match(html, /ARS\s*0\.00/);
  assert.match(html, />0<\/div>/);
  for (const text of ['No daily sales yet', 'No product sales yet', 'No customer sales yet']) assert.ok(html.includes(text));
  reports(w, { ...salesFixture(), currency: null });
  assert.match(w.run('salesView()'), /Sales currency unavailable/);
  assert.doesNotMatch(w.run('salesView()'), /ARS\s*1,234,500/);
});

test('Sales rejects malformed numeric, date, ranking, and notice contracts', () => {
  const w = workspace({ search: '?demo=1' });
  const bad = [
    { currency: 'ars' }, { since: '2026-02-30' }, { until: '2026-09-01' }, { total: '1' }, { averageOrder: '0.5' }, { orders: -1 },
    { daily: false }, { daily: [{ date: '2026-09-08', total: 1, orders: 1 }] }, { daily: [{ date: '2026-09-09', total: Infinity, orders: 1 }] },
    { daily: [{ date: '2026-09-09', total: 1, orders: 0.5 }] }, { daily: [salesFixture().daily[0], salesFixture().daily[0]] },
    { topProducts: [{ id: 'MILK', name: 'Milk', quantity: -0.5, total: 1 }] }, { topProducts: [{ id: '', name: 'Milk', quantity: 1, total: 1 }] },
    { topProducts: [{ id: 'MILK', name: 'Milk', quantity: 1, total: '0.25' }] }, { topCustomers: [{ id: 'C', name: 'Shop', orders: '3', total: 1 }] },
    { errors: [null] }, { truncated: [{}] }, { total: Number.MAX_SAFE_INTEGER + 1 },
  ];
  for (const change of bad) { w.context.bad = { ...salesFixture(), ...change }; assert.throws(() => w.run('validateSales(bad)'), /invalid/); }
  for (const key of Object.keys(salesFixture())) { w.context.bad = salesFixture(); delete w.context.bad[key]; assert.throws(() => w.run('validateSales(bad)'), /invalid/, key); }
  w.context.good = { ...salesFixture(), topProducts: [{ id: 'CHEESE', name: 'Cheese', quantity: 0.5, total: -1 }] };
  assert.equal(w.run('validateSales(good).topProducts[0].quantity'), 0.5);
  assert.equal(w.run('validateSales(good).daily[0].date'), '2026-09-09');
});

test('Advice rejects unknown kinds, invalid references, amounts and text', () => {
  const w = workspace({ search: '?demo=1' });
  for (const change of [{ kind: 'other' }, { amount: '0.5' }, { amount: Infinity }, { customerId: '' }, { orderId: 42 }, { productId: false }, { assumption: null }, { title: 1 }, { about: null }, { body: [] }, { id: '' }]) {
    w.context.bad = { ...adviceFixture(), items: [{ ...adviceFixture().items[0], ...change }] };
    assert.throws(() => w.run('validateAdvice(bad)'), /invalid/);
  }
  for (const change of [{ enabled: null }, { currency: 'ars' }, { generatedAt: '2026-02-30T10:00:00Z' }, { items: null }, { errors: null }, { truncated: [1] }]) {
    w.context.bad = { ...adviceFixture(), ...change }; assert.throws(() => w.run('validateAdvice(bad)'), /invalid/);
  }
  w.context.good = adviceFixture();
  w.context.sinMoneda = { ...adviceFixture(), currency: null };
  assert.equal(w.run('validateAdvice(sinMoneda).currency'), null);
  assert.equal(w.run('validateAdvice(good).items[3].amount'), null);
  assert.equal(w.run('validateAdvice(good).items[1].amount'), -18400);
});

test('Both report validators project allowlisted fields and their views escape all text', () => {
  const w = workspace({ search: '?demo=1' });
  const sales = salesFixture(), advice = adviceFixture();
  const poison = '<img src=x onerror=alert(1)> & "quoted"';
  sales.phone = advice.phone = 'PHONE-MUST-NOT-RENDER';
  for (const row of [...sales.daily, ...sales.topProducts, ...sales.topCustomers, ...advice.items]) row.phone = 'PHONE-MUST-NOT-RENDER';
  sales.topProducts[0].name = sales.topCustomers[0].name = poison;
  sales.topProducts[0].id = sales.topCustomers[0].id = poison;
  advice.items[0] = { ...advice.items[0], title: poison, body: poison, assumption: poison, about: poison, customerId: poison, orderId: poison, productId: poison };
  sales.errors = advice.errors = [poison];
  reports(w, sales, advice);
  assert.doesNotMatch(w.run('JSON.stringify([data.sales,data.advice])'), /PHONE-MUST-NOT-RENDER|"phone"/);
  const html = w.run('salesView()+adviceView()');
  assert.doesNotMatch(html, /<img|onerror="|data-customer="<|PHONE-MUST-NOT-RENDER/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt; &amp; &quot;quoted&quot;/);
  assert.match(html, /data-order="&lt;img/);
  assert.match(html, /Assumption<\/strong>&lt;img/);
});

test('Sales reads the selected period, caches for sixty seconds, and refreshes explicitly', async () => {
  const w = workspace(); w.live();
  reportApi(w, url => {
    assert.equal(url.pathname, '/api/dashboard/sales');
    assert.ok(['7', '30'].includes(url.searchParams.get('days')));
    return response({ ...salesFixture(), since: url.searchParams.get('days') === '30' ? '2026-08-17' : '2026-09-09' });
  });
  await w.click({ view: 'sales' });
  assert.equal(w.requests.length, 1);
  await w.click({ view: 'sales' }); assert.equal(w.requests.length, 1);
  await w.listeners.change({ target: { id: 'range', value: '30' } });
  assert.equal(w.requests.length, 2);
  assert.equal(new URL(w.requests[1][0]).searchParams.get('days'), '30');
  assert.match(w.nodes.app.innerHTML, /17 Aug 2026/);
  await w.click({ read: 'sales' }); assert.equal(w.requests.length, 3);
  w.run("state.reads.sales.loadedAt=new Date(Date.now()-61000).toISOString()");
  await w.click({ view: 'sales' }); assert.equal(w.requests.length, 4);
  await w.listeners.change({ target: { id: 'range', value: '9' } }); assert.equal(w.requests.length, 4);
});

test('Sales ignores stale successes and failures during rapid period changes', async () => {
  const w = workspace(); w.live();
  const pending = [];
  reportApi(w, url => { const wait = deferred(); pending.push({ days: url.searchParams.get('days'), wait }); return wait.promise; });
  w.run("state.view='sales'");
  const first = w.run("loadRead('sales')");
  const second = w.run("state.range=30;loadRead('sales')");
  const third = w.run("state.range=7;loadRead('sales')");
  assert.deepEqual(pending.map(p => p.days), ['7', '30', '7']);
  pending[2].wait.resolve(response({ ...salesFixture(), total: 777 })); await third;
  pending[0].wait.resolve(response({ ...salesFixture(), total: 111 })); await first;
  assert.equal(w.run('data.sales.total'), 777);
  pending[1].wait.resolve({ ok: false, status: 503 }); await second;
  assert.equal(w.run('data.sales.total'), 777);
  assert.equal(w.run('state.reads.sales.error'), '');
  assert.equal(w.run('state.reads.sales.busy'), false);
});

test('Report reads survive snapshot refresh and manual refresh updates only the active report', async () => {
  const w = workspace(); w.live();
  reportApi(w, url => {
    const routes = { '/api/dashboard/sales': salesFixture, '/api/dashboard/advice': adviceFixture, '/api/dashboard/snapshot': w.fixture };
    assert.ok(routes[url.pathname]); return response(routes[url.pathname]());
  });
  await w.click({ view: 'sales' }); await w.click({ view: 'advice' });
  const cached = w.run('JSON.stringify([data.sales,data.advice,state.reads.sales,state.reads.advice])');
  w.requests.length = 0; await w.run('refresh(true)');
  assert.equal(w.run('JSON.stringify([data.sales,data.advice,state.reads.sales,state.reads.advice])'), cached);
  assert.deepEqual(w.requests.map(([url]) => new URL(url).pathname), ['/api/dashboard/snapshot']);
  w.requests.length = 0; await w.run('refresh()');
  assert.deepEqual(w.requests.map(([url]) => new URL(url).pathname), ['/api/dashboard/snapshot', '/api/dashboard/advice']);
  await w.click({ view: 'advice' }); assert.equal(w.requests.length, 2);
});

test('Both new reads clear on sign-out, reject late sessions, and sign out on a revoked token', async () => {
  for (const key of ['sales', 'advice']) {
    const w = workspace(); w.live(); reports(w);
    const wait = deferred(); reportApi(w, () => wait.promise);
    const reading = w.run(`loadRead('${key}',true)`);
    await w.click({ action: 'disconnect' });
    assert.equal(w.run('data.sales'), null); assert.equal(w.run('data.advice'), null);
    wait.resolve(response(key === 'sales' ? salesFixture() : adviceFixture())); await reading;
    assert.equal(w.run('data.sales'), null); assert.equal(w.run('data.advice'), null);
    w.live(); reports(w); reportApi(w, () => ({ ok: false, status: 401 }));
    await w.run(`loadRead('${key}',true)`);
    assert.equal(w.run('data.mode'), 'disconnected');
    assert.equal(w.run('data.sales'), null); assert.equal(w.run('data.advice'), null);
    assert.equal(w.run('state.connection'), null);
    assert.equal(w.run('Object.values(state.reads).some(r=>r.busy||r.error||r.loadedAt)'), false);
  }
});

test('Missing report endpoints show a useful unavailable state and a working retry', async () => {
  for (const key of ['sales', 'advice']) {
    const w = workspace(); w.live();
    reportApi(w, url => { assert.equal(url.pathname, '/api/dashboard/' + key); return { ok: false, status: 404 }; });
    await w.click({ view: key });
    assert.match(w.nodes.app.innerHTML, /This view is not available on this agent yet/);
    assert.equal(w.run(`data.${key}`), null);
    assert.match(w.nodes.app.innerHTML, new RegExp(`data-read="${key}"`));
    reportApi(w, () => response(key === 'sales' ? salesFixture() : adviceFixture()));
    await w.click({ read: key });
    assert.ok(w.run(`data.${key}`));
    reportApi(w, () => response({ invalid: true }));
    await w.click({ read: key });
    assert.match(w.nodes.app.innerHTML, /The agent returned invalid/);
    assert.equal(w.run(`data.${key}`), null);
  }
});

test('Disabled Advice explains the preview and empty enabled reports make no false claims', () => {
  const w = workspace({ search: '?demo=1' }); reports(w);
  assert.match(w.run('adviceView()'), /ADVICE IS OFF/);
  assert.match(w.run('adviceView()'), /These sample findings show what you would see/);
  assert.match(w.run('adviceView()'), /Sold below cost/);
  w.run("data.mode='live';data.advice.items=[]");
  let html = w.run('adviceView()');
  assert.match(html, /No saved findings/);
  assert.match(html, /last available report/);
  assert.match(html, /What advice looks for/);
  assert.doesNotMatch(html, /These sample findings|Sold below cost/);
  w.run('data.advice.enabled=true'); html = w.run('adviceView()');
  assert.match(html, /No advice to review/);
  assert.doesNotMatch(html, /ADVICE IS OFF|All clear|No risks/);
});

test('Advice renders in the order the server sent, without percentages, and distinguishes all four detectors', () => {
  const w = workspace({ search: '?demo=1' }); reports(w);
  const html = w.run('adviceView()');
  // El servidor ordena DENTRO de cada clase y el panel respeta la lista.
  // Ordenar acá compararía plata contra unidades.
  const enviados = JSON.parse(w.run('JSON.stringify(data.advice.items.map(i=>i.title))'));
  const positions = enviados.map(title => html.indexOf('<h3>' + title));
  assert.ok(positions.every((pos, i) => pos >= 0 && (!i || pos > positions[i - 1])));
  for (const kind of ['perdida', 'dormido', 'deuda', 'quiebre']) assert.match(html, new RegExp(`advice-row advice-kind-${kind}`));
  assert.doesNotMatch(html, /82%|20%|100%|confidence/i);
  assert.match(html, /Assumption<\/strong>Purchase price list/);
});

test('Advice category and text filters compose, and navigation resets them', async () => {
  const w = workspace({ search: '?demo=1' }); reports(w);
  await w.click({ view: 'advice' }); await w.click({ filter: 'perdida' });
  assert.match(w.nodes.app.innerHTML, /<h3>Sold below cost/);
  assert.doesNotMatch(w.nodes.app.innerHTML, /<h3>Overdue balance/);
  await w.click({ filter: 'all' });
  w.nodes.search.value = 'fourteen day'; w.listeners.input({ target: w.nodes.search });
  assert.match(w.nodes.app.innerHTML, /<h3>Overdue balance/);
  assert.doesNotMatch(w.nodes.app.innerHTML, /<h3>Sold below cost/);
  await w.click({ filter: 'quiebre' }); assert.match(w.nodes.app.innerHTML, /No matching advice/);
  await w.click({ view: 'sales' }); await w.click({ view: 'advice' });
  assert.equal(w.run('state.search'), ''); assert.equal(w.run('state.filter'), 'all');
});

test('Sales and Advice link to existing details even for customers outside the snapshot', async () => {
  const w = workspace(); w.live(); reports(w);
  const order = { ...w.fixture().orders[0], id: 'ORDER-LOSS', items: [{ name: 'Detail milk', qty: 1, rate: 10 }] };
  reportApi(w, url => {
    if (url.pathname === '/api/dashboard/orders/ORDER-LOSS') return response(order);
    const match = url.pathname.match(/^\/api\/dashboard\/customers\/([^/]+)\/conversation$/);
    assert.ok(match); return response(transcriptFixture(decodeURIComponent(match[1])));
  });
  assert.match(w.run('salesView()'), /data-customer="CUST-RANKED"/);
  assert.match(w.run('adviceView()'), /data-customer="CUST-ADVISED"/);
  assert.match(w.run('adviceView()'), /data-order="ORDER-LOSS"/);
  await w.run("showCustomer('CUST-RANKED')"); assert.match(w.nodes['detail-dialog'].innerHTML, /Ranked shop/);
  await w.run("showCustomer('CUST-ADVISED')"); assert.match(w.nodes['detail-dialog'].innerHTML, /CUST-ADVISED/);
  await w.run("showOrder('ORDER-LOSS')"); assert.match(w.nodes['detail-dialog'].innerHTML, /Detail milk/);
  assert.equal(w.requests.length, 3);
});

test('Report errors are surfaced and truncation notices stay next to their own lists', () => {
  const w = workspace({ search: '?demo=1' });
  reports(w, { ...salesFixture(), errors: ['Sales partial'], truncated: ['daily', 'topProducts', 'topCustomers'] }, { ...adviceFixture(), errors: ['Advice partial'], truncated: ['items'] });
  const sales = w.run('salesView()'), advice = w.run('adviceView()');
  assert.match(sales, /notice error-notice[^>]*>.*Sales partial/);
  assert.match(advice, /notice error-notice[^>]*>.*Advice partial/);
  assert.equal((sales.match(/This list was capped/g) || []).length, 3);
  assert.match(sales, /This list was capped[^]*sales-chart/);
  assert.match(sales, /Top products[^]*This list was capped[^]*Whole milk/);
  assert.match(sales, /Top customers[^]*This list was capped[^]*Ranked shop/);
  assert.match(advice, /This list was capped[^]*advice-list/);
  assert.doesNotMatch(sales, /totals cover loaded records/);
});

test('Report money uses its own currency and converts every monetary consumer including negative advice', async () => {
  const w = workspace({ search: '?demo=1' });
  reports(w, { ...salesFixture(), currency: 'USD' }, { ...adviceFixture(), currency: 'USD' });
  assert.match(w.run('salesView()'), /USD\s*1,234,500\.00/);
  assert.match(w.run('adviceView()'), /-USD\s*18,400\.00/);
  w.run("state.displayCurrency='INR';state.fx={target:'INR',rates:{USD:0.01,ARS:2}};");
  const sales = w.run('salesView()'), advice = w.run('adviceView()');
  for (const amount of ['123,450,000.00', '2,939,200.00', '40,000,000.00', '18,000,000.00', '12,000,000.00']) assert.ok(sales.includes('INR\u00a0' + amount), amount);
  assert.match(sales, /aria-label="[^"]*: INR\s*12,000,000\.00, 4 orders"/);
  assert.match(sales, /title="[^"]* · INR\s*12,000,000\.00 · 4 orders"/);
  assert.match(advice, /-INR\s*1,840,000\.00/);
  assert.match(advice, /INR\s*0\.00/);
  assert.equal((advice.match(/Amount unavailable/g) || []).length, 2);
  await w.click({ salesDate: '2026-09-09' });
  assert.match(w.nodes['detail-dialog'].innerHTML, /INR\s*12,000,000\.00/);
  assert.match(w.nodes['detail-dialog'].innerHTML, /<dt>Orders<\/dt><dd>4<\/dd>/);
  assert.equal(w.run('data.sales.total'), 1234500);
  assert.equal(w.run('data.advice.items[1].amount'), -18400);
});

test('The charcoal preference is opt-in, persistent, local, and safe when storage is unavailable', async () => {
  const w = workspace({ search: '?demo=1' });
  assert.equal(w.run('document.documentElement.dataset.theme'), 'light');
  assert.match(w.nodes.app.innerHTML, /Switch to charcoal night mode/);
  await w.click({ action: 'theme' });
  assert.equal(w.run('document.documentElement.dataset.theme'), 'dark');
  assert.match(w.nodes.app.innerHTML, /Switch to light mode/);
  assert.equal(w.preferences.get('plus.dashboard.theme'), 'dark');
  const restored = workspace({ preferences: [...w.preferences] });
  assert.equal(restored.run('document.documentElement.dataset.theme'), 'dark');
  await w.click({ action: 'theme' }); assert.equal(w.preferences.get('plus.dashboard.theme'), 'light');
  w.context.localStorage.setItem = () => { throw new Error('Storage blocked'); };
  await w.click({ action: 'theme' }); assert.equal(w.run('state.theme'), 'dark');
  assert.equal(workspace({ preferences: [['plus.dashboard.theme', 'untrusted']] }).run('state.theme'), 'light');
  assert.equal(w.requests.length, 0);
});

test('The decorative logo reveal cleans up once without delaying dashboard navigation', async () => {
  const w = workspace({ search: '?demo=1' });
  assert.equal(w.decorations.length, 1);
  assert.equal(w.decorations[0]['aria-hidden'], 'true');
  assert.match(w.decorations[0].innerHTML, /mate-launch/);
  assert.match(w.nodes.app.innerHTML, /mate-sidebar/);
  await w.click({ view: 'sales' });
  assert.equal(w.run('state.view'), 'sales');
  assert.equal(w.decorations.length, 1);
  const cleanup = [...w.timeouts.values()].find(timer => timer.ms === 1400);
  assert.ok(cleanup); cleanup.callback();
  assert.equal(w.decorations[0].removed, true);
  assert.equal(w.requests.length, 0);
});


test('Report lists beyond the display cap are rejected instead of rendered', () => {
  const w = workspace({ search: '?demo=1' });
  const fila = { date: '2026-09-09', total: 1, orders: 1 };
  w.context.bad = { ...salesFixture(), daily: Array.from({ length: 501 }, (_, i) => ({ ...fila, date: '2026-09-09' })) };
  assert.throws(() => w.run('validateSales(bad)'), /invalid/);
  w.context.bad = { ...adviceFixture(), items: Array.from({ length: 501 }, () => adviceFixture().items[0]) };
  assert.throws(() => w.run('validateAdvice(bad)'), /invalid/);
  // El límite se prueba por sus DOS lados: con sólo el rechazo, un tope
  // accidentalmente más bajo dejaría este test en verde.
  const dias = Array.from({ length: 500 }, (_, i) => ({
    date: new Date(Date.UTC(2025, 0, 1 + i)).toISOString().slice(0, 10), total: 1, orders: 1,
  }));
  w.context.good = { ...salesFixture(), since: dias[0].date, until: dias[499].date, daily: dias };
  assert.equal(w.run('validateSales(good).daily.length'), 500);
});


test('The timer mock never reuses an id, so a live timer is not overwritten', () => {
  // Reproduce la secuencia real: toast() programa el suyo, exportOrders()
  // programa su limpieza, y el segundo toast() borra el primero. Con
  // `timeouts.size + 1` el tercer id volvía a ser el del cleanup y le pisaba
  // el registro: el harness perdía un timer vivo sin que nada fallara.
  const w = workspace({ search: '?demo=1' });
  const unoAviso = w.run('setTimeout(()=>{},4000)');
  const limpieza = w.run('setTimeout(()=>{},1000)');
  w.run(`clearTimeout(${unoAviso})`);
  const otroAviso = w.run('setTimeout(()=>{},4000)');
  assert.notEqual(otroAviso, limpieza);
  assert.equal(w.timeouts.get(limpieza).ms, 1000);
  assert.equal(w.timeouts.get(otroAviso).ms, 4000);
});

// ---------------------------------------------------------------------------
// Los ajustes del negocio. El panel PROPONE; el código lo confirma WhatsApp.
// ---------------------------------------------------------------------------
const settingsFixture = () => ({
  problem: '',
  pending: null,
  groups: [
    {
      id: 'negocio', name: 'Your business', settings: [
        { id: 'NOMBRE_NEGOCIO', name: 'nombre del negocio', meaning: 'How your business is named', unit: 'texto', kind: 'texto', optional: true, value: 'Plus Dairy', display: 'Plus Dairy', source: 'You set this', configured: true, problem: '' },
      ],
    },
    {
      id: 'limites', name: 'Automatic confirmation', settings: [
        { id: 'AUTO_CONFIRM_MAX', name: 'monto maximo', meaning: 'Largest order confirmed without a person', unit: '$', kind: 'numero', optional: false, value: '0', display: '$ 0', source: 'Shipped default', configured: false, problem: '' },
      ],
    },
  ],
});

// Un doble PROPIO para las escrituras: `apiFixture` afirma `options.body ===
// undefined`, que es correcto para las lecturas y hace imposible probar un POST.
function writeFixture(w, respuesta, status = 200) {
  w.context.fetch = async (url, options) => {
    w.requests.push([url, options]);
    const path = new URL(url).pathname;
    if (path === '/api/dashboard/settings') return new Response(JSON.stringify(settingsFixture()), { status: 200 });
    assert.equal(options.method, 'POST');
    assert.equal(options.headers['Content-Type'], 'application/json');
    assert.equal(options.credentials, 'omit');
    assert.equal(options.redirect, 'error');
    return new Response(JSON.stringify(respuesta), { status });
  };
}

test('Settings show where every value came from, and demo mode offers no Change button', async () => {
  const w = workspace({ search: '?demo=1' });
  await w.click({ view: 'settings' });
  assert.match(w.nodes.app.innerHTML, /Your business/);
  assert.match(w.nodes.app.innerHTML, /Automatic confirmation/);
  // `source` es lo que deja mostrar un tope en 0 sin pintarlo de rojo: dice
  // que el 0 viene de fábrica y no de alguien que rompió algo.
  assert.match(w.nodes.app.innerHTML, /Shipped default/);
  // Sin conexión no hay nada que proponer, y no se ofrece.
  assert.ok(!/data-setting=/.test(w.nodes.app.innerHTML));
  assert.equal(w.requests.length, 0);
});

test('Proposing a setting sends a POST and never shows a code', async () => {
  const w = workspace();
  w.live();
  writeFixture(w, { ok: true, setting: 'AUTO_CONFIRM_MAX', name: 'monto maximo', detail: 'Te mandé el código por WhatsApp.' });
  await w.click({ view: 'settings' });
  await w.run("openSetting('AUTO_CONFIRM_MAX')");
  assert.match(w.nodes['setting-dialog'].innerHTML, /monto maximo/);
  // El valor actual se refleja en un atributo, así que pasa por `escape()`.
  assert.match(w.nodes['setting-dialog'].innerHTML, /value="0"/);

  await w.submit(w.settingForm('AUTO_CONFIRM_MAX', '30000'));

  const post = w.requests.find(([, o]) => o.method === 'POST');
  assert.ok(post, 'tiene que haber salido un POST');
  assert.equal(new URL(post[0]).pathname, '/api/dashboard/settings/propose');
  assert.deepEqual(JSON.parse(post[1].body), { setting: 'AUTO_CONFIRM_MAX', value: '30000' });
  assert.equal(w.nodes['setting-dialog'].open, false);
  assert.match(w.nodes.toast.textContent, /código/);
  // No hay ninguna pantalla que aplique el código, y no se inventa una.
  assert.ok(!/four-digit code<\/label>|name="code"/.test(w.nodes['setting-dialog'].innerHTML));
});

test('A rejected value keeps the dialog open and says why', async () => {
  const w = workspace();
  w.live();
  // 200 con `ok:false` es el caso NORMAL de un valor que no sirve. Tratar todo
  // 200 como éxito cerraba el diálogo diciendo que el cambio quedó pedido.
  writeFixture(w, { ok: false, pending: false, detail: '«monto maximo» no es un número: «mucho»' });
  await w.click({ view: 'settings' });
  await w.run("openSetting('AUTO_CONFIRM_MAX')");
  await w.submit(w.settingForm('AUTO_CONFIRM_MAX', 'mucho'));

  assert.equal(w.nodes['setting-dialog'].open, true);
  assert.match(w.nodes['setting-error'].textContent, /no es un número/);
});

test('A change already waiting is shown, so a second one does not silently replace it', async () => {
  const w = workspace();
  w.live();
  w.context.fetch = async (url) => {
    w.requests.push([url]);
    const esperando = settingsFixture();
    esperando.pending = { id: 'AUTO_CONFIRM_MAX', name: 'monto maximo', from: '0', to: '30000' };
    return new Response(JSON.stringify(esperando), { status: 200 });
  };
  await w.click({ view: 'settings' });
  assert.match(w.nodes.app.innerHTML, /waiting for your four-digit code on WhatsApp/);
  assert.match(w.nodes.app.innerHTML, /0 → 30000/);
});
