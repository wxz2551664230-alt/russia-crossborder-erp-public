<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api";
import type { Listing, StoreListing } from "../types";
type Row = { listing: Listing; store: StoreListing };
const listings = ref<Listing[]>([]),
  loading = ref(false),
  selected = ref<Row>(),
  processing = ref(false),
  timer = ref<number>(),
  imageRetries = ref<Record<string, number>>({});
let loadSequence = 0;
const imageRetryTimers = new Set<number>();
const rows = computed<Row[]>(() =>
  listings.value.flatMap((listing) =>
    listing.stores
      .filter((store) => store.store_enabled && store.status === "image_review")
      .map((store) => ({ listing, store })),
  ),
);
const sourceImage = computed(() =>
  selected.value
    ? retryableImageUrl(`/api/v1/listings/${selected.value.listing.id}/source-image`, `source-detail:${selected.value.listing.id}`)
    : "",
);
const aiImage = computed(() =>
  selected.value
    ? retryableImageUrl(`/api/v1/listings/${selected.value.listing.id}/stores/${selected.value.store.store_id}/image-preview`, `ai:${selected.value.store.id}`)
    : "",
);
function retryableImageUrl(url: string, key: string) {
  const attempt = imageRetries.value[key] || 0;
  return attempt ? `${url}?retry=${attempt}` : url;
}
function retryImage(key: string) {
  const attempt = imageRetries.value[key] || 0;
  if (attempt >= 3) return;
  const delay = 600 * (attempt + 1);
  const timerId = window.setTimeout(() => {
    imageRetryTimers.delete(timerId);
    imageRetries.value = { ...imageRetries.value, [key]: attempt + 1 };
  }, delay);
  imageRetryTimers.add(timerId);
}
async function load() {
  const sequence = ++loadSequence;
  loading.value = true;
  try {
    const result = await api.get<any>("/listings?page_size=200");
    if (sequence !== loadSequence) return;
    listings.value = result.items;
    if (selected.value && !rows.value.some((row) => row.store.id === selected.value?.store.id))
      selected.value = rows.value[0];
    if (!selected.value && rows.value[0]) selected.value = rows.value[0];
  } finally {
    if (sequence === loadSequence) loading.value = false;
  }
}
function choose(row: Row) {
  selected.value = row;
}
async function action(name: "approve" | "regenerate") {
  if (!selected.value || processing.value) return;
  const target = selected.value;
  if (name === "approve")
    await ElMessageBox.confirm(
      "确认 AI 主图与原 OZON 商品一致？上传 OSS 完成后才会进入待提交。",
      "批准主图",
      { type: "warning" },
    );
  processing.value = true;
  try {
    await api.post(
      `/listings/${target.listing.id}/stores/${target.store.store_id}/image/${name}`,
    );
    target.store.status = name === "approve" ? "image_approval_queued" : "image_regeneration_queued";
    selected.value = rows.value[0];
    ElMessage.success(name === "approve" ? "批准任务已进入队列，正在上传 OSS" : "重新生图任务已进入队列");
    await load();
  } finally { processing.value = false; }
}
async function rejectCurrentStore() {
  if (!selected.value || processing.value) return;
  const target = selected.value;
  await ElMessageBox.confirm(
    `确认不通过并移除 ${target.store.store_name} 的这个店铺版本？其他店铺版本不会受影响。`,
    "移除当前店铺版本",
    { type: "warning", confirmButtonText: "确认移除" },
  );
  processing.value = true;
  try {
    await api.delete(`/listings/${target.listing.id}/stores/${target.store.store_id}/image-review`);
    target.store.status = "rejected_removed";
    selected.value = rows.value[0];
    ElMessage.success(`已移除 ${target.store.store_name} 的店铺版本，其他店铺不受影响`);
    await load();
  } finally { processing.value = false; }
}
onMounted(() => { load(); timer.value = window.setInterval(load, 3000); });
onUnmounted(() => {
  clearInterval(timer.value);
  imageRetryTimers.forEach((timerId) => clearTimeout(timerId));
  imageRetryTimers.clear();
});
</script>
<template>
  <section class="panel review-workspace">
    <div class="panel-head">
      <div>
          <h2>人工生图核验 · 待核验 {{ rows.length }} 个</h2>
          <p>这里只显示尚未核验的店铺版本；确认后会立即移出列表并进入待提交</p>
      </div>
      <el-button @click="load">刷新</el-button>
    </div>
    <div class="review-split" v-loading="loading">
      <aside class="review-list">
        <div
          v-for="row in rows"
          :key="row.store.id"
          class="review-list-item"
          :class="{ active: selected?.store.id === row.store.id }"
          @click="choose(row)"
        >
          <img
            :src="retryableImageUrl(`/api/v1/listings/${row.listing.id}/source-image`, `source-list:${row.listing.id}`)"
            alt="原图"
            @error="retryImage(`source-list:${row.listing.id}`)"
          />
          <div>
            <strong>{{ row.listing.title }}</strong
            ><small>{{ row.store.store_name }} · {{ row.store.offer_id }}</small
            ><small v-if="row.store.last_error" class="review-error">上次处理失败：{{ row.store.last_error }}</small
            ><small v-else>等待人工核验</small>
          </div>
          <span>›</span>
        </div>
        <el-empty
          v-if="!rows.length && !loading"
          description="当前没有待核验主图"
        />
      </aside>
      <main v-if="selected" class="review-detail">
        <div class="review-detail-head">
          <div class="review-title-block">
            <el-tag>{{ selected.store.store_name }}</el-tag>
            <h3>{{ selected.listing.title }}</h3>
            <p>{{ selected.store.offer_id }}</p>
          </div>
          <div class="review-actions review-toolbar">
            <el-button type="danger" plain :loading="processing" @click="rejectCurrentStore">不通过，移除此店铺版本</el-button
            ><el-button :loading="processing" @click="action('regenerate')">重新生成</el-button
            ><el-button type="primary" :loading="processing" @click="action('approve')"
              >确认一致并批准</el-button
            >
          </div>
        </div>
        <div class="compare-grid">
          <figure>
            <figcaption>原 OZON 商品主图</figcaption>
            <img :src="sourceImage" alt="原 OZON 商品主图" @error="retryImage(`source-detail:${selected.listing.id}`)" /><small
              >仅用于人工比对，来源商品原图</small
            >
          </figure>
          <figure>
            <figcaption>AI 生成主图</figcaption>
            <img :src="aiImage" alt="AI 生成主图" @error="retryImage(`ai:${selected.store.id}`)" /><small
              >检查商品轮廓、颜色、数量、配件和俄文文字</small
            >
          </figure>
        </div>
      </main>
      <el-empty v-else description="请从左侧选择待核验商品" />
    </div>
  </section>
</template>

<style scoped>
.review-detail-head {
  flex-direction: column;
  gap: 14px;
}
.review-toolbar {
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: flex-start;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 0;
}
.review-toolbar :deep(.el-button + .el-button) {
  margin-left: 0;
}
@media (max-width: 720px) {
  .review-toolbar :deep(.el-button) {
    flex: 1 1 180px;
  }
}
</style>
