<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { AlertTriangle, BarChart3, CircleDollarSign, RefreshCw, ShieldCheck, Sparkles, Upload } from 'lucide-vue-next'
import { api } from '../api'

interface StoreOption { id: string; name: string }
interface AdItem {
  id: string; sku_record_id: string; sku_id: string; product_id: string; image_url: string; name: string; category_path: string
  price: number | null; target_drr: number | null; break_even_drr: number | null; outcome_label: string; weak_label: string; data_maturity: string
  state: string; state_label: string; confidence: number; recommendation: string; recommendation_label: string
  evidence_strength: string; primary_state: string; secondary_flags: string[]; candidate_action: string; action_risk: string
  execution_status: string
  decision_tier: string; review_owner: string; ready_for_shadow_action: boolean; ready_for_execution: boolean; stop_evidence_count: number; stop_evidence_flags: string[]; budget_limited: boolean
  campaign_sku_actions: Record<string, any>[]; multi_campaign_conflict: boolean; risk_flags: string[]; safety_gate: Record<string, boolean>
  reason: string; cohort_key: string; cohort_level: string; cohort_sample_size: number; metrics: Record<string, any>
  evidence: string[]; financial_check: Record<string, any>; updated_at: string
}
interface Threshold {
  id: string; version: string; cohort_key: string; cohort_level: string; sample_size: number
  success_sample_size: number; window_days: number; values: Record<string, any>
}
interface Overview {
  mode: string; stores: StoreOption[]; store_id: string
  summary: { sku_count?: number; stopped_history_count?: number; observation_count?: number; optimization_count?: number; stop_count?: number; system_stop_count?: number; executable_stop_count?: number; codex_review_count?: number; scale_count?: number; executable_scale_count?: number; no_exposure_count?: number; potential_savings?: number; window_spend?: number }
  items: AdItem[]; pagination: { page: number; page_size: number; total: number }; thresholds: Threshold[]; latest_backtest: any
}

const data = ref<Overview>({ mode: 'review_required', stores: [], store_id: '', summary: {}, items: [], pagination: { page: 1, page_size: 50, total: 0 }, thresholds: [], latest_backtest: null })
const loading = ref(false)
const analyzing = ref(false)
const syncing = ref(false)
const backtesting = ref(false)
const lastJobId = ref('')
const storeId = ref('')
const search = ref('')
const state = ref('')
const candidateAction = ref('')
const currentPage = ref(1)
const selectedRows = ref<AdItem[]>([])
const importOpen = ref(false)
const importFile = ref<File>()
const importing = ref(false)
const detail = ref<AdItem>()
const detailOpen = computed({ get: () => Boolean(detail.value), set: (value: boolean) => { if (!value) detail.value = undefined } })
const actionSaving = ref(false)
const executing = ref('')
const executionHistory = ref<any[]>([])
async function loadExecutions() { if (storeId.value) executionHistory.value = (await api.get<any>(`/ad-optimization/actions/batches?store_id=${encodeURIComponent(storeId.value)}`)).items }
async function reconcileExecution(id: string) {
  try {
    const queued = await api.post<any>(`/ad-optimization/actions/batches/${id}/reconcile?store_id=${encodeURIComponent(storeId.value)}`)
    if (queued.job_id) {
      lastJobId.value = queued.job_id
      ElMessage.info('正在原执行电脑上只读查询平台状态')
      for (let attempt = 0; attempt < 300; attempt++) {
        const job = await api.get<any>(`/jobs/${queued.job_id}`)
        if (job.status === 'completed') break
        if (['failed', 'cancelled'].includes(job.status)) throw new Error(job.error_message || '广告核对未完成，请在任务中心查看')
        if (attempt === 299) throw new Error('广告核对仍在原执行电脑运行，请在任务中心查看')
        await new Promise(resolve => setTimeout(resolve, 2000))
      }
    }
    await load()
    ElMessage.success('已查询平台状态，未重发广告操作')
  } catch (e) { ElMessage.error(String((e as Error).message)) }
}

const cards = computed(() => [
  { label: '纳入分析 SKU', value: data.value.summary.sku_count || 0, unit: '个', icon: BarChart3, color: '#1d64f2' },
  { label: '高证据停投', value: data.value.summary.system_stop_count || 0, unit: '个', icon: AlertTriangle, color: '#df4f5f' },
  { label: '已停投历史', value: data.value.summary.stopped_history_count || 0, unit: '个', icon: ShieldCheck, color: '#5f6b7a' },
  { label: '待 Codex 复核', value: data.value.summary.codex_review_count || 0, unit: '个', icon: ShieldCheck, color: '#8b5cf6' },
  { label: '可放量', value: data.value.summary.scale_count || 0, unit: '个', icon: Sparkles, color: '#18a672' },
  { label: '运行中无曝光', value: data.value.summary.no_exposure_count || 0, unit: '个', icon: CircleDollarSign, color: '#e89d1b' },
  { label: '回放最大后续消耗', value: money(data.value.summary.potential_savings), unit: '₽', icon: CircleDollarSign, color: '#18a672' },
])

