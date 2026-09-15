# MCP: la puerta de salida, la de entrada, y qué conviene

Dos archivos, dos direcciones, y no hay que confundirlos:

| | archivo | qué hace |
|---|---|---|
| **salida** | `app/mcp_server.py` | Cualquier harness (n8n, Claude Code, Claude Desktop) se conecta y usa **nuestras** herramientas de gerencia. |
| **entrada** | `app/mcp_cliente.py` | El agente de gerencia usa las herramientas de **otro** servidor MCP. |

---

## Los números, medidos contra los servidores reales

No son estimaciones: salen de un `tools/list` contra cada servidor corriendo.

| | herramientas | bytes de esquema | ~tokens/turno | que escriben |
|---|---:|---:|---:|---:|
| **nuestro** `app/mcp_server.py` | 19 | 20.627 | ~5.700 | 6 |
| `@casys/mcp-erpnext` v3.0.4, todo | 125 | 79.867 | ~22.200 | 35 |
| …con `--categories` para una distribuidora | 62 | 37.316 | ~10.400 | 15 |

`--categories=sales,inventory,delivery,crm,accounting,analytics` es lo que deja
62. Es el **único** filtro que trae: no hay `--read-only` ni lista de exclusión.

---

## Lo que encontramos mirando el servidor, no el README

**1. El precio lo pone el modelo.** Verificado contra `tools/list`:

```
erpnext_sales_order_create
  items[].required = ["item_code", "qty", "rate"]
  "Requires customer and at least one item with item_code, qty, rate"
```

No hay resolución de lista de precios del otro lado. `policy._precio_autorizado`
—que filtra Item Prices por `price_list`, `currency` **y** `uom`— no está en ese
camino. Esto **no se arregla con permisos**: es la forma del esquema. Es la
razón por la que estas herramientas van al agente de **gerencia** y nunca al de
clientes, donde del otro lado hay un desconocido escribiendo.

**2. El submit vive en dos categorías, y una de ellas es la que hace falta.**

- `operations`: `erpnext_doc_submit`, `erpnext_doc_cancel`, `erpnext_doc_delete`
- `sales`: `erpnext_sales_order_submit`, `_cancel`, `erpnext_sales_invoice_submit`

Sacar las dos categorías se lleva puestas las 17 herramientas de venta, o sea
también la de **crear el borrador**. Crear borrador y emitir están en la misma
categoría por construcción. Por eso el filtro de este repo es **por nombre**
(`MCP_EXTERNOS_BLOQUEAR=*_submit,*_cancel,*_delete`) y corre de este lado: lo
que matchea no se carga, no llega al modelo y no existe en su catálogo.

**3. Una clave, sin identidades.** Un servidor MCP de terceros actúa con UNA
credencial de ERPNext: la que tiene en su propio entorno. No hay permisos por
herramienta. La pregunta operativa no es «¿qué herramientas cargué?» sino
**«¿con qué usuario de ERPNext arrancó ese contenedor?»** — y esa decisión vive
fuera del alcance de `app/readiness.py`.

**4. Su HTTP pide un protocolo que el SDK de Python todavía no habla.** El modo
HTTP de Casys 3.0.4 exige `MCP-Protocol-Version: 2026-07-28` y devuelve **400**
a un cliente que manda `2025-06-18`. El SDK `mcp` 1.30.0 de Python habla el
viejo. **Por HTTP no conectan**; por **stdio** sí, porque ahí Casys negocia
hacia abajo. Nuestro servidor habla `2025-06-18`, que es lo que hablan los
clientes de hoy.

**5. Los visores no sirven por WhatsApp.** Los nueve visores interactivos
(kanban, P&L, funnel, KPI) son MCP Apps y se renderizan en un *host* que los
soporte: Claude Desktop, Claude Code, VS Code. Por WhatsApp llega texto. Son una
herramienta de escritorio para el dueño, no una función del producto.

---

## Configuración

```ini
# Un servidor por entrada. Un comando = stdio; una URL = streamable_http.
MCP_EXTERNOS=erpnext=npx -y @casys/mcp-erpnext --categories=sales,inventory,delivery,crm,accounting,analytics

# Nombres que NO se cargan. Acepta comodines. Vacío = no se bloquea nada.
MCP_EXTERNOS_BLOQUEAR=*_submit,*_cancel,*_delete,erpnext_method_call
```

`TOOLS_GERENCIA` **no se toca**: lo externo va a `TOOLS_AGENTE_GERENCIA`, que es
lo único que arma el agente. Si se sumara a la constante, `app/mcp_server.py`
—que la lee— pasaría a republicar el `erpnext_doc_submit` de un tercero por
**nuestra** puerta autenticada, con nuestro token y bajo nuestro nombre. Lo
afirma `tests/test_mcp_cliente.py::test_our_own_mcp_endpoint_never_republishes_a_third_party_tool`.

---

## Conectar un harness a NUESTRO servidor

**Claude Code / Claude Desktop** (`.mcp.json`):

```json
{
  "mcpServers": {
    "plus-agent": {
      "type": "http",
      "url": "https://TU-HOST/mcp",
      "headers": { "Authorization": "Bearer TU_TOKEN_DE_MCP_TOKENS" }
    }
  }
}
```

**stdio**, para correrlo local:

```bash
MCP_TOKEN=<uno de los de MCP_TOKENS> python -m app.mcp_server
```

**n8n**: nodo *MCP Client Tool*, transporte HTTP, URL `https://TU-HOST/mcp`,
header `Authorization: Bearer …`.

---

## Qué conviene, con los números arriba en la mano

- **Automatizaciones y workflows** → nuestro servidor. 19 herramientas
  compuestas que ya aplican los límites y el idioma del dueño. Un
  `informe(que="pendientes")` devuelve la respuesta; un `doc_list("Sales Order")`
  devuelve filas que el modelo todavía tiene que interpretar, y ahí es donde
  inventa un número.
- **El dueño explorando datos en Claude Desktop** → Casys, con `--categories`
  acotadas y una clave de ERPNext de sólo lectura. Los 17 informes y los visores
  valen mucho y no los vamos a escribir nosotros.
- **El camino del cliente** → ninguno de los dos. Ahí va el agente con sus
  herramientas angostas, por el punto 1.
