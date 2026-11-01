<template>
  <div class="profile-page">
    <el-card class="profile-card teaching-card" shadow="never">
      <div class="teaching-heading">
        <div>
          <span class="teaching-eyebrow">数学 · 教学设置</span>
          <h2>{{ needsStageSetup ? '先选择您的教学学段' : '教学学段' }}</h2>
          <p>{{ needsStageSetup ? '只需在这里选择一次，其他页面自动跟随；无需填写年级或教材版本。' : '题库、统计、报告和新建任务统一跟随此设置；已有批次不受影响。' }}</p>
        </div>
        <el-button v-if="needsStageSetup" text :loading="loggingOut" @click="onLogout">退出登录</el-button>
      </div>
      <div class="stage-options" role="radiogroup" aria-label="教学学段（必填）" aria-required="true">
        <button
          v-for="item in EDUCATION_STAGES"
          :key="item.value"
          type="button"
          role="radio"
          class="stage-option"
          :class="{ active: selectedStage === item.value }"
          :aria-checked="selectedStage === item.value"
          :tabindex="selectedStage === item.value || (!selectedStage && item.value === EDUCATION_STAGES[0].value) ? 0 : -1"
          @click="selectedStage = item.value"
          @keydown="onStageKeydown($event, item.value)"
        >
          <span class="stage-option-title"><strong>{{ item.label }}</strong><el-icon v-if="selectedStage === item.value"><Check /></el-icon></span>
          <small>{{ item.description }}</small>
        </button>
      </div>
      <div class="teaching-footer">
        <span>各页面无需重复选择，批阅规则与知识点目录自动加载。</span>
        <el-button type="primary" :disabled="!selectedStage" :loading="savingStage" @click="onSaveStage">
          {{ needsStageSetup ? '保存并进入工作台' : '保存学段' }}
        </el-button>
      </div>
    </el-card>

    <el-card v-if="!needsStageSetup" class="profile-card theme-card" shadow="never">
      <template #header>
        <div class="card-heading">
          <span class="card-title">外观主题</span>
          <span class="card-subtitle">设置会保存在当前浏览器</span>
          <HelpButton title="外观主题" content="主题只影响当前浏览器的显示，不会改变批阅结果或其他老师的界面。" />
        </div>
      </template>
      <div class="theme-grid" role="radiogroup" aria-label="外观主题">
        <button
          v-for="item in themeOptions"
          :key="item.value"
          type="button"
          class="theme-option"
          :class="{ active: currentTheme === item.value }"
          :aria-checked="currentTheme === item.value"
          :tabindex="currentTheme === item.value ? 0 : -1"
          role="radio"
          @click="chooseTheme(item.value)"
          @keydown="onThemeKeydown($event, item.value)"
        >
          <span class="theme-swatch" :class="`swatch-${item.value}`">
            <span />
            <span />
          </span>
          <span class="theme-copy">
            <strong>{{ item.label }}</strong>
            <small>{{ item.description }}</small>
          </span>
          <el-icon v-if="currentTheme === item.value" class="theme-check"><Check /></el-icon>
        </button>
      </div>
      <div class="color-mode-heading">
        <strong>明暗模式</strong>
        <small>跟随系统会随操作系统的深浅色设置自动变化</small>
      </div>
      <div class="color-mode-grid" role="radiogroup" aria-label="明暗模式">
        <button
          v-for="item in colorModeOptions"
          :key="item.value"
          type="button"
          class="color-mode-option"
          :class="{ active: currentColorMode === item.value }"
          :aria-checked="currentColorMode === item.value"
          :tabindex="currentColorMode === item.value ? 0 : -1"
          role="radio"
          @click="chooseColorMode(item.value)"
          @keydown="onColorModeKeydown($event, item.value)"
        >
          <span class="color-mode-icon" :class="`mode-${item.value}`" aria-hidden="true">{{ item.icon }}</span>
          <span class="theme-copy">
            <strong>{{ item.label }}</strong>
            <small>{{ item.description }}</small>
          </span>
          <el-icon v-if="currentColorMode === item.value" class="theme-check"><Check /></el-icon>
        </button>
      </div>
    </el-card>

    <el-card v-if="!needsStageSetup" class="profile-card" shadow="never">
      <template #header>
        <span class="card-title">个人信息</span>
        <HelpButton title="个人信息" content="姓名和学校会用于界面与报告显示。手机号由账号系统管理，不能在这里修改。" />
      </template>

      <el-form :model="infoForm" label-width="80px" size="large">
        <el-form-item label="姓名">
          <el-input v-model="infoForm.name" placeholder="请输入姓名" />
        </el-form-item>
        <el-form-item label="学校">
          <el-input v-model="infoForm.school" placeholder="请输入学校" />
        </el-form-item>
        <el-form-item label="手机号">
          <el-input :model-value="auth.user?.phone" disabled />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="onSaveInfo">保存</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card v-if="!needsStageSetup" class="profile-card" shadow="never">
      <template #header>
        <span class="card-title">{{ auth.user?.has_password ? '修改密码' : '设置密码' }}</span>
        <HelpButton title="密码" content="设置密码后可使用手机号加密码登录。请使用至少 6 位且同时包含字母和数字的密码。" />
      </template>

      <el-form ref="pwdFormRef" :model="pwdForm" :rules="pwdRules" label-width="100px" size="large">
        <el-form-item v-if="auth.user?.has_password" label="当前密码" prop="oldPassword">
          <el-input v-model="pwdForm.oldPassword" type="password" show-password placeholder="请输入当前密码" />
        </el-form-item>
        <el-form-item label="新密码" prop="password">
          <el-input v-model="pwdForm.password" type="password" show-password placeholder="6-20位密码" />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirmPassword">
          <el-input v-model="pwdForm.confirmPassword" type="password" show-password placeholder="再次输入密码" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="settingPwd" @click="onSetPassword">
            {{ auth.user?.has_password ? '修改密码' : '设置密码' }}
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card v-if="desktopRuntime && !needsStageSetup" class="profile-card" shadow="never">
      <template #header>
        <div class="card-heading">
          <span class="card-title">Windows 本机加速</span>
          <el-tag :type="desktopBinding?.configured ? 'success' : 'info'" size="small">
            {{ desktopBinding?.configured ? '已绑定' : '未绑定' }}
          </el-tag>
          <HelpButton title="Windows 本机加速" content="绑定当前电脑后，本机可接收当前账号的图片预处理任务。解除绑定会停止该设备继续接收新任务。" />
        </div>
      </template>
      <div class="desktop-binding">
        <div class="desktop-binding__copy">
          <strong>{{ desktopBinding?.configured ? '本机已接入教师专属加速通道' : '绑定后，本机可处理当前账号的试卷预处理任务' }}</strong>
          <small v-if="desktopBinding?.configured">
            设备 {{ maskedDesktopNode }}<span v-if="desktopBinding.tokenExpiresAt"> · 凭据到期 {{ desktopCredentialExpiry }}</span>
          </small>
          <small v-else>凭据仅存入 Windows 凭据管理器，不会保存在网页或命令行。</small>
        </div>
        <el-button type="primary" :loading="desktopBindingLoading" @click="manageDesktopBinding">
          本机绑定管理
        </el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Check } from '@element-plus/icons-vue'
