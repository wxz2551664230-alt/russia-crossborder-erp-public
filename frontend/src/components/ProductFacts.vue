<script setup lang="ts">
import { computed, reactive } from 'vue'
const props=defineProps<{value:unknown}>()
const opened=reactive<Record<string,boolean>>({})
const entries=computed<Array<[string,unknown]>>(()=>props.value && typeof props.value==='object' ? Object.entries(props.value) : [['内容',props.value]])
const labels:Record<string,string>={cn_name:'中文名称',en_name:'英文名称',cn_desc:'中文说明',en_desc:'英文说明',product_description:'商品说明',colors:'颜色',sizes:'尺码',prices:'价格',stock_info:'库存资料',size_attr_map:'尺码属性映射',size_attr_info:'尺码属性',supplier:'供应商',supplier_name:'供应商名称',product_type_id:'产品款式编号',default_color_id:'默认颜色编号',default_view_id:'默认视角编号',product_code:'产品编码',version_id:'版本编号',version_no:'版本号',recipe:'生产配置',assets:'素材',images:'图片',copywriting:'商品文案',title:'标题',description:'描述',description_ru:'俄文描述',attributes:'属性',name:'名称',value:'值',code:'编码',material:'材质',weight:'重量',width:'宽度',height:'高度',length:'长度',size:'尺码',color:'颜色',barcode:'条形码',price:'价格',currency:'币种',quantity:'数量',renderings_info:'效果图资料',detail_img:'详情图',produce_area:'生产地区',produce_country:'生产国家',warehouse_name:'仓库',production_cycle:'生产周期',print_area:'印刷区域',print_size:'印刷尺寸',specification:'规格',metadata:'补充资料'}
function nested(value:unknown){return value!==null && typeof value==='object'}
function localPath(value:unknown){return typeof value==='string' && /^(?:[a-z]:[\\/]|file:|\\\\)/i.test(value)}
function display(value:unknown){return value===null || value===undefined || value==='' ? '未提供' : String(value).replace(/<[^>]*>/g,' ').replace(/&nbsp;/g,' ')}
</script>
<template>
  <dl class="product-facts">
    <div v-for="[key,item] in entries" :key="key" class="fact-row">
      <template v-if="nested(item)"><details @toggle="opened[key]=($event.target as HTMLDetailsElement).open"><summary>{{ labels[key] || key }} <span>（{{ Object.keys(item as object).length }} 项）</span></summary><ProductFacts v-if="opened[key]" :value="item" /></details></template>
      <template v-else><dt>{{ key==='workbook' ? '原始导入文件' : labels[key] || key }}</dt><dd v-if="localPath(item)">{{ String(item).split(/[\\/]/).pop() }}<small style="display:block;color:#64748b">历史本机来源记录，不是云端文件入口。此处路径不能用于判断原文件是否已归档。</small></dd><dd v-else>{{ display(item) }}</dd></template>
    </div>
  </dl>
</template>
<style scoped>.product-facts{margin:0}.fact-row{border-bottom:1px solid #edf0f4;padding:10px 0;overflow-wrap:anywhere}.fact-row dt{color:#64748b;font-size:12px;margin-bottom:5px}.fact-row dd{margin:0;white-space:pre-wrap;line-height:1.65}.fact-row summary{cursor:pointer;font-weight:600}.fact-row summary span{font-size:12px;color:#64748b;font-weight:400}.fact-row details>.product-facts{padding-left:18px}</style>
