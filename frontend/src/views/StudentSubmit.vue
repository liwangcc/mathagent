<template>
  <main class="ss-page">
    <header class="ss-head">
      <h1>{{ info?.title || '提交试卷照片' }}</h1>
      <p v-if="info" class="ss-sub">
        {{ info.class_name }} · {{ info.open ? `截止 ${formatSubmissionTime(info.closes_at)}` : '已截止' }}
      </p>
    </header>

    <p v-if="loading" class="ss-note">正在打开…</p>
    <section v-else-if="linkError" class="ss-card ss-error" role="alert">{{ linkError }}</section>

    <form v-else-if="!status" class="ss-card ss-form" data-test="submit-identity" @submit.prevent="identify">
      <p class="ss-note">填写与老师名单一致的姓名和学号。提交后，截止前可以随时用同样的姓名和学号回来修改。</p>
      <label>
        <span>姓名</span>
        <input v-model="form.name" name="name" autocomplete="name" maxlength="50" required />
      </label>
      <label>
        <span>学号</span>
        <input v-model="form.studentNo" name="student_no" autocomplete="off" maxlength="50" required />
      </label>
      <label>
        <span>提交密码（选填）</span>
        <input v-model="form.password" name="password" type="password" autocomplete="off" maxlength="32" placeholder="老师没给过密码就不填" />
      </label>
      <p v-if="formError" class="ss-error-text" role="alert">{{ formError }}</p>
      <button type="submit" class="ss-primary" :disabled="identifying">{{ identifying ? '正在核对…' : '下一步' }}</button>
    </form>

    <section v-else class="ss-card" data-test="submit-pages">
      <div class="ss-who">
        <span><strong>{{ status.student_name }}</strong> · 学号 {{ status.student_no }}</span>
        <button type="button" class="ss-link" :disabled="busy" @click="switchStudent">不是我</button>
      </div>

      <p v-if="!status.open" class="ss-closed" role="status">已过截止时间，只能查看，不能再修改。</p>
      <p v-else class="ss-note">
        按试卷页码顺序上传：第 1 页放试卷第一页。每页一张照片，拍完整、拍清楚、文字正着。
      </p>

      <p v-if="actionError" class="ss-error-text" role="alert">{{ actionError }}</p>
      <p class="ss-count" role="status">
        {{ uploadedCount ? `已上传 ${uploadedCount} 页` : '还没有上传' }}<template v-if="progress"> · {{ progress }}</template>
      </p>

      <ol class="ss-pages">
        <li v-for="slot in slots" :key="slot.pageNo" class="ss-slot" :class="{ 'is-empty': !slot.page }">
          <div class="ss-thumb">
            <img v-if="previews.get(slot.pageNo)" :src="previews.get(slot.pageNo)" :alt="`第 ${slot.pageNo} 页`" />
            <span v-else>{{ slot.page ? '已上传' : '未上传' }}</span>
          </div>
          <div class="ss-slot-body">
            <strong>第 {{ slot.pageNo }} 页</strong>
            <small v-if="slot.page">{{ formatSubmissionTime(slot.page.uploaded_at) }} · {{ formatBytes(slot.page.bytes) }}</small>
            <div v-if="status.open" class="ss-actions">
              <button type="button" :disabled="busy" @click="pickFor(slot.pageNo)">{{ slot.page ? '重拍' : '上传' }}</button>
              <label v-if="slot.page && slots.length > 1" class="ss-move">
                <span>改为第</span>
                <select :value="slot.pageNo" :disabled="busy" @change="movePage(slot.pageNo, $event.target.value)">
                  <option v-for="other in slots" :key="other.pageNo" :value="other.pageNo">{{ other.pageNo }}</option>
                </select>
                <span>页</span>
              </label>
              <button
                v-if="slot.page"
                type="button"
                class="ss-danger"
                :disabled="busy"
                @click="removePage(slot.pageNo)"
              >{{ confirmingDelete === slot.pageNo ? '确认删除' : '删除' }}</button>
            </div>
          </div>
        </li>
      </ol>

      <button
        v-if="status.open && remainingPages > 0"
        type="button"
        class="ss-primary"
        data-test="submit-add-photos"
        :disabled="busy"
        @click="pickMore"
      >{{ slots.length ? '继续添加照片' : '选择照片' }}</button>
      <p v-if="status.open" class="ss-note">可以一次选多张，会按选择顺序排在后面；排错了可以用“改为第几页”调整。</p>
    </section>

    <input ref="fileInput" type="file" accept="image/*" hidden @change="onFilesPicked" />
  </main>
