<template>
  <div class="upload-page">
    <div
      v-if="serviceState.status !== 'available'"
      class="service-alert service-alert-global"
      :class="`service-alert-${serviceState.status}`"
      :role="serviceBlocksNewWork ? 'alert' : 'status'"
    >
      <strong>{{ serviceState.label }}</strong>
      <span>{{ serviceState.message || '批改链路当前不可用，请稍后再试。' }}</span>
    </div>
    <section class="hero-band grade-hero" :class="{ 'is-review': Boolean(summary) }">

      <div class="grade-hero__top">
        <div class="grade-hero__copy">
          <p class="hero-band__eyebrow">{{ anonymous ? '无身份批阅' : '详细批改' }} · 批改工作台</p>
          <h1 class="hero-band__title">{{ gradeHero.title }}</h1>
          <p class="hero-band__lead">{{ gradeHero.lead }}</p>
        </div>
        <div v-if="summary" class="hero-metrics grade-hero__metrics">
          <div class="hero-metric">
            <span class="hero-metric__value">{{ summary.total }}<small>{{ anonymous ? '页' : '份' }}</small></span>
            <span class="hero-metric__label">本批次</span>
          </div>
          <div class="hero-metric" :class="pendingReviewCount ? 'is-warn' : 'is-good'">
            <span class="hero-metric__value">{{ pendingReviewCount }}<small>题</small></span>
            <span class="hero-metric__label">待复核</span>
          </div>
          <div v-if="!anonymous" class="hero-metric">
            <span class="hero-metric__value">{{ resolvedActiveReviewCount }}<small>人</small></span>
            <span class="hero-metric__label">可入档</span>
          </div>
          <div v-if="form.category !== 'exam' && dailyAverageCorrectRate != null" class="hero-metric is-accent">
            <span class="hero-metric__value">{{ (dailyAverageCorrectRate * 100).toFixed(0) }}<small>%</small></span>
            <span class="hero-metric__label">平均正确率</span>
          </div>
        </div>
        <div v-else-if="dailyUsage && !dailyUsage.unlimited" class="monthly-quota" role="status">
          <span>今日学生图片</span>
          <strong><b class="quota-now">{{ dailyUsage.current }}</b> / {{ dailyUsage.limit }} 张</strong>
          <span class="quota-meter" aria-hidden="true"><span :style="{ width: `${dailyUsagePercent}%` }"></span></span>
          <span>剩余 {{ dailyUsage.remaining }} 张</span>
        </div>
      </div>
      <ol class="workflow-steps" data-tour="grade-workflow" aria-label="批改流程">
        <li
          v-for="(step, index) in workflowSteps"
          :key="step.key"
          class="workflow-step"
          :class="`is-${step.state}`"
          :aria-current="step.state === 'current' ? 'step' : undefined"
        >
          <span class="workflow-step__index" aria-hidden="true">{{ step.state === 'done' ? '✓' : index + 1 }}</span>
          <span class="workflow-step__text">
            <strong>{{ step.label }}</strong>
            <small>{{ step.hint }}</small>
          </span>
        </li>
      </ol>
    </section>
    <!-- 准备区：仅答案依据与学生试卷 -->
    <div class="columns">
      <!-- 左：答案依据（多页一次性模板，本次批改绝对权威） -->
      <TemplateCard />

      <!-- 右：待批阅试卷 -->
      <StudentPapersCard />
    </div>

    <el-dialog
      v-model="orientationDialogVisible"
      :title="`确认试卷方向${orientationPaper?.filename ? ` · ${orientationPaper.filename}` : ''}`"
      width="min(760px, 94vw)"
      :close-on-click-modal="!orientationSaving"
      :close-on-press-escape="!orientationSaving"
      @closed="resetOrientationReview"
    >
      <div class="orientation-review-hint" role="note">
        这里只生成批阅工作副本，服务器保存的原始上传文件不会被覆盖。请把文字旋转到正立后再确认；页面明显斜拍、缺边或模糊时请重新拍摄。
      </div>
      <div class="orientation-preview-stage">
        <div v-if="orientationImageLoading" class="orientation-preview-state"><InkSpinner />原图加载中…</div>
        <img
          v-else-if="orientationImageUrl"
          :src="orientationImageUrl"
          :alt="`${orientationPaper?.filename || '试卷'}方向预览`"
          :style="{ transform: `rotate(${orientationQuarterTurns * 90}deg)` }"
        />
        <div v-else class="orientation-preview-state is-error">{{ orientationImageError || '原图暂不可用' }}</div>
      </div>
      <div class="orientation-controls" aria-label="旋转试卷预览">
        <button type="button" class="btn btn-ghost" :disabled="orientationSaving" @click="rotateOrientationPreview(-1)">↶ 逆时针 90°</button>
        <span>当前旋转 {{ orientationQuarterTurns * 90 }}°</span>
        <button type="button" class="btn btn-ghost" :disabled="orientationSaving" @click="rotateOrientationPreview(1)">顺时针 90° ↷</button>
      </div>
      <div v-if="orientationImageError" class="recognize-error" role="alert">{{ orientationImageError }}</div>
      <template #footer>
        <span class="dialog-safety-note">确认人、原图哈希、旋转角度和工作副本哈希会写入批次审计记录。</span>
        <button type="button" class="btn btn-ghost" :disabled="orientationSaving" @click="orientationDialogVisible = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="orientationSaving || !orientationImageUrl || recognitionBlocksNewWork" @click="confirmOrientationAndRetry">
          {{ orientationSaving ? '确认并重新识别中…' : '确认文字正立并重新识别此卷' }}
        </button>
      </template>
    </el-dialog>

    <!-- 批改进度 / 审阅结果 -->
    <GradingResultsCard />

    <!-- 原卷＋批阅结果：按教师已确认身份归组，多页保持上传顺序 -->
    <ManualDecisionDialog />

    <!-- 点击开始识别后，确认批次信息与批阅模式 -->
    <el-dialog
      v-model="batchDialogVisible"
      title="确认批次信息"
      width="440px"
      :close-on-click-modal="false"
    >
      <div class="batch-form">
        <div class="form-row">
          <label class="form-label">批次类型</label>
          <select v-model="form.category" class="form-select" :disabled="!!confirmedTemplateId">
            <option value="" disabled>请选择批次类型</option>
            <option value="daily">日常作业</option>
            <option value="exam">考试</option>
          </select>
        </div>

        <div v-if="form.category === 'exam'" class="form-row">
          <label class="form-label">考试名称</label>
          <input v-model="form.exam_name" class="form-input" :disabled="!!confirmedTemplateId" placeholder="请输入考试名称" />
        </div>

        <div class="form-row">
          <label class="form-label">批阅模式</label>
          <div class="mode-cards">
            <div class="mode-card" :class="{ active: form.mode === 'precise' }" @click="form.mode = 'precise'">
              <div class="mode-title">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg>
                精准模式
              </div>
              <div class="mode-desc">逐题精细判定</div>
              <div class="mode-sub">适合考试测验</div>
            </div>
            <div class="mode-card" :class="{ active: form.mode === 'simple' }" @click="form.mode = 'simple'">
              <div class="mode-title">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg>
                简单模式
              </div>
              <div class="mode-desc">快速判对错</div>
              <div class="mode-sub">适合日常作业</div>
            </div>
          </div>
        </div>

      </div>
      <template #footer>
        <button class="btn btn-ghost" @click="batchDialogVisible = false">取消</button>
         <button class="btn btn-primary" :disabled="recognizing || !batchConfirmValid || recognitionBlocksNewWork" @click="onBatchConfirm">
           {{ recognizing ? '识别中...' : '开始识别' }}
         </button>
      </template>
    </el-dialog>

    <!-- 身份确认：未确认身份不建学生、不进入批改 -->
    <el-dialog
      v-if="!anonymous"
      v-model="identityDialogVisible"
      title="确认学生身份"
      width="720px"
      :close-on-click-modal="false"
    >
      <div class="tpl-confirm-hint">
        系统优先按当前班级名单核对姓名，无法确定或重名时才用学号辅助。支持从文件夹名或图片名获取身份；同一目录内姓名一致的重名待确认图片，可一次选择学生。新同学请优先到“学生管理”加入当前班级。
      </div>
      <div v-for="item in identityResolutions" :key="item.paper_id" class="identity-card">
        <div class="identity-paper-preview">
          <img v-if="item.image_url" :src="item.image_url" :alt="`${item.filename} 原图`" @click="openIdentityImage(item)" />
          <div v-else class="identity-image-loading">原图加载中…</div>
          <button v-if="item.image_url" type="button" class="link-btn" @click="openIdentityImage(item)">查看原图</button>
        </div>
        <div class="identity-controls">
          <div class="identity-head">
            <strong>{{ item.filename || item.paper_id }}</strong>
            <span>身份线索：{{ item.ocr_name || '未确定姓名' }} · {{ identityClueLabel(item.clue_source) }}</span>
            <span v-if="item.ocr_student_no">图片中的学号线索：{{ item.ocr_student_no }}</span>
            <span v-if="item.source_relative_path">来源：{{ item.source_relative_path }}</span>
          </div>
          <div v-if="item.pages.length > 1" class="form-hint identity-match-hint">
            <template v-if="item.inherited_page_count">后续 {{ item.inherited_page_count }} 张未写身份的图片沿用本张身份，此次选择应用于这 {{ item.pages.length }} 张图片。</template>
            <template v-else>此次选择应用于同一目录内的 {{ item.pages.length }} 张图片。</template>
            <button v-for="(page, index) in item.pages" :key="page.paper_id" type="button" class="link-btn" @click="openRecognizePaperImage(page)">查看第 {{ index + 1 }} 张</button>
          </div>
          <div v-if="item.inherit_blocked" class="form-hint identity-match-hint">
            本张未写身份，但上一位学生已有同一页，可能是另一位学生忘写姓名，因此没有沿用上一张的身份，请确认。
          </div>
          <div v-if="identityReasonHint(item.reason)" class="form-hint identity-match-hint">
            {{ identityReasonHint(item.reason) }}
          </div>
          <div v-if="identityOptionsLoading" class="form-hint">正在刷新本批次班级名单…</div>
          <div v-else-if="identityOptionsError" class="form-hint identity-options-error">
            {{ identityOptionsError }}
            <button type="button" class="link-btn" @click="refreshIdentityOptions">重新加载</button>
          </div>
          <div class="identity-mode">
            <label><input v-model="item.action" type="radio" value="existing" /> 选择已有学生</label>
            <label><input v-model="item.action" type="radio" value="create" :disabled="item.pages.length > 1" /> 确认新学生（例外）</label>
          </div>
          <div v-if="item.pages.length > 1" class="form-hint">
            这组图片如属于新学生，请先加入当前班级，再刷新名单选择，避免分开建档。
            <button type="button" class="link-btn" :disabled="identityOptionsLoading" @click="refreshIdentityOptions">刷新班级名单</button>
          </div>
          <template v-if="item.action === 'existing'">
            <select
              v-model="item.student_id"
              class="form-select"
              :disabled="identityOptionsLoading && !item.candidates.length"
            >
              <option :value="null" disabled>请选择当前班级学生</option>
              <option v-for="candidate in item.candidates" :key="candidate.student_id" :value="candidate.student_id">
                {{ candidate.name }}（{{ candidate.student_no || '无学号' }}）
              </option>
            </select>
            <div v-if="!item.candidates.length" class="form-hint">当前班级没有可匹配学生。请优先维护班级名单；确需临时建档时，再选择“确认新学生（例外）”。</div>
          </template>
          <div v-else class="identity-create-fields">
            <input v-model="item.confirmed_name" class="form-input" placeholder="确认姓名（必填）" />
            <input v-model="item.confirmed_student_no" class="form-input" placeholder="确认学号（选填）" />
          </div>
        </div>
      </div>
      <template #footer>
        <button class="btn btn-ghost" @click="identityDialogVisible = false" :disabled="grading">暂不批改</button>
        <button class="btn btn-primary" :disabled="grading || !identityCanConfirm" @click="onConfirmIdentitiesAndGrade">
          {{ grading ? '批改中...' : '确认身份并开始批改' }}
        </button>
      </template>
    </el-dialog>

    <!-- 模板编辑弹窗：保存本次权威与写入私库是两个独立操作 -->
    <TemplateEditorDialog />
  </div>
