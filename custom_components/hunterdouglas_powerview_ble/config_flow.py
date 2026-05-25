"""Config flow for Hunter Douglas PowerView (BLE).

Two flavours of flow:

1. **Shade pairing** — Bluetooth discovery or manual pick. Creates a config
   entry per shade. (Existing behaviour.)

2. **Home-key extraction** — a one-shot helper. The user picks a PowerView
   gateway (via mDNS auto-discovery, or by typing its address). The flow
   talks to that gateway over HTTP, pulls the AES home key, persists it in
   `.storage/hunterdouglas_powerview_ble`, and then aborts without creating
   any entry. The gateway itself is never represented in HA — it's only
   needed during this single key-extraction call, and after that all shade
   traffic goes over BLE proxies. HACS updates can't wipe the key because
   `.storage/` lives outside the integration source tree.
"""

from dataclasses import dataclass
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .api import UUID_COV_SERVICE as UUID
from .const import CONF_HOST, DOMAIN, LOGGER, MFCT_ID
from .gateway import GatewayError, extract_home_key, probe_gateway
from .key_store import HomeKeyStore, async_get_home_key


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow for shades and the home-key extractor."""

    VERSION = 1
    MINOR_VERSION = 0

    @dataclass
    class DiscoveredDevice:
        """A discovered Bluetooth device."""

        name: str
        discovery_info: BluetoothServiceInfoBleak

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_device: ConfigFlow.DiscoveredDevice | None = None
        self._discovered_devices: dict[str, ConfigFlow.DiscoveredDevice] = {}
        self._gateway_host: str | None = None

    # ---------- entrypoint when user clicks "Add Integration" ----------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Top-level menu: extract a key from a gateway, or pair a shade."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["extract_key", "shade"],
        )

    # ---------- Home-key extraction (one-shot helper, no entry created) ----------

    async def async_step_extract_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user for a gateway address, extract the key, persist, abort."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            try:
                info = await probe_gateway(host)
                key_hex = await extract_home_key(info["host"])
            except GatewayError as ex:
                LOGGER.warning("Home-key extraction failed: %s", ex)
                errors["base"] = "cannot_connect"
            else:
                await HomeKeyStore(self.hass).async_save(bytes.fromhex(key_hex))
                # Reload every shade entry so coordinators pick up the new key.
                for entry in self.hass.config_entries.async_entries(DOMAIN):
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(entry.entry_id)
                    )
                return self.async_abort(reason="home_key_saved")

        default_host = self._gateway_host or "http://powerview-g3.local"
        return self.async_show_form(
            step_id="extract_key",
            data_schema=vol.Schema(
                {vol.Required(CONF_HOST, default=default_host): str}
            ),
            errors=errors,
            description_placeholders={"default_host": default_host},
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """A PowerView gateway announced itself via mDNS — offer to extract.

        Skipped when we already have a stored key, to avoid spamming the
        Discovered tile after first-time setup. Users can re-extract any
        time from the integration menu if the key rotates.
        """
        if await async_get_home_key(self.hass) is not None:
            return self.async_abort(reason="home_key_already_saved")
        host_str = (
            discovery_info.hostname.rstrip(".")
            if discovery_info.hostname
            else str(discovery_info.ip_address)
        )
        url = f"http://{host_str}"
        LOGGER.debug("Zeroconf-discovered PowerView gateway: %s", url)
        # Stable unique id keyed on the host so multiple rediscoveries collapse.
        await self.async_set_unique_id(f"gateway:{url}")
        self._abort_if_unique_id_configured()
        self._gateway_host = url
        self.context["title_placeholders"] = {"name": host_str}
        return await self.async_step_extract_key_confirm()

    async def async_step_extract_key_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm extraction from a zeroconf-discovered gateway."""
        assert self._gateway_host is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await probe_gateway(self._gateway_host)
                key_hex = await extract_home_key(info["host"])
            except GatewayError as ex:
                LOGGER.warning("Home-key extraction failed: %s", ex)
                errors["base"] = "cannot_connect"
            else:
                await HomeKeyStore(self.hass).async_save(bytes.fromhex(key_hex))
                for entry in self.hass.config_entries.async_entries(DOMAIN):
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(entry.entry_id)
                    )
                return self.async_abort(reason="home_key_saved")

        self._set_confirm_only()
        return self.async_show_form(
            step_id="extract_key_confirm",
            description_placeholders={"host": self._gateway_host},
            errors=errors,
        )

    # ---------- Shade pairing (Bluetooth) ----------

    async def async_step_shade(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """User picked 'pair a shade' from the menu."""
        return await self.async_step_pick_shade(user_input)

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Bluetooth scanner found a shade."""
        LOGGER.debug("Bluetooth device detected: %s", discovery_info)
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovered_device = ConfigFlow.DiscoveredDevice(
            discovery_info.name, discovery_info
        )
        self.context["title_placeholders"] = {"name": self._discovered_device.name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a BLE-discovered shade."""
        assert self._discovered_device is not None
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovered_device.name,
                data={
                    "manufacturer_data": self._discovered_device.discovery_info.manufacturer_data[
                        MFCT_ID
                    ].hex()
                },
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._discovered_device.name},
        )

    async def async_step_pick_shade(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List currently-advertising shades for the user to pick."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self._discovered_device = self._discovered_devices[address]
            self.context["title_placeholders"] = {"name": self._discovered_device.name}
            return self.async_create_entry(
                title=self._discovered_device.name,
                data={
                    "manufacturer_data": self._discovered_device.discovery_info.manufacturer_data[
                        MFCT_ID
                    ].hex()
                },
            )

        current_addresses = self._async_current_ids()
        for discovery_info in async_discovered_service_info(self.hass, False):
            address = discovery_info.address
            if address in current_addresses or address in self._discovered_devices:
                continue
            if MFCT_ID not in discovery_info.manufacturer_data:
                continue
            if UUID not in discovery_info.service_uuids:
                continue
            self._discovered_devices[address] = ConfigFlow.DiscoveredDevice(
                discovery_info.name, discovery_info
            )

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        titles: list[SelectOptionDict] = []
        for address, discovery in self._discovered_devices.items():
            titles.append({"value": address, "label": discovery.name})

        return self.async_show_form(
            step_id="pick_shade",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): SelectSelector(
                        SelectSelectorConfig(options=titles)
                    )
                }
            ),
        )
