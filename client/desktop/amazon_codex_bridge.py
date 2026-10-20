"""Amazon Codex bridge; local material preparation only."""
import json
from pathlib import Path
from erp_desktop import profile_dir, protect


def helpers():
    from codex_bridge import runtime
    runtime()
    from erp.services import amazon_codex
    return amazon_codex


def export(context, target):
    from codex_bridge import atomic, reference_files
    snap = context['snapshot']
    directory = target / snap['job_id'] / snap['store_id']
    directory.mkdir(parents=True, exist_ok=True)
    references = {}
    for item in snap['batch']['items']:
        refs = directory / item['variant_id']; refs.mkdir(parents=True, exist_ok=True)
        references[item['sku']] = reference_files({'facts': {'assets': [
            {'oss_url': url} for url in item['images'][:2]]}}, refs)
    atomic(directory / 'handoff.json', {'contract': 1, 'kind': 'amazon_listing_copy',
           'target_worker_id': context['target_worker_id'], 'status': context['status'],
           **snap, 'reference_files': references})
    skus = [i['sku'] for i in snap['batch']['items']]
    if snap['batch']['parent_sku']: skus.append(snap['batch']['parent_sku'])
    output = directory / 'output-template.json'
    # Preserve authored work when re-exporting. A changed revision will be rejected.
    if not output.exists():
        atomic(output, {'contract': 1, 'job_id': snap['job_id'], 'store_id': snap['store_id'],
               'revision': snap['revision'], 'copies': {sku: {'title': '', 'bullet_points': [],
               'description': '', 'keywords': '', 'attributes': {}} for sku in skus}})
    return {'job_id': snap['job_id'], 'store_id': snap['store_id'],
            'handoff': str(directory / 'handoff.json'), 'output': str(output)}


def submit(client, args, context):
    snap = context['snapshot']
    if context['status'] != 'paused': raise ValueError('本任务不在等待回填阶段，请在网页核验原资料')
    value = json.loads(Path(args.file).read_text(encoding='utf-8'))
    if value.get('contract') != 1 or any(value.get(k) != snap[k] for k in ('job_id', 'store_id', 'revision')) or args.store != snap['store_id']:
        raise ValueError('输出不属于原 Amazon 任务、店铺或资料修订')
    contract = helpers(); copies = contract.validate_outputs(snap, value.get('copies'))
    record = {'producer': 'codex-desktop', 'job_id': args.job, 'store_id': args.store,
              'revision': snap['revision'], 'copies': copies, 'output_sha256': contract.digest(copies)}
    work = profile_dir() / 'amazon-tasks' / args.job
    work.mkdir(parents=True, exist_ok=True)
    from amazon_worker import atomic
    atomic(work / 'codex-content.dpapi', record, protect)
    print(json.dumps({'job_id': args.job, 'saved': True, 'resumed': False}))


def resume(client, args, context):
    from codex_bridge import identity
    snap = context['snapshot']; contract = helpers()
    record = json.loads(protect((profile_dir() / 'amazon-tasks' / args.job / 'codex-content.dpapi').read_bytes(), decrypt=True))
    if any(record.get(k) != snap[k] for k in ('job_id', 'store_id', 'revision')) or record['output_sha256'] != contract.digest(record['copies']):
        raise ValueError('原 Amazon 资料已变化，请重新导出')
    contract.validate_outputs(snap, record['copies'])
    receipt = {snap['store_id']: {k: record[k] for k in ('revision', 'output_sha256', 'copies')}}
    print(json.dumps(client.request('POST', f'/codex-handoffs/{args.job}/resume',
          {'worker_id': identity(), 'receipts': receipt})[0], ensure_ascii=False))


class CodexOutputMissing(ValueError):
    code = 'codex_output_missing'


def consume(snapshot, work, job_id, protector):
    """A leased worker must consume the same local output accepted by the cloud."""
    from erp.services import amazon_codex
    path = work / 'codex-content.dpapi'
    if not path.exists(): raise CodexOutputMissing('缺少本机 Codex 回填文件，请在原电脑完成接手')
    record = json.loads(protector(path.read_bytes(), decrypt=True))
    payload = snapshot['request']; data = snapshot['batch']['snapshot']
    receipt = (payload.get('codex_outputs') or {}).get(data['store_id'])
    expected = {k: record.get(k) for k in ('revision', 'output_sha256', 'copies')}
    revision = amazon_codex.digest([job_id, snapshot['batch']['id'], snapshot['worker_id'], data,
        {level: {k: value[k] for k in ('product_type', 'parentage', 'checksum', 'schema')}
         for level, value in snapshot['definitions'].items()}])
    if record.get('job_id') != job_id or record.get('store_id') != data['store_id'] or not receipt or receipt != expected or receipt['revision'] != revision:
        raise ValueError('本机 Codex 回填与云端原任务资料不一致')
    if record['output_sha256'] != amazon_codex.digest(record['copies']): raise ValueError('Codex 文案文件校验失败')
    snap = {'batch': data, 'definitions': snapshot['definitions']}
    amazon_codex.validate_outputs(snap, record['copies'])
    return {'content': record['copies']}
