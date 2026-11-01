<template>
  <el-dialog
    :model-value="modelValue"
    :title="`学生上传 · ${className || '当前班级'}`"
    width="min(860px, 96vw)"
    append-to-body
    :close-on-click-modal="false"
    :close-on-press-escape="!importing"
    :show-close="!importing"
    @update:model-value="(value) => emit('update:modelValue', value)"
    @open="onOpen"
    @closed="onClosed"
  >
    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" class="ssd-alert" />
    <el-alert v-if="originWarning" type="warning" :closable="false" show-icon :title="originWarning" class="ssd-alert" />

    <div class="ssd-bar">
      <el-select
        v-if="links.length"
        v-model="selectedId"
        placeholder="选择链接"
        class="ssd-select"
        data-test="submission-link-select"
        @change="loadSubmissions"
      >
        <el-option v-for="link in links" :key="link.id" :value="link.id" :label="linkLabel(link)" />
      </el-select>
      <el-button v-if="!creating" type="primary" plain :disabled="!classId" @click="startCreate">新建链接</el-button>
      <el-button v-if="selected" :loading="loading" @click="refresh">刷新</el-button>
    </div>

    <el-form v-if="creating" class="ssd-create" label-width="80px" @submit.prevent>
      <el-form-item label="标题">
        <el-input v-model="draft.title" maxlength="100" placeholder="选填，如：第 3 次周测" />
      </el-form-item>
      <el-form-item label="截止时间">
        <el-date-picker v-model="draft.closesAt" type="datetime" :disabled-date="beforeToday" format="YYYY-MM-DD HH:mm" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="saving" data-test="submission-link-create" @click="createLink">生成链接</el-button>
        <el-button v-if="links.length" @click="creating = false">取消</el-button>
      </el-form-item>
      <p class="ssd-muted">
        学生打开链接后填写姓名、学号（老师单独设置过提交密码的学生还要填密码），按页码上传；截止前可反复修改，
        同一学生同一页只保留最新一张。截止 30 天后自动删除照片。
      </p>
    </el-form>

    <div v-else-if="selected" class="ssd-layout">
      <div class="ssd-qr-col">
        <div class="ssd-qr">
          <svg v-if="qr" :viewBox="`-4 -4 ${qr.size} ${qr.size}`" role="img" aria-label="学生上传二维码" shape-rendering="crispEdges">
            <rect x="-4" y="-4" :width="qr.size" :height="qr.size" fill="#fff" />
            <path :d="qr.path" fill="#111" />
          </svg>
        </div>
        <el-button size="small" @click="copyLink">复制链接</el-button>
        <p class="ssd-muted ssd-url">{{ submitUrl }}</p>
      </div>

      <div class="ssd-main">
        <div class="ssd-deadline">
          <el-tag :type="selected.open ? 'success' : 'info'">{{ selected.open ? '收集中' : '已截止' }}</el-tag>
          <span>截止 {{ formatSubmissionTime(selected.closes_at) }}</span>
          <el-date-picker
            v-model="newDeadline"
            type="datetime"
            size="small"
            :disabled-date="beforeToday"
            format="YYYY-MM-DD HH:mm"
            placeholder="改截止时间"
            class="ssd-deadline-picker"
          />
          <el-button size="small" :disabled="!newDeadline" :loading="saving" @click="saveDeadline(newDeadline)">保存</el-button>
          <el-popconfirm v-if="selected.open" title="现在就截止？学生将不能再修改。" width="240" @confirm="saveDeadline(new Date())">
            <template #reference><el-button size="small">立即截止</el-button></template>
          </el-popconfirm>
          <el-popconfirm title="删除链接和学生已上传的全部照片？" width="260" @confirm="deleteLink">
            <template #reference><el-button size="small" type="danger" plain>删除链接</el-button></template>
          </el-popconfirm>
        </div>

        <p class="ssd-summary" data-test="submission-summary">
          已提交 <strong>{{ selected.submitted_students }}</strong> / {{ selected.roster_size }} 人，共 {{ selected.page_count }} 页
        </p>
        <el-alert
          v-if="missingStudentNo"
          type="warning"
          :closable="false"
          :title="`${missingStudentNo} 名学生在名单里没有学号，无法通过链接提交，请先在班级管理补上学号。`"
          class="ssd-alert"
        />
        <el-alert
          v-if="sharedPhotoCount"
          type="warning"
          :closable="false"
          :title="`有 ${sharedPhotoCount} 页照片与其他学生的完全相同，请核对（下表标“相同照片”）。`"
          class="ssd-alert"
        />
        <p v-if="missing.length" class="ssd-missing">未提交（{{ missing.length }}）：{{ missing.map((row) => row.name).join('、') }}</p>

        <el-table :data="submitted" size="small" max-height="320" empty-text="还没有学生提交" row-key="student_id">
          <el-table-column label="姓名" min-width="120">
            <template #default="{ row }">
              {{ row.name }}
              <el-tag v-if="!row.in_class" size="small" type="info">已移出班级</el-tag>
              <el-tag v-if="row.has_password" size="small" type="info">有密码</el-tag>
              <el-tag v-if="row.pages.some((page) => page.same_photo_as.length)" size="small" type="warning">相同照片</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="student_no" label="学号" min-width="90" />
          <el-table-column label="页" min-width="120">
            <template #default="{ row }">{{ row.pages.map((page) => page.page_no).join('、') }}</template>
          </el-table-column>
          <el-table-column label="最后上传" min-width="110">
            <template #default="{ row }">{{ formatSubmissionTime(row.last_uploaded_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="130">
            <template #default="{ row }">
              <el-button text type="primary" size="small" @click="toggleView(row)">{{ viewing === row.student_id ? '收起' : '查看' }}</el-button>
              <el-popconfirm title="清空该学生上传的全部页？截止前学生可重新上传。" width="260" @confirm="clearStudent(row)">
                <template #reference><el-button text type="danger" size="small">清空</el-button></template>
              </el-popconfirm>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="viewingRow" class="ssd-thumbs" :aria-label="`${viewingRow.name} 的照片`">
          <figure v-for="(page, index) in viewingRow.pages" :key="pageKey(page)" class="ssd-thumb">
            <el-image
              v-if="downloads.has(pageKey(page))"
              :src="downloads.get(pageKey(page)).url"
              :alt="`第 ${page.page_no} 页`"
              fit="cover"
              :preview-src-list="viewingUrls"
              :initial-index="index"
              preview-teleported
              hide-on-click-modal
            />
            <span v-else class="ssd-thumb-loading">加载中</span>
            <figcaption>第 {{ page.page_no }} 页</figcaption>
            <span v-if="page.same_photo_as.length" class="ssd-thumb-flag">同 {{ page.same_photo_as.join('、') }}</span>
          </figure>
        </div>
      </div>
    </div>

    <template #footer>
      <div class="ssd-footer">
        <span v-if="importFiles && selected" class="ssd-muted">
          按“学生提交/姓名_学号/第N页”导入；识别时仍会核对试卷上手写的姓名，不一致的交给你确认。
        </span>
        <span class="ssd-spacer"></span>
        <el-button :disabled="importing" @click="emit('update:modelValue', false)">关闭</el-button>
        <el-button
          v-if="importFiles"
          type="primary"
          :loading="importing"
          :disabled="disabled || !importEntries.length || importEntries.length > MAX_IMPORT_PAGES"
          data-test="submission-import"
          @click="importAll"
        >
          {{ importEntries.length > MAX_IMPORT_PAGES ? `超过 ${MAX_IMPORT_PAGES} 张，无法一次导入` : `导入批阅（${importEntries.length} 张）` }}
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<script setup>
// Teacher side of 学生上传: create class links with a deadline, watch who has
// submitted, and (on the grading page) import the pages with each student's
// typed identity as the folder name.
import { computed, onBeforeUnmount, ref, shallowReactive } from 'vue'
import { ElMessage } from 'element-plus'
import qrcode from 'qrcode-generator'
import api from '@/api'
import { phoneCaptureOrigin } from '@/utils/phoneCaptureSession'
import {
  formatSubmissionTime,
  studentSubmitOriginWarning,
  studentSubmitUrl,
  submissionFile,
  submissionImportEntries,
} from '@/utils/studentSubmission'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  classId: { type: [Number, String], default: null },
  className: { type: String, default: '' },
  disabled: { type: Boolean, default: false },
  // async (files: File[]) => boolean; only the grading page passes it.
  importFiles: { type: Function, default: null },
})
const emit = defineEmits(['update:modelValue'])

