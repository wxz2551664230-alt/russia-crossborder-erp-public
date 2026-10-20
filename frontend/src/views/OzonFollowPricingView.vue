<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api";
import type { Store } from "../types";

const loading = ref(false);
const modules = ref<any[]>([]);
const stores = ref<Store[]>([]);
const globalForm = reactive<any>({
  pricing_category_module: "",
  pricing_product_category: "",
  pricing_sticker_fee: 2.5,
  pricing_operating_cost: 3,
  pricing_return_rate: 10,
  pricing_logistics_service_rate: 2,
});
const storeForms = reactive<Record<string, any>>({});
const categories = computed(
  () =>
    modules.value.find((row) => row.name === globalForm.pricing_category_module)
      ?.categories || [],
);
const selectedCategory = computed(() =>
  categories.value.find(
    (row: any) => row.name === globalForm.pricing_product_category,
  ),
);

async function load() {
  loading.value = true;
  try {
    const [settings, options, storeResult] = await Promise.all([
      api.get<any>("/settings"),
      api.get<any>("/settings/ozon-follow-pricing-options"),
      api.get<any>("/stores"),
    ]);
    Object.assign(globalForm, settings.profiles?.pricing || {});
    modules.value = options.modules;
    stores.value = storeResult.items.filter(
      (store: Store) => store.platform === "ozon",
    );
    for (const store of stores.value) {
      const value = (store.config?.ozon_follow_pricing || {}) as any;
      storeForms[store.id] = {
        enabled: store.enabled,
        offer_prefix: store.offer_prefix || "",
        target_cost_profit_margin: value.target_cost_profit_margin ?? 30,
        pricing_advertising_rate: value.pricing_advertising_rate ?? 0,
        pricing_price_multiplier: value.pricing_price_multiplier ?? 3,
        pricing_old_price_multiplier: value.pricing_old_price_multiplier ?? 3.7,
      };
    }
  } finally {
    loading.value = false;
  }
}
function moduleChanged() {
  globalForm.pricing_product_category = "";
}
async function save() {
  if (
    !globalForm.pricing_category_module ||
    !globalForm.pricing_product_category
  )
    return ElMessage.warning("请选择佣金类目模块和商品类目");
  for (const store of stores.value) {
    const row = storeForms[store.id];
    if (row.pricing_old_price_multiplier <= row.pricing_price_multiplier)
      return ElMessage.warning(`${store.name} 的折前价倍数必须大于售价倍数`);
  }
  loading.value = true;
  try {
    await api.put("/settings/pricing", { value: { ...globalForm } });
    await Promise.all(
      stores.value.map((store) =>
        {
          const { offer_prefix, ...pricing } = storeForms[store.id];
          return api.patch(`/stores/${store.id}`, {
            enabled: Boolean(pricing.enabled),
            offer_prefix: String(offer_prefix || "").trim(),
            config: {
              ...store.config,
              ozon_follow_pricing: Object.fromEntries(
                Object.entries(pricing).filter(([key]) => key !== "enabled"),
              ),
            },
          });
        },
      ),
    );
    ElMessage.success("OZON 跟卖定价配置已保存，下一次计算价格时生效");
    await load();
  } finally {
    loading.value = false;
  }
}
onMounted(load);
</script>

<template>
  <div v-loading="loading" class="page-stack">
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>佣金与公共成本</h2>
          <p>用于反推实际成交价，金额单位均为 CNY</p>
        </div>
        <el-button type="primary" @click="save">保存配置</el-button>
      </div>
      <el-form label-position="top"
        ><div class="form-grid">
          <el-form-item label="佣金类目模块"
            ><el-select
              v-model="globalForm.pricing_category_module"
              filterable
              @change="moduleChanged"
              ><el-option
                v-for="row in modules"
                :key="row.name"
                :label="row.name"
                :value="row.name" /></el-select
          ></el-form-item>
          <el-form-item label="计算佣金的商品类目"
            ><el-select v-model="globalForm.pricing_product_category" filterable
              ><el-option
                v-for="row in categories"
                :key="row.name"
                :label="row.name"
                :value="row.name" /></el-select
          ></el-form-item>
          <el-form-item label="贴单费（CNY）"
            ><el-input-number
              v-model="globalForm.pricing_sticker_fee"
              :min="0"
              :precision="2"
          /></el-form-item>
          <el-form-item label="固定广告及运营费（CNY）"
            ><el-input-number
              v-model="globalForm.pricing_operating_cost"
              :min="0"
              :precision="2"
          /></el-form-item>
          <el-form-item label="退货率（%）"
            ><el-input-number
              v-model="globalForm.pricing_return_rate"
              :min="0"
              :max="100"
              :precision="2"
          /></el-form-item>
          <el-form-item label="物流服务费率（%）"
            ><el-input-number
              v-model="globalForm.pricing_logistics_service_rate"
              :min="0"
              :max="100"
              :precision="2"
          /></el-form-item></div
      ></el-form>
      <el-alert
        v-if="selectedCategory"
        type="info"
        :closable="false"
        :title="`佣金率：成交价 ≤135 为 ${selectedCategory.rates[0]}%，135–450 为 ${selectedCategory.rates[1]}%，>450 为 ${selectedCategory.rates[2]}%`"
      />
    </section>
    <section class="panel">
      <div class="panel-head">
        <div>
          <h2>分店铺跟卖策略</h2>
          <p>配置 Offer ID 前缀并按店铺独立反推成交价；前缀只应用于之后新建的跟卖草稿</p>
        </div>
      </div>
      <el-table :data="stores"
        ><el-table-column prop="name" label="店铺" min-width="140" />
        <el-table-column label="参与跟卖" width="110"
          ><template #default="s"
            ><el-switch v-model="storeForms[s.row.id].enabled" /></template
        ></el-table-column>
        <el-table-column label="Offer ID 前缀" min-width="180"
          ><template #default="s"
            ><el-input
              v-model="storeForms[s.row.id].offer_prefix"
              maxlength="60"
              clearable
              placeholder="例如 ZYH-" /></template
        ></el-table-column>
        <el-table-column label="目标成本利润率（%）" min-width="180"
          ><template #default="s"
            ><el-input-number
              v-model="storeForms[s.row.id].target_cost_profit_margin"
              :min="0"
              :max="10000"
              :precision="2" /></template
        ></el-table-column>
        <el-table-column label="广告份额（%）" min-width="160"
          ><template #default="s"
            ><el-input-number
              v-model="storeForms[s.row.id].pricing_advertising_rate"
              :min="0"
              :max="100"
              :precision="2" /></template
        ></el-table-column>
        <el-table-column label="售价倍数" min-width="140"
          ><template #default="s"
            ><el-input-number
              v-model="storeForms[s.row.id].pricing_price_multiplier"
              :min="0.01"
              :precision="2" /></template
        ></el-table-column>
        <el-table-column label="折前价倍数" min-width="140"
          ><template #default="s"
            ><el-input-number
              v-model="storeForms[s.row.id].pricing_old_price_multiplier"
              :min="0.01"
              :precision="2" /></template
        ></el-table-column>
      </el-table>
    </section>
  </div>
</template>
