"""把 QQ 消息桥接到本机 DeepSeek Harness（dsh headless）。

S5：会话复用（含 TTL）、权限三档切换、附件接收、私聊/群聊白名单。
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
try:
    from astrbot.api.message_components import File, Image, Plain
except Exception:  # 旧版 AstrBot 没有 File 组件
    from astrbot.api.message_components import Image, Plain  # type: ignore

    File = None  # type: ignore
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.event_message_type import EventMessageType

from .bootstrap import doctor as run_doctor, ensure_profiles
from .config_model import normalize
from .dsh_runner import load_api_key_from_astrbot, run_task

NL = chr(10)
PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR.parents[1]  # .../core/data
STATE_FILE = PLUGIN_DIR / "state.json"

RESERVED = {"send", "cancel", "status", "help", "reset", "perm", "doctor", "init"}
LEVELS = ["ro", "ws", "full"]
LEVEL_NAMES = {"ro": "只读", "ws": "工作区写", "full": "完全访问"}

HELP_TEXT = (
    "【/dsh 用法】" + NL
    + "1) /dsh 内容        开始一个新任务，也可以直接写在后面当第一段" + NL
    + "2) 直接发消息       继续追加片段（也可以直接发文件/图片）—— 这期间我不会回复" + NL
    + "3) /dsh send        提交上面所有片段，交给 DSH 执行" + NL
    + "4) /dsh cancel      取消这次的片段收集" + NL
    + "5) /dsh status      看当前状态" + NL
    + "6) /dsh perm        看/切换权限：perm ro|ws|full" + NL
    + "7) /dsh reset       清空和 DSH 的对话记忆（下次开新会话）" + NL
    + "8) /dsh doctor      环境自检（CLI / 凭据 / profile / 工作区）" + NL
    + "9) /dsh init        自动创建缺失的 DSH profile" + NL
    + "10) /dsh help      看这份说明" + NL
    + "小提示：收集过程中 {idle} 分钟没有新片段，会自动{action}"
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
        self.cfg = normalize(config)
        if not self.cfg["scratch_dir"]:
            self.cfg["scratch_dir"] = str(Path.home() / "dsh-bot-workspace")
        self._lock = asyncio.Lock()
        self._buffers: dict[str, Buffer] = {}
        logger.info(
            "[dsh-bridge] 配置加载：白名单=" + str(self.cfg["allowed_uins"])
            + " 群聊=" + str(self.cfg["allow_group"])
            + " 权限=" + self.cfg["default_permission"] + "/" + self.cfg["max_permission"]
            + " 工作区=" + self.cfg["scratch_dir"]
        )
        self._state: dict = {"sessions": {}, "perms": {}}
        self._load_state()
        self._task = asyncio.create_task(self._idle_loop())
        self._boot = asyncio.create_task(self._bootstrap())

    async def terminate(self):
        self._task.cancel()
        self._boot.cancel()

    # ---------- 配置（已由 config_model 归一化） ----------
    def _str(self, key: str, default: str = "") -> str:
        v = self.cfg.get(key, default)
        return str(v).strip() if v is not None else default

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self.cfg.get(key, default))
        except Exception:
            return default

    def _num(self, key: str, default: float) -> float:
        try:
            return float(self.cfg.get(key, default))
        except Exception:
            return default

    def _list(self, key: str) -> list[str]:
        v = self.cfg.get(key, [])
        return list(v) if isinstance(v, (list, tuple)) else []

    def _bool(self, key: str, default: bool) -> bool:
        v = self.cfg.get(key, default)
        return bool(v)

    async def _bootstrap(self):
        """加载后自检 DSH 环境：缺 profile 就建，结果写日志。"""
        try:
            from .dsh_runner import find_cli
            cli = find_cli(self._str("dsh_cli_path"))
            if not cli:
                logger.warning("[dsh-bridge] 未找到 dsh CLI，桥功能不可用；请检查插件配置")
                return
            results = await asyncio.to_thread(ensure_profiles, self.cfg, cli)
            for ok, msg in results:
                (logger.info if ok else logger.warning)("[dsh-bridge] " + msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dsh-bridge] 自举异常：{exc}")

    def _format_checks(self, checks: list[tuple[str, bool, str]]) -> str:
        lines = []
        for name, ok, detail in checks:
            lines.append(("✅ " if ok else "❌ ") + name + "：" + detail)
        return NL.join(lines)

    # ---------- 状态持久化 ----------
    def _load_state(self):
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._state["sessions"] = data.get("sessions", {}) or {}
                    self._state["perms"] = data.get("perms", {}) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dsh-bridge] 状态读取失败：{exc}")

    def _save_state(self):
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, STATE_FILE)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dsh-bridge] 状态写入失败：{exc}")

    # ---------- 会话 / 权限键 ----------
    def _scope_key(self, event: AstrMessageEvent) -> str:
        scope = (self._str("session_scope", "per_chat") or "per_chat").lower()
        platform = str(event.get_platform_id())
        group = str(event.get_group_id() or "")
        chat = group or str(event.get_sender_id())
        if scope == "global":
            return "global"
        if scope == "per_user":
            return f"{platform}|{chat}|{event.get_sender_id()}"
        return f"{platform}|{chat}"

    def _get_session(self, key: str) -> tuple[str, int]:
        rec = (self._state["sessions"] or {}).get(key) or {}
        sid = str(rec.get("session_id") or "")
        turns = int(rec.get("turns") or 0)
        updated = float(rec.get("updated") or 0)
        ttl = self._num("session_ttl_hours", 24)
        if not sid:
            return "", 0
        if ttl > 0 and time.time() - updated > ttl * 3600:
            self._state["sessions"].pop(key, None)
            self._save_state()
            return "", 0
        return sid, turns

    def _set_session(self, key: str, sid: str, turns: int):
        if not sid:
            return
        self._state["sessions"][key] = {"session_id": sid, "turns": turns, "updated": time.time()}
        self._save_state()

    def _perm_index(self, level: str) -> int:
        try:
            return LEVELS.index(level)
        except ValueError:
            return 1

    def _perm_for(self, key: str) -> str:
        cur = str((self._state["perms"] or {}).get(key) or self._str("default_permission", "ws"))
        if cur not in LEVELS:
            cur = "ws"
        cap = self._str("max_permission", "ws")
        if self._perm_index(cur) > self._perm_index(cap):
            cur = cap if cap in LEVELS else "ws"
        return cur

    def _set_perm(self, key: str, level: str):
        self._state["perms"][key] = level
        self._save_state()

    def _profile_for(self, level: str) -> str:
        default_profile = self._str("profile_ws", "bot") or "bot"
        if level == "ro":
            return self._str("profile_ro", "bot-ro") or default_profile
        if level == "full":
            return self._str("profile_full", "bot-full") or default_profile
        return default_profile

    # ---------- 鉴权 ----------
    def _allowed(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id())
        group = str(event.get_group_id() or "")
        if group:
            if not self._bool("allow_group", False):
                return False
            return group in self._list("allowed_groups") and sender in self._list("allowed_uins")
        if not self._bool("allow_private", True):
            return False
        return sender in self._list("allowed_uins")

    def _api_key(self) -> str:
        return self._str("api_key") or load_api_key_from_astrbot(DATA_DIR)

    def _workspace(self) -> Path:
        """工作区目录；未配置时跟随用户目录，跨平台安全。"""
        raw = self._str("scratch_dir")
        return Path(raw) if raw else (Path.home() / "dsh-bot-workspace")

    def _prefix(self) -> str:
        return (self._str("prefix", "dsh") or "dsh").lower()

    def _key(self, event: AstrMessageEvent) -> str:
        kind = "group" if event.get_group_id() else "private"
        chat = str(event.get_group_id() or event.get_sender_id())
        return f"{event.get_platform_id()}|{kind}|{chat}|{event.get_sender_id()}"

    # ---------- 附件 ----------
    async def _ingest_attachments(self, event: AstrMessageEvent) -> tuple[list[str], list[str]]:
        """把消息里的文件/图片下载到 scratch_dir/inbox/<时间戳>/，返回 (路径, 错误)。"""
        paths: list[str] = []
        errors: list[str] = []
        max_mb = self._int("attach_max_mb", 50)
        try:
            segs = event.get_messages()
        except Exception:
            return paths, errors
        inbox = None
        for seg in segs:
            kinds = (Image,) if File is None else (File, Image)
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
                errors.append(name + ": 超过 " + str(max_mb) + "MB")
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

    # ---------- 文本解析 ----------
    def _scan_sub(self, raw: str) -> tuple[str | None, str]:
        """扫描整条消息（可含多行）里的 /dsh send|cancel|status|help|reset|perm。"""
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
                        trailing = s[len(p):].strip()[len(word):].strip()
                        if trailing:
                            kept.append(trailing)
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
        """收集期间的普通消息：并入缓冲并静默，不触发其它处理器。"""
        if not self.conf.get("enabled", True):
            return
        if not self._allowed(event):
            return
        buf = self._buffers.get(self._key(event))
        if not buf:
            return
        raw = (event.message_str or "").strip()
        if not raw:
            return
        p = self._prefix()
        low = raw.lstrip("/").lower()
        head = raw.lstrip("/")[len(p): len(p) + 1]
        if low == p or (low.startswith(p) and head in (" ", "\t", "\n", "，", ",", "：", ":")):
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
        """把任务交给本机 DSH；多段内容用 /dsh send 提交。"""
        if not self.conf.get("enabled", True):
            return
        if not self._allowed(event):
            logger.info(f"[dsh-bridge] 非白名单会话，忽略：{event.unified_msg_origin}")
            return

        att_paths, att_errs = await self._ingest_attachments(event)
        sub, body = self._scan_sub(event.message_str or "")
        key = self._key(event)
        skey = self._scope_key(event)

        if sub == "help":
            yield event.plain_result(self._help_text())
            return

        if sub == "status":
            buf = self._buffers.get(key)
            perm = self._perm_for(skey)
            sid, turns = self._get_session(skey + "#" + perm)
            sess = "续接第 " + str(turns) + " 轮" if sid and turns else "新会话"
            if buf:
                yield event.plain_result(
                    "📝 正在收集：" + str(buf.parts) + " 段，" + str(len(buf.text)) + " 字"
                    + ("，附件 " + str(len(buf.attachments)) + " 个" if buf.attachments else "")
                    + NL + "权限：" + LEVEL_NAMES.get(perm, perm) + " ｜ 会话：" + sess
                )
            else:
                yield event.plain_result(
                    "💤 没有收集中的任务 ｜ 权限：" + LEVEL_NAMES.get(perm, perm) + " ｜ 会话：" + sess
                )
            return

        if sub == "cancel":
            if self._buffers.pop(key, None):
                yield event.plain_result("🗑 已取消")
            else:
                yield event.plain_result("没有正在收集的任务")
            return

        if sub == "reset":
            for k in list(self._state["sessions"].keys()):
                if k == skey or k.startswith(skey + "#"):
                    self._state["sessions"].pop(k, None)
            self._save_state()
            yield event.plain_result("🔄 会话已重置，下次任务开新会话")
            return

        if sub == "perm":
            yield event.plain_result(self._handle_perm(event, skey, body))
            return

        if sub == "doctor":
            from .dsh_runner import find_cli
            cli = find_cli(self._str("dsh_cli_path"))
            checks = await asyncio.to_thread(run_doctor, self.cfg, cli, None, bool(self._api_key()))
            yield event.plain_result("🩺 环境自检" + NL + self._format_checks(checks))
            return

        if sub == "init":
            from .dsh_runner import find_cli
            cli = find_cli(self._str("dsh_cli_path"))
            if not cli:
                yield event.plain_result("❌ 找不到 dsh CLI，请先在插件配置里填 dsh_cli_path")
                return
            results = await asyncio.to_thread(ensure_profiles, self.cfg, cli)
            ok_all = all(ok for ok, _ in results)
            yield event.plain_result(
                ("✅ 初始化完成" if ok_all else "⚠️ 初始化有问题") + NL + self._format_checks(results)
            )
            return

        if sub == "send":
            buf = self._buffers.pop(key, None)
            if not buf or (not buf.text.strip() and not buf.attachments):
                yield event.plain_result("没有可提交的内容，先用 /dsh 内容 开始")
                return
            task = buf.text
            if buf.attachments:
                task += NL + NL + "附件（本机路径，请直接读取）：" + NL + NL.join(buf.attachments)
        else:
            text = self._strip_prefix(body)
            buf = self._buffers.get(key)
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
            b = Buffer(umo=event.unified_msg_origin, text=text, parts=1, updated=time.time())
            b.attachments.extend(att_paths)
            self._buffers[key] = b
            minutes = self._int("idle_minutes", 3)
            tail = ("（" + str(minutes) + " 分钟无新片段会自动提交）") if minutes > 0 else ""
            extra = ("，含 " + str(len(att_paths)) + " 个附件") if att_paths else ""
            yield event.plain_result(
                "🟢 已开始收集" + tail + extra + "，继续发片段，/dsh send 提交，/dsh cancel 取消"
            )
            return

        max_chars = self._int("max_msg_chars", 4000)
        if len(task) > max_chars:
            yield event.plain_result(f"任务太长（{len(task)} 字），上限 {max_chars} 字。")
            return
        if self._lock.locked():
            yield event.plain_result("⏳ 前面还有任务在跑，请稍后再发。")
            return

        if att_errs:
            yield event.plain_result("⚠️ 附件问题：" + "；".join(att_errs[:3]))
        yield event.plain_result("⏳ 收到，开始执行")

        result = await self._run(task, skey)
        await self._deliver(event.unified_msg_origin, result)

    # ---------- 权限命令 ----------
    def _handle_perm(self, event: AstrMessageEvent, skey: str, body: str) -> str:
        tokens = [t.lower() for t in (body or "").split()]
        cur = self._perm_for(skey)
        if not tokens:
            return ("当前权限：" + LEVEL_NAMES.get(cur, cur) + NL
                    + "可选：/dsh perm ro（只读）｜ ws（工作区写）｜ full（完全访问，需私聊 + confirm）")
        level = tokens[0]
        if level not in LEVELS:
            return "未知权限：" + level + "，可选 ro / ws / full"
        if not self._bool("allow_permission_switch", True):
            return "权限切换已被配置关闭"
        cap = self._str("max_permission", "ws")
        if self._perm_index(level) > self._perm_index(cap):
            return ("超出权限上限（当前上限：" + LEVEL_NAMES.get(cap, cap) + "），"
                    "需要在插件配置里把 max_permission 调高")
        if level == "full" and event.get_group_id():
            return "完全访问只能在私聊里切换"
        if level == "full" and "confirm" not in tokens:
            return "⚠️ 切到完全访问等于放开整台电脑，请再发一次：/dsh perm full confirm"
        self._set_perm(skey, level)
        return "✅ 权限已切换为：" + LEVEL_NAMES.get(level, level)

    # ---------- 执行 ----------
    async def _run(self, task: str, skey: str) -> dict:
        perm = self._perm_for(skey)
        profile = self._profile_for(perm)
        sess_key = skey + "#" + perm
        sid, turns = "", 0
        if (self._str("session_mode", "reuse") or "reuse").lower() == "reuse":
            sid, turns = self._get_session(sess_key)
        def invoke(session_id: str) -> dict:
            return run_task(
                task,
                cli_path=self._str("dsh_cli_path"),
                profile=profile,
                cwd=str(self._workspace()),
                timeout=self._int("timeout_sec", 600),
                api_key=self._api_key(),
                max_file_mb=self._int("file_max_mb", 50),
                max_file_count=self._int("file_max_count", 5),
                save_full_over=max(self._int("brief_chars", 400), 1) * 2,
                session_id=session_id,
            )

        async with self._lock:
            result = await asyncio.to_thread(invoke, sid)
            if (not result.get("ok")) and sid:
                # 会话可能已失效或属于别的 profile：退化为新会话重试一次
                retry = await asyncio.to_thread(invoke, "")
                if retry.get("ok"):
                    result = retry
                    sid, turns = "", 0
        if result.get("ok"):
            new_sid = result.get("session_id") or sid
            self._set_session(sess_key, new_sid, turns + 1)
            result["session_note"] = ("续接第 " + str(turns + 1) + " 轮") if sid else "新会话"
        else:
            result["session_note"] = ""
        result["perm"] = LEVEL_NAMES.get(perm, perm)
        return result

    # ---------- 空闲自动动作 ----------
    async def _idle_loop(self):
        while True:
            try:
                await asyncio.sleep(20)
                minutes = self._int("idle_minutes", 3)
                if minutes <= 0:
                    continue
                now = time.time()
                for key, buf in list(self._buffers.items()):
                    if now - buf.updated < minutes * 60:
                        continue
                    action = (self._str("idle_action", "submit") or "submit").lower()
                    self._buffers.pop(key, None)
                    if action == "discard":
                        await self._send(buf.umo, "⏰ 收集超时，已丢弃")
                        continue
                    if self._lock.locked():
                        await self._send(buf.umo, "⏰ 收集超时，但前面还有任务在跑，已取消本次提交")
                        continue
                    await self._send(buf.umo, "⏰ 收集超时，自动提交执行")
                    asyncio.create_task(self._auto_run(buf.umo, key, buf))
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[dsh-bridge] 空闲任务异常：{exc}")

    async def _auto_run(self, umo: str, key: str, buf: Buffer):
        task = buf.text
        if buf.attachments:
            task += NL + NL + "附件（本机路径，请直接读取）：" + NL + NL.join(buf.attachments)
        parts = key.split("|")
        skey = "|".join(parts[:2]) if len(parts) >= 2 else key
        result = await self._run(task, skey)
        await self._deliver(umo, result)

    async def _send(self, umo: str, text: str):
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[dsh-bridge] 主动发送失败：{exc}")

    # ---------- 输出 ----------
    def _format(self, result: dict) -> str:
        if not result.get("ok"):
            return f"❌ 执行失败：{str(result.get('error') or '未知错误')[:300]}"
        brief = result.get("brief") or "（DSH 没有返回内容）"
        limit = self._int("brief_chars", 400)
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
        if self._bool("session_notice", True) and result.get("session_note"):
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
        for i, chunk in enumerate(self._chunks(self._format(result))):
            if i:
                await asyncio.sleep(0.6)
            await self._send(umo, chunk)
        for path in (result.get("files") or [])[: self._int("file_max_count", 5)]:
            if File is None:
                await self._send(umo, "📎 文件（当前 AstrBot 不支持直接发文件）：" + path)
                continue
            try:
                await asyncio.sleep(0.4)
                await self.context.send_message(
                    umo, MessageChain([File(name=Path(path).name, file=path)])
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[dsh-bridge] 文件发送失败 {path}: {exc}")
                await self._send(umo, "⚠️ 文件发送失败：" + Path(path).name)

    def _help_text(self) -> str:
        minutes = self._int("idle_minutes", 3)
        action = "提交" if (self._str("idle_action", "submit") or "submit") == "submit" else "丢弃"
        return HELP_TEXT.format(idle=minutes, action=action)
