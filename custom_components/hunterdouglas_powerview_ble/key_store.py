"""Persistent storage for the PowerView AES home key.

The key is the only piece of per-installation configuration the integration
needs that isn't already a config entry, and it must survive HACS updates
(which overwrite const.py and the rest of the integration source tree).

Stored in HA's `.storage/` directory under the integration's domain — a
single JSON document that holds the hex-encoded 16-byte key.
"""

from __future__ import annotations

from typing import Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, LOGGER

STORAGE_VERSION: Final[int] = 1
STORAGE_KEY: Final[str] = DOMAIN  # → .storage/hunterdouglas_powerview_ble


class HomeKeyStore:
    """Async wrapper around an HA Store for the home key."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the store."""
        self._store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._cache: bytes | None = None
        self._loaded: bool = False

    async def async_load(self) -> bytes | None:
        """Load the persisted key. Returns None if not set."""
        if self._loaded:
            return self._cache
        data = await self._store.async_load()
        self._loaded = True
        if not data:
            return None
        hex_key = data.get("home_key", "")
        if not hex_key:
            return None
        try:
            key = bytes.fromhex(hex_key)
        except ValueError:
            LOGGER.warning("Stored home key is not valid hex; ignoring")
            return None
        if len(key) != 16:
            LOGGER.warning(
                "Stored home key is %d bytes, expected 16; ignoring", len(key)
            )
            return None
        self._cache = key
        return key

    async def async_save(self, key: bytes) -> None:
        """Persist a 16-byte home key."""
        if len(key) != 16:
            raise ValueError(f"Home key must be 16 bytes, got {len(key)}")
        await self._store.async_save({"home_key": key.hex()})
        self._cache = key
        self._loaded = True
        LOGGER.info("Saved PowerView home key to %s", STORAGE_KEY)

    async def async_clear(self) -> None:
        """Forget the stored key."""
        await self._store.async_remove()
        self._cache = None
        self._loaded = True


async def async_get_home_key(hass: HomeAssistant) -> bytes | None:
    """One-shot helper: load the key (creating the store if needed)."""
    store = HomeKeyStore(hass)
    return await store.async_load()
