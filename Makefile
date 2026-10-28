# billparse: start and stop everything with Docker Compose, plus the local commands.
#   make up       build and start PostgreSQL, the API and the web app (in the background)
#   make down     stop and remove the containers (the data volumes stay)
#   make help     every target

COMPOSE ?= docker compose
SERVICE ?= api

.DEFAULT_GOAL := help
.PHONY: help up dev down start stop restart build logs ps status psql migrate nightly shell destroy \
        backup-db reset-db worker logs-worker prices prices-azure samples corpus corpus-check corpus-accept corpus-agent agent-jobs test lint lint-fix typecheck web-types api-local web-local

help:  ## Show the targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ------------------------------------------------------------------ Docker
up:  ## Build the images and start db, api, the workers and web in the background
	$(COMPOSE) up --build --detach
	@echo "API: http://localhost:$${API_PORT:-8010}/parse   web app: http://localhost:$${WEB_PORT:-8090}"

DEV = $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml

# `up --watch` prints each log line again after a hot-reload restart: the watch runs in the background and one
# `logs --follow` prints each line once.
dev:  ## Start everything with hot reload (stays in the foreground; Ctrl-C stops the watch)
	$(DEV) up --build --detach --wait
	@trap 'kill $$watch 2>/dev/null' EXIT INT TERM; \
	$(DEV) watch --no-up --quiet & watch=$$!; \
	$(DEV) logs --follow --since 1s

down:  ## Stop and remove the containers (the data volumes stay)
	$(COMPOSE) down

start:  ## Start the containers that exist, without building
	$(COMPOSE) start

stop:  ## Stop the containers without removing them
	$(COMPOSE) stop

restart:  ## Restart the containers (SERVICE=api restarts one)
	$(COMPOSE) restart $(if $(filter command line,$(origin SERVICE)),$(SERVICE),)

build:  ## Build the images only
	$(COMPOSE) build

logs:  ## Follow the logs (SERVICE=api follows one)
	$(COMPOSE) logs --follow $(if $(filter command line,$(origin SERVICE)),$(SERVICE),)

ps:  ## Show the containers and their health
	$(COMPOSE) ps

status: ps

psql:  ## Open psql in the database container
	$(COMPOSE) exec db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

migrate:  ## Run the database migrations in the api container
	$(COMPOSE) exec api alembic upgrade head

nightly:  ## Run the nightly learning loop in the api container (calls the LLM when a key is set)
	$(COMPOSE) exec api python -m billparse nightly

shell:  ## Open a shell in a container (SERVICE=api by default)
	$(COMPOSE) exec $(SERVICE) sh

backup-db:  ## Save the database to backups/db-<time>/ (pg_dump, and plain SQL)
	@dir=backups/db-$$(date +%Y%m%d-%H%M%S) && mkdir -p $$dir && \
	$(COMPOSE) exec -T db sh -c 'pg_dump -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -Fc' > $$dir/billparse.dump && \
	$(COMPOSE) exec -T db sh -c 'pg_dump -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" --no-owner' > $$dir/billparse.sql && \
	echo "backup: $$dir (the billparse-data volume with the uploads is not in it)"

reset-db:  ## Empty the database (make backup-db first): asks first
	@printf "This deletes every row in the database (make backup-db first). Type yes: " && read answer && [ "$$answer" = "yes" ] && \
	$(COMPOSE) stop api && $(COMPOSE) up --detach --wait db && \
	$(COMPOSE) exec -T db sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -c "drop schema public cascade; create schema public authorization $$POSTGRES_USER;"' && \
	echo "Empty. Start the app: make up (or make dev); the API makes the tables again." || echo "Nothing deleted."

destroy:  ## Remove the containers AND the data volumes (PostgreSQL data, uploads): asks first
	@printf "This deletes the PostgreSQL data and every uploaded file in the Docker volumes. Type yes: " && \
		read answer && [ "$$answer" = "yes" ] && $(COMPOSE) down --volumes || echo "Nothing deleted."

# ------------------------------------------------------------------ local (no Docker)
corpus:  ## Measure every file of the golden set (CORPUS_DIR, default ~/projects/cost_engine_inputs), no LLM
	uv run python scripts/corpus.py --compare

corpus-check:  ## Parse the corpus and compare with the accepted baseline (exit 1 when a file is worse or changed)
	uv run python scripts/corpus.py --check

corpus-accept:  ## Accept the corpus run as the baseline (ONLY=<text> for some files): check the changes first
	uv run python scripts/corpus.py --accept $(if $(ONLY),--only "$(ONLY)",)

corpus-agent:  ## The same, and run the agent on every file not proved (AGENT_ENABLED=1, ANTHROPIC_API_KEY; costs money)
	uv run python scripts/corpus.py --agent --compare

agent-jobs:  ## List the agent's jobs in the local store
	uv run python -m billparse agent list

prices:  ## Load the AWS price files (PRICE_FILES_DIR) into the price catalog (DATABASE_URL); ARGS="--only AmazonRDS"
	uv run python scripts/refresh_prices.py $(ARGS)

prices-azure:  ## Load the Azure retail prices (AZURE_PRICE_FILES_DIR) into the price catalog; ARGS='--only "Virtual Machines"'
	uv run python scripts/refresh_prices.py --azure $(ARGS)

samples:  ## Make the sample files in samples/files
	uv run python samples/make_samples.py

test:  ## Run the tests
	uv run pytest -q

lint:  ## Lint the backend (Ruff) and the web app (oxlint)
	uv run ruff check billparse/api.py billparse/webapi.py billparse/agent.py billparse/auth.py billparse/db.py billparse/store.py billparse/jobs.py billparse/worker.py billparse/headers.py billparse/synonyms.py billparse/detect.py \
		billparse/settings.py billparse/bigfile.py migrations/env.py tests/test_webapi.py tests/test_jobs.py tests/test_headers.py tests/test_service_synonyms.py tests/conftest.py scripts/export_openapi.py
	cd web && pnpm lint

lint-fix:  ## Apply Ruff's safe fixes
	uv run ruff check --fix .

typecheck:  ## Type check the web app (tsc)
	cd web && pnpm typecheck

web-types:  ## Generate the web app's API types from the FastAPI OpenAPI document
	cd web && pnpm gen:api

api-local:  ## Run the API on this machine (SQLite unless DATABASE_URL is set: then run make worker too)
	uv run uvicorn billparse.api:app --reload --port 8000

worker:  ## Run a worker on this machine for every queue (needs DATABASE_URL; SQLite runs the jobs in the API)
	uv run python -m billparse worker --queues small:$${PARSE_WORKERS:-2},learn:1,large:$${LARGE_PARSE_WORKERS:-1},maintenance:1

logs-worker:  ## Follow the workers' logs (worker and worker-large)
	$(COMPOSE) logs --follow worker worker-large

web-local:  ## Run the web app's dev server on this machine (http://localhost:5173; start api-local first)
	cd web && pnpm dev
