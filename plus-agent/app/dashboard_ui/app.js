const $ = (s, root = document) => root.querySelector(s);
const escape = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
  sales: '<path d="M4 20V10m6 10V4m6 16v-7m5 7H2M14 5h7v7m0-7-8 8"/>',
  advice: '<path d="M9 18h6m-5 3h4M8 14a6 6 0 1 1 8 0l-1 2H9zM12 1v1M2 8H1m22 0h-1"/>',
  moon: '<path d="M20 14A9 9 0 0 1 10 4a9 9 0 1 0 10 10Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
  today: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 11h18m-13 5 3 3 5-5"/>',
  queue: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l4 2M3 3l3 3"/>',
  overview: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  orders: '<path d="M8 3H5v18h14V3h-3M8 3v4h8V3zM8 12h8M8 16h5"/>',
  inventory: '<path d="m3 7 9-4 9 4v10l-9 4-9-4zM3 7l9 4 9-4M12 11v10M7.5 5l9 4"/>',
  customers: '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6M21 21v-3a6 6 0 0 0-4-5"/>',
  agents: '<rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 3v4M1 12v4M23 12v4M9 16h6"/><circle cx="8" cy="12" r=".7"/><circle cx="16" cy="12" r=".7"/>',
  settings: '<path d="m9 3-1 3-3 1v4l-2 1 2 2v4l3 1 1 2h5l1-2 4-1v-4l2-2-2-1V7l-4-1-1-3z"/><circle cx="12" cy="12" r="3"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  down: '<path d="m7 10 5 5 5-5"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 11h18"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  trend: '<path d="m3 17 6-6 4 4 8-10M15 5h6v6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  bolt: '<path d="m13 2-9 12h7l-1 8 10-13h-7z"/>',
  link: '<path d="m10 13 4-4M8 16l-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0M16 8l2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0" transform="translate(2 0) scale(.83 1)"/>',
  refresh: '<path d="M20 8a8 8 0 1 0 0 8M20 3v5h-5"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6zM8 12l3 3 5-6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7v1"/>',
  export: '<path d="M12 3v12m-4-4 4 4 4-4M4 15v6h16v-6"/>',
  receipt: '<path d="M5 3v18l3-2 4 2 4-2 3 2V3l-3 2-4-2-4 2zM8 9h8M8 13h5"/>',
};
const icon = (name, cls = '') => `<svg class="icon ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.overview}</svg>`;
const initials = (name) => name.split(/\s+/).slice(0,2).map((v)=>v[0]).join('').toUpperCase();
const dateShift = (date, days) => { const d = new Date(date + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0,10); };
const today = new Intl.DateTimeFormat('en-CA', {timeZone:'America/Argentina/Buenos_Aires'}).format(new Date());
const prettyDate = (date, options = {}) => new Intl.DateTimeFormat('en-GB', {day:'numeric', month:'short', ...options}).format(new Date(date + 'T12:00:00Z'));
const number = (n) => n == null || !Number.isFinite(Number(n)) ? '—' : new Intl.NumberFormat('en-GB', {maximumFractionDigits:1}).format(n);
const money = (n, compact=false) => moneyFor(n,data.currency,compact);
const customers = [
  {id:'CUST-001',name:'Almacén Don Pedro',group:'Grocery store',territory:'Córdoba'},
  {id:'CUST-002',name:'Panadería Santa Rita',group:'Bakery',territory:'Córdoba'},
  {id:'CUST-003',name:'Kiosco La Esquina',group:'Convenience store',territory:'Córdoba'},
  {id:'CUST-004',name:'Café del Parque',group:'Café',territory:'Córdoba'},
  {id:'CUST-005',name:'Mercado Central',group:'Grocery store',territory:'Villa Allende'},
  {id:'CUST-006',name:'La Buena Mesa',group:'Restaurant',territory:'Córdoba'},
  {id:'CUST-007',name:'Despensa Los Olivos',group:'Grocery store',territory:'Córdoba'},
  {id:'CUST-008',name:'Café Magnolia',group:'Café',territory:'Villa Allende'},
];
const products = [
  {id:'LECHE-ENT-1L',name:'Whole milk · 1 L',unit:'units',price:1250,stock:400,reserved:124,available:276,warehouse:'Principal'},
  {id:'LECHE-DESC-1L',name:'Skimmed milk · 1 L',unit:'units',price:1290,stock:260,reserved:76,available:184,warehouse:'Principal'},
  {id:'MANTECA-200',name:'Butter · 200 g',unit:'units',price:2100,stock:90,reserved:34,available:56,warehouse:'Principal'},
  {id:'QUESO-CREM-1K',name:'Creamy cheese · 1 kg',unit:'kg',price:8900,stock:3,reserved:1,available:2,warehouse:'Principal'},
  {id:'YOG-FRUT-190',name:'Strawberry yogurt · 190 g',unit:'units',price:780,stock:500,reserved:115,available:385,warehouse:'Principal'},
  {id:'DDL-400',name:'Dulce de leche · 400 g',unit:'units',price:2450,stock:150,reserved:58,available:92,warehouse:'Principal'},
];
function makeDemo(range=7) {
  const orders = [];
  [4,6,5,7,6,8,9].forEach((count, day) => {
    for (let i=0;i<count;i++) {
      const index=orders.length, customer=customers[index%(day===6?6:5)], product=products[index%products.length];
      const items=[{name:product.name,qty:(i+1)*4,rate:product.price}];
      if (i%2===0) items.push({name:products[(index+2)%6].name,qty:6,rate:products[(index+2)%6].price});
      const total=items.reduce((sum,item)=>sum+item.qty*item.rate,0);
      const status=day===6 && i>=6 ? 'pending' : day===6 ? 'confirmed' : i===0 ? 'closed' : 'completed';
      orders.unshift({id:`SAL-ORD-2026-${String(index+101).padStart(5,'0')}`,customer:customer.name,customerId:customer.id,date:dateShift(today,day-6),deliveryDate:dateShift(today,day-5),total,currency:'ARS',status,channel:'WhatsApp',items,erpStatus:status==='pending'?'Draft':status==='completed'?'Completed':'To Deliver and Bill'});
    }
  });
  const at=time=>`${today}T${time}:00Z`;
  const conversationRows=[0,1,5,6,7].map((index,i)=>({customerId:customers[index].id,customerName:customers[index].name,turns:6-i,lastAt:at(`1${5-i}:30`),lastLine:index>=6?'Gracias, lo consulto y te aviso.':'Perfecto, dejame el pedido para mañana.',orderId:orders.find(o=>o.customerId===customers[index].id&&o.date===today)?.id||null}));
  const activity={date:today,conversations:conversationRows,newCustomers:conversationRows.filter(c=>c.customerId==='CUST-006'),truncated:[]};
  const conversations=Object.fromEntries(customers.map((c,i)=>[c.id,{customerId:c.id,customerName:c.name,reachable:i!==3,messages:i===3||i===4?[]:[{role:'customer',text:'Hola, ¿tenés leche entera para mañana?',at:at('11:12')},{role:'note',text:'The agent looked up the catalogue.',at:at('11:13')},{role:'agent',text:'Sí, tenemos leche entera de 1 L. ¿Cuántas unidades necesitás?',at:at('11:14')},{role:'customer',text:i>=6?'Gracias, lo consulto y te aviso.':'Preparame 12 unidades, por favor.',at:at('11:15')},{role:'agent',text:i>=6?'Dale, quedo atento.':'Dejé el pedido en borrador para que lo revise el equipo.',at:at('11:16')}],truncated:i===1,retentionDays:30}]));
  const queue={upcoming:[{id:'demo-delivery',type:'delivery_notice',orderId:orders[3].id,customer:orders[3].customer,dueAt:at('20:00'),what:'Remind the customer their order arrives at 17:00.'},{id:'demo-review',type:'owner_reminder',orderId:orders[0].id,customer:orders[0].customer,dueAt:at('19:00'),what:'Remind the owner that this draft still needs a decision.'}],waitingOnAPerson:orders.filter(o=>o.status==='pending').map(o=>({orderId:o.id,customer:o.customer,since:at('13:00'),what:'The order needs a manager’s review before it can be confirmed.'})),undelivered:{replies:2,notices:1}};
  const operations={redis:'Connected',worker:'Active lease',queuedMessages:0,queuedNotices:2,failedReplies:2,failedNotices:1};
  const booked=orders.filter(o=>['confirmed','completed'].includes(o.status));
  const salesTotal=booked.reduce((sum,o)=>sum+o.total,0);
  const sales=validateSales({currency:'ARS',since:dateShift(today,-range+1),until:today,total:salesTotal,orders:booked.length,averageOrder:Math.round(salesTotal/booked.length),
    daily:Array.from({length:range},(_,i)=>{const date=dateShift(today,-range+1+i),rows=booked.filter(o=>o.date===date);return {date,total:rows.reduce((sum,o)=>sum+o.total,0),orders:rows.length};}),
    topProducts:products.map(p=>{const rows=booked.flatMap(o=>o.items).filter(item=>item.name===p.name);return {id:p.id,name:p.name,quantity:rows.reduce((sum,item)=>sum+item.qty,0),total:rows.reduce((sum,item)=>sum+item.qty*item.rate,0)};}).sort((a,b)=>b.total-a.total),
    topCustomers:customers.map(c=>{const rows=booked.filter(o=>o.customerId===c.id);return {id:c.id,name:c.name,orders:rows.length,total:rows.reduce((sum,o)=>sum+o.total,0)};}).filter(c=>c.orders).sort((a,b)=>b.total-a.total),errors:[],truncated:[]});
  const advice=validateAdvice({generatedAt:at('15:50'),enabled:false,currency:'ARS',items:[
    {id:'demo-dormido',kind:'dormido',title:'A regular has gone quiet',body:'Café Magnolia has not ordered in the last 21 days.',about:'Café Magnolia',assumption:'Compared with a weekly ordering pattern over the previous eight weeks.',amount:null,customerId:'CUST-008',orderId:null,productId:null},
    {id:'demo-perdida',kind:'perdida',title:'Sold below cost',body:'Two lines on this order went out below their recorded purchase cost.',about:orders[3].id,assumption:'Cost taken from the purchase price list; rebates are not included.',amount:-18400,customerId:null,orderId:orders[3].id,productId:null},
    {id:'demo-deuda',kind:'deuda',title:'An overdue balance needs a look',body:'Almacén Don Pedro has a balance beyond the usual payment window.',about:'Almacén Don Pedro',assumption:'A 14-day payment tolerance; recent unallocated payments may change this.',amount:62500,customerId:'CUST-001',orderId:null,productId:null},
    {id:'demo-quiebre',kind:'quiebre',title:'Creamy cheese may run out',body:'Available stock may not last until the next scheduled delivery.',about:'Creamy cheese · 1 kg',assumption:'Demand follows the last seven days and the next delivery arrives in two days.',customerId:null,orderId:null,productId:'QUESO-CREM-1K'}
  ],errors:[],truncated:[]});
  const prices=validatePrices({priceList:'Standard Selling',currency:'ARS',bandPct:0,canChange:false,
    items:products.map(p=>({id:p.id,price:p.price,unit:'Unidad'})),errors:[],truncated:[]});
  const settings=validateSettings({problem:'',pending:null,groups:[
    {id:'negocio',name:'Your business',settings:[
      {id:'NOMBRE_NEGOCIO',name:'nombre del negocio',meaning:'How your business is named in the first line of both agent prompts',unit:'texto',kind:'texto',optional:true,value:'Plus Dairy',display:'Plus Dairy',source:'You set this',configured:true,problem:''},
      {id:'RUBRO_NEGOCIO',name:'rubro',meaning:'What the business does, in a few words',unit:'texto',kind:'texto',optional:true,value:'-',display:'-',source:'Shipped default',configured:false,problem:''},
      {id:'HORARIO_ATENCION',name:'horario de atencion',meaning:'The hours you tell a customer you are open',unit:'texto',kind:'texto',optional:false,value:'lunes a viernes de 8 a 17',display:'lunes a viernes de 8 a 17',source:'From the server file',configured:false,problem:''}]},
    {id:'plantillas',name:'WhatsApp templates',settings:[
      {id:'WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE',name:'plantilla de confirmado',meaning:'Tells the customer their order was confirmed',unit:'plantilla de Meta',kind:'plantilla',optional:true,value:'pedido_confirmado',display:'pedido_confirmado',source:'You set this',configured:true,problem:''},
      {id:'WHATSAPP_CUSTOMER_EXPIRED_TEMPLATE',name:'plantilla de vencido',meaning:'Tells the customer their request expired with no answer',unit:'plantilla de Meta',kind:'plantilla',optional:true,value:'-',display:'-',source:'Shipped default',configured:false,problem:''}]},
    {id:'limites',name:'Automatic confirmation',settings:[
      {id:'AUTO_CONFIRM_MAX',name:'monto maximo',meaning:'Largest order that can be confirmed without anyone looking at it',unit:'$',kind:'numero',optional:false,value:'0',display:'$ 0',source:'Shipped default',configured:false,problem:''},
      {id:'STOCK_BUFFER_PCT',name:'colchon de stock',meaning:'Stock held back for sales that are not loaded yet',unit:'%',kind:'numero',optional:false,value:'20',display:'20%',source:'Shipped default',configured:false,problem:''}]}]});
  return {prices,settings,sales,advice,mode:'demo',company:'Plus Dairy',today,since:dateShift(today,-29),currency:'ARS',generatedAt:new Date().toISOString(),orders,customers,products,activity:validateActivity(activity),conversations,queue:validateQueue(queue),operations,errors:[],truncated:[],limit:250,policies:[{name:'Order ceiling',value:'$ 150.000',note:'Maximum order value for automatic confirmation'},{name:'New customer ceiling',value:'$ 30.000',note:'Separate limit until a customer has order history'},{name:'Stock buffer',value:'20%',note:'Keep a buffer before confirming an order'},{name:'Stock trust window',value:'24 hours',note:'Require a recent confirmed stock count'}],agents:[{id:'sales',name:'Sales agent',role:'Customer conversations & order drafts',model:'Qwen · sales model',status:'Demo'},{id:'manager',name:'Management agent',role:'Business reports & manager assistance',model:'Qwen · management model',status:'Demo'}]};
}
const repoHosted = /\/dashboard(?:\/|$)/.test(location.pathname);
const demoRequested = new URLSearchParams(location.search).get('demo') === '1';
function disconnectedData() {
  return {mode:'disconnected',company:'Plus CRM',today,since:dateShift(today,-29),currency:'',generatedAt:new Date().toISOString(),orders:null,pendingOrders:null,customers:null,products:null,policies:null,agents:[],operations:null,activity:null,queue:null,conversations:null,sales:null,advice:null,settings:null,prices:null,errors:[],truncated:[],limit:250};
}
function freshReads(){return Object.fromEntries(['activity','queue','operations','sales','advice','settings','prices'].map(key=>[key,{busy:false,error:'',loadedAt:null,pending:null,range:null}]));}
let data=demoRequested?makeDemo():disconnectedData();
const state={theme:readThemePreference(),view:'today',range:7,filter:'all',search:'',stockFilter:'all',page:1,menu:false,busy:false,stale:false,connection:null,session:0,reads:freshReads(),extrasBusy:false,extrasError:'',extrasLoadedAt:null,detailRequest:0,connectRequest:0,configured:null,displayCurrency:'',currencyPreference:readCurrencyPreference(),fx:null,fxLoading:false,fxRequest:0,fxError:'',fxFailedTarget:''};
const currencyNames={ARS:'Argentine peso',INR:'Indian rupee',USD:'US dollar',EUR:'Euro',GBP:'British pound',BRL:'Brazilian real',UYU:'Uruguayan peso',CLP:'Chilean peso',MXN:'Mexican peso',CAD:'Canadian dollar',AUD:'Australian dollar',CHF:'Swiss franc',CNY:'Chinese yuan',JPY:'Japanese yen',AED:'UAE dirham'};
const fxCache=new Map();
const nav=[['today','Today'],['overview','Overview'],['sales','Sales'],['advice','Advice'],['queue','Coming up'],['orders','Orders'],['inventory','Inventory'],['customers','Customers'],['agents','AI agents']];
const labels={pending:'Pending review',confirmed:'Confirmed',completed:'Completed',cancelled:'Cancelled',closed:'Closed','on-hold':'On hold',unknown:'Unknown'};
const badge=(status)=>`<span class="badge badge-${escape(status)}">${icon(status==='pending'?'clock':status==='confirmed'||status==='completed'?'check':'info')}${escape(labels[status] || status)}</span>`;
function periodOrders() { const start=dateShift(data.today,-state.range+1); return (data.orders||[]).filter((o)=>o.date>=start && o.date<=data.today); }
function allOrders(){return [...new Map([...(data.orders||[]),...(data.pendingOrders||[])].map(o=>[o.id,o])).values()];}
function pendingOrders(){return data.pendingOrders ?? (data.mode==='demo'?(data.orders||[]).filter(o=>o.status==='pending'):null);}
function selectedOrders() {return (state.filter==='pending'?(pendingOrders()||[]):periodOrders()).filter((o)=>(state.filter==='all'||o.status===state.filter) && `${o.id} ${o.customer}`.toLowerCase().includes(state.search.toLowerCase()));}
function sumSales(orders) {return orders.filter(o=>['confirmed','completed'].includes(o.status)&&o.currency===data.currency).reduce((s,o)=>s+(o.total??0),0);}
function daysSeries() {return Array.from({length:state.range},(_,i)=>{const date=dateShift(data.today,-state.range+1+i);return {date,orders:periodOrders().filter(o=>o.date===date)};});}
function toast(message) { const node=$('#toast');node.textContent=message;node.classList.add('visible');clearTimeout(toast.timer);toast.timer=setTimeout(()=>node.classList.remove('visible'),4000); }
function goto(view) {if(!Object.hasOwn(views,view))return;state.view=view;state.search='';state.filter='all';state.stockFilter='all';state.page=1;state.menu=false;history.replaceState(null,'','#'+view);render();window.scrollTo({top:0,behavior:'instant'});return loadViewReads();}
function avatar(name,index=0) {return `<span class="avatar avatar-${index%5}" aria-hidden="true">${escape(initials(name))}</span>`;}
function shell() {
  const pending=(pendingOrders()||[]).length;
  const title=nav.find(([key])=>key===state.view)?.[1] || 'Connection & settings';
  return `<div class="dashboard ${state.menu?'menu-open':''}">
    <button class="sidebar-shade" data-action="close-menu" aria-label="Close navigation"></button>
    <aside class="sidebar" aria-label="Main navigation">
      <a class="brand" href="#today" data-view="today">${mateLogo('sidebar')}<span class="brand-wordmark">WhatsApp<span>Mate<span class="brand-period">.</span></span></span></a>
      <div class="workspace"><span class="workspace-icon">${icon('inventory')}</span><div><strong>${escape(data.company)}</strong><span>Operations workspace</span></div></div>
      <div class="nav-label">WORKSPACE</div>
      <nav>${nav.map(([key,label])=>`<a href="#${key}" data-view="${key}" class="nav-item ${state.view===key?'active':''}" ${state.view===key?'aria-current="page"':''}>${icon(key)}<span>${label}</span>${key==='orders'&&pending?`<span class="nav-count">${pending}</span>`:''}${key==='agents'?'<span class="new-tag">AI</span>':''}</a>`).join('')}</nav>
      <div class="sidebar-bottom"><div class="sidebar-note">${icon('shield')}<strong>You set the rules.</strong><p>Your agents work within the limits you approve.</p><button class="text-link" data-view="agents">View agent controls ${icon('arrow')}</button></div>
      <a href="#settings" data-view="settings" class="nav-item ${state.view==='settings'?'active':''}">${icon('settings')}<span>Settings</span></a>
      <div class="profile"><span class="avatar owner">${escape((data.company||'?').trim().charAt(0).toUpperCase())}</span><div><strong>${escape(data.company||'Not connected')}</strong><span>${data.mode==='demo'?'Demo workspace':data.mode==='disconnected'?'Not connected':'Connected workspace'}</span></div></div></div>
    </aside>
    <div class="main-wrap">
      <header class="topbar"><div class="breadcrumbs"><button class="icon-button menu-button" data-action="menu" aria-label="Open navigation" aria-expanded="${state.menu}">${icon('menu')}</button><span>Workspace</span><span class="crumb-slash">/</span><strong>${title}</strong></div>
      <div class="top-actions"><button class="icon-button theme-toggle" data-action="theme" aria-label="${state.theme==='dark'?'Switch to light mode':'Switch to charcoal night mode'}" title="${state.theme==='dark'?'Light mode':'Charcoal night mode'}" aria-pressed="${state.theme==='dark'}">${icon(state.theme==='dark'?'sun':'moon')}</button><span class="mode-chip ${data.mode==='live'?'live-chip':''}">${icon(data.mode==='demo'?'overview':'link')}${data.mode==='demo'?'Demo workspace':data.mode==='disconnected'?'Not connected':state.stale?'Connection interrupted':'Live data'}</span><button class="icon-button notification-button" data-action="pending" aria-label="View ${pending} orders awaiting review">${icon('bell')}${pending?'<span class="notification-dot"></span>':''}</button><span class="avatar owner small">${escape((data.company||'?').trim().charAt(0).toUpperCase())}</span></div></header>
      <main id="main" tabindex="-1">
        ${state.stale?'<div class="notice error-notice">Connection interrupted. The last snapshot remains visible; refresh to try again.</div>':''}
        ${data.errors.length?`<div class="notice error-notice">Some data could not be read: ${escape(data.errors.join(', '))}. Missing information is shown as unavailable.</div>`:''}
        <div class="page-heading"><div><div class="eyebrow">YOUR OPERATIONS, CONNECTED</div><h1>${title}</h1><p>${{today:'Who talked to your agent, and what they needed.',queue:'See the work scheduled next and the decisions waiting for you.',sales:'See what sells, who buys, and how your business is growing.',advice:'A closer look at the signals that deserve your attention.',overview:'A clear view of your business. Every order, every day.',orders:'Follow each order from received to fulfilled.',inventory:'Know what is on the shelf and already reserved.',customers:'The people and businesses behind your orders.',agents:'Your team behind the conversations.',settings:'Connect your dashboard to the agent service.'}[state.view]}</p></div>
        <div class="heading-actions">${currencySelector()}${['overview','orders','sales'].includes(state.view)?`<label class="select-wrap">${icon('calendar')}<select id="range" aria-label="Reporting period"><option value="7" ${state.range===7?'selected':''}>Last 7 days</option><option value="30" ${state.range===30?'selected':''}>Last 30 days</option></select></label>`:''}<button class="button ${data.mode!=='live'?'primary':''}" data-action="${data.mode!=='live'?'connect':'refresh'}" ${state.busy?'disabled':''}>${icon(data.mode!=='live'?'link':'refresh')}${state.busy?'Refreshing…':data.mode!=='live'?'Connect live data':'Refresh'}</button></div></div>
        ${currencyNotice()}<div id="view-content">${data.mode==='disconnected'?connectionGate():views[state.view]()}</div>
        <footer class="footer"><span>Plus CRM <span class="footer-dot">·</span> ${data.mode==='disconnected'?'Sign in to read your CRM':data.mode==='demo'?'Sample data for exploring the dashboard':`Snapshot · ${escape(new Date(data.generatedAt).toLocaleString('en-GB'))}`}</span><span>${data.currency?escape(state.displayCurrency||data.currency)+(state.displayCurrency?' display currency · ':' currency · '):''}Read-only workspace</span></footer>
      </main>
    </div></div>`;
}
function sparkline(values, color) {
  const max=Math.max(1,...values),points=values.map((v,i)=>`${i*15},${32-v/max*26}`).join(' ');
  return `<svg class="sparkline" viewBox="0 0 90 36" aria-hidden="true"><polyline points="${points}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}
function stats() {
  const orders=periodOrders(),missing=data.orders===null;
  const confirmed=orders.filter(o=>['confirmed','completed'].includes(o.status));
  const pending=pendingOrders()||[];
  const series=daysSeries().slice(-7), sales=sumSales(orders);
  const stats=[
    ['Booked sales',missing||!data.currency?'—':money(sales),'receipt','violet',`Confirmed orders · ${state.displayCurrency||data.currency||'currency unavailable'}`,series.map(d=>sumSales(d.orders))],
    ['Orders received',missing?'—':number(orders.length),'orders','blue','All orders in this period',series.map(d=>d.orders.length)],
    ['Confirmed orders',missing?'—':number(confirmed.length),'check','green','Including completed orders',series.map(d=>d.orders.filter(o=>['confirmed','completed'].includes(o.status)).length)],
    ['Awaiting review',pendingOrders()===null?'—':number(pending.length),'clock','amber',pendingOrders()===null?'Pending orders unavailable':pending.length?'Open drafts · all dates':'No pending orders',series.map(d=>d.orders.filter(o=>o.status==='pending').length)]
  ];
  return `<div class="stats-grid">${stats.map(([label,value,ic,color,note,trend])=>`<article class="stat-card"><div class="stat-top"><span>${label}</span><span class="stat-icon ${color}">${icon(ic)}</span></div><div class="stat-value ${label==='Booked sales'?'money-value':''}">${value}</div><div class="stat-bottom"><span>${note}</span>${sparkline(trend,{violet:'#7665e8',blue:'#6396e9',green:'#4fa889',amber:'#c8983e'}[color])}</div></article>`).join('')}</div>`;
}
function chart() {
  if(data.orders===null||!data.currency)return `<section class="card chart-card"><div class="card-heading"><h2>Sales overview</h2></div>${empty('Sales data is unavailable','Refresh the connection to try again.')}</section>`;
  const series=daysSeries(),values=series.map(d=>sumSales(d.orders)),max=Math.max(1,...values);
  const total=values.reduce((a,b)=>a+b,0);
  return `<section class="card chart-card"><div class="card-heading"><div><h2>Sales overview</h2><p>Confirmed order value over time</p></div><span class="legend"><i></i>Booked sales</span></div><div class="chart-summary"><strong>${data.orders===null||!data.currency?'—':money(total)}</strong><span>${prettyDate(dateShift(data.today,-state.range+1))} – ${prettyDate(data.today)}</span></div>
  <div class="chart" role="img" aria-label="Booked sales by day. Total ${escape(money(total))}."><div class="chart-axis">${[1,.75,.5,.25,0].map(n=>`<span>${money(max*n,true)}</span>`).join('')}</div><div class="plot"><div class="grid-lines"><i></i><i></i><i></i><i></i><i></i></div><div class="bars ${state.range===30?'dense':''}">${series.map((d,i)=>`<div class="bar-column"><button class="bar ${i===series.length-1?'current':''}" style="--height:${values[i]/max*100}%" data-chart-date="${d.date}" aria-label="${prettyDate(d.date)}: ${escape(money(values[i]))}" title="${prettyDate(d.date)} · ${escape(money(values[i]))}"></button><span>${state.range===7?prettyDate(d.date,{weekday:'short'}).split(',')[0].split(' ')[0]:i%5===0||i===29?new Date(d.date+'T12:00:00Z').getUTCDate():''}</span></div>`).join('')}</div></div></div><p id="chart-detail" class="chart-note">Select a bar to inspect its daily total.</p></section>`;
}
function orderMix() {
  if(data.orders===null)return `<section class="card mix-card"><div class="card-heading"><h2>Order breakdown</h2></div>${empty('Order data is unavailable','Refresh the connection to try again.')}</section>`;
  const orders=periodOrders(),confirmed=orders.filter(o=>['confirmed','completed'].includes(o.status)).length,pending=orders.filter(o=>o.status==='pending').length,other=orders.length-confirmed-pending,total=orders.length;
  const p=total?confirmed/total*100:0,q=total?pending/total*100:0;
  return `<section class="card mix-card"><div class="card-heading"><div><h2>Order breakdown</h2><p>Where your orders stand</p></div>${icon('orders')}</div><div class="donut-wrap"><div class="donut" style="--confirmed:${p}%;--pending:${p+q}%" role="img" aria-label="${confirmed} confirmed, ${pending} pending, ${other} other"><div><strong>${data.orders===null?'—':total}</strong><span>Total orders</span></div></div></div><div class="mix-legend">${[['Confirmed',confirmed,'purple'],['Pending review',pending,'orange'],['Other states',other,'gray']].map(([label,count,c])=>`<div><span><i class="${c}"></i>${label}</span><strong>${count}</strong><small>${total?Math.round(count/total*100):0}%</small></div>`).join('')}</div></section>`;
}
function orderTable(orders, compact=false, unavailable=data.orders===null) {
  if(unavailable)return empty('Order data is unavailable','Refresh the connection to try again.');
  if(!orders.length)return empty('No orders found','Try another search or reporting period.');
  return `<div class="table-scroll"><table><thead><tr><th>Order</th><th>Customer</th><th>Status</th><th>${compact?'Channel':'Order date'}</th><th class="amount">Amount</th><th><span class="sr-only">Details</span></th></tr></thead><tbody>${orders.map((o,i)=>`<tr><td><button class="order-link" data-order="${escape(o.id)}">${escape(o.id.replace('SAL-ORD-','SO-'))}</button><span class="cell-note">${prettyDate(o.date)}</span></td><td><div class="customer-cell">${avatar(o.customer,i)}<span>${escape(o.customer)}</span></div></td><td>${badge(o.status)}</td><td>${compact?`<span class="channel"><i class="${o.channel==='WhatsApp'?'wa':''}"></i>${escape(o.channel)}</span>`:escape(o.date)}</td><td class="amount">${moneyFor(o.total,o.currency)}</td><td><button class="icon-button row-open" data-order="${escape(o.id)}" aria-label="View order ${escape(o.id)}">${icon('arrow')}</button></td></tr>`).join('')}</tbody></table></div>`;
}
function moneyFor(value,currency,compact=false) {
  const shown=displayAmount(value,currency);
  return formatMoney(shown.value,shown.currency,compact)+(shown.unavailable?' · original':'');
}
function empty(title,note) {return `<div class="empty">${icon('search')}<h3>${escape(title)}</h3><p>${escape(note)}</p></div>`;}
function agentsMini() {
  return `<section class="card agents-strip"><div class="strip-title"><span class="stat-icon violet">${icon('agents')}</span><div><h2>Your AI team</h2><p>Two roles. One connected business.</p></div></div>${data.agents.map(a=>`<div class="mini-agent"><span class="agent-avatar ${a.id}">${icon(a.id==='sales'?'bolt':'shield')}</span><div><strong>${escape(a.name)}</strong><span>${escape(a.role)}</span></div><span class="subtle-pill">${escape(a.status)}</span></div>`).join('')}<button class="icon-button" data-view="agents" aria-label="View AI agents">${icon('arrow')}</button></section>`;
}
function attention() {
  const pending=(pendingOrders()||[]),low=(data.products||[]).filter(p=>p.available!==null&&p.available<=10);
  return `<section class="card attention-card"><div class="card-heading"><div><h2>Needs attention <span class="count-bubble">${pendingOrders()===null||data.products===null?'—':pending.length+low.length}</span></h2><p>The exceptions worth a closer look</p></div></div>${pending.length?`<button class="attention-item" data-action="pending"><span class="attention-icon amber">${icon('clock')}</span><div><strong>${pending.length} orders awaiting review</strong><span>Waiting for a manager’s decision</span></div>${icon('arrow')}</button>`:''}${low.map(p=>`<button class="attention-item" data-product="${escape(p.id)}"><span class="attention-icon rose">${icon('inventory')}</span><div><strong>${escape(p.name)}</strong><span>${number(p.available)} ${escape(p.unit)} of ERP stock available</span></div>${icon('arrow')}</button>`).join('')}${pendingOrders()===null||data.products===null?'<div class="quiet-state">Some alert data is unavailable. Refresh to check again.</div>':!pending.length&&!low.length?'<div class="quiet-state">No alerts in the loaded records.</div>':''}<div class="attention-foot">${icon('shield')}<span>Approvals stay with your manager.</span></div></section>`;
}
function overview() {
  return `${listLimit('orders','pending orders')}${stats()}<div class="overview-activity">${comingUpCard()}${deliveryHealth()}</div><div class="overview-top">${chart()}${orderMix()}</div>${agentsMini()}<div class="overview-bottom"><section class="card orders-card"><div class="card-heading"><div><h2>Recent orders</h2><p>The latest activity in your business</p></div><button class="text-link" data-view="orders">View all ${icon('arrow')}</button></div>${listLimit('orders')}${orderTable(periodOrders().slice(0,5),true)}</section>${attention()}</div>`;
}
function listLimit(...keys) {
  // Report endpoints own their caps; a capped ranking does not imply a capped total.
  if(keys[0]&&typeof keys[0]==='object'){
    const report=keys.shift();
    return keys.some(key=>report.truncated?.includes(key))?'<p class="list-notice">This list was capped. Some records are not shown.</p>':'';
  }
  return keys.some(key=>data.truncated.includes(key))?`<p class="list-notice">This list is limited to ${escape(data.limit)} source records per section. Some records are not shown; counts and totals cover loaded records only.</p>`:'';
}
function prettyMoment(value) {
  return new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}).format(new Date(value));
}
function timeZoneNote(){return `Times shown in ${Intl.DateTimeFormat().resolvedOptions().timeZone}.`;}
function readStatus(key) {
  const read=state.reads[key];
  return `<span class="read-status" role="status">${escape(data.mode==='demo'?'Sample data':read.busy?'Reading…':read.loadedAt?'Last read · '+prettyMoment(read.loadedAt):'Not yet available')}</span>`;
}
function readButton(key) {
  return data.mode==='live'?`<button class="button" data-read="${escape(key)}" ${state.reads[key].busy?'disabled':''}>${icon('refresh')}Refresh</button>`:'';
}
function readEmpty(key,title) {
  return empty(state.reads[key].busy?'Loading…':title,state.reads[key].busy?'Reading from your agent service.':state.reads[key].error||'Open this view or refresh to read the latest information.');
}
// Lightweight vector interpretation of the supplied WhatsApp Mate mark.
// IDs are scoped because the sidebar and launch reveal can coexist.
function mateLogo(scope) {
  return `<svg class="mate-logo" viewBox="0 0 100 100" aria-hidden="true"><defs><linearGradient id="mate-${scope}" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#39e154"/><stop offset=".5" stop-color="#00b678"/><stop offset="1" stop-color="#007478"/></linearGradient></defs><path fill="url(#mate-${scope})" d="M50 3a47 47 0 1 1-24 87L9 95q-6 2-4-5l4-15A47 47 0 0 1 50 3Z"/><g fill="#fff"><circle cx="27" cy="38" r="7"/><circle cx="72" cy="38" r="7"/></g><path class="mate-ribbon" d="m22 53 9 19q5 10 11-1l12-21q5-9 10 1l9 21q4 9 9-3l7-17" fill="none" stroke="#fff" stroke-width="12" stroke-linecap="round" stroke-linejoin="round" transform="translate(-4 0)"/><path d="M50 22v7m-9 0 3 4m15-4-3 4" fill="none" stroke="#fff" stroke-width="2.7" stroke-linecap="round"/></svg>`;
}
function readThemePreference() {
  try{return localStorage.getItem('plus.dashboard.theme')==='dark'?'dark':'light';}catch{return 'light';}
}
function revealLogo() {
  const reveal=document.createElement('div');
  reveal.className='logo-reveal';reveal.setAttribute('aria-hidden','true');
  reveal.innerHTML=`<div class="logo-reveal-content">${mateLogo('launch')}<strong>WhatsApp <span>Mate</span></strong><p>Chat smarter. Together.</p><span class="reveal-line"></span></div>`;
  document.body.append(reveal);
  // Decoration only: it never owns focus, captures input, or delays a read.
  setTimeout(()=>reveal.remove(),1400);
}
function reportErrors(report) {
  if(report.errors===null)return '<p class="list-note">Error details unavailable.</p>';
  return report.errors?.length?`<div class="notice error-notice" role="status">${report.errors.map(error=>`<p>${escape(error)}</p>`).join('')}</div>`:'';
}
function reportWarnings(report,known) {
  if(report.truncated===null)return '<p class="list-note">List completeness unavailable.</p>';
  const unknown=report.truncated?.filter(key=>!known.includes(key))||[];
  return unknown.length?`<p class="list-notice">Additional lists were capped: ${escape(unknown.join(', '))}.</p>`:'';
}
function salesMoney(value,currency){return value===null||currency===null?'Unavailable':moneyFor(value,currency);}
function currentSales(){return data.mode==='live'&&state.reads.sales.range!==state.range?null:data.sales;}
function salesChart(report) {
  const rows=report.daily;
  if(rows===null)return empty('Daily sales unavailable','The daily breakdown could not be read. Your other sales figures may still be available.');
  if(!rows.length)return empty('No daily sales yet','Daily totals will appear here when sales are recorded in this period.');
  if(report.currency===null)return empty('Sales currency unavailable','The daily amounts cannot be displayed until their currency is available.');
  const above=Math.max(0,...rows.map(row=>row.total)),below=Math.max(0,...rows.map(row=>-row.total)),extent=Math.max(1,above+below);
  return `<div class="sales-chart" aria-label="Daily sales"><div class="sales-plot" style="--baseline:${below/extent*100}%">${rows.map((row,i)=>`<div class="sales-column"><button class="sales-day" data-sales-date="${escape(row.date)}" aria-label="${escape(prettyDate(row.date))}: ${escape(moneyFor(row.total,report.currency))}, ${number(row.orders)} orders" title="${escape(prettyDate(row.date))} · ${escape(moneyFor(row.total,report.currency))} · ${number(row.orders)} orders"><span class="sales-bar ${row.total<0?'negative':''}" style="height:${Math.abs(row.total)/extent*100}%;bottom:${(row.total<0?below+row.total:below)/extent*100}%"></span></button><span class="sales-day-label">${rows.length<=10||i%5===0||i===rows.length-1?escape(row.date.slice(8)):''}</span></div>`).join('')}</div></div><p class="list-note">Select a day to see its sales and orders. Only returned dates are shown.</p>`;
}
function salesRanking(report,key,label) {
  const rows=report[key],isCustomer=key==='topCustomers';
  return `<section class="card sales-ranking"><div class="card-heading"><div><h2>${label}</h2><p>${isCustomer?'The relationships behind your revenue':'Your strongest products by sales'}</p></div><span class="stat-icon ${isCustomer?'blue':'green'}">${icon(isCustomer?'customers':'inventory')}</span></div>${listLimit(report,key)}${rows===null?empty(`${label} unavailable`,'This ranking could not be read. Refresh to try again.'):!rows.length?empty(isCustomer?'No customer sales yet':'No product sales yet','Your ranking will take shape as sales are recorded.'): `<ol class="sales-ranking-list">${rows.map((row,i)=>`<li><span class="rank-number">${String(i+1).padStart(2,'0')}</span>${isCustomer?avatar(row.name,i):`<span class="product-icon">${icon('inventory')}</span>`}<div class="rank-identity">${isCustomer?`<button class="text-link customer-name" data-customer="${escape(row.id)}">${escape(row.name)}</button>`:`<strong>${escape(row.name)}</strong>`}<span>${isCustomer?`${number(row.orders)} orders`:`${number(row.quantity)} sold · ${escape(row.id)}`}</span></div><strong class="rank-total">${escape(salesMoney(row.total,report.currency))}</strong></li>`).join('')}</ol>`}</section>`;
}
function salesView() {
  const report=currentSales();
  const header=`<div class="card-heading"><div><h2>Sales performance</h2><p>${report?`${report.since===null?'Start date unavailable':escape(prettyDate(report.since,{year:'numeric'}))} — ${report.until===null?'End date unavailable':escape(prettyDate(report.until,{year:'numeric'}))}`:'Revenue, orders, and the people behind them'}</p>${readStatus('sales')}</div>${readButton('sales')}</div>`;
  if(!report)return `<section class="card sales-report">${header}${readEmpty('sales','Sales are unavailable')}</section>`;
  const metrics=[['Total sales',salesMoney(report.total,report.currency),'sales','green','Sales reported for this period'],['Orders',report.orders===null?'Unavailable':number(report.orders),'orders','blue','Orders reported for this period'],['Average order',salesMoney(report.averageOrder,report.currency),'receipt','violet','Average value reported by your agent']];
  return `${reportErrors(report)}${reportWarnings(report,['daily','topProducts','topCustomers'])}<div class="stats-grid sales-stats">${metrics.map(([label,value,ic,color,note])=>`<article class="stat-card"><div class="stat-top"><span>${label}</span><span class="stat-icon ${color}">${icon(ic)}</span></div><div class="stat-value money-value ${value==='Unavailable'?'unavailable-value':''}">${escape(value)}</div><div class="stat-bottom"><span>${note}</span></div></article>`).join('')}</div><section class="card sales-report">${header}${listLimit(report,'daily')}${salesChart(report)}</section><div class="sales-rankings">${salesRanking(report,'topProducts','Top products')}${salesRanking(report,'topCustomers','Top customers')}</div>`;
}
const adviceKinds={
  perdida:{label:'Below cost',icon:'sales',note:'Sales that may have gone out below cost.'},
  dormido:{label:'Quiet customers',icon:'customers',note:'Regular customers whose ordering pattern has changed.'},
  deuda:{label:'Overdue debt',icon:'receipt',note:'Balances aging beyond the payment tolerance.'},
  quiebre:{label:'Stock risk',icon:'inventory',note:'Products that may run out before the next delivery.'}
};
function adviceGuide() {
  return `<section class="card advice-guide"><div class="card-heading"><div><h2>What advice looks for</h2><p>Four useful checks. Every finding explains its assumptions.</p></div></div><div class="advice-guide-grid">${Object.entries(adviceKinds).map(([kind,meta])=>`<div class="advice-kind-${kind}"><span class="advice-kind-icon">${icon(meta.icon)}</span><h3>${meta.label}</h3><p>${meta.note}</p></div>`).join('')}</div></section>`;
}
function adviceRow(item,currency) {
  const meta=adviceKinds[item.kind];
  return `<article class="advice-row advice-kind-${item.kind}"><div class="advice-row-heading"><span class="advice-kind-icon">${icon(meta.icon)}</span><div><span class="advice-category">${meta.label}</span><h3>${escape(item.title)}</h3></div><span class="advice-amount ${item.amount===null?'amount-unavailable':''}">${item.amount===null?'Amount unavailable':escape(moneyFor(item.amount,currency))}</span></div><p class="advice-body">${escape(item.body)}</p><div class="advice-assumption">${icon('info')}<p><strong>Assumption</strong>${escape(item.assumption)}</p></div><div class="advice-row-footer"><span>${escape(item.about)}</span><div>${item.orderId?`<button class="text-link" data-order="${escape(item.orderId)}">View order ${icon('arrow')}</button>`:''}${item.customerId?`<button class="text-link" data-customer="${escape(item.customerId)}">View customer ${icon('arrow')}</button>`:''}${item.productId?`<span class="advice-product">Product · ${escape(item.productId)}</span>`:''}</div></div></article>`;
}
function adviceView() {
  const report=data.advice;
  const header=`<div class="card-heading"><div><h2>Business signals</h2><p>${report?'Generated · '+escape(prettyMoment(report.generatedAt)):'The findings worth a closer look'} · ${escape(timeZoneNote())}</p>${readStatus('advice')}</div>${readButton('advice')}</div>`;
  if(!report)return `<section class="card advice-report">${header}${readEmpty('advice','Advice is unavailable')}</section>${adviceGuide()}`;
  const rows=report.items.filter(item=>(state.filter==='all'||item.kind===state.filter)&&`${item.title} ${item.body} ${item.about} ${item.assumption}`.toLowerCase().includes(state.search.toLowerCase()));
  return `${reportErrors(report)}${reportWarnings(report,['items'])}${!report.enabled?`<section class="advice-off"><span class="advice-off-icon">${icon('advice')}</span><div><span class="advice-off-label">ADVICE IS OFF</span><h2>A little foresight, when you’re ready.</h2><p>Automatic advice is currently switched off. ${data.mode==='demo'?'These sample findings show what you would see.':'Any findings below are from the last available report.'} Each finding includes the assumption behind it, so you can decide what deserves a closer look.</p></div><span class="subtle-pill">${data.mode==='demo'?'Sample preview':'Switched off'}</span></section>`:''}<section class="card advice-report">${header}<div class="advice-toolbar"><div class="filter-tabs" role="group" aria-label="Filter advice">${[['all','All signals'],...Object.entries(adviceKinds).map(([key,meta])=>[key,meta.label])].map(([key,label])=>`<button data-filter="${key}" class="${state.filter===key?'selected':''}" aria-pressed="${state.filter===key}">${label}<span>${report.items.filter(item=>key==='all'||item.kind===key).length}</span></button>`).join('')}</div>${searchField('Search advice…')}</div>${listLimit(report,'items')}<p class="list-note">${number(rows.length)} displayed · Highest urgency first. Findings need your judgment; no action is taken here.</p>${rows.length?`<div class="advice-list">${rows.map(item=>adviceRow(item,report.currency)).join('')}</div>`:empty(report.items.length?'No matching advice':report.enabled?'No advice to review':'No saved findings',report.items.length?'Try another category or search.':report.enabled?'No findings were returned in this report. New signals will appear here when available.':'Advice is off. The checks below explain what this screen can show once it is enabled.')}</section>${!report.enabled||!report.items.length?adviceGuide():''}`;
}

