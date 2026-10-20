<script setup lang="ts">
import { computed, onMounted, onUnmounted, provide, ref, watch, defineAsyncComponent } from "vue";
import { useRoute } from "vue-router";
import {
  AlertTriangle,
  Boxes,
  Cable,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleGauge,
  FileUp,
  Image,
  Layers3,
  ListTodo,
  Megaphone,
  Menu,
  PackageSearch,
  Rocket,
  Search,
  Settings,
  SlidersHorizontal,
  Store,
  Warehouse,
  X,
} from "lucide-vue-next";
import { api } from "./api";
import AccountMenu from "./components/AccountMenu.vue";
import { sharedProductsPilot, sharedProductPages } from './deployment';

const props = defineProps<{currentUser: {id: string; username: string; display_name: string; role: string}; accountMode: boolean}>();
const pageUpdateAvailable=ref(false)
let lastVersionCheck=0
function refreshPage(){window.location.reload()}
async function checkPageVersion(){if(Date.now()-lastVersionCheck<30000)return;lastVersionCheck=Date.now();try{const current=document.querySelector<HTMLScriptElement>('script[type="module"][src]')?.getAttribute('src');if(!current)return;const response=await fetch('/',{cache:'no-store'});if(!response.ok)return;const html=new DOMParser().parseFromString(await response.text(),'text/html');const latest=html.querySelector<HTMLScriptElement>('script[type="module"][src]')?.getAttribute('src');pageUpdateAvailable.value=!!latest&&latest!==current}catch{/* An unavailable version check never interrupts work. */}}
onMounted(()=>{void checkPageVersion();window.addEventListener('focus',checkPageVersion)})
onUnmounted(()=>window.removeEventListener('focus',checkPageVersion))
provide('erpCurrentUser',computed(()=>props.currentUser));provide('erpAccountMode',computed(()=>props.accountMode));
const emit = defineEmits<{(event: 'logged-out'): void}>();

const route = useRoute();
const moduleTasks = ref(false);
const ModuleTaskList = defineAsyncComponent(()=>import('./views/JobsView.vue'));
watch(()=>route.path,()=>{moduleTasks.value=false});
const taskModule = computed(() => {
  const path=route.path;
  if(path.startsWith('/catalog/wb'))return 'wb';
  if(path.startsWith('/catalog/amazon'))return 'amazon';
  if(path.startsWith('/catalog/ozon'))return 'ozon';
  if(path.startsWith('/orders'))return 'orders';
  if(path.startsWith('/pod/supplier'))return 'supplier';
  if(path.startsWith('/pod/ai'))return 'ai';
  if(path.startsWith('/pod/'))return 'pod';
  if(path.startsWith('/self-listing'))return 'self';
  if(path.startsWith('/ad-optimization'))return 'ads';
  if(path.startsWith('/ozon-follow'))return 'follow';
  return '';
});
const collapsed = ref(false);
const mobileOpen = ref(false);
const apiOnline = ref(false);
const pageTitle = computed(() => String(route.meta.title || "工作台"));
const pageEyebrow = computed(() =>
  String(route.meta.eyebrow || "俄罗斯跨境 ERP"),
);

