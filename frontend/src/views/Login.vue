<template>
  <div class="stage-wrap">
    <!-- ===== 左侧：墨色展示面板（坐标纸 + 函数曲线 + 桌宠） ===== -->
    <section class="showcase" aria-label="MathAgent 介绍">
      <svg class="plot" viewBox="0 0 800 600" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
        <line class="plot-axis" x1="470" y1="470" x2="800" y2="470" />
        <line class="plot-axis" x1="520" y1="260" x2="520" y2="600" />
        <path class="plot-curve is-faint" pathLength="1" d="M470 330 C 540 330, 580 430, 640 420 S 740 250, 800 270" />
        <path class="plot-curve" pathLength="1" d="M470 520 C 560 515, 610 430, 660 340 S 750 150, 800 120" />
        <circle class="plot-dot" cx="660" cy="340" r="6" />
        <circle class="plot-dot" cx="760" cy="176" r="6" />
      </svg>

      <div class="brand">
        <h1>MathAgent<span class="dot">.</span></h1>
        <p>老 师 的 智 能 助 教</p>
      </div>

      <div class="showcase-copy">
        <p class="eyebrow">AI 数学批改 · 教师始终主导</p>
        <h2 class="headline">把批改的时间，<br />还给课堂与学生。</h2>
        <ol class="principles">
          <li class="principle"><span class="principle-index">01</span><span><b>有教育目的性</b>每一次 AI 使用都服务于明确的教学目标</span></li>
          <li class="principle"><span class="principle-index">02</span><span><b>教师始终是主导者</b>AI 是辅助，主导权永远在教师手中</span></li>
          <li class="principle"><span class="principle-index">03</span><span><b>确保师生直接互动</b>教育的本质是人与人的连接</span></li>
        </ol>
      </div>

    </section>

    <!-- ===== 右侧卡片：登录 / 注册 同页切换 ===== -->
    <div class="right">
      <div class="pet" :aria-hidden="!owlFrameDebug">
        <canvas ref="owlCanvasRef" class="owl-canvas" width="384" height="384"></canvas>
        <output v-if="owlFrameDebug" class="owl-frame-debug">{{ owlFrameLabel }}</output>
      </div>
      <div class="card">
        <!-- ===== 登录面板 ===== -->
        <template v-if="panel === 'login'">
          <h2>欢迎回来</h2>
          <p class="sub">请输入您的登录信息</p>

          <div v-if="features.sms_enabled" class="mode-tabs" role="tablist" aria-label="登录方式">
            <button role="tab" :aria-selected="loginMode === 'sms'" :tabindex="loginMode === 'sms' ? 0 : -1" :class="{ active: loginMode === 'sms' }" @click="loginMode = 'sms'">验证码登录</button>
            <button role="tab" :aria-selected="loginMode === 'password'" :tabindex="loginMode === 'password' ? 0 : -1" :class="{ active: loginMode === 'password' }" @click="loginMode = 'password'">密码登录</button>
          </div>

          <p v-if="!features.sms_enabled" class="sms-disabled-tip">
            {{ features.sms_hint || '本实例未开启短信登录，请联系管理员创建账号' }}
          </p>

          <!-- 验证码登录 -->
          <template v-if="loginMode === 'sms'">
            <div class="field">
              <label for="login-phone">手机号</label>
              <input id="login-phone" v-model.trim="loginForm.phone" type="tel" autocomplete="tel" placeholder="请输入手机号" maxlength="11" />
            </div>
            <div class="field">
              <label for="login-code">验证码</label>
              <div class="sms-row">
                <input id="login-code" v-model.trim="loginForm.code" type="text" inputmode="numeric" autocomplete="one-time-code" placeholder="请输入验证码" maxlength="6" />
                <button class="sms-btn" :disabled="cooldown > 0 || sending" @click="onSendCode('login')">
                  {{ cooldown > 0 ? `${cooldown}s` : '获取验证码' }}
                </button>
              </div>
            </div>
            <button class="login-btn" :disabled="logging" @click="onSmsLogin">
              {{ logging ? '登 录 中…' : '登 录' }}
            </button>
          </template>

          <!-- 密码登录 -->
          <template v-else>
            <div class="field">
              <label for="password-phone">手机号</label>
              <input id="password-phone" v-model.trim="loginForm.phone" type="tel" autocomplete="username" placeholder="请输入手机号" maxlength="11" />
            </div>
            <div class="field">
              <label for="login-password">密码</label>
              <input id="login-password" v-model="loginForm.password" type="password" autocomplete="current-password" placeholder="请输入密码" @keyup.enter="onPwdLogin" />
            </div>
            <button class="login-btn" :disabled="pwdLogging" @click="onPwdLogin">
              {{ pwdLogging ? '登 录 中…' : '登 录' }}
            </button>
          </template>

          <div v-if="features.sms_enabled" class="links">
            <span>还没有账号？</span>
            <a href="#" @click.prevent="switchPanel('register')">立即注册</a>
          </div>
        </template>

        <!-- ===== 注册面板 ===== -->
        <template v-else>
          <h2>创建账号</h2>
          <p class="sub">填写信息完成注册</p>

          <div class="field">
            <label for="register-phone">手机号</label>
            <input id="register-phone" v-model.trim="regForm.phone" type="tel" autocomplete="tel" placeholder="请输入手机号" maxlength="11" />
          </div>
          <div class="field">
            <label for="register-code">验证码</label>
            <div class="sms-row">
              <input id="register-code" v-model.trim="regForm.code" type="text" inputmode="numeric" autocomplete="one-time-code" placeholder="请输入验证码" maxlength="6" />
              <button class="sms-btn" :disabled="cooldown > 0 || sending" @click="onSendCode('register')">
                {{ cooldown > 0 ? `${cooldown}s` : '获取验证码' }}
              </button>
            </div>
          </div>
          <div class="field">
            <label for="register-name">姓名</label>
            <input id="register-name" v-model.trim="regForm.name" type="text" autocomplete="name" placeholder="请输入您的姓名" maxlength="50" />
          </div>
          <div class="field">
            <label for="register-school">学校 <span class="optional">（选填）</span></label>
            <input id="register-school" v-model.trim="regForm.school" type="text" autocomplete="organization" placeholder="请输入学校名称" maxlength="100" />
          </div>
          <div class="field">
            <label for="register-password">密码 <span class="optional">（选填，便于下次登录）</span></label>
            <input id="register-password" v-model="regForm.password" type="password" autocomplete="new-password" placeholder="6-20位，至少含字母和数字" maxlength="20" />
          </div>

          <label class="agree">
            <input v-model="agreedToTerms" type="checkbox" />
            <span>我已阅读并同意 <a href="#" @click.prevent="openAgreement">《用户服务协议》</a></span>
          </label>

          <button class="login-btn" :disabled="registering" @click="onRegister">
            {{ registering ? '注 册 中…' : '注 册' }}
          </button>

          <div class="links">
            <span>已有账号？</span>
            <a href="#" @click.prevent="switchPanel('login')">返回登录</a>
          </div>
        </template>

        <p v-if="devCode" class="dev-tip">开发模式验证码：<b>{{ devCode }}</b></p>
      </div>
    </div>

    <!-- 用户协议弹窗 -->
    <div v-if="showAgreement" class="agreement-mask" @click.self="closeAgreement" @keydown.esc="closeAgreement">
      <div ref="agreementDialogRef" class="agreement-dialog" role="dialog" aria-modal="true" aria-labelledby="agreement-title" tabindex="-1">
        <h3 id="agreement-title" class="agreement-title">MathAgent 用户服务协议</h3>
        <div class="agreement-content">
          <h4>服务宗旨</h4>
          <p>MathAgent 是一款面向小学、初中与高中数学教师的 Agent 辅助批改工具。我们深知，<strong>教育的本质是人与人之间的交流与关怀</strong>，技术永远只是辅助手段。本系统的目标是帮助教师从重复性的批改劳动中解放出来，<strong>将更多时间投入到学生关怀、教学创新与自身生活中</strong>。</p>
          <h4>数据使用说明</h4>
          <p>为持续优化批改准确率，您上传的试卷图片中的<strong>题目和标准答案</strong>可能被用于构建题库、优化判题能力、生成匿名化教学统计。我们<strong>不会</strong>公开任何可识别具体学生身份的信息。</p>
          <h4>使用须知</h4>
          <ul>
            <li>批阅结果供教师参考，最终以教师判断为准</li>
            <li>建议对"部分正确"和"待复核"的题目进行人工确认</li>
            <li>请勿上传涉及学生隐私的敏感信息</li>
          </ul>
          <p class="agreement-version">协议版本：v1.0 | 生效日期：2026年6月7日</p>
        </div>
        <div class="agreement-footer">
          <button class="dialog-btn ghost" @click="closeAgreement">关闭</button>
          <button class="dialog-btn" @click="acceptAgreement">我已阅读并同意</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const PHONE_RE = /^1[3-9]\d{9}$/
