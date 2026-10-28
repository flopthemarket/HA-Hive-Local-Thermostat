"""Smoke tests running the integration against the installed Home Assistant."""

from __future__ import annotations

import json
from typing import Any

import probatio
import pytest
from custom_components.hive_local_thermostat.const import DOMAIN
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    MqttMockHAClientGenerator,
)

from homeassistant.components.climate import (
    ATTR_HVAC_ACTION,
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .conftest import TOPIC

SLR1_HEAT: dict[str, Any] = {
    "local_temperature": 19.5,
    "occupied_heating_setpoint": 21,
    "running_state": "idle",
    "system_mode": "heat",
    "temperature_setpoint_hold": True,
    "temperature_setpoint_hold_duration": 0,
}

SLR2_AUTO: dict[str, Any] = {
    "local_temperature_heat": 18.0,
    "occupied_heating_setpoint_heat": 20,
    "running_state_heat": "heat",
    "system_mode_heat": "heat",
    "temperature_setpoint_hold_heat": False,
    "temperature_setpoint_hold_duration_heat": 0,
    "system_mode_water": "off",
    "temperature_setpoint_hold_water": False,
    "temperature_setpoint_hold_duration_water": 0,
}


def _entity_id(hass: HomeAssistant, domain: str, key: str) -> str:
    """Look an entity up by platform/domain/unique-id suffix."""
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if (
            entry.platform == DOMAIN
            and entry.domain == domain
            and key in (entry.unique_id or "")
        ):
            return entry.entity_id
    msg = f"No {domain} entity matching {key!r}"
    raise AssertionError(msg)


async def _setup(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    entry: MockConfigEntry,
) -> Any:
    mqtt_mock = await mqtt_mock_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return mqtt_mock


def _published(mqtt_mock: Any) -> list[Any]:
    return [
        call.args[1]
        for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == f"{TOPIC}/set"
    ]


async def test_config_flow_creates_entry(
    hass: HomeAssistant, mqtt_mock_entry: MqttMockHAClientGenerator
) -> None:
    """The UI config flow works on this HA version."""
    await mqtt_mock_entry()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Lounge",
            "mqtt_topic": TOPIC,
            "model": "SLR2",
            "show_heat_schedule_mode": True,
            "show_water_schedule_mode": False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Lounge"
    assert result["options"]["model"] == "SLR2"
    assert result["options"]["show_water_schedule_mode"] is False


async def test_options_flow(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """The options flow works and reloads the entry."""
    await _setup(hass, mqtt_mock_entry, slr1_entry)

    result = await hass.config_entries.options.async_init(slr1_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "mqtt_topic": TOPIC,
            "model": "SLR1",
            "show_heat_schedule_mode": False,
            "show_water_schedule_mode": True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert slr1_entry.options["show_heat_schedule_mode"] is False
    assert slr1_entry.state is ConfigEntryState.LOADED


async def test_slr1_setup_state_and_unload(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """Heating-only receiver: set up, receive state, unload."""
    mqtt_mock = await _setup(hass, mqtt_mock_entry, slr1_entry)
    assert slr1_entry.state is ConfigEntryState.LOADED
    # coordinator got the entry explicitly (no ContextVar reliance)
    assert slr1_entry.runtime_data.coordinator.config_entry is slr1_entry
    # initial state request was published
    assert any(
        call.args[:2] == (f"{TOPIC}/get", '{"system_mode":""}')
        for call in mqtt_mock.async_publish.call_args_list
    )

    async_fire_mqtt_message(hass, TOPIC, json.dumps(SLR1_HEAT))
    await hass.async_block_till_done()

    climate = hass.states.get(_entity_id(hass, "climate", "climate"))
    assert climate is not None
    assert climate.state == HVACMode.HEAT
    assert climate.attributes[ATTR_TEMPERATURE] == 21
    assert climate.attributes["current_temperature"] == 19.5
    assert climate.attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE
    # SLR1 must not create the SLR2-only select platform
    entities = er.async_entries_for_config_entry(
        er.async_get(hass), slr1_entry.entry_id
    )
    assert entities
    assert all(entity.domain != "select" for entity in entities)

    assert await hass.config_entries.async_unload(slr1_entry.entry_id)
    await hass.async_block_till_done()
    assert slr1_entry.state is ConfigEntryState.NOT_LOADED


async def test_slr1_auto_and_boost_state(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """Schedule mode maps to AUTO; emergency heating is tracked as a boost."""
    await _setup(hass, mqtt_mock_entry, slr1_entry)
    climate_id = _entity_id(hass, "climate", "climate")

    async_fire_mqtt_message(
        hass, TOPIC, json.dumps({**SLR1_HEAT, "temperature_setpoint_hold": False})
    )
    await hass.async_block_till_done()
    assert hass.states.get(climate_id).state == HVACMode.AUTO

    async_fire_mqtt_message(
        hass,
        TOPIC,
        json.dumps(
            {
                **SLR1_HEAT,
                "system_mode": "emergency_heating",
                "temperature_setpoint_hold_duration": 90,
                "occupied_heating_setpoint": 25,
            }
        ),
    )
    await hass.async_block_till_done()
    state = hass.states.get(climate_id)
    assert state.state == HVACMode.HEAT
    assert slr1_entry.runtime_data.coordinator.heat_boost is True
    assert slr1_entry.runtime_data.coordinator.heat_boost_remaining == 90
    assert state.attributes[ATTR_TEMPERATURE] == 25


async def test_climate_services_publish_to_mqtt(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """Standard climate actions translate into Hive MQTT payloads."""
    mqtt_mock = await _setup(hass, mqtt_mock_entry, slr1_entry)
    climate_id = _entity_id(hass, "climate", "climate")
    async_fire_mqtt_message(hass, TOPIC, json.dumps(SLR1_HEAT))
    await hass.async_block_till_done()
    mqtt_mock.async_publish.reset_mock()

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: climate_id, ATTR_TEMPERATURE: 22.5},
        blocking=True,
    )
    assert '{"occupied_heating_setpoint":22.5}' in _published(mqtt_mock)

    mqtt_mock.async_publish.reset_mock()
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: climate_id, "hvac_mode": HVACMode.AUTO},
        blocking=True,
    )
    assert any('"temperature_setpoint_hold":"0"' in p for p in _published(mqtt_mock))

    mqtt_mock.async_publish.reset_mock()
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_PRESET_MODE,
        {ATTR_ENTITY_ID: climate_id, ATTR_PRESET_MODE: "boost"},
        blocking=True,
    )
    assert any("emergency_heating" in p for p in _published(mqtt_mock))


async def test_integration_actions(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """Custom boost actions validate with Probatio and publish."""
    mqtt_mock = await _setup(hass, mqtt_mock_entry, slr1_entry)
    mqtt_mock.async_publish.reset_mock()

    await hass.services.async_call(
        DOMAIN,
        "boost_heating",
        {
            "config_entry_id": slr1_entry.entry_id,
            "minutes_to_boost": 45,
            "temperature_to_boost": 24.5,
        },
        blocking=True,
    )
    payload = _published(mqtt_mock)[-1]
    assert '"temperature_setpoint_hold_duration":45' in payload
    assert '"occupied_heating_setpoint":24.5' in payload

    # Cancel works
    await hass.services.async_call(
        DOMAIN,
        "cancel_boost_heating",
        {"config_entry_id": slr1_entry.entry_id},
        blocking=True,
    )

    # Schema validation (Probatio) rejects bad input
    with pytest.raises(probatio.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "boost_heating",
            {"config_entry_id": slr1_entry.entry_id, "minutes_to_boost": "abc"},
            blocking=True,
        )

    # Water actions are SLR2 only
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "boost_water",
            {"config_entry_id": slr1_entry.entry_id},
            blocking=True,
        )


async def test_slr2_water_and_platforms(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr2_entry: MockConfigEntry,
) -> None:
    """Heating + water receiver sets up the select platform and water boost."""
    mqtt_mock = await _setup(hass, mqtt_mock_entry, slr2_entry)
    async_fire_mqtt_message(hass, TOPIC, json.dumps(SLR2_AUTO))
    await hass.async_block_till_done()

    assert (
        hass.states.get(_entity_id(hass, "climate", "climate")).state == HVACMode.AUTO
    )
    assert hass.states.get(_entity_id(hass, "select", "")) is not None
    assert slr2_entry.runtime_data.coordinator.water_mode == "off"

    mqtt_mock.async_publish.reset_mock()
    await hass.services.async_call(
        DOMAIN,
        "boost_water",
        {"config_entry_id": slr2_entry.entry_id, "minutes_to_boost": 30},
        blocking=True,
    )
    assert any(
        '"temperature_setpoint_hold_duration_water":30' in p
        for p in _published(mqtt_mock)
    )

    async_fire_mqtt_message(
        hass,
        TOPIC,
        json.dumps(
            {
                **SLR2_AUTO,
                "system_mode_water": "emergency_heating",
                "temperature_setpoint_hold_duration_water": 30,
            }
        ),
    )
    await hass.async_block_till_done()
    assert slr2_entry.runtime_data.coordinator.water_boost is True


async def test_wrong_model_payload_is_ignored(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An SLR2 payload on an SLR1 entry is rejected with a helpful error."""
    await _setup(hass, mqtt_mock_entry, slr1_entry)
    async_fire_mqtt_message(hass, TOPIC, json.dumps(SLR2_AUTO))
    await hass.async_block_till_done()
    assert "check you have the correct model set" in caplog.text


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """Diagnostics export works."""
    await _setup(hass, mqtt_mock_entry, slr1_entry)
    async_fire_mqtt_message(hass, TOPIC, json.dumps(SLR1_HEAT))
    await hass.async_block_till_done()

    diag = await get_diagnostics_for_config_entry(hass, hass_client, slr1_entry)
    assert diag["config"]["model"] == "SLR1"
    assert diag["coordinator_state"]["current_temperature"] == 19.5
    assert diag["last_mqtt_payload"]["system_mode"] == "heat"
