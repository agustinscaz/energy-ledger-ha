"""Constantes del dominio, claves de configuración y valores por defecto de Energy Ledger."""

from __future__ import annotations

DOMAIN = "energy_ledger"

# Claves de config_entry.data (paso "user" del config flow, uno por casa/instalación).
CONF_GRID_POWER_ENTITY = "grid_power_entity"
CONF_POSITIVE_IS_EXPORT = "positive_is_export"
CONF_BUY_PRICE_ENTITY = "buy_price_entity"
CONF_SELL_PRICE_ENTITY = "sell_price_entity"
# Opcional: potencia de consumo TOTAL de la casa (W), no solo import/export de red. Sin esto no se
# puede derivar autoconsumo (autoconsumo_kw = load_kw - import_kw), así que el sensor de ahorro
# (ver issue #6) solo se crea si está configurado.
CONF_HOME_LOAD_POWER_ENTITY = "home_load_power_entity"
# Opcionales, ver issue #7: contador real de energía (kWh, típicamente state_class
# total_increasing) para reemplazar la reconstrucción por potencia integrada, que arrastra un
# pequeño error de discretización (Riemann por la izquierda). CONF_CIRCUIT_ENERGY_ENTITY es un
# campo del subentry "circuit"; CONF_GRID_IMPORT_ENERGY_ENTITY es del nodo casa/red.
CONF_CIRCUIT_ENERGY_ENTITY = "energy_entity"
CONF_GRID_IMPORT_ENERGY_ENTITY = "grid_import_energy_entity"

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
# Debounce de Store.async_delay_save (ver issue #10): con un circuito activo el sensor de
# potencia puede actualizar cada pocos segundos, y sin debounce cada evento dispara una escritura
# completa a disco. Un delay corto sigue sin perder nada real entre reinicios (lo único que le
# importa a esta integración) sin escribir en cada tick.
STORAGE_SAVE_DELAY_SECONDS = 10

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

# Se expone en los sensores del nodo casa/red (y de cada circuito, porque sus tarifas dependen de
# la misma lectura) cuando grid_power_entity o buy_price_entity están unavailable/unknown: desde
# cuándo está el dato "congelado" en la última tarifa válida. None cuando no hay hueco en curso.
ATTR_DATA_GAP_SINCE = "datos_incompletos_desde"

# Solo en LedgerEnergySensor (ver issue #7): de qué fuente sale el kWh del período.
ATTR_ENERGY_METHOD = "metodo"
ENERGY_METHOD_METER = "contador_real"
ENERGY_METHOD_INTEGRATED = "potencia_integrada"

# Solo en LedgerCostSensor de un CIRCUITO (nunca en el nodo casa/red, que es exacto siempre — ver
# issue #9): si el coste de este instante repartió el import real entre circuitos concurrentes
# (con home_load_power_entity) o aplicó el precio completo a la potencia entera del circuito
# (comportamiento binario original, sobreestima cuando hay más de un circuito activo a la vez).
ATTR_COST_METHOD = "metodo_coste"
COST_METHOD_SHARED = "repartido"
COST_METHOD_UNSHARED = "binario_sin_repartir"