const thresholdRows = computed(() => data.value.thresholds.map(item => ({
  ...item,
  ctr: percent(item.values.low_ctr_threshold),
  atc: percent(item.values.low_atc_threshold),
  cvr: percent(item.values.low_cvr_threshold),
  impressions: number(item.values.traffic_starved_threshold),
  clicks: number(item.values.min_click_sample),
  cold: number(item.values.cold_start_duration),
})))
const backtestStrategyRows = computed(() => (data.value.latest_backtest?.summary?.strategies || []).map((item: any) => ({
  ...item,
  percentile: `P${Math.round(Number(item.first_order_quantile || 0) * 100)}`,
})))
const backtestOverview = computed(() => ({
  strategyCount: backtestStrategyRows.value.length,
  maxStops: Math.max(0, ...backtestStrategyRows.value.map((item: any) => Number(item.stopped_sku_count || 0))),
  maxSavings: Math.max(0, ...backtestStrategyRows.value.map((item: any) => Number(item.future_spend_saved || 0))),
}))

async function load() {
  loading.value = true
  try {
    const query = new URLSearchParams()
    if (storeId.value) query.set('store_id', storeId.value)
    if (search.value) query.set('search', search.value)
    if (state.value) query.set('state', state.value)
    if (candidateAction.value) query.set('candidate_action', candidateAction.value)
    query.set('page', String(currentPage.value))
    query.set('page_size', '50')
    data.value = await api.get<Overview>(`/ad-optimization?${query}`)
    selectedRows.value = []
    if (!storeId.value && data.value.store_id) storeId.value = data.value.store_id
    await loadExecutions()
  } finally { loading.value = false }
}

