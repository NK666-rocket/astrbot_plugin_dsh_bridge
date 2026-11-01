"""DSH 侧自举与自检：没有 profile 就按模板生成，配错就明确报出来。

设计原则：
- 只碰自己命名的 profile（默认 bot / bot-ro / bot-full），绝不改用户已有的其他 profile
- 已存在的 profile 不覆盖，只做校验（用户可能自己改过）
- 生成后立刻 `--dump-config` 验证，失败就回滚并保留原因
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

DEFAULT_HOME = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
PRESETS = {
    "ws": "bot-ws",
    "ro": "bot-ro",
    "full": "bot-full",
}

PNPM_WORKSPACE = """packages:
  - .

nodeLinker: hoisted
autoInstallPeers: false
"""

CORDIS_YML = """# dsh profile root — an empty entry list. The tree is composed as patches:
# each bundle in package.json's dsh.profile.bundles, then cordis.patch.yml, then any
# --patch overlays. Edit cordis.patch.yml, not this file.
[]
"""


def _patch_yaml(preset: str) -> str:
    return f"""# 由 astrbot_plugin_dsh_bridge 生成；改这里前先看插件 README
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: deepseek-official
    model: deepseek-flash
    reasoningEffort: high
- id: approval
  name: "@deepseek-ai/dsh-user-approval"
  config:
    policy: never
- id: permission
  name: "@deepseek-ai/dsh-permission-presets"
  config:
    defaultPreset: {preset}
    presets:
      bot-ro:
        sandbox: read-only
        approval: never
      bot-ws:
        sandbox: workspace-write
        approval: never
      bot-full:
        sandbox: danger-full-access
        approval: never
"""


def _package_json(name: str) -> str:
    return json.dumps(
        {
            "name": f"dsh-profile-{name}",
            "private": True,
            "dependencies": {},
            "dsh": {
                "profile": {
                    "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"]
                }
            },
        },
        ensure_ascii=False,
        indent=2,
    )


def _run(cli: str, args: list[str], timeout: int = 60) -> tuple[int, str]:
    flags = 0
    if os.name == "nt":
        flags = 0x08000000  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(
            [cli, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=flags,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def make_profile(home: Path, name: str, level: str) -> tuple[bool, str]:
    """按档位生成一个 profile 目录；已存在则不动。"""
    profile_dir = Path(home) / "profiles" / name
    preset = PRESETS[level]
    if profile_dir.exists():
        return True, f"{name} 已存在，保持原样"

    tmp = profile_dir.with_name(profile_dir.name + ".tmp")
    try:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        (tmp / "package.json").write_text(_package_json(name), encoding="utf-8")
        (tmp / "cordis.yml").write_text(CORDIS_YML, encoding="utf-8")
        (tmp / "pnpm-workspace.yaml").write_text(PNPM_WORKSPACE, encoding="utf-8")
        (tmp / "cordis.patch.yml").write_text(_patch_yaml(preset), encoding="utf-8")
        tmp.rename(profile_dir)
        return True, f"{name} 已生成（{preset}）"
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        return False, f"{name} 生成失败：{exc}"


def validate_profile(cli: str, name: str, level: str) -> tuple[bool, str]:
    code, out = _run(cli, ["--profile", name, "--dump-config"], timeout=90)
    if code != 0:
        tail = out.strip().splitlines()[-3:]
        return False, f"{name} 校验失败：" + "；".join(tail)[:300]
    preset = PRESETS[level]
    if f"defaultPreset: {preset}" not in out:
        return False, f"{name} 的 defaultPreset 不是 {preset}"
    if "policy: never" not in out:
        return False, f"{name} 的 approval 不是 never"
    return True, f"{name} 校验通过"


def ensure_profiles(cfg: dict, cli: str, home: Path | None = None) -> list[tuple[bool, str]]:
    """确保三档 profile 存在且可用，返回逐条结果。"""
    home = Path(home or DEFAULT_HOME)
    mapping = {"ws": "bot", "ro": "bot-ro", "full": "bot-full"}
    results: list[tuple[bool, str]] = []
    for level, name in mapping.items():
        ok, msg = make_profile(home, name, level)
        results.append((ok, msg))
        ok2, msg2 = validate_profile(cli, name, level)
        results.append((ok2, msg2))
    return results


def doctor(cfg: dict, cli: str, home: Path | None = None, api_key_available: bool = False) -> list[tuple[str, bool, str]]:
    """环境自检：返回 [(项目, 是否通过, 说明)]。"""
    checks: list[tuple[str, bool, str]] = []
    home = Path(home or DEFAULT_HOME)

    checks.append(("DSH CLI", bool(cli) and Path(cli).exists(), cli or "未找到"))

    if cli and Path(cli).exists():
        code, out = _run(cli, ["--version"], timeout=30)
        checks.append(("DSH 版本", code == 0, out.strip()[:80] or "无法获取"))
        code, out = _run(cli, ["headless", "--help"], timeout=30)
        checks.append(("session 复用支持", "--session-id" in out, "支持" if "--session-id" in out else "不支持（将每次新会话）"))
    else:
        checks.append(("DSH 版本", False, "跳过"))
        checks.append(("session 复用支持", False, "跳过"))

    ws = Path(cfg.get("scratch_dir") or (Path.home() / "dsh-bot-workspace"))
    try:
        ws.mkdir(parents=True, exist_ok=True)
        probe = ws / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        checks.append(("工作区可写", True, str(ws)))
    except Exception as exc:  # noqa: BLE001
        checks.append(("工作区可写", False, f"{ws}：{exc}"))

    if api_key_available:
        checks.append(("API Key", True, "未填，已从 AstrBot 的 DeepSeek provider 读到"))
    else:
        checks.append(("API Key", False, "未填，且 AstrBot 里也没找到 DeepSeek 密钥"))

    for name in ("bot", "bot-ro", "bot-full"):
        exists = (home / "profiles" / name).exists()
        checks.append((f"profile {name}", exists, "已存在" if exists else "缺失（可用 /dsh init 生成）"))
    return checks
