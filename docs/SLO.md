# SLOs

| SLI | Definition | SLO (30d) | Error budget |
|---|---|---|---|
| API availability | non-5xx / all requests, excluding `/healthz`, `/readyz`, `/metrics` | 99.5% | 0.5% ≈ 3h 36m of full outage |
| API latency | requests faster than 500ms / all requests | 99% | 1% |
| Job success | jobs finishing `success` / (`success` + `failed`) | 99% | 1% |
<!-- TASK: adjust targets to the real product; add a freshness SLI if the work is async. -->

## Alerting policy (multi-window, multi-burn-rate)
| Alert | Condition | Budget consumed | Severity |
|---|---|---|---|
| ApiErrorBudgetFastBurn | 14.4x burn over 1h **and** 5m | 2% in 1h | page |
| ApiErrorBudgetSlowBurn | 6x burn over 6h **and** 30m | 5% in 6h | ticket |
| ApiLatencyHigh | p95 > 500ms for 10m | — | ticket |
| JobFailureRateHigh | > 5% permanent failures over 15m, for 5m | — | page |
| QueueBacklogGrowing | depth > 100 for 10m | — | ticket |
| TargetDown | scrape failing for 2m | — | page |

The short window makes alerts reset quickly after recovery; the long window stops brief spikes from paging anyone.

## Error budget policy
- Budget left: ship normally.
- Budget < 25%: reliability work gets priority in planning; risky rollouts need a rollback plan.
- Budget exhausted: freeze non-critical feature releases until the 30d window recovers; write a postmortem for the incidents that consumed it.
