# SlopeCast API

Served by `backend/app.py` on `http://localhost:8000`. All responses are JSON unless noted. Times are UTC ISO 8601; the browser converts them to site time using `utc_offset_hours`.

**Sign-in (demo only).** Send `Authorization: Bearer <token>` or `?token=<token>`. Tokens are in `data/demo/users.json`: `operator-demo`, `committee-demo`, `resident-h01` ... `resident-h24`. No token = public. This is a localhost demo, not a secure login.

| Role | Can |
| --- | --- |
| public | `GET /api/dashboard` with names, assistance needs, checklists and messages removed |
| resident | `GET /api/me/plan`, `POST /api/progress` for their own household only |
| committee | queue, plans, CSV, accept jobs, record arrivals and evacuations; sees assistance details |
| operator | everything, plus approve plans, set the horizon, force a reassessment |

## Read

### `GET /api/health`
`{"ok", "mode": "live"|"replay", "readings", "nodes": {"N01": true|false (live in last 10 s)}, "weather", "claude"}`

### `GET /api/dashboard`
Everything the central dashboard shows:
- `nodes[]`: position, `status` (ok / degraded / unavailable / no-data), source, simulated, wetness, tilt, trend, reasons
- `zones[]`: polygon `ring` ([lon, lat]), `likelihood` from the latest assessment (null = not named), evidence nodes, reasons, origin
- `buildings[]`: position, household IDs, `exposed`, `likelihood`, `needs_help`, `status`
- `shelters[]`, `weather` (totals, hourly series, source, stale), `assessment` (risk level, engine, summary, gaps, timing, check notes)
- `plan` (latest version: id, version, state, horizon and its basis, counts), `approved_plan`
- `rows[]`: one per exposed household (see CSV columns below); filtered for public
- `countdown`: earliest leave-by not yet evacuated; `events[]`; `outbox[]` (operator only)

### `GET /api/committee/queue` (committee, operator)
Households needing help or flagged infeasible, sorted by team arrival deadline; `teams[]` with active jobs; `unacknowledged_high[]`.

### `GET /api/me/plan` (resident)
The household's instruction from the **approved** plan: `action_needed`, `leave_by_utc`, `prepare_by_utc`, `zone`, `destination`, `checklist`, `help` (`state`: requested / proposed / accepted / arrived, team, arrival time), `message`, `progress`. Before any approval: `plan_id: null`.

### `GET /api/plans/{plan_id}` (committee, operator)
The stored plan record.

### `GET /api/plans/{plan_id}/export.csv` (committee, operator)
Columns: household, name, people, zone, likelihood, needs help, assistance, prep min, travel min, start preparing by, leave by, team, team arrival by, destination, feasible, status, problems. Times in site local time.

## Change

| Endpoint | Role | Body | Result / errors |
| --- | --- | --- | --- |
| `POST /api/plans/{plan_id}/approve` | operator | `{"version": 7}` | Approves that exact version and queues one simulated message per household. **409** if a newer version exists. Repeating it never sends twice. |
| `POST /api/horizon` | operator | `{"minutes": 90}` or `{"minutes": null}` | Sets or clears the operator planning horizon (5 min to 72 h); replans. |
| `POST /api/reassess` | operator | `{}` | Forces a new hazard assessment now (skips the rate limit). |
| `POST /api/assignments/{assignment_id}/accept` | committee, operator | `{}` | Team accepts the job; kept in all later plans. **409** if not in the latest plan. |
| `POST /api/progress` | resident (own household), committee, operator | `{"kind": "acknowledged"\|"team_arrived"\|"evacuated", "household_id": "H02", "team_id": "T2", "event_id": "unique"}` | Records once per `event_id`. Residents may omit `household_id`. `team_arrived` needs committee or operator. Acknowledging is not evacuating. |

Errors: `400` bad input, `403` wrong role or another household, `404` unknown, `409` stale version or invalid state, `500` server error (see the terminal).
