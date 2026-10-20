<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
defineProps<{user: {display_name: string; username: string; role: string}; accountMode: boolean}>()
const emit = defineEmits<{(event: 'logged-out'): void}>()
const roles: Record<string, string> = {admin: '管理员', operator: '业务操作员', viewer: '只读用户'}
const open = ref(false), oldPassword = ref(''), newPassword = ref(''), confirmPassword = ref(''), busy = ref(false)
async function logout() {
  busy.value = true
  try { await api.post('/auth/logout'); emit('logged-out') }
  catch (e) { ElMessage.error((e as Error).message) }
  finally { busy.value = false }
}
async function changePassword() {
  if (newPassword.value !== confirmPassword.value) { ElMessage.error('两次新密码不一致'); return }
  busy.value = true
  try {
    await api.post('/auth/password', {old_password: oldPassword.value, new_password: newPassword.value})
    ElMessage.success('密码已修改，请重新登录'); emit('logged-out')
  } catch (e) { ElMessage.error((e as Error).message) }
  finally { busy.value = false }
}
</script>
<template>
  <div class="account-menu"><span>{{ user.display_name || user.username }} · {{ roles[user.role] }}</span><el-button v-if="accountMode" text @click="open = true">修改密码</el-button><el-button text :loading="busy" @click="logout">退出</el-button></div>
  <el-dialog v-model="open" title="修改我的密码" width="min(440px, 95vw)" @closed="oldPassword = ''; newPassword = ''; confirmPassword = ''">
    <p>修改后所有电脑上的当前登录都会失效。</p>
    <el-form label-position="top" @submit.prevent="changePassword">
      <el-form-item label="当前密码"><el-input v-model="oldPassword" type="password" autocomplete="current-password" maxlength="128" /></el-form-item>
      <el-form-item label="新密码（10至128个字符）"><el-input v-model="newPassword" type="password" autocomplete="new-password" maxlength="128" show-password /></el-form-item>
      <el-form-item label="再次输入新密码"><el-input v-model="confirmPassword" type="password" autocomplete="new-password" maxlength="128" /></el-form-item>
      <el-button type="primary" native-type="submit" :loading="busy" :disabled="!oldPassword || newPassword.length < 10 || !confirmPassword">保存并重新登录</el-button>
    </el-form>
  </el-dialog>
</template>
<style scoped>.account-menu{display:flex;align-items:center;gap:4px;flex-wrap:wrap;font-size:13px}</style>
