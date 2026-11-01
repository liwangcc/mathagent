<template>
  <div class="past-papers-page page-shell">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <div class="card-head-left">
            <span class="head-title">整卷真题</span>
            <el-tag size="small">高考数学</el-tag>
            <HelpButton
              title="整卷真题"
              content="按地区或卷型查找高考数学整卷。每卷分别标注版本、检查范围及实际页面尺寸：忠实重排卷保留逐题来源核对与独立复核说明；网络原版 PDF 只作身份、角色、技术完整性检查与分层视觉抽样，未独立逐题核验数学内容。答案/解析按真实性质另行标注（来源配套、多来源核对或 AI 解答），不代表官方认证或出版社再分发许可。"
            />
          </div>
          <div class="card-head-right">
            <el-button @click="router.push('/bank')">返回题库</el-button>
          </div>
        </div>
      </template>

      <div class="toolbar" role="search" aria-label="整卷真题筛选">
        <el-radio-group v-model="mode" aria-label="浏览方式" @change="onModeChange">
          <el-radio-button :value="BROWSE_MODES.region">按地区</el-radio-button>
          <el-radio-button :value="BROWSE_MODES.series">按卷型</el-radio-button>
        </el-radio-group>
        <el-select
          v-if="mode === BROWSE_MODES.region"
          v-model="region"
          placeholder="选择省份 / 直辖市"
          clearable
          filterable
          class="filter-wide"
          aria-label="地区"
          @change="load"
        >
          <el-option v-for="r in facets.regions" :key="r.key" :label="r.name" :value="r.key" />
        </el-select>
        <el-select
          v-else
          v-model="seriesKey"
          placeholder="全部卷型"
          clearable
          class="filter-wide"
          aria-label="卷型"
          @change="load"
        >
          <el-option v-for="s in facets.series" :key="s.key" :label="s.label" :value="s.key" />
        </el-select>
        <el-select v-model="year" placeholder="全部年份" clearable class="filter" aria-label="年份" @change="load">
          <el-option v-for="y in facets.years" :key="y" :label="`${y}年`" :value="y" />
        </el-select>
        <el-select v-model="track" placeholder="全部文理" class="filter" aria-label="文理" @change="load">
          <el-option v-for="t in TRACK_OPTIONS" :key="t.value" :label="t.label" :value="t.value" />
        </el-select>
      </div>

      <p class="scope-note">
        每卷的版本、检查范围与页面尺寸分别标注；网络原版 PDF 保留来源版式及水印，未独立逐题核验数学内容。
        答案/解析是非官方参考资料；下载使用不代表获得出版社再分发许可。
      </p>

      <div v-if="viewState === 'loading'" v-loading="true" class="state-box" element-loading-text="正在加载整卷真题…" />

      <el-result v-else-if="viewState === 'error'" icon="error" title="整卷真题加载失败" :sub-title="errorMessage">
        <template #extra><el-button type="primary" @click="load">重试</el-button></template>
      </el-result>

      <el-empty v-else-if="viewState === 'stage'" :description="data.message">
        <el-button type="primary" plain @click="router.push('/profile')">前往个人资料</el-button>
      </el-empty>

      <el-empty v-else-if="viewState === 'library-empty'" description="试卷库暂未开放可下载的整卷真题">
        <p v-if="withheldText" class="withheld">{{ withheldText }}</p>
      </el-empty>

      <el-empty
        v-else-if="viewState === 'choose-region' && !facets.regions.length"
        description="现有试卷的适用地区尚在核实中，请先按卷型浏览"
      >
        <el-button type="primary" plain @click="mode = BROWSE_MODES.series; onModeChange()">按卷型浏览</el-button>
      </el-empty>

      <section v-else-if="viewState === 'choose-region'" class="region-picker" aria-label="选择地区">
        <p class="picker-hint">请选择省份或直辖市，查看该地历年实际使用的数学试卷：</p>
        <div class="region-grid">
          <el-button v-for="r in facets.regions" :key="r.key" plain @click="pickRegion(r.key)">
            {{ r.name }}<span class="region-years">{{ r.years.length }} 年</span>
          </el-button>
        </div>
      </section>

      <el-empty v-else-if="viewState === 'filtered-empty'" description="没有符合条件的试卷">
        <el-button type="primary" plain @click="clearFilters">清除筛选</el-button>
      </el-empty>

      <section v-else class="results" aria-label="试卷列表">
        <p v-if="withheldText" class="withheld">{{ withheldText }}</p>
        <div v-for="group in groups" :key="group.year" class="year-group">
          <h3 class="year-title">
            {{ group.year }}年
            <small v-if="mode === BROWSE_MODES.region && regionName">{{ regionName }}实际使用的试卷</small>
          </h3>
          <article v-for="item in group.papers" :key="item.paper_id" class="paper-card" :data-paper-id="item.paper_id">
            <div class="paper-main">
              <div class="paper-title-row">
                <h4>{{ paperHeadline(item) }}</h4>
                <el-tag size="small" :type="item.kind === 'national' ? 'primary' : 'warning'" effect="plain">
                  {{ item.kind === 'national' ? '全国统一命题' : '自主命题' }}
                </el-tag>
                <el-tag v-if="item.session === 'spring'" size="small" type="info">春季高考</el-tag>
                <el-tag size="small" type="success" effect="plain">{{ editionLabel(item) }}</el-tag>
              </div>
              <p class="paper-meta">
                <span v-if="item.question_count">共 {{ item.question_count }} 题</span>
                <span v-if="item.total_points">满分 {{ item.total_points }} 分</span>
                <span v-if="studentPageLabel(item)">{{ studentPageLabel(item) }}</span>
                <span :class="['answer-flag', answerState(item).available ? 'has-answer' : 'no-answer']">
                  {{ answerState(item).label }}
                </span>
              </p>
              <p class="paper-regions">{{ verificationState(item).label }}：{{ verificationState(item).description }}</p>
              <p v-if="printNote(item)" class="paper-regions">{{ printNote(item) }}</p>
              <p v-if="item.regions.length" class="paper-regions">适用地区：{{ regionSummary(item.regions) }}</p>
            </div>
            <div class="paper-actions">
              <el-button @click="openPreview(item)">预览</el-button>
              <el-button
                type="primary"
                :loading="downloading === `${item.paper_id}:student`"
                @click="download(item, 'student')"
              >下载试卷</el-button>
              <el-tooltip :content="answerState(item).tooltip" placement="top">
                <span class="tooltip-wrap">
                  <el-button
                    :disabled="!answerState(item).available"
                    :loading="downloading === `${item.paper_id}:answer`"
                    @click="download(item, 'answer')"
                  >下载答案/解析</el-button>
                </span>
              </el-tooltip>
            </div>
          </article>
        </div>
      </section>
    </el-card>

    <el-dialog
      v-model="previewVisible"
      :title="previewItem ? paperHeadline(previewItem) : '试卷预览'"
      width="920px"
      class="past-paper-preview"
      :fullscreen="narrowScreen"
      @closed="closePreview"
    >
      <template v-if="previewItem">
        <el-tabs v-model="previewKind" @tab-change="loadPage(1)">
          <el-tab-pane label="试卷" name="student" />
          <el-tab-pane label="答案/解析" name="answer" :disabled="!detail?.answer?.available" />
        </el-tabs>
        <div class="preview-layout">
          <div class="page-viewer">
            <div class="page-nav">
              <el-button size="small" :disabled="page <= 1 || pageLoading" @click="loadPage(page - 1)">上一页</el-button>
              <span>第 {{ page }} / {{ pageCount || '—' }} 页</span>
              <el-button size="small" :disabled="page >= pageCount || pageLoading" @click="loadPage(page + 1)">下一页</el-button>
            </div>
            <div v-loading="pageLoading" class="page-frame">
              <img v-if="pageUrl" :src="pageUrl" :alt="`第 ${page} 页预览`" class="page-image">
              <p v-else-if="pageError" class="page-error">{{ pageError }}</p>
            </div>
          </div>
          <aside class="paper-details" aria-label="试卷说明">
            <div v-if="detailLoading" v-loading="true" class="detail-loading" />
            <template v-else-if="detail">
              <h5>版本说明</h5>
              <p>{{ detail.edition?.description }}</p>
              <p>{{ studentPageLabel(detail) }}</p>
              <p v-if="printNote(detail)">{{ printNote(detail) }}</p>
              <h5>{{ verificationState(detail).label }}</h5>
              <p>{{ verificationState(detail).description }}</p>
              <h5 v-if="detail.regions.length">适用地区</h5>
              <p v-if="detail.regions.length">{{ detail.regions.map((r) => r.name).join('、') }}</p>
              <h5>答案/解析</h5>
              <p>{{ answerState(detail).tooltip }}</p>
              <p v-if="detail.answer?.available">{{ filePageLabel(detail.answer, detail.verification?.status === 'verified') }}</p>
              <h5>使用说明</h5>
              <p>{{ detail.rights?.notice }}</p>
              <p v-for="note in detail.notes" :key="note" class="detail-note">{{ note }}</p>
              <template v-if="detail.attribution.length">
                <h5>署名</h5>
                <p v-for="a in detail.attribution" :key="a.work" class="detail-note">
                  {{ a.work }}（{{ a.license }}）：{{ a.use }} {{ a.url }}
                </p>
              </template>
            </template>
            <p v-else-if="detailError" class="page-error">{{ detailError }}</p>
          </aside>
        </div>
      </template>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
        <el-button
          :disabled="!detail?.answer?.available"
          :loading="downloading === `${previewItem?.paper_id}:answer`"
          @click="download(previewItem, 'answer')"
        >下载答案/解析</el-button>
        <el-button
          type="primary"
          :loading="downloading === `${previewItem?.paper_id}:student`"
          @click="download(previewItem, 'student')"
        >下载试卷</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '@/api'
