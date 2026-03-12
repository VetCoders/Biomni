# Biomni Portal — Makefile
# VibeCrafted with AI Agents (c)2026 VetCoders

SHELL       := /bin/bash
.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PYTHON_BIN  ?= python3
VENV        := .venv
PIP         := $(VENV)/bin/pip
PY          := $(VENV)/bin/python
UVICORN     := $(VENV)/bin/uvicorn
HOST        ?= 0.0.0.0
PORT        ?= 8129
APP         := app.main:app

VM          ?= libraxis-vm
VM_DIR      := ~/biomni-portal
DEPLOY_HEALTH_URL ?= http://213.160.77.173:8129/api/health
DEPLOY_RETRIES ?= 5
DEPLOY_RETRY_DELAY ?= 3
SSH_OPTS    ?= -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3

RSYNC_EXCLUDE := --exclude '.env' --exclude '.git' --exclude '.venv' \
	--exclude '__pycache__' --exclude '.ai-agents' --exclude '.ai-contexters' \
	--exclude '.claude' --exclude 'node_modules' --exclude '.DS_Store' \
	--exclude '.pytest_cache' --exclude '*.pyc' --exclude 'data/*.db'

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

$(VENV)/bin/activate:
	$(PYTHON_BIN) -m venv $(VENV)
	$(PIP) install --upgrade pip -q

.PHONY: install
install: $(VENV)/bin/activate ## Install production deps
	$(PIP) install -r requirements.txt -q

.PHONY: install-dev
install-dev: install ## Install dev deps (pytest)
	$(PIP) install pytest httpx -q

# ---------------------------------------------------------------------------
# Dev
# ---------------------------------------------------------------------------

.PHONY: dev
dev: install ## Run local server with auto-reload
	$(UVICORN) $(APP) --host $(HOST) --port $(PORT) --reload

.PHONY: run
run: install ## Run local server (production-like)
	$(UVICORN) $(APP) --host $(HOST) --port $(PORT)

.PHONY: health
health: ## Health check (local)
	@curl -sf http://localhost:$(PORT)/api/health | python3 -m json.tool

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------

.PHONY: check
check: install-dev ## Compile check + pytest
	$(PY) -m compileall app tests -q
	$(PY) -m pytest -q

.PHONY: gate
gate: install-dev ## Full quality gate (compile + pytest + semgrep)
	@echo "--- compile ---"
	$(PY) -m compileall app -q
	@echo "--- pytest ---"
	$(PY) -m pytest -q
	@echo "--- semgrep (app/ only) ---"
	semgrep --config auto --error --quiet app/ || true

.PHONY: lint
lint: ## Syntax check all Python modules
	$(PY) -m compileall app -q

.PHONY: test
test: install-dev ## Run tests
	$(PY) -m pytest -v

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

.PHONY: docker-build
docker-build: ## Build Docker image
	docker compose build

.PHONY: docker-up
docker-up: ## Start via Docker Compose
	docker compose up -d --build

.PHONY: docker-down
docker-down: ## Stop Docker Compose
	docker compose down

.PHONY: docker-logs
docker-logs: ## Tail Docker logs
	docker compose logs -f

.PHONY: docker-health
docker-health: ## Health check (Docker)
	@docker exec biomni-portal python -c \
		"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8129/api/health').read().decode())"

# ---------------------------------------------------------------------------
# Deploy (libraxis-vm / VM over SSH)
# ---------------------------------------------------------------------------

.PHONY: deploy
deploy: ## Deploy to VM (rsync + docker rebuild)
	@echo "→ Syncing files to $(VM):$(VM_DIR)/"
	@bash -c 'set -e; ok=0; for i in $$(seq 1 $(DEPLOY_RETRIES)); do \
		echo "  rsync attempt $$i/$(DEPLOY_RETRIES)"; \
		rsync -avz --delete $(RSYNC_EXCLUDE) -e "ssh $(SSH_OPTS)" ./ $(VM):$(VM_DIR)/ && { ok=1; break; }; \
		[ $$i -lt $(DEPLOY_RETRIES) ] && sleep $(DEPLOY_RETRY_DELAY); \
	done; [ $$ok -eq 1 ]'
	@echo "→ Copying .env"
	@bash -c 'set -e; ok=0; for i in $$(seq 1 $(DEPLOY_RETRIES)); do \
		echo "  scp attempt $$i/$(DEPLOY_RETRIES)"; \
		scp $(SSH_OPTS) .env $(VM):$(VM_DIR)/.env && { ok=1; break; }; \
		[ $$i -lt $(DEPLOY_RETRIES) ] && sleep $(DEPLOY_RETRY_DELAY); \
	done; [ $$ok -eq 1 ]'
	@echo "→ Rebuilding container"
	@bash -c 'set -e; ok=0; for i in $$(seq 1 $(DEPLOY_RETRIES)); do \
		echo "  remote rebuild attempt $$i/$(DEPLOY_RETRIES)"; \
		ssh $(SSH_OPTS) $(VM) "cd $(VM_DIR) && docker compose up -d --build" && { ok=1; break; }; \
		[ $$i -lt $(DEPLOY_RETRIES) ] && sleep $(DEPLOY_RETRY_DELAY); \
	done; [ $$ok -eq 1 ]'
	@echo "→ Waiting for health check via $(DEPLOY_HEALTH_URL)..."
	@sleep 5
	@(curl -sf $(DEPLOY_HEALTH_URL) || ssh $(SSH_OPTS) $(VM) "curl -sf http://localhost:$(PORT)/api/health") | python3 -m json.tool
	@echo "✓ Deploy complete"

