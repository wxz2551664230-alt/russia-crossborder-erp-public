<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import CodexHandoffPanel from "../components/CodexHandoffPanel.vue";
import CatalogWbTabs from "../components/CatalogWbTabs.vue";
import WbShopSyncPanel from "../components/WbShopSyncPanel.vue";
import {groupWbExports} from "../codexTaskGroups";
import type { CatalogProduct, Store } from "../types";

const codexBatchIds=ref<string[]>([]), codexReview=ref<any>(), codexReviewVisible=ref(false), codexApproving=ref(false);
const displayExports=computed(()=>groupWbExports(exports.value));
const route = useRoute(),
  products = ref<any[]>([]),
  selected = ref<Record<string, any>>({}),
  selectedVariantKeys = ref<string[]>([]),
  selectedVariantIds = ref<string[]>([]),
  availability = ref<Record<string, any>>({}),
  imageRules = ref<any[]>([]),
  imageSampleId = ref(""),
  imageLoading = ref(false),
  stores = ref<Store[]>([]),
  templates = ref<any[]>([]),
  exports = ref<any[]>([]),
  categories = ref<any[]>([]),
  preview = ref<any>(),
  loading = ref(false),
  submitting = ref(false),
  previewing = ref(false),
  publishProgress = ref(""),
  historyScanning = ref(false),
  uploadedSyncing = ref(false),
  search = ref(""),
  supplier = ref(""),
  category = ref(""),
  status = ref("ready"),
  publishState = ref("available"),
  page = ref(1),
  pageSize = ref(30),
  total = ref(0);
const form = reactive({
  store_id: "",
  template_id: "",
  brand: "Нет бренда",
  price_cny: 0,
  requirement: "",
  mode: "excel",
  generation_provider: "model",
  variant_selection: "selected",
  pricing_mode: "profit",
  rub_to_cny_rate: 0.0783,
  discount_percent: 30,
  target_profit_percent: 30,
  commission_percent: 20,
  advertising_percent: 10,
  return_percent: 10,
  label_fee_cny: 2.5,
  other_cost_cny: 4,
  multiplier: 3,
});
const selectedProducts = computed(() => Object.values(selected.value)),
  selectedVariants = computed(() =>
    selectedProducts.value.flatMap((product: any) =>
      (product.variants || []).filter((variant: any) =>
        selectedVariantIds.value.includes(variant.id),
      ),
    ),
  );
const selectablePageProducts = computed(() =>
  products.value.filter((item) => !!form.store_id && !!availability.value[item.id] && !availability.value[item.id].blocked),
);
const totalPages = computed(() =>
    Math.max(1, Math.ceil(total.value / pageSize.value)),
  ),
  pageFullySelected = computed(
    () =>
      selectablePageProducts.value.length > 0 &&
      selectablePageProducts.value.every((item) => selected.value[item.id]),
  );
