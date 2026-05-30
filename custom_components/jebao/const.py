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

# MD-4.4 firmware exposes 8 channels / 8 timers / 8 intervals through the
# Gizwits cloud even though the physical pump body only has 4 outputs.
MD44_CHANNEL_COUNT: Final = 8

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
