"""Date/time entities."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .entity import HCEntity
from .helpers import create_entities, error_decorator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import HCConfigEntry, HCData
    from .entity_descriptions.descriptions_definitions import HCDateTimeEntityDescription

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    config_entry: HCConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up date/time platform."""
    entities = create_entities({"datetime": HCDateTime}, config_entry.runtime_data)
    async_add_entities(entities)


class HCDateTime(HCEntity, DateTimeEntity):
    """Home Connect date/time entity."""

    entity_description: HCDateTimeEntityDescription

    def __init__(
        self,
        entity_description: HCDateTimeEntityDescription,
        runtime_data: HCData,
    ) -> None:
        """Initialize date/time entity."""
        super().__init__(entity_description, runtime_data)
        self._entity._type = str  # noqa: SLF001
        # The appliance publishes LocalTime as a timestamp, not a 1 Hz clock.
        # Retain the most recent authoritative timestamp and advance it locally.
        self._last_raw: str | None = None
        self._base_time: datetime | None = None
        self._base_monotonic: float = 0.0
        self._tick_counter: int = 0

    async def async_added_to_hass(self) -> None:
        """Update the displayed clock between appliance notifications."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._clock_tick, timedelta(seconds=15))
        )

    def _clock_tick(self, _now: datetime) -> None:
        """Refresh only this entity's state; never send a write to the oven."""
        # Do not gate UI refresh on session.connected: some integrations
        # report a transient false flag even while HA has a valid LocalTime.
        # The base HCEntity.available still reports the true link status.
        if self._entity.value:
            self._tick_counter += 1
            self.async_write_ha_state()

    @property
    def native_value(self) -> datetime | None:
        """Advance last reported appliance time using a monotonic clock.

        Re-anchor whenever the oven sends an updated LocalTime string. This is
        a displayed estimate between updates, not device-clock synchronization.
        """
        value = self._entity.value
        if not value:
            self._last_raw = None
            self._base_time = None
            return None

        raw = str(value)
        if raw != self._last_raw or self._base_time is None:
            parsed = dt_util.parse_datetime(raw)
            if parsed is None:
                self._last_raw = raw
                self._base_time = None
                return None
            if parsed.tzinfo is None:
                timezone = dt_util.get_time_zone(self.hass.config.time_zone)
                if timezone is None:
                    return None
                parsed = parsed.replace(tzinfo=timezone)
            self._last_raw = raw
            self._base_time = parsed
            self._base_monotonic = monotonic()

        elapsed = max(0, monotonic() - self._base_monotonic)
        return self._base_time + timedelta(seconds=int(elapsed))

    @property
    def extra_state_attributes(self) -> dict:
        """Expose ticker activity for diagnostics; never write to the oven."""
        attributes = dict(super().extra_state_attributes or {})
        attributes["appliance_last_local_time"] = self._last_raw
        attributes["local_clock_tick_count"] = self._tick_counter
        attributes["estimated_between_device_updates"] = True
        return attributes

    @error_decorator
    async def async_set_value(self, value: datetime) -> None:
        """Set appliance-local date/time."""
        timezone = dt_util.get_time_zone(self.hass.config.time_zone)
        if timezone is not None:
            value = value.astimezone(timezone)

        await self._entity.set_value(value.strftime("%Y-%m-%dT%H:%M:%S"))


class HCLiveOvenClock(HCEntity, SensorEntity):
    """Read-only ticking display, separate from the datetime editor.

    Firmware reports LocalTime when connected and on changes, not every second.
    The underlying datetime input in the HA frontend is a picker; it may not
    repaint like a running clock. This text sensor provides a live readout.
    No writes and no network requests are performed by the ticker.
    """

    _attr_icon = "mdi:clock-outline"

    def __init__(self, entity_description, runtime_data):
        super().__init__(entity_description, runtime_data)
        self._last_raw = None
        self._base_time = None
        self._base_mono = 0.0
        self._ticks = 0
        self._task = None

    def _now(self):
        raw = self._entity.value
        if not raw:
            return None
        raw = str(raw)
        if raw != self._last_raw or self._base_time is None:
            parsed = dt_util.parse_datetime(raw)
            if parsed is None:
                self._last_raw = raw
                self._base_time = None
                return None
            if parsed.tzinfo is None:
                tz = dt_util.get_time_zone(self.hass.config.time_zone)
                if tz is None:
                    return None
                parsed = parsed.replace(tzinfo=tz)
            self._last_raw = raw
            self._base_time = parsed
            self._base_mono = monotonic()
        return self._base_time + timedelta(seconds=int(max(0, monotonic() - self._base_mono)))

    @property
    def native_value(self):
        now = self._now()
        return now.isoformat(timespec="seconds") if now is not None else None

    @property
    def extra_state_attributes(self):
        attrs = dict(super().extra_state_attributes or {})
        now = self._now()
        attrs.update({
            "date": now.strftime("%Y-%m-%d") if now else None,
            "time_zone": self.hass.config.time_zone,
            "last_appliance_local_time": self._last_raw,
            "clock_tick_count": self._ticks,
            "estimated_between_appliance_updates": True,
        })
        return attrs

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # Independent task avoids relying on HA datetime-picker rerenders.
        task = self.hass.async_create_background_task(
            self._tick_loop(), "homeconnect_local_oven_clock"
        )
        self._task = task
        self.async_on_remove(task.cancel)

    async def _tick_loop(self):
        while True:
            await asyncio.sleep(30)
            if self._entity.value:
                self._ticks += 1
                self.async_write_ha_state()


def make_live_oven_clock(runtime_data):
    """Create one read-only live clock for this NEFF if LocalTime exists."""
    info = runtime_data.appliance.info
    if not (info.get("brand", "").upper() == "NEFF"
            and info.get("vib") == "C28MT23H0"):
        return None
    name = "BSH.Common.Setting.LocalTime"
    if name not in runtime_data.appliance.entities:
        return None
    from .entity_descriptions.descriptions_definitions import HCSensorEntityDescription
    desc = HCSensorEntityDescription(
        key="sensor_oven_clock_live", name="Local time (live)",
        entity=name, icon="mdi:clock-outline",
    )
    return HCLiveOvenClock(desc, runtime_data)
