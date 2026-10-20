<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import { sharedProductsPilot } from "../deployment";
import type { Listing, ResearchProduct, Store, StoreListing } from "../types";
import { statusLabel } from "../status";
const items = ref<Listing[]>([]),
  products = ref<ResearchProduct[]>([]),
  stores = ref<Store[]>([]),
  open = ref(false),
  drawer = ref(false),
  detail = ref<Listing>();
const keyword = ref("");
function sourceImage(row: Listing): string {
  if (!sharedProductsPilot.value) return `/api/v1/listings/${row.id}/source-image`;
  const value = products.value.find((item) => item.id === row.research_product_id)?.image_url || "";
  try {
    const url = new URL(value);
    return url.protocol === "https:" && (url.hostname === "ozone.ru" || url.hostname.endsWith(".ozone.ru")) ? url.href : "";
  } catch { return ""; }
}
const filteredItems = computed(() => {
  const value = keyword.value.trim().toLowerCase();
  return value
    ? items.value.filter((item) =>
        `${item.title} ${item.base_offer_id} ${item.research_product?.source_sku || ""}`
          .toLowerCase()
          .includes(value),
      )
    : items.value;
});
const selectedStoreId = ref("");
const form = reactive({
  research_product_id: "",
  base_offer_id: "",
  store_ids: [] as string[],
  catalog_primary_store_id: "",
});
watch(
  () => [...form.store_ids],
  (ids) => {
    if (!ids.includes(form.catalog_primary_store_id))
      form.catalog_primary_store_id = ids[0] || "";
  },
);
const edit = reactive({
  title: "",
  description_ru: "",
  price: 0,
  old_price: 0,
  depth_mm: 300,
  width_mm: 200,
  height_mm: 100,
  weight_g: 0,
  attributesText: "[]",
});
const definitions = ref<any[]>([]),
  attributeInput = reactive<Record<number, any>>({}),
  attributeOptions = reactive<Record<number, any[]>>({}),
  attributesLoading = ref(false);
