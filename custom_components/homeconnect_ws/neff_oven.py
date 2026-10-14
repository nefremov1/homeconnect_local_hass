"""NEFF C28MT23H0 local controls V6.8.1 (V6.8 value normalization and diagnostics).

One drop-down for 24 observed modes. Program-dependent complete option payloads.
Only Hot Air start has been proven on this particular appliance. Other ordinary
heating programs have an explicit experimental start service; microwave/combi
and pyrolysis remote START is deliberately disabled pending validation.

The static XML intentionally remains read-only for the program and options:
writing each UID through /ro/values was proven to fail on this appliance.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.exceptions import HomeAssistantError
from homeconnect_websocket.entities import Access
from homeconnect_websocket.message import Action, Message

from .const import DOMAIN
from .entity import HCEntity
from .entity_descriptions.descriptions_definitions import (
    HCButtonEntityDescription, HCNumberEntityDescription,
    HCSelectEntityDescription, HCSensorEntityDescription, HCSwitchEntityDescription,
)
from .helpers import error_decorator, get_config_entry_from_call

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from . import HCData

_LOGGER = logging.getLogger(__name__)
SELECTED = 'BSH.Common.Root.SelectedProgram'
ACTIVE = 'BSH.Common.Root.ActiveProgram'
DURATION = 'BSH.Common.Option.Duration'
TEMPERATURE = 'Cooking.Oven.Option.SetpointTemperature'
FAST = 'Cooking.Oven.Option.FastPreHeat'
MICROWAVE_POWER = 'Cooking.Oven.Option.MicrowavePower'
PYRO_LEVEL = 'Cooking.Oven.Option.PyrolysisLevel'
ABORT = 'BSH.Common.Command.AbortProgram'
POWER = 'BSH.Common.Setting.PowerState'
OPERATION = 'BSH.Common.Status.OperationState'
REMOTE = 'BSH.Common.Status.RemoteControlActive'
REMOTE_START = 'BSH.Common.Status.RemoteControlStartAllowed'
LOCAL = 'BSH.Common.Status.LocalControlActive'
DOOR = 'BSH.Common.Status.DoorState'

# The observed programs, grouped by the controls they actually display on
# the local panel. Defaults are observed panel defaults or conservative guesses
# for untested *selection* requests, never validated remote-start defaults.
@dataclass(frozen=True)
class Spec:
    uid: int
    label: str
    category: str  # heat / microwave / combi / pyro
    temp: int | None = None
    duration: int = 600
    power: int | None = None  # 9/18/36/60/100 denote 90/180/360/600/Max


SPECS = (
    Spec(8208, 'Hot Air', 'heat', 160),
    Spec(8209, 'Top/Bottom Heating', 'heat', 170),
    Spec(8210, 'Hot Air Eco', 'heat', 160),
    Spec(8211, 'Top/Bottom Heating Eco', 'heat', 170),
    Spec(8212, 'Hot Air Grilling', 'heat', 180),
    Spec(8213, 'Pizza Setting', 'heat', 200),
    Spec(8226, 'Slow Cook', 'heat', 80),
    Spec(8227, 'Bottom Heating', 'heat', 150),
    Spec(8229, 'Keep Warm', 'heat', 60),
    Spec(8230, 'Preheat Ovenware', 'heat', 50),
    Spec(8235, 'Grill Small Area', 'heat', 275),
    Spec(8236, 'Grill Large Area', 'heat', 275),
    Spec(8237, 'Dough Proving', 'heat', 40),
    Spec(8705, 'Microwave 90 W', 'microwave', power=9, duration=60),
    Spec(8706, 'Microwave 180 W', 'microwave', power=18, duration=60),
    Spec(8707, 'Microwave 360 W', 'microwave', power=36, duration=60),
    Spec(8708, 'Microwave 600 W', 'microwave', power=60, duration=60),
    Spec(8710, 'Microwave Max', 'microwave', power=100, duration=60),
    Spec(8464, 'Hot Air + Microwave', 'combi', 160, 1200, 36),
    Spec(8465, 'Top/Bottom + Microwave', 'combi', 170, 1200, 18),
    Spec(8466, 'Hot Air Grilling + Microwave', 'combi', 180, 1200, 36),
    Spec(8482, 'Grill Large Area + Microwave', 'combi', 275, 1200, 18),
    Spec(8483, 'Grill Small Area + Microwave', 'combi', 275, 1200, 9),
    Spec(9217, 'Pyrolysis', 'pyro', duration=4500),
)
BY_UID = {s.uid: s for s in SPECS}
# Level and duration are linked by the oven's UI, established by panel traces.
PYRO_DURATION = {1: 4500, 2: 5400, 3: 7200}
PROVEN_SELECT = {8208, 8209}
PROVEN_START = {8208}
EXPERIMENTAL_START = {s.uid for s in SPECS if s.category == 'heat'} - PROVEN_START
_LOCKS: dict[str, asyncio.Lock] = {}


def is_neff(runtime: HCData) -> bool:
    info = runtime.appliance.info
    return info.get('brand', '').upper() == 'NEFF' and info.get('vib') == 'C28MT23H0'


def raw(runtime: HCData, name: str):
    entity = runtime.appliance.entities.get(name)
    return None if entity is None else entity.value_raw


def running_program(runtime: HCData) -> int | None:
    val = raw(runtime, ACTIVE)
    return val if type(val) is int and val in BY_UID else None


def selected_program(runtime: HCData) -> int | None:
    val = raw(runtime, SELECTED)
    return val if type(val) is int and val in BY_UID else None


def current_program(runtime: HCData) -> int | None:
    return running_program(runtime) or selected_program(runtime)


def _lock(runtime: HCData) -> asyncio.Lock:
    key = runtime.appliance.info['deviceID']
    if key not in _LOCKS:
        _LOCKS[key] = asyncio.Lock()
    return _LOCKS[key]


def _connected(runtime: HCData) -> bool:
    return bool(runtime.appliance.session.connected)


def _require(runtime: HCData, key: str, expected, label: str) -> None:
    observed = raw(runtime, key)
    if type(observed) is not type(expected) or observed != expected:
        raise HomeAssistantError(f'NEFF: {label}: expected {expected!r}, observed {observed!r}')


def guard_select(runtime: HCData) -> None:
    if not _connected(runtime):
        raise HomeAssistantError('NEFF disconnected')
    # Selecting a mode is not an implicit power-on request.
    if raw(runtime, POWER) != 2:
        raise HomeAssistantError(
            'NEFF: oven is not On. Turn Power on in Home Assistant '
            'and wait for OperationState=Ready before selecting a program'
        )
    for name, value, label in (
        (POWER, 2, 'PowerState(On)'), (OPERATION, 1, 'OperationState(Ready)'),
        (ACTIVE, 0, 'ActiveProgram(idle)'), (REMOTE, True, 'RemoteControlActive'),
        (LOCAL, False, 'LocalControlActive'),
    ):
        _require(runtime, name, value, label)
    # RemoteControlStartAllowed is NOT needed for selection, as tested on NEFF.


def ready_for_select(runtime: HCData) -> bool:
    return (is_neff(runtime) and _connected(runtime)
            and raw(runtime, POWER) == 2 and raw(runtime, OPERATION) == 1
            and raw(runtime, ACTIVE) == 0 and raw(runtime, REMOTE) is True
            and raw(runtime, LOCAL) is False)


def ready_for_start(runtime: HCData) -> bool:
    """May the Start button be clicked? Not permission to send a start request.

    Readiness for remote start (door, appliance authorization, payload) is
    checked immediately on PRESS. A FALSE permission signal should provide
    a useful HA error, not an inexplicably grey button. The selected program
    can be prepared even when those later signals have not yet arrived.
    """
    return ready_for_select(runtime) and selected_program(runtime) in BY_UID


def ready_for_stop(runtime: HCData) -> bool:
    c = runtime.appliance.commands.get(ABORT)
    return bool(is_neff(runtime) and _connected(runtime)
                and raw(runtime, ACTIVE) not in (None, 0)
                and raw(runtime, LOCAL) is False
                and c is not None and c.available is True
                and c.access in (Access.WRITE_ONLY, Access.READ_WRITE))


def _whole(value, name, low, high):
    # A live integer option can arrive through HA as e.g. 160.0. The oven
    # protocol expects an integer; convert ONLY finite, exactly integral floats.
    # Never round, clamp or substitute a default: stale or invalid live data
    # must keep remote start blocked, with an actionable observed value.
    original = value
    if type(value) is float and value.is_integer():
        value = int(value)
    if type(value) is not int or not low <= value <= high:
        raise HomeAssistantError(
            f'NEFF: {name} must be integer {low}..{high}; '
            f'observed {original!r} (type={type(original).__name__}). '
            'Check the selected mode and the live option value before retrying.'
        )
    return value


def temperature_range(spec: Spec) -> tuple[int, int]:
    # Real limits are mode dependent. Values here are constrained envelopes;
    # firmware remains authoritative. Temperatures below 50 are needed for Proof.
    ranges = {
        8237: (30, 50), 8226: (60, 120), 8229: (40, 100),
        8230: (30, 100), 8227: (50, 220),
        8235: (50, 290), 8236: (50, 290), 8482: (50, 290), 8483: (50, 290),
    }
    return ranges.get(spec.uid, (50, 250))


def _temperature(value, spec: Spec) -> int:
    a, b = temperature_range(spec)
    return _whole(value, f'{spec.label} temperature °C', a, b)


def _duration(value, spec: Spec) -> int:
    return _whole(value, 'duration (seconds)', 60, 7200)


def build_options(spec: Spec, *, temperature: int | None = None,
                  duration_seconds: int | None = None, fast_preheat: bool | None = None,
                  microwave_power: int | None = None,
                  pyro_level: int | None = None) -> list[dict]:
    """Candidate full option set by mode; only 8208/8209 proven remotely."""
    if spec.category == 'pyro':
        level = _whole(pyro_level if pyro_level is not None else 1, 'pyrolysis level', 1, 3)
        if any(x is not None for x in (temperature, duration_seconds, fast_preheat, microwave_power)):
            raise HomeAssistantError('NEFF: pyrolysis time is fixed by level; temperature not configurable')
        return [{'uid': 548, 'value': PYRO_DURATION[level]}, {'uid': 5129, 'value': level}]
    secs = _duration(duration_seconds if duration_seconds is not None else spec.duration, spec)
    options = [{'uid': 548, 'value': secs}]
    if spec.category in ('heat', 'combi'):
        temp = _temperature(temperature if temperature is not None else spec.temp, spec)
        options.append({'uid': 5120, 'value': temp})
    elif temperature is not None:
        raise HomeAssistantError('NEFF: microwave mode has no setpoint temperature')
    if spec.category == 'heat':
        if type(fast_preheat) is not bool and fast_preheat is not None:
            raise HomeAssistantError('NEFF: fast_preheat must be true or false')
        options.append({'uid': 5123, 'value': bool(fast_preheat) if fast_preheat is not None else False})
    elif fast_preheat is not None:
        raise HomeAssistantError('NEFF: fast_preheat is not a verified option for this mode')
    if spec.category in ('microwave', 'combi'):
        power = spec.power if microwave_power is None else microwave_power
        if power not in (9, 18, 36, 60, 100):
            raise HomeAssistantError('NEFF: microwave power must be one of 9/18/36/60/100')
        if spec.category == 'microwave' and power != spec.power:
            raise HomeAssistantError('NEFF: select a different microwave program to change power')
        if spec.category == 'combi' and power == 100:
            raise HomeAssistantError('NEFF: microwave Max not enabled in combi')
        options.append({'uid': 5124, 'value': power})
    elif microwave_power is not None:
        raise HomeAssistantError('NEFF: microwave power not valid for this mode')
    return options


def options_for_current(runtime: HCData, spec: Spec) -> list[dict]:
    """Only use live option values for the selected mode; do not reuse stale values."""
    kw = {}
    sec = raw(runtime, DURATION)
    if sec is None:
        raise HomeAssistantError('NEFF: duration has not been reported; select program again')
    kw['duration_seconds'] = sec
    if spec.category in ('heat', 'combi'):
        t = raw(runtime, TEMPERATURE)
        if t is None:
            raise HomeAssistantError('NEFF: temperature has not been reported')
        kw['temperature'] = t
    if spec.category == 'heat':
        f = raw(runtime, FAST)
        if type(f) is not bool:
            raise HomeAssistantError('NEFF: FastPreHeat has not been reported')
        kw['fast_preheat'] = f
    if spec.category in ('microwave', 'combi'):
        p = raw(runtime, MICROWAVE_POWER)
        if p is None:
            raise HomeAssistantError('NEFF: microwave power has not been reported')
        kw['microwave_power'] = p
    if spec.category == 'pyro':
        raise HomeAssistantError('NEFF: remote pyrolysis start deliberately disabled')
    return build_options(spec, **kw)


class NeffController:
    """Serialize requests. Never retry an unverified write automatically."""

    def __init__(self, runtime: HCData):
        if not is_neff(runtime):
            raise HomeAssistantError('Not a NEFF C28MT23H0')
        self.runtime = runtime

    async def select(self, uid: int, *, temperature: int | None = None,
                     duration_seconds: int | None = None, fast_preheat: bool | None = None,
                     microwave_power: int | None = None,
                     pyro_level: int | None = None,
                     reuse_current: bool = False) -> None:
        spec = BY_UID.get(uid)
        if spec is None:
            raise HomeAssistantError(f'NEFF: unknown program {uid}')
        async with _lock(self.runtime):
            guard_select(self.runtime)
            if reuse_current and selected_program(self.runtime) == uid:
                current = options_for_current(self.runtime, spec) if spec.category != 'pyro' else []
                opts = {x['uid']: x['value'] for x in current}
                if temperature is None: temperature = opts.get(5120)
                if duration_seconds is None: duration_seconds = opts.get(548)
                if fast_preheat is None: fast_preheat = opts.get(5123)
                if microwave_power is None: microwave_power = opts.get(5124)
                if pyro_level is None and spec.category == 'pyro':
                    level = raw(self.runtime, PYRO_LEVEL)
                    pyro_level = level if level in PYRO_DURATION else 1
            options = build_options(spec, temperature=temperature,
                                    duration_seconds=duration_seconds,
                                    fast_preheat=fast_preheat,
                                    microwave_power=microwave_power,
                                    pyro_level=pyro_level)
            if uid not in PROVEN_SELECT:
                _LOGGER.warning('NEFF EXPERIMENTAL selection uid=%s, %s (one attempt)', uid, spec.label)
            else:
                _LOGGER.info('NEFF select verified uid=%s', uid)
            await self.runtime.appliance.session.send_sync(Message(
                resource='/ro/selectedProgram', action=Action.POST,
                data={'program': uid, 'options': options}))

    async def start(self, *, acknowledge_unverified: bool = False) -> None:
        async with _lock(self.runtime):
            guard_select(self.runtime)
            uid = selected_program(self.runtime)
            if uid is None:
                raise HomeAssistantError('NEFF: no selected program reported by oven')
            spec = BY_UID[uid]
            # Never attempt remote start for modes not validated on this oven.
            # Keeping the button clickable allows HA to explain WHY start is
            # refused, rather than permanently greying out the control.
            if spec.category in ('microwave', 'combi', 'pyro'):
                raise HomeAssistantError(
                    f'NEFF: remote start of {spec.label} is unverified and disabled. '
                    'Start this mode from the oven panel.')
            # Start permission and door interlocks are enforced at press time;
            # do NOT bypass a false/unknown device-reported value.
            _require(self.runtime, REMOTE_START, True, 'RemoteControlStartAllowed')
            _require(self.runtime, DOOR, 1, 'DoorState(Closed)')
            if uid not in PROVEN_START and not acknowledge_unverified:
                raise HomeAssistantError(
                    f'NEFF: start of {spec.label} is experimental; '
                    'use neff_start_experimental with acknowledge_unverified=true '
                    'while supervising the oven.')
            options = options_for_current(self.runtime, spec)
            _LOGGER.warning('NEFF remote START program=%s experimental=%s', uid, uid not in PROVEN_START)
            # One request, no automatic retry if the firmware rejects it.
            await self.runtime.appliance.session.send_sync(Message(
                resource='/ro/activeProgram', action=Action.POST,
                data={'program': uid, 'options': options}))

    async def stop(self) -> None:
        async with _lock(self.runtime):
            if not _connected(self.runtime):
                raise HomeAssistantError('NEFF: disconnected; use physical STOP')
            if raw(self.runtime, ACTIVE) in (0, None):
                raise HomeAssistantError('NEFF: no active program')
            if raw(self.runtime, LOCAL) is not False:
                raise HomeAssistantError('NEFF: physical panel active; use physical STOP or try when idle')
            c = self.runtime.appliance.commands.get(ABORT)
            if c is None or c.available is not True or c.access not in (Access.READ_WRITE, Access.WRITE_ONLY):
                raise HomeAssistantError('NEFF: AbortProgram unavailable; use physical STOP')
            _LOGGER.warning('NEFF AbortProgram uid 512, one request')
            await c.set_value(True)


def _mode_label(spec: Spec) -> str:
    return spec.label

LABEL_TO_UID = {_mode_label(spec): spec.uid for spec in SPECS}


class NeffProgramSelect(HCEntity, SelectEntity):
    """24-mode selector, only prepares a program; never starts heating."""

    _attr_options = list(LABEL_TO_UID)

    @property
    def available(self):
        # The selector is a WRITE control. Disable in Standby and during Run;
        # the separate Current program sensor still displays a running mode.
        return ready_for_select(self._runtime_data)

    @property
    def current_option(self):
        uid = current_program(self._runtime_data)
        return _mode_label(BY_UID[uid]) if uid in BY_UID else None

    @error_decorator
    async def async_select_option(self, option: str) -> None:
        if option not in LABEL_TO_UID:
            raise HomeAssistantError('NEFF: unrecognized program')
        await NeffController(self._runtime_data).select(LABEL_TO_UID[option])


class NeffCurrentProgramSensor(HCEntity, SensorEntity):
    @property
    def native_value(self):
        uid = current_program(self._runtime_data)
        return BY_UID[uid].label if uid in BY_UID else None

    @property
    def extra_state_attributes(self):
        uid = current_program(self._runtime_data)
        return {'program_uid': uid, 'running': running_program(self._runtime_data) is not None,
                'category': BY_UID[uid].category if uid in BY_UID else None,
                'remote_start_verified': uid in PROVEN_START if uid else False,
                 'remote_start_allowed_raw': raw(self._runtime_data, REMOTE_START),
                 'door_state_raw': raw(self._runtime_data, DOOR),
                 'start_button_clickable': ready_for_start(self._runtime_data),
                'selection_verified': uid in PROVEN_SELECT if uid else False,
                'power_state_raw': raw(self._runtime_data, POWER),
                'operation_state_raw': raw(self._runtime_data, OPERATION),
                'remote_control_active_raw': raw(self._runtime_data, REMOTE),
                'local_control_active_raw': raw(self._runtime_data, LOCAL),
                'websocket_connected': _connected(self._runtime_data),
                'controls_ready': ready_for_select(self._runtime_data)}


class NeffOptionSensor(HCEntity, SensorEntity):
    @property
    def native_value(self):
        uid = current_program(self._runtime_data)
        if uid is None:
            return None
        spec = BY_UID[uid]
        if self.entity_description.key == 'neff_setpoint_temperature' and spec.category not in ('heat', 'combi'):
            return None
        if self.entity_description.key == 'neff_pyrolysis_level' and spec.category != 'pyro':
            return None
        if self.entity_description.key == 'neff_microwave_power' and spec.category not in ('microwave', 'combi'):
            return None
        return self._entity.value_raw


class NeffParameterNumber(HCEntity, NumberEntity):
    @property
    def native_value(self):
        uid = current_program(self._runtime_data)
        if uid not in BY_UID:
            return None
        v = self._entity.value_raw
        if v is None:
            return None
        return v / 60 if self.entity_description.key == 'neff_duration_minutes' else v

    @property
    def available(self):
        uid = selected_program(self._runtime_data)
        if not ready_for_select(self._runtime_data) or uid not in BY_UID:
            return False
        spec = BY_UID[uid]
        if self.entity_description.key == 'neff_target_temperature':
            return spec.category in ('heat', 'combi') and raw(self._runtime_data, TEMPERATURE) is not None
        if self.entity_description.key == 'neff_duration_minutes':
            return spec.category != 'pyro' and raw(self._runtime_data, DURATION) is not None
        return False

    @property
    def native_min_value(self):
        if self.entity_description.key == 'neff_target_temperature':
            uid = selected_program(self._runtime_data)
            if uid in BY_UID:
                return temperature_range(BY_UID[uid])[0]
        return self.entity_description.native_min_value

    @property
    def native_max_value(self):
        if self.entity_description.key == 'neff_target_temperature':
            uid = selected_program(self._runtime_data)
            if uid in BY_UID:
                return temperature_range(BY_UID[uid])[1]
        return self.entity_description.native_max_value

    @error_decorator
    async def async_set_native_value(self, value):
        uid = selected_program(self._runtime_data)
        if uid not in BY_UID:
            raise HomeAssistantError('NEFF: select a program first')
        if not float(value).is_integer():
            raise HomeAssistantError('NEFF: only whole numbers supported')
        field = ('duration_seconds' if self.entity_description.key == 'neff_duration_minutes'
                 else 'temperature')
        await NeffController(self._runtime_data).select(uid, reuse_current=True,
              **{field: int(value) * 60 if field == 'duration_seconds' else int(value)})


class NeffFastSwitch(HCEntity, SwitchEntity):
    @property
    def is_on(self):
        v = raw(self._runtime_data, FAST)
        return None if v is None else bool(v)

    @property
    def available(self):
        # Remote FastPreHeat selection has only been confirmed for these modes.
        return (ready_for_select(self._runtime_data) and selected_program(self._runtime_data) in PROVEN_SELECT
                and type(raw(self._runtime_data, FAST)) is bool)

    @error_decorator
    async def async_turn_on(self, **kwargs):
        await NeffController(self._runtime_data).select(selected_program(self._runtime_data),
                                                        fast_preheat=True, reuse_current=True)

    @error_decorator
    async def async_turn_off(self, **kwargs):
        await NeffController(self._runtime_data).select(selected_program(self._runtime_data),
                                                        fast_preheat=False, reuse_current=True)


class NeffPyroLevelSelect(HCEntity, SelectEntity):
    _attr_options = ['Level 1 (75 min)', 'Level 2 (90 min)', 'Level 3 (120 min)']

    @property
    def available(self):
        # A pyrolysis level is meaningful only AFTER choosing Pyrolysis (9217)
        # in Cooking program. It is disabled in Standby and during Run.
        return (ready_for_select(self._runtime_data)
                and selected_program(self._runtime_data) == 9217)

    @property
    def current_option(self):
        if current_program(self._runtime_data) != 9217:
            return None
        level = raw(self._runtime_data, PYRO_LEVEL)
        return self._attr_options[level - 1] if type(level) is int and level in PYRO_DURATION else None

    @error_decorator
    async def async_select_option(self, option):
        # Do not bypass the entity's availability rule via a direct service.
        if not self.available:
            raise HomeAssistantError('NEFF: choose Pyrolysis in Cooking program first (On/Ready)')
        if option not in self._attr_options:
            raise HomeAssistantError('NEFF: invalid pyrolysis level')
        level = self._attr_options.index(option) + 1
        await NeffController(self._runtime_data).select(9217, pyro_level=level)


class NeffStartButton(HCEntity, ButtonEntity):
    """Click to start selected program with device-side safety checks."""
    _attr_icon = 'mdi:play-circle-outline'

    @property
    def available(self):
        return ready_for_start(self._runtime_data)

    @error_decorator
    async def async_press(self):
        # A button click is an explicit attended start request for ordinary
        # heating modes. Non-heating / pyrolysis / microwave remain blocked.
        await NeffController(self._runtime_data).start(acknowledge_unverified=True)


class NeffStopButton(HCEntity, ButtonEntity):
    @property
    def available(self):
        return ready_for_stop(self._runtime_data)

    @error_decorator
    async def async_press(self):
        await NeffController(self._runtime_data).stop()


# Separate dependency callbacks make controls refresh after program change and
# permission changes; the polling interval is not changed.
CONTROL_DEPS = [SELECTED, ACTIVE, OPERATION, POWER, REMOTE, REMOTE_START, LOCAL]


def get_platform_entities(platform: str, runtime: HCData) -> list[HCEntity]:
    if not is_neff(runtime):
        return []
    specs: list[tuple[type[HCEntity], object]] = []
    if platform == 'select':
        specs = [
            (NeffProgramSelect, HCSelectEntityDescription(
                key='neff_cooking_mode_v6', name='Cooking program (24 modes)',
                entity=SELECTED, entities=[x for x in CONTROL_DEPS if x != SELECTED])),
            (NeffPyroLevelSelect, HCSelectEntityDescription(
                key='neff_pyrolysis_level_select', name='Cooking program - Pyrolysis level',
                # SelectedProgram always exists; PyrolysisLevel (UID 5129) may
                # appear only after selecting pyrolysis, depending on firmware.
                # Keep the dropdown present but disabled for all other modes.
                entity=SELECTED,
                entities=[x for x in CONTROL_DEPS if x != SELECTED]
                         + ([PYRO_LEVEL] if PYRO_LEVEL in runtime.appliance.entities else []))),
        ]
    elif platform == 'sensor':
        specs = [
            (NeffCurrentProgramSensor, HCSensorEntityDescription(
                key='neff_current_program', name='Current program',
                entity=SELECTED, entities=[x for x in (ACTIVE, REMOTE_START, DOOR)
                                           if x in runtime.appliance.entities], icon='mdi:stove')),
            (NeffOptionSensor, HCSensorEntityDescription(
                key='neff_duration', name='Program duration', entity=DURATION,
                entities=[SELECTED, ACTIVE], device_class=SensorDeviceClass.DURATION,
                native_unit_of_measurement=UnitOfTime.SECONDS,
                suggested_unit_of_measurement=UnitOfTime.MINUTES)),
            (NeffOptionSensor, HCSensorEntityDescription(
                key='neff_setpoint_temperature', name='Set temperature',
                entity=TEMPERATURE, entities=[SELECTED, ACTIVE],
                device_class=SensorDeviceClass.TEMPERATURE,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS)),
            (NeffOptionSensor, HCSensorEntityDescription(
                key='neff_microwave_power', name='Microwave power (raw)',
                entity=MICROWAVE_POWER, entities=[SELECTED, ACTIVE],
                icon='mdi:microwave')),
            (NeffOptionSensor, HCSensorEntityDescription(
                key='neff_pyrolysis_level', name='Pyrolysis level',
                entity=PYRO_LEVEL, entities=[SELECTED, ACTIVE],
                icon='mdi:fire')),
        ]
    elif platform == 'number':
        specs = [
            (NeffParameterNumber, HCNumberEntityDescription(
                key='neff_target_temperature', name='Program target temperature',
                entity=TEMPERATURE, entities=CONTROL_DEPS,
                mode=NumberMode.SLIDER,
                native_min_value=30, native_max_value=290, native_step=1,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS)),
            (NeffParameterNumber, HCNumberEntityDescription(
                key='neff_duration_minutes', name='Program duration setting',
                entity=DURATION, entities=CONTROL_DEPS,
                native_min_value=1, native_max_value=120, native_step=1,
                native_unit_of_measurement=UnitOfTime.MINUTES)),
        ]
    elif platform == 'switch':
        specs = [(NeffFastSwitch, HCSwitchEntityDescription(
            key='neff_fast_preheat', name='Fast preheat', entity=FAST,
            entities=CONTROL_DEPS))]
    elif platform == 'button':
        specs = [
            (NeffStartButton, HCButtonEntityDescription(
                key='neff_start_selected_program', name='Start selected program',
                entity_registry_enabled_default=True, entity=SELECTED,
                # Do not suppress button creation if an option is not in XML.
                # Authorization, interlocks and payload are checked on press.
                entities=[x for x in (ACTIVE, POWER, OPERATION, REMOTE, LOCAL,
                                     REMOTE_START, DOOR)
                          if x in runtime.appliance.entities])),
            (NeffStopButton, HCButtonEntityDescription(
                key='neff_abort_program', name='Stop active program',
                entity=ACTIVE, entities=[LOCAL])),
        ]
    entities = []
    for cls, description in specs:
        needs = ([description.entity] if description.entity else []) + (description.entities or [])
        if all(x in runtime.appliance.entities for x in needs):
            entities.append(cls(description, runtime))
        else:
            _LOGGER.warning('NEFF V6.8 missing entity for %s: %s', description.key,
                            [x for x in needs if x not in runtime.appliance.entities])
    return entities


def register_neff_services(hass: HomeAssistant):
    """Expose HA actions; never start microwave/combi/pyrolysis remotely."""
    from .helpers import error_decorator

    async def resolve(call):
        config = await get_config_entry_from_call(hass, call)
        if config is None or not is_neff(config.runtime_data):
            raise HomeAssistantError('Select NEFF C28MT23H0 device')
        return NeffController(config.runtime_data)

    @error_decorator
    async def select_hot_air(call):
        ctrl = await resolve(call)
        await ctrl.select(8208, temperature=call.data['temperature'],
                          duration_seconds=call.data['minutes'] * 60,
                          fast_preheat=call.data['fast_preheat'])

    @error_decorator
    async def select_program(call):
        ctrl = await resolve(call)
        await ctrl.select(call.data['program_uid'],
                          temperature=call.data.get('temperature'),
                          duration_seconds=(call.data['minutes'] * 60 if 'minutes' in call.data else None),
                          pyro_level=call.data.get('pyrolysis_level'))

    @error_decorator
    async def start(call):
        ctrl = await resolve(call)
        await ctrl.start()

    @error_decorator
    async def start_experimental(call):
        if call.data['acknowledge_unverified'] is not True:
            raise HomeAssistantError('NEFF: explicit acknowledge_unverified=true required')
        ctrl = await resolve(call)
        await ctrl.start(acknowledge_unverified=True)

    @error_decorator
    async def stop(call):
        ctrl = await resolve(call)
        await ctrl.stop()

    device_schema = {vol.Required('device_id'): str}
    hass.services.async_register(DOMAIN, 'neff_select_hot_air', select_hot_air,
        schema=vol.Schema({**device_schema,
          vol.Required('temperature'): vol.All(vol.Coerce(int), vol.Range(min=50, max=250)),
          vol.Required('minutes'): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
          vol.Required('fast_preheat'): bool}))
    hass.services.async_register(DOMAIN, 'neff_select_program', select_program,
        schema=vol.Schema({**device_schema,
          vol.Required('program_uid'): vol.All(vol.Coerce(int), vol.In(list(BY_UID))),
          vol.Optional('temperature'): vol.Coerce(int),
          vol.Optional('minutes'): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
          vol.Optional('pyrolysis_level'): vol.All(vol.Coerce(int), vol.Range(min=1, max=3))}))
    hass.services.async_register(DOMAIN, 'neff_start', start,
        schema=vol.Schema(device_schema))
    hass.services.async_register(DOMAIN, 'neff_start_experimental', start_experimental,
        schema=vol.Schema({**device_schema, vol.Required('acknowledge_unverified'): bool}))
    hass.services.async_register(DOMAIN, 'neff_stop', stop,
        schema=vol.Schema(device_schema))


# NEFF_V62_ENTITY_REMOVE: delete exact duplicate entities from the HA entity registry.
# This runs BEFORE platforms are forwarded, so a previously disabled Start button
# can be enabled on the current startup. Nothing touches the working power switch.
NEFF_LEGACY_ENTITY_IDS = (
    ('sensor', 'sensor_power_state'),            # State of power (NOT switch_power_state)
    ('select', 'select_power_state'),            # Optional stock power dropdown
    ('select', 'select_program'),                # Same unique ID (compatibility)
    ('number', 'number_oven_setpoint_temperature'),
    ('number', 'number_duration'),
    ('switch', 'switch_oven_fast_pre_heat'),
    ('select', 'select_pyrolysis_level'),
    ('button', 'neff_select_hot_air'),           # Old V5.1 button
    ('button', 'neff_start_hot_air'),            # Obsolete V5 Hot Air start button
    ('button', 'button_abort_program'),          # Stock abort duplicates Stop active program
)


def reconcile_neff_entity_registry(hass, config_entry) -> None:
    """Physically remove legacy NEFF entities; preserve the new controls.

    Only changes exact unique IDs belonging to this config entry. The companion
    discovery filter prevents the deleted entities from reappearing on restart.
    """
    if not is_neff(config_entry.runtime_data):
        return
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    dev_id = config_entry.runtime_data.appliance.info['deviceID']
    for platform, key in NEFF_LEGACY_ENTITY_IDS:
        # 'program' is an internal description group; the actual HA entity
        # platform is 'select'. Both variants are harmless to look up.
        entity_id = registry.async_get_entity_id(platform, DOMAIN, f'{dev_id}-{key}')
        if not entity_id:
            continue
        entry = registry.async_get(entity_id)
        if entry and entry.config_entry_id == config_entry.entry_id:
            registry.async_remove(entity_id)
            _LOGGER.info('NEFF removed duplicate entity from registry: %s', entity_id)

    # Ensure the only current start control is Start selected program.
    # Reactivate only those disabled BY THE INTEGRATION, not by the user.
    entity_id = registry.async_get_entity_id('button', DOMAIN, f'{dev_id}-neff_start_selected_program')
    if entity_id:
        entry = registry.async_get(entity_id)
        if (entry and entry.config_entry_id == config_entry.entry_id
                and entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION):
            registry.async_update_entity(entity_id, disabled_by=None)
            _LOGGER.info('NEFF enabled Start selected program entity: %s', entity_id)
