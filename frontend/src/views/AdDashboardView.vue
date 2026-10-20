<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowRight, CalendarDays, Database, Info, RefreshCw, Search, ShieldCheck } from 'lucide-vue-next'
import { api } from '../api'

interface SkuOrderRow {
  sku_id: string; name: string; seller_data_available: boolean
  total_orders: number | null; ad_orders: number; estimated_organic_orders: number | null
  total_sales: number | null; ad_sales: number; estimated_organic_sales: number | null
  cancellations: number | null; returns: number | null; delivered_units: number | null
  ad_order_share: number | null; ad_spend: number; ad_drr: number | null; ad_spend_to_total_sales: number | null
  impressions: number; clicks: number; attribution_gap: number; order_profile: string
}

interface Dashboard {
  stores: { id: string; name: string }[]; store_id: string; period: string; date_from?: string; date_to?: string
  totals: Record<string, number | null>; trend: { date: string; ad_spend: number; ad_sales: number; orders: number }[]
  funnel: Record<string, number | null>; campaigns: any[]; campaign_count: number
  order_overview: Record<string, any>; sku_orders: SkuOrderRow[]
  profitability: Record<string, any>
}

const empty: Dashboard = { stores: [], store_id: '', period: '7d', totals: {}, trend: [], funnel: {}, campaigns: [], campaign_count: 0, order_overview: {}, sku_orders: [], profitability: {} }
const data = ref<Dashboard>({ ...empty })
const storeId = ref('')
const period = ref('7d')
const loading = ref(false)
const syncing = ref(false)
const lastJobId = ref('')
const orderSearch = ref('')
const orderProfile = ref('')
const orderPage = ref(1)
const orderPageSize = 50

const metrics = computed(() => [
  { label: '广告消耗', value: money(data.value.totals.ad_spend), unit: '₽', hint: '活动总计' },
  { label: '归因销售额', value: money(data.value.totals.ad_sales), unit: '₽', hint: '广告归因收入' },
  { label: '已售商品件数', value: number(data.value.totals.sold_qty), unit: '', hint: '推广归因商品件数' },
  { label: '平均 CTR', value: percent(data.value.totals.ctr), unit: '', hint: '曝光至点击' },
  { label: '平均 CPC', value: moneyOrDash(data.value.totals.cpc), unit: data.value.totals.cpc == null ? '' : '₽', hint: '单次点击费用' },
  { label: '投产比 ROAS', value: ratio(data.value.totals.roas), unit: '', hint: '销售额 / 消耗' },
  { label: '广告 ACOS', value: percent(data.value.totals.acos), unit: '', hint: '消耗 / 归因销售额' },
  { label: '点击转化率', value: percent(data.value.totals.cvr), unit: '', hint: '推广归因商品件数 / 点击' },
  { label: '单件推广成本 CPA', value: moneyOrDash(data.value.totals.cpa), unit: data.value.totals.cpa == null ? '' : '₽', hint: '消耗 / 推广归因商品件数' },
])

const orderMetrics = computed(() => {
  const value = data.value.order_overview
  return [
    { label: '全部下单件数', value: numberOrDash(value.total_orders), hint: '广告与自然流量合计' },
    { label: '广告归因件数', value: numberOrDash(value.ad_orders), hint: '仅用于广告效率计算' },
    { label: '预估自然件数', value: numberOrDash(value.estimated_organic_orders), hint: '总件数减广告归因件数' },
    { label: '妥投件数', value: numberOrDash(value.delivered_units), hint: '卖家订单履约结果' },
    { label: '取消 / 退货', value: value.cancellations == null ? '—' : `${number(value.cancellations)} / ${number(value.returns)}`, hint: '分别显示取消与退货件数' },
    { label: '广告订单占比', value: percent(value.ad_order_share), hint: '观察商品对广告的依赖程度' },
    { label: '全部销售额', value: value.total_sales == null ? '—' : `${money(value.total_sales)} ₽`, hint: '卖家端统计的商品销售额' },
    { label: '广告费 / 全部销售额', value: percent(value.ad_spend_to_total_sales), hint: '商品整体广告负担，不替代广告 DRR' },
  ]
})