import api from '@/api'
import { useAuthStore } from '@/stores/auth'
import { useDesktopBindingDialog } from '@/composables/useDesktopBindingDialog'
import { getColorMode, getTheme, setColorMode, setTheme } from '@/composables/useTheme'
import HelpButton from '@/components/HelpButton.vue'
import { EDUCATION_STAGES, hasCompleteEducationProfile, normalizeEducationStage, safeProfileRedirect } from '@/utils/educationStage'

const auth = useAuthStore()
const bindingManager = useDesktopBindingDialog(auth)
const route = useRoute()
const router = useRouter()
const selectedStage = ref(normalizeEducationStage(auth.user?.education_stage))
const savingStage = ref(false)
const loggingOut = ref(false)
const needsStageSetup = computed(() => !hasCompleteEducationProfile(auth.user))

watch(() => [auth.user?.id, auth.user?.education_stage], () => {
  selectedStage.value = normalizeEducationStage(auth.user?.education_stage)
})

async function onSaveStage() {
  const stage = normalizeEducationStage(selectedStage.value)
  if (!stage) return
  const continueToWorkspace = needsStageSetup.value || route.query.setup === 'education-stage'
  savingStage.value = true
  try {
    const user = await auth.updateProfile({ education_stage: stage })
    if (!user) return
    if (!hasCompleteEducationProfile(user) || user.education_stage !== stage) {
      ElMessage.error('学段尚未保存，请重试')
      return
    }
    ElMessage.success('教学学段已保存')
    if (continueToWorkspace) await router.replace(safeProfileRedirect(route.query.redirect))
  } catch {
    /* error already shown by interceptor */
  } finally {
    savingStage.value = false
  }
}