const canBulkSelect = computed(() =>
  ["available", "retryable"].includes(publishState.value),
);
const variantOptions = computed(() => {
  const first = selectedProducts.value[0];
  if (!first) return [];
  return variants(first).map((variant: any) => {
    const key = variantKey(variant);
    const matched = selectedProducts.value.filter((product: any) =>
      variants(product).some((row: any) => variantKey(row) === key),
    ).length;
    return {
      key,
      label: variant.size || variant.specification || "单一规格",
      color: variant.color || "默认颜色",
      matched,
      total: selectedProducts.value.length,
    };
  });
});
const hasVariantGaps = computed(() =>
  variantOptions.value.some((option: any) => option.matched < option.total),
);
const enabledImageCount = computed(
  () => imageRules.value.filter((row: any) => row.action !== "exclude").length,
);
const selectedStore = computed(() =>
  stores.value.find((store: any) => store.id === form.store_id),
);
const duplicateVariants = computed(() =>
  selectedProducts.value.flatMap((product: any) => {
    const state = availability.value[product.id];
    return (state?.items || []).filter(
      (item: any) =>
        selectedVariantIds.value.includes(item.variant_id) &&
        (item.external_product_id || [
          "queued",
          "running",
          "paused",
          "exported_pending_confirmation",
          "submitted",
          "completed",
        ].includes(item.status)),
    );
  }),
);
function thumbnail(item: any) {
  return (
    item.thumbnail_url ||
    (item.thumbnail
      ? item.thumbnail.asset.oss_url ||
        `/api/v1/assets/${item.thumbnail.asset.id}/content`
      : "")
  );
}
function variants(item: any) {
  return (item.variants || []).filter(
    (variant: any) => variant.active !== false,
  );
}
function variantKey(variant: any) {
  const color = String(variant.color || "")
      .trim()
      .toLowerCase(),
    size = String(variant.size || "")
      .trim()
      .toLowerCase();
  return color || size
    ? `${color}|${size}`
    : `spec|${String(variant.specification || "")
        .trim()
        .toLowerCase()}`;
}
function syncVariantIds() {
  const keys = new Set(selectedVariantKeys.value);
  selectedVariantIds.value = selectedProducts.value.flatMap((product: any) =>
    variants(product)
      .filter((variant: any) => keys.has(variantKey(variant)))
      .map((variant: any) => String(variant.id)),
  );
  preview.value = undefined;
}
function toggle(item: CatalogProduct) {
  const next = { ...selected.value };
  if (!next[item.id] && (!form.store_id || !availability.value[item.id])) {
    ElMessage.warning('请先选择店铺并等待本店上架状态读取完成');
    return;
  }
  if (!next[item.id] && availability.value[item.id]?.blocked) {
    ElMessage.warning("该产品在当前店铺已上架或正在处理，不能重复选择");
    return;
  }
  const wasEmpty = !Object.keys(next).length;
  if (next[item.id]) delete next[item.id];
  else next[item.id] = item;
  selected.value = next;
  if (wasEmpty && next[item.id])
    selectedVariantKeys.value = variants(item).map(variantKey);
  if (!Object.keys(next).length) selectedVariantKeys.value = [];
  syncVariantIds();
}
function clearSelection() {
  selected.value = {};
  selectedVariantKeys.value = [];
  selectedVariantIds.value = [];
  preview.value = undefined;
}
function imageUrl(link: any) {
  return link.asset?.oss_url || "";
}
function imageKey(link: any) {
  const value = String(link.asset?.name || link.asset?.oss_key || "");
  return value.split(/[\\/]/).pop() || value;
}
async function loadImageSample() {
  const first = selectedProducts.value[0];
  if (!first) {
    imageRules.value = [];
    imageSampleId.value = "";
    return;
  }
  if (imageSampleId.value === first.id && imageRules.value.length) return;
  imageLoading.value = true;
  try {
    const detail = await api.get<any>(`/pod/library/products/${first.id}`);
    const rows = (detail.assets || []).filter(
      (link: any) =>
        ["image", "rendering"].includes(link.asset?.kind) &&
        imageUrl(link).startsWith("https://") &&
        ["main", "mockup", "additional", "sku"].includes(link.role),
    );
    imageRules.value = rows
      .slice(0, 30)
      .map((link: any, index: number) => ({
        key: imageKey(link),
        url: imageUrl(link),
        action: index === 0 ? "main" : "additional",
      }));
    imageSampleId.value = first.id;
  } finally {
    imageLoading.value = false;
  }
}
function setImageAction(index: number, action: string) {
  if (action === "main")
    imageRules.value.forEach((row, position) => {
      if (position !== index && row.action === "main")
        row.action = "additional";
    });
  imageRules.value[index].action = action;
  if (!imageRules.value.some((row) => row.action === "main")) {
    const replacement = imageRules.value.find(
      (row) => row.action !== "exclude",
    );
    if (replacement) replacement.action = "main";
  }
  preview.value = undefined;
}
function moveImage(index: number, direction: number) {
  const target = index + direction;
  if (target < 0 || target >= imageRules.value.length) return;
  const rows = [...imageRules.value];
  [rows[index], rows[target]] = [rows[target], rows[index]];
  imageRules.value = rows;
  preview.value = undefined;
}
function pricing() {
  return {
    mode: form.pricing_mode,
    manual_price_rub: form.price_cny,
    rub_to_cny_rate: form.rub_to_cny_rate,
    discount_percent: form.discount_percent,
    target_profit_percent: form.target_profit_percent,
    commission_percent: form.commission_percent,
    advertising_percent: form.advertising_percent,
    return_percent: form.return_percent,
    label_fee_cny: form.label_fee_cny,
    other_cost_cny: form.other_cost_cny,
    multiplier: form.multiplier,
  };
}
function productQuery(current: number, size: number) {
  return new URLSearchParams({
    page: String(current),
    page_size: String(size),
    search: search.value,
    supplier: supplier.value,
    category: category.value,
    status: status.value,
    wb_store_id: form.store_id,
    wb_state: publishState.value,
  });
}
async function loadProducts() {
  loading.value = true;
  try {
    const data = await api.get<any>(
      `/pod/library/products?${productQuery(page.value, pageSize.value)}`,
    );
    products.value = data.items || [];
    total.value = data.total || 0;
  } finally {
    loading.value = false;
  }
}
let availabilitySequence = 0;
async function loadAvailability() {
  const n = ++availabilitySequence, storeId = form.store_id;
  availability.value = {};
  if (!form.store_id) {
    availability.value = {};
    return;
  }
  const ids = [
    ...new Set(
      [...products.value, ...selectedProducts.value].map(
        (item: any) => item.id,
      ),
    ),
  ].slice(0, 1000);
  if (!ids.length) return;
  const data = await api.post<any>("/pod/wb/availability", {
    store_id: storeId,
    product_ids: ids,
  });
  if (n === availabilitySequence && storeId === form.store_id)
    availability.value = data.products || {};
}
async function query() {
  page.value = 1;
  await loadProducts();
  await loadAvailability();
}
function applyBatchSelection(rows: any[], remove: boolean) {
  const next = { ...selected.value },
    wasEmpty = !Object.keys(next).length;
  for (const item of rows) {
    if (remove) delete next[item.id];
    else next[item.id] = item;
  }
  selected.value = next;
  if (wasEmpty && !remove && rows.length)
    selectedVariantKeys.value = variants(rows[0]).map(variantKey);
  if (!Object.keys(next).length) selectedVariantKeys.value = [];
  syncVariantIds();
}
function togglePage() {
  if (!canBulkSelect.value)
    return ElMessage.warning("请先筛选“可上架”或“失败可重试”的产品");
  const remove = pageFullySelected.value;
  applyBatchSelection(selectablePageProducts.value, remove);
  ElMessage.success(
    remove
      ? `已取消本页 ${selectablePageProducts.value.length} 个产品`
      : `已选择本页 ${selectablePageProducts.value.length} 个产品`,
  );
}
async function selectAllFiltered() {
  if (!canBulkSelect.value)
    return ElMessage.warning("只有“可上架”或“失败可重试”筛选结果可以批量选择");
  loading.value = true;
  try {
    const first = productQuery(1, 200);
    const firstData = await api.get<any>(`/pod/library/products?${first}`);
    const rows = [...(firstData.items || [])];
    for (
      let current = 2;
      current <= Math.ceil((firstData.total || 0) / 200);
      current++
    ) {
      first.set("page", String(current));
      rows.push(
        ...((await api.get<any>(`/pod/library/products?${first}`)).items || []),
      );
    }
    applyBatchSelection(rows, false);
    ElMessage.success(`已选择当前筛选结果 ${rows.length} 个产品`);
  } finally {
    loading.value = false;
  }
}
async function changePage() {
  await loadProducts();
  await loadAvailability();
}
async function changePageSize() {
  page.value = 1;
  await changePage();
}
async function load() {
  const [storeResult, templateResult, exportResult, categoryResult] =
    await Promise.all([
      api.get<any>("/stores"),
      api.get<any>("/pod/wb/templates"),
      api.get<any>("/pod/wb/exports?include_codex_siblings=true"),
      api.get<any>("/pod/library/categories"),
    ]);
  stores.value = (storeResult.items || []).filter(
    (row: Store) =>
      ["wb", "wildberries"].includes(String(row.platform).toLowerCase()) &&
      row.enabled,
  );
  if (!form.store_id && stores.value.length) form.store_id = stores.value[0].id;
  if (route.query.mode === "api") form.mode = "api";
  templates.value = templateResult.items || [];
  exports.value = exportResult.items || [];
  categories.value = categoryResult.items || [];
  await loadProducts();
  const linked: CatalogProduct[] = [];
  for (const id of String(route.query.products || "")
    .split(",")
    .filter(Boolean)) {
    try {
      const item = await api.get<CatalogProduct>(`/catalog/products/${id}`);
      if (item.source_type === "pod") linked.push(item);
    } catch {}
  }
  if (form.store_id && linked.length) {
    const state = await api.post<any>("/pod/wb/availability", {
      store_id: form.store_id,
      product_ids: linked.map((item) => item.id),
    });
    availability.value = state.products || {};
  }
  for (const item of linked) if (!selected.value[item.id]) toggle(item);
  await loadAvailability();
}
async function confirmUpload(row: any, uploaded: boolean) {
  await api.post(
    `/pod/wb/exports/${row.id}/${uploaded ? "confirm-upload" : "cancel-upload"}`,
    {},
  );
  await load();
  ElMessage.success(uploaded ? "已确认本批上传完成；商品编号统一通过“同步已上传商品”更新" : "已取消锁定，可重新上架");
}
function publicationBatches() {
  const selectedIds = new Set(selectedVariantIds.value);
  const splitSizes = Number(templates.value.find((row) => row.id === form.template_id)?.subject_id) === 321;
  const batches: { products: any[]; variantIds: string[] }[] = [];
  let batch = { products: [] as any[], variantIds: [] as string[] };
  let cards = 0;
  for (const product of selectedProducts.value) {
    const variantIds = variants(product).map((row: any) => String(row.id)).filter((id: string) => selectedIds.has(id));
    if (!variantIds.length) throw new Error(`${product.sku || product.name} 没有选中的可用变体`);
    const cardCount = splitSizes ? variantIds.length : 1;
    if (cardCount > 1000) throw new Error(`${product.sku || product.name} 的变体超过单个 WB Excel 上限`);
    if (variantIds.length > 5000) throw new Error(`${product.sku || product.name} 的变体超过单批提交上限`);
    if (batch.products.length && (batch.products.length >= 1000 || cards + cardCount > 1000 || batch.variantIds.length + variantIds.length > 5000)) {
      batches.push(batch);
      batch = { products: [], variantIds: [] };
      cards = 0;
    }
    batch.products.push(product);
    batch.variantIds.push(...variantIds);
    cards += cardCount;
  }
  if (batch.products.length) batches.push(batch);
  return batches;
}
async function previewBatches(batches: ReturnType<typeof publicationBatches>) {
  const template = templates.value.find((row) => row.id === form.template_id);
  if (!template) throw new Error("请选择 WB 类目模板");
  const summaries = [];
  for (const [index, batch] of batches.entries()) {
    publishProgress.value = `正在校验第 ${index + 1}/${batches.length} 批（${batch.products.length} 个产品）`;
    summaries.push(await api.post<any>("/pod/wb/exports/preview", {
      product_ids: batch.products.map((item) => item.id),
      subject_id: template.subject_id,
      variant_selection: "selected",
      variant_ids: batch.variantIds,
      pricing: pricing(),
    }));
  }
  preview.value = {
    product_count: summaries.reduce((count, item) => count + item.product_count, 0),
    variant_count: summaries.reduce((count, item) => count + item.variant_count, 0),
    card_count: summaries.reduce((count, item) => count + item.card_count, 0),
    group_count: summaries.reduce((count, item) => count + item.group_count, 0),
    split_size_cards: summaries.some((item) => item.split_size_cards),
    pricing_errors: summaries.flatMap((item) => item.pricing_errors || []),
    products: summaries.flatMap((item) => item.products || []),
    groups: summaries.flatMap((item) => item.groups || []).map((group, index) => ({ ...group, index: index + 1 })),
    batch_count: batches.length,
  };
}
async function refreshPreview() {
  const template = templates.value.find((row) => row.id === form.template_id);
  if (!selectedProducts.value.length)
    return ElMessage.warning("请先选择 POD 产品");
  if (!template) return ElMessage.warning("请选择 WB 类目模板");
  if (!selectedVariantIds.value.length)
    return ElMessage.warning("请至少选择一个变体");
  previewing.value = true;
  try {
    await previewBatches(publicationBatches());
  } catch (error) {
    preview.value = undefined;
    ElMessage.error(`发布前检查失败：${String((error as Error).message || error)}`);
  } finally {
    previewing.value = false;
    publishProgress.value = "";
  }
}
async function createTask() {
  if (submitting.value) return;
  if (!form.store_id) return ElMessage.warning("请选择 WB 店铺");
  if (!form.template_id) return ElMessage.warning("请选择 WB 类目模板");
  if (!imageRules.value.some((row) => row.action === "main"))
    return ElMessage.warning("请指定一张上架主图");
  if (form.pricing_mode === "manual" && !form.price_cny)
    return ElMessage.warning("请输入 WB 售价");
  submitting.value = true;
  let created = 0;
  const createdIds: string[] = [];
  const codexRequestId = form.generation_provider === "codex" ? crypto.randomUUID().replaceAll("-", "") : "";
  const removeCreatedSelection = () => {
    if (!createdIds.length) return;
    for (const id of createdIds) delete selected.value[id];
    if (!Object.keys(selected.value).length) clearSelection();
    else syncVariantIds();
  };
  try {
    const batches = publicationBatches();
    if (!batches.length) throw new Error("请先选择 POD 产品和变体");
    await previewBatches(batches);
    if (preview.value?.pricing_errors?.length)
      throw new Error(`存在 ${preview.value.pricing_errors.length} 个无法计算价格的 SKU，请先补齐成本或重量`);
    const productCount = preview.value.product_count;
    if (form.generation_provider === "codex") codexBatchIds.value = [];
    for (const [index, batch] of batches.entries()) {
      publishProgress.value = `正在创建第 ${index + 1}/${batches.length} 批（${batch.products.length} 个产品）`;
      const result = await api.post<any>("/pod/wb/exports", {
        product_ids: batch.products.map((item) => item.id),
        store_id: form.store_id,
        template_id: form.template_id,
        brand: form.brand,
        requirement: form.requirement,
        price_cny: form.price_cny || 1,
        pricing: pricing(),
        image_rules: imageRules.value.map((row) => ({ key: row.key, action: row.action })),
        variant_selection: "selected",
        variant_ids: batch.variantIds,
        mode: form.mode,
        generation_provider: form.generation_provider,
        codex_request_id: codexRequestId,
      });
      if(form.generation_provider === "codex") codexBatchIds.value.push(result.job.id);
      created++;
      createdIds.push(...batch.products.map((item) => item.id));
      exports.value.unshift({
        id: result.job.id,
        status: result.job.status,
        created_at: result.job.created_at,
        mode: form.mode,
        publication_status: "queued",
        payload: { product_ids: batch.products.map((item) => item.id), mode: form.mode, generation_provider:form.generation_provider, batch_id:result.job.id, codex_request_id:codexRequestId },
      });
    }
    removeCreatedSelection();
    ElMessage.success(`已创建 ${created} 个 WB ${form.mode === "excel" ? "Excel 发布包" : "Seller API 发布"}任务，共 ${productCount} 个产品`);
  } catch (error) {
    removeCreatedSelection();
    ElMessage.error({ message: `${created ? `已创建 ${created} 批；其余产品仍保留勾选。` : "任务未创建。"}${String((error as Error).message || error)}`, duration: 8000, showClose: true });
  } finally {
    submitting.value = false;
    publishProgress.value = "";
  }
}
async function openCodex(row:any){
  if(row.grouped){codexBatchIds.value=row.children.map((child:any)=>child.payload?.batch_id||child.id);codexReviewVisible.value=false;return}
  codexBatchIds.value=[row.payload?.batch_id || row.id];
  try {
    const result=await api.get<any>("/pod/wb/exports?include_codex_siblings=true");exports.value=result.items || [];
    codexReview.value=exports.value.find((item:any)=>item.id===row.id);
    codexReviewVisible.value=!!codexReview.value?.payload?.codex_outputs;
  } catch(e:any){ElMessage.error(e.message)}
}
async function approveCodex(){
  if(!codexReview.value)return;
  try {
    await ElMessageBox.confirm("将按本任务原店铺、价格、规格和图片顺序创建 WB 商品卡，确认文案及属性无误后继续。", "确认本批次 WB API 发布",{type:"warning"});
    codexApproving.value=true;
    const receipt=Object.values(codexReview.value.payload.codex_outputs)[0] as any;
    await api.post(`/pod/wb/exports/${codexReview.value.id}/approve-codex`,{revision:receipt.revision,output_sha256:receipt.output_sha256});
    codexReviewVisible.value=false;
    exports.value=(await api.get<any>("/pod/wb/exports?include_codex_siblings=true")).items || [];
    ElMessage.success("已确认，原执行电脑将继续本次 WB API 发布任务");
  }catch(e:any){if(e!=="cancel" && e!=="close")ElMessage.error(e.message || String(e))}
  finally{codexApproving.value=false}
}
async function scanHistory() {
  if (!form.store_id) return ElMessage.warning("请先选择 WB 店铺");
  historyScanning.value = true;
  try {
    const result = await api.post<any>("/pod/wb/history/reconcile", {
      store_id: form.store_id,
    });
    exports.value = [
      {
        id: result.job.id,
        status: result.job.status,
        created_at: result.job.created_at,
        mode: "history",
        payload: { store_id: form.store_id },
      },
      ...exports.value,
    ];
    ElMessage.success(
      "历史 POD 商品扫描已交给当前电脑；完成后可在最近任务下载匹配报告",
    );
  } finally {
    historyScanning.value = false;
  }
}
async function syncUploaded() {
  if (!form.store_id) return ElMessage.warning("请先选择 WB 店铺");
  uploadedSyncing.value = true;
  try {
    const result = await api.post<any>("/pod/wb/history/sync-uploaded", {store_id: form.store_id});
    ElMessage.success(`同步任务已创建：${result.job.id}。可在下方“最近 WB 发布任务”点击任务详情查看进度。`);
    exports.value = [{id: result.job.id, status: result.job.status, created_at: result.job.created_at, mode: "history", payload: {store_id: form.store_id}}, ...exports.value];
  } catch (error) {
    ElMessage.error(`同步任务未创建：${String((error as Error).message || error)}`);
  } finally {uploadedSyncing.value = false;}
}
watch(selectedVariantKeys, syncVariantIds, { deep: true });
watch(
  () => selectedProducts.value.map((item) => item.id).join(","),
  loadImageSample,
);
watch(
  () => form.store_id,
  async () => {
    availabilitySequence++;
    availability.value = {};
    page.value = 1;
    await loadProducts();
    await loadAvailability();
  },
);
watch(publishState, query);
watch(
  () => form.template_id,
  () => (preview.value = undefined),
);
onMounted(load);
</script>

