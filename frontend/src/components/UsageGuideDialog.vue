<template>
  <el-dialog
    v-model="visible"
    title="使用引导 · 本机处理检查"
    width="min(760px, calc(100vw - 24px))"
    class="usage-guide-dialog"
    :close-on-click-modal="!desktopSetupRequired"
    :close-on-press-escape="!desktopSetupRequired"
    :show-close="!desktopSetupRequired"
    :before-close="beforeClose"
  >
    <section class="guide-panel" aria-labelledby="guide-desktop-title">
      <div class="guide-heading">
        <div>
          <h3 id="guide-desktop-title">检测并绑定这台电脑的本机处理器</h3>
          <p>客户端先做正确性与性能自检：合格的 NVIDIA GPU 优先，否则使用通过门槛的 CPU；两者都不合格时不会强行启用。</p>
        </div>
      </div>

      <div v-loading="desktopLoading" class="desktop-setup">
        <div class="desktop-status-grid">
          <article>
            <span>设备授权</span>
            <el-tag :type="bindingConfigured ? 'success' : 'warning'" effect="plain">
              {{ bindingConfigured ? '已绑定' : '待绑定' }}
            </el-tag>
            <p>{{ bindingConfigured ? '本机凭据保存在 Windows 凭据管理器中，不会显示在网页。' : '需要使用当前登录账号授权这台电脑接收本地预处理任务。' }}</p>
          </article>
          <article>
            <span>实际处理器</span>
            <el-tag :type="processorTagType" effect="plain">{{ processorLabel }}</el-tag>
            <p>{{ processorDescription }}</p>
          </article>
        </div>

        <el-alert
          v-if="desktopError"
          type="error"
          :closable="false"
          show-icon
          :title="desktopError"
        />
        <el-alert
          v-else-if="desktopWorkerStatus?.handoff?.lastErrorCode === 'node_auth_failed'"
          type="warning"
          :closable="false"
          show-icon
          title="本机授权已失效，请重新绑定后再继续。"
        />
        <el-alert
          v-else-if="processorReady"
          type="success"
          :closable="false"
          show-icon
          :title="`${processorLabel} 已通过自检，本机处理可用。`"
        />
        <el-alert
          v-else
          type="warning"
          :closable="false"
          show-icon
          :title="processorUnavailableReason"
        />

        <div class="desktop-actions">
          <el-button type="primary" :loading="desktopLoading" @click="bindLocalProcessor">
            本机绑定管理
          </el-button>
          <el-button
            v-if="!processorReady && bindingAttempted && runtimeInfo?.webFallbackConfigured"
            :loading="desktopLoading"
            @click="openWebFallback"
          >
            打开网页版继续
          </el-button>
          <el-button
            v-if="!processorReady && bindingAttempted"
            :loading="desktopLoading"
            @click="acceptCloudFallback"
          >
            先由云端接管，稍后重试
          </el-button>
        </div>
        <p class="desktop-footnote">本机只处理受控的图像预处理任务；服务器仍会校验候选结果。GPU 异常时自动回退 CPU，CPU 不满足门槛时由云端接管。</p>
      </div>
    </section>

    <template #footer>
      <div class="guide-footer">
        <span class="guide-footer__spacer" />
        <el-button
          type="primary"
          :disabled="desktopSetupRequired && !desktopSetupResolved"
          @click="completeGuide"
        >
          {{ tourAfterSetup ? '下一步：操作引导' : '完成引导' }}
        </el-button>
      </div>
    </template>
  </el-dialog>
  <DesktopBindingDialog />
  <GuidedTour ref="tourRef" @finish="onTourFinish" />
</template>

<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import DesktopBindingDialog from '@/components/DesktopBindingDialog.vue'
import GuidedTour from '@/components/GuidedTour.vue'
import { formatDesktopBindingError, useDesktopBindingDialog } from '@/composables/useDesktopBindingDialog'
import { buildUsageTourSteps } from '@/utils/usageTour'

// v3: the spotlight walkthrough replaced the text manual; every teacher sees it once.
const WEB_GUIDE_KEY = 'mathagent_usage_guide_v3'
const DESKTOP_SETUP_KEY = 'mathagent_desktop_setup_v1'