// The grading page accepts at most 200 images per batch.
const MAX_IMPORT_PAGES = 200
const POLL_MS = 15000

const links = ref([])
const selectedId = ref(null)
const view = ref(null)
const loading = ref(false)
const saving = ref(false)
const importing = ref(false)
const error = ref('')
const creating = ref(false)
const draft = ref({ title: '', closesAt: null })
const newDeadline = ref(null)
const viewing = ref(null)
// Downloaded photos keyed by page id + upload time (a replaced page changes it).
const downloads = shallowReactive(new Map())
let pollTimer = 0
let open = false

const origin = phoneCaptureOrigin()
const originWarning = studentSubmitOriginWarning(origin)
const selected = computed(() => view.value?.link?.id === selectedId.value ? view.value.link : links.value.find((link) => link.id === selectedId.value) || null)
const submitUrl = computed(() => (selected.value ? studentSubmitUrl(origin, selected.value.token) : ''))
const qr = computed(() => {
  if (!submitUrl.value) return null
  const code = qrcode(0, 'M')
  code.addData(submitUrl.value, 'Byte')
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
const rows = computed(() => view.value?.students || [])
const submitted = computed(() => rows.value.filter((row) => row.pages.length))
const missing = computed(() => rows.value.filter((row) => row.in_class && !row.pages.length))
const missingStudentNo = computed(() => view.value?.missing_student_no || 0)
const sharedPhotoCount = computed(() => submitted.value.reduce(
  (count, row) => count + row.pages.filter((page) => page.same_photo_as.length).length, 0,
))
// Students moved out of the class are left out: their folder name would not
// match anyone on the batch's roster.
const importEntries = computed(() => submissionImportEntries(submitted.value.filter((row) => row.in_class)))
const viewingRow = computed(() => submitted.value.find((row) => row.student_id === viewing.value) || null)
const viewingUrls = computed(() => (viewingRow.value?.pages || []).map((page) => downloads.get(pageKey(page))?.url || ''))

function pageKey(page) {
  return `${page.id}@${page.uploaded_at}`
}

function linkLabel(link) {
  const title = link.title || '未命名'
  return `${title} · ${link.open ? `截止 ${formatSubmissionTime(link.closes_at)}` : '已截止'} · ${link.submitted_students}/${link.roster_size} 人`
}

function beforeToday(date) {
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return date < today
}

function defaultDeadline() {
  const deadline = new Date()
  deadline.setDate(deadline.getDate() + 1)
  deadline.setHours(22, 0, 0, 0)
  return deadline
}

function startCreate() {
  draft.value = { title: '', closesAt: defaultDeadline() }
  creating.value = true
}

function dropDownloads(keep = () => false) {
  for (const [key, { url }] of downloads) {
    if (keep(key)) continue
    URL.revokeObjectURL(url)
    downloads.delete(key)
  }
}

async function loadLinks() {
  links.value = (await api.submissions.list(props.classId, { suppressErrorToast: true })).links || []
  if (!links.value.some((link) => link.id === selectedId.value)) selectedId.value = links.value[0]?.id ?? null
  creating.value = !links.value.length
  if (creating.value) startCreate()
}

async function loadSubmissions() {
  if (!selectedId.value) {
    view.value = null
    return
  }
  const linkId = selectedId.value
  const next = await api.submissions.status(linkId, { suppressErrorToast: true })
  if (linkId !== selectedId.value) return
  view.value = next
  links.value = links.value.map((link) => (link.id === next.link.id ? next.link : link))
  const current = new Set(next.students.flatMap((row) => row.pages.map(pageKey)))
  dropDownloads((key) => current.has(key))
  if (viewing.value) await downloadPages(viewingRow.value?.pages || [])
}

async function withLoading(task) {
  loading.value = true
  try {
    await task()
    error.value = ''
  } catch (err) {
    error.value = err.message || '无法读取学生上传情况'
  } finally {
    loading.value = false
  }
}

function refresh() {
  return withLoading(loadSubmissions)
}

async function downloadPages(pages) {
  for (const page of pages) {
    const key = pageKey(page)
    if (downloads.has(key)) continue
    const blob = await api.submissions.image(selectedId.value, page.id)
    downloads.set(key, { blob, url: URL.createObjectURL(blob) })
  }
}

async function toggleView(row) {
  viewing.value = viewing.value === row.student_id ? null : row.student_id
  if (!viewing.value) return
  try {
    await downloadPages(row.pages)
  } catch {
    ElMessage.error('照片加载失败，请刷新后重试')
  }
}

async function createLink() {
  if (!draft.value.closesAt) {
    ElMessage.warning('请选择截止时间')
    return
  }
  saving.value = true
  try {
    const link = await api.submissions.create({
      class_id: Number(props.classId),
      title: draft.value.title,
      closes_at: new Date(draft.value.closesAt).toISOString(),
    })
    creating.value = false
    selectedId.value = link.id
    await withLoading(async () => {
      await loadLinks()
      await loadSubmissions()
    })
  } catch {
    // The API layer already reported the failure.
  } finally {
    saving.value = false
  }
}

async function saveDeadline(value) {
  saving.value = true
  try {
    await api.submissions.update(selectedId.value, { closes_at: new Date(value).toISOString() })
    newDeadline.value = null
    await refresh()
  } catch {
    // The API layer already reported the failure.
  } finally {
    saving.value = false
  }
}

async function deleteLink() {
  try {
    await api.submissions.remove(selectedId.value)
  } catch {
    return
  }
  selectedId.value = null
  view.value = null
  dropDownloads()
  await withLoading(async () => {
    await loadLinks()
    await loadSubmissions()
  })
}

async function clearStudent(row) {
  try {
    await api.submissions.clearStudent(selectedId.value, row.student_id)
  } catch {
    return
  }
  if (viewing.value === row.student_id) viewing.value = null
  await refresh()
}

async function copyLink() {
  try {
    await navigator.clipboard.writeText(submitUrl.value)
    ElMessage.success('链接已复制，可以发到班级群')
  } catch {
    ElMessage.warning('无法访问剪贴板，请手动复制下方链接')
  }
}

async function importAll() {
  importing.value = true
  try {
    await loadSubmissions()
    const entries = importEntries.value
    const pagesById = new Map(submitted.value.flatMap((row) => row.pages.map((page) => [page.id, page])))
    await downloadPages(entries.map((entry) => pagesById.get(entry.pageId)))
    const files = entries.map((entry) => submissionFile(downloads.get(pageKey(pagesById.get(entry.pageId))).blob, entry.path))
    if (await props.importFiles(files)) emit('update:modelValue', false)
  } catch {
    ElMessage.error('照片下载失败，请稍后再导入')
  } finally {
    importing.value = false
  }
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer)
  pollTimer = 0
}

async function onOpen() {
  open = true
  error.value = ''
  viewing.value = null
  if (!props.classId) {
    error.value = '请先在顶部导航选择班级'
    return
  }
  await withLoading(async () => {
    await loadLinks()
    await loadSubmissions()
  })
  if (!open) return
  stopPolling()
  pollTimer = window.setInterval(() => {
    if (!loading.value && !importing.value && selectedId.value) refresh()
  }, POLL_MS)
}

function onClosed() {
  open = false
  stopPolling()
}

onBeforeUnmount(() => {
  open = false
  stopPolling()
  dropDownloads()
})
</script>

<style scoped>
.ssd-alert { margin-bottom: 10px; }
.ssd-bar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
.ssd-select { flex: 1 1 320px; min-width: 0; }
.ssd-create { max-width: 560px; }
.ssd-muted { margin: 0; font-size: 12px; line-height: 1.6; color: var(--el-text-color-secondary); }
.ssd-layout { display: flex; gap: 20px; align-items: flex-start; flex-wrap: wrap; }
.ssd-qr-col { flex: 0 0 200px; display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center; }
.ssd-qr { width: 184px; height: 184px; display: flex; align-items: center; justify-content: center; background: #fff; border: 1px solid var(--el-border-color); border-radius: 8px; }
.ssd-qr svg { width: 176px; height: 176px; }
.ssd-url { word-break: break-all; }
.ssd-main { flex: 1 1 420px; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
.ssd-deadline { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 14px; }
.ssd-deadline-picker { width: 190px; }
.ssd-summary { margin: 0; font-size: 14px; }
.ssd-missing { margin: 0; font-size: 13px; line-height: 1.6; color: var(--el-text-color-regular); }
.ssd-thumbs { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 8px; }
.ssd-thumb { position: relative; margin: 0; aspect-ratio: 3 / 4; border-radius: 6px; overflow: hidden; background: var(--el-fill-color-light); border: 1px solid var(--el-border-color-lighter); }
.ssd-thumb :deep(.el-image) { width: 100%; height: 100%; display: block; cursor: zoom-in; }
.ssd-thumb figcaption { position: absolute; left: 0; top: 0; padding: 1px 6px; font-size: 12px; background: rgba(0, 0, 0, 0.6); color: #fff; border-bottom-right-radius: 6px; }
.ssd-thumb-flag { position: absolute; left: 0; right: 0; bottom: 0; padding: 1px 4px; font-size: 11px; background: rgba(230, 162, 60, 0.9); color: #fff; }
.ssd-thumb-loading { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 12px; color: var(--el-text-color-secondary); }
.ssd-footer { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; text-align: left; }
.ssd-spacer { flex: 1; }
@media (max-width: 640px) {
  .ssd-qr-col { flex-basis: 100%; }
}
</style>
