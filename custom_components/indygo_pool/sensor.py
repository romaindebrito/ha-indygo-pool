"""Sensor platform for Indygo Pool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import IndygoPoolDataUpdateCoordinator
from .entity import IndygoPoolEntity
from .models import IndygoSensorData


@dataclass
class IndygoSensorEntityDescription(SensorEntityDescription):
    """Class describing Indygo Pool sensor entities."""


SENSOR_TYPES: tuple[IndygoSensorEntityDescription, ...] = (
    # ---------- Pool / filtration ------------------------------------
    IndygoSensorEntityDescription(
        key="filtration_status",
        translation_key="filtration_status",
    ),
    IndygoSensorEntityDescription(
        key="temperature",
        translation_key="water_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    IndygoSensorEntityDescription(
        key="filtration_remaining_time",
        translation_key="filtration_remaining_time",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    # ---------- IPX core --------------------------------------------
    IndygoSensorEntityDescription(
        key="totalElectrolyseDuration",
        translation_key="electrolyzer_duration",
        native_unit_of_measurement=UnitOfTime.HOURS,
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    IndygoSensorEntityDescription(
        key="ipx_salt",
        translation_key="ipx_salt",
        native_unit_of_measurement="g/L",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    IndygoSensorEntityDescription(
        key="ph",
        translation_key="ph",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    IndygoSensorEntityDescription(
        key="ph_setpoint",
        translation_key="ph_setpoint",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    IndygoSensorEntityDescription(
        key="ph_mode",
        translation_key="ph_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    IndygoSensorEntityDescription(
        key="orp_setpoint",
        translation_key="orp_setpoint",
        native_unit_of_measurement="mV",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    IndygoSensorEntityDescription(
        key="production_setpoint",
        translation_key="production_setpoint",
        native_unit_of_measurement=PERCENTAGE,
    ),
    IndygoSensorEntityDescription(
        key="electrolyzer_mode",
        translation_key="electrolyzer_mode",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    IndygoSensorEntityDescription(
        key="boost_remaining_time",
        translation_key="boost_remaining_time",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    IndygoSensorEntityDescription(
        key="cell_voltage",
        translation_key="cell_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    IndygoSensorEntityDescription(
        key="electrolyse_remaining_percent",
        translation_key="electrolyse_remaining_percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    IndygoSensorEntityDescription(
        key="electrolyse_today",
        translation_key="electrolyse_today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    IndygoSensorEntityDescription(
        key="electrolyse_yesterday",
        translation_key="electrolyse_yesterday",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ---------- Probe inputs (pH/ORP/temp/level) — every module -----
    IndygoSensorEntityDescription(
        key="probe_temperature",
        translation_key="probe_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    IndygoSensorEntityDescription(
        key="probe_ph",
        translation_key="probe_ph",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    IndygoSensorEntityDescription(
        key="probe_orp",
        translation_key="probe_orp",
        native_unit_of_measurement="mV",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    IndygoSensorEntityDescription(
        key="water_level",
        translation_key="water_level",
        native_unit_of_measurement=UnitOfLength.CENTIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    # ---------- Diagnostics per module ------------------------------
    IndygoSensorEntityDescription(
        key="battery_level",
        translation_key="battery_level",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
    ),
    IndygoSensorEntityDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
    ),
    IndygoSensorEntityDescription(
        key="secondary_battery_voltage",
        translation_key="secondary_battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
    ),
    IndygoSensorEntityDescription(
        key="cellular_signal_quality",
        translation_key="cellular_signal_quality",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    IndygoSensorEntityDescription(
        key="last_radio_communication",
        translation_key="last_radio_communication",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    IndygoSensorEntityDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


# Sensor keys whose raw value needs a transform before being exposed in HA.
# Avoids touching the parser for what's purely a display concern.
_VALUE_TRANSFORMS: dict[str, callable] = {
    # Indygo encodes battery_voltage as 1/10 V (e.g. 85 -> 8.5 V).
    "battery_voltage": lambda v: v / 10.0 if isinstance(v, (int, float)) else v,
    # Cell voltage in mV — convert to V.
    "cell_voltage": lambda v: v / 1000.0 if isinstance(v, (int, float)) else v,
    # secondary_battery_voltage already in mV — keep as-is.
    # battery_level: enum 0..5, scale to percent for the BATTERY device class.
    "battery_level": (
        lambda v: int(round(v * 20)) if isinstance(v, (int, float)) and v <= 5 else v
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator: IndygoPoolDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[IndygoPoolSensor] = []

    if not coordinator.data:
        return

    desc_map = {desc.key: desc for desc in SENSOR_TYPES}

    # 1. Root Sensors
    for key, _ in coordinator.data.sensors.items():
        if key in desc_map:
            entities.append(
                IndygoPoolSensor(
                    coordinator=coordinator,
                    description=desc_map[key],
                )
            )

    # 2. Module Sensors
    for module_id, module in coordinator.data.modules.items():
        # Regular sensors
        for key in module.sensors:
            if key in desc_map:
                entities.append(
                    IndygoPoolSensor(
                        coordinator=coordinator,
                        description=desc_map[key],
                        module_id=module_id,
                    )
                )

        # Module-level status sensors (other than filtration_status)
        for index in module.pool_status:
            if index != "0" and index in desc_map:
                entities.append(
                    IndygoPoolSensor(
                        coordinator=coordinator,
                        description=desc_map[index],
                        module_id=module_id,
                    )
                )

    async_add_entities(entities)


class IndygoPoolSensor(IndygoPoolEntity, SensorEntity):
    """Indygo Pool Sensor class."""

    entity_description: IndygoSensorEntityDescription

    def __init__(
        self,
        coordinator: IndygoPoolDataUpdateCoordinator,
        description: IndygoSensorEntityDescription,
        module_id: str | None = None,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, module_id)
        self.entity_description = description
        self._attr_unique_id = self._build_unique_id(description.key)
        self.entity_id = (
            f"sensor.{self.device_name_slug}_{slugify(description.translation_key)}"
        )

    def _get_sensor_data(self) -> IndygoSensorData | None:
        """Resolve the sensor data from module or root sensors."""
        data = self.coordinator.data
        if not data:
            return None
        key = self.entity_description.key
        if self._module_id and self._module_id in data.modules:
            sensor = data.modules[self._module_id].sensors.get(key)
            if sensor:
                return sensor
        return data.sensors.get(key)

    @property
    def native_value(self) -> float | str | None:
        """Return the native value of the sensor."""
        sensor = self._get_sensor_data()
        if not sensor:
            return None
        value = sensor.value
        transform = _VALUE_TRANSFORMS.get(self.entity_description.key)
        if transform is not None and value is not None:
            try:
                return transform(value)
            except (TypeError, ValueError):
                return value
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the state attributes."""
        sensor = self._get_sensor_data()
        return sensor.extra_attributes if sensor else None
