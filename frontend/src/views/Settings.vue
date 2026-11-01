<template>
  <div class="settings-page">
    <div class="page-head">
      <h2>系统设置</h2>
      <el-tag type="info" size="small">仅管理员可见 · 配置小M 聊天模型</el-tag>
    </div>

    <el-card shadow="never" class="block">
      <template #header>
        <div class="card-head">
          <span>批阅模型密钥</span>
          <el-tag :type="profilesReady ? 'success' : 'danger'" size="small">
            {{ profilesReady ? '已配置' : '未配置' }}
          </el-tag>
          <HelpButton
            title="批阅模型密钥"
            content="批阅、识图、模板识别都走阿里云 DashScope。简单模式只有两个槽：模板处理、学生卷转录与批阅。同一个 Key 填两处即可跑通全流程；需要给不同职责配不同 Key 时再展开专家模式。密钥只显示掩码，填新值才会覆盖。"
          />
        </div>
      </template>

      <el-alert
        v-if="profilesLoaded && !profilesReady"
        type="warning"
        :closable="false"
        show-icon
        title="先填下面两项：模板处理、学生卷转录与批阅。没有它们，上传试卷后无法识别与判分。"
        class="profile-alert"
      />

      <el-form label-width="140px" class="form">
        <template v-for="slot in simpleSlots" :key="slot.key">
          <el-divider content-position="left">{{ slot.label }}</el-divider>
          <el-form-item label="API Key">
            <el-input
              v-model="simpleForm[slot.key].api_key"
              type="password"
              show-password
              :placeholder="profileSimple[slot.key]?.api_key_masked || '粘贴阿里云 DashScope Key；留空不变更'"
            />
            <div class="tip">
              当前：{{ profileSimple[slot.key]?.api_key_masked || '未设置' }}
              （来源：{{ sourceLabel(profileSimple[slot.key]?.source) }}）
            </div>
          </el-form-item>
          <el-form-item label="模型">
            <el-input
              v-model="simpleForm[slot.key].model"
              :placeholder="profileSimple[slot.key]?.model || 'qwen3.8-flash'"
            />
            <div class="tip">留空则用默认线路，一般不用改。</div>
          </el-form-item>
          <el-form-item>
            <el-button
              size="small"
              :loading="testingGroup === slot.testGroup"
              @click="onTestProfile(slot.testGroup)"
            >
              测试连接
            </el-button>
          </el-form-item>
        </template>

        <el-form-item>
          <el-button type="primary" :loading="savingProfiles" @click="onSaveSimpleProfiles">
            保存两项
          </el-button>
          <el-button text @click="showExpert = !showExpert">
            {{ showExpert ? '收起专家模式' : '展开专家模式（三个职责组）' }}
          </el-button>
        </el-form-item>
      </el-form>

      <div v-if="showExpert" class="expert-block">
        <el-divider>专家模式</el-divider>
        <div class="tip expert-tip">
          每个职责组单独配置，留空表示继承下一层（全局环境变量 → 默认值）。
          并发数只在你的账号额度有限时需要调小。
        </div>
        <el-form
          v-for="group in profileGroups"
          :key="group.group"
          label-width="140px"
          class="form expert-row"
        >
          <el-divider content-position="left">{{ group.label }}</el-divider>
          <el-form-item label="API Key">
            <el-input
              v-model="expertForm[group.group].api_key"
              type="password"
              show-password
              :placeholder="group.api_key_masked || '留空则继承下一层'"
            />
            <div class="tip">
              来源：{{ sourceLabel(group.source) }} · 模型 {{ group.model }} · 并发 {{ group.concurrency }}
            </div>
          </el-form-item>
          <el-form-item label="地址">
            <el-input v-model="expertForm[group.group].base_url" placeholder="base_url（留空继承）" />
          </el-form-item>
          <el-form-item label="模型">
            <el-input v-model="expertForm[group.group].model" placeholder="model（留空继承）" />
          </el-form-item>
          <el-form-item label="并发数">
            <el-input-number v-model="expertForm[group.group].concurrency" :min="1" :max="64" />
          </el-form-item>
          <el-form-item>
            <el-button size="small" :loading="savingGroup === group.group" @click="onSaveExpertProfile(group.group)">
              保存本组
            </el-button>
            <el-button size="small" :loading="testingGroup === group.group" @click="onTestProfile(group.group)">
              测试连接
            </el-button>
          </el-form-item>
        </el-form>
      </div>

      <el-alert
        v-if="profileTestResult"
        :type="profileTestResult.success ? 'success' : 'error'"
        :title="profileTestResult.success ? (profileTestResult.response || '连接正常') : (profileTestResult.error || '连接失败')"
        :closable="false"
        show-icon
        class="profile-alert"
      />
    </el-card>

    <el-card shadow="never" class="block">
      <template #header>
        <div class="card-head">
          <span>教师账号</span>
          <HelpButton
            title="教师账号"
            content="没有开通短信或微信登录时，由管理员在这里直接给老师建号：填姓名和初始密码，手机号可留空（系统会生成一个 199 开头的登录账号）。建好后把「登录账号 + 初始密码」线下交给老师，老师登录后可自行改密。"
          />
        </div>
      </template>

      <el-form label-width="140px" class="form">
        <el-form-item label="姓名">
          <el-input v-model="teacherForm.name" maxlength="50" placeholder="老师姓名" />
        </el-form-item>
        <el-form-item label="手机号">
          <el-input v-model="teacherForm.phone" maxlength="11" placeholder="选填；留空自动生成登录账号" />
        </el-form-item>
        <el-form-item label="学校">
          <el-input v-model="teacherForm.school" maxlength="100" placeholder="选填" />
        </el-form-item>
        <el-form-item label="初始密码">
          <el-input
            v-model="teacherForm.password"
            type="password"
            show-password
            maxlength="20"
            placeholder="6-20 位，至少包含字母和数字"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="creatingTeacher" @click="onCreateTeacher">创建账号</el-button>
        </el-form-item>
      </el-form>

      <el-alert
        v-if="createdTeacher"
        type="success"
        :closable="false"
        show-icon
        :title="`已创建：${createdTeacher.user.name}，登录账号 ${createdTeacher.login_account}${createdTeacher.generated_account ? '（系统生成）' : ''}。请把账号和初始密码线下交给老师。`"
      />
    </el-card>

    <el-card shadow="never" class="block" v-loading="loading">
      <template #header>
        <div class="card-head">
          <span>小M 模型配置</span>
          <el-tag :type="isConfigured ? 'success' : 'danger'" size="small">
            {{ isConfigured ? '已配置' : '未配置' }}
          </el-tag>
          <HelpButton
            title="小M 模型配置"
            content="这里配置小M 聊天的通用接口（LLM_*）；服务器另配了教师聊天模型（TEACHER_LLM_*）时以服务器为准。批阅、识图和报告走 Qwen，不受这里影响。修改后先测试连接，确认成功再保存；密钥不会回填显示。"
          />
        </div>
      </template>

      <el-form :model="form" label-width="120px" class="form">
        <el-form-item label="API 地址">
          <el-input v-model="form.base_url" placeholder="OpenAI 兼容接口地址，以 /v1 结尾" />
          <div class="tip">换服务商时修改</div>
        </el-form-item>

        <el-form-item label="API Key">
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            placeholder="小M 烧光或更换时，粘贴新 key 后保存"
          />
          <div class="tip">
            当前：{{ maskedKey || '未设置' }}。粘贴新 key 即替换；留空不变更。
          </div>
        </el-form-item>

        <el-form-item label="模型">
          <el-input v-model="form.model" placeholder="服务商提供的模型名" />
          <div class="tip">换模型时修改</div>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
          <el-button :loading="testing" :disabled="testing" @click="onTest">{{ testing ? '测试中...' : '测试连接' }}</el-button>
          <el-button text @click="load">重新加载</el-button>
        </el-form-item>

        <el-form-item v-if="testResult" label="测试结果">
          <el-alert
            :type="testResult.success ? 'success' : 'error'"
            :title="testResult.success ? (testResult.response || '连接正常') : (testResult.error || '连接失败')"
            :closable="false"
            show-icon
          />
        </el-form-item>
      </el-form>
    </el-card>

    <el-card v-if="desktopRuntime" shadow="never" class="block desktop-acceleration-card">
      <template #header>
        <div class="card-head">
          <span>本地加速</span>
          <el-tag :type="desktopAccelerationTagType" size="small">
            {{ desktopAccelerationLabel }}
          </el-tag>
          <HelpButton
            title="本地加速"
            content="Windows 客户端可处理部分图片预处理任务。节点不可用时系统会回退网页版，重新检测只读取状态，不会删除已保存会话。"
          />
        </div>
      </template>
      <div v-loading="desktopLoading" class="desktop-status">
        <div class="desktop-status__primary">{{ desktopAccelerationDescription }}</div>
        <div v-if="desktopWorkerStatus?.gpuAcceleration?.gpuTimeMs" class="tip">
          GPU 启动自检 {{ desktopWorkerStatus.gpuAcceleration.gpuTimeMs.toFixed(1) }} ms；异常时自动回退 CPU。
        </div>
        <div v-if="desktopFallbackReason" class="tip">
          当前回退原因：{{ desktopFallbackReason }}
        </div>
        <div v-if="desktopWorkerStatus?.handoff?.configured" class="desktop-progress">
          <span>已完成 {{ desktopWorkerStatus.handoff.completed }}</span>
          <span>失败 {{ desktopWorkerStatus.handoff.failed }}</span>
          <span>剩余 {{ desktopWorkerStatus.handoff.remaining }}</span>
          <span v-if="desktopWorkerStatus.handoff.processing">本地处理中 {{ desktopWorkerStatus.handoff.processing }}</span>
        </div>
        <div v-if="desktopWorkerStatus?.handoff?.lastErrorCode" class="tip">
          加速节点状态：{{ desktopHandoffMessage }}
        </div>
        <el-button
          type="primary"
          size="small"
          :loading="desktopLoading"
          @click="repairDesktopBinding"
        >
          {{ desktopAuthorizationExpired ? '重新绑定' : '本机绑定管理' }}
        </el-button>
        <el-button size="small" :loading="desktopLoading" @click="loadDesktopAcceleration">
          重新检测
        </el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'
