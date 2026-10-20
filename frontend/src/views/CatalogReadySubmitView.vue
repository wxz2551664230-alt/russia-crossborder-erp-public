<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { sharedProductsPilot } from '../deployment'
import CatalogOzonTabs from '../components/CatalogOzonTabs.vue'

const publications=ref<any[]>([])
const selectedIds=ref<string[]>([])
const storeFilter=ref('')
const storeChoices=computed(()=>[...new Map(publications.value.flatMap(p=>p.stores).map((s:any)=>[s.store_id,s])).values()] as any[])
watch(storeFilter,()=>{selectedIds.value=[]})
const loading=ref(false)
const submittingIds=ref<string[]>([])
const rows=computed(()=>publications.value.flatMap(publication=>publication.stores.filter((store:any)=>store.status==='ready_for_submit'&&(!storeFilter.value||store.store_id===storeFilter.value)).map((store:any)=>({publication,store}))))
const selectedRows=computed(()=>rows.value.filter(row=>selectedIds.value.includes(row.store.id)))

function primaryImage(row:any){return row.store.assets?.primary_image||row.store.assets?.original_primary_image||row.publication.product_snapshot?.assets?.find((asset:any)=>asset.role==='main')?.oss_url||''}
function imageCount(row:any){return (row.store.assets?.images||[]).filter(Boolean).length}
function price(row:any){return row.store.pricing?.price||row.store.pricing?.sale_price||'-'}
function selectionChanged(items:any[]){selectedIds.value=items.map(item=>item.store.id)}
function isSubmitting(row:any){return submittingIds.value.includes(row.store.id)}
async function load(){loading.value=true;try{const items:any[]=[];for(let page=1;page<=100;page++){const data=await api.get<any>(`/publications?page_size=200&page=${page}&store_status=ready_for_submit`);items.push(...(data.items||[]));if(items.length>=data.total||!data.items?.length)break}publications.value=items;selectedIds.value=selectedIds.value.filter(id=>rows.value.some(row=>row.store.id===id))}finally{loading.value=false}}
async function enqueue(row:any){submittingIds.value=[...submittingIds.value,row.store.id];try{await api.post(`/publications/${row.publication.id}/stores/${row.store.store_id}/submit`,{});return true}finally{submittingIds.value=submittingIds.value.filter(id=>id!==row.store.id)}}
async function submitOne(row:any){await ElMessageBox.confirm(`确认将 ${row.publication.sku} 提交到 ${row.store.store_name}？提交后将真实调用 Ozon Seller API。`,'真实提交确认',{type:'warning',confirmButtonText:'确认提交'});await enqueue(row);ElMessage.success('已进入 Ozon 提交队列');await load()}
async function submitBatch(){const targets=[...selectedRows.value];if(!targets.length)return ElMessage.warning('请先勾选要提交的店铺商品');await ElMessageBox.confirm(`确认批量提交 ${targets.length} 个店铺商品到 Ozon？已进入队列的商品不会重复提交。`,'批量真实提交确认',{type:'warning',confirmButtonText:'确认批量提交'});if(sharedProductsPilot.value){submittingIds.value=targets.map(row=>row.store.id);try{const result=await api.post<any>('/publications/batch/submit',{entries:targets.map(row=>({publication_id:row.publication.id,store_id:row.store.store_id}))});ElMessage.success(`已创建 ${result.batch_count} 个批次，共 ${result.product_count} 件商品`);await load()}catch(error:any){ElMessage.error(error.message||'批量提交未确认，请刷新核对原队列')}finally{submittingIds.value=[]}return}let succeeded=0,failed=0;for(const row of targets){try{await enqueue(row);succeeded++}catch{failed++}}selectedIds.value=[];ElMessage[failed?'warning':'success'](`成功进入队列 ${succeeded} 个，失败 ${failed} 个`);await load()}
onMounted(load)
</script>

<template><div class="page-stack"><CatalogOzonTabs/><section class="panel"><div class="panel-head"><div><h2>待提交 Ozon</h2><p>图片已人工批准，文案、属性和发布包已准备完成。勾选后可批量提交到对应店铺。</p></div><div><el-select v-model="storeFilter" clearable placeholder="筛选店铺（全部）" style="width:180px"><el-option v-for="store in storeChoices" :key="store.store_id" :label="store.store_name" :value="store.store_id"/></el-select><el-button @click="load">刷新</el-button><el-button type="primary" :disabled="!selectedIds.length" @click="submitBatch">批量提交到 Ozon（{{selectedIds.length}}）</el-button></div></div>
  <el-table v-loading="loading" :data="rows" empty-text="当前没有待提交商品" @selection-change="selectionChanged"><el-table-column type="selection" width="48"/><el-table-column label="主图" width="92"><template #default="{row}"><el-image v-if="primaryImage(row)" class="product-thumb" :src="primaryImage(row)" :preview-src-list="[primaryImage(row)]" preview-teleported fit="cover"/><span v-else class="muted">无图片</span></template></el-table-column><el-table-column label="产品 / SKU" min-width="250"><template #default="{row}"><b>{{row.publication.product_name}}</b><small class="table-sub mono">{{row.publication.sku}}</small></template></el-table-column><el-table-column label="店铺 / Offer ID" min-width="220"><template #default="{row}"><b>{{row.store.store_name}}</b><small class="table-sub mono">{{row.store.offer_id}}</small></template></el-table-column><el-table-column label="发布资料" min-width="180"><template #default="{row}"><b>主图 1 张 · 附图 {{imageCount(row)}} 张</b><small class="table-sub">俄文文案与 Ozon 属性已就绪</small></template></el-table-column><el-table-column label="售价" width="130"><template #default="{row}"><b>{{price(row)}} ₽</b></template></el-table-column><el-table-column label="操作" width="170" fixed="right"><template #default="{row}"><el-button type="primary" :loading="isSubmitting(row)" @click="submitOne(row)">本店提交 Ozon</el-button></template></el-table-column></el-table>
</section></div></template>

<style scoped>.product-thumb{width:58px;height:76px;border-radius:7px;background:#f2f5f9}.muted{color:#98a2b3;font-size:12px}</style>