const PWD_RE = /^(?=.*[A-Za-z])(?=.*\d).{6,20}$/

// 面板：login | register
const panel = ref('login')
// 登录方式：sms | password
const loginMode = ref('sms')

const loginForm = reactive({ phone: '', code: '', password: '' })
const regForm = reactive({ phone: '', code: '', name: '', school: '', password: '' })

// 服务端能力探测：SMS_ENABLED=0 的实例隐藏短信区块，只留密码登录。
const features = reactive({
  sms_enabled: true,
  wechat_enabled: false,
  captcha_provider: 'aliyun',
  captcha_required: false,
  sms_hint: '',
})

const sending = ref(false)
const logging = ref(false)
const pwdLogging = ref(false)
const registering = ref(false)
const cooldown = ref(0)
const devCode = ref('')
const agreedToTerms = ref(false)
const showAgreement = ref(false)
const agreementDialogRef = ref(null)
let agreementTrigger = null

let timer = null
let captchaInstance = null
let captchaReady = false
let captchaResolve = null
let captchaReject = null

// ---- 雪鸮单源姿态环 ----
const owlCanvasRef = ref(null)
const owlFrameDebug = route.query.owlFrameDebug === '1'
const owlFrameLabel = ref('frame-000 · 中心')
const OWL_CENTER_FRAME = 0
// Frame-level similarity finds a reverse overlap between source 000–017 and 103–120.
// Keep only the non-duplicated middle 018–102, then map it uniformly around the pointer ring.
const OWL_RING_FRAMES = Array.from({ length: 85 }, (_, index) => index + 18)
// User-verified cardinal poses. The final position closes the loop back to green-022.
const OWL_RING_ANCHORS = [
  { angle: Math.PI, position: 4 },       // left  → green-022
  { angle: Math.PI * 1.5, position: 29 }, // up    → green-047
  { angle: Math.PI * 2, position: 48 },   // right → green-066
  { angle: Math.PI * 2.5, position: 68 }, // down  → green-086
  { angle: Math.PI * 3, position: 89 },   // left  → green-022 (85 + 4)
]
const OWL_TWO_PI = Math.PI * 2
let owlImages = []
let owlCenterImage = null
let owlPointerFrame = 0
let owlAnimationFrame = 0
let owlRingActive = false
let owlRingCurrent = 0
let owlRingTarget = 0
let owlRingRunning = false
let owlDisposed = false

