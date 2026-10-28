"""Tests for the hardening/bug-fix changes."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import probatio
import pytest
from custom_components.hive_local_thermostat.const import DOMAIN
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    mock_restore_cache,
)
from pytest_homeassistant_custom_component.typing import MqttMockHAClientGenerator

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .conftest import TOPIC
from .test_integration import (
    SLR1_HEAT,
    SLR2_AUTO,
    _entity_id,
    _published,
    _setup,
)

BOOST = {
    **SLR1_HEAT,
    "system_mode": "emergency_heating",
    "temperature_setpoint_hold_duration": 90,
    "occupied_heating_setpoint": 25,
}


async def _fire(hass: HomeAssistant, payload: Any) -> None:
    async_fire_mqtt_message(
        hass, TOPIC, payload if isinstance(payload, str) else json.dumps(payload)
    )
    await hass.async_block_till_done()


# --------------------------------------------------------------------- preset


async def test_boost_preset_reflects_device(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """Preset is 'none' normally and 'boost' while the device is boosting."""
    await _setup(hass, mqtt_mock_entry, slr1_entry)
    climate = _entity_id(hass, "climate", "climate")

    await _fire(hass, SLR1_HEAT)
    assert hass.states.get(climate).attributes[ATTR_PRESET_MODE] == "none"

    await _fire(hass, BOOST)
    assert hass.states.get(climate).attributes[ATTR_PRESET_MODE] == "boost"

    await _fire(hass, SLR1_HEAT)
    assert hass.states.get(climate).attributes[ATTR_PRESET_MODE] == "none"


# ------------------------------------------------------- state retention


@pytest.mark.parametrize(
    "bad_payload",
    [
        "{not json",
        "[1, 2, 3]",
        '"just a string"',
        json.dumps({"system_mode": "heat"}),  # valid JSON, missing fields
        json.dumps(SLR2_AUTO),  # wrong model for this entry
    ],
)
async def test_bad_payload_keeps_previous_state(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
    bad_payload: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A payload we can't use must not wipe the entities."""
    await _setup(hass, mqtt_mock_entry, slr1_entry)
    climate = _entity_id(hass, "climate", "climate")
    coordinator = slr1_entry.runtime_data.coordinator

    await _fire(hass, SLR1_HEAT)
    assert coordinator.current_temperature == 19.5
    assert coordinator.hvac_mode == HVACMode.HEAT
    before = hass.states.get(climate)

    await _fire(hass, bad_payload)

    assert coordinator.current_temperature == 19.5
    assert coordinator.target_temperature == 21
    assert coordinator.hvac_mode == HVACMode.HEAT
    assert hass.states.get(climate).state == before.state
    assert hass.states.get(climate).attributes == before.attributes
    assert "ERROR" in caplog.text or "error" in caplog.text.lower()


# ----------------------------------------------------- boost correction


