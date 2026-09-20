"""Test de integración: alta completa de la config entry (con un circuito), creación de sensores
identificados por unique_id (no por entity_id, que depende de traducciones) y descarga limpia."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_ledger.const import (
    ATTR_ENERGY_METHOD,
    CONF_BUY_PRICE_ENTITY,
    CONF_CIRCUIT_ENERGY_ENTITY,
    CONF_CIRCUIT_NAME,
    CONF_CIRCUIT_POWER_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_HOME_LOAD_POWER_ENTITY,
    CONF_POSITIVE_IS_EXPORT,
    CONF_SELL_PRICE_ENTITY,
    DOMAIN,
    ENERGY_METHOD_INTEGRATED,
    ENERGY_METHOD_METER,
    SUBENTRY_TYPE_CIRCUIT,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er


async def test_setup_creates_expected_sensors_and_unloads_cleanly(hass):
    hass.states.async_set("sensor.grid_power", "-1000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.buy_price", "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.sell_price", "0.05", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.termo_power", "1500", {"unit_of_measurement": "W"})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
            CONF_SELL_PRICE_ENTITY: "sensor.sell_price",
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
    assert entry.state is ConfigEntryState.LOADED

    ent_reg = er.async_get(hass)
    circuit_id = next(iter(entry.subentries))

    # Casa: coste + energía + compensación + balance, 4 períodos cada uno.
    for kind in ("cost", "energy", "compensation", "balance"):
        for period in ("day", "week", "month", "year"):
            unique_id = f"{entry.entry_id}_home_{kind}_{period}"
            entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
            assert entity_id is not None, f"falta {unique_id}"
            assert hass.states.get(entity_id) is not None

    # Circuito: coste + energía, 4 períodos cada uno.
    for kind in ("cost", "energy"):
        for period in ("day", "week", "month", "year"):
            unique_id = f"{entry.entry_id}_{circuit_id}_{kind}_{period}"
            entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
            assert entity_id is not None, f"falta {unique_id}"

    # Sin compensación no debería existir un balance/compensación para el circuito.
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{circuit_id}_compensation_day") is None
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{circuit_id}_balance_day") is None

    all_entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    assert len(all_entities) == 16 + 8  # casa: 4 kinds * 4 periodos; circuito: 2 kinds * 4 periodos

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_without_sell_price_skips_compensation_and_balance(hass):
    hass.states.async_set("sensor.grid_power", "-1000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.buy_price", "0.20", {"unit_of_measurement": "EUR/kWh"})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    all_entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    assert len(all_entities) == 8  # coste + energía, sin sell_price_entity


async def test_setup_with_home_load_power_entity_creates_savings_sensors(hass):
    hass.states.async_set("sensor.grid_power", "-1000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.buy_price", "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.home_load", "2000", {"unit_of_measurement": "W"})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
            CONF_HOME_LOAD_POWER_ENTITY: "sensor.home_load",
        },
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    for period in ("day", "week", "month", "year"):
        unique_id = f"{entry.entry_id}_home_savings_{period}"
        entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
        assert entity_id is not None, f"falta {unique_id}"

    all_entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    assert len(all_entities) == 12  # coste + energía + ahorro, sin sell_price_entity


async def test_circuit_with_energy_entity_reports_meter_method(hass):
    hass.states.async_set("sensor.grid_power", "-1000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.buy_price", "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.termo_power", "1500", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.termo_energy", "10.0", {"unit_of_measurement": "kWh"})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
        },
        subentries_data=[
            {
                "data": {
                    CONF_CIRCUIT_NAME: "Termo",
                    CONF_CIRCUIT_POWER_ENTITY: "sensor.termo_power",
                    CONF_CIRCUIT_ENERGY_ENTITY: "sensor.termo_energy",
                },
                "subentry_type": SUBENTRY_TYPE_CIRCUIT,
                "title": "Termo",
                "unique_id": None,
            }
        ],
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    circuit_id = next(iter(entry.subentries))

    circuit_energy_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{circuit_id}_energy_day")
    home_energy_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_home_energy_day")
    assert circuit_energy_id is not None
    assert home_energy_id is not None

    assert hass.states.get(circuit_energy_id).attributes[ATTR_ENERGY_METHOD] == ENERGY_METHOD_METER
    # La casa no tiene grid_import_energy_entity configurado: sigue en potencia integrada.
    assert hass.states.get(home_energy_id).attributes[ATTR_ENERGY_METHOD] == ENERGY_METHOD_INTEGRATED
