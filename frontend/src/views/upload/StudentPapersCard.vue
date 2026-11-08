<template>
  <div class="card grade-card" :data-step="activeStep === 0 ? '02' : '03'">
    <!-- Step 0: 表单 + 上传 -->
    <template v-if="activeStep === 0">
      <div class="card-title">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17l9.2-9.2M17 17V7H7"/></svg>
        {{ anonymous ? '无身份试卷页面' : '学生试卷' }}
        <span class="card-title-sub">图片 / ZIP / RAR</span>
        <span class="service-state" data-tour="service-state" :class="`service-state-${serviceState.status}`">{{ serviceState.label }}</span>
        <HelpButton
          title="学生试卷上传"
          content="每张图片会独立识别。普通批阅需要可匹配到当前班级学生；无身份批阅只处理页面，不写入学生档案。"
        />
      </div>
      <!-- 班级提示 -->
      <div class="form-row" v-if="!anonymous && auth.currentClassName">
        <div class="form-hint">当前班级：{{ auth.currentClassName }}（顶部导航可切换）</div>
      </div>

      <!-- 上传区域 -->
      <div
        class="upload-area"
        data-tour="paper-upload"
        :class="{ 'is-dragover': isDragover, 'is-disabled': recognitionBlocksNewWork || imagePreprocessing || imageExtracting }"
        :aria-busy="imagePreprocessing || imageExtracting ? 'true' : undefined"
        :aria-disabled="recognitionBlocksNewWork ? 'true' : undefined"
        @dragover.prevent="isDragover = true"
        @dragleave="isDragover = false"
        @drop.prevent="onDrop"
      >
        <SheetLoader v-if="imagePreprocessing || imageExtracting" class="u-icon is-busy" :size="72" />
        <el-icon v-else class="u-icon"><UploadFilled /></el-icon>
        <div class="u-label">{{ imageExtracting ? extractionProgress.message : (imagePreprocessing ? '正在逐张处理试卷图片' : '导入需要批阅的试卷') }}</div>
        <div class="u-sub">{{ imageExtracting ? `${extractionProgress.current} / ${extractionProgress.total}` : (imagePreprocessing ? '完成后即可开始识别' : '点击下方按钮或拖拽文件/文件夹/压缩包到此处') }}</div>
        <div class="upload-buttons">
          <button type="button" class="btn btn-ghost" :disabled="imagePreprocessing || imageExtracting || recognitionBlocksNewWork" @click.stop="triggerFileUpload">选择文件</button>
          <button type="button" class="btn btn-ghost" :disabled="imagePreprocessing || imageExtracting || recognitionBlocksNewWork" @click.stop="triggerFolderUpload">选择文件夹</button>
          <button type="button" class="btn btn-ghost" data-test="open-phone-capture" :disabled="imagePreprocessing || imageExtracting || recognitionBlocksNewWork" @click.stop="openPhoneCapture">手机拍摄</button>
          <button v-if="!anonymous" type="button" class="btn btn-ghost" data-test="open-student-submissions" :disabled="imagePreprocessing || imageExtracting || recognitionBlocksNewWork" @click.stop="openStudentSubmissions">学生上传</button>
        </div>
      </div>
      <PhoneCaptureDialog
        v-model="phoneCaptureVisible"
        :disabled="imagePreprocessing || imageExtracting || recognitionBlocksNewWork || recognizing"
        :import-files="importPhoneCaptures"
      />
      <StudentSubmissionDialog
        v-model="studentSubmissionVisible"
        :class-id="auth.currentClassId"
        :class-name="auth.currentClassName"
        :disabled="imagePreprocessing || imageExtracting || recognitionBlocksNewWork || recognizing"
        :import-files="importStudentSubmissions"
      />
      <input ref="fileInputRef" type="file" :accept="IMPORT_ACCEPT" multiple style="display:none" :disabled="imagePreprocessing || imageExtracting || recognitionBlocksNewWork" @change="onFileInputChange" />
      <input ref="folderInputRef" type="file" webkitdirectory="" style="display:none" :disabled="imagePreprocessing || imageExtracting || recognitionBlocksNewWork" @change="onFolderInputChange" />
      <div class="upload-limit">支持 {{ IMPORT_FORMATS }}。单次最多 200 张、展开后合计 200MB；图片不超过 20MB，压缩包不超过 64MB，不支持加密或嵌套压缩包。文字必须正立且倾斜不超过约 5°；方向异常的试卷不会自动矫正或批阅。</div>

      <!-- 姓名书写规范提示：必须与当前 OCR 的 top-25% + left-15% 读取区域一致 -->
      <div v-if="!anonymous" class="name-guide">
        <div class="name-guide-header" @click="showNameGuide = !showNameGuide">
          <span>姓名书写规范（点击展开）</span>
          <el-icon class="toggle-icon"><ArrowDown v-if="showNameGuide" /><ArrowRight v-else /></el-icon>
        </div>
        <div v-if="showNameGuide" class="name-guide-content">
          <div class="name-guide-diagram">
            <div class="paper-preview">
              <div class="zone zone-top">① 顶部 25%（首选）</div>
              <div class="zone zone-left">② 左侧 15%（顶部以下）</div>
              <div class="zone zone-rest">其他区域<br>可能需人工确认</div>
            </div>
            <div class="name-guide-rules">
              <div class="rule good">✓ 姓名和学号优先写在 <strong>① 顶部 25%</strong></div>
              <div class="rule good">✓ 也可写在 <strong>② 左侧 15%</strong>（从顶部下缘开始）</div>
              <div class="rule good">✓ 姓名与当前班级名单一致，字迹工整</div>
              <div class="rule bad">✗ 不要只写在右侧、右下角或题目中间</div>
              <div class="rule bad">✗ 不要用拼音、英文、连笔</div>
            </div>
          </div>
          <div class="name-guide-tip">也可用“姓名＋学号（选填）”命名图片或所属文件夹，同一学生的多张图片可带不同后缀。未提供命名身份时，建议每页在顶部填写姓名；有重名或身份冲突时由老师确认。</div>
        </div>
      </div>

      <div v-if="fileList.length" class="file-summary">
        已选 {{ fileList.length }} 个文件
        <button class="link-btn" @click="resetFiles">清空</button>
      </div>

      <div v-if="!anonymous" class="identity-inherit-option">
        <label>
          <input v-model="inheritIdentity" type="checkbox" :disabled="recognizing || recognitionBlocksNewWork" />
          未写姓名的试卷沿用上一张的身份
        </label>
        <HelpButton
          title="沿用上一张的身份"
          content="适用于同一学生的多页试卷只在第一页写姓名。开启后按文件名顺序排列试卷：某一页既没写姓名、学号，文件名和文件夹也不含名单姓名时，自动归属上一张试卷的学生。写了姓名但对不上名单的页面、识别失败的页面仍由老师确认；同一学生出现重复的同一页时不再沿用。"
        />
      </div>

      <div class="actions">
        <label class="form-label" for="batch-grade-mode">批阅模式</label>
        <select id="batch-grade-mode" v-model="form.mode" class="form-select" style="width: auto" :disabled="recognizing || grading">
          <option value="precise">精准模式</option>
          <option value="simple">简单模式</option>
        </select>
        <button class="btn btn-ghost" @click="reset" :disabled="recognizing">
          {{ confirmedTemplateId || pendingTemplateId || templateDialogVisible ? '清空学生试卷' : '清空' }}
        </button>
        <button
          class="btn btn-primary"
          :disabled="!fileList.length || !confirmedTemplateId || recognizing || imagePreprocessing || recognitionBlocksNewWork"
          data-tour="start-grading"
          @click="onStartRecognize"
        >
          开始批阅
        </button>
      </div>
    </template>

    <!-- Step 1: 识别中 -->
    <template v-else-if="activeStep === 1">
      <div class="card-title">
        {{ recognizing ? '识别中...' : (recognitionCanGrade ? '识别完成' : '识别未完成') }}
        <HelpButton title="识别进度" content="识别会逐张保存进度。失败或待人工复核的图片不会被静默丢弃，可先处理可用图片，也可重新识别未完成项。" />
        <GradingPhases v-if="!progressCardVisible" class="card-title-phases" :labels="gradingPhaseState.labels" :current="gradingPhaseState.current" />
      </div>
      <GradingProgress
        class="card-progress"
        verb="已识别"
        unit="张"
        :current="recognized.current"
        :total="recognized.total"
        :live="recognizing"
        :message="currentMessage || '正在识别学生试卷'"
        hint="识别结果会逐张出现在下方表格"
      />
      <div v-if="recognitionDone" class="recognize-summary" role="status">
        共 {{ recognitionStats.uploaded }} 张：成功 {{ recognitionStats.succeeded }} 张，需人工复核 {{ recognitionStats.manualReview }} 张，失败 {{ recognitionStats.failed }} 张，未完成 {{ recognitionStats.unresolved }} 张<template v-if="recognitionStats.excluded">，已排除 {{ recognitionStats.excluded }} 张</template>。
      </div>
      <div v-else-if="!recognizing && (recognitionError || recognizeUnresolvedCount)" class="recognize-summary" role="status">
        已处理 {{ recognizeCompletedCount }} / {{ recognized.total }} 张；成功 {{ recognizeSucceededCount }} 张，失败 {{ recognizeFailureCount }} 张，需人工复核 {{ recognizeManualReviewCount }} 张，未完成 {{ recognizeUnresolvedCount }} 张。
      </div>
      <div v-else-if="recognizing && (recognizeFailureCount || recognizeManualReviewCount)" class="recognize-warning" role="status">
        已处理 {{ recognized.current }} / {{ recognized.total }} 张；失败 {{ recognizeFailureCount }} 张，需人工复核 {{ recognizeManualReviewCount }} 张。系统仍在处理其余试卷。
      </div>
      <div v-if="recognitionError" class="recognize-error" role="alert">
        {{ recognitionError }}
      </div>
      <div
        v-if="recognitionRecoveryState.message"
        :class="recognitionRecoveryIsError ? 'recognize-error' : 'recognize-warning'"
        :role="recognitionRecoveryIsError ? 'alert' : 'status'"
      >
        {{ recognitionRecoveryState.message }}
      </div>
      <div v-if="papers.some(p => p.orientation_status && !orientationReady(p))" class="recognize-warning" role="alert">
        有试卷方向未通过检查：系统不会自动矫正，也不会让异常图片进入批阅。请查看原图，旋转到文字正立后由教师确认；明显斜拍仍须重拍。
      </div>
      <div v-if="!recognizing" class="actions">
        <button
          v-if="recognitionCanGrade"
          type="button"
          class="btn btn-primary"
          :disabled="grading || !questions.length"
          @click="continueAfterRecognition"
        >
          继续开始批阅
        </button>
        <button type="button" class="btn btn-ghost" :disabled="grading" @click="startNewBatch">开始新批次</button>
      </div>
      <table v-if="papers.length" class="result-table">
        <thead><tr><th>#</th><th>文件</th><th>{{ anonymous ? '页面' : '学生' }}</th><th>匹配/题数</th><th>原图 / 处理</th></tr></thead>
        <tbody>
          <tr v-for="(p, i) in papers" :key="i">
            <td>{{ i + 1 }}</td>
            <td>{{ p.filename }}</td>
            <td>
              {{ p.student_name }}
              <span v-if="p.identity?.inherited_from" class="identity-inherit-note">沿用 {{ p.identity.inherited_from.filename }}</span>
              <span v-else-if="p.identity?.inherit_anchor" class="identity-inherit-note">待确认，随 {{ p.identity.inherit_anchor.filename }}</span>
            </td>
            <td>
              <span v-if="p.orientation_status && !orientationReady(p)" class="tag-pending">方向未通过：{{ orientationReasonText(p.orientation_reason) }}<span v-if="p.detected_tilt_degrees != null">（检测约 {{ Number(p.detected_tilt_degrees).toFixed(1) }}°）</span></span>
              <span v-else-if="p.orientation_status === 'teacher_confirmed'" class="tag-ok">教师已确认方向</span>
              <span v-else-if="paperOcrPending(p)" class="tag-pending">{{ paperOcrPendingLabel(p) }}</span>
              <span v-else-if="isExcludedPaper(p)" class="tag-muted">已排除，不参与本批批阅</span>
              <span v-else-if="p.error && p.requires_manual_review" class="tag-pending">需人工复核：{{ p.error }}</span>
              <span v-else-if="p.error" class="tag-fail">{{ p.error }}</span>
              <span v-else>{{ p.matched_count }} / {{ p.questions_count }}</span>
            </td>
            <td class="row-actions">
              <button v-if="p.paper_id" type="button" class="link-btn" @click="openRecognizePaperImage(p, 'original')">查看原图</button>
              <button v-if="p.paper_id && p.orientation_status && !orientationReady(p)" type="button" class="link-btn" :disabled="recognizing || orientationSaving" @click="openOrientationReview(p)">旋转并确认</button>
              <button v-if="canExcludeRecognitionPaper(p)" type="button" class="link-btn danger-link" :disabled="!!excludingPaperId" @click="excludeRecognitionPaper(p)">排除此卷</button>
              <span v-if="!p.paper_id">—</span>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!recognizing && !recognitionCanGrade" class="actions">
        <button
          v-if="recognitionResumeVisible"
          class="btn btn-primary"
          :disabled="recognizing || serviceBlocksNewWork"
          @click="resumeRecognitionBatch()"
        >
          继续处理未完成试卷
        </button>
        <button class="btn btn-primary" :disabled="recognizing || !fileList.length || recognitionBlocksNewWork" @click="runRecognize">
          {{ recognizing ? '识别中...' : '重新识别全部' }}
        </button>
      </div>
    </template>
  </div>
