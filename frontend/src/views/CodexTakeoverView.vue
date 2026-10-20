<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'

const projectPath = ref('D:\\Desktop\\俄罗斯跨境ERP')
const goal = ref('')
const prompt = computed(() => `请作为 Codex 接管俄罗斯跨境 ERP 的本次工作。\n\n本次目标：${goal.value.trim() || '先审查当前程序、业务状态和未完成问题，再提出并实施最有价值的修复。'}\nERP 源码目录：${projectPath.value.trim()}\n\n第一步：在这个源码目录读取 AGENTS.md、CODEX接管指南.md、docs/codex/ERP全功能地图.md、docs/codex/全量接口索引.md、docs/codex/openapi.json，以及 reports/migration-baseline-20260923/移植基准与审查记录.md；运行 .venv\\Scripts\\python.exe scripts/codex_full_takeover.py，核对当前分支和已有改动。以上文档用于定位，不将旧的能力描述当成最新验收结果。\n\n工作范围覆盖整个 ERP，包括产品库、导入、POD、供应商、WB/Ozon 上架、订单履约、广告、系统配置、任务中心及程序自身的代码修改、测试、优化和发布。按本次目标定位真实页面、服务、接口与个人电脑执行端，先确认能力状态和业务规则，再完成实现与验收。已有未提交改动要保留；旧程序源目录只读；不得擅自改变原 ERP 页面结构或业务含义。\n\n需要修改程序时，直接在 ERP 源码目录完成代码审查、修改、相关测试、前端构建和文档更新；接口变化后运行索引导出。需要处理真实业务时，使用当前账号授权的已接通流程；谁发起就由谁的电脑执行，服务器只协调并保存共享结果。平台写入前核对现状与旧回执，结果未知时先查询，不盲目重发。桌面 MCP 当前只有共享产品查询、读取和备注比较保存，不能把它当作全功能业务桥接。\n\n请先报告你核实到的现状和本次目标的实施路径，然后持续执行到可验证的结果；不要把“已有入口”写成“已真实验收”。`)

async function copy() {
  try {
    await navigator.clipboard.writeText(prompt.value)
    ElMessage.success('接管说明已复制，请在打开 ERP 源码目录的 Codex 新对话中粘贴')
  } catch {
    ElMessage.warning('复制失败，请从下方文本框手动复制')
  }
}
</script>

<template>
  <div class="page-stack takeover-page">
    <section class="panel">
      <div class="panel-head"><div><h2>Codex 全项目接管</h2><p>让新 Codex 对话接手本次开发、排障或业务目标，并从完整 ERP 功能地图定位工作。</p></div></div>
      <div class="form">
        <label>这台电脑上的 ERP 源码目录<el-input v-model="projectPath" placeholder="例如 D:\Desktop\俄罗斯跨境ERP" /></label>
        <label>本次要 Codex 完成什么<el-input v-model="goal" type="textarea" :rows="4" placeholder="例如：修复 WB 价格任务，或审查并优化订单同步" /></label>
        <div class="actions"><el-button type="primary" :disabled="!projectPath.trim()" @click="copy">复制给新 Codex 对话</el-button><span>在 Codex 桌面应用中打开上述源码目录，再粘贴说明。</span></div>
        <el-input :model-value="prompt" type="textarea" :rows="16" readonly />
      </div>
    </section>
    <section class="panel notes"><h3>接管范围与当前能力</h3><p>Codex 可以在有源码的电脑上审查、修改、测试和优化整个 ERP。业务功能按现有账号权限及已接通的页面、接口和个人电脑执行端运行；新对话先核实每个模块的实际状态。</p><p>现有桌面 MCP 只提供共享产品查询、读取和备注保存。此入口不会自行运行平台任务，也不会把你的密码或店铺密钥放进接管说明。</p><p>只有安装了 ERP 客户端、没有源码的电脑，仍可正常使用已接通的业务功能；要修改程序，需要先取得 ERP 源码并在 Codex 中打开。</p></section>
  </div>
</template>

<style scoped>.form{padding:22px;display:grid;gap:18px}.form label{display:grid;gap:8px;font-weight:600}.actions{display:flex;align-items:center;gap:14px;flex-wrap:wrap}.actions span,.notes p{color:#64748b}.notes{padding:20px 24px}.notes h3{margin:0 0 12px}.notes p{line-height:1.6}</style>
