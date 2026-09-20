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
    CONF_CIRCUIT_ENERGY_ENTITY,
    CONF_CIRCUIT_POWER_ENTITY,
    CONF_GRID_IMPORT_ENERGY_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_HOME_LOAD_POWER_ENTITY,
    CONF_POSITIVE_IS_EXPORT,
    CONF_SELL_PRICE_ENTITY,
    COST_METHOD_SHARED,
    COST_METHOD_UNSHARED,
    DEFAULT_POSITIVE_IS_EXPORT,
    DOMAIN,
    ENERGY_METHOD_INTEGRATED,
    ENERGY_METHOD_METER,
    NODE_HOME,
    PERIOD_LIFETIME,
    PERIODS,
    STORAGE_KEY_PREFIX,
    STORAGE_SAVE_DELAY_SECONDS,
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
    if period == PERIOD_LIFETIME:
        # Constante, no depende de `now`: nunca difiere de sí misma, así que _advance_accumulators
        # nunca lo trata como un corte de calendario — un acumulado que corre desde siempre.
        return datetime.min.replace(tzinfo=now.tzinfo)
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


# PERIODS + el pseudo-período lifetime (#13): todo lo que _new_accumulators/_load_accumulators
# maneja para cada nodo. PERIODS solo (sin lifetime) sigue siendo lo que sensor.py itera para
# crear sensores — lifetime nunca se expone como sensor propio.
_ALL_PERIODS = (*PERIODS, PERIOD_LIFETIME)


def _new_accumulators(now: datetime) -> dict[str, PeriodAccumulator]:
    return {period: PeriodAccumulator(period_start=_period_start(period, now)) for period in _ALL_PERIODS}


def _load_accumulators(data: dict[str, Any], now: datetime) -> dict[str, PeriodAccumulator]:
    """Reconstruye un dict de PeriodAccumulator desde storage, completando con acumuladores
    nuevos (valor 0) cualquier período ausente en `data` — ver uso en _async_load."""
    loaded = {p: PeriodAccumulator.from_dict(d) for p, d in data.items() if p in _ALL_PERIODS}
    for period in _ALL_PERIODS:
        loaded.setdefault(period, PeriodAccumulator(period_start=_period_start(period, now)))
    return loaded