async def test_bogus_boost_duration_with_time_left_resends_remaining(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """A >65000 hold duration is replaced by our own estimate of minutes left."""
    mqtt_mock = await _setup(hass, mqtt_mock_entry, slr1_entry)
    await _fire(hass, SLR1_HEAT)

    await _fire(hass, BOOST)  # real boost, 90 minutes, starts tracking
    mqtt_mock.async_publish.reset_mock()

    with patch(
        "custom_components.hive_local_thermostat.coordinator.utcnow",
        return_value=dt_util.utcnow() + timedelta(minutes=30),
    ):
        await _fire(hass, {**BOOST, "temperature_setpoint_hold_duration": 65535})
        await hass.async_block_till_done(wait_background_tasks=True)

    sent = _published(mqtt_mock)
    assert len(sent) == 1
    # ~60 minutes left (allow a minute of slack for test timing)
    minutes = json.loads(sent[0])["temperature_setpoint_hold_duration"]
    assert 58 <= minutes <= 60


async def test_bogus_boost_duration_with_no_time_left_does_not_restart_boost(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """Regression: 0/negative minutes used to start a brand new default boost."""
    mqtt_mock = await _setup(hass, mqtt_mock_entry, slr1_entry)
    coordinator = slr1_entry.runtime_data.coordinator
    await _fire(hass, SLR1_HEAT)
    await _fire(hass, BOOST)
    mqtt_mock.async_publish.reset_mock()

    with patch(
        "custom_components.hive_local_thermostat.coordinator.utcnow",
        return_value=dt_util.utcnow() + timedelta(minutes=200),  # past the 90 min
    ):
        await _fire(hass, {**BOOST, "temperature_setpoint_hold_duration": 65535})
        await hass.async_block_till_done(wait_background_tasks=True)

    assert _published(mqtt_mock) == []
    assert coordinator.heat_boost_remaining == 0


async def test_bogus_boost_duration_with_unknown_start_does_not_restart_boost(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """If we never saw the boost start (e.g. HA restarted) don't invent one."""
    mqtt_mock = await _setup(hass, mqtt_mock_entry, slr1_entry)
    mqtt_mock.async_publish.reset_mock()

    await _fire(hass, {**BOOST, "temperature_setpoint_hold_duration": 65535})
    await hass.async_block_till_done(wait_background_tasks=True)

    assert _published(mqtt_mock) == []
    assert slr1_entry.runtime_data.coordinator.heat_boost_remaining == 0


# ------------------------------------------------------------------ climate


async def test_turning_heat_on_before_any_state_is_a_clean_error(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """Regression: this used to be a bare AssertionError."""
    mqtt_mock = await _setup(hass, mqtt_mock_entry, slr1_entry)
    mqtt_mock.async_publish.reset_mock()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "turn_on",
            {ATTR_ENTITY_ID: _entity_id(hass, "climate", "climate")},
            blocking=True,
        )
    assert _published(mqtt_mock) == []


async def test_heat_mode_after_off_reuses_last_setpoint(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """Switching OFF -> HEAT goes back to the last known target temperature."""
    mqtt_mock = await _setup(hass, mqtt_mock_entry, slr1_entry)
    await _fire(hass, SLR1_HEAT)  # target 21
    await _fire(hass, {**SLR1_HEAT, "system_mode": "off"})
    mqtt_mock.async_publish.reset_mock()

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {
            ATTR_ENTITY_ID: _entity_id(hass, "climate", "climate"),
            "hvac_mode": HVACMode.HEAT,
        },
        blocking=True,
    )
    assert json.loads(_published(mqtt_mock)[0])["occupied_heating_setpoint"] == 21


# ----------------------------------------------------------------- services


async def test_actions_reject_other_integrations_entries(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """A valid entry id belonging to another integration must be refused."""
    await _setup(hass, mqtt_mock_entry, slr1_entry)
    other = MockConfigEntry(domain="some_other_integration", title="Other")
    other.add_to_hass(hass)
    # Loaded, so only the domain check can reject it
    other.mock_state(hass, ConfigEntryState.LOADED)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "cancel_boost_heating",
            {"config_entry_id": other.entry_id},
            blocking=True,
        )


@pytest.mark.parametrize(
    "data",
    [
        {"minutes_to_boost": 181},
        {"minutes_to_boost": -5},
        {"temperature_to_boost": 4.5},
        {"temperature_to_boost": 33},
    ],
)
async def test_boost_action_enforces_device_limits(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
    data: dict[str, Any],
) -> None:
    """Out-of-range values never reach the device."""
    mqtt_mock = await _setup(hass, mqtt_mock_entry, slr1_entry)
    mqtt_mock.async_publish.reset_mock()
    with pytest.raises(probatio.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "boost_heating",
            {"config_entry_id": slr1_entry.entry_id, **data},
            blocking=True,
        )
    assert _published(mqtt_mock) == []


@pytest.mark.parametrize(
    "data",
    [
        {"minutes_to_boost": 180, "temperature_to_boost": 32},
        {"temperature_to_boost": 5},
    ],
)
async def test_boost_action_accepts_limits(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
    data: dict[str, Any],
) -> None:
    """The limits themselves are valid."""
    mqtt_mock = await _setup(hass, mqtt_mock_entry, slr1_entry)
    mqtt_mock.async_publish.reset_mock()
    await hass.services.async_call(
        DOMAIN,
        "boost_heating",
        {"config_entry_id": slr1_entry.entry_id, **data},
        blocking=True,
    )
    assert len(_published(mqtt_mock)) == 1


# -------------------------------------------------------------------- setup


async def test_setup_does_not_block_and_requests_state(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """The initial state request is a background task, not a 2 s setup delay."""
    mqtt_mock = await mqtt_mock_entry()
    slr1_entry.add_to_hass(hass)
    with patch("custom_components.hive_local_thermostat.sleep", side_effect=_never):
        assert await hass.config_entries.async_setup(slr1_entry.entry_id)
        await hass.async_block_till_done()  # background task is NOT awaited here
    assert slr1_entry.state is ConfigEntryState.LOADED
    assert not any(
        c.args[0].endswith("/get") for c in mqtt_mock.async_publish.call_args_list
    )


async def _never(_delay: float) -> None:
    await asyncio.Event().wait()


async def test_initial_state_request_failure_does_not_fail_setup(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broker hiccup while requesting state is logged, not fatal."""
    mqtt_mock = await mqtt_mock_entry()
    mqtt_mock.async_publish.side_effect = HomeAssistantError("MQTT not connected")
    slr1_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(slr1_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert slr1_entry.state is ConfigEntryState.LOADED
    assert "Could not request initial state" in caplog.text


async def test_setup_retries_when_mqtt_unavailable(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """No MQTT client -> ConfigEntryNotReady (HA retries) rather than a hard failure."""
    await mqtt_mock_entry()
    slr1_entry.add_to_hass(hass)
    with patch(
        "custom_components.hive_local_thermostat.mqtt.async_wait_for_mqtt_client",
        return_value=False,
    ):
        assert not await hass.config_entries.async_setup(slr1_entry.entry_id)
    assert slr1_entry.state is ConfigEntryState.SETUP_RETRY


# ------------------------------------------------------ select / number


async def test_select_survives_unexpected_water_mode(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr2_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: an unknown water mode used to raise ValueError in the callback."""
    await _setup(hass, mqtt_mock_entry, slr2_entry)
    select = _entity_id(hass, "select", "system_mode_water")

    await _fire(
        hass,
        {
            **SLR2_AUTO,
            "system_mode_water": "heat",
            "temperature_setpoint_hold_water": True,
        },
    )
    assert hass.states.get(select).state == "heat"

    await _fire(hass, {**SLR2_AUTO, "system_mode_water": "something_new"})
    assert hass.states.get(select).state == STATE_UNKNOWN
    assert "ValueError" not in caplog.text
    assert "Invalid option" not in caplog.text


async def test_select_ignores_invalid_restored_state(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr2_entry: MockConfigEntry,
) -> None:
    """'unavailable'/'unknown' from a previous run must not become an option."""
    mock_restore_cache(
        hass,
        [State("select.hive_water_mode", "unavailable")],
    )
    await _setup(hass, mqtt_mock_entry, slr2_entry)
    state = hass.states.get(_entity_id(hass, "select", "system_mode_water"))
    assert state.state == STATE_UNKNOWN


async def test_number_defaults_used_when_nothing_restored(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr2_entry: MockConfigEntry,
) -> None:
    """Boost/frost numbers always end up with a usable value on the coordinator."""
    await _setup(hass, mqtt_mock_entry, slr2_entry)
    coordinator = slr2_entry.runtime_data.coordinator
    assert coordinator.heating_boost_duration == 120
    assert coordinator.heating_boost_temperature == 25
    assert coordinator.heating_frost_prevention == 12
    assert coordinator.water_boost_duration == 60