</template>

<script setup>
// Public page behind a class upload link: no account, no login.  The student
// identifies with name + student number (+ the optional password), then adds,
// replaces, renumbers or deletes pages until the deadline.
import { computed, onBeforeUnmount, onMounted, reactive, ref, shallowReactive } from 'vue'
import { useRoute } from 'vue-router'
import { studentSubmitClient } from '@/api'
import { compressImage } from '@/utils/uploadValidation'
import { formatBytes, formatSubmissionTime, nextPageNumbers, pageSlots } from '@/utils/studentSubmission'

// Large enough for handwriting; the grading page shrinks to 1920 anyway.
const UPLOAD_MAX_EDGE = 2560
const UPLOAD_QUALITY = 0.92

const route = useRoute()
const client = studentSubmitClient(String(route.params.token || ''))

const loading = ref(true)
const info = ref(null)
const linkError = ref('')
const form = reactive({ name: '', studentNo: '', password: '' })
const formError = ref('')
const identifying = ref(false)
// The identity the server accepted; every later call repeats it.
const identity = ref(null)
const status = ref(null)
const busy = ref(false)
const actionError = ref('')
const progress = ref('')
const confirmingDelete = ref(0)
// Photos taken on this visit, by page number.  Uploaded photos are never sent
// back by the server, so earlier pages show as "已上传" without a picture.
const previews = shallowReactive(new Map())
const fileInput = ref(null)
let pickTarget = null

const slots = computed(() => pageSlots(status.value?.pages))
const uploadedCount = computed(() => status.value?.pages?.length || 0)
const remainingPages = computed(() => nextPageNumbers(status.value?.pages, Infinity, status.value?.max_pages || 0).length)

function dropPreview(pageNo) {
  const url = previews.get(pageNo)
  if (url) URL.revokeObjectURL(url)
  previews.delete(pageNo)
}

function dropAllPreviews() {
  for (const pageNo of [...previews.keys()]) dropPreview(pageNo)
}

// Errors after identifying: a link that is gone or an identity that no longer
// passes (password added, student moved) sends the student back a step.
function handleActionError(error) {
  if (error.status === 404) {
    linkError.value = error.message
    status.value = null
  } else if (error.status === 403) {
    switchStudent()
    formError.value = error.message
  } else {
    actionError.value = error.message
    if (error.status === 409) refreshStatus()
  }
}

async function refreshStatus() {
  try {
    status.value = await client.status(identity.value)
  } catch (error) {
    handleActionError(error)
  }
}

async function identify() {
  formError.value = ''
  if (!form.name.trim() || !form.studentNo.trim()) {
    formError.value = '请填写姓名和学号'
    return
  }
  identifying.value = true
  try {
    const candidate = { ...form }
    status.value = await client.status(candidate)
    identity.value = candidate
    actionError.value = ''
  } catch (error) {
    if (error.status === 404) linkError.value = error.message
    else formError.value = error.message
  } finally {
    identifying.value = false
  }
}

function switchStudent() {
  identity.value = null
  status.value = null
  form.password = ''
  actionError.value = ''
  progress.value = ''
  confirmingDelete.value = 0
  dropAllPreviews()
}

function openPicker(target, multiple) {
  pickTarget = target
  actionError.value = ''
  confirmingDelete.value = 0
  fileInput.value.multiple = multiple
  fileInput.value.click()
}

function pickFor(pageNo) {
  openPicker(pageNo, false)
}

function pickMore() {
  openPicker(null, true)
}

async function onFilesPicked(event) {
  const files = Array.from(event.target.files || [])
  event.target.value = ''
  if (!files.length || !status.value) return
  const pageNumbers = pickTarget ? [pickTarget] : nextPageNumbers(status.value.pages, files.length, status.value.max_pages)
  if (pageNumbers.length < files.length) {
    actionError.value = `最多上传 ${status.value.max_pages} 页，多出的照片没有上传`
  }
  busy.value = true
  try {
    for (const [index, pageNo] of pageNumbers.entries()) {
      progress.value = pageNumbers.length > 1 ? `正在上传第 ${index + 1} / ${pageNumbers.length} 张` : `正在上传第 ${pageNo} 页`
      let blob
      try {
        blob = await compressImage(files[index], UPLOAD_MAX_EDGE, UPLOAD_QUALITY, 0, { suppressReviewInk: false })
      } catch {
        actionError.value = `第 ${pageNo} 页的照片无法读取，请换一张 JPG 或 PNG 照片`
        break
      }
      try {
        status.value = await client.upload(identity.value, pageNo, blob)
      } catch (error) {
        handleActionError(error)
        break
      }
      dropPreview(pageNo)
      previews.set(pageNo, URL.createObjectURL(blob))
    }
  } finally {
    busy.value = false
    progress.value = ''
  }
}

