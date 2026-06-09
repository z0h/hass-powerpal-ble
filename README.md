# hass-powerpal-ble

A Home Assistant custom integration that talks to a [Powerpal](https://powerpal.net)
energy monitor directly over Bluetooth Low Energy — no cloud, no ESPHome
custom firmware required.

The Bluetooth protocol implementation is a Python port of
[WeekendWarrior1/powerpal_ble](https://github.com/WeekendWarrior1/powerpal_ble)
(ESPHome external component). Credit to that project for working out the
GATT protocol; this repo just makes it available as a HA-native integration.

## How it works

Home Assistant's Bluetooth integration receives advertisements either from a
local adapter or from any [ESPHome Bluetooth Proxy](https://esphome.io/projects/?type=bluetooth-proxy/)
on your network. When this integration sees an advertisement matching your
configured Powerpal, it opens a GATT connection through whichever scanner
saw it best, performs the pairing handshake, subscribes to measurement
notifications, and exposes:

| Entity | Unit | Type |
|---|---|---|
| `sensor.<name>_power` | W | measurement |
| `sensor.<name>_daily_energy` | kWh | total_increasing |
| `sensor.<name>_total_energy` | kWh | total_increasing |
| `sensor.<name>_battery` | % | diagnostic |

## Install

### Via HACS (recommended)

1. HACS → ⋮ → Custom repositories → add
   `https://github.com/z0h/hass-powerpal-ble` as type `Integration`.
2. Click into the repo, **DOWNLOAD**, then restart Home Assistant.
3. Settings → Devices & Services → **+ ADD INTEGRATION** → search "Powerpal".

### Manual

Copy `custom_components/powerpal_ble/` into `<config>/custom_components/` and
restart Home Assistant.

## Configuration

You'll need three values:

- **Pairing code** — in the Powerpal phone app under device settings.
  An 8-digit decimal number (treated as an unsigned 32-bit int).
- **Pulses per kWh** — printed on your electricity meter (e.g. `1000 imp/kWh`,
  `800 imp/kWh`). Common Australian values are 800 (three-phase) and 1000.
- **Notification interval** — minutes between measurement notifications.
  `1` means a power reading every minute. `15` is more battery-friendly.

## Bluetooth range

Powerpal is on your electricity meter (usually outdoor), and a typical
indoor BLE adapter or laptop won't have a usable link to it. The reliable
setup is a small ESP32 with the standard ESPHome `bluetooth-proxy`
firmware plugged into a powerpoint near the meter — Home Assistant routes
the GATT calls through it transparently.

## License

MIT — see `LICENSE`.
