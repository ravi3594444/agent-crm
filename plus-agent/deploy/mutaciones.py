#!/usr/bin/env python
"""¿Hay UN test que falle si rompo cada guardia a mano?

Un test verde no prueba nada por sí solo: prueba que el código actual pasa. Lo
que importa es lo contrario — que si alguien saca una protección, algo grite.
Esto rompe cada guardia de la política de auto-confirmación, una por vez, corre
la suite, y espera que falle. Una mutación que NO se detecta es una protección
sin test: o sobra, o el test que la cuida no existe.

    python deploy/mutaciones.py            # todas
    python deploy/mutaciones.py stock      # sólo las que dicen "stock"

Los archivos se restauran siempre, incluso si algo explota (ver `finally`).
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PY = sys.executable

# (etiqueta) -> (archivo, texto original, con qué se reemplaza)
MUTACIONES: dict[str, tuple[str, str, str]] = {
    # ---------------- 2a: stock comprometido en otros borradores ----------
    "2a stock: la resta no hace nada": (
        "app/policy.py",
        """    disponible -= _comprometido_en_borradores(
        item_code, warehouse, excluir=excluir, company=company, desde=desde
    )""",
        "    disponible -= 0.0",
    ),
    "2a stock: el pedido se resta a sí mismo": (
        "app/policy.py",
        "\n        if nombre != excluir\n    ]",
        "\n    ]",
    ),
    "2a stock: no mira el estado del padre": (
        "app/policy.py",
        '        if sin_reserva(fila.get("status")):\n            continue\n',
        "",
    ),
    "2a stock: no mira el docstatus del padre": (
        "app/policy.py",
        "        if not _es_borrador(fila):\n            continue\n        if company",
        "        if company",
    ),
    "2a stock: no mira la compañía": (
        "app/policy.py",
        '        if company and str(fila.get("company") or "").strip() != company:\n            continue\n',
        "",
    ),
    "2a stock: sin regla de quién pidió primero": (
        "app/policy.py",
        """        if (
            desde
            and propio
            and not _reclamo_previo(fila.get("creation"), nombre, desde, propio)
        ):
            continue
