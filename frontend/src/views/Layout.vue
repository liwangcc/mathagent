<template>
  <el-container class="layout" :class="{ 'is-collapsed': collapsed && !isMobile }">
    <el-aside
      id="primary-navigation"
      :width="collapsed ? '68px' : '228px'"
      class="aside"
      :class="{ 'mobile-hidden': isMobile && !mobileOpen }"
      :aria-hidden="isMobile && !mobileOpen ? 'true' : undefined"
      :inert="isMobile && !mobileOpen ? '' : null"
      @keydown.esc.stop="closeMobileMenu"
      @keydown.tab="trapMobileFocus"
    >
      <div class="logo-bar">
        <span class="logo-mark" aria-hidden="true">M</span>
        <span v-if="!collapsed" class="logo-copy">
          <span class="logo-name">MathAgent</span>
          <span class="logo-tagline">数学教学工作台</span>
        </span>
        <el-button v-if="isMobile" text class="mobile-close" aria-label="关闭导航" @click="closeMobileMenu">
          <el-icon><Close /></el-icon>
        </el-button>
      </div>
      <el-scrollbar class="nav-scroll">
        <el-menu
          :default-active="activeMenu"
          :collapse="collapsed"
          :collapse-transition="false"
          router
          background-color="transparent"
          text-color="var(--sidebar-text)"
          active-text-color="var(--ink)"
        >
          <el-menu-item index="/">
            <el-icon><HomeFilled /></el-icon>
            <template #title>首页</template>
          </el-menu-item>

          <el-menu-item-group title="批阅">
            <el-menu-item index="/upload">
              <el-icon><Upload /></el-icon>
              <template #title>详细批改</template>
            </el-menu-item>
            <el-menu-item index="/anonymous-grade">
              <el-icon><Files /></el-icon>
              <template #title>无身份批阅</template>
            </el-menu-item>
            <el-menu-item index="/answer-sheet">
              <el-icon><Tickets /></el-icon>
              <template #title>答题卡批阅</template>
            </el-menu-item>
            <el-menu-item index="/grade-records" data-tour="nav-grade-records">
              <el-icon><Finished /></el-icon>
              <template #title>批改记录</template>
            </el-menu-item>
          </el-menu-item-group>

          <el-menu-item-group title="班级与学情">
            <el-menu-item index="/students">
              <el-icon><User /></el-icon>
              <template #title>班级管理</template>
            </el-menu-item>
            <el-menu-item index="/reports">
              <el-icon><Document /></el-icon>
              <template #title>成绩报告</template>
            </el-menu-item>
            <el-menu-item index="/stats">
              <el-icon><TrendCharts /></el-icon>
              <template #title>统计分析</template>
            </el-menu-item>
          </el-menu-item-group>

          <el-menu-item-group title="教学资源">
            <el-menu-item index="/bank">
              <el-icon><Collection /></el-icon>
              <template #title>题库管理</template>
            </el-menu-item>
            <el-menu-item index="/bank/past-papers">
              <el-icon><Files /></el-icon>
              <template #title>整卷真题</template>
            </el-menu-item>
            <el-menu-item index="/pending-answers">
              <el-icon><DocumentChecked /></el-icon>
              <span v-if="collapsed && pendingCount > 0" class="nav-dot" aria-hidden="true" />
              <template #title>
                <span class="nav-label-with-count">
                  待入库
                  <span v-if="pendingCount > 0" class="nav-count" :aria-label="`${pendingCount} 条待入库`">{{ pendingCount > 99 ? '99+' : pendingCount }}</span>
                </span>
              </template>
            </el-menu-item>
            <el-menu-item index="/exercise">
              <el-icon><EditPen /></el-icon>
              <template #title>出题练习</template>
            </el-menu-item>
            <el-menu-item index="/courseware">
              <el-icon><Reading /></el-icon>
              <template #title>课件库</template>
            </el-menu-item>
          </el-menu-item-group>

          <el-menu-item-group v-if="auth.user?.role === 'admin'" title="系统">
            <el-menu-item index="/settings">
              <el-icon><Setting /></el-icon>
              <template #title>系统设置</template>
            </el-menu-item>
          </el-menu-item-group>
        </el-menu>
      </el-scrollbar>
    </el-aside>

    <el-container class="workspace">
      <el-header class="header">
        <div class="header-left">
          <el-button
            ref="menuToggleRef"
            text
            class="menu-toggle"
            data-tour="menu-toggle"
            :aria-label="isMobile ? (mobileOpen ? '关闭导航' : '打开导航') : (collapsed ? '展开导航' : '折叠导航')"
            :aria-expanded="isMobile ? String(mobileOpen) : undefined"
            aria-controls="primary-navigation"
            @click="toggleCollapse"
          >
            <el-icon size="18"><Fold v-if="!collapsed" /><Expand v-else /></el-icon>
          </el-button>
          <nav class="crumbs" aria-label="当前位置">
            <span v-if="navGroup" class="crumb-group">{{ navGroup }}</span>
            <span v-if="navGroup" class="crumb-sep" aria-hidden="true">/</span>
            <span class="page-title" aria-current="page">{{ route.meta?.title || '' }}</span>
          </nav>
        </div>
        <div class="header-right">
          <el-button class="guide-button" data-tour="guide-button" plain aria-label="打开使用引导" @click="openUsageGuide">
            <el-icon><QuestionFilled /></el-icon>
            <span class="guide-button__label">使用引导</span>
          </el-button>

          <!-- 班级切换 -->
          <el-dropdown @command="onClassCommand" trigger="click" class="class-dropdown">
            <span class="class-chip" data-tour="class-switcher">
              <el-icon><School /></el-icon>
              <span class="class-name">{{ auth.currentClassName }}</span>
              <el-icon class="chip-caret"><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu class="class-menu">
                <el-dropdown-item
                  v-for="cls in auth.classes"
                  :key="cls.id"
                  :command="{ action: 'switch', id: cls.id }"
                  :class="{ active: auth.currentClassId === cls.id }"
                >
                  <el-icon><School /></el-icon>{{ cls.class_name }}
                  <span class="student-count">({{ cls.student_count }}人)</span>
                </el-dropdown-item>
                <el-dropdown-item divided command="manage">
                  <el-icon><Setting /></el-icon>打开班级管理
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>

          <!-- 用户菜单 -->
          <el-dropdown @command="onCommand">
            <span class="user-chip">
              <span class="user-avatar" aria-hidden="true">{{ userInitial }}</span>
              <span class="user-name">{{ auth.user?.name || '用户' }}</span>
              <el-icon class="chip-caret"><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="profile">
                  <el-icon><Setting /></el-icon>个人资料
                </el-dropdown-item>
                <el-dropdown-item command="logout" divided>
                  <el-icon><SwitchButton /></el-icon>退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
        <div v-if="routeLoading" class="ma-progress-line" role="progressbar" aria-label="页面加载中" />
      </el-header>

      <el-main ref="mainRef" class="main">
        <el-alert
          v-if="needsModelSetup"
          class="model-setup-banner"
          type="warning"
          show-icon
          :closable="false"
          title="还没有可用的批阅模型密钥：请到「系统设置 → 批阅模型密钥」填写「模板处理」和「学生卷转录与批阅」两项。"
        >
          <el-button size="small" type="warning" text @click="router.push('/settings')">
            去设置
          </el-button>
        </el-alert>
        <router-view v-slot="{ Component }">
          <transition name="page" mode="out-in" @before-enter="resetMainScroll">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>

    <UsageGuideDialog ref="usageGuideRef" />
    <transition name="fade">
      <button v-if="isMobile && mobileOpen" type="button" class="mask" aria-label="关闭导航" @click="closeMobileMenu" />
    </transition>
  </el-container>
