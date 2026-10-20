<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import type { Listing, StoreListing } from "../types";
import { statusLabel } from "../status";
const props = defineProps<{
  mode: "submit" | "stock" | "promotion" | "completed";
}>();
const all = ref<Listing[]>([]),
  loading = ref(false),
  selected = ref<string[]>([]),
  stockSettingsVisible = ref(false),
  promotionSettingsVisible = ref(false);
const warehouseOptions = reactive<Record<string, any[]>>({}),
  promotionOptions = reactive<Record<string, any[]>>({}),
  inputs = reactive<Record<string, any>>({}),
  stockStoreInputs = reactive<Record<string, any>>({}),
  promotionStoreInputs = reactive<Record<string, any>>({});
const info = {
  submit: { title: "等待提交 OZON", empty: "当前没有待提交的商品" },
  stock: { title: "等待添加库存", empty: "当前没有待添加库存的商品" },
  promotion: { title: "等待添加促销", empty: "当前没有待添加促销的商品" },
  completed: { title: "已经完成全部流程", empty: "当前还没有已完成的商品" },
}[props.mode];
function storeMatches(s: StoreListing) {
  if (!s.store_enabled) return false;
  if (props.mode === "completed") return s.status === "completed";
  if (props.mode === "stock")
    return ["stock_pending", "IMPORT_COMPLETED_STOCK_PENDING"].includes(
      s.status,
    );
  if (props.mode === "promotion")
    return [
      "promotion_pending",
      "STOCK_COMPLETED_PROMOTION_PENDING",
      "PUBLISHED",
    ].includes(s.status);
  return ["draft", "ready", "pending"].includes(s.status);
}
function matches(item: Listing) {
  return props.mode === "completed"
    ? item.status === "completed" || item.stores.some(storeMatches)
    : item.stores.some(storeMatches);
}
const items = computed(() => all.value.filter(matches));
const stockRows = computed(() =>
  all.value.flatMap((listing) =>
    listing.stores.filter(storeMatches).map((store) => ({ listing, store })),
  ),
);
const promotionRows = computed(() =>
  all.value.flatMap((listing) =>
    listing.stores.filter(storeMatches).map((store) => ({ listing, store })),
  ),
);
const selectedStockRows = computed(() =>
  stockRows.value.filter((row) => selected.value.includes(row.store.id)),
);
const selectedStockStores = computed(() => {
  const rows = selectedStockRows.value.length
    ? selectedStockRows.value
    : stockRows.value;
  return [
    ...new Map(rows.map((row) => [row.store.store_id, row.store])).values(),
  ];
});
const selectedPromotionRows = computed(() =>
  promotionRows.value.filter((row) => selected.value.includes(row.store.id)),
);
const selectedPromotionStores = computed(() => [
  ...new Map(
    selectedPromotionRows.value.map((row) => [row.store.store_id, row.store]),
  ).values(),
]);
const submitRows = computed(() =>
  all.value.flatMap((listing) =>
    listing.stores
      .filter((store) => ["ready", "import_failed"].includes(store.status))
      .map((store) => ({ listing, store })),
  ),
);
async function load() {
  loading.value = true;
  try {
    all.value = (await api.get<any>("/listings?page_size=200")).items;
    for (const listing of all.value)
      for (const store of listing.stores) {
        const storageKey = props.mode === "promotion"
          ? `ozon-promotion-input:${store.store_id}`
          : `ozon-stock-input:${store.store_id}`;
        const saved = localStorage.getItem(storageKey);
        inputs[store.id] ??= saved ? JSON.parse(saved) : { stock: 1 };
      }
    if (props.mode === "stock") await loadStockWarehouses();
    if (props.mode === "promotion") await loadPromotions();
  } finally {
    loading.value = false;
  }
}
function selection(rows: any[]) {
  selected.value = rows.map((x) => x.store.id);
}
function cachedWarehouses(storeId: string) {
  try {
    return JSON.parse(
      localStorage.getItem(`ozon-warehouses:${storeId}`) || "[]",
    );
  } catch {
    return [];
  }
}
async function loadWarehouse(storeId: string, refresh = false) {
  const cached = cachedWarehouses(storeId);
  if (cached.length && !refresh) {
    warehouseOptions[storeId] = cached;
    return;
  }
  const rows =
    (await api.get<any>(`/stores/${storeId}/warehouses`)).items || [];
  warehouseOptions[storeId] = rows;
  localStorage.setItem(`ozon-warehouses:${storeId}`, JSON.stringify(rows));
}
async function loadStockWarehouses(refresh = false) {
  const storeIds = [
    ...new Set(stockRows.value.map((row) => row.store.store_id)),
  ];
  await Promise.all(storeIds.map((id) => loadWarehouse(id, refresh)));
  if (refresh) ElMessage.success("各店铺仓库已刷新并保存在本地");
}
function saveStockInput(store: StoreListing) {
  localStorage.setItem(
    `ozon-stock-input:${store.store_id}`,
    JSON.stringify(inputs[store.id]),
  );
}
function cachedPromotions(storeId: string) {
  try {
    return JSON.parse(localStorage.getItem(`ozon-promotions:${storeId}`) || "[]");
  } catch {
    return [];
  }
}
async function loadPromotion(storeId: string, refresh = false) {
  const cached = cachedPromotions(storeId);
  if (cached.length && !refresh) {
    promotionOptions[storeId] = cached;
    return;
  }
  const rows = (await api.get<any>(`/stores/${storeId}/promotions`)).items || [];
  promotionOptions[storeId] = rows;
  localStorage.setItem(`ozon-promotions:${storeId}`, JSON.stringify(rows));
}
async function loadPromotions(refresh = false) {
  const storeIds = [...new Set(promotionRows.value.map((row) => row.store.store_id))];
  const results = await Promise.allSettled(storeIds.map((id) => loadPromotion(id, refresh)));
  const failed = results.filter((result) => result.status === "rejected").length;
  if (failed) ElMessage.error(`${failed} 个店铺的促销活动读取失败`);
  else if (refresh) ElMessage.success("各店铺促销活动已刷新并保存在本地");
}
function savePromotionInput(store: StoreListing) {
  localStorage.setItem(`ozon-promotion-input:${store.store_id}`, JSON.stringify(inputs[store.id]));
}
function promotionId(item: any) {
  return item.id || item.action_id;
}
function promotionLabel(item: any) {
  const title = item.title || item.name || `活动 ${promotionId(item)}`;
  const end = item.date_end || item.end_date || item.end_at;
  return end ? `${title}（截止 ${String(end).slice(0, 10)}）` : title;
}
function openStockSettings() {
  if (!selected.value.length)
    return ElMessage.warning("请先勾选要添加库存的店铺商品");
  for (const store of selectedStockStores.value) {
    const first = selectedStockRows.value.find(
      (row) => row.store.store_id === store.store_id,
    );
    const saved = first ? inputs[first.store.id] : undefined;
    stockStoreInputs[store.store_id] ??= {
      warehouse_id: saved?.warehouse_id,
      stock: saved?.stock || 1,
    };
  }
  stockSettingsVisible.value = true;
}
function applyStockSettings() {
  for (const store of selectedStockStores.value) {
    const value = stockStoreInputs[store.store_id];
    if (!value?.warehouse_id || !value?.stock)
      return ElMessage.warning(`请填写 ${store.store_name} 的仓库和库存`);
  }
  for (const row of selectedStockRows.value) {
    inputs[row.store.id] = { ...stockStoreInputs[row.store.store_id] };
    saveStockInput(row.store);
  }
  stockSettingsVisible.value = false;
  ElMessage.success("已按店铺应用仓库和库存");
}
function openPromotionSettings() {
  if (!selected.value.length) return ElMessage.warning("请先勾选要添加促销的店铺商品");
  for (const store of selectedPromotionStores.value) {
    const first = selectedPromotionRows.value.find((row) => row.store.store_id === store.store_id);
    promotionStoreInputs[store.store_id] = {
      promotion_action_id: first ? inputs[first.store.id]?.promotion_action_id : undefined,
    };
  }
  promotionSettingsVisible.value = true;
}
function applyPromotionSettings() {
  for (const store of selectedPromotionStores.value) {
    if (!promotionStoreInputs[store.store_id]?.promotion_action_id)
      return ElMessage.warning(`请选择 ${store.store_name} 的促销活动`);
  }
  for (const row of selectedPromotionRows.value) {
    inputs[row.store.id] = { ...inputs[row.store.id], ...promotionStoreInputs[row.store.store_id] };
    savePromotionInput(row.store);
  }
  promotionSettingsVisible.value = false;
  ElMessage.success("已按店铺应用促销活动");
}
async function submitOne(row: any) {
  await ElMessageBox.confirm(
    `确认提交 ${row.store.store_name} 的商品到 OZON？`,
    "真实提交确认",
    { type: "warning" },
  );
  await api.post(`/listings/${row.listing.id}/run`, {
    dry_run: false,
    store_ids: [row.store.store_id],
    start_at: "submit",
  });
  ElMessage.success(`${row.store.store_name} 提交任务已进入队列`);
  load();
}
async function resetFailed(row: any) {
  await ElMessageBox.confirm(
    `确认清除 ${row.store.store_name} 的失败任务并重新提交？`,
    "重新提交确认",
    { type: "warning" },
  );
  await api.post(
    `/listings/${row.listing.id}/stores/${row.store.store_id}/reset-import`,
  );
  ElMessage.success("失败任务已清除，请重新点击提交本店");
  await load();
}
async function submitBatch() {
  if (!selected.value.length) return ElMessage.warning("请先勾选店铺版本");
  await ElMessageBox.confirm(
    `确认提交 ${selected.value.length} 个店铺版本到 OZON？`,
    "真实提交确认",
    { type: "warning" },
  );
  for (const id of selected.value) {
    const row = submitRows.value.find((item) => item.store.id === id);
    if (row)
      await api.post(`/listings/${row.listing.id}/run`, {
        dry_run: false,
        store_ids: [row.store.store_id],
        start_at: "submit",
      });
  }
  ElMessage.success("提交任务已进入队列");
  load();
}
async function stockBatch() {
  if (!selected.value.length) return ElMessage.warning("请先勾选店铺商品");
  const rows = stockRows.value.filter((row) =>
    selected.value.includes(row.store.id),
  );
  if (
    rows.some(
      (row) =>
        !inputs[row.store.id]?.warehouse_id || !inputs[row.store.id]?.stock,
    )
  )
    return ElMessage.warning("请为勾选的每个店铺商品选择仓库并填写库存");
  await ElMessageBox.confirm(
    `确认向 ${rows.length} 个店铺商品发布库存？`,
    "批量库存确认",
    { type: "warning" },
  );
  for (const row of rows) {
    const value = inputs[row.store.id];
    saveStockInput(row.store);
    await api.post(`/listings/${row.listing.id}/run`, {
      dry_run: false,
      store_ids: [row.store.store_id],
      start_at: "stock",
      warehouse_id: Number(value.warehouse_id),
      stock: Number(value.stock),
    });
  }
  ElMessage.success("各店铺库存任务已分别进入队列");
  await load();
}
async function promotionBatch() {
  if (!selected.value.length) return ElMessage.warning("请先勾选店铺商品");
  const rows = selectedPromotionRows.value;
  if (rows.some((row) => !inputs[row.store.id]?.promotion_action_id))
    return ElMessage.warning("请为勾选的每个店铺商品选择促销活动");
  await ElMessageBox.confirm(`确认将 ${rows.length} 个店铺商品加入所选促销？`, "批量促销确认", { type: "warning" });
  for (const row of rows) {
    const value = inputs[row.store.id];
    savePromotionInput(row.store);
    await api.post(`/listings/${row.listing.id}/run`, {
      dry_run: false, store_ids: [row.store.store_id], start_at: "promotion",
      promotion_action_id: Number(value.promotion_action_id),
      ozon_product_id: Number(row.store.external_product_id),
      action_price: Number(row.store.pricing.action_price || row.store.pricing.price),
      stock: Number(value.stock || 1),
    });
  }
  ElMessage.success("各店铺促销任务已分别进入队列");
  await load();
}
async function manualCompleteRows(rows: Array<{ listing: Listing; store: StoreListing }>) {
  if (!rows.length) return ElMessage.warning("请先勾选店铺商品");
  await ElMessageBox.confirm(
    `确认将 ${rows.length} 个店铺商品手动移入已完成？此操作只更新 ERP 状态，不会向 OZON 添加库存或促销。`,
    "手动完成确认",
    { type: "warning", confirmButtonText: "确认移入已完成" },
  );
  for (const row of rows) {
    await api.post(`/listings/${row.listing.id}/stores/${row.store.store_id}/manual-complete`);
  }
  ElMessage.success(`${rows.length} 个店铺商品已移入完成`);
  await load();
}
function manualCompleteSelected() {
  const rows = props.mode === "stock" ? selectedStockRows.value : selectedPromotionRows.value;
  return manualCompleteRows(rows);
}
async function loadOptions(store: StoreListing) {
  if (props.mode === "stock") {
    await loadWarehouse(store.store_id);
  } else {
    promotionOptions[store.store_id] = (
      await api.get<any>(`/stores/${store.store_id}/promotions`)
    ).items;
  }
  inputs[store.id] ??= { stock: 1 };
}
async function runStore(listing: Listing, store: StoreListing) {
  const value = inputs[store.id] || {};
  const body: any = {
    dry_run: false,
    store_ids: [store.store_id],
    start_at: props.mode,
  };
  if (props.mode === "stock") {
    if (!value.warehouse_id || !value.stock)
      return ElMessage.warning("请选择仓库并填写库存");
    body.warehouse_id = Number(value.warehouse_id);
    body.stock = Number(value.stock);
    saveStockInput(store);
  } else {
    if (!value.promotion_action_id) return ElMessage.warning("请选择促销活动");
    body.promotion_action_id = Number(value.promotion_action_id);
    body.ozon_product_id = Number(store.external_product_id);
    body.action_price = Number(
      store.pricing.action_price || store.pricing.price,
    );
    body.stock = Number(value.stock || 1);
    savePromotionInput(store);
  }
  await ElMessageBox.confirm(
    `确认对 ${store.store_name} 执行${props.mode === "stock" ? "库存发布" : "加入促销"}？`,
    "操作确认",
    { type: "warning" },
  );
  await api.post(`/listings/${listing.id}/run`, body);
  ElMessage.success("任务已进入队列");
  await load();
}
onMounted(load);
</script>
<template>
  <section class="panel">
    <div class="panel-head">
      <div>
        <h2>
          {{
            props.mode === "submit"
              ? submitRows.length
              : props.mode === "stock"
                ? stockRows.length
                : props.mode === "promotion"
                  ? promotionRows.length
                  : items.length
          }}
          个店铺版本{{ info.title }}
        </h2>
        <p>
          {{
            props.mode === "submit"
              ? "只有已完成 AI 准备并标记 ready 的店铺版本会显示；每个店铺单独提交"
              : "每家店铺独立处理；已成功的外部操作不会重复执行"
          }}
        </p>
      </div>
      <div>
        <el-button @click="load">刷新</el-button
        ><el-button
          v-if="props.mode === 'stock'"
          @click="loadStockWarehouses(true)"
          >刷新仓库</el-button
        ><el-button
          v-if="props.mode === 'promotion'"
          @click="loadPromotions(true)"
          >刷新促销</el-button
        ><el-button
          v-if="props.mode === 'submit'"
          type="primary"
          :disabled="!selected.length"
          @click="submitBatch"
          >批量提交店铺（{{ selected.length }}）</el-button
        ><el-button
          v-if="props.mode === 'stock'"
          :disabled="!selected.length"
          @click="openStockSettings"
          >批量设置仓库与库存</el-button
        ><el-button
          v-if="props.mode === 'stock'"
          type="primary"
          :disabled="!selected.length"
          @click="stockBatch"
          >批量添加库存（{{ selected.length }}）</el-button
        ><el-button
          v-if="props.mode === 'promotion'"
          :disabled="!selected.length"
          @click="openPromotionSettings"
          >批量设置促销</el-button
        ><el-button
          v-if="props.mode === 'promotion'"
          type="primary"
          :disabled="!selected.length"
          @click="promotionBatch"
          >批量添加促销（{{ selected.length }}）</el-button
        ><el-button
          v-if="['stock', 'promotion'].includes(props.mode)"
          type="warning"
          plain
          :disabled="!selected.length"
          @click="manualCompleteSelected"
          >手动移入已完成（{{ selected.length }}）</el-button
        >
      </div>
    </div>
    <el-table
      v-loading="loading"
      :data="
        props.mode === 'submit'
          ? submitRows
          : props.mode === 'stock'
            ? stockRows
            : props.mode === 'promotion'
              ? promotionRows
              : items
      "
      :empty-text="info.empty"
      @selection-change="selection"
      ><el-table-column
        v-if="['submit', 'stock', 'promotion'].includes(props.mode)"
        type="selection"
        width="48" /><el-table-column label="店铺 Offer ID" width="180"
        ><template #default="scope">{{
          props.mode === "submit"
            ? scope.row.store.offer_id
            : ["stock", "promotion"].includes(props.mode)
              ? scope.row.store.offer_id
              : scope.row.base_offer_id
        }}</template></el-table-column
      ><el-table-column label="商品" min-width="260"
        ><template #default="scope">{{
          props.mode === "submit"
            ? scope.row.store.content.title || scope.row.listing.title
            : ["stock", "promotion"].includes(props.mode)
              ? scope.row.store.content.title || scope.row.listing.title
              : scope.row.title
        }}</template></el-table-column
      ><el-table-column label="店铺" width="150"
        ><template #default="scope">{{
          props.mode === "submit"
            ? scope.row.store.store_name
            : ["stock", "promotion"].includes(props.mode)
              ? scope.row.store.store_name
              : scope.row.stores
                  .filter(storeMatches)
                  .map((store: any) => store.store_name)
                  .join("、")
        }}</template></el-table-column
      ><el-table-column v-if="props.mode === 'submit'" label="状态" width="120"
        ><template #default="scope"
          ><el-tag type="success">{{
            statusLabel(scope.row.store.status)
          }}</el-tag></template
        ></el-table-column
      ><el-table-column v-if="props.mode === 'submit'" label="操作" width="150"
        ><template #default="scope"
          ><el-button
            v-if="scope.row.store.status === 'ready'"
            type="primary"
            @click="submitOne(scope.row)"
            >提交本店</el-button
          ><el-button v-else type="warning" @click="resetFailed(scope.row)"
            >清除失败并重提</el-button
          ></template
        ></el-table-column
      ><el-table-column
        v-if="props.mode === 'stock'"
        label="仓库与库存"
        min-width="430"
        ><template #default="scope"
          ><div class="inline-operation">
            <el-select
              v-model="inputs[scope.row.store.id].warehouse_id"
              placeholder="选择仓库"
              @change="saveStockInput(scope.row.store)"
            >
              <el-option
                v-for="w in warehouseOptions[scope.row.store.store_id] || []"
                :key="w.warehouse_id || w.id"
                :label="w.name || w.warehouse_name"
                :value="w.warehouse_id || w.id"
              />
            </el-select>
            <el-input-number
              v-model="inputs[scope.row.store.id].stock"
              :min="1"
              @change="saveStockInput(scope.row.store)"
            />
            <el-button
              type="primary"
              @click="runStore(scope.row.listing, scope.row.store)"
              >本店添加库存</el-button
            >
            <el-button
              type="warning"
              plain
              @click="manualCompleteRows([scope.row])"
              >手动完成</el-button
            >
          </div></template
        ></el-table-column
      ><el-table-column
        v-if="props.mode === 'promotion'"
        label="促销活动"
        min-width="440"
        ><template #default="scope"
          ><div class="inline-operation">
            <el-select
              v-model="inputs[scope.row.store.id].promotion_action_id"
              filterable
              placeholder="选择从 OZON 读取的促销活动"
              @change="savePromotionInput(scope.row.store)"
            >
              <el-option
                v-for="item in promotionOptions[scope.row.store.store_id] || []"
                :key="promotionId(item)"
                :label="promotionLabel(item)"
                :value="promotionId(item)"
              />
            </el-select>
            <el-button type="primary" @click="runStore(scope.row.listing, scope.row.store)"
              >本店添加促销</el-button
            >
            <el-button
              type="warning"
              plain
              @click="manualCompleteRows([scope.row])"
              >手动完成</el-button
            >
          </div></template
        ></el-table-column
      ><el-table-column
        v-if="!['submit', 'stock', 'promotion'].includes(props.mode)"
        label="店铺进度"
        min-width="430"
        ><template #default="scope"
          ><div
            v-for="store in scope.row.stores.filter(storeMatches)"
            :key="store.id"
            class="queue-store"
          >
            <div class="store-status-line">
              <b>{{ store.store_name }}</b
              ><span>{{ statusLabel(store.status) }}</span
              ><small v-if="store.last_error">{{ store.last_error }}</small>
            </div>
            <div v-if="props.mode === 'stock'" class="inline-operation">
              <el-select
                v-model="inputs[store.id].warehouse_id"
                placeholder="选择仓库"
                @visible-change="(v: boolean) => v && loadOptions(store)"
                ><el-option
                  v-for="w in warehouseOptions[store.store_id] || []"
                  :key="w.warehouse_id || w.id"
                  :label="w.name || w.warehouse_name"
                  :value="w.warehouse_id || w.id" /></el-select
              ><el-input-number
                v-model="inputs[store.id].stock"
                :min="1"
              /><el-button type="primary" @click="runStore(scope.row, store)"
                >本店添加库存</el-button
              >
            </div>
          </div></template
        ></el-table-column
      ><el-table-column prop="updated_at" label="更新时间" width="180"
    /></el-table>
    <el-dialog
      v-model="stockSettingsVisible"
      title="按店铺批量设置仓库与库存"
      width="680px"
    >
      <el-table :data="selectedStockStores">
        <el-table-column prop="store_name" label="店铺" min-width="130" />
        <el-table-column label="仓库" min-width="280">
          <template #default="scope">
            <el-select
              v-model="stockStoreInputs[scope.row.store_id].warehouse_id"
              placeholder="选择该店铺仓库"
              style="width: 100%"
            >
              <el-option
                v-for="w in warehouseOptions[scope.row.store_id] || []"
                :key="w.warehouse_id || w.id"
                :label="w.name || w.warehouse_name"
                :value="w.warehouse_id || w.id"
              />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="每个商品库存" width="160">
          <template #default="scope"
            ><el-input-number
              v-model="stockStoreInputs[scope.row.store_id].stock"
              :min="1"
              style="width: 130px"
          /></template>
        </el-table-column>
      </el-table>
      <template #footer
        ><el-button @click="stockSettingsVisible = false">取消</el-button
        ><el-button type="primary" @click="applyStockSettings"
          >应用到已勾选商品</el-button
        ></template
      >
    </el-dialog>
    <el-dialog
      v-model="promotionSettingsVisible"
      title="按店铺批量设置促销活动"
      width="720px"
    >
      <el-table :data="selectedPromotionStores">
        <el-table-column prop="store_name" label="店铺" min-width="150" />
        <el-table-column label="OZON 促销活动" min-width="430">
          <template #default="scope">
            <el-select
              v-model="promotionStoreInputs[scope.row.store_id].promotion_action_id"
              filterable
              placeholder="选择该店铺促销活动"
              style="width: 100%"
            >
              <el-option
                v-for="item in promotionOptions[scope.row.store_id] || []"
                :key="promotionId(item)"
                :label="promotionLabel(item)"
                :value="promotionId(item)"
              />
            </el-select>
          </template>
        </el-table-column>
      </el-table>
      <template #footer
        ><el-button @click="promotionSettingsVisible = false">取消</el-button
        ><el-button type="primary" @click="applyPromotionSettings"
          >应用到已勾选商品</el-button
        ></template
      >
    </el-dialog>
  </section>
</template>
