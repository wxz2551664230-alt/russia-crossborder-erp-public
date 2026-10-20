<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
type User = {id: string; username: string; display_name: string; role: string; enabled: boolean}
type Audit = {id: string; actor_name: string; action: string; target: string; status_code: number; created_at: string}
const users = ref<User[]>([]), audit = ref<Audit[]>([]), error = ref(''), busy = ref(false), open = ref(false), editing = ref('')
const roles: Record<string, string> = {admin: '管理员', operator: '业务操作员', viewer: '只读用户'}
const actions: Record<string, string> = {account_create: '创建账号', account_update: '修改账号', login_success: '登录成功', login_failed: '登录失败', login_limited: '登录尝试受限', logout: '退出登录', password_change: '修改密码', POST: '提交', PATCH: '修改', PUT: '更新', DELETE: '删除'}
const form = reactive({username: '', display_name: '', password: '', role: 'viewer', enabled: true})
async function load() {
  error.value = ''
  try { [users.value, audit.value] = await Promise.all([api.get<User[]>('/auth/users'), api.get<Audit[]>('/auth/audit')]) }
  catch (e) { error.value = (e as Error).message }
}
function edit(user?: User) {
  editing.value = user?.id || ''
  Object.assign(form, {username: user?.username || '', display_name: user?.display_name || '', password: '', role: user?.role || 'viewer', enabled: user?.enabled ?? true})
  open.value = true
}
async function save() {
  busy.value = true
  try {
    if (editing.value) await api.patch(`/auth/users/${editing.value}`, {display_name: form.display_name, role: form.role, enabled: form.enabled, ...(form.password ? {password: form.password} : {})})
    else await api.post('/auth/users', {username: form.username, display_name: form.display_name, password: form.password, role: form.role})
    form.password = ''; open.value = false; ElMessage.success('账号已保存'); await load()
  } catch (e) { ElMessage.error((e as Error).message) }
  finally { busy.value = false }
}
onMounted(load)
</script>
<template>
  <div class="users-page">
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-card v-else>
      <template #header><div class="toolbar"><strong>团队账号</strong><el-button type="primary" @click="edit()">创建账号</el-button></div></template>
      <p>所有账号共享同一套业务数据。管理员管理账号与系统配置；业务操作员处理日常业务；只读用户查看数据。暂不按店铺隔离数据。</p>
      <el-table :data="users"><el-table-column prop="username" label="账号"/><el-table-column prop="display_name" label="姓名"/><el-table-column label="角色"><template #default="{row}">{{ roles[row.role] }}</template></el-table-column><el-table-column label="状态"><template #default="{row}">{{ row.enabled ? '启用' : '停用' }}</template></el-table-column><el-table-column label="操作"><template #default="{row}"><el-button text @click="edit(row)">编辑</el-button></template></el-table-column></el-table>
    </el-card>
    <el-card v-if="!error"><template #header><div class="toolbar"><strong>最近100条操作记录</strong><el-button @click="load">刷新</el-button></div></template>
      <p>记录账号操作及业务修改请求。处理中或服务中断的请求显示“待核对”，请先核对业务结果。</p>
      <el-table :data="audit"><el-table-column label="时间" width="180"><template #default="{row}">{{ new Date(row.created_at.endsWith('Z') || /[+-]\d\d:\d\d$/.test(row.created_at) ? row.created_at : row.created_at + 'Z').toLocaleString() }}</template></el-table-column><el-table-column prop="actor_name" label="账号" width="130"/><el-table-column label="操作" width="130"><template #default="{row}">{{ actions[row.action] || row.action }}</template></el-table-column><el-table-column prop="target" label="对象" show-overflow-tooltip/><el-table-column label="请求结果" width="120"><template #default="{row}">{{ row.status_code === 0 ? '待核对' : row.status_code < 400 ? '已受理' : '未成功' }}</template></el-table-column></el-table>
    </el-card>
    <el-dialog v-model="open" :title="editing ? '编辑账号' : '创建账号'" width="min(500px, 95vw)" @closed="form.password = ''">
      <el-form label-position="top" @submit.prevent="save">
        <el-form-item label="账号（3至64位字母、数字、点、下划线或短横线）"><el-input v-model="form.username" :disabled="!!editing" maxlength="64" autocomplete="off" /></el-form-item>
        <el-form-item label="显示姓名"><el-input v-model="form.display_name" maxlength="100" /></el-form-item>
        <el-form-item label="角色"><el-select v-model="form.role"><el-option v-for="(label, value) in roles" :key="value" :label="label" :value="value" /></el-select></el-form-item>
        <el-form-item :label="editing ? '重置密码（留空则保持原密码）' : '初始密码（10至128个字符）'"><el-input v-model="form.password" type="password" show-password maxlength="128" autocomplete="new-password" /></el-form-item>
        <el-form-item v-if="editing" label="账号启用"><el-switch v-model="form.enabled" /></el-form-item>
        <p v-if="editing">保存后该用户需要重新登录。系统至少保留一个启用的管理员。</p>
        <el-button type="primary" native-type="submit" :loading="busy" :disabled="!form.username || (!editing && form.password.length < 10) || (!!form.password && form.password.length < 10)">保存</el-button>
      </el-form>
    </el-dialog>
  </div>
</template>
<style scoped>.users-page{display:grid;gap:20px}.toolbar{display:flex;align-items:center;justify-content:space-between}p{font-size:14px;line-height:1.7;color:#526070}</style>
