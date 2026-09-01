# Atajos. Lo que corre en CI es lo mismo que corre acá.

VENV ?= .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.PHONY: help install test lint fmt check check-env up down logs briefing seed build

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

$(VENV):
	python3 -m venv $(VENV)

install: $(VENV) ## Instalar dependencias de desarrollo
	$(PIP) install -q -r requirements-dev.txt

test: install ## Correr los tests (offline, ~2s, sin credenciales)
	$(PY) -m pytest -q

lint: install ## Lint
	$(VENV)/bin/ruff check app tests

fmt: install ## Formatear
	$(VENV)/bin/ruff format app tests
	$(VENV)/bin/ruff check --fix app tests

check: lint test ## Todo lo que corre CI

check-env: ## ¿Están todas las variables obligatorias en .env?
	@test -f .env || { echo "falta .env — copiá .env.example"; exit 1; }
	@missing=0; \
	for v in ERPNEXT_URL ERPNEXT_API_KEY ERPNEXT_API_SECRET \
	         ERPNEXT_POLICY_API_KEY ERPNEXT_POLICY_API_SECRET \
	         META_APP_SECRET META_VERIFY_TOKEN REDIS_URL \
	         WHATSAPP_PHONE_NUMBER_ID WHATSAPP_TOKEN ANTHROPIC_API_KEY \
	         TELEFONOS_EQUIPO; do \
	  val=$$(grep -E "^$$v=" .env | cut -d= -f2-); \
	  if [ -z "$$val" ]; then echo "  FALTA  $$v"; missing=1; fi; \
	done; \
	if [ "$$missing" = "1" ]; then exit 1; fi; \
	agente=$$(grep -E '^ERPNEXT_API_KEY=' .env | cut -d= -f2-); \
	policy=$$(grep -E '^ERPNEXT_POLICY_API_KEY=' .env | cut -d= -f2-); \
	if [ "$$agente" = "$$policy" ]; then \
	  echo "  PELIGRO  ERPNEXT_API_KEY y ERPNEXT_POLICY_API_KEY son iguales."; \
	  echo "           El agente tendría permiso de Submit: el guardarraíl"; \
	  echo "           principal del sistema deja de existir."; \
	  exit 1; \
	fi; \
	echo "  .env completo, y las credenciales del agente y de la política son distintas."

build: ## Buildear la imagen
	docker build -t plus-agent .

up: ## Levantar (agente + redis)
	docker compose up -d --build
	@echo "esperando que esté listo..."
	@for i in $$(seq 1 30); do \
	  curl -fsS localhost:8080/ready >/dev/null 2>&1 && { curl -s localhost:8080/ready; echo; exit 0; }; \
	  sleep 1; \
	done; \
	echo "no llegó a estar listo — mirá 'make logs'"; exit 1

down: ## Bajar
	docker compose down

logs: ## Ver los logs
	docker compose logs -f agente

decisiones: ## Ver qué decidió la política en cada pedido
	docker compose logs agente | grep -E "policy|auto-confirm"

briefing: ## Mandar el briefing ahora (sin esperar al cron)
	docker compose run --rm briefing

seed: ## Cargar catálogo y clientes de demo en ERPNext
	docker compose run --rm agente python deploy/seed_dairy.py
