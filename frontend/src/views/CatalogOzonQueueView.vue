<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import CatalogOzonTabs from '../components/CatalogOzonTabs.vue'
import { api } from '../api'
import { sharedProductsPilot } from '../deployment'

const props=defineProps<{mode:'stock'|'promotion'|'completed'}>(),loading=ref(false),publications=ref<any[]>([]),selectedIds=ref<string[]>([]),settingsOpen=ref(false),submitting=ref(false)
const storeFilter=ref('')
watch(storeFilter,()=>{selectedIds.value=[];initializeInputs();if(props.mode==='stock')void loadAllWarehouses();if(props.mode==='promotion')void loadAllPromotions()})
const optionsErrors=reactive<Record<string,string>>({})
const optionsPending=new Map<string,Promise<void>>()
const scanBusy=ref(false),scanStore=ref(''),scanMessage=ref('')
const storeRegistry=ref<any[]>([]),optionsBusy=reactive<Record<string,boolean>>({})
const scanStores=computed(()=>storeRegistry.value.length?storeRegistry.value:[...new Map(publications.value.flatMap(p=>p.stores).map((s:any)=>[s.store_id,s])).values()] as any[])
const warehouseOptions=reactive<Record<string,any[]>>({}),promotionOptions=reactive<Record<string,any[]>>({}),inputs=reactive<Record<string,any>>({}),storeInputs=reactive<Record<string,any>>({})
const title=computed(()=>({stock:'待添加库存',promotion:'待添加促销',completed:'已完成'})[props.mode]),description=computed(()=>({stock:'商品已通过 Seller API 创建成功，按店铺批量设置仓库与库存。',promotion:'库存阶段已处理，按店铺批量选择促销活动。',completed:'商品建品、库存和促销阶段均已处理完成。'})[props.mode])
const rows=computed(()=>publications.value.flatMap(publication=>publication.stores.filter((store:any)=>(!storeFilter.value||store.store_id===storeFilter.value)&&store.status===(props.mode==='stock'?'stock_pending':props.mode==='promotion'?'promotion_pending':'completed')).map((store:any)=>({publication,store}))))
const busy=(row:any)=>['processing','unknown'].includes(row.store[props.mode==='stock'?'stock_status':'promotion_status'])
const selectedRows=computed(()=>rows.value.filter(row=>selectedIds.value.includes(row.store.id)))
const selectedStores=computed(()=>[...new Map(selectedRows.value.map(row=>[row.store.store_id,row.store])).values()])
function storageKey(row:any){return `catalog-ozon-${props.mode}:${row.store.id}`}
function legacyStorageKey(storeId:string){return `catalog-ozon-${props.mode}:${storeId}`}
function initializeInputs(){for(const row of rows.value){
 const saved=localStorage.getItem(storageKey(row));
 const legacy=localStorage.getItem(legacyStorageKey(row.store.store_id));
 const shared=legacy?JSON.parse(legacy):{};
 const defaults={stock:props.mode==='promotion'?null:shared.stock||1,warehouse_id:shared.warehouse_id,action_id:shared.action_id,
  action_price:row.store.pricing?.action_price||row.store.pricing?.price};
 const individual=saved?JSON.parse(saved):{};
 if(String(individual._planned_action_price||'')!==String(defaults.action_price||''))delete individual.action_price;
 // Old store-wide saved action_price must not silently override another product.
 inputs[row.store.id]??={...defaults,...individual};
 if(props.mode==='promotion')inputs[row.store.id].stock=null;
}}
let loadVersion=0
async function load(){const version=++loadVersion,mode=props.mode;loading.value=true;try{const data=await readPublications();if(version!==loadVersion||mode!==props.mode||disposed)return;publications.value=data;if(version!==loadVersion||mode!==props.mode||disposed)return;initializeInputs();selectedIds.value=[]}finally{if(version===loadVersion)loading.value=false}
 if(props.mode==='stock')void loadAllWarehouses().catch((error:any)=>{if(!disposed)ElMessage.warning(error.message||'仓库读取尚未完成')})
 if(props.mode==='promotion')void loadAllPromotions().catch((error:any)=>{if(!disposed)ElMessage.warning(error.message||'促销读取尚未完成')})
 if(!storeRegistry.value.length)void api.get<any>('/stores').then(data=>{if(!disposed)storeRegistry.value=(data.items||[]).filter((s:any)=>s.platform==='ozon').map((s:any)=>({store_id:s.id,store_name:s.name}))}).catch(()=>{})
}
async function readPublications(mode=props.mode){const stage=mode==='stock'?'stock_pending':mode==='promotion'?'promotion_pending':'completed';const result:any[]=[];for(let page=1;page<=100;page++){const data=await api.get<any>(`/publications?page_size=200&page=${page}&store_status=${stage}&compact=true`);result.push(...(data.items||[]));if(result.length>=data.total||!(data.items||[]).length)break}return result}
async function scanStatus(entries?:any[],refresh=false){
 if(scanBusy.value)throw new Error('上架状态扫描正在执行');scanBusy.value=true;scanMessage.value='正在等待当前电脑批量读取平台状态…';
 try{const result=await api.post<any>('/publications/stock-scan',{entries,store_id:entries?.length?undefined:scanStore.value,refresh});const counts:Record<string,number>={};
 for(const id of result.job_ids||[]){let done=false;for(let attempt=0;attempt<240;attempt++){if(disposed)throw new Error('扫描保留在任务中心');const job=await api.get<any>(`/jobs/${id}`);if(job.status==='completed'){const summary=JSON.parse(job.external_ref||'{}').stock_scan_summary||{};for(const [key,value] of Object.entries(summary))counts[key]=(counts[key]||0)+Number(value);done=true;break}if(['failed','cancelled','action_required'].includes(job.status))throw new Error(job.error_message||'扫描失败，请在任务中心查看');await new Promise(resolve=>setTimeout(resolve,1500))}if(!done)throw new Error('扫描仍在任务中心执行，请稍后刷新核对')}
 scanMessage.value=`扫描完成：已通过 ${counts.ready||0}，复用有效结果 ${result.cached_count||0}，审核中 ${counts.pending||0}，未通过/未找到/归档/编号冲突 ${(counts.blocked||0)+(counts.not_found||0)+(counts.unavailable||0)+(counts.id_conflict||0)}，跳过 ${counts.skipped||0}。`;
 const scannedPublications=await readPublications('stock');if(props.mode==='stock')publications.value=scannedPublications;return {...result,publications:scannedPublications};
 }catch(error:any){scanMessage.value=error.message||'扫描未完成';throw error}finally{scanBusy.value=false}
}
async function scanAll(){try{await scanStatus(undefined,true);await load()}catch(error:any){ElMessage.error(error.message)}}
async function ensureScanned(items:any[]){const result=await scanStatus(items.map(row=>({publication_id:row.publication.id,store_id:row.store.store_id})));for(const original of items){const p=result.publications.find((p:any)=>p.id===original.publication.id);const c=p?.stores.find((s:any)=>s.store_id===original.store.store_id);if(c?.status!=='stock_pending'||['processing','unknown'].includes(c.stock_status))throw new Error('部分商品当前不可添加库存，请根据扫描结果重新选择；本次未写库存')}}
function selectionChanged(items:any[]){selectedIds.value=items.map(item=>item.store.id)}
function warehouseId(item:any){return Number(item.warehouse_id||item.id)}
function warehouseLabel(item:any){return item.name||item.warehouse_name||`仓库 ${warehouseId(item)}`}
function promotionId(item:any){return Number(item.id||item.action_id)}
function promotionLabel(item:any){const title=item.title||item.name||`活动 ${promotionId(item)}`;const end=item.date_end||item.end_date||item.end_at;return end?`${title}（截止 ${String(end).slice(0,10)}）`:title}
let disposed=false
async function readOptions(storeId:string,kind:'warehouses'|'promotions',refresh=false){
  if(!sharedProductsPilot.value)return (await api.get<any>(`/stores/${storeId}/${kind}`)).items||[]
  const response=await api.post<any>(`/stores/${storeId}/ozon-options/${kind}${refresh?'?refresh=true':''}`,{})
  if(!response.job_id)return response.items||[]
  for(let attempt=0;attempt<120;attempt++){
    if(disposed)throw new Error('页面已关闭，读取任务保留在任务中心')
    const job=await api.get<any>(`/jobs/${response.job_id}`)
    if(job.status==='completed')return job.steps?.[0]?.details?.items||[]
    if(['failed','cancelled','action_required'].includes(job.status))throw new Error(job.error_message||'店铺资料读取未完成')
    await new Promise(resolve=>setTimeout(resolve,1000))
  }
  throw new Error('电脑尚未完成读取，请在任务中心查看原任务')
}
async function loadStoreOptions(storeId:string,kind:'warehouses'|'promotions',refresh=false){
 const key=`${storeId}:${kind}`,target=kind==='warehouses'?warehouseOptions:promotionOptions
 if(optionsPending.has(key))return optionsPending.get(key)
 if(!refresh&&(Object.prototype.hasOwnProperty.call(target,storeId)||optionsErrors[key]))return
 const task=(async()=>{optionsBusy[storeId]=true;delete optionsErrors[key];try{target[storeId]=await readOptions(storeId,kind,refresh)}catch(error:any){if(!disposed)optionsErrors[key]=error.message||'店铺资料读取失败';throw error}finally{optionsBusy[storeId]=false;optionsPending.delete(key)}})()
 optionsPending.set(key,task);return task
}
async function loadWarehouse(storeId:string,refresh=false){return loadStoreOptions(storeId,'warehouses',refresh)}
async function loadPromotion(storeId:string,refresh=false){return loadStoreOptions(storeId,'promotions',refresh)}
async function loadAllWarehouses(refresh=false){await Promise.allSettled([...new Set(rows.value.map(row=>row.store.store_id))].map(id=>loadWarehouse(id,refresh)));if(refresh&&!disposed&&!Object.keys(optionsErrors).some(k=>k.endsWith(':warehouses')))ElMessage.success('各店铺仓库已刷新')}
async function loadAllPromotions(refresh=false){await Promise.allSettled([...new Set(rows.value.map(row=>row.store.store_id))].map(id=>loadPromotion(id,refresh)));if(refresh&&!disposed&&!Object.keys(optionsErrors).some(k=>k.endsWith(':promotions')))ElMessage.success('各店铺促销已刷新')}
const visibleOptionsErrors=computed(()=>scanStores.value.flatMap(store=>{const kind=props.mode==='stock'?'warehouses':'promotions',message=optionsErrors[`${store.store_id}:${kind}`];return message&&(!storeFilter.value||storeFilter.value===store.store_id)?[`${store.store_name}：${message}；可点击刷新${props.mode==='stock'?'仓库':'促销'}重读。`]:[]}))
function saveRow(row:any){localStorage.setItem(storageKey(row),JSON.stringify({...inputs[row.store.id],_planned_action_price:row.store.pricing?.action_price||row.store.pricing?.price}))}
function openBatchSettings(){if(!selectedRows.value.length)return ElMessage.warning('请先勾选要处理的店铺商品');for(const store of selectedStores.value){const first=selectedRows.value.find(row=>row.store.store_id===store.store_id);const value=first?inputs[first.store.id]:{};storeInputs[store.store_id]=props.mode==='stock'?{warehouse_id:value?.warehouse_id,stock:value?.stock||1}:{action_id:value?.action_id}}settingsOpen.value=true}
function applyBatchSettings(){for(const store of selectedStores.value){const value=storeInputs[store.store_id];if(props.mode==='stock'&&(!value?.warehouse_id||!value?.stock))return ElMessage.warning(`请填写 ${store.store_name} 的仓库与库存`);if(props.mode==='promotion'&&!value?.action_id)return ElMessage.warning(`请选择 ${store.store_name} 的促销活动`)}for(const row of selectedRows.value){inputs[row.store.id]={...inputs[row.store.id],...storeInputs[row.store.store_id]};saveRow(row)}settingsOpen.value=false;ElMessage.success('已按店铺应用到勾选商品')}
async function addStock(row:any){if(busy(row))throw new Error('原库存任务尚未核对');const value=inputs[row.store.id];if(!value?.warehouse_id||!value?.stock)return ElMessage.warning('请选择仓库并填写库存');const params={warehouse_id:Number(value.warehouse_id),stock:Number(value.stock)};if(sharedProductsPilot.value)await ensureScanned([row]);return await api.post<any>(`/publications/${row.publication.id}/stores/${row.store.store_id}/stock`,params)}
async function addPromotion(row:any){if(busy(row))throw new Error('原促销任务尚未核对');const value=inputs[row.store.id];if(!value?.action_id||!value?.action_price)return ElMessage.warning('请选择促销并确认活动价');return await api.post<any>(`/publications/${row.publication.id}/stores/${row.store.store_id}/promotion`,{action_id:Number(value.action_id),action_price:Number(value.action_price),stock:null})}
async function runOne(row:any){if(submitting.value||busy(row))return;submitting.value=true;try{const result=props.mode==='stock'?await addStock(row):await addPromotion(row);if(!result)return;ElMessage.success(result.job_id?'已提交到当前执行电脑':props.mode==='stock'?'库存已添加':'促销已添加');await load()}finally{submitting.value=false}}
async function runBatch(){
 if(submitting.value||scanBusy.value)return
 const mode=props.mode
 const plannedRows=[...selectedRows.value]
 if(!plannedRows.length)return ElMessage.warning('请先勾选店铺商品')
 const entries=plannedRows.map(row=>({publication_id:row.publication.id,store_id:row.store.store_id,params:mode==='stock'?{warehouse_id:Number(inputs[row.store.id]?.warehouse_id),stock:Number(inputs[row.store.id]?.stock)}:{action_id:Number(inputs[row.store.id]?.action_id),action_price:Number(inputs[row.store.id]?.action_price),stock:null}}))
 for(let i=0;i<plannedRows.length;i++){
  if(busy(plannedRows[i]))return ElMessage.warning('选中商品有处理中或结果未知的任务，请先核对')
  const value=entries[i].params
  if(mode==='stock'&&(!Number.isSafeInteger(value.warehouse_id)||Number(value.warehouse_id)<=0||!Number.isSafeInteger(value.stock)||Number(value.stock)<=0))return ElMessage.warning('请先按店铺选择仓库，并填写正整数库存')
  if(mode==='promotion'&&(!Number.isSafeInteger(value.action_id)||Number(value.action_id)<=0||!Number.isFinite(value.action_price)||Number(value.action_price)<=0))return ElMessage.warning('请先选择促销并设置活动价')
 }
 await ElMessageBox.confirm(`确认批量处理 ${plannedRows.length} 个店铺商品？已成功项目不会重复执行。`,'批量操作确认',{type:'warning'})
 submitting.value=true
 if(sharedProductsPilot.value){
  try{
   if(mode==='stock')await ensureScanned(plannedRows)
   const result=await api.post<any>(`/publications/batch/${mode}`,{entries})
   ElMessage.success(`已创建 ${result.batch_count} 个批次，共 ${result.product_count} 件商品`)
   await load()
  }catch(error:any){ElMessage.error(error.message||'批次提交未确认，请刷新核对原任务')}
  finally{submitting.value=false}
  return
 }
 let completed=0,queued=0,failed=0
 try{for(const row of plannedRows){try{const result=mode==='stock'?await addStock(row):await addPromotion(row);if(result?.job_id)queued++;else if(result)completed++;else{failed++;break}}catch{failed++;break}}ElMessage[failed?'warning':'success'](`已完成 ${completed} 个，已提交电脑执行 ${queued} 个${failed?'，出错后已停止后续提交':''}`);await load()}
 finally{submitting.value=false}
}

