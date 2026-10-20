"""Local Codex handoff runner. Never calls text, vision or image generation APIs."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

from erp_desktop import Client, profile_dir, protect
from catalog_ozon_worker import codex_binding


def identity():
    value=(profile_dir()/'device-id.txt').read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',value):raise ValueError('本机执行电脑编号无效')
    return value


SQUARE_BATCH_ID = '10983d1758e645978c54a8dd5f9bd960'


def square_batch_job(job):
    """The user's 1:1 exception is limited to this exported Ozon batch."""
    bundle_path = profile_dir()/'codex-handoffs'/SQUARE_BATCH_ID/'batch.json'
    if not bundle_path.is_file():
        return False
    bundle = json.loads(bundle_path.read_text(encoding='utf-8'))
    return bundle.get('batch_id') == SQUARE_BATCH_ID and any(
        item.get('job_id') == job for item in bundle.get('jobs', [])
    )


def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix('.partial')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)


def local_context(client,job):
    if not re.fullmatch('[a-f0-9]{32}',job):raise ValueError('任务ID无效')
    worker=identity()
    client.request('POST','/execution-devices/select',{'worker_id':worker})
    return client.get(f'/codex-handoffs/{job}/local-context?worker_id={worker}')


def runtime():
    root=profile_dir()/'codex-dictionary-cache';root.mkdir(parents=True,exist_ok=True)
    os.environ.update(ERP_DATA_DIR=str(root),ERP_DATABASE_URL='sqlite:///'+(root/'unused.db').as_posix(),ERP_MULTIUSER_ENABLED='false')
    path=Path(__file__).parent/'runtime'
    sys.path.insert(0,str(path if path.is_dir() else Path(__file__).resolve().parents[2]/'backend'))
    return root


def definitions(snapshot):
    root=runtime()
    from erp.services.publications import _app_config
    from erp.modules.ozon_follow.legacy.services.ozon_client import OzonSellerClient
    from erp.modules.ozon_follow.legacy.storage import ProductRepository
    cfg=_app_config(snapshot['config'])
    adapter=OzonSellerClient(cfg.ozon_client_id,cfg.ozon_api_key,cfg.ozon_api_base,repository=ProductRepository(root/'ozon-dictionaries.db'))
    category=int(snapshot['requirements'].get('description_category_id') or snapshot['facts'].get('description_category_id') or 0)
    kind=int(snapshot['requirements'].get('type_id') or snapshot['facts'].get('type_id') or 0)
    if not category or not kind:raise ValueError('请先在原页面选择实际类目')
    values=[]
    for row in adapter.category_attributes(category,kind,'RU'):
        clean={k:row.get(k) for k in ('id','name','is_required','dictionary_id','is_collection','max_value_count','attribute_complex_id')}
        # The unrestricted brand dictionary is not needed: original ERP locks no-brand.
        if row.get('dictionary_id') and row.get('is_required') and int(row['id']) not in {85,9048}:
            allowed=adapter.category_attribute_values(category,kind,int(row['id']),language='RU')
            clean['allowed_values']=[{k:v.get(k) for k in ('id','value')} for v in allowed[:160]]
        values.append(clean)
    return values


def reference_files(snapshot,directory):
    import requests
    from PIL import Image
    session=requests.Session();session.trust_env=False;result=[]
    for index,asset in enumerate(snapshot['facts']['assets'][:4]):
        url=asset.get('oss_url','')
        if not url.startswith('https://'):raise ValueError('参考图片必须来自本次任务的 HTTPS 资产')
        destination=directory/f'reference-{index+1}.jpg'
        if not destination.exists():
            response=session.get(url,timeout=(15,45));response.raise_for_status()
            if len(response.content)>50*1024*1024:raise ValueError('参考图片过大')
            destination.write_bytes(response.content)
        with Image.open(destination) as image:image.verify()
        if asset.get('sha256') and hashlib.sha256(destination.read_bytes()).hexdigest()!=asset['sha256']:
            raise ValueError('参考图哈希与原任务不一致')
        result.append(str(destination.resolve()))
    return result


