# Banco de pruebas

Corre los escenarios de release contra **la imagen real del agente**, sin
tocar ERPNext, ni Meta, ni WhatsApp, ni el Redis de staging.

```bash
make demo          # 14 escenarios, determinístico, sin ninguna red
make demo-gemini   # los mismos, con Gemini de verdad (consume cuota)
```

La transcripción queda en `demo-resultados/` (markdown para leer, JSON para
comparar entre corridas).

## Por qué la garantía es la red y no la configuración

```
docker network create --internal plus-demo-net
   |
   +-- plus-demo-redis       Redis Stack 7.4.0-v1, base 0
   +-- plus-demo-servicios   los dobles: ERPNext, Graph de Meta, el modelo
   +-- plus-demo-agente      LA IMAGEN REAL, sin parches y sin .env
   +-- plus-demo-relevo      sólo en modo gemini; el único con salida
```

Una red `--internal` de Docker no tiene ruta a internet **ni al host**. Desde
el contenedor del agente no se llega a `graph.facebook.com`, ni a Google, ni
al ERPNext de staging de esta máquina en `:8080`, ni a su Redis en `:6379`. No
es una promesa del `.env`: `demo/guardas.py::verificar_aislamiento` abre
sockets de verdad hacia cada uno de esos destinos desde adentro del contenedor
y **el banco de pruebas no arranca si alguno responde**.

La imagen tampoco lleva el `.env` real —el `Dockerfile` copia sólo `app/` y el
`.dockerignore` excluye `.env`— así que el `load_dotenv()` de `app/__init__.py`
no encuentra nada y no puede heredar una credencial de producción por
descuido.

Encima de eso hay una capa de guardas de configuración, que existe por el
mensaje de error: si algo apunta a un servicio real, conviene enterarse con el
nombre de la variable y no con un timeout raro diez minutos después.

Ninguna guarda imprime un valor: comparan y cuentan.

## Los dos modos

| | `--modo offline` | `--modo gemini` |
|---|---|---|
| Quién elige las herramientas | un guión escrito a mano (`demo/escenarios.py`) | Gemini de verdad |
| Red | ninguna, en ningún contenedor | sólo el relevo tiene salida |
| Determinístico | sí: la misma corrida da lo mismo siempre | no |
| Para qué sirve | validar el SISTEMA: si un escenario falla, falló el código | validar el MODELO: si elige bien las herramientas |

### Qué se exige en cada modo

Un escenario declara texto esperado (`espera`), texto prohibido (`prohibe`) y
estado de documentos (`documentos`). No pesan igual en los dos modos:

- **`documentos` y las disculpas técnicas fallan siempre.** Que el pedido
  quede en `docstatus` 0 o 1, que aparezca el remito, que el agente no
  conteste "tuve un problema técnico": nada de eso depende de cómo redactó
  nadie.
- **`prohibe` falla siempre.** Que el modelo prometa un descuento o diga
  "confirmado" cuando no lo está es exactamente lo que hay que cazar.
- **`espera` sólo falla en modo offline.** Contra un guión el texto es exacto;
  contra un modelo libre, "tengo leche entera" es una respuesta correcta que
  no contiene la cadena `LECHE-ENT-1L`. En modo gemini esas diferencias se
  informan como `nota:` y no cuentan como falla.

El modo offline no usa un modelo falso *dentro* de la app: sirve el protocolo
de OpenAI en `https://plus-demo-servicios:8444/v1/`, y el agente le habla con
su `ChatGemini` de producción, sin saber que del otro lado hay un guión. El
proveedor sigue siendo `gemini`: no se cambia nada de `app/modelos.py`.

En modo gemini el agente **sigue sin salida a internet**. Le habla al relevo,
que reenvía la request a Google byte por byte —eso importa: la firma de
razonamiento de Gemini viaja en `extra_content.google.thought_signature` y un
relevo que "normalizara" el JSON esconderría justo lo que `ChatGemini`
arregla— y le devuelve la respuesta tal cual.

**La clave real vive sólo en el relevo.** El contenedor del agente lleva una
de mentira y no la necesita. El relevo, que es el único con salida, no lleva
ninguna credencial de ERPNext ni de WhatsApp ni dato del negocio, y eso se
verifica antes de arrancarlo (`guardas.relevo_sin_credenciales`).

