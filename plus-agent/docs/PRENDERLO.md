# Prenderlo, de punta a punta

Todo lo que sigue corre en la VM. Son cinco pasos y unos veinte minutos, de los
cuales los primeros cinco ya te dicen si algo está roto.

El orden importa: el paso 2 es el que hace que el 3 sirva de algo.

---

## 1. Traer el código

```
cd /srv/agent-crm && git pull
cd plus-agent && docker compose up -d --build agente
```

~15 segundos con la caché de capas. Después:

```
curl -s localhost:8081/health
```

---

## 2. La lista de precios y la moneda — ESTO PRIMERO

`AUTO_CONFIRM_PRICE_LIST` vacío es la trampa de todo el sistema: con eso en
blanco **la auto-confirmación no puede verificar ningún precio**, así que subir
`AUTO_CONFIRM_MAX` no cambia nada y no aparece ningún error en ninguna parte.
Lo mismo le pasa a `cambiar_precio`, que se niega a escribir.

Primero averiguá cómo se llama tu lista de venta en ERPNext:

```
docker compose --project-name erpnext -f ~/gitops/erpnext.yml exec backend \
  bench --site agentcrm4.duckdns.org execute frappe.client.get_list \
  --kwargs '{"doctype":"Price List","filters":{"selling":1},"fields":["name","currency"]}'
```

Y poné en `/srv/agent-crm/plus-agent/.env` **el nombre exacto** que salió:

```ini
AUTO_CONFIRM_PRICE_LIST=<el nombre exacto>
AUTO_CONFIRM_CURRENCY=ARS
```

---

## 3. Las herramientas de ERPNext (las 108 de Casys)

Sin esto el agente de gerencia tiene 24 herramientas. Con esto, 132 — y ahí
adentro están precios, submit y cancel.

```ini
MCP_EXTERNOS=erpnext=http://mcp-erpnext:3012/mcp
MCP_EXTERNO_TOKEN_ERPNEXT=<el MCP_AUTH_TOKEN de ese contenedor>
```

Y levantar el contenedor:

```
docker compose -f docker-compose.yml -f ../deploy/mcp-erpnext.compose.yml up -d
```

---

## 4. Lo automático

```ini
# Pedido más grande que se confirma solo. En 0 no se confirma nada solo.
AUTO_CONFIRM_MAX=<tu número, en pesos>

# Cuánto puede moverse un precio de una vez sin que nadie mire.
PRECIO_CAMBIO_MAX_PCT=15
```

Con los dos arriba de 0 el agente confirma pedidos y cambia precios sin
preguntarte. Lo que sigue mirando solo, en Python y sin consultarte: deuda
vencida, cliente nuevo, cantidad por producto, colchón de stock, y que el precio
del renglón sea el de la lista autorizada.

`STOCK_CONFIABLE=false` conviene dejarlo en false hasta que el stock inicial
esté cargado — si no, promete stock que no está contado.

### Cuánto puede costar UN mensaje

```ini
# Llamadas al modelo que puede hacer UN mensaje de WhatsApp, como techo.
PASOS_MAX_CLIENTES=8
PASOS_MAX_GERENCIA=14
```

**No hace falta tocarlos**, y están acá porque son lo que acota la cuenta del
modelo. Sin techo, un modelo que entra en bucle hace miles de llamadas por un
solo mensaje: el default de LangGraph son 10007 superpasos, y medido contra el
agente real dio 121 llamadas al modelo en un turno sin que nada lo frenara. Con
la clave de Gemini en free tier —7 a 34 s por llamada— eso es la cuota del día.

Un pedido bien atendido son tres o cuatro vueltas, así que 8 sobra. Cuando se
llega al techo el agente NO se disculpa: contesta con lo que alcanzó a
averiguar. En el log se ve como `[agent] techo de pasos rol=… llamadas=…`.

Si alguna vez ves esa línea seguido, el problema no es el techo: es que el
modelo está dando vueltas, y subirlo sólo hace más cara la misma vuelta.

**Después de tocar el `.env`:**

```
docker compose up -d --force-recreate agente
```

`restart` NO relee el `.env`. Tiene que ser `--force-recreate`.

---

## 5. Comprobarlo

```
docker compose exec agente python -m app.readiness
```

Es el preflight completo y contra el sistema de verdad: las tres credenciales
separadas, Meta, ERPNext, el depósito, los límites, los servidores MCP
externos y si la memoria cruza al agente de clientes. Si algo de lo de arriba
quedó a medias, sale acá con el nombre de la variable.

Y después, por WhatsApp:

| Desde | Mandá | Tiene que |
|---|---|---|
| un número de cliente | `¿repartís en <tu barrio>?` | contestar con tus zonas reales |
| un número de cliente | `¿puedo pasar a buscarlo?` | contestar con el retiro que configuraste |
| tu número (dueño) | `los jueves salgo antes, contáselo a los clientes` | confirmarte que lo anotó Y que es público |
| un número de cliente | `¿hasta qué hora puedo pedir?` | repetir lo que le dijiste al de gerencia |
| tu número (dueño) | `poné la leche entera en 1850` | cambiarlo y contestar «releído y confirmado» |
| tu número (dueño) | `poné la leche entera en 18500` | negarse y decirte el porcentaje y la banda |

Las dos últimas son las que prueban que la banda está haciendo su trabajo: una
pasa y la otra no, sin que confirmes nada en ninguna de las dos.

---

## Si algo no anda

```
docker compose logs -t agente | tail -30
```

La línea `modelo=1x9.4s herramientas=0x0.0s cola=0.0s total=9.4s` dice dónde se
fue el tiempo antes de que empieces a adivinar.
