<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { useRoute, useRouter } from "vue-router";
import { api } from "../api";
import type { CatalogProduct, Job } from "../types";

const items = ref<CatalogProduct[]>([]),
  total = ref(0),
  loading = ref(false),
  search = ref(""),
  source = ref(""),
  categoryFilter = ref(""),
  categoryStatusFilter = ref(""),
  imageStatusFilter = ref(""),
  statusFilter = ref(""),
  categoryFacets = ref<Array<{ label: string; count: number }>>([]),
  page = ref(1),
  pageSize = ref(50),
  detail = ref<CatalogProduct>(),
  drawer = ref(false),
  createOpen = ref(false),
  assetOpen = ref(false),
  workerOpen = ref(false),
  workerSubmitting = ref(false),
  workerJob = ref<Job>(),
  editOpen = ref(false),
  duplicates = ref<any[]>([]),
  selectedProducts = ref<CatalogProduct[]>([]),
  editCategoryStores = ref<any[]>([]),
  editCategoryOptions = ref<any[]>([]),
  editCategoryStoreId = ref(""),
  editCategoryKey = ref(""),
  editCategoryLoading = ref(false),
  activeTab = ref("products");
const route = useRoute(),
  router = useRouter(),
  scopedSource = String(route.meta.sourceType || "");
