<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
const props=defineProps<{batchId?:string;batchIds?:string[]}>()
const bundles=ref<any[]>([]),loading=ref(false)
const ids=()=>props.batchIds?.length ? props.batchIds : props.batchId ? [props.batchId] : []
const prompt=()=>bundles.value.map((bundle,index)=>`第 ${index+1}/${bundles.value.length} 批 · ${ids()[index]}\n${bundle.prompt}`).join('\n\n---\n\n')
async function load(){loading.value=true;try{bundles.value=await Promise.all(ids().map(id=>api.get(`/codex-handoffs/batches/${id}`)))}catch(e:any){ElMessage.error(e.message)}finally{loading.value=false}}
async function copy(){try{await navigator.clipboard.writeText(prompt());ElMessage.success('已复制全部批次，请粘贴到原执行电脑的新 Codex 对话')}catch{ElMessage.warning('请展开任务说明手动复制')}}
function download(){const url=URL.createObjectURL(new Blob([JSON.stringify(bundles.value,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=`codex-${ids()[0]}${ids().length>1 ? '-all' : ''}.json`;a.click();URL.revokeObjectURL(url)}
watch(()=>ids().join(','),load,{immediate:true})
</script>
<template><el-card v-loading="loading" style="margin-bottom:20px"><h3>Codex 接管任务</h3><p>请在原发起电脑打开 Codex，将任务说明粘贴到新对话。按任务说明完成文案、属性及所需图片，再继续原审核流程。</p><template v-if="bundles.length"><p>{{bundles.reduce((count,bundle)=>count+(bundle.jobs?.length||0),0)}} 个任务 · {{bundles.length}} 批</p><el-button type="primary" @click="copy">复制给新 Codex 对话</el-button><el-button @click="download">下载任务说明</el-button><el-button @click="load">刷新状态</el-button><el-collapse><el-collapse-item title="展开任务说明"><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{{prompt()}}</pre></el-collapse-item></el-collapse></template></el-card></template>
