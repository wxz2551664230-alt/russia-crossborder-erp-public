// Run the shipped button handler with a small DOM, without supplier/network work.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("backend/erp/domains/pod/composer_batch_bridge.js", "utf8");
const controller = source.slice(source.indexOf("const erpQueueFilterLabels"));
assert(controller.includes('"/api/v1/pod/compositions/retry-failed"'));

async function scenario({ cancel = false, blocked = false, previewError = false, submitError = false } = {}) {
  const requests = [], notices = [], confirmations = [];
  let retryButton, loads = 0;
  const context = {
    state: { jobs: [], counts: {} }, elements: { clearFinished: {} }, renderJobs() {},
    erpQueueFilterBar: { replaceChildren() {}, append() {} },
    erpQueueHeadingActions: { querySelector: () => null, insertBefore(node) { retryButton = node; } },
    document: { createElement() { return { style: {}, addEventListener(event, handler) { this[event] = handler; } }; } },
    window: { confirm(message) { confirmations.push(message); return !cancel; } },
    showNotice(message, failed) { notices.push({ message, failed }); },
    async loadJobs() { loads++; },
    async fetch(path) {
      requests.push(path);
      const preview = path.endsWith("preview");
      const ok = !(preview ? previewError : submitError);
      const result = !ok ? { message: "precise ERP error" } : preview
        ? { execution_mode: "original_pc", failed: 1, eligible: blocked ? 0 : 1, hidden_history: 0,
            blocked: blocked ? 1 : 0, reasons: blocked ? { "supplier outcome needs verification": 1 } : {} }
        : { execution_mode: "original_pc", queued: 1, skipped: 0, reasons: {} };
      return { ok, json: async () => result };
    }
  };
  vm.createContext(context);
  vm.runInContext(controller, context);
  await retryButton.click();
  assert.equal(retryButton.disabled, false);
  return { requests, notices, confirmations, loads };
}

(async () => {
  let result = await scenario();
  assert.equal(result.requests.length, 2);
  assert.equal(result.loads, 1);
  assert(result.notices[0].message.includes("1"));
  assert(!result.confirmations[0].includes("undefined"));
  result = await scenario({ cancel: true });
  assert.equal(result.requests.length, 1);
  assert.equal(result.loads, 0);
  result = await scenario({ blocked: true });
  assert.equal(result.requests.length, 1);
  assert.equal(result.confirmations.length, 0);
  assert(result.notices[0].message.includes("supplier outcome needs verification"));
  for (const options of [{ previewError: true }, { submitError: true }]) {
    result = await scenario(options);
    assert.equal(result.notices[0].message, "precise ERP error");
    assert.equal(result.loads, 0);
  }
  console.log("POD retry button: success, cancel, blocked, preview error, submit error passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