const groups = [
  {
    key: "catalog",
    label: "产品库",
    icon: Boxes,
    children: [
      { label: "全部产品", to: "/catalog/products", icon: Boxes },
      { label: "POD 产品库", to: "/pod/products", icon: Image },
      { label: "跟卖产品库", to: "/catalog/follow-products", icon: Layers3 },
      { label: "自主产品库", to: "/self-listing/products", icon: PackageSearch },
      { label: "OZON 上架", to: "/catalog/ozon/publish", icon: Rocket },
      { label: "WB 上架", to: "/catalog/wb/publish", icon: Rocket },
      { label: "Amazon 美国站上架", to: "/catalog/amazon/publish", icon: Rocket },
    ],
  },
  {
    key: "pod",
    label: "POD",
    icon: Image,
    children: [
      { label: "AI 生图超分", to: "/pod/ai-design", icon: Image },
      { label: "设计图库", to: "/pod/artworks", icon: Image },
      { label: "供应商图库", to: "/pod/supplier-gallery", icon: Warehouse },
      { label: "美国货盘", to: "/pod/us-catalog", icon: Warehouse },
      { label: "图片合成", to: "/pod/studio", icon: Layers3 },
    ],
  },
  {
    key: "self-listing",
    label: "自主上架",
    icon: PackageSearch,
    children: [
      { label: "产品导入", to: "/self-listing/import", icon: FileUp },
    ],
  },
  {
    key: "operations",
    label: "OZON 跟卖",
    icon: Layers3,
    children: [
      {
        label: "定价配置",
        to: "/ozon-follow/pricing-settings",
        icon: SlidersHorizontal,
      },
      { label: "自动流程", to: "/ozon-follow/automation", icon: CircleGauge },
      { label: "全部商品", to: "/ozon-follow/products", icon: Boxes },
      { label: "货源人工核验", to: "/ozon-follow/source-review", icon: Search },
      { label: "生图人工核验", to: "/ozon-follow/image-review", icon: Image },
      { label: "异常处理", to: "/ozon-follow/exceptions", icon: AlertTriangle },
      { label: "商品详情", to: "/ozon-follow/details", icon: Rocket },
      { label: "待提交", to: "/ozon-follow/pending-submit", icon: FileUp },
      {
        label: "待添加库存",
        to: "/ozon-follow/pending-stock",
        icon: Warehouse,
      },
      {
        label: "待添加促销",
        to: "/ozon-follow/pending-promotion",
        icon: PackageSearch,
      },
      { label: "已完成", to: "/ozon-follow/completed", icon: Check },
    ],
  },
  {
    key: "orders",
    label: "订单处理",
    icon: ListTodo,
    children: [{ label: "POD 订单", to: "/orders/pod", icon: Boxes }],
  },
  {
    key: "advertising",
    label: "广告优化",
    icon: Megaphone,
    children: [
      { label: "广告数据", to: "/ad-optimization", icon: CircleGauge },
      {
        label: "SKU 诊断",
        to: "/ad-optimization/skus",
        icon: SlidersHorizontal,
      },
    ],
  },
  {
    key: "tasks",
    label: "任务与协作",
    icon: ListTodo,
    children: [{ label: "任务中心", to: "/jobs", icon: ListTodo }, { label: "我的执行电脑", to: "/execution-devices", icon: Settings }, { label: "客户端下载", to: "/client-download", icon: FileUp }, { label: "Codex 全项目接管", to: "/codex-takeover", icon: Rocket }],
  },
  {
    key: "system",
    label: "系统设置",
    icon: SlidersHorizontal,
    children: [
      { label: "系统配置", to: "/configuration", icon: Settings },
      { label: "店铺管理", to: "/stores", icon: Store },
      { label: "接口中心", to: "/connections", icon: Cable },
      { label: "业务与迁移设置", to: "/settings", icon: Settings },
      { label: "用户与操作记录", to: "/users", icon: ListTodo },
    ],
  },
];
const visibleGroups = computed(() => groups.map(group => ({ ...group, children: group.children.filter(item => {
  if (item.to === '/users') return props.accountMode && props.currentUser.role === 'admin';
  if (item.to === '/execution-devices') return props.accountMode;
  return !props.accountMode || props.currentUser.role === 'admin' || !['/connections', '/settings', '/stores', '/ozon-follow/pricing-settings'].includes(item.to);
}) })).filter(group => group.children.length));
const openGroups = ref<string[]>(["operations"]);
function groupActive(group: (typeof groups)[number]) {
  return group.children.some((item) => item.to === route.path);
}
function toggleGroup(key: string) {
  if (collapsed.value) collapsed.value = false;
  openGroups.value = openGroups.value.includes(key) ? [] : [key];
}
watch(
  () => route.path,
  () => {
    const active = groups.find(groupActive);
    if (active) openGroups.value = [active.key];
  },
  { immediate: true },
);

onMounted(async () => {
  try {
    apiOnline.value = (await fetch("/health")).ok;
  } catch {
    apiOnline.value = false;
  }
});
</script>

