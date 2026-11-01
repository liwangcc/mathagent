<template>
  <div class="chat-section" :class="`variant-${variant}`">
    <div class="chat-input-wrap">
      <el-input
        v-model="chatMessage"
        :placeholder="placeholder"
        :size="variant === 'home' ? 'large' : 'default'"
        :type="variant === 'bar' ? 'textarea' : 'text'"
        :rows="variant === 'bar' ? 2 : undefined"
        :resize="variant === 'bar' ? 'none' : undefined"
        @keyup.enter.exact="onChat"
      >
        <template v-if="variant === 'home'" #append>
          <el-button :loading="chatLoading" @click="onChat">
            <el-icon><Promotion /></el-icon>
          </el-button>
        </template>
      </el-input>
      <el-button
        v-if="variant === 'bar'"
        type="primary"
        :loading="chatLoading"
        class="bar-send"
        @click="onChat"
      >
        发送
      </el-button>
    </div>

    <!-- 识图办公（仅首页）：发模板截图给小M，生成 Word/Excel/PPT -->
    <div v-if="variant === 'home'" class="vision-office">
      <input
        ref="fileInput"
        type="file"
        accept=".docx,.xlsx,.pptx,.pdf,image/png,image/jpeg,image/webp"
        style="display: none"
        @change="onPickImage"
      />
      <el-button text size="small" class="vo-trigger" @click="fileInput?.click()">
        <el-icon style="margin-right: 4px"><Picture /></el-icon>
        点击此处上传文件或截图，让小M帮你做文件
      </el-button>

      <div v-if="visionImage" class="vo-panel">
        <img v-if="visionPreview" :src="visionPreview" class="vo-thumb" alt="模板预览" />
        <div v-else class="vo-file">
          <el-icon><Document /></el-icon>
          <span class="vo-file-name">{{ visionFileName }}</span>
        </div>
        <div class="vo-controls">
          <div class="vo-tip">已选模板（{{ visionSizeKB }}KB）。可补充要求，选择格式后生成。</div>
          <el-input
            v-model="visionMessage"
            type="textarea"
            :rows="2"
            resize="none"
            placeholder="补充要求，例如：生成期末考试通知，时间6月25日上午9点，地点301"
          />
          <div class="vo-actions">
            <el-radio-group v-model="visionFormat" size="small">
              <el-radio-button value="auto">自动</el-radio-button>
              <el-radio-button value="word">Word</el-radio-button>
              <el-radio-button value="excel">Excel</el-radio-button>
              <el-radio-button value="ppt">PPT</el-radio-button>
            </el-radio-group>
            <el-button type="primary" size="small" :loading="visionLoading" @click="onVisionSubmit">
              生成并下载
            </el-button>
            <el-button text size="small" @click="clearVision">取消</el-button>
          </div>
        </div>
      </div>
    </div>

    <!-- 颜文字思考动画 -->
    <div v-if="chatLoading && !chatResponse" class="chat-thinking">
      <span class="thinking-indicator" aria-hidden="true"><i /><i /><i /></span>
      <span class="thinking-text">{{ thinkingText }}</span>
    </div>

    <div v-if="chatResponse" class="chat-response">
      <div class="chat-response-header">
        <span class="chat-response-label">小M</span>
        <el-button text size="small" @click="clearHistory" title="清空对话记录">
          <el-icon><Delete /></el-icon>
        </el-button>
      </div>
      <div class="chat-response-text">{{ chatResponse }}</div>
      <div v-if="downloadInfo" class="chat-download">
        <el-button
          type="success"
          size="small"
          :loading="downloading"
          @click="onDownload"
        >
          <el-icon style="margin-right: 4px"><Download /></el-icon>
          {{ downloadInfo.label || '下载文件' }}
        </el-button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { useRouter, onBeforeRouteLeave } from "vue-router"
import { ElMessage } from 'element-plus'
import { Promotion, Delete, Download, Picture, Document } from '@element-plus/icons-vue'
import api from '@/api'

const props = defineProps({
  variant: { type: String, default: 'home' }, // 'home' 首页大栏 | 'bar' 页面顶部窄栏
  placeholder: {
    type: String,
    default: '跟小M说：查李明成绩 / 出5道函数题 / 导出期中考试错题条 / 给张三生成学习报告',
  },
})

const router = useRouter()

