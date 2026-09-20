"""Sensores de Energy Ledger: coste, compensación y balance neto por nodo y período.

No calculan nada por su cuenta — solo leen los acumulados que mantiene el coordinator y se
repintan cuando este avisa (async_add_listener), igual que con un DataUpdateCoordinator normal.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    ATTR_DATA_GAP_SINCE,
    ATTR_LAST_CLOSED_PERIOD,
    ATTR_PERIOD_START,
    CONF_BUY_PRICE_ENTITY,
    CONF_CIRCUIT_NAME,
    DOMAIN,
    NODE_HOME,
    PERIODS,
    SUBENTRY_TYPE_CIRCUIT,
)
from .coordinator import EnergyLedgerCoordinator, PeriodAccumulator

DEFAULT_CURRENCY = "EUR"


def _infer_currency(hass: HomeAssistant, price_entity_id: str) -> str:
    """La unidad de buy_price_entity es algo como "EUR/kWh" o "€/kWh" — la moneda es la parte
    antes de la barra. No se hardcodea "€" para no asumir que todo el mundo factura en euros."""
    state = hass.states.get(price_entity_id)
    unit = state.attributes.get("unit_of_measurement") if state else None
    if not unit or "/" not in unit:
        return DEFAULT_CURRENCY
    currency = unit.split("/", 1)[0].strip()
    return currency or DEFAULT_CURRENCY


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator: EnergyLedgerCoordinator = entry.runtime_data
    currency = _infer_currency(hass, entry.data[CONF_BUY_PRICE_ENTITY])

    home_device = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=entry.title, manufacturer="Energy Ledger")

    home_entities: list[SensorEntity] = []
    for period in PERIODS:
        home_entities.append(LedgerCostSensor(coordinator, entry, NODE_HOME, period, currency, home_device))
        home_entities.append(LedgerEnergySensor(coordinator, entry, NODE_HOME, period, home_device))
        if coordinator.track_compensation:
            home_entities.append(LedgerCompensationSensor(coordinator, entry, NODE_HOME, period, currency, home_device))
            home_entities.append(LedgerBalanceSensor(coordinator, entry, NODE_HOME, period, currency, home_device))
    async_add_entities(home_entities)

    for sub_id, sub in entry.subentries.items():
        if sub.subentry_type != SUBENTRY_TYPE_CIRCUIT:
            continue
        circuit_device = DeviceInfo(
            identifiers={(DOMAIN, sub_id)},
            name=f"{entry.title} {sub.data[CONF_CIRCUIT_NAME]}",
            manufacturer="Energy Ledger",
            via_device=(DOMAIN, entry.entry_id),
        )
        circuit_entities: list[SensorEntity] = []
        for period in PERIODS:
            circuit_entities.append(LedgerCostSensor(coordinator, entry, sub_id, period, currency, circuit_device))
            circuit_entities.append(LedgerEnergySensor(coordinator, entry, sub_id, period, circuit_device))
        async_add_entities(circuit_entities, config_subentry_id=sub_id)


class _LedgerSensorBase(SensorEntity):
    """Común a coste/compensación/balance: unique_id estable, dispositivo, y suscripción al
    coordinator en vez de polling (should_poll=False)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        coordinator: EnergyLedgerCoordinator,
        entry: ConfigEntry,
        node_id: str,
        period: str,
        unit: str,
        device_info: DeviceInfo,
        kind: str,
        device_class: SensorDeviceClass = SensorDeviceClass.MONETARY,
    ) -> None:
        self.coordinator = coordinator
        self._node_id = node_id
        self._period = period
        self._attr_native_unit_of_measurement = unit
        self._attr_device_info = device_info
        self._attr_unique_id = f"{entry.entry_id}_{node_id}_{kind}_{period}"
        self._attr_translation_key = f"{kind}_{period}"
        self._attr_device_class = device_class
        self._remove_listener: callable | None = None

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self.coordinator.async_add_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    def _period_accumulator(self) -> PeriodAccumulator | None:
        raise NotImplementedError

    @property
    def native_value(self) -> float | None:
        acc = self._period_accumulator()
        return round(acc.value, 4) if acc is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, float | str | None]:
        acc = self._period_accumulator()
        if acc is None:
            return {}
        return {
            ATTR_LAST_CLOSED_PERIOD: round(acc.last_closed_value, 4) if acc.last_closed_value is not None else None,
            ATTR_PERIOD_START: acc.period_start.isoformat() if acc.period_start else None,
            ATTR_DATA_GAP_SINCE: (
                self.coordinator.data_gap_since.isoformat() if self.coordinator.data_gap_since else None
            ),
        }


