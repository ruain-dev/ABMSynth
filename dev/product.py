"""Canonical ABMSynth product identifiers shared by Python runtime layers."""

PRODUCT_NAME = "ABMSynth"
PRODUCT_VERSION = "1.0.0"
PRODUCT_TAG = "v" + PRODUCT_VERSION
PRODUCT_PUBLISHER = "ruain"
PRODUCT_COPYRIGHT = "Copyright (c) 2026 ruain"
APP_USER_MODEL_ID = "ABMSynth.Desktop"
MIDI_PORT_NAME = "ABMSynth MIDI In"
WINMM_PIPE_NAME = r"\\.\pipe\ABMSynthMidiIn"

# Environment variables are developer/deployment configuration, not localized
# UI copy. Keep their names aligned with the public product brand.
ENV_LOCALE = "ABMSYNTH_LOCALE"
ENV_UI_ONLY = "ABMSYNTH_UI_ONLY"

