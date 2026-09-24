"""Validated v2 configuration and explicit migration from single-source v0.3."""
from copy import deepcopy
import math
import re
import uuid

CHARGER_DEFAULTS = {
    "enabled": False, "switch_entity": "", "power_entity": "", "solar_power_entity": "", "load_power_entity": "",
    "max_power_w": 7000, "start_surplus_w": 7500, "stop_surplus_w": 0,
    "start_inverter_below_w": 3000, "start_battery_below_w": 200,
    "battery_stop_w": 5000, "battery_resume_w": 200,
    "start_hold_s": 60, "stop_hold_s": 3, "min_off_s": 300,
    "start_rule_mode": "idle", "time_start": "12:00", "time_end": "17:00",
    "forecast_power_entity": "", "forecast_threshold": 0, "time_zone_offset_h": 8,
}
EV_DEFAULTS = CHARGER_DEFAULTS

DEFAULT_CONFIG = {
    "schema_version": 2, "name": "家用能源管理系统", "enabled": False, "dry_run": True,
    "inverter_power_entity": "", "battery_power_entity": "", "solar_power_entity": "", "battery_soc_entity": "", "battery_discharge_direction": "positive", "rated_power_w": 10000,
    "threshold_1_w": 8000, "threshold_2_w": 7000, "threshold_3_w": 5000,
    "battery_threshold_1_w": 8000, "battery_threshold_2_w": 7000, "battery_threshold_3_w": 5000,
    "restore_1_w": 6500, "restore_2_w": 5500, "restore_3_w": 3500,
    "battery_restore_1_w": 6500, "battery_restore_2_w": 5500, "battery_restore_3_w": 3500,
    "restore_enabled": False, "restore_hold_s": 60, "min_off_s": 300, "restore_interval_s": 30,
    "max_sensor_age_s": 30, "check_interval_s": 1, "settle_time_s": 2, "devices": [], "ev": CHARGER_DEFAULTS,
}


def number(value, label, minimum=0, maximum=1000000):
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是数字")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{label}应在 {minimum}~{maximum} 之间")
    return result


def entity(value, domain, required=False):
    if value == "" and not required:
        return ""
    if not isinstance(value, str) or not re.fullmatch(rf"{domain}\.[a-z0-9_]+", value):
        raise ValueError(f"请选择有效的 {domain} 实体：{value!s}")
    return value


