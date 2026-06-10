# Changelog

## v0.12.0
- **Distinct energy entity IDs**: previously both kWh sensors fell back to the `ENERGY` device-class default name "Energy" (no `translations/<lang>.json` was shipped, so the `translation_key` lookup didn't resolve), producing colliding `sensor.<mac>_energy` and `sensor.<mac>_energy_2` entity IDs. Each `SensorEntityDescription` now sets `name` directly, so a fresh setup yields `sensor.<mac>_power`, `sensor.<mac>_daily_energy`, `sensor.<mac>_total_energy`, and `sensor.<mac>_battery`.
- **Upgrade note**: HA's entity registry remembers the entity_id chosen at first registration, so existing installs keep `_energy` / `_energy_2` until the integration is removed and re-added (Settings → Devices & Services → Powerpal → ⋮ → Delete, then re-discover). The Energy Dashboard will need the new `_daily_energy` / `_total_energy` IDs added after re-adding. Long-term statistics from the old IDs are not carried over.

## v0.10.0
- Pairing-code-timeout watchdog. Wrong codes are now diagnosed in the log instead of being silently invisible (Powerpal accepts any 32-bit value at GATT level and just stops sending notifications for an incorrect code).

## v0.9.0
- Lint sweep. `ruff check` and `mypy --strict` (on the pure-Python `_protocol.py`) both clean.
- Removed unreachable coroutine-callback branch in `_notify_listeners` (RUF006: dangling task) — `_protocol.restore_pulses_from_snapshot` typed `dict[str, Any]`.
- Cosmetic: `contextlib.suppress` in place of `try/except/pass`; removed redundant `int(round(...))` casts.
- No behaviour changes.

## v0.8.0
- **Stuck-unavailable fix**: receiving a measurement now flips `state.connected = True`, so a transient HA-bluetooth unavailability event followed by a continued GATT link no longer wedges sensors at unavailable.
- **Off-loop disconnect**: `_on_disconnect` is now routed through the captured event loop via `_dispatch_on_loop` for portability across bleak backends.
- **Daily-pulses leak guard**: if a restored snapshot is missing `day_key`, `daily_pulses` is zeroed to prevent it leaking across an unknown calendar boundary.
- Corrupted `calibration_ppkwh` values (zero, NaN, non-numeric) are guarded; fall back to "no rescale".
- `_dispatch_on_loop` wraps the call in `contextlib.suppress(RuntimeError)` for the TOCTOU window where the loop closes between `is_closed()` and call.
- `diagnostics.async_get_config_entry_diagnostics` no longer `AttributeError`s when invoked between a failed setup and a retry.

## v0.7.0
- **Pulses-canonical persistence**: `total_pulses` is now the source of truth in storage; kWh values are derived for human readability. Snapshot includes `calibration_ppkwh` so a later change of `pulses_per_kwh` (via options flow) rescales pulses to preserve total kWh continuity (otherwise `state_class=TOTAL_INCREASING` would step).
- **Pre-v0.7 migration**: a snapshot missing `total_pulses` but containing `total_energy_kwh` is back-computed at the current rate. Existing users' Energy Dashboard history survives the upgrade.
- **Manifest fix**: dropped `service_uuid` from the bluetooth matcher — Powerpal doesn't advertise the GATT UUID, so requiring it was silently breaking auto-discovery.
- **Listener exception isolation**: a single listener raising no longer breaks iteration or skips persistence.
- **First-connect flicker fix**: `state.connected = True` is set before the battery setup block, so an initial battery notification publishes against the right state.
- Storage debounce dropped 300s → 60s for tighter resilience to hard crashes.

## v0.6.0
- HA diagnostics support (`diagnostics.py`). Redacts pairing code and BLE address.
- Persistence fix: kWh-keyed storage so a change to `pulses_per_kwh` doesn't step the cumulative total downward (would violate TOTAL_INCREASING).
- Docs: `docs/ci-example.yml` — GitHub Actions recipe (move to `.github/workflows/tests.yml` if you want CI).

## v0.5.0
- Options flow: `pulses_per_kwh` and `notification_interval` can be tuned after setup without removing + re-adding the integration.
- Storage-backed accumulators via HA's `Store`. `total_pulses`, `daily_pulses`, `day_key` persist across HA restarts so `TOTAL_INCREASING` sensors don't reset.
- Setup-time INFO log if the device isn't currently in range.
- README rewrite.

## v0.3.0
- **Power formula corrected** — the C++ reference computes `pulses * 60 * batch / (ppkwh/1000)`, which is dimensionally wrong when `batch != 1` (scales by `batch²`). At `notification_interval=15` and 800 ppkwh, the C++ reads 1125 W for 1 pulse; the correct value is 5 W. This port uses `pulses * 60000 / (interval * ppkwh)`. Default `interval=1` masks the bug for most users.
- `_authenticated` flag introduced; mid-handshake exceptions now tear down cleanly instead of leaving a wedged "connected but unauthenticated" client.
- `_protocol.py` extracted; tests import the real implementation.

## v0.2.0
- `async_track_unavailable` for entity availability when the device disappears.

## v0.1.0
- Initial release.
