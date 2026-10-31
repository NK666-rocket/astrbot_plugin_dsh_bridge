"""配置归一化：把 AstrBot 表单里的原始值变成运行时可靠的类型。

表单里列表用逗号分隔字符串填，布尔可能被写成 "true"/"false"，枚举可能被写错——
这里统一兜底，避免运行期散落各种 try/except。
"""

from __future__ import annotations

LEVELS = ("ro", "ws", "full")
TRUE_SET = {"1", "true", "yes", "on", "y", "开", "是"}
FALSE_SET = {"0", "false", "no", "off", "n", "关", "否"}

DEFAULTS = {
    "enabled": True,
    "prefix": "dsh",
    "allowed_uins": [],
    "allowed_groups": [],
    "allow_private": True,
    "allow_group": False,
    "idle_minutes": 3,
    "idle_action": "submit",
    "scratch_dir": "",
    "dsh_cli_path": "",
    "profile_ws": "bot",
    "profile_ro": "bot-ro",
    "profile_full": "bot-full",
    "api_key": "",
    "default_permission": "ws",
    "max_permission": "ws",
    "allow_permission_switch": True,
    "session_mode": "reuse",
    "session_scope": "per_chat",
    "session_ttl_hours": 24,
    "session_notice": True,
    "timeout_sec": 600,
    "max_msg_chars": 4000,
    "max_buffer_chars": 8000,
    "brief_chars": 400,
    "file_max_mb": 50,
    "file_max_count": 5,
    "attach_max_mb": 50,
}

INT_RANGES = {
    "idle_minutes": (0, 1440),
    "timeout_sec": (30, 1800),
    "max_msg_chars": (200, 20000),
    "max_buffer_chars": (500, 50000),
    "brief_chars": (100, 4000),
    "file_max_mb": (1, 500),
    "file_max_count": (1, 20),
    "attach_max_mb": (1, 500),
}


def _as_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in TRUE_SET:
            return True
        if s in FALSE_SET:
            return False
    return default


def _as_int(v, default: int, lo: int, hi: int) -> int:
    try:
        n = int(float(v))
    except Exception:
        return default
    return max(lo, min(hi, n))


def _as_float(v, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _as_list(v) -> list[str]:
    if isinstance(v, (list, tuple, set)):
        items = [str(x).strip() for x in v]
    elif isinstance(v, str):
        items = [x.strip() for x in v.replace("，", ",").replace("\n", ",").split(",")]
    else:
        items = []
    return [x for x in items if x]


def _as_enum(v, allowed: tuple, default: str) -> str:
    s = str(v).strip().lower() if v is not None else ""
    aliases = {
        "read-only": "ro", "readonly": "ro", "只读": "ro", "read": "ro",
        "workspace-write": "ws", "workspace": "ws", "工作区写": "ws", "write": "ws",
        "danger-full-access": "full", "full-access": "full", "完全访问": "full", "all": "full",
    }
    s = aliases.get(s, s)
    return s if s in allowed else default


def normalize(conf) -> dict:
    """把 AstrBot 配置对象整理成运行时字典（含默认值与范围钳制）。"""
    raw = dict(DEFAULTS)
    try:
        for k in list(DEFAULTS.keys()):
            v = conf.get(k, None)
            if v is not None:
                raw[k] = v
    except Exception:
        pass

    out = dict(raw)
    out["enabled"] = _as_bool(raw["enabled"], True)
    out["allow_private"] = _as_bool(raw["allow_private"], True)
    out["allow_group"] = _as_bool(raw["allow_group"], False)
    out["allow_permission_switch"] = _as_bool(raw["allow_permission_switch"], True)
    out["session_notice"] = _as_bool(raw["session_notice"], True)

    out["prefix"] = str(raw["prefix"] or "dsh").strip().lstrip("/") or "dsh"
    out["allowed_uins"] = _as_list(raw["allowed_uins"])
    out["allowed_groups"] = _as_list(raw["allowed_groups"])

    out["idle_action"] = _as_enum(raw["idle_action"], ("submit", "discard"), "submit")
    out["session_mode"] = _as_enum(raw["session_mode"], ("reuse", "new"), "reuse")
    out["session_scope"] = _as_enum(raw["session_scope"], ("per_chat", "per_user", "global"), "per_chat")
    out["default_permission"] = _as_enum(raw["default_permission"], LEVELS, "ws")
    out["max_permission"] = _as_enum(raw["max_permission"], LEVELS, "ws")

    # 默认档不能高于上限
    if LEVELS.index(out["default_permission"]) > LEVELS.index(out["max_permission"]):
        out["default_permission"] = out["max_permission"]

    for key, (lo, hi) in INT_RANGES.items():
        out[key] = _as_int(raw[key], int(DEFAULTS[key]), lo, hi)
    out["session_ttl_hours"] = _as_float(raw["session_ttl_hours"], 24.0)
    if out["session_ttl_hours"] < 0:
        out["session_ttl_hours"] = 0.0

    for key in ("scratch_dir", "dsh_cli_path", "api_key", "profile_ws", "profile_ro", "profile_full"):
        out[key] = str(raw[key] or "").strip()

    if not out["scratch_dir"]:
        out["scratch_dir"] = ""  # 由插件按平台补默认（这里不猜用户目录）
    return out
