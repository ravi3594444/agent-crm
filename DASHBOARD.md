# Plus CRM dashboard

Overview, orders, inventory, customers, AI agents, and connection settings for
the existing Plus Agent. The responsive UI opens with an explicitly labeled
sample dataset. The hosted preview contains no production records or credentials.

## Use with the existing agent

The current Dockerfile copies the app directory, so the dashboard is included
without changing the container, Python dependencies, or WhatsApp routes.

1. Deploy the updated agent code using the project's existing staged release
   process in plus-agent/PRUEBAS.md.
2. Generate a dedicated random token:
   python -c 'import secrets; print(secrets.token_urlsafe(48))'
3. Set DASHBOARD_API_TOKEN in the agent environment. At least 32 characters
   are required. Keep it separate from all ERPNext, WhatsApp, and model keys.
4. Serve the agent over HTTPS and open /dashboard/.
5. Choose **Connect live data**, enter the service origin and dashboard token.

For a separately hosted dashboard, also set DASHBOARD_ALLOWED_ORIGINS on the
agent to its exact HTTPS origin. Separate multiple origins with commas.
Wildcards are never accepted. Leave this unset for same-origin use.

The token grants manager read access: distribute it only to authorized staff.
It is held in browser memory and cleared on reload or disconnect. There is no
browser storage of business records or credentials. Use the reverse proxy's
normal request limits for this endpoint. Individual user accounts and per-user
audit trails remain a future integration.

## Data and authority

GET /api/dashboard/snapshot requires bearer authentication and always returns
Cache-Control: no-store. Its only ERPNext calls are reads within the existing
manager credential scope. It never uses the policy identity, submits an order,
changes limits, or sends a WhatsApp message.

- Orders: last 30 business-calendar days, at most 250. The dashboard offers
  seven- and thirty-day views. Totals cover loaded rows, not all-time records.
- Booked sales: submitted or completed orders in the company's default currency.
  This is order value, not collected payments or invoiced revenue.
- Inventory: configured warehouse; physical stock less ERPNext submitted
  reservations. Draft promises, stock freshness, and safety buffers are not
  included. Low stock uses a display-only threshold of 10 units.
- Customers: up to 250 active customers. Order totals cover the loaded snapshot.
- Missing datasets remain unavailable with an error notice. Truncated datasets
  are labeled. Provider availability is explicitly **Not checked**.
- Live policy values and order line items are not exposed in the first API.
  Existing manager WhatsApp and ERPNext workflows remain available for those.

Demo actions filter, inspect, and export fictional records. They do not pretend
to approve orders or change live settings. Connection errors retain the last
successful live snapshot with a visible stale-data warning.

## Development and verification

The UI is dependency-free HTML, CSS, and JavaScript in
plus-agent/app/dashboard_ui/. The only external asset is the Manrope font
stylesheet, with system-font fallback. Run npm run build at the repository
root to copy the UI into dist/ for static hosting. This does not deploy the
Python service into the hosted preview.

Focused checks without Redis, FastAPI, or external services:

- node --check plus-agent/app/dashboard_ui/app.js
- python plus-agent/tests/test_dashboard.py
- npm run build

Existing CI also discovers the dashboard tests. Complete the repository's
normal integration and release checks before deploying the Python service.
