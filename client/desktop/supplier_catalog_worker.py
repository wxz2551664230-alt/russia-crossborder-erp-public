"""Fetch the unchanged HICUSTOM/S2B catalog flow on the initiating PC."""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def run(client, endpoint: str, job: dict, root: Path, lost) -> dict:
    if job.get('type') != 'pod_supplier_catalog_sync' or not re.fullmatch('[a-f0-9]{32}',job.get('id','')):
        raise ValueError('供应商货盘任务无效')
    headers={'X-Worker-Attempt':str(job['attempts'])}
    work=root/'supplier-catalog-tasks'/job['id']
    work.mkdir(parents=True,exist_ok=True)
    journal=work/'catalog-result.json.gz'
    if not journal.exists():
        snapshot=client.request('GET',endpoint+'/supplier-catalog-snapshot',extra_headers=headers)[0]
        sources=snapshot.get('sources')
        if not isinstance(sources,dict) or set(sources)!={'hicustom','s2b'}:
            raise ValueError('供应商货盘任务快照无效')
        runtime=Path(__file__).parent/'runtime'
        sys.path.insert(0,str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2]/'backend'))
        from erp.core.errors import AppError
        from erp.domains.pod.integrations.common import SupplierConnectionConfig
        from erp.domains.pod.integrations.hicustom import HicustomClient
        from erp.domains.pod.integrations.s2b import S2BClient
        from erp.domains.pod.supplier_catalog import fetch_provider_catalog
        providers=[]
        for supplier,client_type in (('hicustom',HicustomClient),('s2b',S2BClient)):
            if lost.is_set():raise ValueError('供应商货盘任务租约已失效')
            config=sources[supplier]
            if not isinstance(config,dict):raise ValueError('供应商连接快照无效')
            error=str(config.get('error') or '')
            if error:
                providers.append({'supplier':supplier,'products':None,'categories':None,'error':error[:500]})
                continue
            details=config.get('connection')
            if not isinstance(details,dict) or details.get('provider')!=supplier:
                raise ValueError('供应商连接资料不匹配')
            try:
                with client_type(SupplierConnectionConfig(**details)) as remote:
                    products,categories=fetch_provider_catalog(remote,supplier)
                providers.append({'supplier':supplier,'products':products,
                                  'categories':categories,'error':''})
            except AppError as failure:
                providers.append({'supplier':supplier,'products':None,
                                  'categories':None,'error':str(failure)[:500]})
        if all(row['error'] for row in providers):
            raise ValueError('两个供应商货盘均未能读取，已保留原云端资料')
        packed=gzip.compress(json.dumps({'providers':providers},ensure_ascii=False,
                            default=str,separators=(',',':')).encode('utf-8'),mtime=0)
        if not packed or len(packed)>10_000_000:
            raise ValueError('供应商货盘结果超过上传限制')
        temporary=journal.with_suffix('.tmp')
        try:
            temporary.write_bytes(packed)
            os.replace(temporary,journal)
        finally:temporary.unlink(missing_ok=True)
    packed=journal.read_bytes()
    if lost.is_set():raise ValueError('供应商货盘任务租约已失效')
    receipt=client.request('POST',endpoint+'/supplier-catalog-result',{
        'sha256':hashlib.sha256(packed).hexdigest(),
        'content':base64.b64encode(packed).decode('ascii')},extra_headers=headers)[0]
    if not isinstance(receipt,dict) or set(receipt)!={'snapshot_id','product_count','errors'}:
        raise ValueError('服务器供应商货盘回执无效')
    return receipt
