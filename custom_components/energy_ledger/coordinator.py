"""Motor de cálculo y persistencia de Energy Ledger, uno por config entry (una "casa").

No hereda de DataUpdateCoordinator porque no hace polling a nada externo: reacciona a cambios de
estado de las entidades de entrada (grid_power, precios, potencia de cada circuito) y a un tick de
fondo (BACKGROUND_UPDATE_INTERVAL_SECONDS), y cada uno de esos eventos ya trae consigo el nuevo
valor a integrar, sin necesidad de "refetch".

Deliberadamente NO usa ningún helper genérico de HA (template, integration/riemann, utility_meter):
todo el cálculo vive aquí y toda la persistencia pasa por Store, para que sobreviva a reinicios sin
depender de que esos helpers restauren bien su estado (no lo hacen siempre) y sin las ventanas
"rolling" de utility_meter (día/semana/mes/año son SIEMPRE calendario real, ver _period_start).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BACKGROUND_UPDATE_INTERVAL_SECONDS,
    CONF_BUY_PRICE_ENTITY,
    CONF_CIRCUIT_POWER_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_HOME_LOAD_POWER_ENTITY,
    CONF_POSITIVE_IS_EXPORT,
    CONF_SELL_PRICE_ENTITY,
    DEFAULT_POSITIVE_IS_EXPORT,
    DOMAIN,
    NODE_HOME,
    PERIODS,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
    SUBENTRY_TYPE_CIRCUIT,
)

# (cost_rate €/h, compensation_rate €/h, energy_rate kW) vigentes para un nodo.
NodeRates = tuple[float, float, float]

_LOGGER = logging.getLogger(__name__)


def _period_start(period: str, now: datetime) -> datetime:
    """Inicio de calendario REAL del período que contiene `now`, en la zona horaria de `now`.

    Nunca una ventana "rolling" (N días hacia atrás) — ese fue precisamente el problema que
    utility_meter dio en producción y que esta integración existe para evitar.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return midnight
    if period == "week":
        return midnight - timedelta(days=midnight.weekday())  # weekday(): lunes = 0
    if period == "month":
        return midnight.replace(day=1)
    if period == "year":
        return midnight.replace(month=1, day=1)
    raise ValueError(f"periodo desconocido: {period}")


@dataclass
class PeriodAccumulator:
    """Acumulado de un (nodo, período): valor en curso + lo último cerrado, para diagnóstico."""

    value: float = 0.0
    period_start: datetime | None = None
    last_closed_value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "last_closed_value": self.last_closed_value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PeriodAccumulator:
        period_start = dt_util.parse_datetime(data["period_start"]) if data.get("period_start") else None
        return cls(
            value=data.get("value", 0.0),
            period_start=period_start,
            last_closed_value=data.get("last_closed_value"),
        )


def _new_accumulators(now: datetime) -> dict[str, PeriodAccumulator]:
    return {period: PeriodAccumulator(period_start=_period_start(period, now)) for period in PERIODS}


def _load_accumulators(data: dict[str, Any], now: datetime) -> dict[str, PeriodAccumulator]:
    """Reconstruye un dict de PeriodAccumulator desde storage, completando con acumuladores
    nuevos (valor 0) cualquier período ausente en `data` — ver uso en _async_load."""
    loaded = {p: PeriodAccumulator.from_dict(d) for p, d in data.items() if p in PERIODS}
    for period in PERIODS:
        loaded.setdefault(period, PeriodAccumulator(period_start=_period_start(period, now)))
    return loaded


def _advance_accumulators(accumulators: dict[str, PeriodAccumulator], now: datetime, elapsed_h: float, rate: float) -> None:
    """Cierra los períodos cuyo límite de calendario se cruzó y suma la contribución nueva.

    El cierre de calendario SIEMPRE va antes de sumar: si se cruzó un límite, el acumulado viejo
    se guarda en `last_closed_value` y el nuevo arranca en 0 — no se reparte la contribución entre
    el período viejo y el nuevo (no hace falta esa precisión), y tampoco se "rellena" el hueco si
    HA estuvo apagado durante el cruce (cero honesto, no inventado).
    """
    for period, acc in accumulators.items():
        correct_start = _period_start(period, now)
        if acc.period_start is None:
            acc.period_start = correct_start
        elif acc.period_start != correct_start:
            acc.last_closed_value = acc.value
            acc.value = 0.0
            acc.period_start = correct_start
        if elapsed_h > 0 and rate:
            acc.value += elapsed_h * rate