function activityList(rows) {
  return `<div class="activity-list">${rows.map(row=>`<article class="activity-row ${row.orderId===null?'without-order':''}"><div class="activity-person">${avatar(row.customerName)}<div><button class="text-link customer-name" data-customer="${escape(row.customerId)}">${escape(row.customerName)} ${icon('arrow')}</button><span>${escape(number(row.turns))} turns · ${escape(prettyMoment(row.lastAt))}</span></div>${row.orderId===null?'<span class="no-order-label">No order yet</span>':''}</div><p class="last-line">${escape(row.lastLine||'No message preview available.')}</p><div class="activity-links"><button class="text-link" data-customer="${escape(row.customerId)}">Open customer & conversation ${icon('arrow')}</button>${row.orderId!==null?`<button class="text-link" data-order="${escape(row.orderId)}">View order ${escape(row.orderId)} ${icon('arrow')}</button>`:'<span class="muted">Talked to the agent without placing an order.</span>'}</div></article>`).join('')}</div>`;
}
function todayView() {
  const activity=data.activity;
  const header=`<div class="card-heading"><div><h2>${activity?'Activity · '+escape(prettyDate(activity.date,{year:'numeric'})):'Customer activity'}</h2><p>${escape(timeZoneNote())}</p>${readStatus('activity')}</div>${readButton('activity')}</div>`;
  if(!activity)return `<section class="card">${header}${readEmpty('activity','Today is unavailable')}</section>`;
  const without=activity.conversations.filter(row=>row.orderId===null),withOrder=activity.conversations.filter(row=>row.orderId!==null);
  const cut=activity.truncated.length?'<p class="list-notice">Today’s lists are incomplete. Some records are not shown; counts cover loaded activity only.</p>':'';
  return `<div class="day-summary"><div><strong>${escape(number(activity.conversations.length))}</strong><span>Customers who talked to the agent</span></div><div class="opportunity-summary"><strong>${escape(number(without.length))}</strong><span>Conversations without an order</span></div><div><strong>${escape(number(activity.newCustomers.length))}</strong><span>Customers with a first order today</span></div></div><section class="card today-card">${header}${cut}<div class="section-heading"><h3>Conversations without an order</h3><p>Start here: these customers showed interest and have not ordered.</p></div>${without.length?activityList(without):empty('No conversations without an order','Every conversation in this list is linked to an order, or no activity has been recorded.')}<div class="section-heading"><h3>Conversations with an order</h3></div>${withOrder.length?activityList(withOrder):empty('No conversations with an order','New orders linked to today’s conversations will appear here.')}</section><section class="card today-card"><div class="card-heading"><div><h2>First orders today</h2><p>The beginning of a customer relationship</p></div></div>${cut}${activity.newCustomers.length?activityList(activity.newCustomers):empty('No first orders today','Customers placing their first order today will appear here.')}</section>`;
}
function queueRows(rows,waiting=false) {
  return `<div class="queue-list">${rows.map(row=>`<article class="queue-row"><span class="stat-icon ${waiting?'amber':'violet'}">${icon(waiting?'clock':'queue')}</span><div><p class="queue-what">${escape(row.what)}</p><span>${escape(row.customer)}</span><small>${waiting?'Waiting since':'Scheduled for'} ${escape(prettyMoment(waiting?row.since:row.dueAt))}</small></div><button class="text-link" data-order="${escape(row.orderId)}">${escape(row.orderId)} ${icon('arrow')}</button></article>`).join('')}</div>`;
}
function comingUpCard() {
  const queue=data.queue;
  return `<section class="card coming-up-card"><div class="card-heading"><div><h2>Coming up</h2><p>The agent’s next scheduled work</p>${readStatus('queue')}</div><button class="text-link" data-view="queue">View all ${icon('arrow')}</button></div>${queue?`${queue.waitingOnAPerson.length?`<button class="waiting-banner" data-view="queue">${icon('clock')}<span><strong>${escape(number(queue.waitingOnAPerson.length))} ${queue.waitingOnAPerson.length===1?'order':'orders'} waiting on a person</strong><small>A decision is needed before these orders can move forward.</small></span>${icon('arrow')}</button>`:'<p class="list-note">No orders waiting on a person in this read.</p>'}${queueRows(queue.upcoming.slice(0,3))}${queue.upcoming.length?'':empty('Nothing scheduled','The agent has no upcoming work in this read.')}${queue.upcoming.length>3?`<p class="list-note">Showing the next 3 of ${escape(number(queue.upcoming.length))} scheduled actions.</p>`:''}`:readEmpty('queue','Scheduled work is unavailable')}</section>`;
}
function failureCounts(replies,notices) {
  return `<div class="delivery-counts"><p class="${replies>0?'delivery-warning':''}"><strong>${escape(number(replies))}</strong> ${replies===1?'reply never reached a customer':'replies never reached a customer'}</p><p class="${notices>0?'delivery-warning':''}"><strong>${escape(number(notices))}</strong> ${notices===1?'notice was not delivered':'notices were not delivered'}</p></div>`;
}
function deliveryHealth() {
  const ops=data.operations;
  return `<section class="card delivery-card"><div class="card-heading"><div><h2>Delivery health</h2><p>Check messages that need attention</p>${readStatus('operations')}</div>${readButton('operations')}</div>${ops?`${failureCounts(ops.failedReplies,ops.failedNotices)}<dl class="health-fields"><div><dt>Redis</dt><dd>${escape(ops.redis)}</dd></div><div><dt>Worker</dt><dd>${escape(ops.worker)}</dd></div><div><dt>Queued replies / notices</dt><dd>${escape(number(ops.queuedMessages))} / ${escape(number(ops.queuedNotices))}</dd></div></dl><div class="card-footer"><button class="text-link" data-view="agents">Open service details ${icon('arrow')}</button></div>`:readEmpty('operations','Delivery status is unavailable')}</section>`;
}
function queueView() {
  const queue=data.queue;
  const header=`<div class="card-heading"><div><h2>Scheduled work</h2><p>${escape(timeZoneNote())} Refresh to see scheduling changes.</p>${readStatus('queue')}</div>${readButton('queue')}</div>`;
  if(!queue)return `<section class="card">${header}${readEmpty('queue','Scheduled work is unavailable')}</section>`;
  return `<section class="card queue-card">${header}${queue.upcoming.length?queueRows(queue.upcoming):empty('Nothing scheduled','The agent has no upcoming work in this read.')}</section><section class="card queue-card"><div class="card-heading"><div><h2>${escape(number(queue.waitingOnAPerson.length))} ${queue.waitingOnAPerson.length===1?'order':'orders'} waiting on a person</h2><p>The decisions holding up the next step</p></div></div>${queue.waitingOnAPerson.length?queueRows(queue.waitingOnAPerson,true):empty('No decisions waiting','No orders are waiting on a person in this read.')}</section><section class="card queue-card"><div class="card-heading"><div><h2>Messages that did not arrive</h2><p>Failed delivery counts from this queue read</p></div></div>${failureCounts(queue.undelivered.replies,queue.undelivered.notices)}</section>`;
}
function filterTabs() {
  const opts=[['all','All orders'],['pending','Awaiting review'],['confirmed','Confirmed'],['completed','Completed']];
  return `<div class="filter-tabs" role="group" aria-label="Filter order status">${opts.map(([key,label])=>`<button data-filter="${key}" class="${state.filter===key?'selected':''}" aria-pressed="${state.filter===key}">${label}<span>${key==='pending'?pendingOrders()===null?'—':pendingOrders().length:data.orders===null?'—':key==='all'?periodOrders().length:periodOrders().filter(o=>o.status===key).length}</span></button>`).join('')}</div>`;
}
function searchField(placeholder) {return `<label class="search-field">${icon('search')}<input id="search" type="search" placeholder="${placeholder}" aria-label="${placeholder}" value="${escape(state.search)}"></label>`;}
function ordersView() {
  const selected=selectedOrders(),maxPage=Math.max(1,Math.ceil(selected.length/10));state.page=Math.min(state.page,maxPage);
  return `${stats()}<section class="card orders-full"><div class="orders-toolbar">${filterTabs()}<button class="button" data-action="export" ${state.filter==='pending'?pendingOrders()===null?'disabled':'':data.orders===null?'disabled':''}>${icon('export')}Export CSV</button></div><div class="search-toolbar">${searchField('Search orders or customers…')}<span>${selected.length} orders${state.filter==='pending'?' · all dates':''}</span></div><div id="orders-results">${listLimit(state.filter==='pending'?'pending orders':'orders')}${orderTable(selected.slice((state.page-1)*10,state.page*10),false,state.filter==='pending'?pendingOrders()===null:data.orders===null)}</div><div class="pagination"><span>Page ${state.page} of ${maxPage}</span><div><button class="button" data-action="prev" ${state.page===1?'disabled':''}>Previous</button><button class="button" data-action="next" ${state.page>=maxPage?'disabled':''}>Next ${icon('arrow')}</button></div></div></section>`;
}
// El precio de UN producto, cruzado por `id` contra la lectura de `/prices`.
// Tres estados y no dos: no se leyó (`—`), se leyó y no tiene precio en esta
// lista (`Not priced`), y el precio. El del medio importa: un producto sin
// precio en la lista de auto-confirmación es uno que el agente no puede
// cotizar, y verlo en blanco lo hace parecer un problema de conexión.
function priceCell(id) {
  const report=data.prices;
  if(!report||report.items===null)return '<span class="muted">—</span>';
  const row=report.items.find(p=>p.id===id);
  if(!row)return '<span class="muted">Not priced</span>';
  const texto=row.price===null?'—':salesMoney(row.price,report.currency);
  return `<span>${escape(texto)}</span>${report.canChange?`<button class="link-button" data-price="${escape(id)}">Change</button>`:''}`;
}
function priceNotice() {
  const report=data.prices;
  if(data.mode!=='live'||!report)return '';
  if(report.errors.includes('priceList'))return `<div class="notice">${icon('info')} This agent has no price list or currency set for automatic confirmation, so list prices cannot be read or changed here.</div>`;
  if(!report.canChange)return `<div class="notice">${icon('info')} Price changes from this dashboard are off. They turn on by setting a daily band in Settings — «PRECIO_CAMBIO_MAX_PCT», which ships at 0 so nothing changes a price on its own.</div>`;
  return `<div class="notice">${icon('info')} A price can move up to ${escape(String(report.bandPct))}% per product per day, and that daily cap is the real ceiling.</div>`;
}
function inventoryView() {
  const rows=(data.products||[]).filter(p=>`${p.name} ${p.id}`.toLowerCase().includes(state.search.toLowerCase())&&(state.stockFilter!=='low'||p.available!==null&&p.available<=10));
  const low=(data.products||[]).filter(p=>p.available!==null&&p.available<=10).length;
  return `${priceNotice()}<div class="inventory-summary"><div><span class="stat-icon blue">${icon('inventory')}</span><div><strong>${data.products===null?'—':data.products.length}</strong><span>Products in the warehouse</span></div></div><div><span class="stat-icon amber">${icon('clock')}</span><div><strong>${data.products===null?'—':low}</strong><span>At or below 10 available units</span></div></div><div class="inventory-definition">${icon('info')}<p>ERP available = physical stock − submitted reservations. Open drafts and safety rules can reduce what an agent may confirm.</p></div></div><section class="card"><div class="search-toolbar">${searchField('Search products or item codes…')}<label class="select-wrap"><select id="stock-filter" aria-label="Filter inventory"><option value="all">All products</option><option value="low" ${state.stockFilter==='low'?'selected':''}>Low stock · 10 or fewer</option></select></label></div>${listLimit('inventory','product names')}${data.products===null?empty('Inventory is unavailable','Refresh the connection to try again.'):rows.length?`<div class="table-scroll"><table><thead><tr><th>Product</th><th>List price</th><th>On hand</th><th>Reserved</th><th>ERP available</th><th>Stock position</th><th></th></tr></thead><tbody>${rows.map(p=>`<tr><td><button class="product-cell" data-product="${escape(p.id)}"><span class="product-icon">${icon('inventory')}</span><span><strong>${escape(p.name)}</strong><small>${escape(p.id)}</small></span></button></td><td class="price-cell">${priceCell(p.id)}</td><td>${number(p.stock)} <span class="muted">${escape(p.unit)}</span></td><td>${number(p.reserved)}</td><td><strong>${number(p.available)}</strong></td><td><div class="stock-meter"><span style="width:${p.stock?Math.max(0,Math.min(100,p.available/p.stock*100)):0}%" class="${p.available!==null&&p.available<=10?'low':''}"></span></div><span class="cell-note">${p.available===null?'Unknown':p.available<=10?'Low stock':'In stock'}</span></td><td><button class="icon-button" data-product="${escape(p.id)}" aria-label="View ${escape(p.name)}">${icon('arrow')}</button></td></tr>`).join('')}</tbody></table></div>`:empty('No matching products','Try a different search or stock filter.')}</section>`;
}
function customersView() {
  const rows=(data.customers||[]).filter(c=>`${c.name} ${c.territory}`.toLowerCase().includes(state.search.toLowerCase()));
  return `<section class="card"><div class="search-toolbar">${searchField('Search customers or locations…')}<span>${rows.length} customers</span></div>${listLimit('customers','orders','pending orders')}${data.customers===null?empty('Customer data is unavailable','Refresh the connection to try again.'):rows.length?`<div class="customer-grid">${rows.map((c,i)=>{const orders=(data.orders||[]).filter(o=>o.customerId===c.id);return `<button class="customer-card" data-customer="${escape(c.id)}"><div class="customer-card-top">${avatar(c.name,i)}${icon('arrow')}</div><h2>${escape(c.name)}</h2><p>${escape(c.group)} · ${escape(c.territory || 'Location unavailable')}</p><div class="customer-card-stats"><div><strong>${data.orders===null?'—':orders.length}</strong><span>Loaded orders</span></div><div><strong>${data.orders===null?'—':money(sumSales(orders))}</strong><span>Booked sales</span></div></div></button>`;}).join('')}</div>`:empty('No customers found','Try another name or location.')}</section>`;
}