</template>

<script setup>
// The 批改 page's student papers card (right column): choosing the papers to grade (files, folders, phone
// photos, class submissions), the recognition results and the orientation and exclusion actions, and starting
// recognition and grading.  State and actions come from the page's ctx (useStudentFiles, useRecognitionRun, ...).
import { inject } from 'vue'
import { ArrowDown, ArrowRight, UploadFilled } from '@element-plus/icons-vue'
import GradingPhases from '@/components/GradingPhases.vue'
import GradingProgress from '@/components/GradingProgress.vue'
import PhoneCaptureDialog from '@/components/PhoneCaptureDialog.vue'
import StudentSubmissionDialog from '@/components/StudentSubmissionDialog.vue'
import SheetLoader from '@/components/SheetLoader.vue'
import { isExcludedPaper } from '@/utils/batchReadiness'
import { IMPORT_ACCEPT, IMPORT_FORMATS } from '@/utils/fileExtraction'
import HelpButton from '@/components/HelpButton.vue'
import {
  paperOcrPending,
  paperOcrPendingLabel,
  orientationReady,
  orientationReasonText,
} from '@/utils/recognitionStatus'
import { UPLOAD_PAGE } from './uploadPage.js'

const {
  activeStep, anonymous, serviceState, form, auth, isDragover, recognitionBlocksNewWork,
  imagePreprocessing, imageExtracting, onDrop, extractionProgress, triggerFileUpload,
  triggerFolderUpload, openPhoneCapture, openStudentSubmissions, phoneCaptureVisible, recognizing,
  importPhoneCaptures, studentSubmissionVisible, importStudentSubmissions, fileInputRef,
  onFileInputChange, folderInputRef, onFolderInputChange, showNameGuide, fileList, resetFiles,
  reset, inheritIdentity, grading, confirmedTemplateId, pendingTemplateId, templateDialogVisible,
  onStartRecognize, recognitionCanGrade, progressCardVisible, gradingPhaseState, recognized,
  currentMessage, recognitionDone, recognitionStats, recognitionError, recognizeUnresolvedCount,
  recognizeCompletedCount, recognizeSucceededCount, recognizeFailureCount,
  recognizeManualReviewCount, recognitionRecoveryState, recognitionRecoveryIsError, papers,
  questions, continueAfterRecognition, startNewBatch, openRecognizePaperImage, orientationSaving,
  openOrientationReview, canExcludeRecognitionPaper, excludingPaperId, excludeRecognitionPaper,
  recognitionResumeVisible, serviceBlocksNewWork, resumeRecognitionBatch, runRecognize,
} = inject(UPLOAD_PAGE)
</script>

<style scoped>
/* No styles of its own: this component shares Upload.vue's scope id (vite.config.js), so the page's
   scoped styles (upload/page.css, upload/cards.css) apply to it. */
</style>
