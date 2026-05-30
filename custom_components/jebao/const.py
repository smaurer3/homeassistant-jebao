"""Constants for the Jebao integration."""
from typing import Final

DOMAIN: Final = "jebao"

# Configuration
CONF_DEVICE_ID: Final = "device_id"
CONF_MODEL: Final = "model"
CONF_INTERFACES: Final = "interfaces"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_REGION: Final = "region"
CONF_DID: Final = "did"

# Defaults
DEFAULT_NAME: Final = "Jebao Pump"
DEFAULT_SCAN_INTERVAL: Final = 30  # seconds

# Models
MODEL_MDP20000: Final = "MDP-20000"
MODEL_MD44: Final = "MD-4.4"

# The MD-4.4's firmware exposes channe1..channe8 / Timer1..8ON /
# IntervalT1..8 over the cloud — likely because the firmware is shared
# with an 8-head SKU — but the physical pump body has only 4 outputs.
# We only surface the four that actually do anything; channels 5..8 read
# as always-False on a real MD-4.4 and writes to them are silently ignored
# by the pump.
MD44_CHANNEL_COUNT: Final = 4

# Gizwits cloud configuration. App ID was extracted from the decompiled
# Jebao Aqua Android app (com.gizwits.xb, app version 3.3.1). The region
# map mirrors the routing the official app does based on the user's
# country code.
GIZWITS_APP_ID: Final = "c3703c4888ec4736a3a0d9425c321604"
GIZWITS_REGIONS: Final = {
    "us": "https://usapi.gizwits.com",
    "cn": "https://api.gizwits.com",
    "eu": "https://euapi.gizwits.com",
}

# Calibration-factor mode for sub-mL dosing precision.
#
# The official app's UI only accepts whole-mL dose values. Users who need
# finer dosing physically calibrate the pump so it dispenses 1/10th of the
# value it thinks it's dispensing — i.e. tell the pump during calibration
# that it just dispensed 300 mL when it only dispensed 30. After that,
# every entry of "10" in the app actually doses 1 mL, "14" doses 1.4 mL,
# etc.
#
# This integration mirrors that trick with a per-config-entry toggle: when
# enabled, the Channel N schedule text entity shows fractional mL values
# (e.g. 12:00=1.4) and we multiply by 10 before sending to the cloud.
OPT_CAL_FACTOR_10X: Final = "cal_factor_10x"
CAL_FACTOR_ON: Final = 10
CAL_FACTOR_OFF: Final = 1


def cal_factor(entry_options: dict) -> int:
    """Return the active calibration multiplier for an entry (1 or 10)."""
    return CAL_FACTOR_ON if entry_options.get(OPT_CAL_FACTOR_10X) else CAL_FACTOR_OFF


def signal_dose_input_changed(entry_id: str) -> str:
    """Dispatcher signal fired when this entry's Calibration amount value
    changes. Subscribed by the Value-to-enter-in-app sensor so it can
    re-render immediately without waiting for the coordinator's next
    polling cycle."""
    return f"jebao_{entry_id}_dose_input_changed"
