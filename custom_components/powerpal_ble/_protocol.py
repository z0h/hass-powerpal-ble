"""Pure-Python byte-level operations for the Powerpal BLE protocol.

Kept dependency-free (no HA, no bleak) so it can be unit-tested in
isolation. `powerpal_client.py` imports from here at module load.

Reference C++ source:
    https://github.com/WeekendWarrior1/esphome/blob/powerpal_ble/esphome/components/powerpal_ble/

Known correctness note vs the reference:

  The C++ computes average power within a reporting interval as:
      pulses * 60 * batch_size / (pulses_per_kwh / 1000)
  That formula is dimensionally wrong: at batch_size=1 it happens to give
  the right answer (the default and what most users run), but at
  batch_size=N it scales by N^2 instead of by 1 -- 1 pulse in 15 minutes
  at 800 ppkwh ought to be 5 W, the C++ produces 1125 W.

  The corrected derivation:
      energy_in_interval (kWh) = pulses / pulses_per_kwh
      interval_duration  (h)   = batch_size / 60
      average_power      (W)   = energy(Wh) / duration(h)
                               = (pulses * 1000 / ppkwh) / (batch_size / 60)
                               = pulses * 60000 / (batch_size * ppkwh)

  This Python port deliberately diverges from the C++ at this point.
"""
from __future__ import annotations

import struct


def encode_pairing_code(code: int) -> bytes:
    """4-byte little-endian uint32. Matches powerpal_ble.h:91-96."""
    return struct.pack("<I", code)


def encode_batch_size(minutes: int) -> bytes:
    """4-byte buffer [N, 0, 0, 0]. Matches powerpal_ble.h:129."""
    return bytes([minutes & 0xFF, 0x00, 0x00, 0x00])


def parse_measurement(data: bytes) -> tuple[int, int]:
    """[unix_time:LE u32][pulses:LE u16]. Returns (unix_time, pulses).

    Trailing bytes are ignored (the device may include extra fields).
    Caller must check `len(data) >= 6` before calling.
    """
    return struct.unpack("<IH", data[:6])


def average_power_watts(
    pulses: int,
    pulses_per_kwh: float,
    notification_interval_min: int,
) -> float:
    """Average power in watts over the reporting interval.

    See module docstring for the derivation. This is the corrected formula;
    the C++ reference implementation has a bug at notification_interval != 1.
    """
    return (pulses * 60_000.0) / (notification_interval_min * pulses_per_kwh)


def kwh_from_pulses(pulses: int, pulses_per_kwh: float) -> float:
    """Energy in kWh accumulated by `pulses` pulses on a `pulses_per_kwh` meter."""
    return pulses / pulses_per_kwh


def restore_pulses_from_snapshot(
    snapshot: dict | None,
    current_pulses_per_kwh: float,
) -> tuple[int, int, int]:
    """Decode a persisted accumulator snapshot into (total_pulses,
    daily_pulses, day_key) at the current calibration.

    Handles:
      - missing snapshot (fresh install) → (0, 0, 0)
      - pre-v0.7 snapshots with only kWh fields → back-compute pulses at the
        current rate (lossless if rate unchanged since save)
      - calibration_ppkwh-different-from-current → rescale pulses to keep
        total kWh continuous, so a `pulses_per_kwh` change via the options
        flow doesn't cause TOTAL_INCREASING to step
    """
    if not snapshot:
        return (0, 0, 0)

    total_pulses = snapshot.get("total_pulses")
    daily_pulses = snapshot.get("daily_pulses")

    if total_pulses is None and "total_energy_kwh" in snapshot:
        total_pulses = int(round(
            float(snapshot["total_energy_kwh"]) * current_pulses_per_kwh
        ))
        daily_pulses = int(round(
            float(snapshot.get("daily_energy_kwh", 0.0)) * current_pulses_per_kwh
        ))

    total_pulses = int(total_pulses or 0)
    daily_pulses = int(daily_pulses or 0)

    calibration = float(snapshot.get("calibration_ppkwh", current_pulses_per_kwh))
    if calibration != current_pulses_per_kwh and total_pulses:
        ratio = current_pulses_per_kwh / calibration
        total_pulses = int(round(total_pulses * ratio))
        daily_pulses = int(round(daily_pulses * ratio))

    day_key = int(snapshot.get("day_key", 0))
    return (total_pulses, daily_pulses, day_key)