async function onLogout() {
  loggingOut.value = true
  try {
    await auth.logout()
    await router.replace({ name: 'login' })
  } finally {
    loggingOut.value = false
  }
}

function onStageKeydown(event, value) {
  const keys = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End']
  if (!keys.includes(event.key)) return
  event.preventDefault()
  const current = EDUCATION_STAGES.findIndex(item => item.value === value)
  const direction = ['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 1
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? EDUCATION_STAGES.length - 1
    : (current + direction + EDUCATION_STAGES.length) % EDUCATION_STAGES.length
  selectedStage.value = EDUCATION_STAGES[next].value
  event.currentTarget.parentElement?.querySelectorAll('[role="radio"]')?.[next]?.focus()
}

const saving = ref(false)
const settingPwd = ref(false)
const pwdFormRef = ref(null)
const currentTheme = ref(getTheme())
const currentColorMode = ref(getColorMode())
const desktopRuntime = ref(false)
const desktopBindingLoading = ref(false)
const desktopBinding = ref(null)

const maskedDesktopNode = computed(() => {
  const value = desktopBinding.value?.nodeId || ''
  return value.length > 12 ? `${value.slice(0, 6)}…${value.slice(-4)}` : value
})

const desktopCredentialExpiry = computed(() => {
  const value = desktopBinding.value?.tokenExpiresAt
  if (!value) return ''
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '未知' : parsed.toLocaleDateString('zh-CN')
})

const themeOptions = [
  { value: 'song', label: '松间绿', description: '松针绿与清爽中性色' },
  { value: 'qing', label: '青墨', description: '青蓝色调与冷静表面' },
  { value: 'dai', label: '黛色', description: '黛蓝强调与柔和灰阶' },
]

const colorModeOptions = [
  { value: 'light', label: '浅色', description: '明亮纸面与清晰对比', icon: '☼' },
  { value: 'dark', label: '深色', description: '黑底与低干扰表面', icon: '☾' },
  { value: 'system', label: '跟随系统', description: '随操作系统自动切换', icon: '◐' },
]

const infoForm = reactive({
  name: '',
  school: '',
})

const pwdForm = reactive({
  oldPassword: '',
  password: '',
  confirmPassword: '',
})

const pwdRules = {
  oldPassword: auth.user?.has_password
    ? [{ required: true, message: '请输入当前密码', trigger: 'blur' }]
    : [],
  password: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, max: 20, message: '密码长度需6-20位', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请确认密码', trigger: 'blur' },
    {
      validator: (rule, value, callback) => {
        if (value !== pwdForm.password) {
          callback(new Error('两次输入的密码不一致'))
        } else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
}

async function desktopInvoke(command, args = {}) {
  const invoke = window.__TAURI_INTERNALS__?.invoke
  if (typeof invoke !== 'function') throw new Error('desktop_runtime_unavailable')
  return invoke(command, args)
}

async function loadDesktopBinding() {
  try {
    const runtime = await desktopInvoke('runtime_info')
    desktopRuntime.value = runtime?.runtime === 'desktop'
    if (desktopRuntime.value) {
      desktopBinding.value = await desktopInvoke('desktop_binding_status')
    }
  } catch {
    desktopRuntime.value = false
    desktopBinding.value = null
  }
}

async function manageDesktopBinding() {
  if (!auth.token) {
    ElMessage.error('登录已过期，请重新登录')
    return
  }
  desktopBindingLoading.value = true
  try {
    await bindingManager.open('profile')
    await loadDesktopBinding()
  } finally {
    desktopBindingLoading.value = false
  }
}

onMounted(async () => {
  if (auth.user) {
    infoForm.name = auth.user.name || ''
    infoForm.school = auth.user.school || ''
  }
  await loadDesktopBinding()
})

async function onSaveInfo() {
  saving.value = true
  try {
    const user = await auth.updateProfile({
      name: infoForm.name,
      school: infoForm.school,
    })
    if (!user) return
    ElMessage.success('保存成功')
  } catch {
    /* error already shown by interceptor */
  } finally {
    saving.value = false
  }
}

function chooseTheme(theme) {
  currentTheme.value = setTheme(theme)
}

function chooseColorMode(mode) {
  currentColorMode.value = setColorMode(mode)
}

function onThemeKeydown(event, theme) {
  const navigationKeys = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End']
  if (!navigationKeys.includes(event.key)) return

  event.preventDefault()
  const currentIndex = themeOptions.findIndex(item => item.value === theme)
  let nextIndex = currentIndex

  if (event.key === 'Home') nextIndex = 0
  else if (event.key === 'End') nextIndex = themeOptions.length - 1
  else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
    nextIndex = (currentIndex + 1) % themeOptions.length
  } else {
    nextIndex = (currentIndex - 1 + themeOptions.length) % themeOptions.length
  }

  chooseTheme(themeOptions[nextIndex].value)
  const options = event.currentTarget.parentElement?.querySelectorAll('[role="radio"]')
  options?.[nextIndex]?.focus()
}

function onColorModeKeydown(event, mode) {
  const navigationKeys = ['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End']
  if (!navigationKeys.includes(event.key)) return

  event.preventDefault()
  const currentIndex = colorModeOptions.findIndex(item => item.value === mode)
  let nextIndex = currentIndex

  if (event.key === 'Home') nextIndex = 0
  else if (event.key === 'End') nextIndex = colorModeOptions.length - 1
  else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
    nextIndex = (currentIndex + 1) % colorModeOptions.length
  } else {
    nextIndex = (currentIndex - 1 + colorModeOptions.length) % colorModeOptions.length
  }

  chooseColorMode(colorModeOptions[nextIndex].value)
  const options = event.currentTarget.parentElement?.querySelectorAll('[role="radio"]')
  options?.[nextIndex]?.focus()
}

async function onSetPassword() {
  try {
    await pwdFormRef.value.validate()
  } catch {
    return
  }
  settingPwd.value = true
  try {
    await api.auth.setPassword(pwdForm.password, pwdForm.oldPassword || undefined)
    ElMessage.success(auth.user?.has_password ? '密码修改成功' : '密码设置成功')
    // Refresh user info to update has_password
    await auth.fetchMe()
    pwdForm.oldPassword = ''
    pwdForm.password = ''
    pwdForm.confirmPassword = ''
  } catch {
    /* error already shown by interceptor */
  } finally {
    settingPwd.value = false
  }
}
</script>

<style scoped>
.profile-page {
  max-width: 760px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.profile-card {
  border-radius: var(--card-radius);
  border: 1px solid var(--line);
  box-shadow: var(--shadow-sm);
  animation: ma-rise-in var(--dur-4) var(--ease-emphasized) backwards;
}

.profile-card:nth-child(2) {
  animation-delay: 40ms;
}

.profile-card:nth-child(n + 3) {
  animation-delay: 80ms;
}

.teaching-heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}

.teaching-eyebrow { color: var(--primary); font-size: 12px; font-weight: 650; }
.teaching-heading h2 { margin: 7px 0; color: var(--ink); font-size: 22px; }
.teaching-heading p { margin: 0; color: var(--ink-muted); line-height: 1.6; font-size: 13px; }
.stage-options { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 20px 0; }
.stage-option { min-width: 0; padding: 18px; border: 1px solid var(--line); border-radius: var(--radius-lg); background: var(--card); color: var(--ink); font: inherit; text-align: left; cursor: pointer; transition: border-color var(--dur-2) var(--ease-out), background-color var(--dur-2) var(--ease-out), box-shadow var(--dur-3) var(--ease-out), transform var(--dur-1) var(--ease-out); }
.stage-option:hover { border-color: color-mix(in srgb, var(--primary) 35%, var(--line)); box-shadow: var(--shadow-sm); }
.stage-option:active { transform: scale(0.985); }
.stage-option.active { border-color: var(--primary); background: var(--primary-soft, var(--surface-subtle)); box-shadow: 0 0 0 1px var(--primary); }
.stage-option:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; }
.stage-option-title { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: var(--fs-lg); font-weight: var(--fw-semibold); }
.stage-option-title .el-icon { color: var(--primary); }
.stage-option small { display: block; margin-top: 8px; color: var(--ink-muted); line-height: 1.6; font-size: 12px; }
.teaching-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.teaching-footer > span { color: var(--ink-muted); font-size: 12px; }