async function changeStore() { search.value = ''; state.value = ''; candidateAction.value = ''; currentPage.value = 1; await load() }
async function applyFilters() { currentPage.value = 1; await load() }
async function changePage(page: number) { currentPage.value = page; await load() }
async function analyze() {
  if (!storeId.value) return ElMessage.warning('请先添加并选择 OZON 店铺')
  analyzing.value = true
  try {
    const result = await api.post<{job_id:string;already_running:boolean}>(`/ad-optimization/jobs/analyze?store_id=${encodeURIComponent(storeId.value)}`)
    lastJobId.value = result.job_id
    ElMessage.success(result.already_running ? '该店铺已有分析任务运行中' : '分析任务已进入任务中心，可继续操作')
  } finally { analyzing.value = false }
}
async function syncFromOzon() {
  if (!storeId.value) return ElMessage.warning('请先添加并选择 OZON 店铺')
  syncing.value = true
  try {
    const result = await api.post<{job_id:string;already_running:boolean}>(`/ad-optimization/jobs/sync?store_id=${encodeURIComponent(storeId.value)}&days=62`)
    lastJobId.value = result.job_id
    ElMessage.success(result.already_running ? '该店铺已有同步任务运行中' : 'Ozon 同步任务已进入任务中心，可继续操作')
    void refreshWhenSyncFinishes(result.job_id, storeId.value)
  } finally { syncing.value = false }
}
async function refreshWhenSyncFinishes(jobId: string, selectedStore: string) {
  try {
    for (let attempt = 0; attempt < 360; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 5000))
      const job = await api.get<{status:string;error_message?:string}>(`/jobs/${jobId}`)
      if (job.status === 'completed') {
        if (storeId.value === selectedStore) await load()
        ElMessage.success('Ozon 广告数据已同步，页面已刷新')
        return
      }
      if (['failed', 'cancelled'].includes(job.status)) {
        ElMessage.error(job.error_message || '广告同步未完成，请查看任务详情')
        return
      }
    }
  } catch { ElMessage.warning('暂时无法查询广告同步结果，请到任务中心查看') }
}
async function backtest() {
  if (!storeId.value) return ElMessage.warning('请先选择店铺')
  backtesting.value = true
  try {
    const result = await api.post<{job_id:string;already_running:boolean}>(`/ad-optimization/jobs/backtest?store_id=${encodeURIComponent(storeId.value)}`)
    lastJobId.value = result.job_id
    ElMessage.success(result.already_running ? '该店铺已有回测任务运行中' : '回测任务已进入任务中心，可继续操作')
  } finally { backtesting.value = false }
}
async function submitImport() {
  if (!storeId.value || !importFile.value) return ElMessage.warning('请选择店铺和 CSV 文件')
  importing.value = true
  try {
    const form = new FormData()
    form.append('store_id', storeId.value)
    form.append('file', importFile.value)
    const result = await api.upload<{job_id:string}>('/ad-optimization/imports/csv/background', form)
    importOpen.value = false
    importFile.value = undefined
    lastJobId.value = result.job_id
    ElMessage.success('历史数据导入任务已进入任务中心，可继续操作')
  } finally { importing.value = false }
}
async function updateOutcome(row: AdItem, value: string) {
  await api.put(`/ad-optimization/skus/${row.sku_record_id}/outcome`, { outcome_label: value })
  row.outcome_label = value
  ElMessage.success('历史结果标签已保存，请重新分析以更新阈值')
}
async function saveProposal() {
  if (!detail.value) return
  actionSaving.value = true
  try {
    await api.post('/ad-optimization/actions', { state_id: detail.value.id })
    ElMessage.success('已保存建议；通过执行预览并确认后才会修改 Ozon 广告')
  } finally { actionSaving.value = false }
}
async function resolvePreview(queued: any) {
  if (!queued.job_id) return queued
  lastJobId.value = queued.job_id
  ElMessage.info('正在你的电脑上计算本次广告预览，请保持执行端在线')
  for (let attempt = 0; attempt < 300; attempt++) {
    const job = await api.get<any>(`/jobs/${queued.job_id}`)
    if (job.status === 'completed') return api.get<any>(`/ad-optimization/actions/previews/${queued.job_id}`)
    if (['failed', 'cancelled'].includes(job.status)) throw new Error(job.error_message || '广告预览任务未完成，请在任务中心查看')
    await new Promise(resolve => setTimeout(resolve, 2000))
  }
  throw new Error('广告预览仍在本人电脑运行，请稍后从任务中心查看并重新选择')
}
async function resolveExecution(queued: any) {
  if (!queued.job_id) return queued
  lastJobId.value = queued.job_id
  ElMessage.info('已由你的电脑执行审批批次，等待平台结果回传')
  for (let attempt = 0; attempt < 300; attempt++) {
    const job = await api.get<any>(`/jobs/${queued.job_id}`)
    if (job.status === 'completed') return api.get<any>(`/ad-optimization/actions/batches/${queued.approval_id}?store_id=${encodeURIComponent(storeId.value)}`)
    if (['failed', 'cancelled'].includes(job.status)) throw new Error(job.error_message || '广告执行结果待核对，请在任务中心查看，勿重复提交')
    await new Promise(resolve => setTimeout(resolve, 2000))
  }
  throw new Error('广告执行仍在本人电脑运行或等待回传，请在任务中心查看，勿重复提交')
}
async function executeBatch(action: 'STOP_ADS_CANDIDATE' | 'SCALE_CANDIDATES') {
  if (!storeId.value) return
  const isStop = action === 'STOP_ADS_CANDIDATE'
  const matching = selectedRows.value.filter(row => (
    isStop ? row.candidate_action === 'STOP_ADS_CANDIDATE' : row.candidate_action === 'INCREASE_TARGET_DRR_CANDIDATE'
  ))
  if (!matching.length) return ElMessage.warning(isStop ? '请勾选停止广告候选' : '请勾选放量候选')
  executing.value = action
  try {
    const stateIds = matching.map(row => row.id)
    const preview = await resolvePreview(await api.post<any>(`/ad-optimization/actions/preview?store_id=${encodeURIComponent(storeId.value)}`, { action, state_ids: stateIds }))
    const lines = preview.items.map((item: any) => `${item.sku_id} · 计划 ${item.campaign_id} · ${isStop ? '从广告计划移除' : `${percent(item.previous_target_drr)} → ${percent(item.proposed_target_drr)}`}`)
    await ElMessageBox.confirm(`${preview.store_name}：将实际修改 ${lines.length} 个广告商品。\n${lines.join('\n')}\n另有 ${preview.blocked.length} 个未通过校验，不会执行。`, '确认本次广告变更', { confirmButtonText: '确认并正式执行', cancelButtonText: '取消', type: 'warning', customStyle: { whiteSpace: 'pre-line', maxHeight: '80vh', overflowY: 'auto' } })
    const result = await resolveExecution(await api.post<any>(`/ad-optimization/actions/execute-batch?store_id=${encodeURIComponent(storeId.value)}`, { action, confirmed: true, approval_id: preview.approval_id, state_ids: stateIds }))
    if (result.unknown) ElMessage.warning('部分结果待核对，禁止重复提交；请在执行记录中核对原批次')
    else if (result.failed) ElMessage.warning(`执行完成：成功 ${result.succeeded} 个，拦截 ${result.blocked || 0} 个，其他失败 ${Math.max(0, result.failed - (result.blocked || 0))} 个`)
    else ElMessage.success(`执行完成：成功 ${result.succeeded} 个`)
    await load()
  } catch (error) { if (error !== 'cancel' && error !== 'close') ElMessage.error(String((error as Error).message || error)) } finally { executing.value = '' }
}
function selectionChanged(rows: AdItem[]) { selectedRows.value = rows }
const selectedStopCount = computed(() => selectedRows.value.filter(row => row.candidate_action === 'STOP_ADS_CANDIDATE').length)
const selectedScaleCount = computed(() => selectedRows.value.filter(row => row.candidate_action === 'INCREASE_TARGET_DRR_CANDIDATE').length)

