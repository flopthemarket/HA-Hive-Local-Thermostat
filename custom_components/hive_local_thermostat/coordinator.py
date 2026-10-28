"""Coordinator for Hive Local Thermostat integration."""

from __future__ import annotations

import json
from asyncio import sleep
from datetime import datetime
from typing import Any, cast

from homeassistant.components import mqtt
from homeassistant.components.climate.const import (
    PRESET_BOOST,
    PRESET_NONE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.dt import utcnow

from .const import (
    DEFAULT_FROST_TEMPERATURE,
    DEFAULT_HEATING_BOOST_MINUTES,
    DEFAULT_HEATING_BOOST_TEMPERATURE,
    DEFAULT_WATER_BOOST_MINUTES,
    DOMAIN,
    HIVE_BOOST,
    LOGGER,
    MODEL_SLR2,
)

PRESET_MAP = {
    PRESET_NONE: "",
    PRESET_BOOST: HIVE_BOOST,
}

BOOST_ERROR = 65000

# Attributes rebuilt from every MQTT message. They are snapshotted before a
# message is processed so a bad payload can never leave entities blank.
_STATE_FIELDS = (
    "current_temperature",
    "target_temperature",
    "preset_mode",
    "hvac_mode",
    "water_mode",
    "heat_boost",
    "water_boost",
    "running_state_heat",
    "running_state_water",
    "pre_boost_hvac_mode",
    "pre_boost_occupied_heating_setpoint_heat",
    "pre_boost_water_mode",
)


def _payload(**fields: Any) -> str:
    """Build a compact JSON payload (never hand-concatenate JSON)."""
    return json.dumps(fields, separators=(",", ":"))


class HiveCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching Hive data from MQTT."""

    current_temperature: float | None = None
    target_temperature: float | None = None
    preset_mode: str | None = None
    hvac_mode: HVACMode | None = None
    water_mode: str | None = None

    running_state_heat: str = ""
    running_state_water: str = ""

    heat_boost: bool = False
    water_boost: bool = False
    heat_boost_started: datetime | None = None
    water_boost_started: datetime | None = None
    heat_boost_started_duration: int = 0
    water_boost_started_duration: int = 0
    heat_boost_remaining: int = 0
    water_boost_remaining: int = 0

    pre_boost_hvac_mode: HVACMode | None = None
    pre_boost_occupied_heating_setpoint_heat: float | None = None
    pre_boost_water_mode: str | None = None

    # Number entity values
    heating_boost_duration: float = DEFAULT_HEATING_BOOST_MINUTES
    heating_boost_temperature: float = DEFAULT_HEATING_BOOST_TEMPERATURE
    heating_frost_prevention: float = DEFAULT_FROST_TEMPERATURE
    water_boost_duration: float = DEFAULT_WATER_BOOST_MINUTES

    # Diagnostics
    last_mqtt_payload: dict[str, Any] | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        model: str,
        topic: str,
        show_heat_schedule_mode: bool,  # noqa: FBT001
        show_water_schedule_mode: bool,  # noqa: FBT001
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{config_entry.entry_id}",
        )
        self.entry_id = config_entry.entry_id
        self.model = model
        self.topic = topic
        self.show_heating_schedule_mode = show_heat_schedule_mode
        self.show_water_schedule_mode = show_water_schedule_mode
        self.data: dict[str, Any] = {}

    @property
    def topic_get(self) -> str:
        """Return the topic getter."""
        return self.topic + "/get"

    @property
    def topic_set(self) -> str:
        """Return the topic setter."""
        return self.topic + "/set"

    @property
    def boost_remaining_heat(self) -> int:
        """Return the remaining boost time for heating."""
        return self.heat_boost_remaining

    @property
    def boost_remaining_water(self) -> int:
        """Return the remaining boost time for water."""
        return self.water_boost_remaining

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current HVAC action."""
        if self.running_state_heat == "preheating":
            return HVACAction.PREHEATING
        if self.running_state_heat == "heat":
            return HVACAction.HEATING
        if self.running_state_heat == "idle":
            return HVACAction.IDLE
        if self.running_state_heat == "off":
            return HVACAction.OFF
        return None

    @property
    def local_temperature_heat(self) -> float | None:
        """Return the local temperature for heating."""
        return self.current_temperature

    def climate_preset(self, mode: str) -> str:
        """Get the current preset."""
        return next((k for k, v in PRESET_MAP.items() if v == mode), PRESET_NONE)

    @callback
    def handle_mqtt_message(self, message: ReceiveMessage) -> None:  # noqa: C901, PLR0912, PLR0915
        """Handle received MQTT message."""
        topic = message.topic
        payload = message.payload
        LOGGER.debug("Received from %s payload: %s", topic, payload)

        if not payload:
            LOGGER.error(
                "Received empty payload on topic %s, check that you have the correct topic name",
                topic,
            )
            return

        previous = {field: getattr(self, field) for field in _STATE_FIELDS}

        self.current_temperature = None
        self.target_temperature = None
        self.preset_mode = None
        self.hvac_mode = None
        self.heat_boost = False
        self.water_boost = False
        self.water_mode = None

        try:
            parsed_data = json.loads(payload)

            if not isinstance(parsed_data, dict):
                LOGGER.error(
                    "Ignoring MQTT payload on %s, expected a JSON object: %s",
                    topic,
                    payload,
                )
                self._restore_state(previous)
                return

            # Store last payload for diagnostics
            self.last_mqtt_payload = parsed_data

            if not self.valid_data_for_model(parsed_data):
                self._restore_state(previous)
                return

            if self.model == MODEL_SLR2:
                reported_boost_remaining_heat = cast(
                    int,
                    parsed_data["temperature_setpoint_hold_duration_heat"]
                    if parsed_data["system_mode_heat"] == "emergency_heating"
                    else 0,
                )
                reported_boost_remaining_water = cast(
                    int,
                    parsed_data["temperature_setpoint_hold_duration_water"]
                    if parsed_data["system_mode_water"] == "emergency_heating"
                    else 0,
                )
                reported_boost_temperature = parsed_data[
                    "occupied_heating_setpoint_heat"
                ]
                self.running_state_heat = cast(
                    str,
                    parsed_data.get("running_state_heat")
                    if parsed_data.get("running_state_heat")
                    else "preheating",
                )
                self.running_state_water = cast(
                    str,
                    parsed_data.get("running_state_water")
                    if parsed_data.get("running_state_water")
                    else "preheating",
                )
                self.current_temperature = parsed_data["local_temperature_heat"]
                if parsed_data["occupied_heating_setpoint_heat"] == 1:
                    self.target_temperature = self.heating_frost_prevention
                else:
                    self.target_temperature = parsed_data[
                        "occupied_heating_setpoint_heat"
                    ]
                self.preset_mode = self.climate_preset(parsed_data["system_mode_heat"])
                if parsed_data["system_mode_heat"] == "heat":
                    if (
                        parsed_data["temperature_setpoint_hold_heat"] is False
                        and self.show_heating_schedule_mode
                    ):
                        self.hvac_mode = HVACMode.AUTO
                    else:
                        self.hvac_mode = HVACMode.HEAT
                if parsed_data["system_mode_heat"] == "emergency_heating":
                    self.hvac_mode = HVACMode.HEAT
                    self.heat_boost = True
                if parsed_data["system_mode_heat"] == "off":
                    self.hvac_mode = HVACMode.OFF

                if parsed_data["system_mode_heat"] != "emergency_heating":
                    self.pre_boost_occupied_heating_setpoint_heat = (
                        self.target_temperature
                    )
                    self.pre_boost_hvac_mode = self.hvac_mode
                if parsed_data["system_mode_water"] == "heat":
                    if parsed_data["temperature_setpoint_hold_water"] is False:
                        if self.show_water_schedule_mode:
                            self.water_mode = "auto"
                        else:
                            self.water_mode = "heat"
                    else:
                        self.water_mode = "heat"
                if parsed_data["system_mode_water"] == "emergency_heating":
                    self.water_mode = "boost"
                    self.water_boost = True
                if parsed_data["system_mode_water"] == "off":
                    self.water_mode = "off"

                if parsed_data["system_mode_water"] != "emergency_heating":
                    self.pre_boost_water_mode = self.water_mode
            else:
                reported_boost_remaining_heat = cast(
                    int,
                    parsed_data["temperature_setpoint_hold_duration"]
                    if parsed_data["system_mode"] == "emergency_heating"
                    else 0,
                )
                reported_boost_temperature = parsed_data["occupied_heating_setpoint"]
                self.running_state_heat = cast(
                    str,
                    parsed_data.get("running_state")
                    if parsed_data.get("running_state")
                    else "preheating",
                )
                self.current_temperature = parsed_data["local_temperature"]
                if parsed_data["occupied_heating_setpoint"] == 1:
                    self.target_temperature = self.heating_frost_prevention
                else:
                    self.target_temperature = parsed_data["occupied_heating_setpoint"]
                self.preset_mode = self.climate_preset(parsed_data["system_mode"])

                if parsed_data["system_mode"] == "heat":
                    if (
                        parsed_data["temperature_setpoint_hold"] is False
                        and self.show_heating_schedule_mode
                    ):
                        self.hvac_mode = HVACMode.AUTO
                    else:
                        self.hvac_mode = HVACMode.HEAT
                if parsed_data["system_mode"] == "emergency_heating":
                    self.hvac_mode = HVACMode.HEAT
                    self.heat_boost = True
                if parsed_data["system_mode"] == "off":
                    self.hvac_mode = HVACMode.OFF

                if parsed_data["system_mode"] != "emergency_heating":
                    self.pre_boost_occupied_heating_setpoint_heat = (
                        self.target_temperature
                    )
                    self.pre_boost_hvac_mode = self.hvac_mode

            if self.correct_heat_boost(
                reported_boost_remaining_heat, reported_boost_temperature
            ):
                return  # Correction made, exit to avoid state update loop
            self.record_heat_boost_state()

            if self.model == MODEL_SLR2:
                if self.correct_water_boost(reported_boost_remaining_water):
                    return  # Correction made, exit to avoid state update loop
                self.record_water_boost_state()

            self.async_set_updated_data(parsed_data)
        except json.JSONDecodeError:
            self._restore_state(previous)
            LOGGER.error("Failed to parse JSON from MQTT payload: %s", payload)
        except KeyError as err:
            self._restore_state(previous)
            LOGGER.error(
                "MQTT payload on %s is missing expected field %s, "
                "keeping previous state (check the model and topic are correct)",
                topic,
                err,
            )
        except Exception as err:  # noqa: BLE001
            self._restore_state(previous)
            LOGGER.error("Error handling MQTT message: %r", err)

    def _restore_state(self, previous: dict[str, Any]) -> None:
        """Restore state captured before a message that could not be processed."""
        for field, value in previous.items():
            setattr(self, field, value)

    def valid_data_for_model(self, data: dict[str, Any]) -> bool:
        """Check if data is valid for the current model."""
        if self.model == MODEL_SLR2:
            if "system_mode_water" not in data:
                LOGGER.error(
                    "Received data does not contain 'system_mode_water' for SLR2, check you have the correct model set"
                )
                return False
        elif "system_mode_water" in data:
            LOGGER.error(
                "Received data contains 'system_mode_water' for SLR1/OTR1, check you have the correct model set"
            )
            return False
        return True

    def correct_heat_boost(
        self, reported_boost_remaining_heat: int, reported_boost_temperature: float
    ) -> bool:
        """Check and correct boost remaining heat if necessary.

        Returns True when a corrective boost command was sent (the caller should
        then wait for the device to report back instead of publishing state).
        """
        if reported_boost_remaining_heat > BOOST_ERROR:
            # Calculate remaining boost time based on when it started
            remaining = 0
            if self.heat_boost_started and self.heat_boost_started_duration > 0:
                elapsed = (utcnow() - self.heat_boost_started).total_seconds() / 60
                remaining = int(self.heat_boost_started_duration - elapsed)
            self.heat_boost_remaining = max(remaining, 0)

            LOGGER.warning(
                "Correcting reported boost remaining heat from %d to %d",
                reported_boost_remaining_heat,
                self.heat_boost_remaining,
            )
            if remaining > 0 and self.config_entry is not None:
                self.config_entry.async_create_task(
                    self.hass,
                    self.async_heating_boost(remaining, reported_boost_temperature),
                )
                return True
            # Nothing left to re-send. Never start a fresh default-length boost
            # from a bogus reading; the device returns to its previous mode itself.
            return False
        self.heat_boost_remaining = reported_boost_remaining_heat
        return False

    def correct_water_boost(self, reported_boost_remaining_water: int) -> bool:
        """Check and correct boost remaining water if necessary."""
        if reported_boost_remaining_water > BOOST_ERROR:
            # Calculate remaining boost time based on when it started
            remaining = 0
            if self.water_boost_started and self.water_boost_started_duration > 0:
                elapsed = (utcnow() - self.water_boost_started).total_seconds() / 60
                remaining = int(self.water_boost_started_duration - elapsed)
            self.water_boost_remaining = max(remaining, 0)

            LOGGER.warning(
                "Correcting reported boost remaining water from %d to %d",
                reported_boost_remaining_water,
                self.water_boost_remaining,
            )
            if remaining > 0 and self.config_entry is not None:
                self.config_entry.async_create_task(
                    self.hass, self.async_water_boost(remaining)
                )
                return True
            return False
        self.water_boost_remaining = reported_boost_remaining_water
        return False

    def record_heat_boost_state(self) -> None:
        """Record and track boost state for heating."""
        if self.heat_boost and self.heat_boost_remaining > 0:
            # Boost is active, record the start time if not already recorded
            if not self.heat_boost_started:
                self.heat_boost_started = utcnow()
                self.heat_boost_started_duration = self.heat_boost_remaining
        elif not self.heat_boost:
            # Boost is not active, clear tracking state
            self.heat_boost_started = None
            self.heat_boost_started_duration = 0

        if self.water_boost and self.water_boost_remaining > 0:
            # Water boost is active, record the start time if not already recorded
            if not self.water_boost_started:
                self.water_boost_started = utcnow()
                self.water_boost_started_duration = self.water_boost_remaining
        elif not self.water_boost:
            # Water boost is not active, clear tracking state
            self.water_boost_started = None
            self.water_boost_started_duration = 0

    def record_water_boost_state(self) -> None:
        """Record and track boost state for water."""
        if self.water_boost and self.water_boost_remaining > 0:
            # Water boost is active, record the start time if not already recorded
            if not self.water_boost_started:
                self.water_boost_started = utcnow()
                self.water_boost_started_duration = self.water_boost_remaining
        elif not self.water_boost:
            # Water boost is not active, clear tracking state
            self.water_boost_started = None
            self.water_boost_started_duration = 0

    def _heat_payload(self, **fields: Any) -> str:
        """Build a heating payload, using the *_heat keys on two-channel models."""
        if self.model == MODEL_SLR2:
            fields = {f"{key}_heat": value for key, value in fields.items()}
        return _payload(**fields)

    async def _async_publish_set(self, payload: str) -> None:
        """Publish MQTT set message."""
        LOGGER.debug("Sending to %s message %s", self.topic_set, payload)
        await mqtt.async_publish(self.hass, self.topic_set, payload)

    async def async_water_boost(
        self, boost_duration_minutes: int | None = None
    ) -> None:
        """Send water boost command."""

        self.pre_boost_water_mode = self.water_mode

        duration = int(boost_duration_minutes or self.water_boost_duration)
        payload = _payload(
            system_mode_water="emergency_heating",
            temperature_setpoint_hold_duration_water=duration,
            temperature_setpoint_hold_water=1,
        )

        self.water_boost = True
        self.water_boost_started = utcnow()
        self.water_boost_started_duration = duration

        await self._async_publish_set(payload)

    async def async_water_boost_cancel(self) -> None:
        """Cancel water boost command."""

        if self.pre_boost_water_mode == "auto":
            await self.async_water_scheduled()
        elif self.pre_boost_water_mode == "heat":
            await self.async_water_always_on()
        else:
            await self.async_water_always_off()

    async def async_water_scheduled(self) -> None:
        """Send water scheduled command."""

        await self._async_publish_set(
            _payload(
                system_mode_water="heat",
                temperature_setpoint_hold_water="0",
                temperature_setpoint_hold_duration_water="0",
            )
        )

    async def async_water_always_on(self) -> None:
        """Send water always on command."""

        await self._async_publish_set(
            _payload(system_mode_water="heat", temperature_setpoint_hold_water=1)
        )

    async def async_water_always_off(self) -> None:
        """Send water always off command."""

        await self._async_publish_set(
            _payload(system_mode_water="off", temperature_setpoint_hold_water=0)
        )

    async def async_heating_boost(
        self,
        boost_duration_minutes: int | None = None,
        boost_temperature: float | None = None,
    ) -> None:
        """Send heating boost command."""

        self.pre_boost_occupied_heating_setpoint_heat = self.target_temperature
        self.pre_boost_hvac_mode = self.hvac_mode

        duration = int(boost_duration_minutes or self.heating_boost_duration)
        temperature = boost_temperature or self.heating_boost_temperature

        payload = self._heat_payload(
            system_mode="emergency_heating",
            temperature_setpoint_hold_duration=duration,
            temperature_setpoint_hold=1,
            occupied_heating_setpoint=temperature,
        )

        self.heat_boost = True
        self.heat_boost_started = utcnow()
        self.heat_boost_started_duration = duration

        await self._async_publish_set(payload)

    async def async_heating_boost_cancel(self) -> None:
        """Cancel heating boost command."""

        if self.pre_boost_hvac_mode == HVACMode.AUTO:
            await self.async_set_hvac_mode_auto()
        elif self.pre_boost_hvac_mode == HVACMode.HEAT:
            if self.pre_boost_occupied_heating_setpoint_heat is not None:
                await self.async_set_hvac_mode_heat(
                    self.pre_boost_occupied_heating_setpoint_heat
                )
            else:
                await self.async_set_hvac_mode_heat(self.heating_frost_prevention)
        else:
            await self.async_set_hvac_mode_off()

    async def async_set_temperature(self, temperature: float) -> None:
        """Set temperature."""

        await self._async_publish_set(
            self._heat_payload(occupied_heating_setpoint=temperature)
        )

    async def async_set_hvac_mode_off(self) -> None:
        """Set HVAC mode to off."""

        self.hvac_mode = HVACMode.OFF
        await self._async_publish_set(
            self._heat_payload(system_mode="off", temperature_setpoint_hold="0")
        )

        await sleep(0.5)

        # Hold the frost protection temperature while off
        await self._async_publish_set(
            self._heat_payload(
                occupied_heating_setpoint=self.heating_frost_prevention,
                temperature_setpoint_hold="1",
                temperature_setpoint_hold_duration="65535",
            )
        )

    async def async_set_hvac_mode_auto(self) -> None:
        """Set HVAC mode to auto."""

        self.hvac_mode = HVACMode.AUTO
        await self._async_publish_set(
            self._heat_payload(
                system_mode="heat",
                temperature_setpoint_hold="0",
                temperature_setpoint_hold_duration="0",
            )
        )

    async def async_set_hvac_mode_heat(
        self,
        temperature: float,
        set_from_temperature: bool = False,  # noqa: FBT001, FBT002
    ) -> None:
        """Set HVAC mode to heat."""

        payload = self._heat_payload(
            system_mode="heat",
            occupied_heating_setpoint=temperature,
            temperature_setpoint_hold="1",
            temperature_setpoint_hold_duration="0",
        )
        payload_heating_setpoint = self._heat_payload(
            system_mode="heat", occupied_heating_setpoint=temperature
        )

        self.hvac_mode = HVACMode.HEAT
        await self._async_publish_set(payload)

        if not set_from_temperature:
            await sleep(0.5)
            await self._async_publish_set(payload_heating_setpoint)
