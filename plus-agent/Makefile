# Atajos. Lo que corre en CI (.github/workflows/ci.yml) es lo mismo que corre acá.
# El agente escucha en 8081 (8080 es ERPNext), igual que start.sh y compose.

VENV   ?= .venv
PY     := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
PUERTO ?= 8081

.PHONY: help install test lint fmt check check-env build up down logs decisiones briefing seed

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

$(VENV):
	python3 -m venv $(VENV)

install: $(VENV) ## Instalar dependencias de desarrollo (versiones fijas)
	$(PIP) install -q -r requirements-dev.txt

test: install ## Correr los tests (offline, ~1s, sin credenciales ni Redis)
	$(PY) -m pytest -q

lint: install ## Lint (ruff check; el formato todavía no se exige)
	$(VENV)/bin/ruff check app tests

fmt: install ## Formatear y autocorregir lo que ruff sabe arreglar
	$(VENV)/bin/ruff format app tests
	$(VENV)/bin/ruff check --fix app tests

check: lint test ## Todo lo que corre CI

check-env: ## ¿Están todas las variables obligatorias en .env y las 3 claves ERPNext son distintas?
	@test -f .env || { echo "falta .env — copiá .env.example"; exit 1; }
	@missing=0; \
	for v in ERPNEXT_URL \
	         ERPNEXT_API_KEY ERPNEXT_API_SECRET \
	         ERPNEXT_MANAGER_API_KEY ERPNEXT_MANAGER_API_SECRET \
	         ERPNEXT_POLICY_API_KEY ERPNEXT_POLICY_API_SECRET \
	         ERPNEXT_COMPANY ERPNEXT_WAREHOUSE \
	         META_APP_SECRET META_VERIFY_TOKEN REDIS_URL \
	         WHATSAPP_PHONE_NUMBER_ID WHATSAPP_TOKEN \
	         DASHSCOPE_API_KEY TELEFONOS_EQUIPO; do \
	  val=$$(grep -E "^$$v=" .env | cut -d= -f2-); \
	  if [ -z "$$val" ]; then echo "  FALTA  $$v"; missing=1; fi; \
	done; \
	for v in AUTO_CONFIRM_PRICE_LIST AUTO_CONFIRM_CURRENCY \
	         WHATSAPP_STAFF_PENDING_TEMPLATE WHATSAPP_STAFF_CONFIRMED_TEMPLATE \
	         WHATSAPP_CUSTOMER_CONFIRMED_TEMPLATE; do \
	  val=$$(grep -E "^$$v=" .env | cut -d= -f2-); \
	  if [ -z "$$val" ]; then echo "  AVISO  $$v vacía: precios/avisos fallan cerrados hasta que la completes"; fi; \
	done; \
	if [ "$$missing" = "1" ]; then exit 1; fi; \
	agente=$$(grep -E '^ERPNEXT_API_KEY=' .env | cut -d= -f2-); \
	gerencia=$$(grep -E '^ERPNEXT_MANAGER_API_KEY=' .env | cut -d= -f2-); \
	policy=$$(grep -E '^ERPNEXT_POLICY_API_KEY=' .env | cut -d= -f2-); \
	if [ "$$agente" = "$$policy" ] || [ "$$gerencia" = "$$policy" ] || [ "$$agente" = "$$gerencia" ]; then \
	  echo "  PELIGRO  Dos de las tres claves ERPNext son iguales"; \
	  echo "           (ERPNEXT_API_KEY / ERPNEXT_MANAGER_API_KEY / ERPNEXT_POLICY_API_KEY)."; \
	  echo "           Son tres usuarios distintos: agente de clientes (sin Submit),"; \
	  echo "           gerencia (lectura amplia, sin Submit) y política (la única con Submit)."; \
	  echo "           Con una clave repetida un LLM tendría Submit o lectura de todo el"; \
	  echo "           negocio: el guardarraíl principal del sistema deja de existir."; \
	  exit 1; \
	fi; \
	echo "  .env completo, y las tres credenciales de ERPNext son distintas entre sí."

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

seed: install ## Cargar catálogo y clientes de demo en ERPNext (staging; usa credenciales temporales, ver README)
	$(PY) deploy/seed_dairy.py
