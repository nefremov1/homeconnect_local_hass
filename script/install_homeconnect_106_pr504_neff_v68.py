#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Home Connect Local 1.0.6 + PR #504 + NEFF V6.8 - clean 1.0.6 installation.

Single run, no --apply, --self-test, profile verification or version preflight.
Copy datetime.py and neff_oven.py manually into /config/custom_components/homeconnect_ws/ BEFORE running this installer. The installer never copies, imports, or validates these modules.

Only patches the existing integration. It does not modify imported XML or JSON with PSK/IV. It removes only
specified duplicate NEFF entries via Home Assistant entity_registry API.
Important: Runtime safety restrictions on oven heating/pyrolysis remain.
"""
from __future__ import annotations

import time
from pathlib import Path
import re
import shutil

ROOT = Path('/config/custom_components/homeconnect_ws')

SERVICE_DEFINITIONS = r'''neff_select_hot_air:
  name: NEFF Hot Air configuration
  description: "Select Hot Air without starting heat; sends all 3 validated options."
  fields:
    device_id:
      required: true
      selector:
        device:
          integration: homeconnect_ws
    temperature:
      required: true
      selector:
        number:
          min: 50
          max: 250
          step: 1
          unit_of_measurement: "°C"
    minutes:
      required: true
      selector:
        number:
          min: 1
          max: 120
          step: 1
          unit_of_measurement: min
    fast_preheat:
      required: true
      selector:
        boolean: {}

neff_start:
  name: NEFF Start selected program
  description: "Starts selected ordinary heating mode when oven allows remote start. Non-heating, microwave and pyrolysis starts remain blocked."
  fields:
    device_id:
      required: true
      selector:
        device:
          integration: homeconnect_ws

neff_stop:
  name: NEFF Abort current program
  description: "AbortProgram UID 512; never DELETE /ro/activeProgram."
  fields:
    device_id:
      required: true
      selector:
        device:
          integration: homeconnect_ws

neff_select_program:
  name: NEFF Select program (24 modes)
  description: "Select program WITHOUT starting heat. Unverified selections may reset oven to Standby."
  fields:
    device_id:
      required: true
      selector:
        device:
          integration: homeconnect_ws
    program_uid:
      required: true
      selector:
        number:
          min: 0
          max: 99999
          step: 1
          mode: box
    temperature:
      required: false
      selector:
        number:
          min: 30
          max: 290
          step: 1
          unit_of_measurement: "°C"
    minutes:
      required: false
      selector:
        number:
          min: 1
          max: 120
          step: 1
    pyrolysis_level:
      required: false
      selector:
        number:
          min: 1
          max: 3
          step: 1

neff_start_experimental:
  name: NEFF Start unverified conventional heating mode (attended test)
  description: "Unverified start. Requires explicit acknowledgement. NEVER starts microwave/combi/pyrolysis."
  fields:
    device_id:
      required: true
      selector:
        device:
          integration: homeconnect_ws
    acknowledge_unverified:
      required: true
      selector:
        boolean: {}

'''


def update(relative_path: str, transform) -> None:
    path = ROOT / relative_path
    original = path.read_text(encoding='utf-8').replace('\r\n', '\n')
    changed = transform(original)
    if changed != original:
        path.write_text(changed, encoding='utf-8')
        print('UPDATED', relative_path)
    else:
        print('UNCHANGED', relative_path)


def patch_const(s: str) -> str:
    if '    Platform.DATETIME,' not in s:
        s = s.replace('    Platform.NUMBER,\n', '    Platform.NUMBER,\n    Platform.DATETIME,\n', 1)
    return s


def patch_descriptions_init(s: str) -> str:
    if '    HCDateTimeEntityDescription,\n' not in s:
        s = s.replace('    HCButtonEntityDescription,\n',
                      '    HCButtonEntityDescription,\n    HCDateTimeEntityDescription,\n', 1)
    if '        "datetime": [],' not in s:
        s = s.replace('        "button": [],\n', '        "button": [],\n        "datetime": [],\n', 1)
    if '    "HCDateTimeEntityDescription",' not in s:
        s = s.replace('    "HCButtonEntityDescription",\n',
                      '    "HCButtonEntityDescription",\n    "HCDateTimeEntityDescription",\n', 1)
    return s


def patch_descriptions_definitions(s: str) -> str:
    if 'from homeassistant.components.datetime import DateTimeEntityDescription' not in s:
        s = s.replace('from homeassistant.components.button import ButtonEntityDescription\n',
                      'from homeassistant.components.button import ButtonEntityDescription\n'
                      'from homeassistant.components.datetime import DateTimeEntityDescription\n', 1)
    if 'class HCDateTimeEntityDescription(' not in s:
        s = s.replace('class HCLightEntityDescription(',
                      'class HCDateTimeEntityDescription(\n'
                      '    HCEntityDescription, DateTimeEntityDescription, frozen_or_thawed=True\n'
                      '):\n'
                      '    """Description for DateTime Entity."""\n\n'
                      '    available_access: tuple[Access] = (Access.READ_WRITE,)\n\n\n'
                      'class HCLightEntityDescription(', 1)
    if '    datetime: list[HCDateTimeEntityDescription]' not in s:
        s = s.replace('    button: list[HCButtonEntityDescription]\n',
                      '    button: list[HCButtonEntityDescription]\n'
                      '    datetime: list[HCDateTimeEntityDescription]\n', 1)
    if '        "datetime",' not in s:
        # PR504 adds datetime to both TypedDict Literal unions.
        s = s.replace('        "button",\n', '        "button",\n        "datetime",\n')
    return s


def patch_common(s: str) -> str:
    if '    HCDateTimeEntityDescription,\n' not in s:
        s = s.replace('    HCButtonEntityDescription,\n',
                      '    HCButtonEntityDescription,\n    HCDateTimeEntityDescription,\n', 1)
    if '        HCDateTimeEntityDescription(\n' not in s:
        s = s.replace('    "binary_sensor": [\n',
                      '    "datetime": [\n'
                      '        HCDateTimeEntityDescription(\n'
                      '            key="datetime_local_time",\n'
                      '            name="Local time",\n'
                      '            entity="BSH.Common.Setting.LocalTime",\n'
                      '            entity_category=EntityCategory.CONFIG,\n'
                      '        ),\n'
                      '    ],\n'
                      '    "binary_sensor": [\n', 1)
    return s


def patch_platform(s: str, platform: str) -> str:
    """Attach NEFF entities; keep selection control order on a new HA install."""
    marker = f'entities.update(get_platform_entities("{platform}", config_entry.runtime_data))'
    if marker in s or '# NEFF_V68_PLATFORM' in s:
        return s
    anchor = '    async_add_entites(entities)'
    if platform == 'select':
        # Upstream create_entities returns a *set*, so set.update() destroys
        # the requested ordering of Cooking program and Pyrolysis level.
        addition = (
            '    # NEFF_V68_PLATFORM: preserve program -> pyrolysis dropdown order\n'
            '    custom_neff_selects = []\n'
            '    try:\n'
            '        from .neff_oven import get_platform_entities\n'
            '        custom_neff_selects = get_platform_entities("select", config_entry.runtime_data)\n'
            '    except Exception:\n'
            '        import logging\n'
            '        logging.getLogger(__name__).exception("NEFF V6.8 select setup failed")\n'
            '    if custom_neff_selects:\n'
            '        async_add_entites([*custom_neff_selects, *sorted(entities, key=lambda e: e.entity_description.key)])\n'
            '        return\n'
        )
        return replace_once(s, anchor, addition + anchor, f'{platform}.py')
    addition = (
        '    # NEFF_V68_PLATFORM: optional extensions must not break HA setup\n'
        '    try:\n'
        '        from .neff_oven import get_platform_entities\n'
        f'        {marker}\n'
        '    except Exception:\n'
        '        import logging\n'
        f'        logging.getLogger(__name__).exception("NEFF V6.8 {platform} setup failed")\n'
    )
    if platform == 'sensor':
        addition += (
            '    # Read-only oven clock: does not write to appliance or duplicate datetime editor.\n'
            '    try:\n'
            '        from .datetime import make_live_oven_clock\n'
            '        live_clock = make_live_oven_clock(config_entry.runtime_data)\n'
            '        if live_clock is not None:\n'
            '            entities.add(live_clock)\n'
            '    except Exception:\n'
            '        import logging\n'
            '        logging.getLogger(__name__).exception("NEFF live clock sensor setup failed")\n'
        )
    return replace_once(s, anchor, addition + anchor, f'{platform}.py')


def replace_once(s: str, marker: str, replacement: str, path: str) -> str:
    n = s.count(marker)
    if n != 1:
        raise RuntimeError(f'{path}: expected one integration anchor, found {n}: {marker[:70]!r}')
    return s.replace(marker, replacement, 1)


def patch_init(s: str) -> str:
    """Register NEFF actions, but failures must not prevent built-in devices loading."""
    if 'register_neff_services(hass)' not in s:
        anchor = '    hass.services.async_register(DOMAIN, "set_finish_in", handle_set_finish_in)\n'
        registration = (
            anchor
            + '    # NEFF_V68: optional extension must not break integration startup\n'
            + '    try:\n'
            + '        from .neff_oven import register_neff_services\n'
            + '        register_neff_services(hass)\n'
            + '    except Exception:\n'
            + '        _LOGGER.exception("NEFF V6.8 service initialization failed; base integration stays available")\n'
        )
        s = replace_once(s, anchor, registration, '__init__.py')
    if '# NEFF_V6_NATIVE_START_GUARD' not in s:
        anchor = (
            '    async def handle_start_program(call: ServiceCall) -> ServiceResponse:\n'
            '        config_entry = await get_config_entry_from_call(hass, call)\n'
        )
        addition = anchor + (
            '        # NEFF_V6_NATIVE_START_GUARD (safety, applies ONLY to NEFF)\n'
            '        _info = config_entry.runtime_data.appliance.info\n'
            '        if _info.get("brand", "").upper() == "NEFF" and _info.get("vib") == "C28MT23H0":\n'
            '            if "start_in" in call.data or "finish_in" in call.data:\n'
            '                from homeassistant.exceptions import HomeAssistantError\n'
            '                raise HomeAssistantError("NEFF: delayed start is not validated")\n'
            '            from .neff_oven import NeffController\n'
            '            await NeffController(config_entry.runtime_data).start()\n'
            '            return None\n'
        )
        s=replace_once(s,anchor,addition,'__init__.py')
    return s


def patch_services(s: str) -> str:
    if 'neff_select_hot_air:' not in s:
        s = s.rstrip() + '\n\n' + SERVICE_DEFINITIONS.rstrip() + '\n'
    return s




# NEFF_V62_ENTITY_REMOVE discovery filter: suppress old descriptions BEFORE
# Home Assistant creates any duplicate entity on each integration reload.
def patch_remove_neff_entity_descriptions(s: str) -> str:
    marker = '    return available_entities'
    if '# NEFF_V62_ENTITY_REMOVE' in s:
        return s
    block = '''    # NEFF_V62_ENTITY_REMOVE: only suppress duplicated controls on this NEFF.
    if (appliance.info.get("brand", "").upper() == "NEFF"
            and appliance.info.get("vib") == "C28MT23H0"):
        unwanted = {
            "sensor": {"sensor_power_state"},
            "select": {"select_power_state", "select_pyrolysis_level"},
            "program": {"select_program"},
            "number": {"number_oven_setpoint_temperature", "number_duration"},
            "switch": {"switch_oven_fast_pre_heat"},
            "button": {"button_abort_program", "neff_select_hot_air"},
            "start_button": {"button_start_program"},
        }
        for platform, keys in unwanted.items():
            available_entities[platform] = [
                description for description in available_entities[platform]
                if description.key not in keys
            ]
'''
    return s.replace(marker, block + marker, 1)


def patch_reconcile_neff_registry(s: str) -> str:
    """Best-effort cleanup; never crash config entry loading."""
    if 'reconcile_neff_entity_registry(hass, config_entry)' in s:
        return s
    anchor = '    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)'
    hook = (
        '    # NEFF_V68: cleanup AFTER native platform setup, never abort on failure\n'
        '    try:\n'
        '        from .neff_oven import reconcile_neff_entity_registry\n'
        '        reconcile_neff_entity_registry(hass, config_entry)\n'
        '    except Exception:\n'
        '        _LOGGER.exception("NEFF V6.8 optional registry cleanup failed")\n'
    )
    # Keep hook before forwarding? Move AFTER to avoid registry race w/ device registry? after is safer.
    return replace_once(s,anchor, anchor+'\n'+hook.rstrip('\n'), '__init__.py')


def patch_services_remove_legacy_hide(s: str) -> str:
    """Remove only the obsolete neff_cleanup_ui service declaration."""
    lines = s.splitlines(keepends=True)
    output = []
    removing = False
    for line in lines:
        if line.startswith('neff_cleanup_ui:'):
            removing = True
            continue
        if removing and line and not line.startswith((' ', '\t', '\n', '\r')):
            removing = False
        if not removing:
            output.append(line)
    return ''.join(output)


def main() -> None:
    """Install once on clean 1.0.6, no --apply needed. Repeatable and rollback-safe."""
    paths = {
        'const.py': [patch_const],
        'entity_descriptions/__init__.py': [patch_descriptions_init, patch_remove_neff_entity_descriptions],
        'entity_descriptions/descriptions_definitions.py': [patch_descriptions_definitions],
        'entity_descriptions/common.py': [patch_common],
        'sensor.py': [lambda s: patch_platform(s, 'sensor')],
        'number.py': [lambda s: patch_platform(s, 'number')],
        'switch.py': [lambda s: patch_platform(s, 'switch')],
        'button.py': [lambda s: patch_platform(s, 'button')],
        'select.py': [lambda s: patch_platform(s, 'select')],
        '__init__.py': [patch_init, patch_reconcile_neff_registry],
        'services.yaml': [lambda s: patch_services_remove_legacy_hide(patch_services(s))],
    }
    # A stray datetime.py at HA config root can shadow Python's standard
    # library and cause unrelated integration imports to fail.
    for stray in (ROOT.parent.parent / 'datetime.py', Path('/homeassistant/datetime.py')):
        if stray.is_file():
            raise RuntimeError(
                f'Conflicting {stray}: move it OUT of /config or /homeassistant. '
                'PR #504 datetime.py belongs only in custom_components/homeconnect_ws.'
            )
    # These modules are copied manually by the user. Never edit them.
    for module in ('neff_oven.py', 'datetime.py'):
        p = ROOT / module
        if not p.is_file():
            raise RuntimeError(f'Missing {p}. Copy both modules to homeconnect_ws first.')
        compile(p.read_bytes(), str(p), 'exec')
    replacements = {}
    for relative, funcs in paths.items():
        path=ROOT / relative
        original=path.read_text(encoding='utf-8')
        staged=original.replace('\r\n','\n')
        for func in funcs:
            staged=func(staged)
        if relative.endswith('.py'):
            compile(staged, str(path), 'exec')
        if staged != original.replace('\r\n','\n'):
            replacements[relative]=staged
    if not replacements:
        print('ALREADY INSTALLED: no code changes needed.')
        return
    backup=ROOT.parent / ('homeconnect_ws_before_neff_v68_' + time.strftime('%Y%m%d_%H%M%S')
                          + '_' + str(time.time_ns()%1_000_000_000))
    shutil.copytree(ROOT,backup)
    print('BACKUP:',backup)
    written=[]
    try:
        for rel,new in replacements.items():
            p=ROOT/rel
            tmp=p.with_name(p.name+'.neff_v68_tmp')
            tmp.write_text(new,encoding='utf-8')
            tmp.replace(p)
            written.append(rel)
            print('UPDATED',rel)
    except Exception:
        for rel in written:
            shutil.copy2(backup/rel,ROOT/rel)
        raise
    print('DONE: restart Home Assistant. datetime.py and neff_oven.py NOT modified.')
    print('Backup: '+str(backup))


if __name__ == '__main__':
    main()
