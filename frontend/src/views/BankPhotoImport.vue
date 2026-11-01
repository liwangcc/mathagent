<template>
  <div class="photo-import-page page-shell page-shell--narrow">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <div class="card-head-left">
            <el-button text :icon="ArrowLeft" @click="goBack">返回题库</el-button>
            <span class="title">拍照导入题库</span>
            <el-tag type="info" size="small">题干 + 答案 → 自动配对入库</el-tag>
            <el-tag v-if="teachingContext.education_stage" size="small">{{ teachingContext.education_stage === 'primary' ? '小学数学' : teachingContext.education_stage === 'junior' ? '初中数学' : teachingContext.education_stage === 'senior' ? '高中数学' : '历史未标学段' }} · 本批次已固定</el-tag>
            <HelpButton
              title="拍照导入题库"
              content="分别上传题干照片和答案照片，系统按题号尝试配对。识别后请先检查可编辑内容，只有完整题目才会写入当前私库。"
            />
          </div>
        </div>
      </template>

      <el-form :model="form" label-width="80px" class="form-row">
        <el-form-item label="所属章节">
          <el-select
            v-model="form.chapter"
            filterable
            default-first-option
            placeholder="选择本学段章节（可不选）"
            style="width: 320px"
            clearable
          >
            <el-option
              v-for="ch in chapters"
              :key="ch.chapter || ch"
              :label="ch.chapter || ch"
              :value="ch.chapter || ch"
            />
          </el-select>
        </el-form-item>
      </el-form>

      <el-row :gutter="16">
        <el-col :span="12">
          <div class="upload-block">
            <div class="upload-title">题干照片 <HelpButton title="题干照片" content="上传能看清题号和完整题干的图片，可一次选择多张。" /></div>
            <el-upload
              ref="qUploadRef"
              drag
              multiple
              :auto-upload="false"
              :on-change="onQuestionChange"
              :on-remove="onQuestionRemove"
              :file-list="questionFiles"
              accept="image/*"
              class="dropper"
            >
              <el-icon class="upload-icon"><UploadFilled /></el-icon>
              <div class="upload-text">将题干图拖到此处，或<em>点击上传</em></div>
              <template #tip>
                <div class="upload-tip">支持多张，单文件 ≤ 20MB</div>
              </template>
            </el-upload>
          </div>
        </el-col>

        <el-col :span="12">
          <div class="upload-block">
            <div class="upload-title">答案照片 <HelpButton title="答案照片" content="上传与题号对应的答案图片。系统会按题号配对，配对失败的项目需要老师手动补齐。" /></div>
            <el-upload
              ref="aUploadRef"
              drag
              multiple
              :auto-upload="false"
              :on-change="onAnswerChange"
              :on-remove="onAnswerRemove"
              :file-list="answerFiles"
              accept="image/*"
              class="dropper"
            >
              <el-icon class="upload-icon"><UploadFilled /></el-icon>
              <div class="upload-text">将答案图拖到此处，或<em>点击上传</em></div>
              <template #tip>
                <div class="upload-tip">支持多张，按题号自动配对</div>
              </template>
            </el-upload>
          </div>
        </el-col>
      </el-row>

      <div class="actions">
        <el-button @click="resetAll" :disabled="recognizing || saving">清空</el-button>
        <el-button
          type="primary"
          :loading="recognizing"
          :disabled="recognizing || !canRecognize"
          @click="onRecognize"
        >
          识别并配对
        </el-button>
      </div>
    </el-card>

    <el-card v-if="paired.length" shadow="never" class="section">
      <template #header>
        <div class="card-head">
          <div class="card-head-left">
            <span class="title">配对结果</span>
            <el-tag type="success" size="small">完整 {{ matchedCount }}</el-tag>
            <el-tag type="warning" size="small">待补 {{ unmatchedCount }}</el-tag>
            <HelpButton title="配对结果" content="逐行核对题干和答案。完整项目才能入库，待补项目会保留在当前页面，取消则不会写入题库。" />
          </div>
        </div>
      </template>

      <el-table :data="paired" border size="small">
        <el-table-column prop="question_no" label="题号" width="80" />
        <el-table-column label="题干（可编辑）" min-width="280">
          <template #default="{ row }">
            <el-input
              v-model="row.content"
              type="textarea"
              :rows="2"
              autosize
              placeholder="请输入题干"
            />
          </template>
        </el-table-column>
        <el-table-column label="答案（可编辑）" min-width="220">
          <template #default="{ row }">
            <el-input
              v-model="row.answer"
              type="textarea"
              :rows="2"
              autosize
              placeholder="请输入答案"
            />
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110" align="center">
          <template #default="{ row }">
            <el-tag v-if="rowComplete(row)" type="success" size="small">完整</el-tag>
            <el-tag v-else type="warning" size="small">待补</el-tag>
          </template>
        </el-table-column>
      </el-table>

      <div class="actions">
        <el-button @click="resetAll" :disabled="saving">取消</el-button>
        <el-button
          type="primary"
          :loading="saving"
          :disabled="!matchedCount"
          @click="onSaveAll"
        >
          全部入库（{{ matchedCount }}）
        </el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, UploadFilled } from '@element-plus/icons-vue'