const filteredSkuOrders = computed(() => {
  const keyword = orderSearch.value.trim().toLocaleLowerCase()
  return data.value.sku_orders.filter(row => {
    const matchesKeyword = !keyword || row.sku_id.toLocaleLowerCase().includes(keyword) || row.name.toLocaleLowerCase().includes(keyword)
    return matchesKeyword && (!orderProfile.value || row.order_profile === orderProfile.value)
  })
})

const pagedSkuOrders = computed(() => filteredSkuOrders.value.slice((orderPage.value - 1) * orderPageSize, orderPage.value * orderPageSize))

const chart = computed(() => {
  const rows = data.value.trend
  const maxMoney = Math.max(1, ...rows.flatMap(row => [row.ad_spend, row.ad_sales]))
  const maxOrders = Math.max(1, ...rows.map(row => row.orders))
  const x = (index: number) => rows.length <= 1 ? 50 : 40 + index * (920 / (rows.length - 1))
  const line = (key: 'ad_sales' | 'orders') => rows.map((row, index) => `${x(index)},${270 - (key === 'orders' ? row.orders / maxOrders : row[key] / maxMoney) * 220}`).join(' ')
  return { sales: line('ad_sales'), orders: line('orders'), maxMoney, maxOrders }
})

async function load() {
  loading.value = true
  try {
    const query = new URLSearchParams({ period: period.value })
    if (storeId.value) query.set('store_id', storeId.value)
    data.value = await api.get<Dashboard>(`/ad-optimization/dashboard?${query}`)
    if (!storeId.value && data.value.store_id) storeId.value = data.value.store_id
    orderPage.value = 1
  } finally { loading.value = false }
}
async function sync() {
  if (!storeId.value) return ElMessage.warning('请先选择 OZON 店铺')
  syncing.value = true
  try {
    const result = await api.post<{job_id:string;already_running:boolean}>(`/ad-optimization/jobs/sync?store_id=${encodeURIComponent(storeId.value)}&days=62`)
    lastJobId.value = result.job_id
    ElMessage.success(result.already_running ? '该店铺已有广告同步任务运行中' : '广告同步任务已进入任务中心，可继续操作')
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
function money(value: any) { return Number(value || 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }
function moneyOrDash(value: any) { return value == null ? '—' : money(value) }
function number(value: any) { return Number(value || 0).toLocaleString('zh-CN') }
function numberOrDash(value: any) { return value == null ? '—' : number(value) }
function percent(value: any) { return value == null ? '—' : `${(Number(value) * 100).toFixed(2)}%` }
function ratio(value: any) { return value == null ? '—' : Number(value).toFixed(2) }
function shortDate(value?: string) { return value ? value.slice(5).replace('-', '/') : '—' }
function profileLabel(value: string) { return ({ ATTRIBUTION_MISMATCH: '归因口径异常', NO_ORDERS: '暂无订单', ORGANIC_ONLY: '仅自然出单', AD_ONLY: '仅广告出单', AD_DEPENDENT: '广告依赖', ORGANIC_DOMINANT: '自然为主', MIXED: '广告自然混合', SELLER_DATA_MISSING: '总订单待同步' } as Record<string, string>)[value] || value }
function profileType(value: string) { return ({ ATTRIBUTION_MISMATCH: 'danger', AD_DEPENDENT: 'warning', ORGANIC_DOMINANT: 'success', ORGANIC_ONLY: 'success', SELLER_DATA_MISSING: 'info', NO_ORDERS: 'info' } as Record<string, string>)[value] || '' }
function resetOrderPage() { orderPage.value = 1 }

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="ad-dashboard">
    <section class="ad-dashboard-controls">
      <el-select v-model="storeId" placeholder="选择 OZON 店铺" @change="load"><el-option v-for="store in data.stores" :key="store.id" :label="store.name" :value="store.id" /></el-select>
      <el-radio-group v-model="period" @change="load"><el-radio-button value="7d">最近 7 天</el-radio-button><el-radio-button value="30d">最近 30 天</el-radio-button><el-radio-button value="quarter">本季度</el-radio-button></el-radio-group>
      <div class="grow" />
      <span class="date-range"><CalendarDays :size="16" />{{ data.date_from }} 至 {{ data.date_to }}</span>
      <el-button :loading="loading" @click="load"><RefreshCw :size="16" />刷新数据</el-button>
      <el-button type="primary" :loading="syncing" @click="sync"><Database :size="16" />从 Ozon 同步</el-button><router-link v-if="lastJobId" to="/jobs">查看同步任务</router-link>
      <router-link to="/ad-optimization/skus"><el-button>SKU 诊断<ArrowRight :size="16" /></el-button></router-link>
    </section>

    <section class="ad-dashboard-panel trend-panel">
      <div class="section-heading"><div><h2>广告表现趋势</h2><p>按所选日期观察消耗、归因销售额和已售商品件数</p></div><div class="chart-legend"><span class="spend">广告消耗</span><span class="sales">归因销售额</span><span class="orders">已售商品件数</span></div></div>
      <div class="trend-chart">
        <svg viewBox="0 0 1000 310" preserveAspectRatio="none" role="img" aria-label="广告表现趋势图">
          <line v-for="y in [50,105,160,215,270]" :key="y" x1="40" :y1="y" x2="960" :y2="y" class="grid-line" />
          <g v-for="(row, index) in data.trend" :key="row.date">
            <rect :x="data.trend.length <= 1 ? 45 : 42 + index * (920 / Math.max(data.trend.length - 1, 1))" :y="270 - row.ad_spend / chart.maxMoney * 220" :width="Math.max(4, Math.min(18, 700 / Math.max(data.trend.length, 1)))" :height="row.ad_spend / chart.maxMoney * 220" rx="2" class="spend-bar" />
          </g>
          <polyline :points="chart.sales" class="sales-line" />
          <polyline :points="chart.orders" class="orders-line" />
        </svg>
        <div class="chart-axis"><span>{{ shortDate(data.trend[0]?.date) }}</span><span>{{ shortDate(data.trend[Math.floor(data.trend.length / 2)]?.date) }}</span><span>{{ shortDate(data.trend[data.trend.length - 1]?.date) }}</span></div>
      </div>
    </section>

    <section class="ad-kpi-grid"><article v-for="item in metrics" :key="item.label"><span>{{ item.label }}</span><strong>{{ item.value }} <small>{{ item.unit }}</small></strong><em>{{ item.hint }}</em></article></section>

    <section class="ad-dashboard-panel order-overview-panel">
      <div class="section-heading">
        <div><h2>商品订单全貌</h2><p>总订单用于了解商品价值，广告归因订单仍单独计算广告效率</p></div>
        <span class="coverage-badge"><ShieldCheck :size="15" />卖家数据 {{ data.order_overview.coverage_count || 0 }} / {{ data.order_overview.sku_count || 0 }} SKU</span>
      </div>
      <template v-if="data.order_overview.available">
        <div class="order-kpi-grid"><article v-for="item in orderMetrics" :key="item.label"><span>{{ item.label }}</span><strong>{{ item.value }}</strong><small>{{ item.hint }}</small></article></div>
        <div class="order-note" :class="{ warning: !data.order_overview.complete || data.order_overview.attribution_mismatch_count }">
          <Info :size="17" />
          <span>预估自然订单＝同周期全部下单件数－广告归因件数，仅用于订单结构观察，不进入 SKU 诊断的 CVR、CPA 或 DRR。</span>
          <b v-if="data.order_overview.attribution_mismatch_count">{{ data.order_overview.attribution_mismatch_count }} 个 SKU 存在归因日期差异</b>
        </div>
      </template>
      <el-empty v-else description="当前周期的卖家订单数据尚未同步">
        <el-button type="primary" :loading="syncing" @click="sync">同步总订单数据</el-button>
      </el-empty>
    </section>

    <section class="ad-dashboard-panel sku-order-panel">
      <div class="section-heading order-table-heading">
        <div><h2>SKU 订单结构</h2><p>对照广告订单与预估自然订单，不改变 SKU 诊断结果</p></div>
        <div class="order-filters">
          <el-input v-model="orderSearch" clearable placeholder="搜索 SKU 或商品名称" @input="resetOrderPage"><template #prefix><Search :size="15" /></template></el-input>
          <el-select v-model="orderProfile" clearable placeholder="全部订单结构" @change="resetOrderPage">
            <el-option label="自然为主" value="ORGANIC_DOMINANT" /><el-option label="仅自然出单" value="ORGANIC_ONLY" />
            <el-option label="广告自然混合" value="MIXED" /><el-option label="广告依赖" value="AD_DEPENDENT" />
            <el-option label="仅广告出单" value="AD_ONLY" /><el-option label="暂无订单" value="NO_ORDERS" />
            <el-option label="归因口径异常" value="ATTRIBUTION_MISMATCH" /><el-option label="总订单待同步" value="SELLER_DATA_MISSING" />
          </el-select>
        </div>
      </div>
      <el-table :data="pagedSkuOrders" row-key="sku_id" empty-text="没有符合条件的 SKU">
        <el-table-column label="SKU / 商品" min-width="270" fixed><template #default="s"><div class="campaign-name"><b>{{ s.row.name || '未命名商品' }}</b><span>SKU {{ s.row.sku_id }}</span></div></template></el-table-column>
        <el-table-column label="订单结构" min-width="190"><template #default="s"><div class="order-structure"><b>{{ numberOrDash(s.row.total_orders) }} 总订单</b><span>广告 {{ number(s.row.ad_orders) }} · 自然 {{ numberOrDash(s.row.estimated_organic_orders) }}</span></div></template></el-table-column>
        <el-table-column label="订单类型" width="130"><template #default="s"><el-tag size="small" effect="light" :type="profileType(s.row.order_profile)">{{ profileLabel(s.row.order_profile) }}</el-tag></template></el-table-column>
        <el-table-column label="销售额" min-width="165"><template #default="s"><div class="order-structure"><b>{{ s.row.total_sales == null ? '—' : `${money(s.row.total_sales)} ₽` }}</b><span>广告归因 {{ money(s.row.ad_sales) }} ₽</span></div></template></el-table-column>
        <el-table-column label="妥投" width="90"><template #default="s">{{ numberOrDash(s.row.delivered_units) }}</template></el-table-column>
        <el-table-column label="取消 / 退货" width="125"><template #default="s">{{ numberOrDash(s.row.cancellations) }} / {{ numberOrDash(s.row.returns) }}</template></el-table-column>
        <el-table-column label="广告订单占比" width="125"><template #default="s">{{ percent(s.row.ad_order_share) }}</template></el-table-column>
        <el-table-column label="广告消耗 / DRR" min-width="155"><template #default="s"><div class="order-structure"><b>{{ money(s.row.ad_spend) }} ₽</b><span>广告 DRR {{ percent(s.row.ad_drr) }}</span></div></template></el-table-column>
      </el-table>
      <div class="order-pagination"><span>共 {{ filteredSkuOrders.length }} 个 SKU</span><el-pagination v-model:current-page="orderPage" background layout="prev, pager, next" :page-size="orderPageSize" :total="filteredSkuOrders.length" /></div>
    </section>

    <section class="ad-dashboard-panel profitability-panel">
      <div class="section-heading"><div><h2>利润约束与投放边界</h2><p>10% 是计划广告费率；25% 是由定价规则推导的估算盈亏线，不是逐 SKU 精确成本</p></div><span class="coverage-badge"><ShieldCheck :size="15" />{{ data.profitability.financial_model_source || 'STORE_DEFAULT' }} · {{ data.profitability.financial_confidence || 'MEDIUM' }}</span></div>
      <div class="profit-grid">
        <div><span>计划广告 DRR</span><strong>{{ percent(data.profitability.planned_ad_drr) }}</strong><small>定价时预留的广告费用率</small></div>
        <div><span>计划后最低利润</span><strong>{{ percent(data.profitability.minimum_profit_margin_after_planned_ads) }}</strong><small>扣除计划广告费后的最低利润率</small></div>
        <div><span>估算盈亏 DRR</span><strong>{{ percent(data.profitability.estimated_break_even_drr) }}</strong><small>店铺默认理论极限，置信度中等</small></div>
        <div><span>精确成本覆盖</span><strong>{{ data.profitability.exact_count || 0 }} / {{ data.profitability.sku_count || 0 }}</strong><small>其余 SKU 使用 25% 店铺回退值</small></div>
      </div>
    </section>

    <section class="dashboard-split">
      <div class="ad-dashboard-panel data-scope">
        <div class="section-heading"><div><h2>真实广告数据范围</h2><p>来自当前店铺的 Ozon Performance API，不使用演示数据</p></div><span class="real-badge">真实汇总</span></div>
        <div class="scope-summary"><Database :size="22" /><p>当前周期消耗 <b>{{ money(data.totals.ad_spend) }} ₽</b>，归因销售额 <b>{{ money(data.totals.ad_sales) }} ₽</b>，覆盖 <b>{{ data.campaign_count || 0 }}</b> 个广告计划。</p></div>
      </div>
      <div class="ad-dashboard-panel funnel-panel">
        <div class="section-heading"><div><h2>广告转化漏斗</h2><p>由真实曝光、点击与推广归因商品件数生成</p></div></div>
        <div class="funnel-step exposure"><span>曝光</span><b>{{ number(data.funnel.impressions) }}</b><small>100%</small></div>
        <div class="funnel-step clicks"><span>点击</span><b>{{ number(data.funnel.clicks) }}</b><small>{{ percent(data.funnel.click_rate) }}</small></div>
        <div class="funnel-step orders"><span>推广归因商品件数</span><b>{{ number(data.funnel.orders) }}</b><small>{{ percent(data.funnel.order_rate) }}</small></div>
      </div>
    </section>

    <section class="ad-dashboard-panel campaign-panel">
      <div class="section-heading"><div><h2>广告计划表现</h2><p>按广告消耗排序，快速查看计划投入与回报</p></div><span>{{ data.campaign_count || 0 }} 个计划</span></div>
      <el-table :data="data.campaigns" empty-text="同步 Ozon 后显示真实广告计划数据">
        <el-table-column label="广告计划" min-width="260"><template #default="s"><div class="campaign-name"><b>{{ s.row.name || `广告计划 ${s.row.campaign_id}` }}</b><span>ID {{ s.row.campaign_id }}</span></div></template></el-table-column>
        <el-table-column label="消耗" width="130"><template #default="s">{{ money(s.row.ad_spend) }} ₽</template></el-table-column>
        <el-table-column label="归因销售额" width="150"><template #default="s">{{ money(s.row.ad_sales) }} ₽</template></el-table-column>
        <el-table-column label="已售件数" prop="orders" width="100" />
        <el-table-column label="CTR" width="100"><template #default="s">{{ percent(s.row.ctr) }}</template></el-table-column>
        <el-table-column label="ROAS" width="100"><template #default="s">{{ ratio(s.row.roas) }}</template></el-table-column>
        <el-table-column label="ACOS" width="110"><template #default="s">{{ percent(s.row.acos) }}</template></el-table-column>
      </el-table>
    </section>
  </div>
</template>

<style scoped>
.ad-dashboard{display:grid;gap:16px}.ad-dashboard-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.ad-dashboard-controls .el-select{width:180px}.date-range{display:flex;align-items:center;gap:7px;color:#64748b;font-size:13px}.ad-dashboard-panel{background:#fff;border:1px solid #dfe6ef;border-radius:8px;padding:18px;box-shadow:0 7px 22px rgba(32,55,88,.05)}.section-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:14px}.section-heading h2{margin:0;color:#10233f;font-size:16px}.section-heading p{margin:5px 0 0;color:#7b8ba3;font-size:13px}.chart-legend{display:flex;gap:18px;color:#536176;font-size:12px}.chart-legend span:before{content:'';display:inline-block;width:18px;height:4px;border-radius:4px;margin-right:6px;vertical-align:middle}.chart-legend .spend:before{background:#4978eb}.chart-legend .sales:before{background:#82b317}.chart-legend .orders:before{background:#596177}.trend-chart{height:270px}.trend-chart svg{width:100%;height:245px;overflow:visible}.grid-line{stroke:#e7ebf1;stroke-width:1}.spend-bar{fill:#5b82ea;opacity:.55}.sales-line,.orders-line{fill:none;stroke-width:3;vector-effect:non-scaling-stroke}.sales-line{stroke:#8fbd19}.orders-line{stroke:#596177}.chart-axis{display:flex;justify-content:space-between;padding:0 3.5%;color:#8b97a8;font-size:12px}.ad-kpi-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px}.ad-kpi-grid article{min-height:100px;background:#fff;border:1px solid #dfe6ef;border-radius:8px;padding:15px;display:flex;flex-direction:column}.ad-kpi-grid span{color:#71809a;font-size:12px}.ad-kpi-grid strong{margin-top:9px;color:#0f2342;font-size:23px}.ad-kpi-grid strong small{font-size:13px}.ad-kpi-grid em{margin-top:auto;color:#119a79;font-size:11px;font-style:normal}.coverage-badge,.real-badge{display:flex;align-items:center;gap:5px;background:#fff1d9;color:#a76308;border-radius:16px;padding:6px 10px;font-size:12px;white-space:nowrap}.order-kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.order-kpi-grid article{min-height:100px;padding:14px;border-radius:8px;background:#f6f8fc;display:flex;flex-direction:column}.order-kpi-grid span{color:#64748b;font-size:12px}.order-kpi-grid strong{margin:8px 0;color:#142c4d;font-size:21px}.order-kpi-grid small{margin-top:auto;color:#8794a7;font-size:11px}.order-note{display:flex;align-items:center;gap:8px;margin-top:12px;padding:11px 13px;border-radius:7px;background:#edf8f5;color:#27745f;font-size:12px}.order-note.warning{background:#fff6e8;color:#9a6211}.order-note b{margin-left:auto;white-space:nowrap}.order-table-heading{align-items:center}.order-filters{display:flex;gap:8px}.order-filters .el-input{width:240px}.order-filters .el-select{width:170px}.order-structure{display:flex;flex-direction:column;line-height:1.45}.order-structure b{color:#173052}.order-structure span{color:#8693a5;font-size:11px}.order-pagination{display:flex;align-items:center;justify-content:space-between;margin-top:14px;color:#7b889b;font-size:12px}.profit-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.profit-grid>div{background:#f5f7fb;border-radius:7px;padding:13px;display:flex;flex-direction:column}.profit-grid span,.profit-grid small{color:#78879d;font-size:11px}.profit-grid strong{font-size:18px;color:#173052;margin:6px 0}.dashboard-split{display:grid;grid-template-columns:1.45fr .75fr;gap:16px}.real-badge{background:#e9f2ff;color:#2870db}.scope-summary{min-height:150px;display:flex;align-items:center;justify-content:center;gap:14px;color:#65809f}.scope-summary b{color:#15304f}.funnel-panel{display:flex;flex-direction:column}.funnel-step{align-self:center;display:grid;grid-template-columns:1fr auto;align-items:center;padding:12px 16px;border-radius:7px;margin-bottom:8px;color:#132846}.funnel-step small{grid-column:1;color:#657694}.funnel-step b{grid-row:1/3;grid-column:2}.funnel-step.exposure{width:86%;background:#eaf1ff}.funnel-step.clicks{width:70%;background:#f0eaff}.funnel-step.orders{width:54%;background:#e4f5ef}.campaign-name{display:flex;flex-direction:column}.campaign-name span{color:#8a97aa;font-size:11px;margin-top:3px}@media(max-width:1100px){.ad-kpi-grid{grid-template-columns:repeat(3,1fr)}.order-kpi-grid{grid-template-columns:repeat(2,1fr)}.dashboard-split{grid-template-columns:1fr}.order-table-heading{align-items:flex-start;flex-direction:column}.order-filters{width:100%}.order-filters .el-input{flex:1}}@media(max-width:700px){.ad-kpi-grid,.profit-grid,.order-kpi-grid{grid-template-columns:repeat(2,1fr)}.chart-legend{display:none}.date-range{width:100%}.trend-chart{height:220px}.trend-chart svg{height:195px}.order-filters{flex-direction:column}.order-filters .el-input,.order-filters .el-select{width:100%}.order-note{align-items:flex-start}.order-note b{margin-left:0}}
</style>