function normalizeOwlRing(position) {
  const length = OWL_RING_FRAMES.length
  return ((position % length) + length) % length
}

function shortestOwlRingDelta(target, current) {
  const length = OWL_RING_FRAMES.length
  return ((target - current + length / 2) % length + length) % length - length / 2
}

function drawOwl(image) {
  const canvas = owlCanvasRef.value
  if (!canvas || !(image?.naturalWidth || image?.width)) return false
  const context = canvas.getContext('2d')
  context.clearRect(0, 0, canvas.width, canvas.height)
  context.drawImage(image, 0, 0, canvas.width, canvas.height)
  return true
}

function drawOwlCenter() {
  if (drawOwl(owlCenterImage)) owlFrameLabel.value = `green-${String(OWL_CENTER_FRAME).padStart(3, '0')} · 中心`
}

function drawOwlRing(position) {
  const normalized = normalizeOwlRing(position)
  const index = Math.round(normalized) % OWL_RING_FRAMES.length
  if (!drawOwl(owlImages[index])) return
  owlFrameLabel.value = `green-${String(OWL_RING_FRAMES[index]).padStart(3, '0')}`
  owlRingCurrent = normalized
}

function owlRingPositionForAngle(angle) {
  let normalizedAngle = angle
  if (normalizedAngle < 0) normalizedAngle += OWL_TWO_PI
  const pathAngle = normalizedAngle < Math.PI ? normalizedAngle + OWL_TWO_PI : normalizedAngle
  for (let index = 0; index < OWL_RING_ANCHORS.length - 1; index += 1) {
    const start = OWL_RING_ANCHORS[index]
    const end = OWL_RING_ANCHORS[index + 1]
    if (pathAngle >= start.angle && pathAngle <= end.angle) {
      const progress = (pathAngle - start.angle) / (end.angle - start.angle)
      return start.position + (end.position - start.position) * progress
    }
  }
  return 0
}

function animateOwlRing() {
  if (!owlRingRunning) return
  const delta = shortestOwlRingDelta(owlRingTarget, owlRingCurrent)
  if (Math.abs(delta) < .06) {
    owlRingCurrent = normalizeOwlRing(owlRingTarget)
    drawOwlRing(owlRingCurrent)
    owlRingRunning = false
    return
  }
  // Cap a draw step so the browser actually presents the added intermediate frames.
  // A large pointer jump should travel smoothly through the verified frame ring instead of skipping it.
  const step = Math.sign(delta) * Math.min(1.2, Math.max(.22, Math.abs(delta) * .16))
  owlRingCurrent = normalizeOwlRing(owlRingCurrent + step)
  drawOwlRing(owlRingCurrent)
  owlAnimationFrame = requestAnimationFrame(animateOwlRing)
}

