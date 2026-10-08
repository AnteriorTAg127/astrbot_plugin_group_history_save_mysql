# ruff: noqa
"""Agent-D 临时脚本：从 core/webapi/base.py 源码按序提取路由表（路径/回调/方法/描述）。

用法：python _agentd_routes_extract.py <输出json>
重构前后各跑一次，diff 两份 json 即可逐个核对 42 条路由零变化。
"""

import json
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "core" / "webapi" / "base.py"

TUPLE_RE = re.compile(
    r"\(\s*f\"/\{PLUGIN_NAME\}([^\"]*)\",\s*self\.(api_\w+),\s*\[([^\]]*)\],\s*\"([^\"]*)\",?\s*\)",
    re.S,
)


def main() -> None:
    src = SRC.read_text(encoding="utf-8")
    rows = []
    for path, handler, methods, desc in TUPLE_RE.findall(src):
        ms = [m.strip().strip('"') for m in methods.split(",") if m.strip()]
        rows.append({"path": f"/{{PLUGIN_NAME}}{path}", "handler": handler, "methods": ms, "desc": desc})
    out = Path(sys.argv[1])
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(rows)} routes -> {out}")
    # 校验无遗漏：源码中 f"/{PLUGIN_NAME} 出现次数应与提取行数一致
    raw_count = len(re.findall(r'f"/\{PLUGIN_NAME\}', src))
    print(f"raw path-literals in source: {raw_count}")
    if raw_count != len(rows):
        print("WARNING: literal count != extracted rows")


if __name__ == "__main__":
    main()
