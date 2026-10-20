<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, ApiError } from '../api'
import type { CatalogProduct } from '../types'
import SharedProductDetails from '../components/SharedProductDetails.vue'
import { catalogImageUrl } from '../deployment'

defineProps<{readOnly: boolean}>()
const route = useRoute()
const fixedSource = route.path === '/pod/products' ? 'pod' : String(route.meta.sourceType || '')
const source = ref(fixedSource), search = ref(''), page = ref(1), total = ref(0)
const items = ref<CatalogProduct[]>([]), loading = ref(false), error = ref('')
const detail = ref<CatalogProduct>(), drawer = ref(false), notes = ref(''), originalNotes = ref('')
const saving = ref(false), uncertain = ref(false), detailBusy = ref(false)
const labels: Record<string,string> = {pod:'POD', follow:'OZON 跟卖', self_selected:'自主上架'}
let listRequest = 0, detailRequest = 0
async function load() {
  const requestId = ++listRequest
  loading.value = true; error.value = ''
  try {
    const q = new URLSearchParams({search:search.value, source_type:source.value, page:String(page.value), page_size:'50'})
    const data = await api.get<{items:CatalogProduct[]; total:number}>(`/catalog/products?${q}`)
    if (requestId !== listRequest) return
    items.value = data.items; total.value = data.total
  } catch(e) { if(requestId === listRequest) error.value = (e as Error).message }
  finally { if(requestId === listRequest) loading.value = false }
}
function query() { page.value = 1; load() }
async function show(row: CatalogProduct) {
  const requestId = ++detailRequest
  detailBusy.value = true; drawer.value = true; detail.value = undefined
  try {
    const value = await api.get<CatalogProduct>(`/catalog/products/${row.id}`)
    if (requestId !== detailRequest) return
    detail.value = value; notes.value = value.notes || ''; originalNotes.value = notes.value; uncertain.value = false
  } catch(e) { ElMessage.error((e as Error).message) }
  finally { if(requestId === detailRequest) detailBusy.value = false }
}
async function save() {
  if (!detail.value || saving.value || uncertain.value) return
  const id = detail.value.id, desired = notes.value
  saving.value = true
  try {
    await api.put(`/catalog/products/${id}/notes`, {expected_notes:originalNotes.value, notes:desired})
    const saved = await api.get<CatalogProduct>(`/catalog/products/${id}`)
    if (saved.notes !== desired) throw new Error('保存后备注又有变化，请重新读取确认')
    detail.value = saved; originalNotes.value = saved.notes || ''
    ElMessage.success('备注已保存到共享产品库'); await load()
  } catch(e) {
    uncertain.value = true
    ElMessage.error(e instanceof ApiError && e.code === 'conflict' ? '备注已被修改，请重新读取后再编辑' : `${(e as Error).message}。请重新读取确认，避免覆盖其他人的修改。`)
  } finally { saving.value = false }
}
onMounted(load)
</script>
<template>
  <div class="shared-products">
    <el-alert title="共享产品库" description="查看商品详情、规格、成本、图片和生产资料；内部备注支持多人同步。商品资料来自本地库同步，试用修改不会自动回写原本地库。" type="info" :closable="false" show-icon />
    <section class="panel">
      <div class="shared-heading"><div><h2>产品库</h2><p>共 {{ total.toLocaleString() }} 个产品 · 云端共享</p></div><el-button :loading="loading" @click="load">刷新</el-button></div>
      <div class="shared-toolbar">
        <el-input v-model="search" aria-label="搜索名称或SKU" placeholder="搜索产品名称或 SKU" clearable maxlength="200" @keyup.enter="query" />
        <el-select v-if="!fixedSource" v-model="source" aria-label="产品来源" placeholder="全部来源" clearable><el-option v-for="(label,key) in labels" :key="key" :label="label" :value="key" /></el-select>
        <el-tag v-else>{{ labels[fixedSource] }}</el-tag>
        <el-button type="primary" @click="query">查询</el-button>
      </div>
      <el-alert v-if="error" :title="error" type="error" :closable="false" />
      <el-table v-loading="loading" :data="items" row-key="id" @row-click="show" empty-text="没有找到匹配的产品">
        <el-table-column label="主图" width="90"><template #default="{row}"><el-image v-if="catalogImageUrl(row.thumbnail)" :src="catalogImageUrl(row.thumbnail)" fit="cover" lazy style="width:64px;height:64px"><template #error><span>加载失败</span></template></el-image><span v-else>暂无图片</span></template></el-table-column>
        <el-table-column label="产品" min-width="280"><template #default="{row}"><b>{{ row.name }}</b><small class="table-sub mono">{{ row.sku }}</small></template></el-table-column>
        <el-table-column label="来源" width="135"><template #default="{row}"><el-tag>{{ labels[row.source_type] || row.source_type }}</el-tag></template></el-table-column>
        <el-table-column prop="supplier" label="供应商" width="120" />
        <el-table-column label="采购价" width="110"><template #default="{row}">{{ row.purchase_price_cny ?? '未提供' }} {{ row.currency }}</template></el-table-column>
        <el-table-column prop="variant_count" label="SKU 数" width="85" />
        <el-table-column prop="notes" label="内部备注" min-width="240" show-overflow-tooltip />
        <el-table-column label="操作" width="125"><template #default="{row}"><el-button link type="primary" @click.stop="show(row)">{{ readOnly ? '查看详情' : '查看 / 备注' }}</el-button></template></el-table-column>
      </el-table>
      <div class="shared-pagination"><span>每页 50 条</span><el-pagination v-model:current-page="page" :page-size="50" :total="total" layout="prev, pager, next" @current-change="load" /></div>
    </section>
    <el-drawer v-model="drawer" title="完整商品资料" size="min(1120px, 96vw)" :close-on-click-modal="!saving" :close-on-press-escape="!saving" :show-close="!saving">
      <div v-loading="detailBusy">
        <template v-if="detail">
          <h2>{{ detail.name }}</h2><p class="mono">{{ detail.sku }}</p><el-tag>{{ labels[detail.source_type] || detail.source_type }}</el-tag>
          <SharedProductDetails :key="detail.id" :product="detail" />
          <el-form label-position="top" class="notes-form" @submit.prevent="save"><el-form-item label="内部备注"><el-input v-model="notes" type="textarea" :rows="9" maxlength="6000" show-word-limit :disabled="readOnly || saving || uncertain" /></el-form-item>
            <el-alert v-if="uncertain" title="请先重新读取当前备注，确认后再编辑" type="warning" :closable="false" />
            <div class="notes-actions"><el-button :disabled="saving" @click="show(detail)">重新读取</el-button><el-button v-if="!readOnly" type="primary" native-type="submit" :loading="saving" :disabled="uncertain || notes === originalNotes">保存备注</el-button></div>
          </el-form>
          <p class="shared-hint">保存后，其他电脑重新查询即可看到。多人同时修改时会先检查原备注。</p>
        </template>
      </div>
    </el-drawer>
  </div>
</template>
<style scoped>
.shared-products>.panel{padding:24px}
.shared-products{display:grid;gap:20px}.shared-heading,.shared-toolbar,.shared-pagination,.notes-actions{display:flex;align-items:center;gap:12px}.shared-heading,.shared-pagination{justify-content:space-between}.shared-heading h2{margin:0}.shared-heading p,.shared-hint{color:#718096;font-size:13px}.shared-toolbar{margin:20px 0;flex-wrap:wrap}.shared-toolbar .el-input{width:340px;max-width:100%}.shared-toolbar .el-select{width:160px}.shared-pagination{margin-top:20px;flex-wrap:wrap}.notes-form{margin-top:28px}.notes-actions{margin-top:16px;justify-content:flex-end}
</style>