</template>

<script setup>
import { computed, ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessageBox, ElMessage } from 'element-plus'
import api from '@/api'
import {
  Fold, Expand, Close, UserFilled, ArrowDown, Setting, SwitchButton, School,
  HomeFilled, Upload, Tickets, User, Document, Collection, EditPen, Files, Finished, TrendCharts, Reading,
  QuestionFilled, DocumentChecked,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import UsageGuideDialog from '@/components/UsageGuideDialog.vue'
import { hasCompleteEducationProfile } from '@/utils/educationStage'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

// 首次启动引导：没有任何批阅模型密钥时，管理员在每个页面顶部都能看到入口。
const needsModelSetup = ref(false)
const teachingProfileComplete = computed(() => hasCompleteEducationProfile(auth.user))

const activeMenu = computed(() => {
  // 班级相关路由（含具体班级、学生详情）统一高亮「班级管理」
  if (route.path.startsWith('/class/') || route.path.startsWith('/student/')) {
    return '/students'
  }
  // 子页面高亮其所属入口，导航位置与面包屑保持一致
  if (route.path.startsWith('/stats/')) return '/stats'
  if (route.path.startsWith('/bank/past-papers')) return '/bank/past-papers'
  if (route.path.startsWith('/bank/')) return '/bank'
  return route.path
})

const NAV_GROUPS = [
  ['批阅', ['/upload', '/anonymous-grade', '/answer-sheet', '/grade-records']],
  ['班级与学情', ['/students', '/reports', '/stats']],
  ['教学资源', ['/bank', '/pending-answers', '/exercise', '/courseware']],
  ['系统', ['/settings', '/profile']],
]

// Header breadcrumb group, derived from the same entries as the sidebar sections.
const navGroup = computed(() => {
  const path = activeMenu.value
  const match = NAV_GROUPS.find(([, paths]) => paths.some((item) => path === item || path.startsWith(`${item}/`)))
  return match ? match[0] : ''
})

const userInitial = computed(() => String(auth.user?.name || '用').trim().slice(0, 1) || '用')

const collapsed = ref(false)
const mobileOpen = ref(false)
const isMobile = ref(false)
const menuToggleRef = ref(null)
const usageGuideRef = ref(null)
const mainRef = ref(null)
const routeLoading = ref(false)
let routeLoadingTimer = 0
const pendingCount = ref(0)
let pendingInterval = null
let layoutReady = false

// The badge counts the same items the 待入库 page lists (answers awaiting
// confirmation), read from the paginated total rather than a page length.
const PENDING_REFRESH_MS = 300000
async function fetchPendingCount() {
  if (!auth.token || !teachingProfileComplete.value) return
  try {
    const data = await api.bank.pendingAnswers({ page: 1, page_size: 1 }, { suppressErrorToast: true })
    pendingCount.value = Number(data?.total ?? data?.count ?? 0) || 0
  } catch {
    // The badge is advisory; a failed refresh keeps the last known count.
  }
}

// Only surface the loading hairline when a route chunk takes noticeably long.
const stopRouteLoadingStart = router.beforeEach(() => {
  window.clearTimeout(routeLoadingTimer)
  routeLoadingTimer = window.setTimeout(() => { routeLoading.value = true }, 160)
})
function finishRouteLoading() {
  window.clearTimeout(routeLoadingTimer)
  routeLoading.value = false
}
const stopRouteLoadingEnd = router.afterEach(finishRouteLoading)
const stopRouteLoadingError = router.onError(finishRouteLoading)

function resetMainScroll() {
  const main = mainRef.value?.$el || mainRef.value
  if (main) main.scrollTop = 0
}

function checkSize() {
  isMobile.value = window.innerWidth <= 768
  if (isMobile.value) collapsed.value = false
  else mobileOpen.value = false
}

async function toggleCollapse() {
  if (isMobile.value) {
    mobileOpen.value = !mobileOpen.value
    if (mobileOpen.value) {
      await nextTick()
      document.querySelector('#primary-navigation .el-menu-item')?.focus()
    }
  } else {
    collapsed.value = !collapsed.value
  }
}

function focusMenuToggle() {
  const target = menuToggleRef.value?.$el
    || menuToggleRef.value
    || document.querySelector('[aria-controls="primary-navigation"]')
  target?.focus?.()
}

function closeMobileMenu(restoreFocus = true) {
  if (!isMobile.value) return
  mobileOpen.value = false
  if (restoreFocus) {
    nextTick(() => requestAnimationFrame(focusMenuToggle))
  }
}

function trapMobileFocus(event) {
  if (!isMobile.value || !mobileOpen.value) return
  const aside = document.getElementById('primary-navigation')
  const items = [...(aside?.querySelectorAll('button:not([disabled]), [href], [tabindex]:not([tabindex="-1"]), .el-menu-item') || [])]
  if (!items.length) return
  const first = items[0]
  const last = items[items.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

async function onCommand(cmd) {
  if (cmd === 'logout') {
    try {
      await ElMessageBox.confirm('确定退出登录吗？', '提示', {
        confirmButtonText: '退出',
        cancelButtonText: '取消',
        type: 'warning',
      })
      await auth.logout()
      await router.replace('/login')
      ElMessage.success('已退出')
    } catch {
      // 用户取消退出
    }
  } else if (cmd === 'profile') {
    router.push('/profile')
  }
}

async function onClassCommand(cmd) {
  if (cmd === 'manage') {
    router.push(auth.currentClassId ? `/class/${auth.currentClassId}/students` : '/students')
    return
  }
  if (cmd.action === 'switch') {
    if (cmd.id === auth.currentClassId) return
    try {
      await auth.switchClass(cmd.id)
      ElMessage.success(`已切换到「${auth.currentClassName}」`)
      if (route.path.startsWith('/students') || route.path.startsWith('/class/')) {
        router.push(cmd.id ? `/class/${cmd.id}/students` : '/students')
      } else {
        router.go(0)
      }
    } catch {
      // error handled by interceptor
    }
  }
}

async function openUsageGuide() {
  if (!teachingProfileComplete.value) {
    ElMessage.info('请先选择教学学段，保存后继续使用引导')
    return
  }
  await usageGuideRef.value?.open()
}

async function openGuideFromQuery(value) {
  if (value !== '1') return
  await nextTick()
  await openUsageGuide()
  const query = { ...route.query }
  delete query.guide
  await router.replace({ path: route.path, query, hash: route.hash })
}

onMounted(async () => {
  checkSize()
  window.addEventListener('resize', checkSize)
  window.addEventListener('mathagent:pending-answers-changed', fetchPendingCount)
  fetchPendingCount()
  if (auth.user?.role === 'admin') {
    // 首次启动引导：只有管理员能改密钥，其他人不打扰。
    api.settings.get()
      .then((data) => { needsModelSetup.value = !!data?.model_profiles?.needs_setup })
      .catch(() => {})
  }
  try {
    await auth.fetchClasses()
    // 自动选中第一个班级（如果没有选中任何班级且有班级可用）
    if (!auth.currentClassId && auth.classes.length) {
      await auth.switchClass(auth.classes[0].id)
    }
  } finally {
    // Class bootstrap failure must not skip the mandatory desktop setup.
    layoutReady = true
    if (teachingProfileComplete.value) await usageGuideRef.value?.initialize()
    pendingInterval = setInterval(fetchPendingCount, PENDING_REFRESH_MS)
  }
})

onBeforeUnmount(() => {
  layoutReady = false
  stopRouteLoadingStart()
  stopRouteLoadingEnd()
  stopRouteLoadingError()
  finishRouteLoading()
  window.removeEventListener('resize', checkSize)
  window.removeEventListener('mathagent:pending-answers-changed', fetchPendingCount)
  if (pendingInterval) clearInterval(pendingInterval)
})

watch(teachingProfileComplete, async (complete, previous) => {
  if (!complete || previous || !layoutReady) return
  await nextTick()
  await usageGuideRef.value?.initialize()
})

watch(() => route.path, () => {
  if (isMobile.value) closeMobileMenu(false)
})

watch(() => route.query.guide, openGuideFromQuery, { immediate: true })
</script>

<style scoped>
.layout {
  height: 100vh;
  height: 100dvh;
  background: var(--bg);
}

/* ── Kraft rail: warm, flat, bookish ── */
.aside {
  position: relative;
  display: flex;
  flex-direction: column;
  background: var(--sidebar-bg);
  border-right: 1px solid var(--line);
  color: var(--ink-soft);
  overflow: hidden;
  transition: width var(--dur-3) var(--ease-emphasized), transform var(--dur-3) var(--ease-emphasized), visibility var(--dur-3) var(--ease-out);
}

.logo-bar {
  height: 72px;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 0 20px;
  color: var(--ink);
}

.logo-mark {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  color: var(--on-ink);
  background: var(--moss);
  border-radius: 8px;
  font-family: var(--font-serif);
  font-size: 19px;
  font-style: italic;
  font-weight: 500;
}

.logo-copy {
  display: flex;
  flex-direction: column;
  min-width: 0;
  line-height: 1.1;
}

.logo-name {
  overflow: hidden;
  font-family: var(--font-serif);
  font-size: 21px;
  font-weight: 500;
  letter-spacing: -0.01em;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.logo-tagline {
  margin-top: 5px;
  color: var(--ink-muted);
  font-size: var(--fs-2xs);
  font-weight: 500;
  letter-spacing: 0.16em;
}

.nav-scroll {
  flex: 1 1 auto;
  min-height: 0;
}

.aside :deep(.el-menu) {
  padding: 4px 0 20px;
  border-right: none;
  background: transparent;
}

.aside :deep(.el-menu-item-group__title) {
  padding: 18px 24px 6px !important;
  color: var(--ink-faint);
  font-size: var(--fs-2xs);
  font-weight: 600;
  letter-spacing: 0.16em;
  line-height: 1.2;
}

.aside :deep(.el-menu-item) {
  position: relative;
  height: 38px;
  margin: 1px 12px;
  padding: 0 12px !important;
  border-radius: 8px;
  color: var(--ink-soft);
  font-size: var(--fs-md);
  font-weight: 500;
  line-height: 38px;
  background: transparent;
  transition: background-color var(--dur-2) var(--ease-out), color var(--dur-2) var(--ease-out), box-shadow var(--dur-2) var(--ease-out);
}

.aside :deep(.el-menu-item .el-icon) {
  margin-right: 11px;
  color: var(--ink-muted);
  font-size: 17px;
  transition: color var(--dur-2) var(--ease-out);
}

.aside :deep(.el-menu-item:hover),
.aside :deep(.el-menu-item:focus-visible) {
  background: var(--fill);
  color: var(--ink);
}

/* Active entry: a paper tab lifted off the kraft rail, marked with moss. */
.aside :deep(.el-menu-item.is-active) {
  background: var(--paper-raised);
  color: var(--ink);
  font-weight: 600;
  box-shadow: 0 0 0 1px var(--line), var(--shadow-xs);
}

.aside :deep(.el-menu-item.is-active .el-icon) {
  color: var(--moss);
}

.aside :deep(.el-menu-item::before) {
  content: '';
  position: absolute;
  top: 11px;
  bottom: 11px;
  left: -12px;
  width: 3px;
  border-radius: 0 3px 3px 0;
  background: var(--moss);
  opacity: 0;
  transform: scaleY(0.3);
  transition: opacity var(--dur-2) var(--ease-out), transform var(--dur-3) var(--ease-emphasized);
}

.aside :deep(.el-menu-item.is-active::before) {
  opacity: 1;
  transform: scaleY(1);
}

.nav-label-with-count {
  display: inline-flex;
  flex: 1 1 auto;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}

.nav-count {
  min-width: 22px;
  height: 20px;
  padding: 0 7px;
  border-radius: var(--radius-pill);
  background: var(--moss);
  color: var(--on-ink);
  font-size: var(--fs-xs);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  line-height: 20px;
  text-align: center;
}

.nav-dot {
  position: absolute;
  top: 8px;
  left: 30px;
  width: 7px;
  height: 7px;
  border: 2px solid var(--sidebar-bg);
  border-radius: 50%;
  background: var(--moss);
  box-sizing: content-box;
}

.layout.is-collapsed .logo-bar {
  justify-content: center;
  padding: 0;
}

.layout.is-collapsed .aside :deep(.el-menu--collapse) {
  width: 68px;
}

.layout.is-collapsed .aside :deep(.el-menu-item-group__title) {
  height: 1px;
  margin: 12px 18px;
  padding: 0 !important;
  overflow: hidden;
  background: var(--line);
  color: transparent;
}

.layout.is-collapsed .aside :deep(.el-menu-item) {
  justify-content: center;
  margin: 2px 12px;
  padding: 0 !important;
}

.layout.is-collapsed .aside :deep(.el-menu-item .el-icon) {
  margin-right: 0;
}

/* ── Header floats on the paper canvas; no bar, no divider ── */
.workspace {
  min-width: 0;
}

.header {
  position: relative;
  z-index: 5;
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 0 32px 0 18px;
  background: var(--bg);
  border-bottom: 1px solid var(--line);
}

.header-left,
.header-right {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.menu-toggle {
  width: 36px;
  min-height: 36px;
  padding: 0;
  color: var(--ink-muted);
}

.crumbs {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  white-space: nowrap;
}

.crumb-group {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
  font-weight: 600;
  letter-spacing: 0.14em;
}

.crumb-sep {
  color: var(--ink-faint);
  font-size: var(--fs-sm);
}

.page-title {
  overflow: hidden;
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 19px;
  font-weight: 500;
  text-overflow: ellipsis;
}

.guide-button {
  min-height: 34px;
  margin: 0;
  padding: 0 12px;
  border-color: transparent;
  background: transparent;
  color: var(--ink-muted);
  font-weight: var(--fw-medium);
}

.guide-button:hover,
.guide-button:focus-visible {
  border-color: transparent;
  background: color-mix(in srgb, var(--ink) 6%, transparent);
  color: var(--ink);
}

.class-chip,
.user-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 36px;
  cursor: pointer;
  border-radius: var(--radius-pill);
  font-size: var(--fs-sm);
  transition: background-color var(--dur-2) var(--ease-out), box-shadow var(--dur-2) var(--ease-out), color var(--dur-2) var(--ease-out);
}

.class-chip {
  padding: 0 14px 0 12px;
  background: var(--paper-raised);
  color: var(--ink);
  font-weight: var(--fw-semibold);
  box-shadow: 0 0 0 1px var(--border);
}

.class-chip:hover {
  box-shadow: 0 0 0 1px var(--ink);
}

.class-chip .el-icon:first-child {
  color: var(--moss);
}

.chip-caret {
  color: var(--ink-faint);
  font-size: 12px;
}

.class-name {
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.student-count {
  margin-left: 4px;
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

:deep(.el-dropdown-menu__item.active) {
  color: var(--primary-dark);
  background: var(--primary-soft);
}

.user-chip {
  padding: 0 10px 0 3px;
  color: var(--ink-soft);
  font-weight: var(--fw-semibold);
}

.user-chip:hover {
  background: color-mix(in srgb, var(--ink) 6%, transparent);
  color: var(--ink);
}

.user-avatar {
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--moss-soft);
  color: var(--moss-deep);
  font-family: var(--font-serif);
  font-size: var(--fs-md);
  font-weight: 600;
}

.user-name {
  max-width: 110px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── Paper canvas with a faint graph-paper band under the header ── */
.model-setup-banner {
  margin-bottom: 12px;
}

.main {
  position: relative;
  padding: 8px 32px 56px;
  overflow: auto;
  background: var(--bg);
  scroll-behavior: smooth;
}

.main::before {
  content: none;
}

.main > :deep(*) {
  position: relative;
}

.mask {
  position: fixed;
  inset: 0;
  z-index: 99;
  padding: 0;
  border: 0;
  background: rgba(31, 30, 29, 0.38);
  cursor: pointer;
}

@media (max-width: 1280px) {
  .main {
    padding: 6px 24px 40px;
  }

  .header {
    padding-right: 24px;
  }
}

@media (max-width: 768px) {
  .aside {
    position: fixed;
    top: 0;
    bottom: 0;
    left: 0;
    z-index: 100;
    width: 252px !important;
    box-shadow: var(--shadow-pop);
  }

  .aside.mobile-hidden {
    transform: translateX(-100%);
    visibility: hidden;
    pointer-events: none;
  }

  .mobile-close {
    margin-left: auto;
    color: var(--ink);
  }

  .user-name,
  .crumb-group,
  .crumb-sep {
    display: none;
  }

  .header {
    height: 56px;
    padding: 0 10px;
  }

  .header-left,
  .header-right {
    gap: 6px;
  }

  .class-chip {
    padding: 0 8px;
  }

  .user-chip {
    padding: 0 3px;
  }

  .class-name {
    max-width: 84px;
  }

  .guide-button {
    width: 34px;
    padding: 0;
  }

  .guide-button__label {
    display: none;
  }

  .main {
    padding: 8px 12px 28px;
  }

  .main:has(.grading-quick-dock) {
    margin-bottom: calc(78px + env(safe-area-inset-bottom, 0px));
  }

  .menu-toggle,
  .header-right {
    flex-shrink: 0;
  }
}
</style>
