# hass-powerpal-ble

A Home Assistant custom integration that talks to a [Powerpal](https://powerpal.net)
energy monitor directly over Bluetooth Low Energy — no cloud, no ESPHome
custom firmware required.

The Bluetooth protocol implementation is a Python port of
[WeekendWarrior1/powerpal_ble](https://github.com/WeekendWarrior1/powerpal_ble)
(ESPHome external component). Credit to that project for working out the
GATT protocol. This repo makes it available as a HA-native integration.

## How it works

Home Assistant's Bluetooth integration receives advertisements from any
combination of: a local adapter on the HA host, USB BT dongles, or
[ESPHome Bluetooth Proxies](https://esphome.io/projects/?type=bluetooth-proxy/)
elsewhere on your LAN. When this integration sees an advertisement matching
your configured Powerpal, it opens a GATT connection *through whichever
scanner saw it best*, performs the pairing handshake, subscribes to
measurement notifications, and exposes:

| Entity | Unit | State class |
|---|---|---|
| `sensor.<name>_power` | W | measurement |
| `sensor.<name>_daily_energy` | kWh | total_increasing |
| `sensor.<name>_total_energy` | kWh | total_increasing |
| `sensor.<name>_battery` | % | diagnostic |

Total and daily energy accumulators are persisted to HA storage so they
survive restarts (Energy Dashboard works correctly across reboots).

## Differences from the C++ reference

This Python port deliberately diverges from the reference at one point:

- **Power formula at non-default notification intervals.** The C++ computes
  `pulses * 60 * batch / (ppkwh/1000)`, which is dimensionally wrong when
  `batch != 1` — it scales by `batch²` instead of by 1. At the default
  1-minute interval the two formulas coincide. At a 15-minute interval the
  C++ reads ~225× too high. This port uses the correct formula
  `pulses * 60000 / (interval × ppkwh)`. See `_protocol.py` docstring and
  `tests/test_protocol.py::PowerCalculation::test_longer_batch_interval_does_NOT_inflate`.

## Install

### Via HACS (recommended)

1. HACS → ⋮ → Custom repositories → add
   `https://github.com/z0h/hass-powerpal-ble` as type `Integration`.
2. Click into the repo → **DOWNLOAD** → restart Home Assistant.
3. Settings → Devices & Services → **+ ADD INTEGRATION** → search "Powerpal".
   If the device is in range of any HA bluetooth scanner, a discovery
   card appears automatically.

### Manual

Copy `custom_components/powerpal_ble/` into `<config>/custom_components/`
and restart Home Assistant.

## Configuration

You'll need:

- **Pairing code** — in the Powerpal phone app under device settings.
  A decimal number, up to 8 digits.
- **Pulses per kWh** — printed on your electricity meter
  (e.g. `1000 imp/kWh`, `800 imp/kWh`). Common AU values: `800` (3-phase),
  `1000` (single-phase).
- **Notification interval** — minutes between measurement notifications.
  `1` (default) gives a power reading every minute. Higher values are
  gentler on the Powerpal battery and the BLE link.

After setup, **Settings → Devices & Services → Powerpal → Configure**
lets you adjust pulses/kWh and the notification interval without re-pairing.

## Bluetooth range and proxies

Powerpal lives on your electricity meter — usually outside the house — so
a typical indoor BLE adapter or laptop won't have a usable link to it. The
reliable setup is a small ESP32 with the standard ESPHome
[`bluetooth-proxy`](https://esphome.github.io/bluetooth-proxies/) firmware
plugged into a powerpoint near the meter. Home Assistant routes the GATT
calls through it transparently.

Aim for an RSSI of at least **-80 dBm** at the proxy's position for
reliable operation. -90 dBm and below works but you'll see occasional gaps.

## Cross-OS notes

HA's bluetooth integration represents devices by MAC on Linux and by
CoreBluetooth UUID on macOS. The integration uses whatever HA gives it as
the device's unique ID. **Migrating an HA install between Linux and macOS
will lose the integration's link** to the device — you'll need to re-add
it on the new host. Persistent accumulator state in HA storage is
unaffected by the bluetooth address change but isn't auto-migrated to a
new entry. (A future version may key the unique_id off the Powerpal's
serial number instead, which would be cross-OS-stable.)

## Testing

Pure-Python unit tests for the byte-level protocol logic (no HA, no
bleak runtime needed):

```
python3 tests/test_protocol.py
```

31 tests covering pairing-code encoding, batch-size buffer, measurement
parsing, power calculation across batch sizes, energy accumulation, and
the snapshot restore path (v0.5→v0.7 migration + ppkwh-change rescaling +
corrupted-snapshot guards).

`ruff check` and `mypy --strict` (on the pure-Python `_protocol.py`)
should both run clean.

End-to-end integration testing requires installing the integration in HA
against a live Powerpal. There is no test harness for the BLE state machine
itself — review the connection state machine in `powerpal_client.py`
directly if you suspect a bug there.

## Troubleshooting

**Sensors show "unavailable" forever after install.**
The device is either out of range of every HA bluetooth scanner or it's
not advertising right now (Powerpal is sparse on battery). Wait a couple
of minutes for the next advertisement.

**Sensors come available, then never update.**
Pairing code is probably wrong. The Powerpal accepts any code at GATT
level and just doesn't push notifications for an incorrect one. Watch
for a `Powerpal ... received no measurements after N minutes` line in
the HA log (emitted ~2x the notification interval after first connect).

**`Total Energy` resets to zero on HA restart.**
Shouldn't happen as of v0.5 (pulses are persisted via HA's `Store`). If
it does, dump diagnostics and file an issue.

**`Total Energy` decreases when I change `pulses_per_kwh`.**
Shouldn't happen as of v0.7 (snapshot includes `calibration_ppkwh` and
pulses are rescaled to preserve kWh). If it does, dump diagnostics.

## License

MIT — see `LICENSE`.
