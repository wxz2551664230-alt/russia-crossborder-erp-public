<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

type Release = { version: string; filename: string; size_bytes: number; sha256: string; requires: string; download_path: string }
const release = ref<Release>()
const loading = ref(false)
const error = ref('')
async function load() {
  loading.value = true
  try { release.value = await api.get<Release>('/client/releases/latest'); error.value = '' }
  catch (cause) { error.value = String((cause as Error).message || cause) }
  finally { loading.value = false }
}
function download() {
  if (!release.value) return ElMessage.warning('安装包尚未就绪')
  window.location.href = release.value.download_path
}
onMounted(load)
</script>

<template>
  <div class="page-stack">
    <section class="panel">
      <div class="panel-head"><div><h2>在新电脑安装 ERP</h2><p>先在这台电脑登录云端 ERP，再下载客户端。每台电脑使用自己的本机会话与设备编号。</p></div><el-button @click="load">刷新版本</el-button></div>
      <div class="content" v-loading="loading">
        <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
        <template v-if="release">
          <div class="release"><div><strong>Windows 客户端 {{ release.version }}</strong><small>{{ release.filename }} · {{ (release.size_bytes / 1024 / 1024).toFixed(1) }} MB</small></div><el-button type="primary" @click="download">下载客户端安装包</el-button></div>
          <p>需要 {{ release.requires }}。压缩包内的 <b>install.cmd</b> 会为本次安装运行安装脚本，并校验包内文件；首次安装需要联网下载依赖。</p>
          <p class="checksum">安装包 SHA-256：{{ release.sha256 }}</p>
        </template>
      </div>
    </section>
    <section class="panel steps"><h3>安装与连接</h3><ol><li>将压缩包下载到新电脑并解压，确认电脑已安装 Python 3.12 或更新版本（含 Tk）。</li><li>双击解压目录中的 <b>install.cmd</b>，等待依赖安装完成。安装后桌面会出现“俄罗斯跨境 ERP”启动入口。</li><li>打开客户端，填写 <b>https://erp.example.com</b>、你自己的 ERP 账号和密码并登录。</li><li>在网页“我的执行电脑”选择当前电脑，运行连接检查。业务任务只在你发起任务的电脑执行，离线后等待这台电脑恢复。</li></ol><p>如果还要让 Codex 在新电脑修改整个程序，需要另行取得 ERP 源码并在 Codex 中打开；客户端安装包用于使用业务功能。</p><RouterLink to="/execution-devices">前往我的执行电脑</RouterLink></section>
  </div>
</template>

<style scoped>.content,.steps{padding:22px}.release{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:16px}.release strong,.release small{display:block}.release small,.content p,.steps p,.steps li{color:#526070;line-height:1.7}.checksum{overflow-wrap:anywhere;font-family:ui-monospace,Consolas,monospace;font-size:12px}.steps h3{margin-top:0}.steps li{margin:10px 0}</style>