function onMouseMove(event) {
  const canvas = owlCanvasRef.value
  if (!canvas || !owlImages.length || matchMedia('(prefers-reduced-motion: reduce)').matches) return
  const rect = canvas.getBoundingClientRect()
  const dx = event.clientX - (rect.left + rect.width / 2)
  const dy = event.clientY - (rect.top + rect.height / 2)
  if (Math.hypot(dx, dy) < Math.min(rect.width, rect.height) * .24) {
    owlRingRunning = false
    owlRingActive = false
    drawOwlCenter()
    return
  }
  owlRingTarget = normalizeOwlRing(owlRingPositionForAngle(Math.atan2(dy, dx)))
  if (!owlRingActive) {
    owlRingActive = true
    owlRingCurrent = owlRingTarget
    drawOwlRing(owlRingCurrent)
    return
  }
  if (!owlRingRunning) {
    owlRingRunning = true
    owlAnimationFrame = requestAnimationFrame(animateOwlRing)
  }
}

async function loadOwlFrames() {
  const load = source => new Promise(resolve => {
    const image = new Image()
    image.addEventListener('load', async () => {
      try {
        const bitmap = await createImageBitmap(image, { resizeWidth: 384, resizeHeight: 384, resizeQuality: 'high' })
        image.src = ''
        resolve(bitmap)
      } catch {
        resolve(image)
      }
    }, { once: true })
    image.addEventListener('error', () => resolve(null), { once: true })
    image.src = source
  })
  const version = 1
  const sources = OWL_RING_FRAMES.map(frame => `/owl-green-ring/frames/frame-${String(frame).padStart(3, '0')}.webp?v=${version}`)
  const center = await load(`/owl-green-ring/frames/frame-${String(OWL_CENTER_FRAME).padStart(3, '0')}.webp?v=${version}`)
  const ring = new Array(sources.length)
  let cursor = 0
  const workers = Array.from({ length: 4 }, async () => {
    while (cursor < sources.length) {
      const index = cursor++
      ring[index] = await load(sources[index])
    }
  })
  await Promise.all(workers)
  if (owlDisposed) {
    for (const image of [center, ...ring]) image?.close?.()
    return
  }
  owlCenterImage = center
  owlImages = ring
  drawOwlCenter()
}

function getSafeRedirect() {
  const redirect = Array.isArray(route.query.redirect) ? route.query.redirect[0] : route.query.redirect
  return typeof redirect === 'string' && redirect.startsWith('/') && !redirect.startsWith('//') ? redirect : '/'
}

function switchPanel(target) {
  panel.value = target
  devCode.value = ''
}

async function openAgreement(event) {
  agreementTrigger = event?.currentTarget || document.activeElement
  showAgreement.value = true
  await nextTick()
  agreementDialogRef.value?.focus()
}

function closeAgreement() {
  showAgreement.value = false
  nextTick(() => agreementTrigger?.focus?.())
}

function acceptAgreement() {
  agreedToTerms.value = true
  closeAgreement()
}

function startCooldown() {
  cooldown.value = 60
  timer = setInterval(() => {
    cooldown.value--
    if (cooldown.value <= 0) {
      clearInterval(timer)
      timer = null
    }
  }, 1000)
}

// 当前面板对应的手机号
function currentPhone(which) {
  return (which === 'register' ? regForm.phone : loginForm.phone).trim()
}

async function onSendCode(which) {
  const phone = currentPhone(which)
  if (!PHONE_RE.test(phone)) {
    ElMessage.warning('请输入正确的手机号')
    return
  }

  let captchaTicket = ''

  // 内置算术验证（CAPTCHA_PROVIDER=builtin）：服务端出题，用户填答案后换一次性票据。
  if (features.captcha_provider === 'builtin') {
    try {
      const challenge = await api.auth.captchaChallenge()
      const { value } = await ElMessageBox.prompt(
        `请计算：${challenge.problem}`,
        '安全验证',
        {
          confirmButtonText: '验证',
          cancelButtonText: '取消',
          inputPattern: /^-?\d{1,4}$/,
          inputErrorMessage: '请输入数字答案',
        },
      )
      const verified = await api.auth.verifyCaptcha(
        JSON.stringify({ token: challenge.token, answer: String(value).trim() }),
        phone,
      )
      captchaTicket = verified?.ticket || ''
    } catch (err) {
      if (err !== 'cancel' && err !== 'close') {
        ElMessage.error(err?.response?.data?.detail || '验证未通过，请重试')
      }
      return
    }
  }

  // 图形验证码（阿里云 ct4.js）— 已初始化则先过图形验证
  if (features.captcha_provider === 'aliyun' && captchaInstance && captchaReady) {
    try {
      const result = await new Promise((resolve, reject) => {
        captchaResolve = resolve
        captchaReject = reject
        captchaInstance.showCaptcha()
      })
      const captchaResp = await api.auth.verifyCaptcha(JSON.stringify({ ...result, captcha_id: CAPTCHA_ID }), phone)
      captchaTicket = captchaResp?.ticket || ''
      captchaInstance.reset()
    } catch {
      if (captchaInstance) captchaInstance.reset()
      ElMessage.error('图形验证失败，请重试')
      return
    }
  }

  sending.value = true
  try {
    const data = await api.auth.sendSms(phone, captchaTicket)
    ElMessage.success(data.msg || '验证码已发送')
    if (data.dev_code) devCode.value = data.dev_code
    startCooldown()
  } catch {
    /* 拦截器已提示 */
  } finally {
    sending.value = false
  }
}