.PHONY: deploy-sync
deploy-sync: ## Rsync only (no rebuild)
	@bash -c 'set -e; ok=0; for i in $$(seq 1 $(DEPLOY_RETRIES)); do \
		echo "  rsync attempt $$i/$(DEPLOY_RETRIES)"; \
		rsync -avz --delete $(RSYNC_EXCLUDE) -e "ssh $(SSH_OPTS)" ./ $(VM):$(VM_DIR)/ && { ok=1; break; }; \
		[ $$i -lt $(DEPLOY_RETRIES) ] && sleep $(DEPLOY_RETRY_DELAY); \
	done; [ $$ok -eq 1 ]'
	@bash -c 'set -e; ok=0; for i in $$(seq 1 $(DEPLOY_RETRIES)); do \
		echo "  scp attempt $$i/$(DEPLOY_RETRIES)"; \
		scp $(SSH_OPTS) .env $(VM):$(VM_DIR)/.env && { ok=1; break; }; \
		[ $$i -lt $(DEPLOY_RETRIES) ] && sleep $(DEPLOY_RETRY_DELAY); \
	done; [ $$ok -eq 1 ]'

.PHONY: deploy-restart
deploy-restart: ## Restart container on VM (no rebuild)
	@bash -c 'set -e; ok=0; for i in $$(seq 1 $(DEPLOY_RETRIES)); do \
		echo "  remote restart attempt $$i/$(DEPLOY_RETRIES)"; \
		ssh $(SSH_OPTS) $(VM) "cd $(VM_DIR) && docker compose restart" && { ok=1; break; }; \
		[ $$i -lt $(DEPLOY_RETRIES) ] && sleep $(DEPLOY_RETRY_DELAY); \
	done; [ $$ok -eq 1 ]'
	@sleep 3
	@(curl -sf $(DEPLOY_HEALTH_URL) || ssh $(SSH_OPTS) $(VM) "curl -sf http://localhost:$(PORT)/api/health") | python3 -m json.tool

.PHONY: deploy-rebuild
deploy-rebuild: ## Rebuild and restart on VM (no sync)
	@bash -c 'set -e; ok=0; for i in $$(seq 1 $(DEPLOY_RETRIES)); do \
		echo "  remote rebuild attempt $$i/$(DEPLOY_RETRIES)"; \
		ssh $(SSH_OPTS) $(VM) "cd $(VM_DIR) && docker compose up -d --build" && { ok=1; break; }; \
		[ $$i -lt $(DEPLOY_RETRIES) ] && sleep $(DEPLOY_RETRY_DELAY); \
	done; [ $$ok -eq 1 ]'
	@sleep 5
	@(curl -sf $(DEPLOY_HEALTH_URL) || ssh $(SSH_OPTS) $(VM) "curl -sf http://localhost:$(PORT)/api/health") | python3 -m json.tool

.PHONY: deploy-logs
deploy-logs: ## Tail logs on VM
	ssh $(SSH_OPTS) $(VM) "cd $(VM_DIR) && docker compose logs -f --tail=50"

.PHONY: deploy-health
deploy-health: ## Health check from local machine via VM host
	@(curl -sf $(DEPLOY_HEALTH_URL) || ssh $(SSH_OPTS) $(VM) "curl -sf http://localhost:$(PORT)/api/health") | python3 -m json.tool

.PHONY: deploy-status
deploy-status: ## Docker status on VM
	ssh $(SSH_OPTS) $(VM) "cd $(VM_DIR) && docker compose ps"

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

.PHONY: clean
clean: ## Remove build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true

.PHONY: clean-venv
clean-venv: ## Remove virtualenv
	rm -rf $(VENV)

.PHONY: clean-docker
clean-docker: ## Remove Docker image and volumes
	docker compose down --rmi local -v 2>/dev/null || true

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
