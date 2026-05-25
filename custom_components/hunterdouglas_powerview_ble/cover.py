"""Hunter Douglas Powerview cover."""

import time
from typing import Any, Final

from bleak.exc import BleakError

from homeassistant.components.bluetooth.passive_update_coordinator import (
    PassiveBluetoothCoordinatorEntity,
)
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_CURRENT_TILT_POSITION,
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo, format_mac
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import CLOSED_POSITION, OPEN_POSITION
from .const import DOMAIN, HOME_KEY, LOGGER
from .coordinator import PVCoordinator


async def async_setup_entry(
    _hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the demo cover platform."""

    coordinator: PVCoordinator = config_entry.runtime_data
    model: Final[str|None] = coordinator.dev_details.get("model")
    entities: list[PowerViewCover] = []
    if model in ["39"]:
        entities.append(PowerViewCoverTiltOnly(coordinator))
    else:
        entities.append(PowerViewCover(coordinator))

    async_add_entities(entities)


class PowerViewCover(PassiveBluetoothCoordinatorEntity[PVCoordinator], CoverEntity):  # type: ignore[reportIncompatibleVariableOverride]
    """Representation of a PowerView shade with Up/Down functionality only."""

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
        | CoverEntityFeature.STOP
    )

    def __init__(
        self,
        coordinator: PVCoordinator,
    ) -> None:
        """Initialize the shade."""
        LOGGER.debug("%s: init() PowerViewCover", coordinator.name)
        self._attr_name = CoverDeviceClass.SHADE
        self._coord: PVCoordinator = coordinator
        self._attr_device_info = self._coord.device_info
        self._target_position: int | None = None
        # Wall-clock timestamp of the last command that set _target_position.
        # Used by is_opening / is_closing to expire stale targets after
        # TARGET_TTL seconds (see those properties for the rationale).
        self._target_set_at: float | None = None
        self._attr_unique_id = (
            f"{DOMAIN}_{format_mac(self._coord.address)}_{CoverDeviceClass.SHADE}"
        )
        super().__init__(coordinator)

    @property
    def device_info(self) -> DeviceInfo:  # type: ignore[reportIncompatibleVariableOverride]
        """Return the device_info of the device."""
        return self._coord.device_info

    @property
    def available(self) -> bool:
        """Return True only when the shade has produced a recent V2 advert.

        Without this gate, an out-of-range shade keeps reporting its last
        known position indefinitely (e.g. stuck at "closed" while it is
        actually open), because PassiveBluetoothCoordinatorEntity treats
        any BLE address activity as "present".
        """
        return super().available and self._coord.data_available

    # Direction inference. The shade's advert movement bits are
    # unreliable: KDT curtain firmwares invert is_opening / is_closing
    # vs ACR rollers, and with multiple BLE proxies in the same home,
    # adverts arrive out of order with ±1-3% position bounce that
    # produces "closing" flickers during smooth opens.
    #
    # Direction is derived from intent — _target_position (set by every
    # HA / HomeKit command) vs current_position. While the shade is more
    # than DIRECTION_DEADZONE percent away from its commanded target, we
    # report the corresponding "opening" or "closing" state; once it's
    # within the deadzone, the target is considered reached and we fall
    # through to the open/closed steady state.
    #
    # The target also auto-expires after TARGET_TTL seconds of inactivity
    # so a stale command can't make the entity report "opening" forever
    # when the user has since moved the shade by remote.

    DIRECTION_DEADZONE = 2
    TARGET_TTL = 60.0  # seconds since last command

    def _target_is_fresh(self) -> bool:
        if self._target_position is None or self._target_set_at is None:
            return False
        return (time.time() - self._target_set_at) < self.TARGET_TTL

    @property
    def is_opening(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """True while a fresh command is still driving the shade upward."""
        if not self._target_is_fresh():
            return False
        current = self.current_cover_position
        if current is None:
            return False
        return self._target_position > current + self.DIRECTION_DEADZONE

    @property
    def is_closing(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """True while a fresh command is still driving the shade downward."""
        if not self._target_is_fresh():
            return False
        current = self.current_cover_position
        if current is None:
            return False
        return self._target_position < current - self.DIRECTION_DEADZONE

    @property
    def is_closed(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closed.

        Uses the same DIRECTION_DEADZONE as is_opening / is_closing so a
        motor that settles 1-2% short of fully closed (common end-of-
        travel calibration drift) doesn't bounce between "open" and
        "closed" in HomeKit. Returns None when position is unknown so
        HA reports the entity as 'unknown' rather than assuming "open".
        """
        pos = self.current_cover_position
        if pos is None:
            return None
        return pos <= CLOSED_POSITION + self.DIRECTION_DEADZONE

    @property
    def supported_features(self) -> CoverEntityFeature:  # type: ignore[reportIncompatibleVariableOverride]
        """Flag supported features, disable control if encryption is needed."""
        if (
            self._coord.data.get("home_id") and len(HOME_KEY) != 16
        ) or self._coord.data.get("battery_charging"):
            return CoverEntityFeature(0)

        return super().supported_features

    @property
    def current_cover_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return current position of cover.

        None is unknown, 0 is closed, 100 is fully open.
        """
        pos: Final = self._coord.data.get(ATTR_CURRENT_POSITION)
        return round(pos) if pos is not None else None

    @property
    def _needs_wake(self) -> bool:
        """True when the shade is in low-power mode and needs a double-send.

        See api.set_position(wake_first=...) for the rationale.
        """
        return bool(self._coord.data.get("low_power_mode"))

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position.

        State updates are driven exclusively by incoming BLE adverts via
        the coordinator → entity update path. Writing state optimistically
        after the BLE call produces spurious "closed → closing → closed"
        and "open → opening → open" ghost transitions whenever an advert
        with stale position arrives during the ~500ms BLE round-trip.
        """
        target_position: Final = kwargs.get(ATTR_POSITION)
        if target_position is None:
            return
        LOGGER.debug("set cover to position %f", target_position)
        if self.current_cover_position == round(target_position) and not (
            self.is_closing or self.is_opening
        ):
            return
        self._set_target(round(target_position))
        try:
            await self._coord.api.set_position(
                round(target_position), wake_first=self._needs_wake
            )
        except BleakError as err:
            LOGGER.error(
                "Failed to move cover '%s' to %f%%: %s",
                self.name,
                target_position,
                err,
            )

    def _set_target(self, position: int) -> None:
        """Record a new target position and stamp its freshness window."""
        self._target_position = position
        self._target_set_at = time.time()

    def _reset_target_position(self) -> None:
        self._target_position = None
        self._target_set_at = None

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover. State driven by BLE adverts, not optimistic write."""
        LOGGER.debug("open cover")
        if self.current_cover_position == OPEN_POSITION:
            return
        try:
            self._set_target(OPEN_POSITION)
            await self._coord.api.open(wake_first=self._needs_wake)
        except BleakError as err:
            LOGGER.error("Failed to open cover '%s': %s", self.name, err)
            self._reset_target_position()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover. State driven by BLE adverts, not optimistic write."""
        LOGGER.debug("close cover")
        if self.current_cover_position == CLOSED_POSITION:
            return
        try:
            self._set_target(CLOSED_POSITION)
            await self._coord.api.close(wake_first=self._needs_wake)
        except BleakError as err:
            LOGGER.error("Failed to close cover '%s': %s", self.name, err)
            self._reset_target_position()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        LOGGER.debug("stop cover")
        try:
            await self._coord.api.stop()
            self._reset_target_position()
            self.async_write_ha_state()
        except BleakError as err:
            LOGGER.error("Failed to stop cover '%s': %s", self.name, err)


class PowerViewCoverTilt(PowerViewCover):
    """Representation of a PowerView shade with additional tilt functionality."""

    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
        | CoverEntityFeature.OPEN_TILT
        | CoverEntityFeature.CLOSE_TILT
        | CoverEntityFeature.STOP_TILT
        | CoverEntityFeature.SET_TILT_POSITION
    )

    def __init__(
        self,
        coordinator: PVCoordinator,
    ) -> None:
        """Initialize the shade with tilt."""
        LOGGER.debug("%s: init() PowerViewCoverTilt", coordinator.name)
        super().__init__(coordinator)

    @property
    def current_cover_tilt_position(self) -> int | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return current tilt of cover.

        None is unknown
        """
        pos: Final = self._coord.data.get(ATTR_CURRENT_TILT_POSITION)
        return round(pos) if pos is not None else None

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Move the tilt to a specific position."""

        if isinstance(target_position := kwargs.get(ATTR_TILT_POSITION), int):
            LOGGER.debug("set cover tilt to position %i", target_position)
            if (
                self.current_cover_tilt_position == round(target_position)
                or self.current_cover_position is None
            ):
                return

            try:
                await self._coord.api.set_position(
                    self.current_cover_position, tilt=target_position
                )
                self.async_write_ha_state()
            except BleakError as err:
                LOGGER.error(
                    "Failed to tilt cover '%s' to %f%%: %s",
                    self.name,
                    target_position,
                    err,
                )

    async def async_stop_cover_tilt(self, **kwargs: Any) -> None:
        """Stop the cover."""
        await self.async_stop_cover(kwargs=kwargs)

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Open the cover tilt."""
        LOGGER.debug("open cover tilt")
        _kwargs = {**kwargs, ATTR_TILT_POSITION: OPEN_POSITION}
        await self.async_set_cover_tilt_position(**_kwargs)

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Close the cover tilt."""
        LOGGER.debug("close cover tilt")
        _kwargs = {**kwargs, ATTR_TILT_POSITION: CLOSED_POSITION}
        await self.async_set_cover_tilt_position(**_kwargs)


