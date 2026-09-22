# SLOs

| SLI | Definition | SLO (30d) | Error budget |
|---|---|---|---|
| API availability | non-5xx / all requests, excluding `/healthz`, `/readyz`, `/metrics` | 99.5% | 0.5% ≈ 3h 36m of full outage |
| API latency | requests faster than 500ms / all requests | 99% | 1% |
| Sandbox start success | `start_sandbox` jobs ending `success` / (`success` + `failed`), after retries | 99% | 1% |
| Sandbox freshness | sandboxes serving within 10s of the request (`sandbox_time_to_running_seconds`) | 95% | 5% |

Freshness is the SLI agents feel. It covers queue wait, container start and the readiness probe,
so it goes red for a stalled queue, a slow image pull or a broken sandbox image alike.

## Alerting policy (multi-window, multi-burn-rate)
| Alert | Condition | Budget consumed | Severity |
|---|---|---|---|
| ApiErrorBudgetFastBurn | 14.4x burn over 1h **and** 5m | 2% in 1h | page |
| ApiErrorBudgetSlowBurn | 6x burn over 6h **and** 30m | 5% in 6h | ticket |
| ApiLatencyHigh | p95 > 500ms for 10m | — | ticket |
| JobFailureRateHigh | > 5% permanent failures over 15m, for 5m | start success | page |
| SandboxQueueStalled | oldest queued sandbox > 60s, for 1m | — | page |
| QueueNotDraining | a pool has due jobs but finished none in 5m, for 5m | — | page |
| SandboxStartSlow | p95 time-to-running > 10s for 10m | freshness | ticket |
| SandboxStuckInTransition | a sandbox `starting`/`stopping` > 2m, for 2m | — | ticket |
| SandboxCapacityHigh | > 80% of the admission cap for 10m | — | ticket |
| SandboxLeaked | orphaned/vanished sandboxes reaped in 15m | — | ticket |
| SandboxReaperFailing | ≥ 3 failed reaper sweeps in 10m | — | page |
| TargetDown | scrape failing for 2m | — | page |

The short window makes alerts reset quickly after recovery; the long window stops brief spikes from paging anyone.

## Known gaps
- Only the API SLOs have burn-rate alerts. The sandbox SLOs use thresholds: `JobFailureRateHigh` pages at about
  5× burn, and `SandboxStartSlow` opens a ticket at 1× burn (p95 > 10s). A slow leak that stays under those
  thresholds for days is invisible until the budget is gone.
- `sandbox_time_to_running_seconds` has buckets at 8s and 13s but none at 10s, so "serving within 10s" can only be
  interpolated. Add a `10` bucket before measuring the budget from it.
- `JobFailureRateHigh` counts `stop_sandbox` failures too, while the SLI is defined on `start_sandbox` only.
  Add `task="start_sandbox"` to the alert.
- There's no SLO for the A/B rollout yet. Canary health is the input to step 5, which was cut.

## Error budget policy
- Budget left: ship normally.
- Budget < 25%: reliability work gets priority in planning; risky rollouts need a rollback plan.
- Budget exhausted: freeze non-critical feature releases until the 30d window recovers; write a postmortem for the incidents that consumed it.
