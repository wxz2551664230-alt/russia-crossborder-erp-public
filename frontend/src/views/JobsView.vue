<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { api } from "../api";
import type { Job, Listing } from "../types";
import { statusLabel, jobTypeLabel } from "../status";
const props=defineProps<{moduleScope?:string;embedded?:boolean}>();
const items = ref<Job[]>([]),
  detail = ref<Job>(),
  drawer = ref(false),
  timer = ref<number>();
const aiUsage = ref<Record<string, any>>({});
const computers=ref<any[]>([]), advanced=ref(false), groupDrawer=ref(false), groupDetail=ref<any>();
const currentWorker=ref('');
const computerFilter=ref(props.embedded?'current':String(useRoute().query.computer||'current'));
const selectedWorker=computed(()=>computerFilter.value==='current'?currentWorker.value:computerFilter.value==='all'?'':computerFilter.value);
function modulePath(module:string){return ({wb:'/catalog/wb/publish',ozon:'/catalog/ozon/publish',orders:'/orders/pod',pod:'/pod/studio',ai:'/pod/ai-design',supplier:'/pod/supplier-gallery',follow:'/ozon-follow/automation',self:'/catalog/imports',system:'/execution-devices'} as Record<string,string>)[module]||'/catalog/products'}
function groupScope(row:any){return `${row.total_items} 项 · 已完成 ${row.counts?.completed||0}，执行中 ${(row.counts?.running||0)+(row.counts?.retrying||0)}，排队 ${row.counts?.queued||0}，待核验/暂停 ${(row.counts?.action_required||0)+(row.counts?.paused||0)}，失败 ${row.counts?.failed||0}`}
async function openGroup(row:any){if(exceptions.value)return open(row);groupDetail.value=await api.get<any>(`/jobs/overview/${row.id}`);groupDrawer.value=true}
async function openChild(row:any){groupDrawer.value=false;await open(row)}

const overview=computed(()=>[
 {key:'unfinished',label:'未完成',count:unfinishedCount.value},
 {key:'active',label:'进行中',count:(statusCounts.value.running||0)+(statusCounts.value.retrying||0)},
 {key:'attention',label:'需处理',count:(statusCounts.value.failed||0)+(statusCounts.value.action_required||0)+(statusCounts.value.paused||0)},
 {key:'completed',label:'已完成',count:statusCounts.value.completed||0}
]);
function chooseStatus(key:string){statusFilter.value=key;void filterChanged()}
function chooseModule(key:string){moduleFilter.value=key;void filterChanged()}
function computerName(job:Job){return computers.value.find(c=>c.id===job.target_worker_id)?.name || (job.target_worker_id?'原执行电脑':'本机任务')}
function completedSteps(job:Job){return job.steps.filter(s=>s.status==='completed').length}
function nextAction(job:Job){
 if(job.status==='completed')return '查看结果';
 if(job.status==='queued')return '等待原电脑执行';
 if(job.status==='running'||job.status==='retrying')return '查看实时进度';
 if(job.status==='paused')return job.payload?.generation_provider==='codex'?'等待 Codex 接手':'查看暂停原因';
 if(job.status==='action_required')return '查看待核验事项';
 if(job.status==='failed')return completedSteps(job)?'查看剩余失败环节':'查看原因与恢复方式';
 return '查看详情';
}
function priceRecovery(job:Job){return (job.payload?.wb_price_recovery||job.wb_price_recovery) as {state?:string;via?:string}|undefined}
function friendlyStatus(job:Job){
 const recovery=priceRecovery(job);
 if(recovery){
  if(job.status==='completed'&&recovery.state==='verified')return '已核验完成';
  if(['running','retrying','queued'].includes(job.status))return job.status==='running'?'只读核验中':'待原电脑核验';
  if(job.status==='action_required')return recovery.state==='not_applied'?'待人工提交':'结果待核验';
 }
 return job.status==='action_required'?'待核验':statusLabel(job.status)
}
function statusTone(job:Job){return job.status==='completed'?'success':job.status==='failed'?'danger':['paused','action_required'].includes(job.status)?'warning':'info'}
function timeText(value:string){const date=new Date(value);return Number.isNaN(date.getTime())?value:date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}