class PowerViewCoverTiltOnly(PowerViewCoverTilt):
    """Representation of a PowerView shade with additional tilt functionality."""

    OPENCLOSED_THRESHOLD = 5

    _attr_device_class = CoverDeviceClass.BLIND
    _attr_supported_features = (
        CoverEntityFeature.OPEN_TILT
        | CoverEntityFeature.CLOSE_TILT
        | CoverEntityFeature.STOP_TILT
        | CoverEntityFeature.SET_TILT_POSITION
    )

    def __init__(
        self,
        coordinator: PVCoordinator,
    ) -> None:
        """Initialize the shade with tilt only."""
        LOGGER.debug("%s: init() PowerViewCoverTiltOnly", coordinator.name)
        super().__init__(coordinator)

    @property
    def is_opening(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is opening or not."""
        return False

    @property
    def is_closing(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closing or not."""
        return False

    @property
    def is_closed(self) -> bool:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closed."""
        return isinstance(self.current_cover_tilt_position, int) and (
            self.current_cover_tilt_position
            >= OPEN_POSITION - PowerViewCoverTiltOnly.OPENCLOSED_THRESHOLD
            or self.current_cover_tilt_position
            <= CLOSED_POSITION + PowerViewCoverTiltOnly.OPENCLOSED_THRESHOLD
        )
