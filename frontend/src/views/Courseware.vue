<template>
  <div class="courseware-page page-shell page-shell--narrow">
    <section class="courseware-hero"><div><p class="eyebrow">MathAgent · 课件库</p><h1>把错题变成可操作的讲解</h1><p class="hero-copy">每个课件只讲清一个知识点，并把图形、参数和规律放在同一个教学场景里。</p></div></section>
    <section class="toolbar" aria-label="课件筛选"><div class="stage-context" aria-label="当前教师学段">{{ stageLabel }}<small>已按教师学段筛选</small></div><el-input v-model="query" clearable class="search-input" placeholder="搜索章节、大知识点或知识点" /></section>
    <div class="topic-strip"><span class="topic-label">常用章节</span><el-button v-for="item in chapterShortcuts" :key="item" size="small" :type="query === item ? 'primary' : 'default'" plain @click="query = item">{{ item }}</el-button></div>
    <section class="recommend-heading"><div><h2>{{ recommendTarget ? `“${recommendTarget}”的推荐课件` : query ? `“${query}”的课件` : '推荐课件' }}</h2><p>{{ recommendTarget ? '按批阅中的知识点和章节匹配，最多 3 个' : '已按当前教师学段筛选，优先匹配知识点和章节' }}<el-button v-if="recommendTarget" link type="primary" class="show-all" @click="showAllCourseware">查看全部课件</el-button></p></div><span>{{ filteredCards.length }} 个</span></section>
    <section v-if="filteredCards.length" class="courseware-grid ma-stagger">
      <article v-for="card in filteredCards" :key="card.id" class="course-card">
        <CoursewareCover :card="card" />
        <div class="card-body">
          <div class="card-title-row"><h3>{{ card.title }}</h3><el-tag size="small" type="success">{{ card.level }}</el-tag></div>
          <p class="card-summary">{{ card.objective }}</p>
          <p v-if="card.recommendationReason" class="match-reason">{{ card.recommendationReason }}</p>
          <div class="tag-list"><el-tag v-for="topic in card.topics" :key="topic" size="small" effect="plain">{{ topic }}</el-tag></div>
          <div class="card-actions">
            <el-button type="primary" @click="openPreview(card)">预览演示</el-button>
            <el-button plain :loading="downloading === card.id" @click="downloadCourseware(card)">下载离线课件</el-button>
          </div>
        </div>
      </article>
    </section>
    <el-empty v-else :description="recommendTarget ? '暂无与该知识点匹配的课件' : '暂无匹配课件'"><el-button v-if="recommendTarget" type="primary" plain @click="showAllCourseware">查看全部课件</el-button></el-empty>
    <el-dialog v-model="previewVisible" :title="previewCard?.title || '课件预览'" width="900px" class="preview-dialog">
      <template v-if="previewCard">
        <div class="preview-context">
          <span>{{ previewCard.chapter }} · {{ previewCard.knowledgePoint }}</span>
          <strong>目标：{{ previewCard.objective }}</strong>
        </div>
        <div v-if="previewLoading" class="preview-loading">正在加载课件…</div>
        <iframe
          v-else-if="previewHtml"
          :key="previewCard.id"
          :srcdoc="previewHtml"
          class="courseware-frame"
          :title="`${previewCard.title}交互预览`"
          sandbox="allow-scripts"
        />
        <div v-else class="preview-loading">课件暂时无法加载，请直接下载 HTML 文件。</div>
      </template>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
        <el-button type="primary" @click="downloadCourseware(previewCard)">下载离线课件</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import api from '@/api'
import { COURSEWARE_MODELS, modelForCard } from '@/courseware/models'
import CoursewareCover from '@/components/CoursewareCover.vue'

