"""WB desktop handoff. No WB or generation API is invoked here."""
import json
from pathlib import Path
from erp_desktop import profile_dir,protect


def helpers():
    from codex_bridge import runtime
    runtime()
    from erp.services import wb_codex
    return wb_codex


def export(context,target):
    from codex_bridge import atomic
    snap=context['snapshot'];directory=target/snap['job_id']/snap['store_id'];directory.mkdir(parents=True,exist_ok=True)
    atomic(directory/'handoff.json',{'contract':1,'kind':'wb_listing_copy','target_worker_id':context['target_worker_id'],**snap})
    output=directory/'output-template.json'
    if not output.exists():atomic(output,{'contract':1,'job_id':snap['job_id'],'store_id':snap['store_id'],'revision':snap['revision'],
        'copies':{p['product_id']:{'title':'','description':'','characteristics':[]} for p in snap['products']}})
    return {'job_id':snap['job_id'],'store_id':snap['store_id'],'handoff':str(directory/'handoff.json'),'output':str(output)}


def submit(client,args,context):
    snap=context['snapshot'];value=json.loads(Path(args.file).read_text(encoding='utf-8'))
    if any(value.get(k)!=snap[k] for k in ('job_id','store_id','revision')) or args.store!=snap['store_id']:raise ValueError('输出不属于原 WB 任务、店铺或资料修订')
    contract=helpers();copies=contract.validate_outputs(snap,value.get('copies'))
    record={'producer':'codex-desktop','job_id':args.job,'store_id':args.store,'revision':snap['revision'],'copies':copies,'output_sha256':contract.digest(copies)}
    work=profile_dir()/'wb-tasks'/args.job;work.mkdir(parents=True,exist_ok=True)
    path=work/'codex-content.dpapi';temp=path.with_suffix('.partial');temp.write_bytes(protect(json.dumps(record,ensure_ascii=False).encode()));temp.replace(path)
    print(json.dumps({'job_id':args.job,'saved':True,'resumed':False}))


def resume(client,args,context):
    from codex_bridge import identity
    snap=context['snapshot'];record=json.loads(protect((profile_dir()/'wb-tasks'/args.job/'codex-content.dpapi').read_bytes(),decrypt=True))
    contract=helpers()
    if any(record.get(k)!=snap[k] for k in ('job_id','store_id','revision')) or record['output_sha256']!=contract.digest(record['copies']):raise ValueError('原 WB 任务资料已变化，请重新导出')
    contract.validate_outputs(snap,record['copies'])
    receipt={snap['store_id']:{'revision':record['revision'],'output_sha256':record['output_sha256'],'copies':record['copies']}}
    print(json.dumps(client.request('POST',f'/codex-handoffs/{args.job}/resume',{'worker_id':identity(),'receipts':receipt})[0],ensure_ascii=False))
