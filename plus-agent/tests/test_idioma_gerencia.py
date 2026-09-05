"""El idioma del equipo se cambia por el MISMO camino de dos pasos que un límite.

Y por ninguno otro. Lo que se prueba acá:

  * Un número verificado del equipo propone el cambio y lo confirma con el
    código de cuatro dígitos que le llega aparte. El modelo nunca ve ese código.
  * Un cliente no puede cambiarlo. No es que le salga mal: no llega al camino.
  * El mismo pedido dos veces es UN pedido, con el MISMO código, y el código se
    usa una sola vez.
  * Los mensajes de Python salen en el idioma vigente, y los datos que llevan
    adentro —el código, el número de pedido, la fecha— no se tocan al traducir.
  * Perder Redis no deja al sistema mudo: se vuelve al idioma por defecto.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conftest import FakeRedis

from app import idioma, limites, locks, main, whatsapp
from app.tools import configuracion

EQUIPO = "5493511111111"
CLIENTE = "5493510000000"


def _gerencia(telefono: str = EQUIPO) -> dict:
    return {
        "configurable": {
            "thread_id": "ger:thread",
            "actor_scope": "management",
            "actor_phone": telefono,
            "inbound_message_id": "wamid.staff-idioma",
        }
    }


def _cliente() -> dict:
    return {
        "configurable": {
            "thread_id": "cli:thread",
            "actor_scope": "customer",
            "customer_code": "CUST-001",
            "actor_phone": CLIENTE,
            "inbound_message_id": "wamid.cli-idioma",
        }
    }


@pytest.fixture
def almacen(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    """Almacén en memoria + captura de lo que Python le manda al dueño."""
    from app import router

    falso = FakeRedis()
    falso.enviados = []
    monkeypatch.setattr(locks, "conexion", lambda: falso)
    monkeypatch.setattr(router, "STAFF", [EQUIPO])
    monkeypatch.setattr(router, "es_equipo", lambda t: t == EQUIPO)
    monkeypatch.setattr(main, "es_equipo", lambda t: t == EQUIPO)
    monkeypatch.setattr(limites, "_codigo", lambda: "4242")
    monkeypatch.setattr(
        whatsapp,
        "enviar_mensaje",
        Mock(side_effect=lambda tel, texto: falso.enviados.append((tel, texto))),
    )
    # La copia durable vive en ERPNext; acá se anota en memoria.
    escritos: list[str] = []
    falso.comentarios = escritos
    monkeypatch.setattr(
        limites.erpnext, "registrar_comentario", lambda dt, n, texto: escritos.append(texto)
    )
    monkeypatch.setattr(limites.erpnext, "default_company", lambda: "xyz")
    return falso


def _proponer(dicho: str, valor: str, telefono: str = EQUIPO) -> str:
    return configuracion.proponer_limite.func(
        limite=dicho, valor=valor, config=_gerencia(telefono)
    )


def _confirmar(codigo: str = "4242", telefono: str = EQUIPO):
    return main._codigo_de_ajuste(codigo, telefono)


# ------------------------------------------------------ el camino feliz


def test_un_numero_verificado_cambia_el_idioma_con_el_codigo(almacen):
    assert idioma.gerencia() == idioma.ES

    respuesta = _proponer("manager language", "English")
    assert "4242" not in respuesta, "el modelo NO puede ver el código"

    # El código le llegó al dueño, directo y aparte.
    assert almacen.enviados, "no se mandó el código"
    destino, texto = almacen.enviados[-1]
    assert destino == EQUIPO
    assert "4242" in texto

    # Todavía no cambió nada.
    assert idioma.gerencia() == idioma.ES

    acuse = _confirmar()
    assert acuse is not None
    assert idioma.gerencia() == idioma.EN


def test_antes_de_confirmar_se_ve_el_idioma_viejo_y_el_propuesto(almacen):
    _proponer("idioma de gerencia", "inglés")
    _, texto = almacen.enviados[-1]
    # En castellano, porque todavía rige el español.
    assert "español" in texto and "inglés" in texto


def test_el_acuse_sale_ya_en_el_idioma_nuevo(almacen):
    _proponer("manager language", "English")
    acuse = _confirmar()
    assert "went from" in acuse or "is on record" in acuse
    assert "pasó de" not in acuse


def test_volver_a_espanol_funciona_igual(almacen):
    _proponer("manager language", "English")
    _confirmar()
    assert idioma.gerencia() == idioma.EN

    limites.descartar(EQUIPO)
    _proponer("manager language", "Spanish")
    _confirmar()
    assert idioma.gerencia() == idioma.ES


def test_el_cambio_rige_sin_reiniciar_nada(almacen):
    """Se lee del almacén en cada llamada: no hay estado de proceso que reiniciar."""
    _proponer("manager language", "English")
    _confirmar()
    # Sin recargar ningún módulo ni reconstruir ningún agente:
    assert idioma.gerencia() == idioma.EN
    from app.conversacion import prompt_gerencia

    texto = prompt_gerencia({"messages": []}, {})[0].content
    assert "Always reply in English" in texto


# ------------------------------------------------- quién NO puede cambiarlo


def test_un_cliente_no_puede_cambiar_el_idioma_de_la_gerencia(almacen):
    respuesta = configuracion.proponer_limite.func(
        limite="manager language", valor="English", config=_cliente()
    )
    assert "no" in respuesta.lower()
    assert idioma.gerencia() == idioma.ES
    assert not almacen.enviados, "no se le manda ningún código a un cliente"


def test_un_cliente_con_un_codigo_de_cuatro_digitos_no_aplica_nada(almacen):
    _proponer("manager language", "English")
    # El cliente manda EXACTAMENTE el código correcto.
    assert main._codigo_de_ajuste("4242", CLIENTE) is None
    assert idioma.gerencia() == idioma.ES, "el idioma del equipo no se movió"


def test_el_idioma_de_un_cliente_no_toca_el_de_la_gerencia(almacen):
    idioma.recordar_cliente(CLIENTE, idioma.EN)
    assert idioma.cliente_guardado(CLIENTE) == idioma.EN
    assert idioma.gerencia() == idioma.ES


def test_el_idioma_de_la_gerencia_no_toca_el_de_un_cliente(almacen):
    _proponer("manager language", "English")
    _confirmar()
    assert idioma.gerencia() == idioma.EN
    # El cliente no pidió nada: se le sigue espejando el idioma del mensaje.
    assert idioma.cliente_guardado(CLIENTE) is None
    assert idioma.para_cliente(CLIENTE, "hola quiero un pedido") == idioma.ES


# --------------------------------------------- repetidos y códigos usados


def test_pedir_el_mismo_cambio_dos_veces_es_un_solo_cambio(almacen):
    una = _proponer("manager language", "English")
    dos = _proponer("manager language", "English")
    assert "4242" not in una and "4242" not in dos
    # Dos mensajes, el MISMO código.
    codigos = {texto for _, texto in almacen.enviados}
    assert len(almacen.enviados) == 2
    assert all("4242" in t for t in codigos)
    # Y una sola confirmación aplica.
    assert _confirmar() is not None
    assert idioma.gerencia() == idioma.EN


def test_un_codigo_ya_usado_no_se_puede_repetir(almacen):
    _proponer("manager language", "English")
    assert _confirmar() is not None
    # El mismo código otra vez: no queda nada pendiente que aplicar.
    assert _confirmar() is None
    assert idioma.gerencia() == idioma.EN, "no se aplicó dos veces"


def test_un_codigo_que_no_es_el_pendiente_se_contesta_y_no_cambia_nada(almacen):
    _proponer("manager language", "English")
    respuesta = main._codigo_de_ajuste("9999", EQUIPO)
    assert respuesta is not None
    assert idioma.gerencia() == idioma.ES


def test_un_idioma_imposible_no_prepara_nada(almacen):
    respuesta = _proponer("manager language", "klingon")
    assert "no cambié nada" in respuesta.lower()
    assert not almacen.enviados
    assert idioma.gerencia() == idioma.ES


# ------------------------------------------------ durabilidad y pérdida


def test_el_cambio_queda_anotado_en_erpnext_con_su_propia_marca(almacen):
    _proponer("manager language", "English")
    _confirmar()
    assert almacen.comentarios, "no se escribió la copia durable"
    texto = almacen.comentarios[-1]
    assert limites.MARCA_DURABLE_IDIOMA in texto
    assert limites.MARCA_DURABLE not in texto
    assert limites.MARCA_DURABLE_ENTREGA not in texto
    assert "IDIOMA_GERENCIA" in texto


def test_perder_el_almacen_no_deja_al_sistema_mudo(almacen):
    _proponer("manager language", "English")
    _confirmar()
    assert idioma.gerencia() == idioma.EN

    almacen.hashes.clear()          # se perdió Redis
    # NO levanta y NO frena nada: se vuelve al idioma por defecto.
    assert idioma.gerencia() == idioma.ES


def test_redis_caido_tampoco_levanta(almacen):
    almacen.caido = True
    assert idioma.gerencia() == idioma.por_defecto()


def test_perder_el_idioma_no_arma_el_fusible_de_los_limites(almacen):
    """La marca de idioma no puede frenar una venta: son hechos distintos."""
    _proponer("manager language", "English")
    _confirmar()
    marcas = "\n".join(almacen.comentarios)
    # Un almacén vacío + SOLO cambios de idioma registrados no debe leerse
    # como «se perdieron los límites».
    assert limites.MARCA_DURABLE not in marcas


# ------------------------------- los mensajes de Python en los dos idiomas


def test_los_mensajes_del_equipo_salen_en_los_dos_idiomas(almacen):
    es = idioma.t("gerencia.pedido_pendiente", idioma.ES, pedido="SAL-ORD-1",
                  cliente="Panaderia", detalle="5 x LEC", total="$ 6.000",
                  entrega="2026-09-06")
    en = idioma.t("gerencia.pedido_pendiente", idioma.EN, pedido="SAL-ORD-1",
                  cliente="Panaderia", detalle="5 x LEC", total="$ 6.000",
                  entrega="2026-09-06")
    assert es != en
    assert "Pedido" in es and "Order" in en
    # Y los datos, idénticos.
    for dato in ("SAL-ORD-1", "Panaderia", "5 x LEC", "$ 6.000", "2026-09-06"):
        assert dato in es and dato in en


def test_el_pedido_de_codigo_sale_en_el_idioma_vigente(almacen):
    _proponer("manager language", "English")
    _confirmar()
    limites.descartar(EQUIPO)
    almacen.enviados.clear()
    # Ahora rige inglés: el próximo pedido de código llega en inglés.
    _proponer("tope", "15000")
    _, texto = almacen.enviados[-1]
    assert "Reply" in texto and "to apply it" in texto
    assert "4242" in texto, "el código no cambia de idioma"


# --------------------------------- no cambia ninguna autorización


def test_el_idioma_no_aparece_en_la_configuracion_que_decide_un_pedido(almacen):
    """Traducir no puede mover una política."""
    assert "IDIOMA_GERENCIA" not in limites.LIMITES
    cfg = limites.configuracion()
    assert not hasattr(cfg, "idioma")


def test_cambiar_el_idioma_no_toca_ningun_limite(almacen):
    antes = {f["nombre"]: f["valor"] for f in limites.resumen()
             if f["nombre"] in limites.LIMITES}
    _proponer("manager language", "English")
    _confirmar()
    despues = {f["nombre"]: f["valor"] for f in limites.resumen()
               if f["nombre"] in limites.LIMITES}
    assert antes == despues