const attributeRows = computed(() => {
  const current = currentStore();
  const values = [
    ...(current?.attributes || detail.value?.attributes || []),
  ] as any[];
  return values
    .map((item) => ({
      id: item.id,
      name:
        definitions.value.find((def) => Number(def.id) === Number(item.id))
          ?.name || attributeNameZh(item._name, item.id),
      value: (item.values || [])
        .map(
          (value: any) =>
            value.value || value.name || value.dictionary_value_id,
        )
        .join("、"),
      required: Boolean(
        definitions.value.find((def) => Number(def.id) === Number(item.id))
          ?.is_required,
      ),
    }))
    .filter((row) => row.value);
});
const attributeNameMap: Record<string, string> = {
  тип: "类型",
  название: "名称",
  аннотация: "商品简介",
  "вес товара, г": "商品重量（克）",
  "состав комплекта": "套装组成",
  "вид крепления штор": "窗帘安装方式",
  "высота, см": "高度（厘米）",
  "ширина, см": "宽度（厘米）",
  "количество в упаковке, шт": "包装数量（件）",
  "единиц в одном товаре": "单个商品单位数",
  "целевая аудитория": "目标受众",
  "цвет товара": "商品颜色",
  "#хештеги": "主题标签",
  "название модели (для объединения в одну карточку)":
    "型号名称（用于合并到同一商品卡）",
};
function attributeNameZh(name: unknown, id: unknown) {
  const raw = String(name || "").trim();
  return attributeNameMap[raw.toLowerCase()] || raw || `属性 ${id}`;
}
async function boot() {
  const [l, p, s] = await Promise.all([
    api.get<any>("/listings"),
    api.get<any>("/research-products?page_size=200"),
    api.get<any>("/stores"),
  ]);
  items.value = l.items;
  products.value = p.items;
  stores.value = s.items.filter(
    (x: Store) => x.platform === "ozon" && x.enabled,
  );
}
async function create() {
  await api.post("/listings", form);
  open.value = false;
  ElMessage.success("上架草稿已创建");
  boot();
}
function currentStore(): StoreListing | undefined {
  return detail.value?.stores.find((x) => x.store_id === selectedStoreId.value);
}
function loadStoreEditor() {
  const d = detail.value!,
    s = currentStore();
  const content = s?.content || {};
  const pricing = s?.pricing || d.pricing;
  edit.title = String(content.title || d.title || "");
  edit.description_ru = String(
    content.description_ru || d.description_ru || "",
  );
  edit.price = Number(pricing.price || 0);
  edit.old_price = Number(pricing.old_price || 0);
  edit.attributesText = JSON.stringify(
    s?.attributes?.length ? s.attributes : d.attributes,
    null,
    2,
  );
  definitions.value = [];
  Object.keys(attributeInput).forEach((k) => delete attributeInput[Number(k)]);
  Object.keys(attributeOptions).forEach(
    (k) => delete attributeOptions[Number(k)],
  );
}
async function show(row: Listing) {
  detail.value = await api.get(`/listings/${row.id}`);
  const d = detail.value!;
  selectedStoreId.value = d.stores[0]?.store_id || "";
  edit.depth_mm = Number(d.package.depth_mm || 300);
  edit.width_mm = Number(d.package.width_mm || 200);
  edit.height_mm = Number(d.package.height_mm || 100);
  edit.weight_g = Number(d.package.weight_g || 0);
  loadStoreEditor();
  drawer.value = true;
}
async function save() {
  if (definitions.value.length) applyAttributes();
  let attributes;
  try {
    attributes = JSON.parse(edit.attributesText);
  } catch {
    return ElMessage.error("属性 JSON 格式不正确");
  }
  detail.value = await api.patch(`/listings/${detail.value!.id}`, {
    store_id: selectedStoreId.value,
    title: edit.title,
    description_ru: edit.description_ru,
    attributes,
    pricing: { price: edit.price, old_price: edit.old_price, currency: "CNY" },
    package: {
      depth_mm: edit.depth_mm || null,
      width_mm: edit.width_mm || null,
      height_mm: edit.height_mm || null,
      weight_g: edit.weight_g || null,
      dimensions_source: "manual",
    },
  });
  loadStoreEditor();
  ElMessage.success(
    `已保存 ${currentStore()?.store_name || "店铺"} 的独立上架资料`,
  );
  boot();
}
async function loadAttributes() {
  const d = detail.value!,
    p = d.research_product,
    store = currentStore();
  if (!p?.description_category_id || !p.type_id || !store)
    return ElMessage.warning("请先确认实时类目并选择 OZON 店铺");
  attributesLoading.value = true;
  try {
    definitions.value = (
      await api.get<any>(
        `/stores/${store.store_id}/category-attributes?description_category_id=${p.description_category_id}&type_id=${p.type_id}&language=ZH_HANS`,
      )
    ).items;
    const attrs = store.attributes?.length ? store.attributes : d.attributes;
    for (const a of attrs) {
      const v = (a as any).values?.[0];
      attributeInput[Number((a as any).id)] =
        v?.dictionary_value_id || v?.value || "";
    }
  } finally {
    attributesLoading.value = false;
  }
}
async function loadValues(def: any) {
  const p = detail.value!.research_product!,
    store = currentStore()!;
  if (attributeOptions[def.id]) return;
  attributeOptions[def.id] = (
    await api.get<any>(
      `/stores/${store.store_id}/category-attribute-values?description_category_id=${p.description_category_id}&type_id=${p.type_id}&attribute_id=${def.id}&language=ZH_HANS`,
    )
  ).items;
}
function applyAttributes() {
  const values = definitions.value.flatMap((def) => {
    const entered = attributeInput[def.id];
    if (entered === undefined || entered === null || entered === "") return [];
    const option = (attributeOptions[def.id] || []).find(
      (x) => Number(x.id) === Number(entered),
    );
    return [
      {
        id: Number(def.id),
        complex_id: Number(def.complex_id || 0),
        values: [
          option
            ? {
                dictionary_value_id: Number(option.id),
                value: String(option.value || option.name || ""),
              }
            : { value: String(entered) },
        ],
      },
    ];
  });
  edit.attributesText = JSON.stringify(values, null, 2);
  ElMessage.success("实时属性已写入上架资料，请点击保存资料");
}
async function prepareAi() {
  await api.post(`/listings/${detail.value!.id}/ai-prepare`);
  ElMessage.success("AI 文案、属性、主图及核验任务已开始");
}
async function price() {
  detail.value = await api.post(`/listings/${detail.value!.id}/price`);
  ElMessage.success("已按旧程序完整利润规则计算价格");
}
async function run(dry: boolean) {
  if (!dry)
    await ElMessageBox.confirm(
      "这会向所选 OZON 店铺执行真实上架、库存和促销操作。确认继续？",
      "真实发布确认",
      { type: "warning", confirmButtonText: "确认真实发布" },
    );
  await api.post(`/listings/${detail.value!.id}/run`, {
    dry_run: dry,
    store_ids: [],
    start_at: "validate",
  });
  ElMessage.success(dry ? "检查任务已排队" : "真实发布任务已排队");
}
onMounted(boot);
</script>
<template>
  <div class="panel listing-workspace">
    <div class="panel-head">
      <div>
        <h2>OZON 上架</h2>
        <p>一家店铺失败不会回滚其他店铺，可从失败步骤重试</p>
      </div>
      <el-button type="primary" @click="open = true">创建上架草稿</el-button>
    </div>
    <div class="listing-master-detail">
      <aside class="listing-master">
        <el-input
          v-model="keyword"
          clearable
          placeholder="搜索商品"
          class="listing-search"
        />
        <div
          v-for="row in filteredItems"
          :key="row.id"
          class="listing-master-item"
          :class="{ active: detail?.id === row.id }"
          @click="show(row)"
        >
          <img :src="sourceImage(row)" alt="商品图" />
          <div>
            <strong>{{ row.title }}</strong
            ><small
              >{{ row.research_product?.source_sku || row.base_offer_id }} ·
              {{ statusLabel(row.status) }}</small
            ><small>{{
              row.stores.map((store) => store.store_name).join("、")
            }}</small>
          </div>
          <span>›</span>
        </div>
        <el-empty v-if="!filteredItems.length" description="没有匹配商品" />
      </aside>
      <main class="listing-detail-pane">
        <el-empty v-if="!detail" description="请从左侧选择一个商品查看详情" />
        <el-dialog v-model="open" title="创建上架草稿" width="560"
          ><el-form label-width="110"
            ><el-form-item label="选品商品"
              ><el-select
                v-model="form.research_product_id"
                filterable
                style="width: 100%"
                ><el-option
                  v-for="p in products"
                  :key="p.id"
                  :label="`${p.source_sku} · ${p.title}`"
                  :value="p.id" /></el-select></el-form-item
            ><el-form-item label="基础 Offer ID"
              ><el-input v-model="form.base_offer_id" /></el-form-item
            ><el-form-item label="目标店铺"
              ><el-select v-model="form.store_ids" multiple style="width: 100%"
                ><el-option
                  v-for="s in stores"
                  :key="s.id"
                  :label="s.name"
                  :value="s.id" /></el-select></el-form-item
            ><el-form-item label="产品库主店"
              ><el-select
                v-model="form.catalog_primary_store_id"
                style="width: 100%"
                placeholder="选择一家主店铺"
                ><el-option
                  v-for="s in stores.filter((item) =>
                    form.store_ids.includes(item.id),
                  )"
                  :key="s.id"
                  :label="s.name"
                  :value="s.id" /></el-select
              ><small class="field-help"
                >各店会生成不同风格主图；只将这家店的主图与 OZON
                附图作为跟卖产品库资产。</small
              ></el-form-item
            ></el-form
          ><template #footer
            ><el-button @click="open = false">取消</el-button
            ><el-button type="primary" @click="create"
              >创建</el-button
            ></template
          ></el-dialog
        >
        <template v-if="detail"
          ><el-alert
            title="文案、实时属性和价格按店铺独立保存；包装尺寸和重量属于商品公共事实。"
            type="info"
            :closable="false" /><el-form label-width="100" class="listing-form"
            ><el-form-item label="基础 Offer"
              ><el-input
                :model-value="detail.base_offer_id"
                disabled /></el-form-item
            ><el-form-item label="编辑店铺"
              ><el-select
                v-model="selectedStoreId"
                style="width: 100%"
                @change="loadStoreEditor"
                ><el-option
                  v-for="store in detail.stores"
                  :key="store.store_id"
                  :label="`${store.store_name} · ${store.offer_id}`"
                  :value="store.store_id" /></el-select></el-form-item
            ><el-form-item label="俄文标题"
              ><el-input
                v-model="edit.title"
                maxlength="500"
                show-word-limit /></el-form-item
            ><el-form-item label="俄文描述"
              ><el-input
                v-model="edit.description_ru"
                type="textarea"
                :rows="5" /></el-form-item
            ><el-form-item label="OZON 售价"
              ><el-input-number
                v-model="edit.price"
                :min="0"
                :precision="2" /><span class="unit-gap">原价</span
              ><el-input-number
                v-model="edit.old_price"
                :min="0"
                :precision="2" /></el-form-item
            ><el-form-item label="包装尺寸"
              ><el-input-number v-model="edit.depth_mm" :min="1" /><span
                >长 ×</span
              ><el-input-number v-model="edit.width_mm" :min="1" /><span
                >宽 ×</span
              ><el-input-number v-model="edit.height_mm" :min="1" /><span
                >高 mm</span
              ><small class="field-help">默认 300×200×100 用于运费估算和提交 OZON，但不会作为事实提供给 AI；人工保存后才视为真实尺寸。</small
              ></el-form-item
            ><el-form-item label="包装重量"
              ><el-input-number v-model="edit.weight_g" :min="1" /><span
                >g</span
              ></el-form-item
            ></el-form
          >
          <section class="attribute-editor">
            <div class="panel-head">
              <div>
                <h3>{{ currentStore()?.store_name }} · OZON 实时类目属性</h3>
                <p>
                  {{
                    detail.research_product?.category_display || "尚未确认类目"
                  }}
                </p>
              </div>
              <el-button :loading="attributesLoading" @click="loadAttributes"
                >读取实时属性</el-button
              >
            </div>
            <el-table
              v-if="attributeRows.length"
              :data="attributeRows"
              class="attribute-summary"
              stripe
              ><el-table-column
                prop="name"
                label="属性"
                min-width="220" /><el-table-column
                prop="value"
                label="当前值"
                min-width="320"
                show-overflow-tooltip /><el-table-column label="要求" width="90"
                ><template #default="scope"
                  ><el-tag v-if="scope.row.required" type="danger" size="small"
                    >必填</el-tag
                  ><span v-else>选填</span></template
                ></el-table-column
              ><el-table-column prop="id" label="属性 ID" width="100"
            /></el-table>
            <el-empty
              v-else-if="!definitions.length"
              description="暂无已填写属性；点击读取实时属性查看类目字段"
            />
            <el-form v-if="definitions.length" label-width="180"
              ><el-form-item
                v-for="def in definitions"
                :key="def.id"
                :required="def.is_required"
                :label="attributeNameZh(def.name, def.id)"
                ><el-select
                  v-if="def.dictionary_id"
                  v-model="attributeInput[def.id]"
                  filterable
                  clearable
                  style="width: 100%"
                  placeholder="读取并选择 OZON 允许值"
                  @visible-change="(v: boolean) => v && loadValues(def)"
                  ><el-option
                    v-for="option in attributeOptions[def.id] || []"
                    :key="option.id"
                    :label="option.value || option.name"
                    :value="option.id" /></el-select
                ><el-input
                  v-else
                  v-model="attributeInput[def.id]"
                  :placeholder="
                    def.description || '填写属性值'
                  " /></el-form-item
              ><el-button @click="applyAttributes"
                >应用到当前店铺资料</el-button
              ></el-form
            ><el-collapse
              ><el-collapse-item title="高级：查看或修改当前店铺属性 JSON"
                ><el-input
                  v-model="edit.attributesText"
                  type="textarea"
                  :rows="8" /></el-collapse-item
            ></el-collapse>
          </section>
          <div class="drawer-actions">
            <el-button type="primary" @click="save">保存当前店铺资料</el-button
            ><el-button @click="prepareAi">生成各店文案和主图</el-button
            ><el-button @click="price">按利润规则定价</el-button
            ><el-button @click="run(true)">模拟检查</el-button>
          </div>
          <h3>各店铺独立状态</h3>
          <el-table :data="detail.stores"
            ><el-table-column prop="store_name" label="店铺" /><el-table-column
              prop="offer_id"
              label="Offer ID" /><el-table-column
              prop="status"
              label="上架" /><el-table-column
              prop="stock_status"
              label="库存" /><el-table-column
              prop="promotion_status"
              label="促销" /></el-table
        ></template>
      </main>
    </div>
  </div>
</template>
