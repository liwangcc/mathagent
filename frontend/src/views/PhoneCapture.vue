<template>
  <div class="phone-capture" :class="{ 'is-wide': isWide }" :style="rootStyle">
    <section v-if="phase === 'checking'" class="pc-center" role="status">
      <p>正在连接电脑端…</p>
    </section>

    <section v-else-if="phase === 'ended'" class="pc-center" role="alert">
      <h1>拍摄已结束</h1>
      <p>{{ fatalMessage }}</p>
      <p v-if="shots.length" class="pc-note">本机共拍 {{ shots.length }} 张，已上传 {{ uploadedCount }} 张。</p>
    </section>

    <section v-else-if="phase === 'start'" class="pc-center pc-start">
      <h1>手机拍摄试卷</h1>
      <ol class="pc-steps">
        <li>把手机固定在支架上，镜头朝下对准桌面，尽量放平。</li>
        <li>竖版试卷选“竖拍”；横版试卷（如 A3 双栏）把手机横放并选“横拍”。试卷正常摆在面前，顶部朝外。</li>
        <li>屏幕文字是倒的？再点一次同一个按钮即可翻转。</li>
        <li>放好一张并停稳，对焦后自动拍照，听到“叮咚”再换下一张；光线不足可点“补光”。</li>
        <li>两次自动拍照至少间隔 {{ settings.cooldown }} 秒，换纸需要更久可在“更多”里调整。</li>
      </ol>
      <div class="pc-segmented pc-segmented-large" role="group" aria-label="拍摄方向">
        <button
          v-for="mode in SHOOT_MODES"
          :key="mode.kind"
          type="button"
          :class="{ 'is-on': shootMode === mode.kind }"
          :aria-pressed="shootMode === mode.kind"
          @click="chooseShootMode(mode.kind)"
        >{{ mode.label }}</button>
      </div>
      <button type="button" class="pc-primary" :disabled="!liveSupported || starting" @click="startLive">
        {{ starting ? '正在打开相机…' : '开始拍摄' }}
      </button>
      <p v-if="!liveSupported" class="pc-note" role="alert">{{ liveUnsupportedReason }}</p>
      <p v-if="cameraError" class="pc-note" role="alert">{{ cameraError }}</p>
      <button type="button" class="pc-link" @click="startFallback">改用系统相机逐张拍照（画质最高，需手动）</button>
    </section>

    <section v-else class="pc-work">
      <header class="pc-bar">
        <span class="pc-count" data-test="shot-count">已拍 {{ shots.length }} 张 · 已上传 {{ uploadedCount }}</span>
        <span v-if="hasFailed" class="pc-bad">有照片上传失败</span>
        <span
          v-if="phase === 'live' && level !== 'unknown'"
          class="pc-level"
          :class="`is-${level}`"
          data-test="level"
          :title="levelText"
        >
          <span class="pc-level-dial" aria-hidden="true"><span class="pc-level-bubble" :style="bubbleStyle"></span></span>
          {{ levelText }}
        </span>
      </header>

      <div v-if="phase === 'live'" ref="viewportRef" class="pc-viewport">
        <video
          ref="previewRef"
          class="pc-preview"
          :style="previewStyle"
          playsinline
          muted
          autoplay
          aria-label="取景预览"
          @loadedmetadata="syncStreamSize"
          @resize="syncStreamSize"
        ></video>
        <div class="pc-frame" :style="frameStyle">
          <span class="pc-top-mark">▲ 试卷顶部</span>
          <svg v-if="paperOutline" class="pc-outline" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
            <polygon :points="paperOutline" :class="`is-${outlineTone}`" vector-effect="non-scaling-stroke" />
          </svg>
          <div v-else class="pc-guide" aria-hidden="true"></div>
        </div>
        <div class="pc-status" :class="status.tone && `is-${status.tone}`" data-test="capture-status" role="status">
          <strong>{{ status.text }}</strong>
          <span v-if="secondaryHint">{{ secondaryHint }}</span>
        </div>
        <div v-if="flashOn" class="pc-flash" aria-hidden="true"></div>
      </div>

      <div v-else class="pc-fallback">
        <p>每点一次“拍一张”会打开系统相机。拍完请看下方缩略图，确认试卷是正的。</p>
        <p v-if="!liveSupported" class="pc-note">{{ liveUnsupportedReason }}</p>
      </div>

      <div class="pc-strip" aria-label="已拍照片">
        <button
          v-for="(shot, index) in shots"
          :key="shot.clientId"
          type="button"
          class="pc-thumb"
          :class="[`is-${shot.status}`, { 'is-blurry': shot.blurry }]"
          :aria-label="`第 ${index + 1} 张，${SHOT_STATUS[shot.status].text}${shot.blurry ? '，可能模糊' : ''}`"
          @click="selectedShot = shot"
        >
          <img :src="shot.thumbUrl" alt="" />
          <span class="pc-thumb-no">{{ index + 1 }}</span>
          <span v-if="shot.blurry" class="pc-thumb-blur">模糊</span>
          <span class="pc-thumb-state">{{ SHOT_STATUS[shot.status].badge }}</span>
        </button>
      </div>

      <footer class="pc-controls">
        <div class="pc-segmented" role="group" aria-label="拍摄方向">
          <button
            v-for="mode in SHOOT_MODES"
            :key="mode.kind"
            type="button"
            :class="{ 'is-on': shootMode === mode.kind }"
            :aria-pressed="shootMode === mode.kind"
            @click="chooseShootMode(mode.kind)"
          >{{ mode.label }}</button>
        </div>
        <button
          v-if="phase === 'live' && torchSupported"
          type="button"
          class="pc-tool"
          :class="{ 'is-on': torchOn }"
          data-test="torch"
          @click="toggleTorch"
        >
          补光<small>{{ torchOn ? '开' : '关' }}</small>
        </button>
        <button
          v-if="phase === 'live'"
          type="button"
          class="pc-shutter"
          aria-label="手动拍照"
          :disabled="sessionFull || takingPhoto"
          @click="capturePhoto({ manual: true })"
        ></button>
        <button v-else type="button" class="pc-shutter-text" :disabled="sessionFull || processingPhoto" @click="openSystemCamera">
          {{ processingPhoto ? '处理中…' : '拍一张' }}
        </button>
        <button v-if="phase === 'live'" type="button" class="pc-tool" :class="{ 'is-on': settings.auto }" @click="toggleAuto">
          自动<small>{{ settings.auto ? '开' : '关' }}</small>
        </button>
        <button type="button" class="pc-tool" @click="settingsOpen = true">更多<small>设置</small></button>
      </footer>
      <p v-if="sessionFull" class="pc-note pc-bottom-note" role="alert">已达本次上限，请先在电脑上导入后再继续。</p>
    </section>

    <div v-if="settingsOpen" class="pc-sheet" role="dialog" aria-modal="true" aria-label="拍摄设置" @click.self="settingsOpen = false">
      <div class="pc-sheet-body pc-settings">
        <div v-if="phase === 'live'" class="pc-setting-row">
          <span>拍照间隔</span>
          <div class="pc-segmented" role="group" aria-label="拍照间隔" data-test="cooldown">
            <button
              v-for="seconds in COOLDOWN_CHOICES"
              :key="seconds"
              type="button"
              :class="{ 'is-on': settings.cooldown === seconds }"
              :aria-pressed="settings.cooldown === seconds"
              @click="settings.cooldown = seconds"
            >{{ seconds }} 秒</button>
          </div>
          <small>拍完一张后至少等这么久才自动拍下一张；换纸慢就调大。</small>
        </div>
        <div class="pc-setting-row">
          <span>提示音</span>
          <button type="button" :class="{ 'is-on': settings.sound }" @click="toggleSound">{{ settings.sound ? '开' : '关' }}</button>
          <button type="button" :disabled="!settings.sound" @click="previewSound">试听</button>
        </div>
        <div v-if="phase === 'live' && stillCamera" class="pc-setting-row">
          <span>高清拍照</span>
          <button type="button" :class="{ 'is-on': settings.hd }" data-test="hd-toggle" @click="settings.hd = !settings.hd">{{ settings.hd ? '开' : '关' }}</button>
          <small>用相机拍照模式，比取景画面清晰，每张约慢 1 秒</small>
        </div>
        <p v-if="phase === 'live'" class="pc-setting-note" data-test="diagnostics">{{ diagnostics }}</p>
        <div class="pc-sheet-actions">
          <button v-if="phase === 'live'" type="button" @click="switchToSystemCamera">改用系统相机</button>
          <button type="button" class="pc-primary-sm" @click="settingsOpen = false">完成</button>
        </div>
      </div>
    </div>

    <div v-if="selectedShot" class="pc-sheet" role="dialog" aria-modal="true" aria-label="照片操作">
      <div class="pc-sheet-body">
        <img :src="selectedShot.thumbUrl" alt="选中的照片" />
        <p>{{ SHOT_STATUS[selectedShot.status].text }}</p>
        <p class="pc-setting-note">{{ shotInfoText(selectedShot) }}</p>
        <p v-if="selectedShot.error" class="pc-bad">{{ selectedShot.error }}</p>
        <p v-if="selectedShot.blurry" class="pc-note" data-test="blurry-note">
          这张可能拍糊了或字迹太淡{{ selectedShot === latestShot ? '，可删除并重拍。' : '。如需重拍，删除后把试卷放回画面，新照片会排在最后。' }}
        </p>
        <div class="pc-sheet-actions">
          <button v-if="selectedShot.status === 'failed'" type="button" class="pc-primary-sm" @click="retryShot(selectedShot)">重新上传</button>
          <button v-if="selectedShot.blurry && selectedShot === latestShot" type="button" class="pc-primary-sm" :disabled="selectedShot.status === 'uploading'" @click="retakeShot(selectedShot)">删除并重拍</button>
          <button type="button" class="pc-danger" :disabled="selectedShot.status === 'uploading'" @click="deleteShot(selectedShot)">删除这张</button>
          <button type="button" @click="selectedShot = null">取消</button>
        </div>
      </div>
    </div>

    <div v-if="toast" class="pc-toast" role="status" data-test="toast">{{ toast }}</div>
    <input ref="systemCameraRef" type="file" accept="image/*" capture="environment" hidden @change="onSystemPhoto" />
  </div>
