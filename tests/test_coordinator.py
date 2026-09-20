"""Tests del coordinator: la regla de coste 0€, el corte de calendario real (no rolling) y la
persistencia entre reinicios — el núcleo de todo el proyecto (ver README, sección "Diseño")."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_ledger.const import (
    CONF_BUY_PRICE_ENTITY,
    CONF_CIRCUIT_NAME,
    CONF_CIRCUIT_POWER_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_HOME_LOAD_POWER_ENTITY,
    CONF_POSITIVE_IS_EXPORT,
    CONF_SELL_PRICE_ENTITY,
    DOMAIN,
    NODE_HOME,
    SUBENTRY_TYPE_CIRCUIT,
)
from custom_components.energy_ledger.coordinator import (
    EnergyLedgerCoordinator,
    PeriodAccumulator,
    _advance_accumulators,
    _new_accumulators,
    _period_start,
)
from homeassistant.util import dt as dt_util

GRID = "sensor.grid_power"
BUY = "sensor.buy_price"
SELL = "sensor.sell_price"
LOAD = "sensor.home_load"


def _make_entry(hass, *, positive_is_export=True, sell_price=False, home_load=False, circuits=None):
    data = {
        CONF_GRID_POWER_ENTITY: GRID,
        CONF_POSITIVE_IS_EXPORT: positive_is_export,
        CONF_BUY_PRICE_ENTITY: BUY,
    }
    if sell_price:
        data[CONF_SELL_PRICE_ENTITY] = SELL
    if home_load:
        data[CONF_HOME_LOAD_POWER_ENTITY] = LOAD

    subentries_data = [
        {
            "data": {CONF_CIRCUIT_NAME: name, CONF_CIRCUIT_POWER_ENTITY: power_entity},
            "subentry_type": SUBENTRY_TYPE_CIRCUIT,
            "title": name,
            "unique_id": None,
        }
        for name, power_entity in (circuits or {}).items()
    ]

    entry = MockConfigEntry(domain=DOMAIN, data=data, subentries_data=subentries_data)
    entry.add_to_hass(hass)
    return entry


# --- Regla de coste 0€ -------------------------------------------------------------------


async def test_cost_zero_while_exporting_or_zero_default_convention(hass):
    """positive_is_export=True (default): grid_power >= 0 (exportando o cero) => precio 0."""
    entry = _make_entry(hass, positive_is_export=True)
    hass.states.async_set(GRID, "500", {"unit_of_measurement": "W"})
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0)}
    coordinator._recompute(now)

    assert coordinator._last_rates[NODE_HOME][0] == 0.0

    hass.states.async_set(GRID, "0", {"unit_of_measurement": "W"})
    coordinator._recompute(now)
    assert coordinator._last_rates[NODE_HOME][0] == 0.0


async def test_cost_real_price_while_importing_default_convention(hass):
    entry = _make_entry(hass, positive_is_export=True)
    hass.states.async_set(GRID, "-1500", {"unit_of_measurement": "W"})  # importando 1.5 kW
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0)}
    coordinator._recompute(now)

    cost_rate, _, _ = coordinator._last_rates[NODE_HOME]
    assert cost_rate == pytest.approx(0.20 * 1.5)


async def test_cost_rule_with_inverted_convention(hass):
    """positive_is_export=False: la convención de signo se invierte por completo."""
    entry = _make_entry(hass, positive_is_export=False)
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0)}

    hass.states.async_set(GRID, "1500", {"unit_of_measurement": "W"})  # positivo = importando
    coordinator._recompute(now)
    cost_rate, _, _ = coordinator._last_rates[NODE_HOME]
    assert cost_rate == pytest.approx(0.20 * 1.5)

    hass.states.async_set(GRID, "-500", {"unit_of_measurement": "W"})  # negativo = exportando
    coordinator._recompute(now)
    cost_rate, _, _ = coordinator._last_rates[NODE_HOME]
    assert cost_rate == 0.0


async def test_circuit_uses_same_effective_price_as_house(hass):
    """Un circuito nunca "gana" plata: 0 si la casa no importa, precio real si importa."""
    entry = _make_entry(hass, positive_is_export=True, circuits={"Termo": "sensor.termo_power"})
    hass.states.async_set(BUY, "0.10", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.termo_power", "2000", {"unit_of_measurement": "W"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    circuit_id = next(iter(entry.subentries))
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}

    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})  # casa importando
    coordinator._recompute(now)
    circuit_rate, _, _ = coordinator._last_rates[circuit_id]
    assert circuit_rate == pytest.approx(0.10 * 2.0)

    hass.states.async_set(GRID, "1000", {"unit_of_measurement": "W"})  # casa exportando
    coordinator._recompute(now)
    circuit_rate, _, _ = coordinator._last_rates[circuit_id]
    assert circuit_rate == 0.0


# --- Corte de calendario real -------------------------------------------------------------


def test_period_start_is_real_calendar_not_rolling():
    now = datetime(2026, 3, 4, 15, 30, tzinfo=dt_util.UTC)  # miércoles 4 de marzo de 2026
    assert _period_start("day", now) == datetime(2026, 3, 4, 0, 0, tzinfo=dt_util.UTC)
    assert _period_start("week", now) == datetime(2026, 3, 2, 0, 0, tzinfo=dt_util.UTC)  # lunes
    assert _period_start("month", now) == datetime(2026, 3, 1, 0, 0, tzinfo=dt_util.UTC)
    assert _period_start("year", now) == datetime(2026, 1, 1, 0, 0, tzinfo=dt_util.UTC)


@pytest.mark.parametrize(
    ("period", "before", "after"),
    [
        ("day", datetime(2026, 3, 4, 23, 59, tzinfo=dt_util.UTC), datetime(2026, 3, 5, 0, 1, tzinfo=dt_util.UTC)),
        ("week", datetime(2026, 3, 8, 23, 59, tzinfo=dt_util.UTC), datetime(2026, 3, 9, 0, 1, tzinfo=dt_util.UTC)),
        ("month", datetime(2026, 3, 31, 23, 59, tzinfo=dt_util.UTC), datetime(2026, 4, 1, 0, 1, tzinfo=dt_util.UTC)),
        ("year", datetime(2026, 12, 31, 23, 59, tzinfo=dt_util.UTC), datetime(2027, 1, 1, 0, 1, tzinfo=dt_util.UTC)),
    ],
)
def test_boundary_closes_and_resets_regardless_of_crossing_time(period, before, after):
    accumulators = {period: PeriodAccumulator(period_start=_period_start(period, before), value=7.0)}

    _advance_accumulators(accumulators, after, elapsed_h=0.1, rate=5.0)

    acc = accumulators[period]
    assert acc.last_closed_value == 7.0
    assert acc.value == pytest.approx(0.5)  # 0.1h * 5€/h, arrancando de 0 — no se reparte
    assert acc.period_start == _period_start(period, after)


def test_no_boundary_no_reset():
    now = datetime(2026, 3, 4, 10, 0, tzinfo=dt_util.UTC)
    accumulators = _new_accumulators(now)
    accumulators["day"].value = 3.0

    later_same_day = datetime(2026, 3, 4, 11, 0, tzinfo=dt_util.UTC)
    _advance_accumulators(accumulators, later_same_day, elapsed_h=1.0, rate=2.0)

    assert accumulators["day"].last_closed_value is None
    assert accumulators["day"].value == pytest.approx(5.0)  # 3.0 + 1h*2€/h


def test_restart_gap_resets_honestly_without_backfilling():
    """Si HA estuvo apagado y al arrancar ya se cruzó un límite, se reinicia a 0 sin repartir ni
    inventar consumo del tiempo apagado (ver coordinator.async_setup: primer _recompute con
    elapsed=0 antes de que pase tiempo real)."""
    before_shutdown = datetime(2026, 3, 4, 20, 0, tzinfo=dt_util.UTC)
    accumulators = {"day": PeriodAccumulator(period_start=_period_start("day", before_shutdown), value=12.0)}

    after_long_downtime = datetime(2026, 3, 6, 9, 0, tzinfo=dt_util.UTC)  # dos días después
    _advance_accumulators(accumulators, after_long_downtime, elapsed_h=0.0, rate=0.0)

    assert accumulators["day"].last_closed_value == 12.0
    assert accumulators["day"].value == 0.0


# --- Persistencia entre reinicios ----------------------------------------------------------


async def test_persistence_survives_restart(hass):
    entry = _make_entry(hass, positive_is_export=True)
    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})  # importando 1 kW
    hass.states.async_set(BUY, "0.15", {"unit_of_measurement": "EUR/kWh"})

    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator = EnergyLedgerCoordinator(hass, entry)
    coordinator._ensure_nodes(now)
    coordinator._last_update = now
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0)}
    coordinator._recompute(now)  # fija la tarifa (elapsed=0, no acumula nada todavía)

    coordinator._recompute(now + timedelta(minutes=30))  # acumula 0.5h * 0.15€/h
    await coordinator._async_save()

    assert coordinator.nodes[NODE_HOME].cost["day"].value == pytest.approx(0.075)

    # "Reinicio de HA": un coordinator nuevo que carga del mismo Store en disco.
    restarted = EnergyLedgerCoordinator(hass, entry)
    await restarted._async_load(now + timedelta(minutes=30))

    assert restarted.nodes[NODE_HOME].cost["day"].value == pytest.approx(0.075)
    assert restarted.nodes[NODE_HOME].cost["week"].value == pytest.approx(0.075)
    assert restarted.nodes[NODE_HOME].cost["month"].value == pytest.approx(0.075)
    assert restarted.nodes[NODE_HOME].cost["year"].value == pytest.approx(0.075)


# --- Balance neto ----------------------------------------------------------------------------


async def test_balance_is_compensation_minus_cost(hass):
    entry = _make_entry(hass, positive_is_export=True, sell_price=True)
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(SELL, "0.05", {"unit_of_measurement": "EUR/kWh"})

    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator = EnergyLedgerCoordinator(hass, entry)
    coordinator._ensure_nodes(now)
    coordinator._last_update = now
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0)}

    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})  # importando 1kW
    coordinator._recompute(now)  # fija cost_rate=0.20€/h, comp_rate=0
    coordinator._recompute(now + timedelta(hours=1))  # acumula 1h de coste

    hass.states.async_set(GRID, "2000", {"unit_of_measurement": "W"})  # ahora exporta 2kW
    coordinator._recompute(now + timedelta(hours=1))  # recalcula tarifas, elapsed=0
    coordinator._recompute(now + timedelta(hours=2))  # acumula 1h de compensación

    node = coordinator.nodes[NODE_HOME]
    cost = node.cost["day"].value
    compensation = node.compensation["day"].value

    assert cost == pytest.approx(0.20)
    assert compensation == pytest.approx(0.10)  # 0.05 €/kWh * 2 kW * 1h
    assert compensation - cost == pytest.approx(-0.10)


async def test_no_compensation_tracked_without_sell_price_entity(hass):
    entry = _make_entry(hass, positive_is_export=True, sell_price=False)
    coordinator = EnergyLedgerCoordinator(hass, entry)
    assert coordinator.track_compensation is False


# --- kWh por nodo (issue #1) --------------------------------------------------------------


async def test_energy_accumulates_for_home_and_circuit_independent_of_import_status(hass):
    """A diferencia del coste, el kWh de un circuito se integra siempre, aunque la casa esté
    exportando (cost_rate=0 en ese instante, energy_rate no)."""
    entry = _make_entry(hass, positive_is_export=True, circuits={"Termo": "sensor.termo_power"})
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.termo_power", "2000", {"unit_of_measurement": "W"})
    hass.states.async_set(GRID, "1000", {"unit_of_measurement": "W"})  # casa exportando

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    circuit_id = next(iter(entry.subentries))
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)  # fija tarifas, elapsed=0
    coordinator._recompute(now + timedelta(hours=1))

    assert coordinator.nodes[circuit_id].cost["day"].value == 0.0  # casa exportando => coste 0
    assert coordinator.nodes[circuit_id].energy["day"].value == pytest.approx(2.0)  # 2kW * 1h
    assert coordinator.nodes[NODE_HOME].energy["day"].value == pytest.approx(0.0)  # import_kw=0


# --- Hueco de datos (issue #2) --------------------------------------------------------------


async def test_grid_unavailable_freezes_rates_and_marks_gap(hass):
    entry = _make_entry(hass, positive_is_export=True)
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})  # importando 1kW

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)  # fija cost_rate=0.20€/h

    assert coordinator.data_gap_since is None

    hass.states.async_set(GRID, "unavailable")
    later = now + timedelta(minutes=30)
    coordinator._recompute(later)

    assert coordinator.data_gap_since == later
    # La tarifa queda congelada en la última válida, no se pisa con 0.
    assert coordinator._last_rates[NODE_HOME][0] == pytest.approx(0.20)

    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})
    coordinator._recompute(later + timedelta(minutes=5))
    assert coordinator.data_gap_since is None


# --- Ahorro por autoconsumo (issue #6) -------------------------------------------------------


async def test_no_savings_tracked_without_home_load_power_entity(hass):
    entry = _make_entry(hass, positive_is_export=True)
    coordinator = EnergyLedgerCoordinator(hass, entry)
    assert coordinator.track_savings is False

    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    assert coordinator.nodes[NODE_HOME].savings is None


async def test_savings_equals_self_consumption_at_buy_price_plus_export_at_sell_price(hass):
    """ahorro = autoconsumo_kw * precio_compra + export_kw * precio_venta, donde
    autoconsumo_kw = max(load_kw - import_kw, 0). Load 3kW, import 1kW => autoconsumo 2kW."""
    entry = _make_entry(hass, positive_is_export=True, sell_price=True, home_load=True)
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(SELL, "0.05", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(LOAD, "3000", {"unit_of_measurement": "W"})
    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})  # importando 1kW

    coordinator = EnergyLedgerCoordinator(hass, entry)
    assert coordinator.track_savings is True
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)  # fija la tarifa, elapsed=0

    coordinator._recompute(now + timedelta(hours=1))

    # autoconsumo_kw = max(3 - 1, 0) = 2kW; export_kw = 0 (importando) => ahorro = 2 * 0.20 = 0.40
    assert coordinator.nodes[NODE_HOME].savings["day"].value == pytest.approx(0.40)


async def test_savings_includes_export_compensation_when_exporting(hass):
    """Casa exportando: import_kw=0, autoconsumo_kw = load_kw entero; export sí compensa."""
    entry = _make_entry(hass, positive_is_export=True, sell_price=True, home_load=True)
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(SELL, "0.05", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(LOAD, "500", {"unit_of_measurement": "W"})
    hass.states.async_set(GRID, "1500", {"unit_of_measurement": "W"})  # exportando 1.5kW

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)
    coordinator._recompute(now + timedelta(hours=1))

    # autoconsumo_kw = max(0.5 - 0, 0) = 0.5kW * 0.20 = 0.10; export 1.5kW * 0.05 = 0.075
    assert coordinator.nodes[NODE_HOME].savings["day"].value == pytest.approx(0.175)


async def test_savings_freezes_when_load_entity_unavailable(hass):
    entry = _make_entry(hass, positive_is_export=True, home_load=True)
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(LOAD, "2000", {"unit_of_measurement": "W"})
    hass.states.async_set(GRID, "-500", {"unit_of_measurement": "W"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)
    frozen_rate = coordinator._last_savings_rate
    assert frozen_rate == pytest.approx(0.20 * 1.5)  # autoconsumo = 2 - 0.5 = 1.5kW

    hass.states.async_set(LOAD, "unavailable")
    coordinator._recompute(now + timedelta(minutes=30))

    # No se pisa con 0: el hueco de home_load_power_entity no toca grid/buy, sigue sin gap global.
    assert coordinator.data_gap_since is None
    assert coordinator._last_savings_rate == pytest.approx(frozen_rate)
