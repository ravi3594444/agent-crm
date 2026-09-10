const $ = (s, root = document) => root.querySelector(s);
const escape = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
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
let money = (n, compact=false) => n == null ? '—' : new Intl.NumberFormat('es-AR', {style:'currency', currency:data.currency || 'ARS', maximumFractionDigits:0, ...(compact ? {notation:'compact'} : {})}).format(n);
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
function makeDemo() {
  const orders = [];
  [4,6,5,7,6,8,9].forEach((count, day) => {
    for (let i=0;i<count;i++) {
      const index=orders.length, customer=customers[index%customers.length], product=products[index%products.length];
      const items=[{name:product.name,qty:(i+1)*4,rate:product.price}];
      if (i%2===0) items.push({name:products[(index+2)%6].name,qty:6,rate:products[(index+2)%6].price});
      const total=items.reduce((sum,item)=>sum+item.qty*item.rate,0);
      const status=day===6 && i>=6 ? 'pending' : day===6 ? 'confirmed' : i===0 ? 'closed' : 'completed';
      orders.unshift({id:`SAL-ORD-2026-${String(index+101).padStart(5,'0')}`,customer:customer.name,customerId:customer.id,date:dateShift(today,day-6),deliveryDate:dateShift(today,day-5),total,currency:'ARS',status,channel:'WhatsApp',items,erpStatus:status==='pending'?'Draft':status==='completed'?'Completed':'To Deliver and Bill'});
    }
  });
  return {mode:'demo',company:'Plus Dairy',today,since:dateShift(today,-29),currency:'ARS',generatedAt:new Date().toISOString(),orders,customers,products,errors:[],truncated:[],limit:250,policies:[{name:'Order ceiling',value:'$ 150.000',note:'Maximum order value for automatic confirmation'},{name:'New customer ceiling',value:'$ 30.000',note:'Separate limit until a customer has order history'},{name:'Stock buffer',value:'20%',note:'Keep a buffer before confirming an order'},{name:'Stock trust window',value:'24 hours',note:'Require a recent confirmed stock count'}],agents:[{id:'sales',name:'Sales agent',role:'Customer conversations & order drafts',model:'Qwen · sales model',status:'Demo'},{id:'manager',name:'Management agent',role:'Business reports & manager assistance',model:'Qwen · management model',status:'Demo'}]};
}
let data=makeDemo();
const state={view:'overview',range:7,filter:'all',search:'',stockFilter:'all',page:1,menu:false,busy:false,stale:false,connection:null};
const nav=[['overview','Overview'],['orders','Orders'],['inventory','Inventory'],['customers','Customers'],['agents','AI agents']];
const labels={pending:'Pending review',confirmed:'Confirmed',completed:'Completed',cancelled:'Cancelled',closed:'Closed','on-hold':'On hold',unknown:'Unknown'};
const badge=(status)=>`<span class="badge badge-${escape(status)}">${icon(status==='pending'?'clock':status==='confirmed'||status==='completed'?'check':'info')}${escape(labels[status] || status)}</span>`;
function periodOrders() { const start=dateShift(data.today,-state.range+1); return (data.orders||[]).filter((o)=>o.date>=start && o.date<=data.today); }
function selectedOrders() {return periodOrders().filter((o)=>(state.filter==='all'||o.status===state.filter) && `${o.id} ${o.customer}`.toLowerCase().includes(state.search.toLowerCase()));}
function sumSales(orders) {return orders.filter(o=>['confirmed','completed'].includes(o.status)&&o.currency===data.currency).reduce((s,o)=>s+(o.total??0),0);}
function daysSeries() {return Array.from({length:state.range},(_,i)=>{const date=dateShift(data.today,-state.range+1+i);return {date,orders:periodOrders().filter(o=>o.date===date)};});}
function toast(message) { const node=$('#toast');node.textContent=message;node.classList.add('visible');clearTimeout(toast.timer);toast.timer=setTimeout(()=>node.classList.remove('visible'),4000); }
function goto(view) {state.view=view;state.search='';state.filter='all';state.stockFilter='all';state.page=1;state.menu=false;history.replaceState(null,'','#'+view);render();window.scrollTo({top:0,behavior:'instant'});}
function avatar(name,index=0) {return `<span class="avatar avatar-${index%5}" aria-hidden="true">${escape(initials(name))}</span>`;}
function shell() {
  const pending=(data.orders||[]).filter(o=>o.status==='pending').length;
  const title=nav.find(([key])=>key===state.view)?.[1] || 'Connection & settings';
  return `<div class="dashboard ${state.menu?'menu-open':''}">
    <button class="sidebar-shade" data-action="close-menu" aria-label="Close navigation"></button>
    <aside class="sidebar" aria-label="Main navigation">
      <a class="brand" href="#overview" data-view="overview"><span class="brand-symbol">+</span><span>plus<span class="brand-period">.</span></span><span class="brand-tag">CRM</span></a>
      <div class="workspace"><span class="workspace-icon">${icon('inventory')}</span><div><strong>${escape(data.company)}</strong><span>Operations workspace</span></div></div>
      <div class="nav-label">WORKSPACE</div>
      <nav>${nav.map(([key,label])=>`<a href="#${key}" data-view="${key}" class="nav-item ${state.view===key?'active':''}" ${state.view===key?'aria-current="page"':''}>${icon(key)}<span>${label}</span>${key==='orders'&&pending?`<span class="nav-count">${pending}</span>`:''}${key==='agents'?'<span class="new-tag">AI</span>':''}</a>`).join('')}</nav>
      <div class="sidebar-bottom"><div class="sidebar-note">${icon('shield')}<strong>You set the rules.</strong><p>Your agents work within the limits you approve.</p><button class="text-link" data-view="agents">View agent controls ${icon('arrow')}</button></div>
      <a href="#settings" data-view="settings" class="nav-item ${state.view==='settings'?'active':''}">${icon('settings')}<span>Settings</span></a>
      <div class="profile"><span class="avatar owner">R</span><div><strong>Ravi</strong><span>Workspace owner</span></div><span class="profile-label">ADMIN</span></div></div>
    </aside>
    <div class="main-wrap">
      <header class="topbar"><div class="breadcrumbs"><button class="icon-button menu-button" data-action="menu" aria-label="Open navigation" aria-expanded="${state.menu}">${icon('menu')}</button><span>Workspace</span><span class="crumb-slash">/</span><strong>${title}</strong></div>
      <div class="top-actions"><span class="mode-chip ${data.mode==='live'?'live-chip':''}">${icon(data.mode==='demo'?'overview':'link')}${data.mode==='demo'?'Demo workspace':state.stale?'Connection interrupted':'Live data'}</span><button class="icon-button notification-button" data-action="pending" aria-label="View ${pending} orders awaiting review">${icon('bell')}${pending?'<span class="notification-dot"></span>':''}</button><span class="avatar owner small">R</span></div></header>
      <main id="main" tabindex="-1">
        ${state.stale?'<div class="notice error-notice">Connection interrupted. The last snapshot remains visible; refresh to try again.</div>':''}
        ${data.errors.length?`<div class="notice error-notice">Some data could not be read: ${escape(data.errors.join(', '))}. Missing information is shown as unavailable.</div>`:''}
        ${data.truncated.length?`<div class="notice">Showing up to ${data.limit} records per section. Totals cover the loaded records only.</div>`:''}
        <div class="page-heading"><div><div class="eyebrow">YOUR OPERATIONS, CONNECTED</div><h1>${title}</h1><p>${{overview:'A clear view of your business. Every order, every day.',orders:'Follow each order from received to fulfilled.',inventory:'Know what is on the shelf and already reserved.',customers:'The people and businesses behind your orders.',agents:'Your team behind the conversations.',settings:'Connect your dashboard to the agent service.'}[state.view]}</p></div>
        <div class="heading-actions">${['overview','orders'].includes(state.view)?`<label class="select-wrap">${icon('calendar')}<select id="range" aria-label="Reporting period"><option value="7" ${state.range===7?'selected':''}>Last 7 days</option><option value="30" ${state.range===30?'selected':''}>Last 30 days</option></select></label>`:''}<button class="button ${data.mode==='demo'?'primary':''}" data-action="${data.mode==='demo'?'connect':'refresh'}" ${state.busy?'disabled':''}>${icon(data.mode==='demo'?'link':'refresh')}${state.busy?'Refreshing…':data.mode==='demo'?'Connect live data':'Refresh'}</button></div></div>
        <div id="view-content">${views[state.view]()}</div>
        <footer class="footer"><span>Plus CRM <span class="footer-dot">·</span> ${data.mode==='demo'?'Sample data for exploring the dashboard':`Snapshot · ${escape(new Date(data.generatedAt).toLocaleString('en-GB'))}`}</span><span>${data.currency?escape(data.currency)+' currency · ':''}Read-only workspace</span></footer>
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
  const pending=orders.filter(o=>o.status==='pending');
  const series=daysSeries().slice(-7), sales=sumSales(orders);
  const stats=[
    ['Booked sales',missing||!data.currency?'—':money(sales),'receipt','violet',`Confirmed orders · ${data.currency || 'currency unavailable'}`,series.map(d=>sumSales(d.orders))],
    ['Orders received',missing?'—':number(orders.length),'orders','blue','All orders in this period',series.map(d=>d.orders.length)],
    ['Confirmed orders',missing?'—':number(confirmed.length),'check','green','Including completed orders',series.map(d=>d.orders.filter(o=>['confirmed','completed'].includes(o.status)).length)],
    ['Awaiting review',missing?'—':number(pending.length),'clock','amber',pending.length?'Ready for a manager’s attention':'No reviews in this period',series.map(d=>d.orders.filter(o=>o.status==='pending').length)]
  ];
  return `<div class="stats-grid">${stats.map(([label,value,ic,color,note,trend])=>`<article class="stat-card"><div class="stat-top"><span>${label}</span><span class="stat-icon ${color}">${icon(ic)}</span></div><div class="stat-value ${label==='Booked sales'?'money-value':''}">${value}</div><div class="stat-bottom"><span>${note}</span>${sparkline(trend,{violet:'#7665e8',blue:'#6396e9',green:'#4fa889',amber:'#c8983e'}[color])}</div></article>`).join('')}</div>`;
}
function chart() {
  const series=daysSeries(),values=series.map(d=>sumSales(d.orders)),max=Math.max(1,...values);
  const total=values.reduce((a,b)=>a+b,0);
  return `<section class="card chart-card"><div class="card-heading"><div><h2>Sales overview</h2><p>Confirmed order value over time</p></div><span class="legend"><i></i>Booked sales</span></div><div class="chart-summary"><strong>${data.orders===null||!data.currency?'—':money(total)}</strong><span>${prettyDate(dateShift(data.today,-state.range+1))} – ${prettyDate(data.today)}</span></div>
  <div class="chart" role="img" aria-label="Booked sales by day. Total ${escape(money(total))}."><div class="chart-axis">${[1,.75,.5,.25,0].map(n=>`<span>${money(max*n,true)}</span>`).join('')}</div><div class="plot"><div class="grid-lines"><i></i><i></i><i></i><i></i><i></i></div><div class="bars ${state.range===30?'dense':''}">${series.map((d,i)=>`<div class="bar-column"><button class="bar ${i===series.length-1?'current':''}" style="--height:${values[i]/max*100}%" data-chart-date="${d.date}" aria-label="${prettyDate(d.date)}: ${escape(money(values[i]))}" title="${prettyDate(d.date)} · ${escape(money(values[i]))}"></button><span>${state.range===7?prettyDate(d.date,{weekday:'short'}).split(',')[0].split(' ')[0]:i%5===0||i===29?new Date(d.date+'T12:00:00Z').getUTCDate():''}</span></div>`).join('')}</div></div></div><p id="chart-detail" class="chart-note">Select a bar to inspect its daily total.</p></section>`;
}
function orderMix() {
  const orders=periodOrders(),confirmed=orders.filter(o=>['confirmed','completed'].includes(o.status)).length,pending=orders.filter(o=>o.status==='pending').length,other=orders.length-confirmed-pending,total=orders.length;
  const p=total?confirmed/total*100:0,q=total?pending/total*100:0;
  return `<section class="card mix-card"><div class="card-heading"><div><h2>Order breakdown</h2><p>Where your orders stand</p></div>${icon('orders')}</div><div class="donut-wrap"><div class="donut" style="--confirmed:${p}%;--pending:${p+q}%" role="img" aria-label="${confirmed} confirmed, ${pending} pending, ${other} other"><div><strong>${data.orders===null?'—':total}</strong><span>Total orders</span></div></div></div><div class="mix-legend">${[['Confirmed',confirmed,'purple'],['Pending review',pending,'orange'],['Other states',other,'gray']].map(([label,count,c])=>`<div><span><i class="${c}"></i>${label}</span><strong>${count}</strong><small>${total?Math.round(count/total*100):0}%</small></div>`).join('')}</div></section>`;
}
function orderTable(orders, compact=false) {
  if(data.orders===null)return empty('Order data is unavailable','Refresh the connection to try again.');
  if(!orders.length)return empty('No orders found','Try another search or reporting period.');
  return `<div class="table-scroll"><table><thead><tr><th>Order</th><th>Customer</th><th>Status</th><th>${compact?'Channel':'Order date'}</th><th class="amount">Amount</th><th><span class="sr-only">Details</span></th></tr></thead><tbody>${orders.map((o,i)=>`<tr><td><button class="order-link" data-order="${escape(o.id)}">${escape(o.id.replace('SAL-ORD-','SO-'))}</button><span class="cell-note">${prettyDate(o.date)}</span></td><td><div class="customer-cell">${avatar(o.customer,i)}<span>${escape(o.customer)}</span></div></td><td>${badge(o.status)}</td><td>${compact?`<span class="channel"><i class="${o.channel==='WhatsApp'?'wa':''}"></i>${escape(o.channel)}</span>`:escape(o.date)}</td><td class="amount">${moneyFor(o.total,o.currency)}</td><td><button class="icon-button row-open" data-order="${escape(o.id)}" aria-label="View order ${escape(o.id)}">${icon('arrow')}</button></td></tr>`).join('')}</tbody></table></div>`;
}
function moneyFor(value,currency) {if(value==null||!currency)return '—';try{return new Intl.NumberFormat('es-AR',{style:'currency',currency,maximumFractionDigits:0}).format(value);}catch{return '—';}}
function empty(title,note) {return `<div class="empty">${icon('search')}<h3>${title}</h3><p>${note}</p></div>`;}
function agentsMini() {
  return `<section class="card agents-strip"><div class="strip-title"><span class="stat-icon violet">${icon('agents')}</span><div><h2>Your AI team</h2><p>Two roles. One connected business.</p></div></div>${data.agents.map(a=>`<div class="mini-agent"><span class="agent-avatar ${a.id}">${icon(a.id==='sales'?'bolt':'shield')}</span><div><strong>${escape(a.name)}</strong><span>${escape(a.role)}</span></div><span class="subtle-pill">${escape(a.status)}</span></div>`).join('')}<button class="icon-button" data-view="agents" aria-label="View AI agents">${icon('arrow')}</button></section>`;
}
function attention() {
  const pending=(data.orders||[]).filter(o=>o.status==='pending'),low=(data.products||[]).filter(p=>p.available!==null&&p.available<=10);
  return `<section class="card attention-card"><div class="card-heading"><div><h2>Needs attention <span class="count-bubble">${pending.length+low.length}</span></h2><p>The exceptions worth a closer look</p></div></div>${pending.length?`<button class="attention-item" data-action="pending"><span class="attention-icon amber">${icon('clock')}</span><div><strong>${pending.length} orders awaiting review</strong><span>Waiting for a manager’s decision</span></div>${icon('arrow')}</button>`:''}${low.map(p=>`<button class="attention-item" data-product="${escape(p.id)}"><span class="attention-icon rose">${icon('inventory')}</span><div><strong>${escape(p.name)}</strong><span>${number(p.available)} ${escape(p.unit)} of ERP stock available</span></div>${icon('arrow')}</button>`).join('')}${!pending.length&&!low.length?'<div class="quiet-state">No alerts in the loaded records.</div>':''}<div class="attention-foot">${icon('shield')}<span>Approvals stay with your manager.</span></div></section>`;
}
function overview() {
  return `${stats()}<div class="overview-top">${chart()}${orderMix()}</div>${agentsMini()}<div class="overview-bottom"><section class="card orders-card"><div class="card-heading"><div><h2>Recent orders</h2><p>The latest activity in your business</p></div><button class="text-link" data-view="orders">View all ${icon('arrow')}</button></div>${orderTable(periodOrders().slice(0,5),true)}</section>${attention()}</div>`;
}
function filterTabs() {
  const opts=[['all','All orders'],['pending','Awaiting review'],['confirmed','Confirmed'],['completed','Completed']];
  return `<div class="filter-tabs" role="group" aria-label="Filter order status">${opts.map(([key,label])=>`<button data-filter="${key}" class="${state.filter===key?'selected':''}" aria-pressed="${state.filter===key}">${label}<span>${key==='all'?periodOrders().length:periodOrders().filter(o=>o.status===key).length}</span></button>`).join('')}</div>`;
}
function searchField(placeholder) {return `<label class="search-field">${icon('search')}<input id="search" type="search" placeholder="${placeholder}" aria-label="${placeholder}" value="${escape(state.search)}"></label>`;}
function ordersView() {
  const selected=selectedOrders(),maxPage=Math.max(1,Math.ceil(selected.length/10));state.page=Math.min(state.page,maxPage);
  return `${stats()}<section class="card orders-full"><div class="orders-toolbar">${filterTabs()}<button class="button" data-action="export">${icon('export')}Export CSV</button></div><div class="search-toolbar">${searchField('Search orders or customers…')}<span>${selected.length} orders</span></div><div id="orders-results">${orderTable(selected.slice((state.page-1)*10,state.page*10))}</div><div class="pagination"><span>Page ${state.page} of ${maxPage}</span><div><button class="button" data-action="prev" ${state.page===1?'disabled':''}>Previous</button><button class="button" data-action="next" ${state.page>=maxPage?'disabled':''}>Next ${icon('arrow')}</button></div></div></section>`;
}
function inventoryView() {
  const rows=(data.products||[]).filter(p=>`${p.name} ${p.id}`.toLowerCase().includes(state.search.toLowerCase())&&(state.stockFilter!=='low'||p.available!==null&&p.available<=10));
  const low=(data.products||[]).filter(p=>p.available!==null&&p.available<=10).length;
  return `<div class="inventory-summary"><div><span class="stat-icon blue">${icon('inventory')}</span><div><strong>${data.products===null?'—':data.products.length}</strong><span>Products in the warehouse</span></div></div><div><span class="stat-icon amber">${icon('clock')}</span><div><strong>${data.products===null?'—':low}</strong><span>At or below 10 available units</span></div></div><div class="inventory-definition">${icon('info')}<p>ERP available = physical stock − submitted reservations. Open drafts and safety rules can reduce what an agent may confirm.</p></div></div><section class="card"><div class="search-toolbar">${searchField('Search products or item codes…')}<label class="select-wrap"><select id="stock-filter" aria-label="Filter inventory"><option value="all">All products</option><option value="low" ${state.stockFilter==='low'?'selected':''}>Low stock · 10 or fewer</option></select></label></div>${data.products===null?empty('Inventory is unavailable','Refresh the connection to try again.'):rows.length?`<div class="table-scroll"><table><thead><tr><th>Product</th><th>On hand</th><th>Reserved</th><th>ERP available</th><th>Stock position</th><th></th></tr></thead><tbody>${rows.map(p=>`<tr><td><button class="product-cell" data-product="${escape(p.id)}"><span class="product-icon">${icon('inventory')}</span><span><strong>${escape(p.name)}</strong><small>${escape(p.id)}</small></span></button></td><td>${number(p.stock)} <span class="muted">${escape(p.unit)}</span></td><td>${number(p.reserved)}</td><td><strong>${number(p.available)}</strong></td><td><div class="stock-meter"><span style="width:${p.stock?Math.max(0,Math.min(100,p.available/p.stock*100)):0}%" class="${p.available!==null&&p.available<=10?'low':''}"></span></div><span class="cell-note">${p.available===null?'Unknown':p.available<=10?'Low stock':'In stock'}</span></td><td><button class="icon-button" data-product="${escape(p.id)}" aria-label="View ${escape(p.name)}">${icon('arrow')}</button></td></tr>`).join('')}</tbody></table></div>`:empty('No matching products','Try a different search or stock filter.')}</section>`;
}
function customersView() {
  const rows=(data.customers||[]).filter(c=>`${c.name} ${c.territory}`.toLowerCase().includes(state.search.toLowerCase()));
  return `<section class="card"><div class="search-toolbar">${searchField('Search customers or locations…')}<span>${rows.length} customers</span></div>${data.customers===null?empty('Customer data is unavailable','Refresh the connection to try again.'):rows.length?`<div class="customer-grid">${rows.map((c,i)=>{const orders=(data.orders||[]).filter(o=>o.customerId===c.id);return `<button class="customer-card" data-customer="${escape(c.id)}"><div class="customer-card-top">${avatar(c.name,i)}${icon('arrow')}</div><h2>${escape(c.name)}</h2><p>${escape(c.group)} · ${escape(c.territory || 'Location unavailable')}</p><div class="customer-card-stats"><div><strong>${orders.length}</strong><span>Loaded orders</span></div><div><strong>${money(sumSales(orders))}</strong><span>Booked sales</span></div></div></button>`;}).join('')}</div>`:empty('No customers found','Try another name or location.')}</section>`;
}
function agentsView() {
  return `<div class="agent-grid">${data.agents.map(a=>`<section class="card agent-card"><div class="agent-card-top"><span class="agent-avatar ${a.id}">${icon(a.id==='sales'?'bolt':'shield')}</span><span class="subtle-pill">${escape(a.status)}</span></div><h2>${escape(a.name)}</h2><p>${escape(a.role)}</p><div class="model-line"><span>Model</span><strong>${escape(a.model)}</strong></div><ul class="capabilities">${(a.id==='sales'?['Answers product and stock questions','Finds or registers customers','Creates order drafts for policy evaluation']:['Reads sales, stock, and customer reports','Prepares actions requested by the manager','Helps the owner review orders and limits']).map(t=>`<li>${icon('check')}${t}</li>`).join('')}</ul><div class="agent-boundary">${icon('shield')}${a.id==='sales'?'Customer-scoped access · draft-only writes':'Management-scoped access · approved actions only'}</div></section>`).join('')}</div><section class="card policy-card"><div class="card-heading"><div><h2>Automation controls</h2><p>The owner defines the limits. The policy checks every order.</p></div><span class="subtle-pill">${data.mode==='demo'?'Example settings':'Managed through WhatsApp'}</span></div>${data.policies?`<div class="policy-grid">${data.policies.map(p=>`<div><span>${escape(p.name)}</span><strong>${escape(p.value)}</strong><p>${escape(p.note)}</p></div>`).join('')}</div>`:'<div class="policy-explanation"><p>Current policy values are managed through the authorized manager’s WhatsApp thread. Ask the management agent to show the limits; changes use the existing confirmation code.</p></div>'}<div class="policy-footer">${icon('info')}<span>Orders that need an exception wait for the human manager. Agent status and model availability are not probed by this dashboard.</span></div></section>`;
}
function settingsView() {
  return `<div class="settings-grid"><section class="card connection-card"><span class="stat-icon violet">${icon('link')}</span><h2>${data.mode==='demo'?'Connect your business':'Your CRM connection'}</h2><p>${data.mode==='demo'?'Explore sample orders now, or connect to your deployed Plus Agent for a live view of ERPNext.':'This workspace reads orders, customers, and inventory from your agent service.'}</p><dl><div><dt>Workspace</dt><dd>${escape(data.company)}</dd></div><div><dt>Data source</dt><dd>${data.mode==='demo'?'Sample dataset':'ERPNext via Plus Agent'}</dd></div><div><dt>Access</dt><dd>Read-only</dd></div><div><dt>Connection</dt><dd>${data.mode==='demo'?'Not connected':state.stale?'Interrupted':'Connected'}</dd></div></dl><div class="connection-buttons"><button class="button primary" data-action="connect">${icon('link')}${data.mode==='demo'?'Connect live data':'Change connection'}</button>${data.mode==='live'?'<button class="button" data-action="disconnect">Disconnect</button>':''}</div></section><section class="card setting-notes"><h2>Designed around your workflow</h2><div>${icon('orders')}<section><h3>ERPNext is the source of truth</h3><p>The dashboard reads the last 30 days of orders and up to 250 records per section. Loaded totals are labeled when a limit is reached.</p></section></div><div>${icon('shield')}<section><h3>Approvals stay protected</h3><p>Confirm orders and change rules through your existing manager workflow. This dashboard does not submit or modify business records.</p></section></div><div>${icon('link')}<section><h3>A connection for this session</h3><p>Your access token stays in memory. Reloading the page clears the connection and returns to the demo.</p></section></div></section></div>`;
}
const views={overview,orders:ordersView,inventory:inventoryView,customers:customersView,agents:agentsView,settings:settingsView};
function render() {$('#app').innerHTML=shell();}
function detail(title,content) {
  const dialog=$('#detail-dialog');
  dialog.innerHTML=`<div class="detail-head"><span>WORKSPACE DETAILS</span><button class="icon-button" data-close="detail-dialog" aria-label="Close details">${icon('close')}</button></div><div class="detail-body"><h2 id="detail-title">${escape(title)}</h2>${content}</div>`;
  if(!dialog.open)dialog.showModal();
}
function showOrder(id) {
  const o=(data.orders||[]).find(o=>o.id===id);if(!o)return;
  detail(o.id,`<div class="detail-status">${badge(o.status)}<span>${escape(o.channel)}</span></div><div class="detail-customer">${avatar(o.customer)}<div><strong>${escape(o.customer)}</strong><span>Customer</span></div></div><dl class="detail-fields"><div><dt>Order date</dt><dd>${prettyDate(o.date,{year:'numeric'})}</dd></div><div><dt>Delivery date</dt><dd>${o.deliveryDate?prettyDate(o.deliveryDate):'Not set'}</dd></div><div><dt>ERPNext status</dt><dd>${escape(o.erpStatus)}</dd></div></dl>${o.items?`<h3>Order items</h3><div class="line-items">${o.items.map(item=>`<div><span><strong>${escape(item.name)}</strong><small>${item.qty} × ${moneyFor(item.rate,o.currency)}</small></span><b>${moneyFor(item.qty*item.rate,o.currency)}</b></div>`).join('')}</div>`:'<p class="detail-note">Open this order in ERPNext to view its line items and delivery address.</p>'}<div class="order-total"><span>Order total</span><strong>${moneyFor(o.total,o.currency)}</strong></div><div class="detail-callout">${icon('shield')}<p>${o.status==='pending'?'This order is waiting for review. Use the authorized manager’s WhatsApp thread to review or confirm it.':'Order actions remain in the existing ERPNext and manager workflow.'}</p></div><button class="button full" data-copy="${escape(o.id)}">Copy full order ID</button>`);
}
function showProduct(id) {
  const p=(data.products||[]).find(p=>p.id===id);if(!p)return;
  detail(p.name,`<p class="muted">${escape(p.id)}</p><div class="product-detail-number"><strong>${number(p.available)}</strong><span>${escape(p.unit)} ERP available</span></div><dl class="detail-fields"><div><dt>Physical stock</dt><dd>${number(p.stock)}</dd></div><div><dt>Submitted reservations</dt><dd>${number(p.reserved)}</dd></div><div><dt>Warehouse</dt><dd>${escape(p.warehouse)}</dd></div></dl><div class="detail-callout">${icon('info')}<p>These are ERPNext warehouse quantities. Draft reservations, stock freshness, and your safety buffer are evaluated separately by the order policy.</p></div>`);
}
function showCustomer(id) {
  const c=(data.customers||[]).find(c=>c.id===id);if(!c)return;
  const orders=(data.orders||[]).filter(o=>o.customerId===id);
  detail(c.name,`<p class="muted">${escape(c.group)} · ${escape(c.territory)}</p><dl class="detail-fields"><div><dt>Customer ID</dt><dd>${escape(c.id)}</dd></div><div><dt>Loaded orders</dt><dd>${orders.length}</dd></div><div><dt>Booked sales</dt><dd>${money(sumSales(orders))}</dd></div></dl><h3>Recent orders</h3><div class="customer-order-list">${orders.length?orders.slice(0,10).map(o=>`<button data-order="${escape(o.id)}"><span><strong>${escape(o.id)}</strong><small>${prettyDate(o.date)}</small></span><span>${moneyFor(o.total,o.currency)}${icon('arrow')}</span></button>`).join(''):'<p>No orders in the loaded snapshot.</p>'}</div>`);
}
function openConnection() {
  const dialog=$('#connection-dialog');
  dialog.innerHTML=`<div class="detail-head"><span>LIVE WORKSPACE</span><button class="icon-button" data-close="connection-dialog" aria-label="Close connection">${icon('close')}</button></div><form id="connect-form" class="connection-form"><span class="stat-icon violet">${icon('link')}</span><h2 id="connection-title">Connect to Plus Agent</h2><p>Use the address of your deployed agent service and its dashboard access token.</p><label>Agent service URL<input name="url" type="url" required placeholder="https://agent.your-business.com" value="${escape(state.connection?.base || (location.pathname.startsWith('/dashboard')?location.origin:''))}" autocomplete="url"></label><label>Dashboard access token<input name="token" type="password" required minlength="32" autocomplete="off" placeholder="Enter your dashboard token"></label><p class="field-note">This is a dedicated dashboard token, not your ERPNext, WhatsApp, or model API key. It is kept only for this session.</p><div id="connection-error" class="form-error" role="alert"></div><button class="button primary full" type="submit">Connect workspace ${icon('arrow')}</button><p class="field-note">The dashboard API must be enabled on your agent service. A remote service must allow this dashboard’s origin.</p></form>`;
  dialog.showModal();
}
function validateSnapshot(value) {
  if(!value||value.mode!=='live'||typeof value.company!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value.today)||Number.isNaN(Date.parse(value.generatedAt)))throw new Error('The service returned an invalid dashboard response.');
  for(const key of ['orders','customers','products']) if(value[key]!==null&&(!Array.isArray(value[key])||value[key].length>250))throw new Error('The service returned invalid records.');
  if(!Array.isArray(value.agents)||!Array.isArray(value.errors)||!Array.isArray(value.truncated))throw new Error('The service returned incomplete dashboard information.');
  for(const o of value.orders||[])if(typeof o.id!=='string'||typeof o.customer!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(o.date))throw new Error('The service returned an invalid order.');
  if(value.currency){try{new Intl.NumberFormat('en',{style:'currency',currency:value.currency});}catch{throw new Error('The service returned an invalid currency.');}}
  value.policies=null;return value;
}
async function fetchData(connection) {
  const response=await fetch(connection.base+'/api/dashboard/snapshot',{headers:{Authorization:'Bearer '+connection.token},cache:'no-store',credentials:'omit',redirect:'error',signal:AbortSignal.timeout(45000)});
  if(!response.ok)throw new Error(({401:'The dashboard token was not accepted.',403:'This dashboard’s origin is not allowed by the service.',404:'The dashboard API is not installed at this address.',503:'Enable dashboard access on your agent service first.'})[response.status] || 'The agent service could not return a snapshot.');
  return validateSnapshot(await response.json());
}
async function refresh() {
  if(!state.connection||state.busy)return;
  state.busy=true;render();
  try{data=await fetchData(state.connection);state.stale=false;toast('Dashboard refreshed.');}
  catch(e){state.stale=true;toast(e.message||'Could not refresh the dashboard.');}
  finally{state.busy=false;render();}
}
function exportOrders() {
  const cell=(v)=>{const s=String(v??'');return '"'+(/^[=+\-@\t\r]/.test(s)?"'"+s:s).replaceAll('"','""')+'"';};
  const lines=[['Order ID','Customer','Date','Status','Amount','Currency'],...selectedOrders().map(o=>[o.id,o.customer,o.date,labels[o.status],o.total,o.currency])];
  const blob=new Blob(['\ufeff'+lines.map(row=>row.map(cell).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'});
  const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`plus-${data.mode}-orders-${data.today}.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('Filtered orders exported.');
}
document.addEventListener('click',async e=>{
  const target=e.target.closest('button,a');if(!target)return;
  if(target.dataset.view){e.preventDefault();goto(target.dataset.view);}
  if(target.dataset.close)$('#'+target.dataset.close).close();
  if(target.dataset.order)showOrder(target.dataset.order);
  if(target.dataset.product)showProduct(target.dataset.product);
  if(target.dataset.customer)showCustomer(target.dataset.customer);
  if(target.dataset.copy){try{await navigator.clipboard.writeText(target.dataset.copy);toast('Order ID copied.');}catch{toast('Clipboard unavailable. You can select and copy the order ID above.');}}
  if(target.dataset.filter){state.filter=target.dataset.filter;state.page=1;render();}
  if(target.dataset.chartDate){const d=target.dataset.chartDate,orders=periodOrders().filter(o=>o.date===d);$('#chart-detail').textContent=`${prettyDate(d)} · ${orders.length} orders · ${money(sumSales(orders))} booked sales`;document.querySelectorAll('.bar').forEach(b=>b.classList.toggle('inspected',b===target));}
  const action=target.dataset.action;
  if(action==='connect')openConnection();
  if(action==='refresh')refresh();
  if(action==='menu'){state.menu=!state.menu;render();}
  if(action==='close-menu'){state.menu=false;render();}
  if(action==='pending'){goto('orders');state.filter='pending';state.range=30;render();}
  if(action==='export')exportOrders();
  if(action==='prev'||action==='next'){state.page+=action==='next'?1:-1;render();}
  if(action==='disconnect'){state.connection=null;data=makeDemo();state.stale=false;render();toast('Disconnected. Showing sample data.');}
});
document.addEventListener('input',e=>{
  if(e.target.id==='search'){
    const cursor=e.target.selectionStart;state.search=e.target.value;state.page=1;render();
    const input=$('#search');input.focus();try{input.setSelectionRange(cursor,cursor);}catch{}
  }
});
document.addEventListener('change',e=>{
  if(e.target.id==='range'){state.range=Number(e.target.value);state.page=1;render();}
  if(e.target.id==='stock-filter'){state.stockFilter=e.target.value;render();}
});
document.addEventListener('submit',async e=>{
  if(e.target.id!=='connect-form')return;e.preventDefault();
  const form=e.target,button=$('button[type=submit]',form),error=$('#connection-error'),fields=new FormData(form);
  try{
    const url=new URL(String(fields.get('url'))),local=['localhost','127.0.0.1','[::1]'].includes(url.hostname);
    if(url.username||url.password||url.search||url.hash||url.pathname!=='/'||!(url.protocol==='https:'||local&&url.protocol==='http:'))throw new Error('Enter an HTTPS service origin without a path. HTTP is allowed only for local development.');
    const token=String(fields.get('token')).trim();if(token.length<32)throw new Error('The dashboard token must contain at least 32 characters.');
    button.disabled=true;button.textContent='Connecting…';error.textContent='';
    const connection={base:url.origin,token},snapshot=await fetchData(connection);
    data=snapshot;state.connection=connection;state.stale=false;state.page=1;$('#connection-dialog').close();form.reset();render();toast('Connected to your live CRM.');
  }catch(ex){error.textContent=ex.name==='TimeoutError'?'The service took too long to respond. Try again.':ex.message==='Failed to fetch'?'Could not reach the service. Check its address, HTTPS, and allowed dashboard origin.':ex.message;button.disabled=false;button.textContent='Connect workspace';}
});
document.querySelectorAll('dialog').forEach(dialog=>{dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}});dialog.addEventListener('close',()=>{if(dialog.id==='connection-dialog')dialog.innerHTML='';});});
window.addEventListener('hashchange',()=>{const view=location.hash.slice(1);if(views[view])goto(view);});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&state.menu){state.menu=false;render();}});
if(views[location.hash.slice(1)])state.view=location.hash.slice(1);
render();