</template>

<script setup>
import { provide } from 'vue'
import InkSpinner from '@/components/InkSpinner.vue'
import ManualDecisionDialog from './upload/ManualDecisionDialog.vue'
import TemplateEditorDialog from './upload/TemplateEditorDialog.vue'
import GradingResultsCard from './upload/GradingResultsCard.vue'
import TemplateCard from './upload/TemplateCard.vue'
import StudentPapersCard from './upload/StudentPapersCard.vue'
import { useUploadState } from './upload/useUploadState.js'
import { useServiceAvailability } from './upload/useServiceAvailability.js'
import { useTemplateReviewState } from './upload/useTemplateReviewState.js'
import { useRecognitionRecovery } from './upload/useRecognitionRecovery.js'
import { useTemplateReference } from './upload/useTemplateReference.js'
import { useIdentityResolution } from './upload/useIdentityResolution.js'
import { useTemplateRecognition } from './upload/useTemplateRecognition.js'
import { useTemplateCut } from './upload/useTemplateCut.js'
import { useTemplateGeometry } from './upload/useTemplateGeometry.js'
import { useTemplateLayout } from './upload/useTemplateLayout.js'
import { useTemplateConfirm } from './upload/useTemplateConfirm.js'
import { useStudentFiles } from './upload/useStudentFiles.js'
import { useRecognitionRun } from './upload/useRecognitionRun.js'
import { useGrading } from './upload/useGrading.js'
import { useBatchResume } from './upload/useBatchResume.js'
import { useBatchReport } from './upload/useBatchReport.js'
import { useReviewResults } from './upload/useReviewResults.js'
import { useUploadLifecycle } from './upload/useUploadLifecycle.js'
import { UPLOAD_PAGE } from './upload/uploadPage.js'

