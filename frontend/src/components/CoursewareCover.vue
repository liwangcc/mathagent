<template>
  <figure ref="host" class="courseware-cover" :data-cover-code="cover?.code || 'live'">
    <img
      v-if="cover && !imageFailed"
      :src="cover.src"
      :alt="`${card.chapter}：${cover.sceneTitle}教学图`"
      :width="cover.width"
      :height="cover.height"
      loading="lazy"
      decoding="async"
      @error="imageFailed = true; loadLiveCover()"
    />
    <div v-else class="live-cover" :aria-busy="loading">
      <iframe v-if="liveHtml" :srcdoc="liveHtml" sandbox="allow-scripts" tabindex="-1" :title="`${card.title}图形封面`" />
      <button v-else-if="failed" type="button" class="cover-retry" @click="loadLiveCover">重新加载课件封面</button>
      <span v-else class="cover-loading">正在提取课件教学图…</span>
    </div>
    <figcaption>{{ cover?.sceneTitle || card.coverLabel || card.knowledgePoint || card.chapter }}<span>课件实景</span></figcaption>
  </figure>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import api from '@/api'
import coverManifest from '@/courseware/covers.json'
import { coursewareCoverForCard, coursewareThumbnailHtml } from '@/courseware/covers'

const props = defineProps({ card: { type: Object, required: true } })
const cover = computed(() => coursewareCoverForCard(props.card, coverManifest))
const host = ref(null)
const imageFailed = ref(false)
const liveHtml = ref('')
const loading = ref(false)
const failed = ref(false)
let observer
let request = 0
let nearViewport = false

async function loadLiveCover() {
  if (!nearViewport || loading.value || (cover.value && !imageFailed.value)) return
  const revision = ++request
  const card = props.card
  loading.value = true
  failed.value = false
  try {
    let html
    if (card.backendId) {
      html = await api.courseware.preview(card.backendId)
    } else {
      const stage = { primary: '小学', junior: '初中', senior: '高中' }[card.stage]
      const file = card.file_name || card.offline_file_name || card.offlineFileName
      if (!stage || !file) throw new Error('课件来源不完整')
      const response = await fetch(`/courseware/samples/${encodeURIComponent(stage)}/${encodeURIComponent(file)}`)
      if (!response.ok) throw new Error('课件不可用')
      html = await response.text()
    }
    if (revision === request) liveHtml.value = coursewareThumbnailHtml(html)
  } catch {
    if (revision === request) failed.value = true
  } finally {
    if (revision === request) loading.value = false
  }
}

watch(() => props.card, () => {
  request += 1
  imageFailed.value = false
  liveHtml.value = ''
  loading.value = false
  failed.value = false
  loadLiveCover()
})
onMounted(() => {
  if (typeof IntersectionObserver === 'undefined') {
    nearViewport = true
    loadLiveCover()
    return
  }
  observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) {
      nearViewport = true
      loadLiveCover()
      observer.disconnect()
    }
  }, { rootMargin: '180px' })
  observer.observe(host.value)
})
onBeforeUnmount(() => {
  request += 1
  observer?.disconnect()
})
</script>

<style scoped>
.courseware-cover{margin:0;background:#f6f8f4;color:#29473b;overflow:hidden;border-bottom:1px solid var(--line)}
.courseware-cover>img{display:block;width:100%;height:auto;aspect-ratio:16/9;object-fit:contain;transition:transform var(--dur-4) var(--ease-out)}
.courseware-cover:hover>img{transform:scale(1.025)}
figcaption{display:flex;align-items:center;justify-content:space-between;gap:8px;min-height:34px;padding:6px 14px;border-top:1px solid #dce6dd;font-size:12px;line-height:1.4}
figcaption>span{flex:none;font-size:10px;color:#597362}
.live-cover{position:relative;width:100%;aspect-ratio:16/9;overflow:hidden;container-type:inline-size}
.live-cover iframe{display:block;width:800px;height:450px;border:0;transform:scale(calc(100cqw / 800px));transform-origin:top left;pointer-events:none}
.cover-loading,.cover-retry{position:absolute;inset:0;display:grid;place-items:center;font-size:12px;color:#597362;background:transparent;border:0}
.cover-retry{width:100%;cursor:pointer;text-decoration:underline}
.cover-retry:focus-visible{outline:2px solid var(--primary);outline-offset:-4px}
@media(prefers-reduced-motion:reduce){.courseware-cover>img{transition:none}}
</style>
