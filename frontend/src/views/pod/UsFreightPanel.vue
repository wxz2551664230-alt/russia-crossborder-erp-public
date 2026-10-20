<script setup lang="ts">
import { computed, inject, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../../api'

const props = defineProps<{ productKey: string; detail: any; task?: any }>()
const emit = defineEmits<{ job: [value: any] }>()
const currentUser=inject<any>('erpCurrentUser'),accountMode=inject<any>('erpAccountMode')
const canManage=computed(()=>accountMode?.value===false||currentUser?.value?.role==='admin')
const profile = reactive({range_position_percent:50, buffer_percent:5, buffer_cny:0, rounding_cny:1})
const variant = ref<number>(), manual = ref<number>(), result = ref<any>()
const loading = ref(false), saving = ref(false), reading = ref(false)
let generation = 0
const variants = computed(() => (props.detail?.stock_info || []).map((v:any) => ({
  value:Number(v.id), label:`${props.detail?.sizes?.find((s:any)=>String(s.id)===String(v.size_id))?.name||v.size_id} · ${props.detail?.colors?.find((c:any)=>String(c.id)===String(v.color_id))?.name||v.color_id} · ${v.code||v.id}`
})))
const busy = computed(()=>props.task && ['queued','running','retrying','paused'].includes(props.task.status))
async function preview(){
  const current = ++generation; result.value=undefined
  if(!variant.value)return
  loading.value=true
  try{
    const data=await api.post<any>(`/pod/us-catalog/products/${props.productKey}/freight-preview`,{variant_id:variant.value,manual_freight_cny:manual.value??null})
    if(current===generation)result.value=data
  }catch(error:any){if(current===generation)ElMessage.error(error.message||'运费预览失败')}
  finally{if(current===generation)loading.value=false}
}
async function save(){
  saving.value=true
  try{await api.put('/settings/amazon_us_freight',{value:{...profile}});ElMessage.success('统一运费规则已保存到云端');await preview()}
  catch(error:any){ElMessage.error(error.message||'规则保存失败')}finally{saving.value=false}
}
async function quote(refresh=false){
  if(!variant.value)return
  reading.value=true
  try{
    const value=await api.post<any>(`/pod/us-catalog/products/${props.productKey}/freight`,{variant_id:variant.value,refresh})
    if(value.cached){await preview();ElMessage.success('已使用云端有效报价')}
    else {emit('job',value.job);ElMessage.success('运费读取任务已交给当前电脑')}
  }catch(error:any){ElMessage.error(error.message||'运费读取失败')}finally{reading.value=false}
}
watch(()=>props.productKey,()=>{manual.value=undefined;variant.value=variants.value[0]?.value;preview()},{immediate:true})
watch(()=>props.task?.status,()=>{if(props.task?.type==='pod_supplier_us_freight_quote'&&props.task?.status==='completed')preview()})
onMounted(async()=>{try{Object.assign(profile,await api.get('/pod/us-catalog/freight-rule'))}catch(error:any){ElMessage.error(error.message)}})
</script>

<template>
  <section class="freight-panel">
    <h3>美国 POD 运费取值规则</h3>
    <p class="note">统一规则应用于各商品自己的 GOFO 单件报价区间，同规格不同印花复用云端报价。</p>
    <el-form inline label-width="112px" :disabled="!canManage">
      <el-form-item label="区间取值位置 (%)"><el-input-number v-model="profile.range_position_percent" :min="0" :max="100"/></el-form-item>
      <el-form-item label="缓冲比例 (%)"><el-input-number v-model="profile.buffer_percent" :min="0" :max="100"/></el-form-item>
      <el-form-item label="固定缓冲 (元)"><el-input-number v-model="profile.buffer_cny" :min="0" :max="1000000" :precision="2"/></el-form-item>
      <el-form-item label="取整单位 (元)"><el-input-number v-model="profile.rounding_cny" :min="0.01" :max="100" :precision="2"/></el-form-item>
    </el-form>
    <p class="note">下限＋（上限－下限）×取值位置，加缓冲后向上取整。50% 表示区间中点，未代表实际订单平均值。</p>
    <el-button v-if="canManage" :loading="saving" @click="save">保存统一规则</el-button><span v-else class="note">统一规则由管理员配置，业务账号可在预览中手动覆盖运费。</span>
    <el-divider/>
    <h3>所选规格成本预览</h3>
    <div class="freight-controls">
      <el-select v-model="variant" placeholder="选择规格" @change="manual=undefined;preview()"><el-option v-for="v in variants" :key="v.value" :label="v.label" :value="v.value"/></el-select>
      <el-button :loading="reading" :disabled="!!busy||!variant" @click="quote(false)">读取 GOFO 运费</el-button>
      <el-button :loading="reading" :disabled="!!busy||!variant" @click="quote(true)">手动刷新报价</el-button>
    </div>
    <p class="note">打开页面只读取云端缓存。报价有效期 7 天；缺失或过期时需人工读取，由当前电脑执行。</p>
    <div v-loading="loading">
      <el-alert v-if="result&&!result.available" :title="result.message" type="info" :closable="false"/>
      <template v-if="result?.available">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="物流渠道">{{result.quote.channel_name}}</el-descriptions-item>
          <el-descriptions-item label="供应商运费区间">{{result.freight.minimum_cny}}～{{result.freight.maximum_cny}} 元</el-descriptions-item>
          <el-descriptions-item label="规则计算运费">{{result.freight.calculated_cny}} 元</el-descriptions-item>
          <el-descriptions-item label="本次采用运费">{{result.freight.selected_cny}} 元{{result.freight.manual?'（手动）':''}}</el-descriptions-item>
          <el-descriptions-item label="单件拿货价">{{result.purchase_cny}} 元</el-descriptions-item>
          <el-descriptions-item label="拿货价＋运费">{{result.purchase_and_freight_cny}} 元</el-descriptions-item>
          <el-descriptions-item label="报价读取时间">{{result.quote.fetched_at}}</el-descriptions-item>
        </el-descriptions>
        <p>本次手动运费 (元)：<el-input-number v-model="manual" :min="0" :max="1000000" :precision="2" placeholder="留空沿用规则" @change="preview"/><el-button @click="manual=undefined;preview()">恢复统一规则</el-button></p>
        <el-alert :title="result.basis" type="info" :closable="false"/>
      </template>
    </div>
  </section>
</template>

<style scoped>
.freight-panel{margin-top:22px;border-top:1px solid #e4e7ed;padding-top:12px}.note{color:#718096;font-size:13px}.freight-controls{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.freight-controls .el-select{min-width:300px;flex:1}.freight-panel .el-input-number{width:145px}
</style>
