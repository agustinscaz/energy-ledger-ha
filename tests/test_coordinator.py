"""Tests del coordinator: la regla de coste 0€, el corte de calendario real (no rolling) y la
persistencia entre reinicios — el núcleo de todo el proyecto (ver README, sección "Diseño")."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.energy_ledger.const import (
    CONF_BUY_PRICE_ENTITY,
    CONF_CIRCUIT_ENERGY_ENTITY,
    CONF_CIRCUIT_NAME,
    CONF_CIRCUIT_POWER_ENTITY,
    CONF_GRID_IMPORT_ENERGY_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_HOME_LOAD_POWER_ENTITY,
    CONF_POSITIVE_IS_EXPORT,
    CONF_SELL_PRICE_ENTITY,
    COST_METHOD_SHARED,
    COST_METHOD_UNSHARED,
    DOMAIN,
    ENERGY_METHOD_INTEGRATED,
    ENERGY_METHOD_METER,
    NODE_HOME,
    STORAGE_SAVE_DELAY_SECONDS,
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
GRID_ENERGY = "sensor.grid_import_energy"
CIRCUIT_ENERGY = "sensor.termo_energy"


def _make_entry(
    hass, *, positive_is_export=True, sell_price=False, home_load=False, grid_import_energy=False, circuits=None
):
    data = {
        CONF_GRID_POWER_ENTITY: GRID,
        CONF_POSITIVE_IS_EXPORT: positive_is_export,
        CONF_BUY_PRICE_ENTITY: BUY,
    }
    if sell_price:
        data[CONF_SELL_PRICE_ENTITY] = SELL
    if home_load:
        data[CONF_HOME_LOAD_POWER_ENTITY] = LOAD
    if grid_import_energy:
        data[CONF_GRID_IMPORT_ENERGY_ENTITY] = GRID_ENERGY

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

    _advance_accumulators(accumulators, after, amount=0.1 * 5.0)

    acc = accumulators[period]
    assert acc.last_closed_value == 7.0
    assert acc.value == pytest.approx(0.5)  # 0.1h * 5€/h, arrancando de 0 — no se reparte
    assert acc.period_start == _period_start(period, after)


def test_no_boundary_no_reset():
    now = datetime(2026, 3, 4, 10, 0, tzinfo=dt_util.UTC)
    accumulators = _new_accumulators(now)
    accumulators["day"].value = 3.0

    later_same_day = datetime(2026, 3, 4, 11, 0, tzinfo=dt_util.UTC)
    _advance_accumulators(accumulators, later_same_day, amount=1.0 * 2.0)

    assert accumulators["day"].last_closed_value is None
    assert accumulators["day"].value == pytest.approx(5.0)  # 3.0 + 1h*2€/h


def test_restart_gap_resets_honestly_without_backfilling():
    """Si HA estuvo apagado y al arrancar ya se cruzó un límite, se reinicia a 0 sin repartir ni
    inventar consumo del tiempo apagado (ver coordinator.async_setup: primer _recompute con
    elapsed=0 antes de que pase tiempo real)."""
    before_shutdown = datetime(2026, 3, 4, 20, 0, tzinfo=dt_util.UTC)
    accumulators = {"day": PeriodAccumulator(period_start=_period_start("day", before_shutdown), value=12.0)}

    after_long_downtime = datetime(2026, 3, 6, 9, 0, tzinfo=dt_util.UTC)  # dos días después
    _advance_accumulators(accumulators, after_long_downtime, amount=0.0)

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
    later = now + timedelta(minutes=30)
    coordinator._recompute(later)

    # No se pisa con 0: el hueco de home_load_power_entity no toca grid/buy, sigue sin gap global.
    assert coordinator.data_gap_since is None
    assert coordinator._last_savings_rate == pytest.approx(frozen_rate)
    assert coordinator.savings_gap_since == later  # #12: faltaba este gap-tracking

    hass.states.async_set(LOAD, "2000", {"unit_of_measurement": "W"})
    coordinator._recompute(later + timedelta(minutes=5))
    assert coordinator.savings_gap_since is None


# --- kWh exacto desde contador real (issue #7) ------------------------------------------------


def _make_entry_with_circuit_meter(hass, *, circuit_energy_entity=CIRCUIT_ENERGY):
    """Circuito con power_entity Y energy_entity — _make_entry no soporta el segundo, así que
    arma el subentry a mano."""
    data = {CONF_GRID_POWER_ENTITY: GRID, CONF_POSITIVE_IS_EXPORT: True, CONF_BUY_PRICE_ENTITY: BUY}
    subentries_data = [
        {
            "data": {
                CONF_CIRCUIT_NAME: "Termo",
                CONF_CIRCUIT_POWER_ENTITY: "sensor.termo_power",
                CONF_CIRCUIT_ENERGY_ENTITY: circuit_energy_entity,
            },
            "subentry_type": SUBENTRY_TYPE_CIRCUIT,
            "title": "Termo",
            "unique_id": None,
        }
    ]
    entry = MockConfigEntry(domain=DOMAIN, data=data, subentries_data=subentries_data)
    entry.add_to_hass(hass)
    return entry


async def test_energy_method_reports_integrated_by_default(hass):
    entry = _make_entry(hass, circuits={"Termo": "sensor.termo_power"})
    coordinator = EnergyLedgerCoordinator(hass, entry)
    circuit_id = next(iter(entry.subentries))
    assert coordinator.energy_method(NODE_HOME) == ENERGY_METHOD_INTEGRATED
    assert coordinator.energy_method(circuit_id) == ENERGY_METHOD_INTEGRATED


async def test_energy_method_reports_meter_when_energy_entity_configured(hass):
    entry = _make_entry_with_circuit_meter(hass)
    coordinator = EnergyLedgerCoordinator(hass, entry)
    circuit_id = next(iter(entry.subentries))
    assert coordinator.energy_method(circuit_id) == ENERGY_METHOD_METER
    assert coordinator.energy_method(NODE_HOME) == ENERGY_METHOD_INTEGRATED  # sin grid_import_energy_entity


async def test_circuit_energy_uses_real_meter_delta_not_integrated_power(hass):
    """El caso concreto del issue: potencia integrada da ~3% de más que el contador real."""
    entry = _make_entry_with_circuit_meter(hass)
    circuit_id = next(iter(entry.subentries))
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.termo_power", "2000", {"unit_of_measurement": "W"})
    hass.states.async_set(CIRCUIT_ENERGY, "10.0", {"unit_of_measurement": "kWh"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)  # primera lectura: fija last_raw_energy=10.0, no suma nada
    assert coordinator.nodes[circuit_id].energy["day"].value == pytest.approx(0.0)

    hass.states.async_set(CIRCUIT_ENERGY, "10.84", {"unit_of_measurement": "kWh"})
    coordinator._recompute(now + timedelta(hours=1))

    # Delta real del contador (0.84), NO 2kW * 1h = 2.0 (lo que daría potencia integrada).
    assert coordinator.nodes[circuit_id].energy["day"].value == pytest.approx(0.84)


async def test_circuit_energy_meter_reset_assumes_restarted_from_zero(hass):
    entry = _make_entry_with_circuit_meter(hass)
    circuit_id = next(iter(entry.subentries))
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.termo_power", "1000", {"unit_of_measurement": "W"})
    hass.states.async_set(CIRCUIT_ENERGY, "50.0", {"unit_of_measurement": "kWh"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)  # last_raw_energy = 50.0

    hass.states.async_set(CIRCUIT_ENERGY, "0.3", {"unit_of_measurement": "kWh"})  # el dispositivo se reinició
    coordinator._recompute(now + timedelta(minutes=10))

    # No resta (50.0 - 0.3 sería absurdo): asume que arrancó de 0 y suma el valor nuevo tal cual.
    assert coordinator.nodes[circuit_id].energy["day"].value == pytest.approx(0.3)
    assert coordinator._last_raw_energy[circuit_id] == pytest.approx(0.3)


async def test_circuit_energy_freezes_and_marks_gap_when_meter_unavailable(hass):
    entry = _make_entry_with_circuit_meter(hass)
    circuit_id = next(iter(entry.subentries))
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.termo_power", "1000", {"unit_of_measurement": "W"})
    hass.states.async_set(CIRCUIT_ENERGY, "5.0", {"unit_of_measurement": "kWh"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)  # last_raw_energy = 5.0

    hass.states.async_set(CIRCUIT_ENERGY, "unavailable")
    later = now + timedelta(minutes=15)
    coordinator._recompute(later)

    assert coordinator.energy_gap_since(circuit_id) == later
    assert coordinator._last_raw_energy[circuit_id] == pytest.approx(5.0)  # no se pierde
    assert coordinator.nodes[circuit_id].energy["day"].value == pytest.approx(0.0)  # no se suma nada

    hass.states.async_set(CIRCUIT_ENERGY, "5.5", {"unit_of_measurement": "kWh"})
    coordinator._recompute(later + timedelta(minutes=5))

    assert coordinator.energy_gap_since(circuit_id) is None
    assert coordinator.nodes[circuit_id].energy["day"].value == pytest.approx(0.5)  # 5.5 - 5.0


async def test_circuit_energy_meter_respects_calendar_close(hass):
    """Corte de calendario combinado con contador real: cierra en la medianoche igual que con
    potencia integrada, sin repartir el delta entre el día viejo y el nuevo."""
    entry = _make_entry_with_circuit_meter(hass)
    circuit_id = next(iter(entry.subentries))
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.termo_power", "1000", {"unit_of_measurement": "W"})
    hass.states.async_set(CIRCUIT_ENERGY, "1.0", {"unit_of_measurement": "kWh"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 23, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)  # last_raw_energy = 1.0

    hass.states.async_set(CIRCUIT_ENERGY, "1.5", {"unit_of_measurement": "kWh"})
    next_day = now + timedelta(hours=2)  # cruza medianoche
    coordinator._recompute(next_day)

    assert coordinator.nodes[circuit_id].energy["day"].last_closed_value == pytest.approx(0.0)
    assert coordinator.nodes[circuit_id].energy["day"].value == pytest.approx(0.5)  # todo al día nuevo


async def test_home_energy_uses_grid_import_energy_entity_when_configured(hass):
    entry = _make_entry(hass, grid_import_energy=True)
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})
    hass.states.async_set(GRID_ENERGY, "100.0", {"unit_of_measurement": "kWh"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)

    hass.states.async_set(GRID_ENERGY, "100.62", {"unit_of_measurement": "kWh"})
    coordinator._recompute(now + timedelta(hours=1))

    assert coordinator.nodes[NODE_HOME].energy["day"].value == pytest.approx(0.62)


async def test_last_raw_energy_survives_restart(hass):
    """Sin esto, la primera lectura post-reinicio se trataría como "primera vez" y se perdería
    el delta real del primer evento tras arrancar."""
    entry = _make_entry_with_circuit_meter(hass)
    circuit_id = next(iter(entry.subentries))
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set("sensor.termo_power", "1000", {"unit_of_measurement": "W"})
    hass.states.async_set(CIRCUIT_ENERGY, "20.0", {"unit_of_measurement": "kWh"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)
    await coordinator._async_save()

    restarted = EnergyLedgerCoordinator(hass, entry)
    later = now + timedelta(minutes=30)
    await restarted._async_load(later)
    restarted._ensure_nodes(later)
    restarted._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    restarted._last_update = later

    hass.states.async_set(CIRCUIT_ENERGY, "20.3", {"unit_of_measurement": "kWh"})
    restarted._recompute(later + timedelta(minutes=1))

    assert restarted.nodes[circuit_id].energy["day"].value == pytest.approx(0.3)


# --- Reparto de coste entre circuitos concurrentes (issue #9) ---------------------------------


async def test_circuit_cost_shared_never_exceeds_home_cost_with_concurrent_circuits(hass):
    """El caso del issue: 3 circuitos de 2kW cada uno (6kW de carga total) pero la casa solo
    importa 50W de red — sin repartir, cada circuito se cobraría el precio completo por sus 2kW
    enteros (24x sobreestimación entre los 3). Repartiendo, la suma nunca supera el coste real."""
    entry = _make_entry(
        hass,
        positive_is_export=True,
        home_load=True,
        circuits={"Lavavajillas": "sensor.lavavajillas_power", "Termo": "sensor.termo_power", "Vitro": "sensor.vitro_power"},
    )
    circuit_ids = list(entry.subentries)
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(GRID, "-50", {"unit_of_measurement": "W"})  # casa importando solo 50W
    hass.states.async_set(LOAD, "6050", {"unit_of_measurement": "W"})  # 3 * 2kW + 50W del resto
    for power_entity in ("sensor.lavavajillas_power", "sensor.termo_power", "sensor.vitro_power"):
        hass.states.async_set(power_entity, "2000", {"unit_of_measurement": "W"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), **{cid: (0.0, 0.0, 0.0) for cid in circuit_ids}}
    coordinator._last_update = now
    coordinator._recompute(now)  # fija tarifas, elapsed=0
    coordinator._recompute(now + timedelta(hours=1))

    home_cost = coordinator.nodes[NODE_HOME].cost["day"].value
    circuits_total = sum(coordinator.nodes[cid].cost["day"].value for cid in circuit_ids)
    assert home_cost == pytest.approx(0.05 * 0.20)  # 50W * 1h * 0.20€/kWh
    assert circuits_total <= home_cost + 1e-9
    assert coordinator.cost_method(circuit_ids[0]) == COST_METHOD_SHARED

    # Reparto proporcional: cada circuito pesa 2/6.05 del consumo total medido.
    expected_each = 0.05 * 0.20 * (2.0 / 6.05)
    for cid in circuit_ids:
        assert coordinator.nodes[cid].cost["day"].value == pytest.approx(expected_each, rel=1e-3)


async def test_circuit_cost_normalizes_when_sum_of_circuits_exceeds_load_kw(hass):
    """#12: ruido de medición entre sensores que no leen exactamente en el mismo instante puede
    hacer que la SUMA de circuit_kw supere load_kw aunque cada uno individualmente esté por
    debajo — acá 2 circuitos de 3kW cada uno (6kW) contra un load_kw de solo 5.9kW medido un
    instante distinto. Sin normalizar la suma, cada uno se clampearía a min(3/5.9,1)=0.508 y la
    suma dejaría cerca de 1.017 del coste real "facturado". Con la normalización, la suma da
    exactamente el coste real de la casa (import_kw), ni un centavo más."""
    entry = _make_entry(
        hass, positive_is_export=True, home_load=True, circuits={"A": "sensor.circuito_a", "B": "sensor.circuito_b"}
    )
    circuit_ids = list(entry.subentries)
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})  # importando 1kW
    hass.states.async_set(LOAD, "5900", {"unit_of_measurement": "W"})  # 5.9kW, MENOS que la suma de los circuitos
    hass.states.async_set("sensor.circuito_a", "3000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.circuito_b", "3000", {"unit_of_measurement": "W"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), **{cid: (0.0, 0.0, 0.0) for cid in circuit_ids}}
    coordinator._last_update = now
    coordinator._recompute(now)
    coordinator._recompute(now + timedelta(hours=1))

    home_cost = coordinator.nodes[NODE_HOME].cost["day"].value
    circuits_total = sum(coordinator.nodes[cid].cost["day"].value for cid in circuit_ids)
    assert home_cost == pytest.approx(1.0 * 0.20)  # 1kW * 1h * 0.20€/kWh
    # La normalización hace que la suma dé EXACTAMENTE el coste real, no solo "no lo supere".
    assert circuits_total == pytest.approx(home_cost, rel=1e-9)
    # Circuitos iguales (3kW cada uno) → reparto 50/50 del coste real.
    for cid in circuit_ids:
        assert coordinator.nodes[cid].cost["day"].value == pytest.approx(home_cost / 2, rel=1e-9)


async def test_circuit_cost_falls_back_to_unshared_without_home_load_power_entity(hass):
    """Sin home_load_power_entity, se mantiene el comportamiento binario original (conocido,
    puede sobreestimar) — no rompe instalaciones que no configuraron el campo opcional."""
    entry = _make_entry(hass, positive_is_export=True, circuits={"Termo": "sensor.termo_power"})
    circuit_id = next(iter(entry.subentries))
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(GRID, "-50", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.termo_power", "2000", {"unit_of_measurement": "W"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)
    coordinator._recompute(now + timedelta(hours=1))

    # Precio completo (0.20) por los 2kW enteros del circuito, no por los 50W reales de import.
    assert coordinator.nodes[circuit_id].cost["day"].value == pytest.approx(2.0 * 0.20)
    assert coordinator.cost_method(circuit_id) == COST_METHOD_UNSHARED


async def test_circuit_cost_falls_back_to_unshared_when_load_sensor_reads_zero(hass):
    """load_kw <= 0 (sensor disponible pero en 0, o midiendo menos que el propio circuito por
    ruido) también cae al binario en vez de dividir por 0 / dar un reparto sin sentido."""
    entry = _make_entry(hass, positive_is_export=True, home_load=True, circuits={"Termo": "sensor.termo_power"})
    circuit_id = next(iter(entry.subentries))
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(GRID, "-50", {"unit_of_measurement": "W"})
    hass.states.async_set(LOAD, "0", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.termo_power", "2000", {"unit_of_measurement": "W"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)
    coordinator._recompute(now + timedelta(hours=1))

    assert coordinator.cost_method(circuit_id) == COST_METHOD_UNSHARED
    assert coordinator.nodes[circuit_id].cost["day"].value == pytest.approx(2.0 * 0.20)


async def test_cost_method_is_none_for_home_node(hass):
    entry = _make_entry(hass, home_load=True, circuits={"Termo": "sensor.termo_power"})
    coordinator = EnergyLedgerCoordinator(hass, entry)
    assert coordinator.cost_method(NODE_HOME) is None


# --- Debounce de Store.async_save (issue #10) --------------------------------------------------


async def test_recompute_and_save_debounces_via_store_delay_save(hass):
    """Un evento de estado (potencia actualizando cada pocos segundos con un circuito activo) no
    debe disparar una escritura completa a disco — solo programa un guardado debounced."""
    entry = _make_entry(hass)
    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_update = now
    coordinator._store.async_delay_save = MagicMock()
    coordinator._store.async_save = AsyncMock()

    await coordinator._async_recompute_and_save(now + timedelta(minutes=1))

    coordinator._store.async_delay_save.assert_called_once_with(coordinator._to_storage_dict, STORAGE_SAVE_DELAY_SECONDS)
    coordinator._store.async_save.assert_not_called()


async def test_async_unload_flushes_pending_save_immediately(hass):
    """Si HA se apaga justo después de un cambio reciente y antes de que venza el delay del
    debounce, no se pierde el último tramo de acumulado — async_unload fuerza un flush."""
    entry = _make_entry(hass)
    coordinator = EnergyLedgerCoordinator(hass, entry)
    coordinator._store.async_save = AsyncMock()
    coordinator._store.async_delay_save = MagicMock()

    await coordinator.async_unload()

    coordinator._store.async_save.assert_awaited_once_with(coordinator._to_storage_dict())


# --- start_cycle / end_cycle (issue #13) --------------------------------------------------------


async def test_end_cycle_forces_recompute_without_external_event(hass):
    """#14: si no hubo un evento de estado reciente que dispare un _recompute externo, end_cycle
    debe seguir dando un delta exacto al momento exacto de la llamada — no depender de cuándo
    fue el último evento ni del tick de fondo cada 60s."""
    entry = _make_entry(hass, circuits={"Termo": "sensor.termo_power"})
    circuit_id = next(iter(entry.subentries))
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.termo_power", "1000", {"unit_of_measurement": "W"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator.start_cycle(circuit_id, now)  # start_cycle debe forzar el primer recompute solo

    later = now + timedelta(minutes=30)
    result = coordinator.end_cycle(circuit_id, later)  # sin NINGÚN _recompute externo entre medio

    assert result["cost"] == pytest.approx(0.20 * 0.5)  # 1kW * 0.5h * 0.20€/kWh
    assert result["energy_kwh"] == pytest.approx(0.5)
    assert result["duration_seconds"] == 1800


async def test_end_cycle_computes_delta_since_start_cycle(hass):
    entry = _make_entry(hass, positive_is_export=True, circuits={"Lavavajillas": "sensor.lavavajillas_power"})
    circuit_id = next(iter(entry.subentries))
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(GRID, "-2000", {"unit_of_measurement": "W"})  # importando 2kW
    hass.states.async_set("sensor.lavavajillas_power", "2000", {"unit_of_measurement": "W"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)  # fija tarifas, elapsed=0

    coordinator.start_cycle(circuit_id, now)
    coordinator._recompute(now + timedelta(hours=1))  # 2kW * 1h a 0.20€/kWh = 0.40€, 2kWh

    result = coordinator.end_cycle(circuit_id, now + timedelta(hours=1))

    assert result["cost"] == pytest.approx(0.40)
    assert result["energy_kwh"] == pytest.approx(2.0)
    assert result["compensation"] is None  # circuito: nunca compensa
    assert result["savings"] is None  # circuito: no tiene ahorro propio
    assert result["duration_seconds"] == 3600
    assert result["cost_method"] == COST_METHOD_UNSHARED  # sin home_load_power_entity
    assert result["energy_method"] == ENERGY_METHOD_INTEGRATED


async def test_end_cycle_without_start_cycle_raises(hass):
    entry = _make_entry(hass, circuits={"Termo": "sensor.termo_power"})
    circuit_id = next(iter(entry.subentries))
    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)

    with pytest.raises(LookupError):
        coordinator.end_cycle(circuit_id, now)


async def test_start_cycle_called_twice_resets_mark_without_error(hass):
    entry = _make_entry(hass, circuits={"Termo": "sensor.termo_power"})
    circuit_id = next(iter(entry.subentries))
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.termo_power", "1000", {"unit_of_measurement": "W"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)

    coordinator.start_cycle(circuit_id, now)
    coordinator._recompute(now + timedelta(minutes=30))
    coordinator.start_cycle(circuit_id, now + timedelta(minutes=30))  # pisa la marca sin romper nada
    coordinator._recompute(now + timedelta(hours=1))

    result = coordinator.end_cycle(circuit_id, now + timedelta(hours=1))
    assert result["duration_seconds"] == 1800  # solo los últimos 30 minutos, no la hora entera


async def test_cycle_lifetime_accumulator_not_affected_by_calendar_close(hass):
    """El ciclo puede cruzar medianoche (ej. un lavado nocturno) — el acumulado lifetime, a
    diferencia de los de día/semana/mes/año, nunca cierra, así que el delta sigue siendo exacto."""
    entry = _make_entry(hass, circuits={"Termo": "sensor.termo_power"})
    circuit_id = next(iter(entry.subentries))
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.termo_power", "1000", {"unit_of_measurement": "W"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 23, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)

    coordinator.start_cycle(circuit_id, now)
    coordinator._recompute(now + timedelta(minutes=30))  # mismo día: cost_day.value = 0.5h*0.20 = 0.10
    later = now + timedelta(hours=2)  # cruza medianoche
    coordinator._recompute(later)

    # El "day" sí cerró: lo que llevaba acumulado antes del cruce (0.10) queda en last_closed_value,
    # el día nuevo arranca en 0 y solo lleva la porción posterior al cruce...
    assert coordinator.nodes[circuit_id].cost["day"].last_closed_value == pytest.approx(0.10)
    # ...pero el ciclo, basado en lifetime, no se ve afectado por ese corte: ve el total completo.
    result = coordinator.end_cycle(circuit_id, later)
    assert result["cost"] == pytest.approx(0.20 * 2)  # 1kW * 2h * 0.20€/kWh


async def test_cycle_mark_survives_restart(hass):
    entry = _make_entry(hass, circuits={"Termo": "sensor.termo_power"})
    circuit_id = next(iter(entry.subentries))
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.termo_power", "1000", {"unit_of_measurement": "W"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)
    coordinator.start_cycle(circuit_id, now)
    await coordinator._async_save()

    restarted = EnergyLedgerCoordinator(hass, entry)
    later = now + timedelta(hours=1)
    await restarted._async_load(later)
    restarted._ensure_nodes(later)
    restarted._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    restarted._last_update = later
    restarted._recompute(later)  # fija tarifas, elapsed=0 (igual que async_setup real)
    restarted._recompute(later + timedelta(hours=1))

    result = restarted.end_cycle(circuit_id, later + timedelta(hours=1))
    assert result["cost"] == pytest.approx(0.20)  # 1kW * 1h * 0.20€/kWh, la marca sobrevivió


async def test_home_cycle_includes_compensation_and_savings(hass):
    entry = _make_entry(hass, positive_is_export=True, sell_price=True, home_load=True)
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(SELL, "0.05", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(LOAD, "3000", {"unit_of_measurement": "W"})
    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)

    coordinator.start_cycle(NODE_HOME, now)
    coordinator._recompute(now + timedelta(hours=1))

    result = coordinator.end_cycle(NODE_HOME, now + timedelta(hours=1))
    assert result["compensation"] == pytest.approx(0.0)  # importando, no exportando
    assert result["savings"] == pytest.approx(0.20 * 2.0)  # autoconsumo = 3-1 = 2kW


# --- NaN/Infinity no contaminan los acumulados (issue #15) -------------------------------------


async def test_read_float_treats_nan_as_gap_not_as_valid_value(hass):
    """float("nan") NO lanza ValueError — sin el chequeo explícito, se colaría como número
    "válido" y, sumado a un PeriodAccumulator, lo deja contaminado con NaN para siempre."""
    entry = _make_entry(hass)
    coordinator = EnergyLedgerCoordinator(hass, entry)
    hass.states.async_set(GRID, "nan", {"unit_of_measurement": "W"})
    assert coordinator._read_float(GRID) is None


async def test_read_float_treats_infinity_as_gap_not_as_valid_value(hass):
    entry = _make_entry(hass)
    coordinator = EnergyLedgerCoordinator(hass, entry)
    hass.states.async_set(GRID, "inf", {"unit_of_measurement": "W"})
    assert coordinator._read_float(GRID) is None
    hass.states.async_set(GRID, "-inf", {"unit_of_measurement": "W"})
    assert coordinator._read_float(GRID) is None


async def test_grid_power_nan_does_not_corrupt_lifetime_accumulator(hass):
    """El caso que más importa (#13, #15 combinados): un NaN pasajero en grid_power_entity no
    debe dejar el acumulado lifetime (que nunca resetea) contaminado para siempre."""
    entry = _make_entry(hass, circuits={"Termo": "sensor.termo_power"})
    circuit_id = next(iter(entry.subentries))
    hass.states.async_set(BUY, "0.20", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.termo_power", "1000", {"unit_of_measurement": "W"})

    coordinator = EnergyLedgerCoordinator(hass, entry)
    now = datetime(2026, 3, 2, 10, 0, tzinfo=dt_util.UTC)
    coordinator._ensure_nodes(now)
    coordinator._last_rates = {NODE_HOME: (0.0, 0.0, 0.0), circuit_id: (0.0, 0.0, 0.0)}
    coordinator._last_update = now
    coordinator._recompute(now)
    coordinator._recompute(now + timedelta(minutes=30))
    value_before_gap = coordinator.nodes[circuit_id].cost["lifetime"].value
    assert math.isfinite(value_before_gap)

    hass.states.async_set(GRID, "nan", {"unit_of_measurement": "W"})
    coordinator._recompute(now + timedelta(minutes=40))  # el hueco se detecta, pero el punto
    # central es que lo que se sumó (a la última tarifa válida, ver #2) SIGUE siendo finito.

    assert coordinator.data_gap_since is not None
    assert math.isfinite(coordinator.nodes[circuit_id].cost["lifetime"].value)  # nunca NaN

    hass.states.async_set(GRID, "-1000", {"unit_of_measurement": "W"})
    coordinator._recompute(now + timedelta(hours=1))  # se recupera solo, sigue acumulando normal

    assert coordinator.data_gap_since is None
    assert math.isfinite(coordinator.nodes[circuit_id].cost["lifetime"].value)
    assert coordinator.nodes[circuit_id].cost["lifetime"].value > value_before_gap
