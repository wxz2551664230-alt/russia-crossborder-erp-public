"""Read HICUSTOM gallery existence on the initiating PC only."""
import json
import os
from pathlib import Path
import re
import sys


def run(client, endpoint: str, job: dict, root: Path, lost) -> dict:
    if job.get('type') != 'pod_supplier_gallery_sync' or not re.fullmatch(r'[a-f0-9]{32}',job.get('id','')):
        raise ValueError('供应商图库任务无效')
    headers={'X-Worker-Attempt':str(job['attempts'])}
    work=root/'supplier-gallery-tasks'/job['id']
    work.mkdir(parents=True,exist_ok=True)
    journal=work/'facts.json'
    if journal.exists():
        facts=json.loads(journal.read_text(encoding='utf-8'))['facts']
    else:
        snapshot=client.request('GET',endpoint+'/supplier-gallery-snapshot',extra_headers=headers)[0]
        targets=snapshot.get('targets')
        if not isinstance(targets,list):raise ValueError('供应商图库任务清单无效')
        connection=snapshot.get('connection')
        gallery=None
        if any(row.get('supplier')=='hicustom' for row in targets):
            if not isinstance(connection,dict) or connection.get('provider')!='hicustom':
                raise ValueError('指纹科技连接资料缺失')
            runtime=Path(__file__).parent/'runtime'
            sys.path.insert(0,str(runtime if runtime.is_dir() else Path(__file__).resolve().parents[2]/'backend'))
            from erp.domains.pod.integrations.common import SupplierConnectionConfig
            from erp.domains.pod.integrations.hicustom import HicustomClient
            gallery=HicustomClient(SupplierConnectionConfig(**connection))
        facts=[]
        try:
            if gallery:gallery.__enter__()
            for target in targets:
                if lost.is_set():raise ValueError('供应商图库任务租约已失效')
                if target.get('supplier')=='hicustom':
                    exists=gallery.gallery_exists(target['gallery_code'])
                elif target.get('supplier')=='s2b':
                    exists=None
                else:raise ValueError('不支持的供应商')
                facts.append({'id':target['id'],'exists':exists})
        finally:
            if gallery:gallery.__exit__(None,None,None)
        temporary=journal.with_suffix('.tmp')
        try:
            temporary.write_text(json.dumps({'facts':facts},ensure_ascii=False,separators=(',',':')),encoding='utf-8')
            os.replace(temporary,journal)
        finally:temporary.unlink(missing_ok=True)
    if lost.is_set():raise ValueError('供应商图库任务租约已失效')
    result=client.request('POST',endpoint+'/supplier-gallery-result',{'facts':facts},extra_headers=headers)[0]
    if not isinstance(result,dict) or set(result)!={'checked','missing','unsupported_skipped'}:
        raise ValueError('服务器供应商图库回执无效')
    return result