</template>

<script setup>
// The phone half of 手机拍摄: a phone on a stand photographs each sheet the
// teacher puts down and uploads it to the session the desktop QR code opened.
// Camera, canvas, analysis and upload details live in the capture* utils;
// this page wires them to the interface.
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, shallowRef, watch } from 'vue'
import { useRoute } from 'vue-router'
import { phoneCaptureClient } from '@/api'
import {
  ANALYSIS_WIDTH,
  SIGNATURE_WIDTH,
  captureHints,
  containBox,
  createAutoCapture,
  currentScreenAngle,
  detectPaper,
  deviceTiltDegrees,
  downsample2x,
  focusBox,
  inkFraction,
  isSideways,
  levelState,
  nextShootAngle,
  normalizeRotation,
  rotatedSize,
  sharpness,
  shootingLayout,
} from '@/utils/captureVision'
import { copyFrame, createFrameSampler, loadImageFile, renderShot, scaledHeight } from '@/utils/captureCanvas'
import {
  cameraErrorMessage,
  hasTorch,
  openCameraStream,
  openStillCamera,
  preferContinuousFocus,
  setTorch,
  takeAlignedStill,
  upgradeStreamIfSmooth,
  waitForVideo,
  watchFrameRate,
} from '@/utils/captureCamera'
import { COOLDOWN_CHOICES, loadCaptureSettings, saveCaptureSettings } from '@/utils/captureSettings'
import { createCaptureUploads } from '@/utils/captureUploads'
import { playCaptureCue, unlockCaptureSound } from '@/utils/captureSound'
import { isCaptureSessionId, takePhoneCaptureToken } from '@/utils/phoneCaptureSession'

