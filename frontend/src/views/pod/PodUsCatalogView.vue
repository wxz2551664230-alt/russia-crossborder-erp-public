<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../../api'
import { useRouter } from 'vue-router'
import UsFreightPanel from './UsFreightPanel.vue'

const router=useRouter()

const data = ref<any>({items:[], categories:[], provider_status:{}})
const filter = reactive({search:'', category:'', page:1})
const loading = ref(false), submitting = ref(false), detailLoading = ref(false), detailOpen = ref(false)
const selected = ref<any>(), job = ref<any>()
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const busy = computed(()=>job.value && ['queued','running','retrying','paused'].includes(job.value.status))
const labels:Record<string,string> = {queued:'等待本机执行',running:'正在读取供应商',retrying:'等待恢复',paused:'已暂停',completed:'已保存到云端',failed:'读取失败',cancelled:'已取消'}
const categories = computed(()=>{
  const options:any[]=[]
  const visit=(items:any[])=>{for(const x of items){options.push({value:String(x.id||x.category_id),label:x.name||x.cn_name||String(x.id)});visit(x.children||[])}}
  visit(data.value.categories||[])
  return options
})
function names(items:any[]){return (items||[]).map(x=>x.name||x.cn_name||x.en_name||x.id).join('、')||'—'}
async function load(){
  loading.value=true
  try{
    const q=new URLSearchParams({search:filter.search,category:filter.category,page:String(filter.page),page_size:'50'})
    data.value=await api.get(`/pod/us-catalog?${q}`)
    if(data.value.active_job){job.value=data.value.active_job;schedule()}
  }catch(error:any){ElMessage.error(error.message||'美国货盘读取失败')}finally{loading.value=false}
}
function schedule(){if(timer)clearTimeout(timer);if(!disposed&&busy.value)timer=setTimeout(poll,3000)}
async function poll(){
  try{
    job.value=await api.get(`/jobs/${job.value.id}`)
    if(!busy.value){
      await load()
      if(detailOpen.value&&selected.value)selected.value=await api.get(`/pod/us-catalog/products/${selected.value.product.catalog_key}`)
    }
  }catch{/* A temporary cloud outage leaves the original task visible. */}finally{schedule()}
}
async function sync(){
  submitting.value=true
  try{job.value=(await api.post<any>('/pod/us-catalog/sync',{})).job;schedule();ElMessage.success('美国货盘同步任务已建立')}
  catch(error:any){ElMessage.error(error.message||'无法建立同步任务')}finally{submitting.value=false}
}
async function show(row:any){
  selected.value=undefined;detailLoading.value=true;detailOpen.value=true
  try{selected.value=await api.get(`/pod/us-catalog/products/${row.catalog_key}`)}
  catch(error:any){ElMessage.error(error.message||'商品详情读取失败')}finally{detailLoading.value=false}
}
async function refreshDetail(){
  submitting.value=true
  try{job.value=(await api.post<any>(`/pod/us-catalog/products/${selected.value.product.catalog_key}/refresh`,{})).job;schedule()}
  catch(error:any){ElMessage.error(error.message||'无法读取供应商详情')}finally{submitting.value=false}
}
function sizeName(row:any){return selected.value?.detail?.sizes?.find((x:any)=>String(x.id)===String(row.size_id))?.name||row.size_id||'—'}
function freightJob(value:any){job.value=value;schedule()}
onMounted(load)
onUnmounted(()=>{disposed=true;if(timer)clearTimeout(timer)})
</script>

