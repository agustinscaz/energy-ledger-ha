"""Servicios start_cycle/end_cycle de Energy Ledger (ver issue #13): coste/energía de un ciclo de
electrodoméstico (o de toda la casa) sin necesitar un input_number externo que reste manualmente.

Se registran una sola vez a nivel de dominio (async_setup en __init__.py), no por config entry —
un dispositivo puede pertenecer a cualquiera de las instalaciones de Energy Ledger que haya
configuradas, así que la resolución busca en el device registry primero y desde ahí encuentra la
entry/coordinator correcta."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NODE_HOME, SERVICE_END_CYCLE, SERVICE_START_CYCLE
from .coordinator import EnergyLedgerCoordinator

_CYCLE_SERVICE_SCHEMA = vol.Schema({vol.Required(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string])})


def _resolve_node(hass: HomeAssistant, device_ids: list[str]) -> tuple[EnergyLedgerCoordinator, str]:
    """Un dispositivo de Energy Ledger tiene identifiers={(DOMAIN, node_id)} — ver DeviceInfo en
    sensor.py. Para un circuito, node_id ES el subentry_id (coincide con la clave que usa el
    coordinator en self.nodes). Para la casa, el identifier usa entry.entry_id (para que el
    dispositivo tenga un identifier estable ligado a la entry), pero el coordinator guarda ese
    nodo bajo la constante NODE_HOME — hay que traducir uno al otro acá."""
    if len(device_ids) != 1:
        raise ServiceValidationError("Elegí un único circuito (o la casa) por llamada a este servicio.")
    device_id = device_ids[0]

    device_reg = dr.async_get(hass)
    device = device_reg.async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"No existe el dispositivo {device_id}.")

    identifier = next((ident[1] for ident in device.identifiers if ident[0] == DOMAIN), None)
    if identifier is None or not device.config_entries:
        raise ServiceValidationError(f"El dispositivo {device_id} no pertenece a Energy Ledger.")

    entry_id = next(iter(device.config_entries))
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.runtime_data is None:
        raise ServiceValidationError("La instalación de Energy Ledger de este dispositivo no está cargada.")

    node_id = NODE_HOME if identifier == entry.entry_id else identifier
    return entry.runtime_data, node_id


async def _async_handle_start_cycle(hass: HomeAssistant, call: ServiceCall) -> None:
    coordinator, node_id = _resolve_node(hass, call.data[ATTR_DEVICE_ID])
    coordinator.start_cycle(node_id, dt_util.now())
    await coordinator._async_save()  # inmediato: es una acción explícita y poco frecuente, no un tick de potencia


async def _async_handle_end_cycle(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    coordinator, node_id = _resolve_node(hass, call.data[ATTR_DEVICE_ID])
    try:
        result = coordinator.end_cycle(node_id, dt_util.now())
    except LookupError as err:
        raise ServiceValidationError(str(err)) from err
    await coordinator._async_save()
    return result


def async_setup_services(hass: HomeAssistant) -> None:
    """Se llama una sola vez desde async_setup (nivel de dominio, no por config entry) —
    hass.services.has_service evita registrar dos veces si por lo que sea se llama más de una."""
    if hass.services.has_service(DOMAIN, SERVICE_START_CYCLE):
        return

    async def _start_cycle_handler(call: ServiceCall) -> None:
        await _async_handle_start_cycle(hass, call)

    async def _end_cycle_handler(call: ServiceCall) -> ServiceResponse:
        return await _async_handle_end_cycle(hass, call)

    hass.services.async_register(DOMAIN, SERVICE_START_CYCLE, _start_cycle_handler, schema=_CYCLE_SERVICE_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_END_CYCLE,
        _end_cycle_handler,
        schema=_CYCLE_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
