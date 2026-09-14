// Exercise application logic and event handlers without a browser or network.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../plus-agent/app/dashboard_ui/app.js', import.meta.url), 'utf8');

function workspace(options = {}) {
  const listeners = {}, nodes = {}, copied = [], downloads = [], requests = [];
  const preferences = new Map();
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
  return { context, run, click, fixture, live, nodes, copied, downloads, requests, listeners, form, submit, preferences };
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