function percent(value: any) { return value === null || value === undefined ? '—' : `${(Number(value) * 100).toFixed(2)}%` }
function money(value: any) { return Number(value || 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }
function number(value: any) { return value === null || value === undefined ? '—' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 1 }) }
function stateType(value: string) {
  if (['HEALTHY', 'GROWTH', 'SCALE_CANDIDATE'].includes(value)) return 'success'
  if (['LOW_CVR', 'ZERO_ORDER_HIGH_CLICK', 'ZERO_ORDER_HIGH_SPEND', 'RECENT_ZERO_ORDER_DECAY', 'BAD_ECONOMICS', 'STOP_ADS'].includes(value)) return 'danger'
  if (['ACTIVE_NO_EXPOSURE', 'LOW_CTR', 'LOW_ATC', 'HIGH_CART_LOW_ORDER', 'TRAFFIC_STARVED'].includes(value)) return 'warning'
  return 'info'
}
function cohortLevel(value: string) { return ({ full: '完整同类', category: '类目回退', store: '全店回退', insufficient: '样本不足' } as Record<string, string>)[value] || value }
function evidenceType(value: string) { return value === 'HIGH' ? 'success' : value === 'MEDIUM' ? 'warning' : 'info' }
function evidenceLabel(value: string) { return ({ HIGH: '高', MEDIUM: '中', LOW: '低' } as Record<string, string>)[value] || value || '低' }
function freshnessLabel(value: string) { return ({ FRESH: '新鲜', STALE: '陈旧', NO_RECENT_DATA: '近期无数据', NO_METRIC_DATA: '无广告指标' } as Record<string, string>)[value] || value || '未知' }
function actionLabel(value: string) { return ({ STOP_ADS_CANDIDATE: '停止广告候选', INCREASE_TARGET_DRR_CANDIDATE: '提高目标费用候选', INCREASE_BUDGET_CANDIDATE: '提高预算候选', DIAGNOSE_NO_EXPOSURE: '自动排查无曝光', REDUCE_TARGET_DRR: '降低目标费用', OPTIMIZE_CREATIVE: '优化素材', OPTIMIZE_LISTING: '优化商品卡', CHECK_OFFER: '检查报价', OBSERVE: '继续观察', HOLD: '保持' } as Record<string, string>)[value] || value }
function flagLabel(value: string) { return ({ ACTIVE_NO_EXPOSURE: '运行中无曝光', NO_METRIC_DATA: '无广告数据', RECENT_ZERO_ORDER_DECAY: '历史出单后衰退', ZERO_ORDER_HIGH_CLICK: '高点击零成交', ZERO_ORDER_HIGH_SPEND: '高消耗零成交', LOW_ATC: '加购率偏低', LOW_CVR: '成交率偏低', INSUFFICIENT_CLICK_SAMPLE: '点击样本不足', FIRST_ORDER_CLICK_THRESHOLD: '超过首单点击 P80', FIRST_ORDER_SPEND_THRESHOLD: '超过首单消耗 P80', NO_CART_SIGNAL: '无加购信号', BELOW_BREAK_EVEN_CVR: '低于经济生存 CVR', MULTI_CAMPAIGN_CONFLICT: '多计划冲突' } as Record<string, string>)[value] || value }
function gateLabel(value: string) { return ({ sample_strength: '当前样本', cohort_strength: 'Cohort 强度', threshold_stability: '阈值稳定性', cohort_fallback_penalty: 'Cohort 回退', positive_sample_strength: '正向样本', negative_sample_strength: '删失样本', stop_evidence_count: '停投证据数', decision_tier: '决策层级', data_freshness: '数据新鲜度', data_maturity: '数据成熟度', cohort_sample_quality: 'Cohort 样本', first_order_threshold_quality: '首单阈值', survival_estimate_quality: '生存估计', cold_start_protection: '冷启动保护', financial_confidence: '财务可信度', financial_safe_to_scale: '放量财务安全', recent_configuration_change_cooldown: '配置冷却期', multi_campaign_conflict: '多计划冲突检查', campaign_action_scope_available: '计划动作范围', data_quality: '数据质量', observation_window_adequate: '观测窗口充分', shadow_mode: '影子模式' } as Record<string, string>)[value] || value }
function decisionTierLabel(value: string) { return ({ SYSTEM_HIGH_CONFIDENCE: '系统高证据', CODEX_REVIEW: 'Codex 复核', SYSTEM_DIAGNOSIS: '系统诊断', STANDARD: '常规规则' } as Record<string, string>)[value] || value }
function fileChanged(event: Event) { importFile.value = (event.target as HTMLInputElement).files?.[0] }

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="page-stack ad-optimization-page">
    <section class="shadow-banner">
      <div class="shadow-icon"><ShieldCheck :size="22" /></div>
      <div><strong>正式执行模式 · 逐批审批</strong><p>先查看具体 SKU、计划和目标值，再确认执行；结果不确定时先核对，不会自动重发。</p></div>
      <span>LIVE · REVIEWED</span>
    </section>

    <section v-if="executionHistory.length" class="panel compact"><div class="panel-head"><h2>最近广告执行记录</h2></div><el-table :data="executionHistory"><el-table-column prop="approval_id" label="批次"/><el-table-column prop="status" label="状态"/><el-table-column prop="succeeded" label="已确认成功"/><el-table-column prop="unknown" label="待核对"/><el-table-column label="处理"><template #default="{row}"><el-button v-if="['unknown','executing'].includes(row.status)" @click="reconcileExecution(row.approval_id)">核对平台结果</el-button></template></el-table-column></el-table></section>
    <section class="panel compact">
      <div class="toolbar">
        <el-select v-model="storeId" placeholder="选择 OZON 店铺" style="width: 220px" @change="changeStore">
          <el-option v-for="store in data.stores" :key="store.id" :label="store.name" :value="store.id" />
        </el-select>
        <el-input v-model="search" placeholder="搜索 SKU、商品名或类目" clearable @keyup.enter="applyFilters" />
        <el-select v-model="state" placeholder="全部状态" clearable style="width: 170px" @change="applyFilters">
          <el-option v-for="option in ['STOPPED_EXECUTED','SYSTEM_HIGH_CONFIDENCE','CODEX_REVIEW','ACTIVE_NO_EXPOSURE','RECENT_ZERO_ORDER_DECAY','ZERO_ORDER_HIGH_SPEND','ZERO_ORDER_HIGH_CLICK','SCALE_CANDIDATE','BAD_ECONOMICS','LOW_CTR','LOW_ATC','HIGH_CART_LOW_ORDER','LOW_CVR','COLD_START','TRAFFIC_STARVED','NO_METRIC_DATA','OBSERVATION','HEALTHY']" :key="option" :label="({STOPPED_EXECUTED:'已停投历史',SYSTEM_HIGH_CONFIDENCE:'系统高证据',CODEX_REVIEW:'待 Codex 复核',ACTIVE_NO_EXPOSURE:'运行中无曝光',RECENT_ZERO_ORDER_DECAY:'历史出单后衰退',ZERO_ORDER_HIGH_SPEND:'高消耗零成交',ZERO_ORDER_HIGH_CLICK:'高点击零成交',SCALE_CANDIDATE:'可放量',BAD_ECONOMICS:'经济性不成立',LOW_CTR:'点击率偏低',LOW_ATC:'加购偏低',HIGH_CART_LOW_ORDER:'高加购低成交',LOW_CVR:'成交率偏低',COLD_START:'冷启动',TRAFFIC_STARVED:'流量不足',NO_METRIC_DATA:'无广告数据',OBSERVATION:'观察中',HEALTHY:'健康'} as any)[option]" :value="option" />
        </el-select>
        <el-select v-model="candidateAction" placeholder="全部候选动作" clearable style="width: 190px" @change="applyFilters">
          <el-option label="停止广告候选" value="STOP_ADS_CANDIDATE" />
          <el-option label="提高目标费用候选" value="INCREASE_TARGET_DRR_CANDIDATE" />
          <el-option label="提高预算候选" value="INCREASE_BUDGET_CANDIDATE" />
          <el-option label="排查无曝光" value="DIAGNOSE_NO_EXPOSURE" />
          <el-option label="优化商品卡" value="OPTIMIZE_LISTING" />
          <el-option label="优化素材" value="OPTIMIZE_CREATIVE" />
          <el-option label="检查报价" value="CHECK_OFFER" />
          <el-option label="继续观察" value="OBSERVE" />
          <el-option label="保持当前策略" value="HOLD" />
        </el-select>
        <el-button @click="applyFilters">查询</el-button>
        <span class="grow" />
        <el-button type="primary" :loading="syncing" :disabled="!storeId" @click="syncFromOzon"><RefreshCw :size="16" />从 Ozon 同步</el-button>
        <el-button :disabled="!storeId" @click="importOpen = true"><Upload :size="16" />导入历史数据</el-button>
        <el-button :loading="analyzing" :disabled="!storeId" @click="analyze"><RefreshCw :size="16" />重新分析</el-button>
        <el-button type="primary" :loading="backtesting" :disabled="!storeId || !data.summary.sku_count" @click="backtest">运行回测</el-button>
        <router-link v-if="lastJobId" to="/jobs">查看后台任务</router-link>
      </div>
    </section>

    <section v-if="!data.stores.length" class="panel empty-state">
      <strong>还没有可用的 OZON 店铺</strong>
      <p>先在店铺管理中添加 OZON 店铺，再导入广告历史数据。</p>
      <router-link to="/stores"><el-button type="primary">前往店铺管理</el-button></router-link>
    </section>

    <template v-else>
      <section class="ad-metric-grid">
        <div v-for="card in cards" :key="card.label" class="metric-card" :style="{ '--metric-color': card.color }">
          <div class="metric-icon"><component :is="card.icon" :size="18" /></div>
          <strong>{{ card.value }} <small>{{ card.unit }}</small></strong><span>{{ card.label }}</span>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><div><h2>SKU 广告诊断</h2><p>按 展示 → 点击 → 加购 → 成交 → 广告经济性 的顺序定位瓶颈</p></div><div class="batch-actions"><el-button type="danger" :loading="executing === 'STOP_ADS_CANDIDATE'" :disabled="Boolean(executing) || !selectedStopCount" @click="executeBatch('STOP_ADS_CANDIDATE')"><AlertTriangle :size="16" />停投所选 {{ selectedStopCount }}</el-button><el-button type="success" :loading="executing === 'SCALE_CANDIDATES'" :disabled="Boolean(executing) || !selectedScaleCount" @click="executeBatch('SCALE_CANDIDATES')"><Sparkles :size="16" />放量所选 {{ selectedScaleCount }}</el-button><span class="muted">30 天滚动窗口</span></div></div>
        <el-table :data="data.items" empty-text="暂无分析结果，请导入广告历史 CSV" @selection-change="selectionChanged">
          <el-table-column type="selection" width="48" :selectable="(row: AdItem) => ['STOP_ADS_CANDIDATE','INCREASE_TARGET_DRR_CANDIDATE'].includes(row.candidate_action) && row.execution_status !== 'STOPPED_EXECUTED'" />
          <el-table-column label="SKU / 商品" min-width="220">
            <template #default="s"><div class="ad-product-row"><el-image v-if="s.row.image_url" class="ad-product-image" :src="s.row.image_url" fit="cover" lazy :preview-src-list="[s.row.image_url]" preview-teleported /><div v-else class="ad-product-image placeholder">暂无图</div><div class="ad-product"><b class="mono">{{ s.row.sku_id }}</b><span>{{ s.row.name || '未填写商品名' }}</span><small>{{ s.row.category_path || '未分类' }}</small></div></div></template>
          </el-table-column>
          <el-table-column label="当前状态" width="155"><template #default="s"><el-tag v-if="s.row.execution_status === 'STOPPED_EXECUTED'" type="info" effect="dark">已停投</el-tag><el-tag v-else :type="stateType(s.row.state)" effect="light">{{ s.row.state_label }}</el-tag><small class="table-sub">{{ s.row.execution_status === 'STOPPED_EXECUTED' ? '保留历史数据' : `${s.row.secondary_flags.length} 个附加信号` }}</small></template></el-table-column>
          <el-table-column label="数据质量" width="145"><template #default="s"><el-tag size="small" :type="s.row.metrics.data_freshness === 'FRESH' ? 'success' : 'warning'">{{ freshnessLabel(s.row.metrics.data_freshness) }}</el-tag><small class="table-sub">截至 {{ s.row.metrics.last_metric_date || '—' }}</small></template></el-table-column>
          <el-table-column label="展示 / 点击" width="130"><template #default="s"><b>{{ number(s.row.metrics.impressions) }}</b><small class="table-sub">{{ number(s.row.metrics.clicks) }} 点击</small></template></el-table-column>
          <el-table-column label="CTR / CVR" width="130"><template #default="s"><b>{{ percent(s.row.metrics.ctr) }}</b><small class="table-sub">{{ percent(s.row.metrics.cvr) }} CVR</small></template></el-table-column>
          <el-table-column label="实际 / 盈亏 DRR" width="155"><template #default="s"><b>{{ percent(s.row.metrics.actual_drr) }}</b><small class="table-sub">{{ percent(s.row.financial_check.break_even_drr) }} 盈亏线</small></template></el-table-column>
          <el-table-column label="候选动作" min-width="220"><template #default="s"><b :class="{ 'danger-copy': s.row.action_risk === 'HIGH' }">{{ s.row.execution_status === 'STOPPED_EXECUTED' ? '已执行停投' : actionLabel(s.row.candidate_action) }}</b><small class="table-sub">{{ decisionTierLabel(s.row.decision_tier) }} · 证据 {{ evidenceLabel(s.row.evidence_strength) }}</small></template></el-table-column>
          <el-table-column label="历史结果" width="145"><template #default="s"><el-select :model-value="s.row.outcome_label" placeholder="未标记" clearable size="small" @change="(value: string) => updateOutcome(s.row, value || '')"><el-option label="成功" value="SUCCESS"/><el-option label="弱品" value="WEAK"/><el-option label="失败" value="FAILED"/><el-option label="无流量" value="NO_TRAFFIC"/><el-option label="外部停止" value="STOPPED_EXTERNALLY"/></el-select></template></el-table-column>
          <el-table-column label="操作" width="95" fixed="right"><template #default="s"><el-button link type="primary" @click="detail = s.row">看证据</el-button></template></el-table-column>
        </el-table>
        <div class="table-pagination"><el-pagination background layout="total, prev, pager, next" :current-page="data.pagination.page" :page-size="data.pagination.page_size" :total="data.pagination.total" @current-change="changePage" /></div>
      </section>

      <section class="split-grid">
        <div class="panel">
          <div class="panel-head"><div><h2>当前学习阈值</h2><p>来自成功 SKU 分布；样本不足时自动回退到上一级 Cohort</p></div></div>
          <el-table :data="thresholdRows" size="small" empty-text="成功 SKU 样本不足，暂不生成软阈值">
            <el-table-column prop="cohort_key" label="Cohort" min-width="260" show-overflow-tooltip />
            <el-table-column label="层级" width="95"><template #default="s">{{ cohortLevel(s.row.cohort_level) }}</template></el-table-column>
            <el-table-column label="成功样本" width="90"><template #default="s">{{ s.row.success_sample_size }}</template></el-table-column>
            <el-table-column prop="ctr" label="CTR P25" width="95" />
            <el-table-column prop="cvr" label="CVR P25" width="95" />
            <el-table-column prop="clicks" label="点击下界" width="95" />
            <el-table-column prop="cold" label="冷启动 P80" width="110"><template #default="s">{{ s.row.cold === '—' ? '—' : `${s.row.cold} 天` }}</template></el-table-column>
          </el-table>
        </div>
        <div class="panel backtest-card">
          <div class="panel-head"><div><h2>最近一次回测</h2><p>平衡节省费用与误杀风险</p></div></div>
          <div v-if="data.latest_backtest" class="backtest-body">
            <div><span>回放 SKU</span><strong>{{ data.latest_backtest.sample_size }}</strong></div>
            <div><span>比较策略</span><strong>{{ backtestOverview.strategyCount }}</strong></div>
            <div><span>单策略最多候选</span><strong>{{ backtestOverview.maxStops }}</strong></div>
            <div><span>最大停止后消耗</span><strong>{{ money(backtestOverview.maxSavings) }} ₽</strong></div>
            <small>逐日回放 · 仅统计停止日次日以后 · 阈值版本 {{ data.latest_backtest.threshold_version || '暂无' }}</small>
          </div>
          <div v-else class="empty-state small"><strong>尚未运行回测</strong><span>为历史 SKU 标记结果后即可验证规则。</span></div>
        </div>
      </section>

      <section v-if="backtestStrategyRows.length" class="panel">
        <div class="panel-head"><div><h2>逐日时间回放策略</h2><p>仅统计首次触发停止候选之后的消耗与后续订单，策略选择仍需人工确认</p></div><el-tag type="warning" effect="plain">REVIEW REQUIRED</el-tag></div>
        <el-table :data="backtestStrategyRows" size="small">
          <el-table-column prop="percentile" label="首单阈值" width="100" />
          <el-table-column prop="minimum_evidence" label="最少证据" width="100" />
          <el-table-column prop="threshold_available_days" label="阈值可用天数" width="125" />
          <el-table-column prop="stopped_sku_count" label="停止候选" width="100" />
          <el-table-column label="误杀率" width="100"><template #default="s">{{ percent(s.row.false_kill_rate) }}</template></el-table-column>
          <el-table-column prop="future_orders_lost" label="后续订单损失" width="120" />
          <el-table-column label="停止后消耗" min-width="130"><template #default="s">{{ money(s.row.future_spend_saved) }} ₽</template></el-table-column>
        </el-table>
      </section>
    </template>

    <el-dialog v-model="importOpen" title="导入 Ozon 广告历史数据" width="620px">
      <div class="import-guide">
        <p>每一行代表一个 SKU 在某一天的广告快照。系统会根据历史成功 SKU 学习阈值，导入后自动运行分析。</p>
        <ol><li>下载模板并填写数据</li><li>保留 <b>sku_id</b> 与 <b>metric_date</b> 两个必填列</li><li>历史已知结果可填 SUCCESS、WEAK 或 FAILED</li></ol>
        <a class="template-link" href="/api/v1/ad-optimization/import-template">下载 CSV 模板</a>
        <label class="file-picker"><Upload :size="20"/><span>{{ importFile?.name || '选择 CSV 文件' }}</span><input type="file" accept=".csv,text/csv" @change="fileChanged" /></label>
      </div>
      <template #footer><el-button @click="importOpen = false">取消</el-button><el-button type="primary" :loading="importing" :disabled="!importFile" @click="submitImport">导入并分析</el-button></template>
    </el-dialog>

    <el-drawer v-model="detailOpen" title="广告诊断证据" size="520px">
      <div v-if="detail" class="ad-detail">
        <div class="detail-title"><div><span class="mono">{{ detail.sku_id }}</span><h3>{{ detail.name || '未填写商品名' }}</h3></div><el-tag :type="stateType(detail.state)">{{ detail.state_label }}</el-tag></div>
        <section><h4>建议动作</h4><strong class="recommendation-text">{{ actionLabel(detail.candidate_action) }}</strong><div class="detail-tags"><el-tag :type="evidenceType(detail.evidence_strength)">{{ decisionTierLabel(detail.decision_tier) }}</el-tag><el-tag v-if="detail.review_owner === 'USER_CONFIRMATION'" type="warning">需人工确认</el-tag><el-tag v-if="detail.ready_for_shadow_action" type="success">安全门已通过</el-tag></div><p>{{ detail.reason }}</p><div v-if="detail.stop_evidence_flags.length" class="detail-tags"><el-tag v-for="flag in detail.stop_evidence_flags" :key="flag" type="danger" effect="plain">{{ flagLabel(flag) }}</el-tag></div></section>
        <section><h4>诊断与数据边界</h4><div class="detail-metrics"><div><span>主状态</span><b>{{ detail.state_label }}</b></div><div><span>数据成熟度</span><b>{{ detail.data_maturity }}</b></div><div><span>数据新鲜度</span><b>{{ freshnessLabel(detail.metrics.data_freshness) }}</b></div><div><span>分析截止日</span><b>{{ detail.metrics.analysis_as_of || '—' }}</b></div><div><span>最后数据日</span><b>{{ detail.metrics.last_metric_date || '—' }}</b></div><div><span>距截止日</span><b>{{ number(detail.metrics.days_since_last_metric) }} 天</b></div></div><div v-if="detail.secondary_flags.length" class="detail-tags"><el-tag v-for="flag in detail.secondary_flags" :key="flag" type="warning" effect="plain">{{ flagLabel(flag) }}</el-tag></div></section>
        <section><h4>判断证据</h4><ul><li v-for="line in detail.evidence" :key="line">{{ line }}</li></ul></section>
        <section><h4>使用的比较组</h4><p>{{ detail.cohort_key }}</p><small>{{ cohortLevel(detail.cohort_level) }} · 总样本 {{ detail.metrics.cohort?.total_samples || 0 }} · 成功样本 {{ detail.metrics.cohort?.success_samples || 0 }} · 失败标签 {{ detail.metrics.cohort?.failure_samples || 0 }}</small></section>
        <section><h4>证据强度组成</h4><div class="detail-metrics"><div v-for="(value, key) in detail.metrics.evidence_strength_components" :key="key"><span>{{ gateLabel(String(key)) }}</span><b>{{ evidenceLabel(String(value)) }}</b></div></div></section>
        <section><h4>安全门</h4><div class="gate-list"><div v-for="(passed, key) in detail.safety_gate" :key="key"><span>{{ gateLabel(String(key)) }}</span><el-tag size="small" :type="passed ? 'success' : 'danger'">{{ passed ? '通过' : '阻止' }}</el-tag></div></div><div v-if="detail.risk_flags.length" class="detail-tags"><el-tag v-for="flag in detail.risk_flags" :key="flag" type="danger" effect="plain">{{ flagLabel(flag) }}</el-tag></div></section>
        <section><h4>首单生存估计</h4><div class="detail-metrics"><div><span>首单历史</span><b>{{ detail.metrics.survival?.first_order_history_quality || 'UNKNOWN' }}</b></div><div><span>当前点击分位</span><b>{{ percent(detail.metrics.survival?.clicks_to_first_order_percentile) }}</b></div><div><span>未来首单概率</span><b>{{ percent(detail.metrics.survival?.estimated_future_order_probability) }}</b></div><div><span>可用样本</span><b>{{ number(detail.metrics.survival?.eligible_sample_size) }}</b></div><div><span>首单事件</span><b>{{ number(detail.metrics.survival?.event_count) }}</b></div><div><span>右删失样本</span><b>{{ number(detail.metrics.survival?.right_censored_count) }}</b></div></div></section>
        <section v-if="detail.campaign_sku_actions.length"><h4>Campaign + SKU 动作范围</h4><el-alert v-if="detail.multi_campaign_conflict" title="同一 SKU 位于多个广告计划，必须分别审核，不能使用 SKU 平均目标费用执行。" type="warning" :closable="false" show-icon /><el-table :data="detail.campaign_sku_actions" size="small"><el-table-column prop="campaign_name" label="广告计划" min-width="150"><template #default="s">{{ s.row.campaign_name || s.row.campaign_id }}</template></el-table-column><el-table-column label="当前目标 DRR" width="125"><template #default="s">{{ percent(s.row.current_target_drr) }}</template></el-table-column><el-table-column label="拟议目标" width="105"><template #default="s">{{ percent(s.row.proposed_target_drr) }}</template></el-table-column></el-table></section>
        <section><h4>财务红线检查</h4><div class="detail-metrics"><div><span>实际 DRR</span><b>{{ percent(detail.financial_check.actual_drr) }}</b></div><div><span>计划 DRR</span><b>{{ percent(detail.financial_check.planned_ad_drr) }}</b></div><div><span>有效盈亏 DRR</span><b>{{ percent(detail.financial_check.effective_break_even_drr) }}</b></div><div><span>保守最低剩余利润</span><b>{{ percent(detail.financial_check.estimated_min_remaining_margin) }}</b></div><div><span>经营区间</span><b>{{ detail.financial_check.financial_status || 'UNKNOWN' }}</b></div><div><span>模型来源 / 可信度</span><b>{{ detail.financial_check.source }} / {{ detail.financial_check.confidence }}</b></div><div><span>经济生存 CVR</span><b>{{ percent(detail.financial_check.break_even_cvr) }}</b></div></div></section>
        <el-button v-if="!['OBSERVE','HOLD'].includes(detail.candidate_action)" type="primary" :loading="actionSaving" @click="saveProposal">保存为待审批建议</el-button>
        <p class="shadow-note">保存建议只会写入本机审计记录，不会调用 Ozon 广告接口。</p>
      </div>
    </el-drawer>
  </div>
</template>
