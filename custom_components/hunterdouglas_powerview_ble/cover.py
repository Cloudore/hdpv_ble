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
from homeassistant.const import ATTR_ENTITY_ID, STATE_CLOSING, STATE_OPENING
from homeassistant.core import Event, HomeAssistant, callback
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
        # Optional 'opening' / 'closing' override that pins the direction
        # while a command is in flight. Necessary because the entity can
        # otherwise pass through a transient 'open' or 'closed' state
        # within the same event-loop tick as the command (e.g. if a BLE
        # advert arrives just before async_set_cover_position runs), and
        # HA's HomeKit Bridge clobbers HK's TargetPosition any time the
        # entity is not in a moving state — breaking the iOS direction
        # label for the rest of the move. See _pin_direction().
        self._pinned_direction: str | None = None
        # Last advertised position + when it last changed. Used by
        # `_has_settled()` to decide whether the motor is still moving.
        # Without this, is_opening/is_closing fall to False at the same
        # advert where the shade enters DIRECTION_DEADZONE of its target,
        # which leaves a non-moving "open"/"closed" frame *while the motor
        # is still physically running*. HA's HomeKit Bridge clobbers HK's
        # TargetPosition on any non-moving frame, so that single transient
        # frame flips iOS's direction label for the rest of the move.
        self._last_position: int | None = None
        self._last_position_change_ts: float | None = None
        self._attr_unique_id = (
            f"{DOMAIN}_{format_mac(self._coord.address)}_{CoverDeviceClass.SHADE}"
        )
        super().__init__(coordinator)

    async def async_added_to_hass(self) -> None:
        """Subscribe to HomeKit Bridge's command event for pre-emptive pinning.

        HA's HomeKit Bridge fires `homekit_state_change` synchronously inside
        the TargetPosition setter callback, before the service-call task is
        queued. Pinning the direction in that synchronous frame beats any
        racing BLE advert that would otherwise write a non-moving state and
        trip the Bridge into resetting HK's TargetPosition (which flips the
        iOS direction label for the whole move).
        """
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen("homekit_state_change", self._on_homekit_event)
        )

    @callback
    def _on_homekit_event(self, event: Event) -> None:
        """Pin direction the moment iOS issues a position command."""
        if event.data.get(ATTR_ENTITY_ID) != self.entity_id:
            return
        if event.data.get("service") != "set_cover_position":
            return
        value = event.data.get("value")
        if not isinstance(value, (int, float)):
            return
        current = self.current_cover_position
        if current is None or int(value) == current:
            return
        direction = STATE_OPENING if int(value) > current else STATE_CLOSING
        self._pin_direction(direction)
        self._set_target(int(value))
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:  # type: ignore[reportIncompatibleVariableOverride]
        """Return the device_info of the device."""
        return self._coord.device_info

    @property
    def available(self) -> bool:
        """Always controllable as long as the BLE address is reachable.

        Hunter Douglas shades — especially ACR rollers — back off their
        V2 advert cadence after a few minutes of idle and may not transmit
        again for hours. They still accept BLE writes the entire time and
        wake on command. Gating `available` on advert freshness here would
        make HA refuse to dispatch service calls (logs a "Referenced entity
        ... not currently available" warning and the command is dropped),
        which breaks every UI surface the user controls the shade from.

        Stale advert data is surfaced via `assumed_state` instead — the
        UI shows the "assumed" indicator without blocking commands.
        """
        return super().available

    @property
    def assumed_state(self) -> bool:
        """True when the cached position is older than STALE_AFTER seconds.

        Tells the UI not to fully trust `current_cover_position` — the
        shade may have been moved by remote / wall switch since the last
        advert we decoded.
        """
        return not self._coord.data_available

    # Direction inference. The shade's advert movement bits are
    # unreliable (KDT curtain firmwares invert them vs ACR rollers), so
    # we derive direction from intent: `_target_position` (set by every
    # HA / HomeKit command) compared against `current_position`. While
    # the target is fresh AND the motor is still moving (see
    # `_has_settled()`), we report "opening" / "closing"; once the motor
    # has gone quiet we fall through to the open/closed steady state.
    #
    # The target auto-expires after TARGET_TTL seconds so a stale command
    # can't make the entity report "opening" forever when the user has
    # since moved the shade by remote.

    ENDPOINT_DEADZONE = 5  # is_closed deadzone, absorbs ±2-3% advert noise
    # from a settled-closed shade so it doesn't flip "closed" → "open"
    # → "closed" while sitting at the bottom.
    TARGET_TTL = 60.0  # seconds since last command — pin/target window
    SETTLED_AFTER = 1.5  # seconds without position change = motor stopped

    def _target_is_fresh(self) -> bool:
        if self._target_position is None or self._target_set_at is None:
            return False
        return (time.time() - self._target_set_at) < self.TARGET_TTL

    def _pin_is_valid(self) -> bool:
        """Pin is honored only while the matching target is fresh.

        Backstop for cases where a pin is set (by `_on_homekit_event` or
        by a command method) but never released — e.g. the service call
        failed to dispatch, or the BLE write raised an unhandled exception.
        After TARGET_TTL the pin is treated as released and the entity
        falls back to advert-derived state, so the user isn't stuck
        looking at "Opening..." forever.
        """
        return self._pinned_direction is not None and self._target_is_fresh()

    def _has_settled(self) -> bool:
        """True iff the last position change was longer than SETTLED_AFTER ago.

        Position-stability proxy for "motor stopped". Used by is_opening /
        is_closing to keep the entity in a moving state through the *last
        few percent* of a move where the position is still ticking up but
        within a naive `target - current` deadzone would already snap to
        "open"/"closed". A single such non-moving frame is enough for HA's
        HomeKit Bridge to clobber HK's TargetPosition (it resets Target to
        Current whenever the entity is not in MOVING_STATES) — which then
        flips iOS's direction label backwards for the rest of the move.
        """
        if self._last_position_change_ts is None:
            return True  # never seen a position → not moving
        return (time.time() - self._last_position_change_ts) > self.SETTLED_AFTER

    @property
    def is_opening(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """True while the shade is being driven upward."""
        if self._pin_is_valid():
            return self._pinned_direction == STATE_OPENING
        if not self._target_is_fresh():
            return False
        current = self.current_cover_position
        if current is None:
            return False
        if self._target_position <= current:
            return False
        # Target is above us. Still "opening" until either the position
        # matches the target exactly or the motor has gone quiet.
        return not self._has_settled()

    @property
    def is_closing(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """True while the shade is being driven downward."""
        if self._pin_is_valid():
            return self._pinned_direction == STATE_CLOSING
        if not self._target_is_fresh():
            return False
        current = self.current_cover_position
        if current is None:
            return False
        if self._target_position >= current:
            return False
        return not self._has_settled()

    @property
    def is_closed(self) -> bool | None:  # type: ignore[reportIncompatibleVariableOverride]
        """Return if the cover is closed."""
        pos = self.current_cover_position
        if pos is None:
            return None
        return pos <= CLOSED_POSITION + self.ENDPOINT_DEADZONE

    @callback
    def _handle_coordinator_update(self) -> None:
        """Track position-change timestamps for `_has_settled()` then propagate.

        Only override is the position-change timestamping; the state write is
        still done by the parent so HomeKit gets a live `current_position`
        update on every advert.
        """
        current = self.current_cover_position
        if current is not None and current != self._last_position:
            self._last_position = current
            self._last_position_change_ts = time.time()
        super()._handle_coordinator_update()


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

        Pins the entity to STATE_OPENING / STATE_CLOSING for the duration
        of the command so HA's HomeKit Bridge never sees a non-moving
        frame mid-command — see _pin_direction() and is_opening for the
        full rationale.
        """
        target_position: Final = kwargs.get(ATTR_POSITION)
        if target_position is None:
            return
        target = round(target_position)
        LOGGER.debug("set cover to position %d", target)
        if self.current_cover_position == target and not (
            self.is_closing or self.is_opening
        ):
            return
        current = self.current_cover_position
        if current is not None:
            self._pin_direction(STATE_OPENING if target > current else STATE_CLOSING)
        self._set_target(target)
        self.async_write_ha_state()
        try:
            await self._coord.api.set_position(target, wake_first=self._needs_wake)
        except BleakError as err:
            LOGGER.error(
                "Failed to move cover '%s' to %d%%: %s", self.name, target, err
            )
        finally:
            self._pin_direction(None)

    def _set_target(self, position: int) -> None:
        """Record a new target position and stamp its freshness window."""
        self._target_position = position
        self._target_set_at = time.time()

    def _pin_direction(self, direction: str | None) -> None:
        """Pin is_opening / is_closing to a specific direction.

        Used inside command handlers to guarantee the entity enters
        STATE_OPENING / STATE_CLOSING immediately and stays there until
        we clear it, regardless of what a racing BLE advert would
        otherwise compute from target-vs-current. Pass None to release.
        """
        self._pinned_direction = direction

    def _reset_target_position(self) -> None:
        self._target_position = None
        self._target_set_at = None

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover. Pins direction so HK keeps TargetPosition."""
        LOGGER.debug("open cover")
        if self.current_cover_position == OPEN_POSITION:
            return
        self._pin_direction(STATE_OPENING)
        self._set_target(OPEN_POSITION)
        self.async_write_ha_state()
        try:
            await self._coord.api.open(wake_first=self._needs_wake)
        except BleakError as err:
            LOGGER.error("Failed to open cover '%s': %s", self.name, err)
            self._reset_target_position()
        finally:
            self._pin_direction(None)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover. Pins direction so HK keeps TargetPosition."""
        LOGGER.debug("close cover")
        if self.current_cover_position == CLOSED_POSITION:
            return
        self._pin_direction(STATE_CLOSING)
        self._set_target(CLOSED_POSITION)
        self.async_write_ha_state()
        try:
            await self._coord.api.close(wake_first=self._needs_wake)
        except BleakError as err:
            LOGGER.error("Failed to close cover '%s': %s", self.name, err)
            self._reset_target_position()
        finally:
            self._pin_direction(None)

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
