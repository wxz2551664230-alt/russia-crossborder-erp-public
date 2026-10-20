// ERP bridge only: the archived composer remains byte-for-byte unchanged.
const ERP_COMPOSER_BATCH_KEY = "erp.pod.composerBatch.v1";
const erpComposerParams = new URLSearchParams(window.location.search);
const erpUsComposer = erpComposerParams.get('market') === 'US';

// Adapt the original domestic labels only in the verified US workspace.
if (erpUsComposer) {
  const originalProductPage = renderProductPage;
  renderProductPage = function() {
    originalProductPage();
    for (const meta of elements.productList.querySelectorAll('small')) {
      meta.textContent = meta.textContent.replace('国内发货', '美国生产发货');
    }
  };
  const originalSelectedProduct = renderSelectedProduct;
  renderSelectedProduct = function() {
    originalSelectedProduct();
    for (const meta of elements.selectedProduct.querySelectorAll('small')) {
      meta.textContent = meta.textContent.replace('国内发货', '美国生产发货');
    }
  };
  const originalSalesSizes = renderSalesSizes;
  renderSalesSizes = function() {
    originalSalesSizes();
    for (const meta of elements.salesSizeOptions.querySelectorAll('small')) {
      const currency = state.detail?.prices?.currency || state.detail?.currency || '币种待核实';
      meta.textContent = meta.textContent.replace(/参考价 \$([^\s]+)/g, `参考价 $1（${currency}）`);
    }
  };
}

const erpOriginalRequest = request;
request = async function(path, options = {}) {
  if (erpUsComposer) {
    if (path === '/api/v1/pod/catalog') path = '/api/v1/pod/us-composer/catalog';
    if (path.startsWith('/api/v1/pod/product-types/') && !path.endsWith('/previews')) path = path.replace('/product-types/', '/us-composer/products/');
    if (path === '/api/v1/pod/product-types/previews') return {};
    if (options.body instanceof FormData && ['/api/v1/pod/legacy-api/jobs','/api/v1/pod/legacy-api/jobs/multi'].includes(path)) options.body.set('market','US');
  }
  const result = await erpOriginalRequest(path, options);
  if (path === '/api/v1/pod/artworks' && options.method === 'POST') {
    if (!result.job_id) return result.artwork ? result : { artwork: result };
    showNotice('图片已登记，正在等待所选电脑处理并保存云端；请保持执行端开启');
    for (let attempt=0;attempt<60;attempt++) {
      await new Promise(resolve=>setTimeout(resolve,2000));
      const progress=await erpOriginalRequest('/api/v1/pod/artworks/upload-result/'+result.job_id);
      if (progress.status==='completed') return {artwork:progress.artwork};
      if (['failed','cancelled','paused'].includes(progress.status)) throw new Error(progress.message||'上传任务已停止，请在任务中心查看');
    }
    throw new Error('上传任务仍在原电脑队列中，请勿重复上传；完成后可从设计图库选择');
  }
  return result;
};

async function importErpComposerBatch() {
  let payload = null;
  try {
    payload = JSON.parse(window.sessionStorage.getItem(ERP_COMPOSER_BATCH_KEY) || "null");
  } catch {
    window.sessionStorage.removeItem(ERP_COMPOSER_BATCH_KEY);
  }
  const items = Array.isArray(payload?.items)
    ? payload.items.filter((item) => item?.id && item?.thumbnail_url)
    : [];
  if (!items.length) return;
  const batchMarket = payload.market || payload.product?.market || 'CN';
  if (batchMarket !== (erpUsComposer ? 'US' : 'CN')) {
    showNotice('设计图所选货盘与当前合成页不一致，请返回设计图库重新进入对应货盘的合成页', true);
    return;
  }
  if (payload?.product?.key) {
    if (!state.products?.length) await loadCatalog();
    const product = state.products.find((candidate) => productId(candidate) === payload.product.key);
    if (!product) {
      showNotice(`未找到所选产品 ${payload.product.name || payload.product.key}，请在货盘中重新选择`, true);
      return;
    }
    if (!state.selectedProduct || productId(state.selectedProduct) !== payload.product.key) await selectProduct(product);
    if (!state.detail) {
      showNotice('所选产品的规格读取失败，请返回设计图库读取产品详情后重新进入', true);
      return;
    }
  }
  state.selectedArtworkItems = new Map(items.map((item) => [item.id, item]));
  state.batchArtworkItems = items;
  state.artworkBatchMode = false;
  await useArtworkLibraryItem(items[0], { preserveBatch: true });
  // The archived loader reports errors in state instead of rejecting its promise.
  if (state.artworkReferenceId !== items[0].id || state.artworkStatusMessage) {
    state.batchArtworkItems = [];
    showNotice(state.artworkStatusMessage || '设计图未能载入，请返回设计图库重新进入', true);
    return;
  }
  // Preserve the selection on failed reads, and consume only a successful import.
  window.sessionStorage.removeItem(ERP_COMPOSER_BATCH_KEY);
  showNotice(`已带入 ${items.length} 张设计图；确定印刷面和排版后，可批量加入队列`);
  updateReadiness();
}

