"""Fixtures for Hive Local Thermostat tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from custom_components.hive_local_thermostat.const import (
    CONF_MODEL,
    CONF_MQTT_TOPIC,
    CONF_SHOW_HEAT_SCHEDULE_MODE,
    CONF_SHOW_WATER_SCHEDULE_MODE,
    DOMAIN,
    MODEL_SLR1,
    MODEL_SLR2,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

TOPIC = "zigbee2mqtt/HiveReceiver"


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Provide a config dir with an (empty) configuration.yaml.

    The test plugin's wheel does not ship one, and MQTT reads it during setup.
    """
    (Path(hass_tmp_config_dir) / "configuration.yaml").touch()
    return hass_tmp_config_dir


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading of custom integrations in all tests."""


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Allow the periodic timer that Home Assistant's MQTT client keeps running."""
    return True


@pytest.fixture(autouse=True)
async def unload_entries(hass: HomeAssistant) -> AsyncGenerator[None]:
    """Unload our config entries after each test so no timers/tasks linger.

    The MQTT entry is left for Home Assistant's own shutdown to clean up.
    """
    yield
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
def no_sleep() -> Generator[None]:
    """Skip the deliberate pauses used when talking to the thermostat."""

    async def _instant(_delay: float) -> None:
        return None

    with (
        patch("custom_components.hive_local_thermostat.sleep", _instant),
        patch("custom_components.hive_local_thermostat.coordinator.sleep", _instant),
    ):
        yield


def _entry(model: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Hive",
        version=1,
        options={
            "name": "Hive",
            CONF_MQTT_TOPIC: TOPIC,
            CONF_MODEL: model,
            CONF_SHOW_HEAT_SCHEDULE_MODE: True,
            CONF_SHOW_WATER_SCHEDULE_MODE: True,
        },
    )


@pytest.fixture
def slr1_entry() -> MockConfigEntry:
    """Config entry for a heating-only receiver."""
    return _entry(MODEL_SLR1)


@pytest.fixture
def slr2_entry() -> MockConfigEntry:
    """Config entry for a heating + hot water receiver."""
    return _entry(MODEL_SLR2)
