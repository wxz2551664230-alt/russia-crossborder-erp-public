<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'

const loaded = ref(false)
const route = useRoute()
const composerUrl = computed(()=>'/api/v1/pod/original-composer'+(route.query.market==='US'?'?market=US&product='+encodeURIComponent(String(route.query.product||'')):''))
</script>

<template>
  <div class="original-composer-shell">
    <div v-if="!loaded" class="composer-loading">
      <span class="loading-dot" />
      正在连接原版图片合成工作台…
    </div>
    <iframe
      class="original-composer-frame"
      :key="composerUrl"
      :src="composerUrl"
      title="POD 图片合成工作台"
      @load="loaded = true"
    />
  </div>
</template>

<style scoped>
.original-composer-shell {
  position: relative;
  height: calc(100vh - 82px);
  min-height: 680px;
  margin: -24px calc(-1 * clamp(18px, 3vw, 40px)) -48px;
  overflow: hidden;
  background: #f3f6f5;
}
.original-composer-frame {
  display: block;
  width: 100%;
  height: 100%;
  border: 0;
  background: #f3f6f5;
}
.composer-loading {
  position: absolute;
  inset: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: #637269;
  background: #f3f6f5;
  font-size: 14px;
}
.loading-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: #2cc7a6;
  box-shadow: 0 0 0 5px rgb(44 199 166 / 14%);
}
@media (max-width: 760px) {
  .original-composer-shell {
    height: calc(100vh - 74px);
    margin-right: -14px;
    margin-left: -14px;
  }
}
</style>
