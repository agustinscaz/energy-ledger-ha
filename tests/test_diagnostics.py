"""Test de diagnostics.py: alta completa de la config entry (con un circuito) y verificación de
la forma del dict que devuelve async_get_config_entry_diagnostics (ver issue #3)."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_ledger.const import (
    CONF_BUY_PRICE_ENTITY,
    CONF_CIRCUIT_ENERGY_ENTITY,
    CONF_CIRCUIT_NAME,
    CONF_CIRCUIT_POWER_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_POSITIVE_IS_EXPORT,
    CONF_SELL_PRICE_ENTITY,
    DOMAIN,
    SUBENTRY_TYPE_CIRCUIT,
)
from custom_components.energy_ledger.diagnostics import async_get_config_entry_diagnostics


async def test_diagnostics_reports_config_coordinator_and_tracked_entities(hass):
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

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["config"][CONF_GRID_POWER_ENTITY] == "sensor.grid_power"
    circuit_id = next(iter(entry.subentries))
    assert circuit_id in diagnostics["config"]["circuits"]

    assert "home" in diagnostics["coordinator"]["nodes"]
    assert circuit_id in diagnostics["coordinator"]["nodes"]
    assert diagnostics["coordinator"]["last_update"] is not None
    assert diagnostics["coordinator"]["data_gap_since"] is None

    assert diagnostics["tracked_entities"]["sensor.grid_power"]["state"] == "-1000"
    assert diagnostics["tracked_entities"]["sensor.grid_power"]["available"] is True


async def test_diagnostics_marks_unavailable_grid_entity(hass):
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

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["tracked_entities"]["sensor.grid_power"]["state"] is None
    assert diagnostics["tracked_entities"]["sensor.grid_power"]["available"] is False


async def test_diagnostics_includes_circuit_energy_entity_when_configured(hass):
    """#16: faltaba desde que se agregó energy_entity en #7 — sin esto, solo se podía saber qué
    circuitos tienen contador real cruzando tracked_entities contra el atributo metodo."""
    hass.states.async_set("sensor.grid_power", "-1000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.buy_price", "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.termo_power", "1000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.termo_energy", "5.0", {"unit_of_measurement": "kWh"})

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
    circuit_id = next(iter(entry.subentries))

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["config"]["circuits"][circuit_id][CONF_CIRCUIT_ENERGY_ENTITY] == "sensor.termo_energy"


async def test_diagnostics_omits_circuit_energy_entity_when_not_configured(hass):
    hass.states.async_set("sensor.grid_power", "-1000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.buy_price", "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.termo_power", "1000", {"unit_of_measurement": "W"})

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
    circuit_id = next(iter(entry.subentries))

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert CONF_CIRCUIT_ENERGY_ENTITY not in diagnostics["config"]["circuits"][circuit_id]