<template>
  <div class="page-stack">
    <section class="panel">
      <div class="panel-head"><div><h2>美国生产货盘</h2><p>Amazon 美国站 · FBM 商品来源。货盘统一保存在云端，手动同步由你当前的电脑读取供应商。</p></div><el-button type="primary" :loading="submitting" :disabled="!!busy" @click="sync">同步美国货盘</el-button></div>
      <div class="panel-body">
        <el-alert title="已支持 S2B 美国货盘、合成、GOFO 成本定价与 Amazon 美国站上架；美国订单履约仍在开发。" type="info" :closable="false"/>
        <div v-if="job" class="task-banner"><span>{{labels[job.status]||job.status}}</span><el-progress :percentage="Math.round(job.progress||0)" style="width:200px"/><RouterLink :to="`/jobs?job=${job.id}`">查看任务</RouterLink><span v-if="job.error_message">{{job.error_message}}</span></div>
        <div class="toolbar"><el-input v-model="filter.search" clearable placeholder="产品名、编号" @keyup.enter="filter.page=1;load()"/><el-select-v2 v-model="filter.category" :options="categories" clearable filterable placeholder="全部类目"/><el-button @click="filter.page=1;load()">查询云端缓存</el-button></div>
        <p class="cache-note">已保存 {{data.product_count||0}} 个美国商品 · 最近同步：{{data.synced_at||'尚未同步'}} · <RouterLink to="/pod/products?market=US">查看 Amazon POD 合成产品</RouterLink></p>
        <el-table v-loading="loading" :data="data.items" row-key="catalog_key">
          <el-table-column label="图片" width="94"><template #default="s"><img v-if="s.row.preview_url||s.row.view_image_src" :src="s.row.preview_url||s.row.view_image_src" loading="lazy" class="preview"/></template></el-table-column>
          <el-table-column label="商品" min-width="260"><template #default="s"><b>{{s.row.cn_name||s.row.name||s.row.en_name}}</b><small class="table-sub">{{s.row.en_name}}</small><small class="table-sub mono">S2B · {{s.row.code||s.row.source_id}}</small></template></el-table-column>
          <el-table-column label="生产国家 / 仓库" min-width="165"><template #default="s">US · 美国<small class="table-sub">{{s.row.warehouse_name||'仓库名称未提供'}}</small></template></el-table-column>
          <el-table-column label="规格" min-width="170"><template #default="s">{{names(s.row.sizes)}}<small class="table-sub">{{names(s.row.colors)}}</small></template></el-table-column>
          <el-table-column label="采购价（人民币）" min-width="170"><template #default="s">{{s.row.purchase_price??'未提供'}}<small class="table-sub">CNY · 运费按所选规格核算</small></template></el-table-column>
          <el-table-column label="操作" width="120"><template #default="s"><el-button link type="primary" @click="show(s.row)">查看资料</el-button></template></el-table-column>
        </el-table>
        <el-pagination v-model:current-page="filter.page" :page-size="50" :total="data.total||0" layout="prev, pager, next, total" @current-change="load"/>
      </div>
    </section>
    <el-dialog v-model="detailOpen" title="美国货盘商品资料" width="900px" destroy-on-close>
      <div v-loading="detailLoading" v-if="selected">
        <h3>{{selected.product.cn_name||selected.product.name}}</h3><p>{{selected.product.en_name}}</p>
        <el-descriptions :column="2" border><el-descriptions-item label="来源">S2B · {{selected.product.code||selected.product.source_id}}</el-descriptions-item><el-descriptions-item label="生产国家">US · 美国</el-descriptions-item><el-descriptions-item label="仓库">{{selected.product.warehouse_name||'未提供'}}</el-descriptions-item><el-descriptions-item label="详情读取时间">{{selected.detail?.fetched_at||'尚未读取'}}</el-descriptions-item></el-descriptions>
        <p><el-button :loading="submitting" :disabled="!!busy" @click="refreshDetail">{{selected.detail_cached?'手动刷新供应商详情':'读取规格与成本详情'}}</el-button><el-button v-if="selected.detail_cached" type="primary" @click="router.push({path:'/pod/studio',query:{market:'US',product:selected.product.catalog_key}})">进入美国商品合成</el-button><span class="cache-note"> 打开此窗口只读取云端缓存。</span></p>
        <template v-if="selected.detail">
          <p>材质：{{selected.detail.product_material_text||'未提供'}} · 工艺：{{selected.detail.product_technology_text||'未提供'}}</p>
          <el-table :data="selected.detail.stock_info||[]"><el-table-column prop="code" label="供应商规格编号"/><el-table-column label="规格"><template #default="s">{{sizeName(s.row)}}</template></el-table-column><el-table-column prop="price" label="采购价（元）"/><el-table-column prop="weight" label="重量（g）"/><el-table-column label="包装长 / 宽 / 高（cm）"><template #default="s">{{s.row.length||'—'}} / {{s.row.width||'—'}} / {{s.row.height||'—'}}</template></el-table-column></el-table>
          <UsFreightPanel :key="selected.product.catalog_key" :product-key="selected.product.catalog_key" :detail="selected.detail" :task="job" @job="freightJob"/>
        </template>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.task-banner{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin:18px 0}
.toolbar{margin-top:18px}.cache-note{color:#718096;font-size:13px}.preview{width:64px;height:64px;object-fit:contain;border-radius:6px}
</style>
