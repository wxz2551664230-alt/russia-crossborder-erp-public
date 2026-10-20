import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import App from './AuthenticatedApp.vue'
import router from './router'
import './styles.css'

createApp(App).use(ElementPlus).use(router).mount('#app')
