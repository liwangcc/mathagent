<template>
  <el-dialog
    :model-value="modelValue"
    title="用手机拍摄试卷"
    width="min(760px, 94vw)"
    append-to-body
    :close-on-click-modal="false"
    :close-on-press-escape="!importing"
    :show-close="!importing"
    @update:model-value="(value) => emit('update:modelValue', value)"
    @open="onOpen"
    @closed="onClosed"
  >
    <div class="pcd-layout">
      <div class="pcd-qr-col">
        <div class="pcd-qr" data-test="phone-capture-qr">
          <svg v-if="qr" :viewBox="`-4 -4 ${qr.size} ${qr.size}`" role="img" aria-label="手机拍摄二维码" shape-rendering="crispEdges">
            <rect x="-4" y="-4" :width="qr.size" :height="qr.size" fill="#fff" />
            <path :d="qr.path" fill="#111" />
          </svg>
          <span v-else class="pcd-qr-placeholder">{{ loading ? '正在生成…' : '暂无二维码' }}</span>
        </div>
        <p class="pcd-qr-tip">用手机相机或浏览器扫码打开（不需要登录）。</p>
        <div v-if="captureUrl" class="pcd-link-row">
          <el-button size="small" @click="copyLink">复制链接</el-button>
          <el-button size="small" :loading="loading" @click="reissue">刷新二维码</el-button>
        </div>
        <p v-if="expiresText" class="pcd-muted">有效至 {{ expiresText }}；刷新后旧二维码失效，已拍照片保留。</p>
      </div>

      <div class="pcd-main">
        <el-alert v-if="originWarning" type="warning" :closable="false" show-icon :title="originWarning" class="pcd-alert" />
        <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" class="pcd-alert" />
        <div class="pcd-status" role="status">
          <span class="pcd-dot" :class="{ 'is-on': phoneConnected }"></span>
          {{ phoneConnected ? `手机已连接 · 最近活动 ${lastSeenText}` : '等待手机扫码…' }}
          <strong class="pcd-count" data-test="phone-capture-count">已收到 {{ images.length }} 张</strong>
        </div>
        <ol class="pcd-hints">
          <li>手机镜头朝下固定在支架上，尽量放平；试卷顶部朝向取景框的“▲”。</li>
          <li>放好一张、停稳后会自动拍照，听到提示音再换下一张。</li>
          <li>点缩略图可放大查看清晰度；照片按拍摄顺序导入，拍歪、拍糊的可以在这里或手机上删除。</li>
        </ol>
        <div class="pcd-grid" aria-label="已收到的照片">
          <figure v-for="(image, index) in images" :key="image.seq" class="pcd-thumb">
            <el-image
              v-if="downloads.has(image.seq)"
              :src="downloads.get(image.seq).url"
              :alt="`第 ${index + 1} 张`"
              fit="cover"
              :preview-src-list="previewList"
              :initial-index="index"
              preview-teleported
              hide-on-click-modal
              :title="`点击查看原图 ${image.width}×${image.height}`"
            />
            <span v-else class="pcd-thumb-loading">加载中</span>
            <figcaption>{{ index + 1 }}</figcaption>
            <span class="pcd-thumb-size">{{ image.width }}×{{ image.height }}</span>
            <button type="button" class="pcd-thumb-del" :aria-label="`删除第 ${index + 1} 张`" :disabled="importing" @click="removeImage(image)">×</button>
          </figure>
        </div>
        <p v-if="importedAt" class="pcd-muted">已于 {{ importedAt }} 导入本页；开始批阅后会自动从服务器删除这些照片。</p>
      </div>
    </div>

    <template #footer>
      <div class="pcd-footer">
        <el-popconfirm
          title="删除服务器上本次拍摄的全部照片？手机端也会停止上传。"
          width="280"
          @confirm="discard"
        >
          <template #reference>
            <el-button type="danger" plain :disabled="importing || !session">放弃本次拍摄</el-button>
          </template>
        </el-popconfirm>
        <span class="pcd-spacer"></span>
        <el-button :disabled="importing" @click="emit('update:modelValue', false)">稍后导入</el-button>
        <el-button
          type="primary"
          :loading="importing"
          :disabled="!images.length || disabled"
          data-test="phone-capture-import"
          @click="importAll"
        >
          导入 {{ images.length }} 张
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<script setup>
// The desktop half of 手机拍摄: shows the QR code that pairs a phone with the
// teacher's capture session, polls for the photos it uploads, and hands them
// to the grading page in shooting order.
import { computed, onBeforeUnmount, ref, shallowReactive } from 'vue'
import { ElMessage } from 'element-plus'
import qrcode from 'qrcode-generator'
import api from '@/api'
import { useAuthStore } from '@/stores/auth'
import {
  forgetPhoneCaptureSession,
  loadPhoneCaptureSession,
  phoneCaptureFiles,
  phoneCaptureOrigin,
  phoneCaptureOriginWarning,
  phoneCaptureUrl,
  savePhoneCaptureSession,
} from '@/utils/phoneCaptureSession'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  // async (files: File[], { sessionId }) => boolean; true once the grading
  // page accepted them.  The page deletes the session when grading starts.
  importFiles: { type: Function, required: true },
})
const emit = defineEmits(['update:modelValue'])

