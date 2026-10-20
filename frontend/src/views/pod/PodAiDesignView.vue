<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../../api'
import { sharedProductsPilot } from '../../deployment'

type Row = Record<string, any>
const loading = ref(false)
const status = ref<Row>({ runtime: {}, connections: [], batches: [] })
const current = ref<Row | null>(null)
const references = ref<File[]>([])
const upscaleFiles = ref<File[]>([])
const upscaleScale = ref(2)
let timer = 0

const form = reactive({
  brief: '生成适合俄罗斯消费者、可用于桌布和毛毯高清印刷的独立平面图案，主体清晰，边缘完整。',
  product_type: '桌布 / 毛毯', count: 4, aspect_ratio: '4:3', max_edge: 3264,
  quality: 'high', background: 'opaque', output_format: 'png', upscale_scale: 2,
  planner_connection_id: '', image_connection_id: '', planner_model: 'gpt-5.6-sol',
  image_model: 'gpt-image-2-openai', custom_instruction: '',
})
const plannerConnections = computed(() => status.value.connections?.filter((row: Row) => row.provider === 'ai_chat') || [])
const imageConnections = computed(() => status.value.connections?.filter((row: Row) => row.provider === 'ai_image') || [])
const planItems = computed(() => current.value?.plan?.items || [])
const activeJobId = computed(() => current.value?.generation_job_id || current.value?.plan_job_id || current.value?.job_id || '')

