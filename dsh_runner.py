"""DSH headless 调用封装：不依赖 AstrBot，可被插件与调试脚本共用。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0

def _cli_candidates() -> list[str]:
    """按平台给出常见的 DSH 安装位置（不写死任何个人路径）。"""
    out: list[str] = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or ""
        pf = os.environ.get("ProgramFiles") or ""
        pf86 = os.environ.get("ProgramFiles(x86)") or ""
        for base in (local + "/Programs", pf, pf86):
            if base.strip():
                out.append(str(Path(base) / "DeepSeek Harness/resources/runtime/cli/bin/dsh.cmd"))
    return out


def _registry_candidates() -> list[str]:
    """从 Windows 卸载表里找 DSH 安装目录（安装器一般会登记）。"""
    out: list[str] = []
    if os.name != "nt":
        return out
    try:
        import winreg
    except Exception:
        return out
    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    bases = (
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
        r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    for root in roots:
        for base in bases:
            try:
                with winreg.OpenKey(root, base) as key:
                    count = winreg.QueryInfoKey(key)[0]
                    for i in range(count):
                        try:
                            name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, name) as sub:
                                display = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                                if "DeepSeek" not in display and "Harness" not in display:
                                    continue
                                loc = str(winreg.QueryValueEx(sub, "InstallLocation")[0])
                                if loc:
                                    out.append(str(Path(loc) / "resources/runtime/cli/bin/dsh.cmd"))
                        except Exception:
                            continue
            except Exception:
                continue
    return out


CLI_CANDIDATES = _cli_candidates() + _registry_candidates()

BRIEF_RE = re.compile(r"<简报>(.*?)</简报>", re.S)
ARTIFACT_RE = re.compile(r"^\s*产物\s*[:：]\s*(.*)$", re.M)
WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|*?`\n]+")


def find_cli(configured: str = "") -> str:
    """解析 dsh CLI 路径：配置优先，其次常见安装位置，最后 PATH。"""
    if configured and Path(configured).exists():
        return configured
    for cand in CLI_CANDIDATES:
        if os.path.exists(cand):
            return cand
    found = shutil.which("dsh")
    return found or ""


_SESSION_SUPPORT: dict[str, bool] = {}


def supports_session(cli: str) -> bool:
    """探测 CLI 是否支持 --session-id；不支持时降级为每次新会话。"""
    if cli in _SESSION_SUPPORT:
        return _SESSION_SUPPORT[cli]
    ok = False
    try:
        out = subprocess.run(
            [cli, "headless", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        text = (out.stdout or "") + (out.stderr or "")
        ok = "--session-id" in text
    except Exception:
        ok = False
    _SESSION_SUPPORT[cli] = ok
    return ok


def load_api_key_from_astrbot(data_dir: str | Path) -> str:
    """从 AstrBot 的 provider_sources 里取 DeepSeek key（用户本机已有，避免重复配置）。"""
    cfg = Path(data_dir) / "cmd_config.json"
    if not cfg.exists():
        return ""
    try:
        data = json.loads(cfg.read_text(encoding="utf-8-sig"))
    except Exception:
        return ""
    for src in data.get("provider_sources", []) or []:
        if src.get("id") == "deepseek" or "api.deepseek.com" in str(src.get("api_base", "")):
            key = src.get("key")
            if isinstance(key, list) and key:
                return str(key[0])
            if isinstance(key, str) and key:
                return key
    return ""


def load_platform_id(data_dir: str | Path) -> str:
    """从 AstrBot 配置里取第一个 aiocqhttp 平台的 id（用于拼 UMO）。"""
    cfg = Path(data_dir) / "cmd_config.json"
    if not cfg.exists():
        return ""
    try:
        data = json.loads(cfg.read_text(encoding="utf-8-sig"))
    except Exception:
        return ""
    for p in data.get("platform", []) or []:
        if p.get("type") == "aiocqhttp":
            return str(p.get("id") or "")
    platforms = data.get("platform") or []
    if platforms:
        return str(platforms[0].get("id") or "")
    return ""


def extract_brief(text: str, limit: int = 0) -> str:
    """优先取 <简报> 块，其次取正文；limit>0 时裁剪。"""
    text = (text or "").strip()
    m = BRIEF_RE.search(text)
    body = m.group(1).strip() if m else re.sub(r"```.*?```", "", text, flags=re.S).strip()
    if limit and len(body) > limit:
        body = body[:limit].rstrip() + "…（已截断）"
    return body


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def collect_files(text: str, root: Path, max_mb: int = 50, max_count: int = 5, since_ts: float = 0.0) -> list[str]:
    """从回复的『产物：』段里提取工作区内的文件绝对路径。"""
    root = Path(root)
    candidates: list[str] = []
    for m in ARTIFACT_RE.finditer(text or ""):
        line = m.group(1).strip()
        if not line or line in ("无", "none", "-"):
            continue
        candidates.extend(WIN_PATH_RE.findall(line))
    # 没写『产物：』时，允许正文里出现的工作区路径也算（只取存在的文件）
    if not candidates:
        candidates = WIN_PATH_RE.findall(text or "")

    out: list[str] = []
    for raw in candidates:
        p = Path(raw.strip().rstrip("）)】].,，。;；"))
        try:
            if not p.is_file() or not _is_inside(p, root):
                continue
            if p.stat().st_size > max_mb * 1024 * 1024:
                continue
        except Exception:
            continue
        s = str(p.resolve())
        if s not in out:
            out.append(s)
        if len(out) >= max_count:
            break

    # 兜底：模型没写路径时，用任务时间段内新增/修改的文件
    if not out and since_ts > 0:
        try:
            for f in root.rglob("*"):
                if len(out) >= max_count:
                    break
                if not f.is_file():
                    continue
                rel = str(f.relative_to(root)).replace("\\", "/")
                if rel.startswith(".dsh-bridge/") or rel.startswith("inbox/"):
                    continue
                if f.stat().st_mtime < since_ts - 2:
                    continue
                if f.stat().st_size > max_mb * 1024 * 1024:
                    continue
                out.append(str(f.resolve()))
        except Exception:
            pass
    return out


def _kill_tree(pid: int) -> None:
    if IS_WIN:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        try:
            os.killpg(pid, 9)
        except Exception:
            pass


def run_task(
    task: str,
    *,
    cli_path: str = "",
    profile: str = "bot",
    cwd: str = "",
    timeout: int = 600,
    api_key: str = "",
    extra_env: dict | None = None,
    max_file_mb: int = 50,
    max_file_count: int = 5,
    save_full_over: int = 0,
    session_id: str = "",
) -> dict:
    """跑一次 DSH 任务，返回结构化结果。不抛异常，失败也返回 dict。"""
    started = time.time()
    if cli_path and not Path(cli_path).exists():
        return {
            "ok": False,
            "error": "配置的 dsh_cli_path 不存在：" + cli_path + "（把它清空可自动探测）",
            "duration": 0,
        }
    cli = find_cli(cli_path)
    if not cli:
        return {"ok": False, "error": "找不到 dsh CLI，请配置 dsh_cli_path", "duration": 0}

    workdir = Path(cwd) if cwd else (Path.home() / "dsh-bot-workspace")
    workdir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if api_key:
        env["DEEPSEEK_API_KEY"] = api_key
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})

    if session_id and not supports_session(cli):
        session_id = ""

    cmd = [cli, "--profile", profile, "--json"]
    if session_id:
        cmd += ["--session-id", session_id]
    cmd += ["-"]
    kwargs = dict(
        cwd=str(workdir),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if IS_WIN:
        kwargs["creationflags"] = CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"启动失败：{exc}", "duration": time.time() - started}

    try:
        # 任务走 stdin：dsh.cmd 是批处理，命令行参数里的换行会被 cmd.exe 截断
        out, err = proc.communicate(input=task, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:
            out, err = "", ""
        return {
            "ok": False,
            "error": f"超时（{timeout}s）",
            "stdout_tail": (out or "")[-500:],
            "stderr_tail": (err or "")[-500:],
            "duration": round(time.time() - started, 1),
        }

    final_text, session_id, usage = "", "", {}
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t = ev.get("type")
        if t == "session":
            session_id = ev.get("sessionId", "")
        elif t == "final":
            final_text = ev.get("text", "") or final_text
        elif t == "status" and ev.get("phase") == "step_end":
            usage = ev.get("usage") or usage

    duration = round(time.time() - started, 1)
    ok = proc.returncode == 0 and bool(final_text)

    full_path = ""
    brief = extract_brief(final_text)
    if save_full_over and len(final_text or "") > save_full_over:
        try:
            outdir = workdir / ".dsh-bridge" / "out"
            outdir.mkdir(parents=True, exist_ok=True)
            fp = outdir / time.strftime("%Y%m%d-%H%M%S.md")
            fp.write_text(final_text, encoding="utf-8")
            full_path = str(fp)
        except Exception:
            full_path = ""

    result = {
        "ok": ok,
        "final": final_text,
        "brief": brief,
        "session_id": session_id,
        "usage": usage,
        "duration": duration,
        "files": collect_files(final_text, workdir, max_file_mb, max_file_count, started) if ok else [],
        "full_path": full_path,
    }
    if not ok:
        tail = (err or out or "").strip().splitlines()[-6:]
        result["error"] = "；".join(x for x in tail if x)[:400] or f"退出码 {proc.returncode}"
    return result
