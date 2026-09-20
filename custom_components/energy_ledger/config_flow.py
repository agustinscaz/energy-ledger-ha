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
    CONF_CIRCUIT_NAME,
    CONF_CIRCUIT_POWER_ENTITY,
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
    }
)


class EnergyLedgerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Un solo paso: sensor de potencia de red, convención de signo y precios de compra/venta."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_GRID_POWER_ENTITY])
            self._abort_if_unique_id_configured()

            data = dict(user_input)
            if not data.get(CONF_SELL_PRICE_ENTITY):
                data.pop(CONF_SELL_PRICE_ENTITY, None)
            if not data.get(CONF_HOME_LOAD_POWER_ENTITY):
                data.pop(CONF_HOME_LOAD_POWER_ENTITY, None)

            return self.async_create_entry(title="Energy Ledger", data=data)

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_CIRCUIT: CircuitSubentryFlowHandler}


STEP_CIRCUIT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CIRCUIT_NAME): str,
        vol.Required(CONF_CIRCUIT_POWER_ENTITY): _SENSOR_SELECTOR,
    }
)


class CircuitSubentryFlowHandler(ConfigSubentryFlow):
    """Añade un circuito (electrodoméstico) con su propio sensor de potencia en W."""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_CIRCUIT_NAME].strip()
            if not name:
                errors["base"] = "name_required"
            else:
                return self.async_create_entry(
                    title=name,
                    data={CONF_CIRCUIT_NAME: name, CONF_CIRCUIT_POWER_ENTITY: user_input[CONF_CIRCUIT_POWER_ENTITY]},
                )

        return self.async_show_form(step_id="user", data_schema=STEP_CIRCUIT_SCHEMA, errors=errors)
