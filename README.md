# SlopeCast

Landslide watch for typhoon-season hillsides. Sensor stakes measure soil wetness and tilt, an Apify Actor supplies rainfall, the Claude API (checked by rules) picks which pre-drawn landslide paths are likely to be hit, and a people engine works out who is exposed, how long they have and who needs help. Residents, the rescue committee and a central dashboard all read the same approved plan.

> Demonstration project. One inexpensive stake cannot reliably predict a landslide. Hazard zones are drawn for the demo, timing is experimental, and every household is synthetic. Localhost demo only: not a secure deployment.

```
stakes (USB) ─┐
              ├─> hazard engine (Claude + rules) ─┐
Apify weather ┘                                   ├─> people engine + planner ─> plan ─> residents / committee / dashboard
                       housing + people database ─┘
```

## Setup (once)

Python 3.11 or newer. The backend uses only the standard library plus `pyserial`.

```
rm -f uv.lock                 # the committed lock file is empty; let uv make a real one
uv sync                       # or: pip install pyserial pytest
cp .env.example .env          # then add ANTHROPIC_API_KEY and APIFY_TOKEN if you have them
uv run pytest                 # 117 tests, no network or hardware needed
```

Both keys are optional. Without `ANTHROPIC_API_KEY` the hazard engine is rule-based; without `APIFY_TOKEN` weather comes from Open-Meteo directly, then from a saved series (all labelled on screen). For Apify, put your Actor in `config/rainfall-policy.json` → `apify_actor_id` (format `owner~actor-name`).

## Run

```
uv run python -m backend                                                   # LIVE: Arduino over USB
uv run python -m backend --replay tests/fixtures/scenarios/escalating.json  # REPLAY: no hardware
```

Open the links it prints:

| View | URL |
| --- | --- |
| Central dashboard (operator) | http://localhost:8000/?view=dashboard&token=operator-demo |
| Rescue committee | http://localhost:8000/?view=committee&token=committee-demo |
| Resident (household H02) | http://localhost:8000/?view=resident&token=resident-h02 |
| Public dashboard | http://localhost:8000/?view=dashboard |

Options: `--speed 120` (replay speed), `--no-claude`, `--no-filler` (live: don't simulate N02/N03), `--serial /dev/cu.usbmodem1101` (skip auto-detect), `--http-port 8001`, `--keep` (keep the last run's data; default starts clean).

Headless check of a scenario: `uv run python tools/replay.py tests/fixtures/scenarios/urgent-review.json --no-claude`

## Hardware

| Part | Connects to |
| --- | --- |
| Arduino Uno R4 (USB-C) + Grove Base Shield v2 (switch on **5V**) | Laptop by USB |
| Grove Moisture Sensor | **A0** |
| Grove 3-axis accelerometer (ADXL345 or LIS3DHTR) | any **I2C** port |
| Printed stake + lid | `hardware/stl/`, notes in `hardware/print-notes.md` |

Upload `firmware/slope_node/slope_node.ino` with the Arduino IDE (board package "Arduino UNO R4 Boards"). Serial Monitor at 115200 should show a `hello` line then one `reading` per second. **Close Serial Monitor before starting the backend**: only one program can hold the port. Format: `docs/serial-protocol.md`.

Calibration (optional but better): `uv run python -m backend.sensors calibrate --port <port> --step dry|wet|baseline|downslope`. Without it, the backend uses labelled typical moisture values and takes the first 10 still seconds after connecting as the tilt baseline.

More stakes: change `NODE_ID` in the firmware per board, add the node to `config/site.json`, plug all of them in (a USB hub works).

## What every file does

| File | Job |
| --- | --- |
| `firmware/slope_node/slope_node.ino` | Uno firmware: detects the accelerometer, averages readings, sends one JSON line per second with health flags |
| `backend/__main__.py` | `python -m backend`: parses options, starts the server and background tasks, prints the links |
| `backend/app.py` | Web server, JSON API, the four views' data, CSV export, serial auto-detect, weather and tick tasks |
| `backend/pipeline.py` | The conductor: ingests readings and weather, runs hazard → people → planner in order, applies approvals, job acceptances and progress |
| `backend/schemas.py` | The shape of every record (readings, assessments, plans, messages...) |
| `backend/settings.py` | Reads `.env` and `config/*.json` once and checks them |
| `backend/clock.py` | Real time in live mode, a faster simulated clock in replays |
| `backend/db.py` | SQLite storage in `var/` with the uniqueness rules (no duplicate readings, plans or messages) |
| `backend/security.py` | Demo tokens and roles; removes personal details from the public view |
| `backend/sensors.py` | Parses the Uno's lines, handles restarts and duplicates, computes wetness and tilt; bench commands |
| `backend/weather.py` | Apify Actor call, Open-Meteo fallback, cache and offline file; rainfall totals and threshold crossing |
| `backend/households.py` | Loads and validates households, people, buildings, shelters, zones, teams; point-in-polygon and distance |
| `backend/hazard.py` | Builds evidence, calls Claude with a forced tool, checks the answer, rule-based fallback, inverse-velocity timing fit |
| `backend/people.py` | Exposure per household, assistance from stated needs, preparation and travel time, shelter choice |
| `backend/planner.py` | Planning horizon, leave-by and team arrival times, team assignment, infeasible jobs |
| `backend/notifications.py` | Message templates, simulated outbox, one message per household per plan, acknowledgements |
| `backend/static/index.html` | The single page; loads the view named in `?view=` |
| `backend/static/api.js` | The only browser code that calls the backend; time formatting |
| `backend/static/dashboard.js` | Map of paths, homes, stakes and shelters; stakes, rain, hazard panels; plan table and approve button |
| `backend/static/committee.js` | Assistance queue with Accept / Arrived / Evacuated, teams, unacknowledged households |
| `backend/static/resident.js` | Large-type instruction in English or Chinese with "I've seen this" and "We have left" |
| `backend/static/styles.css` | Shared styling |
| `config/site.json` | Site, stakes (position, sector, live or simulated), data file paths |
| `config/calibration.json` | Per-stake moisture and tilt calibration, plus labelled defaults |
| `config/rainfall-policy.json` | Apify Actor, fallbacks, refresh interval, rainfall threshold |
| `config/hazard-policy.json` | Claude model and limits, rule thresholds, timing-fit rules |
| `config/hazard-prompt.md` | Instructions sent to Claude |
| `config/evacuation-policy.json` | Preparation minutes, speeds, safety buffer, default horizons |
| `config/messages.json` | Resident message templates (en, zh-Hant) |
| `data/demo/*.json`, `hazard-zones.geojson` | Synthetic households, shelters, teams, users and hand-drawn landslide paths |
| `tools/import_demo.py` | Loads demo data into the database (`--reset` clears runtime data) |
| `tools/replay.py` | Scenario player and the simulated filler stakes; headless replay |
| `tests/` | 117 tests; `tests/fixtures/scenarios/` holds the five demo scenarios |
| `hardware/` | Stake enclosure (`enclosure.scad`, STLs, print notes) |
| `docs/` | Serial protocol and API reference |

## Honest limits

Zones are drawn for the demo, not taken from an official hazard map. Claude chooses among those zones and explains; it does not model slope physics, and code checks every answer. Timing appears only when a stake's tilt fits an accelerating-creep model, and is labelled experimental. Default horizons are a demo planning setting, shown as such. Messages are simulated. All people are synthetic.