const ANALYSIS_INTERVAL_MS = 120
const SHOOT_MODES = [
  { kind: 'portrait', label: '竖拍' },
  { kind: 'landscape', label: '横拍' },
]
const SHOT_STATUS = {
  pending: { badge: '…', text: '等待上传' },
  uploading: { badge: '↑', text: '正在上传' },
  done: { badge: '✓', text: '已上传到电脑' },
  failed: { badge: '!', text: '上传失败' },
}
const SHOT_SOURCES = { photo: '高清拍照', frame: '取景画面', system: '系统相机' }

const liveSupported = window.isSecureContext && Boolean(navigator.mediaDevices?.getUserMedia)
const liveUnsupportedReason = window.isSecureContext
  ? '当前浏览器不支持网页取景（部分 App 内置浏览器如此）。请用系统浏览器打开此链接，或改用系统相机。'
  : '当前地址不是 HTTPS，浏览器不允许网页打开实时取景，只能改用系统相机逐张拍照。'

const sessionId = String(useRoute().params.sessionId || '')
const token = takePhoneCaptureToken(sessionId)
const phoneApi = phoneCaptureClient(sessionId, token)
const settings = reactive(loadCaptureSettings())
watch(settings, () => saveCaptureSettings(settings))

// checking → start → live or fallback; ended from any of them.
const phase = ref('checking')
const fatalMessage = ref('')
const cameraError = ref('')
const starting = ref(false)
const takingPhoto = ref(false)
const processingPhoto = ref(false)
const selectedShot = ref(null)
const settingsOpen = ref(false)
const toast = ref('')
const flashOn = ref(false)
let toastTimer = 0
let capturing = false

const uploads = createCaptureUploads({
  upload: phoneApi.upload,
  remove: phoneApi.remove,
  onSessionEnded: endSession,
  onFailed: () => cue('warn'),
})
const { shots, sessionFull, uploadedCount, hasFailed } = uploads
const latestShot = computed(() => shots.value.at(-1) || null)

// Camera
let stream = null
let stopFrameRate = null
let wakeLock = null
let stillFailures = 0
const stillCamera = shallowRef(null)
const torchSupported = ref(false)
const torchOn = ref(false)
const streamSize = ref({ width: 0, height: 0 })
const measuredFps = ref(0)

// Frame analysis
const autoCapture = createAutoCapture()
const sampleFrame = createFrameSampler()
let analysisHandle = 0
let lastAnalysisAt = 0
let lastSignature = null
const tilt = ref(null)
const paper = ref(null)
const hints = ref([])
const autoState = ref('empty')
const cooldownLeft = ref(0) // whole seconds, while autoState is 'cooldown'

// Layout: the screen angle the system shows (settings.shootAngle is the
// teacher's choice), the window, and the preview area inside it.
const screenAngle = ref(currentScreenAngle())
const viewSize = ref({ width: window.innerWidth, height: window.innerHeight })
const viewportSize = ref({ width: 0, height: 0 })
const viewportRef = ref(null)
const previewRef = ref(null)
const systemCameraRef = ref(null)
let viewportObserver = null

const layout = computed(() => shootingLayout(settings.shootAngle, screenAngle.value))
const imageRotation = computed(() => layout.value.imageRotation)
const shootMode = computed(() => (layout.value.landscape ? 'landscape' : 'portrait'))
// Turn the whole page with CSS when the system has not: a flat phone never
// auto-rotates.  Fixed pixel sizes avoid mobile 100vh quirks.
const rootStyle = computed(() => {
  const { width, height } = viewSize.value
  const turn = layout.value.uiTurn
  const sideways = { width: `${height}px`, height: `${width}px`, right: 'auto', bottom: 'auto', transformOrigin: 'top left', padding: '0' }
  if (turn === 90) return { ...sideways, transform: `translateX(${width}px) rotate(90deg)` }
  if (turn === 270) return { ...sideways, transform: `translateY(${height}px) rotate(-90deg)` }
  if (turn === 180) return { transform: 'rotate(180deg)', padding: '0' }
  return {}
})
// Wide by the page's own shape, which includes the CSS turn for 横拍.
const isWide = computed(() => {
  const { width, height } = viewSize.value
  return isSideways(layout.value.uiTurn) ? height > width : width > height
})
// The camera frame turned like the upload and fitted into the preview area
// (all of it until the camera reports its size).
const previewBox = computed(() => {
  const area = viewportSize.value
  if (!streamSize.value.width) return { x: 0, y: 0, ...area }
  const [width, height] = rotatedSize(streamSize.value.width, streamSize.value.height, imageRotation.value)
  return containBox(width, height, area.width, area.height)
})
const frameStyle = computed(() => boxStyle(previewBox.value))
// The preview is the <video> itself, composited by the phone at the camera's
// frame rate; CSS turns it exactly like the uploaded photo.
const previewStyle = computed(() => {
  const box = previewBox.value
  const [width, height] = rotatedSize(box.width, box.height, imageRotation.value)
  return {
    ...boxStyle({ x: box.x + (box.width - width) / 2, y: box.y + (box.height - height) / 2, width, height }),
    transform: imageRotation.value ? `rotate(${imageRotation.value}deg)` : 'none',
  }
})

