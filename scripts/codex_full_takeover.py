"""Print a secret-free, current project snapshot for a new Codex chat.

This is a project handoff, not a second ERP business executor.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = (
    'AGENTS.md',
    'CODEX接管指南.md',
    'docs/codex/ERP全功能地图.md',
    'docs/codex/全量接口索引.md',
    'docs/codex/openapi.json',
    'reports/migration-baseline-20260923/移植基准与审查记录.md',
)


def git(*args: str) -> str:
    try:
        return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True,
                              text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def snapshot() -> dict:
    files = {name: (ROOT / name).is_file() for name in DOCS}
    changed = git('status', '--porcelain=v1', '-uno').splitlines()
    return {
        'project_root': str(ROOT),
        'git_branch': git('branch', '--show-current'),
        'git_head': git('rev-parse', '--short', 'HEAD'),
        'tracked_changed_file_count': len(changed),
        'required_documents': files,
        'backend_test_command': r'.venv\Scripts\python.exe -m pytest',
        'frontend_build_command': 'npm run build',
        'api_index_command': r'.venv\Scripts\python.exe scripts/export_codex_function_index.py',
        'desktop_mcp_current_scope': ['shared_product_search', 'shared_product_read', 'product_notes_compare_and_save'],
        'business_execution_rule': 'Business jobs run only on the initiating user computer; server coordinates and stores shared results.',
        'old_source_directories': 'Read-only, permanently preserved; see AGENTS.md.',
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='emit machine-readable snapshot')
    args = parser.parse_args()
    value = snapshot()
    if args.json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    print('Codex ERP 全项目接管检查')
    print(f"项目目录：{value['project_root']}")
    print(f"当前代码：{value['git_branch'] or '(未在分支)'} / {value['git_head'] or '(未知)'}")
    print(f"已有未提交跟踪文件：{value['tracked_changed_file_count']} 个；修改前先检查并保留他人的工作。")
    for name, present in value['required_documents'].items():
        print(f"{'可读' if present else '缺失'}：{name}")
    print('桌面 MCP 当前仅支持共享产品查询、读取和备注比较保存；其他业务按功能地图核对实际能力。')
    print('业务任务在发起人的电脑执行；服务器只协调任务并保存共享资料和结果。')


if __name__ == '__main__':
    main()
