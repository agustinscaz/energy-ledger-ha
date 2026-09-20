"""Tests del config flow: alta inicial y subentry de circuito."""

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


def _suggested_value(schema, key: str):
    for marker in schema.schema:
        if str(marker) == key:
            return (marker.description or {}).get("suggested_value")
    return None


# --- Reconfigure (issue #8): editar sin borrar/recrear y perder el acumulado -------------------


async def test_reconfigure_updates_entry_data_without_changing_entry_id(hass):
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
    original_entry_id = entry.entry_id

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    # Pre-poblado con los valores actuales.
    assert _suggested_value(result["data_schema"], CONF_GRID_POWER_ENTITY) == "sensor.grid_power"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
            CONF_SELL_PRICE_ENTITY: "sensor.sell_price",  # agrega compensación, no la tenía
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    reloaded = hass.config_entries.async_get_entry(original_entry_id)
    assert reloaded.entry_id == original_entry_id  # el Store no se mueve, ver issue #8
    assert reloaded.data[CONF_SELL_PRICE_ENTITY] == "sensor.sell_price"


async def test_reconfigure_clears_optional_field_left_empty(hass):
    """Vaciar un campo opcional en el form de reconfigure debe borrarlo de entry.data, no dejar
    el valor viejo pisando (_normalize_user_data reemplaza data entera, no hace un update parcial)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
            CONF_SELL_PRICE_ENTITY: "sensor.sell_price",
        },
        unique_id="sensor.grid_power",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_GRID_POWER_ENTITY: "sensor.grid_power",
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
            # sell_price_entity vacío: no se manda en el input, como si se hubiera borrado del form.
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    reloaded = hass.config_entries.async_get_entry(entry.entry_id)
    assert CONF_SELL_PRICE_ENTITY not in reloaded.data


async def test_reconfigure_grid_power_entity_mismatch_aborts(hass):
    """El unique_id (grid_power_entity) identifica la instalación — cambiarlo en un reconfigure
    debería fallar en vez de "robarle" silenciosamente el unique_id a otra instalación."""
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

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_GRID_POWER_ENTITY: "sensor.otro_grid_power",  # distinto al original
            CONF_POSITIVE_IS_EXPORT: True,
            CONF_BUY_PRICE_ENTITY: "sensor.buy_price",
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"


async def test_reconfigure_circuit_subentry_preserves_subentry_id(hass):
    """El subentry_id no cambia con un reconfigure — su nodo de acumulados en el coordinator
    (identificado por ese id) tampoco se pierde."""
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
    subentry_id = next(iter(entry.subentries))

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert _suggested_value(result["data_schema"], CONF_CIRCUIT_NAME) == "Termo"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_CIRCUIT_NAME: "Termo", CONF_CIRCUIT_POWER_ENTITY: "sensor.termo_power", CONF_CIRCUIT_ENERGY_ENTITY: "sensor.termo_energy"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    reloaded = hass.config_entries.async_get_entry(entry.entry_id)
    assert subentry_id in reloaded.subentries  # mismo id, no se recreó
    assert reloaded.subentries[subentry_id].data[CONF_CIRCUIT_ENERGY_ENTITY] == "sensor.termo_energy"


async def test_reconfigure_circuit_subentry_rejects_empty_name(hass):
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
    subentry_id = next(iter(entry.subentries))

    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_CIRCUIT_NAME: "   ", CONF_CIRCUIT_POWER_ENTITY: "sensor.termo_power"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "name_required"}