const POLL_MS = 2000
const PHONE_ACTIVE_MS = 60000

const auth = useAuthStore()
const session = ref(null)
const token = ref('')
const loading = ref(false)
const importing = ref(false)
const error = ref('')
const importedAt = ref('')
// Downloaded photos by sequence number: { blob, url } for preview and import.
const downloads = shallowReactive(new Map())
let pollTimer = 0
let refreshing = null
let open = false

const origin = phoneCaptureOrigin()
const originWarning = phoneCaptureOriginWarning(origin)
const images = computed(() => [...(session.value?.images || [])].sort((a, b) => a.seq - b.seq))
const captureUrl = computed(() => (session.value && token.value ? phoneCaptureUrl(origin, session.value.session_id, token.value) : ''))
// The QR code as one SVG path of unit squares; the template adds the
// 4-module white quiet zone the QR spec asks for.
const qr = computed(() => {
  if (!captureUrl.value) return null
  const code = qrcode(0, 'M')
  code.addData(captureUrl.value, 'Byte')
  code.make()
  const count = code.getModuleCount()
  let path = ''
  for (let row = 0; row < count; row += 1) {
    for (let col = 0; col < count; col += 1) {
      if (code.isDark(row, col)) path += `M${col},${row}h1v1h-1z`
    }
  }
  return { path, size: count + 8 }
})
// Only downloaded photos can be opened; indexes follow the grid.
const previewList = computed(() => images.value.map((image) => downloads.get(image.seq)?.url || ''))
const phoneConnected = computed(() => {
  const seen = Date.parse(session.value?.phone_seen_at || '')
  return Number.isFinite(seen) && Date.now() - seen < PHONE_ACTIVE_MS
})
const lastSeenText = computed(() => formatTime(session.value?.phone_seen_at))
const expiresText = computed(() => formatTime(session.value?.expires_at))

function formatTime(value) {
  const time = Date.parse(value || '')
  if (!Number.isFinite(time)) return ''
  return new Date(time).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function teacherId() {
  return auth.user?.id || ''
}

// Drop downloaded photos, all of them or those `gone(seq)` selects.
function dropDownloads(gone = () => true) {
  for (const [seq, { url }] of downloads) {
    if (!gone(seq)) continue
    URL.revokeObjectURL(url)
    downloads.delete(seq)
  }
}

// Forget photos the session no longer lists (deleted here or on the phone).
function dropStaleDownloads() {
  const listed = new Set(images.value.map((image) => image.seq))
  dropDownloads((seq) => !listed.has(seq))
}

// Sequence numbers restart in a new session, so a different session must
// never inherit the previous one's downloads.
function setSession(next) {
  if (session.value?.session_id !== next?.session_id) {
    dropDownloads()
    importedAt.value = ''
  }
  session.value = next
}

function resetLocal() {
  forgetPhoneCaptureSession(teacherId())
  setSession(null)
  token.value = ''
}

async function openSession() {
  const { token: issued, ...view } = await api.capture.open()
  setSession(view)
  token.value = issued
  savePhoneCaptureSession(teacherId(), view.session_id, issued)
}

async function withLoading(task, failureMessage) {
  loading.value = true
  error.value = ''
  try {
    await task()
  } catch (err) {
    error.value = err.message || failureMessage
  } finally {
    loading.value = false
  }
}

// Reuse the session saved in this tab (same QR code) or open one.
function ensureSession() {
  return withLoading(async () => {
    const saved = loadPhoneCaptureSession(teacherId())
    if (saved) {
      try {
        setSession(await api.capture.get(saved.session_id, { suppressErrorToast: true }))
        token.value = saved.token
        return
      } catch {
        forgetPhoneCaptureSession(teacherId())
      }
    }
    await openSession()
  }, '无法创建拍摄会话')
}

function reissue() {
  return withLoading(openSession, '无法刷新二维码')
}

async function downloadNewPhotos() {
  for (const image of images.value) {
    if (!open || downloads.has(image.seq)) continue
    try {
      const blob = await api.capture.image(session.value.session_id, image.seq)
      downloads.set(image.seq, { blob, url: URL.createObjectURL(blob) })
    } catch {
      // Retried on the next refresh; a deleted photo leaves the list.
      return
    }
  }
}

// One refresh at a time; a caller arriving mid-refresh waits for it.
function refresh() {
  refreshing ||= fetchSession().finally(() => { refreshing = null })
  return refreshing
}

async function fetchSession() {
  if (!open || !session.value) return
  try {
    setSession(await api.capture.get(session.value.session_id, { suppressErrorToast: true }))
    error.value = ''
    dropStaleDownloads()
    await downloadNewPhotos()
  } catch (err) {
    if (err.response?.status === 404) {
      resetLocal()
      error.value = '拍摄会话已结束或过期。关闭后重新打开即可生成新的二维码。'
    } else {
      error.value = '暂时无法获取手机照片，正在重试…'
    }
  }
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer)
  pollTimer = 0
}

