"""Dual-source OR shedding and state-based load restoration."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
import logging
import os
from pathlib import Path
import threading
import time
import math
import urllib.error
import urllib.parse
import urllib.request
from settings import DEFAULT_CONFIG, validate_config, migrate_config
from backup import export_backup

LOG = logging.getLogger("home_energy_manager")


def atomic_json(path, data):
    tmp = path.with_suffix(".tmp")
    try:
        with tmp.open("w",encoding="utf-8") as handle:
            json.dump(data,handle,ensure_ascii=False,indent=2,allow_nan=False)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp,path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise

def report_time(state):
    raw = state.get("last_reported") or state.get("last_updated")
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def read_power(state, now, max_age, *, signed=False):
    if not isinstance(state, dict):
        return None, None
    stamp = report_time(state)
    age = now - stamp if stamp is not None else None
    if age is None or age < 0 or age > max_age:
        return None, age
    return power_value(state, signed=signed), age


def power_value(state, *, signed=False):
    """Parse the entity's current stored value independently of report age."""
    if not isinstance(state, dict):
        return None
    attributes = state.get("attributes", {})
    if not isinstance(attributes, dict):
        return None
    unit = attributes.get("unit_of_measurement")
    if unit not in ("W", "kW"):
        return None
    try:
        result = float(state["state"]) * (1000 if unit == "kW" else 1)
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    if not math.isfinite(result) or (not signed and result < 0):
        return None
    return result


def read_battery_power(state, now):
    """Use HA's current numeric battery state, including unchanged zero.

    Report age is informational only. Never substitute a cached display value
    for an unavailable/non-numeric entity in control decisions.
    """
    if not isinstance(state, dict):
        return None, None
    stamp=report_time(state)
    return power_value(state,signed=True), now-stamp if stamp is not None else None




def numeric_value(state):
    """Read a finite numeric sensor value, preserving the entity unit."""
    if not isinstance(state, dict):
        return None
    raw = state.get("state")
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def read_numeric(state, now, max_age):
    value = numeric_value(state)
    stamp = report_time(state)
    age = now - stamp if stamp is not None else None
    if value is None or stamp is None or age < 0 or age > max_age:
        return None, age
    return value, age


def triggers(config, grade, inverter, battery):
    if grade not in (1,2,3):
        return []
    return [source for source,power,key in (("inverter",inverter,f"threshold_{grade}_w"),
                                           ("battery",battery,f"battery_threshold_{grade}_w"))
            if power is not None and power > config[key]]