const level = computed(() => levelState(tilt.value))
const levelText = computed(() => {
  if (level.value === 'level') return '手机水平'
  if (level.value === 'slight') return `略斜 ${Math.round(tilt.value)}°`
  return `倾斜 ${Math.round(tilt.value)}°`
})
const bubbleStyle = computed(() => {
  const offset = Math.min(1, (tilt.value || 0) / 20) * 9
  return { transform: `translate(${offset}px, ${-offset}px)` }
})
const paperOutline = computed(() => {
  const outline = paper.value?.outline
  if (!paper.value?.present || !outline) return ''
  return outline.map(([x, y]) => `${x.toFixed(4)},${y.toFixed(4)}`).join(' ')
})
const blockingHint = computed(() => hints.value.find((hint) => hint.tone === 'block') || null)
const secondaryHint = computed(() => hints.value.find((hint) => hint.tone !== 'block' && hint.code !== 'no_paper')?.text || '')
const outlineTone = computed(() => {
  if (blockingHint.value) return 'block'
  if (autoState.value === 'captured') return 'done'
  if (paper.value?.confident) return 'ok'
  return 'unsure'
})
// The line under the preview; `tone` colours it (blocked red, done green).
const status = computed(() => {
  if (takingPhoto.value) return { text: '拍照中，请勿移动…' }
  if (sessionFull.value) return { text: '已达上限' }
  if (blockingHint.value) return { text: blockingHint.value.text, tone: settings.auto ? 'block' : '' }
  if (autoState.value === 'empty') return { text: '请放入试卷' }
  if (autoState.value === 'captured') return { text: '已拍 ✓ 请换下一张', tone: 'done' }
  if (!settings.auto) return { text: '自动拍照已关闭，请点快门' }
  if (autoState.value === 'moving') return { text: '保持不动…' }
  if (autoState.value === 'unsure') return { text: '没认出整张试卷，请把整张纸平放在画面中间' }
  if (autoState.value === 'small') return { text: '试卷太小，请降低支架或让手机靠近试卷' }
  if (autoState.value === 'focusing') return { text: '对焦中…' }
  if (autoState.value === 'blank') return { text: '看不清纸上的字，请等对焦或加强光线' }
  if (autoState.value === 'cooldown') return { text: `${cooldownLeft.value} 秒后拍照` }
  return { text: '准备拍照' }
})
// Auto-capture waits while the teacher is busy with a photo or the settings.
const autoPaused = computed(() => sessionFull.value || Boolean(selectedShot.value) || settingsOpen.value || takingPhoto.value)
const resolutionLow = computed(() => Math.max(streamSize.value.width, streamSize.value.height) < 1900)
const diagnostics = computed(() => {
  const parts = [`取景 ${streamSize.value.width}×${streamSize.value.height} · ${measuredFps.value || '—'} 帧/秒`]
  const still = stillCamera.value
  parts.push(still ? `拍照 ${still.width}×${still.height}${settings.hd ? '' : '（已关）'}` : '本机浏览器不支持高清拍照，使用取景画面')
  parts.push(torchSupported.value ? '可补光' : '不支持补光')
  return parts.join(' · ')
})

function boxStyle({ x, y, width, height }) {
  return { left: `${x}px`, top: `${y}px`, width: `${width}px`, height: `${height}px` }
}

function shotInfoText({ info, mode }) {
  return `${SHOT_SOURCES[mode]} ${info.sourceWidth}×${info.sourceHeight} → 上传 ${info.width}×${info.height}${info.cropped ? '（已裁到纸张）' : ''}`
}

function cue(kind) {
  if (settings.sound) playCaptureCue(kind)
}

function showToast(message) {
  toast.value = message
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 2200)
}

function flashScreen() {
  flashOn.value = true
  setTimeout(() => { flashOn.value = false }, 140)
}

function endSession(message) {
  fatalMessage.value = message || '拍摄链接已失效，请在电脑上重新打开二维码并扫码。'
  phase.value = 'ended'
  selectedShot.value = null
  settingsOpen.value = false
  stopCamera()
  cue('warn')
}

// ── Settings ──

function chooseShootMode(kind) {
  const flipped = (kind === 'landscape') === layout.value.landscape
  settings.shootAngle = nextShootAngle(settings.shootAngle, kind, screenAngle.value)
  showToast(flipped ? '已上下翻转' : kind === 'landscape' ? '横拍：手机横放，试卷正常摆放' : '竖拍')
  autoCapture.reset()
}

function toggleAuto() {
  settings.auto = !settings.auto
  autoCapture.reset()
}

function toggleSound() {
  settings.sound = !settings.sound
  if (settings.sound) previewSound()
}

function previewSound() {
  unlockCaptureSound()
  playCaptureCue('shutter')
}

function videoTrack() {
  return stream?.getVideoTracks()[0] || null
}

async function switchTorch(on) {
  await setTorch(videoTrack(), on)
  torchOn.value = on
}