def validate_config(data):
    if not isinstance(data, dict) or set(data) - set(DEFAULT_CONFIG):
        raise ValueError("配置必须是有效对象且不能含未知字段，请刷新网页后重试")
    out = deepcopy(DEFAULT_CONFIG)
    out.update(deepcopy(data))
    if type(out["schema_version"]) is not int or out["schema_version"] != 2:
        raise ValueError("配置版本不支持，请刷新网页")
    if not isinstance(out["name"], str) or not 1 <= len(out["name"].strip()) <= 100:
        raise ValueError("名称应为 1~100 个字符")
    out["name"] = out["name"].strip()
    for key in ("enabled", "dry_run", "restore_enabled"):
        if type(out[key]) is not bool:
            raise ValueError(f"{key} 必须为布尔值")
    if out["battery_discharge_direction"] not in ("positive", "negative"):
        raise ValueError("请选择电池放电方向")
    for key in ("inverter_power_entity", "battery_power_entity"):
        out[key] = entity(out[key], "sensor", out["enabled"])
    out["solar_power_entity"] = entity(out["solar_power_entity"], "sensor", False)
    out["battery_soc_entity"] = entity(out["battery_soc_entity"], "sensor", False)
    out["rated_power_w"] = number(out["rated_power_w"], "逆变器额定功率", 1)
    for prefix in ("", "battery_"):
        cuts, restores = [], []
        for grade in (1, 2, 3):
            cut_key, restore_key = f"{prefix}threshold_{grade}_w", f"{prefix}restore_{grade}_w"
            out[cut_key] = number(out[cut_key], f"{prefix}{grade}级切断功率", 1)
            out[restore_key] = number(out[restore_key], f"{prefix}{grade}级恢复功率", 0)
            if out[restore_key] >= out[cut_key]:
                raise ValueError("每级恢复阈值必须严格小于同路同级切断阈值")
            cuts.append(out[cut_key]); restores.append(out[restore_key])
        if not cuts[0] >= cuts[1] >= cuts[2] or not restores[0] >= restores[1] >= restores[2]:
            raise ValueError("两路的切断和恢复阈值分别应满足 1 级 ≥ 2 级 ≥ 3 级")
    if out["threshold_1_w"] >= out["rated_power_w"]:
        raise ValueError("逆变器切断阈值必须低于额定最大功率")
    for key, low, high in (("max_sensor_age_s",1,3600),("check_interval_s",1,60),("settle_time_s",0.1,60),
                            ("restore_hold_s",1,3600),("min_off_s",0,86400),("restore_interval_s",1,3600)):
        out[key] = number(out[key], key, low, high)
    if not isinstance(out["devices"], list) or len(out["devices"]) > 200:
        raise ValueError("设备列表最多 200 台")
    seen, ids, devices = set(), set(), []
    for item in out["devices"]:
        if not isinstance(item, dict) or set(item) - {"id","name","switch_entity","power_entity","grade","enabled","max_power_w"}:
            raise ValueError("设备配置含未知字段或格式错误")
        switch = entity(item.get("switch_entity"), "switch", True)
        if switch in seen:
            raise ValueError(f"开关重复：{switch}")
        seen.add(switch)
        if type(item.get("grade")) is not int or item["grade"] not in (0,1,2,3):
            raise ValueError("设备等级只能为整数 0、1、2、3")
        if type(item.get("enabled", True)) is not bool:
            raise ValueError("设备启用状态必须为布尔值")
        name = item.get("name", "")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError("设备名称应为 1~100 个字符")
        identifier = item.get("id") or uuid.uuid4().hex
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identifier) or identifier in ids:
            raise ValueError("设备标识不合法或重复")
        ids.add(identifier)
        devices.append({"id":identifier,"name":name.strip(),"switch_entity":switch,
                        "power_entity":entity(item.get("power_entity"),"sensor",True),
                        "grade":item["grade"],"enabled":item.get("enabled",True),
                        "max_power_w":number(item.get("max_power_w",0),"设备最大功率",0)})
    out["devices"] = devices
    ev_in = out.get("ev", CHARGER_DEFAULTS)
    if not isinstance(ev_in, dict) or set(ev_in) - set(CHARGER_DEFAULTS):
        raise ValueError("充电桩配置格式错误")
    ev = {**deepcopy(CHARGER_DEFAULTS), **ev_in}
    if type(ev["enabled"]) is not bool:
        raise ValueError("充电桩启用状态必须为布尔值")
    for key in ("switch_entity", "power_entity", "solar_power_entity", "load_power_entity", "forecast_power_entity"):
        ev[key] = entity(ev[key], "switch" if key == "switch_entity" else "sensor", ev["enabled"] if key == "switch_entity" else False)
    if ev["start_rule_mode"] not in ("idle", "time", "forecast", "time_and_forecast", "time_or_forecast"):
        raise ValueError("充电桩启动规则无效")
    for key in ("time_start", "time_end"):
        if not isinstance(ev[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", ev[key]):
            raise ValueError("充电桩时间规则必须为 HH:MM")
    ev["forecast_threshold"] = number(ev["forecast_threshold"], "今日光伏预测阈值", 0, 1000000)
    ev["time_zone_offset_h"] = number(ev["time_zone_offset_h"], "时间规则时区偏移", -12, 14)
    if ev["start_rule_mode"] in ("forecast", "time_and_forecast", "time_or_forecast") and not ev["forecast_power_entity"]:
        raise ValueError("选择预测规则时必须配置今日光伏预测实体")
    if ev["switch_entity"] and ev["switch_entity"] in seen:
        raise ValueError("充电桩开关不能与普通设备重复")
    for key, low, high in (("max_power_w", 1, 1000000), ("start_surplus_w", 0, 1000000), ("start_inverter_below_w", 0, 1000000), ("start_battery_below_w", 0, 1000000),
                           ("stop_surplus_w", -1000000, 1000000), ("battery_stop_w", 1, 1000000),
                           ("battery_resume_w", 0, 1000000), ("start_hold_s", 1, 3600),
                           ("stop_hold_s", 1, 3600), ("min_off_s", 0, 86400)):
        ev[key] = number(ev[key], key, low, high)
    if ev["start_surplus_w"] <= ev["stop_surplus_w"] or ev["battery_resume_w"] >= ev["battery_stop_w"]:
        raise ValueError("充电桩余量和电池阈值无效")
    out["ev"] = ev
    return out


def migrate_config(data):
    """Preserve v0.3 user settings without silently enabling the new controller."""
    if not isinstance(data, dict):
        raise ValueError("配置文件不是对象")
    if data.get("schema_version") == 2:
        # v0.4.x stored an optional EV block. Keep it and merge new charger
        # rule defaults so upgrades remain safe and editable.
        cleaned = deepcopy(data)
        notice = None
        return validate_config(cleaned), notice
    if data.get("schema_version") not in (None,1):
        raise ValueError("配置版本不支持")
    out = {key:deepcopy(value) for key,value in data.items() if key in DEFAULT_CONFIG}
    out.update(schema_version=2, enabled=False, dry_run=True, restore_enabled=False)
    rating = number(out.get("rated_power_w",10000),"额定功率",1)
    for grade, fraction in ((1,.8),(2,.7),(3,.5)):
        value = number(data.get(f"threshold_{grade}_w",DEFAULT_CONFIG[f"threshold_{grade}_w"]),"旧阈值",1)
        out[f"threshold_{grade}_w"] = min(value, rating*fraction)
        out[f"battery_threshold_{grade}_w"] = value
        out[f"restore_{grade}_w"] = out[f"threshold_{grade}_w"]*.8
        out[f"battery_restore_{grade}_w"] = value*.8
    for device in out.get("devices",[]):
        device.setdefault("max_power_w",0)
    return validate_config(out), "已导入旧设备和阈值。双源规则已启用，自动控制暂时关闭且处于模拟模式；请补齐两路实体、设备最大功率，核对阈值后保存启用。"