function isSafeLocalPath(path) {
  return typeof path === 'string' && path.startsWith('/') && !path.startsWith('//')
}

const chatMessage = ref('')
const chatResponse = ref('')
const chatLoading = ref(false)
const chatHistory = ref([])
const downloadInfo = ref(null)
const downloading = ref(false)
let chatController = null
let visionController = null

// ── 颜文字进度提示 ──
const THINKING_LINES = [
  { kaomoji: '(・ω・)', text: '小M正在思考...' },
  { kaomoji: '(°▽°)', text: '正在翻阅知识库...' },
  { kaomoji: '(•̀ᴗ•́)و', text: '马上就好！' },
  { kaomoji: '(..•˘_˘•..)', text: '让我想想...' },
  { kaomoji: '(っ´▽`)っ', text: '正在努力分析中...' },
  { kaomoji: '⸜(｡˃ ᵕ ˂ )⸝', text: '小脑袋转起来了~' },
  { kaomoji: '(ﾉ◕ヮ◕)ﾉ*:・ﾟ', text: '灵感涌现中...' },
  { kaomoji: '( •̀ ω •́ )y', text: '查资料 ing...' },
  { kaomoji: '(≖ᴗ≖✿)', text: '正在认真思考...' },
  { kaomoji: '(ง •̀_•́)ง', text: '冲冲冲！' },
  { kaomoji: '(◠‿◠)', text: '别急，快了~' },
  { kaomoji: '(∩ᄑ_ᄑ)⊃━☆ﾟ.*', text: '施展魔法中...' },
  { kaomoji: '( ˘▽˘)っ♨', text: '在煮答案了...' },
  { kaomoji: '(ノಠ益ಠ)ノ', text: '正在使劲想...' },
  { kaomoji: '(´• ω •`)', text: '数据收集中...' },
  // ── 更皮的 ──
  { kaomoji: '(╬▔皿▔)╯', text: '这题有点难...' },
  { kaomoji: '(￣▽￣)~*', text: '给你变个魔术~' },
  { kaomoji: '( ´_ゝ`)', text: '唔...让我捋捋...' },
  { kaomoji: '(╯‵□′)╯︵┻━┻', text: '脑子要炸了...' },
  { kaomoji: '┬─┬ノ( º _ ºノ)', text: '好了好了，冷静一下' },
  { kaomoji: '( ͡° ͜ʖ ͡°)', text: '好像发现了什么...' },
  { kaomoji: '(；・∀・)', text: '等等，我算算...' },
  { kaomoji: '(╥﹏╥)', text: '太难了也要做...' },
  { kaomoji: '(☆▽☆)', text: '找到答案了！...等等还没' },
  { kaomoji: '(¬‿¬)', text: '小M有妙计~' },
  { kaomoji: '(⊙_⊙)', text: '信息量有点大...' },
  { kaomoji: '(𓂀𥻎𓂀)', text: '正在施展禁术...' },
  { kaomoji: '(～￣▽￣)～', text: '摇一摇，答案就出来了' },
  { kaomoji: '(；′⌒`)', text: 'CPU 要冒烟了...' },
  { kaomoji: '(づ｡◕‿‿◕｡)づ', text: '抱紧我的知识库~' },
  { kaomoji: 'ᶘ ᵒᴥᵒᶅ', text: '海豹式思考中...' },
  { kaomoji: '(´,,•ω•,,)', text: '偷偷努力中...' },
  { kaomoji: '(ㆆᴗㆆ)', text: '让我蒙一蒙...' },
  { kaomoji: '(σ°▽°)σ', text: '抓住灵感了！' },
  { kaomoji: '( ˘•ω•˘ )', text: '困...但还在想...' },
]
let _thinkIdx = -1
let _thinkTimer = null
const thinkingKaomoji = ref('')
const thinkingText = ref('')

function startThinking() {
  _thinkIdx = Math.floor(Math.random() * THINKING_LINES.length)
  showNextThinkingLine()
  _thinkTimer = setInterval(showNextThinkingLine, 2500)
}

function showNextThinkingLine() {
  _thinkIdx = (_thinkIdx + 1) % THINKING_LINES.length
  const line = THINKING_LINES[_thinkIdx]
  thinkingKaomoji.value = line.kaomoji
  thinkingText.value = line.text
}