import HelpButton from '@/components/HelpButton.vue'
import {
  BROWSE_MODES,
  TRACK_OPTIONS,
  answerState,
  buildPastPaperQuery,
  downloadName,
  editionLabel,
  filePageLabel,
  groupByYear,
  listViewState,
  paperHeadline,
  printNote,
  regionSummary,
  studentPageLabel,
  verificationState,
} from '@/utils/pastPapers'

const router = useRouter()
const mode = ref(BROWSE_MODES.region)
const region = ref('')
const seriesKey = ref('')
const year = ref('')
const track = ref('')
const data = ref(null)
const loading = ref(false)
const errorMessage = ref('')
const downloading = ref('')
const narrowScreen = ref(typeof window !== 'undefined' && window.innerWidth < 760)

const facets = computed(() => data.value?.facets || { years: [], series: [], regions: [] })
const viewState = computed(() => listViewState({
  loading: loading.value,
  error: errorMessage.value,
  data: data.value,
  mode: mode.value,
  region: region.value,
}))
const groups = computed(() => groupByYear(data.value?.items || []))
const regionName = computed(() => facets.value.regions.find((r) => r.key === region.value)?.name || '')
const withheldText = computed(() => {
  const withheld = data.value?.withheld
  if (!withheld) return ''
  const parts = []
  if (withheld.rights_pending) parts.push(`${withheld.rights_pending} 套标记为旧 verified 的未开放资料（核验方法或权益条件需确认）`)
  if (withheld.not_verified) parts.push(`${withheld.not_verified} 套其他未开放资料（含检查或权益条件未满足）`)
  return parts.length ? `管理员提示：另有${parts.join('，')}，未向教师开放。` : ''
})

