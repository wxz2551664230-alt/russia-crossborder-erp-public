<script setup lang="ts">
import { onMounted,reactive,ref } from 'vue'; import { ElMessage } from 'element-plus'; import { api } from '../api'; import type { Store } from '../types'
const items=ref<Store[]>([]),open=ref(false),editing=ref<Store>(),testing=ref(''),testingAds=ref(''),saving=ref(false),testJobs=ref<Record<string,string>>({})
const form=reactive<any>({platform:'ozon',name:'',external_account_id:'',offer_prefix:'',currency:'RUB',enabled:true,config:{},token:'',performance_client_id:'',performance_client_secret:'',lwa_client_id:'',lwa_client_secret:'',refresh_token:'',shipping_template:''})
async function load(){items.value=(await api.get<any>('/stores')).items}
function add(){editing.value=undefined;Object.assign(form,{platform:'ozon',name:'',external_account_id:'',offer_prefix:'',currency:'RUB',enabled:true,config:{},token:'',performance_client_id:'',performance_client_secret:'',lwa_client_id:'',lwa_client_secret:'',refresh_token:'',shipping_template:''});open.value=true}
function edit(s:Store){editing.value=s;Object.assign(form,{...s,token:'',performance_client_id:'',performance_client_secret:'',lwa_client_id:'',lwa_client_secret:'',refresh_token:'',shipping_template:s.config?.merchant_shipping_group||''});open.value=true}
async function save(){const secrets:Record<string,string>={};if(form.token&&form.platform!=='amazon')secrets[form.platform==='ozon'?'api_key':'token']=form.token;if(form.performance_client_id)secrets.performance_client_id=form.performance_client_id;if(form.performance_client_secret)secrets.performance_client_secret=form.performance_client_secret;if(form.platform==='amazon')for(const key of ['lwa_client_id','lwa_client_secret','refresh_token'])if(form[key])secrets[key]=form[key];const body={platform:form.platform,name:form.name,external_account_id:form.external_account_id,offer_prefix:form.offer_prefix,currency:form.platform==='amazon'?'USD':form.currency,enabled:form.enabled,config:form.platform==='amazon'?{...(form.config||{}),region:'NA',marketplace_id:'ATVPDKIKX0DER',country:'US',fulfillment_channel:'DEFAULT',merchant_shipping_group:form.shipping_template||''}:form.config||{},secrets};saving.value=true;try{if(editing.value)await api.patch(`/stores/${editing.value.id}`,body);else await api.post('/stores',body);open.value=false;ElMessage.success('店铺已保存');await load()}catch(error:any){ElMessage.error(error?.message||'店铺保存失败')}finally{saving.value=false}}
async function test(s:Store){testing.value=s.id;try{let result=await api.post<any>(`/stores/${s.id}/test`);if(result.job_id){const jobId=String(result.job_id);testJobs.value={...testJobs.value,[s.id]:jobId};ElMessage.info({message:`${s.name} 连接测试已排队，可在任务中心查看`,duration:6000});const deadline=Date.now()+120000;let completed=false;while(Date.now()<deadline){await new Promise(resolve=>setTimeout(resolve,2000));const job=await api.get<any>(`/jobs/${jobId}`);if(job.status==='completed'){result=job.steps?.[0]?.details||{};completed=true;break}if(['failed','cancelled','action_required'].includes(job.status))throw new Error(job.error_message||'Seller API 连接测试失败')}if(!completed){ElMessage.warning({message:`${s.name} 连接测试仍在排队或执行，请点击该店铺的“查看测试任务”跟踪结果`,duration:8000});return}}ElMessage.success(result.note||(s.platform==='amazon'?'Amazon 美国站授权读取成功；上架权限需通过类目读取与平台预检核验':`Seller API 连接成功，读取到 ${result.category_roots} 个类目根节点`))}catch(error:any){ElMessage.error(error?.message||'Seller API 连接失败')}finally{testing.value=''}}
async function testAds(s:Store){testingAds.value=s.id;try{const result=await api.post<any>(`/stores/${s.id}/test-performance`);ElMessage.success(`广告 API 连接成功，读取到 ${result.campaign_count} 个计划`)}finally{testingAds.value=''}}
function hasSecret(s:Store,key:string){return Boolean(s.secret_status?.[key]?.configured)}
function sellerConfigured(s:Store){return s.platform==='amazon'?['lwa_client_id','lwa_client_secret','refresh_token'].every(k=>hasSecret(s,k)):hasSecret(s,s.platform==='ozon'?'api_key':'token')}
onMounted(load)
</script>
<template>
  <div class="panel">
    <div class="panel-head"><div><h2>店铺管理</h2><p>分别配置 Seller API 与广告 Performance API，所有密钥均加密保存</p></div><el-button type="primary" @click="add">添加店铺</el-button></div>
    <el-table :data="items">
      <el-table-column prop="platform" label="平台" width="120" />
      <el-table-column prop="name" label="店铺" min-width="160" />
      <el-table-column prop="offer_prefix" label="Offer ID / SKU 前缀" min-width="180"/><el-table-column prop="external_account_id" label="Seller Client ID" min-width="180" />
      <el-table-column label="Seller API" width="120"><template #default="s"><el-tag :type="sellerConfigured(s.row)?'success':'warning'">{{sellerConfigured(s.row)?'已配置':'未配置'}}</el-tag></template></el-table-column>
      <el-table-column label="广告 API" width="120"><template #default="s"><el-tag :type="hasSecret(s.row,'performance_client_id')&&hasSecret(s.row,'performance_client_secret')?'success':'warning'">{{hasSecret(s.row,'performance_client_id')&&hasSecret(s.row,'performance_client_secret')?'已配置':'未配置'}}</el-tag></template></el-table-column>
      <el-table-column label="启用" width="80"><template #default="s"><el-tag :type="s.row.enabled?'success':'info'">{{s.row.enabled?'是':'否'}}</el-tag></template></el-table-column>
      <el-table-column label="操作" width="310"><template #default="s"><el-button link @click.stop="edit(s.row)">编辑</el-button><el-button link type="primary" :loading="testing===s.row.id" @click.stop="test(s.row)">测试 Seller</el-button><RouterLink v-if="testJobs[s.row.id]" :to="`/jobs?job=${testJobs[s.row.id]}`">查看测试任务</RouterLink><el-button v-if="s.row.platform==='ozon'" link type="success" :loading="testingAds===s.row.id" :disabled="!hasSecret(s.row,'performance_client_id')||!hasSecret(s.row,'performance_client_secret')" @click.stop="testAds(s.row)">测试广告</el-button></template></el-table-column>
    </el-table>

    <el-dialog v-model="open" :title="editing?'编辑店铺':'添加店铺'" width="680">
      <el-form label-width="150">
        <el-form-item label="平台"><el-select v-model="form.platform"><el-option label="OZON" value="ozon"/><el-option label="Wildberries" value="wildberries"/><el-option label="Amazon 美国 · FBM" value="amazon"/></el-select></el-form-item>
        <el-form-item label="店铺名称"><el-input v-model="form.name"/></el-form-item>
        <template v-if="form.platform==='ozon'">
          <el-divider content-position="left">Seller API · 商品与订单</el-divider>
          <el-form-item label="Seller Client ID"><el-input v-model="form.external_account_id" placeholder="Ozon Seller API Client ID"/></el-form-item>
          <el-form-item label="Seller API Key"><el-input v-model="form.token" type="password" show-password :placeholder="editing?'留空表示不修改':'输入 Seller API Key'"/></el-form-item>
          <el-divider content-position="left">广告 Performance API · 诊断执行</el-divider>
          <el-form-item label="广告 Client ID"><el-input v-model="form.performance_client_id" :placeholder="editing?'留空表示不修改':'例如 xxx@advertising.performance.ozon.ru'"/></el-form-item>
          <el-form-item label="广告 Client Secret"><el-input v-model="form.performance_client_secret" type="password" show-password :placeholder="editing?'留空表示不修改':'输入广告 Client Secret'"/></el-form-item>
          <el-alert title="广告 API 用于同步广告报表，以及执行已确认的停投和放量操作。" type="warning" :closable="false" show-icon />
        </template>
        <template v-else-if="form.platform==='amazon'"><el-divider content-position="left">Amazon 美国站 · FBM</el-divider><el-form-item label="Seller ID"><el-input v-model="form.external_account_id" placeholder="卖家编号"/></el-form-item><el-form-item label="LWA Client ID"><el-input v-model="form.lwa_client_id" :placeholder="editing?'留空表示不修改':'应用 Client ID'"/></el-form-item><el-form-item label="LWA Client Secret"><el-input v-model="form.lwa_client_secret" type="password" show-password :placeholder="editing?'留空表示不修改':'应用 Client Secret'"/></el-form-item><el-form-item label="Refresh Token"><el-input v-model="form.refresh_token" type="password" show-password :placeholder="editing?'留空表示不修改':'卖家授权 Refresh Token'"/></el-form-item><el-form-item label="配送模板标识"><el-input v-model="form.shipping_template" placeholder="Seller Central 已配置的 FBM 配送模板标识"/></el-form-item><el-alert title="授权测试、类目读取和上架任务由本机执行端完成。请创建并选择实际 FBM 配送模板；美国货盘已接通，Amazon 订单处理另行接入。" type="info" :closable="false"/></template><template v-else><el-form-item label="Token"><el-input v-model="form.token" type="password" show-password :placeholder="editing?'留空表示不修改':'输入 Token'"/></el-form-item></template>
        <el-form-item label="Offer ID / SKU 前缀"><el-input v-model="form.offer_prefix" maxlength="60" placeholder="例如 XSZGM-，分隔符请一并填写"/><p class="form-tip">Ozon 新发布使用“前缀 + 产品SKU”；WB沿用原SKU前缀规则。已有平台商品标识不会改名。</p></el-form-item><el-form-item label="启用"><el-switch v-model="form.enabled"/></el-form-item>
      </el-form>
      <template #footer><el-button @click="open=false">取消</el-button><el-button type="primary" :loading="saving" @click="save">加密保存</el-button></template>
    </el-dialog>
  </div>
</template>