const auth = useAuthStore()
const bindingManager = useDesktopBindingDialog(auth)
const route = useRoute()
const visible = ref(false)
const tourRef = ref(null)
// The desktop processor check runs first; the walkthrough follows when this is set.
const tourAfterSetup = ref(false)
const initialized = ref(false)
const desktopRuntime = ref(false)
const runtimeInfo = ref(null)
const desktopBinding = ref(null)
const desktopWorkerStatus = ref(null)
const desktopLoading = ref(false)
const desktopError = ref('')
const bindingAttempted = ref(false)
const desktopSetupResolved = ref(false)
const desktopSetupPending = ref(false)

const desktopSetupRequired = computed(() => desktopRuntime.value && desktopSetupPending.value)
const bindingConfigured = computed(() => desktopBinding.value?.configured === true)
const handoffReady = computed(() => (
  desktopWorkerStatus.value?.handoff?.configured === true
  && desktopWorkerStatus.value?.handoff?.running === true
  && desktopWorkerStatus.value?.handoff?.connected === true
  && desktopWorkerStatus.value?.handoff?.lastErrorCode !== 'node_auth_failed'
))
const processorReady = computed(() => (
  bindingConfigured.value
  && handoffReady.value
  && desktopWorkerStatus.value?.localProcessingAvailable === true
  && desktopWorkerStatus.value?.paused !== true
))
const processorLabel = computed(() => {
  const status = desktopWorkerStatus.value
  if (!status) return '尚未检测'
  if (status.paused === true) return '已暂停'
  if (status.computeBackend === 'windows_nvidia_gpu') return 'NVIDIA GPU'
  if (status.computeBackend === 'windows_cpu_opencv') return 'CPU'
  return '暂不可用'
})
const processorTagType = computed(() => {
  if (processorLabel.value === 'NVIDIA GPU') return 'success'
  if (processorLabel.value === 'CPU') return 'info'
  if (processorLabel.value === '已暂停') return 'warning'
  return 'danger'
})
const processorDescription = computed(() => {
  const status = desktopWorkerStatus.value
  if (!status) return '点击下方按钮读取本机 GPU 与 CPU 自检结果。'
  if (status.paused === true) return '本机处理已暂停，重新检测时会恢复。'
  if (status.computeBackend === 'windows_nvidia_gpu') {
    return `${status.gpuAcceleration?.deviceName || 'NVIDIA GPU'} 已通过正确性与速度自检。`
  }
  if (status.computeBackend === 'windows_cpu_opencv') {
    return `CPU 已通过性能与内存门槛，可用并发 ${status.cpuAcceleration?.workerCount || status.workerCount || 0}。`
  }
  return 'GPU 与 CPU 当前都未达到本地处理门槛，将由云端接管。'
})
const processorUnavailableReason = computed(() => {
  const status = desktopWorkerStatus.value
  if (!status) return '尚未完成本机处理器检测。'
  if (!bindingConfigured.value) return '请先授权这台电脑，再确认实际使用 GPU 还是 CPU。'
  if (status.handoff?.lastErrorCode === 'node_auth_failed') return '设备授权已失效，请重新绑定后继续。'
  if (status.handoff?.configured !== true) return '本机任务通道尚未配置完成，请重新检测。'
  if (status.handoff?.connected !== true) return '本机任务通道正在连接服务器，请稍候重新检测。'
  const code = status.gpuAcceleration?.fallbackReason
    || status.cpuAcceleration?.reasonCodes?.[0]
    || status.reasonCodes?.[0]
  const labels = {
    gpu_runtime_missing: '未检测到可用的 NVIDIA 加速组件；系统将继续检查 CPU。',
    gpu_known_answer_failed: 'GPU 正确性自检失败；系统不会启用不可靠的 GPU。',
    gpu_known_answer_timeout: 'GPU 自检超时；系统不会启用不确定的 GPU。',
    gpu_known_answer_mismatch: 'GPU 自检结果不一致；系统不会启用该 GPU。',
    gpu_geometry_contract_mismatch: 'GPU 几何结果未达到安全门槛；系统不会启用该 GPU。',
    gpu_not_faster_than_cpu: 'GPU 没有快于本机 CPU，系统将优先使用 CPU。',
    preprocessor_unavailable: '本地图片处理组件不可用，请重新检测或改用网页版。',
    insufficient_physical_cores: 'CPU 物理核心数未达到本地处理门槛。',
    insufficient_available_memory: '当前可用内存未达到本地处理门槛。',
    minimum_worker_capacity_unmet: 'CPU 可用并发未达到本地处理门槛。',
    cpu_busy: 'CPU 当前负载过高，请稍后重新检测。',
    user_paused: '本机处理已暂停，重新检测时会恢复。',
  }
  return labels[code] || '本机处理器暂不可用；可以稍后重试或使用网页版。'
})