async function toggleTorch() {
  try {
    await switchTorch(!torchOn.value)
    settings.torch = torchOn.value
    autoCapture.reset()
  } catch {
    torchSupported.value = false
  }
}

// ── Live camera ──

async function startLive() {
  // Sound and motion access must be asked for inside the tap itself.
  if (settings.sound) unlockCaptureSound()
  const motionPermission = requestMotionPermission()
  starting.value = true
  cameraError.value = ''
  try {
    stream = await openCameraStream()
    const track = videoTrack()
    await preferContinuousFocus(track)
    phase.value = 'live'
    await nextTick()
    const video = previewRef.value
    video.srcObject = stream
    await video.play().catch(() => {})
    await waitForVideo(video)
    stillCamera.value = await openStillCamera(track, video)
    torchSupported.value = hasTorch(track)
    if (torchSupported.value && settings.torch) switchTorch(true).catch(() => {})
    // Listen whatever the answer: it only decides whether events arrive, and
    // the level badge stays hidden until one does.
    await motionPermission
    window.addEventListener('deviceorientation', onDeviceOrientation)
    startFrameRateReadout(video, track)
    // Without full-resolution stills the stream is the photo; try for more pixels.
    if (!stillCamera.value) upgradeStreamIfSmooth(track, video)
    requestWakeLock()
    autoCapture.reset()
    analysisHandle = requestAnimationFrame(analysisLoop)
    cue('shutter')
    // Some phones only report the torch once frames are flowing.
    setTimeout(() => {
      if (phase.value === 'live' && !torchSupported.value) torchSupported.value = hasTorch(videoTrack())
    }, 1200)
  } catch (err) {
    stopCamera()
    if (phase.value === 'live') phase.value = 'start'
    cameraError.value = cameraErrorMessage(err)
  } finally {
    starting.value = false
  }
}

function stopCamera() {
  cancelAnimationFrame(analysisHandle)
  analysisHandle = 0
  stopFrameRate?.()
  stopFrameRate = null
  if (previewRef.value) previewRef.value.srcObject = null
  stream?.getTracks().forEach((track) => track.stop())
  stream = null
  stillCamera.value = null
  torchOn.value = false
  window.removeEventListener('deviceorientation', onDeviceOrientation)
  wakeLock?.release().catch(() => {})
  wakeLock = null
}

function syncStreamSize() {
  const video = previewRef.value
  if (video?.videoWidth) streamSize.value = { width: video.videoWidth, height: video.videoHeight }
}

// Shown under 更多: the rate the camera really delivers.
function startFrameRateReadout(video, track) {
  stopFrameRate = watchFrameRate(video, (fps) => { measuredFps.value = Math.round(fps) })
  // Without frame callbacks, show what the camera was set to.
  if (!stopFrameRate) measuredFps.value = Math.round(track?.getSettings?.().frameRate || 0)
}

// iOS only grants motion access when asked inside a tap.
function requestMotionPermission() {
  const request = globalThis.DeviceOrientationEvent?.requestPermission
  return typeof request === 'function'
    ? request.call(globalThis.DeviceOrientationEvent).catch(() => 'denied')
    : Promise.resolve('granted')
}

async function requestWakeLock() {
  try {
    if (navigator.wakeLock && document.visibilityState === 'visible') wakeLock = await navigator.wakeLock.request('screen')
  } catch {
    wakeLock = null
  }
}

// ── Analysis and capture ──

// The browser draws the preview; this loop only runs detection a few times a second.
function analysisLoop(now) {
  analysisHandle = requestAnimationFrame(analysisLoop)
  const video = previewRef.value
  if (!video?.videoWidth || now - lastAnalysisAt < ANALYSIS_INTERVAL_MS) return
  lastAnalysisAt = now
  analyse(video, now)
}

function analyse(video, now) {
  const turn = imageRotation.value
  const analysisHeight = scaledHeight(ANALYSIS_WIDTH, rotatedSize(video.videoWidth, video.videoHeight, turn))
  const signatureHeight = analysisHeight * 2
  const signature = sampleFrame(video, turn, SIGNATURE_WIDTH, signatureHeight)
  const gray = downsample2x(signature, SIGNATURE_WIDTH, signatureHeight)
  const detected = detectPaper(gray, ANALYSIS_WIDTH, analysisHeight)
  lastSignature = signature
  paper.value = detected
  hints.value = captureHints({
    paper: detected,
    tilt: tilt.value,
    resolutionLow: resolutionLow.value && !stillCamera.value,
    torchOff: torchSupported.value && !torchOn.value,
  })
  const inside = focusBox(detected)
  const decision = autoCapture.update({
    gray,
    signature,
    time: now,
    paper: detected,
    sharpness: sharpness(signature, SIGNATURE_WIDTH, signatureHeight, inside),
    ink: inkFraction(signature, SIGNATURE_WIDTH, signatureHeight, inside),
    blocked: Boolean(blockingHint.value),
    enabled: settings.auto && !autoPaused.value,
    cooldownMs: settings.cooldown * 1000,
  })
  autoState.value = decision.state
  cooldownLeft.value = decision.state === 'cooldown' ? Math.ceil(decision.waitMs / 1000) : 0
  if (decision.action === 'capture') capturePhoto()
}

