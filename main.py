"""把 QQ 消息桥接到本机 DeepSeek Harness（dsh headless）。

v2 配置模型：UMO 白名单、管理员名单、私聊/群聊分区、群聊两种上下文、一次性提权。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.event_message_type import EventMessageType

from .bootstrap import doctor as run_doctor, ensure_profiles
from .config_model import LEVELS, normalize, parse_umo
from .dsh_runner import load_api_key_from_astrbot, load_platform_id, run_task

NL = chr(10)
PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR.parents[1]  # .../core/data
STATE_FILE = PLUGIN_DIR / "state.json"

RESERVED = {"send", "cancel", "status", "help", "reset", "perm", "doctor", "init", "set"}
LEVEL_NAMES = {"ro": "只读", "ws": "工作区写", "full": "完全访问"}

SETTABLE = {
    "private_whitelist": "list",
    "group_shared_context": "list",
    "group_isolated_context": "list",
    "admin_list": "list",
    "private_default_permission": "perm",
    "group_default_permission": "perm",
    "private_attach_max_mb": "int",
    "private_file_max_mb": "int",
    "private_file_max_count": "int",
    "group_attach_max_mb": "int",
    "group_file_max_mb": "int",
    "group_file_max_count": "int",
}

HELP_TEXT = (
    "【/dsh 用法】" + NL
    + "1) /dsh 内容        开始一个新任务，也可以直接写在后面当第一段" + NL
    + "2) 直接发消息       继续追加片段（也可以直接发文件/图片）—— 这期间我不会回复" + NL
    + "3) /dsh send        提交上面所有片段，交给 DSH 执行" + NL
    + "4) /dsh cancel      取消这次的片段收集" + NL
    + "5) /dsh status      看当前状态" + NL
    + "6) /dsh perm        看/切换权限：perm ro|ws|full" + NL
    + "7) /dsh doctor      环境自检" + NL
    + "8) /dsh reset       清空本会话的 DSH 记忆" + NL
    + "9) /dsh help        看这份说明" + NL
    + "小提示：收集过程中 {idle} 分钟没有新片段，会自动{action}"
)

ADMIN_HELP = (
    "👑 管理员命令" + NL
    + "/dsh init                      创建缺失的 DSH profile" + NL
    + "/dsh perm full confirm         私聊：切到完全访问；群聊：一次性提权（仅下一条）" + NL
    + "/dsh set <键> <值> confirm     修改白名单 / 默认权限 / 文件上限" + NL
    + "可改键：" + "、".join(sorted(SETTABLE))
)


@dataclass
class Buffer:
    umo: str
    text: str = ""
    parts: int = 0
    updated: float = field(default_factory=time.time)
    attachments: list[str] = field(default_factory=list)


class DshBridgePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        self._lock = asyncio.Lock()
        self._buffers: dict[str, Buffer] = {}
        self._state: dict = {"sessions": {}, "perms": {}, "oneshot": {}, "overrides": {}}
        self._load_state()
        self.platform_id = load_platform_id(DATA_DIR)
        self.cfg = normalize(config, self.platform_id, self._state.get("overrides") or {})
        if not self.cfg["scratch_dir"]:
            self.cfg["scratch_dir"] = str(Path.home() / "dsh-bot-workspace")
        self._log_config()
        self._task = asyncio.create_task(self._idle_loop())
        self._boot = asyncio.create_task(self._bootstrap())

    async def terminate(self):
        self._task.cancel()
        self._boot.cancel()

    # ---------- 配置 ----------
    def _reload_cfg(self):
        self.cfg = normalize(self.conf, self.platform_id, self._state.get("overrides") or {})
        if not self.cfg["scratch_dir"]:
            self.cfg["scratch_dir"] = str(Path.home() / "dsh-bot-workspace")

    def _log_config(self):
        c = self.cfg
        logger.info(
            "[dsh-bridge] 配置加载：私聊白名单=" + str(len(c["private_whitelist"]))
            + " 群(共用/独立)=" + str(len(c["group_shared_context"])) + "/" + str(len(c["group_isolated_context"]))
            + " 管理员=" + str(len(c["admin_list"]))
            + " 私聊权限=" + c["private_default_permission"]
            + " 群权限=" + c["group_default_permission"]
            + " 工作区=" + c["scratch_dir"]
        )
        for w in c.get("_warnings", []):
            logger.warning("[dsh-bridge] " + w)

    def _workspace(self) -> Path:
        raw = self.cfg.get("scratch_dir") or ""
        return Path(raw) if raw else (Path.home() / "dsh-bot-workspace")

    def _prefix(self) -> str:
        return (str(self.cfg.get("prefix") or "dsh")).lower()

    # ---------- 状态 ----------
    def _load_state(self):
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k in ("sessions", "perms", "oneshot", "overrides"):
                        if isinstance(data.get(k), dict):
                            self._state[k] = data[k]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dsh-bridge] 状态读取失败：{exc}")

    def _save_state(self):
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, STATE_FILE)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dsh-bridge] 状态写入失败：{exc}")

    # ---------- 身份与会话键 ----------
    def _is_group(self, event: AstrMessageEvent) -> bool:
        return bool(event.get_group_id())

    def _umo(self, event: AstrMessageEvent) -> str:
        return str(event.unified_msg_origin or "")

    def _sender_umo(self, event: AstrMessageEvent) -> str:
        return f"{self.platform_id or event.get_platform_id()}:FriendMessage:{event.get_sender_id()}"

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admins = self.cfg.get("admin_list") or []
        return self._sender_umo(event) in admins or self._umo(event) in admins

    def _allowed(self, event: AstrMessageEvent) -> bool:
        umo = self._umo(event)
        if self._is_group(event):
            if not self.cfg["allow_group"]:
                return False
            return umo in (self.cfg["group_shared_context"] or []) or umo in (
                self.cfg["group_isolated_context"] or []
            )
        if not self.cfg["allow_private"]:
            return False
        return umo in (self.cfg["private_whitelist"] or [])

    def _group_shared(self, umo: str) -> bool:
        return umo in (self.cfg["group_shared_context"] or [])

    def _session_key(self, event: AstrMessageEvent, perm: str) -> str:
        umo = self._umo(event)
        if self._is_group(event) and not self._group_shared(umo):
            return f"{umo}|{event.get_sender_id()}|{perm}"
        return f"{umo}|{perm}"

    def _perm_cap(self, event: AstrMessageEvent) -> str:
        if self._is_group(event):
            return "ws"
        return "full" if self._is_admin(event) else "ws"

    def _perm_default(self, event: AstrMessageEvent) -> str:
        if self._is_group(event):
            return self.cfg["group_default_permission"]
        return self.cfg["private_default_permission"]

    def _perm_explicit(self, event: AstrMessageEvent, perm: str) -> str:
        cap = self._perm_cap(event)
        try:
            if LEVELS.index(perm) > LEVELS.index(cap):
                return cap
        except ValueError:
            return self._perm_default(event)
        return perm

    def _perm_base(self, event: AstrMessageEvent) -> str:
        key = self._session_key(event, "x")
        return key.rsplit("|", 1)[0]

    def _current_perm(self, event: AstrMessageEvent) -> str:
        stored = (self._state["perms"] or {}).get(self._perm_base(event)) or self._perm_default(event)
        if stored not in LEVELS:
            stored = self._perm_default(event)
        return self._perm_explicit(event, stored)

    def _set_perm(self, event: AstrMessageEvent, level: str):
        self._state["perms"][self._perm_base(event)] = level
        self._save_state()

    # ---------- 一次性提权 ----------
    def _oneshot_get(self, event: AstrMessageEvent) -> dict | None:
        rec = (self._state["oneshot"] or {}).get(self._umo(event))
        if not rec:
            return None
        if float(rec.get("expires") or 0) < time.time():
            self._state["oneshot"].pop(self._umo(event), None)
            self._save_state()
            return None
        return rec

    def _oneshot_set(self, event: AstrMessageEvent):
        self._state["oneshot"][self._umo(event)] = {
            "umo": self._sender_umo(event),
            "expires": time.time() + 600,
        }
        self._save_state()

    def _oneshot_clear(self, umo: str):
        if (self._state["oneshot"] or {}).pop(umo, None) is not None:
            self._save_state()

    # ---------- 附件 ----------
    async def _ingest_attachments(self, event: AstrMessageEvent) -> tuple[list[str], list[str]]:
        paths: list[str] = []
        errors: list[str] = []
        max_mb = self.cfg["group_attach_max_mb"] if self._is_group(event) else self.cfg["private_attach_max_mb"]
        try:
            segs = event.get_messages()
        except Exception:
            return paths, errors
        inbox = None
        for seg in segs:
            kinds = (File, Image) if File is not None else (Image,)
            if not isinstance(seg, kinds):
                continue
            name = (getattr(seg, "name", "") or "attachment").strip()
            try:
                local = await seg.get_file()
            except Exception as exc:  # noqa: BLE001
                errors.append(name + ": " + str(exc)[:80])
                continue
            if not local or not os.path.exists(local):
                errors.append(name + ": 下载失败")
                continue
            if os.path.getsize(local) > max_mb * 1024 * 1024:
                errors.append(name + f": 超过 {max_mb}MB")
                continue
            if inbox is None:
                inbox = self._workspace() / "inbox" / time.strftime("%Y%m%d-%H%M%S")
                inbox.mkdir(parents=True, exist_ok=True)
            dst = inbox / Path(name).name
            try:
                shutil.copy2(local, dst)
            except Exception as exc:  # noqa: BLE001
                errors.append(name + ": " + str(exc)[:80])
                continue
            paths.append(str(dst))
        return paths, errors

    # ---------- 解析 ----------
    def _scan_sub(self, raw: str) -> tuple[str | None, str]:
        p = self._prefix()
        sub: str | None = None
        kept: list[str] = []
        for line in (raw or "").splitlines():
            line = line.strip()
            if not line:
                continue
            s = line.lstrip("/").strip()
            low = s.lower()
            if low == p:
                continue
            if low.startswith(p):
                nxt = s[len(p): len(p) + 1]
                if nxt in (" ", "\t", "，", ",", "：", ":"):
                    word = s[len(p):].strip().split(" ")[0].lower()
                    if word in RESERVED:
                        sub = word
                        rest = s[len(p):].strip()[len(word):].strip()
                        if rest:
                            kept.append(rest)
                        continue
            kept.append(line)
        return sub, NL.join(kept).strip()

    def _strip_prefix(self, text: str) -> str:
        p = self._prefix()
        s = (text or "").lstrip("/").strip()
        low = s.lower()
        if low == p:
            return ""
        if low.startswith(p):
            return s[len(p):].lstrip(" \t\r\n，,：:").strip()
        return (text or "").strip()

    # ---------- 静默收集 ----------
    @filter.event_message_type(EventMessageType.ALL, priority=10)
    async def collect_listener(self, event: AstrMessageEvent):
        if not self.cfg["enabled"]:
            return
        if not self._allowed(event):
            return
        buf = self._buffers.get(self._umo(event))
        if not buf:
            return
        raw = (event.message_str or "").strip()
        if not raw:
            return
        p = self._prefix()
        low = raw.lstrip("/").lower()
        if low == p or (low.startswith(p) and raw.lstrip("/")[len(p): len(p) + 1] in (" ", "\t", "\n", "，", ",", "：", ":")):
            return
        if raw.startswith("/"):
            return
        buf.text = (buf.text + NL + raw).strip()
        buf.parts += 1
        buf.updated = time.time()
        event.stop_event()

    # ---------- 命令 ----------
    @filter.command("dsh")
    async def dsh(self, event: AstrMessageEvent):
        if not self.cfg["enabled"]:
            return
        if not self._allowed(event):
            logger.info(f"[dsh-bridge] 非白名单会话，忽略：{self._umo(event)}")
            return

        att_paths, att_errs = await self._ingest_attachments(event)
        sub, body = self._scan_sub(event.message_str or "")
        umo = self._umo(event)
        is_admin = self._is_admin(event)

        if sub == "help":
            text = self._help_text()
            if is_admin:
                text += NL + NL + ADMIN_HELP
            yield event.plain_result(text)
            return

        if sub == "status":
            yield event.plain_result(self._status_text(event))
            return

        if sub == "doctor":
            from .dsh_runner import find_cli
            cli = find_cli(self.cfg.get("dsh_cli_path") or "")
            checks = await asyncio.to_thread(run_doctor, self.cfg, cli, None, bool(self._api_key()))
            head = "🩺 环境自检" + NL
            if self.cfg.get("_conflicts"):
                head += "⚠️ 群上下文冲突：" + "、".join(self.cfg["_conflicts"]) + NL
            yield event.plain_result(head + self._format_checks(checks))
            return

        if sub == "cancel":
            if self._buffers.pop(umo, None):
                yield event.plain_result("🗑 已取消")
            else:
                yield event.plain_result("没有正在收集的任务")
            return

        if sub in ("reset", "init"):
            if not is_admin:
                yield event.plain_result("❌ 仅管理员可使用该命令")
                return
            if sub == "reset":
                for k in list(self._state["sessions"].keys()):
                    if k.startswith(umo + "|"):
                        self._state["sessions"].pop(k, None)
                self._save_state()
                yield event.plain_result("🔄 会话已重置，下次任务开新会话")
                return
            from .dsh_runner import find_cli
            cli = find_cli(self.cfg.get("dsh_cli_path") or "")
            if not cli:
                yield event.plain_result("❌ 找不到 dsh CLI，请先在插件配置里填 DSH CLI 路径")
                return
            results = await asyncio.to_thread(ensure_profiles, self.cfg, cli)
            ok_all = all(ok for ok, _ in results)
            yield event.plain_result(
                ("✅ 初始化完成" if ok_all else "⚠️ 初始化有问题") + NL + self._format_checks(results)
            )
            return

        if sub == "perm":
            yield event.plain_result(self._handle_perm(event, body))
            return

        if sub == "set":
            yield event.plain_result(self._handle_set(event, body))
            return

        if sub == "send":
            buf = self._buffers.pop(umo, None)
            if not buf or (not buf.text.strip() and not buf.attachments):
                yield event.plain_result("没有可提交的内容，先用 /dsh 内容 开始")
                return
            task = buf.text
            if buf.attachments:
                task += NL + NL + "附件（本机路径，请直接读取）：" + NL + NL.join(buf.attachments)
        else:
            text = self._strip_prefix(body)
            buf = self._buffers.get(umo)
            if buf:
                if text:
                    buf.text = (buf.text + NL + text).strip()
                    buf.parts += 1
                if att_paths:
                    buf.attachments.extend(att_paths)
                if text or att_paths:
                    buf.updated = time.time()
                    event.stop_event()
                    return
                preview = buf.text[:80].replace(NL, " ")
                yield event.plain_result(
                    "📝 正在收集（" + str(buf.parts) + " 段）：" + preview + "…" + NL
                    + "继续发片段，/dsh send 提交，/dsh cancel 取消"
                )
                return
            if not text and not att_paths:
                yield event.plain_result(self._help_text())
                return
            b = Buffer(umo=umo, text=text, parts=1, updated=time.time())
            b.attachments.extend(att_paths)
            self._buffers[umo] = b
            minutes = self.cfg["idle_minutes"]
            tail = ("（" + str(minutes) + " 分钟无新片段会自动提交）") if minutes > 0 else ""
            extra = ("，含 " + str(len(att_paths)) + " 个附件") if att_paths else ""
            yield event.plain_result(
                "🟢 已开始收集" + tail + extra + "，继续发片段，/dsh send 提交，/dsh cancel 取消"
            )
            return

        if len(task) > self.cfg["max_msg_chars"]:
            yield event.plain_result(f"任务太长（{len(task)} 字），上限 {self.cfg['max_msg_chars']} 字。")
            return
        if self._lock.locked():
            yield event.plain_result("⏳ 前面还有任务在跑，请稍后再发。")
            return

        if att_errs:
            yield event.plain_result("⚠️ 附件问题：" + "；".join(att_errs[:3]))
        yield event.plain_result("⏳ 收到，开始执行")

        result = await self._run(task, event)
        await self._deliver(umo, result)

    # ---------- 权限命令 ----------
    def _handle_perm(self, event: AstrMessageEvent, body: str) -> str:
        tokens = [t.lower() for t in (body or "").split()]
        is_group = self._is_group(event)
        is_admin = self._is_admin(event)
        cur = self._current_perm(event)

        if not tokens:
            extra = ""
            rec = self._oneshot_get(event)
            if rec:
                extra = NL + "⚡ 一次性完全访问剩余 " + str(int(rec["expires"] - time.time())) + " 秒"
            lines = ["当前权限：" + LEVEL_NAMES.get(cur, cur)]
            if is_group:
                lines.append("群聊持久上限：工作区写")
            lines.append(
                "可选：/dsh perm ro ｜ /dsh perm ws"
                + ("｜ /dsh perm full confirm" if is_admin else "")
            )
            return NL.join(lines) + extra

        level = tokens[0]
        if level not in LEVELS:
            return "未知权限：" + level + "，可选 ro / ws / full"

        if is_group and not self.cfg["allow_switch_group"]:
            return "群聊权限切换已被管理员关闭"
        if not is_group and not self.cfg["allow_switch_private"]:
            return "私聊权限切换已被管理员关闭"

        if is_group and not is_admin:
            return "❌ 仅管理员可在群里切换权限"
        if not is_admin and level == "full":
            return "❌ 完全访问是管理员专属"

        if level == "full" and "confirm" not in tokens:
            return "⚠️ 完全访问等于放开整台电脑，请再发一次：/dsh perm full confirm"

        if is_group and level == "full":
            self._oneshot_set(event)
            return "⚡ 已授予一次性完全访问：仅对下一条你发起的任务生效"

        self._set_perm(event, level)
        return "✅ 权限已切换为：" + LEVEL_NAMES[level]

    # ---------- 管理员改配置 ----------
    def _handle_set(self, event: AstrMessageEvent, body: str) -> str:
        if not self._is_admin(event):
            return "❌ 仅管理员可修改配置"
        parts = (body or "").split()
        if len(parts) < 3 or parts[-1].lower() != "confirm":
            return (
                "用法：/dsh set <键> <值> confirm" + NL
                + "可改键：" + "、".join(sorted(SETTABLE)) + NL
                + "示例：/dsh set private_whitelist 肥嘟嘟:FriendMessage:2809688590 confirm" + NL
                + "列表用逗号分隔；恢复后台值用 /dsh set <键> reset confirm"
            )
        key = parts[0]
        if key not in SETTABLE:
            return "❌ 不可修改的键：" + key + NL + "可改：" + "、".join(sorted(SETTABLE))
        value = " ".join(parts[1:-1]).strip()
        kind = SETTABLE[key]

        overrides = self._state.setdefault("overrides", {})
        if value.lower() == "reset":
            overrides.pop(key, None)
            self._save_state()
            self._reload_cfg()
            return "↩️ 已恢复该项为后台配置值"

        if kind == "perm":
            v = value.lower()
            if v not in LEVELS:
                return "❌ 权限值应为 ro / ws / full"
            overrides[key] = v
        elif kind == "int":
            try:
                overrides[key] = int(value)
            except Exception:
                return "❌ 需要整数"
        else:
            items = [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]
            bad = [x for x in items if not parse_umo(x)]
            if bad:
                return "❌ 这些不是合法 UMO：" + "、".join(bad) + NL + "示例：肥嘟嘟:FriendMessage:2809688590"
            overrides[key] = items

        self._save_state()
        self._reload_cfg()
        return "✅ 已更新 " + key + "（运行期覆盖，后台配置不变；用 reset 可恢复）"

    # ---------- 执行 ----------
    def _api_key(self) -> str:
        return load_api_key_from_astrbot(DATA_DIR)

    async def _run(self, task: str, event: AstrMessageEvent) -> dict:
        is_group = self._is_group(event)
        perm = self._current_perm(event)
        oneshot = self._oneshot_get(event)
        used_oneshot = False
        if oneshot and oneshot.get("umo") == self._sender_umo(event):
            perm = "full"
            used_oneshot = True

        profile = {"ro": "bot-ro", "ws": "bot", "full": "bot-full"}[perm]
        file_mb = self.cfg["group_file_max_mb"] if is_group else self.cfg["private_file_max_mb"]
        file_count = self.cfg["group_file_max_count"] if is_group else self.cfg["private_file_max_count"]

        skey = self._session_key(event, perm)
        sid, turns = "", 0
        if float(self.cfg["session_ttl_hours"]) > 0:
            sid, turns = self._get_session(skey)

        def invoke(session_id: str) -> dict:
            return run_task(
                task,
                cli_path=self.cfg.get("dsh_cli_path") or "",
                profile=profile,
                cwd=str(self._workspace()),
                timeout=self.cfg["timeout_sec"],
                api_key=self._api_key(),
                max_file_mb=file_mb,
                max_file_count=file_count,
                save_full_over=max(self.cfg["brief_chars"], 1) * 2,
                session_id=session_id,
            )

        async with self._lock:
            result = await asyncio.to_thread(invoke, sid)
            if (not result.get("ok")) and sid:
                retry = await asyncio.to_thread(invoke, "")
                if retry.get("ok"):
                    result = retry
                    sid, turns = "", 0
        if used_oneshot:
            self._oneshot_clear(self._umo(event))

        if result.get("ok"):
            new_sid = result.get("session_id") or sid
            self._set_session(skey, new_sid, turns + 1)
            result["session_note"] = ("续接第 " + str(turns + 1) + " 轮") if sid else "新会话"
        else:
            result["session_note"] = ""
        result["perm"] = LEVEL_NAMES.get(perm, perm) + ("（一次性）" if used_oneshot else "")
        return result

    def _get_session(self, key: str) -> tuple[str, int]:
        rec = (self._state["sessions"] or {}).get(key) or {}
        sid = str(rec.get("session_id") or "")
        turns = int(rec.get("turns") or 0)
        updated = float(rec.get("updated") or 0)
        ttl = float(self.cfg["session_ttl_hours"]) * 3600
        if not sid:
            return "", 0
        if ttl > 0 and time.time() - updated > ttl:
            self._state["sessions"].pop(key, None)
            self._save_state()
            return "", 0
        return sid, turns

    def _set_session(self, key: str, sid: str, turns: int):
        if not sid:
            return
        self._state["sessions"][key] = {"session_id": sid, "turns": turns, "updated": time.time()}
        self._save_state()

    # ---------- 自举 ----------
    async def _bootstrap(self):
        try:
            from .dsh_runner import find_cli
            cli = find_cli(self.cfg.get("dsh_cli_path") or "")
            if not cli:
                logger.warning("[dsh-bridge] 未找到 dsh CLI，桥功能不可用；请在插件配置里填 DSH CLI 路径")
                return
            results = await asyncio.to_thread(ensure_profiles, self.cfg, cli)
            for ok, msg in results:
                (logger.info if ok else logger.warning)("[dsh-bridge] " + msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dsh-bridge] 自举异常：{exc}")

    # ---------- 空闲 ----------
    async def _idle_loop(self):
        while True:
            try:
                await asyncio.sleep(20)
                minutes = int(self.cfg["idle_minutes"])
                if minutes <= 0:
                    continue
                now = time.time()
                for umo, buf in list(self._buffers.items()):
                    if now - buf.updated < minutes * 60:
                        continue
                    action = self.cfg["idle_action"]
                    self._buffers.pop(umo, None)
                    if action == "discard":
                        await self._send(umo, "⏰ 收集超时，已丢弃")
                        continue
                    if self._lock.locked():
                        await self._send(umo, "⏰ 收集超时，但前面还有任务在跑，已取消本次提交")
                        continue
                    await self._send(umo, "⏰ 收集超时，自动提交执行")
                    asyncio.create_task(self._auto_run(umo, buf))
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[dsh-bridge] 空闲任务异常：{exc}")

    async def _auto_run(self, umo: str, buf: Buffer):
        task = buf.text
        if buf.attachments:
            task += NL + NL + "附件（本机路径，请直接读取）：" + NL + NL.join(buf.attachments)
        perm = self.cfg["private_default_permission"]
        profile = {"ro": "bot-ro", "ws": "bot", "full": "bot-full"}[perm]
        result = await asyncio.to_thread(
            run_task,
            task,
            cli_path=self.cfg.get("dsh_cli_path") or "",
            profile=profile,
            cwd=str(self._workspace()),
            timeout=self.cfg["timeout_sec"],
            api_key=self._api_key(),
            max_file_mb=self.cfg["private_file_max_mb"],
            max_file_count=self.cfg["private_file_max_count"],
            save_full_over=max(self.cfg["brief_chars"], 1) * 2,
        )
        await self._deliver(umo, result)

    # ---------- 输出 ----------
    def _status_text(self, event: AstrMessageEvent) -> str:
        is_group = self._is_group(event)
        umo = self._umo(event)
        ctx = "共用" if (is_group and self._group_shared(umo)) else ("独立" if is_group else "—")
        buf = self._buffers.get(umo)
        lines = [
            "🪪 会话：" + umo,
            "身份：" + ("管理员" if self._is_admin(event) else "白名单用户"),
            "权限：" + LEVEL_NAMES.get(self._current_perm(event), "?"),
        ]
        if is_group:
            lines.append("群上下文：" + ctx + "（" + ("所有发言者共用" if ctx == "共用" else "每人独立") + "）")
        rec = self._oneshot_get(event)
        if rec:
            lines.append("⚡ 一次性完全访问：剩余 " + str(int(rec["expires"] - time.time())) + " 秒")
        if buf:
            lines.append("📝 收集中：" + str(buf.parts) + " 段，" + str(len(buf.text)) + " 字")
        else:
            lines.append("💤 没有收集中的任务")
        return NL.join(lines)

    def _format_checks(self, checks: list[tuple[str, bool, str]]) -> str:
        return NL.join(("✅ " if ok else "❌ ") + name + "：" + detail for name, ok, detail in checks)

    async def _send(self, umo: str, text: str):
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dsh-bridge] 主动发送失败：{exc}")

    def _format(self, result: dict) -> str:
        if not result.get("ok"):
            return f"❌ 执行失败：{str(result.get('error') or '未知错误')[:300]}"
        brief = result.get("brief") or "（DSH 没有返回内容）"
        limit = self.cfg["brief_chars"]
        if len(brief) > limit:
            brief = brief[:limit].rstrip() + "…（已截断）"
        full = result.get("full_path") or ""
        if full:
            brief += NL + "全文：" + full
        usage = result.get("usage") or {}
        meta = NL + NL + "— 用时 " + str(result.get("duration")) + "s"
        if usage.get("totalTokens"):
            meta += " · " + str(usage["totalTokens"]) + " tokens"
        files = result.get("files") or []
        if files:
            meta += " · " + str(len(files)) + " 个文件"
        if self.cfg["session_notice"] and result.get("session_note"):
            meta += " · " + str(result["session_note"])
        if result.get("perm"):
            meta += " · " + str(result["perm"])
        return "✅ 完成" + NL + brief + meta

    def _chunks(self, text: str, size: int = 1200) -> list[str]:
        out: list[str] = []
        buf = ""
        for line in text.splitlines(keepends=True):
            if len(buf) + len(line) > size and buf:
                out.append(buf.rstrip())
                buf = line
                while len(buf) > size:
                    out.append(buf[:size])
                    buf = buf[size:]
            else:
                buf += line
        if buf.strip():
            out.append(buf.rstrip())
        return out or [text]

    async def _deliver(self, umo: str, result: dict):
        files_all = result.get("files") or []
        if files_all:
            logger.info("[dsh-bridge] 回传文件：" + str(len(files_all)) + " 个 → " + umo)
        for i, chunk in enumerate(self._chunks(self._format(result))):
            if i:
                await asyncio.sleep(0.6)
            await self._send(umo, chunk)
        count = self.cfg["group_file_max_count"] if "GroupMessage" in umo else self.cfg["private_file_max_count"]
        for path in (result.get("files") or [])[:count]:
            if File is None:
                await self._send(umo, "📎 文件（当前 AstrBot 不支持直接发文件）：" + path)
                continue
            try:
                await asyncio.sleep(0.4)
                await self.context.send_message(umo, MessageChain([File(name=Path(path).name, file=path)]))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[dsh-bridge] 文件发送失败 {path}: {exc}")
                await self._send(umo, "⚠️ 文件发送失败：" + Path(path).name)

    def _help_text(self) -> str:
        action = "提交" if self.cfg["idle_action"] == "submit" else "丢弃"
        return HELP_TEXT.format(idle=self.cfg["idle_minutes"], action=action)
