<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../../api'

type Row = Record<string, any>
const rows = ref<Row[]>([]), stores = ref<Row[]>([]), counts = ref<Row>({}), selected = ref<Row[]>([]), page=ref(1),total=ref(0)
const orderLoading = ref(false)
const orderTable = ref<{ clearSelection: () => void } | null>(null)
let loadedQuery = ''
let latestLoad = 0
const search = ref(''), platform = ref(''), storeId = ref(''), matchStatus = ref(''), submissionStatus = ref(''), workflowStatus = ref(''), trackingStatus=ref(''), days = ref(3)
const syncVisible = ref(false), syncMode = ref('time'), syncPreset = ref<number | string>(3)
const syncRange = ref<[Date, Date]>([new Date(Date.now()-3*86400000), new Date()])
const syncNumbers = ref(''), syncWithImages = ref(false)
function openSync() {
  if (!storeId.value) { ElMessage.warning('请先选择店铺'); return }
  syncVisible.value = true
}
function syncPlatform() { return String(stores.value.find(s => s.id === storeId.value)?.platform || '').toLowerCase() }
const busy = ref(false), detail = ref<Row | null>(null), drawer = ref(false), options = ref<Row[]>([])
const batch = ref<Row | null>(null), batchVisible = ref(false), groups = ref<Row[]>([]), historyVisible = ref(false)
const manualTrackingVisible = ref(false), manualTrackingNumber = ref(''), manualTrackingCompany = ref('')
const mergeMode = ref('smart'), purchaseDate = ref(new Date().toLocaleDateString('sv-SE'))
const importVisible = ref(false), importText = ref(''), jobId = ref('')
const manualVisible = ref(false), manualStoreId = ref(''), manualOrderNumber = ref('')
const manualItems = ref<{sku: string; quantity: number}[]>([{sku:'', quantity:1}])
const manualOptions = ref<Row[]>([]), manualProductLoading = ref(false)
let manualSearchVersion = 0
async function searchManualProducts(value: string) {
  const version = ++manualSearchVersion
  manualProductLoading.value = true
  try {
    const result = await api.get<Row[]>(`/orders/pod/product-options?search=${encodeURIComponent(value)}`)
    if (version === manualSearchVersion) {
      const retained = manualOptions.value.filter(option => manualItems.value.some(item => item.sku === option.sku))
      manualOptions.value = [...new Map([...retained, ...result].map(option => [option.sku, option])).values()]
    }
  } catch (error) { if (version === manualSearchVersion) ElMessage.error(String(error)) }
  finally { if (version === manualSearchVersion) manualProductLoading.value = false }
}
function openManualOrder() {
  manualStoreId.value = storeId.value
  manualOrderNumber.value = ''
  manualItems.value = [{sku:'', quantity:1}]
  manualOptions.value = []
  manualVisible.value = true
  void searchManualProducts('')
}
async function saveManualOrder() { await run(async () => {
  if (!manualStoreId.value) throw new Error('请选择店铺')
  const number = manualOrderNumber.value.trim()
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/.test(number)) throw new Error('订单号须为1–100位字母、数字、短横线、下划线或点，并以字母或数字开头')
  if (manualItems.value.some(item => !item.sku || !Number.isInteger(item.quantity) || item.quantity < 1 || item.quantity > 10000)) throw new Error('请选择商品规格并填写1–10000的整数数量')
  if (new Set(manualItems.value.map(item => item.sku)).size !== manualItems.value.length) throw new Error('同一规格请合并数量，不要重复添加')
  await api.post<Row>('/orders/pod/manual', {store_id:manualStoreId.value, order_number:number, items:manualItems.value})
  manualVisible.value = false
  storeId.value = manualStoreId.value; platform.value = ''; search.value = ''
  workflowStatus.value = 'pending_purchase'; matchStatus.value = ''; submissionStatus.value = ''; trackingStatus.value = ''; page.value = 1
  await load()
  ElMessage.success('手工订单已保存至待创建采购列表')
}) }
const purchaseAddress = ref<Row>({ country:'CN', region:'', city:'', area:'', street:'', postcode:'', firstname:'', lastname:'', mobile_phone:'', telephone:'', email:'' })
const addresses=ref<Row[]>([]),savedAddressId=ref(''),addressName=ref(''),saveAsDefault=ref(false)
const selectedPurchases = ref<Row[]>([]), batchSubmitting = ref(false)
const purchaseSubmitKeys = new Map<string, string>()
function purchaseSelectable(row: Row) { return row.submission_status === 'queued' }
async function submitSelectedPurchases() {
  if (batchSubmitting.value || busy.value) return
  const targets = selectedPurchases.value.filter(purchaseSelectable).map(row => ({ ...row }))
  if (!targets.length) return
  batchSubmitting.value = true
  try {
    try {
      await ElMessageBox.confirm(`将真实提交 ${targets.length} 笔采购，可能产生费用：\n${targets.map(row => `${String(row.supplier).toUpperCase()} · ${row.sku} × ${row.quantity}`).join('\n')}`, '批量确认真实采购', { confirmButtonText: '确认提交这批采购', type: 'warning' })
    } catch { return }
    await run(async () => {
      let submitted = 0
      try {
        for (const group of targets) {
          const requestKey = purchaseSubmitKeys.get(group.id) || crypto.randomUUID()
          purchaseSubmitKeys.set(group.id, requestKey)
          const result = await api.post<Row>('/orders/pod/purchase/submit', { target_type: 'group', target_id: group.id, confirmation: 'CONFIRM_REAL_PURCHASE', idempotency_key: requestKey })
          jobId.value = result.job_id
          submitted++
        }
        ElMessage.success(`已创建 ${submitted} 个真实采购任务，请在任务中心查看`)
      } catch (error) {
        throw new Error(`已创建 ${submitted} 个任务，批量提交已停止。请先刷新采购记录核对结果。${error instanceof Error ? error.message : String(error)}`)
      } finally {
        selectedPurchases.value = []
        groups.value = await api.get<Row[]>('/orders/pod/batch/groups')
      }
    })
  } finally { batchSubmitting.value = false }
}
const logistics = ref<Row[]>([])
const labels: Row = { not_submitted: '尚未创建采购', queued: '采购任务待执行', preparing_product: '正在准备供应商产品', submitting: '正在提交供应商', checking: '正在核对供应商订单', submitted: '供应商订单已创建', failed: '采购提交失败', unknown: '提交结果待核实', matched: '已匹配', unmatched: '未匹配', not_found: '找不到 SKU', ambiguous: 'SKU 不唯一', pending_purchase: '待创建采购', supplier_processing: '供应商处理中', waiting_official_tracking: '等待正式圆通单号', purchased_pending_tms: '正式单号已就绪，待人工交接', purchased_in_transit: '供应商已发货', completed: 'ERP处理已完成', tms_error: '采购后处理异常' }
labels.pending_match = '待匹配 SKU'
labels.cancelled = '平台已取消，禁止采购'
labels.cancelled_purchased = '平台已取消，供应商采购需人工核对'
labels.manual_tracking = '人工录入物流，待处理（未创建供应商采购）'
const platformStatusLabels:Row={awaiting_packaging:'平台待配货',awaiting_deliver:'平台待发货',awaiting_delivery:'平台待发货',delivering:'平台配送中',delivered:'平台已签收',cancelled:'平台已取消',canceled:'平台已取消',arbitration:'平台争议处理中'}
Object.assign(platformStatusLabels,{declined_by_client:'买家已取消',canceled_by_client:'买家已取消',cancelled_by_client:'买家已取消',canceled_by_carrier:'物流取消',defect:'平台取消'})
platformStatusLabels.manual = '手工录入'
const recipientFields: [string, string][] = [['country','国家'],['region','省/州'],['city','城市'],['area','区'],['street','详细地址'],['postcode','邮编'],['firstname','名字'],['lastname','姓'],['mobile_phone','手机'],['telephone','电话'],['email','邮箱']]
const key = (row: Row) => encodeURIComponent(row.order_key)
async function run(action: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  try { await action() } catch (e) { ElMessage.error(e instanceof Error ? e.message : '操作失败') } finally { busy.value = false }
}
async function load() {
  const clean = (value: unknown) => typeof value === 'string' && !['undefined','null'].includes(value) ? value : ''
  const query = new URLSearchParams({ search: clean(search.value), platform: clean(platform.value), store_id: clean(storeId.value), match_status: clean(matchStatus.value), submission_status: clean(submissionStatus.value), workflow_status: clean(workflowStatus.value), tracking_status: clean(trackingStatus.value), page:String(page.value),page_size:'100' })
  const request = ++latestLoad
  orderLoading.value = true
  try {
    const data = await api.get<Row>(`/orders/pod?${query}`)
    if (request !== latestLoad) return
    if (loadedQuery && loadedQuery !== query.toString()) {
      orderTable.value?.clearSelection()
      selected.value = []
    }
    loadedQuery = query.toString()
    rows.value = data.orders;total.value=data.total??data.orders.length; counts.value = data.counts
  } catch (error) {
    if (request === latestLoad) ElMessage.error(error instanceof Error ? error.message : '订单加载失败')
  } finally {
    if (request === latestLoad) orderLoading.value = false
  }
}
function workflow(value:string){workflowStatus.value=value;matchStatus.value='';submissionStatus.value='';trackingStatus.value='';page.value=1;void load()}
function changeFilter(){storeId.value=storeId.value||'';platform.value=platform.value||'';page.value=1;void load()}
function amount(row:Row){return row.items.reduce((sum:number,item:Row)=>sum+Number(item.price||0)*Number(item.quantity||0),0).toFixed(2)}
function itemCount(row:Row){return row.items.reduce((sum:number,item:Row)=>sum+Number(item.quantity||0),0)}
function dateTime(value:string){return value?new Date(value).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—'}
function imageSource(item:Row){return item.platform_image_source==='platform'?'平台同步主图':item.platform_image_source==='listing'?'店铺上架主图':item.image_url?'产品库效果图':'暂无图片'}
async function open(row: Row) { await run(async () => { detail.value = await api.get<Row>(`/orders/pod/${key(row)}`); drawer.value = true }) }
async function sync() { await run(async () => {
  if (!storeId.value) throw new Error('请先选择店铺')
  const numbers = [...new Set(syncNumbers.value.split(/[\s,，;；]+/).filter(Boolean))]
  if (syncMode.value === 'numbers' && (!numbers.length || numbers.length > 100)) throw new Error('请填写1至100个平台订单号')
  if (syncMode.value === 'numbers' && syncPlatform() !== 'ozon' && numbers.some(n => !/^[0-9]+$/.test(n) || Number(n) <= 0)) throw new Error('WB 请填写数字组装任务 ID')
  const customRange = syncPreset.value === 'custom' && !(syncMode.value === 'numbers' && syncPlatform() === 'ozon')
  const end = customRange ? syncRange.value?.[1] : new Date()
  const start = customRange ? syncRange.value?.[0] : new Date(end.getTime()-(Number(syncPreset.value) || 3)*86400000)
  const span = end?.getTime()-start?.getTime()
  if (!start || !end || !Number.isFinite(span) || span <= 0 || span > (syncPlatform() === 'ozon' ? 90 : 30)*86400000 || end.getTime()>Date.now()+60000) throw new Error('请填写有效时间范围，OZON 最多90天，WB 最多30天，结束时间不能晚于现在')
  const result = await api.post<Row>('/orders/pod/sync', { store_id: storeId.value, days: Math.max(1,Math.ceil(span/86400000)), scope_version:1,
    sync_mode:syncMode.value, date_from:start.toISOString(), date_to:end.toISOString(),
    order_numbers:syncMode.value === 'numbers' ? numbers : [], sync_images:syncWithImages.value })
  syncVisible.value = false
  jobId.value = result.job_id; ElMessage.success('同步任务已创建，完成后请刷新订单')
}) }
async function syncImages() { await run(async () => {
  if (!storeId.value) throw new Error('请先选择店铺')
  const result = await api.post<Row>('/orders/pod/images/sync', { store_id: storeId.value })
  jobId.value = result.job_id; ElMessage.success('平台商品图片同步任务已创建，请在任务中心查看')
}) }
async function searchProducts(value: string) {
  try { options.value = await api.get<Row[]>(`/orders/pod/product-options?search=${encodeURIComponent(value)}`) }
  catch (e) { ElMessage.error(String(e)) }
}
async function match(line: Row, sku: string) { await run(async () => {
  if (!detail.value) return
  detail.value = await api.post<Row>(`/orders/pod/${key(detail.value)}/items/${line.line_no}/match`, { sku }); await load()
}) }
function selection() {
  if (!selected.value.length) throw new Error('请先勾选订单')
  return { order_keys: selected.value.map(row => row.order_key), merge_mode: mergeMode.value }
}
async function preview() { await run(async () => { const value=await api.post<Row>('/orders/pod/batch/preview', selection()); batch.value=value; logistics.value=value.groups.map((row:Row)=>({group_key:row.group_key,sku:row.sku,company_name:row.supplier==='s2b'?'圆通':'圆通速递',shipping_amount:row.supplier==='s2b'?5:0,logistics_id:row.supplier==='s2b'?109:0})); batchVisible.value = true }) }
async function queuePurchase() { await run(async () => {
  if (!batch.value) return
  const result = await api.post<Row>('/orders/pod/batch/queue', { ...selection(), address: purchaseAddress.value, logistics: logistics.value })
  batchVisible.value = false; groups.value = result.groups; historyVisible.value = true; orderTable.value?.clearSelection(); selected.value = []; await load(); ElMessage.success('采购任务已建立，尚未提交供应商')
}) }
async function loadAddresses(){const result=await api.get<Row>('/orders/pod/addresses');addresses.value=result.items||[];const preferred=addresses.value.find(x=>x.is_default);if(preferred&&!savedAddressId.value)useAddress(preferred.id)}
function useAddress(id:string){savedAddressId.value=id||'';const item=addresses.value.find(x=>x.id===id);if(item){purchaseAddress.value={...item.address};addressName.value=item.name||'';saveAsDefault.value=!!item.is_default}}
async function saveAddress(){await run(async()=>{if(!addressName.value.trim())throw new Error('请输入地址名称');const result=await api.post<Row>('/orders/pod/addresses',{id:savedAddressId.value||'',name:addressName.value.trim(),address:purchaseAddress.value,is_default:saveAsDefault.value});addresses.value=result.items||[];const saved=addresses.value.find(x=>x.id===savedAddressId.value)||addresses.value.find(x=>x.name===addressName.value.trim());if(saved){savedAddressId.value=saved.id;addressName.value=saved.name;saveAsDefault.value=!!saved.is_default}ElMessage.success('常用地址已加密保存')})}
async function deleteAddress(){if(!savedAddressId.value)return;await ElMessageBox.confirm('删除这个常用地址？不会影响已经建立的采购记录。','删除地址');const result=await api.delete<Row>(`/orders/pod/addresses/${savedAddressId.value}`);addresses.value=result.items;savedAddressId.value='';ElMessage.success('常用地址已删除')}
async function submitPurchase(group: Row) {
  try { await ElMessageBox.confirm(`将真实提交 ${group.supplier.toUpperCase()} 采购：${group.sku} × ${group.quantity}。提交后可能产生费用。`, '确认真实采购', { confirmButtonText:'确认提交采购', type:'warning' }) } catch { return }
  await run(async () => { const result = await api.post<Row>('/orders/pod/purchase/submit', { target_type:'group', target_id:group.id, confirmation:'CONFIRM_REAL_PURCHASE', idempotency_key:crypto.randomUUID() }); jobId.value=result.job_id; ElMessage.success('真实采购任务已提交，请在任务中心查看'); await history() })
}
async function resubmitOrder(order: Row) {
  await run(async () => {
    const purchaseGroups = await api.get<Row[]>(`/orders/pod/${key(order)}/purchase-groups`)
    const recoverable = purchaseGroups.filter(group => group.recovery_job || group.submission_status === 'failed')
    if (!recoverable.length) throw new Error('没有可继续的失败采购任务；若任务仍在执行，请稍后刷新')
    try { await ElMessageBox.confirm(`将继续订单 ${order.posting_number} 的 ${recoverable.length} 个失败采购任务。已有原任务时只恢复原任务，并先核对供应商订单；不会另建重复任务。`, '继续采购任务', { confirmButtonText:'确认继续', type:'warning' }) } catch { return }
    for (const group of recoverable) {
      const result = group.recovery_job
        ? await api.post<Row>(`/jobs/${group.recovery_job.id}/${group.recovery_job.status === 'paused' ? 'resume' : 'retry'}`, {})
        : await api.post<Row>('/orders/pod/purchase/submit', { target_type:'group', target_id:group.id, confirmation:'CONFIRM_REAL_PURCHASE', idempotency_key:crypto.randomUUID() })
      if (!jobId.value) jobId.value = result.job_id || result.id
    }
    ElMessage.success(`已继续 ${recoverable.length} 个原采购任务，请在任务中心查看`); await load()
  })
}
async function syncPurchases(supplier='') { await run(async()=>{ const result=await api.post<Row>(`/orders/pod/purchase/sync?supplier=${supplier}`); jobId.value=result.job_id; ElMessage.success(`${supplier?supplier.toUpperCase():'全部供应商'}采购状态同步任务已创建`) }) }
async function rematchCurrent(){if(!detail.value)return;await run(async()=>{detail.value=await api.post<Row>(`/orders/pod/${key(detail.value!)}/rematch`,{});await load();ElMessage.success('当前订单已重新匹配')})}
async function singlePurchase(){if(!detail.value)return;selected.value=[detail.value];drawer.value=false;await preview()}
function openManualTracking() {
  if (selected.value.length !== 1) { ElMessage.warning('请只勾选一张订单录入物流单号'); return }
  const row = selected.value[0]
  if (row.workflow_status !== 'pending_purchase' && row.workflow_status !== 'manual_tracking') { ElMessage.warning('仅待创建采购或已人工录入物流的订单可操作'); return }
  manualTrackingNumber.value = row.manual_tracking_number || ''
  manualTrackingCompany.value = row.manual_tracking_company || ''
  manualTrackingVisible.value = true
}
async function saveManualTracking() { await run(async () => {
  if (selected.value.length !== 1) throw new Error('请只勾选一张订单')
  if (!manualTrackingNumber.value.trim() || !manualTrackingCompany.value.trim()) throw new Error('请填写物流公司和物流单号')
  await api.post(`/orders/pod/${key(selected.value[0])}/manual-tracking`, { tracking_number: manualTrackingNumber.value.trim(), tracking_company: manualTrackingCompany.value.trim() })
  manualTrackingVisible.value = false; orderTable.value?.clearSelection(); selected.value = []; await load()
  ElMessage.success('物流单号已保存；订单已进入采购后处理中，未创建供应商采购')
}) }
async function history() { await run(async () => { groups.value = await api.get<Row[]>('/orders/pod/batch/groups'); historyVisible.value = true }) }
async function complete() {
  try { await ElMessageBox.confirm('将所选已采购订单标记为处理完成？这不会向平台或供应商发送请求。', '确认完成') }
  catch { return }
  await run(async () => { await api.post('/orders/pod/manual-complete', selection()); orderTable.value?.clearSelection(); selected.value = []; await load(); ElMessage.success('已标记完成') })
}
async function download(path: string, filename: string, body?: unknown) { await run(async () => {
  const response = await fetch(`/api/v1/orders/pod/${path}`, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {})
  if (!response.ok) { const value = await response.json(); throw new Error(value.message || '导出失败') }
  const url = URL.createObjectURL(await response.blob()); const link = document.createElement('a')
  link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}) }
async function tracking() {
  try { await download('tracking-export', 'POD物流单号.xlsx', selection()) } catch (e) { ElMessage.error(String(e)) }
}
async function importOrders() { await run(async () => {
  if (!storeId.value) throw new Error('请先选择导入订单所属的店铺')
  const value = JSON.parse(importText.value)
  await api.post('/orders/pod/import', { store_id: storeId.value, postings: Array.isArray(value) ? value : value.postings || value.result?.postings })
  importVisible.value = false; importText.value = ''; await load(); ElMessage.success('订单已导入')
}) }
let refreshTimer:number|undefined
onMounted(() => run(async () => {
  const value = await api.get<any>('/stores'); stores.value = (Array.isArray(value) ? value : value.items || []).filter((row: Row) => ['ozon','wb','wildberries'].includes(row.platform.toLowerCase()) && row.enabled)
  await Promise.all([load(),loadAddresses()]);refreshTimer=window.setInterval(()=>{if(jobId.value&&!busy.value)void load()},8000)
}))
onUnmounted(()=>{if(refreshTimer)window.clearInterval(refreshTimer)})
</script>

<template>
  <section class="pod-orders" v-loading="busy || orderLoading">
    <div style="margin-bottom:12px"><el-button type="primary" :disabled="busy" @click="openManualOrder">创建手工订单</el-button></div>
    <el-dialog v-model="manualVisible" title="创建手工订单" width="min(760px, 95vw)" :close-on-click-modal="false" :close-on-press-escape="!busy" :show-close="!busy">
      <p class="hint">选择店铺和产品库规格，填写订单号及数量。无需收件信息，保存后进入待创建采购列表。</p>
      <el-form label-position="top" :disabled="busy">
        <el-form-item label="店铺" required><el-select v-model="manualStoreId" filterable placeholder="选择 OZON / WB 店铺" style="width:100%"><el-option v-for="store in stores" :key="store.id" :value="store.id" :label="`${store.platform.toUpperCase()} · ${store.name}`" /></el-select></el-form-item>
        <el-form-item label="订单号" required><el-input v-model="manualOrderNumber" maxlength="100" placeholder="填写真实订单号，同一店铺不可重复" /></el-form-item>
        <div v-for="(item,index) in manualItems" :key="index" style="display:flex;gap:12px;align-items:center">
          <el-form-item :label="`商品 ${index+1}`" required style="flex:1;min-width:0"><el-select v-model="item.sku" filterable remote :remote-method="searchManualProducts" :loading="manualProductLoading" placeholder="搜索产品名或 SKU，选择规格" style="width:100%"><el-option v-for="option in manualOptions" :key="option.sku" :value="option.sku" :label="`${option.sku} · ${option.name} · ${option.color_name} ${option.size_name}`" /></el-select></el-form-item>
          <el-form-item label="数量" required><el-input-number v-model="item.quantity" :min="1" :max="10000" :precision="0" :step="1" style="width:130px" /></el-form-item>
          <el-button :disabled="manualItems.length===1" @click="manualItems.splice(index,1)">移除</el-button>
        </div>
        <el-button :disabled="manualItems.length>=100" @click="manualItems.push({sku:'',quantity:1})">添加商品</el-button>
      </el-form>
      <template #footer><el-button :disabled="busy" @click="manualVisible=false">取消</el-button><el-button type="primary" :loading="busy" @click="saveManualOrder">保存到待采购</el-button></template>
    </el-dialog>
    <el-alert v-if="workflowStatus==='post_purchase_processing'" title="采购后人工处理" type="info" :closable="false" description="这里汇总已创建供应商采购，以及未创建供应商采购但已人工录入物流单号的订单。核对正式物流单号后可导出；完成后再手动移入处理完成，不会自动向平台提交发货。" />
    <el-alert v-if="workflowStatus==='cancelled'" title="平台取消订单" type="warning" :closable="false" description="取消订单不再进入正常采购或采购后处理。已提交的供应商采购须人工核对付款和物流；系统不会自动取消或重新提交。" />
    <div class="stats">
      <button :class="{active:!workflowStatus}" @click="workflow('')"><span>全部订单</span><strong>{{counts.total||0}}</strong></button>
      <button :class="{active:workflowStatus==='pending_purchase'}" @click="workflow('pending_purchase')"><span>待创建采购</span><strong>{{counts.pending_purchase||0}}</strong></button>
      <button :class="{active:workflowStatus==='pending_match'}" @click="workflow('pending_match')"><span>待匹配 SKU</span><strong>{{counts.pending_match||0}}</strong></button>
      <button :class="{active:workflowStatus==='post_purchase_processing'}" @click="workflow('post_purchase_processing')"><span>采购后处理中</span><strong>{{counts.post_purchase_processing||0}}</strong></button>
      <button :class="{active:workflowStatus==='cancelled'}" @click="workflow('cancelled')"><span>平台已取消</span><strong>{{counts.cancelled||0}}</strong><small v-if="counts.cancelled_purchased">其中已采购 {{counts.cancelled_purchased}}</small></button>
      <button :class="{active:workflowStatus==='completed'}" @click="workflow('completed')"><span>ERP处理完成</span><strong>{{counts.completed||0}}</strong></button>
      <button :class="{active:workflowStatus==='tms_error'}" @click="workflow('tms_error')"><span>异常待处理</span><strong>{{counts.failed||0}}</strong></button>
    </div>
    <div class="tools">
      <el-select v-model="platform" clearable placeholder="全部平台" style="width:140px" @change="storeId='';changeFilter()"><el-option label="OZON" value="ozon"/><el-option label="Wildberries" value="wb"/></el-select><el-select v-model="storeId" clearable placeholder="全部店铺（同步前需选择）" style="width:260px" @change="changeFilter"><el-option v-for="store in stores.filter(s=>!platform||s.platform.toLowerCase().startsWith(platform))" :key="store.id" :value="store.id" :label="`${store.name} · ${store.platform.toUpperCase()}`" /></el-select>
      <el-input v-model="search" placeholder="订单号 / SKU / 商品名称 / 物流单号" clearable style="width:300px" @keyup.enter="changeFilter" />
      <el-select v-if="!workflowStatus" v-model="matchStatus" clearable placeholder="匹配状态" style="width:140px" @change="changeFilter"><el-option label="已匹配" value="matched" /><el-option label="未匹配" value="unmatched" /></el-select>
      <el-select v-if="!workflowStatus||workflowStatus==='tms_error'" v-model="submissionStatus" clearable placeholder="采购状态" style="width:160px" @change="changeFilter"><el-option v-for="value in ['not_submitted','queued','preparing_product','checking','submitting','submitted','failed','unknown']" :key="value" :label="labels[value]" :value="value" /></el-select>
      <el-select v-if="workflowStatus==='post_purchase_processing'" v-model="trackingStatus" clearable placeholder="正式物流单号" style="width:180px" @change="changeFilter"><el-option label="已取得正式物流单号" value="ready"/><el-option label="等待正式物流单号" value="waiting"/></el-select>
      <el-button @click="changeFilter">查询 / 刷新</el-button><el-button @click="syncImages">同步已有订单的平台图片</el-button>
    </div>
    <div v-if="!workflowStatus||workflowStatus==='pending_match'" class="tools action-bar">
      <el-button type="primary" @click="openSync">同步所选店铺订单</el-button>
      <el-button @click="importVisible = true">导入订单 JSON</el-button><el-button v-if="workflowStatus==='pending_match'" @click="run(async () => { await api.post('/orders/pod/rematch'); await load() })">重新匹配全部待匹配订单</el-button>
      <RouterLink v-if="jobId" to="/jobs">查看同步任务</RouterLink>
    </div>
    <div v-if="workflowStatus==='pending_purchase'" class="tools action-bar">
      <span>已选 {{ selected.length }} 单</span><el-select v-model="mergeMode" style="width:160px"><el-option label="智能合并" value="smart" /><el-option label="相同 SKU 合并" value="same_sku" /><el-option label="逐单分开" value="separate" /></el-select>
      <el-button type="primary" @click="preview">采购合并预览</el-button><el-button @click="openManualTracking">人工填写物流单号</el-button><el-button @click="history">采购记录</el-button>
      <el-date-picker v-model="purchaseDate" type="date" value-format="YYYY-MM-DD" placeholder="采购日期" style="width:160px" /><el-button @click="download(`purchase-export?purchase_date=${purchaseDate}`, 'OZON采购明细.xlsx')">导出 OZON 采购</el-button>
    </div>
    <div v-if="workflowStatus==='post_purchase_processing'" class="tools action-bar">
      <span>已选 {{ selected.length }} 单</span><el-button @click="syncPurchases('hicustom')">同步 HICUSTOM 物流</el-button><el-button @click="syncPurchases('s2b')">同步 S2B 物流</el-button><el-button @click="openManualTracking">修改人工物流单号</el-button><el-button @click="history">采购记录</el-button><el-button type="primary" plain @click="tracking">导出物流单号</el-button><el-button type="success" plain @click="complete">手动移入处理完成</el-button><RouterLink v-if="jobId" to="/jobs">查看同步任务</RouterLink>
    </div>
    <div v-if="workflowStatus==='tms_error'" class="tools action-bar">
      <el-button @click="syncPurchases('hicustom')">核对 HICUSTOM 采购</el-button><el-button @click="syncPurchases('s2b')">核对 S2B 采购</el-button><el-button @click="history">采购记录与异常恢复</el-button><RouterLink v-if="jobId" to="/jobs">查看同步任务</RouterLink>
    </div>
    <el-table ref="orderTable" :data="rows" row-key="order_key" @selection-change="selected = $event" stripe max-height="650">
      <el-table-column v-if="workflowStatus==='pending_purchase'||workflowStatus==='post_purchase_processing'" type="selection" :reserve-selection="true" width="45" /><el-table-column label="平台 / 店铺" min-width="125"><template #default="{row}"><b>{{row.platform.toUpperCase()}}</b><small>{{row.store_name}}</small></template></el-table-column><el-table-column prop="posting_number" label="订单号" min-width="190" />
      <el-table-column label="商品 / SKU" min-width="300"><template #default="{ row }"><div v-for="item in row.items" :key="item.line_no" class="order-product"><el-image v-if="item.platform_image_url || item.image_url" :src="item.platform_image_url || item.image_url" fit="cover" :preview-src-list="[item.platform_image_url || item.image_url]"/><div><b>{{ item.product_name }}</b><small>{{ item.offer_id }} × {{ item.quantity }} · {{ item.matched_sku || '未匹配' }}</small><small>{{ imageSource(item) }}</small></div></div></template></el-table-column>
      <el-table-column label="匹配" width="95"><template #default="{ row }"><el-tag :type="row.match_status === 'matched' ? 'success' : 'warning'">{{ labels[row.match_status] || row.match_status }}</el-tag></template></el-table-column>
      <el-table-column label="平台状态 / ERP进度" min-width="200"><template #default="{ row }"><b>{{platformStatusLabels[row.substatus]||platformStatusLabels[row.order_status]||`平台状态：${row.order_status||'未知'}`}}</b><small>ERP：{{labels[row.workflow_status]||row.workflow_status}}</small><small v-if="row.submission_error||row.tms_error" class="error-text">{{row.submission_error||row.tms_error}}</small></template></el-table-column><el-table-column label="金额（人民币）" width="125"><template #default="{row}">¥ {{amount(row)}}<small>{{itemCount(row)}} 件 · 不换算</small></template></el-table-column><el-table-column label="接单 / 最晚发货" min-width="155"><template #default="{row}">{{dateTime(row.in_process_at)}}<small>{{dateTime(row.last_ship_at)}}</small></template></el-table-column><el-table-column label="仓库 / 配送" min-width="145"><template #default="{row}">{{row.delivery_method||row.fulfillment_mode||'—'}}</template></el-table-column><el-table-column label="操作" width="175" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="open(row)">详情</el-button><el-button v-if="['failed','preparing_product','submitting','checking'].includes(row.submission_status)&&row.workflow_status!=='cancelled'" link type="danger" @click="resubmitOrder(row)">检查并继续采购</el-button></template></el-table-column>
    </el-table>
    <el-pagination v-model:current-page="page" :page-size="100" :total="total" layout="prev, pager, next, total" @current-change="load"/><p class="hint">每页100单；汇总数按当前平台和店铺统计。</p>
    <el-dialog v-model="manualTrackingVisible" title="人工填写物流单号" width="480px">
      <el-alert title="此操作只记录物流信息并转入采购后处理中，不会创建供应商采购，也不会向平台提交发货。" type="warning" :closable="false" />
      <el-form label-position="top" style="margin-top:16px"><el-form-item label="物流公司"><el-input v-model="manualTrackingCompany" maxlength="100" placeholder="例如：圆通速递" /></el-form-item><el-form-item label="物流单号"><el-input v-model="manualTrackingNumber" maxlength="100" placeholder="填写正式物流单号" /></el-form-item></el-form>
      <template #footer><el-button @click="manualTrackingVisible=false">取消</el-button><el-button type="primary" @click="saveManualTracking">保存并转入采购后处理</el-button></template>
    </el-dialog>
    <el-drawer v-model="drawer" title="POD 订单详情" size="min(920px, 95vw)">
      <template v-if="detail">
        <h3>{{ detail.posting_number }} · {{ detail.store_name }}</h3><el-alert v-if="detail.workflow_status==='cancelled_purchased'" title="买家已取消，但供应商采购已提交。请人工核对供应商订单、付款和物流，勿按正常订单处理。" type="warning" :closable="false" /><p>{{ detail.submission_error || detail.tms_error }}</p><div class="detail-actions"><el-button @click="rematchCurrent">重新匹配当前订单</el-button><el-button v-if="detail.match_status==='matched'&&detail.submission_status==='not_submitted'&&detail.workflow_status!=='cancelled'" type="primary" @click="singlePurchase">创建单笔采购</el-button><el-button v-if="['failed','preparing_product','submitting','checking'].includes(detail.submission_status)&&detail.workflow_status!=='cancelled'" type="danger" @click="resubmitOrder(detail)">检查并继续采购</el-button></div>
        <el-table :data="detail.items"><el-table-column label="图片" width="100"><template #default="{row}"><el-image v-if="row.platform_image_url || row.image_url" class="detail-image" :src="row.platform_image_url || row.image_url" fit="cover" :preview-src-list="[row.platform_image_url || row.image_url]"/><span v-else>—</span><small>{{ imageSource(row) }}</small></template></el-table-column><el-table-column prop="product_name" label="商品" /><el-table-column prop="offer_id" label="平台 SKU" /><el-table-column prop="quantity" label="数量" width="60" /><el-table-column label="匹配 POD SKU" min-width="220"><template #default="{ row }"><el-select :model-value="row.matched_sku || undefined" filterable remote :remote-method="searchProducts" placeholder="输入 SKU 或产品名" @change="match(row, $event)"><el-option v-for="option in options" :key="option.sku" :value="option.sku" :label="`${option.sku} · ${option.color_name} ${option.size_name}`" /></el-select><small>{{ row.match_error }}</small></template></el-table-column></el-table>
        <h3>采购与物流</h3><el-descriptions :column="2" border><el-descriptions-item label="采购状态">{{ labels[detail.submission_status] || detail.submission_status }}</el-descriptions-item><el-descriptions-item label="供应商订单">{{ detail.hicustom_order_id || '—' }}</el-descriptions-item><el-descriptions-item label="物流公司">{{ detail.tracking_company || '—' }}</el-descriptions-item><el-descriptions-item label="物流单号">{{ detail.tracking_number || '—' }}</el-descriptions-item></el-descriptions>
      </template>
    </el-drawer>
    <el-dialog v-model="batchVisible" title="采购合并预览（尚未提交）" width="950px"><template v-if="batch"><p>{{ batch.order_count }} 个订单 · {{ batch.group_count }} 组 SKU · {{ batch.total_quantity }} 件 · 预计 {{ batch.estimated_purchase_count }} 笔采购</p><el-table :data="batch.groups"><el-table-column prop="supplier" label="供应商" /><el-table-column prop="sku" label="SKU" /><el-table-column prop="quantity" label="数量" /><el-table-column prop="source_order_count" label="来源订单数" /><el-table-column label="物流名称/报价/渠道 ID" min-width="310"><template #default="{row}"><el-input v-model="logistics[batch.groups.indexOf(row)].company_name" placeholder="圆通速递" /><el-input-number v-model="logistics[batch.groups.indexOf(row)].shipping_amount" :min="0" /><el-input-number v-if="row.supplier==='s2b'" v-model="logistics[batch.groups.indexOf(row)].logistics_id" :min="1" /></template></el-table-column></el-table><h3>国内收货地址</h3><div class="saved-address"><el-select :model-value="savedAddressId" clearable placeholder="选择常用地址" @change="useAddress"><el-option v-for="a in addresses" :key="a.id" :value="a.id" :label="`${a.name}${a.is_default?'（默认）':''}`"/></el-select><el-input v-model="addressName" placeholder="地址名称，例如：义乌仓"/><el-checkbox v-model="saveAsDefault">设为默认</el-checkbox><el-button @click="saveAddress">保存当前地址</el-button><el-button :disabled="!savedAddressId" type="danger" plain @click="deleteAddress">删除</el-button></div><div class="address-grid"><el-input v-for="[field,label] in recipientFields" :key="field" v-model="purchaseAddress[field]" :placeholder="label" /></div><el-button type="primary" style="margin-top:12px" @click="queuePurchase">建立采购任务（不立即提交）</el-button></template></el-dialog>
    <el-dialog v-model="historyVisible" title="采购记录与异常恢复" width="min(1380px, 96vw)"><el-alert title="结果待核实的采购不会自动重发，请先向对应供应商同步原订单结果。" type="warning" :closable="false"/><div style="margin:12px 0"><el-button type="danger" :loading="batchSubmitting" :disabled="busy || !selectedPurchases.length" @click="submitSelectedPurchases">批量确认并提交（{{ selectedPurchases.length }}）</el-button><span style="margin-left:12px">勾选待执行采购后，一次确认提交</span></div><el-table :data="groups" max-height="650" row-key="id" @selection-change="selectedPurchases = $event"><el-table-column type="selection" width="48" :selectable="purchaseSelectable" />
      <el-table-column type="expand"><template #default="{row}"><div class="purchase-detail"><el-descriptions :column="3" border><el-descriptions-item label="商品名称">{{row.product_name||'—'}}</el-descriptions-item><el-descriptions-item label="供应商商品 ID">{{row.remote_product_id||row.stock_item_id||'—'}}</el-descriptions-item><el-descriptions-item label="生产产品编码">{{row.fulfillment_product_code||row.product_code||'—'}}</el-descriptions-item><el-descriptions-item label="颜色 / 尺码">{{row.color_name||'—'}} / {{row.size_name||'—'}}</el-descriptions-item><el-descriptions-item label="供应商订单号">{{row.hicustom_order_id||row.purchase_id||'—'}}</el-descriptions-item><el-descriptions-item label="外部幂等单号">{{row.out_order_id||'—'}}</el-descriptions-item><el-descriptions-item label="预计运费">{{row.quoted_shipping_amount??'—'}}</el-descriptions-item><el-descriptions-item label="实际货款">{{row.actual_product_amount??'—'}}</el-descriptions-item><el-descriptions-item label="实际总额">{{row.actual_grand_total??'—'}}</el-descriptions-item><el-descriptions-item label="生产素材编码" :span="3">{{row.fulfillment_gallery_codes||'—'}}</el-descriptions-item><el-descriptions-item label="创建 / 更新" :span="2">{{dateTime(row.created_at)}} / {{dateTime(row.updated_at)}}</el-descriptions-item><el-descriptions-item label="供应商核对">{{dateTime(row.remote_checked_at)}}</el-descriptions-item><el-descriptions-item label="异常原因" :span="3"><span class="error-text">{{row.submission_error||'—'}}</span></el-descriptions-item></el-descriptions><h4>来源平台订单</h4><el-table :data="row.source_orders||[]" size="small"><el-table-column prop="posting_number" label="订单号"/><el-table-column prop="platform" label="平台"/><el-table-column prop="store_name" label="店铺"/><el-table-column prop="quantity" label="数量"/></el-table></div></template></el-table-column>
      <el-table-column label="商品 / SKU" min-width="190"><template #default="{row}">{{row.product_name||'—'}}<small>{{row.sku}} × {{row.quantity}}</small></template></el-table-column><el-table-column label="货盘" width="100"><template #default="{row}">{{String(row.supplier||'').toUpperCase()}}</template></el-table-column><el-table-column label="供应商订单" min-width="155"><template #default="{row}">{{row.hicustom_order_id||row.purchase_id||'—'}}<small>{{row.out_order_id||'尚未生成'}}</small></template></el-table-column><el-table-column label="状态" width="115"><template #default="{ row }"><el-tag :type="row.submission_status==='submitted'?'success':row.submission_status==='failed'?'danger':'warning'">{{ labels[row.submission_status] || row.submission_status }}</el-tag></template></el-table-column><el-table-column label="实际总额" width="100"><template #default="{row}">{{row.actual_grand_total??'—'}}</template></el-table-column><el-table-column label="物流" min-width="140"><template #default="{row}">{{row.tracking_company||'—'}}<small>{{row.tracking_number||'暂无单号'}}</small></template></el-table-column><el-table-column label="真实操作" width="130" fixed="right"><template #default="{row}"><el-button v-if="row.submission_status==='queued'" link type="danger" :disabled="batchSubmitting || busy" @click="submitPurchase(row)">确认并提交</el-button><el-button v-else-if="row.submission_status==='failed'" link type="danger" :disabled="batchSubmitting || busy" @click="submitPurchase(row)">重新提交</el-button><el-button v-else-if="row.submission_status==='unknown'" link type="warning" @click="syncPurchases(row.supplier)">核对原订单</el-button><span v-else>—</span></template></el-table-column></el-table></el-dialog>
    <el-dialog v-model="syncVisible" title="同步订单" width="min(1000px, 94vw)" :close-on-click-modal="false">
      <el-tabs v-model="syncMode"><el-tab-pane label="按订单时间同步" name="time" /><el-tab-pane label="按平台订单号同步" name="numbers" /></el-tabs>
      <p>当前店铺：{{ stores.find(s => s.id === storeId)?.name }} · {{ syncPlatform().toUpperCase() }}</p>
      <div v-if="syncMode === 'numbers'" style="margin-bottom:20px">
        <el-input v-model="syncNumbers" type="textarea" :rows="4" placeholder="每行一个订单号，或用逗号分隔；最多100个，重复编号自动去重" />
        <p>{{ syncPlatform() === 'ozon' ? '填写 OZON 发货单号（posting_number），直接读取指定订单，不扫描历史列表。此模式不按下面时间范围限制。' : '填写 WB 数字组装任务 ID，在下面的时间范围内查找；找到全部编号后提前结束分页。' }}</p>
      </div>
      <div v-if="syncMode === 'time' || syncPlatform() !== 'ozon'">
        <span>订单创建时间：</span><el-radio-group v-model="syncPreset"><el-radio v-for="n in [1,3,7,15,30]" :key="n" :value="n">{{ n }}天</el-radio><el-radio value="custom">自定义</el-radio></el-radio-group>
        <div v-if="syncPreset === 'custom'" style="margin:20px 0"><el-date-picker v-model="syncRange" type="datetimerange" start-placeholder="开始时间" end-placeholder="结束时间" format="YYYY-MM-DD HH:mm:ss" /></div>
        <p>按平台订单创建时间筛选；时间按当前电脑时区显示。更早创建的订单不在本次范围内。</p>
      </div>
      <el-checkbox v-model="syncWithImages">同时同步商品图片（会增加请求次数）</el-checkbox>
      <template #footer><el-button @click="syncVisible=false">取消</el-button><el-button type="primary" :loading="busy" :disabled="busy" @click="sync">开始同步</el-button></template>
    </el-dialog>
    <el-dialog v-model="importVisible" title="导入所选店铺订单" width="700px"><p>粘贴平台订单数组，或包含 postings 的 JSON；按平台、店铺和订单号去重。不提交采购。</p><el-input v-model="importText" type="textarea" :rows="12" /><template #footer><el-button @click="importVisible = false">取消</el-button><el-button type="primary" @click="importOrders">校验并导入</el-button></template></el-dialog>
  </section>
</template>

<style scoped>
.pod-orders{display:grid;gap:16px;min-width:0}.stats{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:12px}.stats>button{appearance:none;text-align:left;background:white;border:1px solid #dce5ef;border-radius:12px;padding:18px;display:grid;gap:8px;cursor:pointer}.stats>button:hover,.stats>button.active{border-color:#2878ed;box-shadow:0 0 0 2px #dbeaff}.stats strong{font-size:26px}.tools,.saved-address{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.action-bar{padding:12px 14px;background:#f7f9fc;border:1px solid #e5eaf2;border-radius:10px}.saved-address{padding:10px;margin-bottom:10px;background:#f5f8fc;border-radius:8px}.saved-address .el-select,.saved-address .el-input{width:210px}.hint,small{color:#748398;font-size:12px}small{display:block;margin-top:4px}.error-text{color:#d84b4b}.pod-orders :deep(.el-table){border-radius:10px}.order-product{display:flex;align-items:center;gap:10px;margin:5px 0}.order-product .el-image,.detail-image{width:58px;height:58px;border-radius:7px;background:#f2f4f7;flex:none}.address-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.purchase-detail{padding:14px 22px;background:#f7f9fc}.purchase-detail h4{margin:16px 0 8px}pre{max-height:430px;overflow:auto;background:#f5f7fa;padding:12px}@media(max-width:1100px){.stats{grid-template-columns:repeat(4,1fr)}}@media(max-width:700px){.stats{grid-template-columns:repeat(2,1fr)}.address-grid{grid-template-columns:1fr}}
</style>
