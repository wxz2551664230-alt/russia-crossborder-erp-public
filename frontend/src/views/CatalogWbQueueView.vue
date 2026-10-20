<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import CatalogWbTabs from '../components/CatalogWbTabs.vue'
import WbShopSyncPanel from '../components/WbShopSyncPanel.vue'
import { api } from '../api'
const props=defineProps<{mode:string}>()
const labels:Record<string,[string,string]>={image_review:['生图核验','逐 SKU 核对 WB 主图和附图。'],pending_submit:['待提交 WB','发布资料核验完成后在这里批量提交。'],processing:['建品处理中','WB 异步建品任务及结果回查。'],pending_stock:['待添加库存','商品卡和图片完成后，按店铺批量设置卖家仓库存量。'],pending_price:['待设置价格与折扣','库存成功后，批量提交价格和折扣并等待 WB 处理。'],completed:['已完成','WB 建品、图片、库存、价格和折扣均已处理。'],exceptions:['异常处理','查看失败原因并从断点重试。']}
const title=computed(()=>labels[props.mode]?.[0]||'WB 上架'),description=computed(()=>labels[props.mode]?.[1]||'')
const loading=ref(false),submitting=ref(false),items=ref<any[]>([]),selected=ref<any[]>([]),warehouses=ref<any[]>([])
const page=ref(1),pageSize=ref(30),total=ref(0)
const search=ref(''),storeId=ref(''),stores=ref<any[]>([])
let loadRequest=0
const form=reactive<{warehouse_id?:number,amount:number,price?:number,discount_percent:number}>({amount:10,discount_percent:30})
const apiStage=computed(()=>['processing','pending_price','pending_stock','completed','exceptions'].includes(props.mode)?props.mode:'processing')
async function load(){const request=++loadRequest;selected.value=[];loading.value=true;try{const result=await api.get<any>(`/pod/wb/publications?stage=${apiStage.value}&page=${page.value}&page_size=${pageSize.value}&search=${encodeURIComponent(search.value.trim())}&store_id=${encodeURIComponent(storeId.value)}`);if(request!==loadRequest)return;items.value=result.items||[];total.value=result.total||0}finally{if(request===loadRequest)loading.value=false}}
function searchRows(){page.value=1;load()}
function changePage(value:number){page.value=value;load()}
const removing=ref('')
async function removeRow(row:any){
  try{
    await ElMessageBox.confirm(`将“${row.sku}”从上架队列移除，保留产品库与任务记录，不删除 WB 商品。原任务将不能重试，需要时请重新创建上架任务。`,'移除误操作记录',{confirmButtonText:'移除记录',cancelButtonText:'保留',type:'warning'})
    removing.value=row.id
    await api.post(`/pod/wb/publications/${row.id}/remove`,{})
    ElMessage.success('已从上架队列移除');await load()
  }catch(error){if(error!=='cancel'&&error!=='close')ElMessage.error(String((error as Error).message||error))}finally{removing.value=''}
}
async function removeSelected(){
  const rows=selected.value.filter(row=>row.can_remove)
  if(!rows.length)return
  try{await ElMessageBox.confirm(`从本地上架队列移除所选 ${rows.length} 项，保留产品库和任务记录，不删除 WB 商品。涉及的原任务不能重试，需要时请重新创建上架任务。`,'批量移除',{confirmButtonText:'移除所选',cancelButtonText:'取消',type:'warning'})}catch{return}
  let count=0
  try{for(const row of rows){removing.value=row.id;await api.post(`/pod/wb/publications/${row.id}/remove`,{});count++}ElMessage.success(`已移除 ${count} 项`)}
  catch(error){ElMessage.error(`已移除 ${count} 项，其余未处理：${String((error as Error).message||error)}`)}
  finally{removing.value='';selected.value=[];await load()}
}
async function reconcilePrice(row:any){
  if(!row.price)return ElMessage.warning('这条历史记录缺少计划价格，暂不能核对')
  try{await ElMessageBox.confirm(`只读核对 ${row.offer_id} 在 WB 的价格 ${row.price} CNY、折扣 ${row.discount_percent}%，不会提交价格。`,'核对现有价格',{confirmButtonText:'开始核对',cancelButtonText:'取消',type:'info'})}catch{return}
  submitting.value=true
  try{await api.post('/pod/wb/publications/price/reconcile',{ledger_ids:[row.id],price:row.price,discount_percent:row.discount_percent});ElMessage.success('已交给当前电脑只读核对');await load()}
  catch(error){ElMessage.error(String((error as Error).message||error))}
  finally{submitting.value=false}
}
async function resubmitVerifiedPrice(row:any){
  if(!row.price)return ElMessage.warning('这条记录缺少计划价格')
  try{await ElMessageBox.confirm(`已只读核对 ${row.offer_id} 的平台价格仍与计划不符。现在由你手动提交 ${row.price} CNY、折扣 ${row.discount_percent}%；不会自动重试。`,'人工重新提交价格',{confirmButtonText:'提交一次',cancelButtonText:'取消',type:'warning'})}catch{return}
  submitting.value=true
  try{await api.post('/pod/wb/publications/price',{ledger_ids:[row.id],price:row.price,discount_percent:row.discount_percent});ElMessage.success('人工提交任务已创建');await load()}
  catch(error){ElMessage.error(String((error as Error).message||error))}
  finally{submitting.value=false}
}
function changePageSize(value:number){pageSize.value=value;page.value=1;load()}
function selectable(row:any){return ['processing','exceptions'].includes(props.mode)?row.can_remove:row.status===props.mode&&!(props.mode==='pending_price'&&row.price_submit_blocked_reason)}
const warehouseLoading=ref(false)
const warehouseStoreId=computed(()=>{const stores=[...new Set(selected.value.map(row=>row.store_id))];return stores.length===1?String(stores[0]):''})
let warehouseRequest=0
async function loadWarehouses(notify=true){
  const storeId=warehouseStoreId.value
  if(!storeId)return ElMessage.warning('请先勾选同一家 WB 店铺的商品')
  const request=++warehouseRequest
  warehouseLoading.value=true
  try{
    const result=await api.get<any>(`/pod/wb/warehouses?store_id=${encodeURIComponent(storeId)}`)
    if(request!==warehouseRequest||warehouseStoreId.value!==storeId)return
    warehouses.value=result.items||[]
    if(!warehouses.value.some(row=>row.id===form.warehouse_id))form.warehouse_id=undefined
    if(!warehouses.value.length)ElMessage.warning('这家店铺没有返回可用仓库')
    else if(notify)ElMessage.success(`已加载 ${warehouses.value.length} 个仓库`)
  }catch(error){if(request===warehouseRequest)ElMessage.error(String((error as Error).message||error))}
  finally{if(request===warehouseRequest)warehouseLoading.value=false}
}
function selectionChanged(rows:any[]){selected.value=rows}
watch(warehouseStoreId,()=>{warehouseRequest++;warehouses.value=[];form.warehouse_id=undefined;warehouseLoading.value=false;if(props.mode==='pending_stock'&&warehouseStoreId.value)loadWarehouses(false)})
async function submitBatch(){if(!selected.value.length)return ElMessage.warning('请先勾选商品');if(new Set(selected.value.map(row=>row.store_id)).size!==1)return ElMessage.warning('一次只能处理同一个 WB 店铺');if(props.mode==='pending_price'&&!form.price&&selected.value.some(row=>!row.price))return ElMessage.warning('历史同步商品没有计划价格，请先填写统一价格');submitting.value=true;try{if(props.mode==='pending_stock'){if(warehouseLoading.value)return ElMessage.warning('仓库正在加载，请稍后');if(!form.warehouse_id||!warehouses.value.some(row=>row.id===form.warehouse_id))return ElMessage.warning('请选择当前店铺的 WB 卖家仓库');await api.post('/pod/wb/publications/stock',{ledger_ids:selected.value.map(row=>row.id),warehouse_id:form.warehouse_id,amount:form.amount});ElMessage.success('批量库存任务已进入后台，成功后自动进入待设置价格')}else{await api.post('/pod/wb/publications/price',{ledger_ids:selected.value.map(row=>row.id),price:form.price||null,discount_percent:form.discount_percent});ElMessage.success('批量价格和折扣任务已进入后台，WB 确认后自动进入已完成')}await load();selected.value=[]}catch(error){ElMessage.error(String((error as Error).message||error))}finally{submitting.value=false}}
function statusText(value:string){return ({sync_conflict:'同步匹配冲突，需核验',sync_unknown:'已建品，状态未核验',cancelled:'已停止',paused:'已暂停',withdrawn:'已移除',queued:'等待建品',running:'正在建品',submitted:'等待 WB 返回商品编号',pending_price:'待设置价格和折扣',price_processing:'价格处理中',pending_stock:'待添加库存',stock_processing:'库存处理中',completed:'全部完成',failed:'建品失败',price_failed:'价格设置失败',stock_failed:'库存设置失败'} as Record<string,string>)[value]||value}
onMounted(async()=>{await load();try{const result=await api.get<{items:any[]}>('/stores');stores.value=result.items.filter(row=>['wb','wildberries'].includes(String(row.platform).toLowerCase()))}catch(error){ElMessage.error(String((error as Error).message||error))}});watch(storeId,()=>{page.value=1;items.value=[];selected.value=[];load()});watch(()=>props.mode,()=>{page.value=1;selected.value=[];load()})
</script>
<template><div class="page-stack"><CatalogWbTabs/><section class="panel"><div class="panel-head"><div><h2>{{title}}</h2><p>{{description}}</p></div><div><el-select v-if="mode==='pending_stock'||mode==='pending_price'" v-model="storeId" clearable placeholder="全部店铺" style="width:180px" :disabled="submitting"><el-option v-for="store in stores" :key="store.id" :label="store.name" :value="store.id"/></el-select><WbShopSyncPanel v-if="mode==='pending_stock'||mode==='pending_price'" :store-id="storeId" @updated="load"/><el-input v-model="search" clearable placeholder="搜索商品、SKU、货号或 WB 编号" style="width:280px" @keyup.enter="searchRows" @clear="searchRows"/><el-button @click="searchRows">搜索</el-button><el-tag>{{total}} 个 SKU</el-tag><el-button @click="load">刷新</el-button><el-button v-if="['processing','exceptions'].includes(mode)" type="danger" plain :disabled="!selected.length||!!removing" @click="removeSelected">移除所选 {{selected.length}} 项</el-button></div></div><div v-if="mode==='pending_stock'||mode==='pending_price'" class="batch-bar"><template v-if="mode==='pending_stock'"><el-select v-model="form.warehouse_id" :loading="warehouseLoading" :disabled="!warehouseStoreId" placeholder="先勾选商品，再选择仓库"><el-option v-for="row in warehouses" :key="row.id" :label="row.name" :value="row.id"/></el-select><el-button :loading="warehouseLoading" :disabled="!warehouseStoreId||submitting" @click="loadWarehouses()">加载仓库</el-button><span>每个 SKU 库存</span><el-input-number v-model="form.amount" :min="0"/></template><template v-else><span>统一价格（留空使用计划价） CNY</span><el-input-number v-model="form.price" :min="1" :precision="0" clearable/><span>折扣</span><el-input-number v-model="form.discount_percent" :min="0" :max="99"/><span>%</span></template><el-button type="primary" :loading="submitting" :disabled="!selected.length" @click="submitBatch">批量提交 {{selected.length}} 个 SKU</el-button></div><el-table v-loading="loading" :data="items" empty-text="当前队列没有商品" @selection-change="selectionChanged"><el-table-column v-if="['pending_stock','pending_price','processing','exceptions'].includes(mode)" type="selection" width="48" :selectable="selectable"/><el-table-column label="产品 / SKU" min-width="250"><template #default="{row}"><b>{{row.product_name}}</b><small class="table-sub mono">{{row.sku}} · {{row.size}}</small></template></el-table-column><el-table-column label="店铺 / 卖家 SKU" min-width="240"><template #default="{row}"><b>{{row.store_name}}</b><small class="table-sub mono">{{row.offer_id}}</small></template></el-table-column><el-table-column label="WB 商品" min-width="150"><template #default="{row}"><span class="mono">{{row.nm_id||'等待回写'}}</span></template></el-table-column><el-table-column v-if="mode==='pending_price'" label="计划价格(CNY) / 折扣" min-width="180"><template #default="{row}">{{row.price??'-'}} / {{row.discount_percent}}%</template></el-table-column><el-table-column label="流程状态" min-width="190"><template #default="{row}"><el-tag :type="row.status==='completed'?'success':row.status.includes('failed')?'danger':'warning'">{{statusText(row.status)}}</el-tag><small v-if="mode==='pending_price'&&row.price_submit_blocked_reason" class="table-sub">{{row.price_submit_blocked_reason}}</small><small v-if="row.last_error" class="table-sub error-text">{{row.last_error}}</small></template></el-table-column><el-table-column v-if="mode==='processing'||mode==='exceptions'||mode==='pending_price'" label="操作" min-width="190"><template #default="{row}"><el-button v-if="(mode==='exceptions'&&row.status==='price_failed')||(mode==='pending_price'&&row.status==='pending_price'&&row.last_error?.startsWith('WB 上传编号 '))" type="primary" link :disabled="submitting||!row.price" @click="reconcilePrice(row)">只读核对价格</el-button><el-button v-if="mode==='exceptions'&&row.status==='price_failed'&&row.last_error?.includes('WB 实际价格或折扣与计划不一致；本次未写入')" type="danger" link :disabled="submitting||!row.price" @click="resubmitVerifiedPrice(row)">人工提交一次</el-button><el-button type="danger" link :loading="removing===row.id" :disabled="!row.can_remove" @click="removeRow(row)">移除记录</el-button><small v-if="!row.can_remove" class="table-sub">{{row.remove_blocked_reason}}</small></template></el-table-column><el-table-column label="任务" width="110"><template #default="{row}"><RouterLink v-if="row.operation_job_id||row.job_id" :to="`/jobs?job=${row.operation_job_id||row.job_id}`">查看任务</RouterLink></template></el-table-column></el-table><div v-if="total" class="pager"><el-pagination :current-page="page" :page-size="pageSize" :page-sizes="[30,60,90,200]" layout="total, sizes, prev, pager, next, jumper" :total="total" @update:current-page="changePage" @update:page-size="changePageSize"/></div></section></div></template>

<style scoped>.batch-bar{display:flex;align-items:center;gap:12px;padding:14px 18px;background:#f7f9fc;border-bottom:1px solid #e5eaf2}.batch-bar .el-select{width:280px}.table-sub{display:block;margin-top:4px;color:#718096}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.error-text{color:#d93025}.pager{display:flex;justify-content:flex-end;padding:18px}</style>
