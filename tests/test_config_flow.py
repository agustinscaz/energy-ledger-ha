"""Tests del config flow: alta inicial y subentry de circuito."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_ledger.const import (
    CONF_BUY_PRICE_ENTITY,
    CONF_CIRCUIT_NAME,
    CONF_CIRCUIT_POWER_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_POSITIVE_IS_EXPORT,
    CONF_SELL_PRICE_ENTITY,
    DOMAIN,
    SUBENTRY_TYPE_CIRCUIT,
)
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType


async def test_user_step_creates_entry_without_sell_price(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Energy Ledger"
    assert CONF_SELL_PRICE_ENTITY not in result["data"]


async def test_user_step_keeps_sell_price_when_given(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
            CONF_SELL_PRICE_ENTITY: "sensor.sell_price",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SELL_PRICE_ENTITY] == "sensor.sell_price"


async def test_duplicate_grid_power_entity_aborts(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
        },
        unique_id="sensor.grid_power",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_add_circuit_subentry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_CIRCUIT), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_CIRCUIT_NAME: "Termo", CONF_CIRCUIT_POWER_ENTITY: "sensor.termo_power"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Termo"

    reloaded = hass.config_entries.async_get_entry(entry.entry_id)
    assert len(reloaded.subentries) == 1
    subentry = next(iter(reloaded.subentries.values()))
    assert subentry.subentry_type == SUBENTRY_TYPE_CIRCUIT
    assert subentry.data[CONF_CIRCUIT_NAME] == "Termo"
    assert subentry.data[CONF_CIRCUIT_POWER_ENTITY] == "sensor.termo_power"


async def test_add_circuit_subentry_rejects_empty_name(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_CIRCUIT), context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_CIRCUIT_NAME: "   ", CONF_CIRCUIT_POWER_ENTITY: "sensor.termo_power"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "name_required"}
