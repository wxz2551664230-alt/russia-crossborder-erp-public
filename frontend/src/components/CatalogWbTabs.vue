<template>
  <nav class="wb-stage-tabs" aria-label="WB 上架阶段">
    <RouterLink to="/catalog/wb/publish">产品上架</RouterLink>
    <RouterLink to="/catalog/wb/image-review">生图核验</RouterLink>
    <RouterLink to="/catalog/wb/pending-submit">待提交</RouterLink>
    <RouterLink to="/catalog/wb/processing">建品处理中</RouterLink>
    <RouterLink to="/catalog/wb/pending-stock">待添加库存</RouterLink>
    <RouterLink to="/catalog/wb/pending-price">待设置价格与折扣</RouterLink>
    <RouterLink to="/catalog/wb/completed">已完成</RouterLink>
    <RouterLink to="/catalog/wb/exceptions">异常处理</RouterLink>
  </nav>
</template>

<style scoped>
.wb-stage-tabs{display:flex;gap:8px;padding:8px;background:#f3f6fb;border-radius:12px;overflow-x:auto}.wb-stage-tabs a{color:#526175;text-decoration:none;padding:10px 20px;border-radius:9px;white-space:nowrap;font-weight:600}.wb-stage-tabs a.router-link-active{color:#fff;background:#7b2cbf;box-shadow:0 4px 12px rgba(123,44,191,.22)}
</style>
