"""Pin the exact MQTT payloads sent to the receiver.

These were captured from the original string-concatenating implementation, so
they prove the move to ``json.dumps`` did not change anything the device sees.
The one intentional difference is the second ``off`` payload, which used to be
invalid JSON (``"..._hold_duration:"65535"``) and was therefore never applied.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import MqttMockHAClientGenerator

from homeassistant.core import HomeAssistant

from .test_integration import _published, _setup

SLR1_EXPECTED: dict[str, list[str]] = {
    "boost_default": [
        '{"system_mode":"emergency_heating","temperature_setpoint_hold_duration":120,"temperature_setpoint_hold":1,"occupied_heating_setpoint":25}'
    ],
    "boost_args": [
        '{"system_mode":"emergency_heating","temperature_setpoint_hold_duration":45,"temperature_setpoint_hold":1,"occupied_heating_setpoint":24.5}'
    ],
    "temp": ['{"occupied_heating_setpoint":21.5}'],
    "off": [
        '{"system_mode":"off","temperature_setpoint_hold":"0"}',
        '{"occupied_heating_setpoint":12,"temperature_setpoint_hold":"1","temperature_setpoint_hold_duration":"65535"}',
    ],
    "auto": [
        '{"system_mode":"heat","temperature_setpoint_hold":"0","temperature_setpoint_hold_duration":"0"}'
    ],
    "heat": [
        '{"system_mode":"heat","occupied_heating_setpoint":20.5,"temperature_setpoint_hold":"1","temperature_setpoint_hold_duration":"0"}',
        '{"system_mode":"heat","occupied_heating_setpoint":20.5}',
    ],
    "heat_from_temp": [
        '{"system_mode":"heat","occupied_heating_setpoint":20.5,"temperature_setpoint_hold":"1","temperature_setpoint_hold_duration":"0"}'
    ],
}

SLR2_EXPECTED: dict[str, list[str]] = {
    "boost_default": [
        '{"system_mode_heat":"emergency_heating","temperature_setpoint_hold_duration_heat":120,"temperature_setpoint_hold_heat":1,"occupied_heating_setpoint_heat":25}'
    ],
    "boost_args": [
        '{"system_mode_heat":"emergency_heating","temperature_setpoint_hold_duration_heat":45,"temperature_setpoint_hold_heat":1,"occupied_heating_setpoint_heat":24.5}'
    ],
    "temp": ['{"occupied_heating_setpoint_heat":21.5}'],
    "off": [
        '{"system_mode_heat":"off","temperature_setpoint_hold_heat":"0"}',
        '{"occupied_heating_setpoint_heat":12,"temperature_setpoint_hold_heat":"1","temperature_setpoint_hold_duration_heat":"65535"}',
    ],
    "auto": [
        '{"system_mode_heat":"heat","temperature_setpoint_hold_heat":"0","temperature_setpoint_hold_duration_heat":"0"}'
    ],
    "heat": [
        '{"system_mode_heat":"heat","occupied_heating_setpoint_heat":20.5,"temperature_setpoint_hold_heat":"1","temperature_setpoint_hold_duration_heat":"0"}',
        '{"system_mode_heat":"heat","occupied_heating_setpoint_heat":20.5}',
    ],
    "heat_from_temp": [
        '{"system_mode_heat":"heat","occupied_heating_setpoint_heat":20.5,"temperature_setpoint_hold_heat":"1","temperature_setpoint_hold_duration_heat":"0"}'
    ],
    "wboost_default": [
        '{"system_mode_water":"emergency_heating","temperature_setpoint_hold_duration_water":60,"temperature_setpoint_hold_water":1}'
    ],
    "wboost_args": [
        '{"system_mode_water":"emergency_heating","temperature_setpoint_hold_duration_water":30,"temperature_setpoint_hold_water":1}'
    ],
    "w_sched": [
        '{"system_mode_water":"heat","temperature_setpoint_hold_water":"0","temperature_setpoint_hold_duration_water":"0"}'
    ],
    "w_on": ['{"system_mode_water":"heat","temperature_setpoint_hold_water":1}'],
    "w_off": ['{"system_mode_water":"off","temperature_setpoint_hold_water":0}'],
}


async def _collect(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    entry: MockConfigEntry,
) -> dict[str, list[str]]:
    mqtt_mock = await _setup(hass, mqtt_mock_entry, entry)
    coordinator = entry.runtime_data.coordinator
    calls: dict[str, Any] = {
        "boost_default": coordinator.async_heating_boost(),
        "boost_args": coordinator.async_heating_boost(45, 24.5),
        "temp": coordinator.async_set_temperature(21.5),
        "off": coordinator.async_set_hvac_mode_off(),
        "auto": coordinator.async_set_hvac_mode_auto(),
        "heat": coordinator.async_set_hvac_mode_heat(20.5),
        "heat_from_temp": coordinator.async_set_hvac_mode_heat(20.5, True),  # noqa: FBT003
    }
    if coordinator.model == "SLR2":
        calls |= {
            "wboost_default": coordinator.async_water_boost(),
            "wboost_args": coordinator.async_water_boost(30),
            "w_sched": coordinator.async_water_scheduled(),
            "w_on": coordinator.async_water_always_on(),
            "w_off": coordinator.async_water_always_off(),
        }
    out: dict[str, list[str]] = {}
    for name, coro in calls.items():
        mqtt_mock.async_publish.reset_mock()
        await coro
        out[name] = _published(mqtt_mock)
    return out


async def test_slr1_payloads(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr1_entry: MockConfigEntry,
) -> None:
    """Heating-only receiver payloads are unchanged."""
    assert await _collect(hass, mqtt_mock_entry, slr1_entry) == SLR1_EXPECTED


async def test_slr2_payloads(
    hass: HomeAssistant,
    mqtt_mock_entry: MqttMockHAClientGenerator,
    slr2_entry: MockConfigEntry,
) -> None:
    """Heating + water receiver payloads are unchanged."""
    assert await _collect(hass, mqtt_mock_entry, slr2_entry) == SLR2_EXPECTED


@pytest.mark.parametrize("expected", [SLR1_EXPECTED, SLR2_EXPECTED])
def test_every_expected_payload_is_valid_json(expected: dict[str, list[str]]) -> None:
    """Regression: the OFF payload used to be malformed JSON."""
    for payloads in expected.values():
        for payload in payloads:
            json.loads(payload)