async function onOpen() {
  open = true
  await ensureSession()
  if (!open) return
  await refresh()
  stopPolling()
  pollTimer = window.setInterval(refresh, POLL_MS)
}

function onClosed() {
  open = false
  stopPolling()
}

async function copyLink() {
  try {
    await navigator.clipboard.writeText(captureUrl.value)
    ElMessage.success('链接已复制，可发到手机上打开')
  } catch {
    ElMessage.warning('无法访问剪贴板，请直接扫码')
  }
}

async function removeImage(image) {
  try {
    setSession(await api.capture.deleteImage(session.value.session_id, image.seq))
    dropStaleDownloads()
  } catch {
    // The API layer already reported the failure.
  }
}

async function discard() {
  const current = session.value
  if (!current) return
  try {
    await api.capture.close(current.session_id)
  } catch (err) {
    if (err.response?.status !== 404) return
  }
  resetLocal()
  emit('update:modelValue', false)
}

async function importAll() {
  if (!session.value || !images.value.length) return
  importing.value = true
  try {
    await refresh()
    const entries = images.value.map((image) => ({ seq: image.seq, blob: downloads.get(image.seq)?.blob }))
    if (entries.some((entry) => !entry.blob)) {
      ElMessage.error('还有照片未下载完成，请稍后再导入')
      return
    }
    const accepted = await props.importFiles(phoneCaptureFiles(entries), { sessionId: session.value.session_id })
    if (!accepted) return
    importedAt.value = formatTime(new Date().toISOString())
    emit('update:modelValue', false)
  } finally {
    importing.value = false
  }
}

onBeforeUnmount(() => {
  open = false
  stopPolling()
  dropDownloads()
})
</script>

<style scoped>
.pcd-layout { display: flex; gap: 20px; align-items: flex-start; flex-wrap: wrap; }
.pcd-qr-col { flex: 0 0 220px; display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center; }
.pcd-qr { width: 200px; height: 200px; display: flex; align-items: center; justify-content: center; background: #fff; border: 1px solid var(--el-border-color); border-radius: 8px; }
.pcd-qr svg { width: 192px; height: 192px; }
.pcd-qr-placeholder { color: var(--el-text-color-secondary); }
.pcd-qr-tip { margin: 0; font-size: 13px; }
.pcd-link-row { display: flex; gap: 8px; }
.pcd-muted { margin: 0; font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.6; }
.pcd-main { flex: 1 1 320px; min-width: 0; display: flex; flex-direction: column; gap: 10px; }
.pcd-alert { margin: 0; }
.pcd-status { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 14px; }
.pcd-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--el-color-info-light-5); }
.pcd-dot.is-on { background: var(--el-color-success); }
.pcd-count { margin-left: auto; }
.pcd-hints { margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.7; color: var(--el-text-color-regular); }
.pcd-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(76px, 1fr));
  gap: 8px;
  max-height: 300px;
  overflow-y: auto;
  padding: 2px;
}
.pcd-thumb { position: relative; margin: 0; aspect-ratio: 3 / 4; border-radius: 6px; overflow: hidden; background: var(--el-fill-color-light); border: 1px solid var(--el-border-color-lighter); }
.pcd-thumb :deep(.el-image) { width: 100%; height: 100%; display: block; cursor: zoom-in; }
.pcd-thumb-size { position: absolute; left: 0; right: 0; bottom: 0; padding: 1px 4px; font-size: 11px; text-align: center; background: rgba(0, 0, 0, 0.55); color: #fff; pointer-events: none; }
.pcd-thumb-loading { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 12px; color: var(--el-text-color-secondary); }
.pcd-thumb figcaption { position: absolute; left: 0; top: 0; padding: 1px 6px; font-size: 12px; background: rgba(0, 0, 0, 0.6); color: #fff; border-bottom-right-radius: 6px; }
.pcd-thumb-del { position: absolute; right: 2px; top: 2px; width: 22px; height: 22px; border-radius: 50%; border: 0; background: rgba(0, 0, 0, 0.6); color: #fff; cursor: pointer; line-height: 1; }
.pcd-thumb-del:hover { background: var(--el-color-danger); }
.pcd-footer { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pcd-spacer { flex: 1; }
@media (max-width: 640px) {
  .pcd-qr-col { flex-basis: 100%; }
}
</style>