""",
        "",
    ),
    "2a stock: FIFO sin el número de pedido": (
        "app/policy.py",
        "    return (datetime.fromisoformat(str(creacion)), nombre)",
        '    return (datetime.fromisoformat(str(creacion)), "")',
    ),
    "2a stock: sin tope de borradores": (
        "app/policy.py",
        '''    if len(padres) > MAX_BORRADORES:
        raise erpnext.ERPNextError(
            "demasiados pedidos en borrador para verificar el stock"
        )
''',
        "",
    ),
    "2a stock: sin tope de renglones": (
        "app/policy.py",
        '''        if len(filas) > tope:
            raise erpnext.ERPNextError(
                f"demasiados renglones de {item_code} para verificar el stock"
            )
''',
        "",
    ),
    "2a stock: sin filtro de estado en la consulta": (
        "app/policy.py",
        '        ["status", "not in", list(ESTADOS_SIN_RESERVA)],\n',
        "",
    ),
    "2a stock: sin cuantizar el disponible": (
        "app/policy.py",
        "    disponible = round(disponible, 6)\n",
        "",
    ),
    "2a stock: los nombres no van en lotes": (
        "app/policy.py",
        "LOTE_BORRADORES = 50",
        "LOTE_BORRADORES = 10_000",
    ),
    "2a stock: cantidad no convertida a unidad de stock": (
        "app/policy.py",
        "            qty = _cantidad_en_stock_uom(item)",
        '            qty = _float(item.get("qty"))',
    ),
    "2a cliente: una respuesta sin lista se lee como cero filas": (
        "app/erpnext.py",
        '    data = body.get("data")\n    # An answer with no list in it is NOT "zero rows"',
        '    data = body.get("data") or []\n    # An answer with no list in it is NOT "zero rows"',
    ),
    "2a rechazo: cierra un pedido ya confirmado": (
        "app/decisiones.py",
        '''        actual = _leer_doc("Sales Order", nombre)
        if int(actual.get("docstatus") or 0) != 0:
            print(f"[decisiones] {nombre}: no lo cierro, ya no es un borrador")
            return False
''',
        "",
    ),
    "2a aprobación: somete un pedido rechazado": (
        "app/aprobacion.py",
        '        if not ya_confirmado and policy.sin_reserva(actual.get("status")):',
        "        if False:",
    ),
    "2a.1 catálogo: el nivel ignora los borradores": (
        "app/tools/catalogo.py",
        "        available -= policy.comprometido_en_borradores(item_code, warehouse)",
        "        available -= 0.0",
    ),
    # ---------------- 2b: los límites del dueño ---------------------------
    "2b límites: se confía sin validar": (
        "app/policy.py",
        """    try:
        cfg = limites.configuracion()
    except limites.LimiteError as exc:""",
        """    try:
        cfg = limites.configuracion()
    except ZeroDivisionError as exc:""",
    ),
    "2b límites: sin tope por producto": (
        "app/policy.py",
        '''        if qty > cfg.tope_qty_por_producto:
            motivos.append(
                f"{qty:g} de {code} supera el máximo de "
                f"{cfg.tope_qty_por_producto:g} por producto"
            )
''',
        "",
    ),
    "2b límites: sin configurar = sin límite": (
        "app/policy.py",
        '''        if cfg.tope_qty_por_producto <= 0:
            motivos.append(
                "falta configurar la cantidad máxima por producto"
            )
            break
''',
        "        if cfg.tope_qty_por_producto <= 0:\n            continue\n",
    ),
    "2b límites: ignora el tope de cliente nuevo": (
        "app/policy.py",
        """                elif cfg.tope_cliente_nuevo <= 0:
                    motivos.append(
                        f"cliente con solo {len(importes)} pedidos confirmados"
                    )
                elif total > cfg.tope_cliente_nuevo:
                    motivos.append(
                        f"cliente nuevo: {pesos(total)} supera su tope de "
                        f"{pesos(cfg.tope_cliente_nuevo)}"
                    )""",
        """                elif True:
                    motivos.append(
                        f"cliente con solo {len(importes)} pedidos confirmados"
                    )""",
    ),
    "2b descuentos: no mira el del pedido": (
        "app/policy.py",
        "    if cfg.descuentos_aprueban:\n        for field_name in (",
        "    if False:\n        for field_name in (",
    ),
    "2b descuentos: no mira el del renglón": (
        "app/policy.py",
        '''        if cfg.descuentos_aprueban:
            try:
                if any(
                    not _zero(item.get(campo))
                    for campo in (
                        "discount_percentage",
                        "discount_amount",
                        "distributed_discount_amount",
                    )
                ):
                    motivos.append(f"descuento en {code} requiere aprobación")
            except erpnext.ERPNextError:
                motivos.append(f"descuento inválido en {code}")
''',
        "",
    ),
    "2b precio: con descuento acepta arriba de la lista": (
        "app/policy.py",
        "    if permitir_descuento:\n        if rate > list_rate + 0.01:\n            return False",
        "    if permitir_descuento:\n        pass",
    ),
    "2b almacén: si Redis se cae, cae al entorno": (
        "app/limites.py",
        """    except (locks.CoordinationError, RedisError) as exc:
        raise LimiteError("no pude leer los límites configurados") from exc""",
        "    except (locks.CoordinationError, RedisError):\n        return {}",
    ),
    "2b autorización: no mira la lista del equipo": (
        "app/runtime_context.py",
        '    if not router.es_equipo(actor.actor_phone):\n        raise RuntimeContextError("teléfono no autorizado")\n',
        "",
    ),
    "2b confirmación: no mira el código": (
        "app/limites.py",
        '''    if str(propuesta.get("codigo")) != str(codigo or "").strip():
        raise LimiteError("ese código no es el del cambio pendiente")
''',
        "",
    ),
    "2b validación: sin tope superior": (
        "app/limites.py",
        '''    if valor > defi.maximo:
        raise LimiteError(
            f"«{defi.alias[0]}» {valor:g} es imposible: el máximo es "
            f"{defi.maximo:g} {defi.unidad}".strip()
        )
''',
        "",
    ),
    "2b validación: acepta negativos": (
        "app/limites.py",
        '''    if valor < defi.minimo:
        raise LimiteError(
            f"«{defi.alias[0]}» no puede ser menor que {defi.minimo:g}"
        )
''',
        "",
    ),
    "2b auditoría: no se escribe": (
        "app/limites.py",
        "        cliente.rpush(CLAVE_AUDITORIA, json.dumps(entrada, ensure_ascii=False))\n",
        "",
    ),
    "2b auditoría: crece para siempre": (
        "app/limites.py",
        "        cliente.ltrim(CLAVE_AUDITORIA, -AUDITORIA_MAXIMA, -1)\n",
        "",
    ),
    "2b resolución: el entorno le gana al dueño": (
        "app/limites.py",
        '''    fijado = almacen.get(nombre, "").strip()
    if fijado:
        return fijado, "dueño"
''',
        "",
    ),
    # ---------------- 2b.1: correcciones ---------------------------------
    "2b.1 descuentos: sin tope de porcentaje": (
        "app/policy.py",
        '''            if efectivo > cfg.tope_descuento_pct + 0.000001:
                motivos.append(
                    f"descuento de {efectivo * 100:.2f}% supera el tope de "
                    f"{cfg.tope_descuento_pct * 100:g}%"
                )
''',
        "            pass\n",
    ),
    "2b.1 descuentos: el tope tolera cualquier cosa": (
        "app/policy.py",
        "if efectivo > cfg.tope_descuento_pct + 0.000001:",
        "if efectivo > cfg.tope_descuento_pct + 1.0:",
    ),
    "2b.1 descuentos: no suma el del pedido": (
        "app/policy.py",
        "        peor = max(peor, 1.0 - (1.0 - linea) * (1.0 - doc))",
        "        peor = max(peor, linea)",
    ),
    "2b.1 descuentos: mira el mejor renglón, no el peor": (
        "app/policy.py",
        '    peor = 0.0\n    for item in items:\n        lista = _float(item.get("price_list_rate"))',
        '    peor = 1.0\n    for item in items:\n        lista = _float(item.get("price_list_rate"))',
    ),
    "2b.1 descuentos: sin precio de lista = sin descuento": (
        "app/policy.py",
        '''        if lista <= 0:
            raise erpnext.ERPNextError(
                "renglón sin precio de lista: no puedo medir su descuento"
            )
''',
        "        if lista <= 0:\n            continue\n",
    ),
    "2b.1 cliente nuevo: se habilita antes de verificar la dirección": (
        "app/policy.py",
        "CLIENTE_NUEVO_HABILITADO = False",
        "CLIENTE_NUEVO_HABILITADO = True",
    ),
    "2b.1 durable: no se registra en ERPNext": (
        "app/limites.py",
        "    _auditar_en_erpnext(entrada)\n",
        "",
    ),
    "2b.1 durable: no detecta que se perdió el almacén": (
        "app/limites.py",
        '''    if not valores and _hubo_cambios_durables():
        raise LimiteError(
            "los límites que configuró el dueño no están en el almacén, y "
            "ERPNext tiene cambios registrados: hay que restaurarlos antes de "
            "que algo se confirme solo"
        )
''',
        "",
    ),
    "2b.1 durable: si no puede registrar, aplica igual": (
        "app/limites.py",
        """    except erpnext.ERPNextError as exc:
        raise LimiteError(
            "no pude registrar el cambio en ERPNext, así que no lo apliqué"
        ) from exc""",
        "    except erpnext.ERPNextError:\n        pass",
    ),
    # ---------------- 2c: la confianza se gana y se vence ----------------
    "2c conteo: la confianza no vence": (
        "app/inventario.py",
        '''    if ahora - momento > timedelta(hours=horas):
        antiguedad = (ahora - momento).total_seconds() / 3600.0
        return False, (
            f"el último conteo de {item_code} es de hace {antiguedad:.0f} h "
            f"(vale {horas:g} h)"
        )
''',
        "",
    ),
    "2c conteo: un borrador cuenta": (
        "app/inventario.py",
        '            ["docstatus", "=", 1],\n        ],\n        fields=["parent", "item_code", "warehouse", "docstatus"],',
        '            ["docstatus", "=", 0],\n        ],\n        fields=["parent", "item_code", "warehouse", "docstatus"],',
    ),
    "2c conteo: no revisa el docstatus del documento": (
        "app/inventario.py",
        '        if int(float(conteo.get("docstatus") or 0)) != 1:\n            continue\n',
        "",
    ),
    "2c conteo: sin conteos está bien": (
        "app/inventario.py",
        '    if momento is None:\n        return False, f"nadie confirmó un conteo de {item_code}"\n',
        "",
    ),
    "2c conteo: ignora el interruptor maestro": (
        "app/inventario.py",
        '    if not maestra_encendida():\n        return False, "el inventario está marcado como no confiable"\n',
        "",
    ),
    "2c conteo: acepta un conteo del futuro": (
        "app/inventario.py",
        '    if momento > ahora:\n        return False, f"el último conteo de {item_code} dice ser del futuro"\n',
        "",
    ),
    "2c conteo: ventana ilegible = default": (
        "app/inventario.py",
        "    return horas if horas > 0 else 0.0",
        "    return horas if horas > 0 else 24.0",
    ),
    "2c conteo: si no puede leer, confía": (
        "app/inventario.py",
        '''    except erpnext.ERPNextError as exc:
        print(f"[inventario] no pude leer conteos de {item_code}: {exc}")
        return False, f"no pude verificar el último conteo de {item_code}"''',
        '    except erpnext.ERPNextError:\n        return True, ""',
    ),
    "2c política: ignora si el conteo está fresco": (
        "app/policy.py",
        '''            fresco, sin_confianza = inventario.confiable(code, warehouse)
            if not fresco:
                motivos.append(sin_confianza or f"stock de {code} sin verificar")
                continue
''',
        "",
    ),
    "2c catálogo: le promete al cliente sin conteo": (
        "app/tools/catalogo.py",
        '''    fresco, sin_confianza = inventario.confiable(item_code, warehouse)
    if not fresco:
        return (
            f"{item_code}: {sin_confianza}. No confirmes disponibilidad; "
            "el pedido solo puede quedar pendiente de revisión."
        )
''',
        "",
    ),
    "2c conteo: el bot lo somete solo": (
        "app/tools/captura.py",
        '''    pedido = notificar.pedir_confirmacion_conteo(
        actor.actor_phone,
        doc["name"],
        f"{resumen}\\n\\n¿Confirmo el ajuste?",
    )''',
        '    erpnext.submit_doc("Stock Reconciliation", doc["name"])\n    pedido = True',
    ),
    "2c conteo: cualquiera puede cargar uno": (
        "app/tools/captura.py",
        '''    try:
        actor = require_management(config)
    except RuntimeContextError:
        return "No pude autenticar quién cuenta; no cargué el conteo."
''',
        "    actor = actor_context(config)\n",
    ),
    "2c conteo: se confirma dos veces": (
        "app/decisiones.py",
        '    if estado == 1:\n        return {"ok": True, "detalle": f"El conteo {nombre} ya estaba confirmado."}\n',
        "",
    ),
}


def main() -> int:
    filtro = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    elegidas = {k: v for k, v in MUTACIONES.items() if filtro in k.lower()}
    if not elegidas:
        print(f"ninguna mutación coincide con {filtro!r}")
        return 2

    originales = {f: (RAIZ / f).read_text(encoding="utf-8") for f, _, _ in elegidas.values()}
    sin_detectar: list[str] = []
    try:
        for etiqueta, (archivo, viejo, nuevo) in elegidas.items():
            ruta = RAIZ / archivo
            fuente = originales[archivo]
            if fuente.count(viejo) != 1:
                print(f"{etiqueta:52} ANCLA x{fuente.count(viejo)} — revisar")
                sin_detectar.append(etiqueta)
                continue
            ruta.write_text(fuente.replace(viejo, nuevo), encoding="utf-8")
            salida = subprocess.run(
                [PY, "-m", "pytest", "-q", "--no-header", "--tb=no", "-p", "no:warnings"],
                cwd=RAIZ, capture_output=True, text=True,
            ).stdout
            ruta.write_text(fuente, encoding="utf-8")
            resumen = [
                linea for linea in salida.splitlines()
                if re.search(r"\d+ (passed|failed|error)", linea)
            ]
            cola = resumen[-1] if resumen else "SIN RESUMEN"
            detectada = ("failed" in cola) or ("error" in cola)
            print(f"{etiqueta:52} {'la agarra ' if detectada else 'NO LA AGARRA'} {cola[:34]}")
            if not detectada:
                sin_detectar.append(etiqueta)
    finally:
        for archivo, fuente in originales.items():
            (RAIZ / archivo).write_text(fuente, encoding="utf-8")

    print()
    if sin_detectar:
        print(f"SIN TEST QUE LAS AGARRE ({len(sin_detectar)}):")
        for etiqueta in sin_detectar:
            print(f"  · {etiqueta}")
        return 1
    print(f"las {len(elegidas)} mutaciones se detectan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