import { useAuthStore } from '@/stores/auth'
import { useDesktopBindingDialog } from '@/composables/useDesktopBindingDialog'
import HelpButton from '@/components/HelpButton.vue'

const auth = useAuthStore()
const bindingManager = useDesktopBindingDialog(auth)
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const isConfigured = ref(false)
const maskedKey = ref('')
const testResult = ref(null)
const desktopRuntime = ref(false)
const desktopLoading = ref(false)
const desktopWorkerStatus = ref(null)

// ---- 批阅模型密钥（简单模式两槽 / 专家模式三组） ----
const simpleSlots = [
  { key: 'template', label: '模板定位与解题', testGroup: 'template_solve' },
  { key: 'student', label: '学生卷转录与批阅（含题库分类）', testGroup: 'student_grade' },
]
const simpleForm = reactive({
  template: { api_key: '', model: '' },
  student: { api_key: '', model: '' },
})
const expertForm = reactive({})
const profileSimple = ref({})
const profileGroups = ref([])
const profilesReady = ref(false)
const profilesLoaded = ref(false)
const showExpert = ref(false)
const savingProfiles = ref(false)
const savingGroup = ref('')
const testingGroup = ref('')
const profileTestResult = ref(null)

// ---- 教师账号（管理员建号） ----
const teacherForm = reactive({ name: '', phone: '', school: '', password: '' })
const creatingTeacher = ref(false)
const createdTeacher = ref(null)

