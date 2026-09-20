"""Constantes del dominio, claves de configuración y valores por defecto de Energy Ledger."""

from __future__ import annotations

DOMAIN = "energy_ledger"

# Claves de config_entry.data (paso "user" del config flow, uno por casa/instalación).
CONF_GRID_POWER_ENTITY = "grid_power_entity"
CONF_POSITIVE_IS_EXPORT = "positive_is_export"
CONF_BUY_PRICE_ENTITY = "buy_price_entity"
CONF_SELL_PRICE_ENTITY = "sell_price_entity"

# Claves de subentry.data (subentries de tipo "circuit", repetibles, uno por electrodoméstico).
CONF_CIRCUIT_NAME = "name"
CONF_CIRCUIT_POWER_ENTITY = "power_entity"

SUBENTRY_TYPE_CIRCUIT = "circuit"

# positive_is_export=True es la convención real de sensor.goodwe_meter_active_power_total: positivo
# = exportando, negativo = importando. Configurable porque no todos los medidores de red la usan.
DEFAULT_POSITIVE_IS_EXPORT = True

# Cubre el caso de una entidad de entrada (típicamente una tarifa de compra fija) que no emite
# cambios de estado durante horas: sin este tick, el coste dejaría de acumularse aunque la casa
# siga importando, porque el coordinator solo recalcula al reaccionar a eventos de estado.
BACKGROUND_UPDATE_INTERVAL_SECONDS = 60

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}_ledger"

NODE_HOME = "home"

# Periodos de acumulación. Claves internas en inglés (convención de HA core); los sufijos que ve
# el usuario en entity_id y atributos van en español, ver PERIOD_SUFFIX.
PERIOD_DAY = "day"
PERIOD_WEEK = "week"
PERIOD_MONTH = "month"
PERIOD_YEAR = "year"
PERIODS = (PERIOD_DAY, PERIOD_WEEK, PERIOD_MONTH, PERIOD_YEAR)

# "ano" sin tilde a propósito: un entity_id de HA no admite "ñ" en su slug.
PERIOD_SUFFIX = {
    PERIOD_DAY: "hoy",
    PERIOD_WEEK: "semana",
    PERIOD_MONTH: "mes",
    PERIOD_YEAR: "ano",
}

ATTR_LAST_CLOSED_PERIOD = "ultimo_periodo"
ATTR_PERIOD_START = "fecha_inicio_periodo"