// Share in-flight reads, including requests triggered by the polling timer.
const erpCatalogRead = loadCatalog;
const erpJobsRead = loadJobs;
let erpCatalogPending = null;
let erpJobsPending = null;
loadCatalog = function () {
  if (!erpCatalogPending) erpCatalogPending = erpCatalogRead().finally(() => { erpCatalogPending = null; });
  return erpCatalogPending;
};
loadJobs = function () {
  if (!erpJobsPending) erpJobsPending = erpJobsRead().finally(() => { erpJobsPending = null; });
  return erpJobsPending;
};

async function initializeErpComposer() {
  setWorkspaceView("composer");
  // Historical tasks and the hidden artwork library must not gate placement.
  void loadJobs();
  try {
    await Promise.all([loadStatus(), loadCatalog(), loadComposerCategories()]);
    if (erpUsComposer) {
      const product = state.products.find(p=>productId(p)===erpComposerParams.get('product'));
      if (product) await selectProduct(product);
      const option=document.createElement('option');option.value='';option.textContent='Amazon 美国 POD';
      elements.composerCategory.replaceChildren(option);elements.composerCategory.disabled=true;
      elements.syncCatalog.disabled=true;elements.syncCatalog.title='请到美国货盘页面手动同步';
      showNotice('美国生产货盘；合成完成后入库到 Amazon 美国 POD 分类');
    }
    await importErpComposerBatch();
  } catch (error) {
    showNotice(error instanceof Error ? error.message : "程序初始化失败", true);
  }
  renderSelectedProduct();
  updateWorkspace();
  state.pollTimer = window.setInterval(() => {
    if (document.hidden) return;
    void loadJobs();
    if (!elements.productLibraryDialog.hidden) void loadProductExports();
    if (!elements.wildberriesWorkspace.hidden) void loadWildberriesListings();
    if (!elements.ordersWorkspace.hidden) void loadOrders({ quiet: true });
  }, 1500);
}

async function persistErpComposerBatch(event) {
  if (!state.batchArtworkItems.length) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  saveActiveSide();
  const templateSide = state.sides[state.viewId]
    || Object.values(state.sides).find((side) => side?.file && side?.placement);
  if (elements.addToQueue.disabled || !templateSide?.placement) return;
  const batchItems = [...state.batchArtworkItems];
  state.adding = true;
  updateReadiness(`正在一次性登记 ${batchItems.length} 个后台合成任务`);
  try {
    const response = await fetch("/api/v1/pod/compositions/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        market: erpUsComposer ? 'US' : 'CN',
        supplier: productSupplier(state.selectedProduct),
        product_type_id: Number(sourceProductId(state.selectedProduct)),
        product_name: productName(state.selectedProduct),
        default_color_id: Number(state.colorId),
        category_id: elements.composerCategory.value,
        selected_size_ids: [...state.selectedSizeIds].map(Number),
        default_view_id: Number(templateSide.viewId),
        placements: [{
          view_id: Number(templateSide.viewId),
          view_name: templateSide.viewName,
          width: Math.max(1, Math.round(templateSide.placement.width)),
          height: Math.max(1, Math.round(templateSide.placement.height)),
          top_x: Math.round(templateSide.placement.x * 10) / 10,
          top_y: Math.round(templateSide.placement.y * 10) / 10,
          artwork_id: batchItems[0].id,
          rotation: Number(templateSide.rotation || 0)
        }],
        artwork_ids: batchItems.map((item) => item.id),
        prevent_duplicate: true
      })
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result?.error?.message || result?.detail || "批量任务登记失败");
    state.batchArtworkItems = [];
    state.selectedArtworkItems.clear();
    clearArtwork();
    await loadJobs();
    showNotice(`已一次性登记 ${result.accepted} 个任务；可以离开页面，后台会逐个合成`);
  } catch (error) {
    showNotice(error instanceof Error ? error.message : "批量任务登记失败", true);
  } finally {
    state.adding = false;
    updateReadiness();
  }
}

// Capture the click before the archived UI's old per-image request loop.
elements.addToQueue.addEventListener("click", (event) => void persistErpComposerBatch(event), true);

