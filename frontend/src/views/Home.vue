<template>
  <div class="home-page page-shell">
    <section class="hero-band home-hero">
      <svg class="hero-plot home-plot" viewBox="0 0 420 260" preserveAspectRatio="xMaxYMid meet" aria-hidden="true">
        <rect class="wash" x="232" y="38" width="120" height="120" rx="6" />
        <line class="axis" x1="40" y1="214" x2="410" y2="214" />
        <line class="axis" x1="72" y1="20" x2="72" y2="240" />
        <path class="curve is-faint" pathLength="1" d="M40 110 C 120 110, 170 200, 240 186 S 360 70, 410 88" />
        <path class="curve" pathLength="1" d="M52 226 C 120 222, 170 170, 232 134 S 350 48, 404 34" />
        <circle class="dot" cx="232" cy="134" r="5.5" />
        <circle class="dot is-hollow" cx="336" cy="66" r="5.5" />
        <circle class="dot" cx="404" cy="34" r="4" />
      </svg>
      <div class="home-hero__main">
        <p class="hero-band__eyebrow">{{ todayLabel }} · MathAgent 教学工作台</p>
        <h1 class="hero-band__title">{{ greeting }}，{{ auth.user?.name || '老师' }}</h1>
        <p class="hero-band__lead">
          当前工作台连接已配置的批改、题库与报告服务。
          <span v-if="auth.user?.school" class="welcome-school">{{ auth.user.school }}</span>
        </p>
        <div class="home-hero__actions">
          <el-button class="is-solid" size="large" @click="openQuickItem(primaryQuickItems[0])">
            开始批改<el-icon class="el-icon--right"><ArrowRight /></el-icon>
          </el-button>
          <el-button class="is-outline" size="large" @click="openQuickItem(primaryQuickItems[1])">打开班级</el-button>
        </div>
      </div>
      <div class="hero-metrics home-hero__metrics">
        <div class="hero-metric is-accent">
          <span class="hero-metric__value">{{ currentClassSize }}<small>人</small></span>
          <span class="hero-metric__label">{{ auth.currentClassName }} · 学生</span>
        </div>
        <div class="hero-metric">
          <span class="hero-metric__value">{{ auth.classes.length }}<small>个</small></span>
          <span class="hero-metric__label">我的班级</span>
        </div>
        <div class="hero-metric">
          <span class="hero-metric__value">{{ recentFiles.length }}<small>份</small></span>
          <span class="hero-metric__label">最近生成文件</span>
        </div>
      </div>
    </section>

    <div class="home-bento">
      <div class="home-main">
        <section class="home-section">
          <div class="section-heading">
            <div>
              <h2>快速开始</h2>
              <p>进入常用功能，所有入口均连接现有页面。</p>
            </div>
            <HelpButton
              title="快速开始"
              content="先选择或创建班级，再上传模板和学生试卷进行批阅；批阅完成后可查看错题、练习、报告和导出文件。"
            />
          </div>
          <div class="task-grid ma-stagger">
            <button
              v-for="(item, index) in primaryQuickItems"
              :key="item.title"
              type="button"
              class="task-tile"
              :class="{ 'is-lead': index === 0 }"
              @click="openQuickItem(item)"
            >
              <span class="index-numeral">{{ String(index + 1).padStart(2, '0') }}</span>
              <span class="task-tile__icon"><el-icon><component :is="item.icon" /></el-icon></span>
              <span class="task-tile__copy">
                <strong>{{ item.title }}</strong>
                <small>{{ item.desc }}</small>
              </span>
              <span v-if="index === 0" class="task-tile__flow" aria-hidden="true">
                <i>答案依据</i><i>学生试卷</i><i>识别确认</i><i>批改审阅</i>
              </span>
              <span class="task-tile__go" aria-hidden="true"><el-icon><ArrowRight /></el-icon></span>
            </button>
          </div>
        </section>

        <section class="home-section command-card">
          <div class="section-heading">
            <div>
              <h2>更多入口</h2>
              <p>题库、课件、统计与公告</p>
            </div>
          </div>
          <div class="command-list ma-stagger">
            <button
              v-for="item in secondaryQuickItems"
              :key="item.title"
              type="button"
              class="command-row"
              @click="openQuickItem(item)"
            >
              <span class="command-row__icon"><el-icon><component :is="item.icon" /></el-icon></span>
              <span class="command-row__copy">
                <strong>
                  {{ item.title }}
                  <span v-if="item.action === 'notice' && newNoticeCount" class="command-row__badge">{{ newNoticeCount }} 条新公告</span>
                </strong>
                <small>{{ item.desc }}</small>
              </span>
              <el-icon class="command-row__arrow"><ArrowRight /></el-icon>
            </button>
          </div>
        </section>
      </div>

      <aside class="home-rail">
        <section class="home-section chat-block">
          <div class="section-heading">
            <div>
              <h2>小M 助手</h2>
              <p>查询、生成与导出会继续使用已接入的真实服务。</p>
            </div>
            <HelpButton
              title="小M 助手"
              content="在这里用自然语言查询班级、学生和批阅数据，也可以请求生成练习或报告。涉及学生资料的操作仍以当前班级权限为准。"
            />
          </div>
          <ChatBar variant="home" />
        </section>

        <section v-if="recentFiles.length" class="home-section recent-section">
          <div class="section-heading">
            <div>
              <h2>最近生成的文件</h2>
              <p>文件来自当前账号的真实生成记录。</p>
            </div>
            <HelpButton
              title="最近生成的文件"
              content="这里只显示当前账号近期生成且仍可访问的文件。下载后请按学校的隐私要求保存和分享。"
            />
          </div>
          <TransitionGroup name="list" tag="div" class="file-list">
            <div v-for="file in recentFiles" :key="file.id" class="file-row">
              <span class="file-icon"><el-icon><Document /></el-icon></span>
              <span class="file-name" :title="file.name">{{ file.name }}</span>
              <el-button type="primary" link :icon="Download" @click="downloadFile(file.download_url, file.name)">
                下载
              </el-button>
            </div>
          </TransitionGroup>
        </section>
      </aside>
    </div>

    <el-dialog
      v-model="showDisclaimer"
      title="用户协议与免责声明"
      width="600px"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
    >
      <div class="dialog-copy">
        <section>
          <h3>使用须知</h3>
          <p>本系统仅供教学辅助使用，AI 批改结果仅供参考，不替代人工判断。</p>
        </section>
        <section>
          <h3>责任声明</h3>
          <ul>
            <li>用户对上传的试卷内容负责。</li>
            <li>AI 批改结果可能存在误差，请以人工审核为准。</li>
            <li>系统不对批改结果的准确性承担法律责任。</li>
          </ul>
        </section>
        <section>
          <h3>隐私保护</h3>
          <p>上传的试卷仅用于批改处理，不会用于其他用途。</p>
        </section>
      </div>
      <template #footer>
        <el-button type="primary" size="large" :icon="CircleCheck" @click="agreeDisclaimer">
          我已阅读并同意
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="showNotice" title="系统公告" width="580px" @closed="acknowledgeNotices">
      <div class="notice-list">
        <article
          v-for="item in noticeItems"
          :key="noticeKey(item)"
          class="notice-item"
          :class="{ 'is-new': isNewNotice(item) }"
        >
          <div class="notice-meta">
            <span class="notice-meta__tags">
              <el-tag size="small" :type="item.type === 'feature' ? 'success' : 'warning'">{{ item.tag }}</el-tag>
              <el-tag v-if="isNewNotice(item)" size="small" type="danger" effect="dark">新</el-tag>
            </span>
            <time>{{ item.date }}</time>
          </div>
          <h3>{{ item.title }}</h3>
          <p>{{ item.content }}</p>
        </article>
        <a class="document-download" :href="manualDocUrl" download="MathAgent_产品说明书与公告文本.docx">
          <span><strong>查看完整公告与使用规范</strong><small>下载 Word 详细版，含拍照规范、流程耗时、异常处置与产品理念</small></span>
          <el-icon><Download /></el-icon>
        </a>
      </div>
    </el-dialog>

  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowRight,
  Bell,
  CircleCheck,
  Collection,
  DataAnalysis,
  Document,
  Download,
  Files,
  Help,
  UploadFilled,
  UserFilled,
  Reading,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import ChatBar from '@/components/ChatBar.vue'
