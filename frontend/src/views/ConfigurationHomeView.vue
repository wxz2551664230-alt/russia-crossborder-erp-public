<script setup lang="ts">
import {computed,inject,onMounted,ref} from 'vue'
import {api} from '../api'
import type {Store} from '../types'
const currentUser=inject<any>('erpCurrentUser'),accountMode=inject<any>('erpAccountMode')
const canManage=computed(()=>accountMode?.value===false||currentUser?.value?.role==='admin')
const stores=ref<Store[]>([]),loading=ref(false)
const entries=[{title:'店铺管理',description:'店铺账号、Offer ID / SKU 前缀、API 凭据和启用状态',path:'/stores'},{title:'接口中心',description:'文字AI、生图AI、OSS及供应商连接',path:'/connections'},{title:'业务与系统设置',description:'定价、财务、文案、图片、AI模型与并发参数',path:'/settings'},{title:'用户与操作记录',description:'团队账号、角色和操作记录',path:'/users'}]
onMounted(async()=>{loading.value=true;try{stores.value=(await api.get<any>('/stores')).items||[]}finally{loading.value=false}})
</script>
<template><div class="page-stack"><section class="panel"><div class="panel-head"><div><h2>系统配置</h2><p>统一查看店铺与业务配置入口。{{canManage?'管理员可进入下方页面修改配置。':'当前账号可查看店铺配置；默认参数和账号凭据由管理员维护。'}}</p></div></div><div class="configuration-grid"><article v-for="entry in entries" :key="entry.path" class="configuration-card"><h3>{{entry.title}}</h3><p>{{entry.description}}</p><RouterLink v-if="canManage" :to="entry.path">打开配置</RouterLink><span v-else class="form-tip">需要管理员账号</span></article><article class="configuration-card"><h3>我的执行电脑</h3><p>查看当前电脑连接和本地执行状态。</p><RouterLink to="/execution-devices">打开执行电脑</RouterLink></article></div></section><section class="panel"><div class="panel-head"><div><h2>店铺前缀一览</h2><p>Ozon使用“前缀 + 产品SKU”，分隔符按配置原样保留。</p></div><RouterLink v-if="canManage" to="/stores">编辑店铺前缀</RouterLink></div><el-table v-loading="loading" :data="stores"><el-table-column prop="name" label="店铺"/><el-table-column prop="platform" label="平台"/><el-table-column label="Offer ID / SKU 前缀"><template #default="{row}">{{row.offer_prefix||'未配置'}}</template></el-table-column><el-table-column label="启用"><template #default="{row}">{{row.enabled?'是':'否'}}</template></el-table-column></el-table></section></div></template>
<style scoped>.configuration-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px;padding:20px}.configuration-card{padding:18px;border:1px solid #e1e8f2;border-radius:10px}.configuration-card h3{margin:0 0 9px}.configuration-card p{color:#65758a;line-height:1.6}</style>
