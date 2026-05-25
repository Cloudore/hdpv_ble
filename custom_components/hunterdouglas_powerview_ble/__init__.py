"""The Hunter Douglas PowerView (BLE) integration.

@author: patman15
@license: Apache-2.0 license
"""

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from .const import HOME_KEY as FALLBACK_HOME_KEY, LOGGER
from .coordinator import PVCoordinator
from .key_store import async_get_home_key

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.COVER,
    Platform.SENSOR,
    Platform.BUTTON,
]

type ConfigEntryType = ConfigEntry[PVCoordinator]


async def _resolve_home_key(hass: HomeAssistant) -> bytes:
    """Return the persisted home key, or const.HOME_KEY fallback."""
    stored = await async_get_home_key(hass)
    if stored is not None:
        return stored
    return FALLBACK_HOME_KEY


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntryType) -> bool:
    """Set up a single shade config entry."""
    LOGGER.debug("Setup of %s", repr(entry))

    if entry.unique_id is None:
        raise ConfigEntryError("Missing unique ID for device.")

    ble_device: BLEDevice | None = async_ble_device_from_address(
        hass=hass, address=entry.unique_id, connectable=True
    )

    if not ble_device:
        raise ConfigEntryNotReady(
            f"Could not find PowerView device ({entry.unique_id}) via Bluetooth"
        )

    home_key = await _resolve_home_key(hass)
    coordinator = PVCoordinator(hass, ble_device, entry.data.copy(), home_key=home_key)
    try:
        await coordinator.query_dev_info()
    except BleakError as err:
        raise ConfigEntryNotReady("Unable to query device info.") from err

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(coordinator.async_start())
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntryType) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    LOGGER.debug("Unloaded config entry: %s, ok? %s!", entry.unique_id, str(unload_ok))
    return unload_ok


async def async_migrate_entry(
    _hass: HomeAssistant, config_entry: ConfigEntryType
) -> bool:
    """Migrate old entry."""

    if config_entry.version > 1:
        # This means the user has downgraded from a future version
        LOGGER.debug("Cannot downgrade from version %s", config_entry.version)
        return False

    LOGGER.debug("Migrating from version %s", config_entry.version)

    return False