import HelpButton from '@/components/HelpButton.vue'
import api from '@/api'
import {
  loadSeenNotices,
  noticeKey,
  readFlag,
  saveSeenNotices,
  shouldAutoOpenNotice,
  unseenNotices,
  usageGuideStorageKey,
} from '@/utils/systemNotice'
import manualDocUrl from '@/assets/docs/MathAgent_产品说明书与公告文本.docx?url'

const auth = useAuthStore()
const router = useRouter()
const recentFiles = ref([])
const showDisclaimer = ref(false)
const showNotice = ref(false)

const now = new Date()
const hour = now.getHours()
const greeting = hour < 12 ? '早上好' : hour < 18 ? '下午好' : '晚上好'
const todayLabel = now.toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' })
const noticeItems = [
  {
    tag: '服务维护',
    type: 'warning',
    date: '2026-10-28',
    title: '管理员暂时无力维护',
    content: '管理员目前在外找工作，暂时既没有时间也没有钱维护本系统。',
  },
  {
    tag: '使用必读',
    type: 'warning',
    date: '2026-07-30',
    title: '先确认班级，每一页都填写姓名和学号',
    content: '系统逐页独立核验学生身份，不会让后一页自动继承前一页。姓名和学号请清楚写在页面顶部统一栏。',
  },
  {
    tag: '拍照规范',
    type: 'warning',
    date: '2026-07-30',
    title: '页面完整、文字正立、光线均匀',
    content: '镜头尽量正对纸面，倾斜控制在约 5° 以内。四角、题号和答题区不要缺失；横倒、模糊、反光或明显斜拍请重拍。',
  },
  {
    tag: '处理时间',
    type: 'feature',
    date: '2026-07-30',
    title: '100 页以内，系统流程通常 20 分钟内完成',
    content: '主要时间通常花在教师拍照、检查和上传。系统完成后仍需教师核对参考答案、待复核题和最终归档。',
  },
  {
    tag: '服务维护',
    type: 'warning',
    date: '2026-08-21',
    title: '批改服务连续超过 24 小时不可用',
    content: '阿里云和小米的模型接口欠费后会自动停用。若批改服务连续超过 24 小时不可用，大概率是管理员暂时没钱续费维护，请等待恢复。',
  },
]

