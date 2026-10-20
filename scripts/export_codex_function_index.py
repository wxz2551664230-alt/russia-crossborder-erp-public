"""Export route metadata only; never call business endpoints or external services."""
from __future__ import annotations

import inspect
import json
import re
from collections import defaultdict
from pathlib import Path

from fastapi.routing import APIRoute

from erp.main import app


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    destination = root / "docs" / "codex"
    destination.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list] = defaultdict(list)
    # Recent FastAPI versions keep included routers lazy; preserve their prefix.
    try:
        from fastapi.routing import _iter_routes_with_context
        routes_with_context = _iter_routes_with_context(app.routes)
    except ImportError:
        routes_with_context = ((route, None) for route in app.routes)
    for route, context in routes_with_context:
        effective_path = context.path if context is not None else getattr(route, "path", "")
        if isinstance(route, APIRoute) and effective_path.startswith("/api/v1/"):
            grouped[", ".join(route.tags) or "system"].append((effective_path, route))
    if not grouped:
        raise RuntimeError("No business routes found; refusing to write an empty index")
    lines = [
        "# ERP 全量接口索引（自动生成）", "",
        "生成方式：在项目根目录运行 `.venv\\Scripts\\python.exe scripts/export_codex_function_index.py`。",
        "仅导出路由元数据，不调用业务接口、不执行平台操作。先阅读根目录 `CODEX接管指南.md` 与 `docs/codex/ERP全功能地图.md`。",
        "存在路由不等于可用或获得执行授权。兼容旧程序的部分路由已禁用；非 GET 不一定是业务写入，请阅读实现。",
        "请求体、查询参数、枚举与响应结构见同目录 `openapi.json`。运行中的 `/openapi.json` 可用于核对版本。隐藏路由不在 OpenAPI 中，必须读取源码。", "",
    ]
    total = 0
    for group, routes in sorted(grouped.items()):
        lines += [f"## {group}", "", "| 方法 | 完整路径 | 处理函数与源码 | OpenAPI |", "|---|---|---|---|"]
        for effective_path, route in sorted(routes, key=lambda value: (value[0], sorted(value[1].methods))):
            path = Path(inspect.getsourcefile(route.endpoint)).resolve()
            relative = path.relative_to(root).as_posix()
            line = inspect.getsourcelines(route.endpoint)[1]
            methods = ", ".join(sorted(route.methods))
            lines.append(f"| {methods} | `{effective_path}` | `{relative}:{line}` · `{route.endpoint.__name__}` | {'是' if route.include_in_schema else '隐藏'} |")
            total += 1
        lines.append("")
    lines += [f"接口路由总数：{total}。同一路径的不同方法分别记录。", "", "## 前端页面与跳转入口", "",
              "来源：`frontend/src/router.ts`。页面可能只是重定向，不代表独立业务模块。", ""]
    source = (root / "frontend/src/router.ts").read_text(encoding="utf-8")
    paths = re.findall(r'\bpath:\s*"([^"]+)"', source)
    for path in dict.fromkeys(paths):
        lines.append(f"- `{path}`")
    (destination / "全量接口索引.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (destination / "openapi.json").write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Exported {total} API routes and {len(set(paths))} frontend paths; no business requests executed.")


if __name__ == "__main__":
    main()