function stopThinking() {
  if (_thinkTimer) { clearInterval(_thinkTimer); _thinkTimer = null }
  thinkingKaomoji.value = ''
  thinkingText.value = ''
}

// 识图/解析办公（仅 home）
const fileInput = ref(null)
const visionImage = ref(null)      // 图片=压缩后 blob；Office/PDF=原 File
const visionPreview = ref('')      // 图片预览 dataURL（文件为空）
const visionFileName = ref('')     // 非图片文件显示文件名
const visionMessage = ref('')
const visionFormat = ref('auto')
const visionLoading = ref(false)
const visionSizeKB = ref(0)

const _OFFICE_EXT = /\.(docx|xlsx|pptx|pdf)$/i
const MAX_OFFICE_SIZE = 20 * 1024 * 1024

// 选文件 → 图片走 canvas 压缩，Office/PDF 直接透传
async function onPickImage(e) {
  const file = e.target.files?.[0]
  e.target.value = ''  // 允许重选同一文件
  if (!file) return

  const isImage = /image\/(png|jpe?g|webp)/.test(file.type)
  const isDoc = _OFFICE_EXT.test(file.name)
  if (!isImage && !isDoc) {
    ElMessage.warning('支持 Word/Excel/PPT/PDF 文件，或 png/jpg/webp 截图')
    return
  }
  if (file.size > MAX_OFFICE_SIZE) {
    ElMessage.warning('单个文件不能超过 20MB')
    return
  }

  clearVision()
  try {
    if (isImage) {
      const blob = await compressImage(file, 1280)
      visionImage.value = blob
      visionSizeKB.value = Math.round(blob.size / 1024)
      visionPreview.value = URL.createObjectURL(blob)
    } else {
      visionImage.value = file
      visionFileName.value = file.name
      visionSizeKB.value = Math.round(file.size / 1024)
    }
  } catch {
    ElMessage.error('文件处理失败，请重试')
  }
}

function compressImage(file, maxEdge) {
  return new Promise((resolve, reject) => {
    const img = new Image()
    const url = URL.createObjectURL(file)
    img.onload = () => {
      URL.revokeObjectURL(url)
      let { width, height } = img
      const scale = Math.min(1, maxEdge / Math.max(width, height))
      width = Math.round(width * scale)
      height = Math.round(height * scale)
      const canvas = document.createElement('canvas')
      canvas.width = width
      canvas.height = height
      const ctx = canvas.getContext('2d')
      ctx.fillStyle = '#fff'
      ctx.fillRect(0, 0, width, height)
      ctx.drawImage(img, 0, 0, width, height)
      canvas.toBlob(
        (blob) => (blob ? resolve(blob) : reject(new Error('toBlob failed'))),
        'image/jpeg',
        0.85,
      )
    }
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('load failed')) }
    img.src = url
  })
}

function clearVision() {
  if (visionPreview.value) URL.revokeObjectURL(visionPreview.value)
  visionImage.value = null
  visionPreview.value = ''
  visionFileName.value = ''
  visionSizeKB.value = 0
  visionMessage.value = ''
  visionFormat.value = 'auto'
}

async function onVisionSubmit() {
  if (!visionImage.value || visionLoading.value) return
  visionLoading.value = true
  visionController = new AbortController()
  try {
    const { blob, filename } = await api.visionOffice.submit(visionImage.value, visionMessage.value.trim(), visionFormat.value, visionController.signal)
    // 在用户点击事件上下文中触发下载，避免 Chrome 静默拦截
    const objUrl = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = objUrl
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(objUrl), 3000)
    ElMessage.success(`小M 已生成: ${filename}`)
    clearVision()
  } catch (e) {
    if (e?.code === 'ECONNABORTED') {
      // The server keeps working after the browser stops waiting.
      ElMessage.warning('生成时间较长，页面已停止等待；文件完成后会出现在首页「最近生成文件」，请稍后查看，避免重复提交')
    } else if (e?.name !== 'CanceledError' && e?.name !== 'AbortError') {
      ElMessage.error(e?.message || '生成失败，请重试')
    }
  } finally {
    visionLoading.value = false
    visionController = null
  }
}

const CHAT_HISTORY_KEY = 'mathagent_chat_history'
const MAX_HISTORY_LENGTH = 20
// The server accepts at most 4000 characters per history item; a longer
// saved reply would otherwise break every later message.
const MAX_HISTORY_CONTENT = 4000