// Notices this account has already closed, kept in the browser. Anything
// newer pops up once on entry and stays marked 新 until the dialog is closed.
const seenNoticeKeys = ref(new Set())
const newNotices = computed(() => unseenNotices(noticeItems, seenNoticeKeys.value))
const newNoticeCount = computed(() => newNotices.value.length)

function isNewNotice(item) {
  return !seenNoticeKeys.value.has(noticeKey(item))
}

function acknowledgeNotices() {
  seenNoticeKeys.value = saveSeenNotices(auth.user, noticeItems)
}

const quickItems = [
  { path: '/upload', title: '详细批改', desc: '上传试卷并完成识别、确认与批改', icon: UploadFilled },
  { path: '/students', title: '班级管理', desc: '管理学生名单与成长数据', icon: UserFilled },
  { path: '/reports', title: '成绩报告', desc: '生成班级或学生报告', icon: Document },
  { path: '/bank', title: '题库管理', desc: '维护真实题目与答案库', icon: Collection },
  { path: '/courseware', title: '课件库', desc: '按章节查看可互动课件', icon: Reading },
  { path: '/stats', title: '统计分析', desc: '查看学生错题分布与趋势', icon: DataAnalysis },
  { action: 'notice', title: '系统公告', desc: '查看功能更新与流程变化', icon: Bell },
  { action: 'guide', title: '使用引导', desc: '高亮带你找到每一步该点的位置', icon: Help },
  { path: '/pending-answers', title: '待入库', desc: '确认 AI 发现的新题答案', icon: Files },
]
// The three daily tasks lead; the remaining entries sit in a compact tier.
const primaryQuickItems = quickItems.slice(0, 3)
// Hero metric: size of the class currently selected in the header (already loaded by the shell).
const currentClassSize = computed(() => auth.classes.find((cls) => cls.id === auth.currentClassId)?.student_count ?? '—')
const secondaryQuickItems = quickItems.slice(3)

