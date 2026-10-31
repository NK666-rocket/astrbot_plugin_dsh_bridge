"""不经过 QQ，直接验证 DSH 调用链。用法：
    python tools/probe.py "在当前目录建 test.txt，内容 hi"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from dsh_runner import load_api_key_from_astrbot, run_task  # noqa: E402


def main() -> int:
    task = " ".join(sys.argv[1:]).strip() or "在当前目录创建 probe.txt，内容写 probe-ok"
    data_dir = PLUGIN_DIR.parents[1]
    key = load_api_key_from_astrbot(data_dir)
    print(f"api_key: {'已从 AstrBot 读取' if key else '未找到'}")

    result = run_task(
        task,
        profile="bot",
        cwd=r"D:\dsh-bot-workspace",
        timeout=600,
        api_key=key,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