function clipHistory(items) {
  return (Array.isArray(items) ? items : [])
    .filter((item) => item && typeof item.content === 'string')
    .map((item) => item.content.length > MAX_HISTORY_CONTENT
      ? { ...item, content: `${item.content.slice(0, MAX_HISTORY_CONTENT - 1)}…` }
      : item)
}

function loadChatHistory() {
  try {
    const saved = localStorage.getItem(CHAT_HISTORY_KEY)
    if (saved) {
      chatHistory.value = clipHistory(JSON.parse(saved))
      const lastAssistant = [...chatHistory.value].reverse().find(h => h.role === 'assistant')
      if (lastAssistant) chatResponse.value = lastAssistant.content
    }
  } catch {
    chatHistory.value = []
  }
}

function saveChatHistory() {
  try {
    const trimmed = clipHistory(chatHistory.value.slice(-MAX_HISTORY_LENGTH))
    localStorage.setItem(CHAT_HISTORY_KEY, JSON.stringify(trimmed))
  } catch {
    // localStorage 可能满了，忽略
  }
}

function clearHistory() {
  chatHistory.value = []
  chatResponse.value = ''
  downloadInfo.value = null
  localStorage.removeItem(CHAT_HISTORY_KEY)
}

async function onChat() {
  if (!chatMessage.value.trim() || chatLoading.value) return
  const userMsg = chatMessage.value.trim()
  chatMessage.value = ''
  chatLoading.value = true
  chatResponse.value = ''
  downloadInfo.value = null
  startThinking()
  chatController = new AbortController()

  chatHistory.value.push({ role: 'user', content: userMsg })

  try {
    const data = await api.chat.send(userMsg, clipHistory(chatHistory.value.slice(0, -1)), chatController.signal)
    stopThinking()
    const assistantMsg = data.response || '暂无回复'
    chatResponse.value = assistantMsg
    // 服务端返回压缩后的历史，直接替换前端存储（省空间 + 跨轮压缩）
    if (data.history && data.history.length) {
      chatHistory.value = data.history
    } else {
      chatHistory.value.push({ role: 'assistant', content: assistantMsg })
    }
    saveChatHistory()

    // 服务端优先返回结构化下载；兼容 Hermes 工具在文本中输出本站下载链接。
    if (data.download?.url) downloadInfo.value = data.download
    else {
      const match = assistantMsg.match(/\/api\/download\/(\d+)/)
      if (match) downloadInfo.value = { url: match[0], method: 'GET', filename: '小M生成文件' }
    }

    // 导航指令 → 自动跳转
    const navigateTo = data.data?.navigate_to
    if (isSafeLocalPath(navigateTo)) {
      setTimeout(() => router.push(navigateTo), 800)
    }
  } catch (error) {
    stopThinking()
    if (error?.name !== 'CanceledError' && error?.name !== 'AbortError') {
      chatResponse.value = '小M暂时不在线，请稍后再试'
    }
    chatHistory.value.pop()
  } finally {
    chatLoading.value = false
    chatController = null
  }
}

async function onDownload() {
  if (!downloadInfo.value || downloading.value) return
  downloading.value = true
  try {
    await api.chat.download(downloadInfo.value)
  } catch (e) {
    ElMessage.error('下载失败，请稍后重试')
  } finally {
    downloading.value = false
  }
}

onBeforeRouteLeave((to, from, next) => {
  if (chatLoading.value || visionLoading.value) {
    if (confirm('小M正在回答中，离开会中断。确定离开吗？')) {
      chatController?.abort()
      visionController?.abort()
      next()
    } else {
      next(false)
    }
  } else {
    next()
  }
})

onMounted(() => {
  loadChatHistory()
})

onBeforeUnmount(() => {
  chatController?.abort()
  visionController?.abort()
  stopThinking()
  clearVision()
})
</script>

<style scoped>
.chat-section {
  margin-bottom: var(--sp-5);
}

.variant-bar .chat-input-wrap {
  display: flex;
  gap: var(--sp-2);
  align-items: flex-start;
}

.variant-bar .chat-input-wrap :deep(.el-textarea) {
  flex: 1;
}