let poll:ReturnType<typeof setInterval>|undefined
async function refreshProcessing(){if(loading.value||submitting.value||!rows.value.some(row=>row.store[props.mode==='stock'?'stock_status':'promotion_status']==='processing'))return;try{publications.value=await readPublications();initializeInputs()}catch{/* Retain visible state on transient read failures. */}}
async function skipSelected(){if(!selectedRows.value.length)return ElMessage.warning('请先勾选店铺商品');const label=props.mode==='stock'?'库存':'促销';await ElMessageBox.confirm(`确认跳过 ${selectedRows.value.length} 个商品的${label}阶段？`,'跳过确认',{type:'warning'});for(const row of selectedRows.value)await api.post(`/publications/${row.publication.id}/stores/${row.store.store_id}/${props.mode==='stock'?'skip-stock':'skip-promotion'}`,{});await load()}
watch(()=>props.mode,()=>{publications.value=[];selectedIds.value=[];scanMessage.value='';void load()})
onMounted(()=>{void load();poll=setInterval(()=>{void refreshProcessing()},3000)})
onUnmounted(()=>{disposed=true;if(poll)clearInterval(poll)})
</script>

<template><div class="page-stack"><CatalogOzonTabs/><section class="panel"><div class="panel-head"><div><h2>{{title}}</h2><p>{{description}}</p></div><div><el-select v-model="storeFilter" clearable placeholder="筛选店铺（全部）" style="width:180px"><el-option v-for="store in scanStores" :key="store.store_id" :label="store.store_name" :value="store.store_id"/></el-select><el-select v-if="mode==='stock'" v-model="scanStore" clearable placeholder="扫描店铺（默认全部）" style="width:190px"><el-option v-for="store in scanStores" :key="store.store_id" :label="store.store_name" :value="store.store_id"/></el-select><el-button v-if="mode==='stock'&&sharedProductsPilot" :loading="scanBusy" :disabled="submitting" @click="scanAll">扫描上架状态</el-button><el-button @click="load">刷新</el-button><el-button v-if="mode==='stock'" @click="loadAllWarehouses(true)">刷新仓库</el-button><el-button v-if="mode==='promotion'" @click="loadAllPromotions(true)">刷新促销</el-button><el-button v-if="mode!=='completed'" :disabled="submitting||!selectedIds.length" @click="openBatchSettings">按店铺批量设置</el-button><el-button v-if="mode!=='completed'" type="primary" :disabled="submitting||!selectedIds.length" @click="runBatch">批量{{mode==='stock'?'添加库存':'添加促销'}}（{{selectedIds.length}}）</el-button><el-button v-if="mode!=='completed'" type="warning" plain :disabled="submitting||!selectedIds.length" @click="skipSelected">批量跳过</el-button></div></div>
  <el-alert v-if="mode==='stock'&&scanMessage" :title="scanMessage" :closable="false" type="info" show-icon/>
  <el-alert v-for="message in visibleOptionsErrors" :key="message" :title="message" type="warning" :closable="false" show-icon/>
  <el-table v-loading="loading" :data="rows" empty-text="当前队列没有商品" @selection-change="selectionChanged"><el-table-column v-if="mode!=='completed'" type="selection" width="48" :selectable="(row:any)=>!busy(row)"/><el-table-column label="产品 / SKU" min-width="230"><template #default="{row}"><b>{{row.publication.product_name}}</b><small class="table-sub mono">{{row.publication.sku}}</small></template></el-table-column><el-table-column label="店铺 / Offer ID" min-width="220"><template #default="{row}"><b>{{row.store.store_name}}</b><small class="table-sub mono">{{row.store.offer_id}}</small></template></el-table-column>
  <el-table-column v-if="mode==='stock'" label="仓库与库存" min-width="390"><template #default="{row}"><div class="inline-operation"><el-select :loading="optionsBusy[row.store.store_id]" v-model="inputs[row.store.id].warehouse_id" placeholder="选择仓库" @change="saveRow(row)"><el-option v-for="item in warehouseOptions[row.store.store_id]||[]" :key="warehouseId(item)" :label="warehouseLabel(item)" :value="warehouseId(item)"/></el-select><el-input-number v-model="inputs[row.store.id].stock" :min="1" @change="saveRow(row)"/><el-button type="primary" :disabled="submitting||busy(row)" @click="runOne(row)">{{row.store.stock_status==='processing'?'电脑执行中':row.store.stock_status==='unknown'?'结果待核对':'本店添加库存'}}</el-button></div></template></el-table-column>
  <el-table-column v-if="mode==='promotion'" label="促销与活动价" min-width="490"><template #default="{row}"><div class="inline-operation"><el-select :loading="optionsBusy[row.store.store_id]" v-model="inputs[row.store.id].action_id" filterable placeholder="选择活动" @change="saveRow(row)"><el-option v-for="item in promotionOptions[row.store.store_id]||[]" :key="promotionId(item)" :label="promotionLabel(item)" :value="promotionId(item)"/></el-select><el-input-number v-model="inputs[row.store.id].action_price" :min="1" :precision="2" @change="saveRow(row)"/><span>活动数量：无限</span><el-button type="primary" :disabled="submitting||busy(row)" @click="runOne(row)">{{row.store.promotion_status==='processing'?'电脑执行中':row.store.promotion_status==='unknown'?'结果待核对':'本店添加促销'}}</el-button></div></template></el-table-column>
  <el-table-column label="OZON 商品 ID" width="170"><template #default="{row}">{{row.store.external_product_id||'等待回写'}}</template></el-table-column><el-table-column v-if="mode==='completed'" label="结果" width="190"><template #default="{row}"><el-tag type="success">已完成</el-tag><small class="table-sub">库存 {{row.store.stock_status}} · 促销 {{row.store.promotion_status}}</small></template></el-table-column></el-table>
  <el-dialog v-model="settingsOpen" :title="mode==='stock'?'按店铺批量设置仓库与库存':'按店铺批量设置促销'" width="760px"><el-table :data="selectedStores"><el-table-column prop="store_name" label="店铺" min-width="150"/><el-table-column v-if="mode==='stock'" label="仓库" min-width="300"><template #default="{row}"><el-select v-model="storeInputs[row.store_id].warehouse_id" style="width:100%" placeholder="选择该店铺仓库"><el-option v-for="item in warehouseOptions[row.store_id]||[]" :key="warehouseId(item)" :label="warehouseLabel(item)" :value="warehouseId(item)"/></el-select></template></el-table-column><el-table-column v-if="mode==='stock'" label="每个商品库存" width="170"><template #default="{row}"><el-input-number v-model="storeInputs[row.store_id].stock" :min="1"/></template></el-table-column><el-table-column v-if="mode==='promotion'" label="促销活动" min-width="460"><template #default="{row}"><el-select v-model="storeInputs[row.store_id].action_id" filterable style="width:100%" placeholder="选择该店铺活动"><el-option v-for="item in promotionOptions[row.store_id]||[]" :key="promotionId(item)" :label="promotionLabel(item)" :value="promotionId(item)"/></el-select></template></el-table-column></el-table><template #footer><el-button @click="settingsOpen=false">取消</el-button><el-button type="primary" @click="applyBatchSettings">应用到已勾选商品</el-button></template></el-dialog>
</section></div></template>
