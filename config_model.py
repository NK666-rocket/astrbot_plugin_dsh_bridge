"""配置归一化：把 AstrBot 表单里的值整理成运行时可靠的类型。

要点：
- 表单是分组嵌套（object），这里先摊平成一层
- 白名单只接受 UMO（平台:类型:ID），非法项丢弃并记录
- 旧版平铺配置自动迁移（QQ 号 → UMO 需要平台 id）
- 数值统一钳制，避免用户填出离谱值
"""

from __future__ import annotations

import re

LEVELS = ("ro", "ws", "full")
UMO_RE = re.compile(r"^(?P<platform>.+):(?P<kind>FriendMessage|GroupMessage):(?P<id>\d+)$")

DEFAULTS = {
    "enabled": True,
    "prefix": "dsh",
    "admin_list": [],
    "scratch_dir": "",
    "dsh_cli_path": "",
    "allow_private": True,
    "private_whitelist": [],
    "private_default_permission": "ws",
    "allow_switch_private": True,
    "private_attach_max_mb": 50,
    "private_file_max_mb": 50,
    "private_file_max_count": 5,
    "allow_group": False,
    "group_shared_context": [],
    "group_isolated_context": [],
    "group_default_permission": "ro",
    "allow_switch_group": True,
    "group_attach_max_mb": 20,
    "group_file_max_mb": 20,
    "group_file_max_count": 3,
    "session_ttl_hours": 24.0,
    "session_notice": True,
    "timeout_sec": 600,
    "max_msg_chars": 4000,
    "max_buffer_chars": 8000,
    "brief_chars": 400,
    "idle_minutes": 3,
    "idle_action": "submit",
}

INT_RANGES = {
    "private_attach_max_mb": (1, 100),
    "private_file_max_mb": (1, 100),
    "private_file_max_count": (1, 10),
    "group_attach_max_mb": (1, 100),
    "group_file_max_mb": (1, 100),
    "group_file_max_count": (1, 5),
    "timeout_sec": (30, 1800),
    "max_msg_chars": (200, 20000),
    "max_buffer_chars": (500, 50000),
    "brief_chars": (100, 4000),
    "idle_minutes": (0, 1440),
}

LIST_KEYS = {"admin_list", "private_whitelist", "group_shared_context", "group_isolated_context"}

LEGACY_KEYS = {
    "allowed_uins", "allowed_groups", "allowed_sessions", "default_permission",
    "max_permission", "attach_max_mb", "file_max_mb", "file_max_count",
    "api_key", "session_scope", "profile_ws", "profile_ro", "profile_full",
    "allow_permission_switch",
}

TRUE_SET = {"1", "true", "yes", "on", "y", "开", "是"}
FALSE_SET = {"0", "false", "no", "off", "n", "关", "否"}


def parse_umo(value: str):
    """解析 UMO；返回 (platform, kind, id) 或 None。"""
    m = UMO_RE.match(str(value or "").strip())
    if not m:
        return None
    return m.group("platform"), m.group("kind"), m.group("id")


def is_friend_umo(value: str) -> bool:
    p = parse_umo(value)
    return bool(p) and p[1] == "FriendMessage"


def is_group_umo(value: str) -> bool:
    p = parse_umo(value)
    return bool(p) and p[1] == "GroupMessage"


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


def _as_float(v, default: float, lo: float, hi: float) -> float:
    try:
        n = float(v)
    except Exception:
        return default
    return max(lo, min(hi, n))


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


def _flatten(conf) -> dict:
    """把分组嵌套配置摊平；非 dict 值原样保留。"""
    out: dict = {}
    try:
        items = conf.items()
    except Exception:
        return out
    for key, value in items:
        if isinstance(value, dict):
            out.update(_flatten(value))
        else:
            out[key] = value
    return out


def _to_umo_list(values: list[str], kind: str, warnings: list[str], label: str) -> list[str]:
    good: list[str] = []
    for raw in values:
        parsed = parse_umo(raw)
        if not parsed:
            warnings.append(f"{label}：忽略非法项 {raw!r}（应为 平台:{kind}:ID）")
            continue
        if parsed[1] != kind:
            warnings.append(f"{label}：忽略类型不符的项 {raw!r}（这里只接受 {kind}）")
            continue
        if raw not in good:
            good.append(raw)
    return good


