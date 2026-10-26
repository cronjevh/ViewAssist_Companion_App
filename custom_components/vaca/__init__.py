"""The Wyoming integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.wyoming import (
    DomainDataItem,
    WyomingService,
    async_register_websocket_api,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .client import AsyncTcpClient
from .const import ATTR_SPEAKER, DOMAIN
from .custom import CustomEvent
from .devices import VASatelliteDevice

try:
    # HA 2026.10+ refreshes Wyoming service info through a coordinator
    from homeassistant.components.wyoming.coordinator import WyomingInfoCoordinator
except ImportError:  # HA <= 2026.7
    WyomingInfoCoordinator = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

SATELLITE_PLATFORMS = [
    Platform.ASSIST_SATELLITE,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SENSOR,
]

__all__ = [
    "ATTR_SPEAKER",
    "DOMAIN",
    "async_setup",
    "async_setup_entry",
    "async_unload_entry",
]


class WyomingError(HomeAssistantError):
    """Base class for Wyoming errors."""


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Wyoming integration."""
    async_register_websocket_api(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load Wyoming."""
    service = await WyomingService.create(entry.data["host"], entry.data["port"])

    if service is None:
        raise ConfigEntryNotReady("Unable to connect")

    # HA 2026.10 made the coordinator a required part of DomainDataItem.
    # Build it when the running core exposes one, so the same file loads on
    # both sides of that change (a core rollback must not break the fork).
    coordinator = None
    if WyomingInfoCoordinator is not None:
        coordinator = WyomingInfoCoordinator(
            hass, entry, entry.data["host"], entry.data["port"]
        )
        # Core polls the service every 30s by default. The VACA app treats every
        # new TCP connection as a client and drops audio streaming while one is
        # open, so a 30s poll mid-turn loses the microphone audio and cloud STT
        # reports "400 No audio data received". Service info is pushed to us over
        # the long-lived connection anyway, so switch the periodic refresh off.
        coordinator.update_interval = None

        @callback
        def _async_update_service_info() -> None:
            """Keep the service info in sync with the coordinator."""
            service.info = coordinator.data

        entry.async_on_unload(coordinator.async_add_listener(_async_update_service_info))
        coordinator.async_set_updated_data(service.info)

        item = DomainDataItem(service=service, coordinator=coordinator)
    else:
        item = DomainDataItem(service=service)

    entry.runtime_data = item
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = item

    await hass.config_entries.async_forward_entry_setups(entry, service.platforms)
    entry.async_on_unload(entry.add_update_listener(update_listener))

    if (satellite_info := service.info.satellite) is not None:
        # Create satellite device
        dev_reg = dr.async_get(hass)

        # Use config entry id since only one satellite per entry is supported
        satellite_id = entry.entry_id
        device = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, satellite_id)},
            name=satellite_info.name,
            suggested_area=satellite_info.area,
        )

        item.device = VASatelliteDevice(
            satellite_id=satellite_id,
            device_id=device.id,
        )
        item.device.capabilities = await get_device_capabilities(item)

        # Set up satellite entity, sensors, switches, etc.
        await hass.config_entries.async_forward_entry_setups(entry, SATELLITE_PLATFORMS)

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Wyoming."""
    item: DomainDataItem = hass.data[DOMAIN][entry.entry_id]

    platforms = list(item.service.platforms)
    if item.device is not None:
        platforms += SATELLITE_PLATFORMS

    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unload_ok:
        del hass.data[DOMAIN][entry.entry_id]

    return unload_ok


async def get_device_capabilities(item: DomainDataItem):
    """Get device capabilities."""
    capabilities: dict[str, Any] | None = None

    for _ in range(4):
        try:
            async with (
                AsyncTcpClient(item.service.host, item.service.port) as client,
                asyncio.timeout(1),
            ):
                # Describe -> Info
                await client.write_event(CustomEvent("capabilities").event())
                while True:
                    event = await client.read_event()
                    if event is None:
                        raise WyomingError(  # noqa: TRY301
                            "Connection closed unexpectedly",
                        )

                    if CustomEvent.is_type(event.type) and (
                        event_data := CustomEvent.from_event(event).event_data
                    ):
                        capabilities = event_data.get("capabilities")
                        break  # while

                if capabilities is not None:
                    break  # for
        except (TimeoutError, OSError, WyomingError) as ex:
            _LOGGER.warning(
                "Error getting device capabilities: %s, %s", ex, capabilities
            )
            # Sleep and try again
            await asyncio.sleep(2)

    return capabilities
