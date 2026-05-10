"""Parser for Indygo Pool data."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from .const import (
    INPUT_TYPE_ORP,
    INPUT_TYPE_PH,
    INPUT_TYPE_TEMPERATURE,
    INPUT_TYPE_WATER_LEVEL,
    IPX_OUTPUT_ELECTROLYSER,
    IPX_OUTPUT_PH,
    PROGRAM_TYPE_FILTRATION,
)
from .models import IndygoModuleData, IndygoPoolData, IndygoSensorData

_LOGGER = logging.getLogger(__name__)


# Backwards-compatible alias used by older tests.
IPX_PH_SENSOR_TYPE = INPUT_TYPE_PH

# Map "input.type" -> sensor key + display unit, used by
# ``_parse_module_inputs_typed`` to expose probe values from any module
# (IPX, lr-mas, lr-niv) in a uniform way.
INPUT_TYPE_SENSORS: dict[int, str] = {
    INPUT_TYPE_TEMPERATURE: "probe_temperature",
    INPUT_TYPE_PH: "probe_ph",
    INPUT_TYPE_ORP: "probe_orp",
    INPUT_TYPE_WATER_LEVEL: "water_level",
}

# Sentinel value used by Indygo for "no measurement / sensor absent".
_INDYGO_INT_MAX = 2147483647

# Module types that legitimately expose probe inputs (pH/ORP/temp/level).
# Excluding ``lr-pc*`` and ``lr-mb*`` avoids polluting Pool Command and
# Gateway devices with empty probe entities — they have ``inputs[]`` for
# other purposes (hardware diagnostics) but no live probe values.
_PROBE_MODULE_TYPE_PREFIXES: tuple[str, ...] = ("ipx", "lr-mas", "lr-niv", "lr-ps")


def _get_nested(obj: dict | list | None, *keys: str) -> Any:
    """Safely traverse nested dicts/lists by key or index."""
    for k in keys:
        if not isinstance(obj, (dict, list)):
            return None
        if isinstance(obj, list):
            try:
                obj = obj[int(k)]
            except (IndexError, ValueError):
                return None
        else:
            obj = obj.get(k)
    return obj


class IndygoParser:
    """Parser for Indygo Pool data."""

    # ------------------------------------------------------------------
    # Hardware ID resolution (from module list, no HTML needed)
    # ------------------------------------------------------------------

    @staticmethod
    def _name_suffix(name: str | None) -> str | None:
        """Extract the hardware suffix from a module name.

        Module names follow the pattern ``<MODEL>-<HEX_ID>`` (e.g.
        ``LRPCVS2-0C91F2``, ``IPX-A3EA4F``, ``LRMB10-0DB093``).  This
        helper returns the suffix, which is the device short id used by
        the API in URLs.
        """
        if not name:
            return None
        parts = str(name).rsplit("-", 1)
        if len(parts) == 2 and parts[1]:
            return parts[1]
        return None

    # Public alias so api.py can compute short ids without reaching into
    # a private method.
    name_suffix = _name_suffix

    def _extract_device_ids(self, lr_pc: dict) -> tuple[str | None, str | None]:
        """Extract device short ID and relay ID from lr-pc module."""
        device_short_id = self._name_suffix(lr_pc.get("name"))
        if not device_short_id:
            serial = lr_pc.get("serialNumber") or ""
            device_short_id = serial[-6:] if serial else None

        relay_id = lr_pc.get("relay") or device_short_id
        return device_short_id, relay_id

    def _resolve_lr_pc(
        self, modules: list[dict]
    ) -> tuple[str | None, str | None, str | None]:
        """Resolve IDs from lr-pc + gateway modules.

        Accepts any module whose ``type`` starts with ``lr-pc`` (covers
        ``lr-pc``, ``lr-pc-vs1``, ``lr-pc-vs2``, ...) and any gateway
        whose ``type`` starts with ``lr-mb`` (``lr-mb-10``, ...).  When
        the gateway lacks a ``serialNumber`` field, falls back to the
        hardware suffix of its name.
        """
        gateway = next(
            (m for m in modules if str(m.get("type", "")).startswith("lr-mb")),
            None,
        )
        lr_pc = next(
            (m for m in modules if str(m.get("type", "")).startswith("lr-pc")),
            None,
        )

        if not lr_pc:
            return None, None, None

        if not gateway:
            gateway = lr_pc

        pool_address = gateway.get("serialNumber") or self._name_suffix(
            gateway.get("name")
        )
        device_short_id, relay_id = self._extract_device_ids(lr_pc)
        _LOGGER.debug(
            "Resolved lr-pc: type=%s name=%s -> pool_address=%s "
            "device_short_id=%s relay_id=%s",
            lr_pc.get("type"),
            lr_pc.get("name"),
            pool_address,
            device_short_id,
            relay_id,
        )
        return pool_address, device_short_id, relay_id

    def _resolve_ipx(
        self, modules: list[dict]
    ) -> tuple[str | None, str | None, str | None]:
        """Resolve IDs from IPX module as fallback.

        When ``serialNumber`` / ``ipxRelay`` are missing in the API
        payload, falls back to the hardware suffix of the module name
        (``IPX-A3EA4F`` -> ``A3EA4F``).
        """
        ipx = next((m for m in modules if m.get("type") == "ipx"), None)
        if not ipx:
            return None, None, None

        suffix = self._name_suffix(ipx.get("name"))
        pool_address = ipx.get("serialNumber") or suffix
        device_short_id = ipx.get("ipxRelay") or suffix
        relay_id = device_short_id
        _LOGGER.debug(
            "Resolved ipx: name=%s serialNumber=%s ipxRelay=%s "
            "-> pool_address=%s device_short_id=%s",
            ipx.get("name"),
            ipx.get("serialNumber"),
            ipx.get("ipxRelay"),
            pool_address,
            device_short_id,
        )
        return pool_address, device_short_id, relay_id

    def resolve_hardware_ids(
        self, modules: list[dict]
    ) -> tuple[str | None, str | None, str | None]:
        """Resolve pool_address, device_short_id and relay_id from modules.

        Tries lr-pc (any sub-variant) first, falls back to IPX.  Each
        identifier is resolved independently so that a partial match in
        lr-pc can be completed by the IPX resolver.

        Returns:
            Tuple of (pool_address, device_short_id, relay_id)
        """
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Resolving hardware IDs from %d modules: %s",
                len(modules),
                [
                    {
                        "id": m.get("id"),
                        "type": m.get("type"),
                        "name": m.get("name"),
                        "serialNumber": m.get("serialNumber"),
                        "relay": m.get("relay"),
                        "ipxRelay": m.get("ipxRelay"),
                        "pool": m.get("pool"),
                    }
                    for m in modules
                ],
            )

        pool_address, device_short_id, relay_id = self._resolve_lr_pc(modules)

        if not pool_address or not device_short_id:
            ipx_pa, ipx_dsi, ipx_relay = self._resolve_ipx(modules)
            pool_address = pool_address or ipx_pa
            device_short_id = device_short_id or ipx_dsi
            relay_id = relay_id or ipx_relay

        if not pool_address:
            _LOGGER.error(
                "No compatible module (lr-pc* or ipx) yielded a "
                "pool_address from %d modules.",
                len(modules),
            )
        return pool_address, device_short_id, relay_id

    # ------------------------------------------------------------------
    # Main data parser
    # ------------------------------------------------------------------

    @staticmethod
    def _find_filtration_module(
        pool_data: IndygoPoolData,
    ) -> IndygoModuleData | None:
        """Find the module responsible for filtration.

        Looks up modules carrying a filtration program first, then any
        module whose type starts with ``lr-pc`` (covers lr-pc, lr-pc-vs1,
        lr-pc-vs2, ...).
        """
        return next(
            (m for m in pool_data.modules.values() if m.filtration_program),
            next(
                (
                    m
                    for m in pool_data.modules.values()
                    if str(m.type or "").startswith("lr-pc")
                ),
                None,
            ),
        )

    def parse_data(
        self,
        json_data: dict,
        pool_id: str,
        pool_address: str,
        relay_id: str,
    ) -> IndygoPoolData:
        """Parse the API response into a structured IndygoPoolData object."""
        pool_data = IndygoPoolData(
            pool_id=pool_id, address=pool_address, relay_id=relay_id, raw_data=json_data
        )

        # 1. Modules Data
        self._parse_modules(json_data, pool_data)

        # 2. IPX-specific parsing (electrolyser, salt, ORP, boost, etc.)
        self._parse_scraped_ipx(json_data, pool_data)

        # 3. Probe inputs (pH/ORP/temperature/water level) for every module
        #    that exposes them — this catches the IPX *and* additional
        #    sensors like lr-mas (LRPS) or lr-niv (water level).
        self._parse_module_inputs_typed(pool_data)

        # 3bis. Live values from per-module status endpoints (lr-mas,
        #    lr-niv...).  Those modules don't expose ``inputs[].lastValue``
        #    but their ``sensorState`` (fetched from
        #    ``/v1/module/<gw>/status/<short_id>``) contains the actual
        #    pH/ORP/temperature/water-level values.
        self._parse_module_statuses(json_data, pool_data)

        # 4. Per-module diagnostics (batteries, signal, last radio com,
        #    alarms, frost-free, hivernage flags).
        self._parse_module_diagnostics(pool_data)

        # 5. Main Pool Data — resolve filtration module once
        filt_module = self._find_filtration_module(pool_data)
        self._parse_root_sensors(json_data, pool_data, filt_module)
        self._parse_sensor_state(json_data, pool_data, filt_module)
        self._parse_pool_status_list(json_data, pool_data, filt_module)

        return pool_data

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _minutes_to_time(minutes: int) -> str:
        """Convert minutes since midnight to HH:MM string."""
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours:02d}:{mins:02d}"

    @staticmethod
    def _parse_remaining_time(time_str: str) -> int | None:
        """Parse remaining time string 'HH:MM' into total minutes."""
        try:
            parts = time_str.split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _parse_dialog_timestamp(raw: str | None) -> datetime | None:
        """Parse dialogTimeStamp (ISO 8601) into a timezone-aware datetime."""
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None

    def _build_schedule_attributes(
        self,
        filt_module: IndygoModuleData,
        temp_ref: int | None,
        dialog_ts: datetime | None,
    ) -> dict:
        """Build filtration schedule attributes from temperatureSchedules."""
        if temp_ref is None or not filt_module.filtration_program:
            return {}

        schedules = filt_module.filtration_program.get("temperatureSchedules", [])
        if not schedules:
            return {}

        thresholds = schedules[0].get("thresholds", [])
        if not isinstance(thresholds, list) or temp_ref >= len(thresholds):
            return {}

        windows = thresholds[temp_ref]
        if not windows:
            return {}

        first = windows[0]
        start_min = first.get("start")
        end_min = first.get("end")
        if start_min is None or end_min is None:
            return {}

        schedule_start = self._minutes_to_time(start_min)
        schedule_end = self._minutes_to_time(end_min)
        if dialog_ts:
            base_date = dialog_ts.replace(hour=0, minute=0, second=0, microsecond=0)
            schedule_start = base_date.replace(
                hour=start_min // 60, minute=start_min % 60
            ).isoformat()
            schedule_end = base_date.replace(
                hour=min(end_min // 60, 23), minute=end_min % 60
            ).isoformat()

        return {
            "schedule_start": schedule_start,
            "schedule_end": schedule_end,
            "schedule_duration_minutes": sum(
                w.get("end", 0) - w.get("start", 0) for w in windows
            ),
            "schedule_windows": [
                {
                    "start": self._minutes_to_time(w["start"]),
                    "end": self._minutes_to_time(w["end"]),
                }
                for w in windows
                if "start" in w and "end" in w
            ],
        }

    # ------------------------------------------------------------------
    # Parsing sub-sections
    # ------------------------------------------------------------------

    def _parse_pool_status_list(
        self,
        json_data: dict,
        pool_data: IndygoPoolData,
        filt_module: IndygoModuleData | None = None,
    ) -> None:
        """Parse 'pool' list which contains status for Filtration, etc."""
        if "pool" not in json_data or not isinstance(json_data["pool"], list):
            return
        target_status = (
            filt_module.pool_status if filt_module else pool_data.pool_status
        )

        for item in json_data["pool"]:
            idx = item.get("index")
            val = item.get("value")

            if idx == 0:
                temp_ref = item.get("tempRef")
                remaining_time = item.get("time")

                extra_attributes = {
                    "info": item.get("info"),
                    "time": remaining_time,
                    "tempRef": temp_ref,
                }

                if filt_module:
                    dialog_ts = self._parse_dialog_timestamp(
                        json_data.get("dialogTimeStamp")
                    )
                    extra_attributes.update(
                        self._build_schedule_attributes(
                            filt_module, temp_ref, dialog_ts
                        )
                    )

                target_status["0"] = IndygoSensorData(
                    key="filtration_status",
                    value=val,
                    extra_attributes=extra_attributes,
                )

                if remaining_time and filt_module:
                    remaining_minutes = self._parse_remaining_time(remaining_time)
                    if remaining_minutes is not None:
                        filt_module.sensors["filtration_remaining_time"] = (
                            IndygoSensorData(
                                key="filtration_remaining_time",
                                value=remaining_minutes,
                            )
                        )

    def _parse_root_sensors(
        self,
        json_data: dict,
        pool_data: IndygoPoolData,
        filt_module: IndygoModuleData | None = None,
    ) -> None:
        """Parse root level sensors."""
        root_sensors_map = {
            "temperature": {
                "attributes": {"temperatureTime": "last_measurement_time"},
            },
        }

        target_sensors = filt_module.sensors if filt_module else pool_data.sensors

        for key, config in root_sensors_map.items():
            if key in json_data and json_data[key] is not None:
                extra_attributes = {}
                attr_map = config.get("attributes", {})
                for source_key, target_key in attr_map.items():
                    if source_key in json_data:
                        extra_attributes[target_key] = json_data[source_key]

                target_sensors[key] = IndygoSensorData(
                    key=key,
                    value=json_data[key],
                    extra_attributes=extra_attributes,
                )

    def _parse_sensor_state(
        self,
        json_data: dict,
        pool_data: IndygoPoolData,
        filt_module: IndygoModuleData | None = None,
    ) -> None:
        """Parse sensorState (legacy/generic list)."""
        if "sensorState" not in json_data or not isinstance(
            json_data["sensorState"], list
        ):
            return

        target_sensors = filt_module.sensors if filt_module else pool_data.sensors

        for sensor_item in json_data["sensorState"]:
            idx = sensor_item.get("index")
            val = sensor_item.get("value")
            if idx == 0 and val is not None:
                temp_c = val / 100.0
                if "temperature" in target_sensors:
                    target_sensors["temperature"].value = temp_c
                else:
                    target_sensors["temperature"] = IndygoSensorData(
                        key="temperature",
                        value=temp_c,
                    )

    def _parse_modules(
        self,
        json_data: dict,
        pool_data: IndygoPoolData,
    ) -> None:
        """Parse modules list."""
        if "modules" not in json_data:
            return
        for module in json_data["modules"]:
            m_id = module.get("id")
            m_type = module.get("type", "unknown")
            m_name = module.get("name", f"Module {m_id}")

            indygo_module = IndygoModuleData(
                id=str(m_id), type=m_type, name=m_name, raw_data=module
            )

            # IPX Data
            if m_type == "ipx" and "ipxData" in module:
                ipx_data = module["ipxData"]
                if "totalElectrolyseDuration" in ipx_data:
                    indygo_module.sensors["totalElectrolyseDuration"] = (
                        IndygoSensorData(
                            key="totalElectrolyseDuration",
                            value=ipx_data["totalElectrolyseDuration"],
                        )
                    )

            # Programs
            programs = module.get("programs", [])
            if programs:
                indygo_module.programs = programs
                for prog in programs:
                    if (
                        "programCharacteristics" in prog
                        and prog["programCharacteristics"].get("programType")
                        == PROGRAM_TYPE_FILTRATION
                    ):
                        indygo_module.filtration_program = prog
                        break

            pool_data.modules[str(m_id)] = indygo_module

    def _parse_scraped_ipx(self, json_data: dict, pool_data: IndygoPoolData) -> None:
        """Parse ipx_module data (electrolyser, salt, ORP, boost, etc.).

        Extracts every IPX-specific field from ``ipx_module`` (which is a
        copy of the IPX module enriched server-side).  Generic probe
        inputs (pH/ORP/temperature) are handled by
        ``_parse_module_inputs_typed`` so we don't duplicate them here.
        """
        if "ipx_module" not in json_data:
            return

        ipx_mod = json_data["ipx_module"]
        outputs = ipx_mod.get("outputs", [])
        ipx_data = ipx_mod.get("ipxData", {}) or {}

        ipx_module = next(
            (m for m in pool_data.modules.values() if m.type == "ipx"), None
        )
        target_sensors = ipx_module.sensors if ipx_module else pool_data.sensors

        # ----- outputs[IPX_OUTPUT_PH] -> pH-related setpoints --------
        ph_set = _get_nested(outputs, IPX_OUTPUT_PH, "ipxData", "pHSetpoint")
        if ph_set is not None:
            target_sensors["ph_setpoint"] = IndygoSensorData(
                key="ph_setpoint", value=ph_set
            )
        ph_mode = _get_nested(outputs, IPX_OUTPUT_PH, "ipxData", "pHMode")
        if ph_mode is not None:
            target_sensors["ph_mode"] = IndygoSensorData(key="ph_mode", value=ph_mode)

        # ----- outputs[IPX_OUTPUT_ELECTROLYSER] -> salt/ORP/boost ----
        salt = _get_nested(outputs, IPX_OUTPUT_ELECTROLYSER, "ipxData", "saltValue")
        if salt is not None:
            target_sensors["ipx_salt"] = IndygoSensorData(key="ipx_salt", value=salt)

        orp_set = _get_nested(
            outputs, IPX_OUTPUT_ELECTROLYSER, "ipxData", "orpSetpoint"
        )
        if orp_set is not None:
            target_sensors["orp_setpoint"] = IndygoSensorData(
                key="orp_setpoint", value=orp_set
            )

        prod_set = _get_nested(
            outputs, IPX_OUTPUT_ELECTROLYSER, "ipxData", "percentageSetpoint"
        )
        if prod_set is not None:
            target_sensors["production_setpoint"] = IndygoSensorData(
                key="production_setpoint",
                value=prod_set,
            )

        # The API renamed ``electrolyzerMode`` to ``controllerMode`` at
        # some point — fall back to either to stay compatible with both.
        elec_mode = _get_nested(
            outputs, IPX_OUTPUT_ELECTROLYSER, "ipxData", "controllerMode"
        )
        if elec_mode is None:
            elec_mode = _get_nested(
                outputs, IPX_OUTPUT_ELECTROLYSER, "ipxData", "electrolyzerMode"
            )
        if elec_mode is not None:
            target_sensors["electrolyzer_mode"] = IndygoSensorData(
                key="electrolyzer_mode",
                value=elec_mode,
            )

        boost_remaining = _get_nested(
            outputs, IPX_OUTPUT_ELECTROLYSER, "ipxData", "boostRemainingTime"
        )
        if boost_remaining is not None:
            target_sensors["boost_remaining_time"] = IndygoSensorData(
                key="boost_remaining_time", value=boost_remaining
            )

        # ----- ipxData (root) -> diagnostic / gauge sensors ----------
        cell_voltage = ipx_data.get("cellVoltage")
        if cell_voltage is not None:
            target_sensors["cell_voltage"] = IndygoSensorData(
                key="cell_voltage", value=cell_voltage
            )

        elec_pct = ipx_data.get("remainingTimeElectrolyseDurationInPercent")
        if elec_pct is not None:
            target_sensors["electrolyse_remaining_percent"] = IndygoSensorData(
                key="electrolyse_remaining_percent", value=elec_pct
            )

        elec_today = _get_nested(
            ipx_data, "totalElectrolyseDurationCurrent", "value"
        )
        if elec_today is not None:
            target_sensors["electrolyse_today"] = IndygoSensorData(
                key="electrolyse_today",
                value=elec_today,
                extra_attributes={
                    "date": _get_nested(
                        ipx_data, "totalElectrolyseDurationCurrent", "date"
                    )
                },
            )

        elec_yesterday = _get_nested(
            ipx_data, "totalElectrolyseDurationTheDayBefore", "value"
        )
        if elec_yesterday is not None:
            target_sensors["electrolyse_yesterday"] = IndygoSensorData(
                key="electrolyse_yesterday",
                value=elec_yesterday,
                extra_attributes={
                    "date": _get_nested(
                        ipx_data, "totalElectrolyseDurationTheDayBefore", "date"
                    )
                },
            )

        # totalElectrolyseDuration was already exposed by _parse_modules
        # (as ``totalElectrolyseDuration`` key).  We expose the same value
        # under a snake-case key for consistency in the sensor platform.
        total_elec = ipx_data.get("totalElectrolyseDuration")
        if total_elec is not None and "totalElectrolyseDuration" not in target_sensors:
            target_sensors["totalElectrolyseDuration"] = IndygoSensorData(
                key="totalElectrolyseDuration", value=total_elec
            )

    # ------------------------------------------------------------------
    # Generic probe inputs (pH / ORP / temperature / water level)
    # ------------------------------------------------------------------

    def _parse_module_inputs_typed(self, pool_data: IndygoPoolData) -> None:
        """Expose pH/ORP/temperature/water-level inputs from every module.

        Indygo encodes probe values in ``module.inputs[].lastValue`` using a
        type-based discriminator (5=temp, 6=pH, 7=ORP, 33=level).  This
        method walks every module and stores those values in the module's
        sensors dict so they can be turned into per-device entities.

        The IPX exposes pH and ORP, the optional ``lr-mas`` (LRPS) probe
        exposes temperature + pH + ORP, the ``lr-niv`` exposes a water
        level.  We treat them all the same way — distinct entities are
        created later because each module is a distinct HA device.
        """
        for module in pool_data.modules.values():
            mod_type = str(module.type or "")
            # Only expose probe inputs for actual probe-bearing modules
            # (IPX electrolyser, dedicated probe sensors, water level).
            if not any(mod_type.startswith(p) for p in _PROBE_MODULE_TYPE_PREFIXES):
                continue

            inputs = module.raw_data.get("inputs") or []
            if not isinstance(inputs, list):
                continue

            for inp in inputs:
                inp_type = inp.get("type")
                key = INPUT_TYPE_SENSORS.get(inp_type)
                if key is None:
                    continue

                last_val = inp.get("lastValue") or {}
                value = last_val.get("value")
                if value is None or value == _INDYGO_INT_MAX:
                    # Module is sleeping or sensor not connected — still
                    # emit an unavailable sensor by registering the key
                    # without a value, so HA shows the entity but as
                    # ``unavailable`` until the module reports.
                    if key not in module.sensors:
                        module.sensors[key] = IndygoSensorData(
                            key=key,
                            value=None,
                            extra_attributes={
                                "input_type": inp_type,
                                "input_index": inp.get("index"),
                            },
                        )
                    continue

                extra: dict[str, Any] = {
                    "input_type": inp_type,
                    "input_index": inp.get("index"),
                    "last_measurement_time": last_val.get("date"),
                }
                last_calibrated = inp.get("lastCalibratedAt")
                if last_calibrated:
                    extra["last_calibrated_at"] = last_calibrated

                module.sensors[key] = IndygoSensorData(
                    key=key,
                    value=value,
                    extra_attributes=extra,
                )

                # Backwards compatibility: the IPX pH was previously
                # exposed under the bare ``ph`` key (consumed by the
                # legacy IPX sensor description).  Keep mirroring it so
                # existing dashboards keep working.
                if module.type == "ipx" and inp_type == INPUT_TYPE_PH:
                    module.sensors["ph"] = IndygoSensorData(
                        key="ph", value=value, extra_attributes=extra
                    )

    # ------------------------------------------------------------------
    # Per-module live status (sensorState from /v1/module/<gw>/status/...)
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_sensor_state_value(value: float, input_type: int | None):
        """Apply the per-type transform on a raw sensorState value.

        - temperature (5) and pH (6) are stored as ``x*100`` (e.g. 2975
          for 29.75°C, 727 for pH 7.27).
        - ORP (7) and water level (33) are stored raw.
        """
        if value is None or value == _INDYGO_INT_MAX:
            return None
        if input_type in (INPUT_TYPE_TEMPERATURE, INPUT_TYPE_PH):
            return round(value / 100.0, 2)
        return value

    def _parse_module_statuses(
        self, json_data: dict, pool_data: IndygoPoolData
    ) -> None:
        """Decode ``module_statuses`` (per-module /status payloads).

        Each entry contains ``sensorState[]`` whose indices map to the
        module's ``inputs[]`` (sensorState[i] -> input.index == i+1).
        We decode the value using the input's ``type`` so the resulting
        ``module.sensors[probe_*]`` carries human-readable values.
        """
        statuses = json_data.get("module_statuses")
        if not isinstance(statuses, dict):
            return

        for module_id, status in statuses.items():
            module = pool_data.modules.get(str(module_id))
            if module is None or not isinstance(status, dict):
                continue

            ts = status.get("dialogTimeStamp")

            # 1. Battery (primary + secondary "sps")
            bat = status.get("battery")
            if bat is not None and bat != _INDYGO_INT_MAX:
                # Override or create — /status battery is fresher than the
                # one cached in module.raw_data.
                module.sensors["battery_level"] = IndygoSensorData(
                    key="battery_level",
                    value=bat,
                    extra_attributes={"source": "module_status", "timestamp": ts},
                )

            sps = status.get("sps") or {}
            sps_bat = sps.get("battery")
            if sps_bat is not None and sps_bat != _INDYGO_INT_MAX:
                module.sensors["secondary_battery_voltage"] = IndygoSensorData(
                    key="secondary_battery_voltage",
                    value=sps_bat,
                    extra_attributes={"source": "module_status", "timestamp": ts},
                )

            # 2. sensorState[] -> typed probe values
            sensor_state = status.get("sensorState") or []
            if not isinstance(sensor_state, list):
                continue

            inputs = module.raw_data.get("inputs") or []
            inputs_by_index: dict[int, dict] = {}
            for inp in inputs:
                idx = inp.get("index")
                if isinstance(idx, int):
                    inputs_by_index[idx] = inp

            for pos, item in enumerate(sensor_state):
                if not isinstance(item, dict):
                    continue
                raw = item.get("value")
                if raw is None or raw == _INDYGO_INT_MAX:
                    continue

                # sensorState position is 0-based, input.index is 1-based.
                inp = inputs_by_index.get(pos + 1)
                if inp is None:
                    continue
                inp_type = inp.get("type")
                key = INPUT_TYPE_SENSORS.get(inp_type)
                if not key:
                    continue

                decoded = self._decode_sensor_state_value(raw, inp_type)
                if decoded is None:
                    continue

                module.sensors[key] = IndygoSensorData(
                    key=key,
                    value=decoded,
                    extra_attributes={
                        "source": "module_status",
                        "raw_value": raw,
                        "input_type": inp_type,
                        "input_index": inp.get("index"),
                        "last_measurement_time": ts,
                    },
                )

                # Keep the legacy ``ph`` key in sync for the IPX so
                # existing dashboards/users don't break.
                if module.type == "ipx" and inp_type == INPUT_TYPE_PH:
                    module.sensors["ph"] = IndygoSensorData(
                        key="ph",
                        value=decoded,
                        extra_attributes={"source": "module_status"},
                    )

    # ------------------------------------------------------------------
    # Per-module diagnostics (batteries, radio, signal, alarms)
    # ------------------------------------------------------------------

    def _parse_module_diagnostics(self, pool_data: IndygoPoolData) -> None:
        """Expose battery / signal / radio / alarm fields per module."""
        for module in pool_data.modules.values():
            raw = module.raw_data

            # Primary battery — Indygo encodes it as 0..5 (level enum).
            battery_level = raw.get("battery")
            if battery_level is not None and battery_level != _INDYGO_INT_MAX:
                module.sensors["battery_level"] = IndygoSensorData(
                    key="battery_level", value=battery_level
                )

            # Battery voltage in 1/10 V (e.g. 85 -> 8.5 V) — keep raw,
            # the sensor platform applies the divisor for display.
            battery_voltage = raw.get("batteryVoltage")
            if battery_voltage is not None and battery_voltage != _INDYGO_INT_MAX:
                module.sensors["battery_voltage"] = IndygoSensorData(
                    key="battery_voltage", value=battery_voltage
                )

            # Secondary battery (mV typical scale)
            sec_battery = raw.get("secondaryBatteryVoltage")
            if sec_battery is not None and sec_battery != _INDYGO_INT_MAX:
                module.sensors["secondary_battery_voltage"] = IndygoSensorData(
                    key="secondary_battery_voltage", value=sec_battery
                )

            # Cellular signal quality — only present on the gateway (lr-mb)
            csq = raw.get("cellularSignalQuality")
            if csq is not None and str(module.type or "").startswith("lr-mb"):
                module.sensors["cellular_signal_quality"] = IndygoSensorData(
                    key="cellular_signal_quality", value=csq
                )

            # Last radio communication / last seen
            last_radio = raw.get("lastRadioCommunication")
            if last_radio:
                module.sensors["last_radio_communication"] = IndygoSensorData(
                    key="last_radio_communication", value=last_radio
                )
            seen_at = raw.get("seenAt")
            if seen_at:
                module.sensors["last_seen"] = IndygoSensorData(
                    key="last_seen", value=seen_at
                )

            # Software / hardware versions as diagnostic attributes on
            # the per-module battery_level sensor (no need to create
            # dedicated sensors for static info).
            sw = raw.get("softwareVersion")
            hw = raw.get("hardwareVersion")
            if (sw or hw) and "battery_level" in module.sensors:
                module.sensors["battery_level"].extra_attributes.update(
                    {
                        k: v
                        for k, v in {
                            "software_version": sw,
                            "hardware_version": hw,
                            "serial_number": raw.get("serialNumber"),
                        }.items()
                        if v
                    }
                )
