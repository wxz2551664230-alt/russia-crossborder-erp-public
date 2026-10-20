"""Personal-session executor. Downloads selected product files; never uses a local ERP DB."""
import hashlib,json,os,re,shutil,socket,threading,time,urllib.parse,urllib.request,uuid
from pathlib import Path
from erp_desktop import Client,NoRedirect,profile_dir,TemporaryConnectionError

MAX_FILE=128*1024**2
MAX_JOB=1024**3
MIN_FREE=512*1024**2

def checksum(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def restore(job,root,lost):
    if not re.fullmatch('[a-f0-9]{32}',job['id']):raise ValueError('任务编号无效')
    folder=(root/'restored'/job['id']).resolve()
    if not folder.is_relative_to(root.resolve()):raise ValueError('恢复目录无效')
    folder.mkdir(parents=True,exist_ok=True)
    files=job['payload']['files'];result=[];total=0
    if not files or len(files)>1000:raise ValueError('任务文件数量无效')
    for item in files:
        if lost.is_set():raise ValueError('任务连接已失效，停止下载')
        if not re.fullmatch('[a-f0-9]{32}',item['id']):raise ValueError('资产编号无效')
        url=urllib.parse.urlsplit(item['url'])
        if url.scheme!='https' or url.hostname!='example-assets.oss-cn-chengdu.aliyuncs.com' or url.port not in (None,443) or url.username or url.password or url.query or url.fragment:
            raise ValueError('只允许本账户 OSS 文件')
        suffix={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp'}.get(item['mime_type'],'.bin')
        target=folder/(item['id']+suffix)
        if target.is_symlink() or target.resolve().parent!=folder:raise ValueError('恢复文件路径无效')
        expected=item.get('sha256') or ''
        if target.exists():
            size=target.stat().st_size;digest=checksum(target)
            if not expected or digest!=expected or (item['size_bytes'] and size!=item['size_bytes']):
                raise ValueError('已有恢复文件与快照不同，请人工核对')
        else:
            budget=min(MAX_FILE,MAX_JOB-total)
            if budget<=0 or shutil.disk_usage(folder).free<MIN_FREE+(item['size_bytes'] or MAX_FILE):raise ValueError('本机空间不足，停止恢复')
            temporary=folder/(uuid.uuid4().hex+'.partial')
            size=0;sha=hashlib.sha256()
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
            remote=urllib.parse.urlunsplit((url.scheme,url.netloc,urllib.parse.quote(url.path,safe='/%:@'),'',''))
            try:
                with opener.open(remote,timeout=30) as response,temporary.open('xb') as output:
                    while chunk:=response.read(1024*1024):
                        if lost.is_set():raise ValueError('任务连接已失效')
                        size+=len(chunk)
                        if size>budget or shutil.disk_usage(folder).free<MIN_FREE+len(chunk):raise ValueError('恢复文件超过限制或磁盘不足')
                        output.write(chunk);sha.update(chunk)
                digest=sha.hexdigest()
                if size<=0 or (expected and digest!=expected) or (item['size_bytes'] and size!=item['size_bytes']):raise ValueError('OSS 文件校验失败')
                if target.exists():raise ValueError('恢复文件已被其他进程创建')
                os.rename(temporary,target)
            finally:
                temporary.unlink(missing_ok=True)
        total+=size
        if total>MAX_JOB:raise ValueError('单次恢复超过1GiB限制')
        result.append({'id':item['id'],'bytes':size,'sha256':digest})
    return {'files':result,'folder':str(folder),'product_id':job['payload']['product_id'],'verified':True}

def stopped_by_user(client,job,endpoint,headers):
    state=client.get('/jobs/'+job['id'])
    if state['status']=='running' and (state.get('pause_requested') or state.get('cancel_requested')):
        try:client.request('POST',endpoint+'/heartbeat',{},extra_headers=headers)
        except ValueError:pass
        state=client.get('/jobs/'+job['id'])
    if state['status'] in ('paused','cancelled'):
        print('任务已暂停或停止，已校验文件保留：'+job['id'],flush=True)
        return True
    return False


def execute_job(client,base,job,root):
    headers={'X-Worker-Attempt':str(job['attempts'])};stop=threading.Event();lost=threading.Event()
    endpoint=base+'/jobs/'+job['id']
    def renew():
        while not stop.wait(10):
            try:client.request('POST',endpoint+'/heartbeat',{},extra_headers=headers)
            except Exception:lost.set();return
    thread=threading.Thread(target=renew,daemon=True);thread.start()
    try:
        try:
            if job['type'] in {'pod_order_sync','pod_order_image_sync','pod_purchase_sync','pod_purchase_submit'}:
                from order_worker import run as read_orders
                result=read_orders(client,endpoint,job,root,lost)
            elif job['type'] in {'ad_optimization_analyze','ad_optimization_backtest'}:
                from ad_worker import run as analyze_ads
                result=analyze_ads(client,endpoint,job,root,lost)
            elif job['type']=='ad_optimization_sync':
                from ad_sync_worker import run as sync_ads
                result=sync_ads(client,endpoint,job,root,lost)
            elif job['type']=='ad_optimization_reports':
                from ad_report_worker import run as report_ads
                result=report_ads(client,endpoint,job,root,lost)
            elif job['type']=='ad_optimization_preview':
                from ad_preview_worker import run as preview_ads
                result=preview_ads(client,endpoint,job,root,lost)
            elif job['type']=='ad_optimization_execute':
                from ad_execution_worker import run as execute_ads
                result=execute_ads(client,endpoint,job,root,lost)
            elif job['type']=='ad_optimization_reconcile':
                from ad_reconcile_worker import run as reconcile_ads
                result=reconcile_ads(client,endpoint,job,root,lost)
            elif job['type']=='pod_wb_excel_export':
                from wb_worker import run as wb_export
                result=wb_export(client,endpoint,job,root,lost)
            elif job['type']=='pod_wb_api_publish':
                from wb_worker import run as wb_publish
                result=wb_publish(client,endpoint,job,root,lost)
            elif job['type']=='pod_wb_excel_reconcile':
                from wb_reconcile_worker import run as wb_reconcile
                result=wb_reconcile(client,endpoint,job,root,lost)
            elif job['type']=='pod_wb_history_reconcile':
                from wb_history_worker import run as wb_history
                result=wb_history(client,endpoint,job,root,lost)
            elif job['type']=='catalog_ozon_followup':
                from catalog_ozon_followup_worker import run as catalog_ozon_followup
                result=catalog_ozon_followup(client,endpoint,job,root,lost)
            elif job['type']=='catalog_ozon_publication' and job['payload'].get('batch_phase'):
                from catalog_ozon_batch_worker import run as batch_run
                result=batch_run(client,endpoint,job,root,lost)
            elif job['type']=='catalog_ozon_publication':
                from catalog_ozon_submit_worker import run as catalog_ozon_submit
                result=catalog_ozon_submit(client,endpoint,job,root,lost)
            elif job['type']=='catalog_ozon_prepare':
                from catalog_ozon_worker import run as catalog_ozon_prepare
                result=catalog_ozon_prepare(client,endpoint,job,root,lost)
            elif job['type']=='pod_wb_stock':
                from wb_stock_worker import run as wb_stock
                result=wb_stock(client,endpoint,job,root,lost)
            elif job['type']=='pod_wb_price':
                from wb_price_worker import run as wb_price
                result=wb_price(client,endpoint,job,root,lost)
            elif job['type'].startswith('pod_ai_design_'):
                from ai_worker import run as ai_design
                result=ai_design(client,endpoint,job,root,lost)
            elif job['type']=='pod_artwork_upload':
                from artwork_worker import run as upload_artwork
                result=upload_artwork(client,endpoint,job,root,lost)
            elif job['type'] in {'pod_native_composition','pod_us_composition'}:
                from composition_worker import run as compose
                result=compose(client,endpoint,job,root,lost)
            elif job['type']=='pod_supplier_gallery_sync':
                from supplier_gallery_worker import run as sync_gallery
                result=sync_gallery(client,endpoint,job,root,lost)
            elif job['type']=='pod_supplier_catalog_sync':
                from supplier_catalog_worker import run as sync_catalog
                result=sync_catalog(client,endpoint,job,root,lost)
            elif job['type'] in {'pod_supplier_us_catalog_sync','pod_supplier_us_freight_quote','pod_supplier_us_freight_quote'}:
                from us_catalog_worker import run as sync_us_catalog
                result=sync_us_catalog(client,endpoint,job,root,lost)
            elif job['type']=='catalog_self_selected_import':
                from catalog_import_worker import run as validate_catalog_import
                result=validate_catalog_import(client,endpoint,job,root,lost)
            elif job['type']=='catalog_pod_import':
                from pod_import_worker import run as inspect_pod_history
                result=inspect_pod_history(client,endpoint,job,root,lost)
            elif job['type']=='self_listing_pipeline':
                from self_listing_pipeline_worker import run as run_self_listing
                result=run_self_listing(client,endpoint,job,root,lost)
            elif job['type']=='catalog_ozon_options':
                from catalog_ozon_options_worker import run as ozon_options
                result=ozon_options(client,endpoint,job,root,lost)
            elif job['type']=='store_connection_test':
                from store_test_worker import run as test_store
                result=test_store(client,endpoint,job,root,lost)
            elif job['type'].startswith('amazon_'):
                from amazon_worker import run as run_amazon
                result=run_amazon(client,endpoint,job,root,lost)
            elif job['type']=='pod_library_restore':result=restore(job,root,lost)
            elif job['type']=='worker_probe':result={'ok':True,'computer':socket.gethostname()}
            else:raise ValueError('不支持的任务类型')
            if lost.is_set():
                state=client.get('/jobs/'+job['id'])
                if (state['status']!='completed' or not state['steps']
                        or any(step['details']!=result for step in state['steps'])):
                    raise ValueError('任务续租失败')
        except Exception as error:
            if job['type'] in {'ad_optimization_analyze','ad_optimization_backtest','ad_optimization_sync','ad_optimization_reports','ad_optimization_preview','ad_optimization_execute','ad_optimization_reconcile','pod_supplier_gallery_sync','pod_supplier_catalog_sync','pod_supplier_us_catalog_sync','pod_supplier_us_freight_quote','amazon_metadata','amazon_prepare','amazon_prepare_codex','amazon_validate','amazon_submit','amazon_reconcile','catalog_self_selected_import','catalog_pod_import','self_listing_pipeline'} and isinstance(error,TemporaryConnectionError):
                print('业务回执已留在本机，连接恢复后由原电脑继续提交：'+job['id'],flush=True)
                return
            if stopped_by_user(client,job,endpoint,headers):return
            if lost.is_set():raise
            code=getattr(error,'code','local_execution_failed')
            if job['type'] in {'pod_native_composition','pod_us_composition'} and type(error).__name__=='JobPaused':code='pod_write_outcome_unknown'
            client.request('POST',endpoint+'/fail',{'error_code':code,'error_message':type(error).__name__+': '+str(error)[:300]},extra_headers=headers)
            print('任务失败，结果已回传：'+str(error),flush=True)
            return
        if result.get('wb_price_deferred') is True:
            print('WB 原价格上传编号已保存；自动重试已停止，等待人工只读核对：'+job['id'],flush=True)
            return
        if job['type'] in {'catalog_ozon_prepare','catalog_ozon_publication','catalog_ozon_followup'} and result.get('catalog_ozon_finalized') is True:
            print('Ozon 产品库阶段已回传：'+job['id'],flush=True)
            return
        journal=root/'results';journal.mkdir(exist_ok=True)
        (journal/(job['id']+'.json')).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
        try:client.request('POST',endpoint+'/complete',{'result':result},extra_headers=headers)
        except Exception:
            if stopped_by_user(client,job,endpoint,headers):return
            state=client.get('/jobs/'+job['id'])
            if state['status']!='completed' or not state['steps'] or any(s['details']!=result for s in state['steps']):raise ValueError('结果回传未确认，已保留本机结果；重新连接后核对')
        print('任务完成：'+job['id'],flush=True)
        if job['type'] in {'ad_optimization_analyze','ad_optimization_backtest','ad_optimization_sync','ad_optimization_reports','ad_optimization_preview','ad_optimization_execute','ad_optimization_reconcile'}:
            from ad_worker import cleanup_completed_input
            try:cleanup_completed_input(root,job['id'])
            except OSError as error:print('广告输入清理稍后重试：'+str(error),flush=True)
        if result.get('folder'):print('文件保存位置：'+result['folder'],flush=True)
    finally:stop.set();thread.join(timeout=35)


def run(*,once=False):
    if not once:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        global _worker_mutex
        _worker_mutex = kernel.CreateMutexW(None, False, 'Local\\RussiaERPClient-' + hashlib.sha256(str(profile_dir().resolve()).encode()).hexdigest())
        if not _worker_mutex:
            raise OSError('无法建立本机执行端连接锁')
        if ctypes.get_last_error() == 183:
            return
    root=profile_dir();root.mkdir(parents=True,exist_ok=True)
    identity=root/'device-id.txt'
    if not identity.exists():
        with identity.open('x',encoding='ascii') as stream:stream.write('pc-'+uuid.uuid4().hex)
    worker=identity.read_text(encoding='ascii').strip()
    client=Client()
    from ai_worker import runtime_path
    client.request('POST','/execution-devices/local/register',{'id':worker,'name':socket.gethostname(),'amazon_version':1,'composition_version':2,'artwork_version':1,'ai_version':1,'order_read_version':1,'purchase_read_version':1,'purchase_submit_version':1,'wb_version':1,'wb_api_version':1,'wb_reconcile_version':1,'wb_history_version':3,'ozon_promotion_unlimited_version':1,'wb_stock_version':1,'wb_price_version':1,'ad_analysis_version':1,'ad_backtest_version':1,'ad_sync_version':1,'ad_report_version':1,'ad_preview_version':1,'ad_execute_version':1,'ad_reconcile_version':1,'supplier_gallery_version':1,'supplier_catalog_version':3,'catalog_import_version':1,'pod_import_version':1,'self_listing_version':1,'store_test_version':1,'catalog_ozon_version':3,'codex_generation_version':4,'upscale_ready':bool(runtime_path())})
    if not once:
        client.request('POST', '/execution-devices/select', {'worker_id': worker})
    if not once and getattr(client, 'base', None):
        from local_picker import start_picker
        try:
            start_picker(client.base)
        except OSError as error:
            print('本机文件夹选择器未启动：'+str(error),flush=True)
    base='/execution-devices/local/'+worker
    print('本机执行端已连接：'+worker,flush=True)
    disconnected=False
    while True:
        try:
            job=client.request('POST',base+'/claim',{})[0]
        except TemporaryConnectionError:
            if once:raise
            if not disconnected:print('连接暂时中断，执行端保持打开，每 10 秒重新连接；请勿重复提交任务。',flush=True)
            disconnected=True;time.sleep(10);continue
        if disconnected:print('连接已恢复，继续等待原电脑的任务。',flush=True)
        disconnected=False
        if job:
            try:execute_job(client,base,job,root)
            except Exception as error:
                print('本机执行连接中断，保留原任务检查点；重新连接后核对：'+type(error).__name__,flush=True)
                if once:raise
                time.sleep(3)
        if once:return
        time.sleep(3)