def _migrate(flat: dict, platform_id: str) -> tuple[dict, list[str]]:
    """旧版平铺配置 → 新版键；返回 (新字典, 说明)。"""
    warnings: list[str] = []
    out: dict = {}
    if not any(k in flat for k in LEGACY_KEYS):
        return out, warnings

    plat = platform_id or ""

    def to_umo(item: str, kind: str) -> str:
        p = parse_umo(item)
        if p:
            return item
        if plat and str(item).strip().isdigit():
            return f"{plat}:{kind}:{str(item).strip()}"
        return ""

    uins = _as_list(flat.get("allowed_uins")) + _as_list(flat.get("private_whitelist"))
    groups = _as_list(flat.get("allowed_groups")) + _as_list(flat.get("group_isolated_context"))
    sessions = _as_list(flat.get("allowed_sessions"))
    for s in sessions:
        p = parse_umo(s)
        if p and p[1] == "GroupMessage":
            groups.append(s)
        elif p:
            uins.append(s)
        elif plat and str(s).isdigit():
            uins.append(f"{plat}:FriendMessage:{s}")

    priv = [u for u in (to_umo(x, "FriendMessage") for x in uins) if u]
    grp = [u for u in (to_umo(x, "GroupMessage") for x in groups) if u]
    if priv:
        out["private_whitelist"] = priv
    if grp:
        out["group_isolated_context"] = grp

    d = flat.get("default_permission")
    if d:
        out["private_default_permission"] = _as_enum(d, LEVELS, "ws")
        out["group_default_permission"] = _as_enum(d, ("ro", "ws"), "ro")
    if flat.get("max_permission"):
        warnings.append(
            "旧配置 max_permission 已废弃：普通用户固定 ws、管理员固定 full"
        )
    if flat.get("api_key"):
        warnings.append("旧配置 api_key 已废弃：改为自动读取 AstrBot 的 DeepSeek 密钥")
    if flat.get("session_scope"):
        warnings.append("旧配置 session_scope 已废弃：私聊固定按人、群聊由两个群列表决定")

    if flat.get("attach_max_mb") is not None:
        out["private_attach_max_mb"] = flat["attach_max_mb"]
        out["group_attach_max_mb"] = flat["attach_max_mb"]
    if flat.get("file_max_mb") is not None:
        out["private_file_max_mb"] = flat["file_max_mb"]
        out["group_file_max_mb"] = flat["file_max_mb"]
    if flat.get("file_max_count") is not None:
        out["private_file_max_count"] = flat["file_max_count"]

    if out:
        warnings.append("已从旧版配置迁移：" + "、".join(sorted(out.keys())))
    return out, warnings


def normalize(conf, platform_id: str = "", overrides: dict | None = None) -> dict:
    """返回运行时配置字典；附带 _warnings / _conflicts 供 doctor 使用。"""
    try:
        raw_keys = set(conf.keys())
    except Exception:
        raw_keys = set()
    nested_present = bool(raw_keys & {"basic", "admin", "paths", "private", "group", "session", "limits"})

    flat = _flatten(conf)
    if not flat:
        try:
            flat = dict(conf)
        except Exception:
            flat = {}

    migrated, warnings = ({}, []) if nested_present else _migrate(flat, platform_id)
    merged = {**flat, **migrated}
    if overrides:
        merged.update({k: v for k, v in overrides.items() if k in DEFAULTS})

    out: dict = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in merged and merged[key] is not None:
            out[key] = merged[key]

    out["enabled"] = _as_bool(out["enabled"], True)
    out["allow_private"] = _as_bool(out["allow_private"], True)
    out["allow_group"] = _as_bool(out["allow_group"], False)
    out["allow_switch_private"] = _as_bool(out["allow_switch_private"], True)
    out["allow_switch_group"] = _as_bool(out["allow_switch_group"], True)
    out["session_notice"] = _as_bool(out["session_notice"], True)

    out["prefix"] = str(out["prefix"] or "dsh").strip().lstrip("/") or "dsh"
    out["scratch_dir"] = str(out["scratch_dir"] or "").strip()
    out["dsh_cli_path"] = str(out["dsh_cli_path"] or "").strip()

    out["private_default_permission"] = _as_enum(out["private_default_permission"], LEVELS, "ws")
    out["group_default_permission"] = _as_enum(out["group_default_permission"], ("ro", "ws"), "ro")
    out["idle_action"] = _as_enum(out["idle_action"], ("submit", "discard"), "submit")

    for key, (lo, hi) in INT_RANGES.items():
        out[key] = _as_int(out[key], int(DEFAULTS[key]), lo, hi)
    out["session_ttl_hours"] = _as_float(out["session_ttl_hours"], 24.0, 0.0, 24 * 365)

    out["admin_list"] = _to_umo_list(_as_list(out["admin_list"]), "FriendMessage", warnings, "管理员名单")
    out["private_whitelist"] = _to_umo_list(
        _as_list(out["private_whitelist"]), "FriendMessage", warnings, "私聊白名单"
    )
    shared = _to_umo_list(_as_list(out["group_shared_context"]), "GroupMessage", warnings, "共用上下文的群")
    isolated = _to_umo_list(_as_list(out["group_isolated_context"]), "GroupMessage", warnings, "每人独立的群")

    conflicts = [g for g in shared if g in isolated]
    if conflicts:
        warnings.append(
            "以下群同时出现在两个上下文列表，按「共用一个上下文」处理：" + "、".join(conflicts)
        )
        isolated = [g for g in isolated if g not in conflicts]
    out["group_shared_context"] = shared
    out["group_isolated_context"] = isolated

    if not platform_id:
        warnings.append("未取到平台 ID，旧版 QQ 号形式的白名单无法自动迁移")

    out["_warnings"] = warnings
    out["_conflicts"] = conflicts
    out["_platform_id"] = platform_id
    return out
