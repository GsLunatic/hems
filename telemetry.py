"""One read-only HA poller shared by all browser tabs and the controller."""
from copy import deepcopy
import logging
import threading
import time

from core import power_value, read_power, read_battery_power, report_time

LOG = logging.getLogger("home_energy_manager.telemetry")


class Telemetry:
    INTERVAL = 1.0
    MAX_TRANSPORT_AGE = 3.0

    def __init__(self, request, stop):
        self.request = request
        self.stop = stop
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._states = {}
        self._numbers = {}
        self._nonnegative_numbers = {}
        self._received = None
        self._monotonic_received = None
        self._online = False
        self._error = "正在连接 Home Assistant"
        self._generation = 0
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.loop, daemon=True, name="ha-telemetry")
            self.thread.start()

    def poll(self):
        # Coalesce requests rather than multiply HA load with each browser tab.
        if not self._poll_lock.acquire(blocking=False):
            return
        try:
            rows = self.request("states")
            if not isinstance(rows, list):
                raise ValueError("HA states response is not a list")
            states = {}
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("entity_id"), str):
                    continue
                entity = row["entity_id"]
                if not entity.startswith(("sensor.", "switch.")):
                    continue
                attributes = row.get("attributes")
                attributes = attributes if isinstance(attributes, dict) else {}
                states[entity] = {key: row.get(key) for key in (
                    "entity_id", "state", "last_reported", "last_updated", "last_changed")}
                states[entity]["attributes"] = {key: attributes.get(key) for key in (
                    "friendly_name", "unit_of_measurement")}
            with self.lock:
                for entity, row in states.items():
                    value = power_value(row, signed=True)
                    if value is not None:
                        self._numbers[entity] = (value, report_time(row))
                        if value >= 0:
                            self._nonnegative_numbers[entity] = (value, report_time(row))
                # Keep last values for unavailable entities, but drop removed ones.
                self._numbers = {k: v for k, v in self._numbers.items() if k in states}
                self._nonnegative_numbers = {k: v for k, v in self._nonnegative_numbers.items() if k in states}
                self._states = states
                self._received = time.time()
                self._monotonic_received = time.monotonic()
                self._online = True
                self._error = None
                self._generation += 1
        except Exception as exc:
            with self.lock:
                if self._online or self._received is None:
                    LOG.warning("HA telemetry request failed (%s)", type(exc).__name__)
                self._online = False
                self._error = "HA 数据读取失败，保留最后读数并自动重连"
        finally:
            self.ready.set()
            self._poll_lock.release()

    def loop(self):
        while not self.stop.is_set():
            started = time.monotonic()
            self.poll()
            self.stop.wait(max(0.05, self.INTERVAL - (time.monotonic() - started)))

    def _connected(self):
        return (self._online and self._monotonic_received is not None
                and time.monotonic() - self._monotonic_received <= self.MAX_TRANSPORT_AGE)

    def control_states(self):
        with self.lock:
            if not self._connected():
                raise RuntimeError("HA telemetry is not connected")
            return [{**deepcopy(row), "_ha_observed_at": self._received} for row in self._states.values()]

    def entities(self):
        with self.lock:
            return [{"entity_id": entity, "state": row["state"],
                     "friendly_name": row["attributes"].get("friendly_name") or "",
                     "unit_of_measurement": row["attributes"].get("unit_of_measurement") or ""}
                    for entity, row in self._states.items()]

    def display(self, config, owned=()):
        # No action lock or HA calls here: a slow switch never blocks the page.
        with self.lock:
            entities = {d["power_entity"] for d in config["devices"]}
            entities.update(d["switch_entity"] for d in config["devices"])
            entities.update(config[k] for k in ("inverter_power_entity", "battery_power_entity", "solar_power_entity", "battery_soc_entity") if config.get(k))
            ev=config.get("ev",{})
            entities.update(ev.get(k) for k in ("switch_entity","power_entity","solar_power_entity","load_power_entity","forecast_power_entity") if ev.get(k))
            states = {k: deepcopy(self._states.get(k, {})) for k in entities}
            numbers = {k: self._numbers.get(k) for k in entities}
            nonnegative = {k: self._nonnegative_numbers.get(k) for k in entities}
            connected = self._connected()
            received, generation, error = self._received, self._generation, self._error
        now, max_age = time.time(), config["max_sensor_age_s"]

        def reading(entity, battery=False):
            row = states.get(entity, {})
            current = power_value(row, signed=battery)
            stamp = report_time(row)
            fallback = current is None
            last = (numbers if battery else nonnegative).get(entity)
            if fallback and last is not None:
                current, stamp = last
            age = now - stamp if stamp is not None else None
            valid, _ = read_battery_power(row, now) if battery else read_power(row, now, max_age)
            valid = connected and valid is not None
            if battery and current is not None:
                current = max(0, current if config["battery_discharge_direction"] == "positive" else -current)
            return {"power_w": current, "age_s": age, "control_valid": valid,
                    "last_value": fallback or not valid}

        sources = {
            "inverter": reading(config["inverter_power_entity"]),
            "battery": reading(config["battery_power_entity"], battery=True),
            "solar": reading(config.get("solar_power_entity")) if config.get("solar_power_entity") else {"power_w":None,"age_s":None,"control_valid":False,"last_value":False},
        }
        soc_entity=config.get("battery_soc_entity", "")
        soc_row=states.get(soc_entity, {}) if soc_entity else {}
        soc_value=None
        try:
            soc_value=float(soc_row.get("state"))
            if soc_value != soc_value or soc_value < 0 or soc_value > 100: soc_value=None
        except (TypeError, ValueError):
            soc_value=None
        sources["soc"]={"power_w":soc_value,"age_s":(now-report_time(soc_row) if report_time(soc_row) is not None else None),"control_valid":bool(soc_value is not None and connected),"last_value":False}
        loads = []
        for device in config["devices"]:
            value = reading(device["power_entity"])
            power = value["power_w"]
            maximum = device["max_power_w"]
            live = states.get(device["switch_entity"], {}).get("state", "unavailable")
            ratio = power / maximum if maximum > 0 and power is not None else None
            # A valid changing power reading proves the device is reachable even
            # when HA has temporarily omitted the switch state from its snapshot.
            # Only show offline when both switch and power are unavailable.
            label = ("off" if live == "off" else
                     "unknown" if power is None and live in ("on", "unknown") else
                     "offline" if power is None else "over_limit" if ratio is not None and ratio > 1 else
                     "near_limit" if ratio is not None and ratio >= .9 else "standby" if power <= 5 else "running")
            loads.append({**device, "state": live, "power_w": power, "power_ratio": ratio,
                          "power_status": label, "power_age_s": value["age_s"],
                          "power_from_cache": value["last_value"], "shed_by_manager": device["switch_entity"] in owned,
                          "rule_countdown_s": None, "rule_countdown_total_s": None, "rule_countdown_kind": None})
        # The page polls telemetry independently of the control loop, so expose
        # the same countdown hints here for responsive mobile rendering.
        grade_totals = {str(g): 0.0 for g in (0, 1, 2, 3)}
        inverter_power = sources["inverter"]["power_w"]
        for load in loads:
            if load["power_w"] is not None and load.get("state") != "off":
                grade_totals[str(load["grade"])] += max(0.0, load["power_w"])
            if load.get("state") == "off":
                # Controller publishes both countdown and unmet conditions.
                # Telemetry only contributes the latest device reading.
                load["restore_pending"] = bool(config.get("restore_enabled") and load.get("enabled", True))
                load["restore_countdown_active"] = False
                load["rule_countdown_kind"] = "restore"
            elif load["state"] == "on" and load["grade"] in (1, 2, 3):
                grade = load["grade"]
                inv_trip = inverter_power is not None and inverter_power > config[f"threshold_{grade}_w"]
                battery_power = sources["battery"]["power_w"] if sources["battery"]["control_valid"] else None
                bat_trip = battery_power is not None and battery_power > config[f"battery_threshold_{grade}_w"]
                if inv_trip or bat_trip:
                    load["rule_countdown_s"] = 0.0
                    load["rule_countdown_total_s"] = max(1.0, float(config["settle_time_s"]))
                    load["rule_countdown_kind"] = "cut"
        grade_percent = {g: (round(v / inverter_power * 100, 1) if inverter_power and inverter_power > 0 else None)
                         for g, v in grade_totals.items()}
        unavailable_devices=[]
        for load in loads:
            if load["power_status"] == "offline":
                unavailable_devices.append({"name": load["name"], "entity_id": load["power_entity"], "reason": "开关和功率实体均无有效数据"})
            elif load["state"] in ("unavailable", "unknown"):
                unavailable_devices.append({"name": load["name"], "entity_id": load["switch_entity"], "reason": "开关实体状态不可用"})
        unavailable_sources=[]
        for key,label in (("inverter","逆变器总负载"),("battery","电池放电功率")):
            if not sources[key]["control_valid"]:
                reason=("HA 数据连接中断" if not connected else "电池实体不可用、未配置或无有效功率数值") if key=="battery" else "报告过期、缺少数值或实体未配置"
                unavailable_sources.append({"name":label,"entity_id":config.get(key+"_power_entity", ""),"reason":reason})
        if config.get("solar_power_entity") and sources["solar"]["power_w"] is None:
            unavailable_sources.append({"name":"太阳能发电功率","entity_id":config["solar_power_entity"],"reason":"报告过期或无有效数值"})
        if config.get("battery_soc_entity") and sources["soc"]["power_w"] is None:
            unavailable_sources.append({"name":"电池 SOC","entity_id":config["battery_soc_entity"],"reason":"报告过期或无有效数值"})
        evcfg=config.get("ev",{})
        evswitch=states.get(evcfg.get("switch_entity"),{}).get("state","unavailable") if evcfg.get("switch_entity") else "unavailable"
        evsolar=reading(evcfg.get("solar_power_entity")) if evcfg.get("solar_power_entity") else {"power_w":None,"age_s":None,"control_valid":False}
        evload=reading(evcfg.get("load_power_entity")) if evcfg.get("load_power_entity") else {"power_w":None,"age_s":None,"control_valid":False}
        evpower=reading(evcfg.get("power_entity")) if evcfg.get("power_entity") else {"power_w":None,"age_s":None,"control_valid":False}
        forecast_row=states.get(evcfg.get("forecast_power_entity"),{}) if evcfg.get("forecast_power_entity") else {}
        try:
            forecast_value=float(forecast_row.get("state"))
        except (TypeError, ValueError):
            forecast_value=None
        ev_status={"enabled":bool(evcfg.get("enabled")),"state":evswitch,"power_w":evpower["power_w"],
                   "solar_power_w":evsolar["power_w"],"load_power_w":evload["power_w"],
                   "surplus_w":(evsolar["power_w"]-evload["power_w"] if evsolar["power_w"] is not None and evload["power_w"] is not None else None),
                   "forecast_value":forecast_value,"forecast_entity":evcfg.get("forecast_power_entity",""),
                   "start_rule_mode":evcfg.get("start_rule_mode","idle"),"priority":"低于家庭设备"}
        return {"sources": sources, "devices": loads, "ev": ev_status,
                "grade_power_totals": grade_totals, "grade_power_percent": grade_percent,
                "unavailable_devices": unavailable_devices, "unavailable_sources": unavailable_sources,
                "entity_values": {key:{"state":row.get("state", "unavailable"),
                    "unit_of_measurement":row.get("attributes", {}).get("unit_of_measurement") or ""}
                    for key,row in states.items() if key},
                "connected": connected, "received_at": received, "generation": generation,
                "error": error if not connected else None,
                "poll_interval_s": self.INTERVAL}
