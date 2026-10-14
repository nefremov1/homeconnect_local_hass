# NEFF C28MT23H0 — Home Connect Local 1.0.6 + PR #504 + V6.8.1

This bundle contains the latest supplied `datetime.py`, `neff_oven.py` and `install_homeconnect_106_pr504_neff_v68.py`.

## Install on a clean 1.0.6 Home Assistant integration

1. Back up your existing `/config/custom_components/homeconnect_ws/` and Home Assistant configuration.
2. Copy `custom_components/homeconnect_ws/datetime.py` and `custom_components/homeconnect_ws/neff_oven.py` from this branch to `/config/custom_components/homeconnect_ws/`.
3. Copy `script/install_homeconnect_106_pr504_neff_v68.py` to `/tmp/install_homeconnect_106_pr504_neff_v68.py` in the Home Assistant container. Do **not** put a standalone `datetime.py` directly in `/config`.
4. Within that same environment, run `python3 /tmp/install_homeconnect_106_pr504_neff_v68.py`.
5. Check the console for `DONE`, note the generated backup location, and restart Home Assistant.

The installer expects the unmodified integration version 1.0.6 layout, applies the date/time platform changes from PR #504 and the NEFF-specific updates, checks staged Python syntax, and creates a timestamped backup before writing. It does **not** copy or validate the XML/PSK/IV configuration.

## Functionality and safety

- 24 selected cooking programs, mode-specific options, temperature, duration, fast preheat and pyrolysis level controls.
- Local time datetime entity and a separate read-only ticking clock sensor.
- NEFF service actions to select, start and stop programs; safeguards for unsafe or unverified remote starts.
- Microwave/combi/pyrolysis remote start intentionally disabled. Conventional heating beyond Hot Air (UID 8208) is experimental and should be attended.
- Removal of specifically identified duplicate NEFF registry entries.

## Scope

These three source files are preserved as supplied on 14 October 2026. The installer applies its changes to a **clean version 1.0.6** install, not to an already-patched integration directory. This PR does not itself bake every installer's modification into the repository's main integration files: use the installer when deploying. No live oven end-to-end test was performed as part of preparing the PR.
