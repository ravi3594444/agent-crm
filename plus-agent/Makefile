# Atajos. Lo que corre en CI (.github/workflows/ci.yml) es lo mismo que corre acá.
# El agente escucha en 8081 (8080 es ERPNext), igual que start.sh y compose.

VENV   ?= .venv
PY     := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
PUERTO ?= 8081

.PHONY: help install test lint fmt check check-env check-env-offline verificar-modelos build up down logs decisiones briefing demo demo-gemini seed

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

$(VENV):
	python3 -m venv $(VENV)

install: $(VENV) ## Instalar dependencias de desarrollo (versiones fijas)
	$(PIP) install -q -r requirements-dev.txt

test: install ## Correr los tests (sin credenciales ni red, pero NECESITA un Redis Stack en REDIS_URL, base 0 — ver README)
	$(PY) -m pytest -q

lint: install ## Lint (ruff check; el formato todavía no se exige)
	$(VENV)/bin/ruff check app demo tests

fmt: install ## Formatear y autocorregir lo que ruff sabe arreglar
	$(VENV)/bin/ruff format app demo tests
	$(VENV)/bin/ruff check --fix app demo tests

check: lint test ## Todo lo que corre CI

check-env: install ## ¿Está todo listo para probar en vivo? Valida .env, Meta, ERPNext, Redis y límites sin mostrar valores
	$(PY) -m app.readiness

check-env-offline: install ## Lo mismo sin llamar a Meta ni a ERPNext (sólo el .env y Redis)
	$(PY) -m app.readiness --sin-red

verificar-modelos: install ## Una llamada real con herramientas a cada modelo del proveedor elegido (manual, nunca en CI)
	$(PY) deploy/verificar_modelos.py

build: ## Buildear la imagen
	docker build -t plus-agent .

up: ## Levantar (agente + Redis Stack) y esperar /health
	docker compose up -d --build
	@echo "esperando que esté listo..."
	@for i in $$(seq 1 30); do \
	  curl -fsS localhost:$(PUERTO)/health >/dev/null 2>&1 && { curl -s localhost:$(PUERTO)/health; echo; exit 0; }; \
	  sleep 1; \
	done; \
	echo "no llegó a estar listo — mirá 'make logs'"; exit 1

down: ## Bajar
	docker compose down

logs: ## Ver los logs
	docker compose logs -f agente

decisiones: ## Ver qué hizo la política y los avisos en cada pedido
	docker compose logs agente | grep -E "\[orders\]|\[approval\]|\[staff-notify\]|\[customer-notify\]|auto-confirm|política"

briefing: ## Mandar el briefing ahora (sin esperar al cron)
	docker compose run --rm briefing

demo: install ## Banco de pruebas: los 17 escenarios, determinístico y sin red (ver demo/README.md)
	$(PY) -m demo.piloto --modo offline

demo-gemini: install ## Lo mismo pero con Gemini de verdad, por un relevo aislado (consume cuota)
	$(PY) -m demo.piloto --modo gemini

seed: install ## Cargar catálogo y clientes de demo en ERPNext (staging; usa credenciales temporales, ver README)
	$(PY) deploy/seed_dairy.py