function desktopInvoke(command, args = {}) {
  const invoke = window.__TAURI_INTERNALS__?.invoke
  if (typeof invoke !== 'function') throw new Error('desktop_runtime_unavailable')
  return invoke(command, args)
}

function accountStorageKey(prefix) {
  const identity = auth.user?.id ?? auth.user?.username ?? 'current-account'
  return `${prefix}:${identity}`
}

function storageHas(key) {
  try {
    return localStorage.getItem(key) === 'true'
  } catch {
    return false
  }
}

function storageValue(key) {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function storageSet(key) {
  try {
    localStorage.setItem(key, 'true')
  } catch {
    // The guide remains available from the header when storage is unavailable.
  }
}

function storageSetValue(key, value) {
  try {
    localStorage.setItem(key, value)
  } catch {
    // A failed write keeps desktop setup pending on the next launch.
  }
}

function desktopSetupMatchesBinding() {
  const nodeId = desktopBinding.value?.nodeId
  const storedResolution = storageValue(accountStorageKey(DESKTOP_SETUP_KEY))
  const cloudFallbackStillValid = storedResolution === 'cloud'
    && (
      desktopWorkerStatus.value?.webFallbackRequired === true
      || desktopWorkerStatus.value?.statusReadFailed === true
    )
  return cloudFallbackStillValid
    || (
      typeof nodeId === 'string'
      && nodeId.length > 0
      && storedResolution === nodeId
    )
}

function formatDesktopError(error) {
  return formatDesktopBindingError(error)
}

async function loadDesktopContext() {
  desktopError.value = ''
  let runtime
  try {
    runtime = await desktopInvoke('runtime_info')
  } catch {
    desktopRuntime.value = false
    runtimeInfo.value = null
    initialized.value = true
    return
  }
  desktopRuntime.value = runtime?.runtime === 'desktop'
  runtimeInfo.value = runtime
  if (!desktopRuntime.value) {
    initialized.value = true
    return
  }

  try {
    desktopBinding.value = await desktopInvoke('desktop_binding_status')
  } catch (error) {
    desktopBinding.value = { configured: false }
    desktopError.value = formatDesktopError(error)
  }
  try {
    desktopWorkerStatus.value = await desktopInvoke('get_worker_status')
  } catch (error) {
    desktopWorkerStatus.value = {
      computeBackend: 'none',
      localProcessingAvailable: false,
      webFallbackRequired: true,
      statusReadFailed: true,
      reasonCodes: [],
      gpuAcceleration: { fallbackReason: null },
      cpuAcceleration: { reasonCodes: [] },
    }
    desktopError.value = formatDesktopError(error)
  }
  desktopSetupResolved.value = processorReady.value
  initialized.value = true
}

async function initialize() {
  if (!initialized.value) await loadDesktopContext()
  const webGuidePending = !storageHas(accountStorageKey(WEB_GUIDE_KEY))
  desktopSetupPending.value = desktopRuntime.value
    && !desktopSetupMatchesBinding()
  if (desktopSetupPending.value) desktopSetupResolved.value = false
  const processorNeedsAttention = desktopRuntime.value
    && (
      !bindingConfigured.value
      || !handoffReady.value
      || (
        desktopWorkerStatus.value?.webFallbackRequired === true
        && desktopWorkerStatus.value?.paused !== true
      )
    )
  const disclaimerPendingOnHome = route.path === '/' && !storageHas('disclaimer_agreed')

  if (disclaimerPendingOnHome) return

  if (desktopSetupPending.value || processorNeedsAttention) {
    openDesktopSetup(webGuidePending)
    return
  }
  if (webGuidePending && storageHas('disclaimer_agreed')) startTour()
}

async function open() {
  if (!initialized.value || desktopRuntime.value) await loadDesktopContext()
  desktopSetupPending.value = desktopRuntime.value
    && !desktopSetupMatchesBinding()
  if (desktopSetupPending.value) desktopSetupResolved.value = false
  if (desktopRuntime.value) openDesktopSetup(true)
  else startTour()
}

function openDesktopSetup(thenTour) {
  tourAfterSetup.value = thenTour
  visible.value = true
}

function startTour() {
  const steps = buildUsageTourSteps({ classes: auth.classes, currentClassId: auth.currentClassId })
  // Not awaited: callers (the ?guide=1 handler) must not wait on the whole walkthrough.
  tourRef.value?.start(steps)
}

function onTourFinish(reason) {
  storageSet(accountStorageKey(WEB_GUIDE_KEY))
  if (reason === 'skip') ElMessage.info('已跳过引导，可随时点右上角「使用引导」重看。')
}

async function bindLocalProcessor() {
  if (!auth.token) {
    bindingAttempted.value = true
    desktopError.value = '登录已过期，请重新登录后绑定本机。'
    return
  }
  desktopLoading.value = true
  desktopError.value = ''
  try {
    // Closing the manager returns to this exact step; status alone is not
    // proof that a credential belongs to the teacher completing first setup.
    const outcome = await bindingManager.open('guide')
    bindingAttempted.value = bindingAttempted.value || outcome.attempted
    desktopBinding.value = outcome.binding
    desktopWorkerStatus.value = outcome.worker
    desktopError.value = outcome.error
    desktopSetupResolved.value = outcome.verified && !outcome.error && processorReady.value
  } finally {
    desktopLoading.value = false
  }
}

async function openWebFallback() {
  desktopLoading.value = true
  try {
    await desktopInvoke('open_web_fallback')
    desktopSetupResolved.value = true
    ElMessage.success('已打开固定的 MathAgent 网页版入口')
  } catch {
    desktopError.value = '暂时无法打开网页版，请联系管理员确认固定入口。'
  } finally {
    desktopLoading.value = false
  }
}

function acceptCloudFallback() {
  desktopSetupResolved.value = true
  ElMessage.info('已记录：本机暂不处理任务，可稍后从“使用引导”重新检测。')
}

function completeGuide() {
  if (desktopSetupRequired.value && !desktopSetupResolved.value) return
  // Continuing past an optional, unresolved check (setup already recorded for
  // this binding) must not overwrite the stored resolution.
  if (desktopRuntime.value && desktopSetupResolved.value) {
    const nodeId = desktopBinding.value?.nodeId
    const resolution = typeof nodeId === 'string' && nodeId.length > 0 ? nodeId : 'cloud'
    storageSetValue(accountStorageKey(DESKTOP_SETUP_KEY), resolution)
    desktopSetupPending.value = false
  }
  visible.value = false
  if (tourAfterSetup.value) startTour()
}

function beforeClose(done) {
  if (desktopSetupRequired.value) {
    ElMessage.warning('请先完成本机处理器检测，或确认暂由云端接管。')
    return
  }
  done()
}

defineExpose({ initialize, open })
</script>

<style scoped>
:global(.usage-guide-dialog) {
  display: flex;
  flex-direction: column;
  max-height: 94vh;
  margin-top: 3vh !important;
}

:global(.usage-guide-dialog .el-dialog__body) {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.guide-heading {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
}

.guide-heading h3,
.guide-heading p,
.desktop-status-grid p,
.desktop-footnote {
  margin: 0;
}

.guide-heading h3 {
  color: var(--ink);
  font-size: 16px;
  line-height: 1.4;
}

.guide-heading p {
  margin-top: 4px;
  color: var(--ink-muted);
  font-size: 12px;
  line-height: 1.6;
}

.desktop-setup {
  display: grid;
  gap: 14px;
}

.desktop-status-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.desktop-status-grid article {
  min-height: 124px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--card);
}

.desktop-status-grid article > span:first-child {
  display: block;
  margin-bottom: 10px;
  color: var(--ink-muted);
  font-size: 12px;
}

.desktop-status-grid p {
  margin-top: 9px;
  color: var(--ink-muted);
  font-size: 12px;
  line-height: 1.55;
}

.desktop-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.desktop-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.desktop-footnote {
  color: var(--ink-muted);
  font-size: 12px;
  line-height: 1.65;
}

.guide-footer {
  display: flex;
  align-items: center;
  width: 100%;
}

.guide-footer__spacer {
  flex: 1;
}

@media (max-width: 640px) {
  .desktop-status-grid {
    grid-template-columns: 1fr;
  }
}
</style>