async function loadRecentFiles() {
  try {
    const data = await api.file.recent()
    recentFiles.value = data.items || []
  } catch {
    recentFiles.value = []
  }
}

async function downloadFile(url, name) {
  try {
    await api.file.downloadUrl(url, name)
  } catch (error) {
    ElMessage.error(`下载失败：${error.message}`)
  }
}

function openQuickItem(item) {
  if (item.path) {
    router.push(item.path)
  } else if (item.action === 'notice') {
    showNotice.value = true
  } else if (item.action === 'guide') {
    router.push({ path: '/', query: { guide: '1' } })
  }
}

function agreeDisclaimer() {
  localStorage.setItem('disclaimer_agreed', 'true')
  showDisclaimer.value = false
  router.replace({ path: '/', query: { guide: '1' } })
}

onMounted(() => {
  loadRecentFiles()
  showDisclaimer.value = !localStorage.getItem('disclaimer_agreed')
  seenNoticeKeys.value = loadSeenNotices(auth.user)
  // The disclaimer and the first-run tour take precedence; a teacher who still
  // has those pending sees the notice the next time the home page opens.
  showNotice.value = shouldAutoOpenNotice({
    unseenCount: newNoticeCount.value,
    disclaimerAgreed: !showDisclaimer.value,
    usageGuideDone: readFlag(usageGuideStorageKey(auth.user)),
  })
})
</script>

<style scoped>
.home-page {
  gap: 26px;
}

/* ── Masthead: serif greeting and actions on paper, a ruled ledger of class figures ── */
.home-hero {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
  grid-template-rows: auto auto;
  align-items: end;
  gap: 30px 48px;
  padding: 40px 44px 34px;
  animation: ma-fade-in var(--dur-4) var(--ease-out) backwards;
}

.home-plot {
  top: 18px;
  right: 24px;
  width: 44%;
  height: 58%;
}

.home-hero__main {
  min-width: 0;
  grid-row: 1;
}

.home-hero__main .hero-band__title {
  font-size: clamp(36px, 3.6vw, 54px);
}

.welcome-school {
  display: inline-block;
  margin-left: 8px;
  padding-left: 10px;
  border-left: 1px solid var(--rule);
  color: var(--ink);
  font-size: var(--fs-md);
}

.home-hero__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 26px;
}

.home-hero__actions .el-button + .el-button {
  margin-left: 0;
}

.home-hero__metrics {
  grid-column: 1 / -1;
  grid-row: 2;
  padding-top: 24px;
  border-top: 1px solid var(--rule);
}

/* ── Bento workbench ── */
.home-bento {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(320px, 1fr);
  gap: 24px;
  align-items: start;
}

.home-main,
.home-rail {
  display: flex;
  flex-direction: column;
  gap: 24px;
  min-width: 0;
}

.home-section {
  min-width: 0;
}

.section-heading {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--sp-3);
  margin-bottom: 14px;
}

.section-heading h2 {
  margin: 0;
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 22px;
  font-weight: 500;
  letter-spacing: -0.005em;
}

.section-heading p {
  margin: 4px 0 0;
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

.task-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr);
  grid-template-rows: repeat(2, minmax(118px, auto));
  gap: 14px;
}

.task-tile {
  position: relative;
  appearance: none;
  min-width: 0;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  grid-template-rows: auto 1fr;
  align-items: start;
  gap: 10px 14px;
  padding: 18px 20px;
  overflow: hidden;
  color: var(--ink);
  font: inherit;
  text-align: left;
  background: var(--paper-raised);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  box-shadow: none;
  cursor: pointer;
  transition: transform var(--dur-1) var(--ease-out), box-shadow var(--dur-3) var(--ease-out), border-color var(--dur-3) var(--ease-out);
}

