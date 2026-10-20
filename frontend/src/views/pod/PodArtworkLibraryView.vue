<script setup lang="ts">
import { computed,onMounted,onUnmounted,ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage,ElMessageBox } from 'element-plus'
import { api } from '../../api'
import { sharedProductsPilot } from '../../deployment'
const pendingUploadJobs=new Set<string>()
let uploadPoll:ReturnType<typeof setInterval>|undefined,checkingUploads=false
async function checkUploads(){
  if(checkingUploads||!pendingUploadJobs.size)return
  checkingUploads=true
  try{
    const result=await api.get<any>('/jobs?job_type=pod_artwork_upload&page_size=200')
    let changed=false
    for(const job of result.items||[]){
      if(pendingUploadJobs.has(job.id)&&['completed','failed','cancelled'].includes(job.status)){
        pendingUploadJobs.delete(job.id);changed=true
        if(job.status==='failed')ElMessage.error(`设计图入库失败：${job.error_message||'请查看任务详情'}`)
      }
    }
    if(changed)await load()
  }finally{checkingUploads=false}
}

const items=ref<any[]>([]),categories=ref<any[]>([]),selected=ref<string[]>([]),search=ref(''),category=ref(''),uploadDates=ref<string[]>([]),page=ref(1),total=ref(0),loading=ref(false),activeUploads=ref(0),files=ref<File[]>([]),uploadResults=ref<any[]>([]),uploadDialog=ref(false),categoryDialog=ref(false),newCategory=ref(''),moveTo=ref(''),categoryNames=ref<Record<string,string>>({})
const router=useRouter()
const market=ref('CN'),readingProduct=ref(false),productDetailJob=ref('')
let productOptionsRequest=0,artworkListRequest=0,disposed=false
const productOptions=ref<any[]>([]),productKey=ref(''),productSearchLoading=ref(false),usageFilter=ref('all'),usageDialog=ref(false),usageRows=ref<any[]>([]),usageArtwork=ref<any>(null)
const chosenProduct=computed(()=>productOptions.value.find(x=>x.key===productKey.value))
async function loadProductOptions(search=''){const request=++productOptionsRequest,currentMarket=market.value;productSearchLoading.value=true;try{const data=await api.get<any>(`/pod/artworks/product-options?${new URLSearchParams({search,market:currentMarket})}`);if(request!==productOptionsRequest||currentMarket!==market.value)return;const selected=chosenProduct.value;productOptions.value=data.items||[];if(selected&&!productOptions.value.some(x=>x.key===selected.key))productOptions.value.unshift(selected)}finally{if(request===productOptionsRequest)productSearchLoading.value=false}}
async function changeMarket(){productKey.value='';productOptions.value=[];usageFilter.value='all';selected.value=[];page.value=1;await Promise.all([loadProductOptions(),load()])}
async function changeProduct(){usageFilter.value=productKey.value?'available':'all';page.value=1;await load()}
async function readProductDetail(){
  const product=chosenProduct.value
  if(market.value!=='US'||!product||product.detail_cached||readingProduct.value)return
  readingProduct.value=true
  try{
    const result=await api.post<any>(`/pod/us-catalog/products/${encodeURIComponent(product.key)}/refresh`)
    productDetailJob.value=result.job.id
    for(let n=0;n<90&&!disposed;n++){
      const job=await api.get<any>(`/jobs/${productDetailJob.value}`)
      if(job.status==='completed'){
        const saved=await api.get<any>(`/pod/us-catalog/products/${encodeURIComponent(product.key)}`)
        if(!saved.detail_cached)throw new Error('商品规格尚未保存，请查看任务详情')
        if(market.value==='US')productOptions.value.filter(x=>x.key===product.key).forEach(x=>{x.detail_cached=true})
        ElMessage.success('美国产品规格已保存，可以批量合成');return
      }
      if(['failed','cancelled','paused','action_required'].includes(job.status))throw new Error(job.error_message||'商品详情读取未完成，请查看任务详情')
      await new Promise(resolve=>setTimeout(resolve,2000))
    }
    if(!disposed)ElMessage.info('读取任务仍在原电脑运行，请到任务中心查看；完成后重新选择此产品')
  }catch(error:any){ElMessage.error(error?.message||'商品详情读取失败')}
  finally{readingProduct.value=false}
}
async function showUsage(artwork:any){usageArtwork.value=artwork;usageRows.value=[];usageDialog.value=true;try{const data=await api.get<any>(`/pod/artworks/${artwork.id}/usages`);usageRows.value=data.items||[]}catch(error:any){ElMessage.error(error?.message||'读取使用记录失败')}}
const allSelected=computed(()=>items.value.length>0&&items.value.every(x=>selected.value.includes(x.id)))
const selectedItems=computed(()=>items.value.filter(x=>selected.value.includes(x.id)))
async function load(){const request=++artworkListRequest;loading.value=true;try{const q=new URLSearchParams({search:search.value,category_id:category.value,page:String(page.value),page_size:'60',product_key:productKey.value,usage:usageFilter.value,market:market.value});if(uploadDates.value?.[0])q.set('uploaded_from',uploadDates.value[0]);if(uploadDates.value?.[1])q.set('uploaded_to',uploadDates.value[1]);const data=await api.get<any>(`/pod/artworks?${q}`);if(request!==artworkListRequest||disposed)return;items.value=data.items||[];categories.value=data.categories||[];total.value=data.total||0;selected.value=[]}finally{if(request===artworkListRequest)loading.value=false}}
function formatUploadDate(value:string){return value?new Date(value).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit'}):'-'}
function toggleAll(){selected.value=allSelected.value?[]:items.value.map(x=>x.id)}
function chooseFiles(event:Event){files.value=Array.from((event.target as HTMLInputElement).files||[]);uploadResults.value=[]}
async function upload(){
  if(!files.value.length)return ElMessage.warning('请选择 PNG/JPG 图片')
  if(files.value.length>100)return ElMessage.warning('每批最多选择 100 张图片')
  const batchFiles=[...files.value],batchCategory=category.value||'uncategorized'
  files.value=[]
  activeUploads.value+=1
  ElMessage.info(`正在登记 ${batchFiles.length} 张图片，由所选电脑处理，你可以立即选择下一批`)
  try{
    const rows:any[]=[]
    for(const group of (sharedProductsPilot.value?batchFiles.map(file=>[file]):[batchFiles])){
      const form=new FormData();group.forEach(value=>form.append('images',value,value.name));form.append('category_id',batchCategory)
      try{const response=await api.upload<any>('/pod/artworks/batch',form);rows.push(...(response.results||[]))}
      catch(error:any){rows.push(...group.map(file=>({filename:file.name,status:'failed',message:error?.message||'登记未确认，请先在任务中心核对再重试'})))}
    }
    uploadResults.value=rows
    rows.forEach(row=>{if(row.job_id)pendingUploadJobs.add(row.job_id)})
    const result={accepted:rows.filter(x=>x.status==='queued').length,failed:rows.filter(x=>x.status==='failed').length}
    const failedNames=new Set(rows.filter(x=>x.status==='failed').map(x=>x.filename));files.value=[...files.value,...batchFiles.filter(value=>failedNames.has(value.name))]
    if(result.failed){uploadDialog.value=true;ElMessage.warning(`已登记 ${result.accepted} 张，${result.failed} 张未能登记`) }
    else ElMessage.success(`已登记 ${result.accepted} 张后台上传任务，可以立即创建下一批`)
    page.value=1;window.setTimeout(()=>void load(),1200)
  }catch(error:any){files.value=batchFiles;ElMessage.error(error?.message||'上传任务登记失败，文件已保留可重试')}
  finally{activeUploads.value-=1}
}
async function createCategory(){if(!newCategory.value.trim())return;await api.post('/pod/artworks/categories',{name:newCategory.value.trim()});newCategory.value='';ElMessage.success('分类已新建');await load();categoryNames.value=Object.fromEntries(categories.value.map(c=>[c.id,c.name]))}
function openCategoryManager(){categoryNames.value=Object.fromEntries(categories.value.map(c=>[c.id,c.name]));categoryDialog.value=true}
async function renameCategory(item:any){const name=(categoryNames.value[item.id]||'').trim();if(!name)return ElMessage.warning('分类名称不能为空');if(name===item.name)return;await api.patch(`/pod/artworks/categories/${item.id}`,{name});ElMessage.success('分类名称已更新');await load();categoryNames.value=Object.fromEntries(categories.value.map(c=>[c.id,c.name]))}
async function deleteCategory(item:any){if(item.count)return ElMessage.warning('请先将分类内图片移动到其他分类');await ElMessageBox.confirm(`确认删除空分类“${item.name}”？原图不会被删除。`,'删除分类',{type:'warning'});await api.delete(`/pod/artworks/categories/${item.id}`);if(category.value===item.id)category.value='';ElMessage.success('分类已删除');await load()}
function enterCategory(item:any){category.value=item.is_system?item.id:item.id;page.value=1;categoryDialog.value=false;load()}
async function move(){if(!selected.value.length||!moveTo.value)return ElMessage.warning('请选择图案和目标分类');await api.patch('/pod/artworks/batch-category',{item_ids:selected.value,category_id:moveTo.value});ElMessage.success(`已移动 ${selected.value.length} 张图案`);load()}
async function composeSelected(){
  if(!productKey.value||!chosenProduct.value)return ElMessage.warning('请先选择要合成的产品')
  if(market.value==='US'&&!chosenProduct.value.detail_cached)return ElMessage.warning('该美国产品尚未缓存规格，请先点击“读取产品详情”')
  if(!selectedItems.value.length)return ElMessage.warning('请先选择需要合成的设计图')
  if(selectedItems.value.some(x=>x.used_by_selected))return ElMessage.warning('所选设计图已用于当前产品，请取消这些图片')
  const batch={created_at:new Date().toISOString(),market:market.value,product:chosenProduct.value,items:selectedItems.value.map(({id,name,thumbnail_url,file_url,usable_url,source,file_size}:any)=>({id,name,thumbnail_url,file_url,usable_url,source,file_size}))}
  window.sessionStorage.setItem('erp.pod.composerBatch.v1',JSON.stringify(batch))
  await router.push({path:'/pod/studio',query:{batch:String(batch.items.length),...(market.value==='US'?{market:'US',product:productKey.value}:{})}})
}
onMounted(()=>{void loadProductOptions();void load();uploadPoll=setInterval(()=>void checkUploads().catch(()=>{}),4000)})
onUnmounted(()=>{disposed=true;if(uploadPoll)clearInterval(uploadPoll)})
</script>
<template><div class="page-stack"><section class="panel"><div class="panel-head"><div><h2>设计图库</h2><p>{{total}} 张原始图案，合成时只引用不重复复制</p></div></div><div class="panel-body">
  <div class="pod-product-picker"><strong>1. 选择要合成的产品</strong><el-select v-model="market" class="market-picker" aria-label="合成货盘" @change="changeMarket"><el-option label="原有货盘" value="CN"/><el-option label="美国货盘（Amazon）" value="US"/></el-select><el-select v-model="productKey" filterable remote clearable :remote-method="loadProductOptions" :loading="productSearchLoading" placeholder="搜索产品名称或货号" @change="changeProduct"><el-option v-for="p in productOptions" :key="p.key" :label="p.label" :value="p.key"/></el-select><el-button v-if="market==='US'&&chosenProduct&&!chosenProduct.detail_cached" :loading="readingProduct" @click="readProductDetail">读取产品详情</el-button><el-button v-if="market==='US'" link type="primary" @click="router.push('/pod/us-catalog')">管理美国货盘</el-button><RouterLink v-if="readingProduct&&productDetailJob" :to="`/jobs?job=${productDetailJob}`">查看读取进度</RouterLink><span>选好产品后，默认只显示尚未用于该货盘产品的设计图。</span></div>
  <div class="pod-product-picker"><strong>2. 筛选设计图</strong><el-select v-model="usageFilter" :disabled="!productKey" @change="page=1;load()"><el-option label="当前产品未用过" value="available"/><el-option label="已用于当前产品" value="used_selected"/><el-option label="全部设计图" value="all"/><el-option label="从未用于任何产品" value="unused_any"/></el-select></div>
  <div class="pod-library-toolbar"><el-input v-model="search" clearable placeholder="搜索图案名称" @keyup.enter="page=1;load()"/><el-select v-model="category" clearable placeholder="全部分类" @change="page=1;load()"><el-option v-for="c in categories" :key="c.id" :value="c.id" :label="`${c.name} (${c.count??0})`"/></el-select><el-date-picker v-model="uploadDates" type="daterange" value-format="YYYY-MM-DD" start-placeholder="上传开始日期" end-placeholder="上传结束日期" range-separator="至" clearable @change="page=1;load()"/><el-button @click="load">查询</el-button><el-button @click="toggleAll">{{allSelected?'取消本页':'全选本页'}}</el-button><span class="grow"/><el-button @click="openCategoryManager">管理分类</el-button></div>
  <div class="pod-library-actions"><label class="file-picker compact"><input type="file" accept="image/png,image/jpeg" multiple @change="chooseFiles"/><span>{{files.length?`已选择 ${files.length} 张图片`:'批量选择新图案'}}</span></label><el-button type="primary" :disabled="!files.length" @click="upload">{{files.length>1?`创建后台上传任务 (${files.length})`:'上传到当前分类'}}</el-button><el-tag v-if="activeUploads" type="warning">{{activeUploads}} 批正在传到本机，可继续选图</el-tag><el-button v-if="files.length" text @click="files=[]">清空选择</el-button><el-select v-model="moveTo" placeholder="将所选图片移动到"><el-option v-for="c in categories" :key="c.id" :value="c.id" :label="c.name"/></el-select><el-button :disabled="!selected.length" @click="move">批量移动 ({{selected.length}})</el-button><el-button class="compose-selected" type="success" :disabled="!selected.length" @click="composeSelected">批量合成 ({{selected.length}})</el-button></div>
  <p class="pod-library-hint">相同文件上传时自动识别，不会重复入库；使用记录按已登记的合成记录统计。</p><div v-loading="loading" class="artwork-library-grid"><label v-for="a in items" :key="a.id" :class="{selected:selected.includes(a.id)}"><el-checkbox :model-value="selected.includes(a.id)" @change="(v:boolean)=>selected=v?[...selected,a.id]:selected.filter(x=>x!==a.id)"/><a :href="`/api/v1/pod/artworks/${a.id}/file`" target="_blank"><img :src="a.thumbnail_url" loading="lazy"/></a><strong>{{a.name}}</strong><small>{{a.category_name||'未分类'}} · {{a.file_size?Math.round(a.file_size/1024/1024)+' MB':'-'}}</small><small>上传于 {{formatUploadDate(a.uploaded_at)}}</small><el-tag v-if="a.used_by_selected" size="small" type="danger">当前产品已使用</el-tag><el-tag v-else size="small" :type="a.used_product_count?'warning':'success'">{{a.used_product_count?`已用于 ${a.used_product_count} 类产品`:'尚未使用'}}</el-tag><el-button link type="primary" @click.prevent="showUsage(a)">查看使用记录</el-button></label></div><el-empty v-if="!loading&&!items.length" description="没有找到图案"/><el-pagination v-model:current-page="page" :page-size="60" :total="total" layout="prev, pager, next, total" @current-change="load"/>
</div></section><el-dialog v-model="usageDialog" :title="`使用记录 · ${usageArtwork?.name||''}`" width="800px"><el-empty v-if="!usageRows.length" description="尚无使用记录"/><el-table v-else :data="usageRows" max-height="500"><el-table-column prop="product_name" label="产品" min-width="200"/><el-table-column label="货盘" width="100"><template #default="{row}">{{row.market==='US'?'美国':'原有货盘'}}</template></el-table-column><el-table-column prop="product_key" label="供应商产品" width="150"/><el-table-column prop="product_code" label="合成编号" min-width="180"/><el-table-column prop="view_name" label="印刷面" width="100"/><el-table-column label="状态" width="110"><template #default="{row}"><el-tag :type="row.counts_as_used?'success':'info'">{{row.counts_as_used?'已使用':'未计入'}}</el-tag></template></el-table-column><el-table-column label="日期" width="120"><template #default="{row}">{{formatUploadDate(row.created_at)}}</template></el-table-column></el-table></el-dialog><el-dialog v-model="categoryDialog" title="设计图库分类管理" width="760px"><div class="category-create"><el-input v-model="newCategory" maxlength="100" show-word-limit placeholder="输入新分类名称" @keyup.enter="createCategory"/><el-button type="primary" @click="createCategory">新建分类</el-button></div><div class="category-summary">共 {{categories.length}} 个分类，{{total}} 张设计图。分类内有图片时不能直接删除。</div><div class="category-list"><div v-for="c in categories" :key="c.id" class="category-row"><div class="category-icon">{{c.is_system?'默':'类'}}</div><div class="category-main"><strong v-if="c.is_system">{{c.name}}</strong><el-input v-else v-model="categoryNames[c.id]" maxlength="100" @keyup.enter="renameCategory(c)"/><small>{{c.count??0}} 张图片<span v-if="c.is_system"> · 系统分类</span></small></div><div class="category-actions"><el-button link type="primary" @click="enterCategory(c)">查看图片</el-button><template v-if="!c.is_system"><el-button link :disabled="categoryNames[c.id]===c.name" @click="renameCategory(c)">保存名称</el-button><el-tooltip :content="c.count?'分类内有图片，请先批量移动':'删除空分类'"><span><el-button link type="danger" :disabled="Boolean(c.count)" @click="deleteCategory(c)">删除</el-button></span></el-tooltip></template></div></div></div></el-dialog><el-dialog v-model="uploadDialog" title="上传任务登记结果" width="720px"><el-table :data="uploadResults" max-height="500"><el-table-column prop="filename" label="文件" min-width="300" show-overflow-tooltip/><el-table-column label="结果" width="110"><template #default="{row}"><el-tag :type="row.status==='queued'?'success':'danger'">{{row.status==='queued'?'已进入后台':'登记失败'}}</el-tag></template></el-table-column><el-table-column prop="message" label="说明" min-width="220"/></el-table><template #footer><span v-if="files.length">仍有 {{files.length}} 个未登记文件保留，可关闭后重试。</span><el-button @click="uploadDialog=false">关闭</el-button></template></el-dialog></div></template>
<style scoped>.pod-product-picker{display:flex;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap}.pod-product-picker .el-select{width:min(560px,100%)}.pod-product-picker .market-picker{width:200px}.pod-product-picker span,.pod-library-hint{color:#78859a;font-size:13px}.pod-library-toolbar,.pod-library-actions{display:flex;gap:10px;align-items:center;margin-bottom:12px}.pod-library-toolbar .el-input{max-width:360px}.pod-library-actions .el-select{width:220px}.compose-selected{font-weight:700}.artwork-library-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;min-height:220px}.artwork-library-grid>label{position:relative;display:flex;flex-direction:column;gap:5px;border:1px solid #e4eaf2;border-radius:10px;padding:7px;background:white}.artwork-library-grid>label.selected{border-color:#1d64f2;box-shadow:0 0 0 2px #dce8ff}.artwork-library-grid .el-checkbox{position:absolute;z-index:2;top:10px;left:10px;background:#fff;border-radius:4px;padding:2px}.artwork-library-grid img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:7px}.artwork-library-grid strong{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.artwork-library-grid small,.category-summary,.category-main small{color:#78859a}.compact{min-width:190px}.category-create{display:flex;gap:10px;padding:14px;background:#f5f8fc;border:1px solid #e1e8f0;border-radius:12px}.category-summary{padding:14px 2px 8px}.category-list{display:grid;gap:8px;max-height:520px;overflow:auto}.category-row{display:grid;grid-template-columns:44px minmax(220px,1fr) auto;align-items:center;gap:12px;padding:12px;border:1px solid #e1e8f0;border-radius:12px}.category-icon{width:40px;height:40px;border-radius:10px;background:#eaf2ff;color:#1764d9;display:grid;place-items:center;font-weight:700}.category-main{display:grid;gap:5px}.category-main .el-input{max-width:360px}.category-actions{display:flex;align-items:center;white-space:nowrap}@media(max-width:800px){.pod-library-toolbar,.pod-library-actions{flex-wrap:wrap}.artwork-library-grid{grid-template-columns:repeat(2,1fr)}.category-row{grid-template-columns:40px 1fr}.category-actions{grid-column:2;flex-wrap:wrap}}</style>
