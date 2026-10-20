"""Original catalog Ozon preparation on one PC, with encrypted local recovery.

This module is not yet advertised as an installed-worker capability. Human image
review and the leased HTTP bridge must be wired before enabling task creation.
"""
import json
import hashlib
import re
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


def codex_binding(snapshot):
    return hashlib.sha256(json.dumps({key: snapshot[key] for key in
        ('job_id', 'publication_id', 'store_id', 'offer_id', 'facts', 'requirements', 'image_policy')},
        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def normalize_codex_attributes(suggestions):
    """Adapt the legacy id alias in memory without rewriting signed output."""
    if suggestions is None:
        return []
    if not isinstance(suggestions, list):
        raise ValueError('Codex 属性建议必须为数组')
    normalized = []
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            raise ValueError('Codex 属性建议必须为对象')
        keys = [key for key in ('attribute_id', 'id') if key in suggestion]
        if not keys:
            raise ValueError('Codex 属性建议缺少 attribute_id')
        for key in keys:
            value = suggestion[key]
            if type(value) is not int or value <= 0:
                raise ValueError('Codex 属性 ID 必须为正整数')
        if len(keys) == 2 and suggestion['attribute_id'] != suggestion['id']:
            raise ValueError('Codex 属性 attribute_id 与 id 冲突')
        value = suggestion.get('value')
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Codex 属性 value 必须为非空字符串')
        normalized.append({'attribute_id': suggestion[keys[0]], 'value': value})
    return normalized


def approved_codex_action_matches(record, snapshot, review):
    approval = review.get('human_approval') or {}
    return bool(record and approval and not review.get('image_action')
        and record.get('image_action_id')
        and (snapshot['resume'].get('assets') or {}).get('personal_image_action_id') == record.get('image_action_id')
        and review.get('revision') == snapshot['revision']
        and approval.get('sha256') == review.get('sha256')
        and approval.get('candidate_hash') == review.get('candidate_hash')
        and approval.get('sha256') == (record.get('main_candidate') or {}).get('sha256'))


def codex_content_preparer(record, snapshot, check):
    if record.get('binding') != codex_binding(snapshot) or record.get('producer') != 'codex-desktop':
        raise ValueError('Codex 文案与原任务资料不一致')
    content = record.get('content') or {}
    if not content.get('title_ru') or not content.get('description_ru'):
        raise ValueError('Codex 文案缺少标题或描述')
    for previous in snapshot['prior_copy_for_difference_check']:
        if content['description_ru'].strip() == str(previous.get('description_ru') or '').strip():
            raise ValueError('禁止复用其他上架任务的文案')
    def prepared(*, config, product, draft, definitions, ozon_client, sourcing,
                 on_progress=None, on_content_checkpoint=None, generate_image=True):
        from erp.modules.ozon_follow.legacy.services import ai_listing
        check()
        if generate_image and not record.get('main_candidate'):
            raise ValueError('Codex 任务需要生成并回填所选图片')
        suggestions = normalize_codex_attributes(content.get('attributes'))
        draft.title = content['title_ru']; draft.description_ru = content['description_ru']
        draft.tags_ru = list(content.get('tags_ru') or [])
        result = ai_listing.apply_ai_attributes(draft, definitions, suggestions, ozon_client)
        result['applied'].extend(ai_listing.apply_content_attributes(draft, definitions, content))
        result['applied'].extend(ai_listing.enforce_system_attributes(draft, definitions))
        result['applied'].extend(ai_listing.enforce_known_fact_attributes(draft, definitions, product))
        draft.ai_preparation.update(content=content, attributes=result, producer='codex-desktop',
            codex_binding=record['binding'], reference_image_urls=[draft.primary_image],
            original_additional_image_urls=list(draft.images), image_stage_status='NOT_REQUESTED')
        preserved=record.get('preserved_additional_urls') or []
        if preserved:
            if any(not isinstance(url,str) or not url.startswith('https://') for url in preserved):raise ValueError('原附图地址无效')
            draft.images=list(preserved);draft.ai_preparation['generated_additional_urls']=list(preserved)
        additions=record.get('additional_candidates') or []
        if additions:
            from erp.services import publications as original
            urls=[]
            for index,row in enumerate(additions):
                path=Path(row['path'])
                if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256'] or row.get('matches_sku') is not True:
                    raise ValueError('Codex 附图校验记录或文件已变化')
                check()
                urls.append(original.OssUploader(config).upload(path,snapshot['store_id']+'-'+snapshot['offer_id'],f'附图{index+1:02d}.png'))
            draft.images=urls
            draft.ai_preparation.update(generated_additional_urls=urls,codex_additional_checks=additions)
        if generate_image:
            row=record['main_candidate'];path=Path(row['path'])
            if hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256'] or row.get('matches_sku') is not True:
                raise ValueError('Codex 主图校验记录或文件已变化')
            draft.ai_preparation.update(generated_image_path=str(path),image_review_status='PENDING',image_stage_status='DONE')
            if row.get('preserve_primary_url'):
                draft.ai_preparation['codex_preserve_primary_url']=row['preserve_primary_url']
        check()
        if on_content_checkpoint: on_content_checkpoint()
        return draft.ai_preparation
    return prepared


def prepare(snapshot, work, check, protect):
    from erp.core.jsonutil import dumps, loads
    from erp.services import publications as original
    from erp.modules.ozon_follow.legacy.services import ai_listing
    from ozon_paid_transport import paid_transport

    if snapshot.get('schema') != 1:
        raise ValueError('Ozon 产品库任务快照版本无效')
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    agent_path = work / 'codex-content.dpapi'
    agent_record = json.loads(protect(agent_path.read_bytes(), decrypt=True)) if agent_path.exists() else None
    is_codex=snapshot['requirements'].get('generation_provider')=='codex'
    if is_codex and not agent_record:
        raise CatalogWorkerError('等待 Codex 回填文案和所选图片','codex_output_missing')
    agent_preparer = codex_content_preparer(agent_record, snapshot, check) if agent_record else None
    if original.OZON_WORK_DATA_DIR.resolve() != (work.resolve() / 'ozon_follow'):
        raise ValueError('Ozon 运行目录未隔离，请使用新的任务执行进程')
    local = work / 'preparation.dpapi'
    if snapshot['requirements'].get('personal_merge_request'):
        snapshot = dict(snapshot)
        merge = (agent_record or {}).get('resolved_merge')
        if merge:
            if merge.get('request') != snapshot['requirements']['personal_merge_request'] or not isinstance(merge.get('plan'), dict):
                raise ValueError('Codex 合并资料与原批次不同')
            snapshot['requirements'] = {**snapshot['requirements'], 'ozon_merge': merge['plan']}
        else:
            snapshot['requirements'] = resolve_merge(snapshot, work, check, protect)
    resume = snapshot['resume']
    review = (resume.get('assets') or {}).get('personal_review') or {}
    approval = review.get('human_approval')
    image_action = review.get('image_action') or {}
    approved_action_matches = is_codex and approved_codex_action_matches(agent_record, snapshot, review)
    if is_codex and agent_record.get('image_action_id','')!=image_action.get('id','') and not approved_action_matches:
        raise CatalogWorkerError('等待 Codex 完成本次图片操作','codex_output_missing')
    saved = None
    if local.exists():
        saved = json.loads(protect(local.read_bytes(), decrypt=True))
        if saved['job_id'] != snapshot['job_id'] or saved['revision'] != snapshot['revision']:
            raise ValueError('本机检查点与任务资料不同，禁止复用历史文案')
        resume = saved['resume']
        if saved.get('review_candidate') and not approval and (not image_action or saved.get('image_action_id') == image_action.get('id')):
            return {'image_review': saved['review_candidate']}
    child = SimpleNamespace(id='personal-child', store_id=snapshot['store_id'],
        store=SimpleNamespace(name=snapshot['store_name']), offer_id=snapshot['offer_id'],
        status='queued', external_task_id='', **{key + '_json': dumps(value) for key, value in resume.items()})
    # Profile index and sibling copy are retained from the original batch.
    publication = SimpleNamespace(id=snapshot['publication_id'], product_id=snapshot['product_id'],
        product_snapshot_json=dumps(snapshot['facts']), requirements_json=dumps(snapshot['requirements']),
        image_policy_json=dumps(snapshot['image_policy']), stores=[child])
    publication.personal_store_index = snapshot['store_index']
    publication.personal_other_versions = snapshot['prior_copy_for_difference_check']
    def checkpoint():
        check()
        value = {'job_id': snapshot['job_id'], 'revision': snapshot['revision'],
                 'image_action_id': image_action.get('id'), 'resume': {key: loads(getattr(child, key + '_json'), [] if key in {'attributes', 'complex_attributes'} else {})
                            for key in ('content', 'attributes', 'complex_attributes', 'assets', 'pricing', 'package')}}
        temporary = local.with_suffix('.partial')
        temporary.write_bytes(protect(json.dumps(value, ensure_ascii=False).encode()))
        temporary.replace(local)
    session = SimpleNamespace(commit=checkpoint,
        scalars=lambda query: SimpleNamespace(all=lambda: []))
    context = SimpleNamespace(session=session, start_step=lambda *args, **kwargs: check(),
        complete_step=lambda *args, **kwargs: check(), event=lambda *args, **kwargs: check())
    original_post = ai_listing.ai_post
    original_preparer = original.prepare_listing_with_ai
    original_repository = original.ProductRepository
    original_main_requested = original._main_generation_requested
    if agent_preparer:
        original.prepare_listing_with_ai = agent_preparer
        cache_path = Path(agent_record['dictionary_cache_path']).resolve() if agent_record.get('dictionary_cache_path') else None
        if cache_path:
            cache_root = work.resolve().parents[2] / 'codex-dictionary-cache'
            if not cache_path.is_relative_to(cache_root) or not cache_path.is_file():
                raise ValueError('Codex 字典缓存路径无效')
            original.ProductRepository = lambda: original_repository(database_path=cache_path)
        if agent_record.get('main_candidate'):
            original._main_generation_requested = lambda policy: True
        if is_codex:
            def block_external_generation(*args,**kwargs):
                raise CatalogWorkerError('Codex 接手任务禁止回退到外部生成模型','codex_output_missing')
            ai_listing.ai_post=block_external_generation
    if not is_codex:
        ai_listing.ai_post = paid_transport(original_post, work / 'paid-calls', lease=check,
                                          scope=snapshot['job_id'] + '-' + snapshot['store_id'] + ('-' + image_action['id'] if image_action else ''))
    try:
        check()
        if image_action and loads(child.assets_json, {}).get('personal_image_action_id') != image_action['id']:
            # The original operation keeps this task's copy and applies its original repair/redesign rules.
            child.status = 'image_review'
            if is_codex:
                # The new Codex artifact replaces only this explicitly requested candidate.
                state=loads(child.assets_json,{})
                state['ai_preparation']={};state.pop('package_ready',None)
                child.assets_json=dumps(state)
            else:
                original.review_publication_image(session, publication, child, image_action['action'])
            state = loads(child.assets_json, {})
            state['personal_image_action_id'] = image_action['id']
            child.assets_json = dumps(state)
            checkpoint()
        if approval and not loads(child.assets_json, {}).get('package_ready'):
            draft = original._review_draft(publication, child)
            candidate_path = ai_listing.pending_main_image_path(draft)
            if hashlib.sha256(candidate_path.read_bytes()).hexdigest() != approval.get('sha256'):
                raise ValueError('原电脑候选图与人工批准图片不同')
            original.approve_pending_main_image(config=original._app_config(snapshot['config']), draft=draft, reviewer='HUMAN')
            state = loads(child.assets_json, {})
            state.update(primary_image=draft.primary_image, images=draft.images, ai_preparation=draft.ai_preparation,
                         approved_candidate_sha256=approval['sha256'])
            child.assets_json = dumps(state)
            checkpoint()
        try:
            original.prepare_store_package(context, publication, child, snapshot['config'])
        except Exception as error:
            if getattr(error, 'code', '') != 'publication_image_review_required':
                raise
            draft = original._review_draft(publication, child)
            candidate_path = ai_listing.pending_main_image_path(draft)
            check()
            url = original.OssUploader(original._app_config(snapshot['config'])).upload(
                candidate_path, snapshot['store_id'] + '-' + snapshot['offer_id'], '待人工核验主图.jpg')
            candidate = public_result({'sha256': hashlib.sha256(candidate_path.read_bytes()).hexdigest(), 'url': url,
                **{key: loads(getattr(child, key + '_json'), [] if key in {'attributes', 'complex_attributes'} else {})
                   for key in ('content', 'attributes', 'complex_attributes', 'assets')}})
            checkpoint()
            saved = json.loads(protect(local.read_bytes(), decrypt=True))
            saved['review_candidate'] = candidate
            temporary = local.with_suffix('.partial')
            temporary.write_bytes(protect(json.dumps(saved, ensure_ascii=False).encode()))
            temporary.replace(local)
            return {'image_review': candidate}
        if approval:
            state = loads(child.assets_json, {})
            state['approved_candidate_sha256'] = approval['sha256']
            child.assets_json = dumps(state)
        checkpoint()
    finally:
        ai_listing.ai_post = original_post
        original.prepare_listing_with_ai = original_preparer
        original.ProductRepository = original_repository
        original._main_generation_requested = original_main_requested
    return {key: loads(getattr(child, key + '_json'), [] if key in {'attributes', 'complex_attributes'} else {})
            for key in ('content', 'attributes', 'complex_attributes', 'assets', 'pricing', 'package')}


def public_result(value):
    """Keep private file references only in the encrypted original-PC journal."""
    if isinstance(value, dict):
        return {key: public_result(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_result(item) for item in value]
    if isinstance(value, str) and (re.match(r'^[A-Za-z]:[\\/]', value) or value.startswith(('\\\\', 'file://', '/tmp/', '/app/'))):
        return ''
    return value


def resolve_merge(snapshot, work, check, protect):
    from erp.integrations.ozon import OzonSellerAdapter
    from erp.services.publications import build_ozon_merge_plan
    path = Path(work) / 'merge-plan.dpapi'
    if path.exists():
        saved = json.loads(protect(path.read_bytes(), decrypt=True))
        if saved['job_id'] != snapshot['job_id'] or saved['revision'] != snapshot['revision']:
            raise ValueError('合并字典检查点与原任务不同')
        plan = saved['plan']
    else:
        request = snapshot['requirements']['personal_merge_request']
        category = int(snapshot['requirements'].get('description_category_id') or snapshot['facts']['description_category_id'])
        kind = int(snapshot['requirements'].get('type_id') or snapshot['facts']['type_id'])
        adapter = OzonSellerAdapter(snapshot['config'])
        check()
        definitions = adapter.category_attributes(category, kind, 'ZH_HANS')
        if not any('型号名称' in str(row.get('name') or '') for row in definitions):
            raise ValueError('当前 Ozon 类目不支持通过型号名称合并商品卡')
        values = {}
        for identity in request['variant_attribute_ids']:
            check()
            values[identity] = adapter.category_attribute_values(category, kind, identity, 'ZH_HANS')
        plan = build_ozon_merge_plan(request['facts'], definitions, values, model_name=request['model_name'],
                                    variant_attribute_ids=request['variant_attribute_ids'])
        check()
        temporary = path.with_suffix('.partial')
        temporary.write_bytes(protect(json.dumps({'job_id': snapshot['job_id'], 'revision': snapshot['revision'], 'plan': plan}, ensure_ascii=False).encode()))
        temporary.replace(path)
    return {**snapshot['requirements'], 'ozon_merge': plan[str(snapshot['facts']['sku'])]}


class CatalogWorkerError(ValueError):
    def __init__(self, message, code='local_execution_failed'):
        super().__init__(message); self.code = code


def run(client, endpoint, job, root, lost):
    from composition_worker import atomic_json
    if job['type'] != 'catalog_ozon_prepare' or not re.fullmatch('[a-f0-9]{32}', job['id']):
        raise ValueError('产品库 Ozon 准备任务无效')
    headers = {'X-Worker-Attempt': str(job['attempts'])}
    targets = client.request('GET', endpoint + '/catalog-ozon-targets', extra_headers=headers)[0]['stores']
    receipts = {}
    for store_id in targets:
        if not re.fullmatch('[a-f0-9]{32}', store_id):
            raise ValueError('目标店铺编号无效')
        work = Path(root) / 'catalog-ozon-preparations' / job['id'] / store_id
        work.mkdir(parents=True, exist_ok=True)
        source = work / 'input.json'; output = work / 'transport-result.json'
        atomic_json(source, {'job': job, 'endpoint': endpoint, 'store_id': store_id})
        output.unlink(missing_ok=True)
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child', str(source.resolve())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while process.poll() is None:
                if lost.wait(.5): raise ValueError('连接失效，已保留原电脑加密准备检查点')
            value = json.loads(output.read_text(encoding='utf-8'))
            if 'error' in value: raise CatalogWorkerError(value['error'], value['code'])
            receipts[store_id] = value['result']
        finally:
            if process.poll() is None: process.terminate(); process.wait(timeout=15)
    finalized = client.request('POST', endpoint + '/catalog-ozon-prepare-finish',
        {'result': {'schema': 1, 'stores': receipts}}, extra_headers=headers)[0]
    return {'catalog_ozon_finalized': True, 'status': finalized['status'], 'job_id': job['id']}


def initialize_ai_runtime():
    from erp.core.database import engine
    from erp.models import SystemSetting
    SystemSetting.__table__.create(engine, checkfirst=True)


def execute(source):
    from erp_desktop import Client, protect
    value = json.loads(source.read_text(encoding='utf-8'))
    job = value['job']; endpoint = value['endpoint']; store_id = value['store_id']; work = source.parent
    os.environ.update(ERP_DATA_DIR=str(work), ERP_DATABASE_URL='sqlite:///' + (work / 'unused.db').as_posix(),
                      ERP_MULTIUSER_ENABLED='false', ERP_RUNTIME_MODE='worker')
    runtime = Path(__file__).parent / 'runtime'
    sys.path.insert(0, str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2] / 'backend'))
    # Original AI concurrency/accounting reads SystemSetting before sending.
    # This is an isolated task database, never the shared business database.
    initialize_ai_runtime()
    client = Client(); headers = {'X-Worker-Attempt': str(job['attempts'])}
    def post(suffix, body):
        from erp_desktop import TemporaryConnectionError
        import time
        for attempt in range(3):
            try:
                return client.request('POST', endpoint + suffix, body, extra_headers=headers)[0]
            except TemporaryConnectionError:
                if attempt == 2: raise
                time.sleep(2 ** attempt)

    def check(): post('/heartbeat', {})
    snapshot = client.request('GET', endpoint + '/catalog-ozon-snapshot/' + store_id, extra_headers=headers)[0]
    result = prepare(snapshot, work, check, protect)
    if 'image_review' in result:
        receipt = post('/catalog-ozon-candidate/' + store_id, {'revision': snapshot['revision'], 'candidate': result['image_review']})
        return {'status': receipt['status'], 'candidate_hash': receipt['candidate_hash']}
    receipt = post('/catalog-ozon-prepared/' + store_id, {'revision': snapshot['revision'], 'result': public_result(result)})
    return {'status': receipt['status'], 'result_hash': receipt['result_hash']}


if __name__ == '__main__':
    from composition_worker import atomic_json
    source = Path(sys.argv[2]).resolve()
    try:
        value = {'result': execute(source)}
    except Exception as error:
        import traceback
        # Local-only diagnostics deliberately exclude exception messages,
        # request arguments, source lines, and credentials.
        chain = []
        current = error
        while current is not None and len(chain) < 8:
            chain.append({'type': type(current).__name__, 'frames': [
                {'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
                for frame in traceback.extract_tb(current.__traceback__)]})
            current = current.__cause__ or current.__context__
        diagnostic = {'exceptions': chain}
        if getattr(error, 'code', '') == 'publication_validation_blocked':
            blockers = getattr(error, 'details', None)
            if isinstance(blockers, list) and all(isinstance(item, str) for item in blockers):
                diagnostic['validation_blockers'] = blockers
        atomic_json(source.parent / 'error-diagnostic.json', diagnostic)
        value = {'error': ('AI 自动重试3次仍失败，请检查服务商状态后重试原任务' if getattr(error, 'code', '') == 'ai_write_outcome_unknown' else '自动恢复仍未完成，请查看本机连接与任务诊断'),
                 'code': getattr(error, 'code', 'local_execution_failed')}
    atomic_json(source.parent / 'transport-result.json', value)