.task-tile .index-numeral {
  grid-column: 1 / -1;
}

.task-tile:hover {
  border-color: var(--ink);
  box-shadow: none;
}

.task-tile:active {
  transform: scale(0.985);
}

.task-tile:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

/* The daily task leads: tall ink-edged tile with a paper graph texture. */
.task-tile.is-lead {
  grid-row: 1 / span 2;
  align-content: space-between;
  padding: 26px;
  background: var(--paper-deep);
  border-color: transparent;
  box-shadow: none;
}

.task-tile.is-lead .index-numeral {
  font-size: 72px;
}

.task-tile.is-lead .task-tile__copy strong {
  font-family: var(--font-serif);
  font-size: 28px;
  font-weight: 500;
}

.task-tile__icon {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  border-radius: 10px;
  background: var(--moss-soft);
  color: var(--moss-deep);
  font-size: 21px;
  transition: background-color var(--dur-2) var(--ease-out), color var(--dur-2) var(--ease-out);
}

.task-tile.is-lead .task-tile__icon {
  background: var(--moss);
  color: var(--on-ink);
}

.task-tile:hover .task-tile__icon {
  background: var(--moss);
  color: var(--on-ink);
}

.task-tile__copy {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  padding-right: 34px;
}

.task-tile__copy strong {
  font-family: var(--font-serif);
  font-size: 20px;
  font-weight: 500;
}

.task-tile__copy small {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
  line-height: var(--lh-snug);
}

.task-tile__flow {
  grid-column: 1 / -1;
  align-self: end;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding-right: 44px;
}

.task-tile__flow i {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: var(--radius-pill);
  background: var(--paper-raised);
  box-shadow: 0 0 0 1px var(--line);
  color: var(--ink-soft);
  font-size: var(--fs-xs);
  font-style: normal;
  font-weight: var(--fw-semibold);
}

.task-tile__flow i + i::before {
  content: '';
  width: 5px;
  height: 5px;
  margin-left: -2px;
  border-radius: 50%;
  background: var(--moss);
}

.task-tile__go {
  position: absolute;
  right: 18px;
  bottom: 18px;
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: color-mix(in srgb, var(--ink) 6%, transparent);
  color: var(--ink-soft);
  transition: background-color var(--dur-2) var(--ease-out), color var(--dur-2) var(--ease-out), transform var(--dur-3) var(--ease-emphasized);
}

.task-tile:hover .task-tile__go {
  background: var(--ink-solid);
  color: var(--on-ink);
  transform: translateX(3px);
}

.command-card,
.chat-block,
.recent-section {
  padding: 22px 24px 22px;
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  background: var(--paper-raised);
}

.command-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 16px;
  margin: 0 -8px;
}

.command-row {
  appearance: none;
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  min-width: 0;
  padding: 9px 10px;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--ink);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background-color var(--dur-2) var(--ease-out);
}

.command-row:hover,
.command-row:focus-visible {
  outline: none;
  background: var(--fill);
}

.command-row__icon {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--ink) 5%, transparent);
  color: var(--ink-soft);
  font-size: 16px;
}

.command-row:hover .command-row__icon {
  background: var(--moss-soft);
  color: var(--moss-deep);
}

