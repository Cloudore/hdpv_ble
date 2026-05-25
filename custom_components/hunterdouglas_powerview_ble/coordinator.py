"""Home Assistant coordinator for Hunter Douglas PowerView (BLE) integration."""

import time
from typing import Any

from bleak.backends.device import BLEDevice

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.const import DOMAIN as BLUETOOTH_DOMAIN
from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import (
    CONNECTION_BLUETOOTH,
    DeviceInfo,
    format_mac,
)

from .api import SHADE_TYPE, PowerViewBLE
from .const import ATTR_RSSI, DOMAIN, HOME_KEY as _DEFAULT_HOME_KEY, LOGGER, STALE_AFTER


class PVCoordinator(PassiveBluetoothDataUpdateCoordinator):
    """Update coordinator for a battery management system."""

    def __init__(
        self,
        hass: HomeAssistant,
        ble_device: BLEDevice,
        data: dict[str, Any],
        *,
        home_key: bytes | None = None,
    ) -> None:
        """Initialize BMS data coordinator.

        home_key: 16-byte AES key shared by all shades on this PowerView
        home. Pass the value resolved from a gateway config entry. Falls
        back to const.HOME_KEY when None, which is empty by default — so
        commands go out plaintext (works for ACR rollers, but KDT motors
        will silently reject them).
        """
        assert ble_device.name is not None
        self._mac = ble_device.address
        self.api = PowerViewBLE(
            ble_device, home_key if home_key is not None else _DEFAULT_HOME_KEY
        )
        self.data: dict[str, int | float | bool] = {}
        self._manuf_dat = data.get("manufacturer_data")
        self.dev_details: dict[str, str] = {}
        self._last_v2_ts: float | None = None

        LOGGER.debug(
            "Initializing coordinator for %s (%s)",
            ble_device.name,
            ble_device.address,
        )
        super().__init__(
            hass,
            LOGGER,
            ble_device.address,
            bluetooth.BluetoothScanningMode.ACTIVE,
        )

    async def query_dev_info(self) -> None:
        """Receive detailed information from device."""
        LOGGER.debug("%s: querying device info", self.name)
        self.dev_details.update(await self.api.query_dev_info())

    @property
    def device_info(self) -> DeviceInfo:
        """Return detailed device information for GUI."""
        LOGGER.debug("%s: device_info, %s", self.name, self.dev_details)
        return DeviceInfo(
            identifiers={
                # Use the immutable MAC, not the BLE-advertised local name.
                # local_name can transiently match across devices (e.g. a generic
                # firmware fallback like "AWEI T13 Pro"), which causes HA's
                # device registry to merge unrelated shades into one record.
                (DOMAIN, format_mac(self.address)),
                (BLUETOOTH_DOMAIN, self.address),
            },
            connections={(CONNECTION_BLUETOOTH, self.address)},
            name=self.name,
            configuration_url=None,
            # properties used in GUI:
            manufacturer="Hunter Douglas",
            model=(
                str(SHADE_TYPE.get(int(bytes.fromhex(self._manuf_dat)[2]), "unknown"))
                if self._manuf_dat
                else None
            ),
            model_id=(
                str(bytes.fromhex(self._manuf_dat)[2]) if self._manuf_dat else None
            ),
            serial_number=self.dev_details.get("serial_nr"),
            sw_version=self.dev_details.get("sw_rev"),
            hw_version=self.dev_details.get("hw_rev"),
        )

    @property
    def device_present(self) -> bool:
        """Check if a device is present."""
        return bluetooth.async_address_present(self.hass, self._mac, connectable=True)

    @property
    def data_available(self) -> bool:
        """Return True iff a V2 advertisement was decoded within STALE_AFTER seconds."""
        return (
            self._last_v2_ts is not None
            and (time.time() - self._last_v2_ts) < STALE_AFTER
        )

    def _async_stop(self) -> None:
        """Shutdown coordinator and any connection."""
        LOGGER.debug("%s: shutting down BMS device", self.name)
        self.hass.async_create_task(self.api.disconnect())
        super()._async_stop()

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle a Bluetooth event."""

        # if not self.dev_details:
        #     self.hass.async_create_task(self._get_device_info())

        LOGGER.debug("BLE event %s: %s", change, service_info.manufacturer_data)
        # Always refresh RSSI. Preserve last-known positional fields across
        # events so a shade doesn't briefly report "no position" — which the
        # cover entity surfaces as state="open" with current_position=None,
        # the opposite of the truth on a closed shade.
        #
        # But clear state-of-the-moment flags so they only reflect data from
        # the current V2 frame — otherwise stale movement bits would freeze
        # the entity in "opening"/"closing" after the shade has actually
        # stopped moving.
        self.data[ATTR_RSSI] = service_info.rssi
        for _k in (
            "type_id",
            "is_opening",
            "is_closing",
            "battery_charging",
            "resetMode",
            "resetClock",
            "service_required",
        ):
            self.data.pop(_k, None)
        if change == bluetooth.BluetoothChange.ADVERTISEMENT:
            mfg = self.api.dec_manufacturer_data(
                bytearray(service_info.manufacturer_data.get(2073, b""))
            )
            if mfg:
                self.data.update(mfg)
                # Consume home_id here (drives api.encrypted) rather than
                # leaving it sticky in self.data, because cover.supported_features
                # treats any non-zero home_id as "encryption required" and
                # disables UI controls. In practice many paired roller shades
                # accept plaintext commands even with home_id != 0 and no
                # HOME_KEY, so over-disabling the UI hurts more than it helps.
                # Encryption is still applied when both home_id and HOME_KEY
                # are present (Cipher is built only on a 16-byte HOME_KEY).
                self.api.encrypted = bool(self.data.pop("home_id", 0))
                self._last_v2_ts = time.time()

        LOGGER.debug("data sample %s", self.data)
        super()._async_handle_bluetooth_event(service_info, change)
