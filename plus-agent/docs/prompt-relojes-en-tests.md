# Session opener — el reloj del lado de los tests (mitad restante del hallazgo 2)

Fresh session in `plus-agent/` of `agent-crm`. Tu tarea es la mitad de `tests/` del issue **#22**:
https://github.com/ravi3594444/agent-crm/issues/22

**Primer commit:** guardá este mensaje como `docs/prompt-relojes-en-tests.md` en una rama nueva desde `main`, y pushealo.

**Prerrequisito: cumplido.** #26 ya está en `main` (`81492a0`), así que `app/reloj.py` existe y la mitad de `app/` está hecha. Esto es la otra mitad y se puede arrancar ya.

## El estado

Seis archivos de test definen su propio reloj:

| archivo | lo que define por su cuenta |
|---|---|
| `tests/test_pendientes.py` | `ZONA` y `epoch()` |
| `tests/test_inventario.py` | `ZONA` **y** `AHORA` |
| `tests/test_autonomia.py` | `AHORA` |
| `tests/test_digest.py` | `HOY` |
| `tests/test_fechas_entrega.py` | `HOY` |
| `tests/test_policy_reglas.py` | `HOY` |

Más 16 apariciones de `"America/Argentina/Buenos_Aires"` escritas a mano en 10 archivos de `tests/`.

| | |
|---|---|
| copias | 6 archivos |
| divergencias | 2 (#12, y el bug de zona de #16 invisible a los tests que tocaban ese campo) |
| costo | ~1 medio día — **el más barato de los cinco hallazgos** |

## Por qué importa, y es más fuerte que «es repetitivo»

Los seis **re-derivaron la suposición del código**, así que ninguno puede discreparle. `epoch()` arma sus momentos con Buenos Aires escrito a mano; `test_inventario.py` compone su sello naive a partir de su propio `AHORA` en la misma zona. Cuando el código interpretaba mal un sello de ERPNext, los tests lo interpretaban mal igual, y todos pasaban.

Los fixtures duplicados no sólo encarecen el cambio: **hacen clases enteras de bug infalsificables.** Un test que no puede estar en desacuerdo con el código sobre un concepto no está probando ese concepto.

## La forma ya está demostrada

`tests/test_reloj.py::test_el_dia_es_el_del_negocio_y_no_el_del_servidor` (en #26) usa **un instante y tres zonas** con `time_machine.travel`, y afirma que la fecha del negocio se mueve con `BUSINESS_TIMEZONE`. Ése es el patrón: el test **nombra** el momento y la zona en vez de heredarlos.

Y `tests/test_order_safety.py::test_date_parser_uses_business_timezone_and_never_defaults_missing_date` es el antes/después de un test que pinaba la implementación: parchaba `pedidos.datetime` y comparaba el nombre del default. Leé los dos antes de empezar.

## Lo que este brief NO decide, y vos sí

1. **Qué forma tiene el fixture compartido.** Un fixture de `conftest.py` que dé el momento y la zona; si conviene que sea autouse o pedido; si `epoch()` se generaliza o se queda. No hay una respuesta obvia y el diseño es tuyo.
2. **Cuáles de las 16 apariciones literales se quedan.** Un test que nombra su zona **a propósito**, para probar que el código la respeta, es correcto y tiene que quedarse — `test_reloj.py` lo hace tres veces en un solo test. Lo que se va es la zona heredada como decorado. Un renglón por archivo en el PR diciendo cuál es cuál.
3. **Si algún archivo gana un test que antes no podía escribir.** `test_inventario.py` es el candidato claro: hoy no puede pedirle a ERPNext una zona distinta de la del negocio, que es justo el caso de #16 y del `_momento` de `inventario`. Si aparece uno, es el mejor entregable de este PR.

## Bordes

- **No cambies lo que un test afirma.** Esto es consolidación de fixtures: si un test pasa a probar otra cosa, el PR se volvió otra cosa.
- `BUSINESS_TIMEZONE` y `DIGEST_ACTIVO` siguen **fijas** en `_FIJAS` de `tests/conftest.py`, y eso está bien: ahí fijar **es** el arreglo (`epoch()` tiene Buenos Aires escrito a mano y el reloj del negocio tiene que ser el mismo). El idioma se sacó de esa lista por lo contrario; no confundas los dos casos, está explicado al lado de la lista.
- `time-machine` ya está declarado en `requirements-dev.txt` desde #26. No lo instales a mano.

## Done when

- `ruff check app demo tests`; `pytest -q` (Redis Stack en `REDIS_URL`, db 0) con el **mismo total** que antes: consolidar fixtures no agrega ni saca tests, salvo los nuevos que declares como nuevos.
- Los seis archivos usan el fixture compartido.
- Un renglón por cada aparición literal que se quedó, con el motivo.
- Y la prueba de que sirvió: **un test que antes no se podía escribir**, o la explicación de por qué no apareció ninguno.
