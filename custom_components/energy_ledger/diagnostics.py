"""Diagnósticos de Energy Ledger: config, estado interno del coordinator y estado de cada
entidad trackeada, para depurar "¿por qué este número no cuadra?" sin logs ni acceso a la
instancia real. Nada de lo que se expone acá es sensible: solo entity_id, valores monetarios/kWh
propios de la instalación y estados de HA (ver issue #3)."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CIRCUIT_NAME, CONF_CIRCUIT_POWER_ENTITY, SUBENTRY_TYPE_CIRCUIT
from .coordinator import EnergyLedgerCoordinator


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator: EnergyLedgerCoordinator = entry.runtime_data

    circuits = {
        sub_id: {CONF_CIRCUIT_NAME: sub.data[CONF_CIRCUIT_NAME], CONF_CIRCUIT_POWER_ENTITY: sub.data[CONF_CIRCUIT_POWER_ENTITY]}
        for sub_id, sub in entry.subentries.items()
        if sub.subentry_type == SUBENTRY_TYPE_CIRCUIT
    }

    tracked_entities = {}
    for entity_id in coordinator._tracked_entity_ids():
        state = hass.states.get(entity_id)
        tracked_entities[entity_id] = {
            "state": state.state if state else None,
            "available": state is not None and state.state not in ("unknown", "unavailable"),
        }

    return {
        "config": {**entry.data, "circuits": circuits},
        "coordinator": coordinator.diagnostics_snapshot(),
        "tracked_entities": tracked_entities,
    }
