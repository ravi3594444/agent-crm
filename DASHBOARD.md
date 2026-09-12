# Plus CRM dashboard

A responsive dashboard for the existing Plus Agent: overview, orders, inventory,
customers, AI agents, and connection settings. It starts signed out. Real data
appears only after a successful connection; sample records require choosing
**Explore sample data** or adding `?demo=1` to the dashboard URL.

## Open it in your agent

The existing Dockerfile includes the UI and API automatically. There is no
additional frontend service or Python dependency to install in production.

1. Deploy this branch using the staged release process in
   [plus-agent/PRUEBAS.md](plus-agent/PRUEBAS.md).
2. In your existing configured `plus-agent` directory, run:

   ```sh
   make dashboard-setup
   ```

   This adds a dedicated random dashboard token to the existing `.env`, preserves
   other settings and any valid token already present, and prints a new token
   once. Store it in your password manager. It refuses a missing `.env`, duplicate
   entries, and an existing token that is too short.
3. Restart the agent with your normal deployment process so it loads the new
   environment. Serve the agent over HTTPS.
4. Open **`/dashboard/` on that agent's address** and choose **Sign in to this
   agent**. Only the dashboard token is needed; the service address is detected.

You can also supply `DASHBOARD_API_TOKEN` through your deployment's environment
manager. It must contain at least 32 characters and be different from all
ERPNext, WhatsApp, and model API keys. Do not put those keys into the dashboard.

The repository alone does not provide a running server. A separate static
preview must connect to an agent deployed with this branch's API.

### A separately hosted dashboard

Set `DASHBOARD_ALLOWED_ORIGINS` on the agent to the dashboard's exact HTTPS
origin, for example `https://plus-crm-dashboard.belugaremodeling.chatgpt.site`.
Separate multiple origins with commas. Wildcards are never accepted; leave
this unset for the dashboard served by the agent itself.

Choose **Connect to your agent**, enter the agent's HTTPS origin without a path,
and enter its dashboard token. HTTP is supported only for local development
on localhost, 127.0.0.1, or ::1.

Optionally set `ERPNEXT_PUBLIC_URL` to ERPNext's browser-facing HTTPS origin to
show **Open in ERPNext** in order details. This is separate from the internal
ERPNext API address, which is never exposed. ERPNext enforces its own sign-in
and permissions when that link opens.

## What works

| Screen | Connected behavior |
| --- | --- |
| Overview | Booked sales, order totals, daily chart inspection, order status breakdown, pending review and low-stock alerts |
| Orders | Seven- or thirty-day history, all-date pending drafts, status filters, search, ten-row pagination and filtered CSV export |
| Order details | Fresh ERPNext line items, quantities, prices, total, status and delivery address; copy order ID; optional ERPNext link |
| Inventory | Configured warehouse stock, submitted reservations, available quantities, product search, low-stock filter and item details |
| Customers | Active customer search, group and territory, loaded order totals and recent orders that open full details |
| AI agents | Actual configured model names, current guarded owner settings, Redis availability, worker lease and queue/failure counts |
| Settings | Connect or switch agent, display connection status, and disconnect |

Visible signed-in dashboards refresh every minute, except while a dialog or
input is active. Manual refresh is always available. Failed refreshes retain
the last snapshot with a visible interruption notice. Missing datasets stay
unavailable; failed settings or queue reads never reuse sample values. Requests
from a closed sign-in or an old session cannot restore signed-out records.

## Display currency

Use the **Currency** selector beside the reporting period to choose INR, USD,
ARS, EUR, GBP, BRL, and other common currencies. **Original** restores the
recorded currencies. The selection is remembered on this device; this preference
is the only dashboard value written to browser storage.

Overview totals, charts, order amounts and customer totals convert using dated
[ExchangeRate-API open-access rates](https://www.exchangerate-api.com/docs/free).
Rates update daily, are cached in memory for an hour, and are labeled as display
estimates. Historical orders use that displayed rate, not a historical booking
rate. ERPNext records and saved automation limits remain in their original units.
Order details always show the original total. CSV exports include original and
display amounts, their currency codes, the rate date, and conversion status.

The public rate request contains only a currency code, with no CRM credentials
or records. Failed, malformed or outdated rates cannot relabel an amount: the
last valid display currency stays selected with a retry message. If a particular
order currency has no rate, its original amount is labeled explicitly.

## Data and authority

All business endpoints below require the dedicated bearer token, return
`Cache-Control: no-store`, and reject write methods. The token grants manager
read access; distribute it only to authorized staff. It stays in browser memory
and is cleared on reload or disconnect. No business data or token is persisted
in browser storage. Use the reverse proxy's normal request limits. Individual
user accounts and per-user audit trails are not part of this dashboard.

| Endpoint | Data source and scope |
| --- | --- |
| `GET /api/dashboard/config` | Public setup status only, with no business data |
| `GET /api/dashboard/snapshot` | ERPNext orders, customers, stock and currency through the manager credential scope; configured model names without provider calls |
| `GET /api/dashboard/orders/{id}` | Manager-scoped order read, limited to the configured company; full items and sanitized delivery address |
| `GET /api/dashboard/controls` | The existing `limites.resumen()` reader, including its Redis and durable-setting guards |
| `GET /api/dashboard/operations` | Read-only Redis queue counts and worker-lease presence |

The canonical settings reader can use policy-scoped **reads** to check durable
Company audit markers. It preserves the existing lost-state guard and does not
silently bootstrap owner settings. The dashboard does not submit orders, change
limits, send WhatsApp messages, or invoke a model. Approval and limit changes
continue through the authorized manager workflow and its confirmation codes.

Record and metric definitions:

- Recent orders cover the last 30 business-calendar days. Pending review is a
  separate all-date query so older drafts remain visible.
- Each list is capped at 250 records, with a truncation notice. Totals and CSV
  exports cover the loaded records. Customer totals cover recent loaded orders.
- Booked sales include submitted/completed orders in the company currency.
  This is order value, not collected payments or invoiced revenue.
- ERP available stock is physical stock minus submitted reservations in the
  configured warehouse. Draft promises, stock freshness, and safety buffers
  are evaluated separately by the policy. The low-stock display threshold is 10.
- A worker lease indicates queue ownership, not provider health or end-to-end
  delivery. Provider availability is **not probed**; configured credentials do
  not establish that a provider is reachable.

## Development and verification

The dependency-free UI lives in `plus-agent/app/dashboard_ui/`. The only external
asset is the Manrope font stylesheet, with a system-font fallback.

From the repository root:

```sh
npm run check
npm test
npm run build
```

The build copies the UI into `dist/` for static hosting; it does not run the
Python service. The JavaScript tests exercise application state and event
handlers without a browser or network.

With the repository's development requirements installed, from `plus-agent`:

```sh
pytest -q tests/test_dashboard.py tests/test_dashboard_integration.py
```

HTTP integration tests mount the production dashboard routes and use the
production ERPNext client against the repository's Frappe test double. They
check authentication, company isolation, data mappings, old drafts, timeouts,
partial failures, settings guards, queue status and setup. They do not claim
verification against your production ERPNext instance. Existing CI runs these
alongside the complete suite, container startup checks and JavaScript checks.
