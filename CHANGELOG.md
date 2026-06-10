# Changelog

## v0.13.0
Concurrency/lifecycle hardening pass — five fixes from an adversarial review of the BLE client and coordinator. No new features.

- **CI activated**: the v0.6 sample workflow moved from `docs/ci-example.yml` to `.github/workflows/tests.yml` and gained `ruff` + `mypy --strict` jobs. Unit tests (43), syntax/JSON validation, lint, and HACS validation now run on every push and PR.

- **Snapshot restore can no longer destroy energy history**: an exception while seeding accumulators from a corrupt persisted snapshot left a half-initialised client whose first save overwrote the on-disk totals with zeros. Seeding failure now degrades loudly to fresh accumulators, and `restore_pulses_from_snapshot` tolerates garbage fields (strings, lists, non-dict payloads) per-field. The `inf`-calibration case — which silently zeroed totals via a `current/inf == 0` rescale — is guarded with `math.isfinite` (the previous `> 0` check let it through).
- **No more false "unavailable" while connected**: Powerpal stops advertising while a central is connected, so HA's advertisement-unavailability tracker fires on every healthy session; sensors flapped unavailable until the next measurement (up to a full notification interval). The coordinator now ignores the tracker while the GATT link is up, and `start()` repairs published state if it had been flipped.
- **Stale disconnect callbacks ignored**: a late `disconnected_callback` from a superseded bleak client could mark a freshly established connection as dead and trigger a teardown/reconnect loop. Callbacks are now matched against the currently-owned client object.
- **Connect-attempt dedup + 30 s retry cooldown**: during an outage, every advertisement queued a fresh `establish_connection` cycle behind the connect lock (log spam, adapter contention, blocked unload). One attempt in flight at a time; failures arm a cooldown.
- **Coordinator lifecycle gate**: explicit `_stopped` flag — post-stop advertisement tasks can no longer construct a zombie client, and a notification arriving during shutdown can no longer re-arm a delayed save after the flush (stale-totals window across an options-flow reload).
- **Manual address entry validated**: non-canonical MAC input (`aabbccddeeff`, dashes, whitespace) previously created a permanently dead entry with no feedback — the bluetooth matcher never fired. Input is now normalised via `format_mac`, validated, and rejected with a form error.

## v0.12.0
- **`encode_batch_size` fails loud on overflow**: previously silently `&0xFF`-truncated, which would write `batch_size=0` (undefined Powerpal behaviour) if `notification_interval` were ever widened past 255 in the config flow. Now raises `ValueError`. Config flow still caps at 60 — the guard is defense in depth.
- **`parse_measurement` contract**: short payloads (`len < 6`) now raise `ValueError` rather than relying on the caller. `_handle_measurement` already guards `len < 6` so this is unreachable from production but tightens the unit-test surface.
- **Cleanups**: hoisted `async_address_present` import in `__init__.py`; documented intentional plaintext pairing-code storage in `config_flow.py`; tightened `_settings_from_entry` return type to `dict[str, Any]`.
- **Distinct energy entity IDs**: previously both kWh sensors fell back to the `ENERGY` device-class default name "Energy" (no `translations/<lang>.json` was shipped, so the `translation_key` lookup didn't resolve), producing colliding `sensor.<mac>_energy` and `sensor.<mac>_energy_2` entity IDs. Each `SensorEntityDescription` now sets `name` directly, so a fresh setup yields `sensor.<mac>_power`, `sensor.<mac>_daily_energy`, `sensor.<mac>_total_energy`, and `sensor.<mac>_battery`.
- **Upgrade note**: HA's entity registry persists `entity_id` against the entity's `unique_id`, and remove + re-add does *not* free the old `entity_id` (HA keeps the entry as an orphan and re-adopts it on next setup). Existing installs therefore keep `_energy` / `_energy_2` until each sensor is renamed in the UI: Settings → Devices & Services → Powerpal → click the sensor → ⚙ → change "Entity ID" to `…_daily_energy` and `…_total_energy`. Long-term statistics follow the registry entry and are preserved across the rename; Energy Dashboard cards reference the new ID once renamed.

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