## Cómo entra un mensaje

Igual que en producción: un `POST /webhook/whatsapp` firmado con
`X-Hub-Signature-256` sobre los bytes exactos del cuerpo. No hay atajos por
`_generate_response` ni por el grafo: se ejercitan la firma, la cola durable
de Redis, el worker con lease, la idempotencia y los dos envíos por turno (el
acuse y la respuesta).

Cada turno se mide y se guarda: lo que dijo la persona, lo que recibió, cuánto
tardó y **qué documentos cambiaron** en ERPNext (una foto antes y después).

## Los dobles

`demo/falso_erpnext.py` no es un ERPNext: es el subconjunto de la REST de
Frappe que este sistema toca. Imita a propósito las cosas de las que depende
el código y que un doble ingenuo rompe en silencio:

- los filtros de Frappe, incluido el `like` con `%` interleaved con el que
  `app/clientes.py` busca un teléfono
- las tablas hijas, que no se listan sin `parent` y cuyo `docstatus` sigue al
  del padre (`app/inventario.py` sólo le cree a un conteo confirmado)
- `order_by` + `limit_start` estables, porque la reconstrucción del índice de
  solicitudes pagina comentarios por `creation desc`
- que un `PUT` sea un **save**: se recalculan los totales y se devuelve el
  documento completo
- que una línea sin `rate` se valorice con la lista de precios, y que lleve el
  `stock_uom` del Item — sin eso `app/policy.py` no aprueba nada nunca
- **los tres permisos**: la credencial de agente y la de gerencia no pueden
  llevar un documento a `docstatus` 1 ni 2. Sólo la de política. Es la
  frontera que el sistema real delega en ERPNext, y si el doble no la tuviera
  el banco de pruebas no probaría nada sobre permisos.

`demo/falso_meta.py` guarda cada envío en un buzón en vez de mandarlo, y sabe
fallar a pedido con la forma real de un error de Meta (código en el cuerpo,
`x-fb-request-id`, `retry-after`) para ejercitar la clasificación
permanente/transitorio.

El banco de pruebas se prueba: `tests/test_demo.py` verifica cada una de esas
promesas, y que las guardas frenen de verdad. Un doble que miente es peor que
no tener doble.

## Los datos

Una distribuidora de lácteos inventada (`demo/datos.py`): seis productos, dos
clientes, stock, un conteo confirmado de hoy, una factura vencida y tres
pedidos de historial. Los teléfonos usan cuerpos obviamente falsos
(`5493511110001`) y los dominios son `.invalid`, que por RFC 2606 no resuelve.

Nada envejece mal: las fechas se calculan al sembrar, porque el conteo de
stock tiene que caer dentro de `STOCK_CONFIABLE_HORAS` para que el sistema
considere el stock confiable.

## Agregar un escenario

En `demo/escenarios.py`: un `Escenario` con sus `Paso`, y en `porque` **qué
prueba** — un escenario que no dice qué prueba no se puede evaluar cuando
falla. Si el paso lo escribe un cliente, agregá la regla del guión para que el
modo offline sepa qué herramienta llamar; los comandos de gerencia y el
`acepto` del cliente los resuelve `app/main.py` sin modelo y no la necesitan.
`tests/test_demo.py` verifica que ningún mensaje de cliente quede sin regla.

Usá `ULTIMO_PEDIDO` donde vaya un número de pedido: no se puede escribir uno
que todavía no existe.

## Límites conocidos

- **El modo gemini no cabe en el tier gratuito.** Son 20 pedidos por día y por
  modelo (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`), y un turno de
  cliente gasta dos o más. Los 14 escenarios necesitan ~80. Con `--modelo` se
  puede usar otro modelo de Gemini, que tiene su propia cuota diaria, para
  probar un subconjunto.
- El reloj no se controla: los escenarios que dependen de un vencimiento
  esperan de verdad. Por eso no hay uno de "la solicitud venció"; lo cubre la
  suite unitaria.
- `demo/datos.py` siembra por HTTP contra el doble, no por `deploy/seed_dairy.py`.