if (scopedSource) source.value = scopedSource;
const summary = reactive({
  total_products: 0,
  variants: 0,
  current_assets: 0,
  duplicate_suggestions: 0,
  by_source: {} as Record<string, number>,
});
const form = reactive<any>({
  sku: "",
  group_code: "",
  source_type: "self_selected",
  name: "",
  source_url: "",
  supplier: "1688",
  purchase_price_cny: undefined,
  package_length_mm: undefined,
  package_width_mm: undefined,
  package_height_mm: undefined,
  package_weight_g: undefined,
  notes: "",
});
const editForm = reactive<any>({});
// Virtualize the saved category list: thousands of mounted el-options freeze the dialog.
const editCategorySelectOptions = computed(() => editCategoryOptions.value.map((item:any) => ({
  label: item.display,
  value: `${item.description_category_id}:${item.type_id}`,
})));
const assetForm = reactive<any>({
  oss_url: "",
  name: "",
  role: "additional",
  position: 0,
  variant_id: "",
  set_current: true,
});
const workerForm = reactive({
  source_url: "",
  name: "",
  role: "additional",
  position: 0,
  variant_id: "",
  set_current: true,
  max_dimension: 2000,
  quality: 85,
  output_format: "JPEG",
});
let workerPoll: ReturnType<typeof setInterval> | undefined;
const sourceLabel: Record<string, string> = {
  pod: "POD",
  follow: "OZON 跟卖",
  self_selected: "自主上架",
};
async function load() {
  loading.value = true;
  try {
    const q = new URLSearchParams({
      search: search.value,
      source_type: scopedSource || source.value,
      category: categoryFilter.value,
      category_status: categoryStatusFilter.value,
      image_status: imageStatusFilter.value,
      status: statusFilter.value,
      page: String(page.value),
      page_size: String(pageSize.value),
    });
    const [list, stats] = await Promise.all([
      api.get<any>(`/catalog/products?${q}`),
      api.get<any>(`/catalog/summary${scopedSource ? `?source_type=${encodeURIComponent(scopedSource)}` : ""}`),
    ]);
    items.value = list.items;
    total.value = list.total;
    categoryFacets.value = list.category_options || [];
    Object.assign(summary, stats);
  } finally {
    loading.value = false;
  }
}
function resetFilters() {
  search.value = "";
  if (!scopedSource) source.value = "";
  categoryFilter.value = "";
  categoryStatusFilter.value = "";
  imageStatusFilter.value = "";
  statusFilter.value = "";
  page.value = 1;
  load();
}
function queryProducts() {
  page.value = 1;
  load();
}
function publishSelected() {
  if (!selectedProducts.value.length) {
    ElMessage.warning("请先勾选要上架的产品");
    return;
  }
  router.push({
    path: "/catalog/ozon/publish",
    query: { products: selectedProducts.value.map((item) => item.id).join(",") },
  });
}
function publishSelectedToWb() {
  if (!selectedProducts.value.length) {
    ElMessage.warning("请先勾选要上架的产品");
    return;
  }
  router.push({
    path: "/catalog/wb/publish",
    query: { products: selectedProducts.value.map((item) => item.id).join(",") },
  });
}
async function show(row: CatalogProduct) {
  detail.value = await api.get<CatalogProduct>(`/catalog/products/${row.id}`);
  drawer.value = true;
}
function imageUrl(link: any) {
  return link.asset.oss_url || `/api/v1/assets/${link.asset.id}/content`;
}
async function create() {
  await api.post("/catalog/products", {
    ...form,
    variants: [{ sku: form.sku }],
  });
  createOpen.value = false;
  ElMessage.success("产品已建立；入库过程未调用 AI");
  Object.assign(form, {
    sku: "",
    group_code: "",
    source_type: "self_selected",
    name: "",
    source_url: "",
    supplier: "1688",
    purchase_price_cny: undefined,
    package_length_mm: undefined,
    package_width_mm: undefined,
    package_height_mm: undefined,
    package_weight_g: undefined,
    notes: "",
  });
  load();
}
async function remove(row: CatalogProduct) {
  await ElMessageBox.confirm(
    `确认删除产品库记录 ${row.sku}？原始文件和 OSS 对象不会删除。`,
    "删除产品",
    { type: "warning" },
  );
  await api.delete(`/catalog/products/${row.id}`);
  ElMessage.success("产品库关联已删除");
  load();
}
async function loadDuplicates() {
  duplicates.value = (await api.get<any>("/catalog/duplicates")).items;
}
async function setDuplicate(row: any, status: string) {
  await api.patch(`/catalog/duplicates/${row.id}`, { status });
  ElMessage.success(status === "ignored" ? "已忽略提示" : "已记录人工合并结果");
  loadDuplicates();
  load();
}
async function loadEditCategoryOptions() {
  editCategoryOptions.value = [];
  if (!editCategoryStoreId.value) return;
  editCategoryLoading.value = true;
  try {
    const result = await api.get<any>(`/stores/${editCategoryStoreId.value}/category-options`);
    editCategoryOptions.value = result.options || result.items || [];
    const current = editCategoryOptions.value.find((item:any) => item.description_category_id === editForm.description_category_id && item.type_id === editForm.type_id);
    editCategoryKey.value = current ? `${current.description_category_id}:${current.type_id}` : "";
  } catch (error:any) {
    ElMessage.warning(error?.message || "该店铺还没有本地类目快照");
  } finally {
    editCategoryLoading.value = false;
  }
}
function applyEditCategory(key:string) {
  const selected = editCategoryOptions.value.find((item:any) => `${item.description_category_id}:${item.type_id}` === key);
  if (!selected) return;
  editForm.description_category_id = selected.description_category_id;
  editForm.type_id = selected.type_id;
  editForm.category_display = selected.display;
  editForm.category = selected.display;
}
async function openEdit() {
  if (!detail.value) return;
  Object.assign(editForm, {
    group_code: detail.value.group_code,
    name: detail.value.name,
    category: detail.value.category,
    source_url: detail.value.source_url,
    supplier: detail.value.supplier,
    supplier_product_id: detail.value.supplier_product_id,
    purchase_price_cny: detail.value.purchase_price_cny,
    domestic_delivery_cny: detail.value.domestic_delivery_cny,
    currency: detail.value.currency,
    package_length_mm: detail.value.package_length_mm,
    package_width_mm: detail.value.package_width_mm,
    package_height_mm: detail.value.package_height_mm,
    package_weight_g: detail.value.package_weight_g,
    description_category_id: detail.value.description_category_id,
    type_id: detail.value.type_id,
    category_display: detail.value.category_display,
    notes: detail.value.notes,
    status: detail.value.status,
  });
  editCategoryStores.value = (await api.get<any>("/stores")).items.filter((item:any) => item.platform === "ozon" && item.enabled);
  if (!editCategoryStoreId.value && editCategoryStores.value.length) editCategoryStoreId.value = editCategoryStores.value[0].id;
  await loadEditCategoryOptions();
  editOpen.value = true;
}
async function saveEdit() {
  detail.value = await api.patch<CatalogProduct>(
    `/catalog/products/${detail.value!.id}`,
    editForm,
  );
  editOpen.value = false;
  ElMessage.success("产品资料已更新");
  load();
}
function openAsset() {
  Object.assign(assetForm, {
    oss_url: "",
    name: "",
    role: "additional",
    position:
      detail.value?.assets?.filter((x) => x.role === "additional").length || 0,
    variant_id: "",
    set_current: true,
  });
  assetOpen.value = true;
}
async function saveAsset() {
  detail.value = await api.post<CatalogProduct>(
    `/catalog/products/${detail.value!.id}/assets/url`,
    { ...assetForm, variant_id: assetForm.variant_id || null },
  );
  assetOpen.value = false;
  ElMessage.success("当前图库已更新");
}
function openWorkerAsset() {
  Object.assign(workerForm, {
    source_url: "",
    name: "",
    role: detail.value?.assets?.some((x) => x.role === "main") ? "additional" : "main",
    position: detail.value?.assets?.filter((x) => x.role === "additional").length || 0,
    variant_id: "",
    set_current: true,
    max_dimension: 2000,
    quality: 85,
    output_format: "JPEG",
  });
  workerJob.value = undefined;
  workerOpen.value = true;
}
function stopWorkerPoll() {
  if (workerPoll) clearInterval(workerPoll);
  workerPoll = undefined;
}
async function refreshWorkerJob() {
  if (!workerJob.value) return;
  const current = await api.get<Job>(`/jobs/${workerJob.value.id}`);
  workerJob.value = current;
  if (["completed", "failed", "cancelled"].includes(current.status)) {
    stopWorkerPoll();
    if (current.status === "completed") {
      detail.value = await api.get<CatalogProduct>(`/catalog/products/${detail.value!.id}`);
      await load();
      ElMessage.success("图片已由本地 Worker 处理并加入产品图库");
    } else {
      ElMessage.error(current.error_message || "图片处理失败");
    }
  }
}
async function submitWorkerAsset() {
  if (!workerForm.source_url.trim()) {
    ElMessage.warning("请填写需要处理的图片地址");
    return;
  }
  workerSubmitting.value = true;
  try {
    workerJob.value = await api.post<Job>(
      `/catalog/products/${detail.value!.id}/assets/prepare`,
      { ...workerForm, variant_id: workerForm.variant_id || null },
    );
    stopWorkerPoll();
    workerPoll = setInterval(() => void refreshWorkerJob(), 1500);
    await refreshWorkerJob();
  } finally {
    workerSubmitting.value = false;
  }
}
async function removeAsset(link: any) {
  await ElMessageBox.confirm(
    detail.value?.source_type === "follow"
      ? "将永久删除该 OSS 图片并移除产品库关联，此操作不可恢复，也可能影响仍引用该图片地址的已上架商品。确认继续？"
      : "只移除产品库图片关联，原文件和 OSS 对象不会删除。",
    "移除图片",
    { type: "warning" },
  );
  await api.delete(`/catalog/products/${detail.value!.id}/assets/${link.id}`);
  detail.value = await api.get<CatalogProduct>(
    `/catalog/products/${detail.value!.id}`,
  );
  ElMessage.success(
    detail.value?.source_type === "follow"
      ? "图片关联和 OSS 对象已删除"
      : "图片关联已移除",
  );
}
onMounted(() => {
  load();
  loadDuplicates();
});
onUnmounted(stopWorkerPoll);
</script>
<template>
  <div class="page-stack">
    <section class="metric-grid">
      <div class="metric-card" style="--metric-color: #1d64f2">
        <strong>{{ summary.total_products }}</strong
        ><span>产品总数</span>
      </div>
      <div class="metric-card" style="--metric-color: #7c5ce7">
        <strong>{{ summary.variants }}</strong
        ><span>SKU 规格</span>
      </div>
      <div class="metric-card" style="--metric-color: #18a672">
        <strong>{{ summary.current_assets }}</strong
        ><span>当前图片资产</span>
      </div>
      <div class="metric-card" style="--metric-color: #e89d1b">
        <strong>{{ summary.duplicate_suggestions }}</strong
        ><span>待核对重复提示</span>
      </div>
    </section>
    <section class="panel">
      <el-tabs
        v-model="activeTab"
        class="catalog-tabs"
        @tab-change="(name: any) => name === 'duplicates' && loadDuplicates()"
      >
        <el-tab-pane label="产品总览" name="products">
          <div class="toolbar catalog-toolbar">
            <el-input
              v-model="search"
              placeholder="产品组、SKU、名称或货源链接"
              clearable
              @keyup.enter="queryProducts"
            /><el-select
              v-if="!scopedSource"
              v-model="source"
              clearable
              placeholder="全部来源"
              ><el-option label="POD" value="pod" /><el-option
                label="OZON 跟卖"
                value="follow" /><el-option
                label="自主上架"
                value="self_selected" /></el-select
            ><el-tag v-else size="large">{{ sourceLabel[scopedSource] }}</el-tag
            ><el-select v-model="categoryFilter" clearable filterable placeholder="全部 Ozon 类目" class="category-filter"><el-option v-for="item in categoryFacets" :key="item.label" :label="`${item.label}（${item.count}）`" :value="item.label"/></el-select
            ><el-select v-model="categoryStatusFilter" clearable placeholder="分类状态"><el-option label="已分类" value="assigned"/><el-option label="未分类" value="unassigned"/></el-select
            ><el-select v-model="imageStatusFilter" clearable placeholder="图片状态"><el-option label="有图片" value="with_images"/><el-option label="缺少图片" value="without_images"/></el-select
            ><el-select v-model="statusFilter" clearable placeholder="产品状态"><el-option label="可发布" value="ready"/><el-option label="草稿" value="draft"/><el-option label="已归档" value="archived"/></el-select
            ><el-button type="primary" @click="queryProducts">查询</el-button><el-button @click="resetFilters">重置</el-button
            ><span class="grow" /><el-button
              type="success"
              :disabled="!selectedProducts.length"
              @click="publishSelected"
              >批量上架到 OZON（{{ selectedProducts.length }}）</el-button
            ><el-button
              type="success"
              plain
              :disabled="!selectedProducts.length"
              @click="publishSelectedToWb"
              >批量上架到 WB（{{ selectedProducts.length }}）</el-button
            ><el-button
              v-if="scopedSource !== 'follow'"
              type="primary"
              @click="createOpen = true"
              >新建产品</el-button
            >
          </div>
          <el-table
            v-loading="loading"
            :data="items"
            row-key="id"
            @selection-change="selectedProducts = $event"
            @row-click="show"
          >
            <el-table-column
              type="selection"
              width="46"
              :selectable="(row: CatalogProduct) => row.status === 'ready'"
            />
            <el-table-column label="主图" width="92"
              ><template #default="s"
                ><el-image v-if="s.row.thumbnail" class="product-thumbnail" :src="imageUrl(s.row.thumbnail)" fit="cover" lazy/><div v-else class="thumbnail-empty">暂无图片</div></template
              ></el-table-column
            >
            <el-table-column label="产品" min-width="260"
              ><template #default="s"
                ><b>{{ s.row.name }}</b
                ><small class="table-sub mono"
                  >{{ s.row.group_code }} · {{ s.row.sku }}</small
                ></template
              ></el-table-column
            >
            <el-table-column label="来源" width="120"
              ><template #default="s"
                ><el-tag>{{ sourceLabel[s.row.source_type] }}</el-tag></template
              ></el-table-column
            >
            <el-table-column label="Ozon 类目" min-width="230"><template #default="s"><span v-if="s.row.category_display" class="category-path">{{ s.row.category_display }}</span><el-tag v-else type="warning" effect="plain">未分类</el-tag></template></el-table-column>
            <el-table-column
              prop="variant_count"
              label="SKU 数"
              width="90"
            /><el-table-column prop="supplier" label="供应商" width="130" />
            <el-table-column label="成本" width="110"
              ><template #default="s"
                >{{ s.row.purchase_price_cny ?? "-" }}
                {{ s.row.currency }}</template
              ></el-table-column
            >
            <el-table-column label="状态" width="110"><template #default="s"><el-tag :type="s.row.status==='ready'?'success':s.row.status==='archived'?'info':'warning'">{{ s.row.status==='ready'?'可发布':s.row.status==='archived'?'已归档':'草稿' }}</el-tag></template></el-table-column><el-table-column label="操作" width="130"
              ><template #default="s"
                ><el-button link type="primary" @click.stop="show(s.row)"
                  >详情</el-button
                ><el-button link type="danger" @click.stop="remove(s.row)"
                  >删除</el-button
                ></template
              ></el-table-column
            >
          </el-table>
          <div class="catalog-pagination"><span>共 {{ total }} 个产品</span><el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[20,50,100,200]" layout="sizes, prev, pager, next" :total="total" @change="load"/></div>
        </el-tab-pane>
        <el-tab-pane
          :label="`疑似重复 (${summary.duplicate_suggestions})`"
          name="duplicates"
        >
          <el-table :data="duplicates"
            ><el-table-column
              prop="product_id"
              label="产品 A"
              min-width="250"
            /><el-table-column
              prop="candidate_product_id"
              label="产品 B"
              min-width="250"
            /><el-table-column label="依据" min-width="260"
              ><template #default="s">{{
                s.row.reasons.join("；")
              }}</template></el-table-column
            ><el-table-column label="相似度" width="100"
              ><template #default="s"
                >{{ Math.round(s.row.score * 100) }}%</template
              ></el-table-column
            ><el-table-column label="人工处理" width="190"
              ><template #default="s"
                ><el-button link @click="setDuplicate(s.row, 'ignored')"
                  >不是同品</el-button
                ><el-button
                  link
                  type="primary"
                  @click="setDuplicate(s.row, 'merged')"
                  >已人工合并</el-button
                ></template
              ></el-table-column
            ></el-table
          >
          <div v-if="!duplicates.length" class="empty-state">
            <strong>暂无待核对的重复产品</strong
            ><span>系统只提示，不会自动合并不同来源产品。</span>
          </div>
        </el-tab-pane>
      </el-tabs>
    </section>
    <el-drawer v-model="drawer" size="72%" :title="detail?.name">
      <template v-if="detail"
        ><div class="drawer-actions top">
          <el-button @click="openEdit">编辑准确资料</el-button
          ><el-button type="success" @click="openWorkerAsset">本地 Worker 处理图片</el-button
          ><el-button type="primary" @click="openAsset">添加 OSS 图片</el-button
          ><RouterLink
            class="el-button el-button--success"
            :to="`/catalog/ozon/publish?product=${detail.id}`"
            >发布到 OZON</RouterLink
          ><RouterLink
            v-if="detail.source_type === 'pod'"
            class="el-button el-button--success"
            :to="{ path: '/catalog/wb/publish', query: { products: detail.id, mode: 'api' } }"
            >发布到 WB</RouterLink
          >
        </div>
        <el-descriptions :column="3" border
          ><el-descriptions-item label="产品组">{{
            detail.group_code
          }}</el-descriptions-item
          ><el-descriptions-item label="来源">{{
            sourceLabel[detail.source_type]
          }}</el-descriptions-item
          ><el-descriptions-item label="供应商">{{
            detail.supplier || "-"
          }}</el-descriptions-item
          ><el-descriptions-item label="货源链接" :span="3"
            ><a
              v-if="detail.source_url"
              :href="detail.source_url"
              target="_blank"
              >{{ detail.source_url }}</a
            ><span v-else>-</span></el-descriptions-item
          ><el-descriptions-item label="包装"
            ><template v-if="detail.source_type === 'follow' && !detail.package_length_mm">暂未采集</template><template v-else>{{ detail.package_length_mm }} × {{ detail.package_width_mm }} ×
            {{ detail.package_height_mm }} mm</template></el-descriptions-item
          ><el-descriptions-item label="重量"
            >{{ detail.package_weight_g }} g</el-descriptions-item
          ><el-descriptions-item label="OZON 类目">{{
            detail.category_display || "发布时选择"
          }}</el-descriptions-item></el-descriptions
        >
        <h3 class="section-title">当前产品图片</h3>
        <div class="catalog-gallery">
          <div v-for="link in detail.assets" :key="link.id">
            <el-image
              :src="imageUrl(link)"
              :preview-src-list="detail.assets?.map(imageUrl)"
              fit="cover"
              lazy
            /><span
              >{{ link.role }} · {{ link.position + 1 }}
              <el-button link type="danger" @click="removeAsset(link)"
                >移除</el-button
              ></span
            >
          </div>
        </div>
        <el-empty
          v-if="!detail.assets?.length"
          description="尚未关联当前图片"
        />
        <h3 class="section-title">SKU 与生产规格</h3>
        <el-table :data="detail.variants"
          ><el-table-column
            prop="sku"
            label="SKU"
            min-width="180" /><el-table-column
            prop="specification"
            label="规格"
            min-width="150" /><el-table-column
            prop="color"
            label="颜色" /><el-table-column
            prop="size"
            label="尺码" /><el-table-column
            prop="supplier_sku_id"
            label="供应商 SKU"
            min-width="150" /><el-table-column
            prop="weight_grams"
            label="重量(g)"
            width="100" /><el-table-column
            prop="active"
            label="启用"
            width="80"
        /></el-table>
        <h3 class="section-title">生产资料快照</h3>
        <pre class="json-preview">{{
          JSON.stringify(detail.production || {}, null, 2)
        }}</pre>
        <template v-if="detail.source_type === 'follow'">
          <h3 class="section-title">OZON 原商品事实快照</h3>
          <pre class="json-preview">{{
            JSON.stringify(detail.source_snapshot || {}, null, 2)
          }}</pre>
        </template>
      </template>
    </el-drawer>
    <el-dialog v-model="createOpen" title="新建产品主档" width="760"
      ><el-alert
        title="入库只保存准确资料和图片，不调用 AI。文案与属性在发布时生成。"
        type="info"
        :closable="false"
      /><el-form label-width="120" class="catalog-form"
        ><el-form-item label="来源"
          ><el-select v-model="form.source_type"
            ><el-option label="自行选品" value="self_selected" /><el-option
              label="POD"
              value="pod" /><el-option
              label="OZON 跟卖"
              value="follow" /></el-select></el-form-item
        ><el-form-item label="产品组编号"
          ><el-input v-model="form.group_code" /></el-form-item
        ><el-form-item label="主 SKU"
          ><el-input v-model="form.sku" /></el-form-item
        ><el-form-item label="中文名称"
          ><el-input v-model="form.name" /></el-form-item
        ><el-form-item label="货源链接"
          ><el-input v-model="form.source_url" /></el-form-item
        ><el-form-item label="供应商"
          ><el-input v-model="form.supplier" /></el-form-item
        ><el-form-item label="拿货价"
          ><el-input-number
            v-model="form.purchase_price_cny"
            :min="0"
            :precision="2"
          />
          CNY</el-form-item
        ><el-form-item label="包装(mm)"
          ><el-input-number
            v-model="form.package_length_mm"
            :min="1"
            placeholder="长" /><el-input-number
            v-model="form.package_width_mm"
            :min="1"
            placeholder="宽" /><el-input-number
            v-model="form.package_height_mm"
            :min="1"
            placeholder="高" /></el-form-item
        ><el-form-item label="重量(g)"
          ><el-input-number
            v-model="form.package_weight_g"
            :min="1" /></el-form-item
        ><el-form-item label="备注"
          ><el-input
            v-model="form.notes"
            type="textarea" /></el-form-item></el-form
      ><template #footer
        ><el-button @click="createOpen = false">取消</el-button
        ><el-button type="primary" @click="create"
          >保存产品</el-button
        ></template
      ></el-dialog
    >
    <el-dialog v-model="editOpen" title="编辑产品准确资料" width="820"
      ><el-alert
        title="这些数据会直接用于上架，AI 不会猜测或覆盖。"
        type="warning"
        :closable="false"
      /><el-form label-width="130" class="catalog-form"
        ><el-form-item label="产品组编号"
          ><el-input v-model="editForm.group_code" /></el-form-item
        ><el-form-item label="中文名称"
          ><el-input v-model="editForm.name" /></el-form-item
        ><el-form-item label="货源链接"
          ><el-input v-model="editForm.source_url" /></el-form-item
        ><el-form-item label="供应商 / ID"
          ><el-input v-model="editForm.supplier" /><el-input
            v-model="editForm.supplier_product_id"
            placeholder="供应商商品 ID" /></el-form-item
        ><el-form-item label="拿货价 / 运费"
          ><el-input-number
            v-model="editForm.purchase_price_cny"
            :min="0"
            :precision="2"
          /><el-input-number
            v-model="editForm.domestic_delivery_cny"
            :min="0"
            :precision="2"
          />
          CNY</el-form-item
        ><el-form-item label="包装(mm)"
          ><el-input-number
            v-model="editForm.package_length_mm"
            :min="1" /><el-input-number
            v-model="editForm.package_width_mm"
            :min="1" /><el-input-number
            v-model="editForm.package_height_mm"
            :min="1" /></el-form-item
        ><el-form-item label="重量(g)"
          ><el-input-number
            v-model="editForm.package_weight_g"
            :min="1" /></el-form-item
        ><el-form-item label="OZON 类目"
          ><el-select v-model="editCategoryStoreId" placeholder="类目快照所属店铺" @change="loadEditCategoryOptions"><el-option v-for="store in editCategoryStores" :key="store.id" :label="store.name" :value="store.id"/></el-select><el-select-v2 v-model="editCategoryKey" :options="editCategorySelectOptions" filterable :loading="editCategoryLoading" :height="280" placeholder="搜索本地 Ozon 类目" @change="applyEditCategory"/></el-form-item
        ><el-form-item label="当前类目路径"
          ><el-input v-model="editForm.category_display" readonly /></el-form-item
        ><el-form-item label="状态"
          ><el-select v-model="editForm.status"
            ><el-option label="可发布" value="ready" /><el-option
              label="草稿"
              value="draft" /><el-option
              label="已归档"
              value="archived" /></el-select></el-form-item
        ><el-form-item label="备注"
          ><el-input
            v-model="editForm.notes"
            type="textarea" /></el-form-item></el-form
      ><template #footer
        ><el-button @click="editOpen = false">取消</el-button
        ><el-button type="primary" @click="saveEdit">保存</el-button></template
      ></el-dialog
    >
    <el-dialog v-model="assetOpen" title="添加产品 OSS 图片" width="620"
      ><el-form label-width="110"
        ><el-form-item label="HTTPS 链接"
          ><el-input
            v-model="assetForm.oss_url"
            type="textarea"
            :rows="2" /></el-form-item
        ><el-form-item label="图片类型"
          ><el-select v-model="assetForm.role"
            ><el-option label="主图" value="main" /><el-option
              label="附图"
              value="additional" /><el-option
              label="POD 印花"
              value="artwork" /><el-option
              label="效果图"
              value="mockup" /><el-option
              label="SKU 图"
              value="sku" /></el-select></el-form-item
        ><el-form-item label="关联 SKU"
          ><el-select
            v-model="assetForm.variant_id"
            clearable
            placeholder="留空表示整个产品"
            ><el-option
              v-for="v in detail?.variants"
              :key="v.id"
              :label="v.sku"
              :value="v.id" /></el-select></el-form-item
        ><el-form-item label="排序"
          ><el-input-number
            v-model="assetForm.position"
            :min="0" /></el-form-item
        ><el-form-item label="设为当前"
          ><el-switch v-model="assetForm.set_current" /></el-form-item></el-form
      ><template #footer
        ><el-button @click="assetOpen = false">取消</el-button
        ><el-button type="primary" @click="saveAsset"
          >保存图片</el-button
        ></template
      ></el-dialog
    >
    <el-dialog
      v-model="workerOpen"
      title="本地 Worker 处理图片"
      width="680"
      :close-on-click-modal="!workerJob || workerJob.status === 'completed' || workerJob.status === 'failed'"
      @closed="stopWorkerPoll"
    >
      <el-alert
        title="图片将在本地电脑下载和压缩，直接上传 OSS；云服务器不保存图片文件。"
        type="success"
        :closable="false"
      />
      <el-form v-if="!workerJob" label-width="120" class="catalog-form">
        <el-form-item label="原始图片地址">
          <el-input v-model="workerForm.source_url" type="textarea" :rows="2" placeholder="https://..." />
        </el-form-item>
        <el-form-item label="图片名称">
          <el-input v-model="workerForm.name" placeholder="例如：红色款主图.jpg" />
        </el-form-item>
        <el-form-item label="图片类型">
          <el-select v-model="workerForm.role">
            <el-option label="主图" value="main" />
            <el-option label="附图" value="additional" />
            <el-option label="POD 印花" value="artwork" />
            <el-option label="效果图" value="mockup" />
            <el-option label="SKU 图" value="sku" />
          </el-select>
        </el-form-item>
        <el-form-item label="关联 SKU">
          <el-select v-model="workerForm.variant_id" clearable placeholder="留空表示整个产品">
            <el-option v-for="v in detail?.variants" :key="v.id" :label="v.sku" :value="v.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="最大边长">
          <el-input-number v-model="workerForm.max_dimension" :min="320" :max="4096" :step="100" /> px
        </el-form-item>
        <el-form-item label="输出格式 / 质量">
          <el-select v-model="workerForm.output_format" style="width: 130px">
            <el-option label="JPEG" value="JPEG" />
            <el-option label="WebP" value="WEBP" />
          </el-select>
          <el-slider v-model="workerForm.quality" :min="60" :max="95" show-input style="width: 360px; margin-left: 18px" />
        </el-form-item>
        <el-form-item label="排序 / 当前图">
          <el-input-number v-model="workerForm.position" :min="0" />
          <el-switch v-model="workerForm.set_current" style="margin-left: 18px" />
        </el-form-item>
      </el-form>
      <div v-else class="worker-progress">
        <el-result
          :icon="workerJob.status === 'completed' ? 'success' : workerJob.status === 'failed' ? 'error' : 'info'"
          :title="workerJob.status === 'completed' ? '图片处理完成' : workerJob.status === 'failed' ? '图片处理失败' : '本地 Worker 正在处理'"
          :sub-title="workerJob.error_message || `任务编号：${workerJob.id}`"
        />
        <el-progress
          :percentage="workerJob.status === 'completed' ? 100 : Math.max(5, Math.round(workerJob.progress || 0))"
          :status="workerJob.status === 'failed' ? 'exception' : workerJob.status === 'completed' ? 'success' : undefined"
        />
        <el-steps :active="workerJob.steps.filter((step) => step.status === 'completed').length" finish-status="success" simple>
          <el-step v-for="step in workerJob.steps" :key="step.id" :title="step.name" />
        </el-steps>
      </div>
      <template #footer>
        <el-button @click="workerOpen = false">{{ workerJob?.status === 'completed' ? '完成' : '关闭' }}</el-button>
        <el-button v-if="!workerJob" type="primary" :loading="workerSubmitting" @click="submitWorkerAsset">提交给本地 Worker</el-button>
      </template>
    </el-dialog>
  </div>
</template>
