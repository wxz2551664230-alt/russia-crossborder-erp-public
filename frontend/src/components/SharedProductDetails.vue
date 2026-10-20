<script setup lang="ts">
import { computed, ref } from 'vue'
import type { CatalogProduct, CatalogAssetLink } from '../types'
import ProductFacts from './ProductFacts.vue'
import { catalogImageUrl, safeProductUrl } from '../deployment'
const props=defineProps<{product:CatalogProduct}>()
const tab=ref('overview')
const labels:Record<string,string>={pod:'POD',follow:'OZON 跟卖',self_selected:'自主上架'}
const status:Record<string,string>={ready:'可发布',draft:'草稿',archived:'已归档'}
const roles:Record<string,string>={main:'主图',additional:'附图',print:'印刷图',design:'设计图',artwork:'设计图',mockup:'效果图',detail:'详情图',preview:'效果图'}
const images=computed(()=>props.product.assets || [])
const preview=computed(()=>images.value.map(catalogImageUrl).filter(Boolean))
function value(v:unknown){return v===undefined || v===null || v==='' ? '未提供' : v}
function dimensions(l?:number,w?:number,h?:number){return [l,w,h].map(value).join(' × ')}
const sourceUrl=computed(()=>safeProductUrl(props.product.source_url))
const description=computed(()=> {
  const s=props.product.source_snapshot || {}
  const raw=s.cn_desc || s.product_description || s.en_desc || ''
  return typeof raw==='string' ? raw.replace(/<[^>]*>/g,' ').replace(/&nbsp;/g,' ') : raw
})
function linkedSku(link:CatalogAssetLink){return props.product.variants?.find(v=>v.id===link.variant_id)?.sku || '通用'}
</script>
<template>
  <el-tabs v-model="tab" class="product-detail-tabs">
    <el-tab-pane label="基本资料" name="overview">
      <el-descriptions :column="2" border>
        <el-descriptions-item label="产品 SKU">{{ product.sku }}</el-descriptions-item><el-descriptions-item label="分组编码">{{ value(product.group_code) }}</el-descriptions-item>
        <el-descriptions-item label="来源">{{ labels[product.source_type] || product.source_type }}</el-descriptions-item><el-descriptions-item label="状态">{{ status[product.status] || product.status }}</el-descriptions-item>
        <el-descriptions-item label="供应商">{{ value(product.supplier) }}</el-descriptions-item><el-descriptions-item label="供应商产品编号">{{ value(product.supplier_product_id) }}</el-descriptions-item>
        <el-descriptions-item label="采购成本">{{ value(product.purchase_price_cny) }} {{ product.currency }}</el-descriptions-item><el-descriptions-item label="国内运费">{{ value(product.domestic_delivery_cny) }} CNY</el-descriptions-item>
        <el-descriptions-item label="包装尺寸（毫米）">{{ dimensions(product.package_length_mm,product.package_width_mm,product.package_height_mm) }}</el-descriptions-item><el-descriptions-item label="包装重量（克）">{{ value(product.package_weight_g) }}</el-descriptions-item>
        <el-descriptions-item label="分类">{{ value(product.category) }}</el-descriptions-item><el-descriptions-item label="Ozon 类目">{{ value(product.category_display) }}</el-descriptions-item>
        <el-descriptions-item label="Ozon 类目编号">{{ value(product.description_category_id) }}</el-descriptions-item><el-descriptions-item label="商品类型编号">{{ value(product.type_id) }}</el-descriptions-item>
        <el-descriptions-item label="货源地址" :span="2"><a v-if="sourceUrl" :href="sourceUrl" target="_blank" rel="noopener noreferrer">查看供应商商品</a><span v-else>未提供可打开的货源地址</span></el-descriptions-item>
      </el-descriptions>
      <div v-if="product.tags?.length" class="detail-tags"><el-tag v-for="tag in product.tags" :key="tag">{{ tag }}</el-tag></div>
      <p class="shared-hint">多规格商品的价格、重量及包装信息请同时查看“规格 / SKU”。未提供表示源商品资料中未登记该字段。</p>
      <section v-if="description" class="product-description"><h3>商品说明</h3><p v-if="typeof description==='string'">{{ description }}</p><ProductFacts v-else :value="description" /></section>
      <el-collapse v-if="product.metadata && Object.keys(product.metadata).length"><el-collapse-item title="补充资料"><ProductFacts :value="product.metadata" /></el-collapse-item></el-collapse>
    </el-tab-pane>
    <el-tab-pane :label="`规格 / SKU（${product.variants?.length || 0}）`" name="variants" lazy>
      <el-table :data="product.variants || []" row-key="id" empty-text="本商品未登记变体">
        <el-table-column type="expand"><template #default="{row}"><div class="variant-more"><el-descriptions :column="2" border><el-descriptions-item label="供应商产品编号">{{ value(row.supplier_product_id) }}</el-descriptions-item><el-descriptions-item label="供应商 SKU">{{ value(row.supplier_sku_id) }}</el-descriptions-item><el-descriptions-item label="国内运费">{{ value(row.domestic_delivery_cny) }} CNY</el-descriptions-item><el-descriptions-item label="包装尺寸（毫米）">{{ dimensions(row.package_length_mm,row.package_width_mm,row.package_height_mm) }}</el-descriptions-item><el-descriptions-item label="条形码">{{ value(row.barcode) }}</el-descriptions-item></el-descriptions><ProductFacts v-if="row.properties && Object.keys(row.properties).length" :value="row.properties" /></div></template></el-table-column>
        <el-table-column prop="sku" label="SKU" min-width="170" /><el-table-column prop="specification" label="规格" min-width="150" /><el-table-column prop="color" label="颜色" min-width="90" /><el-table-column prop="size" label="尺码" min-width="100" />
        <el-table-column label="采购价（CNY）" width="130"><template #default="{row}">{{ value(row.purchase_price_cny) }}</template></el-table-column><el-table-column label="重量（克）" width="100"><template #default="{row}">{{ value(row.weight_grams) }}</template></el-table-column><el-table-column label="状态" width="85"><template #default="{row}">{{ row.active ? '启用' : '停用' }}</template></el-table-column>
      </el-table>
    </el-tab-pane>
    <el-tab-pane :label="`商品图片（${images.length}）`" name="images" lazy>
      <div v-if="images.length" class="product-gallery"><article v-for="link in images" :key="link.id">
        <el-image v-if="catalogImageUrl(link)" :src="catalogImageUrl(link)" :preview-src-list="preview" :initial-index="preview.indexOf(catalogImageUrl(link))" preview-teleported fit="contain" lazy><template #error><div class="image-missing">图片暂时无法加载</div></template></el-image><div v-else class="image-missing">暂无可用图片</div>
        <div class="gallery-caption"><el-tag size="small">{{ roles[link.role] || link.role }}</el-tag><span>{{ linkedSku(link) }}</span><small>{{ link.asset.name }}</small></div>
      </article></div><el-empty v-else description="本商品未登记图片" />
    </el-tab-pane>
    <el-tab-pane label="生产资料" name="production" lazy><ProductFacts :value="product.production || {}" /></el-tab-pane>
    <el-tab-pane label="供应商资料 / 导入记录" name="source" lazy><p class="shared-hint">此处展示已保存的供应商资料或导入记录。导入文件名称仅用于追溯来源；记录中未包含的供应商资料不会自动补齐。</p><ProductFacts :value="product.source_snapshot || {}" /></el-tab-pane>
  </el-tabs>
</template>
<style scoped>.product-detail-tabs{margin-top:20px}.detail-tags{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}.product-description p{white-space:pre-wrap;line-height:1.8;overflow-wrap:anywhere}.variant-more{padding:18px}.product-gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:16px}.product-gallery article{border:1px solid #e2e8f0;border-radius:8px;overflow:hidden}.product-gallery .el-image{width:100%;height:200px;background:#f8fafc}.image-missing{height:200px;display:flex;align-items:center;justify-content:center;color:#94a3b8}.gallery-caption{padding:12px;display:flex;gap:8px;flex-wrap:wrap;overflow-wrap:anywhere}.gallery-caption small{width:100%;color:#64748b}</style>