const SOURCE_LABELS = {
  group: '设置页',
  env: '服务器环境变量',
  default: '默认值',
}

function sourceLabel(source) {
  return SOURCE_LABELS[source] || '默认值'
}

function ensureExpertForm(group) {
  if (!expertForm[group]) {
    expertForm[group] = { api_key: '', base_url: '', model: '', concurrency: undefined }
  }
  return expertForm[group]
}

async function loadProfiles() {
  try {
    const data = await api.settings.getModelProfiles()
    profileSimple.value = data.simple || {}
    profileGroups.value = data.groups || []
    profilesReady.value = !!data.any_configured
    profilesLoaded.value = true
    for (const group of profileGroups.value) {
      const form = ensureExpertForm(group.group)
      form.api_key = ''
      form.base_url = group.base_url || ''
      form.model = group.model || ''
      form.concurrency = group.concurrency || undefined
    }
    for (const slot of simpleSlots) {
      simpleForm[slot.key].api_key = ''
      simpleForm[slot.key].model = ''
    }
  } catch {
    profilesLoaded.value = true
  }
}

async function onSaveSimpleProfiles() {
  savingProfiles.value = true
  profileTestResult.value = null
  try {
    const payload = {}
    for (const slot of simpleSlots) {
      const api_key = simpleForm[slot.key].api_key.trim()
      const model = simpleForm[slot.key].model.trim()
      if (!api_key && !model) continue
      payload[slot.key] = {}
      if (api_key) payload[slot.key].api_key = api_key
      if (model) payload[slot.key].model = model
    }
    if (!Object.keys(payload).length) {
      ElMessage.info('没有需要保存的变更')
      return
    }
    const res = await api.settings.saveSimpleProfiles(payload)
    ElMessage.success(`已保存：${(res.changed || []).length} 个职责组`)
    await loadProfiles()
  } catch {
    // 拦截器已提示
  } finally {
    savingProfiles.value = false
  }
}

