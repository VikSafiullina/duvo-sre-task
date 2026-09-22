# PLAN — sandbox orchestrator (timed, ~1h)

**Goal:** a queue-driven service that starts one HTTP sandbox container per job, shows its health end to end, and moves consumer traffic to a new worker version automatically once metrics show that version is healthy.

**Assumptions:** Docker Compose is the demo "prod". Terraform/Cloud Run stays untouched because Cloud Run can't launch containers. The worker gets the Docker socket. Job type `http` runs a stock image (`traefik/whoami`) on a random host port. Sandboxes are short-lived (TTL). Postgres holds the true sandbox state and Redis/arq is the queue.

**Slice 0: thinnest end-to-end path (~10m).** Rename `items`→`sandboxes`. `POST /sandboxes {"type":"http"}` creates a row with status `queued` and enqueues arq job `start_sandbox:{id}`. The worker logs the job and marks the row `running`. Add `GET /sandboxes/{id}`. Update smoke, k6 and tests.

**Increments** (one commit each; `make lint test` + `make restart smoke` must pass before the next one)
1. **Containers (~15m):** the worker starts `sandbox-{id}` through the Docker SDK with labels, mem/CPU caps and a timeout, then logs the URL and stores it. A retry reuses the existing container. Add `DELETE /sandboxes/{id}` and a TTL reaper cron (`stopped`). Smoke curls the URL.
2. **Observability (~10m):** metrics `sandbox_starts_total{outcome,deployment}`, `sandbox_start_seconds`, `sandboxes_running` and per-queue depth. Add a "Sandbox lifecycle" dashboard row. Alerts: start-failure ratio, slow starts, stuck in `starting`, running over cap. Each alert gets a RUNBOOK entry.
3. **A/B (~10m):** two worker services, `worker-a` (stable) and `worker-b` (canary), each with `DEPLOYMENT`/`VERSION` env and its own arq queue. The producer routes a job to canary when `crc32(id) % 100 < canary_weight`. The weight lives in Redis and is set with `PUT /rollout`. Routing is deterministic, so retries stay on the same deployment.
4. **Metric cutover (~10m):** a `rollout` loop (same image, compose service) queries Prometheus. It steps the weight 10→25→50→100 when, over the window, the canary has ≥ N starts, a success ratio ≥ stable, and p95 start time ≤ 1.2× stable. It sets the weight to 0 when a threshold is breached. It exposes `rollout_canary_weight` and logs every decision. Demo: chaos on `worker-b` only → automatic rollback.
5. **Wrap-up (~5m):** README (decisions, next steps, time log), architecture diagram, `make check`.

**Risks / failure modes → mitigation**
- Worker dies between `docker run` and the DB write, leaving an orphaned container → deterministic container names + a reaper that finds containers by label.
- Docker socket = root on the host → explicit shortcut, documented. Image pull time can exceed the job timeout → pre-pull the image.
- Canary gets too little traffic to judge, so it looks "healthy" → minimum-sample gate. No data means hold; never promote on silence.
- Prometheus or the controller goes down mid-rollout → the weight persists in Redis and stays where it is (fail-static). Alert on a stale rollout.
- Canary pool goes down and its queue is stranded → per-queue depth alert. Rollback moves pending canary jobs back to stable.
- Host ports or resources run out → worker concurrency cap, per-container limits, `sandboxes_running` alert.

**Deliberately not doing:** real isolation (gVisor/Firecracker/k8s), multi-host scheduling, sandbox ingress or auth, Argo Rollouts/Flagger/service mesh, sandboxes on Cloud Run, job types other than `http`, any UI besides Grafana.
