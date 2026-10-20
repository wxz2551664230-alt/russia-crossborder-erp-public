<script setup lang="ts">
import { computed } from 'vue'
defineOptions({name:'AmazonSchemaInput'})
const props=defineProps<{node:any;definitions:any;modelValue:any;label?:string}>()
const emit=defineEmits<{(event:'update:modelValue',value:any):void}>()
const schema=computed(()=>{let n=props.node||{};if(n.$ref?.startsWith('#/$defs/'))n={...props.definitions?.[n.$ref.slice(8)],...n};return n})
const type=computed(()=>schema.value.type||'string')
const options=computed(()=>(schema.value.enum||[]).map((v:any,i:number)=>({value:v,label:schema.value.enumNames?.[i]||String(v)})))
const fields=computed(()=>Object.entries(schema.value.properties||{}).filter(([k,v]:any)=>!['marketplace_id','language_tag'].includes(k)&&!v.hidden))
function setField(k:string,v:any){emit('update:modelValue',{...(props.modelValue||{}),[k]:v})}
function setItem(i:number,v:any){const values=[...(Array.isArray(props.modelValue)?props.modelValue:[undefined])];values[i]=v;emit('update:modelValue',values)}
</script>
<template>
  <div class="amz-schema-input">
    <template v-if="type==='array'"><AmazonSchemaInput v-for="(row,i) in (Array.isArray(modelValue)?modelValue:[undefined])" :key="i" :node="schema.items" :definitions="definitions" :model-value="row" @update:model-value="setItem(i,$event)"/></template>
    <template v-else-if="type==='object'"><div v-for="([k,n]) in fields" :key="k" class="field"><small>{{ (n as any).title||k }}{{schema.required?.includes(k)?' *':''}}</small><AmazonSchemaInput :node="n" :definitions="definitions" :model-value="modelValue?.[k]" @update:model-value="setField(k,$event)"/></div></template>
    <el-select-v2 v-else-if="options.length" :model-value="modelValue" :options="options" filterable clearable style="width:100%" @update:model-value="emit('update:modelValue',$event)"/>
    <el-input-number v-else-if="type==='number'||type==='integer'" :model-value="modelValue" :min="schema.minimum" :max="schema.maximum" :step="type==='integer'?1:.1" @update:model-value="emit('update:modelValue',$event)"/>
    <el-select v-else-if="type==='boolean'" :model-value="modelValue" placeholder="请选择" @update:model-value="emit('update:modelValue',$event)"><el-option label="是" :value="true"/><el-option label="否" :value="false"/></el-select>
    <el-input v-else :model-value="modelValue" :placeholder="schema.description||label" @update:model-value="emit('update:modelValue',$event)"/>
  </div>
</template>
<style scoped>.field{margin:6px 0}.field small{display:block;color:#64748b;margin-bottom:5px}</style>
