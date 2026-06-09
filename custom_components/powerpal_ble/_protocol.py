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