const auth = useAuthStore(); const route = useRoute(); const router = useRouter(); const stage = computed(() => auth.user?.education_stage || 'primary'); const stageLabel = computed(() => ({ primary: '小学数学', junior: '初中数学', senior: '高中数学' }[stage.value] || '当前学段'));  const recommendTarget = ref(String(route.query.knowledge_point || route.query.chapter || '')); const query = ref(''); const previewVisible = ref(false); const previewCard = ref(null); const previewHtml = ref(''); const previewLoading = ref(false); const downloading = ref('')
const models = COURSEWARE_MODELS
const stageDirectoryByValue = { primary: '小学', junior: '初中', senior: '高中' }
const toneByModel = { fractions: 'mint', area: 'mint', statistics: 'mint', views3d: 'teal', linear: 'purple', quadratic: 'amber', coordinateTransforms: 'purple', unitCircle: 'purple', vectors: 'coral', derivative: 'purple', solidGeometry: 'teal', spaceRelations: 'slate' }
const demoCards = Object.values(models).map((m, i) => ({ ...m, id: `model-${i}`, modelType: m.key, title: `${m.chapter}：${m.knowledgePoint}`, shortTitle: m.knowledgePoint, coverLabel: m.knowledgePoint, coverTopics: [m.knowledgePoint], topics: [m.knowledgePoint], tone: toneByModel[m.key] || 'blue', level: m.stage === 'primary' ? '小学' : m.stage === 'junior' ? '初中' : '高中' }))
const coursewareCards = ref(demoCards)
const chapterShortcuts = computed(() => [...new Set(coursewareCards.value.filter(c => c.stage === stage.value).map(c => c.chapter))])
const filteredCards = computed(() => { const k = query.value.trim().toLowerCase(); return coursewareCards.value.filter(c => c.stage === stage.value && (!k || [c.chapter, c.title, c.knowledgePoint, ...c.topics].some(t => t.toLowerCase().includes(k)))) })
function sampleUrl(card, fileName) { const folder = stageDirectoryByValue[card?.stage] || '未分类'; return `/courseware/samples/${encodeURIComponent(folder)}/${encodeURIComponent(fileName)}` }
function normaliseTextList(values) { return Array.isArray(values) ? values.map(value => typeof value === 'string' ? value : value?.title || value?.label || value?.detail || '').filter(Boolean) : [] }
let previewRequest = 0
async function openPreview(card) {
  if (!card) return
  const request = ++previewRequest
  previewCard.value = card
  previewVisible.value = true
  previewHtml.value = ''
  previewLoading.value = true
  try {
    let htmlText
    if (card.backendId) {
      htmlText = await api.courseware.preview(card.backendId)
    } else {
      const fileName = card.offlineFileName || `${card.chapter || card.knowledgePoint || card.title}.html`
      const response = await fetch(sampleUrl(card, fileName), { cache: 'no-store' })
      if (!response.ok) throw new Error('课件文件不存在')
      htmlText = await response.text()
    }
    if (request === previewRequest) previewHtml.value = htmlText
  } catch (e) {
    if (request === previewRequest) ElMessage.error(`预览加载失败：${e.message}`)
  } finally {
    if (request === previewRequest) previewLoading.value = false
  }
}
async function loadCourseware() {
  try {
    // Recommended cards are already matched server-side (by knowledge point or
    // chapter); never re-filter them by the raw knowledge-point text here.
    const recommended = Boolean(recommendTarget.value)
    const params = recommended ? { chapter: route.query.chapter, knowledge_point: route.query.knowledge_point, limit: 3 } : { limit: 100 }
    const data = recommended ? await api.courseware.recommendations(params) : await api.courseware.list(params)
    if (recommended && data?.matched === false) { coursewareCards.value = []; return }
    if (Array.isArray(data?.items)) coursewareCards.value = data.items.map(item => {
      const model = modelForCard(item)
      const hasSpecificModel = model && model.chapter === (item.chapter || '')
      const coverTopics = Array.isArray(item.cover_topics) && item.cover_topics.length ? item.cover_topics : [item.knowledgePoint || item.knowledge_point || item.chapter || '数学课件']
      const coverLabel = item.cover_label || coverTopics.slice(0, 3).join(' · ')
      return { ...model, ...item, modelType: hasSpecificModel ? model.key : 'catalog', coverLabel, coverTopics, id: `api-${item.id}`, backendId: item.id,
        title: item.title || `${item.chapter}：${item.knowledgePoint || item.knowledge_point || ''}`,
        shortTitle: coverLabel.slice(0, 14),
        knowledgePoint: item.knowledgePoint || item.knowledge_point || model.knowledgePoint,
        topics: coverTopics, objective: item.objective || model.objective,
        steps: normaliseTextList(item.steps).length ? normaliseTextList(item.steps) : model.steps,
        checkpoints: normaliseTextList(item.checkpoints).length ? normaliseTextList(item.checkpoints) : model.checkpoints,
        interactionState: item.interactionState || model.interactionState, offlineFileName: item.file_name || item.offlineFileName || model.offlineFileName,
        recommendationReason: item.recommendation_reason || item.recommendationReason || '' }
    })
  } catch {
    // A recommendation route must never fall back to unrelated bundled cards.
    // Keep the bundled models only for the ordinary catalogue when the API is offline.
    if (recommended) coursewareCards.value = []
  }
}
async function showAllCourseware() {
  recommendTarget.value = ''
  query.value = ''
  coursewareCards.value = demoCards
  await router.replace({ path: '/courseware' })
  await loadCourseware()
}
async function downloadCourseware(card) {
  if (!card) return
  downloading.value = card.id
  try {
    if (card.backendId) {
      await api.courseware.download(card.backendId, card.offlineFileName)
    } else {
      // Demo cards use the same bundled HTML that is served from public/courseware/samples.
      // This keeps the downloaded file and the preview on the same interaction model.
      const fileName = card.offlineFileName || `${card.chapter || card.knowledgePoint || card.title}.html`
      const response = await fetch(sampleUrl(card, fileName), { cache: 'no-store' })
      if (!response.ok) throw new Error('课件文件不存在')
      const blob = await response.blob()
      const a = document.createElement('a')
      const objectUrl = URL.createObjectURL(blob)
      a.href = objectUrl
      a.download = fileName
      a.style.display = 'none'
      document.body.appendChild(a)
      a.click()
      a.remove()
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
    }
    ElMessage.success('已下载离线课件')
  } catch (e) {
    ElMessage.error(`下载失败：${e.message}`)
  } finally {
    downloading.value = ''
  }
}
onMounted(loadCourseware)
</script>

