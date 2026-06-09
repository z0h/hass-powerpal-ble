"""Constants for the Powerpal BLE integration."""

DOMAIN = "powerpal_ble"

# BLE service / characteristic UUIDs from WeekendWarrior1/powerpal_ble
POWERPAL_SERVICE_UUID = "59daabcd-12f4-25a6-7d4f-55961dce4205"
PAIRING_CODE_UUID = "59da0011-12f4-25a6-7d4f-55961dce4205"
READING_BATCH_SIZE_UUID = "59da0013-12f4-25a6-7d4f-55961dce4205"
MEASUREMENT_UUID = "59da0001-12f4-25a6-7d4f-55961dce4205"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

CONF_PAIRING_CODE = "pairing_code"
CONF_PULSES_PER_KWH = "pulses_per_kwh"
CONF_NOTIFICATION_INTERVAL = "notification_interval"

DEFAULT_NOTIFICATION_INTERVAL = 1   # minutes between Powerpal pushing data
DEFAULT_PULSES_PER_KWH = 1000.0
