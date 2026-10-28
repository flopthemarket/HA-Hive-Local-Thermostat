"""Custom integration to integrate hive_local_thermostat with Home Assistant.

For more details about this integration, please refer to
https://github.com/andrew-codechimp/HA_Hive_Local_Thermostat
"""

from __future__ import annotations

from asyncio import sleep

from awesomeversion.awesomeversion import AwesomeVersion

from homeassistant.components import mqtt
from homeassistant.const import (
    Platform,
    __version__ as HA_VERSION,  # noqa: N812
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .common import HiveConfigEntry, HiveData
from .const import (
    CONF_MODEL,
    CONF_MQTT_TOPIC,
    CONF_SHOW_HEAT_SCHEDULE_MODE,
    CONF_SHOW_WATER_SCHEDULE_MODE,
    DOMAIN,
    LOGGER,
    MIN_HA_VERSION,
    MODEL_SLR2,
)
from .coordinator import HiveCoordinator
from .services import async_setup_services

PLATFORMS_SLR1: list[Platform] = [
    Platform.SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
]

PLATFORMS_SLR2: list[Platform] = [
    Platform.SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def get_platforms(model: str) -> list[Platform]:
    """Return platforms for model."""
    return PLATFORMS_SLR2 if model == MODEL_SLR2 else PLATFORMS_SLR1


async def async_setup(
    hass: HomeAssistant,
    config: ConfigType,  # noqa: ARG001
) -> bool:
    """Integration setup."""

    if AwesomeVersion(HA_VERSION) < AwesomeVersion(MIN_HA_VERSION):  # pragma: no cover
        msg = (
            "This integration requires at least Home Assistant version "
            f" {MIN_HA_VERSION}, you are running version {HA_VERSION}."
            " Please upgrade Home Assistant to continue using this integration."
        )
        LOGGER.critical(msg)
        return False

    async_setup_services(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: HiveConfigEntry) -> bool:
    """Set up this integration using UI."""

    if not await mqtt.async_wait_for_mqtt_client(hass):
        msg = "The MQTT integration is not available"
        raise ConfigEntryNotReady(msg)

    coordinator = HiveCoordinator(
        hass,
        entry,
        entry.options[CONF_MODEL],
        entry.options[CONF_MQTT_TOPIC],
        entry.options.get(CONF_SHOW_HEAT_SCHEDULE_MODE, True),
        entry.options.get(CONF_SHOW_WATER_SCHEDULE_MODE, True),
    )

    platforms = get_platforms(coordinator.model)

    entry.runtime_data = HiveData(
        platforms=platforms,
        coordinator=coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, platforms)

    LOGGER.debug(
        "Subscribing to MQTT topic: %s, will parse platforms for %s",
        coordinator.topic,
        coordinator.model,
    )

    # Subscribe to MQTT and have the coordinator handle messages
    entry.async_on_unload(
        await mqtt.async_subscribe(
            hass, coordinator.topic, coordinator.handle_mqtt_message, 1
        )
    )

    # Ask the receiver for its current state. Done in the background (after
    # giving the subscription a moment to be established) so setup isn't blocked,
    # and a broker hiccup here doesn't fail the whole entry.
    async def _async_request_state() -> None:
        await sleep(2)
        payload = r'{"system_mode":""}'
        LOGGER.debug("Sending to %s message %s", coordinator.topic_get, payload)
        try:
            await mqtt.async_publish(hass, coordinator.topic_get, payload)
        except HomeAssistantError as err:
            LOGGER.warning(
                "Could not request initial state from %s: %s",
                coordinator.topic_get,
                err,
            )

    entry.async_create_background_task(
        hass, _async_request_state(), name=f"{DOMAIN} request initial state"
    )

    entry.async_on_unload(entry.add_update_listener(config_entry_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: HiveConfigEntry) -> bool:
    """Handle removal of an entry."""
    return await hass.config_entries.async_unload_platforms(
        entry, entry.runtime_data.platforms
    )


async def config_entry_update_listener(
    hass: HomeAssistant, entry: HiveConfigEntry
) -> None:
    """Update listener, called when the config entry options are changed."""

    await hass.config_entries.async_reload(entry.entry_id)