async function capturePhoto({ manual = false } = {}) {
  const video = previewRef.value
  if (capturing || !video?.videoWidth || sessionFull.value) return
  capturing = true
  takingPhoto.value = true
  try {
    const source = await grabSource(video)
    // The cue means "safe to swap the paper", so it only sounds once the
    // picture is taken, and the cooldown before the next automatic shot
    // starts at the same moment.
    takingPhoto.value = false
    autoCapture.startCooldown(performance.now())
    if (manual) autoCapture.markCaptured(lastSignature)
    flashScreen()
    cue('shutter')
    const turn = normalizeRotation(source.turn + imageRotation.value)
    addShot({ ...(await renderShot(source.image, turn)), mode: source.mode })
    source.image.close?.()
  } catch {
    cue('warn')
  } finally {
    takingPhoto.value = false
    capturing = false
  }
}

// A full-resolution still where the browser offers one and it lines up with
// the preview; otherwise the live frame.
async function grabSource(video) {
  const frame = copyFrame(video)
  const fallback = { image: frame, turn: 0, mode: 'frame' }
  const camera = stillCamera.value
  // With the torch on, a still must fire the flash; otherwise keep the lit
  // live frame.
  if (!camera || !settings.hd || (torchOn.value && !camera.canFlash)) return fallback
  try {
    const still = await takeAlignedStill(camera, video, frame, { flash: torchOn.value })
    if (!still) return fallback
    stillFailures = 0
    return { image: still.image, turn: still.turn, mode: 'photo' }
  } catch {
    stillFailures += 1
    if (stillFailures >= 2) stillCamera.value = null
    return fallback
  } finally {
    // Some phones drop the torch while taking a still.
    if (torchOn.value) switchTorch(true).catch(() => {})
  }
}

// ── System camera fallback ──

function startFallback() {
  if (settings.sound) unlockCaptureSound()
  phase.value = 'fallback'
}

function switchToSystemCamera() {
  settingsOpen.value = false
  stopCamera()
  phase.value = 'fallback'
}

function openSystemCamera() {
  if (!sessionFull.value) systemCameraRef.value?.click()
}

async function onSystemPhoto(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  processingPhoto.value = true
  try {
    const { image, release } = await loadImageFile(file)
    try {
      const photo = { ...(await renderShot(image, imageRotation.value)), mode: 'system' }
      cue('shutter')
      addShot(photo)
    } finally {
      release()
    }
  } catch {
    cue('warn')
  } finally {
    processingPhoto.value = false
  }
}

// ── Taken photos ──

// A blurry photo is flagged at once, while its sheet is usually still on the
// stand, so the teacher can retake it.
function addShot(photo) {
  uploads.add(photo)
  if (!photo.blurry) return
  cue('warn')
  showToast(`第 ${shots.value.length} 张可能拍糊了，点缩略图查看`)
}

// Only offered for the latest photo, whose sheet is the one in view (or has
// just been swapped for a new one): re-arming automatic capture shoots it
// again once it is still.
async function retakeShot(shot) {
  if (!(await uploads.discard(shot))) return
  selectedShot.value = null
  autoCapture.reset()
  showToast(settings.auto ? '已删除，试卷保持不动会自动重拍' : '已删除，请点快门重拍')
}

function retryShot(shot) {
  selectedShot.value = null
  uploads.retry(shot)
}

async function deleteShot(shot) {
  if (await uploads.discard(shot)) selectedShot.value = null
}

// ── Page events ──

function onResize() {
  viewSize.value = { width: window.innerWidth, height: window.innerHeight }
}

function onScreenOrientationChange() {
  onResize()
  const angle = currentScreenAngle()
  if (angle === screenAngle.value) return
  // The system only turns the screen when it sees the phone being turned, so
  // the shooting mode follows it.
  screenAngle.value = angle
  settings.shootAngle = angle
  showToast(isSideways(angle) ? '已切换为横拍' : '已切换为竖拍')
  autoCapture.reset()
}

function onDeviceOrientation(event) {
  tilt.value = deviceTiltDegrees(event.beta, event.gamma)
}

function onVisibilityChange() {
  if (document.visibilityState === 'visible' && phase.value === 'live') {
    requestWakeLock()
    autoCapture.reset()
  }
}

function onBeforeUnload(event) {
  // Once the session has ended nothing more can upload; do not nag.
  if (phase.value !== 'ended' && uploads.hasUnsent.value) {
    event.preventDefault()
    event.returnValue = ''
  }
}

// The preview box follows the preview area, which changes with rotation, the
// wide layout and the notes around it.
watch(viewportRef, (element) => {
  viewportObserver?.disconnect()
  viewportObserver = null
  if (!element) return
  viewportObserver = new ResizeObserver(([entry]) => {
    viewportSize.value = { width: entry.contentRect.width, height: entry.contentRect.height }
  })
  viewportObserver.observe(element)
}, { flush: 'post' })

onMounted(async () => {
  // If the system already shows the screen sideways, the phone is sideways.
  if (screenAngle.value !== 0) settings.shootAngle = screenAngle.value
  window.addEventListener('beforeunload', onBeforeUnload)
  document.addEventListener('visibilitychange', onVisibilityChange)
  window.addEventListener('resize', onResize)
  window.addEventListener('orientationchange', onScreenOrientationChange)
  screen.orientation?.addEventListener?.('change', onScreenOrientationChange)
  if (!isCaptureSessionId(sessionId) || !token) {
    endSession('拍摄链接不完整，请在电脑上重新打开二维码并扫码。')
    return
  }
  try {
    const state = await phoneApi.state()
    sessionFull.value = (state.images?.length || 0) >= (Number(state.max_images) || 200)
    phase.value = 'start'
  } catch (err) {
    endSession(err.status === 403 || err.status === 410 ? err.message : `${err.message}。请检查网络后刷新本页。`)
  }
})

