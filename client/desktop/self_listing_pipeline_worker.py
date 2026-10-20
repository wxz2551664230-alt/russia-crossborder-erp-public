"""Run the original self-listing directory pipeline on its initiating computer."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import sys
import uuid

from erp_desktop import TemporaryConnectionError


STEPS = ('generate_images', 'generate_copywriting', 'watermark',
         'upload_original', 'upload_watermarked')
ARTIFACT_ROLES = {'generate_copywriting': 'copywriting_xlsx',
                  'upload_original': 'oss_original_xlsx',
                  'upload_watermarked': 'oss_watermarked_xlsx'}


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')),
                             encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _validate_paths(payload: dict) -> None:
    product = Path(str(payload.get('product_root') or '')).expanduser().resolve()
    if not product.is_dir():
        raise ValueError('当前执行电脑找不到产品图片根目录')
    output = Path(payload.get('output_dir') or product.parent / f'{product.name}-ERP输出').expanduser().resolve()
    watermarked = Path(payload.get('watermark_root') or product.parent / f'{product.name}-水印图').expanduser().resolve()
    # The preserved old applications may be read, but must never be edited in place.
    protected = [Path('D:/Desktop') / name for name in
                 ('指纹科技图片合成', 'ozon自动选品上架', 'OZON自动化工作文件夹 - 2')]
    protected += [Path.home() / 'Desktop' / name for name in
                  ('指纹科技图片合成', 'ozon自动选品上架', 'OZON自动化工作文件夹 - 2')]
    roots = {path.resolve() for path in protected}
    write_targets = [output, watermarked]
    if 'generate_images' in payload.get('selected_steps', []):
        write_targets.append(product)
    if any(target.is_relative_to(root) for target in write_targets for root in roots):
        raise ValueError('旧程序目录永久只读，请选 ERP 工作目录')


class PersonalContext:
    def __init__(self, job: dict, work: Path, lost):
        self.payload = job['payload']
        self.work = work
        self.lost = lost
        self.state_path = work / 'steps.json'
        self.assets_path = work / 'artifacts.json'
        self.states = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {}
        self.assets = json.loads(self.assets_path.read_text(encoding='utf-8')) if self.assets_path.exists() else {}
        selected = list(dict.fromkeys(self.payload['selected_steps']))
        if not selected or any(key not in STEPS for key in selected):
            raise ValueError('目录流水线步骤无效')
        if any(self.states.get(key) == 'running' for key in selected):
            raise ValueError('上次流水线步骤结果不明；已保留本机文件，请核对后恢复')
        self.job = SimpleNamespace(id=job['id'], steps=[SimpleNamespace(
            key=key, status=self.states.get(key, 'queued')) for key in selected])

    def refresh(self):
        if self.lost.is_set():
            raise TemporaryConnectionError('执行电脑连接已中断，已保留本机检查点')

    def start_step(self, key: str):
        self.refresh()
        step = next((item for item in self.job.steps if item.key == key), None)
        if step is None:
            raise ValueError('目录流水线步骤与任务不一致')
        if step.status == 'completed':
            return step
        if step.status == 'running':
            return step
        self.states[key] = 'running'
        _write_json(self.state_path, self.states)
        step.status = 'running'
        return step

    def complete_step(self, key: str, *, details=None):
        step = self.start_step(key)
        self.states[key] = 'completed'
        _write_json(self.state_path, self.states)
        step.status = 'completed'

    def event(self, message: str, **_kwargs):
        print(str(message)[:500], flush=True)

    def output_asset(self, _context, path: Path, role: str):
        if role not in ARTIFACT_ROLES.values() or not path.is_file():
            raise ValueError('目录流水线未生成有效表格')
        product = Path(self.payload['product_root']).expanduser().resolve()
        output = Path(self.payload.get('output_dir') or product.parent / f'{product.name}-ERP输出').expanduser().resolve()
        if not path.resolve().is_relative_to(output):
            raise ValueError('结果表格不在本次流水线输出目录')
        self.assets[role] = {'path': str(path.resolve()), 'sha256': _digest(path),
                             'bytes': path.stat().st_size}
        _write_json(self.assets_path, self.assets)


def _watch_lost(lost):
    def watch(_job_id, stop_event, finished):
        while not finished.wait(1):
            if lost.is_set():
                stop_event.set()
                return
    return watch


def run(client, endpoint: str, job: dict, root: Path, lost) -> dict:
    if (job.get('type') != 'self_listing_pipeline'
            or not re.fullmatch(r'[a-f0-9]{32}', str(job.get('id') or ''))):
        raise ValueError('自主上架目录任务无效')
    work = root / 'self-listing-tasks' / job['id']
    work.mkdir(parents=True, exist_ok=True)
    context = PersonalContext(job, work, lost)
    selected = [item.key for item in context.job.steps]
    _validate_paths(job['payload'])
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    if any(step.status != 'completed' for step in context.job.steps):
        snapshot = client.request('GET', endpoint + '/self-listing-config',
                                  extra_headers=headers)[0]
        if (not isinstance(snapshot, dict)
                or not isinstance(snapshot.get('connections'), dict)
                or not isinstance(snapshot.get('secrets'), dict)):
            raise ValueError('目录流水线连接快照无效')
        runtime = Path(__file__).parent / 'runtime'
        sys.path.insert(0, str(runtime if runtime.is_dir()
                               else Path(__file__).resolve().parents[2] / 'backend'))
        from erp.services.self_listing_pipeline import run_self_listing_pipeline
        run_self_listing_pipeline(context, snapshot=snapshot,
                                  output_asset=context.output_asset,
                                  watch_cancel=_watch_lost(lost))
    if any(step.status != 'completed' for step in context.job.steps):
        raise ValueError('目录流水线尚有未完成步骤')
    receipts_path = work / 'uploaded-artifacts.json'
    receipts = json.loads(receipts_path.read_text(encoding='utf-8')) if receipts_path.exists() else {}
    for step in selected:
        role = ARTIFACT_ROLES.get(step)
        if not role:
            continue
        item = context.assets.get(role)
        if not item:
            raise ValueError('已完成步骤缺少本机表格检查点')
        path = Path(item['path'])
        if (not path.is_file() or path.stat().st_size != item['bytes']
                or _digest(path) != item['sha256']):
            raise ValueError('本机表格已变化，请保留任务目录核对')
        if role not in receipts:
            if lost.is_set():
                raise TemporaryConnectionError('执行电脑连接已中断，待恢复表格上传')
            packed = path.read_bytes()
            if len(packed) > 25 * 1024**2:
                raise ValueError('单张结果表格超过 25 MB，请保留本机文件')
            receipt = client.request('POST', endpoint + '/self-listing-artifact', {
                'role': role, 'sha256': item['sha256'],
                'content': base64.b64encode(packed).decode('ascii')}, extra_headers=headers)[0]
            if (receipt.get('role') != role or receipt.get('sha256') != item['sha256']
                    or receipt.get('bytes') != item['bytes']):
                raise ValueError('云端表格回执与本机文件不一致')
            receipts[role] = receipt
            _write_json(receipts_path, receipts)
    return {'schema': 1, 'completed_steps': selected,
            'artifacts': [receipts[ARTIFACT_ROLES[step]] for step in selected if step in ARTIFACT_ROLES]}