function selectFiles(event: Event, target: 'references' | 'upscale') {
  const files = Array.from((event.target as HTMLInputElement).files || [])
  if (target === 'references') references.value = files.slice(0, 16)
  else upscaleFiles.value = files.slice(0, 30)
}
async function loadStatus() {
  status.value = await api.get('/pod/ai-design/status')
  if (!form.planner_connection_id && plannerConnections.value[0]) form.planner_connection_id = plannerConnections.value[0].id
  if (!form.image_connection_id && imageConnections.value[0]) form.image_connection_id = imageConnections.value[0].id
}
async function refreshCurrent() {
  if (!current.value?.id) return
  current.value = await api.get(`/pod/ai-design/batches/${current.value.id}`)
}
async function openBatch(row: Row) { current.value = await api.get(`/pod/ai-design/batches/${row.id}`) }
async function addInputs(data:FormData, files:File[], field:string){
  if(!sharedProductsPilot.value){files.forEach(file=>data.append(field,file));return}
  const ids:string[]=[]
  for(const file of files){const form=new FormData();form.append('image',file);const result=await api.upload<Row>('/pod/ai-design/uploads',form);ids.push(result.id)}
  data.append('uploaded_inputs',JSON.stringify(ids))
}
async function createBatch() {
  if (!form.planner_connection_id || !form.image_connection_id) return ElMessage.warning('请先在接口中心配置并启用文字 AI 与生图 AI 连接')
  loading.value = true
  try {
    const data = new FormData()
    Object.entries(form).forEach(([key, value]) => data.append(key, String(value)))
    await addInputs(data,references.value,'references')
    const result = await api.upload<Row>('/pod/ai-design/batches', data)
    current.value = result.batch
    ElMessage.success('已进入 AI 规划队列')
    await loadStatus()
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { loading.value = false }
}
async function savePlan() {
  if (!current.value) return
  loading.value = true
  try {
    current.value = await api.patch(`/pod/ai-design/batches/${current.value.id}/plan`, { items: planItems.value.map((row: Row) => ({ index: row.index, title: row.title, concept: row.concept || '', prompt: row.prompt, negative_prompt: row.negative_prompt || '', selected: row.selected !== false })) })
    ElMessage.success('规划修改已保存')
    return true
  } catch (error) { ElMessage.error((error as Error).message);return false }
  finally { loading.value = false }
}
async function generate() {
  if (!current.value) return
  loading.value = true
  try {
    if(!await savePlan())return
    const result = await api.post<Row>(`/pod/ai-design/batches/${current.value.id}/generate`)
    current.value = result.batch
    ElMessage.success('批量生图与超分任务已进入队列')
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { loading.value = false }
}
async function runUpscale() {
  if (!upscaleFiles.value.length) return ElMessage.warning('请选择需要放大的图片')
  loading.value = true
  try {
    const data = new FormData(); data.append('scale', String(upscaleScale.value))
    await addInputs(data,upscaleFiles.value,'images')
    const result = await api.upload<Row>('/pod/ai-design/upscale', data)
    current.value = result.batch
    ElMessage.success('本地超分任务已进入队列，不消耗 AI Token')
    await loadStatus()
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { loading.value = false }
}
function formatDate(value: string) { return value ? new Date(value).toLocaleString('zh-CN') : '-' }
function statusText(value: string) { return ({ planning:'规划中', planned:'待确认规划', queued:'排队中', generating:'生成中', completed:'已完成', failed:'失败' } as Row)[value] || value }

onMounted(async () => {
  try { await loadStatus() } catch (error) { ElMessage.error((error as Error).message) }
  timer = window.setInterval(async () => {
    try { if (current.value && !['completed', 'failed', 'planned'].includes(current.value.status)) await refreshCurrent(); await loadStatus() } catch {}
  }, 3500)
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<template>
  <section class="ai-workbench" v-loading="loading">
    <el-alert v-if="!status.runtime?.available" title="本地 Real-ESRGAN 运行时不可用，生图可以规划，但超分任务无法执行。" type="error" :closable="false" />
    <div class="hero">
      <div><span class="kicker">POD PRINT LAB</span><h2>AI 生图超分</h2><p>从设计规划、参考图生图到本地 2×/4× 超分，结果保存在 ERP 并进入任务中心。单独超分不调用 AI 接口。</p></div>
      <div class="runtime"><b>{{ status.runtime?.available ? '本地引擎就绪' : '引擎未就绪' }}</b><span>{{ status.runtime?.model || 'Real-ESRGAN' }}</span><small>单张输出 ≤ 30 MB</small></div>
    </div>

    <el-tabs type="border-card">
      <el-tab-pane label="AI 生图 + 自动超分">
        <el-form label-position="top" class="form-grid">
          <el-form-item class="wide" label="设计需求"><el-input v-model="form.brief" type="textarea" :rows="4" /></el-form-item>
          <el-form-item label="产品载体"><el-input v-model="form.product_type" /></el-form-item>
          <el-form-item label="设计数量"><el-input-number v-model="form.count" :min="1" :max="30" /></el-form-item>
          <el-form-item label="画布比例"><el-select v-model="form.aspect_ratio" allow-create filterable><el-option v-for="value in ['1:1','4:3','3:4','16:9','9:16','2:3','3:2']" :key="value" :value="value" /></el-select></el-form-item>
          <el-form-item label="基础最长边"><el-input-number v-model="form.max_edge" :min="256" :max="3840" :step="16" /></el-form-item>
          <el-form-item label="生成质量"><el-select v-model="form.quality"><el-option v-for="value in ['high','medium','low','auto']" :key="value" :value="value" /></el-select></el-form-item>
          <el-form-item label="背景"><el-select v-model="form.background"><el-option label="不透明" value="opaque"/><el-option label="透明" value="transparent"/><el-option label="自动" value="auto"/></el-select></el-form-item>
          <el-form-item label="输出格式"><el-select v-model="form.output_format"><el-option label="PNG" value="png"/><el-option label="JPEG" value="jpeg"/><el-option label="WebP" value="webp"/></el-select></el-form-item>
          <el-form-item label="生成后超分"><el-radio-group v-model="form.upscale_scale"><el-radio-button :value="0">关闭</el-radio-button><el-radio-button :value="2">2×</el-radio-button><el-radio-button :value="4">4×</el-radio-button></el-radio-group></el-form-item>
          <el-form-item label="规划 AI 连接"><el-select v-model="form.planner_connection_id" placeholder="接口中心的 ai_chat"><el-option v-for="row in plannerConnections" :key="row.id" :label="row.name" :value="row.id"/></el-select></el-form-item>
          <el-form-item label="生图 AI 连接"><el-select v-model="form.image_connection_id" placeholder="接口中心的 ai_image"><el-option v-for="row in imageConnections" :key="row.id" :label="row.name" :value="row.id"/></el-select></el-form-item>
          <el-form-item label="规划模型"><el-input v-model="form.planner_model" /></el-form-item>
          <el-form-item label="生图模型"><el-input v-model="form.image_model" /></el-form-item>
          <el-form-item class="wide" label="参考图（最多 16 张）"><input type="file" accept="image/png,image/jpeg,image/webp" multiple @change="selectFiles($event, 'references')"><small>{{ references.length ? `已选择 ${references.length} 张` : '没有参考图时按文字需求生图' }}</small></el-form-item>
          <el-form-item class="wide" label="补充指令"><el-input v-model="form.custom_instruction" type="textarea" :rows="2" /></el-form-item>
        </el-form>
        <el-button type="primary" size="large" @click="createBatch">生成 AI 规划</el-button>
      </el-tab-pane>
      <el-tab-pane label="仅本地超分（零 Token）">
        <div class="upscale-panel"><div><h3>批量图片超分</h3><p>原图不会被修改，输出进入 ERP 资产目录。2×适合大多数桌布和毛毯；4×耗时和显存占用更高。</p></div><el-radio-group v-model="upscaleScale"><el-radio-button :value="2">2×</el-radio-button><el-radio-button :value="4">4×</el-radio-button></el-radio-group><input type="file" accept="image/png,image/jpeg,image/webp" multiple @change="selectFiles($event, 'upscale')"><el-button type="success" @click="runUpscale">开始本地超分（{{ upscaleFiles.length }} 张）</el-button></div>
      </el-tab-pane>
    </el-tabs>

    <el-card v-if="current" class="current-card" shadow="never">
      <template #header><div class="card-head"><div><b>{{ current.kind === 'upscale' ? '本地超分批次' : 'AI 生图批次' }}</b><small>{{ current.id }} · {{ formatDate(current.created_at) }}</small></div><el-tag :type="current.status === 'completed' ? 'success' : current.status === 'failed' ? 'danger' : 'warning'">{{ statusText(current.status) }}</el-tag></div></template>
      <el-alert v-if="activeJobId" :title="`后台任务 ${activeJobId}`" type="info" :closable="false"><template #default><RouterLink :to="`/jobs?job=${activeJobId}`">前往任务中心查看进度和错误</RouterLink></template></el-alert>
      <div v-if="planItems.length" class="plan-list">
        <div class="plan-head"><h3>确认设计规划</h3><div><el-button @click="savePlan">保存修改</el-button><el-button type="primary" @click="generate">开始批量生图</el-button></div></div>
        <article v-for="item in planItems" :key="item.index" class="plan-item"><el-checkbox v-model="item.selected"/><div><el-input v-model="item.title"/><el-input v-model="item.concept" placeholder="设计说明"/><el-input v-model="item.prompt" type="textarea" :rows="4"/><el-input v-model="item.negative_prompt" type="textarea" :rows="2" placeholder="需要避免的内容"/></div><el-tag>{{ item.status }}</el-tag></article>
      </div>
      <div v-if="current.outputs?.length" class="output-grid">
        <article v-for="item in current.outputs" :key="`${item.index}-${item.title}`" :class="{ failed: item.status === 'failed' }"><el-image v-if="item.url" :src="item.url" fit="cover" :preview-src-list="[item.url]"/><div><b>{{ item.title }}</b><span v-if="item.width">{{ item.width }} × {{ item.height }} · {{ (item.size_bytes/1e6).toFixed(1) }} MB</span><span v-if="item.error">{{ item.error }}</span><a v-if="item.url" :href="item.url" download>下载原图</a></div></article>
      </div>
    </el-card>

    <el-card shadow="never"><template #header><b>最近批次</b></template><el-table :data="status.batches || []" @row-click="openBatch"><el-table-column label="时间" width="180"><template #default="s">{{ formatDate(s.row.created_at) }}</template></el-table-column><el-table-column label="类型" width="120"><template #default="s">{{ s.row.kind === 'upscale' ? '仅超分' : 'AI 生图' }}</template></el-table-column><el-table-column label="状态" width="120"><template #default="s">{{ statusText(s.row.status) }}</template></el-table-column><el-table-column label="数量"><template #default="s">{{ s.row.output_count || 0 }} 个结果</template></el-table-column></el-table></el-card>
  </section>
</template>

<style scoped>
.ai-workbench{display:grid;gap:18px}.hero{display:flex;justify-content:space-between;gap:24px;padding:28px;border-radius:18px;color:#fff;background:linear-gradient(125deg,#172a46,#284f75 55%,#1f765f)}.hero h2{margin:5px 0 8px;font-size:30px}.hero p{max-width:720px;margin:0;color:#dbe9f4;line-height:1.7}.kicker{font-size:12px;letter-spacing:.18em;color:#8ee0c0}.runtime{min-width:210px;padding:18px;border:1px solid #ffffff30;border-radius:14px;background:#ffffff12;display:grid;gap:8px}.runtime b{font-size:18px}.runtime span,.runtime small{color:#dce8ef}.form-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0 16px}.form-grid .wide{grid-column:1/-1}.form-grid :deep(.el-select),.form-grid :deep(.el-input-number){width:100%}.form-grid small{display:block;color:#7b8796;margin-top:6px}.upscale-panel{display:grid;gap:18px;max-width:760px;padding:16px}.upscale-panel h3{margin:0 0 6px}.upscale-panel p{color:#687689;line-height:1.7}.current-card{border-color:#b9d9cf}.card-head,.plan-head{display:flex;align-items:center;justify-content:space-between;gap:16px}.card-head>div{display:grid;gap:5px}.card-head small{color:#758398}.plan-list{margin-top:18px}.plan-item{display:grid;grid-template-columns:28px 1fr 80px;gap:12px;padding:16px 0;border-top:1px solid #e8edf3}.plan-item>div{display:grid;gap:8px}.output-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;margin-top:18px}.output-grid article{border:1px solid #dfe7ef;border-radius:12px;overflow:hidden;background:#fff}.output-grid article.failed{padding:14px;border-color:#efb6b6}.output-grid .el-image{width:100%;height:210px;background:#f2f5f8}.output-grid article>div{display:grid;gap:5px;padding:12px}.output-grid span{font-size:12px;color:#708096}.output-grid a{color:#2367bd;text-decoration:none}@media(max-width:1100px){.form-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:700px){.hero{flex-direction:column}.form-grid{grid-template-columns:1fr}.form-grid .wide{grid-column:auto}.plan-item{grid-template-columns:28px 1fr}.plan-item>.el-tag{grid-column:2}.card-head,.plan-head{align-items:flex-start;flex-direction:column}.output-grid{grid-template-columns:1fr}}
</style>
