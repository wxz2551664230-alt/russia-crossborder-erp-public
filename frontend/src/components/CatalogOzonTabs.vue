<template>
  <nav class="ozon-stage-tabs" aria-label="OZON 上架阶段">
    <RouterLink to="/catalog/ozon/publish">产品上架</RouterLink>
    <RouterLink to="/catalog/ozon/image-review">生图核验</RouterLink>
    <RouterLink to="/catalog/ozon/pending-submit">待提交</RouterLink>
    <RouterLink to="/catalog/ozon/pending-stock">待添加库存</RouterLink>
    <RouterLink to="/catalog/ozon/pending-promotion">待添加促销</RouterLink>
    <RouterLink to="/catalog/ozon/completed">已完成</RouterLink>
  </nav>
</template>

<style scoped>
.ozon-stage-tabs{display:flex;gap:8px;padding:8px;background:#f3f6fb;border-radius:12px;overflow-x:auto}.ozon-stage-tabs a{color:#526175;text-decoration:none;padding:10px 20px;border-radius:9px;white-space:nowrap;font-weight:600}.ozon-stage-tabs a.router-link-active{color:#fff;background:#1d64f2;box-shadow:0 4px 12px rgba(29,100,242,.22)}
</style>