// ---- 验证码登录 ----
async function onSmsLogin() {
  const phone = loginForm.phone.trim()
  if (!PHONE_RE.test(phone)) return ElMessage.warning('请输入正确的手机号')
  if (!/^\d{6}$/.test(loginForm.code.trim())) return ElMessage.warning('请输入6位验证码')

  logging.value = true
  try {
    const data = await api.auth.smsLogin({ phone, code: loginForm.code.trim() })
    auth.login(data.token, data.user)
    ElMessage.success('登录成功')
    router.push(getSafeRedirect())
  } catch (err) {
    const detail = err.response?.data?.detail || ''
    // 未注册手机号 → 引导去注册，并带过手机号（后端未消费验证码，可继续用）
    if (typeof detail === 'string' && (detail.includes('未注册') || detail.includes('姓名'))) {
      regForm.phone = phone
      regForm.code = loginForm.code.trim()
      switchPanel('register')
      ElMessage.info('该手机号尚未注册，请完成注册')
    }
    /* 其余错误拦截器已提示 */
  } finally {
    logging.value = false
  }
}

// ---- 密码登录 ----
async function onPwdLogin() {
  const phone = loginForm.phone.trim()
  if (!PHONE_RE.test(phone)) return ElMessage.warning('请输入正确的手机号')
  if (!loginForm.password) return ElMessage.warning('请输入密码')

  pwdLogging.value = true
  try {
    const data = await api.auth.passwordLogin({ phone, password: loginForm.password })
    auth.login(data.token, data.user)
    ElMessage.success('登录成功')
    router.push(getSafeRedirect())
  } catch {
    /* 拦截器已提示 */
  } finally {
    pwdLogging.value = false
  }
}

// ---- 注册 ----
async function onRegister() {
  const phone = regForm.phone.trim()
  if (!PHONE_RE.test(phone)) return ElMessage.warning('请输入正确的手机号')
  if (!/^\d{6}$/.test(regForm.code.trim())) return ElMessage.warning('请输入6位验证码')
  if (!regForm.name.trim()) return ElMessage.warning('请输入姓名')
  if (regForm.password && !PWD_RE.test(regForm.password)) {
    return ElMessage.warning('密码需6-20位，且至少包含字母和数字')
  }
  if (!agreedToTerms.value) return ElMessage.warning('请先阅读并同意用户服务协议')

  registering.value = true
  try {
    const data = await api.auth.smsLogin({
      phone,
      code: regForm.code.trim(),
      name: regForm.name.trim(),
      school: regForm.school.trim() || undefined,
    })
    auth.login(data.token, data.user)
    // 选填密码：注册成功后设定，便于下次密码登录
    if (regForm.password) {
      try {
        await api.auth.setPassword(regForm.password, null)
      } catch {
        ElMessage.warning('账号已注册，但密码设置失败，可稍后在个人资料中重设')
      }
    }
    ElMessage.success('注册成功')
    router.push(getSafeRedirect())
  } catch {
    /* 拦截器已提示 */
  } finally {
    registering.value = false
  }
}

const CAPTCHA_ID = '2cd9f5594e793a7730000007c0bc735f'

function initCaptcha() {
  if (typeof window.initAlicom4 === 'undefined') return
  try {
    window.initAlicom4({ captchaId: CAPTCHA_ID, product: 'bind' }, (captchaObj) => {
      captchaInstance = captchaObj
      captchaObj.onNextReady(function() { captchaReady = true })
      captchaObj.onSuccess(function() {
        const data = captchaObj.getValidate()
        if (captchaResolve) captchaResolve(data)
        captchaResolve = null
        captchaReject = null
      })
      captchaObj.onFail(function() {
        if (captchaReject) captchaReject(new Error('验证失败'))
        captchaResolve = null
        captchaReject = null
      })
      captchaObj.onError(function(error) {
        if (captchaReject) captchaReject(error)
        captchaResolve = null
        captchaReject = null
      })
      captchaObj.onClose(function() {
        if (captchaReject) captchaReject(new Error('验证已关闭'))
        captchaResolve = null
        captchaReject = null
      })
    })
  } catch { /* 未加载时由后端短信频控兜底 */ }
}

