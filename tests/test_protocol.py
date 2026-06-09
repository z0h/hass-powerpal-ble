"""Unit tests for the byte-level protocol port.

These tests don't require Home Assistant or bleak — they exercise the
real `_protocol.py` module directly. Run with:

    python3 -m pytest tests/

or just:

    python3 tests/test_protocol.py
"""
from __future__ import annotations

import importlib.util
import os
import struct
import unittest

# Load _protocol.py directly without going through the package __init__
# (which would pull in homeassistant, not installed for unit tests).
_HERE = os.path.dirname(__file__)
_PROTOCOL_PATH = os.path.join(
    _HERE, "..", "custom_components", "powerpal_ble", "_protocol.py"
)
_spec = importlib.util.spec_from_file_location("_protocol", _PROTOCOL_PATH)
_protocol = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_protocol)

average_power_watts = _protocol.average_power_watts
encode_batch_size = _protocol.encode_batch_size
encode_pairing_code = _protocol.encode_pairing_code
kwh_from_pulses = _protocol.kwh_from_pulses
parse_measurement = _protocol.parse_measurement


class PairingCodeEncoding(unittest.TestCase):
    def test_smallest_code(self):
        self.assertEqual(encode_pairing_code(1), b"\x01\x00\x00\x00")

    def test_six_digit_typical(self):
        # 532667 = 0x000820BB → BB 20 08 00 LE
        self.assertEqual(encode_pairing_code(532667), b"\xbb\x20\x08\x00")

    def test_eight_digit_max(self):
        # 99999999 = 0x05F5E0FF → FF E0 F5 05 LE
        self.assertEqual(encode_pairing_code(99_999_999), b"\xff\xe0\xf5\x05")

    def test_always_four_bytes(self):
        for code in (1, 100, 999999, 99_999_999, 0xFFFFFFFF):
            self.assertEqual(len(encode_pairing_code(code)), 4)


class BatchSizeEncoding(unittest.TestCase):
    def test_one_minute(self):
        self.assertEqual(encode_batch_size(1), b"\x01\x00\x00\x00")

    def test_fifteen_minutes(self):
        self.assertEqual(encode_batch_size(15), b"\x0f\x00\x00\x00")

    def test_hour(self):
        self.assertEqual(encode_batch_size(60), b"\x3c\x00\x00\x00")

    def test_overflow_truncates(self):
        # The C++ field is uint8_t; we mask &0xFF.
        self.assertEqual(encode_batch_size(256), b"\x00\x00\x00\x00")


class MeasurementParsing(unittest.TestCase):
    def test_typical_payload(self):
        payload = struct.pack("<IH", 1718000000, 42)
        ts, pulses = parse_measurement(payload)
        self.assertEqual(ts, 1718000000)
        self.assertEqual(pulses, 42)

    def test_zero_pulses(self):
        _, pulses = parse_measurement(struct.pack("<IH", 1718000000, 0))
        self.assertEqual(pulses, 0)

    def test_max_pulses_in_u16(self):
        _, pulses = parse_measurement(struct.pack("<IH", 1718000000, 0xFFFF))
        self.assertEqual(pulses, 65535)

    def test_extra_bytes_ignored(self):
        payload = struct.pack("<IH", 1718000000, 42) + b"\xde\xad\xbe\xef"
        _, pulses = parse_measurement(payload)
        self.assertEqual(pulses, 42)


class PowerCalculation(unittest.TestCase):
    """power_W = pulses * 60000 / (notification_interval * pulses_per_kwh).

    Reads correctly at all batch sizes — the C++ reference has a bug here
    that this Python port deliberately corrects. See `_protocol.py` docstring.
    """

    def test_one_pulse_per_minute_at_800_ppkwh(self):
        # 800 ppkwh = 1.25 Wh per pulse. 1 pulse in 60s = 75 W average.
        self.assertAlmostEqual(average_power_watts(1, 800, 1), 75.0)

    def test_one_pulse_per_minute_at_1000_ppkwh(self):
        self.assertAlmostEqual(average_power_watts(1, 1000, 1), 60.0)

    def test_ten_pulses_per_minute_at_1000_ppkwh(self):
        self.assertAlmostEqual(average_power_watts(10, 1000, 1), 600.0)

    def test_idle_household(self):
        self.assertEqual(average_power_watts(0, 800, 1), 0.0)

    def test_longer_batch_interval_does_NOT_inflate(self):
        # 1 pulse over 15 minutes on an 800 ppkwh meter = 5 W average.
        # The C++ reference would (incorrectly) give 1125 W here.
        self.assertAlmostEqual(average_power_watts(1, 800, 15), 5.0)

    def test_one_hour_interval(self):
        # 1 pulse in 60 min on 1000 ppkwh = 1 Wh / 1 h = 1 W average.
        self.assertAlmostEqual(average_power_watts(1, 1000, 60), 1.0)


class EnergyAccumulation(unittest.TestCase):
    def test_total_energy_from_pulse_count(self):
        # 800 pulses at 800 ppkwh = 1 kWh exactly.
        self.assertAlmostEqual(kwh_from_pulses(800, 800.0), 1.0)

    def test_partial_kwh(self):
        # 1 pulse at 1000 ppkwh = 0.001 kWh = 1 Wh.
        self.assertAlmostEqual(kwh_from_pulses(1, 1000.0), 0.001)

    def test_zero_pulses(self):
        self.assertEqual(kwh_from_pulses(0, 800.0), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
