# LLM Gateway with Spend Controls

A proxy that sits between an organization's apps and an LLM provider. Apps never hold the provider key. Each person or service gets a **virtual key** the gateway controls, and every call passes through identity, model policy, rate limits, budgets, metering, audit logging and alerting.

This version runs against a **mock upstream** that returns Claude-shaped responses with realistic `usage` blocks. It is free, offline and deterministic, and needs no API key.

![make demo: normal traffic, a model blocked by role, a runaway job hitting the team budget with webhook alerts, a rate limit, offboarding, and the spend report](docs/demo.gif)

```
 app / user ──(gw_ key)──▶ ┌──────────────── gateway ────────────────┐ ──▶ upstream (mock Claude API)
                           │ 1 authenticate  key → user, team, role  │
                           │ 2 validate      body; no streaming (v1) │
                           │ 3 authorize     model allowed for role? │
                           │ 4 rate limit    requests/min per key    │
                           │ 5 budget        spent + in-flight + est │
                           │ 6 forward                               │
                           │ 7 meter         price real usage, log   │
                           │ 8 alert         webhook once/threshold  │
                           └─────────────────────────────────────────┘
                                 │ SQLite          │ audit.jsonl     │ Slack / Discord
```

## Quickstart
Needs Python 3.9+ and make.
```bash
make demo      # install, fresh keys, start gateway, run the scripted scenes, stop
```

Other targets:
```bash
make run                              # gateway on :8000 (WEBHOOK_URL=… for real Slack/Discord alerts)
make seed                             # reset DB + issue new keys (restart `make run` after)
make usage                            # spend report from the running gateway
make revoke EMAIL=bob@acme.com        # deactivate a user's keys
```

## What the demo shows
| Scene | What happens | Response |
|---|---|---|
| 1. Normal traffic | Alice (marketing) and Bob (engineering) make calls; cost and team spend come back in response headers | 200 |
| 2. Model RBAC | Alice's `pilot` role only allows Haiku; she asks for Opus | 403 `model_not_allowed` |
| 3. Runaway job | `ci-bot` loops Sonnet calls. At 80% of the $0.20 budget a webhook fires; then the team is cut off, **including Bob**, while marketing is unaffected | 429 `budget_exceeded` |
| 4. Burst | Alice exceeds 10 requests/minute | 429 `rate_limited` |
| 5. Offboarding | Admin deactivates Bob, as a SCIM `active: false` would; his key dies immediately | 401 `key_revoked` |
| 6. Reports | Spend by team and user, plus the audit log | — |

## Design decisions
| Concern | Decision |
|---|---|
| **Key storage** | Only a sha256 hash and a short prefix are stored. The plaintext key is shown once at issue time, like a password. |
| **Budget check** | Reserve the *worst case* before the call (estimated input + `max_tokens` × price), then bill the *actual* `usage` after. It's the gas-station pre-authorization pattern: you can't know output length in advance, so you can't overspend by more than one estimate. |
| **Concurrency** | In-flight reservations count against the budget, and the check-and-reserve step contains no `await`, so parallel requests can't all slip through the last few cents. |
| **Fail mode** | If spend can't be read, `fail_mode: closed` returns 503 (protects cost); `open` allows the call, unmetered and flagged in the audit log (protects productivity). The right choice depends on the client. Try it with `GATEWAY_SIMULATE_METERING_OUTAGE=1`. |
| **Audit log** | One JSON line per event, including rejections and admin actions. **Metadata only: prompts and completions are never logged**, because they can contain PII or confidential data. |
| **Alerts** | Idempotent: each threshold fires once per team per day (`alerts_sent` table), not on every request past the line. Slack or Discord payload shape is picked from the URL. |
| **Error format** | Same envelope as the Claude API (`{"type":"error","error":{…}}`), so existing clients handle it, plus a `gateway_reason` field. |
| **Config, not code** | Prices, roles, model allowlists, rate limits, budgets and thresholds all live in `config.json`. |

## Mapping to enterprise controls
- **Identity lifecycle:** `/admin/users/{user}/deactivate` is the hook a SCIM deprovisioning event would call.
- **RBAC:** roles map to model allowlists; in production, roles come from identity-provider groups (e.g. an Entra group `AI-Pilot-Users` maps to the `pilot` role).
- **SOC 2:** CC6.1/6.2/6.3 (logical access, provisioning, removal) via keys, roles and revocation; CC7.2 (monitoring) via the audit log and alerts.
- **FinOps:** per-team chargeback or showback from `/admin/usage`.

## Production path
In a real engagement you'd usually configure an existing gateway rather than run this one: LiteLLM Proxy, Portkey, Kong AI Gateway, Cloudflare AI Gateway, or the token-limit policies in Azure API Management. Things this v1 leaves out:
- streaming (usage arrives at the end of the stream)
- prompt-caching discounts
- a shared store (Redis or Postgres) so multiple replicas agree on spend
- key rotation
- real identity-provider integration

## Files
```
gateway/app.py            request pipeline + admin endpoints
gateway/db.py             SQLite: keys, usage, alerts_sent
gateway/mock_upstream.py  Claude-shaped fake responses (seeded)
config.json               prices, roles, budgets, thresholds, fail mode
seed.py                   reset state, issue demo keys
demo.py                   scripted scenes + local webhook receiver
cli.py                    admin CLI (usage, revoke)
```