// Keep failed compositions visible even when a large batch has many newer successes.
const erpQueueFilterBar = document.createElement("div");
erpQueueFilterBar.style.cssText = "display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0";
const erpQueuePanel = elements.queueList.parentElement;
erpQueuePanel.style.gridTemplateRows = "auto auto auto minmax(0, 1fr)";
erpQueuePanel.insertBefore(erpQueueFilterBar, elements.queueList);
const erpQueueHeading = elements.clearFinished.parentElement;
erpQueueHeading.style.flexWrap = "wrap";
const erpQueueHeadingActions = document.createElement("div");
erpQueueHeadingActions.style.cssText = "display:flex;align-items:center;gap:6px;flex-wrap:wrap";
erpQueueHeading.insertBefore(erpQueueHeadingActions, elements.clearFinished);
erpQueueHeadingActions.append(elements.clearFinished);
const erpQueueFilterLabels = { all: "全部", failed: "失败", queued: "排队", active: "处理中", completed: "完成" };
let erpQueueFilter = "all";
let erpRetryingFailed = false;
const erpOriginalRenderJobs = renderJobs;
renderJobs = function renderErpJobs() {
  const allJobs = state.jobs;
  const allCounts = state.counts;
  const activeStatuses = new Set(["running", "uploading", "creating", "rendering", "downloading", "archiving"]);
  const matches = (job, filter) => filter === "all" || (filter === "active" ? activeStatuses.has(job.status) : job.status === filter);
  erpQueueFilterBar.replaceChildren();
  for (const [key, label] of Object.entries(erpQueueFilterLabels)) {
    const count = allJobs.filter((job) => matches(job, key)).length;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${label} ${count}`;
    button.style.cssText = `padding:6px 10px;border-radius:7px;cursor:pointer;border:1px solid ${erpQueueFilter === key ? "#178f78" : "#cbd5d0"};background:${erpQueueFilter === key ? "#e2f8ef" : "white"};color:#244339`;
    button.addEventListener("click", () => { erpQueueFilter = key; renderJobs(); });
    erpQueueFilterBar.append(button);
  }
  const retryFailed = document.createElement("button");
  erpQueueHeadingActions.querySelector(".erp-retry-failed")?.remove();
  retryFailed.className = "erp-retry-failed";
  retryFailed.type = "button";
  retryFailed.textContent = erpRetryingFailed ? "正在核对失败任务…" : "一键重试失败任务";
  retryFailed.disabled = erpRetryingFailed;
  retryFailed.style.cssText = "min-height:30px;padding:6px 9px;border:0;border-radius:7px;background:#b45309;color:#fff;cursor:pointer;font-size:9px;font-weight:700;white-space:nowrap";
  retryFailed.addEventListener("click", async () => {
    if (erpRetryingFailed) return;
    erpRetryingFailed = true;
    renderJobs();
    try {
      const previewResponse = await fetch("/api/v1/pod/compositions/retry-failed/preview");
      const preview = await previewResponse.json();
      if (!previewResponse.ok) throw new Error(preview?.message || preview?.error?.message || "失败任务核对失败");
      const desktop = preview.execution_mode === "original_pc";
      if (!preview.failed) { showNotice(desktop ? "本人当前电脑没有失败的合成任务" : "目前没有失败的合成任务"); return; }
      const reasons = Object.entries(preview.reasons || {}).map(([reason, count]) => `${reason}（${count} 个）`).join("；");
      if (desktop && !preview.eligible) { showNotice(`没有可直接重试的任务：${reasons}`, true); return; }
      const confirmation = desktop
        ? `将重试本人当前电脑的 ${preview.eligible} 个失败任务，失败记录中含 ${preview.hidden_history} 个此前已清除的记录。由原电脑检查原图并从已有进度继续，电脑离线时等待恢复。${preview.blocked ? `跳过 ${preview.blocked} 个：${reasons}。` : ""}继续吗？`
        : `将核对并重试 ${preview.failed} 个失败任务（含 ${preview.hidden_history} 个此前已清除的记录）。本地原图完整 ${preview.local_ready} 个；远端产品将逐个确认不存在后才重新建品。继续吗？`;
      const approved = window.confirm(confirmation);
      if (!approved) return;
      const response = await fetch("/api/v1/pod/compositions/retry-failed", { method: "POST" });
      const result = await response.json();
      if (!response.ok) throw new Error(result?.message || result?.error?.message || "批量重试登记失败");
      erpQueueFilter = "queued";
      await loadJobs();
      const skippedReasons = Object.entries(result.reasons || {}).map(([reason, count]) => `${reason}（${count} 个）`).join("；");
      showNotice(`已重新排队 ${result.queued} 个失败任务${result.execution_mode === "original_pc" ? "，由原电脑继续" : ""}${result.skipped ? `；跳过 ${result.skipped} 个：${skippedReasons || "请查看任务详情核对"}` : ""}`,
        Boolean(result.skipped));
    } catch (error) {
      showNotice(error instanceof Error ? error.message : "批量重试登记失败", true);
    } finally {
      erpRetryingFailed = false;
      renderJobs();
    }
  });
  erpQueueHeadingActions.insertBefore(retryFailed, elements.clearFinished);
  try {
    state.jobs = allJobs.filter((job) => matches(job, erpQueueFilter));
    state.counts = {
      ...allCounts, total: state.jobs.length,
      queued: state.jobs.filter((job) => job.status === "queued").length,
      active: state.jobs.filter((job) => activeStatuses.has(job.status)).length,
      completed: state.jobs.filter((job) => job.status === "completed").length,
      failed: state.jobs.filter((job) => job.status === "failed").length,
    };
    erpOriginalRenderJobs();
  } finally {
    state.jobs = allJobs;
    state.counts = allCounts;
  }
};
renderJobs();
