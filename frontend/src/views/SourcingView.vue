<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { ExternalLink, Search, ShoppingBag } from "lucide-vue-next";
import { api } from "../api";
import type { ResearchProduct, SourcingCandidate } from "../types";

const products = ref<ResearchProduct[]>([]);
const detail = ref<ResearchProduct>();
const dialog = ref(false);
const variants = ref<any[]>([]);
const loading = ref(false);
const loadingVariants = ref(false);
const variantHint = ref("");
const search = ref("");
const statusFilter = ref("pending");
const form = reactive({
  provider: "manual",
  external_product_id: "",
  external_sku_id: "",
  title: "",
  sku_name: "",
  image_url: "",
  source_url: "",
  unit_price_cny: undefined as number | undefined,
  domestic_delivery_cny: 0,
  color: "",
  size: "",
  remark: "",
  minimum_order_quantity: 1,
  confidence: 1,
  raw: {} as Record<string, unknown>,
});

const pendingStatuses = new Set([
  "category_confirmed",
  "pending_sourcing",
  "sourcing_queued",
  "pending_source_confirmation",
]);
const visibleProducts = computed(() => {
  const token = search.value.trim().toLocaleLowerCase();
  return products.value.filter((item) => {
    const confirmed = item.status === "source_confirmed";
    if (statusFilter.value === "pending" && confirmed) return false;
    if (statusFilter.value === "confirmed" && !confirmed) return false;
    if (!token) return true;
    return `${item.source_sku} ${item.title}`.toLocaleLowerCase().includes(token);
  });
});
const pendingCount = computed(
  () => products.value.filter((item) => item.status !== "source_confirmed").length,
);
const confirmedCount = computed(
  () => products.value.filter((item) => item.status === "source_confirmed").length,
);
const highImage = computed(() =>
  String(detail.value?.image_url || "").replace(/\/wc\d+\//i, "/wc2000/"),
);
const selectedCandidate = computed(() =>
  detail.value?.sourcing_candidates?.find((item) => item.selected),
);

async function boot() {
  loading.value = true;
  try {
    products.value = (
      await api.get<any>("/research-products?page_size=200")
    ).items;
  } finally {
    loading.value = false;
  }
}
async function loadDetail(product: ResearchProduct) {
  detail.value = await api.get(`/research-products/${product.id}`);
}
function resetForm() {
  Object.assign(form, {
    provider: "manual",
    external_product_id: "",
    external_sku_id: "",
    title: "",
    sku_name: "",
    image_url: "",
    source_url: "",
    unit_price_cny: undefined,
    domestic_delivery_cny: 0,
    color: "",
    size: "",
    remark: "",
    minimum_order_quantity: 1,
    confidence: 1,
    raw: {},
  });
  variants.value = [];
  variantHint.value = "";
}
async function copyImage() {
  if (!highImage.value) return ElMessage.warning("该商品没有可复制的主图链接");
  try {
    await navigator.clipboard.writeText(highImage.value);
    ElMessage.success("OZON 高清主图链接已复制");
  } catch {
    ElMessage.warning("浏览器未允许复制，请右键商品图片复制地址");
  }
}
async function begin(product: ResearchProduct) {
  await loadDetail(product);
  resetForm();
  form.title = detail.value!.title;
  form.image_url = detail.value!.image_url;
  const existing = selectedCandidate.value;
  if (existing) {
    form.external_product_id = existing.external_product_id;
    form.external_sku_id = existing.external_sku_id;
    form.title = existing.title || detail.value!.title;
    form.sku_name = existing.sku_name;
    form.image_url = existing.image_url || detail.value!.image_url;
    form.source_url = existing.source_url;
    form.unit_price_cny = existing.unit_price_cny;
    form.domestic_delivery_cny = existing.domestic_delivery_cny || 0;
    form.color = existing.color || "";
    form.size = existing.size || "";
    form.remark = existing.remark || "";
  }
  dialog.value = true;
}
async function copyAndOpen1688() {
  await copyImage();
  window.open("https://www.1688.com/", "_blank", "noopener");
}
function openOzon() {
  if (!detail.value?.ozon_url) return ElMessage.warning("该商品没有 OZON 页面链接");
  window.open(detail.value.ozon_url, "_blank", "noopener");
}
function itemId() {
  const value = form.source_url.trim();
  return value.match(/\/offer\/(\d+)/i)?.[1]
    || value.match(/[?&](?:offerId|itemId)=(\d+)/i)?.[1]
    || form.external_product_id.trim();
}
async function loadVariants() {
  const id = itemId();
  if (!id) return ElMessage.warning("请先填写正确的 1688 产品链接");
  loadingVariants.value = true;
  try {
    const result = await api.post<any>(
      `/research-products/${detail.value!.id}/manual-source/variants`,
      { source_url: form.source_url, item_id: id },
    );
    form.external_product_id = result.item_id;
    variants.value = result.items;
    if (variants.value.length === 1) {
      chooseVariant(variants.value[0], false);
      variantHint.value = "该商品只有一个规格，已自动识别并选中";
    } else {
      variantHint.value = `已读取 ${variants.value.length} 个规格，请选择与 OZON 商品一致的规格`;
    }
  } catch (error: any) {
    variantHint.value = "当前没有 Alibaba 规格接口，请手工填写与1688页面一致的规格名称";
    ElMessage.warning(error.message || "无法自动读取规格，已切换为手工填写");
  } finally {
    loadingVariants.value = false;
  }
}
function chooseVariant(value: any, collapse = true) {
  form.external_sku_id = value.sku_id;
  form.sku_name = value.sku_name || "默认规格";
  if (value.price) form.unit_price_cny = Number(value.price);
  variantHint.value = `已选择：${form.sku_name}`;
  if (collapse) variants.value = [];
}
async function sourceUrlChanged() {
  const id = itemId();
  if (!id || (id === form.external_product_id && !!form.external_sku_id)) return;
  form.external_product_id = id;
  form.external_sku_id = "";
  variants.value = [];
  variantHint.value = `已从链接识别1688商品 ID：${id}；请继续手工填写规格名称`;
}
watch(() => form.source_url, (value) => {
  const id = value.match(/\/offer\/(\d+)/i)?.[1]
    || value.match(/[?&](?:offerId|itemId)=(\d+)/i)?.[1];
  if (id) form.external_product_id = id;
});
function openGalleryCapture() {
  const value = detail.value?.ozon_url;
  if (!value) return;
  const url = new URL(value);
  url.searchParams.set("__ozon_gallery_bridge", "1");
  url.searchParams.set("__erp_source_sku", String(detail.value?.source_sku || ""));
  url.hash = "ozon-gallery-bridge-auto-close";
  window.open(url.toString(), "_blank", "noopener");
}
async function confirmSource() {
  const id = itemId();
  if (!id || !form.sku_name || !form.unit_price_cny)
    return ElMessage.warning(
      "请填写1688链接、准确规格名称和采购价",
    );
  form.external_product_id = id;
  form.raw = {
    skuName: form.sku_name,
    title: form.title,
    color: form.color,
    size: form.size,
    remark: form.remark,
    supplier_sku_id_pending: !form.external_sku_id,
  };
  const candidate = await api.post<any>(
    `/research-products/${detail.value!.id}/sourcing-candidates`,
    form,
  );
  const selected = await api.post<any>(
    `/research-products/${detail.value!.id}/sourcing-candidates/${candidate.id}/select`,
  );
  openGalleryCapture();
  dialog.value = false;
  ElMessage.success(selected.jobs?.length ? "货源已确认，各店铺独立文案和主图任务已进入后台" : "货源已确认，OZON 图库正在由插件回传");
  await boot();
}
async function removeNoSource() {
  if (!detail.value) return;
  await ElMessageBox.confirm(
    `确认“${detail.value.title}”没有合适货源并从ERP本地删除？不会删除OZON、ECS、OSS或旧程序数据。`,
    "无合适货源",
    { type: "warning" },
  );
  await api.delete(`/research-products/${detail.value.id}`);
  dialog.value = false;
  detail.value = undefined;
  await boot();
  ElMessage.success("已删除 ERP 本地流程记录");
}
function statusText(product: ResearchProduct) {
  if (product.status === "source_confirmed") return "货源已确认";
  if (pendingStatuses.has(product.status)) return "等待人工核验";
  return product.status || "等待人工核验";
}
onMounted(boot);
</script>

<template>
  <div class="page-stack sourcing-page">
    <section class="sourcing-summary">
      <div><strong>{{ pendingCount }}</strong><span>待人工核验</span></div>
      <div><strong>{{ confirmedCount }}</strong><span>已确认货源</span></div>
      <div class="summary-note">
        <Search :size="20" />
        <span>仅采用人工找同款，不执行自动找源</span>
      </div>
    </section>

    <section class="panel sourcing-list-panel">
      <div class="panel-head sourcing-head">
        <div>
          <h2>货源人工核验</h2>
          <p>所有待核验商品集中展示，点击“开始核验”即可录入准确的1688商品和SKU。</p>
        </div>
        <div class="sourcing-filters">
          <el-input v-model="search" clearable placeholder="搜索 OZON SKU 或商品名称" />
          <el-segmented v-model="statusFilter" :options="[{label:`待核验 ${pendingCount}`,value:'pending'},{label:`已确认 ${confirmedCount}`,value:'confirmed'},{label:'全部',value:'all'}]" />
          <el-button @click="boot">刷新</el-button>
        </div>
      </div>
      <el-alert title="首次使用需在 Edge 加载 ERP 专用图库扩展：browser-extensions/ozon-gallery-bridge" type="info" :closable="false" show-icon />
      <el-table v-loading="loading" :data="visibleProducts" row-key="id" class="sourcing-table">
        <el-table-column label="OZON 商品" min-width="460">
          <template #default="scope">
            <div class="product-row">
              <el-image :src="String(scope.row.image_url||'').replace(/\/wc\d+\//i,'/wc2000/')" fit="cover" lazy />
              <div>
                <b>{{ scope.row.title }}</b>
                <small>OZON SKU {{ scope.row.source_sku }}</small>
                <a v-if="scope.row.ozon_url" :href="scope.row.ozon_url" target="_blank" @click.stop><ExternalLink :size="14" />查看 OZON 页面</a>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类目" min-width="180"><template #default="scope">{{ scope.row.category_display || scope.row.category || "待确认" }}</template></el-table-column>
        <el-table-column label="重量" width="100"><template #default="scope">{{ scope.row.weight_grams ? `${scope.row.weight_grams} g` : "-" }}</template></el-table-column>
        <el-table-column label="状态" width="150"><template #default="scope"><el-tag :type="scope.row.status==='source_confirmed'?'success':'warning'">{{ statusText(scope.row) }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="150" fixed="right"><template #default="scope"><el-button type="primary" @click="begin(scope.row)">{{ scope.row.status==='source_confirmed'?'查看或修改':'开始核验' }}</el-button></template></el-table-column>
      </el-table>
      <el-empty v-if="!loading && !visibleProducts.length" description="当前筛选条件下没有商品" />
    </section>

    <el-dialog v-model="dialog" title="人工添加1688货源" width="780" class="manual-source-dialog">
      <div v-if="detail" class="source-reference">
        <el-image :src="highImage" fit="cover" :preview-src-list="[highImage]" />
        <div class="reference-copy">
          <small>正在核验的 OZON 商品</small>
          <b>{{ detail.title }}</b>
          <span>OZON SKU {{ detail.source_sku }}</span>
          <div class="reference-actions">
            <el-button type="primary" plain @click="openOzon"><ExternalLink :size="15" />快速打开 OZON 页面</el-button>
            <el-button @click="copyImage">复制高清主图链接</el-button>
            <el-button type="success" plain @click="copyAndOpen1688"><ShoppingBag :size="15" />复制主图并打开1688</el-button>
          </div>
        </div>
      </div>
      <el-alert title="当前没有 Alibaba 接口：粘贴链接后手工填写准确规格即可确认货源，1688 SKU ID 暂时不必填写。" type="info" :closable="false" />
      <el-form label-width="125" class="manual-source-form">
        <el-form-item label="1688产品链接"><el-input v-model="form.source_url" placeholder="粘贴1688商品链接，程序会自动识别商品 ID" @change="sourceUrlChanged"><template #append><el-button :loading="loadingVariants" @click="loadVariants">有接口时读取规格</el-button></template></el-input></el-form-item>
        <el-form-item label="规格型号"><el-input v-model="form.sku_name" placeholder="请照抄1688商品页面的规格，例如：米白色 / 160×100cm" /><small class="sku-auto-note">{{ form.external_sku_id ? `已自动识别 SKU ID ${form.external_sku_id}` : '未配置接口时 SKU ID 留空，不影响完成货源核验' }}</small></el-form-item>
        <div class="two-column-fields">
          <el-form-item label="采购价 CNY"><el-input-number v-model="form.unit_price_cny" :min="0.01" :precision="2" /></el-form-item>
          <el-form-item label="国内运费 CNY"><el-input-number v-model="form.domestic_delivery_cny" :min="0" :precision="2" /></el-form-item>
          <el-form-item label="颜色（可留空）"><el-input v-model="form.color" /></el-form-item>
          <el-form-item label="尺码（可留空）"><el-input v-model="form.size" /></el-form-item>
        </div>
        <el-form-item label="备注（可留空）"><el-input v-model="form.remark" /></el-form-item>
      </el-form>
      <div v-if="loadingVariants || variantHint" class="variant-hint" :class="{ success: !!form.external_sku_id }"><span v-if="loadingVariants">正在从1688读取规格…</span><span v-else>{{ variantHint }}</span></div>
      <div v-if="variants.length > 1" class="variant-options">
        <button v-for="variant in variants" :key="variant.sku_id" type="button" :class="{selected:form.external_sku_id===variant.sku_id}" @click="chooseVariant(variant, false)"><span>{{ variant.sku_name || '默认规格' }}</span><small>{{ variant.price ? `¥${variant.price}` : `SKU ${variant.sku_id}` }}</small></button>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <el-button type="danger" plain @click="removeNoSource">无合适货源，删除商品</el-button>
          <span class="grow" />
          <el-button @click="dialog=false">取消</el-button>
          <el-button type="primary" @click="confirmSource">确认货源并进入下一步</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.sourcing-summary{display:grid;grid-template-columns:180px 180px 1fr;gap:14px}.sourcing-summary>div{background:#fff;border:1px solid #e2e9f3;border-radius:14px;padding:17px 20px;box-shadow:0 8px 24px rgba(30,55,90,.05)}.sourcing-summary strong{display:block;font-size:28px;color:#14213d}.sourcing-summary span{color:#6b7890;font-size:13px}.sourcing-summary .summary-note{display:flex;align-items:center;gap:12px;background:#f5f9ff;color:#2f68c5}.sourcing-head{align-items:flex-end}.sourcing-filters{display:flex;gap:10px;align-items:center}.sourcing-filters .el-input{width:280px}.sourcing-list-panel>.el-alert{margin:0 0 14px}.product-row{display:flex;align-items:center;gap:14px;padding:5px 0}.product-row .el-image{width:76px;height:76px;border-radius:9px;border:1px solid #e1e7f0;flex:0 0 auto}.product-row>div{display:flex;flex-direction:column;gap:6px;min-width:0}.product-row b{font-size:15px;color:#17233c;line-height:1.4}.product-row small{color:#78869c}.product-row a{display:inline-flex;align-items:center;gap:4px;color:#2975df;width:max-content}.source-reference{display:flex;gap:16px;align-items:center;margin-bottom:14px;padding:14px;border:1px solid #dce5f1;background:#f9fbfe;border-radius:12px}.source-reference>.el-image{width:104px;height:104px;border-radius:10px;flex:0 0 auto}.reference-copy{display:flex;flex-direction:column;gap:7px;min-width:0}.reference-copy b{font-size:16px}.reference-copy small,.reference-copy span{color:#6f7e94}.reference-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:5px}.manual-source-form{margin-top:18px}.sku-auto-note{display:block;color:#7b8799;margin-top:5px}.two-column-fields{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}.variant-hint{margin:0 0 10px 125px;padding:10px 13px;border-radius:8px;background:#fff6e8;color:#a66710}.variant-hint.success{background:#edf9f4;color:#16845b}.variant-options{display:grid;gap:8px;max-height:240px;overflow:auto;margin:0 0 10px 125px}.variant-options button{display:flex;justify-content:space-between;align-items:center;padding:11px 14px;border:1px solid #dbe4ef;border-radius:8px;background:#fff;color:#23324d;cursor:pointer}.variant-options button:hover,.variant-options button.selected{border-color:#409eff;background:#edf6ff}.variant-options button.selected{box-shadow:inset 3px 0 #409eff}.variant-options small{color:#6f7e94}.dialog-footer{display:flex;width:100%;align-items:center}.grow{flex:1}@media(max-width:1000px){.sourcing-summary{grid-template-columns:1fr 1fr}.sourcing-summary .summary-note{grid-column:1/-1}.sourcing-head{align-items:flex-start}.sourcing-filters{width:100%;flex-wrap:wrap}.sourcing-filters .el-input{width:100%}.two-column-fields{grid-template-columns:1fr}}
</style>