def eligible(config, states, inverter, battery):
    candidates=[]
    for device in config["devices"]:
        sources=triggers(config,device["grade"],inverter,battery)
        if not device["enabled"] or states.get(device["switch_entity"],{}).get("state")!="on" or not sources:
            continue
        # Shed the lowest priority grade first; within a grade, cut the largest
        # currently reported load first. power_value intentionally reads HA's
        # stored value even when last_reported is old, matching the dashboard.
        watts=power_value(states.get(device["power_entity"]))
        candidates.append({**device,"trigger_sources":sources,"_shed_power_w":watts})
    candidates.sort(key=lambda device:(device["grade"],device["_shed_power_w"] if device["_shed_power_w"] is not None else -1),reverse=True)
    for device in candidates:
        device.pop("_shed_power_w",None)
    return candidates


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class State:
    def __init__(self,data_dir=Path("/data"),ha_api="http://supervisor/core/api",token=""):
        self.data_dir=Path(data_dir); self.data_dir.mkdir(parents=True,exist_ok=True)
        self.config_file=self.data_dir/"manager.json"
        self.runtime_file=self.data_dir/"runtime.json"
        self.ha_api,self.token=ha_api.rstrip("/"),token
        self.lock=threading.RLock(); self._action_lock=threading.Lock()
        self.stop=threading.Event(); self._wake=threading.Event()
        self._waiting_since=None; self._failures={}; self._holds={}
        self.last_actions=[]; self.migration_notice=None
        self.last_status={"state":"starting","message":"等待两路功率数据"}
        self.config=deepcopy(DEFAULT_CONFIG)
        self.runtime={"shed":{},"last_restore":0,"waiting_since":None,"ev_last_off":0,"ev_owned":False}
        try:
            if self.config_file.exists():
                self.config,self.migration_notice=migrate_config(json.loads(self.config_file.read_text(encoding="utf-8")))
        except (ValueError,TypeError,OSError):
            self.last_status.update(state="configuration_error",message="配置读取失败，控制已停用，请重新保存")
        try:
            if self.runtime_file.exists():
                saved=json.loads(self.runtime_file.read_text(encoding="utf-8"))
                if not isinstance(saved,dict) or not isinstance(saved.get("shed"),dict):
                    raise ValueError("invalid runtime")
                # Only well-formed confirmed off records may grant restore authority.
                for entity_id,record in saved["shed"].items():
                    if (not isinstance(record,dict) or not isinstance(record.get("off_changed"),str)
                        or not isinstance(record.get("id"),str) or record.get("grade") not in (1,2,3)
                        or not isinstance(record.get("off_since"),(int,float)) or not math.isfinite(record["off_since"])):
                        raise ValueError("invalid ownership")
                self.runtime.update({key: value for key, value in saved.items() if key in self.runtime})
                for key in ("last_restore","ev_last_off"):
                    if not isinstance(self.runtime[key],(int,float)) or not math.isfinite(self.runtime[key]):
                        raise ValueError("invalid runtime time")
                stamp=self.runtime.get("waiting_since")
                self._waiting_since=stamp if isinstance(stamp,(int,float)) and math.isfinite(stamp) else None
        except (ValueError,TypeError,OSError):
            self.runtime={"shed":{},"last_restore":0,"waiting_since":None,"ev_last_off":0,"ev_owned":False}
            LOG.error("Runtime ownership unavailable; automatic restoration queue discarded")
        self._restore_head=None; self._last_ev_state=None
        self._device_power_cache={}
        self._off_observed={}
        self._restore_context=None
        self.telemetry=None
        self._opener=urllib.request.build_opener(NoRedirect)

    def get_config(self):
        with self.lock:
            return deepcopy(self.config)

    def get_payload(self):
        with self.lock:
            payload=deepcopy({"status":self.last_status,"actions":self.last_actions})
            config=deepcopy(self.config)
            owned=self.runtime["shed"].copy()
        if self.telemetry is not None:
            display=self.telemetry.display(config,owned)
            # Use the same decision details as the controller, never infer
            # restoration from the historical shedding ownership or min-off time.
            pending={d["switch_entity"]:d for d in payload["status"].get("restore_queue",[])}
            for load in display["devices"]:
                detail=pending.get(load["switch_entity"])
                if detail and load["state"]=="off":
                    for key,value in detail.items():
                        if key.startswith(("restore_","rule_countdown_")):
                            load[key]=deepcopy(value)
                elif load["state"]=="off":
                    load["restore_wait_reasons"]=["等待下一次控制检查" if load.get("enabled") else "此设备未启用管理"]
            payload["status"].update(display=display,devices=display["devices"],
                                     rated_power_w=config["rated_power_w"],
                                     enabled=config["enabled"],dry_run=config["dry_run"])
        return payload

    def get_entities(self):
        if self.telemetry is not None:
            return self.telemetry.entities()
        rows=self.ha_request("states")
        return [{"entity_id":row["entity_id"],"state":row.get("state"),
                 "friendly_name":(row.get("attributes") or {}).get("friendly_name", ""),
                 "unit_of_measurement":(row.get("attributes") or {}).get("unit_of_measurement", "")}
                for row in rows if isinstance(row,dict) and isinstance(row.get("entity_id"),str)]

    def save(self,data, *, backup_before=False):
        config=validate_config(data)
        with self._action_lock:
            if backup_before:
                atomic_json(self.data_dir/"before_restore.json", export_backup(self.get_config()))
            atomic_json(self.config_file,config)
            with self.lock:
                self.config=config; self.migration_notice=None; self._holds.clear()
                self.last_status.update(state="settings_saved",message="配置已保存，后续检查使用新设置")
        self._wake.set()

    def _persist_runtime(self):
        self.runtime["waiting_since"]=self._waiting_since
        atomic_json(self.runtime_file,self.runtime)

    def ha_request(self, path, method="GET", payload=None):
        if not self.token:
            raise RuntimeError("Home Assistant API 令牌不可用")
        parsed = urllib.parse.urlsplit(self.ha_api)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise RuntimeError("HA_API 地址配置无效")
        body = json.dumps(payload, allow_nan=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.ha_api + "/" + path.lstrip("/"), data=body, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=5) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise RuntimeError("HA API 响应过大")
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HA API 返回 HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise RuntimeError("无法读取 HA API，请检查加载项权限、网络及 HA 运行状态") from None


    def set_status(self,**values):
        with self.lock:
            self.last_status={**self.last_status,**values,"updated_at":time.time()}

    def _record(self,action):
        with self.lock:
            self.last_actions.insert(0,{"time":time.time(),**action})
            del self.last_actions[50:]

    def _hold(self,key,condition,seconds):
        if not condition:
            self._holds.pop(key,None)
            return False
        now=time.time()
        self._holds.setdefault(key,now)
        return now-self._holds[key]>=seconds

    def _reconcile(self,config,states):
        changed=False
        devices={d["switch_entity"]:d for d in config["devices"]}
        for switch,record in list(self.runtime["shed"].items()):
            device=devices.get(switch); state=states.get(switch,{})
            invalid=(not device or not device["enabled"] or device["grade"]==0
                     or device["id"]!=record["id"] or device["grade"]!=record["grade"])
            manual=(state.get("state")=="on" or
                    (state.get("state")=="off" and state.get("last_changed")!=record["off_changed"]))
            if invalid or manual:
                self.runtime["shed"].pop(switch,None); changed=True
        if changed:
            self._persist_runtime()

    def _display_power(self, entity, state, now, max_age):
        """Display HA's last numeric load reading even before the next report.

        In-memory fallback is per entity, never used by protection decisions.
        """
        value = power_value(state)
        stamp = report_time(state) if isinstance(state, dict) else None
        age = now - stamp if stamp is not None else None
        if value is not None:
            if entity:
                self._device_power_cache[entity] = {"value": value, "report_time": stamp}
            recent = age is not None and 0 <= age <= max_age
            return value, age, not recent
        cached = self._device_power_cache.get(entity)
        if cached is not None:
            stamp = cached["report_time"]
            return cached["value"], now - stamp if stamp is not None else None, True
        return None, age, False

    def _closed_queue(self, config, states, now):
        """Track every managed off load, including manual closures and restarts."""
        observed={}
        queue=[]
        for device in config["devices"]:
            switch=device["switch_entity"]
            row=states.get(switch,{})
            if not device["enabled"] or row.get("state")!="off":
                continue
            changed=row.get("last_changed")
            previous=self._off_observed.get(switch)
            if previous and previous["off_changed"]==changed:
                record=previous
            else:
                stamp=report_time({"last_reported":changed})
                record={"off_changed":changed,"off_since":min(now,stamp) if stamp is not None else now}
                if self._restore_head==device["id"]:
                    self._holds.pop("restore",None)
            observed[switch]=record
            queue.append({**device,**record})
        self._off_observed=observed
        return sorted(queue,key=lambda d:d["grade"])

    def _snapshot(self,config,states):
        now=time.time(); age=config["max_sensor_age_s"]
        inv,inv_age=read_power(states.get(config["inverter_power_entity"]),now,age)
        bat,bat_age=read_battery_power(states.get(config["battery_power_entity"]),now)
        solar,solar_age=read_power(states.get(config.get("solar_power_entity","")),now,age) if config.get("solar_power_entity") else (None,None)
        soc,soc_age=read_numeric(states.get(config.get("battery_soc_entity","")),now,age) if config.get("battery_soc_entity") else (None,None)
        if soc is not None:
            soc=max(0.0,min(100.0,soc))
        if bat is not None:
            bat=max(0,bat if config["battery_discharge_direction"]=="positive" else -bat)
        self._reconcile(config,states)
        display_entities={d["power_entity"] for d in config["devices"]}
        self._device_power_cache={k:v for k,v in self._device_power_cache.items() if k in display_entities}
        loads=[]
        for device in config["devices"]:
            live=states.get(device["switch_entity"],{}).get("state","unavailable")
            power,power_age,power_from_cache=self._display_power(device["power_entity"],states.get(device["power_entity"]),now,age)
            maximum=device["max_power_w"]
            ratio=power/maximum if maximum>0 and power is not None else None
            label=("off" if live=="off" else
                   "unknown" if power is None and live in ("on","unknown") else
                   "offline" if power is None else "last_value" if power_from_cache else "over_limit" if ratio is not None and ratio>1 else
                   "near_limit" if ratio is not None and ratio>=.9 else "standby" if power<=5 else "running")
            loads.append({**device,"state":live,"power_w":power,"power_age_s":power_age,"power_from_cache":power_from_cache,"power_ratio":ratio,"power_status":label,
                          "shed_by_manager":device["switch_entity"] in self.runtime["shed"]})
        candidates=eligible(config,states,inv,bat)
        evc=config.get("ev",{}); ev_state=states.get(evc.get("switch_entity"),{}).get("state","unavailable") if evc.get("switch_entity") else "unavailable"
        ev_power,_=read_power(states.get(evc.get("power_entity")),now,age) if evc.get("power_entity") else (None,None)
        solar,_=read_power(states.get(evc.get("solar_power_entity")),now,age) if evc.get("solar_power_entity") else (None,None)
        house,_=read_power(states.get(evc.get("load_power_entity")),now,age) if evc.get("load_power_entity") else (None,None)
        forecast,_=read_numeric(states.get(evc.get("forecast_power_entity")),now,age) if evc.get("forecast_power_entity") else (None,None)
        if evc.get("enabled") and ev_state=="off" and self._last_ev_state=="on":
            self.runtime["ev_last_off"]=now; self.runtime["ev_owned"]=False; self._persist_runtime()
        self._last_ev_state=ev_state
        surplus=solar-house if solar is not None and house is not None else None
        ev_status={"enabled":bool(evc.get("enabled")),"state":ev_state,"power_w":ev_power,
                   "solar_power_w":solar,"load_power_w":house,"surplus_w":surplus,
                   "forecast_value":forecast,"forecast_entity":evc.get("forecast_power_entity",""),
                   "start_rule_mode":evc.get("start_rule_mode","idle"),
                   "owned":self.runtime.get("ev_owned",False),"priority":"低于家庭设备"}
        queue=self._closed_queue(config,states,now)
        queued={d["switch_entity"]: d for d in queue}
        candidate_switches={d["switch_entity"] for d in candidates}
        grade_totals={str(g): 0.0 for g in (0,1,2,3)}
        for load in loads:
            if load["power_w"] is not None and load.get("state") != "off":
                grade_totals[str(load["grade"])] += max(0.0, load["power_w"])
            switch=load["switch_entity"]
            if load.get("state") == "off":
                # Final restore reasons and countdown are published after the
                # decision, so early returns cannot leave yesterday's timer.
                load.update(restore_pending=switch in queued,restore_countdown_active=False,
                            rule_countdown_s=None,rule_countdown_total_s=None,rule_countdown_kind="restore")
            elif switch in candidate_switches:
                load["rule_countdown_s"] = 0.0
                load["rule_countdown_total_s"] = max(1.0, float(config["settle_time_s"]))
                load["rule_countdown_kind"] = "cut"
            else:
                load["rule_countdown_s"] = None
                load["rule_countdown_total_s"] = None
                load["rule_countdown_kind"] = None
        grade_percent={g:(round(v/inv*100,1) if inv and inv>0 else None) for g,v in grade_totals.items()}
        self.set_status(inverter_power_w=inv,battery_power_w=bat,inverter_age_s=inv_age,battery_age_s=bat,
                        solar_power_w=solar,solar_age_s=solar_age,battery_soc=soc,battery_soc_age_s=soc_age,
                        rated_power_w=config["rated_power_w"],available_power_w=max(0,config["rated_power_w"]-inv) if inv is not None else None,
                        inverter_exceeded_grades=[g for g in (3,2,1) if inv is not None and inv>config[f"threshold_{g}_w"]],
                        battery_exceeded_grades=[g for g in (3,2,1) if bat is not None and bat>config[f"battery_threshold_{g}_w"]],
                        exceeded_grades=[g for g in (3,2,1) if triggers(config,g,inv,bat)],
                        eligible_devices=candidates,restore_queue=queue,devices=loads,
                        grade_power_totals=grade_totals,grade_power_percent=grade_percent,
                        enabled=config["enabled"],dry_run=config["dry_run"],migration_notice=self.migration_notice,ev=ev_status)
        stamps={source:report_time(states.get(config[f"{source}_power_entity"],{})) for source in ("inverter","battery")}
        # A new HA poll can confirm unchanged battery power after an action.
        # Do not wait indefinitely for its last_reported timestamp to change.
        battery_row=states.get(config["battery_power_entity"],{})
        stamps["battery"]=battery_row.get("_ha_observed_at",now) if bat is not None else None
        self._restore_context=(config,states,inv,bat,stamps,candidates,queue,ev_status)
        return inv,bat,stamps,candidates,queue,ev_status

    def _fresh_after_action(self,config,stamps,sources):
        if self._waiting_since is None:
            return True
        ready_at=self._waiting_since+config["settle_time_s"]
        return time.time()>=ready_at and all(stamps.get(key) is not None and stamps[key]>=ready_at for key in sources)

    def _act(self,config,device,kind,on,inv,bat,reason):
        switch=device["switch_entity"]
        if time.time()<self._failures.get(switch,0):
            self.set_status(state="actuation_failed",message="设备正在失败重试冷却期")
            return False
        if config["dry_run"]:
            self.set_status(state="dry_run_action",message=f'模拟：{reason}，将{"开启" if on else "关闭"} {device["name"]}，未发送命令')
            return False
        if self.stop.is_set():
            return False
        # Re-check immediately before acting; the user may have toggled the
        # device while the bulk sensor response was in flight.
        try:
            current=self.ha_request("states/"+urllib.parse.quote(switch,safe="."))
        except Exception as exc:
            self._failures[switch]=time.time()+30
            self.set_status(state="actuation_failed",message="无法复核设备开关状态，未发送命令，稍后重试")
            LOG.warning("Switch preflight failed for %s (%s)",switch,type(exc).__name__)
            return False
        if not isinstance(current,dict):
            return False
        if kind=="restore":
            # Restoration is intentionally state based: every configured,
            # enabled device that is currently off may be restored.  Earlier
            # versions required manager ownership, which left manually
            # switched-off devices stuck forever.
            if current.get("state")!="off":
                self.runtime["shed"].pop(switch,None)
                self._persist_runtime()
                self._holds.pop("restore",None)
                return False
        elif current.get("state")!=("off" if on else "on"):
            return False
        if on:
            # Forget historical shedding ownership; current off states are
            # rediscovered each cycle, including after a failed command.
            if kind=="restore":
                self.runtime["shed"].pop(switch,None)
                self.runtime["last_restore"]=time.time()
            self._persist_runtime()
        target="on" if on else "off"
        self.set_status(state="restoring" if on else "shedding",message=f'正在{"开启" if on else "关闭"} {device["name"]}')
        action={"device":device["name"],"entity":switch,"grade":device.get("grade"),"kind":kind,
                "power_w":inv,"inverter_power_w":inv,"battery_power_w":bat,"dry_run":False,"reason":reason}
        success=False
        try:
            self.ha_request(f"services/switch/turn_{target}","POST",{"entity_id":switch})
            deadline=time.monotonic()+2
            while True:
                reply=self.ha_request("states/"+urllib.parse.quote(switch,safe="."))
                if isinstance(reply,dict) and reply.get("state")==target:
                    break
                if time.monotonic()>=deadline or self.stop.wait(.2):
                    raise RuntimeError("未收到目标状态确认")
            success=True
            if self._restore_context is not None:
                self._restore_context[1][switch]=reply
            self._failures.pop(switch,None)
            if kind=="shed" and reply.get("last_changed"):
                self.runtime["shed"][switch]={"id":device["id"],"grade":device["grade"],
                    "off_since":time.time(),"off_changed":reply["last_changed"]}
            self._record({**action,"result":"turned_on" if on else "turned_off"})
            self.set_status(state="waiting_for_measurement",message="已确认动作，等待新的稳定功率报告")
        except Exception as exc:
            self._failures[switch]=time.time()+30
            self._record({**action,"result":"error","error":"未能确认目标状态，请检查设备和 HA 日志"})
            self.set_status(state="actuation_failed",message="动作失败或未确认，等待新读数后重新判断")
            LOG.warning("Switch action failed for %s (%s)",switch,type(exc).__name__)
        finally:
            self._waiting_since=time.time(); self._holds.clear()
            self._persist_runtime()
        return success

    def _restore_conditions(self,config,device,inv,bat,now):
        """Actual readings, not projected device maximums, gate household recovery."""
        grade=device["grade"] if device["grade"] in (1,2,3) else 1
        reasons=[]
        for label,value,key in (("逆变器",inv,f"restore_{grade}_w"),("电池放电",bat,f"battery_restore_{grade}_w")):
            limit=config[key]
            if value is None:
                reasons.append("电池实体不可用或无有效功率数值，等待有效读数" if label=="电池放电"
                               else f"{label}功率无效或报告过期，等待有效读数")
            elif value>=limit:
                reasons.append(f"{label} {value:,.0f} W，需低于 {limit:,.0f} W")
        if inv is not None and inv>=config["rated_power_w"]:
            reasons.append(f'逆变器达到额定上限 {config["rated_power_w"]:,.0f} W')
        waits=[]
        for label,deadline in (("最短断电",device["off_since"]+config["min_off_s"]),
                               ("逐台恢复间隔",self.runtime["last_restore"]+config["restore_interval_s"]),
                               ("操作失败重试冷却",self._failures.get(device["switch_entity"],0))):
            remaining=max(0.0,deadline-now)
            if remaining>0:
                reasons.append(f"{label}剩余 {math.ceil(remaining)} 秒")
                waits.append(remaining)
        return reasons,waits

    def _publish_restore_details(self):
        if self._restore_context is None:
            return
        config,states,inv,bat,stamps,candidates,queue,ev=self._restore_context
        now=time.time()
        queue=[d for d in queue if states.get(d["switch_entity"],{}).get("state")=="off"]
        global_reasons=[]
        if not config["enabled"]:
            global_reasons.append("自动控制未启用")
        if not config["restore_enabled"]:
            global_reasons.append("自动恢复未启用")
        if config["dry_run"]:
            global_reasons.append("模拟模式开启，不发送开机命令")
        if candidates:
            global_reasons.append("正在优先处理减载："+"、".join(d["name"] for d in candidates))
        stop_reason=self._ev_stop_needed(config,ev,inv,bat,candidates)
        if stop_reason:
            global_reasons.append("优先停止充电桩："+stop_reason)
        if not self._fresh_after_action(config,stamps,["inverter","battery"]):
            ready_at=self._waiting_since+config["settle_time_s"]
            if now<ready_at:
                global_reasons.append(f"操作后稳定等待剩余 {math.ceil(ready_at-now)} 秒")
            for source,label in (("inverter","逆变器"),("battery","电池")):
                if stamps.get(source) is None or stamps[source]<ready_at:
                    global_reasons.append("等待操作稳定后重新读取电池实体" if source=="battery"
                                          else f"等待{label}在上次操作稳定后重新报告功率")
        for i,device in enumerate(queue):
            reasons,waits=self._restore_conditions(config,device,inv,bat,now)
            reasons=global_reasons+reasons
            if i:
                reasons.append(f'排队第 {i+1} 位，先恢复「{queue[0]["name"]}」')
            started=self._holds.get("restore") if device["id"]==self._restore_head else None
            active=not reasons and started is not None
            remaining=max(0.0,config["restore_hold_s"]-(now-started)) if active else None
            if active:
                reasons.append(f'恢复条件持续倒计时剩余 {math.ceil(remaining)} 秒')
            elif not reasons:
                reasons.append("等待下一次控制检查启动倒计时")
            device.update(restore_pending=True,restore_countdown_active=active,
                          restore_wait_reasons=reasons,rule_countdown_kind="restore",
                          rule_countdown_s=remaining,rule_countdown_total_s=config["restore_hold_s"] if active else None)
        with self.lock:
            details={d["switch_entity"]:d for d in queue}
            for load in self.last_status.get("devices",[]):
                detail=details.get(load["switch_entity"])
                if detail:
                    load.update({k:deepcopy(v) for k,v in detail.items() if k.startswith(("restore_","rule_countdown_"))})
            self.last_status["restore_queue"]=deepcopy(queue)

    def _restore(self,config,states,inv,bat,queue):
        if not config["restore_enabled"] or not queue:
            self._holds.pop("restore",None)
            return False
        # Restore in queue order and evaluate actual readings again after each action.
        device=queue[0]; switch=device["switch_entity"]; grade=device["grade"]
        if self._restore_head != device["id"]:
            self._holds.pop("restore",None)
            self._restore_head=device["id"]
        live=states.get(switch,{}).get("state")
        reasons,_=self._restore_conditions(config,device,inv,bat,time.time())
        ready=not reasons and live=="off"
        if not self._hold("restore",ready,config["restore_hold_s"]):
            reason="；".join(reasons) if reasons else "等待低功率稳定时间"
            self.set_status(state="restore_wait",message=f'{grade} 级 {device["name"]}：{reason}')
            return True
        self._act(config,device,"restore",True,inv,bat,"两路功率均满足恢复条件")
        return True

    def _ev_action(self, config, ev, inv, bat, on, reason):
        rules=config.get("ev",{})
        if not rules.get("enabled") or not rules.get("switch_entity"):
            return False
        device={"id":"ev","name":"新能源车充电桩","switch_entity":rules["switch_entity"]}
        ok=self._act(config,device,"ev_start" if on else "ev_stop",on,inv,bat,reason)
        if ok:
            self.runtime["ev_owned"]=on
            if not on: self.runtime["ev_last_off"]=time.time()
            self._persist_runtime()
        return ok

    def _ev_stop_needed(self, config, ev, inv, bat, candidates):
        rules=config.get("ev",{})
        if not rules.get("enabled") or ev.get("state")!="on":
            return None
        if bat is not None and bat > rules.get("battery_stop_w",5000):
            return "电池放电超过充电桩停止阈值"
        if inv is not None and inv >= config["rated_power_w"]:
            return "逆变器达到额定功率"
        # Preserve household grades 0-2; EV is sacrificed before any grade 2/3 trip.
        if any(d["grade"] in (2,3) for d in candidates):
            return "家庭设备达到保护阈值，优先停止充电桩"
        if ev.get("surplus_w") is not None and ev["surplus_w"] < rules.get("stop_surplus_w",0):
            return "光伏余量低于停止阈值"
        return None

    def _ev_start(self, config, ev, inv, bat, candidates, queue):
        rules=config.get("ev",{})
        if not rules.get("enabled") or ev.get("state")!="off" or candidates or queue:
            return False
        if inv is None or bat is None:
            self._holds.pop("ev_start",None); return False
        # Idle charging is allowed either when configured solar surplus is
        # available, or when both inverter and battery discharge are quiet.
        surplus = ev.get("surplus_w")
        if surplus is not None:
            idle_ok = surplus >= rules["start_surplus_w"] and surplus >= rules["max_power_w"]
        else:
            idle_ok = inv < rules.get("start_inverter_below_w", 3000) and bat < rules.get("start_battery_below_w", 200)
        if not idle_ok or bat >= rules["battery_resume_w"] or inv + rules["max_power_w"] >= config["rated_power_w"]:
            self._holds.pop("ev_start",None); return False
        rule = rules.get("start_rule_mode", "idle")
        now_dt = datetime.now(timezone.utc) + timedelta(hours=float(rules.get("time_zone_offset_h", 8)))
        def in_window(start, end):
            current = now_dt.hour * 60 + now_dt.minute
            a = int(start[:2]) * 60 + int(start[3:])
            b = int(end[:2]) * 60 + int(end[3:])
            return current >= a and current < b if a <= b else current >= a or current < b
        time_ok = in_window(rules.get("time_start", "12:00"), rules.get("time_end", "17:00"))
        forecast = ev.get("forecast_value")
        forecast_ok = forecast is not None and forecast >= rules.get("forecast_threshold", 0)
        rule_ok = {"idle": True, "time": time_ok, "forecast": forecast_ok,
                   "time_and_forecast": time_ok and forecast_ok,
                   "time_or_forecast": time_ok or forecast_ok}.get(rule, False)
        if not rule_ok:
            self._holds.pop("ev_start",None)
            return False
        # Reserve headroom for household level 2 and above; level 3 is the first sacrificial tier.
        limits=[config[f"threshold_{g}_w"] for g in (1, 2)]
        if any(inv + rules["max_power_w"] >= limit for limit in limits):
            self._holds.pop("ev_start",None); return False
        if time.time()-self.runtime.get("ev_last_off",0) < rules["min_off_s"]:
            self._holds.pop("ev_start",None); return False
        if not self._hold("ev_start",True,rules["start_hold_s"]):
            self.set_status(state="ev_wait",message="等待充电桩启动条件持续满足")
            return False
        return self._ev_action(config,ev,inv,bat,True,"光伏余量充足且家庭功率安全")

    def control_cycle(self):
        if self.stop.is_set() or not self._action_lock.acquire(blocking=False):
            return
        try:
            self._restore_context=None
            self._cycle()
            self._publish_restore_details()
        except Exception as exc:
            LOG.error("Control cycle failed (%s)",type(exc).__name__)
            self._holds.clear()
            self.set_status(state="api_error",message="状态或持久化访问失败，暂停自动开启负载",inverter_power_w=None,
                            battery_power_w=None,available_power_w=None,eligible_devices=[],devices=[],restore_queue=[])
        finally:
            self._action_lock.release()

    def _cycle(self):
        config=self.get_config()
        raw=self.telemetry.control_states() if self.telemetry is not None else self.ha_request("states")
        if not isinstance(raw,list) or not all(isinstance(row,dict) and isinstance(row.get("entity_id"),str) for row in raw):
            raise RuntimeError("Invalid HA states response")
        states={row["entity_id"]:row for row in raw}
        inv,bat,stamps,candidates,queue,ev=self._snapshot(config,states)
        if not config["enabled"]:
            self._holds.clear(); self.set_status(state="disabled",message=self.migration_notice or "自动控制已停用")
            return
        if not config["inverter_power_entity"] or not config["battery_power_entity"]:
            self._holds.clear(); self.set_status(state="not_configured",message="请配置逆变器和电池两路功率实体")
            return
        stop_reason=self._ev_stop_needed(config,ev,inv,bat,candidates)
        if stop_reason:
            # The charger is always sacrificed before ordinary household loads.
            self._ev_action(config,ev,inv,bat,False,stop_reason)
            return
        if candidates:
            self._holds.pop("restore",None)
            # Faulty other source does not mask a valid source's OR trip.
            ready=[d for d in candidates if any(self._fresh_after_action(config,stamps,[source]) for source in d["trigger_sources"])]
            if not ready:
                self.set_status(state="waiting_for_measurement",message="等待触发来源在上次动作后重新报告")
                return
            available=[d for d in ready if time.time()>=self._failures.get(d["switch_entity"],0)]
            if not available:
                self.set_status(state="actuation_failed",message="可切断设备均在重试冷却期")
                return
            device=available[0]
            reason=" / ".join("逆变器超阈值" if source=="inverter" else "电池放电超阈值" for source in device["trigger_sources"])
            self._act(config,device,"shed",False,inv,bat,reason)
            return
        if inv is None or bat is None:
            self._holds.clear(); self.set_status(state="source_unavailable",message="至少一路功率无效；有效来源仍可触发减载，暂停恢复")
            return
        if not self._fresh_after_action(config,stamps,["inverter","battery"]):
            self._holds.clear(); self.set_status(state="waiting_for_measurement",message="等待两路新的稳定功率报告")
            return
        if self._restore(config,states,inv,bat,queue):
            return
        if self._ev_start(config,ev,inv,bat,candidates,queue):
            return
        exceeds=any(triggers(config,g,inv,bat) for g in (1,2,3))
        self.set_status(state="no_sheddable_load" if exceeds else "normal",
                        message="有阈值超限，但没有可切断普通设备" if exceeds else "双路功率监测中")

    def loop(self):
        if self.telemetry is not None:
            self.telemetry.ready.wait(5.5)
        while not self.stop.is_set():
            started=time.monotonic()
            self._wake.clear()
            self.control_cycle()
            self._wake.wait(max(.05,self.get_config()["check_interval_s"]-(time.monotonic()-started)))