def export(client,args):
    target=Path(args.output).resolve() if args.output else profile_dir()/'codex-handoffs'/(args.batch or args.job)
    target.mkdir(parents=True,exist_ok=True)
    if args.batch:
        bundle=client.get('/codex-handoffs/batches/'+args.batch)
        atomic(target/'batch.json',bundle)
        jobs=[j['job_id'] for j in bundle['jobs']]
    else:jobs=[args.job]
    output=[]
    for job in jobs:
        context=local_context(client,job)
        if context['kind']=='wb_listing_copy':
            from wb_codex_bridge import export as export_wb
            output.append(export_wb(context,target));continue
        if context['kind']=='amazon_listing_copy':
            from amazon_codex_bridge import export as export_amazon
            output.append(export_amazon(context,target));continue
        for snap in context['stores']:
            directory=target/job/snap['store_id'];directory.mkdir(parents=True,exist_ok=True)
            completed=bool(snap['resume']['assets'].get('package_ready'))
            handoff={k:snap[k] for k in ('job_id','publication_id','product_id','store_id','store_name','offer_id','revision','facts','requirements','image_policy','prior_copy_for_difference_check','resume')}
            handoff.update(contract=1,kind=context['kind'],target_worker_id=context['target_worker_id'],status=context['status'],completed=completed)
            runtime()
            from ozon_image_rules import export_rules
            handoff['image_generation_rules']=export_rules(snap)
            if square_batch_job(job):
                handoff['image_generation_rules'].update(aspect_ratio='1:1',example_sizes=[[1200,1200],[1254,1254]])
                handoff['image_generation_rules']['instructions'] += ' 本批次按用户要求生成1:1方图；保持印花位置和形状，桌面不摆物品。'
            handoff['copy_generation_rules']={'tags_target':30,'tags_max':30,'tag_max_chars_including_hash':30,'format':'#俄罗斯买家自然俄语检索词组，多词用下划线，不重复；避免中文直译、生硬造词和近义堆砌','avoid':'无关词、品牌、参数、货号、型号、促销词、商品名称；不足30个说明事实限制'}
            handoff['store_index']=snap.get('store_index',0)
            handoff['image_action']=(snap['resume']['assets'].get('personal_review') or {}).get('image_action') or {}
            if not completed:
                handoff['attribute_definitions']=definitions(snap)
                handoff['reference_files']=reference_files(snap,directory)
            atomic(directory/'handoff.json',handoff)
            saved=snap['resume']['content']
            template={'contract':1,'job_id':job,'store_id':snap['store_id'],'revision':snap['revision'],'image_action_id':handoff['image_action'].get('id',''),
                'content':({'title_ru':saved.get('title'),'description_ru':saved.get('description_ru'),'tags_ru':saved.get('tags',[]),'attributes':((snap['resume']['assets'].get('ai_preparation') or {}).get('content') or {}).get('attributes',[])} if saved.get('description_ru') else
                    {'title_ru':'','description_ru':'','tags_ru':[],'attributes':[]}),
                'images':[]}
            template['content'].update(tags_shortfall_reason='',poster_text_lines_ru=((snap['resume']['assets'].get('ai_preparation') or {}).get('content') or {}).get('poster_text_lines_ru',[]),creative_direction_en=((snap['resume']['assets'].get('ai_preparation') or {}).get('content') or {}).get('creative_direction_en',''))
            if not (directory/'output-template.json').exists():atomic(directory/'output-template.json',template)
            output.append({'job_id':job,'store_id':snap['store_id'],'completed':completed,'handoff':str(directory/'handoff.json'),'output':str(directory/'output-template.json')})
    atomic(target/'manifest.json',output)
    print(json.dumps({'contract':1,'output':str(target),'items':len(output)},ensure_ascii=False))


def image_record(row,work,name,*,generated=True,poster_lines=None,aspect_ratio='3:4'):
    from PIL import Image
    source=Path(row['path']).resolve()
    if not source.is_file() or source.stat().st_size>50*1024*1024:raise ValueError('生成图片不存在或过大')
    if row.get('matches_sku') is not True or not str(row.get('check_notes') or '').strip():
        raise ValueError('必须实际查看生成图并填写商品一致性检查记录')
    sha=hashlib.sha256(source.read_bytes()).hexdigest()
    if row.get('sha256') and row['sha256']!=sha:raise ValueError('生成图片哈希不一致')
    with Image.open(source) as image:
        if generated:
            from ozon_image_rules import validate_image
            validate_image(row,image.size,poster_lines,aspect_ratio=aspect_ratio)
        image.verify()
    destination=work/name;destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():
        if hashlib.sha256(destination.read_bytes()).hexdigest()!=sha:raise ValueError('原任务已保存不同图片，请先在原页面申请重新生图')
    else:shutil.copyfile(source,destination)
    return {'path':str(destination.resolve()),'sha256':sha,'matches_sku':True,'check_notes':str(row['check_notes'])[:1000]}