<template>
  <div
    class="app-shell"
    :class="{ 'is-collapsed': collapsed, 'mobile-open': mobileOpen }"
  >
    <div class="mobile-backdrop" @click="mobileOpen = false" />
    <aside class="sidebar">
      <div class="brand-row">
        <div class="brand-mark"><span>RU</span></div>
        <div class="brand-copy">
          <strong>俄罗斯跨境</strong><small>ERP 工作台</small>
        </div>
        <button
          class="plain-icon mobile-close"
          aria-label="关闭菜单"
          @click="mobileOpen = false"
        >
          <X :size="20" />
        </button>
      </div>
      <div class="side-caption">工作台</div>
      <nav class="side-groups" aria-label="主导航">
        <section
          v-for="group in visibleGroups"
          :key="group.key"
          class="nav-group"
          :class="{
            'is-open': openGroups.includes(group.key),
            'has-active': groupActive(group),
          }"
        >
          <button
            class="nav-group-trigger"
            :aria-expanded="openGroups.includes(group.key)"
            @click="toggleGroup(group.key)"
          >
            <component :is="group.icon" :size="19" /><span>{{
              group.label
            }}</span>
            <ChevronDown
              v-if="openGroups.includes(group.key)"
              class="group-chevron"
              :size="16"
            />
            <ChevronRight v-else class="group-chevron" :size="16" />
          </button>
          <div
            v-show="openGroups.includes(group.key) && !collapsed"
            class="nav-children"
          >
            <RouterLink
              v-for="item in group.children"
              :key="item.to"
              :to="item.to"
              @click="mobileOpen = false"
            >
              <component :is="item.icon" :size="18" /><span>{{
                item.label
              }}</span>
              <small v-if="sharedProductsPilot && !sharedProductPages.includes(item.to)" class="pilot-pending">待接入</small>
            </RouterLink>
          </div>
        </section>
      </nav>
      <div class="sidebar-status">
        <span class="status-dot" :class="{ online: apiOnline }" />
        <div>
          <strong>{{ apiOnline ? "ERP 服务已连接" : "服务未连接" }}</strong
          ><small>{{ accountMode ? '团队共享数据' : '本地模式' }}</small>
        </div>
      </div>
      <button class="collapse-button" @click="collapsed = !collapsed">
        <ChevronLeft :size="18" /><span>收起导航</span>
      </button>
    </aside>

    <main class="workspace">
      <header class="topbar">
        <button
          class="plain-icon mobile-menu"
          aria-label="打开菜单"
          @click="mobileOpen = true"
        >
          <Menu :size="22" />
        </button>
        <div>
          <span>{{ pageEyebrow }}</span>
          <h1>{{ pageTitle }}</h1>
        </div>
        <div class="local-badge">
          <span class="status-dot online" />{{ accountMode ? '团队模式' : '本机模式' }}
        </div>
        <AccountMenu :user="currentUser" :account-mode="accountMode" @logged-out="emit('logged-out')" />
        <el-button v-if="taskModule" @click="moduleTasks=true">本板块未完成任务</el-button>
      </header>
      <div class="page-stage"><el-alert v-if="pageUpdateAvailable" title="网页已有新版本，刷新后使用最新功能" type="info" :closable="false" show-icon><el-button size="small" @click="refreshPage">刷新到新版</el-button><span>请先保存正在编辑的内容。</span></el-alert>
        <template v-if="sharedProductsPilot">
          <RouterView v-if="sharedProductPages.includes(route.path)" :key="route.path" />
          <section v-else class="panel"><el-empty :description="`${pageTitle}正在接入多人版本`"><p>当前可使用共享产品查询和内部备注同步。该模块接通后，业务任务将在你的电脑上执行。</p><el-button type="primary" @click="$router.push('/catalog/products')">返回共享产品库</el-button></el-empty></section>
        </template>
        <RouterView v-else :key="route.path" />
      </div>
    </main>
    <el-drawer v-model="moduleTasks" title="本板块任务" size="90%" destroy-on-close><ModuleTaskList v-if="moduleTasks" :module-scope="taskModule" embedded/></el-drawer>
  </div>
</template>
<style scoped>.pilot-pending{margin-left:auto;font-size:10px;opacity:.65;white-space:nowrap}</style>
