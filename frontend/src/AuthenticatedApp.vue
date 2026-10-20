<script setup lang="ts">
import { onMounted, ref } from 'vue'
import App from './App.vue'
import { useRouter } from 'vue-router'
import { sharedProductsPilot } from './deployment'
const router = useRouter()
const ready = ref(false), loading = ref(true), token = ref(''), error = ref('')
const multiuser = ref(false), username = ref(''), password = ref('')
const remember = ref(true)
const user = ref<{id: string; username: string; display_name: string; role: string} | null>(null)
async function connectThisComputer() {
  if (!multiuser.value || !user.value) return
  try {
    const local = await fetch('http://127.0.0.1:48739/status', {signal: AbortSignal.timeout(2500)})
    if (!local.ok) return
    const device = await local.json()
    if (device.user_id !== user.value.id || !device.worker_id) return
    await fetch('/api/v1/execution-devices/select', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({worker_id: device.worker_id})})
  } catch { /* Manual device selection remains available when the local client is unavailable. */ }
}
async function login() {
  loading.value = true; error.value = ''
  try {
    const response = await fetch('/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(multiuser.value ? { username: username.value, password: password.value, remember: remember.value } : { token: token.value }) })
    if (!response.ok) throw new Error((await response.json()).message || '登录失败')
    const result = await response.json()
    user.value = result.user; token.value = ''; password.value = ''
    await connectThisComputer()
    ready.value = true
  } catch (e) { error.value = String((e as Error).message) }
  finally { loading.value = false }
}
onMounted(async () => {
  window.addEventListener('erp-session-expired', () => { ready.value = false; user.value = null; error.value = '登录已失效，请重新登录' })
  const params = new URLSearchParams(location.hash.slice(1))
  const initial = params.get('access_token')
  if (initial) history.replaceState(null, '', location.pathname + location.search)
  try {
    await router.isReady()
    const config = await fetch('/api/v1/auth/config')
    if (!config.ok) throw new Error('无法读取登录配置')
    const configuration = await config.json()
    multiuser.value = configuration.multiuser
    sharedProductsPilot.value = configuration.ui_profile === 'shared-products-pilot'
    if (sharedProductsPilot.value && ['/', '/ozon-follow/automation'].includes(router.currentRoute.value.path)) await router.replace('/catalog/products')
    if (initial && !multiuser.value) { token.value = initial; await login(); return }
    const response = await fetch('/api/v1/auth/session')
    if (response.ok) { user.value = (await response.json()).user; await connectThisComputer(); ready.value = true }
  }
  catch { error.value = '无法连接 ERP 服务' }
  finally { loading.value = false }
})
</script>
<template>
  <App v-if="ready && user" :current-user="user" :account-mode="multiuser" @logged-out="ready = false; user = null; error = ''" />
  <main v-else class="erp-login"><el-card><h1>俄罗斯跨境 ERP</h1><p>{{ multiuser ? '使用个人账号登录，共享团队业务数据。账号由管理员创建。' : '请输入访问密钥。使用本地启动入口可自动登录。' }}</p><el-form @submit.prevent="login">
    <template v-if="multiuser"><el-form-item label="账号"><el-input v-model="username" name="username" autocomplete="username" maxlength="64" /></el-form-item><el-form-item label="密码"><el-input v-model="password" name="password" type="password" show-password autocomplete="current-password" maxlength="128" /></el-form-item><el-checkbox v-model="remember">记住登录（30天）</el-checkbox></template>
    <el-input v-else v-model="token" type="password" show-password autocomplete="current-password" placeholder="访问密钥"/>
    <p v-if="error" role="alert">{{ error }}</p><el-button native-type="submit" type="primary" :loading="loading" :disabled="multiuser ? !username || !password : !token">登录</el-button></el-form></el-card></main>
</template>
<style scoped>.erp-login{max-width:480px;margin:15vh auto;padding:24px}.erp-login p{line-height:1.6}.erp-login button{margin-top:20px}</style>