async function movePage(pageNo, value) {
  const toPageNo = Number(value)
  if (!toPageNo || toPageNo === pageNo) return
  busy.value = true
  actionError.value = ''
  try {
    status.value = await client.move(identity.value, pageNo, toPageNo)
    const moving = previews.get(pageNo)
    const other = previews.get(toPageNo)
    previews.delete(pageNo)
    previews.delete(toPageNo)
    if (moving) previews.set(toPageNo, moving)
    if (other) previews.set(pageNo, other)
  } catch (error) {
    handleActionError(error)
  } finally {
    busy.value = false
  }
}

async function removePage(pageNo) {
  if (confirmingDelete.value !== pageNo) {
    confirmingDelete.value = pageNo
    return
  }
  confirmingDelete.value = 0
  busy.value = true
  actionError.value = ''
  try {
    status.value = await client.remove(identity.value, pageNo)
    dropPreview(pageNo)
  } catch (error) {
    handleActionError(error)
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  try {
    info.value = await client.info()
    document.title = info.value.title || '提交试卷照片'
  } catch (error) {
    linkError.value = error.status === 404 ? error.message : `${error.message}，请稍后刷新重试`
  } finally {
    loading.value = false
  }
})

onBeforeUnmount(dropAllPreviews)
</script>

<style scoped>
.ss-page {
  max-width: 560px;
  margin: 0 auto;
  padding: 20px 16px 40px;
  color: #1f2328;
  font-size: 15px;
  line-height: 1.6;
}
.ss-head h1 { margin: 0; font-size: 22px; }
.ss-sub { margin: 4px 0 16px; color: #57606a; }
.ss-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  border: 1px solid #d0d7de;
  border-radius: 10px;
  background: #fff;
}
.ss-form label { display: flex; flex-direction: column; gap: 4px; }
.ss-form input {
  font-size: 16px; /* keeps iOS from zooming into the field */
  padding: 10px 12px;
  border: 1px solid #d0d7de;
  border-radius: 8px;
}
.ss-note { margin: 0; color: #57606a; font-size: 14px; }
.ss-error { color: #b42318; }
.ss-error-text { margin: 0; color: #b42318; font-size: 14px; }
.ss-closed { margin: 0; padding: 8px 12px; border-radius: 8px; background: #fff4e5; color: #8a4b00; }
.ss-primary {
  padding: 12px;
  border: 0;
  border-radius: 8px;
  background: #1a56db;
  color: #fff;
  font-size: 16px;
}
.ss-primary:disabled { opacity: 0.6; }
.ss-who { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
.ss-link { border: 0; background: none; color: #1a56db; font-size: 14px; padding: 4px; }
.ss-count { margin: 0; font-weight: 600; }
.ss-pages { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.ss-slot { display: flex; gap: 12px; align-items: flex-start; padding: 8px; border: 1px solid #eaeef2; border-radius: 8px; }
.ss-slot.is-empty { border-style: dashed; }
.ss-thumb {
  flex: 0 0 72px;
  height: 96px;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  border-radius: 6px;
  background: #f6f8fa;
  color: #57606a;
  font-size: 12px;
}
.ss-thumb img { width: 100%; height: 100%; object-fit: cover; }
.ss-slot-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.ss-slot-body small { color: #57606a; }
.ss-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.ss-actions button {
  padding: 6px 12px;
  border: 1px solid #d0d7de;
  border-radius: 6px;
  background: #fff;
  font-size: 14px;
}
.ss-actions .ss-danger { color: #b42318; border-color: #f1b5ae; }
.ss-move { display: inline-flex; align-items: center; gap: 4px; font-size: 14px; color: #57606a; }
.ss-move select { font-size: 14px; padding: 4px; }
</style>