let loadToken = 0
async function load() {
  const token = ++loadToken
  loading.value = true
  errorMessage.value = ''
  try {
    const params = buildPastPaperQuery({
      mode: mode.value, region: region.value, seriesKey: seriesKey.value, year: year.value, track: track.value,
    })
    const result = await api.pastPapers.list(params, { suppressErrorToast: true })
    if (token === loadToken) data.value = result
  } catch (err) {
    if (token === loadToken) errorMessage.value = err?.message || '请稍后重试'
  } finally {
    if (token === loadToken) loading.value = false
  }
}

function onModeChange() {
  region.value = ''
  seriesKey.value = ''
  load()
}

function pickRegion(key) {
  region.value = key
  load()
}

function clearFilters() {
  region.value = ''
  seriesKey.value = ''
  year.value = ''
  track.value = ''
  load()
}

async function download(item, kind) {
  if (!item || downloading.value) return
  downloading.value = `${item.paper_id}:${kind}`
  try {
    await api.pastPapers.download(item.paper_id, kind, downloadName(item, kind))
  } catch (err) {
    ElMessage.error(`下载失败：${err?.message || '请稍后重试'}`)
  } finally {
    downloading.value = ''
  }
}

// ---- preview
const previewVisible = ref(false)
const previewItem = ref(null)
const previewKind = ref('student')
const detail = ref(null)
const detailLoading = ref(false)
const detailError = ref('')
const page = ref(1)
const pageUrl = ref('')
const pageError = ref('')
const pageLoading = ref(false)
const pageCount = computed(() => {
  if (!previewItem.value) return 0
  if (previewKind.value === 'answer') return detail.value?.answer?.preview_pages || 0
  return previewItem.value.preview_pages || 0
})
let previewToken = 0

function releasePage() {
  if (pageUrl.value) URL.revokeObjectURL(pageUrl.value)
  pageUrl.value = ''
}

