# Energy Ledger

Integración de Home Assistant, instalable vía [HACS](https://hacs.xyz/), que lleva la cuenta de
**coste, compensación y balance neto** de tu consumo eléctrico — de la casa entera y, opcionalmente,
de circuitos individuales (un termo, una lavadora...) — a partir de entidades que ya existen en tu
instancia. No añade ningún add-on ni proceso externo: es una integración pura que solo lee estados y
persiste sus propios acumulados.

No controla tu inversor, tu batería, ni nada — eso sigue viviendo en tus automatizaciones. Energy
Ledger solo mira y cuenta.

## Qué necesitás antes de instalar

Tres (o cuatro) entidades que ya tengas en Home Assistant:

- Un sensor de **potencia neta de red**, en W (ej. `sensor.goodwe_meter_active_power_total`).
- Un sensor de **precio de compra** en tiempo real, en `<moneda>/kWh` (ej. `EUR/kWh`).
- Opcionalmente, un sensor de **precio de venta de excedentes**, en las mismas unidades. Si no lo
  configurás, la integración solo trackea coste — no compensación ni balance.

## Configuración

1. HACS → Integraciones → buscá "Energy Ledger" → instalar → reiniciar HA.
2. Ajustes → Dispositivos y servicios → Añadir integración → "Energy Ledger".
3. En el único paso del asistente:
   - **Sensor de potencia neta de red**: elegí tu sensor de potencia en W.
   - **Positivo = exportando**: dejalo marcado si tu sensor usa la convención "positivo cuando
     exportás a la red, negativo cuando importás" (es la de `sensor.goodwe_meter_active_power_total`).
     Desmarcalo si tu sensor usa la convención contraria.
   - **Sensor de precio de compra**: tu tarifa de compra en tiempo real.
   - **Sensor de precio de venta de excedentes** (opcional): dejalo vacío si no te compensan los
     excedentes.
4. Una vez creada la integración, podés añadir circuitos: en la tarjeta de la integración, "Añadir
   circuito" (subentrada), tantas veces como quieras. Cada uno pide un nombre libre (ej. "Termo") y
   su propio sensor de potencia en W.

Podés editar o borrar un circuito, o quitar la integración entera, desde la propia UI de Home
Assistant — no hace falta tocar YAML en ningún momento.

## Qué entidades crea

Por cada instalación (nodo casa/red), sensores de tipo `monetary` con `state_class: total`, uno por
período — hoy, esta semana, este mes, este año:

- `sensor.<...>_coste_*`
- `sensor.<...>_compensacion_*` (solo si configuraste precio de venta)
- `sensor.<...>_balance_*` (solo si configuraste precio de venta)

Por cada circuito, solo coste (un circuito nunca "gana" plata):

- `sensor.<...>_<circuito>_coste_*`

Todos los sensores traen dos atributos para poder diagnosticar sin mirar logs:

- `ultimo_periodo`: el valor con el que cerró el período anterior (ej. el coste total del mes
  pasado, visible durante todo el mes en curso).
- `fecha_inicio_periodo`: desde cuándo corre el acumulado actual.

La unidad monetaria se toma automáticamente del sensor de precio de compra (lo que tenga antes de
la barra en su `unit_of_measurement`, ej. `EUR/kWh` → `EUR`) — no está hardcodeada a euros.

## Diseño

### La regla de los 0€

En cualquier instante, si la casa **no** está importando de la red (está exportando excedente o en
cero — según cómo esté configurada tu convención de signo), el precio efectivo de **todo** el
consumo de ese instante, tanto el total de la casa como el de cada circuito, es **0**. Se asume
autoconsumo solar/batería al 100%. Si la casa está importando, el precio efectivo es el de tu
tarifa de compra en ese momento.

El coste del nodo casa/red se calcula sobre la potencia de **import real**, no sobre el consumo
total — es lo que de verdad pagás. La compensación (solo para casa/red, nunca para un circuito) se
calcula sobre la potencia de **export real** por el precio de venta. Un circuito individual nunca
resta ni compensa: solo cuesta 0 o el precio real, según lo que esté haciendo la casa en conjunto en
ese instante — no importa si ESE circuito en particular está "usando" más o menos que la solar
disponible, la pregunta relevante es si la casa en su conjunto está importando o no.

### Por qué semana/mes/año son SIEMPRE calendario real, nunca "rolling"

El helper `utility_meter` de Home Assistant, con ciclo semanal o mensual, usa una ventana
**rolling** — los últimos 7 o 30 días hacia atrás desde hoy, no "de lunes a hoy" ni "del día 1 a
hoy". Esto se comprobó en producción y es contraintuitivo: un sensor que dice "esta semana" en
realidad muestra una semana que no empieza en lunes.

Energy Ledger evita esto por diseño: los cuatro períodos (día, semana, mes, año) se calculan
siempre como calendario real —lunes 00:00 (ISO), día 1 00:00, 1 de enero 00:00, en la zona horaria
configurada de Home Assistant— y punto. En cada actualización se compara el inicio de período
"correcto" para el instante actual contra el que tenía guardado el acumulado; si no coinciden, se
cerró un límite de calendario, así que el acumulado viejo se archiva en `ultimo_periodo` y el nuevo
arranca en cero. No se reparte la contribución entre el período viejo y el nuevo (no hace falta esa
precisión), y si Home Assistant estuvo apagado y el límite se cruzó mientras tanto, el criterio es
el mismo: cero honesto, nunca un valor inventado para rellenar el hueco.

### Por qué no usa ningún helper genérico de HA

Ni plantillas, ni el helper "Integration - Riemann sum integral", ni `utility_meter`. Todo el
cálculo (una integración tipo Riemann por la izquierda: se toma la tarifa vigente en el último
evento y se multiplica por el tiempo transcurrido hasta el evento siguiente) y toda la persistencia
viven en código Python propio de esta integración, usando `homeassistant.helpers.storage.Store`
para que los acumulados del día/semana/mes/año en curso sobrevivan un reinicio de Home Assistant sin
depender de que un helper genérico restaure bien su estado — cosa que, en la versión de HA con la
que se detectó este problema, el helper de integral de Riemann no hacía siempre correctamente.

## Desarrollo

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest tests/
```

## Licencia

MIT.
