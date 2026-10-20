"""Task-bound transfer of compact WB card identities from the initiating PC."""
import hashlib
import json
import re
import shutil
from pathlib import Path

from erp.core.config import get_settings
from erp.core.errors import ConflictError
from erp.core.jsonutil import dumps, loads
from erp.domains.pod.wb_export import apply_history_cards
from erp.models import JobEvent


def _directory(job_id:str)->Path:
    if not re.fullmatch('[a-f0-9]{32}',job_id):raise ConflictError('任务编号无效')
    return get_settings().data_dir/'wb_history_staging'/job_id


def _bytes(cards, rich=False):
    if not isinstance(cards,list) or len(cards)>200:raise ConflictError('WB 商品卡分片过大')
    if rich:
        from erp.domains.pod.wb_shop_sync import validate_cards
        validate_cards(cards)
        raw=dumps(cards).encode('utf-8')
        if len(raw)>48000:raise ConflictError('WB 商品卡分片超过传输限制')
        return raw
    normalized=[]
    for card in cards:
        if not isinstance(card,dict) or set(card)!={'vendorCode','nmID'}:
            raise ConflictError('WB 商品卡格式无效')
        vendor=card['vendorCode'];identity=card['nmID']
        if not isinstance(vendor,str) or len(vendor)>200 or type(identity)is not int or identity<=0:
            raise ConflictError('WB 商品卡字段无效')
        normalized.append({'vendorCode':vendor,'nmID':identity})
    raw=dumps(normalized).encode('utf-8')
    if len(raw)>48000:raise ConflictError('WB 商品卡分片超过传输限制')
    return raw


def stage(job,index:int,cards):
    if job.type!='pod_wb_history_reconcile' or type(index)is not int or not 0<=index<5000:
        raise ConflictError('不是 WB 历史扫描分片')
    raw=_bytes(cards, bool(loads(job.payload_json,{}).get('shop_sync')));folder=_directory(job.id);folder.mkdir(parents=True,exist_ok=True)
    path=folder/f'{index:04d}.json'
    if path.exists():
        if path.read_bytes()!=raw:raise ConflictError('同一分片已有不同内容，请核对原扫描')
    else:
        temporary=path.with_suffix('.partial');temporary.write_bytes(raw);temporary.replace(path)
    return {'index':index,'sha256':hashlib.sha256(raw).hexdigest()}


def accept(session,job,result):
    rich=bool(loads(job.payload_json,{}).get('shop_sync'))
    if (not isinstance(result,dict) or set(result)!=({'chunks','cards','sha256','shop_sync'} if rich else {'chunks','cards','sha256'})
            or type(result['chunks'])is not int or not 0<=result['chunks']<=5000
            or type(result['cards'])is not int or not 0<=result['cards']<=1000000
            or not re.fullmatch('[a-f0-9]{64}',str(result['sha256']))):
        raise ConflictError('WB 扫描回执无效')
    folder=_directory(job.id)
    files=sorted(folder.glob('[0-9][0-9][0-9][0-9].json')) if folder.exists() else []
    if len(files)!=result['chunks'] or [p.name for p in files]!=[f'{i:04d}.json' for i in range(result['chunks'])]:
        raise ConflictError('WB 扫描分片不完整')
    cards=[];digest=hashlib.sha256()
    for path in files:
        part=json.loads(path.read_text(encoding='utf-8'))
        _bytes(part,rich)
        digest.update(path.read_bytes())
        cards.extend(part)
    if len(cards)!=result['cards'] or digest.hexdigest()!=result['sha256']:
        raise ConflictError('WB 扫描内容校验不一致')
    if rich:
        from erp.domains.pod.wb_shop_sync import apply
        meta=result['shop_sync']
        if not isinstance(meta,dict) or set(meta)!={'warnings','retry_seconds','cards_read'} or not isinstance(meta['warnings'],list) or len(meta['warnings'])>10 or any(not isinstance(v,str) or len(v)>300 for v in meta['warnings']) or type(meta['retry_seconds'])is not int or not 0<=meta['retry_seconds']<=86400 or type(meta['cards_read'])is not int or not len(cards)<=meta['cards_read']<=50000:
            raise ConflictError('WB 店铺同步完成依据无效')
        summary=apply(session,job,cards,warnings=meta['warnings'],retry_seconds=meta['retry_seconds'],cards_read=meta['cards_read'])
    else:
        summary=apply_history_cards(session,job,cards)
    job.external_ref=dumps({'wb_history_summary':summary})
    session.add(JobEvent(job_id=job.id,message=f"WB 扫描完成：读取 {summary['wb_cards']} 张商品卡，匹配 {summary['matched_variants']} 个规格，冲突 {summary['ambiguous']} 项；详细报告已保存"))
    return summary


def cleanup(job_id:str):
    path=_directory(job_id).resolve()
    base=(get_settings().data_dir/'wb_history_staging').resolve()
    if path.parent!=base:raise ConflictError('WB 暂存目录越界')
    if path.exists():shutil.rmtree(path)