onMounted(async () => {
  owlDisposed = false
  await nextTick()
  try {
    const caps = await api.auth.features()
    Object.assign(features, caps || {})
  } catch {
    // 探测失败时保持「短信可用 + 阿里云验证」的旧行为，由后端兜底拒绝
  }
  if (!features.sms_enabled) {
    loginMode.value = 'password'
    panel.value = 'login'
  }
  if (features.captcha_provider === 'aliyun') initCaptcha()
  document.addEventListener('mousemove', onMouseMove)
  loadOwlFrames().catch(() => {})
})

onBeforeUnmount(() => {
  owlDisposed = true
  if (timer) clearInterval(timer)
  cancelAnimationFrame(owlPointerFrame)
  cancelAnimationFrame(owlAnimationFrame)
  document.removeEventListener('mousemove', onMouseMove)
  for (const image of [owlCenterImage, ...owlImages]) image?.close?.()
  owlCenterImage = null
  owlImages = []
})
</script>

<style scoped>
.stage-wrap {
  position: fixed; inset: 0; overflow: hidden;
  display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(420px, 0.85fr);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "PingFang SC", "Noto Sans SC", "Microsoft YaHei", system-ui, sans-serif;
  background: #eef1f0; color: #1c1c1e;
  color-scheme: light;
  /* 登录页始终保持浅色；深色模式只作用于登录后的教师工作台。 */
  --primary: #12795f; --primary-dark: #0e6350; --primary-soft: #e4f1ec;
  --accent: #12795f; --accent-deep: #0e6350;
  --ink: #1c1c1e; --ink-soft: #3a3a3c; --ink-muted: #6b6b70; --ink-faint: #68686d;
  --ink-2: #6b6b70; --ink-3: #68686d;
  --hair: rgba(60,60,67,.12); --fill: rgba(118,118,128,.08);
  --page: #eef1f0; --card: #ffffff; --card-2: #fafafa;
  --serif: -apple-system, BlinkMacSystemFont, "SF Pro Display", "PingFang SC", "Noto Sans SC", system-ui, sans-serif;
  --display: 'Tiempos Headline', 'Iowan Old Style', 'Palatino Linotype', 'Book Antiqua', Palatino, Georgia, 'Noto Serif SC', 'Source Han Serif SC', 'Songti SC', STSong, serif;
  --paper: #f3f1ea; --kraft: #e9e4d8; --ivory: #fbfaf7; --moss: #3f6b4f; --moss-soft: #e4eadc;
  --ink-solid: #1f1e1d; --rule: rgba(31,30,29,.16);
}

/* ── Left: kraft-paper showcase with an inked function plot ── */
.showcase {
  position: relative; overflow: hidden; isolation: isolate;
  display: flex; flex-direction: column; justify-content: space-between;
  padding: clamp(28px, 5vh, 56px) clamp(28px, 5vw, 72px);
  color: var(--ink);
  background: var(--kraft);
  border-right: 1px solid var(--rule);
}
.plot { position: absolute; inset: 0; width: 100%; height: 100%; z-index: -1; pointer-events: none; }
.plot-axis { stroke: var(--rule); stroke-width: 1; }
.plot-curve {
  fill: none; stroke: var(--ink); stroke-width: 1.8; stroke-linecap: round;
  stroke-dasharray: 1; stroke-dashoffset: 0;
  animation: plotDraw 2s cubic-bezier(.22,.61,.36,1) .2s backwards;
}
.plot-curve.is-faint { stroke: var(--rule); stroke-width: 1.4; stroke-dasharray: .01 .012; animation: fadeIn .9s ease .5s backwards; }
.plot-dot { fill: var(--moss); stroke: none; animation: fadeIn .4s ease 1.9s backwards; }
@keyframes plotDraw { from { stroke-dashoffset: 1; } to { stroke-dashoffset: 0; } }
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes riseIn { from { opacity: 0; transform: translateY(12px); } to { opacity: 1; transform: none; } }

/* 品牌 */
.brand { position: relative; z-index: 2; animation: riseIn .6s cubic-bezier(.16,1,.3,1) backwards; }
.brand h1 {
  margin: 0; font-family: var(--display); font-size: clamp(30px, 2.6vw, 40px); font-weight: 500;
  letter-spacing: -.01em; line-height: 1; color: var(--ink);
}
.brand h1 .dot { color: var(--moss); }
.brand p { margin: 10px 0 0; font-size: 13px; color: var(--ink-muted); letter-spacing: 4px; }