def submit(client,args):
    ctx=local_context(client,args.job)
    if ctx['kind']=='wb_listing_copy':
        from wb_codex_bridge import submit as submit_wb
        return submit_wb(client,args,ctx)
    if ctx['kind']=='amazon_listing_copy':
        from amazon_codex_bridge import submit as submit_amazon
        return submit_amazon(client,args,ctx)
    snap=next((s for s in ctx['stores'] if s['store_id']==args.store),None)
    if not snap:raise ValueError('店铺不属于原任务')
    value=json.loads(Path(args.file).read_text(encoding='utf-8'))
    if any(value.get(k)!=snap[k] for k in ('job_id','store_id','revision')):raise ValueError('输出不属于该原任务和修订')
    if snap['resume']['assets'].get('package_ready'):raise ValueError('本任务已准备完成，请直接查询状态，不要重新生成')
    action=(snap['resume']['assets'].get('personal_review') or {}).get('image_action') or {}
    if value.get('image_action_id','')!=action.get('id',''):raise ValueError('图片操作已变化，请重新导出')
    if not (profile_dir()/'codex-dictionary-cache'/'ozon-dictionaries.db').is_file():raise ValueError('请先导出任务取得原类目字典')
    content=value.get('content') or {}
    if not content.get('title_ru') or not content.get('description_ru'):raise ValueError('缺少文案输出')
    if not re.search('[А-Яа-яЁё]',content['title_ru']) or not re.search('[А-Яа-яЁё]',content['description_ru']):raise ValueError('上架文案必须填写俄文')
    if any(content['description_ru'].strip()==str(p.get('description_ru') or '').strip() for p in snap['prior_copy_for_difference_check']):raise ValueError('禁止复用其他任务或店铺文案')
    if not action:
        runtime()
        from erp.modules.ozon_follow.legacy.services.ai_listing import normalize_hashtags
        tags=content.get('tags_ru') or []
        if not isinstance(tags,list) or not tags or len(tags)>30 or normalize_hashtags(tags)!=tags:raise ValueError('标签必须是1–30个不重复标签，含#每个最多30字符，多词用下划线')
        if len(tags)<30 and not str(content.get('tags_shortfall_reason') or '').strip():raise ValueError('标签应尽量填满30个；确有事实限制时填写tags_shortfall_reason')
    policy=snap['image_policy'];mode=policy['mode'];roles=policy.get('roles',[])
    previous_path=profile_dir()/'catalog-ozon-preparations'/args.job/args.store/'codex-content.dpapi'
    previous=json.loads(protect(previous_path.read_bytes(),decrypt=True)) if previous_path.exists() else {}
    if action:
        saved=snap['resume']['content']
        if content['title_ru']!=saved.get('title') or content['description_ru']!=saved.get('description_ru'):raise ValueError('修复或重做图片时保留本任务成功文案')
        if previous.get('content'):content={**previous['content'],**{key:content.get(key) for key in ('poster_text_lines_ru','creative_direction_en','visual_analysis')}}
    need_main=mode in {'regenerate_main','regenerate_all'} or mode=='custom' and 'main' in roles
    need_additional=mode in {'regenerate_additional','regenerate_all'} or mode=='custom' and 'additional' in roles
    if action:need_main=True;need_additional=False
    images=value.get('images') or [];main=[r for r in images if r.get('role')=='main'];additional=[r for r in images if r.get('role')=='additional']
    count=max(1,min(6,int(snap['requirements'].get('additional_image_count') or 3)))
    if len(main)!=(1 if need_main else 0) or len(additional)!=(count if need_additional else 0) or len(main)+len(additional)!=len(images):
        raise ValueError('生成图数量和角色必须完全符合原图片策略')
    action=(snap['resume']['assets'].get('personal_review') or {}).get('image_action') or {}
    if need_main:
        runtime()
        from ozon_image_rules import poster_content
        content=poster_content(content,snap.get('store_index',0))
    if action and not main and not additional:raise ValueError('本次要求重新处理图片，必须提供新图片')
    work=profile_dir()/'catalog-ozon-preparations'/args.job/args.store
    work.mkdir(parents=True,exist_ok=True)
    suffix=action.get('id','initial')
    record={'producer':'codex-desktop','binding':codex_binding(snap),'content':content,'revision':snap['revision'],
            'image_action_id':action.get('id',''),'dictionary_cache_path':str(runtime()/'ozon-dictionaries.db')}
    if action:
        preserved=(snap['resume']['assets'].get('ai_preparation') or {}).get('generated_additional_urls') or []
        if preserved:record['preserved_additional_urls']=preserved
    if main:record['main_candidate']=image_record(main[0],work,f'ozon_follow/generated/{snap["facts"]["sku"]}/main-{suffix}.png',poster_lines=content['poster_text_lines_ru'],aspect_ratio='1:1' if square_batch_job(args.job) else '3:4')
    if additional:
        record['additional_candidates']=[image_record(row,work,f'codex-images/{suffix}/additional-{i+1}.png') for i,row in enumerate(additional)]
        if not main:
            # Reuse the selected main image, but require human review of the new gallery.
            directory=work/'codex-references';directory.mkdir(parents=True,exist_ok=True)
            references=reference_files(snap,directory)
            row={'path':references[0],'matches_sku':True,'check_notes':'原任务主图原样保留，仅用于连同新附图人工核验'}
            record['main_candidate']=image_record(row,work,f'ozon_follow/generated/{snap["facts"]["sku"]}/main-{suffix}.png',generated=False)
            record['main_candidate']['preserve_primary_url']=snap['facts']['assets'][0]['oss_url']
    destination=work/'codex-content.dpapi'
    if destination.exists():
        existing=json.loads(protect(destination.read_bytes(),decrypt=True))
        if existing.get('image_action_id','')==record['image_action_id'] and existing!=record:
            raise ValueError('本次接管结果已保存，禁止覆盖；图片更改请使用原重新生图按钮')
    temp=destination.with_suffix('.partial');temp.write_bytes(protect(json.dumps(record,ensure_ascii=False).encode()));temp.replace(destination)
    receipt={'revision':snap['revision'],'output_sha256':hashlib.sha256(json.dumps(record,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),'image_action_id':record['image_action_id']}
    (work/'codex-receipt.dpapi').write_bytes(protect(json.dumps(receipt).encode()))
    print(json.dumps({'job_id':args.job,'store_id':args.store,'saved':True,'resumed':False}))


def resume(client,args):
    ctx=local_context(client,args.job);receipts={}
    if ctx['kind']=='wb_listing_copy':
        from wb_codex_bridge import resume as resume_wb
        return resume_wb(client,args,ctx)
    if ctx['kind']=='amazon_listing_copy':
        from amazon_codex_bridge import resume as resume_amazon
        return resume_amazon(client,args,ctx)
    for snap in ctx['stores']:
        work=profile_dir()/'catalog-ozon-preparations'/args.job/snap['store_id']
        record=json.loads(protect((work/'codex-content.dpapi').read_bytes(),decrypt=True))
        if record['binding']!=codex_binding(snap):raise ValueError('原任务资料已变化，请重新导出')
        receipts[snap['store_id']]=json.loads(protect((work/'codex-receipt.dpapi').read_bytes(),decrypt=True))
    print(json.dumps(client.request('POST',f'/codex-handoffs/{args.job}/resume',{'worker_id':identity(),'receipts':receipts})[0],ensure_ascii=False))


def image_prompt(client,args):
    ctx=local_context(client,args.job)
    if ctx['kind']!='ozon_listing_generation':raise ValueError('本批接手保留所选图片，不支持 Ozon 生图步骤')
    snap=next((s for s in ctx['stores'] if s['store_id']==args.store),None)
    if not snap:raise ValueError('店铺不属于原任务')
    value=json.loads(Path(args.file).read_text(encoding='utf-8'))
    if any(value.get(k)!=snap[k] for k in ('job_id','store_id','revision')):raise ValueError('文案不属于该原任务')
    runtime()
    from ozon_image_rules import main_prompt
    prompt=main_prompt(value.get('content') or {},snap.get('store_index',0))
    prompt+='\n\nCURRENT USER REQUIREMENTS (cannot override factual SKU lock):\n'+json.dumps(snap['requirements'],ensure_ascii=False)
    if square_batch_job(args.job):
        prompt+='\n\nLATEST USER FORMAT REQUIREMENT FOR THIS BATCH: output a true 1:1 square Russian commercial poster. This supersedes any 3:4 aspect language above. Preserve the selected reference print position and shape; leave the tabletop empty.'
    output=Path(args.output) if args.output else Path(args.file).with_name('main-image-prompt.txt')
    output.write_text(prompt,encoding='utf-8')
    print(json.dumps({'prompt_file':str(output.resolve()),'aspect_ratio':'1:1' if square_batch_job(args.job) else '3:4'},ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['export','image-prompt','submit','resume','status'])
    parser.add_argument('--batch');parser.add_argument('--job');parser.add_argument('--store');parser.add_argument('--file');parser.add_argument('--output')
    args=parser.parse_args();client=Client()
    if args.action=='export':
        if bool(args.batch)==bool(args.job):raise ValueError('导出时填写一个批次ID或任务ID')
        export(client,args)
    elif args.action=='image-prompt':image_prompt(client,args)
    elif args.action=='submit':submit(client,args)
    elif args.action=='resume':resume(client,args)
    else:
        job=client.get('/jobs/'+args.job)
        print(json.dumps({k:job.get(k) for k in ('id','status','progress','error_code','error_message','steps')},ensure_ascii=False))


if __name__=='__main__':main()