const pipelineArtifacts = computed<any[]>(() => {
  const value = detail.value?.type === 'self_listing_pipeline' ? detail.value.steps?.[0]?.details?.artifacts : null;
  return Array.isArray(value) ? value : [];
});
function pipelineArtifactName(role:string){return ({copywriting_xlsx:'文案表',oss_original_xlsx:'原图 OSS 表',oss_watermarked_xlsx:'水印图 OSS 表'} as Record<string,string>)[role]||'结果表'}
function costLabel(value: any) {
  if (!value) return '未记录';
  if (value.calls === 0) return '无已记录调用';
  const costs = Object.entries(value.costs).map(([currency, amount]) => `${currency} ${Number(amount).toFixed(6)}`);
  if (value.unknown_cost) costs.push(`${value.unknown_cost} 次待核算`);
  return costs.join('；') || '待核算';
}
const recoveryListing = ref<Listing>(),
  recoveryStore = ref(""),
  recoveryTaskId = ref<number>();
const route = useRoute(),
  exceptions = computed(() => !props.embedded && route.name === "exceptions"),
  scopedJobType = computed(() => props.embedded ? "" : String(route.meta.jobType || ""));
const router = useRouter();
const moduleFilter = ref(props.moduleScope || String(route.query.module || ''));
const statusFilter = ref(props.embedded ? 'unfinished' : String(route.query.status === 'all' ? '' : route.query.status || (exceptions.value ? 'failed' : scopedJobType.value ? '' : 'unfinished')));
const search = ref(props.embedded ? '' : String(route.query.search || ''));
const storeFilter = ref(props.embedded ? '' : String(route.query.store || ''));
const page = ref(props.embedded ? 1 : Number(route.query.page) || 1), pageSize = ref(50), total = ref(0);
const modules = ref<{key:string;label:string}[]>([]), stores = ref<any[]>([]);
const statusCounts = ref<Record<string,number>>({}), loading = ref(false), autoRefresh = ref(true);
let requestNumber = 0, inFlight = false, usageKey = '';
const statuses = [{key:'unfinished',label:'未完成（含失败和暂停）'}, {key:'',label:'全部状态'},
  {key:'active',label:'进行中'}, {key:'attention',label:'需处理'}, {key:'action_required',label:'待核验'}, {key:'queued',label:'排队中'}, {key:'running',label:'运行中'}, {key:'retrying',label:'重试中'},
  {key:'paused',label:'已暂停'}, {key:'failed',label:'失败待处理'}, {key:'completed',label:'已完成'}, {key:'cancelled',label:'已取消'}];
