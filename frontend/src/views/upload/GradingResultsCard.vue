<template>
  <div
    v-if="progressCardVisible"
    class="card progress-card"
    :class="{ 'is-review-focus': Boolean(summary) && !reviewArchived }"
  >
    <div class="card-title">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="M22 4L12 14.01l-3-3"/></svg>
      {{ summary ? (anonymous ? '无身份批阅结果' : '批改结果审阅') : (gradeError ? '批改未完成' : '批改进度') }}
      <span class="mode-pill">{{ form.mode === 'simple' ? '简单模式' : '精准模式' }}</span>
      <HelpButton
        title="批改结果审阅"
        content="先查看原卷与逐题结果，再人工判定或作废异常记录。精准模式可导出教师汇总和错题条，确认无误后归档才会写入正式档案。"
      />
      <span v-if="!grading && reviewArchived" class="status-chip is-good">本批审阅已结束</span>
      <span v-else-if="!grading && summary" class="status-chip is-good">{{ anonymous ? '批阅完成' : '等待老师审阅' }}</span>
      <span v-if="summary && reviewRestoredLabel" class="status-chip is-plain">{{ reviewRestoredLabel }}</span>
      <GradingPhases class="card-title-phases" :labels="gradingPhaseState.labels" :current="gradingPhaseState.current" />
    </div>

    <GradingProgress
      v-if="grading"
      class="card-progress"
      verb="已批阅"
      :unit="anonymous ? '页' : '份'"
      :current="progressList.length"
      :total="gradeTotal"
      :elapsed="formatGradingDuration(gradingElapsedSeconds)"
      :message="currentMessage || '正在批改'"
      :hint="anonymous ? '每页批完会立即出现在下方列表' : '每份批完会立即出现在下方列表'"
    />

    <div v-if="gradeError" class="recognize-error" role="alert">{{ gradeError }}</div>

    <div v-if="progressList.length" class="review-board" role="table" :aria-label="anonymous ? '无身份批阅结果' : '批改结果'">
      <div class="review-board__head" role="row">
        <span role="columnheader">#</span>
        <span role="columnheader">{{ anonymous ? '页面' : '学生姓名' }}</span>
        <span role="columnheader">判定构成</span>
        <span role="columnheader">{{ form.category === 'exam' ? '得分' : '正确率' }}</span>
        <span v-if="summary" role="columnheader">处理</span>
      </div>
      <TransitionGroup id="grading-start-anchor" name="list" tag="div" class="review-board__body">
        <div
          v-for="(row, i) in progressList"
          :key="row.review_id || row.paper_id || i"
          class="review-row"
          role="row"
          :class="{
            'is-voided': row.disposition === 'voided',
            'is-archived': row.disposition === 'archived',
            'is-attention': Number(row.needs_review || 0) > 0 && row.disposition === 'active',
          }"
        >
          <span class="review-row__index" role="cell">{{ String(i + 1).padStart(2, '0') }}</span>
          <span class="review-row__who" role="cell">
            <span class="review-avatar" aria-hidden="true">{{ String(row.student_name || '页').trim().slice(0, 1) }}</span>
            <span class="review-row__name">
              <strong>{{ row.student_name }}</strong>
              <button class="link-btn" type="button" @click="openManualDecision(row)">原卷与批阅结果</button>
            </span>
          </span>
          <span class="review-row__outcome" role="cell">
            <span class="outcome-mini" :title="`正确 ${Number(row.correct || 0)} · 部分正确 ${Number(row.partial || 0)} · 错误 ${Number(row.wrong || 0)} · 待复核 ${Number(row.needs_review || 0)}`">
              <i v-if="Number(row.correct || 0)" class="is-correct" :style="{ flexGrow: Number(row.correct || 0) }" />
              <i v-if="Number(row.partial || 0)" class="is-partial" :style="{ flexGrow: Number(row.partial || 0) }" />
              <i v-if="Number(row.wrong || 0)" class="is-wrong" :style="{ flexGrow: Number(row.wrong || 0) }" />
              <i v-if="Number(row.needs_review || 0)" class="is-pending" :style="{ flexGrow: Number(row.needs_review || 0) }" />
            </span>
            <small>正确 {{ row.correct }} / {{ row.total }}<template v-if="Number(row.wrong || 0)"> · 错 {{ row.wrong }}</template><template v-if="Number(row.partial || 0)"> · 半对 {{ row.partial }}</template></small>
          </span>
          <span class="review-row__metric" role="cell">
            <span v-if="Number(row.needs_review || 0) > 0" class="status-chip is-warn">待复核 {{ row.needs_review }} 题</span>
            <span v-else-if="row.category === 'exam' && row.score != null && Number.isFinite(Number(row.score))" class="review-score">
              {{ Number(row.score).toFixed(Number(row.score) % 1 ? 1 : 0) }}<small> / {{ Number(row.max_score || 0).toFixed(Number(row.max_score || 0) % 1 ? 1 : 0) }} 分</small>
            </span>
            <span v-else-if="row.correct_rate != null && Number.isFinite(Number(row.correct_rate))" class="review-score" :class="{ 'is-low': Number(row.correct_rate) < 0.6 }">
              {{ (Number(row.correct_rate) * 100).toFixed(0) }}<small>%</small>
            </span>
            <span v-else>—</span>
          </span>
          <span v-if="summary" class="review-row__actions row-actions" role="cell">
            <span v-if="row.disposition === 'voided'" class="tag-muted">已作废</span>
            <span v-else-if="row.disposition === 'archived'" class="tag-ok">已入档</span>
            <template v-else>
              <button class="link-btn" :disabled="reviewBusy" @click="openManualDecision(row)">人工判定</button>
              <button class="link-btn danger-link" :disabled="reviewBusy" @click="voidOneReview(row)">作废</button>
              <button v-if="!anonymous && form.mode === 'precise'" class="link-btn" :disabled="reviewBusy" @click="downloadStudentErrors(row)">错题条</button>
              <button class="link-btn" :disabled="reviewBusy" @click="downloadReviewImage(row)">批阅痕迹图</button>
            </template>
          </span>
        </div>
      </TransitionGroup>
    </div>

      <div v-if="summary" id="grading-summary-anchor" class="review-actions">
      <div class="summary-bar">
        <span>总计 <strong>{{ summary.total }}</strong> {{ anonymous ? '页' : '张' }}</span>
        <span v-if="summary.grading_elapsed_seconds != null" class="tag-muted grading-clock">批阅用时 {{ formatGradingDuration(summary.grading_elapsed_seconds) }}</span>
        <span v-if="form.category === 'exam' && summary.avg_score != null" class="tag-ok">已判平均分 {{ Number(summary.avg_score).toFixed(1) }}</span>
        <span v-else-if="form.category !== 'exam' && dailyAverageCorrectRate != null" class="tag-ok">平均正确率 {{ (dailyAverageCorrectRate * 100).toFixed(0) }}%</span>
        <span v-else class="tag-pending">待复核，暂不显示统计</span>
        <span v-if="pendingReviewCount" class="tag-pending">待复核 {{ pendingReviewCount }} 题</span>
        <span v-if="!anonymous && resolvedActiveReviewCount" class="tag-ok">可入档 {{ resolvedActiveReviewCount }} 人</span>
        <span v-if="!anonymous && hasActiveReviewRows && !reviewArchived" class="review-hint">待复核学生不会写入正式档案；可先归档已完成学生。</span>
      </div>
      <div class="review-toolbar">
        <button class="btn btn-ghost" :disabled="reviewBusy || reviewImagesDownloading || !hasReviewImageRows" @click="downloadAllReviewImages">{{ reviewImagesDownloading ? '正在打包批阅图…' : '批量下载批阅痕迹图' }}</button>
        <button class="btn btn-ghost" :disabled="reviewBusy || !hasActiveReviewRows || reviewArchived" @click="voidAllReviews">{{ anonymous ? '排除全部页面' : '全部作废' }}</button>
        <button v-if="!anonymous && form.mode === 'precise'" class="btn btn-ghost" :disabled="reviewBusy || !hasActiveReviewRows" @click="downloadReviewSummary">导出老师汇总</button>
        <button v-if="!anonymous && form.mode === 'precise'" class="btn btn-ghost" :disabled="reviewBusy || !hasActiveReviewRows" @click="downloadAllErrors">导出全部错题</button>
        <button v-if="!anonymous" class="btn btn-primary" :disabled="reviewBusy || !resolvedActiveReviewCount || reviewArchived" @click="archiveAllReviews">
          {{ reviewBusy ? '处理中...' : '归档已完成结果' }}
        </button>
      </div>
        <div v-if="!anonymous && form.mode !== 'precise'" class="form-hint">错题条和汇总文件仅在精准模式提供；简单模式仍可人工判定、作废或入档。</div>
      </div>

      <GradingQuickDock />

      <section v-if="summary" class="batch-report-panel" aria-live="polite">
        <div class="batch-report-head">
          <div class="batch-report-title-wrap">
            <span class="batch-report-eyebrow">本批洞察</span>
            <strong>本批批阅报告</strong>
            <span class="batch-report-subtitle">
              {{ anonymous ? '无需绑定学生，按页面查看整批学情' : '确认后可写入学生档案与班级统计' }}
            </span>
          </div>
          <div class="batch-report-head-actions">
            <span class="status-chip" :class="batchReportStatusTone(batchReport.status)">
              {{ batchReportStatusText(batchReport.status) }}
            </span>
            <HelpButton title="本批批阅报告" content="报告按题目判定结果统计正确、部分正确、错误和待复核；已有知识点标注时再展示知识点分布，不使用错因分类。待复核记录不会被当作已确定的对错结论。" />
          </div>
        </div>

        <template v-if="batchReportStats.question_count">
          <div class="batch-report-overview">
            <article class="batch-report-ring-card">
              <OutcomeDonut
                :segments="batchReportOutcomeSegments"
                :pending="batchReportNeedsReviewCount"
                :center-value="batchReportPrimaryValue"
                :center-label="batchReportPrimaryLabel"
              />
            </article>

            <div class="batch-report-stats">
              <article class="stat-tile">
                <span class="stat-tile__label">批阅范围</span>
                <strong class="stat-tile__value"><AnimatedNumber :value="Number(batchReportStats.paper_count || 0)" /><small class="stat-tile__unit">{{ anonymous ? '页' : '份' }}</small></strong>
                <span class="stat-tile__meta">共 {{ batchReportTotalQuestions }} 道题</span>
              </article>
              <article class="stat-tile">
                <span class="stat-tile__label">判定覆盖</span>
                <strong class="stat-tile__value"><AnimatedNumber :value="batchReportJudgedCount" /><small class="stat-tile__unit"> / {{ batchReportTotalQuestions }}</small></strong>
                <span class="stat-tile__meta">待复核 {{ batchReportNeedsReviewCount }} 题</span>
              </article>
              <article class="stat-tile is-warm">
                <span class="stat-tile__label">错题总量</span>
                <strong class="stat-tile__value"><AnimatedNumber :value="batchReportErrorTotal" /><small class="stat-tile__unit">题</small></strong>
                <span class="stat-tile__meta">错误 + 部分正确</span>
              </article>
              <article class="stat-tile">
                <span class="stat-tile__label">全题错题率</span>
                <strong class="stat-tile__value"><AnimatedNumber :value="batchReportErrorRatePercent" :decimals="1" /><small v-if="batchReportErrorRatePercent != null" class="stat-tile__unit">%</small></strong>
                <span v-if="batchReportStats.choice_only" class="stat-tile__meta">选择题错 {{ Number(batchReportStats.choice_wrong_count || 0) }} 题 · 仅作统计</span>
                <span v-else-if="batchReportStats.choice_count" class="stat-tile__meta">选择题错 {{ Number(batchReportStats.choice_wrong_count || 0) }} 题 · 非选择题诊断 {{ batchReportDiagnosticErrorRateText }}</span>
                <span v-else-if="batchReportStats.diagnostic_judged_count" class="stat-tile__meta">非选择题诊断 {{ batchReportDiagnosticErrorRateText }}</span>
                <span v-else class="stat-tile__meta">以已判题目为统计范围</span>
              </article>
            </div>
          </div>

          <div class="batch-report-insight-grid">
            <section class="insight-card batch-report-top-errors-card">
              <div class="insight-card__head">
                <div>
                  <span class="insight-card__kicker">优先处理</span>
                  <h3 class="insight-card__title">高频错题（前五道）</h3>
                  <p class="insight-card__lead">先讲清错误最多、最值得复盘的题目。</p>
                </div>
                <span v-if="batchReportTopErrors.length" class="insight-card__badge">TOP {{ batchReportTopErrors.length }}</span>
              </div>
              <ol v-if="batchReportTopErrors.length" class="batch-report-top-errors">
                <li v-for="(item, index) in batchReportTopErrors" :key="`${item.question_no}-${item.error_count}`">
                  <span class="batch-report-rank" aria-hidden="true">{{ String(index + 1).padStart(2, '0') }}</span>
                  <div class="batch-report-error-copy">
                    <strong>第{{ item.question_no }}题</strong>
                    <small>错误率 {{ reportPercent(item.error_rate) }} · 已判 {{ item.judged_count ?? item.attempt_count }} / 作答 {{ item.attempt_count }} 次</small>
                    <span class="ink-bar batch-report-error-track" aria-hidden="true">
                      <span :style="{ width: `${reportDistributionPercent(item.error_count, batchReportTopErrorMax)}%`, '--i': index }" />
                    </span>
                    <span v-if="item.question" class="batch-report-question">{{ item.question }}</span>
                  </div>
                  <b class="batch-report-error-count">{{ item.error_count }}<small>次错</small></b>
                </li>
              </ol>
              <div v-else class="batch-report-empty">暂未形成可用的错题分布</div>
            </section>

            <section class="insight-card batch-report-distribution-card">
              <div class="insight-card__head">
                <div>
                  <span class="insight-card__kicker">学习画像</span>
                  <h3 class="insight-card__title">知识点分布</h3>
                  <p class="insight-card__lead">先定位本批次需要讲评的知识点，再安排针对性练习。</p>
                </div>
              </div>
              <div v-if="batchReportKnowledgePoints.length" class="batch-report-distributions">
                <div class="batch-report-distribution-block">
                  <ul class="batch-report-inline-list batch-report-bar-list">
                    <li v-for="(item, index) in batchReportKnowledgePoints" :key="`topic-${item.chapter}-${item.topic || item.name}`" class="batch-report-bar-row">
                      <div class="batch-report-bar-label"><span>{{ reportKnowledgeName(item) }}</span><strong>{{ item.error_count }} 次错</strong></div>
                      <span class="ink-bar batch-report-bar-track" aria-hidden="true"><span :style="{ width: `${reportDistributionPercent(item.error_count, batchReportKnowledgeErrorMax)}%`, '--i': index }" /></span>
                      <small>{{ reportKnowledgeChapter(item) }} · 错 {{ item.error_count }} / 作答 {{ item.attempt_count }} · 错误率 {{ reportPercent(item.error_rate) }}</small>
                    </li>
                  </ul>
                </div>
              </div>
              <div v-else class="batch-report-empty">当前没有知识点标签；上方题目判定统计仍然有效</div>
            </section>
          </div>

          <details v-if="batchReport.stats.all_errors?.length" class="batch-report-details">
            <summary>查看全部错题分布（{{ batchReport.stats.all_errors.length }} 道）</summary>
            <div class="batch-report-table-wrap">
              <table class="batch-report-table">
                <thead>
                  <tr><th>题号</th><th>错题次数</th><th>已判 / 作答</th><th>错误率</th><th>题干摘要</th></tr>
                </thead>
                <tbody>
                  <tr v-for="item in batchReport.stats.all_errors" :key="`all-${item.question_no}`">
                    <td><strong>第{{ item.question_no }}题</strong></td>
                    <td>{{ item.error_count }}</td>
                    <td>{{ item.judged_count ?? item.attempt_count }} / {{ item.attempt_count }}</td>
                    <td>{{ reportPercent(item.error_rate) }}</td>
                    <td class="batch-report-question-cell">{{ item.question || '题干未记录' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </details>

          <div class="batch-report-followup">
            <section class="insight-card batch-report-teaching-card">
              <div class="insight-card__head">
                <div>
                  <span class="insight-card__kicker">下一步</span>
                  <h3 class="insight-card__title">教学建议</h3>
                </div>
              </div>
              <p v-if="['queued', 'generating', 'not_started'].includes(batchReport.status)" class="batch-report-pending">
                <InkSpinner />批阅结果已先显示，报告正在生成中，请稍候。
              </p>
              <p v-else-if="batchReport.status === 'failed'" class="batch-report-failed">
                {{ batchReport.error || '报告生成失败，请稍后刷新重试。' }}
              </p>
              <template v-else>
                <p class="batch-report-text">{{ batchReport.text }}</p>
                <p v-if="batchReport.error" class="batch-report-note">{{ batchReport.error }}</p>
              </template>
            </section>

            <section class="insight-card courseware-rec-block" aria-live="polite">
              <div class="insight-card__head">
                <div>
                  <span class="insight-card__kicker">配套资源</span>
                  <h3 class="insight-card__title">推荐课件</h3>
                  <p class="insight-card__lead">按高频错题知识点匹配（精确知识点优先于同章知识点，最多 3 个，只含当前教师学段）。</p>
                </div>
              </div>
              <p v-if="coursewareRecommendationState === 'loading'" class="courseware-rec-state"><InkSpinner />正在匹配推荐课件…</p>
              <p v-else-if="coursewareRecommendationState === 'error'" class="courseware-rec-state">推荐课件加载失败；可稍后重新打开本批次重试。</p>
              <p v-else-if="coursewareRecommendationState === 'empty' || !coursewareRecommendations.length" class="courseware-rec-state">暂无匹配课件。</p>
              <ul v-else class="courseware-rec-list">
                <li v-for="card in coursewareRecommendations" :key="`rec-${card.id}`" class="courseware-rec-item">
                  <div class="courseware-rec-main">
                    <strong>{{ card.title }}</strong>
                    <small>命中学段：{{ card.stageLabel }} · 章节：{{ card.chapter || '—' }} · 知识点：{{ card.knowledgePoint || '—' }}</small>
                    <small v-if="card.reason">推荐原因：{{ card.reason }}</small>
                  </div>
                  <button type="button" class="btn btn-ghost" @click="openCourseware(card)">去课件库打开</button>
                </li>
              </ul>
            </section>
          </div>
        </template>
        <div v-else class="batch-report-empty-state">
          <VerdictSwatch verdict="pending" />
          <div>
            <strong>还没有可统计的逐题判定结果</strong>
            <p>结果分布、重点错题、教学建议和推荐课件会在批阅判定完成后出现在这里；若本批次尚未批阅，请先点击“继续开始批阅”。</p>
          </div>
        </div>
      </section>
    </div>
</template>

<script setup>
// The 批改 page's grading card: grading progress, then the review board of the graded papers, the batch report
// and the courseware recommendations, with the review actions.  State and actions come from the page's ctx
// (useGrading, useReviewResults, useBatchReport).
import { inject } from 'vue'
import AnimatedNumber from '@/components/AnimatedNumber.vue'
import GradingPhases from '@/components/GradingPhases.vue'
import GradingProgress from '@/components/GradingProgress.vue'
import GradingQuickDock from '@/components/GradingQuickDock.vue'
import InkSpinner from '@/components/InkSpinner.vue'
import OutcomeDonut from '@/components/OutcomeDonut.vue'
import VerdictSwatch from '@/components/VerdictSwatch.vue'
import { formatGradingDuration } from '@/utils/gradingTimer'
import {
  batchReportStatusText,
  batchReportStatusTone,
  reportPercent,
  reportKnowledgeName,
  reportKnowledgeChapter,
  reportDistributionPercent,
} from '@/utils/batchReportView'
import HelpButton from '@/components/HelpButton.vue'
import { UPLOAD_PAGE } from './uploadPage.js'

const {
  progressCardVisible, summary, reviewArchived, anonymous, gradeError, form, grading,
  reviewRestoredLabel, gradingPhaseState, progressList, gradeTotal, gradingElapsedSeconds,
  currentMessage, openManualDecision, reviewBusy, voidOneReview, downloadStudentErrors,
  downloadReviewImage, dailyAverageCorrectRate, pendingReviewCount, resolvedActiveReviewCount,
  hasActiveReviewRows, reviewImagesDownloading, hasReviewImageRows, downloadAllReviewImages,
  voidAllReviews, downloadReviewSummary, downloadAllErrors, archiveAllReviews, batchReport,
  batchReportStats, batchReportOutcomeSegments, batchReportNeedsReviewCount,
  batchReportPrimaryValue, batchReportPrimaryLabel, batchReportTotalQuestions,
  batchReportJudgedCount, batchReportErrorTotal, batchReportErrorRatePercent,
  batchReportDiagnosticErrorRateText, batchReportTopErrors, batchReportTopErrorMax,
  batchReportKnowledgePoints, batchReportKnowledgeErrorMax, coursewareRecommendationState,
  coursewareRecommendations, openCourseware,
} = inject(UPLOAD_PAGE)
</script>

<style scoped>
/* No styles of its own: this component shares Upload.vue's scope id (vite.config.js), so the page's
   scoped styles (upload/page.css, upload/cards.css) apply to it. */
</style>