.card-title {
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
  color: var(--ink);
}

.card-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
}

.card-subtitle {
  color: var(--ink-muted);
  font-size: 12px;
}

.desktop-binding {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
}

.desktop-binding__copy {
  display: grid;
  gap: 6px;
}

.desktop-binding__copy strong {
  color: var(--ink);
  font-size: 15px;
}

.desktop-binding__copy small {
  color: var(--ink-muted);
  line-height: 1.5;
}

.desktop-binding__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.theme-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.theme-option {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  padding: 14px;
  color: var(--ink);
  text-align: left;
  cursor: pointer;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--card);
  transition: border-color 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease;
}

.theme-option:hover {
  border-color: var(--primary-light);
  transform: translateY(-1px);
}

.theme-option.active {
  border-color: var(--primary);
  box-shadow: var(--focus-ring);
}

.theme-swatch {
  display: flex;
  align-items: flex-end;
  gap: 6px;
  height: 42px;
  padding: 8px;
  overflow: hidden;
  border-radius: var(--radius-sm);
  background: #ebe7dd;
}

.theme-swatch span:first-child {
  width: 28%;
  height: 100%;
  border-radius: 5px;
  background: #3f6b4f;
}

.theme-swatch span:last-child {
  flex: 1;
  height: 60%;
  border-radius: 5px;
  background: #fbfaf7;
}