.showcase-copy { position: relative; z-index: 2; max-width: 560px; margin: auto 0; }
.eyebrow {
  display: flex; align-items: center; gap: 10px;
  margin: 0 0 18px; color: var(--moss); font-size: 13px; font-weight: 600; letter-spacing: .16em;
  animation: riseIn .6s cubic-bezier(.16,1,.3,1) .1s backwards;
}
.eyebrow::before { content: ''; width: 24px; height: 1px; background: currentColor; }
.headline {
  margin: 0; color: var(--ink); font-family: var(--display); font-size: clamp(36px, 3.8vw, 60px); font-weight: 400;
  line-height: 1.18; letter-spacing: -.01em;
  animation: riseIn .7s cubic-bezier(.16,1,.3,1) .18s backwards;
}

/* 教学原则：编号列表 */
.principles { list-style: none; margin: 34px 0 0; padding: 0; display: grid; gap: 14px; }
.principle {
  display: grid; grid-template-columns: 44px minmax(0, 1fr); align-items: baseline; gap: 6px;
  padding-top: 14px; border-top: 1px solid var(--rule);
  font-size: 14px; color: var(--ink-muted);
  animation: riseIn .6s cubic-bezier(.16,1,.3,1) backwards;
}
.principle:nth-child(1) { animation-delay: .3s; }
.principle:nth-child(2) { animation-delay: .38s; }
.principle:nth-child(3) { animation-delay: .46s; }
.principle-index { font-family: var(--display); font-size: 17px; font-weight: 500; color: var(--moss); }
.principle b { display: block; margin-bottom: 2px; color: var(--ink); font-size: 15px; font-weight: 650; }

/* 雪鸮：坐在坐标轴上 */
.pet { position: relative; width: clamp(130px, 13vw, 190px); aspect-ratio: 1; margin: 0 auto calc(-1 * clamp(31px, 3.1vw, 45px) - 2px); z-index: 2; pointer-events: none; }
.owl-canvas { width: 100%; height: 100%; display: block; }
.owl-frame-debug {
  position: absolute; top: -24px; left: 50%; transform: translateX(-50%);
  min-width: 92px; padding: 3px 8px; border: 1px solid rgba(255,255,255,0.72);
  background: rgba(24,30,36,0.82); color: #fff; border-radius: 3px;
  font: 600 12px/1.3 ui-monospace, "Cascadia Mono", monospace; letter-spacing: .02em;
  text-align: center; pointer-events: none; white-space: nowrap; z-index: 3;
}

/* ── Right: paper side with the form ── */
.right {
  position: relative; height: 100%;
  display: flex; flex-direction: column; gap: 2px;
  align-items: center; justify-content: center; padding: 32px clamp(24px, 4vw, 64px); overflow-y: auto;
  background: var(--paper);
}
.card {
  position: relative; width: min(100%, 400px);
  background: var(--card);
  background: var(--ivory);
  border: 1px solid var(--rule); border-radius: 14px; padding: 40px 38px 32px;
  box-shadow: 0 1px 2px rgba(31,30,29,.05);
  animation: riseIn .7s cubic-bezier(.16,1,.3,1) .12s backwards;
}
.card h2 {
  font-family: var(--display); font-size: 32px; font-weight: 500; margin: 0 0 6px;
  letter-spacing: -0.01em; color: var(--ink);
}
.card .sub { color: var(--ink-2); font-size: 14px; margin: 0 0 24px; }

.sms-disabled-tip {
  margin: 4px 0 12px;
  padding: 10px 12px;
  border-radius: 10px;
  background: var(--fill);
  color: var(--ink-muted);
  font-size: 13px;
  line-height: 1.5;
}

.mode-tabs { display: flex; gap: 0; margin-bottom: 24px; border-bottom: 1px solid var(--rule); }
.mode-tabs button {
  flex: 1; padding: 10px 0; margin-bottom: -1px; border-radius: 0; cursor: pointer; font-size: 14px; font-weight: 600;
  border: none; background: transparent; color: var(--ink-2);
  transition: background .2s, color .2s, box-shadow .2s;
}
.mode-tabs button { border-bottom: 2px solid transparent !important; }
.mode-tabs button.active { color: var(--ink); border-bottom-color: var(--moss) !important; }