const props = defineProps({
  anonymous: { type: Boolean, default: false },
})

// The rest of the page lives in one composable per part (views/upload/use*.js). Each one gets ctx,
// holding what the earlier parts defined; it calls functions of later parts through ctx.
const ctx = {
  props,
}
Object.assign(ctx, useUploadState(ctx))
Object.assign(ctx, useServiceAvailability(ctx))
Object.assign(ctx, useTemplateReviewState(ctx))
Object.assign(ctx, useRecognitionRecovery(ctx))
Object.assign(ctx, useTemplateReference(ctx))
Object.assign(ctx, useIdentityResolution(ctx))
Object.assign(ctx, useTemplateRecognition(ctx))
Object.assign(ctx, useTemplateCut(ctx))
Object.assign(ctx, useTemplateGeometry(ctx))
Object.assign(ctx, useTemplateLayout(ctx))
Object.assign(ctx, useTemplateConfirm(ctx))
Object.assign(ctx, useStudentFiles(ctx))
Object.assign(ctx, useRecognitionRun(ctx))
Object.assign(ctx, useGrading(ctx))
Object.assign(ctx, useBatchResume(ctx))
Object.assign(ctx, useBatchReport(ctx))
Object.assign(ctx, useReviewResults(ctx))
Object.assign(ctx, useUploadLifecycle(ctx))
// The components that render parts of the page take what they need from ctx (views/upload/*.vue).
provide(UPLOAD_PAGE, ctx)
const {
  anonymous, inheritIdentity, fileList, imagePreprocessing, imageExtracting, batchDialogVisible,
  activeStep, recognizing, grading, dailyUsage, serviceState, serviceBlocksNewWork,
  registrationHardwareState, recognitionBlocksNewWork, templateFiles, confirmedTemplateId,
  templateDialogVisible, confirmingTemplate, templatePages, templateCutBusy, templateCutResult,
  templateCutPreviewPages, templateCutPreviewError, templateCropGesture, pendingTemplateId,
  identityDialogVisible, identityResolutions, identityOptionsLoading, identityOptionsError, form,
  batchId, papers, recognized, recognitionDone, recognitionAutoArmed, recognitionCanGrade,
  recognitionError, recognitionRecoveryState, questions, orientationDialogVisible, orientationPaper,
  orientationImageUrl, orientationImageLoading, orientationImageError, orientationQuarterTurns,
  orientationSaving, progressList, gradeError, summary, gradeHero, workflowSteps, dailyUsagePercent,
  templateCutRunToken, gradablePaperCount, batchConfirmValid, templateGeometryReview,
  templateCanConfirm, setTemplatePreviewEditMode, answerSpecFields, identityCanConfirm,
  openIdentityImage, openRecognizePaperImage, identityReasonHint, identityClueLabel,
  refreshIdentityOptions, applyTemplateRecognitionResult, onRecognizeTemplate,
  applyTemplateCutState, loadTemplateCutPreviews, loadTemplateCutState, onRunTemplateCut,
  resetTemplateColumns, templatePreviewPageIndex, selectTemplatePreviewPage,
  armTemplateGeometryTool, deleteTemplateSelection, startTemplateHandwritingMove,
  startTemplateHandwritingResize, insertTemplateQuestion, deleteTemplateQuestion,
  onImportTemplateBank, cancelTemplateDialog, processFiles, importPhoneCaptures,
  importStudentSubmissions, resetOrientationReview, rotateOrientationPreview, applyOrientationState,
  confirmOrientationAndRetry, continueAfterRecognition, startNewBatch, onStartRecognize,
  onBatchConfirm, runRecognize, onConfirmIdentitiesAndGrade, onConfirmGrade,
  dailyAverageCorrectRate, pendingReviewCount, resolvedActiveReviewCount,
} = ctx
</script>

<style scoped src="./upload/page.css"></style>
<style scoped src="./upload/cards.css"></style>