<template>
  <div class="page-stack">
    <CatalogWbTabs />
    <section class="panel" style="padding:16px;display:flex;gap:16px;align-items:center;flex-wrap:wrap">
      <el-select v-model="form.store_id" placeholder="选择 WB 店铺" style="width:220px"><el-option v-for="store in stores" :key="store.id" :label="store.name" :value="store.id"/></el-select>
      <WbShopSyncPanel :store-id="form.store_id" @updated="query"/>
    </section>
    <CodexHandoffPanel v-if="codexBatchIds.length" :batch-ids="codexBatchIds" />
    <el-dialog v-model="codexReviewVisible" title="WB Codex 文案及属性核验" width="80%">
      <template v-if="codexReview">
        <p>原发布方式：{{codexReview.mode === 'api' ? 'Seller API 直接上架' : '导出 Excel'}}；图片和定价沿用本任务原设置。</p>
        <template v-for="receipt in codexReview.payload?.codex_outputs" :key="receipt.output_sha256">
          <el-card v-for="(copy,productId) in receipt.copies" :key="productId" style="margin-bottom:12px">
            <strong>{{codexReview.payload.codex_products?.find((p:any)=>p.id===productId)?.sku || productId}} · {{copy.title}}</strong><p style="white-space:pre-wrap">{{copy.description}}</p>
            <pre style="white-space:pre-wrap">{{JSON.stringify(copy.characteristics,null,2)}}</pre>
          </el-card>
        </template>
      </template>
      <template #footer><el-button @click="codexReviewVisible=false">关闭</el-button><el-button v-if="codexReview?.mode==='api' && codexReview?.status==='paused' && !codexReview?.payload?.codex_publish_approved" type="primary" :loading="codexApproving" @click="approveCodex">确认文案并继续 API 发布</el-button></template>
    </el-dialog>
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>1. 选择同类 POD 产品</h2>
          <p>
            默认只显示当前店铺尚未上架、可以选择的产品；也可以查看已上架、处理中或失败可重试产品。
          </p>
        </div>
        <el-tag size="large" type="success"
          >{{ selectedProducts.length }} 个印花 ·
          {{ selectedVariants.length }} 个实际 SKU</el-tag
        >
      </div>
      <div class="toolbar catalog-toolbar">
        <el-input
          v-model="search"
          clearable
          placeholder="搜索名称、产品组或 SKU"
          @keyup.enter="query"
        /><el-select
          v-model="category"
          clearable
          filterable
          placeholder="产品分类"
          ><el-option
            v-for="item in categories"
            :key="item.id"
            :label="`${item.name}（${item.count}）`"
            :value="item.name" /></el-select
        ><el-select v-model="publishState" placeholder="上架状态"
          ><el-option
            label="可上架（未在本店发布）"
            value="available" /><el-option
            label="当前店铺已上架"
            value="listed" /><el-option
            label="正在处理"
            value="processing" /><el-option
            label="失败或已取消，可重试"
            value="retryable" /><el-option
            label="全部产品"
            value="" /></el-select
        ><el-select v-model="supplier" clearable placeholder="供应商"
          ><el-option label="S2B" value="S2B" /><el-option
            label="HICUSTOM"
            value="HICUSTOM" /></el-select
        ><el-select v-model="status" clearable placeholder="产品状态"
          ><el-option label="资料可发布" value="ready" /><el-option
            label="待整理"
            value="draft" /><el-option
            label="已归档"
            value="archived" /></el-select
        ><el-button type="primary" @click="query">查询</el-button
        ><el-button
          :disabled="!canBulkSelect || !selectablePageProducts.length"
          @click="togglePage"
          >{{ pageFullySelected ? "取消本页" : "全选本页" }}（{{
            selectablePageProducts.length
          }}）</el-button
        ><el-button
          type="success"
          plain
          :disabled="!canBulkSelect || !total"
          @click="selectAllFiltered"
          >全选筛选结果 {{ total }} 项</el-button
        ><el-button
          v-if="selectedProducts.length"
          text
          type="danger"
          @click="clearSelection"
          >清空已选</el-button
        >
      </div>
      <div v-loading="loading" class="wb-product-grid">
        <article
          v-for="item in products"
          :key="item.id"
          class="wb-product-card"
          :class="{
            selected: !!selected[item.id],
            blocked: availability[item.id]?.blocked,
          }"
          @click="toggle(item)"
        >
          <div class="image">
            <el-image
              v-if="thumbnail(item)"
              :src="thumbnail(item)"
              fit="cover"
            /><span v-else>暂无图片</span
            ><i>{{ selected[item.id] ? "✓" : "+" }}</i>
          </div>
          <div class="body">
            <h3>{{ item.name }}</h3>
            <small class="mono">{{ item.sku }}</small
            ><b>{{ item.variant_skus?.length || item.variant_count }} 个规格</b
            ><small
              >{{ item.category || "未分类" }} ·
              {{ item.supplier || "未知供应商" }}</small
            ><el-tag
              v-if="!form.store_id || !availability[item.id]" size="small" type="info">{{form.store_id?"待核对本店状态":"请先选择店铺"}}</el-tag
            ><el-tag v-else-if="!availability[item.id]?.blocked"
              size="small"
              type="success"
              >可上架</el-tag
            ><el-tag
              v-else
              size="small"
              :type="
                availability[item.id]?.status === 'completed'
                  ? 'info'
                  : 'warning'
              "
              >{{
                availability[item.id]?.status === "completed"
                  ? "当前店铺已上架"
                  : availability[item.id]?.status === "pending_stock"
                    ? "待添加库存"
                    : availability[item.id]?.status === "stock_processing"
                      ? "库存处理中"
                      : availability[item.id]?.status === "pending_price"
                        ? "待设置价格与折扣"
                        : availability[item.id]?.status === "price_processing"
                          ? "价格与折扣处理中"
                  : availability[item.id]?.status ===
                      "exported_pending_confirmation"
                    ? "Excel 待确认"
                    : availability[item.id]?.status === "failed"
                      ? "已建品，需核验"
                      : availability[item.id]?.status === "cancelled"
                        ? "已建品，需核验"
                        : ["sync_conflict","sync_unknown","stock_failed","price_failed"].includes(availability[item.id]?.status) ? "已建品，需核验" : "正在处理"
              }}</el-tag
            >
          </div>
        </article>
        <el-empty
          v-if="!loading && !products.length"
          :description="
            publishState === 'available'
              ? '当前筛选条件下没有可上架产品'
              : '当前筛选条件下没有产品'
          "
        />
      </div>
      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[30, 60, 90]"
          :total="total"
          layout="total, sizes, prev, pager, next"
          @current-change="changePage"
          @size-change="changePageSize"
        /><span
          >第 {{ page }} / {{ totalPages }} 页，已跨页选择
          {{ selectedProducts.length }} 个</span
        >
      </div>
    </section>

    <section v-if="selectedProducts.length" class="panel">
      <div class="panel-head">
        <div>
          <h2>2. 统一选择本批上架规格</h2>
          <p>
            本批是同一个产品的不同印花，只需选择一次；系统会自动匹配到每个印花的相同规格。
          </p>
        </div>
        <el-tag>{{ selectedVariantKeys.length }} 个规格</el-tag>
      </div>
      <div class="shared-variants">
        <el-checkbox-group v-model="selectedVariantKeys"
          ><el-checkbox
            v-for="option in variantOptions"
            :key="option.key"
            :value="option.key"
            border
            ><b>{{ option.label }}</b
            ><small
              >{{ option.color }} · 覆盖 {{ option.matched }} /
              {{ option.total }} 个印花</small
            ></el-checkbox
          ></el-checkbox-group
        ><el-alert
          v-if="hasVariantGaps"
          title="部分印花缺少所选规格；系统只会提交实际匹配到的 SKU，请在发布前检查数量。"
          type="warning"
          :closable="false"
        />
      </div>
    </section>
    <el-alert
      v-if="selectedProducts.length && selectedStore"
      :title="
        selectedStore.offer_prefix
          ? `当前店铺：${selectedStore.name}；最终卖家 SKU 示例：${selectedStore.offer_prefix.replace(/-$/, '')}-${selectedVariants[0]?.sku || 'ERP-SKU'}`
          : `店铺“${selectedStore.name}”尚未设置 SKU 前缀，提交将被阻止`
      "
      :type="selectedStore.offer_prefix ? 'success' : 'error'"
      :closable="false"
      show-icon
    />
    <el-alert
      v-if="duplicateVariants.length"
      :title="`当前所选规格中有 ${duplicateVariants.length} 个 SKU 已上架或正在处理，提交前必须取消这些重复项`"
      type="error"
      :closable="false"
      show-icon
    />

    <section v-if="selectedProducts.length" class="panel">
      <div class="panel-head">
        <div>
          <h2>3. 统一设置上架图片顺序</h2>
          <p>
            只展示第一个印花作为样例；主图、附图和停用规则会按相同 OSS
            文件名应用到所有印花。
          </p>
        </div>
        <el-tag type="success">{{ enabledImageCount }} 张启用</el-tag>
      </div>
      <div v-loading="imageLoading" class="image-rule-grid">
        <article
          v-for="(row, index) in imageRules"
          :key="row.key"
          class="image-rule-card"
          :class="{
            excluded: row.action === 'exclude',
            main: row.action === 'main',
          }"
        >
          <el-image :src="row.url" fit="cover" :preview-src-list="[row.url]" />
          <div class="image-rule-index">
            {{ row.action === "exclude" ? "不用" : index + 1 }}
          </div>
          <b>{{
            row.action === "main"
              ? "主图"
              : row.action === "additional"
                ? "附图"
                : "不上架"
          }}</b
          ><small class="mono" :title="row.key">{{ row.key }}</small
          ><el-segmented
            :model-value="row.action"
            :options="[
              { label: '主图', value: 'main' },
              { label: '附图', value: 'additional' },
              { label: '不用', value: 'exclude' },
            ]"
            @change="setImageAction(index, String($event))"
          />
          <div>
            <el-button
              size="small"
              :disabled="index === 0"
              @click="moveImage(index, -1)"
              >前移</el-button
            ><el-button
              size="small"
              :disabled="index === imageRules.length - 1"
              @click="moveImage(index, 1)"
              >后移</el-button
            >
          </div>
        </article>
        <el-empty
          v-if="!imageLoading && !imageRules.length"
          description="样例产品没有可公开访问的 OSS 上架图片"
        />
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>4. WB 类目、定价与发布方式</h2>
          <p>
            官方模板限定字段；实时类目字典用于枚举校验；最终按 WB 商品卡每 30
            张自动合并。
          </p>
        </div>
      </div>
      <div class="panel-body">
        <el-form label-width="150" class="wb-form"
          ><el-form-item label="目标 WB 店铺"
            ><div class="store-picker">
              <el-select v-model="form.store_id" placeholder="选择 WB 店铺"
                ><el-option
                  v-for="store in stores"
                  :key="store.id"
                  :label="store.name"
                  :value="store.id" /></el-select
              ><el-button :loading="historyScanning" @click="scanHistory"
                >扫描历史 POD 商品</el-button
              ><el-button type="primary" plain :loading="uploadedSyncing" @click="syncUploaded"
                >同步已上传商品</el-button
              >
            </div></el-form-item
          ><el-form-item label="WB 官方类目模板"
            ><el-select
              v-model="form.template_id"
              filterable
              style="width: 100%"
              placeholder="选择已保存的官方模板"
              ><el-option
                v-for="item in templates"
                :key="item.id"
                :label="`${item.subject_name} · Subject ${item.subject_id} · ${item.filename}`"
                :value="item.id" /></el-select></el-form-item
          ><el-form-item label="品牌"
            ><el-input v-model="form.brand" /></el-form-item
          ><el-form-item label="定价方式"
            ><el-radio-group v-model="form.pricing_mode"
              ><el-radio-button value="profit">目标利润反算</el-radio-button
              ><el-radio-button value="multiplier">成本倍率</el-radio-button
              ><el-radio-button value="manual"
                >统一手动价</el-radio-button
              ></el-radio-group
            ></el-form-item
          ><el-form-item
            v-if="form.pricing_mode === 'manual'"
            label="本批统一价格"
            ><el-input-number
              v-model="form.price_cny"
              :min="1"
              :precision="0"
            /><span class="unit">人民币数值原样提交，不换算</span></el-form-item
          ><template v-else
            ><el-alert
              title="自动定价按人民币成本计算，所得价格数值直接提交给 WB，不做卢布汇率换算。"
              type="info"
              :closable="false"
              show-icon
            /><el-form-item label="后台折扣"
              ><el-input-number
                v-model="form.discount_percent"
                :min="0"
                :max="99"
              /><span class="unit">%</span></el-form-item
            ><el-form-item
              v-if="form.pricing_mode === 'multiplier'"
              label="成本售价倍率"
              ><el-input-number
                v-model="form.multiplier"
                :min="0.1"
                :precision="2" /></el-form-item
            ><template v-else
              ><el-form-item label="利润 / 佣金 / 广告"
                ><el-input-number
                  v-model="form.target_profit_percent"
                  :min="0"
                /><span class="unit">%</span
                ><el-input-number
                  v-model="form.commission_percent"
                  :min="0"
                  :max="99"
                /><span class="unit">%</span
                ><el-input-number
                  v-model="form.advertising_percent"
                  :min="0"
                  :max="99"
                /><span class="unit">%</span></el-form-item
              ><el-form-item label="退货 / 贴标 / 其他"
                ><el-input-number
                  v-model="form.return_percent"
                  :min="0"
                  :max="99"
                /><span class="unit">%</span
                ><el-input-number
                  v-model="form.label_fee_cny"
                  :min="0"
                  :precision="2"
                /><span class="unit">CNY</span
                ><el-input-number
                  v-model="form.other_cost_cny"
                  :min="0"
                  :precision="2"
                /><span class="unit">CNY</span></el-form-item
              ></template
            ></template
          ><el-form-item label="文案要求"
            ><el-input
              v-model="form.requirement"
              type="textarea"
              :rows="3"
              placeholder="俄文标题、描述和属性的额外要求" /></el-form-item
          ><el-form-item label="文案生成方式"><el-radio-group v-model="form.generation_provider"><el-radio-button value="model">现有文案模型</el-radio-button><el-radio-button value="codex">Codex 接手</el-radio-button></el-radio-group></el-form-item>
          <el-form-item v-if="form.generation_provider==='codex'"><el-alert title="创建后可复制给新 Codex 对话；原电脑回填本次文案和属性，图片沿用当前选择。API 发布需核验文案后确认。执行端需 0.6.72 或更新版本。" type="info" :closable="false" /></el-form-item>
          <el-form-item label="发布方式"
            ><el-radio-group v-model="form.mode"
              ><el-radio-button value="excel">导出 WB Excel</el-radio-button
              ><el-radio-button value="api"
                >Seller API 直接上架</el-radio-button
              ></el-radio-group
            ></el-form-item
          ><el-form-item v-if="form.mode === 'api' && form.generation_provider!=='codex'"
            ><el-alert
              title="将直接创建 WB 商品卡并上传当前排序的图片。任务会先按卖家 SKU 查询，断点重试不会重复建卡。"
              type="warning"
              :closable="false"
              show-icon /></el-form-item
          ><el-form-item label="发布前检查"
            ><el-button type="success" plain :loading="previewing" :disabled="submitting" @click="refreshPreview"
              >校验价格、变体与 30 卡分组</el-button
            ></el-form-item
          >
          <div v-if="preview" class="group-preview">
            <div class="metrics">
              <div>
                <strong>{{ preview.product_count }}</strong
                ><span>ERP 产品</span>
              </div>
              <div>
                <strong>{{ preview.variant_count }}</strong
                ><span>销售变体</span>
              </div>
              <div>
                <strong>{{ preview.card_count }}</strong
                ><span>WB 商品卡</span>
              </div>
              <div>
                <strong>{{ preview.group_count }}</strong
                ><span>合并链接</span>
              </div>
            </div>
            <el-alert
              v-if="preview.pricing_errors?.length"
              :title="`${preview.pricing_errors.length} 个 SKU 无法定价：${preview.pricing_errors
                .slice(0, 3)
                .map((x: any) => x.sku + ' ' + x.error)
                .join('；')}`"
              type="error"
              :closable="false"
            /><el-alert
              v-if="preview.split_size_cards"
              title="当前是毛毯类目：尺寸属于商品卡级属性，因此每个所选尺寸会拆成独立 WB 商品卡。"
              type="warning"
              :closable="false"
            />
            <div class="price-chips">
              <el-tag
                v-for="row in preview.products.flatMap((p: any) => p.variants).slice(0, 100)"
                :key="row.id"
                :type="row.pricing.error ? 'danger' : 'success'"
                >{{ row.sku }} ·
                {{
                  row.pricing.error || row.pricing.list_price_rub + " CNY"
                }}</el-tag
              >
            </div>
            <p v-if="preview.variant_count > 100">仅展示前 100 个变体；全部 {{ preview.variant_count }} 个已完成校验。</p>
            <div class="groups">
              <el-tag
                v-for="group in preview.groups"
                :key="group.index"
                size="large"
                >链接 {{ group.index }} · {{ group.card_count }} / 30
                张卡</el-tag
              >
            </div>
          </div>
          <el-form-item
            ><el-button
              type="primary"
              size="large"
              :loading="submitting"
              :disabled="!selectedProducts.length"
              @click="createTask"
              >{{
                form.mode === "excel"
                  ? "生成 WB Excel 发布包"
                  : "创建 Seller API 发布包"
              }}</el-button
            ><span v-if="publishProgress" class="unit">{{ publishProgress }}</span></el-form-item
          ></el-form
        >
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>最近 WB 发布任务</h2>
          <p>
            Excel 导出后需要人工确认；Seller API 会自动核对 WB
            商品编号并上传图片。
          </p>
        </div>
      </div>
      <el-table :data="displayExports" row-key="id" empty-text="暂无 WB 发布任务"
        ><el-table-column type="expand" width="45"><template #default="{row}"><template v-if="row.grouped"><p>本次 Codex 批次共 {{row.children.length}} 个发布包，原任务和电脑归属保持不变。</p><el-table :data="row.children"><el-table-column label="产品数" width="100"><template #default="{row}">{{row.payload?.product_ids?.length||0}}</template></el-table-column><el-table-column prop="status" label="阶段"/><el-table-column label="操作" min-width="440"><template #default="{row}"><el-button link type="primary" @click="openCodex(row)">核验本组文案</el-button><RouterLink :to="`/jobs?job=${row.id}`">执行进度</RouterLink><a v-if="row.download_ready" :href="`/api/v1/pod/wb/exports/${row.id}/download`" class="download">下载本组 Excel</a><template v-if="row.publication_status==='exported_pending_confirmation'"><el-button link type="success" @click="confirmUpload(row,true)">本组已上传 WB</el-button><el-button link type="danger" @click="confirmUpload(row,false)">本组未上传，取消锁定</el-button></template></template></el-table-column></el-table></template></template></el-table-column><el-table-column
          label="时间"
          width="190"
          prop="created_at"
        /><el-table-column label="方式" width="130"
          ><template #default="{ row }"
            ><el-tag
              :type="
                row.mode === 'api'
                  ? 'success'
                  : row.mode === 'history'
                    ? 'warning'
                    : 'info'
              "
              >{{
                row.mode === "api"
                  ? "Seller API"
                  : row.mode === "history"
                    ? "历史扫描"
                    : "Excel"
              }}</el-tag
            ></template
          ></el-table-column
        ><el-table-column label="产品数" width="100"
          ><template #default="{ row }">{{
            row.payload?.product_ids?.length ??
            (row.mode === "history" ? "-" : 0)
          }}<el-tag v-if="row.copy_failed_items" type="danger" size="small">文案失败 {{ row.copy_failed_items }}</el-tag></template></el-table-column
        ><el-table-column
          label="任务"
          width="110"
          prop="status"
        ><template #default="{row}">{{row.status==='paused' && row.payload?.generation_provider==='codex' ? (row.payload?.codex_outputs&&!row.grouped ? '待确认发布' : '等待 Codex') : row.status}}<small v-if="row.grouped"> · {{row.children.length}} 组</small><el-progress v-if="row.grouped" :percentage="row.progress" :stroke-width="6"/></template></el-table-column><el-table-column label="上架台账" min-width="220"
          ><template #default="{ row }"
            ><el-tag
              v-if="row.publication_status === 'exported_pending_confirmation'"
              type="warning"
              >Excel 待确认</el-tag
            ><el-tag
              v-else-if="row.publication_status === 'completed'"
              type="success"
              >已在 WB 创建</el-tag
            ><el-tag
              v-else-if="row.publication_status === 'submitted'"
              type="warning"
              >已确认上传 WB</el-tag
            ><el-tag
              v-else-if="row.publication_status === 'pending_stock'"
              type="warning"
              >待添加库存</el-tag
            ><el-tag
              v-else-if="row.publication_status === 'cancelled'"
              type="info"
              >已取消</el-tag
            ><span v-else>{{
              row.mode === "history"
                ? "扫描完成后自动关联"
                : row.publication_status || "-"
            }}</span
            ><template v-if="row.external_product_count"
              ><span class="nm-summary"
                >已关联 {{ row.external_product_count }} 个 WB 商品</span
              ><el-popover
                v-if="row.external_product_ids?.length"
                placement="top"
                :width="360"
                trigger="click"
                ><template #reference
                  ><el-button link>查看示例 nmID</el-button></template
                >
                <div class="nm-list mono">
                  {{ row.external_product_ids.join("\n")
                  }}<small
                    v-if="
                      row.external_product_count >
                      row.external_product_ids.length
                    "
                    >仅展示前
                    {{
                      row.external_product_ids.length
                    }}
                    个，完整结果见任务报告</small
                  >
                </div></el-popover
              ></template
            ></template
          ></el-table-column
        ><el-table-column label="操作" min-width="260"
          ><template #default="{ row }"
            ><el-button v-if="row.payload?.generation_provider==='codex'" link type="primary" @click="openCodex(row)">{{row.grouped?'复制本批全部提示词':'Codex 接管 / 核验文案'}}</el-button><RouterLink :to="`/jobs?job=${row.grouped?row.children[0].id:row.id}`">任务详情</RouterLink
            ><a
              v-if="row.history_report_asset_id"
              class="download"
              :href="`/api/v1/pod/wb/history/${row.id}/report`"
              target="_blank"
              rel="noopener"
              >匹配报告</a
            ><a
              v-if="row.download_ready"
              class="download"
              :href="`/api/v1/pod/wb/exports/${row.id}/download`"
              >下载 Excel</a
            ><template
              v-if="!row.grouped && row.publication_status === 'exported_pending_confirmation'"
              ><el-button link type="success" @click="confirmUpload(row, true)"
                >已上传 WB</el-button
              ><el-button link type="danger" @click="confirmUpload(row, false)"
                >未上传，取消锁定</el-button
              ></template
            ></template
          ></el-table-column
        ></el-table
      >
    </section>
  </div>
</template>
<style scoped>
.store-picker {
  display: flex;
  width: 100%;
  gap: 10px;
}
.store-picker .el-select {
  flex: 1;
}
.nm-summary {
  margin-left: 10px;
  color: #52657a;
}
.nm-list {
  white-space: pre-line;
  max-height: 240px;
  overflow: auto;
}
.nm-list small {
  display: block;
  margin-top: 10px;
  color: #8492a6;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
}
.price-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 12px 0;
}
.shared-variants {
  padding: 20px;
}
.shared-variants .el-checkbox-group {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 16px;
}
.shared-variants .el-checkbox {
  height: auto;
  min-width: 210px;
  margin: 0;
  padding: 12px 16px;
}
.shared-variants b,
.shared-variants small {
  display: block;
}
.shared-variants small {
  margin-top: 4px;
  color: #718096;
}
.image-rule-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: 14px;
  padding: 20px;
}
.image-rule-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  border: 2px solid #dce5f0;
  border-radius: 12px;
  background: #fff;
}
.image-rule-card.main {
  border-color: #7b2cbf;
}
.image-rule-card.excluded {
  opacity: 0.48;
  filter: grayscale(0.8);
}
.image-rule-card .el-image {
  width: 100%;
  aspect-ratio: 1/1;
  border-radius: 8px;
  background: #edf1f6;
}
.image-rule-card small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #718096;
}
.image-rule-index {
  position: absolute;
  z-index: 2;
  top: 16px;
  left: 16px;
  padding: 4px 9px;
  border-radius: 999px;
  background: #172b4dcc;
  color: #fff;
  font-weight: 700;
}
</style>

<style scoped>
.wb-product-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px;
  padding: 18px;
  background: #f6f8fb;
  min-height: 220px;
}
.wb-product-card {
  overflow: hidden;
  border: 2px solid transparent;
  border-radius: 12px;
  background: #fff;
  cursor: pointer;
  box-shadow: 0 3px 12px #1c324e14;
}
.wb-product-card.selected {
  border-color: #7b2cbf;
}
.image {
  position: relative;
  display: grid;
  place-items: center;
  aspect-ratio: 4/3;
  background: #edf1f6;
  color: #8996a8;
}
.image .el-image {
  width: 100%;
  height: 100%;
}
.image i {
  position: absolute;
  right: 10px;
  top: 10px;
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: #fff;
  color: #7b2cbf;
  font-style: normal;
  font-size: 20px;
  font-weight: 800;
}
.selected .image i {
  color: #fff;
  background: #7b2cbf;
}
.body {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 13px;
}
.body h3 {
  margin: 0;
  color: #193553;
  font-size: 15px;
}
.body small {
  color: #718096;
}
.variant-products {
  display: grid;
  gap: 14px;
  padding: 18px;
}
.variant-product {
  padding: 14px;
  border: 1px solid #dce5f0;
  border-radius: 10px;
}
.variant-product header {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 12px;
}
.variant-product header > div {
  display: flex;
  flex: 1;
  flex-direction: column;
}
.variant-product .el-checkbox-group {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.variant-product .el-checkbox {
  height: auto;
  margin: 0;
  padding: 9px 12px;
}
.variant-product .el-checkbox b,
.variant-product .el-checkbox small {
  display: block;
}
.wb-form {
  max-width: 1050px;
}
.wb-form .el-input {
  max-width: 620px;
}
.unit {
  margin-left: 8px;
}
.group-preview {
  margin: 0 0 22px 150px;
  padding: 16px;
  border: 1px solid #ded4ec;
  border-radius: 12px;
  background: #fbf8ff;
}
.metrics {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-bottom: 14px;
}
.metrics div {
  padding: 12px;
  border-radius: 9px;
  background: #fff;
}
.metrics strong,
.metrics span {
  display: block;
}
.metrics strong {
  color: #6d28a8;
  font-size: 24px;
}
.metrics span {
  color: #718096;
  font-size: 12px;
}
.groups {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.download {
  margin-left: 16px;
}
@media (max-width: 700px) {
  .wb-product-grid {
    grid-template-columns: 1fr;
  }
  .metrics {
    grid-template-columns: 1fr 1fr;
  }
  .group-preview {
    margin-left: 0;
  }
}
</style>