import api from '@/api'
import { validateUploadList } from '@/utils/uploadValidation'
import HelpButton from '@/components/HelpButton.vue'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()
const importId = ref('')
const teachingContext = ref({})
const loadedChapterStage = ref('')
let chapterLoadGeneration = 0
let importGeneration = 0
const savedImportKey = () => `mathagent-bank-import:${auth.user?.id || ''}`

function applyRecognition(resp) {
  importId.value = resp?.import_id || ''
  teachingContext.value = resp?.teaching_context || {}
  paired.value = (resp?.paired || []).map(p => ({ ...p, content: p.content || '', answer: p.answer || '', chapter: p.chapter || '' }))
  if (importId.value) localStorage.setItem(savedImportKey(), importId.value)
}

const qUploadRef = ref(null)
const aUploadRef = ref(null)
const questionFiles = ref([])
const answerFiles = ref([])
const recognizing = ref(false)
const saving = ref(false)
const paired = ref([])
const chapters = ref([])

const form = reactive({
  chapter: '',
})

async function loadChapters(stage = teachingContext.value.education_stage || auth.user?.education_stage) {
  const generation = ++chapterLoadGeneration
  chapters.value = []
  loadedChapterStage.value = ''
  try {
    const data = await api.bank.chapters(stage ? { education_stage: stage } : undefined)
    if (generation !== chapterLoadGeneration) return
    chapters.value = Array.isArray(data) ? data : data?.items || []
    loadedChapterStage.value = stage || ''
  } catch {
    if (generation === chapterLoadGeneration) chapters.value = []
  }
}

const canRecognize = computed(
  () => questionFiles.value.length > 0 || answerFiles.value.length > 0
)

const matchedCount = computed(
  () => paired.value.filter(rowComplete).length
)

const unmatchedCount = computed(
  () => paired.value.length - matchedCount.value
)

function rowComplete(row) {
  return !!(row.content || '').trim() && !!(row.answer || '').trim()
}

function goBack() {
  router.push('/bank')
}

function onQuestionChange(_file, list) {
  const result = validateUploadList(list, { maxFiles: 20 })
  if (result.errors.length) {
    ElMessage.warning(result.errors[0])
    qUploadRef.value?.clearFiles()
  }
  questionFiles.value = result.files
}
function onQuestionRemove(_file, list) {
  questionFiles.value = list
}
function onAnswerChange(_file, list) {
  const result = validateUploadList(list, { maxFiles: 20 })
  if (result.errors.length) {
    ElMessage.warning(result.errors[0])
    aUploadRef.value?.clearFiles()
  }
  answerFiles.value = result.files
}
function onAnswerRemove(_file, list) {
  answerFiles.value = list
}

function resetAll() {
  importGeneration += 1
  importId.value = ''
  teachingContext.value = {}
  form.chapter = ''
  localStorage.removeItem(savedImportKey())
  questionFiles.value = []
  answerFiles.value = []
  qUploadRef.value?.clearFiles()
  aUploadRef.value?.clearFiles()
  paired.value = []
  recognizing.value = false
  saving.value = false
  return loadChapters(auth.user?.education_stage)
}

async function onRecognize() {
  if (recognizing.value) return
  if (!canRecognize.value) {
    ElMessage.warning('请至少上传一张图片')
    return
  }

  recognizing.value = true
  try {
    // Recognition starts a new task, even when an older batch was restored above.
    if (loadedChapterStage.value !== auth.user?.education_stage) {
      form.chapter = ''
      await loadChapters(auth.user?.education_stage)
    }
    const fd = new FormData()
    questionFiles.value.forEach((f) => fd.append('question_files', f.raw))
    answerFiles.value.forEach((f) => fd.append('answer_files', f.raw))
    if (form.chapter) fd.append('chapter', form.chapter)
    const resp = await api.bank.photoRecognize(fd)
    applyRecognition(resp)
    if (!paired.value.length) {
      ElMessage.warning('未识别到任何题目')
    } else {
      ElMessage.success(`识别完成，共 ${paired.value.length} 题`)
    }
  } catch {
    /* error toast handled by interceptor */
  } finally {
    recognizing.value = false
  }
}