async function onSaveExpertProfile(group) {
  savingGroup.value = group
  profileTestResult.value = null
  try {
    const form = ensureExpertForm(group)
    const payload = { group }
    if (form.api_key.trim()) payload.api_key = form.api_key.trim()
    payload.base_url = (form.base_url || '').trim()
    payload.model = (form.model || '').trim()
    if (form.concurrency) payload.concurrency = form.concurrency
    await api.settings.saveExpertProfile(payload)
    ElMessage.success('已保存本组配置')
    await loadProfiles()
  } catch {
    // 拦截器已提示
  } finally {
    savingGroup.value = ''
  }
}

async function onTestProfile(group) {
  if (testingGroup.value) return
  testingGroup.value = group
  profileTestResult.value = null
  try {
    const form = ensureExpertForm(group)
    const payload = { group }
    // 只传用户新填的 key；没填就让后端用已保存的 key，并沿用已保存地址
    if (form.api_key.trim()) payload.api_key = form.api_key.trim()
    if (form.model.trim()) payload.model = form.model.trim()
    profileTestResult.value = await api.settings.testModelProfile(payload)
  } catch (e) {
    profileTestResult.value = { success: false, error: e?.message || '连接失败' }
  } finally {
    testingGroup.value = ''
  }
}

async function onCreateTeacher() {
  if (!teacherForm.name.trim()) {
    ElMessage.warning('请填写老师姓名')
    return
  }
  if (!teacherForm.password) {
    ElMessage.warning('请填写初始密码')
    return
  }
  creatingTeacher.value = true
  try {
    const payload = {
      name: teacherForm.name.trim(),
      password: teacherForm.password,
    }
    if (teacherForm.phone.trim()) payload.phone = teacherForm.phone.trim()
    if (teacherForm.school.trim()) payload.school = teacherForm.school.trim()
    createdTeacher.value = await api.auth.adminCreateUser(payload)
    ElMessage.success('账号已创建')
    teacherForm.name = ''
    teacherForm.phone = ''
    teacherForm.school = ''
    teacherForm.password = ''
  } catch {
    // 拦截器已提示
  } finally {
    creatingTeacher.value = false
  }
}

const desktopAuthorizationExpired = computed(() => (
  desktopWorkerStatus.value?.handoff?.lastErrorCode === 'node_auth_failed'
))

const form = reactive({
  base_url: '',
  api_key: '',
  model: '',
})