class LedgerCostSensor(_LedgerSensorBase):
    """Coste acumulado del nodo (casa o circuito) en el período, sobre potencia de import real."""

    def __init__(self, coordinator, entry, node_id, period, currency, device_info) -> None:
        super().__init__(coordinator, entry, node_id, period, currency, device_info, kind="cost")

    def _period_accumulator(self) -> PeriodAccumulator | None:
        node = self.coordinator.nodes.get(self._node_id)
        return node.cost.get(self._period) if node else None


class LedgerEnergySensor(_LedgerSensorBase):
    """kWh acumulados del nodo (casa o circuito) en el período — la potencia integrada sola, sin
    multiplicar por precio. A diferencia del coste, se acumula siempre (no solo mientras la casa
    importa): es el consumo real del nodo, no lo que costó."""

    def __init__(self, coordinator, entry, node_id, period, device_info) -> None:
        super().__init__(
            coordinator, entry, node_id, period, "kWh", device_info, kind="energy", device_class=SensorDeviceClass.ENERGY
        )

    def _period_accumulator(self) -> PeriodAccumulator | None:
        node = self.coordinator.nodes.get(self._node_id)
        return node.energy.get(self._period) if node else None


class LedgerCompensationSensor(_LedgerSensorBase):
    """Compensación por excedentes exportados — solo existe para el nodo casa/red."""

    def __init__(self, coordinator, entry, node_id, period, currency, device_info) -> None:
        super().__init__(coordinator, entry, node_id, period, currency, device_info, kind="compensation")

    def _period_accumulator(self) -> PeriodAccumulator | None:
        node = self.coordinator.nodes.get(self._node_id)
        return node.compensation.get(self._period) if node and node.compensation else None


class LedgerBalanceSensor(_LedgerSensorBase):
    """Balance neto = compensación − coste, para el nodo casa/red."""

    def __init__(self, coordinator, entry, node_id, period, currency, device_info) -> None:
        super().__init__(coordinator, entry, node_id, period, currency, device_info, kind="balance")

    @property
    def native_value(self) -> float | None:
        node = self.coordinator.nodes.get(self._node_id)
        if node is None or node.compensation is None:
            return None
        cost = node.cost.get(self._period)
        compensation = node.compensation.get(self._period)
        if cost is None or compensation is None:
            return None
        return round(compensation.value - cost.value, 4)

    @property
    def extra_state_attributes(self) -> dict[str, float | str | None]:
        node = self.coordinator.nodes.get(self._node_id)
        if node is None or node.compensation is None:
            return {}
        cost = node.cost.get(self._period)
        compensation = node.compensation.get(self._period)
        if cost is None or compensation is None:
            return {}
        last_closed = None
        if cost.last_closed_value is not None and compensation.last_closed_value is not None:
            last_closed = round(compensation.last_closed_value - cost.last_closed_value, 4)
        return {
            ATTR_LAST_CLOSED_PERIOD: last_closed,
            ATTR_PERIOD_START: cost.period_start.isoformat() if cost.period_start else None,
            ATTR_DATA_GAP_SINCE: (
                self.coordinator.data_gap_since.isoformat() if self.coordinator.data_gap_since else None
            ),
        }
