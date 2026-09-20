"""Punto de entrada de Energy Ledger: crea el coordinator por config entry y lo guarda en
entry.runtime_data (evita el patrón manual hass.data[DOMAIN][entry_id])."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import EnergyLedgerCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


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
        entry.runtime_data.async_unload()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Cambios en el paso "user" o en los circuitos (añadir/editar/quitar): recargar la entry
    entera es más simple y seguro que intentar mutar el coordinator en caliente."""
    await hass.config_entries.async_reload(entry.entry_id)
