# Energy Ledger

Integración de Home Assistant, instalable vía [HACS](https://hacs.xyz/), que lleva la cuenta de
**coste, compensación, ahorro por autoconsumo y balance neto** de tu consumo eléctrico — de la casa
entera y, opcionalmente, de circuitos individuales (un termo, una lavadora...) — a partir de
entidades que ya existen en tu instancia. No añade ningún add-on ni proceso externo: es una
integración pura que solo lee estados y persiste sus propios acumulados.

No controla tu inversor, tu batería, ni nada — eso sigue viviendo en tus automatizaciones. Energy
Ledger solo mira y cuenta.

## Qué necesitás antes de instalar

Dos entidades obligatorias, y hasta cuatro más opcionales — todas entidades que ya tengas en Home
Assistant, no hace falta crear nada nuevo:

- **Obligatorio.** Un sensor de **potencia neta de red**, en W (ej.
  `sensor.goodwe_meter_active_power_total`).
- **Obligatorio.** Un sensor de **precio de compra** en tiempo real, en `<moneda>/kWh` (ej.
  `EUR/kWh`).
- *Opcional.* Un sensor de **precio de venta de excedentes**, en las mismas unidades. Sin esto la
  integración solo trackea coste — no compensación ni balance.
- *Opcional.* Un sensor de **potencia de consumo TOTAL de la casa**, en W (no solo import/export de
  red). Habilita el sensor de **ahorro por autoconsumo** y, con circuitos configurados, el
  **reparto de coste entre circuitos concurrentes** (ver más abajo, es la pieza más importante de
  todo el diseño).
- *Opcional.* Un sensor de **contador real de energía importada**, en kWh (típicamente
  `state_class: total_increasing`, ej. el propio contador del inversor). Si lo tenés, el kWh de la
  casa sale de ahí exacto en vez de reconstruirse por potencia integrada.
- *Opcional, por cada circuito.* Un sensor de **contador real de energía** de ESE circuito, en kWh.
  Mismo mecanismo que el anterior pero a nivel circuito.

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
   - **Sensor de potencia de consumo total de la casa** (opcional): habilita el ahorro por
     autoconsumo y el reparto de coste entre circuitos.
   - **Sensor de contador real de energía importada** (opcional): kWh exacto de la casa en vez de
     potencia integrada.
4. Una vez creada la integración, podés añadir circuitos: en la tarjeta de la integración, "Añadir
   circuito" (subentrada), tantas veces como quieras. Cada uno pide un nombre libre (ej. "Termo"),
   su propio sensor de potencia en W y, opcionalmente, su propio sensor de contador real de
   energía en kWh.

Para editar cualquiera de estos campos más adelante (por ejemplo, agregar el sensor de consumo
total que no configuraste al principio, o el contador real de un circuito), usá **Reconfigurar**
desde la tarjeta de la integración (o el menú de los tres puntos de un circuito para reconfigurar
solo ese circuito) — no hace falta borrar y recrear nada, y el acumulado del período en curso no se
pierde. Podés editar, reconfigurar o borrar un circuito, o quitar la integración entera, desde la
propia UI de Home Assistant — no hace falta tocar YAML en ningún momento.

## Qué entidades crea

Por cada instalación (nodo casa/red), sensores de tipo `monetary`/`energy` con
`state_class: total`, uno por período — hoy, esta semana, este mes, este año:

- `sensor.<...>_coste_*` — siempre.
- `sensor.<...>_energia_*` — siempre, en kWh.
- `sensor.<...>_compensacion_*` y `sensor.<...>_balance_*` — solo si configuraste precio de venta.
- `sensor.<...>_ahorro_*` — solo si configuraste el sensor de consumo total de la casa.

Por cada circuito:

- `sensor.<...>_<circuito>_coste_*` — un circuito nunca "gana" plata, solo cuesta 0 o el precio
  real (nunca compensación ni balance por circuito).
- `sensor.<...>_<circuito>_energia_*` — en kWh.

Todos los sensores traen atributos para poder diagnosticar sin mirar logs:

- `ultimo_periodo`: el valor con el que cerró el período anterior (ej. el coste total del mes
  pasado, visible durante todo el mes en curso).
- `fecha_inicio_periodo`: desde cuándo corre el acumulado actual.
- `datos_incompletos_desde`: si la fuente de la que depende este sensor está `unavailable`/`unknown`
  ahora mismo, desde cuándo — `null` si no hay ningún hueco en curso. Cada sensor mira la fuente que
  le corresponde: coste/compensación/balance/ahorro miran su propia entidad de origen; energía mira
  su propio contador real cuando lo tiene configurado (ver `metodo` abajo).
- Solo en los sensores de **energía**: `metodo`, `contador_real` o `potencia_integrada` — de dónde
  sale el kWh de ESE sensor en particular (ver "kWh exacto desde contador real" más abajo).
- Solo en los sensores de **coste de un circuito**: `metodo_coste`, `repartido` o
  `binario_sin_repartir` — si el coste de ESE circuito está usando el reparto proporcional o el
  comportamiento binario original (ver "Reparto de coste entre circuitos concurrentes" más abajo).
  Nunca aparece en el coste del nodo casa/red: ese es exacto siempre, no hay nada que repartir ahí.

La unidad monetaria se toma automáticamente del sensor de precio de compra (lo que tenga antes de
la barra en su `unit_of_measurement`, ej. `EUR/kWh` → `EUR`) — no está hardcodeada a euros.

## Diagnósticos

Ajustes → Dispositivos y servicios → Energy Ledger → ⋮ → Descargar diagnósticos. El archivo trae la
configuración completa (sin datos sensibles: solo entity_id de tus propios sensores), el estado
interno del coordinator (acumulados de cada nodo, últimas tarifas calculadas, huecos de datos en
curso) y el estado/disponibilidad de cada entidad que la integración está mirando — útil para
resolver "¿por qué este número no cuadra?" sin acceso a la instancia real ni a los logs.

## Diseño

### La regla de los 0€

En cualquier instante, si la casa **no** está importando de la red (está exportando excedente o en
cero — según cómo esté configurada tu convención de signo), el precio efectivo de **todo** el
consumo de ese instante, tanto el total de la casa como el de cada circuito, es **0**. Se asume
autoconsumo solar/batería al 100%. Si la casa está importando, el precio efectivo es el de tu
tarifa de compra en ese momento.

El coste del nodo casa/red se calcula sobre la potencia de **import real**, no sobre el consumo
total — es lo que de verdad pagás. La compensación (solo para casa/red, nunca para un circuito) se
calcula sobre la potencia de **export real** por el precio de venta.

### Reparto de coste entre circuitos concurrentes

Sin el sensor de consumo total de la casa configurado, el coste de un circuito individual usa el
mismo gate binario que el resto: 0€ si la casa no importa, precio completo de compra si importa —
aplicado a la potencia ENTERA de ese circuito. Esto es simple pero puede sobreestimar mucho: si en
un instante dado la casa importa apenas 50W (un pico breve antes de que el solar cubra todo) pero
hay 3 circuitos corriendo con 2kW cada uno, **los 3 se cobran el precio completo por sus 2kW
enteros** — 6kW "facturados" cuando la red solo puso 50W. Sumado sobre todo un día de actividad
normal, el error compone y puede llegar a ser de un orden de magnitud (verificado en producción:
24x en una auditoría real).

Con el sensor de consumo total configurado, el import real se **reparte proporcionalmente** entre
los circuitos activos según lo que pesa cada uno sobre el consumo total medido en ese instante — la
suma de coste de TODOS los circuitos trackeados nunca supera el coste real de la casa. Si además la
suma de potencia de varios circuitos concurrentes supera levemente el consumo total medido (ruido
normal entre sensores que no leen exactamente en el mismo instante, cada uno un dispositivo
distinto), el reparto se normaliza para que la suma de todos dé como máximo el coste real, nunca
más — no alcanza con clampear cada circuito por separado contra el consumo total, hace falta mirar
a todos los circuitos del mismo ciclo en conjunto.

Sin ese sensor configurado (o si en un instante puntual da 0 pese a estar disponible), cada
circuito cae al comportamiento binario original — sobreestimación conocida, visible en el atributo
`metodo_coste` de cada sensor de coste de circuito.

### Ahorro por autoconsumo

Con el sensor de consumo total configurado, además del reparto de coste se calcula un sensor de
**ahorro**: lo que dejaste de pagar por generar tu propia energía. `autoconsumo_kw = max(consumo
total − import real, 0)` — la parte del consumo que NO vino de la red. El ahorro de cada instante es
ese autoconsumo valorizado a tu precio de compra, más lo que exportaste valorizado a tu precio de
venta (si configuraste uno). Solo existe para el nodo casa/red, no por circuito.

### kWh exacto desde contador real

Por defecto, el kWh de cada sensor de energía se reconstruye integrando potencia en el tiempo
(Riemann por la izquierda) — el mismo mecanismo que el coste, pero sin multiplicar por precio. Esto
arrastra un pequeño error de discretización (unos pocos puntos porcentuales, verificado en
producción) frente a un contador real, porque la potencia solo se conoce en los instantes en que el
sensor actualiza, no de forma continua.

Si tenés un contador de energía real (kWh acumulados, típicamente `state_class: total_increasing`)
para la casa o para un circuito en particular, configuralo y ese sensor de energía pasa a usar el
delta del contador real en vez de reconstruir nada — exacto, sin el error de integración. Cada
sensor de energía es independiente: podés tener el contador real solo en un circuito y potencia
integrada en el resto, o en la casa y no en los circuitos. El atributo `metodo` de cada sensor
indica cuál está usando. Si el contador real se reinicia (el dispositivo se reinició y volvió a
cero), la integración lo detecta y asume que arrancó de 0 en vez de restar un delta negativo sin
sentido.

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
(con guardado debounced tras unos segundos de inactividad, y un flush inmediato al descargar la
integración) para que los acumulados del día/semana/mes/año en curso sobrevivan un reinicio de Home
Assistant sin depender de que un helper genérico restaure bien su estado — cosa que, en la versión
de HA con la que se detectó este problema, el helper de integral de Riemann no hacía siempre
correctamente.

## Desarrollo

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest tests/
```

## Licencia

MIT.
