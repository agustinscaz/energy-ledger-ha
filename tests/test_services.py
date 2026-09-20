"""Test de integración de los servicios start_cycle/end_cycle (#13): alta completa vía config
entry (los dispositivos los crea sensor.py), resolución device_id -> nodo, y el flujo real de
llamada a servicio con SupportsResponse.ONLY."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_ledger.const import (
    CONF_BUY_PRICE_ENTITY,
    CONF_CIRCUIT_NAME,
    CONF_CIRCUIT_POWER_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_POSITIVE_IS_EXPORT,
    DOMAIN,
    SUBENTRY_TYPE_CIRCUIT,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr


async def _setup_entry_with_circuit(hass):
    hass.states.async_set("sensor.grid_power", "-2000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.buy_price", "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.termo_power", "2000", {"unit_of_measurement": "W"})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
        },
        subentries_data=[
            {
                "data": {CONF_CIRCUIT_NAME: "Termo", CONF_CIRCUIT_POWER_ENTITY: "sensor.termo_power"},
                "subentry_type": SUBENTRY_TYPE_CIRCUIT,
                "title": "Termo",
                "unique_id": None,
            }
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _device_for_node(hass, node_id: str):
    device_reg = dr.async_get(hass)
    return device_reg.async_get_device(identifiers={(DOMAIN, node_id)})


async def test_start_and_end_cycle_via_service_call(hass):
    entry = await _setup_entry_with_circuit(hass)
    circuit_id = next(iter(entry.subentries))
    device = _device_for_node(hass, circuit_id)
    assert device is not None

    await hass.services.async_call(DOMAIN, "start_cycle", {"device_id": device.id}, blocking=True)

    hass.states.async_set("sensor.grid_power", "-1000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()

    result = await hass.services.async_call(
        DOMAIN, "end_cycle", {"device_id": device.id}, blocking=True, return_response=True
    )

    assert result["cost"] >= 0
    assert "energy_kwh" in result
    assert result["compensation"] is None  # circuito
    assert result["cost_method"] in ("repartido", "binario_sin_repartir")


async def test_end_cycle_without_start_cycle_raises_service_validation_error(hass):
    entry = await _setup_entry_with_circuit(hass)
    circuit_id = next(iter(entry.subentries))
    device = _device_for_node(hass, circuit_id)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "end_cycle", {"device_id": device.id}, blocking=True, return_response=True
        )


async def test_service_rejects_device_from_another_domain(hass):
    await _setup_entry_with_circuit(hass)
    device_reg = dr.async_get(hass)
    other_entry = MockConfigEntry(domain="other_domain", data={})
    other_entry.add_to_hass(hass)
    other_device = device_reg.async_get_or_create(
        config_entry_id=other_entry.entry_id, identifiers={("other_domain", "algo")}
    )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "start_cycle", {"device_id": other_device.id}, blocking=True
        )


async def test_service_rejects_multiple_devices(hass):
    entry = await _setup_entry_with_circuit(hass)
    circuit_id = next(iter(entry.subentries))
    circuit_device = _device_for_node(hass, circuit_id)
    home_device = _device_for_node(hass, entry.entry_id)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "start_cycle", {"device_id": [circuit_device.id, home_device.id]}, blocking=True
        )


async def test_service_rejects_unknown_device_id(hass):
    await _setup_entry_with_circuit(hass)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "start_cycle", {"device_id": "no_existe"}, blocking=True)
    entry = await _setup_entry_with_circuit(hass)
    device = _device_for_node(hass, entry.entry_id)  # el nodo casa/red usa entry.entry_id
    assert device is not None

    await hass.services.async_call(DOMAIN, "start_cycle", {"device_id": device.id}, blocking=True)
    result = await hass.services.async_call(
        DOMAIN, "end_cycle", {"device_id": device.id}, blocking=True, return_response=True
    )
    assert "cost" in result
