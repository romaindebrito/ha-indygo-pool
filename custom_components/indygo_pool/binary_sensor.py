"""Binary sensor platform for Indygo Pool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import IndygoPoolDataUpdateCoordinator
from .entity import IndygoPoolEntity


@dataclass
class IndygoBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Class describing Indygo Pool binary sensor entities."""

    sub_path: str | None = None
    is_pool_status: bool = False
    is_inverted: bool = False
    # When set, value is read from module.raw_data at top-level (sub_path None).
    # When ``is_module_root`` is True, the binary sensor is created for every
    # module that has the field, not only the IPX.
    is_module_root: bool = False


BINARY_SENSOR_TYPES: tuple[IndygoBinarySensorEntityDescription, ...] = (
    # ----- Connectivity (every module) -------------------------------
    IndygoBinarySensorEntityDescription(
        key="isOnline",
        translation_key="is_online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ----- IPX deviceState flags -------------------------------------
    IndygoBinarySensorEntityDescription(
        key="shutterEntry",
        translation_key="shutter",
        device_class=BinarySensorDeviceClass.WINDOW,
        sub_path="ipxData.deviceState",
        is_inverted=True,
    ),
    IndygoBinarySensorEntityDescription(
        key="flowEntry",
        translation_key="flow",
        device_class=BinarySensorDeviceClass.PROBLEM,
        sub_path="ipxData.deviceState",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    IndygoBinarySensorEntityDescription(
        key="cmdEntry",
        translation_key="cmd_entry",
        sub_path="ipxData.deviceState",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    IndygoBinarySensorEntityDescription(
        key="canPhEntry",
        translation_key="can_ph_entry",
        sub_path="ipxData.deviceState",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    IndygoBinarySensorEntityDescription(
        key="boostEnabled",
        translation_key="boost_enabled",
        sub_path="ipxData.deviceState",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    IndygoBinarySensorEntityDescription(
        key="testProd",
        translation_key="test_prod",
        sub_path="ipxData.deviceState",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    IndygoBinarySensorEntityDescription(
        key="pHInjection",
        translation_key="ph_injection",
        sub_path="ipxData.deviceState",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    IndygoBinarySensorEntityDescription(
        key="cellPolaruty",
        translation_key="cell_polarity",
        sub_path="ipxData.deviceState",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    IndygoBinarySensorEntityDescription(
        key="prodStatus",
        translation_key="production_status",
        sub_path="ipxData.deviceState",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # ----- Filtration runner (pool[0]) -------------------------------
    IndygoBinarySensorEntityDescription(
        key="0",
        translation_key="filtration",
        device_class=BinarySensorDeviceClass.RUNNING,
        is_pool_status=True,
    ),
    # ----- Per-module root flags (battery, alarms, frost-free) -------
    IndygoBinarySensorEntityDescription(
        key="batteryLow",
        translation_key="battery_low",
        device_class=BinarySensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_module_root=True,
    ),
    IndygoBinarySensorEntityDescription(
        key="batteryAlarm",
        translation_key="battery_alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_module_root=True,
    ),
    IndygoBinarySensorEntityDescription(
        key="clockAlarm",
        translation_key="clock_alarm",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_module_root=True,
    ),
    IndygoBinarySensorEntityDescription(
        key="isFrostFreeEnabled",
        translation_key="frost_free_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_module_root=True,
    ),
    IndygoBinarySensorEntityDescription(
        key="waitingForStatusUpdate",
        translation_key="waiting_for_status_update",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_module_root=True,
    ),
    # Wintered status — sourced from outputs[].ipxData.isWintered
    # (resolved by the parser into module.raw_data via _is_wintered cache;
    # falls back to outputs scanning at read time).
    IndygoBinarySensorEntityDescription(
        key="isWintered",
        translation_key="is_wintered",
        entity_category=EntityCategory.DIAGNOSTIC,
        sub_path="__outputs_ipxData__",  # special marker handled in is_on
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary_sensor platform."""
    coordinator: IndygoPoolDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[IndygoPoolBinarySensor] = []

    if not coordinator.data:
        return

    desc_map = {desc.key: desc for desc in BINARY_SENSOR_TYPES}

    for module_id, module in coordinator.data.modules.items():
        # General Module Sensors
        if "isOnline" in module.raw_data:
            entities.append(
                IndygoPoolBinarySensor(
                    coordinator=coordinator,
                    description=desc_map["isOnline"],
                    module_id=module_id,
                    module_name=module.type.upper() if module.type else "Unknown",
                )
            )

        # Per-module root flags (battery low/alarm, clock alarm, frost free,
        # waiting for status update, …) — created for every module that has
        # the corresponding field.
        for desc in BINARY_SENSOR_TYPES:
            if desc.is_module_root and desc.key in module.raw_data:
                entities.append(
                    IndygoPoolBinarySensor(
                        coordinator=coordinator,
                        description=desc,
                        module_id=module_id,
                    )
                )

        # IPX Specific Sensors (deviceState flags + isWintered)
        if module.type == "ipx" and "ipxData" in module.raw_data:
            ipx_data = module.raw_data["ipxData"]
            if "deviceState" in ipx_data:
                device_state = ipx_data["deviceState"]
                for key in device_state:
                    if (
                        key in desc_map
                        and desc_map[key].sub_path == "ipxData.deviceState"
                    ):
                        entities.append(
                            IndygoPoolBinarySensor(
                                coordinator=coordinator,
                                description=desc_map[key],
                                module_id=module_id,
                            )
                        )
            # isWintered lives inside outputs[].ipxData
            outputs = module.raw_data.get("outputs") or []
            if any(
                isinstance(o, dict)
                and isinstance(o.get("ipxData"), dict)
                and "isWintered" in o["ipxData"]
                for o in outputs
            ):
                entities.append(
                    IndygoPoolBinarySensor(
                        coordinator=coordinator,
                        description=desc_map["isWintered"],
                        module_id=module_id,
                    )
                )

        # Module-level status sensors (Filtration, etc)
        for index in module.pool_status:
            if index == "0":
                entities.append(
                    IndygoPoolBinarySensor(
                        coordinator=coordinator,
                        description=desc_map["0"],
                        module_id=module_id,
                    )
                )

    # Root Level Pool Status Sensors (Fallback if not moved to module)
    for index in coordinator.data.pool_status:
        if index == "0":
            entities.append(
                IndygoPoolBinarySensor(
                    coordinator=coordinator,
                    description=desc_map["0"],
                )
            )

    async_add_entities(entities)


class IndygoPoolBinarySensor(IndygoPoolEntity, BinarySensorEntity):
    """Indygo Pool binary_sensor class."""

    entity_description: IndygoBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: IndygoPoolDataUpdateCoordinator,
        description: IndygoBinarySensorEntityDescription,
        module_id: str | None = None,
        module_name: str | None = None,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, module_id)
        self.entity_description = description

        if description.key == "isOnline" and module_name:
            self._attr_translation_placeholders = {"module": module_name}

        self._attr_unique_id = self._build_unique_id(description.key)
        suffix = slugify(description.translation_key)
        self.entity_id = f"binary_sensor.{self.device_name_slug}_{suffix}"

    def _get_pool_status(self) -> dict:
        """Resolve the correct pool_status dict (module-level or root)."""
        if self._module_id and self._module_id in self.coordinator.data.modules:
            return self.coordinator.data.modules[self._module_id].pool_status
        return self.coordinator.data.pool_status

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary_sensor is on."""
        desc = self.entity_description

        if desc.is_pool_status:
            target_status = self._get_pool_status()
            if desc.key in target_status:
                val = target_status[desc.key].value
                if val is not None:
                    try:
                        return float(val) == 1.0
                    except (ValueError, TypeError):
                        pass
            return None

        if self._module_id in self.coordinator.data.modules:
            module = self.coordinator.data.modules[self._module_id]

            # Special marker: isWintered lives inside outputs[].ipxData;
            # we OR across the outputs so a single wintered output flips
            # the binary sensor on.
            if desc.sub_path == "__outputs_ipxData__":
                outputs = module.raw_data.get("outputs") or []
                vals: list[bool] = []
                for out in outputs:
                    if not isinstance(out, dict):
                        continue
                    ipx = out.get("ipxData")
                    if isinstance(ipx, dict) and desc.key in ipx:
                        v = ipx[desc.key]
                        if isinstance(v, bool):
                            vals.append(v)
                        elif v is not None:
                            try:
                                vals.append(float(v) == 1.0)
                            except (TypeError, ValueError):
                                pass
                if not vals:
                    return None
                result = any(vals)
                return not result if desc.is_inverted else result

            target = module.raw_data
            if desc.sub_path:
                for path_part in desc.sub_path.split("."):
                    if not isinstance(target, dict):
                        target = {}
                        break
                    target = target.get(path_part, {})

            val = target.get(desc.key) if isinstance(target, dict) else None

            if isinstance(val, bool):
                return not val if desc.is_inverted else val

            if val is not None:
                try:
                    is_true = float(val) == 1.0
                    return not is_true if desc.is_inverted else is_true
                except (ValueError, TypeError):
                    pass

        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the state attributes."""
        desc = self.entity_description
        if desc.is_pool_status:
            target_status = self._get_pool_status()
            if desc.key in target_status:
                return target_status[desc.key].extra_attributes
        return None