onBeforeUnmount(() => {
  stopCamera()
  viewportObserver?.disconnect()
  clearTimeout(toastTimer)
  uploads.dispose()
  window.removeEventListener('beforeunload', onBeforeUnload)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  window.removeEventListener('resize', onResize)
  window.removeEventListener('orientationchange', onScreenOrientationChange)
  screen.orientation?.removeEventListener?.('change', onScreenOrientationChange)
})
</script>

<style scoped>
.phone-capture {
  --pc-bg: #0d0f12;
  --pc-panel: #1a1d22;
  --pc-text: #f3f4f6;
  --pc-muted: #a3a8b0;
  --pc-ok: #3ccf7a;
  --pc-warn: #f5b84b;
  --pc-bad: #ff6b6b;
  --pc-accent: #4c9dff;
  --pc-line: #3a3f47;
  position: fixed;
  inset: 0;
  display: flex;
  flex-direction: column;
  background: var(--pc-bg);
  color: var(--pc-text);
  font-size: 15px;
  padding: env(safe-area-inset-top, 0px) env(safe-area-inset-right, 0px) env(safe-area-inset-bottom, 0px) env(safe-area-inset-left, 0px);
  overflow: hidden;
  -webkit-user-select: none;
  user-select: none;
}
.pc-center {
  margin: auto;
  width: min(440px, 100%);
  padding: 24px 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  overflow-y: auto;
  max-height: 100%;
}
.pc-center h1 { margin: 0; font-size: 22px; }
.pc-steps { margin: 0; padding-left: 20px; line-height: 1.7; color: var(--pc-muted); }
.pc-note { margin: 0; color: var(--pc-warn); line-height: 1.6; font-size: 14px; }
.pc-primary, .pc-primary-sm {
  border: 0;
  border-radius: 12px;
  background: var(--pc-accent);
  color: #fff;
  font-weight: 600;
}
.pc-primary { min-height: 52px; font-size: 18px; }
.pc-primary:disabled { opacity: 0.5; }
.pc-primary-sm { padding: 10px 14px; }
.pc-link { background: none; border: 0; color: var(--pc-accent); font-size: 15px; padding: 8px; }
.pc-work { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.pc-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  font-size: 14px;
  flex-wrap: wrap;
}
.pc-count { font-weight: 600; }
.pc-bad { color: var(--pc-bad); margin: 0; }
.pc-level { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; color: var(--pc-ok); }
.pc-level.is-slight { color: var(--pc-warn); }
.pc-level.is-tilted { color: var(--pc-bad); }
.pc-level-dial {
  position: relative;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 2px solid currentColor;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.pc-level-bubble { width: 8px; height: 8px; border-radius: 50%; background: currentColor; transition: transform 0.2s ease; }
.pc-viewport { position: relative; flex: 1; min-height: 0; background: #000; }
.pc-preview { position: absolute; object-fit: fill; background: #000; transform-origin: center; }
.pc-frame { position: absolute; pointer-events: none; }
.pc-top-mark {
  position: absolute;
  top: 6px;
  left: 50%;
  transform: translateX(-50%);
  padding: 2px 10px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.55);
  font-size: 12px;
  white-space: nowrap;
}
.pc-guide {
  position: absolute;
  inset: 8% 12%;
  border: 2px dashed rgba(255, 255, 255, 0.55);
  border-radius: 6px;
}
.pc-outline { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
.pc-outline polygon { fill: none; stroke-width: 3; }
.pc-outline .is-ok { stroke: var(--pc-ok); }
.pc-outline .is-done { stroke: var(--pc-accent); }
.pc-outline .is-block { stroke: var(--pc-bad); }
.pc-outline .is-unsure { stroke: rgba(255, 255, 255, 0.7); stroke-dasharray: 6 5; }
.pc-status {
  position: absolute;
  left: 50%;
  bottom: 14px;
  transform: translateX(-50%);
  max-width: calc(100% - 32px);
  padding: 8px 16px;
  border-radius: 14px;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 2px;
}
.pc-status strong { font-size: 16px; }
.pc-status span { font-size: 13px; color: var(--pc-warn); }
.pc-status.is-block strong { color: var(--pc-bad); }
.pc-status.is-done strong { color: var(--pc-ok); }
.pc-flash { position: absolute; inset: 0; background: #fff; opacity: 0.6; animation: pc-flash 0.14s ease-out forwards; }
@keyframes pc-flash { to { opacity: 0; } }
.pc-toast {
  position: absolute;
  left: 50%;
  top: 56px;
  transform: translateX(-50%);
  z-index: 20;
  padding: 8px 16px;
  border-radius: 999px;
  background: rgba(76, 157, 255, 0.92);
  color: #fff;
  font-size: 14px;
  white-space: nowrap;
  pointer-events: none;
}
/* Joined choice buttons: 竖拍/横拍 and the shot interval. */
.pc-segmented { display: inline-flex; flex: 0 0 auto; border: 1px solid var(--pc-line); border-radius: 12px; overflow: hidden; }
.pc-segmented button { min-width: 46px; min-height: 48px; padding: 0 8px; border: 0; background: var(--pc-panel); color: var(--pc-text); font-size: 14px; }
.pc-segmented button + button { border-left: 1px solid var(--pc-line); }
.pc-segmented button.is-on { background: var(--pc-accent); color: #fff; font-weight: 600; }
.pc-segmented-large { align-self: center; }
.pc-segmented-large button { min-width: 96px; font-size: 16px; }
.pc-fallback { flex: 1; padding: 16px; color: var(--pc-muted); line-height: 1.7; overflow-y: auto; }
.pc-strip {
  display: flex;
  gap: 8px;
  padding: 8px 16px;
  overflow-x: auto;
  min-height: 72px;
  scrollbar-width: none;
}
.pc-thumb {
  position: relative;
  flex: 0 0 auto;
  width: 52px;
  height: 64px;
  padding: 0;
  border-radius: 8px;
  border: 2px solid transparent;
  background: var(--pc-panel);
  overflow: hidden;
}
.pc-thumb img { width: 100%; height: 100%; object-fit: cover; }
.pc-thumb.is-done { border-color: rgba(60, 207, 122, 0.6); }
.pc-thumb.is-blurry { border-color: var(--pc-warn); }
.pc-thumb.is-failed { border-color: var(--pc-bad); }
.pc-thumb-blur {
  position: absolute;
  right: 0;
  top: 0;
  font-size: 10px;
  line-height: 1;
  padding: 2px 3px;
  border-bottom-left-radius: 6px;
  background: var(--pc-warn);
  color: #1a1a1a;
}
.pc-thumb-no, .pc-thumb-state {
  position: absolute;
  font-size: 11px;
  line-height: 1;
  padding: 2px 4px;
  background: rgba(0, 0, 0, 0.65);
  color: #fff;
}
.pc-thumb-no { left: 0; top: 0; border-bottom-right-radius: 6px; }
.pc-thumb-state { right: 0; bottom: 0; border-top-left-radius: 6px; }
.pc-thumb.is-failed .pc-thumb-state { background: var(--pc-bad); }
.pc-controls {
  display: flex;
  align-items: center;
  justify-content: space-around;
  gap: 8px;
  padding: 10px 12px 14px;
}
.pc-tool {
  min-width: 50px;
  min-height: 48px;
  border-radius: 12px;
  border: 1px solid var(--pc-line);
  background: var(--pc-panel);
  color: var(--pc-text);
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  font-size: 13px;
}
.pc-tool small { color: var(--pc-muted); font-size: 11px; }
.pc-tool.is-on small { color: var(--pc-ok); }
.pc-shutter {
  flex: 0 0 auto;
  width: 72px;
  height: 72px;
  border-radius: 50%;
  border: 4px solid #fff;
  background: rgba(255, 255, 255, 0.25);
  box-shadow: inset 0 0 0 4px var(--pc-bg);
}
.pc-shutter:active { background: rgba(255, 255, 255, 0.6); }
.pc-shutter:disabled, .pc-shutter-text:disabled { opacity: 0.4; }
.pc-shutter-text {
  min-width: 110px;
  min-height: 56px;
  border-radius: 28px;
  border: 0;
  background: #fff;
  color: #111;
  font-size: 17px;
  font-weight: 600;
}
.pc-bottom-note { padding: 0 16px 10px; text-align: center; }
.pc-sheet {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: flex-end;
  justify-content: center;
  z-index: 10;
}
.pc-sheet-body {
  width: min(480px, 100%);
  max-height: calc(100% - 24px);
  overflow-y: auto;
  margin: 0 16px calc(16px + env(safe-area-inset-bottom, 0px));
  padding: 16px;
  border-radius: 16px;
  background: var(--pc-panel);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}
.pc-sheet-body img { max-height: 40vh; max-width: 100%; border-radius: 8px; }
.pc-sheet-body p { margin: 0; }
.pc-sheet-actions { display: flex; gap: 10px; flex-wrap: wrap; justify-content: center; }
.pc-sheet-actions button:not(.pc-primary-sm),
.pc-setting-row > button {
  padding: 10px 14px;
  border-radius: 12px;
  border: 1px solid var(--pc-line);
  background: transparent;
  color: var(--pc-text);
}
.pc-setting-row > button.is-on { border-color: var(--pc-ok); color: var(--pc-ok); }
.pc-setting-row > button:disabled { opacity: 0.4; }
.pc-sheet-actions .pc-danger { border-color: var(--pc-bad); color: var(--pc-bad); }
.pc-settings { align-items: stretch; }
.pc-setting-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.pc-setting-row > span { min-width: 64px; font-weight: 600; }
.pc-setting-row small, .pc-setting-note { color: var(--pc-muted); font-size: 12px; line-height: 1.6; }
.phone-capture.is-wide .pc-work {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  grid-template-rows: auto minmax(0, 1fr) auto;
}
.phone-capture.is-wide .pc-bar { grid-column: 1 / -1; padding: 6px 12px; }
.phone-capture.is-wide .pc-viewport, .phone-capture.is-wide .pc-fallback { grid-column: 1; grid-row: 2 / 4; }
.phone-capture.is-wide .pc-strip {
  grid-column: 2;
  grid-row: 2 / 4;
  flex-direction: column;
  overflow-y: auto;
  overflow-x: hidden;
  min-height: 0;
  padding: 6px;
}
.phone-capture.is-wide .pc-controls {
  grid-column: 3;
  grid-row: 2 / 4;
  flex-direction: column;
  justify-content: center;
  gap: 6px;
  padding: 6px 10px;
}
.phone-capture.is-wide .pc-tool { min-height: 40px; min-width: 60px; }
.phone-capture.is-wide .pc-controls .pc-segmented button { min-height: 40px; }
.phone-capture.is-wide .pc-shutter { width: 62px; height: 62px; }
.phone-capture.is-wide .pc-bottom-note { grid-column: 1 / -1; }
.phone-capture.is-wide .pc-sheet { align-items: center; }
</style>
