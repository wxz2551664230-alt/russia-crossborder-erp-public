<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { sharedProductsPilot } from '../deployment'
type Device = {id: string; name: string; owner_user_id: string; online: boolean; capabilities: string[]}
const devices = ref<Device[]>([]), users = ref<{id: string; display_name: string; username: string}[]>([])
const current = ref(''), userId = ref(''), admin = ref(false), error = ref(''), busy = ref(false), probeJob = ref('')
const selectedOwners = ref<Record<string, string>>({})
async function load() {
  try {
    const data = await api.get<any>('/execution-devices')
    devices.value = data.items; current.value = data.current_worker_id; userId.value = data.user_id; admin.value = data.admin
    selectedOwners.value = Object.fromEntries(data.items.map((row: Device) => [row.id, row.owner_user_id]))
    if (admin.value) users.value = await api.get<any[]>('/auth/users')
    error.value = ''
  } catch (e) { error.value = (e as Error).message }
}
async function select(device: Device) {
  busy.value = true
  try { await api.post('/execution-devices/select', {worker_id: device.id}); await load(); ElMessage.success('本次登录已选择执行电脑') }
  catch (e) { ElMessage.error((e as Error).message) }
  finally { busy.value = false }
}
async function bind(device: Device) {
  busy.value = true
  try { await api.patch(`/execution-devices/${encodeURIComponent(device.id)}/owner`, {user_id: selectedOwners.value[device.id] || ''}); await load(); ElMessage.success('电脑归属已保存，使用者需重新选择当前电脑') }
  catch (e) { ElMessage.error((e as Error).message) }
  finally { busy.value = false }
}
async function probe() {
  busy.value = true
  try { const data = await api.post<any>('/execution-devices/probe'); probeJob.value = data.job_id; ElMessage.success('已提交给当前电脑，请到任务中心查看结果') }
  catch (e) { ElMessage.error((e as Error).message) }
  finally { busy.value = false }
}
onMounted(load)
</script>
<template>
  <el-card>
    <template #header><div class="device-toolbar"><strong>我的执行电脑</strong><el-button @click="load">刷新状态</el-button></div></template>
    <p>任务在你当前使用的电脑上运行，服务器统一保存数据与结果。请选择面前这台电脑；本次登录的选择不影响其他电脑的登录。</p>
    <el-alert :title="sharedProductsPilot ? '电脑离线后任务等待原电脑恢复。已接通连接检查、从 OSS 恢复产品文件和图片合成；图片合成需要客户端 0.3.0。' : '电脑离线后任务等待原电脑恢复，不自动交给别人。当前已接通连接检查和图片准备，其他业务仍在适配。'" type="info" :closable="false" />
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-empty v-if="!error && !devices.length" :description="sharedProductsPilot ? '请先在本机客户端登录，再打开 Russia ERP Local Worker，随后刷新此页并选择这台电脑。' : '尚无绑定电脑。请先连接本地执行端，再由管理员绑定账号。'" />
    <el-table v-else :data="devices">
      <el-table-column label="电脑"><template #default="{row}"><strong>{{ row.name || row.id }}</strong><div>{{ row.id }}</div></template></el-table-column>
      <el-table-column label="连接" width="100"><template #default="{row}">{{ row.online ? '最近在线' : '离线或任务中' }}</template></el-table-column>
      <el-table-column v-if="admin" label="绑定账号" min-width="240"><template #default="{row}"><el-select v-model="selectedOwners[row.id]" clearable placeholder="未绑定"><el-option v-for="user in users" :key="user.id" :value="user.id" :label="`${user.display_name} (${user.username})`" /></el-select><el-button text :disabled="busy" @click="bind(row)">保存归属</el-button></template></el-table-column>
      <el-table-column label="当前电脑" width="170"><template #default="{row}"><el-tag v-if="row.id === current">本次登录使用</el-tag><el-button v-else-if="row.owner_user_id === userId" :disabled="busy" @click="select(row)">选择这台电脑</el-button></template></el-table-column>
    </el-table>
    <p>当前选择：{{ devices.find(d => d.id === current)?.name || current || '尚未选择' }}</p>
    <el-button :disabled="!current || busy" @click="probe">检查本机任务连接</el-button>
    <p v-if="probeJob">检查任务已创建。<RouterLink to="/jobs">前往任务中心</RouterLink></p>
  </el-card>
</template>
<style scoped>.device-toolbar{display:flex;align-items:center;justify-content:space-between}p{line-height:1.7;color:#526070}.el-alert{margin:16px 0}</style>
