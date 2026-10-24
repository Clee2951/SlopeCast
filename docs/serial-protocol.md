# Serial protocol: slope node → laptop gateway

Shared contract between `firmware/slope_node/slope_node.ino` (producer) and `backend/sensors.py` (consumer). Change both together and bump `v` if a field changes meaning.

## Link

- USB serial, **115200 baud**, 8N1.
- One JSON object per line, terminated by `\n` (`\r\n` is accepted).
- Lines that are not valid JSON objects, such as boot noise or partial lines after a reconnect, are counted and dropped by the gateway. They are never turned into readings.
- The gateway sends `?\n` after opening the port. The node answers with a `hello` record. The Uno R4 does not reset when the port opens, so this is how the gateway learns the boot ID and accelerometer model mid-run.

## Record: `hello`

Sent once at power-up, again after `?`, and again when a lost accelerometer is re-detected.

```json
{"v":1,"type":"hello","node":"N01","boot":1804289383,"up_ms":12,"fw":"1.0.0","acc_model":"ADXL345","acc_addr":83}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `v` | int | Protocol version (currently 1) |
| `type` | string | `"hello"` |
| `node` | string | Stable node ID, set by `NODE_ID` in the firmware |
| `boot` | int | Random ID chosen at each power-up. A new value means the Uno restarted. |
| `up_ms` | int | Milliseconds since power-up |
| `fw` | string | Firmware version |
| `acc_model` | string | `ADXL345`, `LIS3DHTR`, `MMA7660` (unsupported) or `none` |
| `acc_addr` | int | I2C address found (decimal), 0 if none |

## Record: `reading`

Sent every second.

```json
{"v":1,"type":"reading","node":"N01","boot":1804289383,"seq":42,"up_ms":42010,"moist_raw":512,"moist_min":508,"moist_max":517,"ax":0.0120,"ay":-0.0040,"az":0.9980,"acc_units":"g","acc_model":"ADXL345","acc_n":20,"fw":"1.0.0","flags":[]}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `v`, `type`, `node`, `boot`, `fw` | | As in `hello`; `type` is `"reading"` |
| `seq` | int | Starts at 1 after each boot and increases by 1 per reading. Unique within (`node`, `boot`). |
| `up_ms` | int | Milliseconds since power-up when the reading finished. The node has no clock; the gateway maps this to UTC. |
| `moist_raw` | int | Mean of 20 analog reads of A0, raw ADC counts 0–1023. Higher means wetter. Not a percentage. |
| `moist_min`, `moist_max` | int | Lowest and highest of those 20 reads |
| `ax`, `ay`, `az` | float or `null` | Mean acceleration on each sensor axis, in g, over the samples that succeeded. `null` if no sample succeeded. |
| `acc_units` | string | Always `"g"` |
| `acc_model` | string | Model that produced the values, `none` if unavailable |
| `acc_n` | int | Accelerometer samples that succeeded, out of 20 |
| `flags` | array of strings | Health flags, empty when healthy (see below) |

## Health flags

| Flag | Raised when | Gateway treatment |
| --- | --- | --- |
| `accel_missing` | No supported accelerometer was found on I2C | Tilt unavailable; monitoring degraded |
| `accel_unsupported_model` | Only the ±1.5 g MMA7660 was found | Tilt unavailable; swap the module |
| `accel_read_failed` | The accelerometer was found but all 20 reads failed this second. Firmware re-detects it. | Tilt unavailable for this reading |
| `accel_partial` | Some, but not all, of the 20 reads failed | Values usable; quality degraded |
| `moisture_unstable` | `moist_max − moist_min` > 120 counts. Typical of an unplugged sensor (floating input). | Wetness unavailable for this reading |
| `moisture_rail_high` | `moist_raw` ≥ 1020, pinned at the top of the range | Wetness unavailable; check wiring |

A missing value is `null` plus a flag, never `0`. A dry Grove moisture sensor really can read near 0, so a low value alone is not a fault.

## Gateway rules (implemented in `backend/sensors.py`)

- **Session** = (`node`, `boot`). A new `boot` value starts a new session; history from earlier sessions is kept.
- **Duplicates**: a second reading with the same (`node`, `boot`, `seq`) is dropped.
- **Time**: for each session the gateway keeps `anchor = min(receipt_utc − up_ms)` over the readings so far, and sets `sample_time_utc = anchor + up_ms`. USB delay only ever makes receipt later, so the minimum is the best estimate. Receipt time is stored as well.
- **Units**: acceleration stays in g; tilt is derived in degrees from a stored installation baseline, never by integrating acceleration.

## Example sequence

```
{"v":1,"type":"hello","node":"N01","boot":1804289383,"up_ms":12,"fw":"1.0.0","acc_model":"ADXL345","acc_addr":83}
{"v":1,"type":"reading","node":"N01","boot":1804289383,"seq":1,"up_ms":1001,"moist_raw":301,...,"flags":[]}
{"v":1,"type":"reading","node":"N01","boot":1804289383,"seq":2,"up_ms":2001,"moist_raw":302,...,"flags":[]}
(accelerometer cable pulled)
{"v":1,"type":"reading","node":"N01","boot":1804289383,"seq":3,"up_ms":3001,"moist_raw":301,"ax":null,"ay":null,"az":null,"acc_model":"none","acc_n":0,...,"flags":["accel_read_failed"]}
{"v":1,"type":"reading","node":"N01","boot":1804289383,"seq":4,"up_ms":4001,...,"flags":["accel_missing"]}
(power cycled)
{"v":1,"type":"hello","node":"N01","boot":846930886,"up_ms":11,...}
{"v":1,"type":"reading","node":"N01","boot":846930886,"seq":1,"up_ms":1001,...}
```
