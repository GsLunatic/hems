"""Portable configuration backups; credentials and runtime ownership are excluded."""
from datetime import datetime, timezone

from settings import DEFAULT_CONFIG, validate_config

APP_VERSION = "0.5.13"
BACKUP_FORMAT = "home_energy_manager_config"


def export_backup(config):
    return {
        "format": BACKUP_FORMAT,
        "backup_version": 1,
        "app_version": APP_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": validate_config(config),
    }


def validate_backup(data):
    if not isinstance(data, dict) or set(data) != {"format", "backup_version", "app_version", "created_at", "config"}:
        raise ValueError("请选择本应用导出的完整 JSON 配置备份。")
    if data["format"] != BACKUP_FORMAT or type(data["backup_version"]) is not int or data["backup_version"] != 1:
        raise ValueError("备份格式或版本不支持，请使用兼容版本的应用恢复。")
    if not isinstance(data["app_version"], str) or len(data["app_version"]) > 40:
        raise ValueError("备份版本信息无效。")
    try:
        stamp = datetime.fromisoformat(data["created_at"])
        if stamp.tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("备份时间信息无效。") from None
    raw = data["config"]
    if not isinstance(raw, dict) or set(raw) - (set(DEFAULT_CONFIG) | {"ev"}):
        raise ValueError("备份配置字段不完整或版本不兼容。")
    raw = {key: value for key, value in raw.items() if key in DEFAULT_CONFIG}
    if set(raw) != set(DEFAULT_CONFIG):
        raise ValueError("备份配置字段不完整或版本不兼容。")
    device_fields = {"id", "name", "switch_entity", "power_entity", "grade", "enabled", "max_power_w"}
    if not isinstance(raw["devices"], list) or any(not isinstance(d, dict) or set(d) != device_fields for d in raw["devices"]):
        raise ValueError("备份设备配置不完整。")
    config = validate_config(raw)
    # Importing settings never grants permission to immediately operate appliances.
    config.update(enabled=False, dry_run=True)
    return config