function agentsView() {
  const ops=data.operations;
  const service=data.mode==='live'?`<section class="card service-card">
    <div class="card-heading"><div><h2>Agent service</h2><p>Current Redis queue state · provider availability is not probed</p>${readStatus('operations')}</div>
    <button class="button" data-action="retry-extras" ${state.extrasBusy?'disabled':''}>${icon('refresh')}${state.extrasBusy?'Reading…':'Refresh status'}</button></div>
    <div class="service-grid">
      <div><span>Redis</span><strong>${escape(ops?.redis||'Unavailable')}</strong></div>
      <div><span>Worker</span><strong>${escape(ops?.worker||'Unknown')}</strong></div>
      <div><span>Queued messages</span><strong>${number(ops?.queuedMessages)}</strong></div>
      <div><span>Queued notices</span><strong>${number(ops?.queuedNotices)}</strong></div>
      <div><span>Failed replies / notices</span><strong>${number(ops?.failedReplies)} / ${number(ops?.failedNotices)}</strong></div>
    </div>
    ${state.extrasError?`<div class="notice error-notice">${escape(state.extrasError)} Please retry.</div>`:''}
  </section>`:'';
  return `${service}<div class="agent-grid">${data.agents.map(a=>`
    <section class="card agent-card"><div class="agent-card-top"><span class="agent-avatar ${a.id}">${icon(a.id==='sales'?'bolt':'shield')}</span><span class="subtle-pill">${escape(a.status)}</span></div>
    <h2>${escape(a.name)}</h2><p>${escape(a.role)}</p>
    <div class="model-line"><span>Model</span><strong>${escape(a.model)}</strong></div>
    <ul class="capabilities">${(a.id==='sales'?['Answers product and stock questions','Finds or registers customers','Creates order drafts for policy evaluation']:['Reads sales, stock, and customer reports','Prepares actions requested by the manager','Helps the owner review orders and limits']).map(t=>`<li>${icon('check')}${t}</li>`).join('')}</ul>
    <div class="agent-boundary">${icon('shield')}${a.id==='sales'?'Customer-scoped access · draft-only writes':'Management-scoped access · approved actions only'}</div></section>`).join('')}</div>
    <section class="card policy-card"><div class="card-heading"><div><h2>Automation controls</h2><p>Saved limits in their original units. The policy checks every order.</p>${state.extrasLoadedAt?`<span class="read-status">Last read · ${escape(prettyMoment(state.extrasLoadedAt))}</span>`:''}</div><span class="subtle-pill">${data.mode==='demo'?'Example settings':'Current agent settings'}</span></div>
    ${data.policies?`<div class="policy-grid">${data.policies.map(p=>`<div class="${p.valid===false?'invalid-policy':''}"><span>${escape(p.name)}</span><strong>${escape(p.value)}${p.unit?` <small>${escape(p.unit)}</small>`:''}</strong><p>${escape(p.note)}</p>${p.source?`<small>Source: ${escape(p.source)}</small>`:''}</div>`).join('')}</div>`:`<div class="policy-explanation"><p>${state.extrasBusy?'Reading current limits from the agent…':'Current limits are unavailable. The manager can still check them through the authorized WhatsApp workflow.'}</p></div>`}
    <div class="policy-footer">${icon('shield')}<span>Changing a limit still takes the four-digit code the agent sends you on WhatsApp. Settings can be proposed from the Settings screen; nothing here applies one.</span></div></section>`;
}
function settingRow(item) {
  return `<div class="setting-row">
    <div class="setting-what"><strong>${escape(item.name)}</strong><small>${escape(item.meaning)}</small></div>
    <div class="setting-value"><span>${escape(item.display||'—')}</span><small>${escape(item.source)}</small></div>
    <div class="setting-do">${data.mode==='live'?`<button class="button" data-setting="${escape(item.id)}">Change</button>`:''}</div>
    ${item.problem?`<p class="setting-problem">${icon('info')}${escape(item.problem)}</p>`:''}
  </div>`;
}
function settingsList() {
  const read=state.reads.settings, report=data.settings;
  if(data.mode==='disconnected')return '';
  if(read.busy&&!report)return `<section class="card"><div class="card-heading"><h2>Business settings</h2></div><p class="policy-explanation">Reading your settings…</p></section>`;
  if(!report)return `<section class="card"><div class="card-heading"><h2>Business settings</h2></div>${empty('Settings are unavailable',read.error||'Refresh the connection to try again.')}<button class="button" data-read="settings">Try again</button></section>`;
  if(report.problem)return `<section class="card"><div class="card-heading"><h2>Business settings</h2></div><div class="notice error-notice">${escape(report.problem)}</div></section>`;
  // EL CAMBIO QUE ESPERA VA ARRIBA DE TODO, y no es decoración: sólo hay UNA
  // propuesta viva por teléfono, así que pedir un segundo cambio pisa el
  // primero. Verlo es lo que evita que eso pase sin que nadie se entere.
  const esperando=report.pending?`<div class="notice pending-notice">${icon('clock')}<span><strong>${escape(report.pending.name)}</strong> is waiting for your four-digit code on WhatsApp: ${escape(report.pending.from||'—')} → ${escape(report.pending.to||'—')}. Reply there to apply it, or propose another change to replace it.</span></div>`:'';
  return `${esperando}${report.groups.map(group=>`<section class="card settings-group"><div class="card-heading"><div><h2>${escape(group.name)}</h2></div><span class="subtle-pill">${group.settings.length} settings</span></div>${group.settings.map(settingRow).join('')}</section>`).join('')}`;
}
function settingsView() {
  return `${settingsList()}<div class="settings-grid"><section class="card connection-card"><span class="stat-icon violet">${icon('link')}</span><h2>${data.mode==='demo'?'Connect your business':'Your CRM connection'}</h2><p>${data.mode==='demo'?'Explore sample orders now, or connect to your deployed Plus Agent for a live view of ERPNext.':'This workspace reads orders, customers, and inventory from your agent service.'}</p><dl><div><dt>Workspace</dt><dd>${escape(data.company)}</dd></div><div><dt>Data source</dt><dd>${data.mode==='demo'?'Sample dataset':'ERPNext via Plus Agent'}</dd></div><div><dt>Access</dt><dd>Read, confirm orders, propose settings</dd></div><div><dt>Connection</dt><dd>${data.mode==='demo'?'Not connected':state.stale?'Interrupted':'Connected'}</dd></div></dl><div class="connection-buttons"><button class="button primary" data-action="connect">${icon('link')}${data.mode==='demo'?'Connect live data':'Change connection'}</button>${data.mode==='live'?'<button class="button" data-action="disconnect">Disconnect</button>':''}</div></section><section class="card setting-notes"><h2>Designed around your workflow</h2><div>${icon('orders')}<section><h3>ERPNext is the source of truth</h3><p>The dashboard reads recent orders, all-date pending orders, and up to 250 records per section. Loaded totals are labeled when a limit is reached.</p></section></div><div>${icon('shield')}<section><h3>Approvals stay protected</h3><p>You can confirm an order here, and propose a settings change. A settings change is never applied from this screen: the agent texts you a four-digit code, and you reply to it on WhatsApp. That second step stays on another device on purpose.</p></section></div><div>${icon('link')}<section><h3>A connection for this session</h3><p>Your access token stays in memory. Reloading the page signs you out. While you are signed in, visible dashboards refresh every minute.</p></section></div></section></div>`;
}
const views={today:todayView,queue:queueView,overview,sales:salesView,advice:adviceView,orders:ordersView,inventory:inventoryView,customers:customersView,agents:agentsView,settings:settingsView};
function render() {
  const focused=document.activeElement, preserve=focused?.id==='search', cursor=preserve?focused.selectionStart:null;
  document.documentElement.dataset.theme=state.theme;
  $('#app').innerHTML=shell();
  if(preserve&&$('#search')){const input=$('#search');input.focus();try{input.setSelectionRange(cursor,cursor);}catch{}}
}
function detail(title,content) {
  const dialog=$('#detail-dialog');
  dialog.innerHTML=`<div class="detail-head"><span>WORKSPACE DETAILS</span><button class="icon-button" data-close="detail-dialog" aria-label="Close details">${icon('close')}</button></div><div class="detail-body"><h2 id="detail-title">${escape(title)}</h2>${content}</div>`;
  if(!dialog.open)dialog.showModal();
}
function renderOrder(o) {
  detail(o.id,`<div class="detail-status">${badge(o.status)}<span>${escape(o.channel)}</span></div><div class="detail-customer">${avatar(o.customer)}<div><strong>${escape(o.customer)}</strong><span>Customer</span></div></div><dl class="detail-fields"><div><dt>Order date</dt><dd>${prettyDate(o.date,{year:'numeric'})}</dd></div><div><dt>Delivery date</dt><dd>${o.deliveryDate?prettyDate(o.deliveryDate):'Not set'}</dd></div><div><dt>ERPNext status</dt><dd>${escape(o.erpStatus)}</dd></div><div><dt>Original order total</dt><dd>${formatMoney(o.total,o.currency)}</dd></div></dl>${o.items?`<h3>Order items</h3><div class="line-items">${o.items.map(item=>`<div><span><strong>${escape(item.name)}</strong><small>${number(item.qty)} ${escape(item.unit||'')} × ${moneyFor(item.rate,o.currency)}</small></span><b>${moneyFor(item.amount??(item.qty!=null&&item.rate!=null?item.qty*item.rate:null),o.currency)}</b></div>`).join('')}</div>`:'<p class="detail-note">Open this order in ERPNext to view its line items and delivery address.</p>'}${o.address?`<h3>Delivery address</h3><p class="detail-note">${escape(o.address)}</p>`:''}<div class="order-total"><span>Order total</span><strong>${moneyFor(o.total,o.currency)}</strong></div><div class="detail-callout">${icon('shield')}<p>${o.status==='pending'?'This order is waiting for review. Use the authorized manager’s WhatsApp thread to review or confirm it.':'Order actions remain in the existing ERPNext and manager workflow.'}</p></div>${safeErpUrl(o.erpUrl)?`<a class="button primary full" href="${escape(o.erpUrl)}" target="_blank" rel="noopener noreferrer">Open in ERPNext ${icon('arrow')}</a>`:''}<button class="button full" data-copy="${escape(o.id)}">Copy full order ID</button>`);
}
function showProduct(id) {
  state.detailRequest++;
  const p=(data.products||[]).find(p=>p.id===id);if(!p)return;
  detail(p.name,`<p class="muted">${escape(p.id)}</p><div class="product-detail-number"><strong>${number(p.available)}</strong><span>${escape(p.unit)} ERP available</span></div><dl class="detail-fields"><div><dt>Physical stock</dt><dd>${number(p.stock)}</dd></div><div><dt>Submitted reservations</dt><dd>${number(p.reserved)}</dd></div><div><dt>Warehouse</dt><dd>${escape(p.warehouse)}</dd></div></dl><div class="detail-callout">${icon('info')}<p>These are ERPNext warehouse quantities. Draft reservations, stock freshness, and your safety buffer are evaluated separately by the order policy.</p></div>`);
}
function renderCustomer(c,conversation) {
  const orders=allOrders().filter(o=>o.customerId===c.id),unavailable=data.orders===null;
  detail(c.name,`<p class="muted">${escape([c.group,c.territory].filter(Boolean).join(' · '))}</p><dl class="detail-fields"><div><dt>Customer ID</dt><dd>${escape(c.id)}</dd></div><div><dt>Loaded orders</dt><dd>${escape(number(unavailable?null:orders.length))}</dd></div><div><dt>Booked sales</dt><dd>${escape(unavailable?'—':money(sumSales(orders)))}</dd></div></dl><h3>Recent orders</h3>${listLimit('orders','pending orders','customers')}<p class="list-note">Orders from the loaded snapshot and open drafts. This is not a complete order history.</p><div class="customer-order-list">${orders.length?orders.slice(0,10).map(o=>`<button data-order="${escape(o.id)}"><span><strong>${escape(o.id)}</strong><small>${escape(prettyDate(o.date))}</small></span><span>${escape(moneyFor(o.total,o.currency))}${icon('arrow')}</span></button>`).join(''):unavailable?empty('Order data is unavailable','Refresh to try again.'):empty('No loaded orders','No orders for this customer are in the current snapshot.')}</div>${orders.length>10?`<p class="list-notice">Showing 10 of ${escape(number(orders.length))} loaded orders for this customer.</p>`:''}<section class="customer-conversation" aria-labelledby="conversation-title"><h3 id="conversation-title">WhatsApp conversation</h3><div class="conversation-privacy">${icon('shield')}<p>Conversations are opened through a customer in your company’s workspace. There is no public inbox or phone directory.</p></div>${conversation}</section>`);
}
function conversationView(value) {
  const retention=`<p class="retention-note">Messages are retained for ${escape(number(value.retentionDays))} days. Older conversations expire; this is not a permanent archive.</p>`;
  if(!value.reachable)return `${empty('No WhatsApp number on file','No WhatsApp number on file for this customer, so there is no conversation to show.')}${retention}`;
  return `${retention}${value.truncated?'<p class="list-notice">Earlier messages were omitted. Only the most recent part of this conversation is shown.</p>':''}${value.messages.length?`<p class="list-note">Oldest first. ${escape(timeZoneNote())}</p><ol class="transcript">${value.messages.map(message=>`<li class="message message-${escape(message.role)}"><div class="message-meta"><strong>${escape({customer:'Customer',agent:'Agent',note:'Activity note'}[message.role])}</strong><time datetime="${escape(message.at)}">${escape(prettyMoment(message.at))}</time></div><p>${escape(message.text)}</p></li>`).join('')}</ol>`:empty('No retained messages','There are no messages within this customer’s retention window.')}`;
}
async function showCustomer(id) {
  const activity=[...(data.activity?.conversations||[]),...(data.activity?.newCustomers||[])].find(c=>c.customerId===id);
  const ranked=data.sales?.topCustomers?.find(c=>c.id===id),advised=data.advice?.items.find(item=>item.customerId===id);
  const c=(data.customers||[]).find(c=>c.id===id)||(activity?{id:activity.customerId,name:activity.customerName}:ranked?{id:ranked.id,name:ranked.name}:advised?{id,name:id}:null);
  if(!c)return;
  const connection=state.connection,session=state.session,request=++state.detailRequest;
  if(data.mode==='demo'){renderCustomer(c,conversationView(validateConversation(data.conversations[id],id)));return;}
  if(data.mode!=='live')return;
  renderCustomer(c,empty('Loading conversation…','Reading retained messages for this customer.'));
  try {
    const value=validateConversation(await apiRead(connection,'/customers/'+encodeURIComponent(id)+'/conversation'),id);
    if(state.session!==session||state.detailRequest!==request||!$('#detail-dialog').open)return;
    renderCustomer(c.name===c.id?{...c,name:value.customerName}:c,conversationView(value));
  }catch(error){
    if(state.session!==session)return;
    if(error.status===401){cerrarSesion('Your dashboard access is no longer valid. Sign in again.');return;}
    if(state.detailRequest!==request||!$('#detail-dialog').open)return;
    renderCustomer(c,`${empty('Conversation is unavailable',readError(error))}<button class="button" data-customer="${escape(id)}">Try again</button>`);
  }
}
function openConnection() {
  state.connectRequest++;
  const dialog=$('#connection-dialog');
  dialog.innerHTML=`<div class="detail-head"><span>LIVE WORKSPACE</span><button class="icon-button" data-close="connection-dialog" aria-label="Close connection">${icon('close')}</button></div><form id="connect-form" class="connection-form"><span class="stat-icon violet">${icon('link')}</span><h2 id="connection-title">Connect to Plus Agent</h2><p>Use the address of your deployed agent service and its dashboard access token.</p><label ${repoHosted?'hidden':''}>Agent service URL<input name="url" type="url" required placeholder="https://agent.your-business.com" value="${escape(state.connection?.base || (repoHosted?location.origin:''))}" autocomplete="url"></label><label>Dashboard access token<input name="token" type="password" required minlength="32" autocomplete="off" placeholder="Enter your dashboard token"></label><p class="field-note">This is a dedicated dashboard token, not your ERPNext, WhatsApp, or model API key. It is kept only for this session.</p><div id="connection-error" class="form-error" role="alert"></div><button class="button primary full" type="submit">Connect workspace ${icon('arrow')}</button><p class="field-note">The dashboard API must be enabled on your agent service. A remote service must allow this dashboard’s origin.</p></form>`;
  dialog.showModal();
}
function openSetting(id) {
  const item=(data.settings?.groups||[]).flatMap(g=>g.settings).find(s=>s.id===id);
  if(!item)return;
  const dialog=$('#setting-dialog');
  // El ajuste viaja en un campo oculto y no en `state`: el formulario vive en
  // un <dialog>, que `render()` no toca, así que no hay nada que preservar ni
  // que limpiar en `goto`, en `cerrarSesion` ni en el botón de demo. Un estado
  // de pantalla que sobrevive a la navegación reaparece donde no va.
  dialog.innerHTML=`<div class="detail-head"><span>SETTING</span><button class="icon-button" data-close="setting-dialog" aria-label="Close setting">${icon('close')}</button></div><form id="setting-form" class="connection-form"><h2 id="setting-title">${escape(item.name)}</h2><p>${escape(item.meaning)}</p><input type="hidden" name="setting" value="${escape(item.id)}"><label>New value<input name="value" required maxlength="600" autocomplete="off" value="${escape(item.value)}"></label><p class="field-note">Now: ${escape(item.display||'—')}${item.unit?` · ${escape(item.unit)}`:''} · ${escape(item.source)}</p><div id="setting-error" class="form-error" role="alert"></div><button class="button primary full" type="submit">Send me the code ${icon('arrow')}</button><p class="field-note">Nothing changes yet. The agent sends a four-digit code to your WhatsApp and the change applies when you reply there — on purpose, so the second step is not this same screen.</p></form>`;
  dialog.showModal();
}
function openPrice(id) {
  const report=data.prices;
  if(!report?.canChange)return;
  const row=(report.items||[]).find(p=>p.id===id);
  const producto=(data.products||[]).find(p=>p.id===id);
  const dialog=$('#price-dialog');
  // Mismo patrón que `openSetting`: el producto viaja en un campo oculto y el
  // formulario vive en un <dialog>, que `render()` no toca.
  dialog.innerHTML=`<div class="detail-head"><span>LIST PRICE</span><button class="icon-button" data-close="price-dialog" aria-label="Close price">${icon('close')}</button></div><form id="price-form" class="connection-form"><h2 id="price-title">${escape(producto?.name||id)}</h2><p>${escape(id)}${row?` · per ${escape(row.unit)}`:''}</p><input type="hidden" name="product" value="${escape(id)}"><label>New list price<input name="value" required inputmode="decimal" autocomplete="off" value="${escape(row&&row.price!==null?String(row.price):'')}"></label><p class="field-note">Now: ${escape(row&&row.price!==null?salesMoney(row.price,report.currency):'—')} · ${escape(report.priceList||'')} · ${escape(report.currency||'')}</p><div id="price-error" class="form-error" role="alert"></div><button class="button primary full" type="submit">Change the price ${icon('arrow')}</button><p class="field-note">This applies right away — there is no confirmation code for a price. It can move up to ${escape(String(report.bandPct))}% per day for this product, and only once a day.</p></form>`;
  dialog.showModal();
}
function validateSnapshot(value) {
  if(!value||value.mode!=='live'||typeof value.company!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value.today)||Number.isNaN(Date.parse(value.generatedAt)))throw new Error('The service returned an invalid dashboard response.');
  for(const key of ['orders','pendingOrders','customers','products']) if(value[key]!==null&&(!Array.isArray(value[key])||value[key].length>250))throw new Error('The service returned invalid records.');
  if(!Array.isArray(value.agents)||!Array.isArray(value.errors)||!Array.isArray(value.truncated))throw new Error('The service returned incomplete dashboard information.');
  for(const o of [...(value.orders||[]),...(value.pendingOrders||[])])if(typeof o.id!=='string'||typeof o.customer!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(o.date))throw new Error('The service returned an invalid order.');
  if(value.currency){try{new Intl.NumberFormat('en',{style:'currency',currency:value.currency});}catch{throw new Error('The service returned an invalid currency.');}}
  if(!value.orders&&!value.customers&&!value.products)throw new Error('The agent is reachable but its ERPNext data is unavailable. Check the manager connection.');
  value.policies=null;value.operations=null;value.activity=null;value.queue=null;value.conversations=null;value.sales=null;value.advice=null;return value;
}
async function fetchData(connection) {
  const response=await fetch(connection.base+'/api/dashboard/snapshot',{headers:{Authorization:'Bearer '+connection.token},cache:'no-store',credentials:'omit',redirect:'error',signal:AbortSignal.timeout(45000)});
  if(!response.ok){
    // El STATUS viaja con el error. Sin esto el que refresca no puede
    // distinguir «no se pudo leer ahora» de «este token ya no sirve», y las
    // dos cosas terminaban igual: los datos del CRM en pantalla.
    const error=new Error(({401:'The dashboard token was not accepted.',403:'This dashboard’s origin is not allowed by the service.',404:'The dashboard API is not installed at this address.',503:'Enable dashboard access on your agent service first.'})[response.status] || 'The agent service could not return a snapshot.');
    error.status=response.status;
    throw error;
  }
  return validateSnapshot(await response.json());
}
function cerrarSesion(aviso) {
  // El reseteo de sesión, escrito UNA vez. Lo usan Disconnect y el 401 del
  // refresco: dos salidas con dos copias del reseteo son dos salidas que se
  // desincronizan, y la que se olvide de limpiar `data` deja el CRM visible.
  state.connectRequest++;
  document.querySelectorAll('dialog[open]').forEach(d=>d.close());
  state.connection=null;state.session++;state.busy=false;
  state.detailRequest++;state.fxRequest++;state.fxLoading=false;state.fxError='';
  data=disconnectedData();state.stale=false;
  state.extrasBusy=false;state.extrasError='';state.extrasLoadedAt=null;state.reads=freshReads();
  render();toast(aviso);
}
async function refresh(silent=false) {
  if(!state.connection||state.busy)return;
  const connection=state.connection,session=state.session;
  state.busy=true;if(!silent)render();
  try{
    const snapshot=await fetchData(connection);
    if(state.session!==session)return;
    data={...snapshot,activity:data.activity,queue:data.queue,operations:data.operations,policies:data.policies,sales:data.sales,advice:data.advice};state.stale=false;if(state.displayCurrency&&Date.now()-(state.fx?.fetchedAt||0)>=3600000)setDisplayCurrency(state.displayCurrency);if(!silent)toast('Dashboard refreshed.');
    if(!silent)await loadViewReads(true);
  }catch(e){
    if(state.session===session){
      // Un 401 NO es un fallo pasajero: el token dejó de servir. Guardar la
      // foto anterior y marcarla «vieja» dejaba pedidos, clientes, inventario
      // y datos de los agentes a la vista hasta que alguien recargara a mano.
      // Se cierra la sesión con el MISMO reseteo que Disconnect, para que no
      // haya dos formas de salir que se puedan desincronizar.
      if(e&&e.status===401){cerrarSesion('Your dashboard access is no longer valid. Sign in again.');return;}
      state.stale=true;if(!silent)toast(e.message||'Could not refresh the dashboard.');
    }
  }
  finally{if(state.session===session){state.busy=false;render();}}
}
function exportOrders() {
  if(state.filter==='pending'?pendingOrders()===null:data.orders===null){toast('Order data is unavailable. Refresh before exporting.');return;}
  const cell=(v)=>{const s=String(v??'');return '"'+(/^[=+\-@\t\r]/.test(s)?"'"+s:s).replaceAll('"','""')+'"';};
  const lines=[['Order ID','Customer','Date','Status','Original amount','Original currency','Display amount','Display currency','Rate date','Conversion status'],...selectedOrders().map(o=>{const shown=displayAmount(o.total,o.currency);return [o.id,o.customer,o.date,labels[o.status],o.total,o.currency,shown.value,shown.currency,shown.converted?state.fx.updatedAt.slice(0,10):'',shown.unavailable?'Rate unavailable: original amount':shown.converted?'Display estimate':'Original amount'];})];
  const blob=new Blob(['\ufeff'+lines.map(row=>row.map(cell).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'});
  const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`plus-${data.mode}-orders-${data.today}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('Filtered orders exported.');
}
document.addEventListener('click',async e=>{
  const target=e.target.closest('button,a');if(!target)return;
  if(target.dataset.view){e.preventDefault();await goto(target.dataset.view);}
  if(target.dataset.close)$('#'+target.dataset.close).close();
  if(target.dataset.order)showOrder(target.dataset.order);
  if(target.dataset.product)showProduct(target.dataset.product);
  if(target.dataset.customer)showCustomer(target.dataset.customer);
  if(target.dataset.setting)openSetting(target.dataset.setting);
  if(target.dataset.price)openPrice(target.dataset.price);
  if(target.dataset.copy){try{await navigator.clipboard.writeText(target.dataset.copy);toast('Order ID copied.');}catch{toast('Clipboard unavailable. You can select and copy the order ID above.');}}
  if(target.dataset.filter){state.filter=target.dataset.filter;state.page=1;render();}
  if(target.dataset.salesDate){const report=currentSales(),row=report?.daily?.find(day=>day.date===target.dataset.salesDate);if(row){state.detailRequest++;detail(prettyDate(row.date,{year:'numeric'}),`<dl class="detail-fields"><div><dt>Sales</dt><dd>${escape(salesMoney(row.total,report.currency))}</dd></div><div><dt>Orders</dt><dd>${number(row.orders)}</dd></div></dl>`);}}
  if(target.dataset.chartDate){const d=target.dataset.chartDate,orders=periodOrders().filter(o=>o.date===d);$('#chart-detail').textContent=`${prettyDate(d)} · ${orders.length} orders · ${money(sumSales(orders))} booked sales`;document.querySelectorAll('.bar').forEach(b=>b.classList.toggle('inspected',b===target));}
  const action=target.dataset.action;
  if(action==='connect')openConnection();
  if(action==='theme'){state.theme=state.theme==='dark'?'light':'dark';try{localStorage.setItem('plus.dashboard.theme',state.theme);}catch{}render();}
  if(action==='demo'){state.connectRequest++;state.detailRequest++;data=makeDemo(state.range);state.session++;state.connection=null;state.stale=false;state.busy=false;state.extrasBusy=false;state.extrasError='';state.extrasLoadedAt=null;state.reads=freshReads();render();restoreDisplayCurrency();}
  if(action==='retry-currency')setDisplayCurrency(state.fxFailedTarget||state.displayCurrency);
  if(action==='retry-extras')loadExtras(true);
  if(target.dataset.read)await loadRead(target.dataset.read,true);
  if(action==='refresh')refresh();
  if(action==='menu'){state.menu=!state.menu;render();}
  if(action==='close-menu'){state.menu=false;render();}
  if(action==='pending'){goto('orders');state.filter='pending';state.range=30;render();}
  if(action==='export')exportOrders();
  if(action==='prev'||action==='next'){state.page+=action==='next'?1:-1;render();}
  if(action==='disconnect'){cerrarSesion('Signed out of your CRM.');}
});
document.addEventListener('input',e=>{
  if(e.target.id==='search'){
    const cursor=e.target.selectionStart;state.search=e.target.value;state.page=1;render();
    const input=$('#search');input.focus();try{input.setSelectionRange(cursor,cursor);}catch{}
  }
});
document.addEventListener('change',async e=>{
  if(e.target.id==='display-currency')await setDisplayCurrency(e.target.value);
  if(e.target.id==='range'&&['7','30'].includes(e.target.value)){state.range=Number(e.target.value);state.page=1;if(data.mode==='demo')data.sales=makeDemo(state.range).sales;render();if(state.view==='sales')await loadRead('sales');}
  if(e.target.id==='stock-filter'){state.stockFilter=e.target.value;render();}
});
document.addEventListener('submit',async e=>{
  if(e.target.id==='setting-form'){
    e.preventDefault();
    const form=e.target,button=$('button[type=submit]',form),error=$('#setting-error');
    const fields=new FormData(form),connection=state.connection,session=state.session;
    if(!connection)return;
    try{
      button.disabled=true;button.textContent='Preparing…';error.textContent='';
      const answer=await apiWrite(connection,'/settings/propose',{
        setting:String(fields.get('setting')),value:String(fields.get('value')),
      });
      if(state.session!==session)return;
      // 200 CON `ok:false` ES EL CASO NORMAL de un valor que no sirve: el
      // servidor contesta la prosa que explica por qué y no cambia nada.
      // Tratar todo 200 como éxito cerraba el diálogo diciendo que salió bien.
      if(!answer?.ok){error.textContent=String(answer?.detail||'That change could not be prepared.');button.disabled=false;button.textContent='Send me the code';return;}
      $('#setting-dialog').close();
      toast(String(answer.detail||'Check WhatsApp for your four-digit code.'));
      await loadRead('settings',true);
    }catch(ex){
      if(state.session!==session)return;
      if(ex.status===401){cerrarSesion('Your dashboard access is no longer valid. Sign in again.');return;}
      error.textContent=ex.name==='TimeoutError'?'The agent took too long to respond. Try again.':ex.message;
      button.disabled=false;button.textContent='Send me the code';
    }
    return;
  }
  if(e.target.id==='price-form'){
    e.preventDefault();
    const form=e.target,button=$('button[type=submit]',form),error=$('#price-error');
    const fields=new FormData(form),connection=state.connection,session=state.session;
    if(!connection)return;
    const producto=String(fields.get('product'));
    try{
      button.disabled=true;button.textContent='Changing…';error.textContent='';
      const answer=await apiWrite(connection,'/products/'+encodeURIComponent(producto)+'/price',{value:String(fields.get('value'))});
      if(state.session!==session)return;
      // Igual que en los ajustes: 200 con `ok:false` es el caso normal —fuera
      // de banda, ya se cambió hoy, sin precio anterior contra el que medir—.
      if(!answer?.ok){error.textContent=String(answer?.detail||'That price could not be changed.');button.disabled=false;button.textContent='Change the price';return;}
      $('#price-dialog').close();
      toast(String(answer.detail||'The list price was changed.'));
      await loadRead('prices',true);
    }catch(ex){
      if(state.session!==session)return;
      if(ex.status===401){cerrarSesion('Your dashboard access is no longer valid. Sign in again.');return;}
      error.textContent=ex.name==='TimeoutError'?'The agent took too long to respond. Try again.':ex.message;
      button.disabled=false;button.textContent='Change the price';
    }
    return;
  }
  if(e.target.id!=='connect-form')return;e.preventDefault();
  const form=e.target,button=$('button[type=submit]',form),error=$('#connection-error'),fields=new FormData(form),attempt=++state.connectRequest;
  try{
    const url=new URL(String(fields.get('url'))),local=['localhost','127.0.0.1','[::1]'].includes(url.hostname);
    if(url.username||url.password||url.search||url.hash||url.pathname!=='/'||!(url.protocol==='https:'||local&&url.protocol==='http:'))throw new Error('Enter an HTTPS service origin without a path. HTTP is allowed only for local development.');
    const token=String(fields.get('token')).trim();if(token.length<32)throw new Error('The dashboard token must contain at least 32 characters.');
    button.disabled=true;button.textContent='Connecting…';error.textContent='';
    const connection={base:url.origin,token},snapshot=await fetchData(connection);
    if(attempt!==state.connectRequest||!$('#connection-dialog').open)return;
    data=snapshot;state.connection=connection;state.session++;state.detailRequest++;state.busy=false;state.stale=false;state.page=1;state.extrasError='';state.extrasBusy=false;state.extrasLoadedAt=null;state.reads=freshReads();$('#detail-dialog').close();$('#connection-dialog').close();form.reset();render();restoreDisplayCurrency();toast('Connected to your live CRM.');loadViewReads();
  }catch(ex){if(attempt!==state.connectRequest||!$('#connection-dialog').open)return;error.textContent=ex.name==='TimeoutError'?'The service took too long to respond. Try again.':ex.message==='Failed to fetch'?'Could not reach the service. Check its address, HTTPS, and allowed dashboard origin.':ex.message;button.disabled=false;button.textContent='Connect workspace';}
});
document.querySelectorAll('dialog').forEach(dialog=>{dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}});dialog.addEventListener('close',()=>{if(dialog.id==='connection-dialog'){state.connectRequest++;dialog.innerHTML='';}if(dialog.id==='detail-dialog'){state.detailRequest++;dialog.innerHTML='';}if(dialog.id==='setting-dialog')dialog.innerHTML='';if(dialog.id==='price-dialog')dialog.innerHTML='';});});
window.addEventListener('hashchange',()=>{const view=location.hash.slice(1);if(Object.hasOwn(views,view))goto(view);});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&state.menu){state.menu=false;render();}});
if(Object.hasOwn(views,location.hash.slice(1)))state.view=location.hash.slice(1);
render();
revealLogo();
if(data.mode!=='disconnected')restoreDisplayCurrency();

function connectionGate() {
  return `<section class="card live-gate">
    <span class="stat-icon violet">${icon('link')}</span>
    <h2>Open your live CRM workspace</h2>
    <p>${repoHosted?'This dashboard runs inside your Plus Agent. Sign in to read your real ERPNext orders, stock, and customers.':'Connect to your deployed Plus Agent to see your real ERPNext orders, stock, and customers.'}</p>
    ${state.configured===false?'<div class="notice">Dashboard access has not been enabled on this agent yet. Complete the one-time setup, then sign in.</div>':''}
    <button class="button primary" data-action="connect">${icon('link')}${repoHosted?'Sign in to this agent':'Connect to your agent'}</button>
    <div class="gate-links"><a href="https://github.com/ravi3594444/agent-crm/blob/feat/plus-operations-dashboard/DASHBOARD.md" target="_blank" rel="noopener noreferrer">Setup instructions</a><button class="text-link" data-action="demo">Explore sample data</button></div>
    <div class="gate-note">${icon('shield')}No business records are shown until you sign in.</div>
  </section>`;
}

function safeErpUrl(value) {
  if(!value)return '';
  try {
    const url=new URL(value),local=['localhost','127.0.0.1','[::1]'].includes(url.hostname);
    if(url.username||url.password||!(url.protocol==='https:'||local&&url.protocol==='http:'))return '';
    return url.href;
  }catch{return '';}
}

function readCurrencyPreference() {
  try {
    const value=localStorage.getItem('plus.dashboard.displayCurrency')||'';
    return /^[A-Z]{3}$/.test(value)?value:'';
  }catch{return '';}
}

function rememberCurrency(value) {
  state.currencyPreference=value;
  try {
    if(value)localStorage.setItem('plus.dashboard.displayCurrency',value);
    else localStorage.removeItem('plus.dashboard.displayCurrency');
  }catch{}
}

function currencySelector() {
  if(data.mode==='disconnected')return '';
  const choices=[...new Set([data.currency,...Object.keys(currencyNames),state.currencyPreference])].filter(Boolean);
  return `<label class="select-wrap currency-control"><span>Currency</span><select id="display-currency" aria-label="Display currency" ${state.fxLoading?'disabled':''}>
    <option value="" ${!state.displayCurrency?'selected':''}>Original${data.currency?' · '+escape(data.currency):''}</option>
    ${choices.map(code=>`<option value="${escape(code)}" ${state.displayCurrency===code?'selected':''}>${escape(code)}${currencyNames[code]?' · '+escape(currencyNames[code]):''}</option>`).join('')}
  </select></label>`;
}

function currencyNotice() {
  if(data.mode==='disconnected')return '';
  if(state.fxLoading)return '<div class="currency-note" role="status">Loading exchange rates…</div>';
  const error=state.fxError?`<div class="currency-note currency-error" role="alert">${escape(state.fxError)} <button class="text-link" data-action="retry-currency">Try again</button></div>`:'';
  if(!state.displayCurrency||!state.fx)return error;
  return `${error}<div class="currency-note" role="status"><span>Display estimates in <strong>${escape(state.displayCurrency)}</strong> · Rates dated ${prettyDate(state.fx.updatedAt.slice(0,10),{year:'numeric'})}. Original amounts are in order details.</span><a href="https://www.exchangerate-api.com" target="_blank" rel="noopener noreferrer">Rates By Exchange Rate API</a></div>`;
}

function formatMoney(value,currency,compact=false) {
  if(value==null||!Number.isFinite(Number(value))||!currency)return '—';
  try {
    return new Intl.NumberFormat('en-GB',{
      style:'currency',currency,currencyDisplay:'code',
      ...(compact?{notation:'compact',maximumFractionDigits:1,minimumFractionDigits:0}:{}),
    }).format(value);
  }catch{return '—';}
}

function displayAmount(value,currency) {
  const original={value:value==null||!Number.isFinite(Number(value))?null:Number(value),currency,converted:false,unavailable:false};
  if(original.value===null||!state.displayCurrency||currency===state.displayCurrency)return original;
  const rate=state.fx?.target===state.displayCurrency?state.fx.rates[currency]:null;
  // Provider rates are units of the source currency per one target unit.
  const converted=original.value/rate;
  if(!(rate>0)||!Number.isFinite(converted))return {...original,unavailable:true};
  return {value:converted,currency:state.displayCurrency,converted:true,unavailable:false};
}

function validateRates(value,target) {
  const updated=Number(value?.time_last_update_unix)*1000;
  if(value?.result!=='success'||value.base_code!==target||!value.rates||Array.isArray(value.rates)
    ||!Number.isFinite(updated)||updated<=0||updated>Date.now()+300000||Date.now()-updated>7*86400000)
    throw new Error('The exchange service returned invalid or outdated rates.');
  const rates=Object.fromEntries(Object.entries(value.rates).filter(([code,rate])=>/^[A-Z]{3}$/.test(code)&&typeof rate==='number'&&Number.isFinite(rate)&&rate>0));
  if(rates[target]!==1)throw new Error('The exchange service returned an invalid base rate.');
  return {target,rates,updatedAt:new Date(updated).toISOString(),fetchedAt:Date.now()};
}

async function setDisplayCurrency(target) {
  if(target&&!/^[A-Z]{3}$/.test(target))return;
  const request=++state.fxRequest,session=state.session;
  state.fxError='';state.fxFailedTarget='';
  if(!target){state.displayCurrency='';state.fx=null;state.fxLoading=false;rememberCurrency('');render();return;}
  if(data.mode==='disconnected'){rememberCurrency(target);return;}
  state.fxLoading=true;render();
  try {
    let rates=fxCache.get(target);
    if(!rates||Date.now()-rates.fetchedAt>=3600000){
      // This public request contains only a currency code, never CRM data or tokens.
      const response=await fetch('https://open.er-api.com/v6/latest/'+target,{
        credentials:'omit',redirect:'error',signal:AbortSignal.timeout(12000),
      });
      if(!response.ok)throw new Error(response.status===429?'The exchange service is busy. Try again later.':'Exchange rates could not be loaded.');
      rates=validateRates(await response.json(),target);
    }
    if(state.session!==session||state.fxRequest!==request)return;
    if(data.currency&&!rates.rates[data.currency])throw new Error('A rate for the company currency is unavailable.');
    fxCache.set(target,rates);state.fx=rates;state.displayCurrency=target;rememberCurrency(target);
  }catch(error){
    if(state.session!==session||state.fxRequest!==request)return;
    state.fxError=(error.name==='TypeError'||error.name==='TimeoutError'?'Exchange rates could not be reached.':error.message)+' The displayed currency has been kept.';
    state.fxFailedTarget=target;
  }finally{
    if(state.session===session&&state.fxRequest===request){state.fxLoading=false;render();}
  }
}

function restoreDisplayCurrency() {
  state.fxRequest++;state.fxLoading=false;state.fxError='';
  if(state.currencyPreference)setDisplayCurrency(state.currencyPreference);
}

async function apiRead(connection, path) {
  const response=await fetch(connection.base+'/api/dashboard'+path,{
    headers:{Authorization:'Bearer '+connection.token},cache:'no-store',credentials:'omit',
    redirect:'error',signal:AbortSignal.timeout(45000),
  });
  if(!response.ok){const error=new Error(({
    401:'Your dashboard token was not accepted. Sign in again.',
    403:'This dashboard origin is not allowed by the agent.',
    404:'The requested record or dashboard endpoint was not found.',
    503:'Enable dashboard access on the agent first.',
  })[response.status]||'The agent could not read this information. Check its ERPNext or Redis connection.');error.status=response.status;throw error;}
  return response.json();
}

// LA PRIMERA ESCRITURA DE ESTA PANTALLA. `apiRead` no sirve: un POST con
// cuerpo necesita `Content-Type`, que NO está en la lista segura de CORS —el
// servidor ya lo permite en `access-control-allow-headers`, y si no lo hiciera
// el navegador rechazaría el preflight y no saldría ningún pedido, sin log en
// ninguna parte («el botón no hace nada»).
//
// El cuerpo de error del servidor SE USA cuando viene: `{"error": ...}` dice
// cuál fue el problema —el ajuste no existe, el cuerpo no es JSON— y taparlo
// con una frase genérica deja al dueño adivinando.
async function apiWrite(connection, path, body) {
  const response=await fetch(connection.base+'/api/dashboard'+path,{
    method:'POST',
    headers:{Authorization:'Bearer '+connection.token,'Content-Type':'application/json'},
    body:JSON.stringify(body),cache:'no-store',credentials:'omit',
    redirect:'error',signal:AbortSignal.timeout(45000),
  });
  let payload=null;
  try{payload=await response.json();}catch{}
  if(!response.ok){
    const error=new Error(payload?.error||({
      401:'Your dashboard token was not accepted. Sign in again.',
      403:'This token can read the dashboard but cannot change settings.',
      404:'This agent does not have the settings endpoint yet. Update the service.',
      503:'Enable dashboard access on the agent first.',
    })[response.status]||'The agent could not prepare this change.');
    error.status=response.status;throw error;
  }
  return payload;
}

// Each read has its own contract. Project only display fields: no phone,
// checkpoint metadata, system messages, tool names or arguments enter the view.
function requireValue(ok,label){if(!ok)throw new Error('The agent returned invalid '+label+'.');}
function isText(value){return typeof value==='string';}
function isId(value){return isText(value)&&value.trim().length>0;}
// Declaración y no `const`: `makeDemo()` corre al cargar el módulo y llama a
// los validadores, así que una `const` de más abajo caería en su zona muerta.
// Un informe de display no trae listas ilimitadas: el servidor ya recorta
// (10 en los rankings, la ventana en `daily`) y avisa con `truncated`. Una
// lista enorme sólo puede venir de algo roto, y ordenarla e interpolarla
// entera congela la pantalla. Declaración, no `const`: ver `isMoney`.
function listaSana(v){return Array.isArray(v)&&v.length<=500;}
function isMoney(v){return Number.isFinite(v)&&Math.abs(v)<=Number.MAX_SAFE_INTEGER;}
function isCount(value){return Number.isSafeInteger(value)&&value>=0;}
function isDay(value){return isText(value)&&/^\d{4}-\d{2}-\d{2}$/.test(value)&&!Number.isNaN(Date.parse(value))&&new Date(value).toISOString().slice(0,10)===value;}
function isMoment(value){return isText(value)&&/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)&&isDay(value.slice(0,10))&&!Number.isNaN(Date.parse(value));}
// Declaración y no `const`: `makeDemo()` corre al cargar el módulo y llama a
// este validador, así que una `const` caería en su zona muerta. Ver `isMoney`.
function validateSetting(s) {
  requireValue(isId(s?.id)&&isText(s.name)&&isText(s.meaning)&&isText(s.unit)&&isText(s.kind)&&typeof s.optional==='boolean'&&isText(s.value)&&isText(s.display)&&isText(s.source)&&typeof s.configured==='boolean'&&isText(s.problem),'a setting');
  return {id:s.id,name:s.name,meaning:s.meaning,unit:s.unit,kind:s.kind,optional:s.optional,value:s.value,display:s.display,source:s.source,configured:s.configured,problem:s.problem};
}
function validateSettings(value) {
  requireValue(value&&listaSana(value.groups)&&isText(value.problem),'settings');
  const groups=value.groups.map(g=>{
    requireValue(isId(g?.id)&&isText(g.name)&&listaSana(g.settings),'a settings group');
    return {id:g.id,name:g.name,settings:g.settings.map(validateSetting)};
  });
  // `pending` es null o el cambio que ESTA persona dejó esperando su código.
  // Nunca trae el código: el servidor usa `limites.pendiente()`, que lo saca.
  let pending=null;
  if(value.pending!=null){
    const p=value.pending;
    requireValue(isId(p.id)&&isText(p.name)&&isText(p.from)&&isText(p.to),'the pending change');
    pending={id:p.id,name:p.name,from:p.from,to:p.to};
  }
  return {groups,pending,problem:value.problem};
}
function validatePrices(value) {
  requireValue(value&&typeof value.canChange==='boolean'&&isMoney(value.bandPct)&&listaSana(value.errors)&&listaSana(value.truncated),'price information');
  requireValue(value.priceList===null||isText(value.priceList),'the price list');
  requireValue(value.currency===null||isText(value.currency),'the price currency');
  // `items` null es «no se pudo leer», que NO es «no hay precios cargados».
  let items=null;
  if(value.items!==null&&value.items!==undefined){
    requireValue(listaSana(value.items),'the price list rows');
    items=value.items.map(row=>{
      requireValue(isId(row?.id)&&isText(row.unit)&&(row.price===null||isMoney(row.price)),'a price');
      return {id:row.id,price:row.price,unit:row.unit};
    });
  }
  return {priceList:value.priceList,currency:value.currency,bandPct:value.bandPct,canChange:value.canChange,items,errors:[...value.errors],truncated:[...value.truncated]};
}
function validateConversation(value,id) {
  requireValue(value?.customerId===id&&isText(value.customerName)&&typeof value.reachable==='boolean'&&typeof value.truncated==='boolean'&&isCount(value.retentionDays)&&Array.isArray(value.messages),'conversation information');
  requireValue(value.reachable||value.messages.length===0,'conversation reachability');
  const messages=value.messages.map(message=>{
    requireValue(message&&['customer','agent','note'].includes(message.role)&&isText(message.text)&&isMoment(message.at),'conversation messages');
    return {role:message.role,text:message.text,at:message.at};
  });
  requireValue(messages.every((message,i)=>!i||Date.parse(message.at)>=Date.parse(messages[i-1].at)),'conversation order');
  return {customerId:value.customerId,customerName:value.customerName,reachable:value.reachable,messages,truncated:value.truncated,retentionDays:value.retentionDays};
}
function validateActivity(value) {
  requireValue(value&&isDay(value.date)&&Array.isArray(value.conversations)&&Array.isArray(value.newCustomers)&&Array.isArray(value.truncated)&&value.truncated.every(isText),'today information');
  const rows=items=>items.map(row=>{
    requireValue(row&&isId(row.customerId)&&isText(row.customerName)&&isCount(row.turns)&&isMoment(row.lastAt)&&isText(row.lastLine)&&(row.orderId===null||isId(row.orderId)),'customer activity');
    return {customerId:row.customerId,customerName:row.customerName,turns:row.turns,lastAt:row.lastAt,lastLine:row.lastLine,orderId:row.orderId};
  }).sort((a,b)=>Date.parse(b.lastAt)-Date.parse(a.lastAt));
  return {date:value.date,conversations:rows(value.conversations),newCustomers:rows(value.newCustomers),truncated:[...value.truncated]};
}
function validateQueue(value) {
  requireValue(value&&Array.isArray(value.upcoming)&&Array.isArray(value.waitingOnAPerson)&&isCount(value.undelivered?.replies)&&isCount(value.undelivered?.notices),'scheduled work');
  const upcoming=value.upcoming.map(row=>{
    requireValue(row&&isId(row.id)&&isText(row.type)&&isId(row.orderId)&&isText(row.customer)&&isMoment(row.dueAt)&&isText(row.what),'scheduled action');
    return {id:row.id,type:row.type,orderId:row.orderId,customer:row.customer,dueAt:row.dueAt,what:row.what};
  }).sort((a,b)=>Date.parse(a.dueAt)-Date.parse(b.dueAt));
  const waitingOnAPerson=value.waitingOnAPerson.map(row=>{
    requireValue(row&&isId(row.orderId)&&isText(row.customer)&&isMoment(row.since)&&isText(row.what),'waiting decision');
    return {orderId:row.orderId,customer:row.customer,since:row.since,what:row.what};
  }).sort((a,b)=>Date.parse(a.since)-Date.parse(b.since));
  return {upcoming,waitingOnAPerson,undelivered:{replies:value.undelivered.replies,notices:value.undelivered.notices}};
}
function validateOperations(value) {
  requireValue(value&&isText(value.redis)&&isText(value.worker)&&['queuedMessages','queuedNotices','failedReplies','failedNotices'].every(key=>value[key]===null||isCount(value[key])),'delivery status');
  return {redis:value.redis,worker:value.worker,queuedMessages:value.queuedMessages,queuedNotices:value.queuedNotices,failedReplies:value.failedReplies,failedNotices:value.failedNotices};
}
function validateSales(value) {
  requireValue(value&&typeof value==='object','sales information');
  requireValue(value.currency===null||isCurrency(value.currency),'sales currency');
  requireValue((value.since===null||isDay(value.since))&&(value.until===null||isDay(value.until))&&(value.since===null||value.until===null||value.since<=value.until),'sales dates');
  requireValue((value.total===null||isMoney(value.total))&&(value.averageOrder===null||isMoney(value.averageOrder))&&(value.orders===null||isCount(value.orders)),'sales totals');
  requireValue(['daily','topProducts','topCustomers','errors','truncated'].every(key=>value[key]===null||listaSana(value[key])),'sales lists');
  const daily=value.daily===null?null:value.daily.map(row=>{
    requireValue(row&&isDay(row.date)&&isMoney(row.total)&&isCount(row.orders),'daily sales');
    requireValue((value.since===null||row.date>=value.since)&&(value.until===null||row.date<=value.until),'daily sales period');
    return {date:row.date,total:row.total,orders:row.orders};
  }).sort((a,b)=>a.date.localeCompare(b.date));
  requireValue(daily===null||new Set(daily.map(row=>row.date)).size===daily.length,'duplicate sales dates');
  const topProducts=value.topProducts===null?null:value.topProducts.map(row=>{
    requireValue(row&&isId(row.id)&&isText(row.name)&&Number.isFinite(row.quantity)&&row.quantity>=0&&isMoney(row.total),'top products');
    return {id:row.id,name:row.name,quantity:row.quantity,total:row.total};
  });
  const topCustomers=value.topCustomers===null?null:value.topCustomers.map(row=>{
    requireValue(row&&isId(row.id)&&isText(row.name)&&isCount(row.orders)&&isMoney(row.total),'top customers');
    return {id:row.id,name:row.name,orders:row.orders,total:row.total};
  });
  requireValue(['errors','truncated'].every(key=>value[key]===null||value[key].every(isText)),'sales notices');
  return {currency:value.currency,since:value.since,until:value.until,total:value.total,orders:value.orders,averageOrder:value.averageOrder,daily,topProducts,topCustomers,errors:value.errors===null?null:[...value.errors],truncated:value.truncated===null?null:[...value.truncated]};
}
function isCurrency(value){return isText(value)&&/^[A-Z]{3}$/.test(value);}
function validateAdvice(value) {
  requireValue(value&&isMoment(value.generatedAt)&&typeof value.enabled==='boolean'&&(value.currency===null||isCurrency(value.currency))&&listaSana(value.items),'advice information');
  requireValue(['errors','truncated'].every(key=>Array.isArray(value[key])&&value[key].every(isText)),'advice notices');
  const items=value.items.map(row=>{
    requireValue(row&&isId(row.id)&&['perdida','dormido','deuda','quiebre'].includes(row.kind)&&['title','body','about','assumption'].every(key=>isText(row[key])),'advice finding');
    requireValue(row.amount===undefined||row.amount===null||isMoney(row.amount),'advice amount');
    requireValue(['customerId','orderId','productId'].every(key=>row[key]===null||isId(row[key])),'advice references');
    return {id:row.id,kind:row.kind,title:row.title,body:row.body,about:row.about,assumption:row.assumption,amount:row.amount??null,customerId:row.customerId,orderId:row.orderId,productId:row.productId,at:row.at};
  });
  return {generatedAt:value.generatedAt,enabled:value.enabled,currency:value.currency,items,errors:[...value.errors],truncated:[...value.truncated]};
}

function readError(error) {
  if(error.status===404||error.status===405)return 'This view is not available on this agent yet, or the record is no longer available. Refresh after the service is updated.';
  if(error.name==='TypeError'||error.name==='TimeoutError')return 'The agent could not be reached. Check your connection and try again.';
  return error.message||'This information could not be read. Please try again.';
}
async function loadRead(key,force=false) {
  if(!Object.hasOwn(state.reads,key))return;
  const range=key==='sales'?state.range:null;
  const contract={activity:['/today',validateActivity],queue:['/queue',validateQueue],operations:['/operations',validateOperations],sales:[`/sales?days=${range}`,validateSales],advice:['/advice',validateAdvice],settings:['/settings',validateSettings],prices:['/prices',validatePrices]}[key];
  if(!contract||data.mode!=='live')return;
  // A different period owns a different read. Its late predecessor cannot
  // replace it, including when the user switches 7 -> 30 -> 7 quickly.
  if(key==='sales'&&state.reads.sales.range!==range){
    state.reads.sales={...freshReads().sales,range};data.sales=null;
  }
  const read=state.reads[key];
  if(read.busy)return read.pending;
  if(!force&&data[key]&&Date.now()-Date.parse(read.loadedAt)<60000)return;
  const connection=state.connection,session=state.session;
  const current=()=>state.session===session&&state.reads[key]===read;
  read.busy=true;read.error='';render();
  read.pending=(async()=>{
  try {
    const value=contract[1](await apiRead(connection,contract[0]));
    if(state.session!==session)return;
    if(!current())return;
    data[key]=value;read.loadedAt=new Date().toISOString();
  }catch(error){
    if(state.session!==session)return;
    if(error.status===401){cerrarSesion('Your dashboard access is no longer valid. Sign in again.');return;}
    if(!current())return;
    data[key]=null;read.error=readError(error);read.loadedAt=null;
  }finally{if(current()){read.busy=false;read.pending=null;render();}}
  })();
  return read.pending;
}
async function loadViewReads(force=false) {
  if(data.mode!=='live')return;
  if(state.view==='sales')await loadRead('sales',force);
  if(state.view==='advice')await loadRead('advice',force);
  if(state.view==='today')await loadRead('activity',force);
  if(state.view==='queue')await loadRead('queue',force);
  if(state.view==='overview')await Promise.all([loadRead('queue',force),loadRead('operations',force)]);
  if(state.view==='settings')await loadRead('settings',force);
  if(state.view==='inventory')await loadRead('prices',force);
  if(state.view==='agents')await loadExtras(force);
}

async function showOrder(id) {
  if(data.mode!=='live'){
    const sample=allOrders().find(o=>o.id===id);if(sample)renderOrder(sample);
    return;
  }
  const connection=state.connection,session=state.session,request=++state.detailRequest;
  detail(id,'<p class="detail-note">Reading the order from ERPNext…</p>');
  try {
    const order=await apiRead(connection,'/orders/'+encodeURIComponent(id));
    if(state.session!==session||state.detailRequest!==request||!$('#detail-dialog').open)return;
    if(order.id!==id||!Array.isArray(order.items))throw new Error('The agent returned an invalid order.');
    renderOrder(order);
  }catch(error){
    if(state.session===session&&error.status===401){cerrarSesion('Your dashboard access is no longer valid. Sign in again.');return;}
    if(state.session===session&&state.detailRequest===request&&$('#detail-dialog').open)
      detail(id,`<div class="notice error-notice">${escape(error.message)}</div><button class="button" data-order="${escape(id)}">Try again</button>`);
  }
}

async function loadExtras(force=false) {
  if(data.mode!=='live'||state.extrasBusy||(!force&&data.policies&&data.operations&&Date.now()-state.extrasLoadedAt<60000&&Date.now()-Date.parse(state.reads.operations.loadedAt)<60000))return;
  const connection=state.connection,session=state.session;
  state.extrasBusy=true;state.extrasError='';if(state.view==='agents')render();
  const results=await Promise.allSettled([apiRead(connection,'/controls'),loadRead('operations',force)]);
  if(state.session!==session)return;
  if(results[0].status==='rejected'&&results[0].reason.status===401){cerrarSesion('Your dashboard access is no longer valid. Sign in again.');return;}
  const failures=[];
  if(results[0].status==='fulfilled'&&Array.isArray(results[0].value.policies)){data.policies=results[0].value.policies;state.extrasLoadedAt=Date.now();}
  else {data.policies=null;state.extrasLoadedAt=null;failures.push('Current limits could not be read.');}
  if(!data.operations)failures.push('Queue status could not be read.');
  state.extrasError=failures.join(' ');state.extrasBusy=false;
  if(state.view==='agents')render();
}

if(repoHosted&&!demoRequested){
  fetch(location.origin+'/api/dashboard/config',{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(8000)})
    .then(r=>r.ok?r.json():null)
    .then(value=>{if(value?.service==='plus-agent'&&data.mode==='disconnected'){state.configured=value.configured;render();}})
    .catch(()=>{});
}
setInterval(()=>{
  const editing=['INPUT','SELECT','TEXTAREA'].includes(document.activeElement?.tagName);
  if(data.mode==='live'&&document.visibilityState==='visible'&&!editing&&!document.querySelector('dialog[open]'))refresh(true);
},60000);