async function load() {
  loading.value = true
  testResult.value = null
  try {
    const data = await api.settings.get()
    form.base_url = data.llm_base_url || ''
    form.model = data.llm_model || ''
    form.api_key = ''            // key 不回填，避免把掩码当真值提交
    maskedKey.value = data.llm_api_key || ''
    isConfigured.value = !!data.is_configured
  } catch {
    // 拦截器已弹错
  } finally {
    loading.value = false
  }
}

// 构造提交/测试用的 payload：key 仅在用户填了新值时才带上
function buildPayload() {
  const p = {
    base_url: form.base_url.trim(),
    model: form.model.trim(),
  }
  const k = form.api_key.trim()
  if (k) p.api_key = k
  return p
}

async function onSave() {
  saving.value = true
  try {
    const res = await api.settings.save(buildPayload())
    const changed = res?.changed || []
    ElMessage.success(changed.length ? `已保存：${changed.join('、')}` : '没有变更')
    await load()
  } catch {
    // 拦截器已弹错
  } finally {
    saving.value = false
  }
}

async function onTest() {
  if (testing.value) return
  testing.value = true
  testResult.value = null
  try {
    // 测试用当前表单值；未填新 key 时传空，由后端用已保存的 key
    testResult.value = await api.settings.testConnection(buildPayload())
  } catch (e) {
    testResult.value = { success: false, error: e?.message || '连接失败' }
  } finally {
    testing.value = false
  }
}

const desktopAccelerationLabel = computed(() => {
  const status = desktopWorkerStatus.value
  if (desktopAuthorizationExpired.value) return '需重新绑定'
  if (status?.paused === true) return '已暂停'
  if (!status || status.localProcessingAvailable !== true) return '不可用'
  if (status.computeBackend === 'windows_nvidia_gpu') return 'NVIDIA GPU'
  if (status.computeBackend === 'windows_cpu_opencv') return 'CPU'
  return '不可用'
})

const desktopAccelerationTagType = computed(() => {
  if (desktopAccelerationLabel.value === 'NVIDIA GPU') return 'success'
  if (desktopAccelerationLabel.value === 'CPU') return 'info'
  if (desktopAccelerationLabel.value === '已暂停') return 'warning'
  return 'danger'
})

const desktopAccelerationDescription = computed(() => {
  const status = desktopWorkerStatus.value
  if (!status) return '无法读取本地处理器状态'
  if (desktopAuthorizationExpired.value) return '本地处理器可用，但设备授权已失效；重新绑定前将由云端接管'
  if (status.paused === true) return '本地加速已暂停'
  if (status.localProcessingAvailable !== true) return '本地加速不可用，将使用网页版处理'
  if (status.computeBackend === 'windows_nvidia_gpu') {
    return `本地加速：${status.gpuAcceleration?.deviceName || 'NVIDIA GPU'}`
  }
  if (status.computeBackend === 'windows_cpu_opencv') return '本地加速：CPU'
  return '本地加速不可用，将使用网页版处理'
})

const desktopFallbackReason = computed(() => {
  const status = desktopWorkerStatus.value
  if (status?.statusReadFailed) return '无法读取本地处理器状态'
  const reason = status?.gpuAcceleration?.fallbackReason
    || status?.cpuAcceleration?.reasonCodes?.[0]
  const labels = {
    gpu_runtime_missing: '未检测到可用的 NVIDIA 加速组件',
    gpu_known_answer_failed: 'GPU 启动自检失败',
    gpu_known_answer_timeout: 'GPU 启动自检超时',
    gpu_known_answer_mismatch: 'GPU 正确性自检未通过',
    gpu_geometry_contract_mismatch: 'GPU 几何结果未达到安全门槛',
    gpu_not_faster_than_cpu: 'GPU 未快于本机 CPU',
    preprocessor_unavailable: '本地图片处理组件不可用',
    insufficient_physical_cores: 'CPU 核心数未达到本地处理要求',
    insufficient_available_memory: '可用内存未达到本地处理要求',
    minimum_worker_capacity_unmet: 'CPU 可用并发未达到本地处理要求',
    cpu_busy: 'CPU 当前负载过高',
  }
  return reason ? (labels[reason] || '本地处理器暂不可用') : ''
})