.bar-send {
  flex-shrink: 0;
  min-height: 54px;
  padding: var(--sp-3) var(--sp-6);
  font-size: 13px;
  font-weight: 600;
  border-radius: var(--radius-md);
  background: var(--primary);
  color: var(--on-primary);
  box-shadow: var(--shadow-btn);
  transition: all 0.15s ease;
}

.bar-send:hover {
  background: var(--primary-dark);
  transform: translateY(-1px);
  box-shadow: var(--shadow-btn-hover);
}

.chat-input-wrap :deep(.el-input__wrapper),
.chat-input-wrap :deep(.el-textarea__inner) {
  border-radius: var(--radius-md);
  background: var(--card);
  box-shadow: 0 0 0 1px var(--border) inset, var(--shadow-sm) !important;
  transition: all 0.15s ease;
}

.chat-input-wrap :deep(.el-input__wrapper.is-focus),
.chat-input-wrap :deep(.el-textarea__inner:focus) {
  box-shadow: var(--focus-ring), 0 0 0 1px var(--primary) inset !important;
}

.chat-input-wrap :deep(.el-textarea__inner) {
  min-height: 54px !important;
  font-size: 13px;
  font-family: var(--font-body);
  color: var(--ink);
}

.chat-input-wrap :deep(.el-textarea__inner::placeholder) {
  color: var(--ink-muted);
  font-size: 13px;
}

.chat-response {
  margin-top: var(--sp-3);
  background: var(--primary-soft);
  border: 1px solid color-mix(in srgb, var(--primary) 18%, transparent);
  border-radius: var(--radius-md);
  padding: var(--sp-4);
}

.chat-response-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--sp-2);
}

.chat-response-label {
  font-size: 12px;
  color: var(--primary-dark);
  font-weight: 700;
  letter-spacing: 0.02em;
}

.chat-response-text {
  font-size: 14px;
  color: var(--ink);
  line-height: 1.8;
  white-space: pre-wrap;
}

.chat-download {
  margin-top: var(--sp-3);
}

/* ── 颜文字思考动画 ── */
.chat-thinking {
  margin-top: var(--sp-3);
  background: var(--surface-subtle);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--sp-4);
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  animation: thinking-fade-in 0.3s ease;
}

.thinking-indicator {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.thinking-indicator i {
  width: 6px;
  height: 6px;
  background: var(--primary);
  border-radius: 50%;
  animation: thinking-dot-blink 1.2s ease-in-out infinite;
}

.thinking-indicator i:nth-child(2) {
  animation-delay: 0.15s;
}

.thinking-indicator i:nth-child(3) {
  animation-delay: 0.3s;
}

.thinking-text {
  font-size: 14px;
  color: var(--ink-soft);
  font-weight: 500;
  transition: opacity 0.2s;
}

@keyframes thinking-dot-blink {
  0%, 80%, 100% { opacity: 0.2; }
  40% { opacity: 1; }
}

@keyframes thinking-fade-in {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

.vision-office {
  margin-top: var(--sp-3);
}

.vo-trigger {
  color: var(--primary-dark);
  font-weight: 600;
  border: 1px solid var(--border);
  background: var(--surface-subtle);
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius-sm);
  height: auto;
  white-space: normal;
  text-align: left;
  transition: all 0.15s ease;
}

.vo-trigger:hover,
.vo-trigger:focus {
  color: var(--primary);
  background: var(--primary-soft);
  border-color: var(--primary);
}

.vo-panel {
  display: flex;
  gap: var(--sp-3);
  margin-top: var(--sp-2);
  padding: var(--sp-3);
  background: var(--surface-subtle);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}

.vo-thumb {
  width: 96px;
  height: 96px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  flex-shrink: 0;
}

.vo-file {
  width: 96px;
  height: 96px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--card);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 6px;
  color: var(--ink-muted);
  font-size: 20px;
}
.vo-file-name {
  font-size: 11px;
  line-height: 1.3;
  text-align: center;
  word-break: break-all;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.vo-controls {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.vo-tip {
  font-size: 12px;
  color: var(--ink-muted);
}

.vo-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}

@media (max-width: 640px) {
  .vo-panel {
    flex-direction: column;
  }

  .vo-thumb,
  .vo-file {
    width: 72px;
    height: 72px;
  }

  .variant-bar .chat-input-wrap {
    flex-direction: column;
  }

  .bar-send {
    width: 100%;
    min-height: 40px;
  }
}

</style>