async function loadPage(target) {
  const token = ++previewToken
  const item = previewItem.value
  if (!item) return
  page.value = Math.max(1, target)
  releasePage()
  pageError.value = ''
  if (!pageCount.value) {
    pageError.value = '该文件暂无预览'
    return
  }
  pageLoading.value = true
  try {
    const url = await api.pastPapers.previewImage(item.paper_id, previewKind.value, page.value)
    if (token === previewToken) pageUrl.value = url
    else URL.revokeObjectURL(url)
  } catch (err) {
    if (token === previewToken) pageError.value = `预览加载失败：${err?.message || '请稍后重试'}`
  } finally {
    if (token === previewToken) pageLoading.value = false
  }
}

async function openPreview(item) {
  previewItem.value = item
  previewKind.value = 'student'
  previewVisible.value = true
  detail.value = null
  detailError.value = ''
  detailLoading.value = true
  loadPage(1)
  try {
    const result = await api.pastPapers.detail(item.paper_id)
    if (previewItem.value?.paper_id === item.paper_id) detail.value = result
  } catch (err) {
    if (previewItem.value?.paper_id === item.paper_id) detailError.value = `试卷说明加载失败：${err?.message || ''}`
  } finally {
    detailLoading.value = false
  }
}

function closePreview() {
  previewToken += 1
  releasePage()
  previewItem.value = null
  detail.value = null
}

function onResize() {
  narrowScreen.value = window.innerWidth < 760
}

onMounted(() => {
  window.addEventListener('resize', onResize)
  load()
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  releasePage()
})
</script>

<style scoped>
.card-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.card-head-left { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.head-title { font-size: 16px; font-weight: 600; }
.toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 8px; }
.filter { width: 130px; }
.filter-wide { width: 260px; }
.scope-note { color: var(--el-text-color-secondary); font-size: 13px; margin: 4px 0 14px; line-height: 1.6; }
.state-box { min-height: 220px; }
.withheld { color: var(--el-color-warning-dark-2, #b88230); font-size: 13px; margin: 0 0 12px; }
.picker-hint { margin: 6px 0 12px; color: var(--el-text-color-regular); }
.region-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(112px, 1fr)); gap: 10px; }
.region-grid .el-button { margin: 0; justify-content: space-between; }
.region-years { margin-left: 6px; color: var(--el-text-color-secondary); font-size: 12px; }
.year-group { margin-bottom: 18px; }
.year-title { font-size: 15px; margin: 0 0 8px; padding-bottom: 6px; border-bottom: 1px solid var(--el-border-color-lighter); }
.year-title small { font-weight: normal; color: var(--el-text-color-secondary); margin-left: 8px; }
.paper-card { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 12px 14px;
  border: 1px solid var(--el-border-color-lighter); border-radius: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.paper-main { flex: 1 1 360px; min-width: 0; }
.paper-title-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.paper-title-row h4 { margin: 0; font-size: 15px; }
.paper-meta { display: flex; flex-wrap: wrap; gap: 12px; margin: 6px 0 0; color: var(--el-text-color-secondary); font-size: 13px; }
.answer-flag.has-answer { color: var(--el-color-success); }
.answer-flag.no-answer { color: var(--el-text-color-placeholder); }
.paper-regions { margin: 4px 0 0; font-size: 13px; color: var(--el-text-color-regular); }
.paper-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.paper-actions .el-button { margin: 0; }
/* The global theme only changes the cursor of disabled buttons; an unavailable
   answer file must not look downloadable. */
.paper-actions .el-button.is-disabled,
.past-paper-preview .el-button.is-disabled { opacity: 0.45; border-style: dashed; }
.tooltip-wrap { display: inline-flex; }
.preview-layout { display: grid; grid-template-columns: minmax(0, 1fr) 260px; gap: 16px; }
.page-nav { display: flex; align-items: center; justify-content: center; gap: 12px; margin-bottom: 8px; }
.page-frame { min-height: 420px; border: 1px solid var(--el-border-color-lighter); background: #fff;
  display: flex; align-items: flex-start; justify-content: center; overflow: auto; }
.page-image { width: 100%; height: auto; display: block; }
.page-error { color: var(--el-color-danger); padding: 24px; }
.paper-details { font-size: 13px; line-height: 1.6; color: var(--el-text-color-regular); }
.paper-details h5 { margin: 10px 0 2px; font-size: 13px; color: var(--el-text-color-primary); }
.paper-details p { margin: 0; }
.detail-note { color: var(--el-text-color-secondary); word-break: break-all; }
.detail-loading { min-height: 120px; }
@media (max-width: 760px) {
  .filter, .filter-wide { width: 100%; }
  .preview-layout { grid-template-columns: 1fr; }
}
</style>