.command-row__copy {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.command-row__copy strong {
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
}

.command-row__copy small {
  overflow: hidden;
  color: var(--ink-muted);
  font-size: var(--fs-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.command-row__badge {
  display: inline-block;
  margin-left: 8px;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--danger);
  color: #fff;
  font-size: 11px;
  font-weight: var(--fw-semibold);
  line-height: 1.6;
  vertical-align: middle;
}

.command-row__arrow {
  color: var(--ink-faint);
  transition: transform var(--dur-2) var(--ease-out), color var(--dur-2) var(--ease-out);
}

.command-row:hover .command-row__arrow {
  color: var(--moss);
  transform: translateX(3px);
}

.file-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin: 0 -8px;
}

.file-row {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 50px;
  padding: 6px 8px;
  border-radius: var(--radius-md);
  transition: background-color var(--dur-2) var(--ease-out);
}

.file-row:hover {
  background: var(--fill-2);
}

.file-icon {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  flex: 0 0 auto;
  border-radius: 8px;
  background: var(--paper-deep);
  color: var(--ink-soft);
}

.file-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  color: var(--ink-soft);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.dialog-copy,
.guide-content {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.dialog-copy section,
.guide-content section {
  padding-bottom: 18px;
  border-bottom: 1px solid var(--border);
}

.dialog-copy section:last-child,
.guide-content section:last-of-type {
  padding-bottom: 0;
  border-bottom: 0;
}

.dialog-copy h3,
.guide-content h3,
.notice-item h3 {
  margin: 0 0 7px;
  font-size: 14px;
  font-weight: 650;
}

.dialog-copy p,
.dialog-copy ul,
.guide-content p,
.notice-item p {
  margin: 0;
  color: var(--ink-muted);
  font-size: 13px;
  line-height: 1.7;
}

.dialog-copy ul {
  padding-left: 20px;
}

.notice-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.notice-item {
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: 12px;
}

.notice-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 9px;
}

.notice-meta__tags {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.notice-item.is-new {
  border-color: color-mix(in srgb, var(--danger) 40%, var(--border));
  background: var(--danger-soft);
}

.notice-meta time {
  color: var(--ink-faint);
  font-size: 12px;
}

.flow-list {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
  counter-reset: flow;
}

.flow-list li {
  counter-increment: flow;
  padding: 14px;
  background: var(--surface-subtle);
  border-radius: 12px;
}

.flow-list li::before {
  content: counter(flow);
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  margin-bottom: 10px;
  color: var(--on-primary);
  background: var(--primary);
  border-radius: 8px;
  font-size: 12px;
  font-weight: 700;
}

.flow-list strong,
.flow-list span,
.mode-grid strong,
.mode-grid span {
  display: block;
}

.flow-list strong,
.mode-grid strong {
  font-size: 13px;
}

.flow-list span,
.mode-grid span {
  margin-top: 5px;
  color: var(--ink-muted);
  font-size: 12px;
  line-height: 1.6;
}

.mode-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.mode-grid > div {
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 12px;
}

.guide-steps {
  grid-template-columns: repeat(4, minmax(0, 1fr));
}

.document-download {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 16px;
  color: var(--ink);
  text-decoration: none;
  background: var(--primary-soft);
  border: 1px solid color-mix(in srgb, var(--primary) 24%, var(--border));
  border-radius: 12px;
  transition: border-color 0.16s ease, background 0.16s ease;
}

.document-download:hover {
  background: color-mix(in srgb, var(--primary-soft) 72%, var(--card));
  border-color: var(--primary);
}

.document-download:focus-visible {
  outline: none;
  box-shadow: var(--focus-ring);
}

.document-download span,
.document-download strong,
.document-download small {
  display: block;
}

.document-download strong {
  font-size: 13px;
}

.document-download small {
  margin-top: 3px;
  color: var(--ink-muted);
  font-size: 12px;
  line-height: 1.5;
}

.document-download > .el-icon {
  flex: 0 0 auto;
  color: var(--primary);
  font-size: 18px;
}

.chat-block :deep(.chat-section) {
  margin-bottom: 0;
}

@media (max-width: 1180px) {
  .home-bento,
  .home-hero {
    grid-template-columns: minmax(0, 1fr);
  }

  .home-hero__metrics {
    justify-content: flex-start;
  }
}

@media (max-width: 760px) {
  .task-grid,
  .command-list {
    grid-template-columns: minmax(0, 1fr);
  }

  .task-grid {
    grid-template-rows: none;
  }

  .task-tile.is-lead {
    grid-row: auto;
  }

  .home-hero {
    padding: 24px;
  }

  .flow-list,
  .mode-grid {
    grid-template-columns: 1fr;
  }
}
</style>