def _advance_accumulators(accumulators: dict[str, PeriodAccumulator], now: datetime, amount: float) -> None:
    """Cierra los períodos cuyo límite de calendario se cruzó y suma `amount` (ya calculado por
    el caller: elapsed_h * rate para las fuentes basadas en potencia integrada, o el delta de un
    contador real de energía — ver issue #7).

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
        if amount:
            acc.value += amount


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
        # node_id -> última lectura cruda (kWh) de su contador real de energía, si tiene uno
        # configurado (ver issue #7). Persistido en Store — perder esto en un reinicio haría que
        # la primera lectura post-reinicio se tratara como "primera vez" y se pierda un delta real.
        self._last_raw_energy: dict[str, float | None] = {}
        # node_id -> desde cuándo su energy_entity/grid_import_energy_entity está
        # unavailable/unknown. No persistido (mismo criterio que _data_gap_since: se
        # redetecta fresco en cada arranque).
        self._energy_gap_since: dict[str, datetime | None] = {}
        # circuit_id -> "repartido"/"binario_sin_repartir" vigente (ver issue #9). Solo circuitos;
        # el coste del nodo casa/red no tiene este concepto, se calcula directo sobre el import
        # real. En memoria nomás, igual que _last_rates: se recalcula en cada ciclo.
        self._last_cost_method: dict[str, str] = {}
        # Desde cuándo home_load_power_entity está unavailable/unknown (ver issue #12: faltaba
        # este gap-tracking, a diferencia de data_gap_since y energy_gap_since que ya lo tienen
        # para sus respectivas fuentes). No persistido, mismo criterio que los otros dos.
        self._savings_gap_since: datetime | None = None
        # node_id -> marca de start_cycle (ver issue #13): snapshot de los acumulados "lifetime"
        # de ese nodo al momento de empezar el ciclo, más el timestamp. Persistido en Store — un
        # ciclo largo (ej. un lavado de 3 horas) no debería perder la marca solo porque HA se
        # reinició a mitad de camino. Un ciclo que nunca se cierra con end_cycle no causa ningún
        # problema, solo queda ahí sin usarse; no hace falta expirarlo.
        self._cycle_marks: dict[str, dict[str, Any]] = {}
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

    def _energy_entity_for(self, node_id: str) -> str | None:
        """entity_id del contador real de energía de este nodo, si tiene uno configurado —
        grid_import_energy_entity para la casa, energy_entity del subentry para un circuito."""
        if node_id == NODE_HOME:
            return self.entry.data.get(CONF_GRID_IMPORT_ENERGY_ENTITY)
        sub = self.entry.subentries.get(node_id)
        if sub is not None and sub.subentry_type == SUBENTRY_TYPE_CIRCUIT:
            return sub.data.get(CONF_CIRCUIT_ENERGY_ENTITY)
        return None

    def energy_method(self, node_id: str) -> str:
        return ENERGY_METHOD_METER if self._energy_entity_for(node_id) else ENERGY_METHOD_INTEGRATED

    def energy_gap_since(self, node_id: str) -> datetime | None:
        return self._energy_gap_since.get(node_id)

    def cost_method(self, node_id: str) -> str | None:
        """None para el nodo casa/red — no tiene este concepto, su coste es exacto siempre
        (se calcula directo sobre el import real medido, no hay nada que repartir entre nadie)."""
        if node_id == NODE_HOME:
            return None
        return self._last_cost_method.get(node_id, COST_METHOD_UNSHARED)

    @property
    def savings_gap_since(self) -> datetime | None:
        return self._savings_gap_since

    def start_cycle(self, node_id: str, now: datetime) -> None:
        """Guarda un snapshot de los acumulados "lifetime" de este nodo como marca de inicio de
        ciclo. Llamar de nuevo sin haber cerrado el anterior simplemente pisa la marca — no hace
        falta soportar ciclos anidados (ver issue #13).

        _recompute(now) primero (#14): sin esto, la foto podría tener hasta
        BACKGROUND_UPDATE_INTERVAL_SECONDS de desfasaje si no hubo un evento de estado reciente
        — llamar al servicio no debería depender de cuándo fue el último tick de fondo."""
        self._recompute(now)
        node = self.nodes.get(node_id)
        if node is None:
            raise ValueError(f"Nodo desconocido: {node_id}")
        self._cycle_marks[node_id] = {
            "start_time": now.isoformat(),
            "cost": node.cost[PERIOD_LIFETIME].value,
            "energy": node.energy[PERIOD_LIFETIME].value,
            "compensation": node.compensation[PERIOD_LIFETIME].value if node.compensation is not None else None,
            "savings": node.savings[PERIOD_LIFETIME].value if node.savings is not None else None,
        }

    def end_cycle(self, node_id: str, now: datetime) -> dict[str, Any]:
        """Delta contra la marca de start_cycle. LookupError si no hay marca previa para este
        nodo — el llamador (services.py) lo traduce a un error de servicio claro en vez de un
        delta sin sentido contra un mark inexistente.

        _recompute(now) primero (#14), mismo motivo que en start_cycle: la foto de cierre tiene
        que ser exacta al momento de la llamada, no depender de cuándo fue el último evento."""
        self._recompute(now)
        node = self.nodes.get(node_id)
        if node is None:
            raise ValueError(f"Nodo desconocido: {node_id}")
        mark = self._cycle_marks.pop(node_id, None)
        if mark is None:
            raise LookupError(f"No hay un ciclo iniciado (start_cycle) para el nodo {node_id}")

        compensation_delta = None
        if node.compensation is not None and mark["compensation"] is not None:
            compensation_delta = round(node.compensation[PERIOD_LIFETIME].value - mark["compensation"], 4)
        savings_delta = None
        if node.savings is not None and mark["savings"] is not None:
            savings_delta = round(node.savings[PERIOD_LIFETIME].value - mark["savings"], 4)

        start_time = dt_util.parse_datetime(mark["start_time"]) or now
        return {
            "cost": round(node.cost[PERIOD_LIFETIME].value - mark["cost"], 4),
            "energy_kwh": round(node.energy[PERIOD_LIFETIME].value - mark["energy"], 4),
            "compensation": compensation_delta,
            "savings": savings_delta,
            "duration_seconds": round((now - start_time).total_seconds()),
            "cost_method": self.cost_method(node_id),
            "energy_method": self.energy_method(node_id),
        }

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
        grid_import_energy_entity = self.entry.data.get(CONF_GRID_IMPORT_ENERGY_ENTITY)
        if grid_import_energy_entity:
            ids.append(grid_import_energy_entity)
        for sub in self.entry.subentries.values():
            if sub.subentry_type == SUBENTRY_TYPE_CIRCUIT:
                ids.append(sub.data[CONF_CIRCUIT_POWER_ENTITY])
                circuit_energy_entity = sub.data.get(CONF_CIRCUIT_ENERGY_ENTITY)
                if circuit_energy_entity:
                    ids.append(circuit_energy_entity)
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

    async def async_unload(self) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        # Flush final inmediato (#10): si había un guardado debounced pendiente de
        # _async_recompute_and_save, no perder el último tramo de acumulado por descargar la
        # integración antes de que venza el delay. Store.async_save cancela el delay pendiente.
        await self._async_save()

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
                self._last_raw_energy.pop(node_id, None)
                self._energy_gap_since.pop(node_id, None)
                self._last_cost_method.pop(node_id, None)
                self._cycle_marks.pop(node_id, None)

    @callback
    def _handle_state_change(self, event: Event) -> None:
        self.hass.async_create_task(self._async_recompute_and_save(dt_util.now()))

    @callback
    def _handle_interval(self, now: datetime) -> None:
        self.hass.async_create_task(self._async_recompute_and_save(dt_util.now()))

    async def _async_recompute_and_save(self, now: datetime) -> None:
        self._recompute(now)
        # Debounced (#10), no escritura inmediata: con actividad sostenida (un circuito cuyo
        # sensor de potencia actualiza cada pocos segundos) esto evita una escritura completa a
        # disco por evento. Store arma el dict recién cuando efectivamente escribe, así que
        # siempre serializa el estado más reciente aunque hayan pasado varios recompute entre
        # medio. El flush final al descargar la integración está en async_unload.
        self._store.async_delay_save(self._to_storage_dict, STORAGE_SAVE_DELAY_SECONDS)
        self._notify_listeners()

    def _recompute(self, now: datetime) -> None:
        elapsed_h = 0.0
        if self._last_update is not None:
            elapsed_h = max((now - self._last_update).total_seconds() / 3600, 0.0)

        for node_id, node in self.nodes.items():
            cost_rate, compensation_rate, energy_rate = self._last_rates.get(node_id, (0.0, 0.0, 0.0))
            _advance_accumulators(node.cost, now, elapsed_h * cost_rate)
            if node.compensation is not None:
                _advance_accumulators(node.compensation, now, elapsed_h * compensation_rate)
            self._advance_node_energy(node_id, node, now, energy_rate, elapsed_h)
            if node.savings is not None:
                _advance_accumulators(node.savings, now, elapsed_h * self._last_savings_rate)

        self._last_update = now
        self._recompute_rates(now)

    def _advance_node_energy(
        self, node_id: str, node: NodeAccumulators, now: datetime, energy_rate_kw: float, elapsed_h: float
    ) -> None:
        """kWh del nodo en el período: por defecto potencia integrada (energy_rate_kw * elapsed_h,
        Riemann por la izquierda, ~3% de error frente a un contador real — ver issue #7). Si el
        nodo tiene un contador real configurado (energy_entity del circuito, o
        grid_import_energy_entity de la casa), se usa ESE en cambio: se lee el valor crudo y se
        acumula el delta contra la última lectura, exacto sin reconstruir nada.

        Si el contador real está unavailable/unknown, se congela (no se suma nada, no se pisa
        last_raw_value) y se marca el hueco en self._energy_gap_since[node_id] — mismo criterio
        de "cero honesto, no pisar con 0" que _recompute_rates para grid/buy_price (#2), pero acá
        acotado a este nodo/entidad en particular, sin tocar el data_gap_since global."""
        energy_entity = self._energy_entity_for(node_id)
        if not energy_entity:
            _advance_accumulators(node.energy, now, elapsed_h * energy_rate_kw)
            return

        raw = self._read_float(energy_entity)
        if raw is None:
            if self._energy_gap_since.get(node_id) is None:
                self._energy_gap_since[node_id] = now
            _advance_accumulators(node.energy, now, 0.0)  # el calendario cierra igual, sin sumar
            return
        self._energy_gap_since[node_id] = None

        last_raw = self._last_raw_energy.get(node_id)
        if last_raw is None:
            delta_kwh = 0.0  # primera lectura: nada contra qué comparar todavía
        elif raw >= last_raw:
            delta_kwh = raw - last_raw
        else:
            delta_kwh = raw  # el contador se reinició: asume que arrancó de 0, no resta negativo
        self._last_raw_energy[node_id] = raw
        _advance_accumulators(node.energy, now, delta_kwh)

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
        # Marca su propio hueco en _savings_gap_since (#12: faltaba, a diferencia de
        # data_gap_since/energy_gap_since que ya lo tienen para sus fuentes).
        # load_kw se reutiliza abajo para repartir el coste entre circuitos concurrentes (#9).
        home_load_power_entity = self.entry.data.get(CONF_HOME_LOAD_POWER_ENTITY)
        load_kw: float | None = None
        if home_load_power_entity:
            load_power = self._read_float(home_load_power_entity)
            if load_power is None:
                if self._savings_gap_since is None:
                    self._savings_gap_since = now
            else:
                self._savings_gap_since = None
                load_kw = max(load_power, 0.0) / 1000
                autoconsumo_kw = max(load_kw - import_kw, 0.0)
                self._last_savings_rate = autoconsumo_kw * buy_price + export_kw * sell_price

        circuit_kws: dict[str, float] = {}
        for sub_id, sub in self.entry.subentries.items():
            if sub.subentry_type != SUBENTRY_TYPE_CIRCUIT:
                continue
            circuit_power = self._read_float(sub.data[CONF_CIRCUIT_POWER_ENTITY])
            circuit_kws[sub_id] = max(circuit_power, 0.0) / 1000 if circuit_power is not None else 0.0

        # effective_price*circuit_kw es un gate binario por CASA aplicado entero a CADA circuito
        # por separado — si hay 3 circuitos de 2kW y la casa importa apenas 50W, los 3 se cobran
        # a precio completo por sus 2kW enteros (6kW "facturados" cuando la red solo puso 50W).
        # Con home_load_power_entity se puede repartir el import real proporcionalmente a lo que
        # pesa cada circuito sobre el consumo total medido. Sin esa entidad (o con load_kw <= 0)
        # se cae al comportamiento binario original — sobreestima conocida, ver ATTR_COST_METHOD.
        if load_kw is not None and import_kw > 0 and load_kw > 0:
            # scale normaliza la SUMA de todos los circuitos contra load_kw en un solo paso — no
            # alcanza con clampear cada circuito por separado contra load_kw (#9 original): si la
            # SUMA de varios circuitos concurrentes supera load_kw por ruido de medición entre
            # sensores que no leen exactamente en el mismo instante, sin esto la suma de costes
            # repartidos podría seguir superando levemente el coste real de la casa (#12). Con
            # scale, cada circuito pesa circuit_kw/total_circuit_kw cuando el total excede
            # load_kw — la suma de las razones da exactamente 1.0, nunca más.
            total_circuit_kw = sum(circuit_kws.values())
            scale = min(load_kw / total_circuit_kw, 1.0) if total_circuit_kw > 0 else 1.0
            for sub_id, circuit_kw in circuit_kws.items():
                circuit_import_share_kw = import_kw * circuit_kw * scale / load_kw
                self._last_rates[sub_id] = (buy_price * circuit_import_share_kw, 0.0, circuit_kw)
                self._last_cost_method[sub_id] = COST_METHOD_SHARED
        else:
            for sub_id, circuit_kw in circuit_kws.items():
                self._last_rates[sub_id] = (effective_price * circuit_kw, 0.0, circuit_kw)
                self._last_cost_method[sub_id] = COST_METHOD_UNSHARED

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
        self._last_raw_energy = dict(stored.get("last_raw_energy", {}))
        self._cycle_marks = dict(stored.get("cycle_marks", {}))
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
            },
            "last_raw_energy": self._last_raw_energy,
            "cycle_marks": self._cycle_marks,
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
            "last_raw_energy": self._last_raw_energy,
            "energy_gap_since": {
                node_id: gap.isoformat() for node_id, gap in self._energy_gap_since.items() if gap is not None
            },
            "last_cost_method": dict(self._last_cost_method),
            "savings_gap_since": self._savings_gap_since.isoformat() if self._savings_gap_since else None,
            "cycle_marks": dict(self._cycle_marks),
        }