.swatch-qing {
  background: #ebe7dd;
}

.swatch-qing span:first-child {
  background: #2f6b6e;
}

.swatch-dai {
  background: #ebe7dd;
}

.swatch-dai span:first-child {
  background: #46507c;
}

.theme-copy {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.theme-copy strong {
  font-size: 14px;
}

.theme-copy small {
  color: var(--ink-muted);
  line-height: 1.4;
}

.theme-check {
  position: absolute;
  top: 12px;
  right: 12px;
  display: grid;
  width: 22px;
  height: 22px;
  color: var(--on-primary);
  border-radius: 50%;
  background: var(--primary);
  place-items: center;
}

.color-mode-heading {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 16px;
  margin-top: 22px;
  margin-bottom: 10px;
}

.color-mode-heading strong {
  color: var(--ink);
  font-size: 14px;
}

.color-mode-heading small {
  color: var(--ink-muted);
  font-size: 12px;
}

.color-mode-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.color-mode-option {
  position: relative;
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 10px;
  padding: 12px;
  color: var(--ink);
  text-align: left;
  cursor: pointer;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--card);
  transition: border-color 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease;
}

.color-mode-option:hover {
  border-color: var(--primary-light);
  transform: translateY(-1px);
}

.color-mode-option.active {
  border-color: var(--primary);
  box-shadow: var(--focus-ring);
}

.color-mode-icon {
  display: grid;
  flex: 0 0 32px;
  width: 32px;
  height: 32px;
  color: var(--primary);
  font-size: 20px;
  border: 1px solid var(--border);
  border-radius: 50%;
  background: var(--surface-subtle);
  place-items: center;
}

.color-mode-option .theme-copy {
  min-width: 0;
}

.color-mode-option .theme-copy strong,
.color-mode-option .theme-copy small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 640px) {
  .stage-options { grid-template-columns: 1fr; gap: 8px; margin: 16px 0; }
  .stage-option { padding: 14px; }
  .teaching-footer { align-items: stretch; flex-direction: column; }
  .teaching-heading h2 { font-size: 20px; }
  .theme-grid,
  .color-mode-grid {
    grid-template-columns: 1fr;
  }

  .color-mode-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }

  .card-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }

  .desktop-binding {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