const unfinishedCount = computed(()=>Object.entries(statusCounts.value).filter(([key])=>!['completed','cancelled'].includes(key)).reduce((n,[,count])=>n+count,0));
function moduleName(job:Job){return modules.value.find(m=>m.key===job.business_module)?.label || '其他任务'}
function scopeText(job:Job){const p=job.payload||{};const store=stores.value.find(s=>s.id===p.store_id)?.name;const ids=p.ledger_ids||p.product_ids||p.offer_ids;return [store,Array.isArray(ids)?`${ids.length} 项`:''].filter(Boolean).join(' · ')}
function stageText(job:Job){
  if(priceRecovery(job)){
    if(job.status==='completed')return '已核对原计划价格与折扣一致，执行中断记录已保留';
    if(job.status==='action_required')return priceRecovery(job)?.state==='not_applied'?'已核验价格尚未符合计划，请到WB待设置价格折扣页面人工提交':'平台结果尚未确认，请查看核验原因；未重新提交价格';
    if(['queued','running','retrying'].includes(job.status))return '由原电脑批量只读核对价格与折扣，不重新提交';
  }
  if(job.status==='queued')return '等待原执行电脑领取';
  if(job.status==='running' && job.lease_expires_at && Date.parse(job.lease_expires_at)<Date.now())return '连接中断，等待原电脑恢复';
  if(job.status==='completed')return '执行完成';
  if(job.status==='failed')return job.steps.find(s=>s.status==='failed')?.name || '执行失败，查看详情';
  if(job.status==='action_required')return '需要核验或处理，查看详情';
  if(job.status==='paused')return '已暂停，等待继续';
  const step=job.steps.find(s=>s.status==='running');
  return step?.name || (job.status==='running'?'执行中，尚未上报步骤进度':statusLabel(job.status));
}
function progressValue(job:Job){return job.status==='completed'?100:Math.max(0,Math.min(100,Math.round(job.progress)))}
async function filterChanged(){page.value=1;await syncQuery();await load()}
async function syncQuery(){if(props.embedded)return;await router.replace({query:{...route.query,module:moduleFilter.value||undefined,status:statusFilter.value||'all',search:search.value||undefined,store:storeFilter.value||undefined,computer:computerFilter.value==='current'?undefined:computerFilter.value,page:String(page.value)}})}
async function pageChanged(value:number){page.value=value;await syncQuery();await load()}
const followJobTypes = new Set([
  "ozon_ai_prepare",
  "ozon_image_review_action",
  "listing_pipeline",
  "ozon_listing_pipeline",
  "ozon_submit",
  "ozon_stock",
  "ozon_promotion",
]);
async function unresolvedFollowJobs(jobs: Job[]) {
  const followJobs = jobs.filter(
    (job) => followJobTypes.has(job.type) || job.type.startsWith("ozon_"),
  );
  const listingIds = [
    ...new Set(
      followJobs.map((job) => String(job.payload?.listing_id || "")).filter(Boolean),
    ),
  ];
  const listings = new Map<string, Listing>();
  await Promise.all(
    listingIds.map(async (id) =>
      listings.set(id, await api.get<Listing>(`/listings/${id}`)),
    ),
  );
  return followJobs.filter((job) => {
    // A failed job is itself an unresolved exception even when its store
    // child was left in an in-progress status by the worker.
    if (job.status === "failed") return true;
    const listing = listings.get(String(job.payload?.listing_id || ""));
    if (!listing) return true;
    const rawStoreIds = job.payload?.store_ids;
    const targetIds = new Set(
      (Array.isArray(rawStoreIds) ? rawStoreIds : []).map(String),
    );
    const targets = listing.stores.filter(
      (store) => !targetIds.size || targetIds.has(store.store_id),
    );
    return targets.some((store) =>
      ["blocked", "failed", "import_failed", "import_uncertain"].includes(
        store.status,
      ),
    );
  });
}
async function load() {
  const request = ++requestNumber; inFlight = true; loading.value = true;
  try {
    const query = new URLSearchParams({page_size:String(pageSize.value),page:String(page.value)});
    if (exceptions.value) query.set('status', 'failed');
    else if(statusFilter.value && statusFilter.value !== 'all') query.set('status',statusFilter.value);
    if(moduleFilter.value)query.set('module',moduleFilter.value);
    if(storeFilter.value)query.set('store_id',storeFilter.value);
    if(search.value.trim())query.set('search',search.value.trim());
    if(scopedJobType.value)query.set('job_type',scopedJobType.value);
    if(selectedWorker.value)query.set('worker_id',selectedWorker.value);
    const r=await api.get<any>(`${exceptions.value?'/jobs':'/jobs/overview'}?${query}`);
    if(request!==requestNumber)return;
    const nextItems=exceptions.value?await unresolvedFollowJobs(r.items):r.items;
    if(request!==requestNumber)return;items.value=nextItems;
    total.value=r.total;modules.value=r.modules||[];statusCounts.value=r.status_counts||{};
    if(groupDrawer.value&&groupDetail.value){const id=groupDetail.value.id;const next=await api.get<any>(`/jobs/overview/${id}`);if(request===requestNumber&&groupDetail.value?.id===id)groupDetail.value=next;}
    if(drawer.value&&detail.value){const id=detail.value.id;const next=await api.get<Job>(`/jobs/${id}`);if(request===requestNumber&&detail.value?.id===id)detail.value=next;await loadUsage(id);}
  }catch(error){ElMessage.error(String((error as Error).message||error))}
  finally{if(request===requestNumber){loading.value=false;inFlight=false}}
}
async function loadUsage(id:string){try{const usage=await api.get<any>(`/ai-usage?job_id=${id}`);const item=usage.items?.find((x:any)=>x.job_id===id);if(item)aiUsage.value[id]=item}catch{/* Optional cost data must not block task status. */}}
async function open(row: Job) {
  const job = await api.get<Job>(`/jobs/${row.id}`);
  detail.value = job;
  recoveryListing.value = undefined;
  const listingId = String(job.payload?.listing_id || "");
  if (
    listingId &&
    ["OzonImportUncertain", "ozon_import_recovery_required"].includes(
      job.error_code,
    )
  ) {
    const listing = await api.get<Listing>(`/listings/${listingId}`);
    recoveryListing.value = listing;
    recoveryStore.value =
      listing.stores.find((s) => s.status === "import_uncertain")?.store_id ||
      listing.stores[0]?.store_id ||
      "";
  }
  drawer.value = true;void loadUsage(job.id);
}
onMounted(async () => {
  await Promise.allSettled([api.get<{items:any[]}>('/stores').then(result=>{stores.value=result.items}),api.get<any>('/execution-devices').then(result=>{computers.value=result.items||[];currentWorker.value=result.current_worker_id||''})]);
  await load();
  if(!props.embedded&&route.query.job)try{await open({id:String(route.query.job)} as Job)}catch(error){ElMessage.error(String(error))}
  timer.value=window.setInterval(()=>{if(autoRefresh.value&&!document.hidden&&!inFlight)load()},10000);
});
watch(()=>route.query,async query=>{
  if(props.embedded)return;
  const m=String(query.module||''),st=String(query.status||'unfinished'),store=String(query.store||''),term=String(query.search||''),pc=String(query.computer||'current'),pg=Number(query.page)||1;
  if(m!==moduleFilter.value||st!==(statusFilter.value||'all')||store!==storeFilter.value||term!==search.value||pc!==computerFilter.value||pg!==page.value){
    moduleFilter.value=m;statusFilter.value=st==='all'?'':st;storeFilter.value=store;search.value=term;computerFilter.value=pc;page.value=pg;await load();
  }
  if(query.job&&String(query.job)!==detail.value?.id)try{await open({id:String(query.job)} as Job)}catch(error){ElMessage.error(String(error))}
});
onUnmounted(() => clearInterval(timer.value));
</script>
<template>
  <div class="panel task-center">
    <div class="panel-head">
      <div>
        <h2>
          {{
            exceptions
              ? "异常处理"
              : scopedJobType
                ? "POD 制作记录"
                : "任务中心"
          }}
        </h2>
        <p>
          {{
            exceptions
              ? "仅展示当前仍未解决的 OZON 跟卖异常；已处理记录保留在运行记录中"
              : "关闭网页不会停止任务；执行电脑需要保持客户端连接"
          }}
        </p>
      </div>
      <div><span class="refresh-note">默认查看当前电脑的未完成任务</span><el-button @click="load">刷新</el-button></div>
    </div>
    <div class="task-computer-scope"><el-select v-model="computerFilter" aria-label="执行电脑筛选" @change="filterChanged"><el-option :label="currentWorker?`当前电脑 · ${computerName({target_worker_id:currentWorker} as Job)}`:'当前电脑（尚未选择）'" value="current"/><el-option label="全部电脑" value="all"/><el-option v-for="c in computers.filter(c=>c.id!==currentWorker)" :key="c.id" :label="`${c.name}${c.online?'':' · 离线'}`" :value="c.id"/></el-select><span class="task-sub">{{computerFilter==='current'&&!currentWorker?'尚未选择执行电脑，暂显示全部电脑任务。':'不同电脑各自执行队列；离线任务等待原电脑恢复。'}}</span></div>
    <template v-if="!exceptions">
      <div class="task-overview"><button v-for="card in overview" :key="card.key" :class="{selected:statusFilter===card.key}" @click="chooseStatus(card.key)"><span>{{card.label}}</span><strong>{{card.count}}</strong></button></div>
      <nav class="module-tabs" v-if="!props.embedded"><button :class="{selected:!moduleFilter}" @click="chooseModule('')">全部业务</button><button v-for="m in modules" :key="m.key" :class="{selected:moduleFilter===m.key}" @click="chooseModule(m.key)">{{m.label}}</button></nav>
      <div class="task-search"><el-input v-model="search" clearable placeholder="搜索任务编号、SKU 或错误关键词" @keyup.enter="filterChanged" @clear="filterChanged"/><el-button type="primary" @click="filterChanged">搜索</el-button><el-button text @click="advanced=!advanced">{{advanced?'收起筛选':'更多筛选'}}</el-button><el-checkbox v-model="autoRefresh">自动刷新</el-checkbox></div>
      <div v-if="advanced" class="task-filters"><el-select v-model="statusFilter" @change="filterChanged"><el-option v-for="s in statuses" :key="s.key" :label="s.label" :value="s.key"/></el-select><el-select v-model="storeFilter" clearable placeholder="全部店铺" @change="filterChanged"><el-option v-for="s in stores" :key="s.id" :label="`${s.name} · ${s.platform}`" :value="s.id"/></el-select><el-tag v-if="moduleFilter">{{modules.find(m=>m.key===moduleFilter)?.label}}</el-tag></div>
    </template>
    <el-table v-loading="loading" :data="items" @row-click="openGroup" class="task-table" empty-text="当前筛选下没有任务">
      <el-table-column label="任务" min-width="250"><template #default="{row}"><div class="task-name">{{jobTypeLabel(row.type)}}<span v-if="row.total_items>1"> · {{row.total_items}} 项</span></div><small class="task-sub">{{moduleName(row)}}</small><small class="task-sub">{{computerName(row)}} · {{timeText(row.updated_at)}}</small></template></el-table-column>
      <el-table-column label="状态" width="110"><template #default="{row}"><el-tag :type="statusTone(row)">{{friendlyStatus(row)}}</el-tag></template></el-table-column>
      <el-table-column label="当前阶段" min-width="270"><template #default="{row}"><template v-if="!exceptions"><el-progress :percentage="row.progress" :stroke-width="7"/><div class="step-count">{{groupScope(row)}}</div></template><template v-else><b>{{stageText(row)}}</b><div class="step-count">已完成 {{completedSteps(row)}} / {{row.steps.length}} 个步骤</div></template></template></el-table-column>
      <el-table-column label="明细" width="110"><template #default="{row}"><el-button text type="primary" @click.stop="openGroup(row)">查看明细</el-button></template></el-table-column>
    </el-table>
    <el-pagination :current-page="page" :page-size="pageSize" :total="total" layout="total, prev, pager, next, jumper" @update:current-page="pageChanged"/>
    <el-drawer v-model="groupDrawer" title="批量任务进度" size="760px" append-to-body><template v-if="groupDetail"><h3>{{jobTypeLabel(groupDetail.type)}} · {{groupDetail.total_items}} 项</h3><el-progress :percentage="groupDetail.progress"/><p>{{groupScope(groupDetail)}}</p><el-button type="primary" plain @click="router.push(modulePath(groupDetail.business_module))">前往对应业务页面</el-button><p class="task-sub">这里仅查看进度。核验、继续执行和其他操作请在对应业务页面完成。</p><el-table :data="groupDetail.children" @row-click="openChild"><el-table-column label="单件 / 子任务" min-width="260"><template #default="{row}"><b>{{row.sku||row.payload?.sku||row.payload?.offer_id||jobTypeLabel(row.type)}}</b><small class="task-sub">{{row.id}}</small></template></el-table-column><el-table-column label="状态" width="110"><template #default="{row}">{{friendlyStatus(row)}}</template></el-table-column><el-table-column label="当前步骤" min-width="230"><template #default="{row}">{{stageText(row)}}<small class="task-sub">已完成 {{completedSteps(row)}} / {{row.steps.length}} 个步骤</small></template></el-table-column><el-table-column label="详情" width="85"><template #default="{row}"><el-button text @click.stop="openChild(row)">查看</el-button></template></el-table-column></el-table></template></el-drawer>
    <el-drawer v-model="drawer" title="任务详情" size="620px" append-to-body
      ><template v-if="detail"><div style="padding:16px"><b>{{ jobTypeLabel(detail.type) }} · {{ friendlyStatus(detail) }}</b><p class="task-id">{{ detail.id }}</p><p>{{ stageText(detail) }}</p><el-alert v-if="detail.error_message" :title="detail.error_message" :type="priceRecovery(detail)?'warning':'error'" :closable="false"/><p>{{ computerName(detail) }} · 最近更新 {{timeText(detail.updated_at)}} · 已尝试 {{detail.attempts}} 次</p><el-button @click="props.embedded ? router.push({path:'/jobs',query:{module:moduleFilter,status:statusFilter||'all',job:detail.id}}) : router.replace({query:{...route.query,job:detail.id}})">固定此任务链接</el-button></div
        ><div v-if="aiUsage[detail.id]" style="padding: 16px"><h3>AI 调用统计</h3><p class="task-sub">费用为估算；未记录不代表没有调用，未知结果需核对。</p><p>文字 {{ aiUsage[detail.id].text }} 次 · 生图 {{ aiUsage[detail.id].image }} 次</p><p>成功 {{ aiUsage[detail.id].success }} 次 · 错误 {{ aiUsage[detail.id].errors }} 次 · 进行中/结果不明 {{ aiUsage[detail.id].pending }} 次</p><p>已返回输入 token {{ aiUsage[detail.id].input_tokens }} · 输出 token {{ aiUsage[detail.id].output_tokens }}；{{ aiUsage[detail.id].missing_usage }} 次未返回完整用量</p><p>估算费用：{{ costLabel(aiUsage[detail.id]) }}</p><p v-for="model in aiUsage[detail.id].models" :key="model">{{ model }}</p></div
        ><div v-if="aiUsage[detail.id]" style="padding: 0 16px 16px"><p>文字费用：{{ costLabel(aiUsage[detail.id].by_kind.text) }}</p><p>生图费用：{{ costLabel(aiUsage[detail.id].by_kind.image) }} · 接口返回 {{ aiUsage[detail.id].images }} 张</p></div><div v-if="recoveryListing" style="padding:16px"><el-alert title="原平台结果需要核对，请前往Ozon跟卖业务页面绑定原任务号并恢复。" :closable="false" type="warning"/></div>
        <el-alert v-if="detail.error_code==='ai_write_outcome_unknown'&&detail.type!=='catalog_ozon_prepare'" title="AI 请求结果待核对" type="warning" :closable="false" description="原请求可能已计费。请保留本机执行端和原任务，不要新建重复任务。已保存的成功响应会在原任务恢复时复用；没有成功回执时需先核对服务商请求记录，确认未成功后才能重新调用。" />
        <div style="padding:16px"><el-button type="primary" plain @click="router.push(modulePath(detail.business_module||moduleName(detail)))">前往业务页面处理</el-button></div>
        <div v-if="pipelineArtifacts.length" style="padding: 0 16px 16px">
          <h3>自主上架结果表</h3>
          <p v-for="item in pipelineArtifacts" :key="item.asset_id"><a :href="`/api/v1/assets/${item.asset_id}/content`" target="_blank" rel="noopener">下载{{ pipelineArtifactName(item.role) }}</a></p>
        </div>
        <el-timeline
          ><el-timeline-item
            v-for="s in detail.steps"
            :key="s.id"
            :timestamp="`${statusLabel(s.status)} · ${s.progress}%`"
            :type="
              s.status === 'completed'
                ? 'success'
                : s.status === 'failed'
                  ? 'danger'
                  : 'primary'
            "
            ><b>{{ s.name }}</b>
            <p v-if="((detail.type === 'pod_library_restore' && s.key === 'verify') || (['pod_native_composition','pod_us_composition'].includes(detail.type) && s.key === 'sync_catalog') || (detail.type === 'pod_artwork_upload' && s.key === 'oss')) && s.details?.folder" style="overflow-wrap: anywhere; user-select: text">
              文件保存在执行电脑：{{ s.details.folder }}
            </p>
            <p v-if="s.error_message">
              {{ s.error_message }}
            </p></el-timeline-item
          ></el-timeline
        >
        <h3>运行日志</h3>
        <div class="event-list">
          <div v-for="e in detail.events" :key="e.id">
            <small>{{ e.created_at }} · {{ e.level }}</small
            ><span>{{ e.message }}</span>
          </div>
        </div></template
      ></el-drawer
    >
  </div>