const desktopHandoffMessage = computed(() => {
  const code = desktopWorkerStatus.value?.handoff?.lastErrorCode
  const labels = {
    node_auth_failed: '设备授权已失效，请重新绑定',
    server_unreachable: '暂时无法连接服务器，将由云端接管',
    asset_download_failed: '任务下载失败，已进入重试或云端接管',
    download_hash_mismatch: '任务文件校验失败，已停止本地处理',
    candidate_rejected: '本地候选未通过服务器校验，已由云端接管',
    gpu_operation_failed: 'GPU 处理异常，已自动回退 CPU',
    gpu_candidate_near_contract_boundary: 'GPU 候选接近安全边界，已自动回退 CPU',
    server_candidate_contract_mismatch: '本地候选未通过服务器复核，已由云端接管',
    processing_failed: '本地处理失败，已由云端接管',
  }
  return labels[code] || '本地加速暂不可用，云端仍可继续处理'
})

async function desktopInvoke(command, args = {}) {
  const invoke = window.__TAURI_INTERNALS__?.invoke
  if (typeof invoke !== 'function') throw new Error('desktop_runtime_unavailable')
  return invoke(command, args)
}

async function loadDesktopAcceleration() {
  if (!desktopRuntime.value) return
  desktopLoading.value = true
  try {
    desktopWorkerStatus.value = await desktopInvoke('get_worker_status')
  } catch {
    desktopWorkerStatus.value = {
      computeBackend: 'none',
      localProcessingAvailable: false,
      localProcessorState: 'unavailable',
      webFallbackRequired: true,
      statusReadFailed: true,
      gpuAcceleration: { enabled: false, fallbackReason: null },
      cpuAcceleration: { available: false, enabled: false, reasonCodes: [] },
    }
  } finally {
    desktopLoading.value = false
  }
}

async function repairDesktopBinding() {
  if (!auth.token) {
    ElMessage.error('登录已过期，请重新登录')
    return
  }
  desktopLoading.value = true
  try {
    await bindingManager.open('settings')
    await loadDesktopAcceleration()
  } finally {
    desktopLoading.value = false
  }
}

onMounted(async () => {
  load()
  loadProfiles()
  try {
    const runtime = await desktopInvoke('runtime_info')
    desktopRuntime.value = runtime?.runtime === 'desktop'
  } catch {
    desktopRuntime.value = false
  }
  await loadDesktopAcceleration()
})
</script>

<style scoped>
.settings-page {
  max-width: 820px;
  margin: 0 auto;
}

.page-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 20px;
}

.page-head h2 {
  margin: 0;
  color: var(--ink);
  font-size: var(--fs-2xl);
  font-weight: var(--fw-bold);
  letter-spacing: -0.02em;
}

.card-head > span:first-child {
  color: var(--ink);
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
}

.block {
  border: 1px solid var(--line);
  border-radius: var(--card-radius);
  box-shadow: var(--shadow-sm);
}

.desktop-acceleration-card {
  margin-top: 16px;
}

.profile-alert {
  margin-bottom: 16px;
}

.expert-block {
  margin-top: 8px;
}

.expert-tip {
  margin-bottom: 8px;
}

.expert-row {
  margin-bottom: 8px;
}

.desktop-status {
  display: grid;
  gap: 12px;
}

.desktop-status__primary {
  font-size: 16px;
  font-weight: 650;
  color: var(--ink);
}

.desktop-progress {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  padding: 10px 12px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--primary) 7%, transparent);
  color: var(--ink-muted);
  font-size: 13px;
}

.form {
  max-width: 640px;
}

.tip {
  font-size: 12px;
  color: var(--ink-muted);
  margin-top: 4px;
  line-height: 1.5;
}

@media (max-width: 640px) {
  .page-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .form :deep(.el-form-item) {
    align-items: stretch;
    flex-direction: column;
  }

  .form :deep(.el-form-item__label) {
    width: auto !important;
    justify-content: flex-start;
  }

  .form :deep(.el-form-item__content) {
    margin-left: 0 !important;
  }
}
</style>
