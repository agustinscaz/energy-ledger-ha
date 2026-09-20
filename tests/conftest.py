"""Configuración común de pytest.

Todos los módulos de esta integración (coordinator.py, config_flow.py, sensor.py, __init__.py)
dependen de Home Assistant real —a diferencia de otros proyectos que separan lógica "pura"—, así
que a diferencia de edistribucion-ha no hace falta ningún stub de sys.modules: estos tests siempre
requieren pytest-homeassistant-custom-component instalado (ver requirements_test.txt).
"""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def _auto_enable_custom_integrations(enable_custom_integrations):
    """Sin esto, Home Assistant no encuentra `custom_components.energy_ledger` como una
    integración instalable de verdad y `hass.config_entries.async_setup(...)` falla."""
    yield