</template>
<style scoped>.task-computer-scope{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin:0 0 16px}.task-computer-scope .el-select{width:310px;max-width:100%}</style>
<style scoped>
.task-center{padding:0 22px 16px}.task-center .panel-head{padding-left:0;padding-right:0}.refresh-note{font-size:12px;color:#7b889a;margin-right:14px}.task-overview{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:4px 0 20px}.task-overview button{display:flex;justify-content:space-between;align-items:center;padding:15px 20px;border:1px solid #e1e7ef;border-radius:12px;background:#f8fafc;color:#627087;cursor:pointer;text-align:left}.task-overview strong{font-size:25px;color:#253b55}.task-overview button.selected{border-color:#409eff;background:#edf5ff;color:#1764ca}.task-overview button.selected strong{color:#1764ca}.module-tabs{display:flex;gap:6px;flex-wrap:wrap;padding-bottom:14px;border-bottom:1px solid #edf0f5}.module-tabs button{border:0;background:transparent;color:#67758a;padding:7px 12px;border-radius:7px;cursor:pointer}.module-tabs button.selected{background:#eaf2ff;color:#1764ca;font-weight:600}.task-search{display:flex;align-items:center;gap:10px;margin:18px 0}.task-search .el-input{max-width:440px}.task-search .el-checkbox{margin-left:auto}.task-filters{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0}.task-filters .el-select{width:210px}.task-name{font-weight:650;color:#263b54}.task-sub{display:block;color:#8290a2;font-size:12px;line-height:1.8}.step-count{color:#8190a2;font-size:12px;margin:5px 0}.error-summary{display:block;color:#b86d58;font-size:12px}.task-id{font-size:12px;color:#68778d;overflow-wrap:anywhere;user-select:text}.el-pagination{margin:16px 0}.task-table{cursor:pointer}@media(max-width:720px){.task-center{padding:0 12px 12px}.task-overview{grid-template-columns:repeat(2,1fr)}.task-search{flex-wrap:wrap}.task-search .el-checkbox{margin-left:0}.refresh-note{display:none}}
</style>
