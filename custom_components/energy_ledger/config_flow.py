"""Config flow de Energy Ledger: un paso "user" por casa/instalación + subentries repetibles de
tipo "circuit" para trackear electrodomésticos individuales además del total de la casa."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BUY_PRICE_ENTITY,
    CONF_CIRCUIT_ENERGY_ENTITY,
    CONF_CIRCUIT_NAME,
    CONF_CIRCUIT_POWER_ENTITY,
    CONF_GRID_IMPORT_ENERGY_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_HOME_LOAD_POWER_ENTITY,
    CONF_POSITIVE_IS_EXPORT,
    CONF_SELL_PRICE_ENTITY,
    DEFAULT_POSITIVE_IS_EXPORT,
    DOMAIN,
    SUBENTRY_TYPE_CIRCUIT,
)

_SENSOR_SELECTOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GRID_POWER_ENTITY): _SENSOR_SELECTOR,
        vol.Required(CONF_POSITIVE_IS_EXPORT, default=DEFAULT_POSITIVE_IS_EXPORT): bool,
        vol.Required(CONF_BUY_PRICE_ENTITY): _SENSOR_SELECTOR,
        vol.Optional(CONF_SELL_PRICE_ENTITY): _SENSOR_SELECTOR,
        vol.Optional(CONF_HOME_LOAD_POWER_ENTITY): _SENSOR_SELECTOR,
        vol.Optional(CONF_GRID_IMPORT_ENERGY_ENTITY): _SENSOR_SELECTOR,
    }
)


def _normalize_user_data(user_input: dict[str, Any]) -> dict[str, Any]:
    """Los campos opcionales (sell_price_entity, home_load_power_entity,
    grid_import_energy_entity) se guardan solo si vienen con valor — un campo opcional vacío
    en el form NO debe quedar como clave vacía en entry.data, y en reconfigure además debe
    BORRAR el valor viejo si el usuario lo vació (por eso data = dict(user_input) completo,
    nunca un update parcial que dejaría la clave vieja pisando)."""
    data = dict(user_input)
    for key in (CONF_SELL_PRICE_ENTITY, CONF_HOME_LOAD_POWER_ENTITY, CONF_GRID_IMPORT_ENERGY_ENTITY):
        if not data.get(key):
            data.pop(key, None)
    return data


class EnergyLedgerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Un solo paso: sensor de potencia de red, convención de signo y precios de compra/venta."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_GRID_POWER_ENTITY])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title="Energy Ledger", data=_normalize_user_data(user_input))

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Editar grid/precios/ahorro/contador de una instalación ya dada de alta sin
        borrarla y recrearla — eso perdería el acumulado del período en curso, que vive en el
        Store bajo entry.entry_id (sin cambios con un reconfigure, ver issue #8)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_GRID_POWER_ENTITY])
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(entry, data=_normalize_user_data(user_input))

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, entry.data),
            errors=errors,
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_CIRCUIT: CircuitSubentryFlowHandler}


STEP_CIRCUIT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CIRCUIT_NAME): str,
        vol.Required(CONF_CIRCUIT_POWER_ENTITY): _SENSOR_SELECTOR,
        vol.Optional(CONF_CIRCUIT_ENERGY_ENTITY): _SENSOR_SELECTOR,
    }
)


def _build_circuit_data(user_input: dict[str, Any], name: str) -> dict[str, Any]:
    data = {CONF_CIRCUIT_NAME: name, CONF_CIRCUIT_POWER_ENTITY: user_input[CONF_CIRCUIT_POWER_ENTITY]}
    if user_input.get(CONF_CIRCUIT_ENERGY_ENTITY):
        data[CONF_CIRCUIT_ENERGY_ENTITY] = user_input[CONF_CIRCUIT_ENERGY_ENTITY]
    return data


class CircuitSubentryFlowHandler(ConfigSubentryFlow):
    """Añade un circuito (electrodoméstico) con su propio sensor de potencia en W."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_CIRCUIT_NAME].strip()
            if not name:
                errors["base"] = "name_required"
            else:
                return self.async_create_entry(title=name, data=_build_circuit_data(user_input, name))

        return self.async_show_form(step_id="user", data_schema=STEP_CIRCUIT_SCHEMA, errors=errors)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Editar un circuito ya creado (ej. agregarle energy_entity) sin borrarlo y recrearlo
        a mano — ver issue #8. El subentry_id no cambia, así que su nodo en el coordinator
        (acumulados de coste/energía) tampoco se pierde."""
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_CIRCUIT_NAME].strip()
            if not name:
                errors["base"] = "name_required"
            else:
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=name, data=_build_circuit_data(user_input, name)
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(STEP_CIRCUIT_SCHEMA, subentry.data),
            errors=errors,
        )