@dataclass
class NodeAccumulators:
    """Acumulados de un nodo (la casa/red, o un circuito). `compensation` es None para los
    circuitos: un circuito nunca "gana" plata, solo cuesta 0 o el precio real. `energy` (kWh)
    existe siempre: es la potencia del nodo integrada sola, sin multiplicar por precio. `savings`
    (ahorro por autoconsumo) solo existe para la casa, y solo si hay home_load_power_entity
    configurado (ver issue #6) — un circuito individual no tiene "ahorro" propio."""

    cost: dict[str, PeriodAccumulator]
    energy: dict[str, PeriodAccumulator]
    compensation: dict[str, PeriodAccumulator] | None = None
    savings: dict[str, PeriodAccumulator] | None = None


class EnergyLedgerCoordinator:
    """Coordina el cálculo Riemann-por-la-izquierda y la persistencia de una config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{entry.entry_id}")
        self.nodes: dict[str, NodeAccumulators] = {}
        self._last_update: datetime | None = None
        # node_id -> NodeRates. En memoria nomás: se recalcula desde los estados actuales en cada
        # arranque, no hace falta persistirla.
        self._last_rates: dict[str, NodeRates] = {}
        # Desde cuándo grid_power_entity o buy_price_entity están unavailable/unknown y por lo
        # tanto _last_rates está "congelado" en su último valor válido. None si no hay hueco.
        self._data_gap_since: datetime | None = None
        # Tarifa de ahorro (€/h) vigente para la casa. Separada de _last_rates (que es por nodo y
        # todo nodo la tiene) porque el ahorro es un concepto exclusivo de la casa y opcional.
        self._last_savings_rate: float = 0.0
        self._listeners: list[Callable[[], None]] = []
        self._unsub_state: Callable[[], None] | None = None
        self._unsub_interval: Callable[[], None] | None = None

    @property
    def track_compensation(self) -> bool:
        return bool(self.entry.data.get(CONF_SELL_PRICE_ENTITY))

    @property
    def track_savings(self) -> bool:
        return bool(self.entry.data.get(CONF_HOME_LOAD_POWER_ENTITY))

    @property
    def data_gap_since(self) -> datetime | None:
        return self._data_gap_since

    def _circuit_subentry_ids(self) -> list[str]:
        return [sub_id for sub_id, sub in self.entry.subentries.items() if sub.subentry_type == SUBENTRY_TYPE_CIRCUIT]

    def _tracked_entity_ids(self) -> list[str]:
        ids = [self.entry.data[CONF_GRID_POWER_ENTITY], self.entry.data[CONF_BUY_PRICE_ENTITY]]
        sell_price_entity = self.entry.data.get(CONF_SELL_PRICE_ENTITY)
        if sell_price_entity:
            ids.append(sell_price_entity)
        home_load_power_entity = self.entry.data.get(CONF_HOME_LOAD_POWER_ENTITY)
        if home_load_power_entity:
            ids.append(home_load_power_entity)
        for sub in self.entry.subentries.values():
            if sub.subentry_type == SUBENTRY_TYPE_CIRCUIT:
                ids.append(sub.data[CONF_CIRCUIT_POWER_ENTITY])
        return ids

    async def async_setup(self) -> None:
        now = dt_util.now()
        await self._async_load(now)
        self._ensure_nodes(now)
        self._last_update = now
        self._last_rates = {node_id: (0.0, 0.0, 0.0) for node_id in self.nodes}
        # Cálculo inicial con elapsed=0: fija la tarifa vigente de cada nodo para el próximo
        # evento real, sin inventar consumo del tiempo que HA estuvo apagado.
        self._recompute(now)
        await self._async_save()

        entity_ids = self._tracked_entity_ids()
        if entity_ids:
            self._unsub_state = async_track_state_change_event(self.hass, entity_ids, self._handle_state_change)
        self._unsub_interval = async_track_time_interval(
            self.hass, self._handle_interval, timedelta(seconds=BACKGROUND_UPDATE_INTERVAL_SECONDS)
        )

    def async_unload(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        """Se usa igual que en DataUpdateCoordinator: las entidades se suscriben con
        async_write_ha_state y se dan de baja al quitarse de hass."""
        self._listeners.append(update_callback)

        def _remove() -> None:
            self._listeners.remove(update_callback)

        return _remove

    def _notify_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()

    def _ensure_nodes(self, now: datetime) -> None:
        wanted = {NODE_HOME, *self._circuit_subentry_ids()}
        for node_id in wanted:
            if node_id not in self.nodes:
                self.nodes[node_id] = NodeAccumulators(
                    cost=_new_accumulators(now),
                    energy=_new_accumulators(now),
                    compensation=_new_accumulators(now) if node_id == NODE_HOME else None,
                    savings=_new_accumulators(now) if node_id == NODE_HOME and self.track_savings else None,
                )
        # Si se borró un circuito, se borra también su nodo — los subentry_id no se reutilizan
        # (uno nuevo siempre trae un id nuevo), así que no hay riesgo de "resucitar" acumulados
        # de un circuito distinto que por casualidad tuviera el mismo nombre.
        for node_id in list(self.nodes):
            if node_id not in wanted:
                del self.nodes[node_id]
                self._last_rates.pop(node_id, None)

    @callback
    def _handle_state_change(self, event: Event) -> None:
        self.hass.async_create_task(self._async_recompute_and_save(dt_util.now()))

    @callback
    def _handle_interval(self, now: datetime) -> None:
        self.hass.async_create_task(self._async_recompute_and_save(dt_util.now()))

    async def _async_recompute_and_save(self, now: datetime) -> None:
        self._recompute(now)
        await self._async_save()
        self._notify_listeners()

    def _recompute(self, now: datetime) -> None:
        elapsed_h = 0.0
        if self._last_update is not None:
            elapsed_h = max((now - self._last_update).total_seconds() / 3600, 0.0)

        for node_id, node in self.nodes.items():
            cost_rate, compensation_rate, energy_rate = self._last_rates.get(node_id, (0.0, 0.0, 0.0))
            _advance_accumulators(node.cost, now, elapsed_h, cost_rate)
            if node.compensation is not None:
                _advance_accumulators(node.compensation, now, elapsed_h, compensation_rate)
            _advance_accumulators(node.energy, now, elapsed_h, energy_rate)
            if node.savings is not None:
                _advance_accumulators(node.savings, now, elapsed_h, self._last_savings_rate)

        self._last_update = now
        self._recompute_rates(now)

    def _recompute_rates(self, now: datetime) -> None:
        """Tarifa efectiva = precio de compra si la casa importa, 0 si no (autoconsumo/excedente).
        La tarifa de CADA nodo es esa tarifa efectiva multiplicada por SU potencia, en €/h — es lo
        que _advance_accumulators integra sobre el tiempo transcurrido. El kWh de cada nodo es esa
        misma potencia sola, sin multiplicar por precio.

        Si grid_power_entity o buy_price_entity están unavailable/unknown, NO se pisa
        self._last_rates con ceros (eso sería indistinguible de "casa exportando/autoconsumiendo
        al 100%") — se deja la última tarifa válida congelada y se marca el hueco en
        self._data_gap_since, ver ATTR_DATA_GAP_SINCE en sensor.py."""
        grid_power = self._read_float(self.entry.data[CONF_GRID_POWER_ENTITY])
        buy_price = self._read_float(self.entry.data[CONF_BUY_PRICE_ENTITY])

        if grid_power is None or buy_price is None:
            if self._data_gap_since is None:
                self._data_gap_since = now
            return
        self._data_gap_since = None

        positive_is_export = self.entry.data.get(CONF_POSITIVE_IS_EXPORT, DEFAULT_POSITIVE_IS_EXPORT)
        if positive_is_export:
            import_kw = max(-grid_power, 0.0) / 1000
            export_kw = max(grid_power, 0.0) / 1000
        else:
            import_kw = max(grid_power, 0.0) / 1000
            export_kw = max(-grid_power, 0.0) / 1000

        effective_price = buy_price if import_kw > 0 else 0.0
        home_cost_rate = effective_price * import_kw

        sell_price = self._read_float(self.entry.data.get(CONF_SELL_PRICE_ENTITY)) or 0.0
        home_compensation_rate = sell_price * export_kw

        self._last_rates[NODE_HOME] = (home_cost_rate, home_compensation_rate, import_kw)

        # Ahorro: solo si hay home_load_power_entity configurado (ver issue #6). Si el sensor
        # de consumo total está unavailable/unknown, se congela la última tarifa de ahorro
        # válida en vez de pisarla con 0 — mismo criterio que el hueco de #2, acotado a esta
        # única entidad opcional (no toca self._data_gap_since, que es solo de grid/buy_price).
        home_load_power_entity = self.entry.data.get(CONF_HOME_LOAD_POWER_ENTITY)
        if home_load_power_entity:
            load_power = self._read_float(home_load_power_entity)
            if load_power is not None:
                load_kw = max(load_power, 0.0) / 1000
                autoconsumo_kw = max(load_kw - import_kw, 0.0)
                self._last_savings_rate = autoconsumo_kw * buy_price + export_kw * sell_price

        for sub_id, sub in self.entry.subentries.items():
            if sub.subentry_type != SUBENTRY_TYPE_CIRCUIT:
                continue
            circuit_power = self._read_float(sub.data[CONF_CIRCUIT_POWER_ENTITY])
            circuit_kw = max(circuit_power, 0.0) / 1000 if circuit_power is not None else 0.0
            self._last_rates[sub_id] = (effective_price * circuit_kw, 0.0, circuit_kw)

    def _read_float(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except ValueError:
            _LOGGER.debug("Estado no numérico de %s: %r", entity_id, state.state)
            return None

    async def _async_load(self, now: datetime) -> None:
        stored = await self._store.async_load()
        if not stored:
            return
        for node_id, node_data in stored.get("nodes", {}).items():
            cost = _load_accumulators(node_data.get("cost", {}), now)
            # "energy" no existía en versiones previas del storage: los períodos que falten
            # arrancan en 0 en vez de romper la carga (mismo criterio de cero honesto, no de
            # backfill inventado).
            energy = _load_accumulators(node_data.get("energy", {}), now)
            compensation_data = node_data.get("compensation")
            compensation = _load_accumulators(compensation_data, now) if compensation_data is not None else None
            savings_data = node_data.get("savings")
            savings = _load_accumulators(savings_data, now) if savings_data is not None else None
            self.nodes[node_id] = NodeAccumulators(cost=cost, energy=energy, compensation=compensation, savings=savings)

    async def _async_save(self) -> None:
        await self._store.async_save(self._to_storage_dict())

    def _to_storage_dict(self) -> dict[str, Any]:
        return {
            "nodes": {
                node_id: {
                    "cost": {p: acc.to_dict() for p, acc in node.cost.items()},
                    "energy": {p: acc.to_dict() for p, acc in node.energy.items()},
                    "compensation": (
                        {p: acc.to_dict() for p, acc in node.compensation.items()}
                        if node.compensation is not None
                        else None
                    ),
                    "savings": (
                        {p: acc.to_dict() for p, acc in node.savings.items()} if node.savings is not None else None
                    ),
                }
                for node_id, node in self.nodes.items()
            }
        }

    def diagnostics_snapshot(self) -> dict[str, Any]:
        """Usado por diagnostics.py: estado interno completo del coordinator para poder resolver
        "¿por qué este número no cuadra?" bajando un JSON en vez de pedir logs/acceso a la
        instancia real (ver issue #3)."""
        return {
            "nodes": self._to_storage_dict()["nodes"],
            "last_update": self._last_update.isoformat() if self._last_update else None,
            "last_rates": {
                node_id: {"cost_rate": cost_rate, "compensation_rate": compensation_rate, "energy_rate": energy_rate}
                for node_id, (cost_rate, compensation_rate, energy_rate) in self._last_rates.items()
            },
            "data_gap_since": self._data_gap_since.isoformat() if self._data_gap_since else None,
            "last_savings_rate": self._last_savings_rate,
        }
