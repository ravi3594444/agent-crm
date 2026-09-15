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

**4. Su HTTP pide un protocolo que el SDK `mcp` 1.30.0 no habla — resuelto.**
El modo HTTP de Casys 3.0.4 exige `MCP-Protocol-Version: 2026-07-28` y devuelve
**400** a un cliente que manda `2025-06-18`, que es lo que habla el SDK `mcp`
1.30.0. Así que `app/mcp_cliente.py` **no usa el SDK**: habla el protocolo
directo con el `httpx` que ya estaba. El contrato completo, aprendido
preguntándole al servidor:

```
MCP-Protocol-Version: 2026-07-28      en toda petición
Mcp-Method: <el método del cuerpo>    en toda petición
Mcp-Name: <params.name>               sólo en tools/call
params._meta["io.modelcontextprotocol/protocolVersion"]
```

Se manda el **superconjunto** y no se negocia a mano: un servidor viejo ignora
los headers y las claves de `_meta` que no conoce —`_meta` está reservado para
eso— y uno nuevo los exige. Después del `initialize` se usa la versión que el
servidor devolvió. De paso desaparecen 12 paquetes de la imagen y el bucle de
eventos en un hilo de fondo.

El alcance exacto de lo medido, que no es «no hay otra forma»: el `httpx`
directo es lo único que PROBAMOS contra Casys 3.0.4, y anda; el 400 salió del
SDK `mcp` **1.30.0**. La v2 del SDK sí soporta `2026-07-28` y no la evaluamos —
volver a ella es volver a los 12 paquetes, así que para hacerlo hace falta una
razón y no sólo que sea posible—.

**5. Por stdio, `select()` no alcanza.** Un `select()` que dice «hay algo para
leer» prueba que hay UN byte, no una línea: un servidor que escribe `{"jsonrpc"`
y se queda callado dejaba a `readline()` esperando el `\n` para siempre, y
esperando con el candado del cliente tomado, así que se colgaba también todo lo
que viniera después para ese servidor. `app/mcp_cliente.py` lee bytes del
descriptor y arma las líneas de su lado, con una fecha límite que se calcula una
sola vez y vale para todos los pedazos —si se recalculara por pedazo, un
servidor que gotea la correría para siempre—.

**6. Los visores no sirven por WhatsApp.** Los nueve visores interactivos
(kanban, P&L, funnel, KPI) son MCP Apps y se renderizan en un *host* que los
soporte: Claude Desktop, Claude Code, VS Code. Por WhatsApp llega texto. Son una
herramienta de escritorio para el dueño, no una función del producto.

---

## Configuración

```ini
# Un servidor por entrada. Un comando = stdio; una URL = http.
MCP_EXTERNOS=erpnext=http://mcp-erpnext:3012/mcp
MCP_EXTERNO_TOKEN_ERPNEXT=<el MCP_AUTH_TOKEN de ese contenedor>

# Sólo si el `http://` de arriba apunta a una red tuya que no se reconoce sola
# (una VPN, un Kubernetes con dominio). Ver «El token del otro sistema» abajo.
MCP_EXTERNOS_HTTP_INTERNOS=

# NO saca poderes: saca MÓDULOS que este negocio no usa (RRHH, nómina, activos,
# manufactura, proyectos). 149 -> 108 herramientas, ~38.200 -> ~29.100 tokens
# por turno. Submit, cancel y los precios SIGUEN estando, que es lo que el dueño
# pidió. Ver .env.example para los dos dials que siguen.
MCP_EXTERNOS_BLOQUEAR=erpnext_asset*,erpnext_attendance*,erpnext_employee*,...

```

Y el contenedor, en `deploy/mcp-erpnext.compose.yml`:

```bash
cd /srv/agent-crm/plus-agent
docker compose -f docker-compose.yml -f ../deploy/mcp-erpnext.compose.yml up -d
```

### El token del otro sistema, y por qué `http://` no es gratis

`MCP_EXTERNO_TOKEN_<NOMBRE>` es la credencial de OTRO sistema y `http://` la
manda en texto plano. Con el despliegue de arriba eso no es un problema —el
contenedor de al lado, en la red de Docker, sin publicar al host— y por eso no
se exige TLS a secas: se exige que el destino **no salga a ninguna red**. Lo que
cuenta como eso, sin configurar nada: `localhost`, una IP privada o de loopback,
un nombre de una sola etiqueta (`mcp-erpnext`, que el DNS público no resuelve) y
los sufijos `.local` e `.internal`.

Cualquier otro `http://` **con token configurado** se rechaza al leer la
configuración, y el error dice cómo seguir: poner `https://`, o declarar esa red
como propia en `MCP_EXTERNOS_HTTP_INTERNOS=<nombre del servidor>` —nombres de
servidor, así que declarar uno no declara al de al lado—. Sin token no se
rechaza nada: no hay nada que filtrar, y ese caso ya lo avisa
`readiness.chequear_mcp_externos`. La regla corre en los dos lugares que
importan: al parsear `MCP_EXTERNOS` y otra vez al armar el header, porque un
`ClienteMCP` se puede construir sin pasar por el parser.

**La pregunta que importa no es qué herramientas carga, es con qué usuario.**
Ese servidor actúa con UNA credencial de ERPNext y no tiene permisos por
herramienta: lo que puede hacer lo decide `MCP_ERPNEXT_API_KEY` y nada más. Un
usuario de sólo lectura deja funcionando 90 de las 125 y ninguna que escriba.

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