.field { margin-bottom: 16px; }
.field label { display: block; font-size: 13px; color: var(--ink-2); margin-bottom: 7px; font-weight: 600; }
.field label .optional { font-weight: 400; color: var(--ink-3); font-size: 12px; }
.field input {
  width: 100%; padding: 13px 15px; border: 1px solid var(--rule); border-radius: 8px;
  font-size: 15px; outline: none; color: var(--ink); background: #fff;
  transition: border-color .2s, box-shadow .2s, background .2s;
}
.field input::placeholder { color: var(--ink-3); }
.field input:focus { border-color: var(--moss); background: #fff; box-shadow: 0 0 0 3px rgba(63,107,79,.18); }

.sms-row { display: flex; gap: 10px; }
.sms-row input { flex: 1; }
.sms-btn {
  padding: 0 16px; background: transparent; color: var(--ink);
  border: 1px solid var(--rule); border-radius: 8px; font-size: 13px; font-weight: 600;
  cursor: pointer; white-space: nowrap; transition: background .2s, color .2s;
}
.sms-btn:hover:not(:disabled) { background: var(--moss-soft); border-color: var(--moss); color: var(--moss); }
.sms-btn:disabled { opacity: 0.5; cursor: not-allowed; }

.login-btn {
  width: 100%; padding: 14px; margin-top: 12px; border: none; border-radius: 8px; cursor: pointer;
  font-size: 15px; font-weight: 600; letter-spacing: .1em; color: #faf9f5;
  background: var(--ink-solid);
  transition: transform .15s, background .2s;
}
.login-btn:hover:not(:disabled) { background: #3d3a35; }
.login-btn:active:not(:disabled) { transform: scale(.98); }
.login-btn:disabled { opacity: 0.6; cursor: not-allowed; }

.agree { display: flex; align-items: flex-start; gap: 8px; margin: 4px 0 6px; font-size: 13px; color: var(--ink-2); cursor: pointer; }
.agree input { margin-top: 2px; accent-color: var(--accent); }
.agree a { color: var(--accent); text-decoration: none; font-weight: 600; }
.agree a:hover { text-decoration: underline; }

.links { margin-top: 20px; text-align: center; font-size: 13px; color: var(--ink-2); }
.links a { color: var(--moss); text-decoration: underline; text-underline-offset: 3px; text-decoration-thickness: 1px; margin-left: 6px; font-weight: 600; }
.links a:hover { color: var(--accent-deep); text-decoration: underline; }

.dev-tip { text-align: center; color: var(--accent-deep); background: var(--primary-soft); border-radius: 8px; padding: 8px; font-size: 13px; margin: 14px 0 0; }
/* 用户协议弹窗 */
.agreement-mask {
  position: fixed; inset: 0; z-index: 20; display: flex; align-items: center; justify-content: center;
  background: rgba(31,30,29,0.38);
}
.agreement-dialog {
  width: min(620px, 92vw); max-height: 82vh; display: flex; flex-direction: column;
  background: var(--ivory); border: 1px solid var(--rule); border-radius: 14px; padding: 28px 30px; box-shadow: 0 24px 60px -24px rgba(31,30,29,0.3);
  animation: riseIn .35s cubic-bezier(.16,1,.3,1) backwards;
}
.agreement-title { font-family: var(--display); font-size: 24px; font-weight: 500; color: var(--ink); margin-bottom: 16px; }
.agreement-content { overflow-y: auto; line-height: 1.8; color: var(--ink-soft); padding-right: 6px; }
.agreement-content h4 { margin: 16px 0 8px; font-size: 15px; color: var(--ink); border-left: 3px solid var(--accent); padding-left: 10px; }
.agreement-content h4:first-child { margin-top: 0; }
.agreement-content ul { margin: 8px 0; padding-left: 20px; }
.agreement-content li { margin: 5px 0; }
.agreement-content strong { color: var(--accent-deep); }
.agreement-version { color: var(--ink-muted); font-size: 12px; margin-top: 16px; }
.agreement-footer { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
.dialog-btn {
  padding: 9px 18px; border-radius: 8px; border: none; cursor: pointer; font-size: 14px; font-weight: 600;
  background: var(--ink-solid); color: #faf9f5; transition: background .2s;
}
.dialog-btn:hover { background: #3d3a35; }
.dialog-btn.ghost { background: transparent; border: 1px solid var(--hair); color: var(--ink-soft); }
.dialog-btn.ghost:hover { background: var(--fill); }

/* 窄屏：展示面板收为顶部横幅 */
@media (max-width: 900px) {
  .stage-wrap { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); overflow-y: auto; position: fixed; }
  .showcase { padding: 24px; min-height: 180px; }
  .showcase-copy .principles, .stage { display: none; }
  .headline { font-size: 28px; }
  .right { height: auto; align-items: flex-start; }
}
@media (max-width: 480px) {
  .right { padding: 24px 16px; }
  .card { width: 100%; padding: 28px 20px 24px; }
  .sms-row input { min-width: 0; }
  .sms-btn { padding-inline: 10px; }
  .agreement-dialog { padding: 22px 18px; }
  .agreement-footer { flex-wrap: wrap; }
}
@media (prefers-reduced-motion: reduce) {
  .brand, .card, .eyebrow, .headline, .principle, .plot-curve, .plot-dot, .agreement-dialog { animation: none !important; }
}
</style>
