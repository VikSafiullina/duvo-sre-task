.DEFAULT_GOAL := help
COMPOSE := docker compose

.PHONY: help up down sandboxes restart ps logs test lint fmt smoke load chaos tf-validate rules-check scan check urls rollout

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Build and start the full stack, wait until healthy
	$(COMPOSE) up -d --build --wait --remove-orphans
	@$(MAKE) --no-print-directory urls

down: ## Stop the stack, delete volumes and any sandbox containers
	-docker ps -aq --filter label=duvo.sandbox.id | xargs docker rm -f >/dev/null 2>&1
	$(COMPOSE) down -v --remove-orphans

sandboxes: ## List sandbox containers the worker launched
	docker ps -a --filter label=duvo.sandbox.id --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

restart: ## Rebuild + restart only api and both worker pools
	$(COMPOSE) up -d --build --wait --remove-orphans api worker-a worker-b

ps: ## Container status
	$(COMPOSE) ps

logs: ## Tail api + worker logs (both pools)
	$(COMPOSE) logs -f --tail=50 api worker-a worker-b

test: ## Run tests (starts Postgres + Redis if needed)
	$(COMPOSE) up -d --wait postgres redis
	uv run pytest

lint: ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-fix lint issues and format
	uv run ruff check --fix .
	uv run ruff format .

smoke: ## End-to-end check against the running stack
	./scripts/smoke.sh

load: ## k6 load test against the running stack
	$(COMPOSE) --profile load run --rm k6

chaos: ## Inject 30% job failures into one pool: canary by default (WORKER=worker-a, RATE=0 to stop)
	CHAOS_FAILURE_RATE=$(or $(RATE),0.3) $(COMPOSE) up -d --wait --no-deps $(or $(WORKER),worker-b)

rollout: ## Route W% of new jobs to the canary pool: make rollout W=25
	@curl -fsS -X PUT localhost:8000/rollout -H 'content-type: application/json' \
	    -d '{"canary_weight": $(or $(W),0)}' && echo

tf-validate: ## terraform fmt + validate (no cloud credentials needed)
	terraform -chdir=infra/terraform fmt -check -recursive
	terraform -chdir=infra/terraform init -backend=false -input=false >/dev/null
	terraform -chdir=infra/terraform validate

rules-check: ## Validate Prometheus config + alert rules with promtool
	docker run --rm \
	    -v $(CURDIR)/observability/prometheus/prometheus.yaml:/etc/prometheus/prometheus.yaml:ro \
	    -v $(CURDIR)/observability/prometheus/rules.yml:/otel-lgtm/rules.yml:ro \
	    --entrypoint promtool prom/prometheus:latest check config /etc/prometheus/prometheus.yaml

scan: ## Trivy scan of the app image (HIGH/CRITICAL, fixable only)
	docker build -t duvo-app:local .
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:0.74.0 \
	    image -q --table-mode detailed --db-repository ghcr.io/aquasecurity/trivy-db:2 --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 duvo-app:local

check: lint test tf-validate rules-check ## Everything CI runs, locally

urls: ## Print useful local URLs
	@echo "API docs    http://localhost:8000/docs"
	@echo "Grafana     http://localhost:3000   (home = 'Duvo service' dashboard)"
	@echo "Alerts      http://localhost:9090/alerts"
	@echo "Workers     http://localhost:9100/metrics (stable)  http://localhost:9101/metrics (canary)"
	@echo "Rollout     curl localhost:8000/rollout   (make rollout W=25)"