<style scoped>
.courseware-page{gap:16px;padding-bottom:32px}.courseware-hero{padding:30px 34px;background:var(--paper-deep);border:0;border-radius:var(--radius-xl);animation:ma-fade-in var(--dur-4) var(--ease-out) backwards}.courseware-hero h1{font-family:var(--font-serif);font-weight:500}.eyebrow{margin:0 0 6px;color:var(--primary-dark);font-size:var(--fs-xs);font-weight:var(--fw-semibold);letter-spacing:.04em}.courseware-hero h1{margin:0;font-size:34px;letter-spacing:-.01em;line-height:var(--lh-tight)}.hero-copy{max-width:720px;margin:8px 0 0;color:var(--ink-muted);font-size:var(--fs-md)}.toolbar{display:flex;gap:12px;margin:0}.search-input{max-width:520px}.topic-strip{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.topic-label{margin-right:4px;color:var(--ink-muted);font-size:var(--fs-xs);font-weight:var(--fw-semibold)}.topic-strip .el-button+.el-button{margin-left:0}.recommend-heading{display:flex;align-items:flex-end;justify-content:space-between;margin:6px 0 0}.recommend-heading h2{margin:0;font-size:var(--fs-xl);font-weight:var(--fw-semibold)}.recommend-heading p{margin:4px 0 0;color:var(--ink-muted);font-size:var(--fs-xs)}.recommend-heading .show-all{margin-left:8px;font-size:var(--fs-xs)}.recommend-heading>span{color:var(--ink-muted);font-size:var(--fs-sm);font-weight:var(--fw-semibold);font-variant-numeric:tabular-nums}.courseware-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.course-card{display:flex;flex-direction:column;background:var(--card);border:1px solid var(--line);border-radius:var(--radius-lg);overflow:hidden;box-shadow:var(--shadow-xs);transition:box-shadow var(--dur-3) var(--ease-out),border-color var(--dur-3) var(--ease-out)}.course-card:hover{border-color:color-mix(in srgb,var(--primary) 26%,var(--line));box-shadow:var(--shadow-md)}.card-visual svg,.catalog-cover{transition:transform var(--dur-4) var(--ease-emphasized)}.course-card:hover .card-visual svg,.course-card:hover .catalog-cover{transform:scale(1.03)}.card-visual{height:142px;padding:13px 15px;color:#184537;display:flex;align-items:center;justify-content:space-between;gap:8px}.card-visual svg{width:58%;height:104px;flex:none;border:0;background:transparent;box-shadow:none}.visual-copy{min-width:0;max-width:41%;align-self:flex-end;padding-bottom:5px}.visual-copy span{display:block;font-size:11px;line-height:1.35;opacity:.82}.visual-copy strong{display:block;margin-top:8px;font-size:18px;line-height:1.25}.visual-mark{font-size:11px;fill:#536b7d}.visual-mint{background:#dff4e8}.visual-purple{background:#ece5fa}.visual-amber{background:#f8eddc}.visual-coral{background:#fbe7e4}.visual-teal{background:#dff0f0}.visual-slate{background:#e7edf3}.visual-blue{background:#ddeefa}.card-body{display:flex;flex:1;flex-direction:column;padding:16px}.card-actions{margin-top:auto}.card-title-row{display:flex;justify-content:space-between;gap:8px}.card-title-row h3{margin:0;font-size:16px}.card-summary{color:var(--ink-muted);font-size:13px;min-height:46px}.tag-list{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:14px}.card-actions{display:flex;gap:8px}.card-actions .el-button{flex:1}.lesson-head{display:flex;justify-content:space-between;gap:15px}.lesson-head h3{margin:8px 0}.direct-tip{margin:8px 0;color:var(--ink-muted);font-size:12px}.drag-point{cursor:grab;touch-action:none}.circle-svg{touch-action:none;cursor:crosshair}.linear-svg{touch-action:none}.linear-svg path{pointer-events:none}.intercept-handle{cursor:ns-resize;touch-action:none}.operation-tip{padding:12px;background:var(--surface-subtle);border-left:4px solid var(--primary);border-radius:8px}.lesson-meta{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0}.lesson-meta span{padding:10px 12px;background:#f8fafc;border:1px solid #e1e8ee;border-radius:8px;color:var(--ink-muted);font-size:12px}.lesson-meta b{display:block;color:var(--ink);font-size:11px;margin-bottom:3px}.linear-svg{background-image:linear-gradient(#e8eff4 1px,transparent 1px),linear-gradient(90deg,#e8eff4 1px,transparent 1px);background-size:30px 30px;box-shadow:0 8px 22px #23405b16}.circle-svg{background-image:radial-gradient(circle at 50% 50%,#fff 0,#f5faff 75%)}.fraction-svg{filter:drop-shadow(0 6px 12px #23405b12)}.model-panel{padding:12px;background:#fbfdfc;border:1px solid #dbece3;border-radius:12px}.fraction-controls,.param-controls{display:grid;gap:8px}.fraction-controls label,.param-controls label{display:flex;align-items:center;gap:8px}.fraction-controls input,.param-controls input{flex:1}.fraction-svg,.linear-svg{width:100%;max-height:360px}.anno{font-size:12px;fill:#467b68}.tick,.axis-tick{font-size:10px;fill:#789}.axis-label{font-size:13px;fill:#536b7d;font-weight:600}.point-coord{font-size:12px;fill:#b34}.projection-cos{font-size:12px;fill:#2878c8}.projection-sin{font-size:12px;fill:#b34}.match-reason{font-size:12px;color:var(--primary-dark);margin:4px 0}.formula{font-size:14px;fill:#315f8c}.formula-row{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:10px}.formula-row span{font-size:18px;font-weight:600}.lesson-footer{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:14px;font-size:13px;color:var(--ink-muted)}.answer-text{flex-basis:100%;padding:10px 12px;border-radius:8px;background:#edf8f2;color:#27654d}@media(max-width:700px){.toolbar{flex-direction:column}.search-input{width:100%;max-width:none}.courseware-grid{grid-template-columns:1fr}.card-visual{height:128px}.formula-row{flex-direction:column;align-items:flex-start}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
.preview-context{display:grid;gap:6px;padding:12px 14px;margin-bottom:12px;background:var(--surface-subtle);border-left:4px solid var(--primary);border-radius:8px;color:var(--ink-muted);font-size:13px}.preview-context strong{color:var(--ink);font-size:15px}.courseware-frame{display:block;width:100%;height:720px;border:1px solid #dbe6ed;border-radius:14px;background:#f8fbfd}.preview-loading{min-height:260px;display:grid;place-items:center;color:var(--ink-muted)}@media(max-width:700px){.courseware-frame{height:660px}}
.stage-context{display:flex;align-items:center;gap:8px;min-width:180px;padding:0 13px;border:1px solid color-mix(in srgb,var(--primary) 22%,var(--line));border-radius:var(--radius-sm);background:var(--primary-soft);color:var(--primary-dark);font-weight:var(--fw-semibold)}.stage-context small{font-size:11px;font-weight:500;color:var(--ink-muted)}.catalog-cover{width:100%;min-height:112px;padding:17px 18px;display:flex;flex-direction:column;justify-content:center;border:1px solid #b7d9cb;border-radius:13px;background:linear-gradient(135deg,#effaf5,#e3f1f8);color:#1f5d4c}.catalog-cover span{font-size:19px;font-weight:750;line-height:1.35}.catalog-cover small{margin-top:8px;color:#47766c;font-size:12px;line-height:1.45}@media(max-width:700px){.stage-context{width:100%;max-width:none;min-height:42px}}
</style>