async function onSaveAll() {
  const items = paired.value.filter(rowComplete).map((r) => ({
    question_no: r.question_no,
    content: r.content.trim(),
    answer: r.answer.trim(),
    chapter: (r.chapter || form.chapter || '').trim(),
  }))
  if (!items.length) {
    ElMessage.warning('没有可入库的完整题目')
    return
  }

  try {
    await ElMessageBox.confirm(
      `将入库 ${items.length} 题（已存在的题目会自动跳过）。<br><br>题目仅进入你的私有题库，学段沿用本批次识别时的设置。是否继续？`,
      '确认入库',
      {
        confirmButtonText: '确认入库',
        cancelButtonText: '取消',
        type: 'warning',
        dangerouslyUseHTMLString: true,
      }
    )
  } catch {
    return
  }

  saving.value = true
  try {
    const resp = await api.bank.photoSave({ items, import_id: importId.value })
    const inserted = resp?.inserted ?? 0
    const skipped = resp?.skipped ?? 0
    ElMessage.success(`入库成功：新增 ${inserted} 题，跳过 ${skipped} 题`)
    resetAll()
    router.push('/bank')
  } catch {
    /* error toast handled by interceptor */
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  const generation = importGeneration
  const savedImport = localStorage.getItem(savedImportKey())
  if (savedImport) {
    try {
      const restored = await api.bank.photoRestore(savedImport)
      if (generation === importGeneration) applyRecognition(restored)
    } catch {
      if (generation === importGeneration) localStorage.removeItem(savedImportKey())
    }
  }
  await loadChapters()
})
</script>

<style scoped>
.title {
  color: var(--ink);
  font-size: var(--fs-lg);
  font-weight: var(--fw-semibold);
}

.form-row {
  margin-top: 4px;
}

.photo-import-page :deep(.el-row) {
  counter-reset: upload-step;
}

.upload-block {
  display: flex;
  flex-direction: column;
}

/* Numbered drop zones make the question → answer order explicit. */
.upload-title {
  display: flex;
  align-items: center;
  gap: 8px;
  counter-increment: upload-step;
  font-size: var(--fs-md);
  color: var(--ink);
  margin-bottom: 10px;
  font-weight: var(--fw-semibold);
}

.upload-title::before {
  content: counter(upload-step);
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--primary-soft);
  color: var(--primary-dark);
  font-size: var(--fs-xs);
  font-weight: var(--fw-bold);
}

.dropper {
  width: 100%;
}

.dropper :deep(.el-upload-dragger) {
  width: 100%;
  border-style: dashed;
  border-width: 1.5px;
  border-color: color-mix(in srgb, var(--primary) 30%, var(--border));
}

.dropper :deep(.el-upload-dragger.is-dragover) {
  border-color: var(--primary);
  background: var(--primary-soft);
  box-shadow: var(--focus-ring);
}

.upload-icon {
  font-size: 44px;
  color: var(--primary);
  transition: transform var(--dur-3) var(--ease-emphasized);
}

.dropper :deep(.el-upload-dragger:hover) .upload-icon,
.dropper :deep(.el-upload-dragger.is-dragover) .upload-icon {
  transform: translateY(-3px);
}

.upload-text {
  margin-top: 6px;
  color: var(--ink-soft);
  font-size: var(--fs-sm);
}

.upload-text em {
  color: var(--primary-dark);
  font-style: normal;
  font-weight: var(--fw-semibold);
}

.upload-tip {
  font-size: var(--fs-xs);
  color: var(--ink-muted);
  margin-top: 6px;
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--line);
}

.section {
  animation: ma-rise-in var(--dur-4) var(--ease-emphasized) backwards;
}

@media (max-width: 760px) {
  .photo-import-page :deep(.el-col-12) {
    max-width: 100%;
    flex: 0 0 100%;
  }

  .form-row :deep(.el-form-item) {
    align-items: stretch;
    flex-direction: column;
  }

  .form-row :deep(.el-select) {
    width: 100% !important;
  }
}
</style>
