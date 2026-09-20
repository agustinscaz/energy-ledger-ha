"""Punto de entrada de Energy Ledger: crea el coordinator por config entry y lo guarda en
entry.runtime_data (evita el patrón manual hass.data[DOMAIN][entry_id])."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import EnergyLedgerCoordinator
from .services import async_setup_services

PLATFORMS: list[Platform] = [Platform.SENSOR]

# hassfest lo exige para cualquier integración que implemente async_setup (acá: para registrar
# los servicios start_cycle/end_cycle una sola vez a nivel de dominio, ver #13) — esta integración
# solo se configura vía config entries, nunca vía YAML.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Nivel de dominio, no por config entry: los servicios start_cycle/end_cycle (#13) no son
    propios de UNA instalación — target: device resuelve a la instalación que corresponda según
    el dispositivo elegido (ver services.py), así que se registran una sola vez acá."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = EnergyLedgerCoordinator(hass, entry)
    await coordinator.async_setup()
    entry.runtime_data = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_unload()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Cambios en el paso "user" o en los circuitos (añadir/editar/quitar): recargar la entry
    entera es más simple y seguro que intentar mutar el coordinator en caliente."""
    await hass.config_entries.async_reload(entry.entry_id)
