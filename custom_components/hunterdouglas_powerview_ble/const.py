"""Constants for the BLE Battery Management System integration."""

import logging
from typing import Final

DOMAIN: Final[str] = "hunterdouglas_powerview_ble"
LOGGER: Final = logging.getLogger(__package__)
MFCT_ID: Final[int] = 2073
TIMEOUT: Final[int] = 5
STALE_AFTER: Final[float] = 300.0  # seconds without a V2 advert before entity is unavailable

# Config flow form field
CONF_HOST: Final[str] = "host"

# Fallback HOME_KEY for users who haven't run the gateway extractor yet.
# Leaving this populated is harmless (the value persisted via the extractor
# in `.storage/hunterdouglas_powerview_ble` takes precedence), but it gets
# overwritten on every HACS update — so the persistent key store is the
# durable place to put your real key.
HOME_KEY: Final[bytes] = b""


# attributes (do not change)
ATTR_RSSI: Final[str] = "rssi"
