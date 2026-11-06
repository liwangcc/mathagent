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
      <div class="card template-card">
        <div class="card-title">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/><line x1="8" y1="7" x2="16" y2="7"/><line x1="8" y1="11" x2="13" y2="11"/></svg>
          答案依据
          <el-tag size="small" effect="plain">{{ templateStageLabel }}</el-tag>
          <HelpButton
            title="答案依据"
            content="必须先导入并确认模板。模板上的手写内容不会作为标准答案；标准答案始终由模型独立生成，教师确认后才用于批阅。"
          />
        </div>
        <div class="batch-setup" data-tour="grade-category">
          <div class="batch-setup-title">先确认本次批阅类型</div>
          <div class="batch-setup-fields">
            <label>
              <span>批次类型</span>
              <select v-model="form.category" class="form-select" :disabled="templateLocked">
                <option value="" disabled>请选择</option>
                <option value="daily">日常作业</option>
                <option value="exam">考试</option>
              </select>
            </label>
            <label v-if="form.category === 'exam'">
              <span>考试名称</span>
              <input v-model="form.exam_name" class="form-input" :disabled="templateLocked" placeholder="如：高一月考" />
            </label>
          </div>
          <div class="form-hint">
            {{ form.category === 'exam' ? '考试模板确认时填写每题/每小问分值。' : '日常作业只统计小问对错和正确率，不计算分数。' }}
          </div>
        </div>

        <!-- 模板上的手写痕迹由统一提示词处理，老师无需再选择模板类型。 -->

        <div class="tpl-column-mode">
          <label class="tpl-column-mode-label" for="template-column-mode">试卷栏数</label>
          <select
            id="template-column-mode"
            v-model="templateColumnMode"
            class="form-select"
            :disabled="templateLocked"
            aria-describedby="template-column-mode-help"
          >
            <option value="single">单栏</option>
            <option value="double">双栏</option>
            <option value="triple">三栏</option>
          </select>
          <HelpButton
            title="试卷栏数"
            content="正常试卷通常为双栏；单栏页面不做栏分割。栏数会在模板识别请求中同时作为 column_mode 和 layout_mode 发送，并在确认前锁定。"
          />
          <span id="template-column-mode-help" class="form-hint tpl-column-mode-hint">默认双栏。</span>
        </div>

        <!-- 已保存：可在学生试卷开始识别前再次编辑 -->
        <div v-if="confirmedTemplateId" class="tpl-confirmed" data-tour="template-import">
          <div class="tpl-confirmed-head">本次模板已保存（{{ templatePageCount }} 页）</div>
          <div class="template-hint">
            当前题干、答案和分值将作为本次批改依据；学生试卷开始识别前仍可再次编辑。
            <span v-if="templateBankImported">题目已一次性写入私库，后续模板修改不会重复入库。</span>
          </div>
          <div class="tpl-confirmed-actions">
            <button class="link-btn" @click="openTemplateEditor" :disabled="recognizing || grading || confirmingTemplate">再次编辑模板</button>
            <button class="link-btn" @click="resetTemplate" :disabled="recognizing || grading">重新导入模板</button>
          </div>
        </div>

        <!-- 未确认：导入 N 页。模板手写内容统一只作为差分豁免，标准答案由模型生成。 -->
        <template v-else>
          <div
            class="template-upload"
            data-tour="template-import"
            :class="{ 'is-disabled': templatePreprocessing || templateServiceBlocksNewWork }"
            :aria-busy="templatePreprocessing ? 'true' : undefined"
            :aria-disabled="templateServiceBlocksNewWork ? 'true' : undefined"
            @click="triggerTemplateUpload"
          >
            <SheetLoader v-if="templatePreprocessing" class="t-icon is-busy" :size="64" />
            <el-icon v-else class="t-icon"><UploadFilled /></el-icon>
            <div class="t-label">{{ templatePreprocessing ? '正在逐张处理模板图片' : '导入模板 N 页（有几页填几张）' }}</div>
          </div>
          <input ref="templateInputRef" type="file" :accept="IMPORT_ACCEPT" multiple style="display:none" :disabled="templatePreprocessing || templateServiceBlocksNewWork" @change="onTemplateFileChange" />
          <div class="upload-limit">支持 {{ IMPORT_FORMATS }}。模板最多 20 页；压缩包不超过 64MB，不支持加密或嵌套压缩包。</div>
          <div class="template-warn">请勿导入残缺模板。模板上的印刷内容、手写答案、演算或批注将由统一流程处理；标准答案会由模型生成，请在确认页核对。</div>
          <div v-if="templateFiles.length" class="file-summary">
            已选 {{ templateFiles.length }} 页
            <button class="link-btn" @click="templateFiles = []">清空</button>
          </div>
          <div class="actions">
            <button
              type="button"
              class="btn btn-primary"
              :disabled="!templateFiles.length || recognizingTemplate || templatePreprocessing || templateServiceBlocksNewWork"
              @click="onRecognizeTemplate"
            >
              {{ recognizingTemplate ? '识别中...' : '识别模板' }}
            </button>
          </div>
          <div
            v-if="templateServiceState.status !== 'available'"
            class="service-alert template-service-alert"
            :class="`service-alert-${templateServiceState.status}`"
            :role="templateServiceBlocksNewWork ? 'alert' : 'status'"
          >
            <strong>{{ templateServiceState.label }}</strong>
            <span>{{ templateServiceState.message || '模板识别链路当前不可用，请稍后再试。' }}</span>
          </div>
          <div v-if="templateRecognizeMessage" class="tpl-recognize-state" role="status"><InkSpinner v-if="recognizingTemplate" />{{ templateRecognizeMessage }}</div>
          <div v-if="templateRecognizeError" class="tpl-cut-preview-error" role="alert">{{ templateRecognizeError }}</div>
        </template>
      </div>

      <!-- 右：待批阅试卷 -->
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

    <!-- 原卷＋批阅结果：按教师已确认身份归组，多页保持上传顺序 -->
    <el-dialog
      v-model="manualDecisionDialogVisible"
      :class="['manual-review-dialog', { 'is-paper-hidden': !manualDecisionShowPaper }]"
      align-center
      :title="`原卷与批阅结果${manualDecisionRow?.student_name ? ` · ${manualDecisionRow.student_name}` : ''}`"
      width="min(1680px, 99vw)"
      :close-on-click-modal="!manualDecisionSaving"
      :close-on-press-escape="!manualDecisionSaving"
      @closed="resetManualDecisionImage"
    >
      <div class="manual-review-hint">
        {{ anonymous ? '左侧查看本页原图，右侧连续查看系统逐题结果。每张图片独立批阅，不与其他页面合并。' : '左侧原卷在可用宽高内整页显示，保持原始比例且不裁切；右侧连续滚动查看系统逐题结果。多页仅按教师已确认的学生身份归组，不按姓名或文件名猜测。' }}
      </div>
      <div class="manual-review-workspace" :class="{ 'is-paper-hidden': !manualDecisionShowPaper }">
        <aside class="manual-review-paper" :aria-label="anonymous ? '匿名页面原图' : '学生试卷原图'">
          <div class="manual-review-pager">
            <button type="button" class="btn btn-ghost" :disabled="manualDecisionPageIndex <= 0 || manualDecisionImageLoading" @click="changeManualDecisionPage(-1)">上一页</button>
            <strong>{{ manualDecisionPages.length ? manualDecisionPageIndex + 1 : 0 }} / {{ manualDecisionPages.length }}</strong>
            <button type="button" class="btn btn-ghost" :disabled="manualDecisionPageIndex >= manualDecisionPages.length - 1 || manualDecisionImageLoading" @click="changeManualDecisionPage(1)">下一页</button>
          </div>
          <div class="manual-review-image-stage">
            <div v-if="manualDecisionImageLoading" class="manual-review-paper-state"><InkSpinner />批阅痕迹图加载中…</div>
            <img v-else-if="manualDecisionImageUrl" :src="manualDecisionImageUrl" :alt="`${anonymous ? '匿名页' : (manualDecisionRow?.student_name || '学生')}试卷第${manualDecisionPageIndex + 1}页批阅痕迹图`" @click="openManualDecisionImage" />
            <div v-else class="manual-review-paper-state is-error">{{ manualDecisionImageError || '批阅图暂不可用' }}</div>
          </div>
          <div v-if="manualDecisionImageUrl" class="manual-review-image-actions">
            <button type="button" class="link-btn" @click="openManualDecisionImage">放大批阅图</button>
            <button type="button" class="link-btn" :disabled="reviewBusy" @click="downloadCurrentReviewImage">下载批阅痕迹图</button>
          </div>
        </aside>
        <div class="manual-review-list">
          <div class="manual-review-list-toolbar">
            <div>
              <strong>批阅详情</strong>
              <span>{{ manualDecisionDrafts.length }} 题 · 答题卡式核对</span>
            </div>
            <div class="manual-review-legend" aria-label="判定符号说明">
              <span><b class="manual-review-legend-symbol is-unread">·</b>未识别</span>
              <span><b class="manual-review-legend-symbol is-correct">✓</b>对</span>
              <span><b class="manual-review-legend-symbol is-partial">△</b>半对</span>
              <span><b class="manual-review-legend-symbol is-wrong">✗</b>错</span>
            </div>
            <button type="button" class="btn btn-ghost manual-review-paper-toggle" @click="manualDecisionShowPaper = !manualDecisionShowPaper">
              {{ manualDecisionShowPaper ? '关闭原卷' : '显示原卷' }}
            </button>
          </div>
          <div class="manual-review-sheet">
            <section v-if="manualDecisionCompactDrafts.length" class="manual-review-sheet-block manual-review-sheet-compact">
              <div class="manual-review-sheet-heading">
                <strong>选择 / 填空</strong>
                <span>紧凑核对</span>
              </div>
              <div class="manual-review-sheet-grid" :style="{ '--sheet-columns': manualDecisionCompactDrafts.length || 1 }">
                <div class="manual-review-sheet-label">题号</div>
                <button
                  v-for="item in manualDecisionCompactDrafts"
                  :key="`number-${item.paper_id}-${item.question_no}`"
                  type="button"
                  class="manual-review-sheet-cell manual-review-sheet-number"
                  @click="jumpToManualDecisionPage(item.page_index)"
                >{{ item.question_no }}</button>

                <div class="manual-review-sheet-label">学生答案</div>
                <button
                  v-for="item in manualDecisionCompactDrafts"
                  :key="`student-${item.paper_id}-${item.question_no}`"
                  type="button"
                  class="manual-review-sheet-cell manual-review-sheet-answer"
                  :class="{ 'is-pending': item.model_result === 'needs_review', 'is-current-page': item.page_index === manualDecisionPageIndex }"
                  @click="jumpToManualDecisionPage(item.page_index)"
                ><span v-html="renderAnswerLatex(item.student_answer || '—')"></span></button>

                <div class="manual-review-sheet-label">参考答案</div>
                <button
                  v-for="item in manualDecisionCompactDrafts"
                  :key="`correct-${item.paper_id}-${item.question_no}`"
                  type="button"
                  class="manual-review-sheet-cell manual-review-sheet-answer"
                  @click="jumpToManualDecisionPage(item.page_index)"
                ><span v-html="renderAnswerLatex(item.correct_answer || '—')"></span></button>

                <div class="manual-review-sheet-label">最终判定</div>
                <label
                  v-for="item in manualDecisionCompactDrafts"
                  :key="`result-${item.paper_id}-${item.question_no}`"
                  class="manual-review-sheet-cell manual-review-sheet-verdict"
                  :class="[{ 'is-pending': !item.result }, item.result ? `is-verdict-${item.result}` : '']"
                >
                  <select v-model="item.result" :aria-label="`第${item.question_no}题最终判定`" :disabled="!item.editable" @change="jumpToManualDecisionPage(item.page_index, true)" @click.stop>
                    <option value="">·</option>
                    <option value="correct">✓</option>
                    <option value="partial">△</option>
                    <option value="wrong">✗</option>
                  </select>
                </label>
              </div>
            </section>

            <section v-if="manualDecisionSolveDrafts.length" class="manual-review-sheet-block manual-review-sheet-solve">
              <div class="manual-review-sheet-heading">
                <strong>简答题</strong>
                <span>保留较大书写与参考空间</span>
              </div>
              <article
                v-for="item in manualDecisionSolveDrafts"
                :key="`${item.paper_id}-${item.question_no}`"
                class="manual-review-solve-row"
                :class="{ 'is-pending': item.model_result === 'needs_review', 'is-current-page': item.page_index === manualDecisionPageIndex }"
                @click="jumpToManualDecisionPage(item.page_index)"
              >
                <div class="manual-review-solve-head">
                  <button type="button" class="manual-review-solve-number" @click.stop="jumpToManualDecisionPage(item.page_index)">第 {{ item.question_no }} 题</button>
                  <span class="manual-review-type">简答题</span>
                  <label class="manual-review-solve-verdict" :class="item.result ? `is-verdict-${item.result}` : 'is-verdict-empty'">
                    <span>判定</span>
                    <select v-model="item.result" :aria-label="`第${item.question_no}题最终判定`" :disabled="!item.editable" @change="jumpToManualDecisionPage(item.page_index, true)" @click.stop>
                      <option value="">·</option>
                      <option value="correct">✓</option>
                      <option value="partial">△</option>
                      <option value="wrong">✗</option>
                    </select>
                  </label>
                </div>
                <div class="manual-review-solve-evidence">
                  <div><span>学生答案</span><p v-html="renderAnswerLatex(item.answer_kind === 'drawing' ? '作图作答，请查看下方图形证据或左侧原卷' : item.student_answer || '未识别到作答，请以左侧原卷为准')"></p><small v-if="item.error_type === 'ocr_slot_identity_invalid' || item.slot_identity_error" class="manual-review-diagnostic">模板作答位证据未通过校验，请核对模板作答位与左侧原卷；这不代表学生笔迹不清</small><small v-else-if="item.error_type === 'ocr_failed'" class="manual-review-diagnostic">识别处理未完成，请查看错误信息并核对左侧原卷；这不代表学生笔迹不清</small><small v-else-if="item.error_type === 'ocr_answer_alignment_suspected'" class="manual-review-diagnostic">题号对齐待核对：可能存在前后题偏移</small><small v-else-if="item.answer_kind !== 'drawing' && (item.error_type === 'ocr_unreadable' || item.answer_state === 'unreadable')" class="manual-review-diagnostic">原卷笔迹未能可靠识别，请以左侧原图为准</small></div>
                  <div><span>参考答案</span><p v-html="renderAnswerLatex(item.correct_answer || '未提供参考答案')"></p></div>
                </div>
                <DrawingAssessment v-if="item.drawing_assessment" :assessment="item.drawing_assessment" :text-assessment="item.text_assessment" />
              </article>
            </section>
          </div>
        </div>
      </div>
      <template #footer>
        <span class="dialog-safety-note">模型原始结论会保留在审计记录中。</span>
        <button class="btn btn-ghost" :disabled="manualDecisionSaving" @click="manualDecisionDialogVisible = false">取消</button>
        <button v-if="manualDecisionHasEditableRows" class="btn btn-primary" :disabled="manualDecisionSaving || !manualDecisionComplete" @click="saveManualDecision">
          {{ manualDecisionSaving ? '保存中...' : (anonymous ? '保存本页判定' : '保存该学生判定') }}
        </button>
      </template>
    </el-dialog>

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
    <el-dialog
      v-model="templateDialogVisible"
      title="编辑本次模板"
      width="min(1440px, 96vw)"
      class="template-editor-dialog"
      :close-on-click-modal="false"
      :close-on-press-escape="!templateEditorBusy"
      :show-close="!templateEditorBusy"
    >
      <fieldset class="tpl-editor-content" :disabled="templateEditorBusy" :inert="templateEditorBusy">
      <div class="tpl-confirm-hint">
        点击“保存模板”即确认当前显示的题号、框选、标准答案和标准图；统一保存所有页，无需逐页保存或另外确认。
        <details class="tpl-editor-help"><summary>定位与保存说明</summary>
          <p>红色为印刷题号，蓝色为作答定位，紫色豁免框用于去除模板笔迹，绿色为作图区；青色标准图用于固定参考。题号标记不要遮挡学生作答空间。</p>
          <p>标准图由教师按需手绘，可留空。手绘后按像素门和 Flash 双门判定；留空时按题干和学生作图区直接判断。</p>
          <p>保存只更新本批模板，学生卷开始识别前仍可编辑。“写入私库”是独立的一次性操作；标准答案由模型生成，请核对。</p>
        </details>
      </div>

      <section v-if="templateMaxFlagReviewEntries.length" class="tpl-answer-review" aria-label="Max 判定待核对" role="alert">
        <div v-for="entry in templateMaxFlagReviewEntries" :key="`max-${entry.pageIndex}-${entry.questionIndex}`" class="tpl-layout-review-alert tpl-answer-review-row">
          <strong>{{ templateMaxFlagNotice(entry.question) }}</strong>
          <button type="button" class="btn btn-ghost" @click="openTemplateMaxFlags(entry)">去处理</button>
          <button type="button" class="btn btn-ghost" @click="keepTemplateMaxFlags(entry.question)">确认保留</button>
        </div>
      </section>

      <section v-if="templatePendingRemovalEntries.length" class="tpl-answer-review" aria-label="保存时删除的题目" role="status">
        <div v-for="entry in templatePendingRemovalEntries" :key="`removal-${entry.pageIndex}-${entry.question.question_no}`" class="tpl-layout-review-alert tpl-answer-review-row">
          <strong>第 {{ entry.question.question_no }} 题的作答框和画图区已全部删除，保存模板时将删除这道题（不再批阅，其余题号不变）。如需保留，请重新框出作答位置。</strong>
        </div>
      </section>

      <section v-if="templateMissingAnswerEntries.length" class="tpl-answer-review" aria-label="待补充标准答案" role="status">
        <div v-for="entry in templateMissingAnswerEntries" :key="`${entry.pageIndex}-${entry.questionIndex}`" class="tpl-layout-review-alert tpl-answer-review-row">
          <strong>{{ templateMissingAnswerNotice(entry.question) }}</strong>
          <button type="button" class="btn btn-ghost" @click="openTemplateMissingAnswers(entry)">去处理</button>
        </div>
      </section>

      <section v-if="templateEditorProblemEntries.length" class="tpl-answer-review" aria-label="保存前需要调整" role="status">
        <div v-for="problem in templateEditorProblemEntries" :key="problem.key" class="tpl-layout-review-alert tpl-answer-review-row">
          <strong>{{ problem.message }}</strong>
          <button type="button" class="btn btn-ghost" @click="openTemplateProblem(problem)">去处理</button>
        </div>
      </section>

      <section class="tpl-cut-panel" aria-live="polite">
        <div>
          <strong>模板分栏与框选定位</strong>
        </div>
        <select v-model="templateCutImageType" class="form-select tpl-cut-image-type" :disabled="templateCutBusy" aria-label="教师模板图片来源">
          <option value="photo">手机拍照</option>
          <option value="scan">扫描图 / 电子导出</option>
        </select>
        <button type="button" class="btn btn-ghost" :disabled="templateCutBusy || !pendingTemplateId || templateServiceBlocksNewWork" @click="onRunTemplateCut({ force: true })">
          {{ templateCutBusy ? '自动裁剪中...' : '重新自动裁剪' }}
        </button>
        <div v-if="templateCutBusy" class="tpl-cut-status">
          <span>进度 {{ templateCutProgress.completed }}/{{ templateCutProgress.total }}</span>
          <span v-if="templateCutResult?.summary">候选 {{ templateCutResult.summary.total }} 个</span>
          <span v-if="templateCutResult?.summary?.original">原图坐标可信 {{ templateCutResult.summary.original }} 个</span>
          <span v-if="templateCutResult?.summary?.rectified_untrusted" class="is-warning">矫正坐标待核对 {{ templateCutResult.summary.rectified_untrusted }} 个</span>
        </div>
        <div v-if="templateCutPreviewError" class="tpl-cut-preview-error">{{ templateCutPreviewError }}</div>
        <div v-if="templateGeometryReview.needsReview" class="tpl-layout-review-alert" role="alert">
          模板定位仍需教师复核：{{ templateGeometryReview.summary }}
        </div>
        <div v-if="templateCutPreviewPages.length" class="tpl-cut-previews">
          <article v-for="page in templateCutPreviewPages" v-show="templatePreviewPageIndex() === Number(page.pageIndex)" :key="page.pageLabel" :id="`template-reference-page-${page.pageIndex}`" class="tpl-cut-preview-card">
            <header>
              <strong>第 {{ page.pageIndex + 1 }} 页</strong>
              <label class="tpl-column-count"><span>分栏</span>
                <select v-model.number="page.columnCount" class="form-select" :disabled="templatePageEditingLocked(page)" @change="resetTemplateColumns(page)">
                  <option :value="0" disabled>选择栏数</option><option :value="1">单栏</option><option :value="2">双栏</option><option :value="3">三栏</option>
                </select>
              </label>
              <span class="tpl-page-counts">{{ page.questionAnchors.length }} 个题号 · {{ templateVisibleAnswerBoxes(page).length }} 个作答框 · {{ page.drawingBoxes.length }} 个作图框</span>
              <span v-if="page.dirty" class="tag-pending">已调整 · 随模板保存</span>
              <span v-else-if="templatePageNeedsSave(page)" class="tag-pending">请核对本页定位</span>
              <details class="tpl-question-management">
                <summary>题目管理</summary>
                <div>
                  <p>以下操作增删整道题及答案，不是删除图上的框。普通框选请使用“删除所选”。</p>
                  <select v-model="page.editQuestionNumber" class="form-select" aria-label="管理题目" :disabled="templatePageEditingLocked(page)">
                    <option v-for="anchor in page.questionAnchors" :key="anchor.question_number" :value="anchor.question_number">{{ templatePreviewQuestionIdentity(page, anchor) }}</option>
                  </select>
                  <button type="button" class="btn btn-ghost" :disabled="templatePageEditingLocked(page) || !!templateReferenceQuestionEditBlockReason() || !page.editQuestionNumber" @click="insertTemplateQuestion(page, page.editQuestionNumber)">在前面插入题目</button>
                  <button type="button" class="btn btn-ghost" :disabled="templatePageEditingLocked(page) || !!templateReferenceQuestionEditBlockReason()" @click="insertTemplateQuestion(page, '')">末尾追加题目</button>
                  <button type="button" class="link-btn danger-link" :disabled="templatePageEditingLocked(page) || !!templateReferenceQuestionEditBlockReason() || !page.editQuestionNumber" @click="deleteTemplateQuestion(page, page.editQuestionNumber)">删除整道题</button>
                  <p v-if="page.questionSchema.some(isTemplateDirectoryLeaf)">层级 Q 对应独立作答项；由服务端校验，不自动合并不同层级。</p>
                  <p v-else>新增时请输入题号（仅数字和点）；其他题号保持不变，题干与答案随题移动。</p>
                  <p v-if="templateReferenceQuestionEditBlockReason()" role="status">{{ templateReferenceQuestionEditBlockReason() }}</p>
                </div>
              </details>
            </header>
            <div v-if="templateLayoutConflictNotice(page)" class="tpl-layout-review-alert" role="alert">
              {{ templateLayoutConflictNotice(page) }}
            </div>
            <div class="tpl-edit-context">
              <span class="tpl-pointer-hint" role="status">{{ templateGeometryHint(page) }}</span>
            </div>
            <p v-if="page.insertedPlaceholder" class="field-note tpl-insert-placeholder">新增题号为占位，请拖至正确位置；最后保存模板即可。</p>
            <template v-for="q in templateReferenceEditors(page)" :key="`reference-editor-${page.pageLabel}-${q ? q.question_no : 'none'}`">
            <p v-if="q" class="tpl-reference-caption">标准图 / 自定义作图判定 · {{ templateQuestionIdentity(q) }}</p>
            <DrawingReferenceEditor
              :ref="instance => q && setTemplateReferenceEditor(page, q, instance)"
              compact-controls
              shared-delete
              :model-value="q ? q.drawing_reference || null : null"
              :image-url="page.imageUrl"
              :drawing-regions="q ? templateDrawingReferenceRegionsFor(page, q) : []"
              :show-controls="!!q"
              :interactive="!!q && (page.previewEditMode === 'geometry' || (page.previewEditMode === 'reference' && String(page.referenceQuestionNumber) === String(q.question_no)))"
              :priority-editing="!!q && page.previewEditMode === 'reference' && String(page.referenceQuestionNumber) === String(q.question_no)"
              :disabled="templatePageEditingLocked(page)"
              canvas-class="tpl-cut-preview-stage"
              :canvas-visible="templateReferenceCanvasVisible(page, q)"
              @update:model-value="q && updateDrawingReference(q, $event)"
              @image-ready="q && markTemplateReferenceImageReady(page, q)"
              @selection-change="q && setTemplateStrokeSelection(page, $event)"
              @edit-request="q && selectTemplateDrawingReference(page, q.question_no)"
              @pointermove="moveTemplateGeometry"
              @pointerup="finishTemplateGeometry"
              @pointercancel="cancelTemplateGeometry"
            >
              <template #geometry-tools>
                <div class="tpl-add-tools" role="group" aria-label="新增定位标记">
                  <span>新增标记：</span>
                  <button v-for="tool in TEMPLATE_GEOMETRY_TOOLS" :key="tool.key" type="button" class="tpl-tool-btn" :class="`is-${tool.key}`" :aria-pressed="page.geometryTool === tool.key" :disabled="templatePageEditingLocked(page)" @click="armTemplateGeometryTool(page, tool.key)">{{ tool.label }}</button>
                  <button type="button" class="tpl-tool-btn tpl-delete-selected" :disabled="templatePageEditingLocked(page) || (!page.geometrySelection && !page.referenceStrokeSelected)" @click="deleteTemplateSelection(page)">删除所选</button>
                </div>
              </template>
              <template #geometry-labels>
                <TemplateGeometryLegend :anchors="page.questionAnchors" :answers="templateVisibleAnswerBoxes(page)" :drawings="page.drawingBoxes" :labels="page.drawingLabels" :selected="page.geometrySelection" :disabled="templatePageEditingLocked(page)" :hidden="!!page.geometryOverlayHidden" @toggle="page.geometryOverlayHidden = !page.geometryOverlayHidden" @select="page.geometryOverlayHidden = false; selectTemplateGeometryTarget(page, $event)" />
                <section v-if="templateSlotBoxes(page).length" class="tpl-slot-order" aria-label="同题作答槽顺序核对">
                  <strong>作答槽顺序与标准答案</strong>
                  <p>点编号定位；“调换位置”只交换物理框，不移动对应标准答案。保存模板后按此顺序复用。</p>
                  <div v-for="box in templateSlotBoxes(page)" :key="box.slot_id" class="tpl-slot-row" :data-template-slot-row="box.slot_id" :class="{ 'is-missing-answer': templateSlotNeedsAnswer(page, box) || templateSlotMaxFlagged(page, box) || templateProblemBoxes.has(box), 'is-selected': page.geometrySelection?.target === box }">
                    <button type="button" :disabled="templatePageEditingLocked(page)" @click="locateTemplateReviewSlot(page, box)">Q{{ box.slot_id }}{{ box.bbox.length !== 4 ? ' 待框选' : '' }}</button>
                    <span>{{ templateSlotAnswer(page, box) }}</span>
                    <button v-if="box.bbox.length === 4" type="button" :disabled="templatePageEditingLocked(page) || !!page.slotContractError" @click="correctTemplateSlotPosition(page, box)">调换位置</button>
                  </div>
                </section>
                <p v-if="page.slotContractError" role="alert">{{ page.slotContractError }}</p>
              </template>
              <template #canvas-overlays>
              <div v-show="!page.geometryOverlayHidden" class="tpl-reference-geometry" :class="{ 'is-disabled': templatePageEditingLocked(page) || (page.previewEditMode === 'reference' && String(page.referenceQuestionNumber) === String(q?.question_no)) }" @pointerdown.self="clearTemplateSelection(page)">
              <div v-for="column in page.columns" :key="`column-${column.column_index}`" class="tpl-column-overlay" :class="`is-column-${column.column_index}`" :style="templateBoxStyle([column.x1_1000, 0, column.x2_1000, 1000])">
                <span>第{{ column.column_index }}栏</span>
              </div>
              <button v-for="(column, index) in page.columns.slice(0, -1)" :key="`divider-${index}`" type="button" class="tpl-column-divider" :class="{ 'is-selected': page.geometrySelection?.kind === 'divider' && page.geometrySelection?.index === index }" :style="{ left: `${column.x2_1000 / 10}%` }" :aria-label="`调整第 ${index + 1} 条分栏线`" @pointerdown.stop="startTemplateDividerMove($event, page, index)"></button>
              <template v-for="(anchor, index) in page.questionAnchors" :key="`question-anchor-${anchor.question_number}-${index}`">
                <div v-if="templateQuestionAnchorBBox(anchor).length === 4" class="tpl-question-anchor" :class="{ 'is-selected': page.geometrySelection?.target === anchor }" :style="templateBoxStyle(templateQuestionAnchorBBox(anchor))" @pointerdown.stop="startTemplateQuestionMove($event, page, anchor)">

                  <i class="tpl-handwriting-resize" aria-hidden="true" @pointerdown.stop="startTemplateQuestionResize($event, page, anchor)"></i>
                </div>
              </template>
              <div v-for="(box, index) in templateVisibleAnswerBoxes(page)" :key="`answer-region-${box.question_number}-${index}`" class="tpl-handwriting-region" :class="{ 'is-label-anchor': box.geometry_kind === 'label_anchor', 'is-selected': page.geometrySelection?.target === box, 'is-missing-answer': templateSlotNeedsAnswer(page, box) || templateSlotMaxFlagged(page, box) || templateProblemBoxes.has(box) }" :data-template-slot-id="box.slot_id" :style="templateBoxStyle(box.bbox)" :title="`Q${box.slot_id || box.question_number} 作答框；拖动移动，右下角调整大小`" @pointerdown.stop="startTemplateAnswerMove($event, page, box)">
                <i v-if="box.geometry_kind !== 'label_anchor'" class="tpl-handwriting-resize" aria-hidden="true" @pointerdown.stop="startTemplateAnswerResize($event, page, box)"></i>
              </div>
              <div v-for="(box, index) in page.drawingBoxes" :key="`drawing-region-${box.question_number}-${index}`" class="tpl-drawing-region" :class="{ 'is-selected': page.geometrySelection?.target === box, 'is-missing-answer': (box.slot_id && templateSlotMaxFlagged(page, box)) || templateProblemBoxes.has(box) }" :data-template-slot-id="box.slot_id || undefined" :style="templateBoxStyle(box.bbox)" :title="`Q${box.question_number} 作图区域（同时用于豁免与定位）`" @pointerdown.stop="startTemplateGeometryGesture($event, page, box, 'drawing-move')">

                <i class="tpl-handwriting-resize" aria-hidden="true" @pointerdown.stop="startTemplateGeometryGesture($event, page, box, 'drawing-resize')"></i>
              </div>
              <button v-for="label in page.drawingLabels" :key="`drawing-label-${label.question_number}`" type="button" class="tpl-drawing-label" :class="{ 'is-selected': page.geometrySelection?.target === label }" :style="{ left: `${label.label_anchor[0] / 10}%`, top: `${label.label_anchor[1] / 10}%` }" :aria-label="`拖动 Q${label.question_number} 到作图区域左侧`" @pointerdown.stop="startTemplateDrawingLabelMove($event, page, label)"></button>
              <div v-if="page.geometryTool" class="tpl-draw-surface" :aria-label="`在原图上新增${templateGeometryToolLabel(page.geometryTool)}`" @pointerdown.stop="startTemplateGeometryCreation($event, page)">
                <div v-if="page.geometryDraft?.length === 4" class="tpl-draw-draft" :style="templateBoxStyle(page.geometryDraft)"></div>
              </div>
              </div>
              </template>
            </DrawingReferenceEditor>
            </template>
          </article>
        </div>
      </section>

      <section v-if="templateChoiceEntries.length" class="tpl-answer-section">
        <div class="tpl-answer-section-head">
          <div>
            <strong>选择题答题卡</strong>
            <span>{{ templateChoiceEntries.length }} 题 · 单选 {{ templateChoiceSingleCount }} · 多选 {{ templateChoiceMultipleCount }}<em v-if="templateChoicePendingCount"> · 待处理 {{ templateChoicePendingCount }}</em></span>
          </div>
          <div class="tpl-answer-actions">
            <button type="button" class="link-btn" @click="setTemplateDetailOpen('choice', true)">展开详情</button>
            <button type="button" class="link-btn" @click="setTemplateDetailOpen('choice', false)">收起详情</button>
          </div>
        </div>
        <div v-if="form.category === 'exam'" class="tpl-batch-score">
          <label class="form-label">选择题统一分值</label>
          <input v-model.number="templateChoiceScore" class="form-input" type="number" min="0.5" max="1000" step="0.5" aria-label="选择题统一分值" />
          <span>分</span>
          <span class="field-note">点击下方保存并冻结时自动应用</span>
        </div>
        <div class="tpl-answer-grid" aria-label="选择题答题卡">
          <div
            v-for="entry in templateChoiceEntries"
            :key="`choice-${entry.pageIndex}-${entry.questionIndex}`"
            class="tpl-answer-cell tpl-choice-answer-cell"
            :class="{ 'is-unresolved': !choiceContractValid(entry.question) }"
          >
            <div class="tpl-answer-cell-head">
              <button type="button" class="tpl-answer-card-focus" @click="openTemplateQuestion(entry)">
                <span>{{ templateQuestionIdentity(entry.question) }} <em v-if="templatePages.length > 1">P{{ entry.pageIndex + 1 }}</em></span>
              </button>
              <span class="tpl-choice-mode-badge" :class="{ 'is-pending': entry.question.choice_mode === 'unknown' }">
                {{ choiceModeLabel(entry.question.choice_mode) }}
              </span>
            </div>
            <div v-if="entry.question.choice_mode === 'single'" class="tpl-choice-options" role="radiogroup" :aria-label="`第 ${entry.question.question_no} 题单选答案`">
              <button
                v-for="option in choiceOptions"
                :key="option"
                type="button"
                class="tpl-choice-option"
                :class="{ 'is-selected': entry.question.standard_answer === option }"
                :aria-pressed="String(entry.question.standard_answer === option)"
                @click.stop="setSingleChoiceAnswer(entry.question, option)"
              >
                {{ option }}
              </button>
            </div>
            <label v-else-if="entry.question.choice_mode === 'multiple'" class="tpl-choice-multiple-editor">
              <span>多选</span>
              <input
                :value="entry.question.standard_answer"
                class="form-input tpl-choice-answer-input"
                :aria-label="`第 ${entry.question.question_no} 题多选答案`"
                maxlength="4"
                inputmode="text"
                autocomplete="off"
                spellcheck="false"
                placeholder="AC"
                @click.stop
                @input="onChoiceAnswerInput(entry.question, $event)"
                @change="onChoiceAnswerChange(entry.question)"
              />
            </label>
            <div v-else class="tpl-choice-mode-picker" role="group" :aria-label="`第 ${entry.question.question_no} 题选择类型`">
              <button type="button" class="tpl-choice-mode-button" @click.stop="setTemplateChoiceMode(entry.question, 'single')">单选</button>
              <button type="button" class="tpl-choice-mode-button" @click.stop="setTemplateChoiceMode(entry.question, 'multiple')">多选</button>
            </div>
            <span v-if="!choiceContractValid(entry.question)" class="tpl-answer-status">待处理</span>
          </div>
        </div>
      </section>

      <section v-if="templateFillEntries.length" class="tpl-answer-section">
        <div class="tpl-answer-section-head">
          <div>
            <strong>填空题答题卡</strong>
            <span>{{ templateFillEntries.length }} 题</span>
          </div>
          <div class="tpl-answer-actions">
            <button type="button" class="link-btn" @click="setTemplateDetailOpen('fill', true)">展开详情</button>
            <button type="button" class="link-btn" @click="setTemplateDetailOpen('fill', false)">收起详情</button>
          </div>
        </div>
        <div v-if="form.category === 'exam' && templateFillBatchEntryCount" class="tpl-batch-score">
          <label class="form-label">填空题统一分值</label>
          <input v-model.number="templateFillScore" class="form-input" type="number" min="0.5" max="1000" step="0.5" aria-label="填空题统一分值" />
          <span>分</span>
          <span class="field-note">点击下方保存并冻结时自动应用</span>
        </div>
        <p v-if="form.category === 'exam' && templateCustomFillCount" class="tpl-custom-score-note">其中 {{ templateCustomFillCount }} 道多空填空使用自定义各空分值，保留在详细内容中单独设置。</p>
        <div class="tpl-answer-grid" aria-label="填空题答题卡">
          <div v-for="entry in templateFillEntries" :key="`fill-${entry.pageIndex}-${entry.questionIndex}`" class="tpl-answer-cell tpl-fill-answer-cell">
            <button type="button" class="tpl-answer-card-focus" @click="openTemplateQuestion(entry)">
              <span>{{ templateQuestionIdentity(entry.question) }} <em v-if="templatePages.length > 1">P{{ entry.pageIndex + 1 }}</em></span>
            </button>
            <input
              v-if="!Object.hasOwn(entry.question, 'region_kinds') && (Number(entry.question.blank_count) || 1) <= 1"
              v-model="entry.question.standard_answer"
              class="form-input tpl-fill-answer-input"
              :aria-label="`第 ${entry.question.question_no} 题填空答案`"
              @click.stop
              @input="markTemplateAnswerManualEdit(entry.question)"
            />
            <div v-else class="tpl-fill-slot-list">
              <label v-for="(_, slotIndex) in templateFillAnswerSlots(entry.question)" :key="slotIndex" class="tpl-fill-slot">
                <span>{{ slotIndex + 1 }}{{ entry.question.region_kinds?.[slotIndex] === 'drawing' ? ' 作图' : '' }}</span>
                <input
                  :value="templateFillAnswerSlots(entry.question)[slotIndex]"
                  :disabled="entry.question.region_kinds?.[slotIndex] === 'drawing'"
                  :placeholder="entry.question.region_kinds?.[slotIndex] === 'drawing' ? '作图' : ''"
                  class="form-input tpl-fill-answer-input"
                  :aria-label="`第 ${entry.question.question_no} 题第 ${slotIndex + 1} 空答案`"
                  @click.stop
                  @change="onTemplateFillSlotChange(entry.question, slotIndex, $event)"
                />
              </label>
            </div>
          </div>
        </div>
      </section>

      <section v-if="templateSolveEntries.length" class="tpl-answer-section">
        <div class="tpl-answer-section-head">
          <div>
            <strong>简答题答题卡</strong>
            <span>{{ templateSolveEntries.length }} 题 · 点击题目滚动到下方详细核对</span>
          </div>
          <div class="tpl-answer-actions">
            <button type="button" class="link-btn" @click="setTemplateDetailOpen('solve', true)">展开详情</button>
            <button type="button" class="link-btn" @click="setTemplateDetailOpen('solve', false)">收起详情</button>
          </div>
        </div>
        <div class="tpl-answer-grid" aria-label="简答题答题卡">
          <div v-for="entry in templateSolveEntries" :key="`solve-${entry.pageIndex}-${entry.questionIndex}`" class="tpl-answer-cell tpl-solve-answer-cell">
            <button type="button" class="tpl-answer-card-focus" @click="openTemplateQuestion(entry)">
              <span>{{ templateQuestionIdentity(entry.question) }} <em v-if="templatePages.length > 1">P{{ entry.pageIndex + 1 }}</em></span>
            </button>
            <span class="tpl-solve-answer-preview">{{ templateAnswerText(entry.question) }}</span>
          </div>
        </div>
      </section>

      <div v-for="(page, pi) in templatePages" :key="pi" class="tpl-page">
        <div class="tpl-page-title">
          第 {{ page.page_index + 1 }} 页（{{ page.questions.length }} 题）
          <span v-if="templateCutPageOriginalCount(page.page_index)" class="tpl-cut-page-badge">已定位 {{ templateCutPageOriginalCount(page.page_index) }} 个原图题块</span>
        </div>
        <div v-for="(q, qi) in page.questions" :id="templateQuestionDomId(pi, qi)" :key="qi" class="tpl-q" :class="{ 'is-collapsed': !q._open }">
          <div class="tpl-q-head">
            <button type="button" class="tpl-q-toggle" :aria-expanded="String(!!q._open)" @click="q._open = !q._open">
              <span class="ci-no tpl-q-identity">{{ templateQuestionIdentity(q) }}</span>
              <span class="tpl-q-answer-preview">{{ templateAnswerText(q) }}</span>
            </button>
            <div class="tpl-q-head-actions">
              <select v-model="q.question_type" class="form-select tpl-q-type" @click.stop @change="onQuestionTypeChange(q); markTemplateQuestionInput(q)">
                <option value="choice">选择</option>
                <option value="fill">填空</option>
                <option value="solve">解答</option>
              </select>
              <select
                :value="q.answer_kind || 'non_graphic'"
                class="form-select tpl-q-type"
                :aria-label="`第 ${q.question_no} 题作图属性`"
                @click.stop
                @change.stop="setTemplateAnswerDrawingKind(page.page_index, q, $event.target.value); $event.target.value = q.answer_kind || 'non_graphic'"
              >
                <option value="non_graphic">非作图</option>
                <option value="drawing">作图题</option>
                <option value="mixed">图文混合</option>
              </select>
              <span v-if="q.question_type === 'choice'" class="tpl-choice-mode-badge tpl-q-mode-badge" :class="{ 'is-pending': q.choice_mode === 'unknown' }">
                {{ choiceModeLabel(q.choice_mode) }}
              </span>
              <span v-if="q.correction_candidates?.length" class="correction-discovery-badge" :class="{ 'is-excluded': q.correction_excluded }">
                {{ q.correction_excluded ? '已排除纠错' : `可纠错 ${q.correction_candidates.length} 项` }}
              </span>
              <button type="button" class="link-btn" @click="q._open = !q._open">{{ q._open ? '收起详情' : '查看详情' }}</button>
            </div>
          </div>
          <div v-show="q._open" class="tpl-q-details">
            <p v-if="isTemplateDirectoryLeaf(q)" class="field-note">当前 Q 下的作答区域按目录排列，区域数不从题干重新推断。</p>
            <div v-if="q.question_type === 'choice'" class="ci-field scoring-panel">
              <label class="form-label">选择类型</label>
                <select v-model="q.choice_mode" class="form-select" aria-label="选择题类型" @change="markTemplateQuestionInput(q)">
                <option value="unknown">待判型</option>
                <option value="single">单选题</option>
                <option value="multiple">多选题</option>
              </select>
            </div>
            <div v-if="q.question_type === 'fill'" class="ci-field scoring-panel">
              <label class="form-label">填空数量</label>
              <input v-model.number="q.blank_count" class="form-input" type="number" min="1" max="20" step="1" :disabled="isTemplateDirectoryLeaf(q) || Object.hasOwn(q, 'region_kinds')" @change="onBlankCountChange(q)" />
            </div>
            <div v-if="Object.hasOwn(q, 'slot_ids') || Object.hasOwn(q, 'region_kinds') || (q.question_type === 'fill' && Number(q.blank_count) > 1)" class="ci-field scoring-panel">
              <label class="form-label">各空标准答案 <span class="field-note">{{ Object.hasOwn(q, 'region_kinds') ? '作图位不填文字；整题共用一个总分' : '每行一个；整题共用一个总分' }}</span></label>
              <div v-if="Object.hasOwn(q, 'slot_ids') || Object.hasOwn(q, 'region_kinds')" class="tpl-fill-slot-list">
                <label v-for="(_, slotIndex) in templateFillAnswerSlots(q)" :key="slotIndex" class="tpl-fill-slot" :class="{ 'is-missing-answer': templateMissingAnswerSlots(q).some(slot => slot.index === slotIndex) }">
                  <span>{{ templateSlotLabel(q, slotIndex) }}{{ q.region_kinds?.[slotIndex] === 'drawing' ? ' 作图' : '' }}</span>
                  <input
                    :value="templateFillAnswerSlots(q)[slotIndex]"
                    :disabled="q.region_kinds?.[slotIndex] === 'drawing'"
                    :placeholder="q.region_kinds?.[slotIndex] === 'drawing' ? '作图' : ''"
                    class="form-input tpl-fill-answer-input"
                    :aria-label="`第 ${q.question_no} 题第 ${slotIndex + 1} 空答案`"
                    @change="onTemplateFillSlotChange(q, slotIndex, $event)"
                  />
                </label>
              </div>
              <textarea v-else v-model="q.answer_slots_text" class="form-textarea" :rows="Math.min(6, Number(q.blank_count) || 2)" placeholder="第1空答案&#10;第2空答案" @input="onTemplateAnswerSlotsInput(q)"></textarea>
            </div>
            <div v-if="form.category === 'exam' && q.question_type === 'fill' && Number(q.blank_count) > 1 && !q.region_kinds?.includes('drawing')" class="ci-field scoring-panel">
              <label class="form-label">多空计分方式</label>
              <select v-model="q.fill_scoring_mode" class="form-select" @change="onFillScoringModeChange(q)">
                <option value="equal">按空均分</option>
                <option value="all_or_nothing">整题给分（全部空正确）</option>
                <option value="custom">自定义各空分值</option>
              </select>
              <div v-if="isCustomFill(q)" class="subscore-grid fill-score-grid">
                <label v-for="(_, si) in q.blank_scores" :key="si" class="subscore-item">
                  <span>第{{ si + 1 }}空</span>
                  <input v-model.number="q.blank_scores[si]" class="form-input" type="number" min="0.1" max="1000" step="0.5" @change="onBlankScoresChange(q)" />
                </label>
                <span class="fill-score-summary">本题总分 {{ fillScoreTotal(q).toFixed(1) }} 分</span>
              </div>
              <p v-else-if="q.fill_scoring_mode === 'all_or_nothing'" class="field-note">只有所有空都正确才获得本题总分。</p>
            </div>
            <div v-if="form.category === 'exam' && (q.question_type === 'choice' || (q.question_type === 'fill' && !isCustomFill(q)))" class="ci-field scoring-panel tpl-uniform-score-note">
              <label class="form-label">统一分值</label>
              <p>本题 {{ Number(q.max_score || 0) }} 分；请在上方答题卡区域批量设置。</p>
            </div>
            <div v-if="q.question_type === 'solve' && form.category !== 'exam'" class="ci-field scoring-panel">
              <label class="form-label">独立小问数量 <span class="field-note">分别统计对错，不计算分数</span></label>
              <input v-model.number="q.subquestion_count" class="form-input" type="number" min="1" max="20" step="1" :disabled="isTemplateDirectoryLeaf(q)" />
            </div>
            <div v-if="q.question_type === 'solve' && form.category === 'exam'" class="ci-field scoring-panel">
              <label class="form-label">{{ isTemplateDirectoryLeaf(q) ? '当前 Q 分值' : '各小问分值' }} <span v-if="!isTemplateDirectoryLeaf(q)" class="field-note">默认 5 / 7 / 8；0 表示没有该小问</span></label>
              <div class="subscore-grid">
                <div v-for="(_, si) in q.subquestion_scores" :key="si" class="subscore-item" :class="{ 'is-inactive-subscore': Number(q.subquestion_scores[si]) <= 0 }">
                  <span>（{{ si + 1 }}）<em v-if="Number(q.subquestion_scores[si]) <= 0" class="subscore-status">未计入评分</em></span>
                  <input v-model.number="q.subquestion_scores[si]" class="form-input" type="number" min="0" max="1000" step="0.5" :aria-label="`第 ${si + 1} 小问分值`" />
                  <button v-if="q.subquestion_scores.length > 1" type="button" class="subscore-remove" :aria-label="`删除第 ${si + 1} 小问`" @click="removeSubquestionScore(q, si)">×</button>
                </div>
                <button v-if="!isTemplateDirectoryLeaf(q) && q.subquestion_scores.length < 20" type="button" class="link-btn" @click="addSubquestionScore(q)">+ 增加小问</button>
              </div>
            </div>
            <div class="ci-field">
              <label class="form-label">题干</label>
              <textarea v-model="q.question_text" class="form-textarea" rows="2" placeholder="请输入题干" @input="markTemplateQuestionInput(q)"></textarea>
            </div>
            <div class="tpl-q-pair">
              <div class="ci-field">
                <label class="form-label">标准答案</label>
                <textarea v-model="q.standard_answer" class="form-textarea tpl-answer-autogrow" rows="2" placeholder="请输入标准答案" @input="markTemplateAnswerManualEdit(q)"></textarea>
              </div>
              <div class="ci-field">
                <label class="form-label">等价答案 <span class="field-note">每行一个，可留空</span></label>
                <textarea v-model="q.answer_variants_text" class="form-textarea" rows="2" placeholder="如：2.0&#10;2/1"></textarea>
              </div>
            </div>
            <div v-if="['drawing', 'mixed'].includes(q.answer_kind)" class="ci-field drawing-reference-summary">
              <span>{{ templateDrawingReferenceSummary(q) }}</span>
              <button type="button" class="link-btn" @click="openTemplateDrawingReference(page.page_index, q.question_no)">到上方模板核对作图区域与标准图</button>
            </div>
            <div class="ci-field">
              <label class="form-label">解题步骤 / 评分依据 <span class="field-note">每行一个，详细批改与错题条使用</span></label>
              <textarea v-model="q.solution_steps_text" class="form-textarea" rows="3" placeholder="填写关键步骤或扣分依据" @input="markTemplateAnswerManualEdit(q, 'solution_steps_text')"></textarea>
            </div>
            <div v-if="q.correction_candidates?.length" class="ci-field scoring-panel correction-review-panel" :class="{ 'is-excluded': q.correction_excluded }">
              <div class="correction-review-head">
                <div>
                  <label class="form-label">发现可纠错题目</label>
                  <p class="field-note">保存模板即同意建立下列关系；学生本次整题做对后，关闭该学生对应的历史错题。</p>
                </div>
                <button type="button" class="btn btn-ghost correction-decision-btn" @click="toggleQuestionCorrection(q)">
                  {{ q.correction_excluded ? '恢复纠错' : '不纠错' }}
                </button>
              </div>
              <div class="correction-current-question">
                <span>本次题目</span>
                <strong>{{ q.question_text || '题干待补充' }}</strong>
              </div>
              <div class="correction-history-list">
                <div v-for="candidate in q.correction_candidates" :key="candidate.cycle_id" class="correction-history-row">
                  <div class="correction-history-meta">
                    <strong>{{ candidate.student_name || `学生 #${candidate.student_id}` }}</strong>
                    <span v-if="candidate.student_no">学号 {{ candidate.student_no }}</span>
                    <span>{{ candidate.relation === 'same_original' ? '同一原题' : '相似题' }}</span>
                    <span>{{ candidate.knowledge_point || '知识点未标注' }}</span>
                  </div>
                  <p><span>历史错题</span>{{ candidate.historical_question || '历史题干缺失' }}</p>
                </div>
              </div>
              <p v-if="q.correction_excluded" class="correction-excluded-note">此题及上列历史错题已从本模板纠错关系中排除。</p>
            </div>
            <p v-if="q.correction_knowledge_summary" class="field-note correction-knowledge-note">
              同知识点「{{ q.correction_knowledge_summary.topic }}」：本班 {{ q.correction_knowledge_summary.student_count }} 名学生仍有 {{ q.correction_knowledge_summary.record_count }} 道错题未纠正。同知识点的不同题目做对不会关闭这些错题。
            </p>
          </div>
        </div>
      </div>
      </fieldset>
      <template #footer>
        <nav v-if="templateCutPreviewPages.length > 1" class="tpl-page-tabs" aria-label="模板页切换">
          <button type="button" class="tpl-tool-btn" :disabled="templateEditorBusy || templatePreviewPagePosition() <= 0" @click="selectTemplatePreviewPage(templateCutPreviewPages[templatePreviewPagePosition() - 1])">上一页</button>
          <select :value="templatePreviewPageIndex()" class="form-select" aria-label="当前模板页" :disabled="templateEditorBusy" @change="selectTemplatePreviewPage(templateCutPreviewPages.find(page => Number(page.pageIndex) === Number($event.target.value)))">
            <option v-for="page in templateCutPreviewPages" :key="page.pageLabel" :value="page.pageIndex">第 {{ page.pageIndex + 1 }} 页{{ page.dirty ? ' · 已调整' : '' }}</option>
          </select>
          <button type="button" class="tpl-tool-btn" :disabled="templateEditorBusy || templatePreviewPagePosition() >= templateCutPreviewPages.length - 1" @click="selectTemplatePreviewPage(templateCutPreviewPages[templatePreviewPagePosition() + 1])">下一页</button>
        </nav>
        <span class="tpl-save-note" role="status">{{ confirmingTemplate ? '正在保存全部页…' : '各页修改保留，一起保存' }}</span>
        <button class="btn btn-ghost" @click="cancelTemplateDialog" :disabled="templateEditorBusy">取消</button>
        <button class="btn btn-ghost" @click="onImportTemplateBank" :disabled="confirmingTemplate || importingTemplateBank || templateCutBusy || templateBankImported || !templateCanConfirm">
          {{ templateBankImported ? '已写入私库' : (importingTemplateBank ? '入库中...' : '写入私库（一次）') }}
        </button>
        <button class="btn btn-primary" @click="onConfirmTemplate" :disabled="confirmingTemplate || importingTemplateBank || templateCutBusy">
          {{ confirmingTemplate ? '保存中...' : '保存模板（全部页）' }}
        </button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { onBeforeRouteLeave, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { notifyProcessingError } from '@/utils/errorNotification'
import { ArrowDown, ArrowRight, UploadFilled } from '@element-plus/icons-vue'
import { renderAnswerLatex } from '@/utils/latex'
import AnimatedNumber from '@/components/AnimatedNumber.vue'
import GradingPhases from '@/components/GradingPhases.vue'
import GradingProgress from '@/components/GradingProgress.vue'
import GradingQuickDock from '@/components/GradingQuickDock.vue'
import InkSpinner from '@/components/InkSpinner.vue'
import OutcomeDonut from '@/components/OutcomeDonut.vue'
import PhoneCaptureDialog from '@/components/PhoneCaptureDialog.vue'
import StudentSubmissionDialog from '@/components/StudentSubmissionDialog.vue'
import SheetLoader from '@/components/SheetLoader.vue'
import VerdictSwatch from '@/components/VerdictSwatch.vue'
import { forgetPhoneCaptureSession, loadPhoneCaptureSession } from '@/utils/phoneCaptureSession'
import api from '@/api'
import {
  countGradablePapers,
  isExcludedPaper,
  mergeRecognitionPaper,
  normalizeBatchReadiness,
  validateGradeCompletion,
} from '@/utils/batchReadiness'
import { gradingPhases } from '@/utils/gradingPhases'
import { formatGradingDuration } from '@/utils/gradingTimer'
import {
  consumeSse,
  createSseEventError,
  createSseResponseError,
  isSseRequestCancelled,
  isSseServiceFault,
  reportSseServiceFault,
  SseTransportError,
} from '@/utils/sse'
import {
  validateUploadList,
  compressImageFiles,
  createRotatedOcrReadyFile,
} from '@/utils/uploadValidation'
import {
  extractImagesFromDrop,
  extractImagesFromFileList,
  IMPORT_ACCEPT,
  IMPORT_FORMATS,
} from '@/utils/fileExtraction'
import { appendDesktopCanonicalCandidates } from '@/utils/desktopTemplateCanonicalization'
import { appendStudentUploadFiles } from '@/utils/uploadSource'
import { setRouteBusy } from '@/utils/routeBusy'
import { CLASSIFICATION_LABELS } from '@/utils/knowledgeClassification'
import {
  buildIdentityResolutionItems,
  identityResolutionsReady,
  mergeRosterIntoIdentityItems,
  serializeIdentityResolutions,
} from '@/utils/identityResolution'
import {
  applyUniformTemplateScore,
  choiceContractValid,
  templateIsPureDrawing,
  normalizeChoiceAnswer,
  normalizeChoiceInput,
  setTemplateFillAnswerSlot,
  templateAnswerText,
  templateChoiceConflictEntries,
  templateChoiceConflictReason,
  templateChoiceReviewEntries,
  templateFillAnswerSlots,
  templateMissingAnswerSlots,
  templateMissingAnswerNotice,
  templateMaxFlagEntries,
  templateMaxFlagNotice,
  keepTemplateMaxFlags,
  templateRegionSlotsValid,
  templateReviewEntries,
  templateQuestionIdentity,
  templateQuestionMetadata,
  templateDrawingReference,
  isTemplateDirectoryLeaf,
  templateDrawingReview,
  serializeTemplateDrawingReview,
  mergeTemplateDrawingCandidates,
  defaultTemplateDrawingLabel,
  templateBoxSizeProblems,
  templateReferenceLeavesRegions,
  templateChoiceConflictReasons,
} from '@/utils/templateReview'
import { prepareTemplateSlots, serializeTemplateSlots, swapTemplateSlotGeometry, templateSlotBoxes, templateSlotIdsValid, templateSlotLabel, templateSlotStructureLocked, addTemplateSlot, removeTemplateSlot, convertTemplateDrawingSlots } from '@/utils/templateSlots'
import { useAuthStore } from '@/stores/auth'
import HelpButton from '@/components/HelpButton.vue'
import DrawingReferenceEditor from '@/components/DrawingReferenceEditor.vue'
import TemplateGeometryLegend from '@/components/TemplateGeometryLegend.vue'
import DrawingAssessment from '@/components/DrawingAssessment.vue'
import {
  templatePageIndex,
  templateQuestionNumber,
  templateConfidenceRisk,
  templateDiagnosticHasPendingGeometryReview,
  templateDiagnosticReasonText,
  TEMPLATE_REVIEW_PENDING_PAGE_KEYS,
  templatePageReviewPending,
  templateResolveDiagnosticField,
  templateBBoxIsValid,
  templateRegionEntries,
  normalizeTemplateBBoxForPage,
  templateBoxStyle,
  cloneTemplateColumns,
  markTemplateGeometryConfirmed,
  cloneQuestionAnchors,
  templateQuestionAnchorBBox,
  templateVisibleAnswerBoxes,
  flattenTemplateHandwritingRegions,
  mergeTemplateAnswerRegionSources,
  templateRegionCandidateUsable,
  serializeTemplateHandwritingRegions,
  equalTemplateColumns,
  templatePageSaveBoxes,
} from '@/utils/templateGeometry'

const props = defineProps({
  anonymous: { type: Boolean, default: false },
})
const anonymous = computed(() => props.anonymous)
const auth = useAuthStore()
const router = useRouter()
const REVIEW_BATCH_STORAGE_PREFIX = 'mathagent_review_batch_id'
const ORIENTATION_BATCH_STORAGE_PREFIX = 'mathagent_orientation_batch_id'
const TEMPLATE_RECOGNITION_STORAGE_PREFIX = 'mathagent_template_recognition'
const INHERIT_IDENTITY_STORAGE_PREFIX = 'mathagent_inherit_identity'
const choiceOptions = Object.freeze(['A', 'B', 'C', 'D'])

// Opt-in per batch (frozen by the server at upload); the last choice is a
// per-teacher browser convenience only.
const inheritIdentity = ref(false)
function inheritIdentityStorageKey() {
  const teacherId = auth.user?.id
  return teacherId ? `${INHERIT_IDENTITY_STORAGE_PREFIX}:${teacherId}` : ''
}
watch(() => auth.user?.id, () => {
  const key = inheritIdentityStorageKey()
  try {
    inheritIdentity.value = Boolean(key) && localStorage.getItem(key) === 'true'
  } catch {
    inheritIdentity.value = false
  }
}, { immediate: true })
watch(inheritIdentity, (value) => {
  const key = inheritIdentityStorageKey()
  try {
    if (key) localStorage.setItem(key, value ? 'true' : 'false')
  } catch {
    // Storage may be unavailable; the checkbox still applies to this upload.
  }
})

const fileInputRef = ref(null)
const folderInputRef = ref(null)
const templateInputRef = ref(null)
const fileList = ref([])
const phoneCaptureVisible = ref(false)
const studentSubmissionVisible = ref(false)
// Capture session whose photos make up fileList, released once grading starts.
let phoneCaptureSessionForFiles = ''
const imagePreprocessing = ref(false)
const imageExtracting = ref(false)
const extractionProgress = ref({ current: 0, total: 0, message: '' })
const isDragover = ref(false)
const showNameGuide = ref(false)
const batchDialogVisible = ref(false)

// 流程步骤：0 上传 / 1 识别（批改结果展示在独立进度卡片）
const activeStep = ref(0)
const recognizing = ref(false)
const grading = ref(false)
// Live grading stopwatch. Progress events re-anchor it to the server's elapsed
// seconds (first paper graded -> now), so client clock skew does not matter.
const gradingClockStart = ref(0)
const gradingClockNow = ref(0)
let gradingClockTimer = null
const gradingElapsedSeconds = computed(() => (
  gradingClockStart.value ? Math.max(0, (gradingClockNow.value - gradingClockStart.value) / 1000) : 0
))
const dailyUsage = ref(null)
const serviceState = ref({
  status: 'checking',
  label: '检测中',
  message: '正在检查视觉识别、数学批改和任务协调链路。',
})
const SERVICE_FAULT_HOLD_MS = 60_000
const ACCELERATOR_UNAVAILABLE_MESSAGE = '加速节点不可用，请使用客户端'
const TEMPLATE_SERVICE_UNAVAILABLE_MESSAGE = '模板识别链路当前不可用，请稍后再试。'
const ACCELERATOR_AUTO_RESUME_DELAY_MS = 3000
const ACCELERATOR_AUTO_RESUME_LIMIT = 6
// Transient OCR failures (provider capacity, malformed output) keep one durable
// server-side retry. Spend it automatically after a short cool-down instead of
// leaving the teacher at an incomplete batch.
const OCR_RETRY_AUTO_RESUME_DELAY_MS = 15000
let availabilityPollTimer = null
let availabilityCheckPending = false
let serviceFaultHoldUntil = 0
let recognitionAutoResumeTimer = null
let recognitionAutoResumeAttempts = 0
let recognitionAutoResumeGeneration = 0
let recognitionRetryAutoResumedBatchId = ''
const serviceBlocksNewWork = computed(() => (
  ['checking', 'unavailable', 'fault'].includes(serviceState.value.status)
))
const templateServiceState = ref({
  status: 'checking',
  label: '检测中',
  message: '正在检查模板版面识别链路。',
  blocksNewWork: true,
})
const templateServiceBlocksNewWork = computed(() => (
  templateServiceState.value.blocksNewWork !== false
))
const registrationHardwareState = ref({
  status: 'checking',
  blocksNewWork: true,
  advisory: false,
})
const registrationHardwareBlocksNewWork = computed(() => (
  // Only the server's explicit instruction blocks: an advisory node state
  // never blocks, and a missing flag is not a block either.
  registrationHardwareState.value.advisory !== true
  && registrationHardwareState.value.blocksNewWork === true
))
const recognitionBlocksNewWork = computed(() => (
  serviceBlocksNewWork.value
  || registrationHardwareBlocksNewWork.value
))

function requiredHardwareState(hardware) {
  const status = String(hardware?.status || 'fault')
  // Only the server's explicit contract can make this extra hardware check
  // informational. Model/core readiness remains a separate mandatory gate.
  const advisory = hardware?.advisory === true
  if (status === 'ready') {
    return {
      status: 'available',
      blocksNewWork: false,
      advisory,
    }
  }
  if (['waiting_accelerator', 'pending', 'unavailable'].includes(status)) {
    return {
      status: 'unavailable',
      message: advisory ? '状态已记录' : ACCELERATOR_UNAVAILABLE_MESSAGE,
      blocksNewWork: !advisory,
      advisory,
    }
  }
  return {
    status: 'fault',
    message: advisory ? '状态已记录' : ACCELERATOR_UNAVAILABLE_MESSAGE,
    blocksNewWork: !advisory,
    advisory,
  }
}

function markServiceFault(message) {
  serviceFaultHoldUntil = Date.now() + SERVICE_FAULT_HOLD_MS
  serviceState.value = {
    status: 'fault',
    label: '链路故障',
    message: message || '批改链路发生故障，请稍后重试。',
  }
}

// ── 多页一次性模板（本次批改绝对权威，下次重新导入）──
const templateMode = ref('marked')      // 统一模板合同：密集手写卷走有痕迹安全路径
const templateColumnMode = ref('double') // single/double/triple：模板栏布局
const templateFiles = ref([])           // 待识别的 N 页模板图
const templatePreprocessing = ref(false)
const recognizingTemplate = ref(false)
const templateRecognizeMessage = ref('')
const templateRecognizeError = ref('')
const confirmedTemplateId = ref('')     // 已确认模板 id（本次批改权威）
const templatePageCount = ref(0)
const templateTeachingContext = ref(null)
const templateStageLabel = computed(() => {
  const stage = pendingTemplateId.value || confirmedTemplateId.value
    ? templateTeachingContext.value?.education_stage
    : auth.user?.education_stage
  return stage === 'primary' ? '小学数学' : stage === 'junior' ? '初中数学' : stage === 'senior' ? '高中数学' : '历史模板 · 学段未标记'
})
const templateDialogVisible = ref(false) // 模板确认弹窗
const confirmingTemplate = ref(false)
const importingTemplateBank = ref(false)
const templateBankImported = ref(false)
const templatePages = ref([])            // 弹窗内逐页题目（可编辑）
const templateCorrectionCandidatesLoading = ref(false)
let templateCorrectionCandidatesGeneration = 0
const templateCutBusy = ref(false)
const templateCutResult = ref(null)
const templateCutProgress = ref({ completed: 0, total: 0, remaining: 0 })
const templateCutActionId = ref('')
const templateCutImageType = ref('photo')
const templateCutPreviewPages = ref([])
const templateCutPreviewError = ref('')
const templateCropGesture = ref(null)
const templateReferenceEditorInstances = new Map()
const templateActivePreviewPage = ref(0)
const TEMPLATE_GEOMETRY_TOOLS = [
  { key: 'question', label: '红色题号' },
  { key: 'answer', label: '蓝色作答框' },
  { key: 'drawing', label: '绿色画图区' },
  { key: 'drawing-label', label: '作图题题号' },
]
const TEMPLATE_DRAWING_KINDS = [
  { key: 'non_graphic', label: '普通题' },
  { key: 'drawing', label: '作图题' },
  { key: 'mixed', label: '图文混合' },
]
const templateEditorBusy = computed(() => confirmingTemplate.value || importingTemplateBank.value
  || templateQuestionEditing.value || templateCutBusy.value
  || templateCutPreviewPages.value.some((page) => page.saving || page.geometryQuestionPrompting))
const templateQuestionEditing = ref(false)
const templateChoiceScore = ref(5)       // 考试：选择题统一分值
const templateFillScore = ref(5)         // 考试：填空题统一分值（多空自定义除外）
const pendingTemplateId = ref('')        // recognize 返回、待确认的 template_id

// OCR 身份不确定时只收集教师决策；真正建学生延迟到“全部入学生档案”。
const identityDialogVisible = ref(false)
const identityResolutions = ref([])
const identityOptionsLoading = ref(false)
const identityOptionsError = ref('')

const form = reactive({
  category: 'daily',
  exam_name: '',
  mode: 'precise',
})

// 识别阶段状态
const batchId = ref('')
const papers = ref([])
const recognized = reactive({ current: 0, total: 0 })
const recognitionDone = ref(false)
const recognitionAutoArmed = ref(true)
const recognitionCanGrade = ref(false)
const recognitionError = ref('')
const recognitionRecoveryState = ref({ status: 'none', message: '' })
const recognitionStats = ref({ uploaded: 0, succeeded: 0, manualReview: 0, failed: 0, excluded: 0, unresolved: 0 })
const excludingPaperId = ref('')
const currentMessage = ref('')
const questions = ref([])
const orientationDialogVisible = ref(false)
const orientationPaper = ref(null)
const orientationImageUrl = ref('')
const orientationImageLoading = ref(false)
const orientationImageError = ref('')
const orientationQuarterTurns = ref(0)
const orientationSaving = ref(false)
const orientationActionId = ref('')
let orientationImageRequestId = 0

// 批改阶段状态
const progressList = ref([])
const gradeTotal = ref(0)
const gradeError = ref('')
const summary = ref(null)
const reviewRestoredLabel = ref('')
const coursewareRecommendations = ref([])
const coursewareRecommendationState = ref('idle')
const coursewareRecommendationQuery = ref({})
let coursewareRecommendationKey = ''
const batchReport = ref({
  status: 'not_started',
  text: '',
  stats: {},
  error: '',
  updated_at: '',
})
let batchReportPollTimer = null
let batchReportPollGeneration = 0
const reviewBusy = ref(false)
const reviewImagesDownloading = ref(false)
const hasReviewImageRows = computed(() => progressList.value.some((row) => row.disposition !== 'voided' && !row.error))
const reviewArchived = ref(false)
// Hero copy follows the current workflow step (display only).
const gradeHero = computed(() => {
  const current = workflowSteps.value.findIndex((step) => step.state === 'current')
  const copy = [
    ['导入答案依据', '先确认批次类型与试卷栏数，再导入模板；教师确认后的标准答案才是本次批改依据。'],
    [anonymous.value ? '上传无身份页面' : '上传学生试卷', '支持图片、文件夹与 ZIP / RAR 压缩包；每张图片独立识别，身份不确定时由老师确认。'],
    ['识别与确认', '逐张核对识别结果，确认无误后开始批改。'],
    [reviewArchived.value ? '本批审阅已结束' : '审阅批改结果', '逐份查看原卷与逐题判定，先处理待复核题，再把已完成的结果归档。'],
  ]
  const [title, lead] = copy[current < 0 ? 3 : current]
  return { title, lead }
})
// Read-only position in the grading flow for the page stepper; derived purely from existing state.
const workflowSteps = computed(() => {
  const reviewStarted = grading.value || Boolean(summary.value) || progressList.value.length > 0
  const confirmStarted = activeStep.value >= 1
  const current = reviewStarted ? 3 : confirmStarted ? 2 : confirmedTemplateId.value ? 1 : 0
  return [
    { key: 'template', label: '答案依据', hint: '导入并确认模板' },
    { key: 'papers', label: anonymous.value ? '页面上传' : '学生试卷', hint: '选择图片或压缩包' },
    { key: 'confirm', label: '识别与确认', hint: '核对识别结果' },
    { key: 'review', label: '批改审阅', hint: '复核、作废或归档' },
  ].map((step, index) => ({
    ...step,
    state: index < current || (index === 3 && reviewArchived.value) ? 'done' : index === current ? 'current' : 'todo',
  }))
})
// The grading card appears once grading starts and stays for the review it produces.
const progressCardVisible = computed(() => (
  grading.value || progressList.value.length > 0 || Boolean(summary.value) || Boolean(gradeError.value)
))
const gradingPhaseState = computed(() => gradingPhases({
  anonymous: anonymous.value,
  identityPending: identityDialogVisible.value,
  grading: grading.value || Boolean(gradeError.value) || (progressList.value.length > 0 && !summary.value),
  reviewing: Boolean(summary.value),
  archived: reviewArchived.value,
}))
const dailyUsagePercent = computed(() => {
  const limit = Number(dailyUsage.value?.limit || 0)
  return limit > 0 ? Math.min(100, Math.round((Number(dailyUsage.value?.current || 0) / limit) * 100)) : 0
})
const reviewRevision = ref(0)
const manualDecisionDialogVisible = ref(false)
const manualDecisionRow = ref(null)
const manualDecisionDrafts = ref([])
const manualDecisionSaving = ref(false)
const manualDecisionImageUrl = ref('')
const manualDecisionImageLoading = ref(false)
const manualDecisionImageError = ref('')
const manualDecisionPages = ref([])
const manualDecisionPageIndex = ref(0)
const manualDecisionShowPaper = ref(true)
let manualDecisionImageRequestId = 0
let templateRecognizeController = null
let templateConfirmController = null
let templateCutController = null
let templateCutPollTimer = null
// Run token whose finished state was already applied.  Polling can observe the
// completion before the POST resolves; rendering it again would rebuild every
// page preview (refetching images and revoking blobs) on each tick.
let templateCutCompletedRunToken = null
let templateCutRunToken = 0
let templateCutPreviewLoadToken = 0
let recognizeController = null
let gradeController = null

function paperOcrPending(paper) {
  return ['queued', 'running'].includes(String(paper?.ocr_status || '').toLowerCase())
}

function paperOcrPendingLabel(paper) {
  const message = String(paper?.error || '').trim()
  if (message) return message
  return String(paper?.ocr_status || '').toLowerCase() === 'running' ? '正在识别处理' : '等待识别处理'
}

function paperOcrFailed(paper) {
  const status = String(paper?.ocr_status || '').toLowerCase()
  if (status) return status === 'failed'
  return Boolean(paper?.error && !paper.requires_manual_review)
}

function paperOcrNeedsReview(paper) {
  const status = String(paper?.ocr_status || '').toLowerCase()
  if (status) return status === 'needs_review'
  return Boolean(paper?.error && paper.requires_manual_review)
}

function paperOcrSucceeded(paper) {
  const status = String(paper?.ocr_status || '').toLowerCase()
  if (status) return status === 'succeeded'
  return Boolean(paper && !paper.error)
}

const recognizeSucceededCount = computed(() => recognitionDone.value
  ? recognitionStats.value.succeeded
  : papers.value.filter(paperOcrSucceeded).length)
const recognizeFailureCount = computed(() => recognitionDone.value
  ? recognitionStats.value.failed
  : papers.value.filter(paperOcrFailed).length)
const recognizeManualReviewCount = computed(() => recognitionDone.value
  ? recognitionStats.value.manualReview
  : papers.value.filter(paperOcrNeedsReview).length)
const recognizeCompletedCount = computed(() => (
  recognizeSucceededCount.value
  + recognizeFailureCount.value
  + recognizeManualReviewCount.value
  + (recognitionDone.value ? Number(recognitionStats.value.excluded || 0) : papers.value.filter(isExcludedPaper).length)
))
const recognizeUnresolvedCount = computed(() => recognitionDone.value
  ? recognitionStats.value.unresolved
  : Math.max(0, Number(recognized.total || 0) - recognizeCompletedCount.value))
const recognitionResumeVisible = computed(() => (
  ['available', 'service_fault', 'waiting'].includes(recognitionRecoveryState.value.status)
  && !!batchId.value
))
const recognitionRecoveryIsError = computed(() => (
  ['exhausted', 'unrecoverable', 'service_fault'].includes(recognitionRecoveryState.value.status)
))
const gradablePaperCount = computed(() => countGradablePapers(papers.value))

function templateReviewPendingPageIndexes(data = templateCutResult.value) {
  const pending = new Set()
  TEMPLATE_REVIEW_PENDING_PAGE_KEYS.forEach((key) => {
    const pages = data?.[key]
    if (!Array.isArray(pages)) return
    pages.map(Number).forEach((value) => {
      if (Number.isInteger(value) && value >= 0) pending.add(value)
    })
  })
  return pending
}

function templateDiagnosticPendingPageIndexes() {
  // A page whose model diagnostics still carry a pending geometry review is an
  // unsaved teacher decision: the save click must reach the server (which
  // clears the marker) instead of skipping the page and re-reading stale state.
  const pending = new Set()
  templateVisualEvidencePages().forEach((page) => {
    if (page.diagnostics.some(templateDiagnosticHasPendingGeometryReview)) {
      pending.add(Number(page.pageIndex))
    }
  })
  return pending
}

function clearTemplateResolvedGeometryDiagnostics(pageIndex, anchors, handwriting) {
  const targets = []
  const addTarget = (target) => {
    if (target && typeof target === 'object' && !targets.includes(target)) targets.push(target)
  }
  ;(templatePages.value || []).forEach((page) => {
    if (Number(page.page_index ?? page.pageIndex) === Number(pageIndex)) addTarget(page)
  })
  ;(templateCutPreviewPages.value || []).forEach((page) => {
    if (Number(page.pageIndex ?? page.page_index) === Number(pageIndex)) addTarget(page)
  })
  ;(templateCutResult.value?.pages || []).forEach((page) => {
    if (Number(page.page_index ?? page.pageIndex) === Number(pageIndex)) addTarget(page)
  })
  targets.forEach((target) => {
    delete target.question_anchor_candidates
    delete target.template_handwriting_region_candidates
    delete target.layout_review_pending
    delete target.anchor_review_pending
    delete target.geometry_review_pending
    target.layoutReviewPending = false
    target.anchorReviewPending = false
    target.geometryReviewPending = false
    templateResolveDiagnosticField(target, 'anchor_diagnostics', anchors, handwriting)
    templateResolveDiagnosticField(target, 'template_ocr_diagnostics', anchors, handwriting)
    templateResolveDiagnosticField(target, 'anchorDiagnostics', anchors, handwriting)
    templateResolveDiagnosticField(target, 'templateOcrDiagnostics', anchors, handwriting)
    const questionMode = target.modes?.question
    if (questionMode && typeof questionMode === 'object') {
      delete questionMode.layout_review_pending
      delete questionMode.anchor_review_pending
      delete questionMode.geometry_review_pending
      templateResolveDiagnosticField(questionMode, 'anchor_diagnostics', anchors, handwriting)
      templateResolveDiagnosticField(questionMode, 'template_ocr_diagnostics', anchors, handwriting)
      templateResolveDiagnosticField(questionMode, 'anchorDiagnostics', anchors, handwriting)
      templateResolveDiagnosticField(questionMode, 'templateOcrDiagnostics', anchors, handwriting)
      templateResolveDiagnosticField(questionMode, 'diagnostics', anchors, handwriting)
    }
  })
}

function templateVisualEvidencePages() {
  const byIndex = new Map()
  const merge = (raw, patch = {}) => {
    const index = templatePageIndex(raw, patch.pageIndex ?? byIndex.size)
    const current = byIndex.get(index) || {
      pageIndex: index,
      questions: [],
      anchors: undefined,
      handwriting: undefined,
      columns: undefined,
      layoutReviewPending: false,
      columnsSource: '',
      columnsConfirmed: undefined,
      diagnostics: [],
      hasVisualEvidence: false,
    }
    Object.assign(current, patch)
    byIndex.set(index, current)
    return current
  }

  ;(templatePages.value || []).forEach((page, index) => {
    const item = merge(page, { pageIndex: templatePageIndex(page, index) })
    item.questions = Array.isArray(page.questions) ? page.questions : []
    const reviewPending = templatePageReviewPending(page)
    if (reviewPending && Array.isArray(page.question_anchor_candidates) && page.question_anchor_candidates.length) item.anchors = page.question_anchor_candidates
    else if (Array.isArray(page.question_anchors)) item.anchors = page.question_anchors
    const answerRegions = reviewPending && templateRegionCandidateUsable(page.template_answer_region_candidates)
      ? page.template_answer_region_candidates : page.template_answer_regions
    const handwritingRegions = reviewPending && templateRegionCandidateUsable(page.template_handwriting_region_candidates)
      ? page.template_handwriting_region_candidates : page.template_handwriting_regions
    const mergedRegions = mergeTemplateAnswerRegionSources(answerRegions, handwritingRegions)
    if (mergedRegions.length) item.handwriting = mergedRegions
    item.layoutReviewPending = item.layoutReviewPending || templatePageReviewPending(page)
    if (page.anchor_diagnostics) item.diagnostics.push(page.anchor_diagnostics)
    if (page.template_ocr_diagnostics) item.diagnostics.push(page.template_ocr_diagnostics)
  })

  ;(templateCutResult.value?.pages || []).forEach((page, index) => {
    const artifact = page?.artifact || page?.page_artifact || {}
    const questionMode = page?.modes?.question || {}
    const columns = page?.anchor_crop_plan?.parameters?.columns
      ?? questionMode?.column_layout?.columns
    const item = merge(page, { pageIndex: templatePageIndex(page, index) })
    const reviewPending = templatePageReviewPending(page, questionMode)
    if (reviewPending && Array.isArray(page?.question_anchor_candidates) && page.question_anchor_candidates.length) item.anchors = page.question_anchor_candidates
    else if (Array.isArray(page?.question_anchors)) item.anchors = page.question_anchors
    else if (Array.isArray(artifact?.question_anchors)) item.anchors = artifact.question_anchors
    const answerRegions = reviewPending && templateRegionCandidateUsable(page?.template_answer_region_candidates)
      ? page.template_answer_region_candidates
      : (Array.isArray(page?.template_answer_regions) ? page.template_answer_regions : artifact?.template_answer_regions)
    const handwritingRegions = reviewPending && templateRegionCandidateUsable(page?.template_handwriting_region_candidates)
      ? page.template_handwriting_region_candidates
      : (Array.isArray(page?.template_handwriting_regions) ? page.template_handwriting_regions : artifact?.template_handwriting_regions)
    const mergedRegions = mergeTemplateAnswerRegionSources(answerRegions, handwritingRegions)
    if (mergedRegions.length) item.handwriting = mergedRegions
    if (Array.isArray(columns)) {
      item.columns = columns
      item.columnCount = Number(page?.anchor_crop_plan?.parameters?.column_count
        ?? questionMode?.column_layout?.column_count ?? columns.length)
      item.hasVisualEvidence = true
    }
    item.layoutReviewPending = item.layoutReviewPending || templatePageReviewPending(page, questionMode)
    item.diagnostics.push(
      page?.anchor_diagnostics,
      page?.template_ocr_diagnostics,
      questionMode?.anchor_diagnostics,
      questionMode?.diagnostics,
    )
  })

  ;(templateCutPreviewPages.value || []).forEach((page, index) => {
    const item = merge(page, { pageIndex: templatePageIndex(page, index) })
    if (Array.isArray(page.questionAnchors)) item.anchors = page.questionAnchors
    if (Array.isArray(page.answerBoxes)) item.handwriting = page.answerBoxes
    if (Array.isArray(page.columns)) {
      item.columns = page.columns
      item.columnCount = Number(page.columnCount ?? page.columns.length)
      item.hasVisualEvidence = page.columns.length > 0
    }
    item.layoutReviewPending = item.layoutReviewPending
      || page.layoutReviewPending === true
      || page.layout_review_pending === true
    item.columnsSource = page.columnsSource || item.columnsSource
    if (page.columnsConfirmed !== undefined) item.columnsConfirmed = page.columnsConfirmed
    item.diagnostics.push(page.anchorDiagnostics, page.templateOcrDiagnostics)
  })

  const pendingPages = templateReviewPendingPageIndexes()
  byIndex.forEach((page) => {
    if (pendingPages.has(page.pageIndex)) page.layoutReviewPending = true
    page.diagnostics = page.diagnostics.filter(Boolean)
  })
  return [...byIndex.values()].sort((left, right) => left.pageIndex - right.pageIndex)
}

function templateGeometryReviewState() {
  const pages = templateVisualEvidencePages()
  const expectedOccurrences = new Map()
  const actualAnchorOccurrences = new Map()
  const missingQuestionNumbers = []
  const duplicateQuestionNumbers = []
  const orphanQuestionNumbers = []
  const outOfBoundsQuestionNumbers = []
  const outOfBoundsHandwritingQuestions = []
  const lowConfidenceQuestionNumbers = []
  const lowConfidenceHandwritingQuestions = []
  const invalidColumnPages = []
  const diagnosticReasons = []
  const diagnosticReviewDetails = []
  const pendingPages = templateReviewPendingPageIndexes()
  const addUnique = (list, value) => {
    const normalized = String(value ?? '').trim()
    if (normalized && !list.includes(normalized)) list.push(normalized)
  }
  const addPage = (list, value) => {
    const normalized = Number(value)
    if (Number.isInteger(normalized) && normalized >= 0 && !list.includes(normalized)) list.push(normalized)
  }

  pages.forEach((page) => {
    const pageIndex = page.pageIndex
    const expected = Array.isArray(page.questions)
      ? page.questions.map((question) => templateQuestionNumber(question?.question_no ?? question?.question_number)).filter(Boolean)
      : []
    expected.forEach((number) => {
      const entries = expectedOccurrences.get(number) || []
      entries.push(pageIndex)
      expectedOccurrences.set(number, entries)
    })

    if (page.layoutReviewPending || page.columnsSource === 'teacher_draft' || page.columnsConfirmed === false) {
      pendingPages.add(pageIndex)
    }
    if (!Array.isArray(page.columns) || !page.columns.length) {
      // A missing plan is a review state, never an implicit equal-column result.
      pendingPages.add(pageIndex)
    } else {
      // The teacher may correct one page independently of the upload default,
      // including deleting a divider. Validate its own saved/draft count.
      const expectedColumnCount = Number(page.columnCount ?? page.columns.length)
      if (!Number.isInteger(expectedColumnCount) || expectedColumnCount < 1 || expectedColumnCount > 3
        || page.columns.length !== expectedColumnCount) addPage(invalidColumnPages, pageIndex)
      page.columns.forEach((column) => {
        if (!templateBBoxIsValid([
          Number(column?.x1_1000), 0, Number(column?.x2_1000), 1000,
        ])) addPage(invalidColumnPages, pageIndex)
      })
    }

    const anchors = Array.isArray(page.anchors) ? page.anchors : []
    anchors.forEach((anchor) => {
      const number = templateQuestionNumber(anchor?.question_number ?? anchor?.question_no)
      if (!number) return
      const occurrences = actualAnchorOccurrences.get(number) || []
      occurrences.push(pageIndex)
      actualAnchorOccurrences.set(number, occurrences)
      if (templateConfidenceRisk(anchor)) addUnique(lowConfidenceQuestionNumbers, number)
    })

    const expectedSet = new Set(expected)
    anchors.forEach((anchor) => {
      const number = templateQuestionNumber(anchor?.question_number ?? anchor?.question_no)
      if (number && !expectedSet.has(number)) addUnique(orphanQuestionNumbers, number)
    })
    const regions = templateRegionEntries(page.handwriting)
    regions.forEach(({ item, box, region }) => {
      const number = templateQuestionNumber(item?.question_number ?? region?.question_number)
      // Question numbers are ownership keys, not geometry.  Only compare them
      // when this page actually has a question list; otherwise a cut-preview
      // page without copied question metadata would falsely look out of bounds.
      if (number && expected.length && !expectedSet.has(number)) addUnique(orphanQuestionNumbers, number)
      if (!templateBBoxIsValid(box)) {
        if (number) addUnique(outOfBoundsHandwritingQuestions, number)
        return
      }
      const provenance = { ...item, ...(region || {}) }
      if (templateConfidenceRisk(provenance)) addUnique(lowConfidenceHandwritingQuestions, number || `P${pageIndex + 1}`)
    })
    page.diagnostics.forEach((diagnostic) => {
      // Directory-stage diagnostics also cover answer/reference failures.  An
      // explicit geometry result must not keep a successfully retried reference
      // behind the geometry gate; legacy diagnostics remain fail-closed.
      if (templateDiagnosticHasPendingGeometryReview(diagnostic)) {
        const reasonText = templateDiagnosticReasonText(diagnostic)
        addUnique(diagnosticReasons, reasonText)
        addUnique(diagnosticReviewDetails, `第${pageIndex + 1}页：${reasonText.slice(0, 48)}`)
      }
      const imageAssociation = diagnostic?.image_association
      if (imageAssociation?.needs_review === true || imageAssociation?.status === 'needs_review') {
        addUnique(diagnosticReasons, 'image_association')
        addUnique(diagnosticReviewDetails, `第${pageIndex + 1}页：image_association`)
      }
    })
  })

  expectedOccurrences.forEach((occurrences, number) => {
    if (occurrences.length > 1) addUnique(duplicateQuestionNumbers, number)
    if (!actualAnchorOccurrences.has(number)) addUnique(missingQuestionNumbers, number)
  })
  actualAnchorOccurrences.forEach((occurrences, number) => {
    if (occurrences.length > 1) addUnique(duplicateQuestionNumbers, number)
  })

  if (!templateCutResult.value && !templateCutPreviewPages.value.length) {
    pages.forEach((page) => pendingPages.add(page.pageIndex))
  }
  invalidColumnPages.forEach((pageIndex) => pendingPages.add(pageIndex))
  const summaryParts = []
  if (pendingPages.size) summaryParts.push(`栏边界 ${[...pendingPages].sort((a, b) => a - b).map((value) => `第${value + 1}页`).join('、')}`)
  if (missingQuestionNumbers.length) summaryParts.push(`缺失题号 ${missingQuestionNumbers.join('、')}`)
  if (duplicateQuestionNumbers.length) summaryParts.push(`重复题号 ${duplicateQuestionNumbers.join('、')}`)
  if (orphanQuestionNumbers.length) summaryParts.push(`未绑定题号 ${orphanQuestionNumbers.join('、')}`)
  if (outOfBoundsHandwritingQuestions.length) summaryParts.push('作答区域框越界')
  if (lowConfidenceQuestionNumbers.length) summaryParts.push('低置信题目归属')
  if (lowConfidenceHandwritingQuestions.length) summaryParts.push('低置信作答区域')
  if (diagnosticReasons.length) {
    const detail = diagnosticReviewDetails.slice(0, 3).join('；')
    summaryParts.push(detail ? `模型诊断待复核（${detail}）` : '模型诊断待复核')
  }
  return {
    needsReview: Boolean(
      pendingPages.size
      || missingQuestionNumbers.length
      || duplicateQuestionNumbers.length
      || orphanQuestionNumbers.length
      || outOfBoundsQuestionNumbers.length
      || outOfBoundsHandwritingQuestions.length
      || lowConfidenceQuestionNumbers.length
      || lowConfidenceHandwritingQuestions.length
      || diagnosticReasons.length,
    ),
    layoutReviewPendingPages: [...pendingPages].sort((a, b) => a - b),
    missingQuestionNumbers,
    duplicateQuestionNumbers,
    orphanQuestionNumbers,
    outOfBoundsQuestionNumbers,
    outOfBoundsHandwritingQuestions,
    lowConfidenceQuestionNumbers,
    lowConfidenceHandwritingQuestions,
    invalidColumnPages,
    diagnosticReasons: [...new Set(diagnosticReasons)],
    diagnosticReviewDetails: [...new Set(diagnosticReviewDetails)],
    summary: summaryParts.join('；'),
  }
}

const templateGeometryReview = computed(templateGeometryReviewState)
const templateCanConfirm = computed(() => {
  // Keep the server's explicit page list in the gate as well as the merged
  // review helper.  This makes a newly polled result fail closed immediately,
  // before the preview-page merge has had a chance to run.
  const serverPendingPages = templateReviewPendingPageIndexes()
  return !!form.category && templatePages.value.length > 0
    && !templateCutPreviewPages.value.some((page) => templateDrawingReferenceQuestions(page).length > 0 && !templateReferenceImagesReady(page))
    && templateChoiceConflictEntries(templatePages.value).length === 0
     && serverPendingPages.size === 0
    && !templateGeometryReview.value.needsReview
    && templatePages.value.every((page) =>
      (page.questions || []).length > 0 && page.questions.every((q) =>
        String(q.question_no ?? '').trim() && ((q.question_type === 'choice' && !templateIsPureDrawing(q)) || (q.question_text || '').trim()) && (templateIsPureDrawing(q) || (q.standard_answer || '').trim()) && questionScoringValid(q)
      )
    )
})
const templateChoiceEntries = computed(() => templateReviewEntries(templatePages.value, 'choice'))
const templateChoiceReadyEntries = computed(() => templateChoiceReviewEntries(templatePages.value))
const templateChoiceConflicts = computed(() => templateChoiceConflictEntries(templatePages.value))
const templateChoiceSingleCount = computed(() => templateChoiceEntries.value.filter(({ question }) => question.choice_mode === 'single').length)
const templateChoiceMultipleCount = computed(() => templateChoiceEntries.value.filter(({ question }) => question.choice_mode === 'multiple').length)
const templateChoicePendingCount = computed(() => templateChoiceEntries.value.filter(({ question }) => !choiceContractValid(question)).length)
const templateFillEntries = computed(() => templateReviewEntries(templatePages.value, 'fill'))
const templateFillBatchEntryCount = computed(() => templateFillEntries.value.filter(({ question }) => !isCustomFill(question)).length)
const templateCustomFillCount = computed(() => templateFillEntries.value.length - templateFillBatchEntryCount.value)
const templateSolveEntries = computed(() => templateReviewEntries(templatePages.value, 'solve'))
const hasActiveReviewRows = computed(() => progressList.value.some((row) => row.disposition === 'active'))
const pendingReviewCount = computed(() => progressList.value
  .filter((row) => row.disposition === 'active')
  .reduce((total, row) => total + Number(row.needs_review || 0), 0))
const resolvedActiveReviewCount = computed(() => progressList.value.filter((row) =>
  row.disposition === 'active' && Number(row.needs_review || 0) === 0
).length)
const dailyAverageCorrectRate = computed(() => {
  const rates = progressList.value
    .filter((row) => row.disposition === 'active' && Number(row.needs_review || 0) === 0)
    .map((row) => Number(row.correct_rate))
    .filter((value) => Number.isFinite(value))
  return rates.length ? rates.reduce((sum, value) => sum + value, 0) / rates.length : null
})
function buildClientBatchReportFallback(rows) {
  const totals = { paper_count: 0, question_count: 0, correct_count: 0, partial_count: 0, wrong_count: 0, needs_review_count: 0 }
  for (const row of rows || []) {
    if (!row || row.disposition === 'voided' || row.error) continue
    const details = Array.isArray(row.details) ? row.details : []
    const detailTotal = details.length
    const total = Math.max(0, Number(row.total) || detailTotal)
    if (!total && !detailTotal) continue
    totals.paper_count += 1
    totals.question_count += total
    for (const key of ['correct', 'partial', 'wrong', 'needs_review']) {
      const value = Math.max(0, Number(row[key]) || details.filter((detail) => detail?.result === key).length)
      totals[`${key}_count`] += value
    }
  }
  const judged = totals.question_count - totals.needs_review_count
  const errors = totals.partial_count + totals.wrong_count
  return {
    ...totals,
    error_rate: judged > 0 ? Number((errors / judged).toFixed(4)) : null,
    all_errors: [],
    top_errors: [],
    knowledge_points: [],
    knowledge_distribution: [],
  }
}
const batchReportStats = computed(() => {
  const stats = batchReport.value?.stats
  const normalized = stats && typeof stats === 'object' ? stats : {}
  if (Number(normalized.question_count) > 0) return normalized
  const fallback = buildClientBatchReportFallback(progressList.value)
  return fallback.question_count > 0 ? { ...normalized, ...fallback } : normalized
})
const batchReportTotalQuestions = computed(() => nonNegativeReportCount(batchReportStats.value.question_count))
const batchReportCorrectCount = computed(() => nonNegativeReportCount(batchReportStats.value.correct_count))
const batchReportPartialCount = computed(() => nonNegativeReportCount(batchReportStats.value.partial_count))
const batchReportWrongCount = computed(() => nonNegativeReportCount(batchReportStats.value.wrong_count))
const batchReportNeedsReviewCount = computed(() => nonNegativeReportCount(batchReportStats.value.needs_review_count))
const batchReportJudgedCount = computed(() => (
  batchReportCorrectCount.value + batchReportPartialCount.value + batchReportWrongCount.value
))
const batchReportErrorTotal = computed(() => batchReportPartialCount.value + batchReportWrongCount.value)
const batchReportAccuracy = computed(() => {
  const judged = batchReportJudgedCount.value
  return judged > 0 ? batchReportCorrectCount.value / judged : null
})
const batchReportPrimaryValue = computed(() => {
  const averageScore = Number(summary.value?.avg_score)
  if (form.category === 'exam' && summary.value?.avg_score != null && Number.isFinite(averageScore)) {
    return averageScore.toFixed(averageScore % 1 ? 1 : 0)
  }
  return batchReportAccuracy.value == null ? '—' : `${Math.round(batchReportAccuracy.value * 100)}%`
})
const batchReportPrimaryLabel = computed(() => (
  form.category === 'exam' && summary.value?.avg_score != null ? '平均得分' : '已判正确率'
))
const batchReportErrorRatePercent = computed(() => {
  const rate = batchReportStats.value.error_rate
  return rate == null || !Number.isFinite(Number(rate)) ? null : Number(rate) * 100
})
const batchReportDiagnosticErrorRateText = computed(() => reportPercent(batchReportStats.value.diagnostic_error_rate))
// Judged outcomes only: pending answers are listed beside the donut, never drawn in it.
const batchReportOutcomeSegments = computed(() => [
  { key: 'correct', label: '正确', value: batchReportCorrectCount.value },
  { key: 'partial', label: '部分正确', value: batchReportPartialCount.value },
  { key: 'wrong', label: '错误', value: batchReportWrongCount.value },
])
const batchReportTopErrors = computed(() => (
  Array.isArray(batchReportStats.value.top_errors) ? batchReportStats.value.top_errors : []
))
const batchReportKnowledgePoints = computed(() => (
  Array.isArray(batchReportStats.value.knowledge_points) ? batchReportStats.value.knowledge_points : []
))
// Both lists are ranked by error count, so their bars are scaled to the
// largest count rather than to the error rate shown alongside.
const batchReportTopErrorMax = computed(() => Math.max(0, ...batchReportTopErrors.value.map((item) => Number(item.error_count) || 0)))
const batchReportKnowledgeErrorMax = computed(() => Math.max(0, ...batchReportKnowledgePoints.value.map((item) => Number(item.error_count) || 0)))
const manualDecisionCompactDrafts = computed(() => manualDecisionDrafts.value.filter((item) => manualDecisionIsCompact(item)))
const manualDecisionSolveDrafts = computed(() => manualDecisionDrafts.value.filter((item) => !manualDecisionIsCompact(item)))
const manualDecisionHasEditableRows = computed(() => manualDecisionDrafts.value.some((item) => item.editable))
const manualDecisionComplete = computed(() => manualDecisionHasEditableRows.value && manualDecisionDrafts.value
  .filter((item) => item.editable)
  .every((item) => ['correct', 'partial', 'wrong'].includes(item.result)))
const needsIdentityResolution = computed(() => papers.value.some((paper) => {
  if (anonymous.value) return false
  if (paper.error && !paper.requires_manual_review) return false
  const identity = paper.identity || {}
  return identity.status !== 'matched' || !identity.student_id
}))
const identityCanConfirm = computed(() => identityResolutionsReady(identityResolutions.value))

function resetRecognitionCompletionState() {
  recognitionDone.value = false
  recognitionCanGrade.value = false
  recognitionError.value = ''
  recognitionStats.value = { uploaded: 0, succeeded: 0, manualReview: 0, failed: 0, excluded: 0, unresolved: 0 }
}

function resetRecognitionRecoveryState() {
  recognitionRecoveryState.value = { status: 'none', message: '' }
}

function canRecoverRecognitionConnection(error, signal) {
  if (isSseRequestCancelled(error, signal)) return false
  const code = String(error?.code || error?.processingCode || '').toUpperCase()
  if (['ACCELERATOR_WAITING', 'BATCH_RECOGNITION_BUSY'].includes(code)) return true
  // A business/authorization failure is not made healthy by a successful GET.
  if (code || error?.sseFailureKind === 'application') return false
  const status = Number(error?.status || error?.response?.status || 0)
  if (status) return [502, 503, 504].includes(status)
  return error instanceof SseTransportError || (!error?.sseFailureKind
    && /failed to fetch|fetch failed|network\s*error|networkerror|load failed|connection (?:was )?(?:lost|closed|reset)/i.test(String(error?.message || '')))
}

function recoverRecognitionConnection(targetBatchId, payload) {
  if (!payload || String(payload.batch_id || '') !== String(targetBatchId || '')
    || String(batchId.value || '') !== String(targetBatchId || '')
    || !['recognizing_remaining', 'recognition_interrupted'].includes(payload.status)
    || recognitionRecoveryState.value.status !== 'available') return false
  // This owned snapshot proves reachability, not spare model/hardware capacity.
  serviceFaultHoldUntil = 0
  serviceState.value = { status: 'queued', label: '批次可恢复', message: '已连接服务器，正在恢复已保存的识别进度。' }
  recognitionError.value = ''
  currentMessage.value = ''
  activeStep.value = 1
  scheduleRecognitionAutoResume(targetBatchId)
  return true
}

async function pollRecognitionRecovery(target, generation) {
  const isCurrent = () => generation === recognitionAutoResumeGeneration
    && target === String(batchId.value || '') && !recognizing.value && !grading.value
  if (!isCurrent()) return
  const payload = await refreshRecognitionStateAfterFailure(target, { isCurrent, suppressAutoResume: true })
  if (!isCurrent() || !payload) return
  if (await continueRefreshedRecognition(payload)) return
  if (recognitionRecoveryState.value.status !== 'available') return
  // Active batch states still own server work. Only read checkpoints until it
  // ends; a race acquiring the lock is handled by BATCH_RECOGNITION_BUSY below.
  if (payload.status === 'recognizing_remaining') {
    recoverRecognitionConnection(target, payload)
    return
  }
  if (payload.status === 'recognition_interrupted') {
    await resumeRecognitionBatch(target, { automatic: true })
  }
}

function clearRecognitionAutoResume({ resetAttempts = false } = {}) {
  recognitionAutoResumeGeneration += 1
  if (recognitionAutoResumeTimer) {
    window.clearTimeout(recognitionAutoResumeTimer)
    recognitionAutoResumeTimer = null
  }
  if (resetAttempts) recognitionAutoResumeAttempts = 0
}

function scheduleRecognitionAutoResume(targetBatchId) {
  const target = String(targetBatchId || '')
  if (!target || target !== String(batchId.value || '')) return false
  clearRecognitionAutoResume()
  if (recognitionAutoResumeAttempts >= ACCELERATOR_AUTO_RESUME_LIMIT) {
    recognitionRecoveryState.value = {
      status: 'available',
      message: '自动恢复检查已达上限。已完成结果仍保留，可稍后点击“继续处理未完成试卷”。',
    }
    return false
  }
  recognitionAutoResumeAttempts += 1
  recognitionRecoveryState.value = {
    status: 'waiting',
    message: `识别进度已保存，系统将在 ${ACCELERATOR_AUTO_RESUME_DELAY_MS / 1000} 秒后检查并继续；服务器仍在处理时只等待，不重复提交（${recognitionAutoResumeAttempts}/${ACCELERATOR_AUTO_RESUME_LIMIT}）。`,
  }
  const generation = recognitionAutoResumeGeneration
  recognitionAutoResumeTimer = window.setTimeout(() => {
    recognitionAutoResumeTimer = null
    void pollRecognitionRecovery(target, generation)
  }, ACCELERATOR_AUTO_RESUME_DELAY_MS)
  return true
}

function scheduleRetryableOcrAutoResume(targetBatchId, payload = {}) {
  const target = String(targetBatchId || '')
  if (!target || target !== String(batchId.value || '')) return false
  if (Number(payload?.retryable_failed_papers || 0) <= 0) return false
  // The server allows each page one more attempt; spend it once per batch.
  if (recognitionRetryAutoResumedBatchId === target) return false
  recognitionRetryAutoResumedBatchId = target
  clearRecognitionAutoResume()
  recognitionRecoveryState.value = {
    status: 'waiting',
    message: `部分试卷识别暂时失败，系统将在 ${OCR_RETRY_AUTO_RESUME_DELAY_MS / 1000} 秒后自动重试这些试卷；也可立即点击“继续处理未完成试卷”。`,
  }
  recognitionAutoResumeTimer = window.setTimeout(() => {
    recognitionAutoResumeTimer = null
    if (target === String(batchId.value || '') && !recognizing.value && !grading.value) {
      void resumeRecognitionBatch(target, { automatic: true })
    }
  }, OCR_RETRY_AUTO_RESUME_DELAY_MS)
  return true
}

function updateRecognitionRecoveryState(payload = {}, { failureCode = '', serviceFault = false } = {}) {
  // A fully recovered snapshot wins over historical status/error codes. Keep
  // the same complete-paper gate as grading, not just the server's boolean.
  if (payload?.can_grade === true && normalizeBatchReadiness(payload, {
    expectedTotal: fileList.value.length,
    papers: papers.value,
  }).canGrade) {
    resetRecognitionRecoveryState()
    return
  }
  const code = String(failureCode || '').toUpperCase()
  if (code === 'OCR_RETRY_EXHAUSTED') {
    recognitionRecoveryState.value = {
      status: 'exhausted',
      message: '识别重试已耗尽，不能再自动恢复。请检查原图；若图片正常，请联系管理员检查模型服务。',
    }
    return
  }
  if (code === 'OCR_NO_RETRYABLE_FAILURES') {
    recognitionRecoveryState.value = {
      status: 'unrecoverable',
      message: '当前失败试卷不可自动恢复，请检查原图或联系管理员。',
    }
    return
  }
  if (serviceFault && !payload?.status) {
    recognitionRecoveryState.value = {
      status: 'service_fault',
      message: '批改链路当前故障，服务恢复后可继续处理未完成试卷。',
    }
    return
  }

  const status = String(payload?.status || '')
  const retryableFailures = Number(payload?.retryable_failed_papers || 0)
  const unresolved = Number(payload?.unresolved_papers || 0)
  const failed = Number(payload?.failed_papers || 0)
  const resumable = (
    ['recognizing_remaining', 'recognition_interrupted'].includes(status)
    || retryableFailures > 0
    || (status === 'failed' && unresolved > 0)
  )
  if (resumable) {
    recognitionRecoveryState.value = serviceFault
      ? {
          status: 'service_fault',
          message: '批改链路当前故障，服务恢复后可继续处理未完成试卷。',
        }
      : {
          status: 'available',
          message: '本批次仍有未完成或可重试的试卷，可点击“继续处理未完成试卷”。',
        }
    return
  }
  if (failed > 0 || ['failed', 'ocr_partial_failed'].includes(status)) {
    recognitionRecoveryState.value = {
      status: 'unrecoverable',
      message: '当前失败试卷不可自动恢复，自动重试可能已耗尽。请检查原图或联系管理员。',
    }
    return
  }
  resetRecognitionRecoveryState()
}

function recognitionIncompleteMessage(stats) {
  return `本批次 OCR 未完整返回：上传 ${stats.uploaded} 张，成功 ${stats.succeeded} 张，需人工复核 ${stats.manualReview} 张，失败 ${stats.failed} 张，未完成 ${stats.unresolved} 张。为避免漏批，系统没有开始批阅。`
}

function applyRecognitionDone(data) {
  const stats = normalizeBatchReadiness(data, {
    expectedTotal: fileList.value.length,
    papers: papers.value,
  })
  recognitionStats.value = stats
  recognitionDone.value = true
  recognitionCanGrade.value = stats.canGrade
  recognitionError.value = stats.canGrade ? '' : recognitionIncompleteMessage(stats)
  // An incomplete `done` must still say what can be done next (resume a
  // retryable failure, or that a page is unrecoverable), not only that the
  // batch is incomplete.
  if (stats.canGrade) resetRecognitionRecoveryState()
  else updateRecognitionRecoveryState(data)
  recognized.current = stats.succeeded + stats.manualReview + stats.failed + stats.excluded
  recognized.total = stats.uploaded || recognized.total
}

function defaultGradingMethod(questionType) {
  return { choice: 'exact', fill: 'equivalent', solve: '' }[questionType] || ''
}

function toTextLines(value) {
  if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean).join('\n')
  return String(value || '').trim()
}

function toStringList(value) {
  if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean)
  return String(value || '').split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
}

function templateQuestionSignature(pages = templatePages.value) {
  return JSON.stringify((pages || []).map((page) => ({
    page_index: Number(page.page_index),
    questions: (page.questions || []).map((question) => ({
      question_no: String(question.question_no ?? question.question_number ?? '').trim(),
      question_text: String(question.question_text ?? question.content ?? '').trim(),
      question_type: String(question.question_type || '').toLowerCase(),
      choice_mode: String(question.choice_mode || '').toLowerCase(),
    })),
  })))
}

function markTemplateQuestionInput(question) {
  // The teacher drew the standard figure; a text edit never discards it. It
  // only needs re-checking, which the next template save records.
  if (question?.drawing_reference) question.drawing_reference.teacher_confirmed = false
}

function markTemplateAnswerManualEdit(question, field = 'standard_answer') {
  if (!question || typeof question !== 'object') return
  // A choice has one physical answer slot; teacher edits own both views.
  if (field !== 'solution_steps_text' && question.question_type === 'choice'
    && question.slot_ids?.length === 1 && !question.region_kinds?.includes('drawing')) {
    question.answer_slots_text = [String(question.standard_answer ?? '')]
    question.answer_slots = [...question.answer_slots_text]
  }
  // Text answers and the teacher-drawn figure are independent answer parts.
  if (question.drawing_reference && field !== 'solution_steps_text') question.drawing_reference.teacher_confirmed = false
}

function updateDrawingReference(question, reference) {
  if (!question) return
  question.drawing_reference = reference
  question._drawingReferenceDirty = true
}

function templateReferenceQuestionEditBlockReason() {
  if (templatePages.value.some((page) => (page.questions || []).some((question) => question._drawingReferenceDirty))) {
    return '有尚未最终保存的作图参考修改，请先核对并最终确认模板保存，再重新打开增删题号；如不保留，请先取消本次模板草稿。'
  }
  return ''
}

function templateDrawingReferenceSaveSnapshot() {
  return new Map(templatePages.value.flatMap((page) => (page.questions || []).map((question) => [question, templateDrawingReferenceSnapshot(question)])))
}

function clearSavedTemplateDrawingReferenceChanges(snapshot) {
  for (const page of templatePages.value) {
    for (const question of page.questions || []) {
      if (snapshot.has(question) && snapshot.get(question) === templateDrawingReferenceSnapshot(question)) {
        question._drawingReferenceDirty = false
        if (question.drawing_reference) question.drawing_reference.teacher_confirmed = true
      }
    }
  }
}

function templateDrawingReferenceQuestions(page) {
  return (templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))?.questions || [])
    .filter((question) => ['drawing', 'mixed'].includes(question.answer_kind))
}

function templateReferenceEditors(page) {
  // 教师标注：每页至少保留一个编辑器宿主——没有作图题的页面也要能看到原图、
  // 调整题号/作答框/分栏；作图参考控件只在该页存在作图题（q 非空）时出现。
  const questions = templateDrawingReferenceQuestions(page)
  return questions.length ? questions : [null]
}

function templateDrawingReferenceRegionsFor(page, question) {
  return page.drawingBoxes.filter((box) => String(box.question_number) === String(question?.question_no)).map((box) => box.bbox)
}


function templateReferenceCanvasVisible(page, question) {
  // 教师标注：整页只显示一张模板画布——几何模式跟随第一道作图题，
  // 参考编辑模式跟随当前选中的题目，避免逐题编辑器各带一张一模一样的模板。
  const questions = templateDrawingReferenceQuestions(page)
  if (!questions.length) return question === null
  const activeNumber = String(page.referenceQuestionNumber || '')
  const editingReference = page.previewEditMode === 'reference'
    && questions.some((item) => String(item.question_no) === activeNumber)
  if (!editingReference) return questions[0] === question
  return String(question.question_no) === activeNumber
}

function markTemplateReferenceImageReady(page, question) {
  page.referenceImageReadyNumbers ||= {}
  page.referenceImageReadyNumbers[String(question?.question_no || '')] = true
}

function templateReferenceImagesReady(page) {
  return templateDrawingReferenceQuestions(page).every((question) => page.referenceImageReadyNumbers?.[String(question.question_no)])
}

function templateDrawingReferenceSummary(question) {
  const reference = question.drawing_reference
  if (!reference) return '未设置标准图；保存后将按题干和作图区直接交给 Flash 判定。'
  if (reference.comparison_mode === 'manual') return reference.variants?.length
    ? '自定义作图：不按固定坐标比较，批改时把题干和作图区交给 Flash。'
    : '未保存标准笔画；保存后将按题干和作图区直接交给 Flash 判定。'
  return `标准图 ${reference.variants?.length || 0} 个完整方案 · ${reference.teacher_confirmed ? '已随模板保存' : '随保存模板一起确认'}`
}

function templateDrawingReferenceSnapshot(question) {
  return JSON.stringify({
    question_no: question.question_no, question_text: question.question_text,
    standard_answer: question.standard_answer, answer_kind: question.answer_kind,
    drawing_regions: question.drawing_regions,
    drawing_reference: question.drawing_reference,
  })
}

function setTemplatePreviewEditMode(page, mode) {
  if (templatePageEditingLocked(page)) return
  if (templateCropGesture.value) cancelTemplateGeometry({ pointerId: templateCropGesture.value.pointerId })
  clearTemplateSelection(page)
  page.geometryTool = ''
  page.previewEditMode = mode
}

function selectTemplateDrawingReference(page, number) {
  if (templatePageEditingLocked(page)) return
  page.referenceQuestionNumber = String(number)
  // Set the active question before switching modes so the visible editor and
  // its interactive SVG are selected in the same render pass.
  setTemplatePreviewEditMode(page, 'reference')
}

function openTemplateDrawingReference(pageIndex, number) {
  const preview = templateCutPreviewPages.value.find((page) => Number(page.pageIndex) === Number(pageIndex))
  if (!preview) { ElMessage.warning('模板预览尚未加载，请稍后重试'); return }
  templateActivePreviewPage.value = Number(preview.pageIndex)
  selectTemplateDrawingReference(preview, number)
  document.getElementById(`template-reference-page-${Number(pageIndex)}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}


function inferSubquestionCount(question) {
  const text = `${question.question_text || question.content || ''}\n${question.standard_answer || question.answer || ''}`
  const matches = [...text.matchAll(/[（(]\s*(\d{1,2})\s*[）)]/g)]
  if (!matches.length) return 1
  return Math.min(20, Math.max(...matches.map((item) => Number(item[1]) || 1)))
}

function inferBlankCount(question) {
  const slots = Array.isArray(question.answer_slots) ? question.answer_slots.filter(Boolean) : []
  if (slots.length) return Math.min(20, slots.length)
  const text = question.question_text || question.content || ''
  const runs = text.match(/_{2,}|＿{2,}|﹏{2,}/g) || []
  return Math.min(20, Math.max(1, runs.length))
}

function defaultSubquestionScores(question, count) {
  const raw = Array.isArray(question.subquestion_scores) ? question.subquestion_scores : []
  if (raw.length) return raw.map((value) => Number(value) || 0)
  const defaults = [5, 7, 8]
  return defaults.map((value, index) => (index < count ? value : 0))
}

function equalBlankScores(total, count) {
  const safeCount = Math.min(20, Math.max(1, Number(count) || 1))
  const safeTotal = Math.max(0.5, Number(total) || 5)
  const base = Math.round((safeTotal / safeCount) * 10000) / 10000
  const scores = Array(safeCount).fill(base)
  scores[safeCount - 1] = Math.round((safeTotal - base * (safeCount - 1)) * 10000) / 10000
  return scores
}

function normalizedBlankScores(question, count, total) {
  const raw = Array.isArray(question.blank_scores) ? question.blank_scores.map((value) => Number(value)) : []
  if (raw.length === count && raw.every((value) => Number.isFinite(value) && value > 0)) return raw
  return equalBlankScores(total, count)
}

function isCustomFill(question) {
  return question?.question_type === 'fill' && Number(question.blank_count) > 1 && question.fill_scoring_mode === 'custom'
}

function fillScoreTotal(question) {
  return (Array.isArray(question?.blank_scores) ? question.blank_scores : [])
    .reduce((sum, value) => sum + Math.max(0, Number(value) || 0), 0)
}

function onFillScoringModeChange(question) {
  const count = Math.min(20, Math.max(1, Number(question.blank_count) || 1))
  if (!['equal', 'all_or_nothing', 'custom'].includes(question.fill_scoring_mode)) question.fill_scoring_mode = 'equal'
  if (question.fill_scoring_mode === 'custom') {
    question.blank_scores = equalBlankScores(question.max_score, count)
    question.max_score = fillScoreTotal(question)
  } else {
    question.blank_scores = []
  }
}

function onBlankScoresChange(question) {
  if (!isCustomFill(question)) return
  const count = Math.min(20, Math.max(1, Number(question.blank_count) || 1))
  const raw = Array.isArray(question.blank_scores) ? question.blank_scores.map((value) => Number(value)) : []
  if (raw.length === count && raw.every((value) => Number.isFinite(value) && value > 0)) {
    question.max_score = raw.reduce((sum, value) => sum + value, 0)
  }
}

function onBlankCountChange(question) {
  question.blank_count = Math.min(20, Math.max(1, Number(question.blank_count) || 1))
  if (isCustomFill(question)) {
    question.blank_scores = equalBlankScores(question.max_score, question.blank_count)
    question.max_score = fillScoreTotal(question)
  }
}

function normalizeAnswerSpecForEditor(question) {
  const questionType = question.question_type || 'solve'
  const directoryLeaf = isTemplateDirectoryLeaf(question)
  const typedSlots = Object.hasOwn(question, 'region_kinds') || Object.hasOwn(question, 'slot_ids')
  const blankCount = typedSlots ? question.blank_count
    : directoryLeaf ? (Number(question.blank_count) || 1) : Math.max(Number(question.blank_count) || 1, inferBlankCount(question))
  const subquestionCount = directoryLeaf ? 1 : Math.max(Number(question.subquestion_count) || 1, inferSubquestionCount(question))
  const maxScore = Number(question.max_score) > 0 ? Number(question.max_score) : 5
  const standardAnswer = String(question.standard_answer ?? question.answer ?? '').trim()
  const normalizedChoiceAnswer = questionType === 'choice' ? normalizeChoiceAnswer(standardAnswer) : ''
  // Keep typed drafts as arrays so missing/invalid positions cannot be repaired by joining text.
  const normalizedAnswerSlots = typedSlots ? templateFillAnswerSlots(question)
    : directoryLeaf || (questionType === 'fill' && blankCount > 1)
    ? templateFillAnswerSlots({ ...question, blank_count: blankCount, standard_answer: standardAnswer }).join('\n')
    : toTextLines(question.answer_slots_text || question.answer_slots)
  const typedGraphic = Array.isArray(question.region_kinds) && question.region_kinds.includes('drawing')
  const fillScoringMode = !typedGraphic && ['equal', 'all_or_nothing', 'custom'].includes(question.fill_scoring_mode)
    ? question.fill_scoring_mode : 'equal'
  return {
    ...question,
    standard_answer: normalizedChoiceAnswer || standardAnswer,
    question_type: questionType,
    choice_mode: questionType === 'choice' && ['single', 'multiple'].includes(question.choice_mode)
      ? question.choice_mode : 'unknown',
    grading_method: question.grading_method || defaultGradingMethod(questionType),
    answer_variants_text: toTextLines(question.answer_variants_text || question.answer_variants),
    solution_steps_text: toTextLines(question.solution_steps_text || question.solution_steps),
    max_score: maxScore,
    blank_count: typedSlots ? blankCount : Math.min(20, Math.max(1, blankCount)),
    answer_slots_text: normalizedAnswerSlots,
    fill_scoring_mode: fillScoringMode,
    blank_scores: typedGraphic ? [] : typedSlots && !Number.isInteger(Number(blankCount))
      ? (Array.isArray(question.blank_scores) ? [...question.blank_scores] : [])
      : normalizedBlankScores(question, blankCount, maxScore),
    subquestion_count: Math.min(20, Math.max(1, subquestionCount)),
    subquestion_scores: directoryLeaf
      ? [Number(question.subquestion_scores?.[0]) || maxScore]
      : defaultSubquestionScores(question, subquestionCount),
  }
}

function answerSpecFields(question) {
  const questionType = question.question_type || 'solve'
  const directoryLeaf = isTemplateDirectoryLeaf(question)
  const typedSlots = Object.hasOwn(question, 'region_kinds') || Object.hasOwn(question, 'slot_ids')
  const blankCount = typedSlots ? question.blank_count
    : questionType === 'fill' || directoryLeaf ? Math.min(20, Math.max(1, Number(question.blank_count) || 1)) : 1
  const subquestionScores = questionType === 'solve'
    ? (Array.isArray(question.subquestion_scores) ? question.subquestion_scores.map((value) => Math.max(0, Number(value) || 0)) : [])
    : []
  const typedGraphic = Array.isArray(question.region_kinds) && question.region_kinds.includes('drawing')
  const fillScoringMode = !typedGraphic && questionType === 'fill' && blankCount > 1 && form.category === 'exam'
    ? (['equal', 'all_or_nothing', 'custom'].includes(question.fill_scoring_mode) ? question.fill_scoring_mode : 'equal')
    : 'equal'
  const blankScores = fillScoringMode === 'custom'
    ? (Array.isArray(question.blank_scores) ? question.blank_scores.map((value) => Number(value)) : [])
    : []
  // A standard figure needs a drawing position. Region marks decide that, as
  // on the server; a stale figure on a non-drawing item would block saving.
  const graphic = Array.isArray(question.region_kinds) && question.region_kinds.length
    ? typedGraphic : ['drawing', 'mixed'].includes(question.answer_kind)
  const drawingReference = graphic ? templateDrawingReference(question) : {}
  return {
    ...templateQuestionMetadata(question),
    ...drawingReference,
    question_type: questionType,
    choice_mode: questionType === 'choice' && ['single', 'multiple'].includes(question.choice_mode)
      ? question.choice_mode : 'unknown',
    answer_variants: toStringList(question.answer_variants_text || question.answer_variants),
    solution_steps: toStringList(question.solution_steps_text || question.solution_steps),
    grading_method: question.grading_method || defaultGradingMethod(questionType),
    max_score: form.category === 'exam' && ['choice', 'fill'].includes(questionType)
      ? Number(question.max_score || 0)
      : null,
    blank_count: blankCount,
    answer_slots: typedSlots || questionType === 'fill' || directoryLeaf ? templateFillAnswerSlots(question) : [],
    fill_scoring_mode: fillScoringMode,
    blank_scores: blankScores,
    subquestion_count: questionType === 'solve' && !directoryLeaf
      ? Math.min(20, Math.max(1, Number(question.subquestion_count) || 1))
      : 1,
    subquestion_scores: form.category === 'exam' && questionType === 'solve'
      ? (directoryLeaf ? subquestionScores.slice(0, 1) : subquestionScores) : [],
  }
}

function questionScoringValid(question) {
  const questionType = question.question_type || 'solve'
  if (!templateRegionSlotsValid(question)) return false
  if (question.region_kinds?.includes('drawing')
    && ((question.fill_scoring_mode && question.fill_scoring_mode !== 'equal') || question.blank_scores?.length)) return false
  if (questionType === 'choice' && !templateIsPureDrawing(question)) {
    const answer = normalizeChoiceAnswer(question.standard_answer ?? question.answer)
    if (question.choice_mode === 'single' && answer.length !== 1) return false
    if (question.choice_mode === 'multiple' && answer.length < 2) return false
    if (!['single', 'multiple'].includes(question.choice_mode)) return false
  }
  if (questionType === 'fill') {
    const blankCount = Number(question.blank_count) || 1
    if (blankCount < 1 || blankCount > 20) return false
    if (!templateIsPureDrawing(question) && !Object.hasOwn(question, 'region_kinds') && blankCount > 1 && toStringList(question.answer_slots_text || question.answer_slots).length !== blankCount) return false
    if (form.category === 'exam' && question.fill_scoring_mode === 'custom') {
      const scores = Array.isArray(question.blank_scores) ? question.blank_scores.map((value) => Number(value)) : []
      if (scores.length !== blankCount || scores.some((value) => !Number.isFinite(value) || value <= 0)) return false
      if (Math.abs(scores.reduce((sum, value) => sum + value, 0) - Number(question.max_score || 0)) > 0.0001) return false
    }
  }
  if (questionType === 'solve' && form.category !== 'exam') {
    const count = Number(question.subquestion_count) || 0
    if (count < 1 || count > 20) return false
  }
  if (form.category !== 'exam') return true
  if (['choice', 'fill'].includes(questionType)) return Number(question.max_score) > 0
  return Array.isArray(question.subquestion_scores) && question.subquestion_scores.some((value) => Number(value) > 0)
}

function onQuestionTypeChange(question) {
  const normalized = normalizeAnswerSpecForEditor({ ...question, question_type: question.question_type })
  Object.assign(question, normalized, { grading_method: defaultGradingMethod(question.question_type) })
}

function choiceModeLabel(mode) {
  return mode === 'single' ? '单选' : mode === 'multiple' ? '多选' : '待判型'
}

function setTemplateChoiceMode(question, mode) {
  if (question?.question_type !== 'choice' || !['single', 'multiple'].includes(mode)) return
  if (question.choice_mode === mode) return
  question.choice_mode = mode
  markTemplateQuestionInput(question)
}

function setSingleChoiceAnswer(question, option) {
  if (question?.question_type !== 'choice' || !choiceOptions.includes(option)) return
  question.standard_answer = option
  markTemplateAnswerManualEdit(question)
}

function onChoiceAnswerChange(question) {
  question.standard_answer = normalizeChoiceAnswer(question.standard_answer)
  markTemplateAnswerManualEdit(question)
}

function onChoiceAnswerInput(question, event) {
  const normalized = normalizeChoiceInput(event?.target?.value)
  question.standard_answer = normalized
  markTemplateAnswerManualEdit(question)
  if (event?.target && event.target.value !== normalized) event.target.value = normalized
}

function onTemplateFillSlotChange(question, slotIndex, event) {
  if (Object.hasOwn(question, 'region_kinds') && question.region_kinds?.[slotIndex] !== 'answer') return
  setTemplateFillAnswerSlot(question, slotIndex, event?.target?.value)
  markTemplateAnswerManualEdit(question)
}

function onTemplateAnswerSlotsInput(question) {
  if (!Array.isArray(question.region_kinds) || !question.region_kinds.includes('drawing')) {
    question.standard_answer = String(question.answer_slots_text || '').split(/\r?\n/).map((slot) => slot.trim()).join('; ')
  }
  markTemplateAnswerManualEdit(question)
}

function templateQuestionDomId(pageIndex, questionIndex) {
  return `template-question-${pageIndex}-${questionIndex}`
}

function syncTemplateBatchScores() {
  const firstChoice = templateChoiceReadyEntries.value.find(({ question }) => Number(question.max_score) > 0)
  const firstFill = templateFillEntries.value.find(({ question }) => !isCustomFill(question) && Number(question.max_score) > 0)
  templateChoiceScore.value = Number(firstChoice?.question.max_score) || 5
  templateFillScore.value = Number(firstFill?.question.max_score) || 5
}

function applyTemplateBatchScores() {
  if (form.category !== 'exam') return
  applyUniformTemplateScore(templatePages.value, 'choice', templateChoiceScore.value)
  applyUniformTemplateScore(templatePages.value, 'fill', templateFillScore.value)
}

function setTemplateDetailOpen(type, open) {
  templateReviewEntries(templatePages.value, type).forEach(({ question }) => { question._open = open })
}

function openTemplateQuestion(entry) {
  entry.question._open = true
  nextTick(() => document.getElementById(templateQuestionDomId(entry.pageIndex, entry.questionIndex))?.scrollIntoView({ behavior: 'smooth', block: 'center' }))
}

const templateMissingAnswerEntries = computed(() => templatePages.value.flatMap((page, pageIndex) =>
  (page.questions || []).map((question, questionIndex) => ({ pageIndex, questionIndex, question }))
    .filter(({ question }) => !question._pendingRemoval && templateMissingAnswerSlots(question).length)))

const templateMaxFlagReviewEntries = computed(() => templatePages.value.flatMap((page, pageIndex) =>
  (page.questions || []).map((question, questionIndex) => ({ pageIndex, questionIndex, question }))
    .filter(({ question }) => !question._pendingRemoval && templateMaxFlagEntries(question).length)))

const templatePendingRemovalEntries = computed(() => templatePages.value.flatMap((page) =>
  (page.questions || []).filter((question) => question._pendingRemoval)
    .map((question) => ({ pageIndex: Number(page.page_index), question }))))

const templateEditorProblemEntries = computed(() => templateEditorProblems())
// Boxes named in a problem row are highlighted on the page like flagged slots.
const templateProblemBoxes = computed(() => new Set(templateEditorProblemEntries.value.map((problem) => problem.box).filter(Boolean)))

async function openTemplateProblem(problem) {
  if (problem.entry) { openTemplateQuestion(problem.entry); return }
  if (problem.referenceNumber) { openTemplateDrawingReference(problem.pageIndex, problem.referenceNumber); return }
  const page = templateCutPreviewPages.value.find(item => Number(item.pageIndex) === Number(problem.pageIndex))
  if (!page) return
  selectTemplatePreviewPage(page)
  if (problem.locateSlot && problem.box) locateTemplateReviewSlot(page, problem.box)
  else if (problem.box && !templatePageEditingLocked(page)) {
    page.previewEditMode = 'geometry'
    page.geometryOverlayHidden = false
    selectTemplateGeometryTarget(page, { kind: problem.kind, target: problem.box })
  }
  await nextTick()
  document.getElementById(`template-reference-page-${page.pageIndex}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

async function openTemplateMaxFlags(entry) {
  const pageIndex = Number(templatePages.value[entry.pageIndex]?.page_index ?? entry.pageIndex)
  const page = templateCutPreviewPages.value.find(item => Number(item.pageIndex) === pageIndex)
  const ids = templateMaxFlagEntries(entry.question).map(flag => flag.slot_id).filter(Boolean)
  const box = page && templateSlotBoxes(page).find(item => ids.includes(item.slot_id)
    && String(item.question_number) === String(entry.question.question_no))
  if (!page || !box) { openTemplateQuestion(entry); return }
  entry.question._open = true
  selectTemplatePreviewPage(page)
  locateTemplateReviewSlot(page, box)
  await nextTick()
  const root = document.getElementById(`template-reference-page-${pageIndex}`)
  const target = [...(root?.querySelectorAll('[data-template-slot-id]') || [])]
    .find(element => element.dataset.templateSlotId === box.slot_id)
    || [...(root?.querySelectorAll('[data-template-slot-row]') || [])]
      .find(element => element.dataset.templateSlotRow === box.slot_id)
  ;(target || root)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

function templateSlotNeedsAnswer(page, box) {
  const question = templatePages.value.find(item => Number(item.page_index) === Number(page.pageIndex))?.questions
    ?.find(item => String(item.question_no) === String(box.question_number))
  return !!question && templateMissingAnswerSlots(question).some(slot => slot.slotId === box.slot_id)
}

function templateSlotMaxFlagged(page, box) {
  const question = templatePages.value.find(item => Number(item.page_index) === Number(page.pageIndex))?.questions
    ?.find(item => String(item.question_no) === String(box.question_number))
  return !!question && templateMaxFlagEntries(question).some(flag => flag.slot_id === box.slot_id)
}

function locateTemplateReviewSlot(page, box) {
  if (templatePageEditingLocked(page)) return
  page.previewEditMode = 'geometry'
  page.geometryOverlayHidden = false
  selectTemplateGeometryTarget(page, { kind: box.region_kind === 'drawing' ? 'drawing' : 'answer', target: box })
  page.geometryTool = box.bbox.length === 4 ? '' : (box.region_kind || 'answer')
  page.pendingSlotId = box.bbox.length === 4 ? '' : box.slot_id
}

async function openTemplateMissingAnswers(entry) {
  const pageIndex = Number(templatePages.value[entry.pageIndex]?.page_index ?? entry.pageIndex)
  const page = templateCutPreviewPages.value.find(item => Number(item.pageIndex) === pageIndex)
  entry.question._open = true
  if (!page) { openTemplateQuestion(entry); return }
  const ids = templateMissingAnswerSlots(entry.question).map(slot => slot.slotId)
  const boxes = templateSlotBoxes(page).filter(box => ids.includes(box.slot_id)
    && String(box.question_number) === String(entry.question.question_no))
  selectTemplatePreviewPage(page)
  if (boxes[0]) locateTemplateReviewSlot(page, boxes[0])
  await nextTick()
  const root = document.getElementById(`template-reference-page-${pageIndex}`)
  const target = [...(root?.querySelectorAll('[data-template-slot-id]') || [])]
    .find(element => ids.includes(element.dataset.templateSlotId))
    || [...(root?.querySelectorAll('[data-template-slot-row]') || [])]
      .find(element => ids.includes(element.dataset.templateSlotRow))
  ;(target || root)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

function addSubquestionScore(question) {
  if (isTemplateDirectoryLeaf(question)) return
  if (!Array.isArray(question.subquestion_scores)) question.subquestion_scores = []
  if (question.subquestion_scores.length < 20) question.subquestion_scores.push(8)
}

function removeSubquestionScore(question, index) {
  if (!Array.isArray(question.subquestion_scores) || question.subquestion_scores.length <= 1) return
  question.subquestion_scores.splice(index, 1)
}

function revokeIdentityImageUrls() {
  identityResolutions.value.forEach((item) => {
    if (item.image_url) URL.revokeObjectURL(item.image_url)
  })
}

async function loadIdentityImages() {
  await Promise.all(identityResolutions.value.map(async (item) => {
    try {
      item.image_url = await api.grade.paperImage(batchId.value, item.paper_id)
    } catch {
      item.image_url = ''
    }
  }))
}

function openIdentityImage(item) {
  if (item.image_url) window.open(item.image_url, '_blank', 'noopener,noreferrer')
}

async function openRecognizePaperImage(paper, variant = 'active') {
  const targetBatchId = paper.batch_id || batchId.value
  if (!targetBatchId || !paper.paper_id) {
    ElMessage.error('原图标识缺失，请重新上传后再试')
    return
  }
  const imageWindow = window.open('about:blank', '_blank')
  try {
    const imageUrl = await api.grade.paperImage(targetBatchId, paper.paper_id, variant)
    if (imageWindow) {
      imageWindow.opener = null
      imageWindow.location.href = imageUrl
    } else {
      window.open(imageUrl, '_blank', 'noopener,noreferrer')
    }
    window.setTimeout(() => URL.revokeObjectURL(imageUrl), 60_000)
  } catch (err) {
    imageWindow?.close()
    notifyProcessingError(err, err.message || '原图加载失败')
  }
}

function identityReasonHint(reason) {
  const hints = {
    name_ambiguous: '当前班级存在同名学生，学号线索不足以区分，请确认学生身份。',
    identity_name_conflict: '图片、文件名或文件夹中的姓名指向不同学生，请结合原图确认，系统不会自动覆盖。',
    identity_number_conflict: '学号线索对应了多名学生，可能包含重名编号或收集工具后缀，请确认后再归属。',
    roster_name_ambiguous: 'AI 在当前班级名单中给出了多个相近候选，请结合原图选择。',
    roster_name_low_confidence: 'AI 只给出了低置信候选，请结合原图确认后再选择。',
    roster_name_not_found: 'AI 未在当前班级名单中找到可靠候选。正常情况下请先维护名单；确认无误时仍可例外新建。',
  }
  return hints[reason] || ''
}

function identityClueLabel(source) {
  const labels = { ocr: '图片识别', filename: '图片文件名', folder: '文件夹名' }
  return String(source || '').split('+').map(key => labels[key]).filter(Boolean).join('、') || '待老师确认'
}

async function prepareIdentityResolutions() {
  revokeIdentityImageUrls()
  identityOptionsError.value = ''
  identityResolutions.value = buildIdentityResolutionItems(papers.value)
  await refreshIdentityOptions()
  loadIdentityImages()
}

async function refreshIdentityOptions() {
  if (!batchId.value || !identityResolutions.value.length || identityOptionsLoading.value) return
  identityOptionsError.value = ''
  identityOptionsLoading.value = true
  try {
    const data = await api.grade.identityOptions(batchId.value)
    identityResolutions.value = mergeRosterIntoIdentityItems(
      identityResolutions.value,
      data.students || [],
    )
  } catch (err) {
    identityOptionsError.value = '未能刷新本批次班级名单，请检查网络后重新加载。'
  } finally {
    identityOptionsLoading.value = false
  }
}

function identityResolutionPayload() {
  if (anonymous.value) return []
  return serializeIdentityResolutions(identityResolutions.value)
}

// 有模板走确认权威路后，识别阶段锁定模式选择（避免中途切换）
const templateLocked = computed(() => recognizing.value || grading.value || recognizingTemplate.value || templateDialogVisible.value || !!pendingTemplateId.value)

// 批次确认弹窗校验：批次类型必填；考试须填名称；模板在前一步已确认
const batchConfirmValid = computed(() => {
  if (!form.category) return false
  if (form.category === 'exam' && !form.exam_name.trim()) return false
  return Boolean(confirmedTemplateId.value)
})

function requireBatchService() {
  if (!serviceBlocksNewWork.value) return true
  ElMessage.error(serviceState.value.message || '批改链路当前不可用，请稍后再试。')
  return false
}

function requireRecognitionService() {
  if (!requireBatchService()) return false
  if (!registrationHardwareBlocksNewWork.value) return true
  ElMessage.error(ACCELERATOR_UNAVAILABLE_MESSAGE)
  return false
}

function triggerFileUpload() {
  if (imagePreprocessing.value || imageExtracting.value) {
    ElMessage.warning('图片正在处理中，请稍候')
    return
  }
  if (requireRecognitionService()) fileInputRef.value?.click()
}

function triggerFolderUpload() {
  if (imagePreprocessing.value || imageExtracting.value) {
    ElMessage.warning('图片正在处理中，请稍候')
    return
  }
  if (requireRecognitionService()) folderInputRef.value?.click()
}

function triggerTemplateUpload() {
  if (templatePreprocessing.value) {
    ElMessage.warning('模板图片正在处理中，请稍候')
    return
  }
  if (requireTemplateService()) templateInputRef.value?.click()
}

function requireTemplateService() {
  if (!templateServiceBlocksNewWork.value) return true
  ElMessage.error(templateServiceState.value.message || '模板识别链路当前不可用，请稍后再试。')
  return false
}

// ── 多页一次性模板：导入 N 页 → 识别 → 确认（即本次批改绝对权威）──
async function onTemplateFileChange(e) {
  const raw = Array.from(e.target.files || [])
  e.target.value = ''
  if (!raw.length || templatePreprocessing.value || !requireTemplateService()) return
  templatePreprocessing.value = true
  let failedCount = 0
  const failedFiles = []
  try {
    const extracted = await extractImagesFromFileList(raw, { maxFiles: 20 })
    const fakeList = extracted.map((f) => ({ raw: f, name: f.name, size: f.size }))
    const result = validateUploadList(fakeList, { maxFiles: 20 })
    if (result.errors.length) throw new Error(result.errors.join('；'))
    if (!result.files.length) return
    const preparedFiles = await compressImageFiles(result.files, 1920, 0.88, {
      onFileError: ({ file }) => { failedCount += 1; failedFiles.push(file.name) },
    })
    if (failedCount) {
      templateFiles.value = []
      ElMessage.error(`[IMPORT_IMAGE_INVALID] ${failedFiles.join('、')}：${failedCount} 张模板图片损坏或尺寸超限，本次选择已清空，请检查原图后重新选择`)
      return
    }
    templateFiles.value = preparedFiles
    clearReviewDisplay()
  } catch (err) {
    notifyProcessingError(err, err.message || '模板图片预处理未完成，请重新选择')
  } finally {
    templatePreprocessing.value = false
  }
}

function templateRecognitionStorageKey() {
  const teacherId = auth.user?.id
  const mode = anonymous.value ? ':anonymous' : ''
  return teacherId ? `${TEMPLATE_RECOGNITION_STORAGE_PREFIX}${mode}:${teacherId}` : ''
}

function normalizeTemplateMode(value, fallback = 'blank') {
  const mode = String(value || '').trim().toLowerCase()
  if (mode === 'blank' || mode === 'marked') return mode
  return fallback === 'marked' ? 'marked' : 'blank'
}

function rememberTemplateRecognition(templateId, mode = templateMode.value) {
  const key = templateRecognitionStorageKey()
  if (!key || !templateId) return
  localStorage.setItem(key, JSON.stringify({
    template_id: String(templateId),
    category: form.category,
    exam_name: form.exam_name,
    template_mode: normalizeTemplateMode(mode, templateMode.value),
    column_mode: normalizeTemplateColumnMode(templateColumnMode.value),
  }))
}

function normalizeTemplateColumnMode(value, fallback = 'double') {
  const mode = String(value || '').trim().toLowerCase()
  if (['single', 'double', 'triple'].includes(mode)) return mode
  return ['single', 'double', 'triple'].includes(fallback) ? fallback : 'double'
}

function forgetTemplateRecognition() {
  const key = templateRecognitionStorageKey()
  if (key) localStorage.removeItem(key)
}

function loadTemplateRecognitionCheckpoint() {
  const key = templateRecognitionStorageKey()
  if (!key) return null
  try {
    const saved = JSON.parse(localStorage.getItem(key) || 'null')
    if (!saved?.template_id) throw new Error('invalid template checkpoint')
    return saved
  } catch {
    localStorage.removeItem(key)
    return null
  }
}

function isTerminalTemplateFailure(error) {
  return Boolean(error?.code) && error?.sseFailureKind === 'application'
}

function waitForTemplatePoll(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      const error = new Error('模板识别状态查询已取消')
      error.name = 'AbortError'
      reject(error)
      return
    }
    const onAbort = () => {
      window.clearTimeout(timer)
      const error = new Error('模板识别状态查询已取消')
      error.name = 'AbortError'
      reject(error)
    }
    const timer = window.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, milliseconds)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

async function waitForTemplateRecognition(templateId, signal) {
  let consecutiveReadFailures = 0
  while (true) {
    let state
    try {
      state = await api.grade.templateRecognitionState(templateId, signal)
      consecutiveReadFailures = 0
    } catch (error) {
      if (isSseRequestCancelled(error, signal)) throw error
      consecutiveReadFailures += 1
      if (consecutiveReadFailures >= 3) throw error
      templateRecognizeMessage.value = '模板仍在服务器处理，正在重新连接任务状态…'
      await waitForTemplatePoll(1000 * (2 ** (consecutiveReadFailures - 1)), signal)
      continue
    }

    const status = String(state?.status || '')
    if (['ocr_done', 'confirmed', 'frozen'].includes(status)) {
      const pages = Array.isArray(state.pages) ? state.pages : []
      if (!pages.length) throw new SseTransportError('模板任务已完成，但服务器未返回页面结果')
      return { templateId: String(state.template_id || templateId), pages, state }
    }
    if (status === 'failed' || state?.terminal === true) {
      throw createSseEventError({
        code: state?.error_code || 'TEMPLATE_IMPORT_FAILED',
        message: state?.error || state?.message || '模板识别失败',
        error_info: state?.error_info,
      }, '模板识别失败')
    }

    templateRecognizeMessage.value = state?.message || '模板仍在服务器处理中，请勿重复提交…'
    const retryAfter = Math.min(5000, Math.max(500, Number(state?.retry_after_ms) || 1000))
    await waitForTemplatePoll(retryAfter, signal)
  }
}

async function applyTemplateRecognitionResult(tid, pages, mode = '', columnMode = '', teachingContext = {}) {
  const runToken = ++templateCutRunToken
  templateCutCompletedRunToken = null
  templateCutController?.abort()
  templateCutController = null
  stopTemplateCutPolling()
  templateCutBusy.value = false
  revokeTemplateCutPreviews()
  templateCutPreviewError.value = ''
  // Keep the unfinished-draft checkpoint until the template is confirmed or
  // cancelled: an unconfirmed template must stay recoverable after a reload
  // instead of being silently orphaned with no editor entry.
  pendingTemplateId.value = tid
  templateTeachingContext.value = teachingContext || {}
  templateMode.value = normalizeTemplateMode(mode, templateMode.value)
  templateColumnMode.value = normalizeTemplateColumnMode(columnMode || (
    pages?.[0]?.column_mode || pages?.[0]?.layout_mode || templateColumnMode.value
  ))
  templateBankImported.value = false
  templateCutResult.value = null
  templateCutActionId.value = ''
  templateCutProgress.value = { completed: 0, total: pages.length, remaining: pages.length }
  templatePages.value = pages.map((page) => ({
    ...page,
    template_mode: templateMode.value,
    layout_review_pending: page.layout_review_pending === true,
    anchor_diagnostics: page.anchor_diagnostics || null,
    template_ocr_diagnostics: page.template_ocr_diagnostics || null,
    questions: (page.questions || []).map((question) => {
      const normalized = normalizeAnswerSpecForEditor(question)
      return {
        ...normalized,
        _open: normalized.question_type === 'solve',
        correction_excluded: normalized.correction_excluded === true,
        authorized_wrong_cycle_ids: [],
        correction_candidates: [],
        correction_knowledge_summary: null,
      }
    }),
  }))
  syncTemplateBatchScores()
  templateDialogVisible.value = true
  const existingCut = await api.grade.templateCutCandidatesState(tid).catch(() => null)
  if (tid !== pendingTemplateId.value || runToken !== templateCutRunToken) return
  if (existingCut?.status === 'complete') {
    applyTemplateCutState(existingCut, runToken)
  } else {
    void onRunTemplateCut()
  }
  void loadTemplateCorrectionCandidates()
}

async function onRecognizeTemplate() {
  if (recognizingTemplate.value) return
  if (!requireTemplateService()) return
  if (!templateFiles.value.length) { ElMessage.warning('请先导入模板图片'); return }
  if (!form.category) { ElMessage.warning('请先选择本次批阅类型'); return }
  if (form.category === 'exam' && !form.exam_name.trim()) { ElMessage.warning('请先填写考试名称'); return }
  const fd = new FormData()
  templateFiles.value.forEach((f) => fd.append('files', f.raw || f))
  fd.append('category', form.category)
  fd.append('exam_name', form.category === 'exam' ? form.exam_name.trim() : '')
  fd.append('template_mode', normalizeTemplateMode(templateMode.value))
  const columnMode = normalizeTemplateColumnMode(templateColumnMode.value)
  fd.append('column_mode', columnMode)
  fd.append('layout_mode', columnMode)
  recognizingTemplate.value = true
  templateRecognizeError.value = ''
  templateRecognizeMessage.value = '正在上传并准备模板…'
  templateRecognizeController = new AbortController()
  let tid = ''
  try {
    await appendDesktopCanonicalCandidates(fd, templateFiles.value)
    const response = await api.grade.templateRecognize(fd, templateRecognizeController.signal)
    if (!response.ok) {
      throw await createSseResponseError(response, '模板识别失败')
    }
    tid = String(response.headers.get('X-Template-ID') || '')
    if (tid) rememberTemplateRecognition(tid, templateMode.value)
    let pages = []
    let teachingContext = {}
    await consumeSse(response, (evt, data) => {
      if (data?.teaching_context) teachingContext = data.teaching_context
      if (data?.template_id) {
        tid = String(data.template_id)
        if (data.template_mode) templateMode.value = normalizeTemplateMode(data.template_mode, templateMode.value)
        if (data.column_mode || data.layout_mode) {
          templateColumnMode.value = normalizeTemplateColumnMode(data.column_mode || data.layout_mode)
        }
        rememberTemplateRecognition(tid, templateMode.value)
      }
      if (evt === 'done') {
        if (data?.template_mode) templateMode.value = normalizeTemplateMode(data.template_mode, templateMode.value)
        if (data?.column_mode || data?.layout_mode) {
          templateColumnMode.value = normalizeTemplateColumnMode(data.column_mode || data.layout_mode)
        }
        pages = data.pages || []
      }
      else if (evt === 'error') { throw createSseEventError(data, '模板识别出错') }
      else if (evt === 'progress') templateRecognizeMessage.value = data.message || '模板识别中…'
    })
    if (!tid || !pages.length) {
      throw new SseTransportError('识别连接已结束，但没有收到模板结果')
    }
    await applyTemplateRecognitionResult(tid, pages, templateMode.value, templateColumnMode.value, teachingContext)
  } catch (err) {
    const signal = templateRecognizeController?.signal
    let failure = err
    const rawFailure = String(failure?.message || '')
    const canRecover = Boolean(tid) && (
      failure instanceof SseTransportError
      || failure?.sseFailureKind === 'transport'
      || /network|fetch|连接提前结束|连接读取中断/i.test(rawFailure)
    )
    if (canRecover && !isSseRequestCancelled(failure, signal)) {
      try {
        templateRecognizeError.value = ''
        templateRecognizeMessage.value = '模板识别连接中断，正在恢复服务器任务…'
        const recovered = await waitForTemplateRecognition(tid, signal)
        await applyTemplateRecognitionResult(
          recovered.templateId,
          recovered.pages,
          recovered.state?.template_mode || templateMode.value,
          recovered.state?.column_mode || recovered.state?.layout_mode || templateColumnMode.value,
          recovered.state?.teaching_context || {},
        )
        return
      } catch (recoveryError) {
        failure = recoveryError
      }
    }
    if (
      isTerminalTemplateFailure(failure)
      || [401, 403, 404].includes(Number(failure?.status || 0))
    ) forgetTemplateRecognition()
    if (!isSseRequestCancelled(failure, signal)) {
      reportSseServiceFault(
        failure,
        markServiceFault,
        '模板识别链路中断，请稍后重试；若持续发生请联系管理员。',
        { signal },
      )
      const raw = String(failure.message || '')
      templateRecognizeError.value = failure instanceof SseTransportError || /network|fetch|连接提前结束/i.test(raw)
        ? '模板识别连接中断，且无法读取服务器任务状态。请勿连续重复提交；稍后重试，如仍失败请联系管理员。'
        : (raw || '模板识别失败')
      notifyProcessingError(failure, templateRecognizeError.value)
    }
  } finally {
    recognizingTemplate.value = false
    templateRecognizeMessage.value = ''
    templateRecognizeController = null
  }
}

async function resumeTemplateRecognitionCheckpoint() {
  const checkpoint = loadTemplateRecognitionCheckpoint()
  if (!checkpoint?.template_id || recognizingTemplate.value) return false
  const checkpointCategory = String(checkpoint.category || '').trim().toLowerCase()
  if (!['daily', 'exam'].includes(checkpointCategory)) {
    forgetTemplateRecognition()
    return false
  }
  form.category = checkpointCategory
  form.exam_name = form.category === 'exam' ? String(checkpoint.exam_name || '') : ''
  templateMode.value = normalizeTemplateMode(checkpoint.template_mode, 'marked')
  templateColumnMode.value = normalizeTemplateColumnMode(checkpoint.column_mode)
  recognizingTemplate.value = true
  templateRecognizeError.value = ''
  templateRecognizeMessage.value = '正在恢复上次未完成的模板识别…'
  templateRecognizeController = new AbortController()
  try {
    const recovered = await waitForTemplateRecognition(
      String(checkpoint.template_id),
      templateRecognizeController.signal,
    )
    const stateCategory = String(recovered.state?.category || '')
    if (['daily', 'exam'].includes(stateCategory)) {
      form.category = stateCategory
      form.exam_name = stateCategory === 'exam'
        ? String(recovered.state?.exam_name || checkpoint.exam_name || '')
        : ''
    }
    if (String(recovered.state?.status || '') === 'frozen') {
      // The template already drives student recognition and cannot be edited
      // any more; retire the checkpoint instead of reopening a frozen editor.
      forgetTemplateRecognition()
      return false
    }
    await applyTemplateRecognitionResult(
      recovered.templateId,
      recovered.pages,
      recovered.state?.template_mode || templateMode.value,
      recovered.state?.column_mode || recovered.state?.layout_mode || templateColumnMode.value,
      recovered.state?.teaching_context || {},
    )
    ElMessage.success('已恢复服务器完成的模板识别结果')
    return true
  } catch (failure) {
    if (
      isTerminalTemplateFailure(failure)
      || [401, 403, 404].includes(Number(failure?.status || 0))
    ) forgetTemplateRecognition()
    const signal = templateRecognizeController?.signal
    if (!isSseRequestCancelled(failure, signal)) {
      reportSseServiceFault(
        failure,
        markServiceFault,
        '无法恢复模板识别任务，请检查网络或联系管理员。',
        { signal },
      )
      templateRecognizeError.value = failure?.message || '模板识别任务恢复失败'
      notifyProcessingError(failure, templateRecognizeError.value)
    }
    return false
  } finally {
    recognizingTemplate.value = false
    templateRecognizeMessage.value = ''
    templateRecognizeController = null
  }
}

function templateCutPageOriginalCount(pageIndex) {
  const page = (templateCutResult.value?.pages || []).find((item) => Number(item.page_index) === Number(pageIndex))
  const result = page?.modes?.question
  if (!result) return 0
  return (result.pages || []).reduce((total, providerPage) => total + (providerPage.candidates || [])
    .filter((candidate) => candidate.coordinate_trust === 'original').length, 0)
}

function stopTemplateCutPolling() {
  if (templateCutPollTimer) clearTimeout(templateCutPollTimer)
  templateCutPollTimer = null
}

function releaseFinishedTemplateCut(runToken) {
  if (runToken !== templateCutRunToken) return
  // The finished run is persisted server-side, so the pending POST no longer
  // owns the editor: holding the lock until it returns locks the teacher out of
  // a template that is already done.
  templateCutBusy.value = false
  stopTemplateCutPolling()
}

function applyTemplateCutState(data, expectedRunToken = templateCutRunToken) {
  if (!pendingTemplateId.value || expectedRunToken !== templateCutRunToken) return
  if (!data || (templateCutActionId.value && data.action_id && data.action_id !== templateCutActionId.value)) return
  const finished = ['complete', 'failed'].includes(data.status)
  if (templateCutCompletedRunToken === expectedRunToken) {
    releaseFinishedTemplateCut(expectedRunToken)
    return
  }
  if (data.progress) templateCutProgress.value = data.progress
  if (['scan', 'photo'].includes(data.image_type)) templateCutImageType.value = data.image_type
  if (data.status !== 'not_run') {
    templateCutResult.value = data
    const pendingPages = templateReviewPendingPageIndexes(data)
    templateCutPreviewPages.value.forEach((page) => {
      const pending = pendingPages.has(Number(page.pageIndex))
        || data.pages?.some((item) => Number(item.page_index) === Number(page.pageIndex) && templatePageReviewPending(item))
      if (pending) {
        page.layoutReviewPending = true
        page.layout_review_pending = true
        page.anchorReviewPending = true
        page.geometryReviewPending = true
        page.columnsConfirmed = false
      }
    })
    if (finished) {
      templateCutCompletedRunToken = expectedRunToken
      releaseFinishedTemplateCut(expectedRunToken)
    }
    if (data.status === 'failed') {
      templateCutPreviewError.value = templateCutErrorMessage(data, '模板自动裁剪失败，请重试。')
    }
    if (data.status === 'complete') void loadTemplateCutPreviews(data, expectedRunToken)
  }
}

function revokeTemplateCutPreviews() {
  templateCutPreviewLoadToken += 1
  templateCutPreviewPages.value.forEach((page) => {
    if (page.imageUrl) URL.revokeObjectURL(page.imageUrl)
  })
  templateCutPreviewPages.value = []
}

function templatePreviewQuestionIdentity(page, anchor) {
  const number = String(anchor.question_number ?? anchor.question_no ?? '')
  const schema = page.questionSchema.find((item) => String(item.question_number ?? item.question_no ?? '') === number)
  return templateQuestionIdentity({ ...anchor, ...schema })
}

function applyTemplateDrawingSchema(page, schema = []) {
  page.questionSchema = (schema || []).map((item) => ({ ...item, ...templateQuestionMetadata(item) }))
  Object.assign(page, templateDrawingReview(page.questionSchema))
}

function templateHasDrawingQuestion(page, number) {
  return page.questionSchema.some((item) => String(item.question_number ?? item.question_no) === String(number)
    && ['drawing', 'mixed'].includes(item.answer_kind))
}

function templateDrawingLabelsMissing(page) {
  return page.questionSchema.filter((question) => ['drawing', 'mixed'].includes(question.answer_kind)
    && !page.drawingLabels.some((label) => String(label.question_number) === String(question.question_number ?? question.question_no)))
}

function templateLayoutConflictNotice(page) {
  const candidateReviewPending = Boolean(page?.layoutReviewPending || page?.layout_review_pending
    || page?.anchorReviewPending || page?.anchor_review_pending
    || page?.geometryReviewPending || page?.geometry_review_pending
    || page?.question_schema_candidates?.length)
  if (!candidateReviewPending) return ''
  const schema = serializeTemplateDrawingReview(
    page?.questionSchema || [], page?.drawingBoxes || [], page?.drawingLabels || [],
    { preserveConflictingGeometry: true },
  )
  const validBox = (value) => Array.isArray(value) && value.length === 4
    && value.every((part) => typeof part === 'number' && Number.isFinite(part) && part >= 0 && part <= 1000)
    && value[0] < value[2] && value[1] < value[3]
  const validPoint = (value) => Array.isArray(value) && value.length === 2
    && value.every((part) => typeof part === 'number' && Number.isFinite(part) && part >= 0 && part <= 1000)
  const conflicts = []
  for (const question of schema) {
    const number = String(question.question_number ?? question.question_no ?? '').trim()
    if (!number) continue
    // A question whose boxes were all deleted leaves on save; its geometry no longer matters.
    if (templateAnswerQuestion(page, number)?._pendingRemoval) continue
    const regions = Array.isArray(question.drawing_regions) ? question.drawing_regions : []
    const hasAnyRegion = regions.length > 0
    const validRegions = regions.filter((region) => validBox(region))
    const hasAnchorValue = question.label_anchor != null
    const hasValidAnchor = validPoint(question.label_anchor)
    if (question.answer_kind === 'non_graphic' && (hasAnyRegion || hasAnchorValue)) {
      conflicts.push(`Q${number} 是非作图题，但仍带有画图区或作图题号（删除它们，或把作图属性改为作图题）`)
      continue
    }
    if (!['drawing', 'mixed'].includes(question.answer_kind)) continue
    // A missing Q marker is re-derived beside the drawing area at save time.
    if (!validRegions.length) conflicts.push(`Q${number} 是${question.answer_kind === 'mixed' ? '图文混合题' : '作图题'}，但还没有画图区`)
    else if (hasAnchorValue && !hasValidAnchor) conflicts.push(`Q${number} 的作图题号位置无效`)
  }
  if (!conflicts.length) return ''
  return `${conflicts.join('；')}。请用“绿色画图区”框出作图位置，或把该题“作图属性”改为非作图；当前草稿中的修改已保留。`
}

function templateDrawingKind(page) {
  return page.questionSchema.find((item) => String(item.question_number ?? item.question_no) === String(page.drawingQuestionNumber))?.answer_kind || 'non_graphic'
}

function setTemplateDrawingKind(page, kind) {
  if (templatePageEditingLocked(page)) return
  if (!['non_graphic', 'drawing', 'mixed'].includes(kind)) return
  const question = page.questionSchema.find((item) => String(item.question_number ?? item.question_no) === String(page.drawingQuestionNumber))
  const staleNonGraphicGeometry = kind === 'non_graphic'
    && (Array.isArray(question?.drawing_regions) && question.drawing_regions.length > 0
      || question?.label_anchor != null
      || question?.region_kinds?.includes('drawing'))
  if (!question || (question.answer_kind === kind && !staleNonGraphicGeometry)) return
  const number = String(page.drawingQuestionNumber)
  const slotQuestion = templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))?.questions
    ?.find((item) => String(item.question_no) === number)
  if (kind !== 'non_graphic' && slotQuestion?.slot_ids
    && !(page.drawingBoxes || []).some((item) => String(item.question_number) === number)) {
    // 作图属性以教师框出的画图区为准：先框选，属性随框生效。
    page.geometryQuestionNumber = number
    ElMessage.info(`请在原图上框出 Q${number} 的画图区`)
    void armTemplateGeometryTool(page, 'drawing')
    return
  }
  if (kind !== 'non_graphic' && slotQuestion?.slot_ids && slotQuestion.region_kinds?.includes('drawing')) {
    // 有画图区时，“作图题/图文混合”由框决定：另有文字作答框即为图文混合。
    const derived = slotQuestion.region_kinds.includes('answer') ? 'mixed' : 'drawing'
    if (derived !== kind) {
      ElMessage.info(derived === 'mixed'
        ? `Q${number} 还有文字作答框，按图文混合保存；只作图请删除文字作答框`
        : `Q${number} 只有画图区，按作图题保存；需要文字作答请用“蓝色作答框”框出位置`)
    }
    return
  }
  clearTemplateSelection(page)
  page.geometryTool = ''
  if (kind === 'non_graphic' && slotQuestion?.slot_ids && slotQuestion.region_kinds?.includes('drawing')) {
    if (slotQuestion.region_kinds.includes('answer')) {
      // 图文混合改为非作图：作图位置随之删除（与逐个删除画图区相同）。
      const drawingSlots = slotQuestion.slot_ids.filter((_, index) => slotQuestion.region_kinds[index] === 'drawing')
      for (const slotId of drawingSlots.reverse()) removeTemplateSlot(page, slotQuestion, slotId)
    } else {
      // 纯作图题改为非作图：画图区改作文字作答框，位置保留。
      convertTemplateDrawingSlots(page, slotQuestion)
    }
  } else if (kind === 'non_graphic' && !slotQuestion?.slot_ids
    && !(page.answerBoxes || []).some((box) => String(box.question_number) === number)) {
    // 无槽编号的纯作图题：同样把画图区改作文字作答框，不让题目失去位置。
    for (const box of (page.drawingBoxes || []).filter((item) => String(item.question_number) === number)) {
      ;(page.answerBoxes ||= []).push({ question_number: number, bbox: [...box.bbox] })
      page.hasAnswerRegionContract = true
    }
  }
  question.answer_kind = kind
  question._teacherDrawingKindEdited = true
  if (kind === 'non_graphic') {
    question.drawing_regions = []
    question.label_anchor = null
    delete question.drawing_reference
    page.drawingBoxes = page.drawingBoxes.filter((item) => String(item.question_number) !== String(page.drawingQuestionNumber))
    page.drawingLabels = page.drawingLabels.filter((item) => String(item.question_number) !== String(page.drawingQuestionNumber))
  }
  const answerQuestion = templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))?.questions
    ?.find((item) => String(item.question_no) === String(page.drawingQuestionNumber))
  if (answerQuestion) {
    Object.assign(answerQuestion, templateQuestionMetadata(question))
    if (kind === 'non_graphic') {
      delete answerQuestion.drawing_reference
      answerQuestion._drawingReferenceDirty = false
    } else {
      page.referenceQuestionNumber = String(page.drawingQuestionNumber)
    }
  }
  markTemplatePageGeometryDirty(page, page.drawingQuestionNumber)
}


function resetTemplateColumns(page) {
  if (templatePageEditingLocked(page)) return
  clearTemplateSelection(page)
  page.geometryTool = ''
  const count = Math.max(1, Math.min(3, Number(page.columnCount) || 2))
  page.columns = equalTemplateColumns(count)
  // Equal partitions are only a teacher-editable draft.  They must not be
  // mistaken for a model-detected layout until this page is saved.
  page.columnsSource = 'teacher_draft'
  page.columnsConfirmed = false
  page.layoutReviewPending = true
  page.layout_review_pending = true
  markTemplatePageGeometryDirty(page)
}


function initializeTemplatePageSlots(page) {
  const questions = templatePages.value.find(item => Number(item.page_index) === Number(page.pageIndex))?.questions || []
  try {
    prepareTemplateSlots(page, questions)
    page.slotContractError = ''
  } catch (error) {
    page.slotContractError = error.message
  }
}

function templateSlotAnswer(page, box) {
  const question = templatePages.value.find(item => Number(item.page_index) === Number(page.pageIndex))?.questions
    ?.find(item => String(item.question_no) === String(box.question_number))
  const index = question?.slot_ids?.indexOf(box.slot_id) ?? -1
  if (index < 0) return '答案未绑定'
  return question.region_kinds?.[index] === 'drawing' ? '作图（不填文字）' : templateFillAnswerSlots(question)[index] || '待填写答案'
}

async function correctTemplateSlotPosition(page, box) {
  if (templatePageEditingLocked(page) || templateCropGesture.value || page.slotContractError) return
  const choices = templateSlotBoxes(page).filter(item => String(item.question_number) === String(box.question_number)
    && item.slot_id !== box.slot_id && item.bbox?.length === 4)
  if (!choices.length) return
  try {
    const { value } = await ElMessageBox.prompt(
      `将 Q${box.slot_id} 的物理框与另一槽调换。答案、类型和分数仍绑定原编号，不随框移动。可选：${choices.map(item => item.slot_id).join('、')}`,
      '修正槽位对应位置',
      { inputPlaceholder: choices[0].slot_id, inputValidator: value => choices.some(item => item.slot_id === value) || '请输入上方完整槽编号（不含 Q）', confirmButtonText: '仅调换物理框', cancelButtonText: '取消' },
    )
    if (templatePageEditingLocked(page) || !templateCutPreviewPages.value.includes(page)) return
    if (swapTemplateSlotGeometry(page, box.slot_id, value)) {
      markTemplatePageGeometryDirty(page, box.question_number)
      clearTemplateSelection(page)
      ElMessage.success('已调换物理框；槽编号对应的答案未改变，保存模板后生效')
    }
  } catch (error) {
    if (error instanceof Error) ElMessage.warning(error.message)
  }
}

function templatePageEditingLocked(page) {
  return templateEditorBusy.value || !!page.saving
}

function templatePreviewPageIndex() {
  return templateCutPreviewPages.value.some((page) => Number(page.pageIndex) === templateActivePreviewPage.value)
    ? templateActivePreviewPage.value : Number(templateCutPreviewPages.value[0]?.pageIndex || 0)
}

function templatePreviewPagePosition() {
  return templateCutPreviewPages.value.findIndex((page) => Number(page.pageIndex) === templatePreviewPageIndex())
}

function selectTemplatePreviewPage(page) {
  if (!page || templatePageEditingLocked(page)) return
  if (templateCropGesture.value) cancelTemplateGeometry({ pointerId: templateCropGesture.value.pointerId })
  templateActivePreviewPage.value = Number(page.pageIndex)
  void nextTick(() => document.getElementById(`template-reference-page-${page.pageIndex}`)?.scrollIntoView({ block: 'start' }))
}

function templateGeometryQuestions(page) {
  const byNumber = new Map()
  for (const item of [...(page.questionAnchors || []), ...(page.questionSchema || [])]) {
    const number = String(item.question_number ?? item.question_no ?? '').trim()
    if (number) byNumber.set(number, { ...byNumber.get(number), ...item, question_number: number })
  }
  return [...byNumber.values()]
}

function templateGeometryQuestionNumber(page) {
  const questions = templateGeometryQuestions(page)
  const preferred = page.geometryQuestionNumber || page.drawingQuestionNumber || page.editQuestionNumber
  return questions.some((item) => item.question_number === String(preferred))
    ? String(preferred) : questions[0]?.question_number || ''
}

function templateGeometryToolLabel(kind) {
  return TEMPLATE_GEOMETRY_TOOLS.find((tool) => tool.key === kind)?.label || '标记'
}

function selectTemplateGeometryTarget(page, selected) {
  clearTemplateSelection(page)
  page.geometrySelection = selected
  const number = selected.target?.question_number
  if (number) {
    page.geometryQuestionNumber = String(number)
    page.drawingQuestionNumber = String(number)
    page.editQuestionNumber = String(number)
    page.newHandwritingQuestion = String(number)
    if (templateHasDrawingQuestion(page, number)) page.referenceQuestionNumber = String(number)
  }
}

function templateGeometryHint(page) {
  if (page.pendingSlotId) return `请在原图上为 Q${page.pendingSlotId} 划出作答区域；若为多余区域，请点击“删除所选”。`
  if (page.geometrySelection?.target?.slot_id) return `已选中 Q${page.geometrySelection.target.slot_id}，移动只改坐标；使用图外“调换位置”纠正槽顺序。删除将同步移除该槽答案及分值，不顺延其余编号。`
  if (page.geometryTool === 'drawing-label') return '在作图区左侧点一下，放置作图 Q；标签向左展开。'
  if (page.geometryTool) return `用鼠标拖拽或触屏划框，新增 Q${templateGeometryQuestionNumber(page)} ${templateGeometryToolLabel(page.geometryTool)}${page.geometryTool === 'question' ? '（替换当前题号框）' : ''}。`
  if (page.referenceStrokeSelected) return '已选中标准图笔画，可点击“删除所选”。'
  if (page.geometrySelection) return `已选中${page.geometrySelection.kind === 'divider' ? '分栏线（删除后合并相邻栏）' : templateGeometryToolLabel(page.geometrySelection.kind)}，可拖动或删除；只删标记，不删题目。`
  return '鼠标 / 触屏：选新增类型后划框；点选标记移动、缩放或删除。'
}

function setTemplateReferenceEditor(page, question, instance) {
  const key = `${page.pageIndex}:${String(question?.question_no || '')}`
  if (instance) templateReferenceEditorInstances.set(key, instance)
  else templateReferenceEditorInstances.delete(key)
}

function activeTemplateReferenceEditor(page) {
  return templateReferenceEditorInstances.get(`${page.pageIndex}:${String(page.referenceQuestionNumber || '')}`) || null
}

function clearTemplateSelection(page) {
  if (page.pendingSlotId) page.geometryTool = ''
  page.pendingSlotId = ''
  page.geometrySelection = null
  page.referenceStrokeSelected = false
  activeTemplateReferenceEditor(page)?.clearSelection()
}

function setTemplateStrokeSelection(page, selected) {
  page.referenceStrokeSelected = selected
  if (selected) {
    page.geometrySelection = null
    page.geometryTool = ''
  }
}

function markTemplatePageGeometryDirty(page, number = '') {
  page.dirty = true
  const answerPage = templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))
  for (const question of answerPage?.questions || []) {
    if (number && String(question.question_no) !== String(number)) continue
    if (question.drawing_reference) {
      question.drawing_reference = { ...question.drawing_reference, teacher_confirmed: false }
      question._drawingReferenceDirty = true
    }
  }
}

async function armTemplateGeometryTool(page, kind) {
  if (templatePageEditingLocked(page)) return false
  if (kind && !TEMPLATE_GEOMETRY_TOOLS.some((tool) => tool.key === kind)) return false
  // Every newly created marker must be explicitly bound to a teacher-entered
  // question identity.  The fallback keeps non-browser unit harnesses (which
  // do not provide Element Plus' prompt) deterministic; the real page always
  // shows the validated editor before arming the canvas tool.
  let number = templateGeometryQuestionNumber(page)
  if (typeof ElMessageBox !== 'undefined' && typeof ElMessageBox.prompt === 'function') {
    page.geometryQuestionPrompting = true
    try {
      const { value } = await ElMessageBox.prompt(
        '请输入此框所属题号（仅数字和“.”，如 2.3）',
        `设置${templateGeometryToolLabel(kind)}题号`,
        {
          inputValue: number,
          inputPattern: /^\d+(\.\d+)*$/,
          inputErrorMessage: '只能输入数字和点；首尾必须是数字；点不能连续',
          inputPlaceholder: '例如 2.3',
          confirmButtonText: '继续框选',
          cancelButtonText: '取消',
        },
      )
      number = String(value || '').trim()
    } catch {
      return false
    } finally {
      page.geometryQuestionPrompting = false
    }
  }
  if (!/^\d+(\.\d+)*$/.test(number)) {
    ElMessage.warning('只能输入数字和点；首尾必须是数字；点不能连续')
    return false
  }
  page.geometryQuestionNumber = number
  page.drawingQuestionNumber = number
  page.editQuestionNumber = number
  page.newHandwritingQuestion = number
  // A drawing area may be drawn on any question (the box makes it a drawing
  // question); the Q marker alone needs an area to sit beside.
  if (kind === 'drawing-label' && !(page.drawingBoxes || []).some((box) => String(box.question_number) === number)) {
    ElMessage.info(`请先用“绿色画图区”框出 Q${number} 的作图位置；作图题号会自动放在画图区左侧`)
    return false
  }
  setTemplatePreviewEditMode(page, 'geometry')
  page.geometryTool = kind
  return true
}

function startTemplateGeometryCreation(event, page) {
  if (templatePageEditingLocked(page) || !page.geometryTool || templateCropGesture.value) return
  if (event.button !== undefined && event.button !== 0) return
  const stage = event.currentTarget.closest('.tpl-cut-preview-stage')
  const rect = stage?.getBoundingClientRect()
  if (!rect?.width || !rect?.height) return
  event.preventDefault()
  event.currentTarget.setPointerCapture?.(event.pointerId)
  const point = [
    Math.max(0, Math.min(1000, Math.round((event.clientX - rect.left) * 1000 / rect.width))),
    Math.max(0, Math.min(1000, Math.round((event.clientY - rect.top) * 1000 / rect.height))),
  ]
  templateCropGesture.value = {
    pointerId: event.pointerId, page, mode: 'create', kind: page.geometryTool,
    questionNumber: templateGeometryQuestionNumber(page), point, endPoint: point,
    startX: event.clientX, startY: event.clientY, width: rect.width, height: rect.height,
  }
  page.geometryDraft = [...point, ...point]
}

function commitTemplateGeometryCreation(gesture) {
  const { page, kind, questionNumber: number } = gesture
  let target
  if (kind === 'drawing-label') {
    target = page.drawingLabels.find((item) => String(item.question_number) === number)
    if (!target) { target = { question_number: number }; page.drawingLabels.push(target) }
    target.label_anchor = [...gesture.endPoint]
    target = page.drawingLabels.find((item) => String(item.question_number) === number)
  } else {
    const bbox = normalizeTemplateBBoxForPage(page.geometryDraft)
    if (bbox.length !== 4 || (bbox[2] - bbox[0]) * gesture.width / 1000 < 4 || (bbox[3] - bbox[1]) * gesture.height / 1000 < 4) return
    // Retain the question identity even if its printed-number marker was deleted.
    let anchor = page.questionAnchors.find((item) => String(item.question_number) === number)
    if (!anchor) {
      page.questionAnchors.push({ question_number: number, question_number_bbox: [] })
      anchor = page.questionAnchors.find((item) => String(item.question_number) === number)
    }
    if (kind === 'question') {
      anchor.question_number_bbox = bbox
      target = anchor
    } else {
      // 兼容回退: a slot-aware question exists in the page schema even when the
      // questions response row is missing — never fall to the legacy branch and
      // create a mixed box shape (slot-less box among slot boxes).
      const slotQuestion = templatePages.value.find(item => Number(item.page_index) === Number(page.pageIndex))?.questions?.find(q => String(q.question_no) === number)
        || (page.questionSchema || []).find(q => String(q.question_no ?? q.question_number) === number && (q.slot_ids || q.region_kinds))
      const clearedSchema = (page.questionSchema || []).find(q => String(q.question_no ?? q.question_number) === number && q._slotsCleared)
      if (clearedSchema && slotQuestion && !slotQuestion.slot_ids) {
        // Every slot of this question was deleted; a new box starts a fresh slot list.
        Object.assign(slotQuestion, { slot_ids: [], region_kinds: [], answer_slots_text: [] })
        delete clearedSchema._slotsCleared
      }
      if (slotQuestion?.slot_ids) {
        try {
          const pending = page.answerBoxes.find(box => box.slot_id === page.pendingSlotId
            && String(box.question_number) === number && box.bbox.length !== 4)
          if (pending) {
            const pendingKind = pending.region_kind || 'answer'
            if (pendingKind !== kind) {
              ElMessage.warning(`Q${pending.slot_id} 是${pendingKind === 'drawing' ? '作图区' : '文字作答框'}，请用对应工具框选`)
              return
            }
            pending.bbox = [...bbox]
            delete pending.pendingGeometry
            target = pending
            if (kind === 'drawing') {
              // Same shape as addTemplateSlot: a drawing slot also owns a
              // drawing region, which feeds drawing_regions and grading crops.
              page.drawingBoxes.push({ ...pending, bbox: [...bbox] })
              target = page.drawingBoxes.at(-1)
            }
          } else target = addTemplateSlot(page, slotQuestion, bbox, kind)
        }
        catch (error) { ElMessage.warning(error.message); return }
      } else {
      const boxes = kind === 'drawing' ? page.drawingBoxes : page.answerBoxes
      if (boxes.filter((item) => String(item.question_number) === number).length >= 8) {
        ElMessage.warning(`Q${number} 最多添加 8 个${templateGeometryToolLabel(kind)}`)
        return
      }
      boxes.push({ question_number: number, bbox })
      target = boxes[boxes.length - 1]
      const schemaQuestion = (page.questionSchema || []).find((item) => String(item.question_number ?? item.question_no) === number)
      if (kind === 'drawing' && schemaQuestion && !['drawing', 'mixed'].includes(schemaQuestion.answer_kind)) {
        // 教师框出画图区即表示此题需要作图：属性随框生效，不再等待另选作图属性。
        const hasText = templateVisibleAnswerBoxes(page).some((item) => String(item.question_number) === number)
        schemaQuestion.answer_kind = hasText ? 'mixed' : 'drawing'
        const answerQuestion = templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))?.questions
          ?.find((item) => String(item.question_no) === number)
        if (answerQuestion) answerQuestion.answer_kind = schemaQuestion.answer_kind
      }
      }
      if (kind === 'answer') page.hasAnswerRegionContract = true
      if (kind === 'drawing') ensureTemplateDrawingLabel(page, number)
      // Re-framing a position keeps a question that was due for removal.
      const keptQuestion = templateAnswerQuestion(page, number)
      if (keptQuestion) delete keptQuestion._pendingRemoval
    }
  }
  markTemplateGeometryConfirmed(target)
  markTemplatePageGeometryDirty(page, number)
  selectTemplateGeometryTarget(page, { kind, target })
  page.geometryTool = ''
}

function ensureTemplateDrawingLabel(page, number) {
  if ((page.drawingLabels || []).some((label) => String(label.question_number) === String(number))) return
  const anchor = defaultTemplateDrawingLabel((page.drawingBoxes || [])
    .filter((box) => String(box.question_number) === String(number)).map((box) => box.bbox))
  if (anchor) page.drawingLabels.push({ question_number: String(number), label_anchor: anchor })
}

function deleteTemplateSelection(page) {
  if (templatePageEditingLocked(page) || templateCropGesture.value) return
  if (page.referenceStrokeSelected) {
    activeTemplateReferenceEditor(page)?.deleteSelectedStroke()
    return
  }
  const selected = page.geometrySelection
  if (!selected) return
  if (['answer', 'drawing'].includes(selected.kind) && selected.target?.slot_id) {
    const question = templatePages.value.find(item => Number(item.page_index) === Number(page.pageIndex))?.questions?.find(q => String(q.question_no) === String(selected.target.question_number))
    try { removeTemplateSlot(page, question, selected.target.slot_id) }
    catch (error) { ElMessage.warning(error.message); return }
    markTemplateQuestionRemovalIfEmpty(page, selected.target.question_number)
    markTemplatePageGeometryDirty(page, selected.target.question_number)
    clearTemplateSelection(page)
    return
  }
  if (selected.kind === 'divider') {
    const left = page.columns[selected.index]
    const right = page.columns[selected.index + 1]
    if (!left || !right) return
    left.x2_1000 = right.x2_1000
    page.columns.splice(selected.index + 1, 1)
    page.columns.forEach((item, index) => { item.column_index = index + 1 })
    page.columnCount = page.columns.length
  } else if (selected.kind === 'question') {
    if (!page.questionAnchors.includes(selected.target)) return
    selected.target.question_number_bbox = []
    delete selected.target.question_bbox
    delete selected.target.bbox
  } else {
    const boxes = { answer: page.answerBoxes, drawing: page.drawingBoxes, 'drawing-label': page.drawingLabels }[selected.kind]
    const index = boxes?.indexOf(selected.target) ?? -1
    if (index < 0) return
    boxes.splice(index, 1)
    if (['drawing', 'drawing-label'].includes(selected.kind)) {
      const number = String(selected.target?.question_number || '')
      const question = (page.questionSchema || []).find((item) => String(item.question_number ?? item.question_no) === number)
      const hasRemainingDrawing = (page.drawingBoxes || []).some((item) => String(item.question_number) === number)
      if (question && !hasRemainingDrawing) {
        // 教师删掉了该题最后一个画图区：作图题号与标准图随之消失，题目按非作图保存。
        page.drawingQuestionNumber = number
        setTemplateDrawingKind(page, 'non_graphic')
      }
    }
    if (['answer', 'drawing'].includes(selected.kind)) markTemplateQuestionRemovalIfEmpty(page, selected.target?.question_number)
  }
  markTemplatePageGeometryDirty(page, selected.target?.question_number || '')
  clearTemplateSelection(page)
}

function templateQuestionHasBoxes(page, number) {
  return (page.answerBoxes || []).some((box) => String(box.question_number) === String(number) && box.bbox?.length === 4)
    || (page.drawingBoxes || []).some((box) => String(box.question_number) === String(number))
}

function templateAnswerQuestion(page, number) {
  return templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))?.questions
    ?.find((item) => String(item.question_no) === String(number))
}

/** 教师删掉了该题全部作答框和画图区：题号随之消失，保存模板时删除这道题（其余题号不变）。 */
function markTemplateQuestionRemovalIfEmpty(page, number) {
  const question = templateAnswerQuestion(page, number)
  if (question && !templateQuestionHasBoxes(page, number)) question._pendingRemoval = true
}

function startTemplateGeometryGesture(event, page, target, mode, dividerIndex = -1, bboxKey = 'bbox') {
  if (templatePageEditingLocked(page) || page.geometryTool || templateCropGesture.value) return
  if (event.button !== undefined && event.button !== 0) return
  const stage = event.currentTarget.closest('.tpl-cut-preview-stage')
  if (!stage) return
  event.preventDefault()
  selectTemplateGeometryTarget(page, { kind: mode === 'drawing-label' ? mode : mode.split('-')[0], target, index: dividerIndex })
  event.currentTarget.setPointerCapture?.(event.pointerId)
  const followLabel = mode === 'drawing-move' ? templateFollowingDrawingLabel(page, target) : null
  templateCropGesture.value = {
    pointerId: event.pointerId, page, target, mode, dividerIndex, bboxKey,
    startX: event.clientX, startY: event.clientY,
    bbox: bboxKey === 'question_number_bbox' ? templateQuestionAnchorBBox(target) : target?.[bboxKey] ? [...target[bboxKey]] : null,
    wasDirty: page.dirty,
    boundary: dividerIndex >= 0 ? page.columns[dividerIndex].x2_1000 : null,
    width: stage.clientWidth, height: stage.clientHeight,
    followLabel, followPoint: followLabel ? [...followLabel.label_anchor] : null,
  }
}

// Labels follow boxes: a question's Q marker belongs to its first (main)
// drawing area, so moving that area carries the marker along.
function templateFollowingDrawingLabel(page, box) {
  const number = String(box?.question_number ?? '')
  const main = (page.drawingBoxes || []).find((item) => String(item.question_number) === number)
  if (!number || main !== box) return null
  return (page.drawingLabels || []).find((label) => String(label.question_number) === number) || null
}

function startTemplateDividerMove(event, page, dividerIndex) {
  startTemplateGeometryGesture(event, page, null, 'divider', dividerIndex)
}

function startTemplateHandwritingMove(event, page, box) {
  startTemplateGeometryGesture(event, page, box, 'handwriting-move')
}

function startTemplateHandwritingResize(event, page, box) {
  startTemplateGeometryGesture(event, page, box, 'handwriting-resize')
}

function startTemplateQuestionMove(event, page, anchor) {
  startTemplateGeometryGesture(event, page, anchor, 'question-move', -1, 'question_number_bbox')
}

function startTemplateQuestionResize(event, page, anchor) {
  startTemplateGeometryGesture(event, page, anchor, 'question-resize', -1, 'question_number_bbox')
}

function startTemplateAnswerMove(event, page, box) {
  startTemplateGeometryGesture(event, page, box, 'answer-move')
}

function startTemplateAnswerResize(event, page, box) {
  startTemplateGeometryGesture(event, page, box, 'answer-resize')
}

function startTemplateDrawingLabelMove(event, page, label) {
  startTemplateGeometryGesture(event, page, label, 'drawing-label')
  if (templateCropGesture.value?.target === label) {
    templateCropGesture.value.point = [...label.label_anchor]
  }
}

function moveTemplateGeometry(event) {
  const gesture = templateCropGesture.value
  if (!gesture || gesture.pointerId !== event.pointerId) return
  if (templatePageEditingLocked(gesture.page)) { cancelTemplateGeometry(event); return }
  const dx = Math.round((event.clientX - gesture.startX) * 1000 / Math.max(1, gesture.width))
  const dy = Math.round((event.clientY - gesture.startY) * 1000 / Math.max(1, gesture.height))
  if (gesture.mode === 'create') {
    const [x, y] = gesture.point
    const endX = Math.max(0, Math.min(1000, x + dx))
    const endY = Math.max(0, Math.min(1000, y + dy))
    gesture.endPoint = [endX, endY]
    gesture.page.geometryDraft = [Math.min(x, endX), Math.min(y, endY), Math.max(x, endX), Math.max(y, endY)]
    return
  }
  if (!dx && !dy && !gesture.changed) return
  gesture.changed = true
  if (gesture.mode === 'drawing-label' && gesture.point) {
    // The point is the Q label's right-edge midpoint, not its top-left corner.
    gesture.target.label_anchor = [
      Math.max(0, Math.min(1000, gesture.point[0] + dx)),
      Math.max(0, Math.min(1000, gesture.point[1] + dy)),
    ]
    gesture.page.dirty = true
    return
  }
  if (gesture.mode === 'divider') {
    const leftColumn = gesture.page.columns[gesture.dividerIndex]
    const rightColumn = gesture.page.columns[gesture.dividerIndex + 1]
    const boundary = Math.max(
      leftColumn.x1_1000 + 60,
      Math.min(rightColumn.x2_1000 - 60, gesture.boundary + dx),
    )
    leftColumn.x2_1000 = boundary
    rightColumn.x1_1000 = boundary
    gesture.page.dirty = true
    return
  }
  if (!gesture.bbox || !gesture.target) return
  const [left, top, right, bottom] = gesture.bbox
  const bboxKey = gesture.bboxKey || 'bbox'
  if (gesture.mode.endsWith('resize')) {
    gesture.target[bboxKey] = [
      left,
      top,
      Math.max(left + 8, Math.min(1000, right + dx)),
      Math.max(top + 8, Math.min(1000, bottom + dy)),
    ]
  } else {
    const width = right - left
    const height = bottom - top
    const nextLeft = Math.max(0, Math.min(1000 - width, left + dx))
    const nextTop = Math.max(0, Math.min(1000 - height, top + dy))
    gesture.target[bboxKey] = [nextLeft, nextTop, nextLeft + width, nextTop + height]
    if (gesture.followLabel) {
      gesture.followLabel.label_anchor = [
        Math.max(0, Math.min(1000, gesture.followPoint[0] + nextLeft - left)),
        Math.max(0, Math.min(1000, gesture.followPoint[1] + nextTop - top)),
      ]
    }
  }
  gesture.page.dirty = true
}

function finishTemplateGeometry(event) {
  const gesture = templateCropGesture.value
  if (!gesture || gesture.pointerId !== event.pointerId) return
  if (templatePageEditingLocked(gesture.page)) { cancelTemplateGeometry(event); return }
  if (Number.isFinite(event.clientX) && Number.isFinite(event.clientY)) moveTemplateGeometry(event)
  if (gesture.mode === 'create') commitTemplateGeometryCreation(gesture)
  else if (gesture.changed) {
    markTemplateGeometryConfirmed(gesture.target)
    markTemplatePageGeometryDirty(gesture.page, gesture.target?.question_number || '')
  }
  gesture.page.geometryDraft = null
  templateCropGesture.value = null
}

function setTemplateAnswerDrawingKind(pageIndex, question, kind) {
  const page = templateCutPreviewPages.value.find((item) => Number(item.pageIndex) === Number(pageIndex))
  if (!page) {
    if (!question || !['non_graphic', 'drawing', 'mixed'].includes(kind)) return
    question.answer_kind = kind
    question._teacherDrawingKindEdited = true
    if (kind === 'non_graphic') {
      question.drawing_regions = []
      question.label_anchor = null
      delete question.drawing_reference
    }
    return
  }
  page.drawingQuestionNumber = String(question?.question_no || '')
  setTemplateDrawingKind(page, kind)
}

function cancelTemplateGeometry(event) {
  const gesture = templateCropGesture.value
  if (!gesture || gesture.pointerId !== event.pointerId) return
  if (gesture.mode !== 'create') {
    if (gesture.mode === 'divider') {
      gesture.page.columns[gesture.dividerIndex].x2_1000 = gesture.boundary
      gesture.page.columns[gesture.dividerIndex + 1].x1_1000 = gesture.boundary
    } else if (gesture.point) gesture.target.label_anchor = [...gesture.point]
    else if (gesture.bbox) gesture.target[gesture.bboxKey] = [...gesture.bbox]
    if (gesture.followLabel) gesture.followLabel.label_anchor = [...gesture.followPoint]
    gesture.page.dirty = gesture.wasDirty
  }
  gesture.page.geometryDraft = null
  templateCropGesture.value = null
}

function templatePageNeedsSave(page) {
  return Boolean(page && (page.dirty || page.layoutReviewPending || page.anchorReviewPending
    || page.geometryReviewPending || page.layout_review_pending || page.anchor_review_pending
    || page.geometry_review_pending || page.columnsSource === 'teacher_draft'
    || page.columnsConfirmed === false || templateReviewPendingPageIndexes().has(Number(page.pageIndex))
    || templateDiagnosticPendingPageIndexes().has(Number(page.pageIndex))))
}

async function saveAllTemplateLayoutPages() {
  const templateId = pendingTemplateId.value
  if (!templateId) return false
  const pages = [...templateCutPreviewPages.value]
  if (pages.some((page) => page.saving)) {
    ElMessage.warning('模板页面仍在保存或生成标准图，请完成后重试')
    return false
  }
  // Page writes share one server-owned template session. Keep them ordered so
  // a failed page never races with final confirmation or loses another draft.
  for (const page of pages) {
    if (templateId !== pendingTemplateId.value || !templateCutPreviewPages.value.includes(page)) return false
    if (templatePageNeedsSave(page) && !await saveTemplateLayoutPage(page, { silent: true })) return false
  }
  return templateId === pendingTemplateId.value
}

async function saveTemplateLayoutPage(page, { silent = false } = {}) {
  if (!pendingTemplateId.value || page.saving || !templateCutPreviewPages.value.includes(page)) return false
  const templateId = pendingTemplateId.value
  const pageIndex = Number(page.pageIndex)
  const isCurrentPage = () => templateId === pendingTemplateId.value && templateCutPreviewPages.value.includes(page)
  if (!Array.isArray(page.columns) || !page.columns.length) {
    ElMessage.warning(`第 ${pageIndex + 1} 页尚未设置栏数，请先选择并核对栏边界`)
    return false
  }
  const conflictNotice = templateLayoutConflictNotice(page)
  if (conflictNotice) {
    ElMessage.warning(conflictNotice)
    return false
  }
  page.saving = true
  try {
    initializeTemplatePageSlots(page)
    if (page.slotContractError) throw new Error(page.slotContractError)
    const slotQuestions = templatePages.value.find(item => Number(item.page_index) === pageIndex)?.questions || []
    // Saving this page is the teacher's explicit acknowledgement of every
    // visible geometry item.  Send that provenance to the server so a low
    // confidence model candidate is promoted only after this action succeeds.
    const confirmedAnchors = cloneQuestionAnchors(page.questionAnchors, { teacherConfirmed: true })
    const { textBoxes, drawingBoxes } = templatePageSaveBoxes(page)
    const confirmedAnswerRegions = serializeTemplateHandwritingRegions(textBoxes, page.questionAnchors, { teacherConfirmed: true })
    const confirmedHandwriting = serializeTemplateHandwritingRegions([...textBoxes, ...drawingBoxes], page.questionAnchors, { teacherConfirmed: true })
    const confirmedSchema = serializeTemplateSlots(page, slotQuestions, serializeTemplateDrawingReview(page.questionSchema, page.drawingBoxes, page.drawingLabels))
    const blankTemplate = normalizeTemplateMode(templateMode.value, 'marked') === 'blank'
    const res = await api.grade.templateLayoutOverrides(templateId, {
      page_index: page.pageIndex,
      columns: cloneTemplateColumns(page.columns),
      question_anchors: cloneQuestionAnchors(page.questionAnchors, { teacherConfirmed: true }),
      question_schema: confirmedSchema,
      // The editor exposes one 作答框 collection. Keep the existing wire
      // contract until the model/prompt compatibility pass is accepted.
      template_answer_regions: blankTemplate || page.hasAnswerRegionContract ? confirmedAnswerRegions : undefined,
      template_handwriting_regions: confirmedHandwriting,
    })
    if (!isCurrentPage()) return false
    if (res?.success !== true || Number(res.page_index) !== pageIndex
      || (res.template_id != null && String(res.template_id) !== String(templateId))
      || !Array.isArray(res.columns) || !res.columns.length) {
      throw new Error('定位保存返回未绑定当前模板页面，未覆盖本页修改')
    }
    page.columns = cloneTemplateColumns(res.columns)
    page.columnCount = page.columns.length
    page.columnsSource = 'teacher'
    page.columnsConfirmed = true
    page.layoutReviewPending = false
    page.layout_review_pending = false
    page.anchorReviewPending = false
    page.geometryReviewPending = false
    page.anchor_review_pending = false
    page.geometry_review_pending = false
    page.questionAnchors = cloneQuestionAnchors(res.question_anchors || confirmedAnchors, { teacherConfirmed: true })
    applyTemplateDrawingSchema(page, res.question_schema || confirmedSchema)
    const answerPage = templatePages.value.find((item) => Number(item.page_index) === Number(page.pageIndex))
    for (const question of answerPage?.questions || []) {
      const schema = page.questionSchema.find((item) => String(item.question_number ?? item.question_no) === String(question.question_no))
      // Actual geometry edits invalidate reference approval at edit time. Do
      // not undo a teacher's subsequent review just because it is persisted.
      if (schema) {
        Object.assign(question, templateQuestionMetadata(schema))
        // Drawing geometry and reference mode are independent of answer type;
        // saving a choice/fill drawing must not rewrite the teacher's contract.
      }
    }
    const savedAnswerRegions = res.template_answer_regions
      ?? (blankTemplate || page.hasAnswerRegionContract ? confirmedAnswerRegions : [])
    page.hasAnswerRegionContract = savedAnswerRegions.length > 0
    page.answerBoxes = flattenTemplateHandwritingRegions(savedAnswerRegions).map(markTemplateGeometryConfirmed)
    page.handwritingBoxes = flattenTemplateHandwritingRegions(
      res.template_handwriting_regions || confirmedHandwriting,
    ).map(markTemplateGeometryConfirmed)
    initializeTemplatePageSlots(page)
    if (page.slotContractError) throw new Error(page.slotContractError)
    clearTemplateResolvedGeometryDiagnostics(
      page.pageIndex,
      page.questionAnchors,
      page.handwritingBoxes,
    )
    const questionNumbers = page.questionAnchors.map((anchor) => String(anchor.question_number))
    if (!questionNumbers.includes(String(page.newHandwritingQuestion))) {
      page.newHandwritingQuestion = questionNumbers[0] || ''
    }
    if (templateCutResult.value) {
      TEMPLATE_REVIEW_PENDING_PAGE_KEYS.forEach((key) => {
        if (!Array.isArray(templateCutResult.value[key])) return
        templateCutResult.value[key] = templateCutResult.value[key]
          .map(Number)
          .filter((value) => value !== Number(page.pageIndex))
      })
      const resultPage = (templateCutResult.value.pages || []).find(
        (item) => Number(item.page_index) === Number(page.pageIndex),
      )
      if (resultPage) {
        delete resultPage.layout_review_pending
        delete resultPage.anchor_review_pending
        delete resultPage.geometry_review_pending
        delete resultPage.question_anchor_candidates
        delete resultPage.template_handwriting_region_candidates
        delete resultPage.template_answer_region_candidates
        delete resultPage.question_schema_candidates
        if (resultPage.modes?.question) {
          delete resultPage.modes.question.layout_review_pending
          delete resultPage.modes.question.anchor_review_pending
          delete resultPage.modes.question.geometry_review_pending
          resultPage.modes.question.layout_status = 'ready'
          resultPage.modes.question.anchor_status = 'ready'
        }
        resultPage.anchor_crop_plan = res.question_crop_plan || resultPage.anchor_crop_plan
        resultPage.question_anchors = page.questionAnchors
        resultPage.question_schema = page.questionSchema
        resultPage.template_answer_regions = savedAnswerRegions
        resultPage.template_handwriting_regions = res.template_handwriting_regions || confirmedHandwriting
      }
    }
    page.dirty = false
    page.insertedPlaceholder = false
    if (!silent) ElMessage.success(`第 ${page.pageIndex + 1} 页分栏与作答区域已保存`)
    return true
  } catch (err) {
    if (isCurrentPage()) notifyProcessingError(err, `第 ${pageIndex + 1} 页定位未保存：${err.message || '请重试'}，本页修改已保留`)
    return false
  } finally {
    page.saving = false
  }
}

async function editTemplateQuestion(page, payload, successText) {
  if (!pendingTemplateId.value || templatePageEditingLocked(page) || templateCropGesture.value) return
  const blocked = templateReferenceQuestionEditBlockReason()
  if (blocked) { ElMessage.warning(blocked); return }
  const templateId = pendingTemplateId.value
  templateQuestionEditing.value = true
  try {
    // Renumbering rewrites every page artifact. Flush geometry first so it
    // cannot replace a teacher's unsaved boxes with an old server snapshot.
    if (!await saveAllTemplateLayoutPages()) return
    page.saving = true
    const res = await api.grade.templateQuestionEdit(templateId, {
      page_index: page.pageIndex,
      ...payload,
    })
    if (templateId !== pendingTemplateId.value || !templateCutPreviewPages.value.includes(page)) return
    // A shift crosses page boundaries, so the answer editor and every preview
    // page have to take the server's new numbering rather than patch their own.
    applyTemplateQuestionEdit(res)
    ElMessage.success(successText)
  } catch (err) {
    notifyProcessingError(err, err.message || '题号调整失败')
  } finally {
    page.saving = false
    templateQuestionEditing.value = false
  }
}

async function insertTemplateQuestion(page, beforeNumber) {
  const target = String(beforeNumber || '').trim()
  if (!pendingTemplateId.value || templatePageEditingLocked(page) || templateCropGesture.value) return
  const blocked = templateReferenceQuestionEditBlockReason()
  if (blocked) { ElMessage.warning(blocked); return }
  // 教师标注：新增题号必须由老师输入——仅数字和点，首尾必须是数字，点不能连续。
  let number = ''
  try {
    const { value } = await ElMessageBox.prompt(
      '请输入新增题号（仅数字和“.”，如 2.3）',
      target ? `在第 ${target} 题前新增` : '末尾新增',
      {
        inputPattern: /^\d+(\.\d+)*$/,
        inputErrorMessage: '只能输入数字和点；开头与结尾必须是数字；点不能连续',
        inputPlaceholder: '例如 2.4',
        confirmButtonText: '新增题目',
        cancelButtonText: '取消',
      },
    )
    number = String(value || '').trim()
  } catch {
    return
  }
  if (!/^\d+(\.\d+)*$/.test(number)) {
    ElMessage.warning('只能输入数字和点；开头与结尾必须是数字；点不能连续')
    return
  }
  return editTemplateQuestion(
    page,
    { action: 'insert', before_question_number: target, new_question_number: number },
    target ? `已新增第 ${number} 题` : `已在本页末尾新增第 ${number} 题`,
  )
}

async function deleteTemplateQuestion(page, questionNumber) {
  const target = String(questionNumber || '').trim()
  if (!target) return
  const blocked = templateReferenceQuestionEditBlockReason()
  if (blocked) { ElMessage.warning(blocked); return }
  try {
    await ElMessageBox.confirm(
      `删除整道第 ${target} 题？该题的题干、答案和定位会一并移除，其余题号不变。若只删除图上的框，请取消并使用“删除所选”。`,
      '删除整道题',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  return editTemplateQuestion(
    page,
    { action: 'delete', question_number: target },
    `已删除第 ${target} 题的题号标注`,
  )
}

function applyTemplateQuestionEdit(res) {
  const localDrafts = new Map(templatePages.value.flatMap((page) => (page.questions || []).map((q) => [
    String(res.renamed_question_numbers?.[String(q.question_no)] ?? q.question_no), q,
  ])))
  const byIndex = new Map(
    (res.pages || []).map((page) => [Number(page.page_index), page.questions || []]),
  )
  templatePages.value = templatePages.value.map((page) => {
    const questions = byIndex.get(Number(page.page_index))
    if (!questions) return page
    const openState = new Map(
      (page.questions || []).map((q) => [String(q.question_no), !!q._open]),
    )
    return {
      ...page,
      questions: questions.map((question) => {
        const normalized = normalizeAnswerSpecForEditor(question)
        const local = localDrafts.get(String(normalized.question_no))
        return {
          ...normalized,
          ...local,
          ...templateQuestionMetadata(normalized),
          question_no: normalized.question_no,
          // A question with nothing in it is the one the teacher has to fill in,
          // so it opens; the rest keep whatever the teacher had open.
          _open: !normalized.question_text
            || openState.get(String(normalized.question_no))
            || false,
        }
      }),
    }
  })
  for (const previewPage of templateCutPreviewPages.value) {
    clearTemplateSelection(previewPage)
    previewPage.geometryTool = ''
    const artifact = (res.page_artifacts || {})[previewPage.pageLabel]
    if (!artifact) continue
    const reviewPending = artifact.geometry_review_pending === true
      || (res.geometry_review_pending_pages || []).map(Number).includes(Number(previewPage.pageIndex))
    // A teacher question-number edit is an explicit geometry decision.  Keep
    // the returned provenance even when an older server omits those fields.
    previewPage.questionAnchors = cloneQuestionAnchors(artifact.question_anchors, { teacherConfirmed: !reviewPending })
    applyTemplateDrawingSchema(previewPage, artifact.question_schema || [])
    previewPage.answerBoxes = mergeTemplateAnswerRegionSources(
      artifact.template_answer_regions,
      artifact.template_handwriting_regions,
    )
    previewPage.hasAnswerRegionContract = previewPage.answerBoxes.length > 0
    previewPage.handwritingBoxes = previewPage.answerBoxes
    initializeTemplatePageSlots(previewPage)
    const numbers = previewPage.questionAnchors.map((anchor) => String(anchor.question_number))
    if (!numbers.includes(String(previewPage.editQuestionNumber))) {
      previewPage.editQuestionNumber = numbers[0] || ''
    }
    if (!numbers.includes(String(previewPage.newHandwritingQuestion))) {
      previewPage.newHandwritingQuestion = numbers[0] || ''
    }
    // Insertion creates a visible placeholder, not a model-verified position.
    previewPage.dirty = reviewPending
    previewPage.insertedPlaceholder = reviewPending && res.action === 'insert'
    previewPage.columnsSource = 'teacher'
    previewPage.columnsConfirmed = !reviewPending
    previewPage.layoutReviewPending = reviewPending
    previewPage.layout_review_pending = reviewPending
    previewPage.geometryReviewPending = reviewPending
    previewPage.geometry_review_pending = reviewPending
    previewPage.anchorReviewPending = reviewPending
    if (!numbers.includes(String(previewPage.drawingQuestionNumber))) previewPage.drawingQuestionNumber = numbers[0] || ''
  }
  if (templateCutResult.value && Array.isArray(res.geometry_review_pending_pages)) {
    templateCutResult.value.geometry_review_pending_pages = [...res.geometry_review_pending_pages]
  }
}

async function loadTemplateCutPreviews(data = templateCutResult.value, expectedRunToken = templateCutRunToken) {
  const templateId = pendingTemplateId.value
  if (!templateId || expectedRunToken !== templateCutRunToken || data?.status !== 'complete') return
  revokeTemplateCutPreviews()
  const loadToken = templateCutPreviewLoadToken
  const isCurrentLoad = () => templateId === pendingTemplateId.value
    && expectedRunToken === templateCutRunToken && loadToken === templateCutPreviewLoadToken
  const imageUrls = new Set()
  let failed = false
  templateCutPreviewError.value = ''
  try {
    const pendingPages = templateReviewPendingPageIndexes(data)
    const pages = await Promise.all((data.pages || []).map(async (page, index) => {
      // Normalize legacy responses in place so the established editor calls
      // keep reading the server-owned page fields.
      if (page.page_index === undefined || page.page_index === null) {
        page.page_index = index
      }
      const pageIndex = Number(page.page_index)
      const planColumns = cloneTemplateColumns(page.anchor_crop_plan?.parameters?.columns)
      const questionMode = page.modes?.question || {}
      // Real model runs also publish the detected layout under the question-mode
      // contract while the crop plan still carries no trusted columns. Show that
      // candidate so the teacher reviews the model's actual result instead of an
      // empty layout — but never promote it: only a saved teacher override
      // confirms a layout, so the candidate stays unconfirmed and under review.
      const candidateColumns = planColumns.length
        ? []
        : cloneTemplateColumns(questionMode?.column_layout?.columns)
      const columns = planColumns.length ? planColumns : candidateColumns
      const reviewPending = pendingPages.has(pageIndex) || templatePageReviewPending(page, questionMode)
      const layoutReviewPending = reviewPending
        || !columns.length
        || !planColumns.length
      const columnState = columns.length
        ? {
          columnsSource: planColumns.length ? 'model' : 'model_candidate',
          columnsConfirmed: !layoutReviewPending && planColumns.length > 0,
        }
        : { columnsSource: 'teacher_draft', columnsConfirmed: false }
      // No trusted columns means the teacher must explicitly choose a count.
      // Keeping this at zero avoids presenting the global two-column default
      // as though it were an already reviewed page layout.
      const columnCount = columns.length
        ? Number(page.anchor_crop_plan?.parameters?.column_count
          || questionMode?.column_layout?.column_count || columns.length)
        : 0
      const artifact = page.artifact || page.page_artifact || {}
      // Semantic/geometry review deliberately keeps the approved fields empty.
      // Prefer candidate fields only as the editor's initial draft; the single
      // 作答框 contract is promoted only after the teacher saves an override.
      const rawAnchors = reviewPending && Array.isArray(page.question_anchor_candidates) && page.question_anchor_candidates.length
        ? page.question_anchor_candidates
        : (Array.isArray(page.question_anchors) ? page.question_anchors : (artifact.question_anchors || []))
      const rawHandwritingRegions = reviewPending && templateRegionCandidateUsable(page.template_handwriting_region_candidates)
        ? page.template_handwriting_region_candidates
        : (Array.isArray(page.template_handwriting_regions) ? page.template_handwriting_regions : (artifact.template_handwriting_regions || []))
      const rawAnswerRegions = reviewPending && templateRegionCandidateUsable(page.template_answer_region_candidates)
        ? page.template_answer_region_candidates
        : (Array.isArray(page.template_answer_regions) ? page.template_answer_regions : (artifact.template_answer_regions || []))
      const mergedAnswerBoxes = mergeTemplateAnswerRegionSources(rawAnswerRegions, rawHandwritingRegions)
      const questionSchema = mergeTemplateDrawingCandidates(
        page.question_schema || artifact.question_schema || [],
        page.question_schema_candidates || [], reviewPending,
      )
      const imageUrl = await api.grade.templatePageImage(templateId, Number(page.page_index))
      if (failed || !isCurrentLoad()) {
        URL.revokeObjectURL(imageUrl)
        return null
      }
      imageUrls.add(imageUrl)
      return {
        pageLabel: page.page_label,
        pageIndex,
        // Do not manufacture equal partitions while reviewing a pending page.
        // The empty list is intentional and keeps the model result distinct
        // from the teacher's later draft in resetTemplateColumns().
        columns,
        // A candidate layout is displayed for review but is never counted as
        // this page's confirmed layout; zero keeps the upload default invisible.
        columnCount,
        ...columnState,
        layoutReviewPending,
        layout_review_pending: layoutReviewPending,
        anchorReviewPending: reviewPending,
        geometryReviewPending: reviewPending,
        questionAnchors: cloneQuestionAnchors(rawAnchors),
        questionSchema,
        ...templateDrawingReview(questionSchema, { includeConflictingGeometry: reviewPending }),
        hasAnswerRegionContract: mergedAnswerBoxes.length > 0,
        answerBoxes: mergedAnswerBoxes,
        // Compatibility alias for older code paths; both references represent
        // the same unified 作答框 collection and are never edited separately.
        handwritingBoxes: mergedAnswerBoxes,
        anchorDiagnostics: page.anchor_diagnostics || questionMode.anchor_diagnostics || null,
        templateOcrDiagnostics: page.template_ocr_diagnostics || questionMode.template_ocr_diagnostics || null,
        newHandwritingQuestion: String(rawAnchors?.[0]?.question_number || ''),
        editQuestionNumber: String(rawAnchors?.[0]?.question_number || ''),
        drawingQuestionNumber: String(questionSchema[0]?.question_number || ''),
        referenceQuestionNumber: '',
        previewEditMode: 'geometry',
        referenceImageReadyNumbers: {},
        insertedPlaceholder: Boolean(page.inserted_placeholder || artifact.inserted_placeholder
          || page.anchor_diagnostics?.review_reason === 'teacher_added_question_placeholder'),
        // Review is an unsaved teacher decision even when the teacher accepts
        // every visible candidate without dragging it first.
        dirty: (reviewPending || layoutReviewPending) && columns.length > 0,
        saving: false,
        imageUrl,
      }
    }))
    if (!isCurrentLoad()) {
      imageUrls.forEach((url) => URL.revokeObjectURL(url))
      return
    }
    for (const page of pages) initializeTemplatePageSlots(page)
    templateCutPreviewPages.value = pages
  } catch {
    failed = true
    imageUrls.forEach((url) => URL.revokeObjectURL(url))
    if (!isCurrentLoad()) return
    templateCutPreviewError.value = '裁剪模板读取失败，请重新打开模板或重试自动裁剪。'
  }
}

async function pollTemplateCutState(templateId, expectedRunToken = templateCutRunToken) {
  if (!templateCutBusy.value || templateId !== pendingTemplateId.value || expectedRunToken !== templateCutRunToken) return
  try {
    const state = await api.grade.templateCutCandidatesState(templateId)
    if (templateId !== pendingTemplateId.value || expectedRunToken !== templateCutRunToken) return
    applyTemplateCutState(state, expectedRunToken)
  } catch {
    // The POST request remains authoritative; transient polling errors stay quiet.
  } finally {
    if (templateCutBusy.value && templateId === pendingTemplateId.value && expectedRunToken === templateCutRunToken) {
      templateCutPollTimer = setTimeout(() => pollTemplateCutState(templateId, expectedRunToken), 800)
    }
  }
}

async function loadTemplateCutState(templateId, expectedRunToken = templateCutRunToken) {
  if (!templateId || templateId !== pendingTemplateId.value || expectedRunToken !== templateCutRunToken) return
  try {
    const state = await api.grade.templateCutCandidatesState(templateId)
    if (templateId !== pendingTemplateId.value || expectedRunToken !== templateCutRunToken) return
    if (state.status !== 'not_run') {
      templateCutResult.value = state
      templateCutProgress.value = state.progress || templateCutProgress.value
      if (['scan', 'photo'].includes(state.image_type)) templateCutImageType.value = state.image_type
      if (state.status === 'complete') void loadTemplateCutPreviews(state, expectedRunToken)
    }
  } catch {
    // Existing template editing remains available if optional candidate state cannot load.
  }
}

function templateCutErrorMessage(data, fallback) {
  const detail = data?.detail ?? data?.message ?? data?.error
  if (typeof detail === 'string') return detail
  if (detail && typeof detail.message === 'string') return detail.message
  return fallback
}

async function onRunTemplateCut({ force = false } = {}) {
  if (!requireTemplateService()) return
  const templateId = pendingTemplateId.value
  if (!templateId || templateCutBusy.value) return
  const runToken = ++templateCutRunToken
  templateCutCompletedRunToken = null
  templateCutController?.abort()
  templateCutPreviewLoadToken += 1
  templateCutPreviewError.value = ''
  if (force) templateCutActionId.value = ''
  if (!templateCutActionId.value) {
    templateCutActionId.value = globalThis.crypto?.randomUUID?.()
      || `template-cut-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  }
  templateCutBusy.value = true
  templateCutProgress.value = {
    completed: 0,
    total: templatePages.value.length,
    remaining: templatePages.value.length,
  }
  templateCutController = new AbortController()
  stopTemplateCutPolling()
  templateCutPollTimer = setTimeout(() => pollTemplateCutState(templateId, runToken), 300)
  try {
    const response = await api.grade.templateCutCandidates(templateId, {
      action_id: templateCutActionId.value,
      cut_types: ['question'],
      image_type: templateCutImageType.value,
      force,
    }, templateCutController.signal)
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(templateCutErrorMessage(data, `裁题失败 (${response.status})`))
    if (runToken !== templateCutRunToken || templateId !== pendingTemplateId.value) return
    if (!['complete', 'failed'].includes(data.status)
      || (data.action_id && data.action_id !== templateCutActionId.value)) {
      throw new Error('模板裁剪结果不完整或不属于本次任务，请重试。')
    }
    applyTemplateCutState(data, runToken)
    templateCutActionId.value = ''
    if (data.status === 'complete' && templateCutResult.value?.status === 'complete') {
      ElMessage.success(`模板自动裁剪完成：已生成 ${data.pages?.reduce((total, page) => total + (page.anchor_crop_plan?.blocks?.length || 0), 0) || 0} 个连续裁剪块`)
    }
  } catch (err) {
    if (runToken !== templateCutRunToken || templateId !== pendingTemplateId.value) return
    if (templateCutCompletedRunToken === runToken) return
    if (err.name !== 'AbortError') notifyProcessingError(err, err.message || '教师模板裁题失败')
  } finally {
    if (runToken === templateCutRunToken) {
      templateCutBusy.value = false
      templateCutController = null
      stopTemplateCutPolling()
    }
  }
}

async function onConfirmTemplate() {
  if (confirmingTemplate.value || importingTemplateBank.value || templateQuestionEditing.value || templateCutBusy.value || !pendingTemplateId.value) return false
  if (templateCropGesture.value) { ElMessage.info('请先完成当前框选，再保存模板'); return false }
  const editorErrors = []
  for (const page of templateCutPreviewPages.value) {
    for (const question of templateDrawingReferenceQuestions(page)) {
      const editor = templateReferenceEditorInstances.get(`${page.pageIndex}:${String(question.question_no)}`)
      const error = editor?.prepareForSave?.()
      if (error) { editorErrors.push(`第 ${page.pageIndex + 1} 页第 ${question.question_no} 题：${error}`); continue }
      // Keep the confirmation payload aligned with the visible SVG draft even
      // when the final pointer/edit event and the save click share one tick.
      // A null value deliberately remains missing so the server-side drawing
      // reference gate is still authoritative.
      const visibleReference = editor?.valueForSave?.()
      if (visibleReference) updateDrawingReference(question, visibleReference)
    }
  }
  if (editorErrors.length) { warnTemplateSaveBlockers(editorErrors); return false }
  const templateId = pendingTemplateId.value
  confirmingTemplate.value = true
  try {
    return await saveAndConfirmTemplateDraft(templateId)
  } catch (err) {
    if (templateId === pendingTemplateId.value && err.name !== 'CanceledError' && err.name !== 'AbortError') {
      notifyProcessingError(err, err.message || '模板未保存，当前修改已保留，请重试')
    }
    return false
  } finally {
    if (templateId === pendingTemplateId.value) confirmingTemplate.value = false
  }
}

/**
 * Everything a save would reject that the Max, removal and missing-answer rows
 * do not already list: oversized boxes, standard figures outside their drawing
 * areas, missing question content and unresolved choice questions. Shown live
 * from the moment the editor opens, so the teacher sees every problem at once.
 */
function templateEditorProblems() {
  const problems = []
  for (const page of templateCutPreviewPages.value) {
    const pageNumber = Number(page.pageIndex) + 1
    const { textBoxes, drawingBoxes } = templatePageSaveBoxes(page)
    const boxes = [...new Set([...textBoxes, ...drawingBoxes])]
      .map((box) => ({ box, kind: page.drawingBoxes.includes(box) ? 'drawing' : 'answer' }))
    for (const problem of templateBoxSizeProblems(boxes, pageNumber)) {
      const index = boxes.findIndex((item) => item.box === problem.box)
      problems.push({ ...problem, key: `size-${page.pageIndex}-${index}`, pageIndex: page.pageIndex })
    }
    // Kept positions still waiting for a box (待框选); the page save names them
    // the same way, but only after the earlier checks have passed.
    const slotBoxes = templateSlotBoxes(page)
    for (const item of page.questionSchema || []) {
      if (item._slotsCleared || !Array.isArray(item.slot_ids)) continue
      const number = String(item.question_number ?? item.question_no)
      if (templateAnswerQuestion(page, number)?._pendingRemoval) continue
      const bound = (id) => slotBoxes.find((box) => box.slot_id === id && String(box.question_number) === number)
      const unboxed = item.slot_ids.filter((id) => bound(id)?.bbox?.length !== 4)
      if (!unboxed.length) continue
      problems.push({
        key: `unboxed-${page.pageIndex}-${number}`, pageIndex: page.pageIndex, box: bound(unboxed[0]), locateSlot: true,
        message: `${unboxed.map((id) => `Q${id}`).join('、')} 还没有框选位置：请在“作答槽顺序”中点该编号后框出位置，或删除该作答位`,
      })
    }
    for (const question of templateDrawingReferenceQuestions(page)) {
      if (question._pendingRemoval) continue
      if (!templateReferenceLeavesRegions(question.drawing_reference, templateDrawingReferenceRegionsFor(page, question))) continue
      problems.push({
        key: `figure-${page.pageIndex}-${question.question_no}`, pageIndex: page.pageIndex, referenceNumber: String(question.question_no),
        message: `第 ${pageNumber} 页第 ${question.question_no} 题：标准图有笔画画在画图区外：请把笔画移回画图区内（可撤销后重画），或把画图区拉大到包住整个标准图`,
      })
    }
  }
  templatePages.value.forEach((page, position) => {
    (page.questions || []).forEach((question, questionIndex) => {
      if (question._pendingRemoval) return
      const number = String(question.question_no ?? '').trim()
      const location = `第 ${Number(page.page_index) + 1} 页${number ? `第 ${number} 题` : `第 ${questionIndex + 1} 个题目`}`
      const messages = []
      if (!number) messages.push(`${location}缺少题号`)
      if ((question.question_type !== 'choice' || templateIsPureDrawing(question)) && !String(question.question_text || '').trim()) messages.push(`${location}缺少题干，请在题目内容中补齐`)
      // Missing slot answers already have their own row.
      if (!templateIsPureDrawing(question) && !String(question.standard_answer || '').trim() && !templateMissingAnswerNotice(question)) messages.push(`${location}缺少标准答案，请在答案编辑区填写`)
      messages.push(...templateChoiceConflictReasons(question))
      messages.forEach((message, index) => problems.push({
        key: `question-${page.page_index}-${questionIndex}-${index}`, pageIndex: Number(page.page_index),
        entry: { pageIndex: position, questionIndex, question }, message,
      }))
    })
  })
  return problems
}

/** Every reason a save would stop before writing anything, in one pass. */
function templatePreSaveBlockers() {
  const blockers = []
  for (const page of templatePages.value) {
    if (!page.questions?.length) { blockers.push(`第 ${Number(page.page_index) + 1} 页没有题目，请在该页添加题目`); continue }
    if (page.questions.every((question) => question._pendingRemoval)) {
      blockers.push(`第 ${Number(page.page_index) + 1} 页的题目框已全部删除；每页至少保留一道题，请为其中一题重新框出作答位置`)
      continue
    }
    for (const question of page.questions) {
      if (question._pendingRemoval) continue
      const maxFlagNotice = templateMaxFlagNotice(question)
      if (maxFlagNotice) blockers.push(maxFlagNotice)
      const missingNotice = templateMissingAnswerNotice(question)
      if (missingNotice) blockers.push(missingNotice)
    }
  }
  for (const page of templateCutPreviewPages.value) {
    if (!templatePageNeedsSave(page)) continue
    if (!Array.isArray(page.columns) || !page.columns.length) blockers.push(`第 ${Number(page.pageIndex) + 1} 页尚未设置栏数，请先选择并核对栏边界`)
    const conflictNotice = templateLayoutConflictNotice(page)
    if (conflictNotice) blockers.push(`第 ${Number(page.pageIndex) + 1} 页：${conflictNotice}`)
  }
  blockers.push(...templateEditorProblems().map((problem) => problem.message))
  return [...new Set(blockers)]
}

/** One message naming every blocker; the review rows at the top keep them visible. */
function warnTemplateSaveBlockers(blockers) {
  if (blockers.length === 1) { ElMessage.warning(blockers[0]); return }
  ElMessage.warning({
    message: `保存前还有 ${blockers.length} 处需要处理（编辑器顶部已逐条列出）：${blockers.map((text, index) => `${index + 1}. ${text}`).join('；')}`,
    duration: 10000, showClose: true,
  })
}

/**
 * Questions whose every box the teacher deleted leave the template at save
 * time (题号随框消失). The server delete keeps all other numbers; only the
 * removed question leaves the local draft, so other unsaved edits survive.
 */
async function removePendingTemplateQuestions(templateId) {
  for (const page of templatePages.value) {
    for (const question of [...(page.questions || [])]) {
      if (!question._pendingRemoval) continue
      const number = String(question.question_no)
      await api.grade.templateQuestionEdit(templateId, { page_index: page.page_index, action: 'delete', question_number: number })
      if (templateId !== pendingTemplateId.value) return false
      page.questions = page.questions.filter((item) => item !== question)
      const preview = templateCutPreviewPages.value.find((item) => Number(item.pageIndex) === Number(page.page_index))
      if (!preview) continue
      const other = (item) => String(item.question_number ?? item.question_no) !== number
      preview.questionAnchors = preview.questionAnchors.filter(other)
      preview.questionSchema = (preview.questionSchema || []).filter(other)
      for (const key of ['answerBoxes', 'handwritingBoxes', 'drawingBoxes', 'drawingLabels']) {
        preview[key] = (preview[key] || []).filter(other)
      }
      preview.dirty = true
    }
  }
  return true
}

async function saveAndConfirmTemplateDraft(templateId) {
  if (templateCutPreviewPages.value.some((page) => templateDrawingReferenceQuestions(page).length > 0 && !templateReferenceImagesReady(page))) {
    ElMessage.warning('作图模板原图尚未加载成功，请先完成图像核对再确认模板')
    return false
  }
  // Saving approves the visible draft. Late correction previews must not
  // change it while geometry is being persisted, or require another approval.
  templateCorrectionCandidatesGeneration += 1
  const blockers = templatePreSaveBlockers()
  if (blockers.length) { warnTemplateSaveBlockers(blockers); return false }
  if (!await removePendingTemplateQuestions(templateId)) return false
  if (!await saveAllTemplateLayoutPages()) return false
  if (templateId !== pendingTemplateId.value) return false
  try {
    applyTemplateBatchScores()
  } catch (err) {
    ElMessage.warning(err.message || '请输入大于 0 的统一分值')
    return false
  }
  if (!templateCanConfirm.value) {
    // Name every remaining category at once instead of one per save attempt.
    const geometryReview = templateGeometryReview.value
    const blockers = []
    if (geometryReview.layoutReviewPendingPages.length) blockers.push('部分页面的栏边界仍待核对，请调整后重新保存模板')
    if (geometryReview.missingQuestionNumbers.length) blockers.push(`请补齐缺失题号定位：${geometryReview.missingQuestionNumbers.join('、')}`)
    if (geometryReview.duplicateQuestionNumbers.length) blockers.push(`请处理重复题号定位：${geometryReview.duplicateQuestionNumbers.join('、')}`)
    if (geometryReview.orphanQuestionNumbers.length) blockers.push(`请处理未绑定到模板题目的题号定位：${geometryReview.orphanQuestionNumbers.join('、')}`)
    if (geometryReview.outOfBoundsHandwritingQuestions.length) blockers.push(`作答区域框越出页面范围：${geometryReview.outOfBoundsHandwritingQuestions.join('、')}，已自动收回页面内，请重试保存`)
    if (geometryReview.lowConfidenceQuestionNumbers.length || geometryReview.lowConfidenceHandwritingQuestions.length) {
      blockers.push(`低置信题目归属或作答区域需要人工复核：${[...geometryReview.lowConfidenceQuestionNumbers, ...geometryReview.lowConfidenceHandwritingQuestions].join('、')}`)
    }
    if (geometryReview.diagnosticReasons.length) {
      const detail = (geometryReview.diagnosticReviewDetails || []).slice(0, 3).join('；')
      blockers.push(`模型定位或逐图关联仍需人工复核，请核对后重新保存模板${detail ? `（${detail}）` : ''}`)
    }
    for (const { question } of templateChoiceConflicts.value) blockers.push(templateChoiceConflictReason(question))
    for (const page of templatePages.value) {
      for (const question of page.questions || []) {
        if (!questionScoringValid(question)) blockers.push(`第 ${Number(page.page_index) + 1} 页第 ${question.question_no} 题的题型或分值设置不完整，请检查该题的选项、填空数量及分值`)
      }
    }
    warnTemplateSaveBlockers(blockers.length ? blockers : ['当前模板页面信息不完整，修改已保留，请检查页面加载状态后重试保存模板'])
    return false
  }
  if (templateId !== pendingTemplateId.value) return false
  const payload = {
    template_id: templateId,
    category: form.category,
    exam_name: form.category === 'exam' ? form.exam_name.trim() : '',
    pages: templatePages.value.map((pg) => ({
      page_index: pg.page_index,
      questions: (pg.questions || []).map((q) => ({
        question_no: q.question_no,
        question_text: q.question_text || '',
        standard_answer: q.standard_answer || '',
        chapter: q.chapter || '',
        topic: q.topic || '',
        correction_excluded: q.correction_excluded === true,
        ...answerSpecFields(q),
        authorized_wrong_cycle_ids: q.authorized_wrong_cycle_ids || [],
        max_flags_kept: Array.isArray(q.max_flags_kept) ? [...q.max_flags_kept] : [],
      })),
    })),
  }
  const drawingReferenceSnapshot = templateDrawingReferenceSaveSnapshot()
  const controller = new AbortController()
  templateConfirmController = controller
  try {
    const res = await api.grade.templateConfirm(payload, controller.signal)
    if (controller.signal.aborted || templateId !== pendingTemplateId.value) return false
    if (!res.success) throw new Error(res.detail || '确认失败')
    clearSavedTemplateDrawingReferenceChanges(drawingReferenceSnapshot)
    // The template is now this batch's saved authority; drop the unfinished
    // draft checkpoint so a later page load does not reopen the editor.
    forgetTemplateRecognition()
    templateMode.value = normalizeTemplateMode(res.template_mode, templateMode.value)
    templateColumnMode.value = normalizeTemplateColumnMode(res.column_mode, templateColumnMode.value)
    confirmedTemplateId.value = templateId
    templatePageCount.value = res.page_count
    templateDialogVisible.value = false
    ElMessage.success(`本次模板已保存（${res.page_count} 页），开始识别学生试卷前仍可再次编辑`)
    return true
  } catch (err) {
    if (templateId === pendingTemplateId.value && err.name !== 'CanceledError' && err.name !== 'AbortError') notifyProcessingError(err, err.message || '模板确认失败，当前修改已保留')
    return false
  } finally {
    if (templateConfirmController === controller) templateConfirmController = null
  }
}

async function loadTemplateCorrectionCandidates() {
  if (!pendingTemplateId.value || confirmingTemplate.value || templateCorrectionCandidatesLoading.value) return false
  const generation = ++templateCorrectionCandidatesGeneration
  const templateId = pendingTemplateId.value
  const signature = templateQuestionSignature()
  templateCorrectionCandidatesLoading.value = true
  try {
    const res = await api.grade.templateCorrectionCandidates({
      template_id: templateId,
      pages: templatePages.value.map((page) => ({
        page_index: page.page_index,
        questions: (page.questions || []).map((q) => ({
          question_no: q.question_no,
          question_text: q.question_text || '',
          standard_answer: q.standard_answer || '',
          chapter: q.chapter || '',
          topic: q.topic || '',
          correction_excluded: q.correction_excluded === true,
          ...answerSpecFields(q),
        })),
      })),
    })
    if (generation !== templateCorrectionCandidatesGeneration || templateId !== pendingTemplateId.value
      || signature !== templateQuestionSignature() || confirmingTemplate.value) return false
    const byNo = new Map((res.candidates || []).map((item) => [String(item.question_no), item]))
    templatePages.value.forEach((page) => page.questions.forEach((q) => {
      const discovery = byNo.get(String(q.question_no))
      q.correction_candidates = discovery?.candidates || []
      q.correction_knowledge_summary = discovery?.knowledge_summary || null
      q.authorized_wrong_cycle_ids = q.correction_excluded
        ? []
        : q.correction_candidates.map((candidate) => candidate.cycle_id)
      if (q.correction_candidates.length) q._open = true
    }))
    return true
  } catch {
    return false
  } finally {
    templateCorrectionCandidatesLoading.value = false
  }
}

function toggleQuestionCorrection(question) {
  question.correction_excluded = !question.correction_excluded
  question.authorized_wrong_cycle_ids = question.correction_excluded
    ? []
    : (question.correction_candidates || []).map((candidate) => candidate.cycle_id)
}

function openTemplateEditor() {
  if (recognizing.value || grading.value) {
    ElMessage.warning('学生试卷已开始处理，本次模板已冻结')
    return
  }
  pendingTemplateId.value = confirmedTemplateId.value
  syncTemplateBatchScores()
  templateDialogVisible.value = true
  void loadTemplateCutState(pendingTemplateId.value)
  void loadTemplateCorrectionCandidates()
}

async function onImportTemplateBank() {
  if (confirmingTemplate.value || importingTemplateBank.value) return false
  if (templateBankImported.value) return false
  // Always save the latest editor contents before the one-time import.
  const saved = await onConfirmTemplate()
  if (!saved || !confirmedTemplateId.value) return
  importingTemplateBank.value = true
  try {
    const res = await api.grade.templateBankImport(confirmedTemplateId.value)
    if (!res.success) throw new Error(res.detail || '入库失败')
    templateBankImported.value = true
    ElMessage.success(res.already_imported
      ? '该模板已写入私库，本次未重复写入'
      : `已将 ${res.imported_count || 0} 道题按去重规则写入私库`)
  } catch (err) {
    notifyProcessingError(err, err.message || '模板入库失败')
  } finally {
    importingTemplateBank.value = false
  }
}

function clearTemplateDraft() {
  recognitionAutoArmed.value = true
  templateCutRunToken += 1
  templateCutCompletedRunToken = null
  templateRecognizeController?.abort()
  templateConfirmController?.abort()
  templateCutController?.abort()
  templateRecognizeController = null
  templateConfirmController = null
  templateCutController = null
  stopTemplateCutPolling()
  templateCutBusy.value = false
  recognizingTemplate.value = false
  confirmingTemplate.value = false
  importingTemplateBank.value = false
  templatePreprocessing.value = false
  revokeTemplateCutPreviews()
  forgetTemplateRecognition()
  confirmedTemplateId.value = ''
  pendingTemplateId.value = ''
  templatePages.value = []
  templateFiles.value = []
  templatePageCount.value = 0
  templateBankImported.value = false
  templateCutResult.value = null
  templateCutPreviewError.value = ''
  templateCutProgress.value = { completed: 0, total: 0, remaining: 0 }
  templateCutActionId.value = ''
  templateCutImageType.value = 'photo'
  templateRecognizeMessage.value = ''
  templateRecognizeError.value = ''
  templateMode.value = 'marked'
  templateColumnMode.value = 'double'
  templateChoiceScore.value = 5
  templateFillScore.value = 5
  templateDialogVisible.value = false
}

function cancelTemplateDialog() {
  if (confirmingTemplate.value || importingTemplateBank.value) return
  clearTemplateDraft()
  ElMessage.info('已取消本次模板识别，当前模板已清空')
}

function resetTemplate() {
  clearTemplateDraft()
}

async function processFiles(rawFiles, { fromExtraction = false } = {}) {
  if (imagePreprocessing.value || (imageExtracting.value && !fromExtraction)) {
    ElMessage.warning('图片正在处理中，请稍候')
    return
  }
  imagePreprocessing.value = true
  let failedCount = 0
  const failedFiles = []
  try {
    const imageFiles = rawFiles.filter((file) => file?.type?.startsWith('image/'))
    if (imageFiles.length !== rawFiles.length) {
      throw new Error('[IMPORT_UNSUPPORTED_FILE] 批改仅支持图片，请先完成文件提取')
    }
    const fakeList = imageFiles.map((f) => ({ raw: f, name: f.name, size: f.size }))
    const result = validateUploadList(fakeList, { maxFiles: 200 })
    if (result.errors.length) throw new Error(result.errors.join('；'))
    if (!result.files.length) return
    const preparedFiles = await compressImageFiles(result.files, 1920, 0.88, {
      onFileError: ({ file }) => { failedCount += 1; failedFiles.push(file.name) },
    })
    if (failedCount) {
      fileList.value = []
      ElMessage.error(`[IMPORT_IMAGE_INVALID] ${failedFiles.join('、')}：${failedCount} 张学生试卷图片损坏或尺寸超限，本次选择已清空，请检查原图后重新选择`)
      return
    }
    recognitionAutoArmed.value = true
    fileList.value = preparedFiles
    phoneCaptureSessionForFiles = ''
    clearReviewDisplay()
    return true
  } catch (err) {
    ElMessage.error(err.message || '学生试卷预处理未完成，请重新选择')
  } finally {
    imagePreprocessing.value = false
  }
}

async function onFileInputChange(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  if (imagePreprocessing.value || imageExtracting.value) return
  if (!files.length || !requireRecognitionService()) return

  // All sources use byte detection; neither MIME nor the suffix selects a decoder.
  {
    imageExtracting.value = true
    try {
      const extractedFiles = await extractImagesFromFileList(files, {
        onProgress: (current, total, message) => {
          extractionProgress.value = { current, total, message }
        }
      })

      if (extractedFiles.length === 0) {
        ElMessage.warning('未找到任何图片文件')
        return
      }

      if (await processFiles(extractedFiles, { fromExtraction: true })) ElMessage.success(`成功导入 ${extractedFiles.length} 张图片`)
    } catch (err) {
      ElMessage.error(err.message || '文件提取失败')
    } finally {
      imageExtracting.value = false
      extractionProgress.value = { current: 0, total: 0, message: '' }
    }
  }
}

async function onFolderInputChange(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  if (imagePreprocessing.value || imageExtracting.value) return
  if (!files.length || !requireRecognitionService()) return

  imageExtracting.value = true
  try {
    const extractedFiles = await extractImagesFromFileList(files, {
      onProgress: (current, total, message) => {
        extractionProgress.value = { current, total, message }
      }
    })

    if (extractedFiles.length === 0) {
      ElMessage.warning('文件夹中未找到任何图片文件')
      return
    }

    if (await processFiles(extractedFiles, { fromExtraction: true })) ElMessage.success(`成功导入 ${extractedFiles.length} 张图片`)
  } catch (err) {
    ElMessage.error(err.message || '文件提取失败')
  } finally {
    imageExtracting.value = false
    extractionProgress.value = { current: 0, total: 0, message: '' }
  }
}

async function onDrop(e) {
  isDragover.value = false
  if (imagePreprocessing.value || imageExtracting.value) return
  if (!requireRecognitionService()) return

  imageExtracting.value = true
  try {
    const extractedFiles = await extractImagesFromDrop(e, {
      onProgress: (current, total, message) => {
        extractionProgress.value = { current, total, message }
      }
    })

    if (extractedFiles.length === 0) {
      ElMessage.warning('未找到任何图片文件，请拖入图片、文件夹或支持的压缩包')
      return
    }

    if (await processFiles(extractedFiles, { fromExtraction: true })) ElMessage.success(`成功导入 ${extractedFiles.length} 张图片`)
  } catch (err) {
    ElMessage.error(err.message || '文件提取失败')
  } finally {
    imageExtracting.value = false
    extractionProgress.value = { current: 0, total: 0, message: '' }
  }
}

function resetFiles() {
  fileList.value = []
  phoneCaptureSessionForFiles = ''
  recognitionAutoArmed.value = true
}

function openPhoneCapture() {
  if (imagePreprocessing.value || imageExtracting.value) {
    ElMessage.warning('图片正在处理中，请稍候')
    return
  }
  if (requireRecognitionService()) phoneCaptureVisible.value = true
}

async function importPhoneCaptures(files, { sessionId = '' } = {}) {
  if (imagePreprocessing.value || imageExtracting.value) {
    ElMessage.warning('图片正在处理中，请稍候')
    return false
  }
  if (!requireRecognitionService()) return false
  if (!(await processFiles(files))) return false
  phoneCaptureSessionForFiles = sessionId
  ElMessage.success(`已导入 ${files.length} 张手机照片`)
  return true
}

function openStudentSubmissions() {
  if (imagePreprocessing.value || imageExtracting.value) {
    ElMessage.warning('图片正在处理中，请稍候')
    return
  }
  if (requireRecognitionService()) studentSubmissionVisible.value = true
}

// Pages students uploaded through a class link arrive as
// 学生提交/<姓名>_学号<学号>/第N页.jpg; the identity gate reads the folder name
// and still checks it against the name written on the paper.
async function importStudentSubmissions(files) {
  if (imagePreprocessing.value || imageExtracting.value) {
    ElMessage.warning('图片正在处理中，请稍候')
    return false
  }
  if (!requireRecognitionService()) return false
  if (!(await processFiles(files))) return false
  ElMessage.success(`已导入 ${files.length} 张学生上传的照片`)
  return true
}

// Phone photos stay on the server until a grading batch has stored its own
// copies; then the capture session is deleted.
function releasePhoneCaptureSession() {
  const sessionId = phoneCaptureSessionForFiles
  phoneCaptureSessionForFiles = ''
  if (!sessionId) return
  if (loadPhoneCaptureSession(auth.user?.id)?.session_id === sessionId) forgetPhoneCaptureSession(auth.user?.id)
  api.capture.close(sessionId, { suppressErrorToast: true }).catch(() => {})
}
function orientationReady(paper) {
  return ['auto_passed', 'teacher_confirmed'].includes(String(paper?.orientation_status || ''))
}
function orientationReasonText(reason) {
  const labels = {
    page_tilt_exceeds_limit: '页面倾斜过大，请重新拍摄',
    exif_rotation_requires_confirmation: '图片带有旋转方向，请旋转后重新上传',
    orientation_confidence_too_low: '无法确认方向，请重新拍摄',
    orientation_detection_failed: '无法检测方向，请重新拍摄',
    teacher_confirmed: '教师已确认方向',
  }
  return labels[reason] || '请确认文字正立后重新上传'
}

function resetOrientationReview() {
  orientationImageRequestId += 1
  if (orientationImageUrl.value) URL.revokeObjectURL(orientationImageUrl.value)
  orientationImageUrl.value = ''
  orientationImageLoading.value = false
  orientationImageError.value = ''
  orientationPaper.value = null
  orientationQuarterTurns.value = 0
  orientationActionId.value = ''
}

async function openOrientationReview(paper) {
  resetOrientationReview()
  orientationPaper.value = paper
  orientationQuarterTurns.value = Number(paper?.manual_quarter_turns || 0) % 4
  orientationDialogVisible.value = true
  orientationImageLoading.value = true
  const requestId = orientationImageRequestId
  try {
    const imageUrl = await api.grade.paperImage(batchId.value, paper.paper_id, 'original')
    if (requestId !== orientationImageRequestId) {
      URL.revokeObjectURL(imageUrl)
      return
    }
    orientationImageUrl.value = imageUrl
  } catch (err) {
    if (requestId === orientationImageRequestId) {
      orientationImageError.value = orientationErrorMessage(err, '原图加载失败，请刷新后重试')
    }
  } finally {
    if (requestId === orientationImageRequestId) orientationImageLoading.value = false
  }
}

function rotateOrientationPreview(delta) {
  orientationQuarterTurns.value = (orientationQuarterTurns.value + delta + 4) % 4
  orientationActionId.value = ''
}

function orientationErrorMessage(err, fallback) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') return detail.message || detail.error || fallback
  return err?.message || fallback
}

function applyOrientationState(payload, recoveryOptions = {}) {
  if (!payload?.batch_id) return
  batchId.value = String(payload.batch_id)
  papers.value = Array.isArray(payload.papers) ? payload.papers : []
  form.category = payload.category || form.category || 'daily'
  form.exam_name = payload.exam_name || ''
  form.mode = payload.mode || form.mode || 'precise'
  if (payload.source_mode === 'template' && payload.template_id) {
    confirmedTemplateId.value = String(payload.template_id)
    templateTeachingContext.value = payload.teaching_context || {}
    templateMode.value = normalizeTemplateMode(payload.template_mode, 'marked')
    templateColumnMode.value = normalizeTemplateColumnMode(payload.column_mode, templateColumnMode.value)
  }
  questions.value = payload.questions || []
  applyRecognitionDone(payload)
  if (
    ['recognizing_remaining', 'recognition_interrupted'].includes(payload.status)
    || (
      ['failed', 'ocr_partial_failed'].includes(payload.status)
      && Number(payload.retryable_failed_papers || 0) > 0
    )
  ) {
    recognitionDone.value = false
    recognitionCanGrade.value = false
    recognitionError.value = ''
    currentMessage.value = ''
  }
  activeStep.value = 1
  updateRecognitionRecoveryState(payload, recoveryOptions)
  if (!recoveryOptions.suppressAutoResume && recognitionRecoveryState.value.status === 'available' && !recognizing.value) {
    scheduleRetryableOcrAutoResume(batchId.value, payload)
  }
  rememberOrientationBatch(batchId.value)
}

function rememberRecognitionBatch(payload) {
  if (!payload?.batch_id) return
  batchId.value = String(payload.batch_id)
  rememberOrientationBatch(batchId.value)
}

function upsertRecognitionPaper(payload) {
  const paperId = String(payload?.paper_id || '')
  const filename = String(payload?.filename || '')
  const index = papers.value.findIndex((item) => (
    (paperId && String(item.paper_id || '') === paperId)
    || (!paperId && filename && String(item.filename || '') === filename)
  ))
  if (index >= 0) papers.value[index] = mergeRecognitionPaper(papers.value[index], payload)
  else papers.value.push(payload)
}

async function confirmOrientationAndRetry() {
  if (!requireRecognitionService()) return
  const paper = orientationPaper.value
  if (!batchId.value || !paper?.paper_id || orientationSaving.value) return
  orientationSaving.value = true
  orientationImageError.value = ''
  if (!orientationActionId.value) {
    orientationActionId.value = globalThis.crypto?.randomUUID?.()
      || `orientation-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  }
  try {
    const sourceResponse = await fetch(orientationImageUrl.value)
    if (!sourceResponse.ok) throw new Error('原图读取失败，请刷新后重试')
    const sourceBlob = await sourceResponse.blob()
    const sourceFile = new File(
      [sourceBlob],
      String(paper.filename || 'paper').replace(/\.[^.]+$/, '') || 'paper',
      { type: sourceBlob.type || 'image/jpeg' },
    )
    const rotatedFile = await createRotatedOcrReadyFile(
      sourceFile,
      orientationQuarterTurns.value,
    )
    const formData = new FormData()
    formData.append('action_id', orientationActionId.value)
    formData.append('quarter_turns_clockwise', String(orientationQuarterTurns.value))
    formData.append('file', rotatedFile)
    const payload = await api.grade.orientationConfirm(
      batchId.value,
      paper.paper_id,
      formData,
    )
    applyOrientationState(payload)
    orientationActionId.value = ''
    orientationDialogVisible.value = false
    if (payload.can_grade) {
      await onConfirmGrade()
      return
    }
    ElMessage.success('该卷方向已确认并完成单卷重识别，请继续处理其余异常试卷')
  } catch (err) {
    orientationImageError.value = orientationErrorMessage(err, '方向确认失败，请稍后重试')
    notifyProcessingError(err, orientationImageError.value)
  } finally {
    orientationSaving.value = false
  }
}

function canExcludeRecognitionPaper(paper) {
  if (!paper?.paper_id || !batchId.value || recognizing.value || grading.value) return false
  if (isExcludedPaper(paper)) return false
  // Resumable or waiting work goes first; exclusion is only for pages whose
  // automatic recovery is exhausted.
  if (['waiting', 'available', 'service_fault'].includes(recognitionRecoveryState.value.status)) return false
  return paperOcrFailed(paper)
}

async function excludeRecognitionPaper(paper) {
  if (!canExcludeRecognitionPaper(paper) || excludingPaperId.value) return false
  const label = paper.filename || paper.student_name || '该试卷'
  if (!window.confirm(`确定将“${label}”排除出本批次吗？该卷不会被批阅，也不会写入学生档案；其余试卷可以继续批阅。如需批阅该卷，请重新拍摄后单独上传。`)) return false
  excludingPaperId.value = String(paper.paper_id)
  try {
    const payload = await api.grade.excludePaper(batchId.value, paper.paper_id)
    applyOrientationState(payload)
    if (payload?.can_grade) {
      ElMessage.success('已排除该卷，开始批阅其余试卷')
      await onConfirmGrade()
      return true
    }
    ElMessage.success('已排除该卷，请继续处理其余未完成试卷')
    return true
  } catch (err) {
    notifyProcessingError(err, reviewErrorMessage(err, '排除失败，请刷新后重试'))
    return false
  } finally {
    excludingPaperId.value = ''
  }
}

function reset() {
  recognitionAutoArmed.value = true
  clearRecognitionAutoResume({ resetAttempts: true })
  fileList.value = []
  forgetOrientationBatch()
  resetRecognitionCompletionState()
  gradeError.value = ''
  // A pending/confirmed template freezes the grading contract; only clear student papers.
  if (confirmedTemplateId.value || pendingTemplateId.value || templateDialogVisible.value) return
  form.category = ''
  form.exam_name = ''
  form.mode = 'precise'
  templateMode.value = 'marked'
  templateColumnMode.value = 'double'
}

async function continueAfterRecognition() {
  if (recognizing.value || grading.value) return false
  if (!recognitionDone.value || !recognitionCanGrade.value) {
    recognitionError.value ||= '本批次 OCR 尚未完整完成，不能开始批阅。'
    ElMessage.warning(recognitionError.value)
    return false
  }
  if (!questions.value.length) {
    recognitionError.value = '识别结果缺少可批阅题目，请重新识别或联系管理员。'
    ElMessage.warning(recognitionError.value)
    return false
  }
  return onConfirmGrade()
}

function startNewBatch() {
  if (recognizing.value || grading.value) return
  // Only release this page's recovery pointers; durable server batches and
  // the teacher-confirmed template remain untouched.
  clearRecognitionAutoResume({ resetAttempts: true })
  forgetOrientationBatch()
  activeStep.value = 0
  questions.value = []
  papers.value = []
  batchId.value = ''
  resetRecognitionCompletionState()
  gradeError.value = ''
  resetFiles()
  forgetReviewBatch()
  clearReviewDisplay()
  resetRecognitionRecoveryState()
  identityResolutions.value = []
  identityOptionsLoading.value = false
  identityOptionsError.value = ''
  recognized.current = 0
  recognized.total = 0
  currentMessage.value = ''
}

// ── 开始识别：先补齐批次信息（后端必需），再进入识别 ──
function onStartRecognize() {
  if (!requireRecognitionService()) return
  if (!fileList.value.length) { ElMessage.warning('请先选择文件'); return }
  if (!confirmedTemplateId.value) {
    ElMessage.warning('请先识别并保存本次模板')
    return
  }
  if (templateDialogVisible.value) {
    ElMessage.warning('请先保存或关闭模板编辑窗口')
    return
  }
  if (!anonymous.value && !auth.currentClassId) {
    ElMessage.warning('请先在顶部选择或创建班级')
    return
  }
  batchDialogVisible.value = true
}

function maybeAutoStartRecognition() {
  // Only the saved answer/geometry contract permits comparison and grading;
  // finishing template correction or student image preparation does not.
  if (!recognitionAutoArmed.value || !confirmedTemplateId.value || !fileList.value.length) return
  if (recognizing.value || grading.value || imagePreprocessing.value || imageExtracting.value) return
  if (recognitionBlocksNewWork.value || templateDialogVisible.value) return
  if (!batchConfirmValid.value || (!anonymous.value && !auth.currentClassId)) return
  recognitionAutoArmed.value = false
  batchDialogVisible.value = false
  void runRecognize()
}

function onBatchConfirm() {
  if (!requireRecognitionService()) return
  if (!batchConfirmValid.value) return
  recognitionAutoArmed.value = false
  batchDialogVisible.value = false
  runRecognize()
}

// ── 阶段一：按已确认模板识别学生试卷 ──
async function runRecognize() {
  if (!requireRecognitionService()) return
  if (recognizing.value) return
  if (!fileList.value.length) { ElMessage.warning('请先选择文件'); return }
  if (!form.category) { ElMessage.warning('请选择批次类型'); return }

  const fd = new FormData()
  appendStudentUploadFiles(fd, fileList.value)
  fd.append('category', form.category)
  fd.append('mode', form.mode)
  if (anonymous.value) fd.append('anonymous', 'true')
  else if (inheritIdentity.value) fd.append('inherit_identity', 'true')
  if (form.category === 'exam' && form.exam_name) fd.append('exam_name', form.exam_name)
  // 学生试卷始终复用已确认模板；服务端拒绝缺少 template_id 的请求。
  fd.append('template_id', confirmedTemplateId.value)

  recognizing.value = true
  activeStep.value = 1
  identityResolutions.value = []
  identityOptionsLoading.value = false
  identityOptionsError.value = ''
  papers.value = []
  questions.value = []
  clearRecognitionAutoResume({ resetAttempts: true })
  // Keep the old durable storage pointer until this request creates a batch,
  // but never use that older batch to recover a failed new upload.
  batchId.value = ''
  resetRecognitionCompletionState()
  resetRecognitionRecoveryState()
  gradeError.value = ''
  recognized.current = 0
  recognized.total = fileList.value.length
  progressList.value = []
  gradeTotal.value = fileList.value.length
  currentMessage.value = ''
  recognizeController = new AbortController()

  let requestBatchId = ''
  let doneData = null
  try {
    const response = await api.grade.recognize(fd, recognizeController.signal)
    void loadDailyUsage()
    if (!response.ok) {
      throw await createSseResponseError(response, '识别请求失败')
    }
    await consumeSse(response, (evt, data) => {
      if (data?.batch_id) {
        requestBatchId = String(data.batch_id)
        releasePhoneCaptureSession()
      }
      if (evt === 'progress') {
        rememberRecognitionBatch(data)
        currentMessage.value = data.message || ''
        if (typeof data.current === 'number') recognized.current = data.current
        if (typeof data.total === 'number') recognized.total = data.total
      } else if (evt === 'paper') {
        rememberRecognitionBatch(data)
        upsertRecognitionPaper(data)
      } else if (evt === 'result') {
        progressList.value.push(data)
      } else if (evt === 'done') {
        rememberRecognitionBatch(data)
        doneData = data
        questions.value = data.questions || []
        clearRecognitionAutoResume({ resetAttempts: true })
        applyRecognitionDone(data)
        currentMessage.value = ''
      } else if (evt === 'error') {
        rememberRecognitionBatch(data)
        throw createSseEventError(data, '识别出错')
      }
    })
    if (!recognitionCanGrade.value) {
      activeStep.value = 1
      scheduleRetryableOcrAutoResume(requestBatchId, doneData)
      return
    }
    if (!questions.value.length) {
      throw new Error('未识别到可用于批改的题目，请检查视觉模型服务、网络或试卷图片')
    }
    // 模板题干和答案已经由老师确认，识别完成后直接进入批改。
    activeStep.value = 1
    await onConfirmGrade()
  } catch (err) {
    const signal = recognizeController?.signal
    if (!isSseRequestCancelled(err, signal)) {
      const failureCode = String(err?.code || '').toUpperCase()
      const targetBatchId = requestBatchId
      const connectionRecovery = canRecoverRecognitionConnection(err, signal)
      const serviceFault = isSseServiceFault(err, { signal })
      const refreshed = targetBatchId
        ? await refreshRecognitionStateAfterFailure(targetBatchId, {
            failureCode, serviceFault: serviceFault && !connectionRecovery,
            isCurrent: () => !signal?.aborted && targetBatchId === String(batchId.value || ''),
          })
        : false
      if (signal?.aborted) return
      if (connectionRecovery && await continueRefreshedRecognition(refreshed)) return
      if (connectionRecovery && recoverRecognitionConnection(targetBatchId, refreshed)) return
      reportSseServiceFault(
        err, markServiceFault,
        '学生试卷识别链路中断，请稍后重试；若持续发生请联系管理员。',
        { signal },
      )
      if (!targetBatchId) updateRecognitionRecoveryState({}, { failureCode, serviceFault })
      if (!refreshed) recognitionDone.value = false
      recognitionCanGrade.value = false
      const rawMessage = String(err?.message || '')
      recognitionError.value = (
        err instanceof SseTransportError
        || rawMessage === 'Failed to fetch'
        || rawMessage === 'Network Error'
        || /networkerror/i.test(rawMessage)
      )
        ? '识别连接中断：服务器可能正在重启或预处理超时，请勿连续重复提交；稍后重试，若仍失败请联系管理员查看该时段服务日志。'
        : (rawMessage || '识别失败')
      currentMessage.value = ''
      notifyProcessingError(err, recognitionRecoveryState.value.message || recognitionError.value)
      activeStep.value = 1
    } else {
      activeStep.value = 0
    }
  } finally {
    recognizing.value = false
    recognizeController = null
  }
}

async function loadDailyUsage() {
  try {
    const data = await api.usage.stats()
    dailyUsage.value = data?.daily_images || data?.monthly_images || null
  } catch {
    dailyUsage.value = null
  }
}

async function onConfirmIdentitiesAndGrade() {
  if (grading.value) return false
  if (!identityCanConfirm.value) {
    ElMessage.warning('请为每张未确认试卷选择名单中的学生；确需例外新建时，请确认姓名和学号')
    return
  }
  identityDialogVisible.value = false
  await onConfirmGrade(true)
}

// ── 阶段二：按冻结模板的答案批改 ──
async function onConfirmGrade(skipIdentityGate = false) {
  if (grading.value) return false
  if (!requireBatchService()) {
    gradeError.value = `批阅未开始：${serviceState.value.message || '批改链路当前不可用，请稍后再试。'}`
    return false
  }
  if (!recognitionDone.value || !recognitionCanGrade.value) {
    recognitionError.value ||= '本批次 OCR 尚未完整完成，不能开始部分批阅。请重新识别全部试卷。'
    gradeError.value = `批阅未开始：${recognitionError.value}`
    activeStep.value = 1
    ElMessage.warning(recognitionError.value)
    return false
  }
  if (!skipIdentityGate && needsIdentityResolution.value) {
    identityDialogVisible.value = true
    if (!identityResolutions.value.length) await prepareIdentityResolutions()
    return false
  }

  const payload = {
    batch_id: batchId.value,
    mode: form.mode,
    category: form.category || 'daily',
    exam_name: form.category === 'exam' ? form.exam_name : '',
    // The server grades against the batch's frozen template answers.
    identity_resolutions: identityResolutionPayload(),
  }

  const expectedGradeTotal = gradablePaperCount.value
  if (!expectedGradeTotal) {
    gradeError.value = '当前没有可批阅的试卷，请重新识别全部试卷。'
    ElMessage.warning(gradeError.value)
    return false
  }

  grading.value = true
  reviewBusy.value = false
  reviewArchived.value = false
  reviewRevision.value = 0
  progressList.value = []
  gradeTotal.value = expectedGradeTotal
  gradeError.value = ''
  summary.value = null
  reviewRestoredLabel.value = ''
  coursewareRecommendations.value = []
  coursewareRecommendationState.value = 'idle'
  coursewareRecommendationQuery.value = {}
  coursewareRecommendationKey = ''
  resetBatchReport()
  currentMessage.value = ''
  gradeController = new AbortController()
  startGradingClock()
  let completedSummary = null
  let completedRevision = 0

  try {
    const response = await api.grade.confirm(payload, gradeController.signal)
    if (!response.ok) {
      throw await createSseResponseError(response, '批改请求失败')
    }
    await consumeSse(response, (evt, data) => {
      if (evt === 'progress') {
        currentMessage.value = data.message || ''
        if (typeof data.total === 'number') gradeTotal.value = data.total
        anchorGradingClock(data.grading_elapsed_seconds)
      } else if (evt === 'result') {
        progressList.value.push(data)
        currentMessage.value = ''
      } else if (evt === 'done') {
        // `done` is sent only after the server saved this batch's review, so
        // keep the current batch restorable even if this client rejects the
        // stream below; an older saved review must not take its place.
        if (Number(data.total) > 0) rememberReviewBatch(batchId.value)
        const completion = validateGradeCompletion({
          expectedTotal: expectedGradeTotal,
          reportedTotal: data.total,
          receivedTotal: progressList.value.length,
        })
        if (!completion.complete) throw new SseTransportError(completion.message)
        completedSummary = {
          total: data.total,
          avg_score: data.avg_score ?? null,
          grading_elapsed_seconds: data.grading_elapsed_seconds ?? null,
        }
        completedRevision = Number(data.review_revision || 0)
        currentMessage.value = ''
      } else if (evt === 'error') {
        throw createSseEventError(data, '批改出错')
      }
    })
    if (completedSummary) {
      summary.value = completedSummary
      reviewRevision.value = completedRevision
      rememberReviewBatch(batchId.value)
      forgetOrientationBatch()
      // SSE rows are progress summaries; reload the authoritative persisted review
      // so page groups and per-question page references are available immediately.
      const reviewPayload = await api.grade.reviewGet(batchId.value)
      if (!hydrateReviewState(reviewPayload)) throw new Error('批阅结果已生成，但审阅工作台恢复失败')
      startBatchReportPolling()
      const elapsed = summary.value.grading_elapsed_seconds
      ElMessage.success(
        `批改完成：共 ${summary.value.total} ${anonymous.value ? '页' : '张'}`
        + (elapsed != null ? `，批阅用时 ${formatGradingDuration(elapsed)}` : ''),
      )
      // 完成后回到上传步骤，结果保留在进度卡片
      activeStep.value = 0
      fileList.value = []
      return true
    }
    throw new SseTransportError('批改连接提前结束')
  } catch (err) {
    summary.value = null
    const signal = gradeController?.signal
    if (!isSseRequestCancelled(err, signal)) {
      reportSseServiceFault(
        err,
        markServiceFault,
        '批改链路连接中断，已完成结果会保留；请稍后重试。',
        { signal },
      )
      gradeError.value = err.message || '批改失败'
      notifyProcessingError(err, gradeError.value)
    }
  } finally {
    stopGradingClock()
    grading.value = false
    gradeController = null
  }
  return false
}

function startGradingClock() {
  stopGradingClock()
  gradingClockStart.value = Date.now()
  gradingClockNow.value = gradingClockStart.value
  gradingClockTimer = window.setInterval(() => { gradingClockNow.value = Date.now() }, 1000)
}

function anchorGradingClock(serverElapsedSeconds) {
  const seconds = Number(serverElapsedSeconds)
  if (!gradingClockStart.value || serverElapsedSeconds == null || !Number.isFinite(seconds) || seconds < 0) return
  gradingClockNow.value = Date.now()
  gradingClockStart.value = gradingClockNow.value - seconds * 1000
}

function stopGradingClock() {
  if (gradingClockTimer) window.clearInterval(gradingClockTimer)
  gradingClockTimer = null
  gradingClockStart.value = 0
}

function reviewErrorMessage(err, fallback) {
  return err?.response?.data?.detail || err?.message || fallback
}

function reviewPaperId(row) {
  return String(row?.paper_id || row?.review_id || '')
}

function orientationStorageKey() {
  const teacherId = auth.user?.id
  const mode = anonymous.value ? ':anonymous' : ''
  return teacherId ? `${ORIENTATION_BATCH_STORAGE_PREFIX}${mode}:${teacherId}` : ''
}

function rememberOrientationBatch(id) {
  const key = orientationStorageKey()
  if (!key || !id) return
  localStorage.setItem(key, String(id))
  // Grading success clears this pointer, so it only ever names work newer than
  // the saved review. A review pointer for another batch would otherwise win
  // on the next mount and hide this unfinished batch.
  const reviewKey = reviewStorageKey()
  const savedReview = reviewKey ? localStorage.getItem(reviewKey) : ''
  if (savedReview && savedReview !== String(id)) localStorage.removeItem(reviewKey)
}

function forgetOrientationBatch() {
  const key = orientationStorageKey()
  if (key) localStorage.removeItem(key)
}

async function resumeOrientationBatch() {
  const key = orientationStorageKey()
  const savedBatchId = key ? localStorage.getItem(key) : ''
  if (!savedBatchId) return false
  try {
    const payload = await api.grade.orientationState(savedBatchId)
    if (String(payload?.batch_id || '') !== String(savedBatchId) || !Array.isArray(payload.papers) || !payload.papers.length) {
      throw new Error('方向复核批次没有可恢复内容')
    }
    applyOrientationState(payload)
    if (recognitionCanGrade.value) {
      await onConfirmGrade()
    } else if (['recognizing_remaining', 'recognition_interrupted'].includes(payload.status)) {
      recoverRecognitionConnection(savedBatchId, payload)
    }
    return true
  } catch (err) {
    const status = Number(err?.response?.status || err?.status || 0)
    if ([401, 403, 404].includes(status)) forgetOrientationBatch()
    return false
  }
}

async function refreshRecognitionStateAfterFailure(targetBatchId, recoveryOptions) {
  try {
    const payload = await api.grade.orientationState(targetBatchId)
    if (recoveryOptions?.isCurrent && !recoveryOptions.isCurrent()) return false
    if (String(payload?.batch_id || '') !== String(targetBatchId) || !Array.isArray(payload.papers) || !payload.papers.length) {
      throw new Error('识别批次状态响应不完整')
    }
    applyOrientationState(payload, recoveryOptions)
    return payload
  } catch (err) {
    if (recoveryOptions?.isCurrent && !recoveryOptions.isCurrent()) return false
    const status = Number(err?.response?.status || err?.status || 0)
    if ([401, 403, 404].includes(status)) forgetOrientationBatch()
    const serviceFault = Boolean(recoveryOptions?.serviceFault) || !status || status >= 500 || [408, 429].includes(status)
    if (serviceFault && !recoveryOptions?.serviceFault) {
      markServiceFault('无法刷新识别批次状态，请检查网络或联系管理员。')
    }
    updateRecognitionRecoveryState({}, { ...recoveryOptions, serviceFault })
    return false
  }
}

async function continueRefreshedRecognition(refreshed) {
  if (!refreshed || !recognitionDone.value || !recognitionCanGrade.value) return false
  if (!questions.value.length) {
    recognitionCanGrade.value = false
    recognitionError.value = '恢复识别后未得到可批阅题目'
    return false
  }
  clearRecognitionAutoResume({ resetAttempts: true })
  currentMessage.value = ''
  // The snapshot is authoritative; transport failure must not erase it or
  // raise a second recognition error. Keep the existing identity/grade gates.
  await onConfirmGrade()
  return true
}

async function resumeRecognitionBatch(resumeBatchId, { automatic = false } = {}) {
  // This endpoint resumes an already-created durable batch. It may consume an
  // artifact that the busy accelerator is about to finish, so the new-work
  // free-slot gate must not suppress the recovery request.
  if (!requireBatchService()) return false
  const targetBatchId = String(resumeBatchId || batchId.value || '')
  if (!targetBatchId || recognizing.value) return false
  clearRecognitionAutoResume({ resetAttempts: !automatic })

  recognizing.value = true
  activeStep.value = 1
  resetRecognitionCompletionState()
  resetRecognitionRecoveryState()
  recognitionError.value = ''
  currentMessage.value = '正在从已保存进度恢复识别'
  recognizeController = new AbortController()
  let doneData = null
  try {
    const response = await api.grade.recognitionResume(targetBatchId, recognizeController.signal)
    if (!response.ok) {
      throw await createSseResponseError(response, '恢复识别请求失败')
    }
    await consumeSse(response, (evt, data) => {
      rememberRecognitionBatch(data)
      if (evt === 'progress') {
        currentMessage.value = data.message || ''
        if (typeof data.current === 'number') recognized.current = data.current
        if (typeof data.total === 'number') recognized.total = data.total
      } else if (evt === 'paper') {
        upsertRecognitionPaper(data)
      } else if (evt === 'done') {
        doneData = data
        questions.value = data.questions || []
        clearRecognitionAutoResume({ resetAttempts: true })
        applyRecognitionDone(data)
        currentMessage.value = ''
      } else if (evt === 'error') {
        throw createSseEventError(data, '恢复识别出错')
      }
    })

    if (!recognitionCanGrade.value) {
      activeStep.value = 1
      scheduleRetryableOcrAutoResume(targetBatchId, doneData)
      return true
    }
    if (!questions.value.length) throw new Error('恢复识别后未得到可批阅题目')
    await onConfirmGrade()
    return true
  } catch (err) {
    const signal = recognizeController?.signal
    if (!isSseRequestCancelled(err, signal)) {
      const failureCode = String(err?.code || '').toUpperCase()
      const connectionRecovery = canRecoverRecognitionConnection(err, signal)
      const serviceFault = isSseServiceFault(err, { signal })
      const refreshed = await refreshRecognitionStateAfterFailure(targetBatchId, {
        failureCode, serviceFault: serviceFault && !connectionRecovery,
        isCurrent: () => !signal?.aborted && targetBatchId === String(batchId.value || ''),
      })
      if (signal?.aborted) return false
      if (connectionRecovery && await continueRefreshedRecognition(refreshed)) return true
      if (connectionRecovery && recoverRecognitionConnection(targetBatchId, refreshed)) return false
      reportSseServiceFault(
        err, markServiceFault,
        '学生试卷识别恢复链路中断，已完成结果仍会保留；请稍后重试。',
        { signal },
      )
      if (!refreshed) recognitionDone.value = false
      recognitionCanGrade.value = false
      recognitionError.value = err.message || '恢复识别失败'
      currentMessage.value = ''
      activeStep.value = 1
      notifyProcessingError(err, recognitionRecoveryState.value.message || recognitionError.value)
    }
    return false
  } finally {
    recognizing.value = false
    recognizeController = null
  }
}

function reviewStorageKey() {
  const teacherId = auth.user?.id
  const mode = anonymous.value ? ':anonymous' : ''
  return teacherId ? `${REVIEW_BATCH_STORAGE_PREFIX}${mode}:${teacherId}` : ''
}

function rememberReviewBatch(id) {
  const key = reviewStorageKey()
  if (key && id) localStorage.setItem(key, String(id))
}

function forgetReviewBatch() {
  const key = reviewStorageKey()
  if (key) localStorage.removeItem(key)
}

function clearReviewDisplay() {
  summary.value = null
  progressList.value = []
  reviewRestoredLabel.value = ''
  reviewRevision.value = 0
  reviewArchived.value = false
  gradeError.value = ''
  coursewareRecommendations.value = []
  coursewareRecommendationState.value = 'idle'
  coursewareRecommendationQuery.value = {}
  coursewareRecommendationKey = ''
  resetBatchReport()
}

function coursewareStageLabel(stage) {
  return ({ primary: '小学', junior: '初中', senior: '高中' })[stage] || '当前学段'
}

async function loadCoursewareRecommendations() {
  // 推荐合同（内部设计文档 2026-09-13 §9）：精确知识点 > 同章知识点，最多 3 个，
  // 只含当前教师学段，须显示命中的学段、章节、知识点和原因；无匹配显示“暂无匹配”。
  const stats = batchReportStats.value
  // The unclassified bucket is not a knowledge point; never let it win the match.
  const points = (Array.isArray(stats.knowledge_points) ? stats.knowledge_points : [])
    .filter((item) => item && (item.topic || item.name) && (item.topic || item.name) !== '未分类')
    .sort((a, b) => Number(b.error_count || b.count || 0) - Number(a.error_count || a.count || 0))
  const top = points[0] || {}
  const query = {
    chapter: top.chapter === '未分类' ? '' : String(top.chapter || '').trim(),
    knowledge_point: String(top.topic || top.name || '').trim(),
  }
  const key = `${batchId.value || ''}|${query.chapter}|${query.knowledge_point}`
  coursewareRecommendationKey = key
  coursewareRecommendationQuery.value = query
  if (!query.chapter && !query.knowledge_point) {
    coursewareRecommendations.value = []
    coursewareRecommendationState.value = 'empty'
    return
  }
  coursewareRecommendationState.value = 'loading'
  try {
    const data = await api.courseware.recommendations({ ...query, limit: 3 })
    if (coursewareRecommendationKey !== key) return
    const items = Array.isArray(data?.items) ? data.items : []
    if (data?.matched === false || !items.length) {
      coursewareRecommendations.value = []
      coursewareRecommendationState.value = 'empty'
      return
    }
    coursewareRecommendations.value = items.slice(0, 3).map((item) => ({
      id: item.id,
      title: item.title || `${item.chapter || ''}：${item.knowledgePoint || item.knowledge_point || ''}`,
      chapter: item.chapter || query.chapter,
      knowledgePoint: item.knowledgePoint || item.knowledge_point || query.knowledge_point,
      stageLabel: coursewareStageLabel(item.stage || auth.user?.education_stage),
      reason: item.recommendation_reason || item.recommendationReason || '',
    }))
    coursewareRecommendationState.value = 'ready'
  } catch {
    if (coursewareRecommendationKey !== key) return
    coursewareRecommendations.value = []
    coursewareRecommendationState.value = 'error'
  }
}

function openCourseware(card) {
  router.push({
    path: '/courseware',
    query: {
      ...(card.chapter ? { chapter: card.chapter } : {}),
      ...(card.knowledgePoint ? { knowledge_point: card.knowledgePoint } : {}),
    },
  })
}

function resetBatchReport() {
  batchReportPollGeneration += 1
  if (batchReportPollTimer) {
    window.clearTimeout(batchReportPollTimer)
    batchReportPollTimer = null
  }
  batchReport.value = {
    status: 'not_started',
    text: '',
    stats: {},
    error: '',
    updated_at: '',
  }
}

function applyBatchReport(payload) {
  const report = payload?.report || payload || {}
  batchReport.value = {
    status: String(report.status || 'not_started'),
    text: String(report.text || ''),
    stats: report.stats && typeof report.stats === 'object' ? report.stats : {},
    error: String(report.error || ''),
    updated_at: String(report.updated_at || ''),
  }
  if (batchReport.value.status === 'ready') void loadCoursewareRecommendations()
}

function batchReportStatusText(status) {
  return ({
    not_started: '等待生成',
    queued: '等待生成',
    generating: '正在生成',
    ready: '已生成',
    failed: '生成失败',
  })[status] || '处理中'
}

function batchReportStatusTone(status) {
  if (status === 'ready') return 'is-good'
  if (status === 'failed') return 'is-bad'
  return 'is-warn'
}

function reportPercent(value) {
  return value == null ? '—' : `${(Number(value) * 100).toFixed(1)}%`
}

// The report waits for classification, so a bucket still without a knowledge
// point failed to classify; show that instead of the internal "未分类" key.
function reportKnowledgeName(item) {
  const name = String(item?.topic || item?.name || '').trim()
  return !name || name === '未分类' ? CLASSIFICATION_LABELS.failed : name
}

function reportKnowledgeChapter(item) {
  const chapter = String(item?.chapter || '').trim()
  return !chapter || chapter === '未分类' ? '未分章' : chapter
}

function nonNegativeReportCount(value) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number : 0
}

function reportDistributionPercent(value, total) {
  const amount = Number(value)
  const denominator = Number(total)
  if (!Number.isFinite(amount) || !Number.isFinite(denominator) || denominator <= 0) return 0
  return Math.min(100, Math.max(0, (amount / denominator) * 100))
}

function reportEntries(value) {
  return Object.entries(value || {}).sort((a, b) => Number(b[1]) - Number(a[1]))
}

function stopBatchReportPolling() {
  if (batchReportPollTimer) {
    window.clearTimeout(batchReportPollTimer)
    batchReportPollTimer = null
  }
}

async function pollBatchReport() {
  const currentBatchId = String(batchId.value || '')
  const currentGeneration = batchReportPollGeneration
  if (!currentBatchId) return
  try {
    const payload = await api.grade.reportGet(currentBatchId)
    if (
      currentGeneration !== batchReportPollGeneration
      || currentBatchId !== String(batchId.value || '')
    ) return
    applyBatchReport(payload)
    if (['ready', 'failed'].includes(batchReport.value.status)) {
      stopBatchReportPolling()
      return
    }
  } catch {
    if (
      currentGeneration !== batchReportPollGeneration
      || currentBatchId !== String(batchId.value || '')
    ) return
    // Keep the result table usable; the next poll can recover from a brief outage.
  }
  if (
    currentGeneration !== batchReportPollGeneration
    || currentBatchId !== String(batchId.value || '')
  ) return
  stopBatchReportPolling()
  batchReportPollTimer = window.setTimeout(pollBatchReport, 2500)
}

function startBatchReportPolling() {
  batchReportPollGeneration += 1
  stopBatchReportPolling()
  void pollBatchReport()
}

const REVIEW_COMPLETE_STATUSES = new Set(['review_ready', 'voided', 'archived', 'done'])

function hydrateReviewState(payload) {
  const rows = Array.isArray(payload?.results) ? payload.results : []
  if (!payload?.batch_id || !rows.length) return false
  // Pipeline grading saves rows while a batch is still being graded; those
  // partial rows must never be shown as a finished review.
  const status = String(payload.status || '')
  if (status && !REVIEW_COMPLETE_STATUSES.has(status)) return false
  const payloadIsAnonymous = String(payload.identity_mode || 'identified') === 'anonymous'
  if (payloadIsAnonymous !== anonymous.value) return false
  batchId.value = String(payload.batch_id)
  progressList.value = rows
  form.category = rows[0]?.category || 'daily'
  form.exam_name = rows[0]?.exam_name || ''
  gradeTotal.value = Number(payload.summary?.total ?? rows.length)
  summary.value = {
    total: Number(payload.summary?.total ?? rows.length),
    avg_score: payload.summary?.avg_score ?? null,
    grading_elapsed_seconds: payload.summary?.grading_elapsed_seconds ?? null,
  }
  reviewRevision.value = Number(payload.review_revision || 0)
  applyBatchReport(payload.report || {})
  reviewArchived.value = !rows.some((row) => row.disposition === 'active')
  manualDecisionDialogVisible.value = false
  manualDecisionRow.value = null
  manualDecisionDrafts.value = []
  activeStep.value = 0
  return true
}

async function resumeReviewBatch() {
  const key = reviewStorageKey()
  const savedBatchId = key ? localStorage.getItem(key) : ''
  if (!savedBatchId) return false
  try {
    const payload = await api.grade.reviewGet(savedBatchId)
    if (!hydrateReviewState(payload)) {
      const status = String(payload?.status || '')
      if (payload?.batch_id && status && !REVIEW_COMPLETE_STATUSES.has(status)) {
        // An unfinished batch resumes through recognition/grading recovery.
        forgetReviewBatch()
        rememberOrientationBatch(String(payload.batch_id))
        return false
      }
      throw new Error('审阅批次没有可恢复的结果')
    }
    reviewRestoredLabel.value = `已恢复历史批次 ${savedBatchId}`
    startBatchReportPolling()
    if (reviewArchived.value) forgetReviewBatch()
    return true
  } catch (err) {
    // Only a definitive ownership/expiry response invalidates a saved pointer.
    // Network and server failures must retain it for a later recovery attempt.
    const status = Number(err?.response?.status || err?.status || 0)
    if ([401, 403, 404].includes(status)) forgetReviewBatch()
    return false
  }
}

function maybeForgetTerminalReview() {
  if (!hasActiveReviewRows.value) forgetReviewBatch()
}

function applyReviewToProgressRow(review) {
  const paperId = reviewPaperId(review)
  const row = progressList.value.find((item) => reviewPaperId(item) === paperId)
  if (!row) return
  const result = review.result || {}
  Object.assign(row, {
    disposition: review.disposition || row.disposition,
    category: review.category || row.category || form.category,
    exam_name: review.exam_name || row.exam_name || '',
    score: result.score ?? null,
    max_score: result.max_score ?? null,
    correct_rate: result.correct_rate ?? null,
    correct: Number(result.correct_count || 0),
    partial: Number(result.partial_count || 0),
    wrong: Number(result.wrong_count || 0),
    needs_review: Number(result.needs_review_count || 0),
    total: Number(result.total || 0),
    details: result.details || [],
  })
}

function openManualDecision(row) {
  const groupId = String(row.review_group_id || '')
  const groupRows = progressList.value.filter((item) => (
    groupId ? String(item.review_group_id || '') === groupId : reviewPaperId(item) === reviewPaperId(row)
  ))
  const details = groupRows.flatMap((groupRow) => (
    Array.isArray(groupRow.details) ? groupRow.details : []
  ).map((detail) => ({
    ...detail,
    paper_id: String(detail.paper_id || reviewPaperId(groupRow)),
    page_index: Number(detail.page_index ?? groupRow.page_index ?? 0),
    editable: groupRow.disposition === 'active',
  })))
  if (!details.length) {
    ElMessage.error('该结果缺少逐题明细，请刷新批改结果后重试')
    return
  }
  manualDecisionRow.value = row
  manualDecisionPages.value = Array.isArray(row.pages) && row.pages.length
    ? row.pages.map((page, index) => ({ ...page, page_index: Number(page.page_index ?? index) }))
    : [{ paper_id: reviewPaperId(row), page_index: 0, filename: row.filename || '' }]
  manualDecisionPageIndex.value = Math.min(
    manualDecisionPages.value.length - 1,
    Math.max(0, Number(row.page_index || 0)),
  )
  manualDecisionDrafts.value = details.map((detail) => {
    const verdict = ['correct', 'partial', 'wrong'].includes(detail.result) ? detail.result : ''
    return {
      question_no: String(detail.question_no || detail.question_number || ''),
      paper_id: String(detail.paper_id || ''),
      page_index: Number(detail.page_index || 0),
      editable: detail.editable !== false,
      question: detail.question || detail.question_text || '',
      question_type: detail.question_type || 'solve',
      student_answer: detail.student_answer || '',
      error_type: detail.error_type || '',
      answer_state: detail.answer_state || '',
      slot_identity_error: detail.slot_identity_error || '',
      answer_kind: detail.answer_kind || 'non_graphic',
      correct_answer: detail.correct_answer || detail.standard_answer || '',
      drawing_assessment: detail.drawing_assessment || null,
      text_assessment: detail.text_assessment || null,
      answer_bboxes: detail.answer_bboxes || detail.answer_bbox || detail.student_answer_bbox || null,
      model_result: detail.result || 'needs_review',
      max_score: detail.max_score ?? null,
      awarded_score: detail.awarded_score ?? null,
      result: verdict,
      note: '',
    }
  })
  manualDecisionShowPaper.value = true
  manualDecisionDialogVisible.value = true
  void loadManualDecisionImage()
}

function resetManualDecisionImage() {
  manualDecisionImageRequestId += 1
  if (manualDecisionImageUrl.value) URL.revokeObjectURL(manualDecisionImageUrl.value)
  manualDecisionImageUrl.value = ''
  manualDecisionImageLoading.value = false
  manualDecisionImageError.value = ''
}

async function loadManualDecisionImage() {
  resetManualDecisionImage()
  const requestId = manualDecisionImageRequestId
  const paperId = String(manualDecisionPages.value[manualDecisionPageIndex.value]?.paper_id || '')
  if (!batchId.value || !paperId) {
    manualDecisionImageError.value = '该结果缺少试卷标识，无法加载批阅图'
    return
  }
  manualDecisionImageLoading.value = true
  try {
    const decisions = manualDecisionDrafts.value
      .filter((item) => item.paper_id === paperId && item.editable && item.result && item.result !== item.model_result)
      .map((item) => ({ question_no: item.question_no, result: item.result }))
    const imageUrl = decisions.length
      ? await api.grade.reviewImage(batchId.value, paperId, decisions)
      : await api.grade.reviewImage(batchId.value, paperId)
    if (requestId !== manualDecisionImageRequestId) {
      URL.revokeObjectURL(imageUrl)
      return
    }
    manualDecisionImageUrl.value = imageUrl
  } catch (err) {
    if (requestId === manualDecisionImageRequestId) {
      manualDecisionImageError.value = reviewErrorMessage(err, '批阅图加载失败，请重试')
    }
  } finally {
    if (requestId === manualDecisionImageRequestId) manualDecisionImageLoading.value = false
  }
}

function changeManualDecisionPage(delta) {
  jumpToManualDecisionPage(manualDecisionPageIndex.value + delta)
}

function jumpToManualDecisionPage(index, refresh = false) {
  const target = Math.min(manualDecisionPages.value.length - 1, Math.max(0, Number(index) || 0))
  if (!refresh && target === manualDecisionPageIndex.value && manualDecisionImageUrl.value) return
  manualDecisionPageIndex.value = target
  void loadManualDecisionImage()
}

function reviewResultText(result) {
  return ({ correct: '正确', partial: '部分正确', wrong: '错误', needs_review: '待复核' })[result] || '未判定'
}

function manualDecisionQuestionType(item) {
  const type = String(item?.question_type || '').trim().toLowerCase()
  return ['choice', 'fill', 'solve'].includes(type) ? type : 'solve'
}

function manualDecisionIsCompact(item) {
  return ['choice', 'fill'].includes(manualDecisionQuestionType(item))
}

function manualDecisionTypeLabel(item) {
  return ({ choice: '选择题', fill: '填空题', solve: '简答题' })[manualDecisionQuestionType(item)]
}

function openManualDecisionImage() {
  if (manualDecisionImageUrl.value) {
    window.open(manualDecisionImageUrl.value, '_blank', 'noopener,noreferrer')
  }
}

async function saveManualDecision() {
  if (!manualDecisionRow.value || !manualDecisionComplete.value) {
    ElMessage.warning('请为每一道题选择最终判定')
    return
  }
  manualDecisionSaving.value = true
  try {
    const paperIds = [...new Set(manualDecisionDrafts.value
      .filter((item) => item.editable)
      .map((item) => item.paper_id))]
    for (const paperId of paperIds) {
      const res = await api.grade.reviewDecision({
        batch_id: batchId.value,
        paper_id: paperId,
        review_revision: reviewRevision.value,
        decisions: manualDecisionDrafts.value
          .filter((item) => item.editable && item.paper_id === paperId)
          .map((item) => ({
            question_no: item.question_no,
            result: item.result,
            note: item.note,
          })),
      })
      if (!res?.ok || !res.review) throw new Error(res?.detail || '保存人工判定失败')
      applyReviewToProgressRow(res.review)
      reviewRevision.value = Number(res.review_revision || reviewRevision.value)
    }
    manualDecisionDialogVisible.value = false
    startBatchReportPolling()
    ElMessage.success(anonymous.value ? '已保存本页人工判定' : '已保存该学生的人工判定')
  } catch (err) {
    notifyProcessingError(err, reviewErrorMessage(err, '保存人工判定失败；若结果已更新，请刷新后重试'))
  } finally {
    manualDecisionSaving.value = false
  }
}

async function voidOneReview(row) {
  const paperId = reviewPaperId(row)
  if (!paperId) { ElMessage.error('该结果缺少试卷标识，无法作废'); return }
  const targetLabel = anonymous.value ? (row.student_name || '该页面') : (row.student_name || '该学生')
  const archiveHint = anonymous.value ? '该结果会从本批统计中排除。' : '该结果不会进入学生档案。'
  if (!window.confirm(`确定作废“${targetLabel}”的本次批改结果吗？${archiveHint}`)) return
  reviewBusy.value = true
  try {
    const res = await api.grade.reviewVoid({ batch_id: batchId.value, paper_id: paperId, reason: '老师在结果页作废' })
    if (!res?.ok) throw new Error(res?.detail || '作废失败')
    row.disposition = 'voided'
    reviewRevision.value = Number(res.review_revision || reviewRevision.value)
    reviewArchived.value = !hasActiveReviewRows.value
    startBatchReportPolling()
    maybeForgetTerminalReview()
    ElMessage.success(anonymous.value ? '已排除该页面结果' : '已作废该学生结果，不会写入学生档案')
  } catch (err) {
    notifyProcessingError(err, reviewErrorMessage(err, '作废失败'))
  } finally {
    reviewBusy.value = false
  }
}

async function voidAllReviews() {
  if (!window.confirm(anonymous.value
    ? '确定排除本批次所有未排除结果吗？排除后不会计入本批统计。'
    : '确定作废本批次所有未作废结果吗？作废后它们不会进入学生档案。')) return
  reviewBusy.value = true
  try {
    const res = await api.grade.reviewVoidAll({ batch_id: batchId.value })
    if (!res?.ok) throw new Error(res?.detail || '全部作废失败')
    progressList.value.forEach((row) => {
      if (row.disposition === 'active') row.disposition = 'voided'
    })
    reviewRevision.value = Number(res.review_revision || reviewRevision.value)
    reviewArchived.value = !hasActiveReviewRows.value
    startBatchReportPolling()
    maybeForgetTerminalReview()
    ElMessage.success(anonymous.value
      ? `已排除 ${res.voided || 0} 页结果`
      : `已作废 ${res.voided || 0} 份结果`)
  } catch (err) {
    notifyProcessingError(err, reviewErrorMessage(err, '全部作废失败'))
  } finally {
    reviewBusy.value = false
  }
}

async function archiveAllReviews() {
  if (!window.confirm('确认归档所有已完成判定的结果吗？待复核学生会留在本页，不会写入学生档案。')) return
  reviewBusy.value = true
  try {
    const res = await api.grade.reviewArchiveAll({ batch_id: batchId.value })
    if (!res?.ok) throw new Error(res?.detail || '入档失败')
    const archivedIds = new Set(res.archived_paper_ids || [])
    progressList.value.forEach((row) => {
      if (archivedIds.has(reviewPaperId(row))) row.disposition = 'archived'
    })
    reviewRevision.value = Number(res.review_revision || reviewRevision.value)
    reviewArchived.value = !hasActiveReviewRows.value
    maybeForgetTerminalReview()
    ElMessage.success(`已入档 ${res.archived || 0} 人；待复核 ${res.skipped_pending || 0} 人；已作废 ${res.skipped_voided || 0} 人`)
  } catch (err) {
    notifyProcessingError(err, reviewErrorMessage(err, '全部入档失败'))
  } finally {
    reviewBusy.value = false
  }
}

async function downloadStudentErrors(row) {
  const paperId = reviewPaperId(row)
  if (!paperId) { ElMessage.error('该结果缺少试卷标识，无法导出'); return }
  reviewBusy.value = true
  try {
    await api.grade.reviewDownload('/batch-grade/review/export/student', {
      batch_id: batchId.value, paper_id: paperId,
    }, `${row.student_name || '学生'}_错题条.pdf`)
  } catch (err) {
    notifyProcessingError(err, reviewErrorMessage(err, '错题条导出失败'))
  } finally {
    reviewBusy.value = false
  }
}

async function downloadReviewImage(row) {
  const paperId = reviewPaperId(row)
  if (!paperId) { ElMessage.error('该结果缺少试卷标识，无法下载批阅痕迹图'); return }
  reviewBusy.value = true
  let imageUrl = ''
  try {
    imageUrl = await api.grade.reviewImage(batchId.value, paperId)
    const link = document.createElement('a')
    link.href = imageUrl
    link.download = `${row?.student_name || '学生'}_批阅痕迹图.png`
    document.body.appendChild(link)
    link.click()
    link.remove()
  } catch (err) {
    notifyProcessingError(err, reviewErrorMessage(err, '批阅痕迹图下载失败'))
  } finally {
    if (imageUrl) window.setTimeout(() => URL.revokeObjectURL(imageUrl), 1000)
    reviewBusy.value = false
  }
}

function downloadCurrentReviewImage() {
  if (!manualDecisionImageUrl.value || manualDecisionImageLoading.value) return
  const link = document.createElement('a')
  link.href = manualDecisionImageUrl.value
  link.download = `${anonymous.value ? '匿名卷' : (manualDecisionRow.value?.student_name || '学生')}_第${manualDecisionPageIndex.value + 1}页_批阅痕迹.png`
  document.body.appendChild(link)
  link.click()
  link.remove()
}

async function downloadAllReviewImages() {
  reviewImagesDownloading.value = true
  try {
    await api.grade.reviewDownload('/batch-grade/review/export/images', {
      batch_id: batchId.value,
    }, '批阅痕迹图.zip', 600000)
  } catch (err) {
    notifyProcessingError(err, reviewErrorMessage(err, '批阅痕迹图打包失败'))
  } finally {
    reviewImagesDownloading.value = false
  }
}

async function downloadAllErrors() {
  reviewBusy.value = true
  try {
    await api.grade.reviewDownload('/batch-grade/review/export/all-errors', {
      batch_id: batchId.value,
    }, '全体学生错题条.zip')
  } catch (err) {
    notifyProcessingError(err, reviewErrorMessage(err, '全部错题导出失败'))
  } finally {
    reviewBusy.value = false
  }
}

async function downloadReviewSummary() {
  reviewBusy.value = true
  try {
    await api.grade.reviewDownload('/batch-grade/review/export/summary', {
      batch_id: batchId.value,
    }, '老师错题汇总.pdf')
  } catch (err) {
    notifyProcessingError(err, reviewErrorMessage(err, '老师汇总导出失败'))
  } finally {
    reviewBusy.value = false
  }
}

// Leaving now would abort an in-flight request (guard below); the usage tour
// reads this so it never triggers the leave-confirm on the teacher's behalf.
const leaveInterruptsWork = computed(() => recognizing.value || grading.value || recognizingTemplate.value || confirmingTemplate.value || templateCutBusy.value)
watch(leaveInterruptsWork, (busy) => setRouteBusy('upload', busy), { immediate: true })

onBeforeRouteLeave((to, from, next) => {
  if (leaveInterruptsWork.value) {
    if (confirm('任务正在处理中，离开会中断当前请求。确定离开吗？')) {
      abortActiveRequests()
      next()
    } else next(false)
  } else next()
})

function onBeforeUnload(event) {
  if (!leaveInterruptsWork.value) return
  event.preventDefault()
  event.returnValue = ''
}

function abortActiveRequests() {
  clearRecognitionAutoResume()
  templateRecognizeController?.abort()
  templateConfirmController?.abort()
  templateCutController?.abort()
  stopTemplateCutPolling()
  templateCutRunToken += 1
  templateCutBusy.value = false
  recognizeController?.abort()
  gradeController?.abort()
}

async function refreshAvailability() {
  if (availabilityCheckPending) return
  availabilityCheckPending = true
  try {
    const [stateResult, templateStateResult] = await Promise.allSettled([
      api.grade.availability(),
      api.grade.templateAvailability(),
    ])
    if (stateResult.status !== 'fulfilled') {
      markServiceFault('无法获取批改链路状态，请检查网络或联系管理员。')
      templateServiceState.value = {
        status: 'fault',
        label: '模板链路故障',
        message: TEMPLATE_SERVICE_UNAVAILABLE_MESSAGE,
        blocksNewWork: true,
      }
      registrationHardwareState.value = requiredHardwareState(null)
      return
    }
    const state = stateResult.value
    const templateState = templateStateResult.status === 'fulfilled'
      ? templateStateResult.value
      : null
    const labels = {
      available: '可用',
      queued: '需排队',
      unavailable: '暂不可用',
      fault: '链路故障',
    }
    if (!labels[state?.status]) {
      markServiceFault('批改链路状态响应异常，请稍后重试或联系管理员。')
      templateServiceState.value = {
        status: 'fault',
        label: '模板链路故障',
        message: '批改链路状态响应异常，请稍后重试或联系管理员。',
        blocksNewWork: true,
      }
      registrationHardwareState.value = requiredHardwareState(null)
      return
    }
    if (
      ['available', 'queued'].includes(state.status)
      && serviceState.value.status === 'fault'
      && Date.now() < serviceFaultHoldUntil
    ) {
      // Keep the recent transport fault visible while still refreshing hardware gates below.
    } else if (state.status === 'fault') {
      markServiceFault(state.message || '批改链路发生故障，请稍后重试。')
    } else {
      serviceState.value = {
        status: state.status,
        label: String(state.label || labels[state.status]),
        message: String(state.message || ''),
      }
    }
    if (!labels[templateState?.status]) {
      templateServiceState.value = {
        status: 'fault',
        label: '模板链路故障',
        message: TEMPLATE_SERVICE_UNAVAILABLE_MESSAGE,
        blocksNewWork: true,
      }
      registrationHardwareState.value = requiredHardwareState(null)
    } else {
      const registrationHardware = requiredHardwareState(templateState.registration_hardware)
      registrationHardwareState.value = registrationHardware
      const templateModelStatus = labels[templateState.service_status]
        ? templateState.service_status
        : templateState.status
      const templateModelBlocks = ['checking', 'unavailable', 'fault'].includes(templateModelStatus)
      // Template correction and column montage run on the server CPU. Only the
      // actual model/core service state can block template import; the student
      // registration accelerator is handled separately below.
      templateServiceState.value = serviceBlocksNewWork.value
        ? {
            status: serviceState.value.status,
            label: serviceState.value.label,
            message: serviceState.value.message,
            blocksNewWork: true,
          }
        : {
            status: templateModelStatus,
            label: String(templateState.label || labels[templateModelStatus]),
            message: String(templateState.message || ''),
            blocksNewWork: templateModelBlocks,
          }
    }
  } catch {
    markServiceFault('无法获取批改链路状态，请检查网络或联系管理员。')
    templateServiceState.value = {
      status: 'fault',
      label: '模板链路故障',
      message: TEMPLATE_SERVICE_UNAVAILABLE_MESSAGE,
      blocksNewWork: true,
    }
    registrationHardwareState.value = requiredHardwareState(null)
  } finally {
    availabilityCheckPending = false
  }
}

watch(
  () => [
    confirmedTemplateId.value,
    fileList.value.length,
    imagePreprocessing.value,
    imageExtracting.value,
    recognizing.value,
    grading.value,
    auth.currentClassId,
    form.category,
    form.exam_name,
    templateDialogVisible.value,
    recognitionBlocksNewWork.value,
    anonymous.value,
  ],
  maybeAutoStartRecognition,
)

watch(() => form.category, (category) => {
  if (category !== 'exam') form.exam_name = ''
})

onMounted(async () => {
  window.addEventListener('beforeunload', onBeforeUnload)
  await Promise.all([refreshAvailability(), loadDailyUsage()])
  availabilityPollTimer = window.setInterval(refreshAvailability, 15000)
  const resumedTemplate = await resumeTemplateRecognitionCheckpoint()
  if (!resumedTemplate) {
    const resumedReview = await resumeReviewBatch()
    if (!resumedReview) await resumeOrientationBatch()
  }
})
onBeforeUnmount(() => {
  setRouteBusy('upload', false)
  window.removeEventListener('beforeunload', onBeforeUnload)
  if (availabilityPollTimer) window.clearInterval(availabilityPollTimer)
  stopGradingClock()
  stopBatchReportPolling()
  revokeIdentityImageUrls()
  resetManualDecisionImage()
  resetOrientationReview()
  revokeTemplateCutPreviews()
  abortActiveRequests()
})
</script>

<style scoped>
/* ═══ Page ═══ */
.upload-page {
  width: 100%;
  max-width: var(--page-max);
  margin: 0 auto;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--sp-5);
  min-height: 0;
}

/* ═══ Studio masthead: serif step title, a ruled ledger of figures, and the step track ═══ */
.grade-hero {
  display: flex;
  flex-direction: column;
  gap: 30px;
  padding: 34px 38px 28px;
  animation: ma-fade-in var(--dur-4) var(--ease-out) backwards;
}


.grade-hero__top {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px 44px;
  flex-wrap: wrap;
}

.grade-hero__copy {
  min-width: 0;
  max-width: 660px;
}

.grade-hero__metrics {
  padding: 4px 0 4px 30px;
  border-left: 1px solid var(--rule);
}

.workflow-steps {
  position: relative;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0;
  margin: 0;
  padding: 24px 0 0;
  border-top: 1px solid var(--rule);
  list-style: none;
}

.workflow-steps::before {
  content: '';
  position: absolute;
  top: 38px;
  left: 14px;
  right: calc(25% - 14px);
  height: 1px;
  background: var(--rule);
}

.workflow-step {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
  padding-right: 16px;
  color: var(--ink-muted);
}

/* The completed stretch of the track is drawn in moss. */
.workflow-step::after {
  content: '';
  position: absolute;
  z-index: -1;
  top: 14px;
  left: 14px;
  right: -14px;
  height: 2px;
  background: var(--moss);
  transform: scaleX(0);
  transform-origin: left center;
  transition: transform var(--dur-4) var(--ease-emphasized);
}

.workflow-step.is-done:not(:last-child)::after {
  transform: scaleX(1);
}

.workflow-step__index {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  border: 1px solid var(--rule);
  border-radius: 50%;
  background: var(--paper-raised);
  color: var(--ink-muted);
  font-family: var(--font-serif);
  font-size: var(--fs-md);
  transition: background-color var(--dur-3) var(--ease-out), border-color var(--dur-3) var(--ease-out), color var(--dur-3) var(--ease-out);
}

.workflow-step__text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  line-height: 1.35;
}

.workflow-step__text strong {
  overflow: hidden;
  font-size: var(--fs-md);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.workflow-step__text small {
  overflow: hidden;
  color: var(--ink-faint);
  font-size: var(--fs-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.workflow-step.is-current {
  color: var(--ink);
}

.workflow-step.is-current .workflow-step__index {
  border-color: var(--moss);
  background: var(--moss);
  color: var(--on-ink);
  outline: 1px solid var(--moss);
  outline-offset: 3px;
}

.workflow-step.is-done {
  color: var(--ink-soft);
}

.workflow-step.is-done .workflow-step__index {
  border-color: var(--moss);
  background: var(--moss-soft);
  color: var(--moss-deep);
}

.monthly-quota {
  display: grid;
  grid-template-columns: auto auto;
  align-items: end;
  gap: 6px 18px;
  min-width: 240px;
  padding: 4px 0 4px 30px;
  color: var(--ink-muted);
  border-left: 1px solid var(--rule);
  font-size: var(--fs-xs);
  white-space: nowrap;
}

.monthly-quota strong {
  grid-column: 1 / -1;
  grid-row: 2;
  color: var(--ink-muted);
  font-size: var(--fs-sm);
  font-weight: 500;
}

.monthly-quota .quota-now {
  margin-right: 4px;
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 44px;
  font-weight: 400;
  line-height: 1;
}

.quota-meter {
  grid-column: 1 / -1;
  grid-row: 3;
  position: relative;
  width: 100%;
  height: 4px;
  overflow: hidden;
  border-radius: 2px;
  background: var(--moss-soft);
}

.quota-meter > span {
  position: absolute;
  inset: 0 auto 0 0;
  border-radius: 2px;
  background: var(--moss);
  transition: width var(--dur-4) var(--ease-out);
}

/* ═══ Setup cards read as numbered steps ═══ */
.template-card > .card-title::before,
.grade-card > .card-title::before {
  content: '01';
  margin-right: 6px;
  color: var(--moss);
  font-family: var(--font-serif);
  font-size: 32px;
  font-weight: 400;
  line-height: 1;
}

.grade-card[data-step] > .card-title::before {
  content: '02';
}

.grade-card[data-step='03'] > .card-title::before {
  content: '03';
}

.template-card > .card-title > svg,
.grade-card > .card-title > svg {
  display: none;
}

/* ═══ Review board: one row per paper, outcome at a glance ═══ */
.review-board {
  margin: 4px 0 var(--sp-3);
  font-variant-numeric: tabular-nums;
}

.review-board__head,
.review-row {
  display: grid;
  grid-template-columns: 40px minmax(140px, 1fr) minmax(160px, 1.3fr) 104px 300px;
  align-items: center;
  gap: 16px;
  padding: 0 16px;
}

.review-board__head {
  height: 36px;
  color: var(--ink-faint);
  font-size: var(--fs-xs);
  font-weight: 600;
  letter-spacing: 0.08em;
}

.review-board__body {
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--rule);
}

.review-row {
  position: relative;
  min-height: 70px;
  padding-top: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--line);
  background: transparent;
  transition: background-color var(--dur-2) var(--ease-out), opacity var(--dur-3) var(--ease-out);
}

.review-row::before {
  content: '';
  position: absolute;
  inset: 14px auto 14px 0;
  width: 2px;
  background: var(--moss);
  opacity: 0;
  transition: opacity var(--dur-2) var(--ease-out);
}

.review-row:hover {
  background: var(--paper);
}

.review-row:hover::before {
  opacity: 1;
}

.review-row.is-attention {
  background: color-mix(in srgb, var(--warning) 6%, var(--paper-raised));
}

.review-row.is-attention::before {
  background: var(--warning);
  opacity: 1;
}

.review-row.is-voided {
  opacity: 0.5;
}

.review-row__index {
  color: var(--moss);
  font-family: var(--font-serif);
  font-size: var(--fs-xl);
  font-variant-numeric: lining-nums;
}

.review-row__who {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.review-avatar {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--moss-soft);
  color: var(--moss-deep);
  font-family: var(--font-serif);
  font-size: var(--fs-lg);
  font-weight: 600;
}

.review-row__name {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  min-width: 0;
}

.review-row__name strong {
  overflow: hidden;
  color: var(--ink);
  font-size: var(--fs-md);
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.review-row__name .link-btn {
  margin: 0 0 0 -6px;
}

.review-row__outcome {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.review-row__outcome small {
  color: var(--ink-muted);
  font-size: var(--fs-xs);
}

.outcome-mini {
  display: flex;
  gap: 2px;
  height: 8px;
  overflow: hidden;
  border-radius: 4px;
  background: var(--fill);
}

.outcome-mini i {
  flex-basis: 0;
  min-width: 3px;
  transform-origin: left center;
  animation: ma-meter-grow var(--dur-4) var(--ease-emphasized) backwards;
}

/* The showcase's outcome steps: wrong carries a hatch, pending a stripe, so color is never alone. */
.outcome-mini .is-correct { background: var(--chart-correct); }
.outcome-mini .is-partial { background: var(--chart-partial); }
.outcome-mini .is-wrong {
  background: repeating-linear-gradient(45deg, transparent 0 3px, rgba(255, 253, 247, 0.45) 3px 4.6px), var(--chart-wrong);
}
.outcome-mini .is-pending {
  background: repeating-linear-gradient(135deg, var(--chart-partial) 0 2px, color-mix(in srgb, var(--chart-partial) 25%, var(--paper-raised)) 2px 5px);
}

.review-score {
  color: var(--ink);
  font-family: var(--font-serif);
  font-size: 22px;
  font-weight: 400;
  line-height: 1;
  white-space: nowrap;
  font-variant-numeric: lining-nums tabular-nums;
}

.review-score small {
  margin-left: 2px;
  color: var(--ink-muted);
  font-family: var(--font-body);
  font-size: var(--fs-xs);
  font-weight: 500;
}

.review-score.is-low {
  color: var(--chart-wrong);
}

.review-row__actions {
  display: flex;
  flex-wrap: nowrap;
  justify-content: flex-end;
  gap: 4px;
}

.review-row__actions .link-btn {
  margin: 0;
  padding: 5px 9px;
  border-radius: 6px;
  color: var(--ink-soft);
  background: transparent;
  text-decoration: underline;
  text-decoration-color: var(--rule);
  text-underline-offset: 4px;
}

.review-row__actions .link-btn:hover:not(:disabled) {
  background: var(--fill);
  color: var(--ink);
  text-decoration-color: currentColor;
}

.review-row__actions .danger-link {
  color: var(--danger);
}

.review-row__actions .danger-link:hover:not(:disabled) {
  background: var(--danger-soft);
  color: var(--danger);
}

.review-row__actions .link-btn + .link-btn {
  margin-left: 0;
}

/* The review action bar stays reachable while scrolling a long batch. */
.progress-card .review-actions {
  position: sticky;
  bottom: -48px;
  z-index: 3;
  margin: var(--sp-3) -22px -20px;
  padding: 14px 22px;
  border-top: 1px solid var(--rule);
  border-radius: 0 0 var(--radius-lg) var(--radius-lg);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 16px;
  background: var(--paper-raised);
}

.progress-card .review-actions .summary-bar {
  flex: 1 1 360px;
  padding: 0;
}

.progress-card .review-actions .review-toolbar {
  margin-top: 0;
}

@media (max-width: 1100px) {
  .review-board__head {
    display: none;
  }

  .review-row {
    grid-template-columns: 36px minmax(0, 1fr) auto;
    grid-template-areas: 'index who metric' 'index outcome outcome' 'index actions actions';
  }

  .review-row__index { grid-area: index; align-self: start; }
  .review-row__who { grid-area: who; }
  .review-row__metric { grid-area: metric; }
  .review-row__outcome { grid-area: outcome; }
  .review-row__actions { grid-area: actions; justify-content: flex-start; }

  .workflow-steps {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    row-gap: 18px;
  }

  .workflow-steps::before,
  .workflow-step::after {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .outcome-mini i {
    animation: none;
  }
}

/* ═══ Columns ═══ */
.columns { display: grid; grid-template-columns: minmax(300px, 360px) minmax(0, 1fr); gap: var(--sp-5); align-items: stretch; }

/* ═══ Card base ═══ */
.card {
  min-width: 0; background: var(--card); border: 1px solid var(--line); border-radius: var(--radius-lg);
  padding: 20px 22px; box-shadow: var(--shadow-sm);
}
.card-title {
  font-family: var(--font-title); font-size: var(--fs-lg); font-weight: var(--fw-semibold); color: var(--ink);
  margin-bottom: var(--sp-4); display: flex; align-items: center; flex-wrap: wrap; gap: var(--sp-2); letter-spacing: -0.01em;
}
.batch-form .form-row:last-child { margin-bottom: 0; }
.card-title svg { box-sizing: border-box; width: 28px; height: 28px; flex: 0 0 auto; padding: 6px; border-radius: var(--radius-sm); background: var(--primary-soft); color: var(--primary); }
.card-title-sub { margin-left: auto; font-size: var(--fs-xs); color: var(--ink-muted); font-family: var(--font-body); font-weight: var(--fw-medium); }
.service-state { display: inline-flex; align-items: center; gap: 5px; margin-left: var(--sp-2); padding: 3px 9px; border-radius: 999px; font-family: var(--font-body); font-size: var(--fs-2xs); font-weight: var(--fw-bold); }
.service-state::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.service-state-checking { color: var(--ink-muted); background: var(--fill); }
.service-state-available { color: color-mix(in srgb, var(--success) 82%, var(--ink)); background: var(--success-soft); }
.service-state-queued { color: var(--warning); background: var(--warning-soft); }
.service-state-unavailable { color: var(--ink-muted); background: var(--surface-subtle); }
.service-state-fault { color: var(--danger); background: var(--danger-soft); }
.service-alert {
  display: flex; align-items: flex-start; gap: var(--sp-2); margin: calc(-1 * var(--sp-2)) 0 var(--sp-4);
  padding: 10px 12px; border: 1px solid var(--line); border-left: 3px solid var(--ink-faint); border-radius: var(--radius-md); background: var(--surface-subtle);
  color: var(--ink-soft); font-size: var(--fs-xs); line-height: 1.5;
}
.service-alert-global { margin: 0; }
.template-service-alert { margin: var(--sp-3) 0 0; }
.service-alert-queued { border-left-color: var(--warning); background: var(--warning-soft); color: var(--warning); }
.service-alert strong { flex: 0 0 auto; color: inherit; }
.service-alert-fault, .service-alert-unavailable { border-left-color: var(--danger); background: var(--danger-soft); color: var(--danger); }

/* ═══ Template card ═══ */
.template-upload {
  border: 1.5px dashed color-mix(in srgb, var(--primary) 30%, var(--border)); border-radius: var(--radius-lg); padding: var(--sp-5) var(--sp-4);
  text-align: center; cursor: pointer; background: var(--card-2);
  transition: border-color var(--dur-2) var(--ease-out), background-color var(--dur-2) var(--ease-out), transform var(--dur-1) var(--ease-out);
}
.template-upload:hover { border-color: var(--primary); background: var(--primary-soft); }
.template-upload:not(.is-disabled):active { transform: scale(0.99); }
.template-upload.is-disabled { cursor: not-allowed; opacity: 0.55; background: var(--surface-subtle); }
.template-upload.is-disabled:hover { border-color: var(--border); background: var(--surface-subtle); }
.template-upload .t-icon { font-size: 26px; margin-bottom: var(--sp-1); color: var(--primary); }
.template-upload .t-label { font-size: var(--fs-sm); color: var(--primary-dark); font-weight: var(--fw-semibold); }
.template-warn { margin-top: var(--sp-3); font-size: var(--fs-xs); color: var(--warning); font-weight: var(--fw-medium); background: var(--warning-soft); padding: 8px var(--sp-3); border-radius: var(--radius-sm); line-height: 1.55; }
.template-hint { margin-top: var(--sp-2); font-size: var(--fs-xs); color: var(--ink-muted); line-height: 1.6; }
.batch-setup { margin-bottom: var(--sp-4); padding: 14px; border: 1px solid var(--line); border-radius: var(--radius-md); background: var(--card-2); }
.batch-setup-title { margin-bottom: var(--sp-2); color: var(--ink); font-size: var(--fs-sm); font-weight: var(--fw-semibold); }
.batch-setup-fields { display: grid; gap: var(--sp-2); }
.batch-setup-fields label > span { display: block; margin-bottom: 4px; color: var(--ink-muted); font-size: var(--fs-xs); font-weight: var(--fw-semibold); }
.link-btn { background: none; border: none; color: var(--primary-dark); font-size: var(--fs-xs); font-weight: var(--fw-semibold); cursor: pointer; padding: 3px 6px; margin: -3px -2px; border-radius: var(--radius-xs); font-family: inherit; transition: background-color var(--dur-2) var(--ease-out), color var(--dur-2) var(--ease-out); }
.link-btn:hover:not(:disabled) { background: var(--primary-soft); }
.link-btn:disabled { color: var(--ink-faint); cursor: not-allowed; }

/* ═══ 多页一次性模板 ═══ */
:global(.template-editor-dialog) { margin: 3vh auto !important; padding: 12px 16px; max-height: 94vh; display: flex; flex-direction: column; }
:global(.template-editor-dialog .el-dialog__header) { flex: 0 0 auto; padding-bottom: 8px; }
:global(.template-editor-dialog .el-dialog__body) { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 0 2px; }
:global(.template-editor-dialog .el-dialog__footer) { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: flex-end; flex: 0 0 auto; padding-top: 8px; }
.tpl-editor-content { min-width: 0; margin: 0; padding: 0; border: 0; }
.tpl-save-note { margin-right: auto; color: var(--ink-muted); font-size: 12px; }
.tpl-page-tabs { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; }
.tpl-page-tabs .form-select { width: auto; min-width: 90px; max-width: 150px; padding: 4px 22px 4px 6px; font-size: 12px; }
.tpl-editor-help { display: inline; margin-left: 10px; }
.tpl-editor-help > summary { display: inline; cursor: pointer; color: var(--primary); text-decoration: underline dotted; }
.tpl-editor-help p { margin: 4px 0 0; }
.tpl-edit-context, .tpl-add-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; min-width: 0; }
.tpl-edit-context { margin: 0 0 6px; color: var(--ink-muted); font-size: 12px; }
.tpl-edit-context label { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.tpl-edit-context .form-select { width: auto; max-width: 210px; min-width: 0; padding: 4px 24px 4px 7px; font-size: 12px; }
.tpl-edit-context .tpl-reference-selector { margin-left: auto; }
.tpl-drawing-kind-tools { display: inline-flex; align-items: center; gap: 3px; padding: 2px 3px; border: 1px solid var(--border); border-radius: 7px; background: var(--surface-subtle); }
.tpl-tool-caption { padding: 0 2px; color: var(--ink-muted); font-size: 12px; white-space: nowrap; }
.tpl-pointer-hint { flex: 1 1 210px; min-width: 0; line-height: 1.4; }
.tpl-add-tools > span { font-size: 12px; white-space: nowrap; }
.tpl-tool-btn { border: 1px solid var(--border); background: var(--card); color: var(--ink-soft); border-radius: 6px; padding: 5px 8px; font-size: 12px; line-height: 1.5; white-space: nowrap; cursor: pointer; }
.tpl-tool-btn[aria-pressed="true"] { color: var(--primary-dark); background: var(--primary-soft); border-color: var(--primary); box-shadow: inset 0 0 0 1px var(--primary); }
.tpl-tool-btn:disabled { opacity: .45; cursor: default; }
.tpl-delete-selected { color: var(--danger); margin-left: 4px; }
.tpl-question-management { margin-left: auto; position: relative; }
.tpl-question-management summary { color: var(--ink-muted); cursor: pointer; white-space: nowrap; }
.tpl-question-management > div { display: flex; flex-wrap: wrap; gap: 6px; padding: 10px; width: min(520px, 82vw); max-width: 100%; }
.tpl-question-management[open] { flex: 1 0 100%; border: 1px solid var(--border); border-radius: 6px; padding: 6px; }
.tpl-question-management p { flex-basis: 100%; }
.tpl-question-management .btn { padding: 5px 8px; font-size: 12px; }
.tpl-question-management .form-select { width: auto; min-width: 80px; }
.tpl-draw-surface { position: absolute; z-index: 15; inset: 0; cursor: crosshair; touch-action: none; }
.tpl-draw-draft { position: absolute; border: 2px dashed var(--primary); background: rgba(16, 127, 104, .14); pointer-events: none; box-sizing: border-box; }
.tpl-reference-geometry .is-selected { outline: 3px solid #f59e0b; outline-offset: 2px; }
.tpl-reference-geometry .is-selected > span { box-shadow: 0 0 0 2px #f59e0b; }
@media (max-width: 640px) {
  .review-row__actions {
    white-space: normal;
    flex-wrap: wrap;
  }

  .review-row__actions .el-button + .el-button {
    margin-left: 0;
  }

  :global(.template-editor-dialog) { width: calc(100vw - 12px) !important; margin: 1vh auto !important; max-height: 98vh; padding: 10px; }
  :global(.template-editor-dialog .el-dialog__footer) { display: grid; grid-template-columns: auto auto minmax(0, 1fr); gap: 5px; padding-top: 5px; }
  :global(.template-editor-dialog .el-dialog__footer .btn) { min-width: 0; padding: 6px 7px; font-size: 12px; white-space: nowrap; }
  .tpl-page-tabs { grid-column: 1 / -1; justify-content: center; }
  .tpl-editor-content .tpl-cut-panel { grid-template-columns: minmax(0, 1fr) auto; }
  .tpl-editor-content .tpl-cut-panel > div:first-child { grid-column: 1 / -1; }
  .tpl-editor-content .tpl-cut-panel > .btn { padding: 6px 8px; font-size: 12px; }
  .tpl-page-counts { display: none; }
  .tpl-edit-context .form-select { max-width: 150px; }
  .tpl-save-note { display: none; }
}
.tpl-ans-mode { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-2); margin-bottom: var(--sp-3); }
.tpl-ans-opt { display: flex; align-items: center; gap: 6px; padding: 7px var(--sp-3); border: 1.5px solid var(--border); border-radius: var(--radius-md); font-size: 12px; cursor: pointer; transition: all 0.15s; }
.tpl-ans-opt.active { border-color: var(--primary); background: var(--primary-soft); color: var(--primary-dark); font-weight: 600; }
.tpl-ans-opt input { accent-color: var(--primary); }
.tpl-column-mode { display: grid; grid-template-columns: auto minmax(120px, 180px) auto minmax(0, 1fr); align-items: center; gap: var(--sp-2); margin-bottom: var(--sp-3); }
.tpl-column-mode-label { color: var(--ink-muted); font-size: 12px; font-weight: 600; }
.tpl-column-mode .form-select { min-width: 0; }
.tpl-column-mode-hint { margin: 0; }
.tpl-confirmed { margin-top: var(--sp-2); padding: var(--sp-3); background: var(--primary-soft); border-radius: var(--radius-md); }
.tpl-confirmed-head { font-size: 13px; font-weight: 600; color: var(--primary-dark); margin-bottom: 4px; }
.tpl-confirm-hint { font-size: 12px; color: var(--ink-muted); line-height: 1.5; margin-bottom: 6px; padding: 5px 8px; background: var(--surface-subtle); border-radius: var(--radius-sm); }
.tpl-layout-review-alert { grid-column: 1 / -1; margin: 0 0 var(--sp-3); padding: var(--sp-2) var(--sp-3); border: 1px solid rgba(154, 106, 25, 0.36); border-left: 3px solid var(--warning); border-radius: var(--radius-sm); background: var(--warning-soft); color: var(--warning); font-size: 12px; line-height: 1.5; }
.tpl-answer-review-row { display: flex; align-items: center; gap: 12px; }
.tpl-answer-review-row strong { flex: 1; font-weight: 500; }
.tpl-answer-review-row button { flex-shrink: 0; }
.tpl-slot-row.is-missing-answer, .tpl-fill-slot.is-missing-answer { background: var(--warning-soft); color: var(--warning); }
.tpl-slot-row.is-selected { outline: 2px solid var(--primary); }
.tpl-handwriting-region.is-missing-answer, .tpl-drawing-region.is-missing-answer { border-color: var(--warning); background: color-mix(in srgb, var(--warning) 12%, transparent); }
.tpl-cut-panel { display: grid; grid-template-columns: minmax(0, 1fr) 150px auto; align-items: center; gap: 6px 8px; margin-bottom: 10px; padding: 6px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-subtle); }
.tpl-cut-panel strong { display: block; margin-bottom: 3px; color: var(--ink); font-size: 13px; }
.tpl-cut-panel p { margin: 0; color: var(--ink-muted); font-size: 11px; line-height: 1.55; }
.tpl-cut-image-type { min-width: 0; }
.tpl-cut-status { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 6px 14px; padding-top: var(--sp-2); border-top: 1px solid var(--border); color: var(--ink-muted); font-size: 11px; }
.tpl-cut-status .is-warning { color: var(--warning); font-weight: 600; }
.tpl-cut-preview-error { grid-column: 1 / -1; color: var(--danger); font-size: 12px; }
.tpl-recognize-state { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-2); color: var(--primary-dark); font-size: 12px; line-height: 1.5; }
.tpl-cut-previews { grid-column: 1 / -1; display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; }
.tpl-cut-preview-card { min-width: 0; padding: var(--sp-2); border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--card); }
.tpl-reference-caption { margin: 6px 0 2px; font-size: 12px; font-weight: 600; color: var(--ink); }
.tpl-cut-preview-card header { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; margin-bottom: 6px; color: var(--ink-muted); font-size: 11px; }
.tpl-cut-preview-card header strong { margin: 0; color: var(--ink); }
.tpl-cut-preview-stage { position: relative; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-sm); background: #fff; }
.tpl-cut-preview-stage img { display: block; width: 100%; height: auto; }
/* 纸面画布与题目裁剪图按视高适配（一屏看全一整张试卷/一道长题）。
   !important：与子组件 scoped 规则同特异度，顺序不保证，必须压过 width:100% 旧规则。
   画布 box 收缩到图片 box，SVG 叠加层 inset:0 仍与图片逐像素对齐，几何坐标不变。 */
:global(.drawing-reference-canvas.tpl-cut-preview-stage) { width: fit-content !important; max-width: 100%; margin: 0 auto; }
:global(.drawing-reference-canvas.tpl-cut-preview-stage img) { display: block; width: auto !important; max-width: 100%; height: auto !important; max-height: 72vh !important; }
:global(.tpl-cut-preview-stage.tpl-crop-preview) { width: fit-content !important; max-width: 100%; margin: 0 auto; }
:global(.tpl-cut-preview-stage.tpl-crop-preview img) { display: block; width: auto !important; max-width: 100%; height: auto !important; max-height: 72vh !important; }
@media (max-width: 900px) {
  :global(.drawing-reference-canvas.tpl-cut-preview-stage img),
  :global(.tpl-cut-preview-stage.tpl-crop-preview img) { max-height: 58vh !important; }
}
.tpl-reference-geometry { position: absolute; inset: 0; pointer-events: none; mix-blend-mode: multiply; }
.tpl-reference-geometry .is-selected { outline: 2px solid #b86100; outline-offset: 2px; }
.tpl-reference-geometry .tpl-column-divider,
.tpl-reference-geometry .tpl-question-anchor,
.tpl-reference-geometry .tpl-handwriting-region,
.tpl-reference-geometry .tpl-drawing-region,
.tpl-reference-geometry .tpl-drawing-label,
.tpl-reference-geometry .tpl-draw-surface { pointer-events: auto; }
.tpl-reference-geometry.is-disabled { pointer-events: none; }
.tpl-reference-geometry.is-disabled * { pointer-events: none !important; }
.drawing-reference-summary { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; color: var(--ink-muted); font-size: 12px; }
.tpl-column-count { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
.tpl-column-count .form-select { min-width: 74px; padding: 5px 22px 5px 7px; font-size: 11px; }
.tpl-column-overlay { position: absolute; z-index: 1; box-sizing: border-box; border: 1px solid rgba(20, 104, 181, 0.58); background: rgba(20, 104, 181, 0.035); pointer-events: none; }
.tpl-column-overlay.is-column-2 { border-color: rgba(20, 128, 88, 0.58); background: rgba(20, 128, 88, 0.035); }
.tpl-column-overlay.is-column-3 { border-color: rgba(151, 89, 18, 0.58); background: rgba(151, 89, 18, 0.035); }
.tpl-column-overlay span { position: absolute; top: 3px; left: 4px; padding: 1px 4px; border-radius: 3px; background: rgba(255,255,255,0.9); color: #1d4f7d; font-size: 9px; font-weight: 700; }
.tpl-column-divider { position: absolute; z-index: 4; top: 0; bottom: 0; width: 14px; margin-left: -7px; padding: 0; border: 0; border-left: 3px solid rgba(20, 104, 181, 0.9); background: transparent; cursor: ew-resize; touch-action: none; }
.tpl-column-divider:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.tpl-handwriting-region { position: absolute; z-index: 6; box-sizing: border-box; min-width: 8px; min-height: 8px; border: 1px solid #1468b5; background: transparent; cursor: move; touch-action: none; }

.tpl-handwriting-region.is-label-anchor { min-width: 8px; min-height: 8px; border: 1px dashed #1468b5; background: none; }

.tpl-handwriting-resize { position: absolute; right: -3px; bottom: -3px; width: 13px; height: 13px; border-right: 3px solid #1468b5; border-bottom: 3px solid #1468b5; cursor: nwse-resize; touch-action: none; }
.tpl-question-anchor { position: absolute; z-index: 7; box-sizing: border-box; min-width: 8px; min-height: 8px; border: 2px solid #b42318; background: rgba(180, 35, 24, 0.1); cursor: move; touch-action: none; }

.tpl-question-anchor .tpl-handwriting-resize { border-color: var(--danger); }
.tpl-drawing-region { position: absolute; z-index: 7; box-sizing: border-box; border: 2px dashed #16845b; background: rgba(22, 132, 91, 0.04); cursor: move; touch-action: none; }

.tpl-drawing-region .tpl-handwriting-resize { border-color: #16845b; }
.tpl-drawing-label { position: absolute; z-index: 9; transform: translate(-50%, -50%); width: 12px; height: 12px; margin: 0; padding: 0; border: 1px dashed #1468b5; background: transparent; cursor: move; touch-action: none; }
.tpl-drawing-note { margin: 6px 0; }
.tpl-slot-order { padding: 10px; border: 1px solid var(--border); }
.tpl-slot-order p { font-size: 12px; color: var(--muted); }
.tpl-slot-row { display: grid; grid-template-columns: minmax(70px, auto) 1fr auto; gap: 8px; align-items: start; padding: 5px 0; }
.tpl-slot-row span { white-space: pre-wrap; overflow-wrap: anywhere; }
.tpl-slot-row button { min-height: 32px; cursor: pointer; }
.tpl-q-identity { width: auto; max-width: 42%; flex-shrink: 0; white-space: normal; overflow-wrap: anywhere; }
.tpl-template-handwriting { position: absolute; z-index: 8; box-sizing: border-box; min-width: 8px; min-height: 8px; border: 2px dashed var(--primary); background: color-mix(in srgb, var(--primary) 10%, transparent); cursor: move; touch-action: none; }
.tpl-template-handwriting span { position: absolute; left: -2px; bottom: 100%; padding: 1px 4px; border-radius: 3px 3px 0 0; background: var(--primary); color: var(--on-primary); font-size: 9px; font-weight: 700; white-space: nowrap; }
.tpl-template-handwriting .tpl-handwriting-resize { border-color: var(--primary); }
.tpl-handwriting-toolbar { display: flex; flex-wrap: wrap; align-items: end; gap: 8px 12px; margin: var(--sp-2) 0; padding: 8px; border-left: 3px solid #1468b5; background: color-mix(in srgb, var(--primary-soft) 42%, var(--card)); color: var(--ink-muted); font-size: 11px; line-height: 1.45; }
.tpl-handwriting-toolbar label { display: inline-flex; align-items: center; gap: 6px; }
.tpl-handwriting-toolbar label span { white-space: nowrap; }
.tpl-handwriting-toolbar .form-select { min-width: 112px; padding: 5px 22px 5px 7px; font-size: 11px; }
.tpl-handwriting-toolbar > .form-select { width: auto; max-width: 100%; }
.tpl-handwriting-toolbar > span { align-self: center; }
.tpl-handwriting-list { display: grid; gap: 4px; margin-top: var(--sp-2); }
.tpl-handwriting-list-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 4px 7px; border: 1px solid var(--border); border-radius: var(--radius-xs); color: var(--ink-muted); font-size: 10px; }
.danger-link { color: var(--danger); }
.tpl-question-samples { display: flex; flex-wrap: wrap; gap: 4px; margin-top: var(--sp-2); }
.tpl-question-samples::before { content: '已定位题号'; margin-right: 3px; color: var(--ink-muted); font-size: 10px; line-height: 20px; }
.tpl-question-samples span { display: inline-grid; min-width: 24px; height: 20px; place-items: center; padding: 0 5px; border: 1px solid rgba(207, 48, 47, 0.35); border-radius: 3px; background: var(--danger-soft); color: var(--danger); font-size: 10px; font-weight: 700; }
.tpl-cut-page-badge { margin-left: var(--sp-2); padding: 2px 7px; border-radius: 999px; background: var(--primary-soft); color: var(--primary-dark); font-size: 10px; font-weight: 600; }
.tpl-answer-section { margin-bottom: var(--sp-3); padding: 0 0 var(--sp-3); border-bottom: 1px solid var(--border); }
.tpl-answer-section-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--sp-3); margin-bottom: var(--sp-2); }
.tpl-answer-section-head strong { display: block; color: var(--ink); font-size: 13px; }
.tpl-answer-section-head span { display: block; margin-top: 2px; color: var(--ink-muted); font-size: 11px; }
.tpl-answer-section-head span em { color: var(--danger); font-style: normal; font-weight: 700; }
.tpl-answer-actions { display: inline-flex; flex-wrap: wrap; justify-content: flex-end; gap: var(--sp-2); white-space: nowrap; }
.tpl-batch-score { display: grid; grid-template-columns: auto 92px auto minmax(130px, 1fr); align-items: center; gap: var(--sp-2); margin: var(--sp-2) 0; padding: var(--sp-2) var(--sp-3); border-left: 3px solid var(--primary); border-radius: var(--radius-sm); background: var(--primary-soft); }
.tpl-batch-score .form-label { margin: 0; white-space: nowrap; }
.tpl-batch-score .form-input { padding: 6px 8px; }
.tpl-batch-apply { justify-self: start; padding: 6px var(--sp-3); white-space: nowrap; }
.tpl-custom-score-note { margin: var(--sp-2) 0; color: var(--ink-muted); font-size: 11px; line-height: 1.5; }
.tpl-answer-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: var(--sp-2); }
.tpl-answer-cell { display: grid; gap: 6px; min-width: 0; min-height: 74px; padding: 8px var(--sp-2); border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-subtle); color: var(--ink); transition: border-color 0.15s, background 0.15s; }
.tpl-answer-cell:hover { border-color: var(--primary); background: var(--primary-soft); }
.tpl-answer-cell.is-unresolved { border-color: rgba(190, 74, 74, 0.55); background: var(--danger-soft); }
.tpl-answer-cell-head { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: var(--sp-2); }
.tpl-answer-card-focus { min-width: 0; padding: 2px 0; border: 0; background: transparent; color: inherit; cursor: pointer; text-align: left; }
.tpl-answer-card-focus:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; border-radius: var(--radius-xs); }
.tpl-answer-card-focus span { display: block; color: var(--ink-muted); font-size: 10px; font-weight: 600; }
.tpl-answer-card-focus span em { margin-left: 3px; color: var(--primary-dark); font-size: 10px; font-style: normal; }
.tpl-choice-mode-badge { flex: 0 0 auto; padding: 2px 6px; border-radius: var(--radius-xs); background: var(--primary-soft); color: var(--primary-dark); font-size: 10px; font-weight: 700; white-space: nowrap; }
.tpl-choice-mode-badge.is-pending { background: var(--danger-soft); color: var(--danger); }
.tpl-q-mode-badge { align-self: center; }
.correction-discovery-badge { flex: 0 0 auto; padding: 2px 6px; border-radius: var(--radius-xs); background: var(--success-soft); color: var(--primary-dark); font-size: 10px; font-weight: 700; white-space: nowrap; }
.correction-discovery-badge.is-excluded { background: var(--surface-subtle); color: var(--ink-muted); }
.correction-review-panel { display: grid; gap: var(--sp-2); border-left-color: var(--primary); background: var(--primary-soft); }
.correction-review-panel.is-excluded { border-left-color: var(--border); background: var(--surface-subtle); }
.correction-review-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--sp-3); }
.correction-review-head .form-label { margin: 0; }
.correction-review-head .field-note { margin: 3px 0 0; }
.correction-decision-btn { flex: 0 0 auto; padding: 5px var(--sp-2); }
.correction-current-question { display: grid; grid-template-columns: 70px minmax(0, 1fr); gap: var(--sp-2); align-items: start; padding: var(--sp-2) 0; border-top: 1px solid var(--border); border-bottom: 1px solid var(--border); }
.correction-current-question span, .correction-history-row p span { color: var(--ink-muted); font-size: 11px; font-weight: 700; }
.correction-current-question strong { min-width: 0; color: var(--ink); font-size: 13px; line-height: 1.55; }
.correction-history-list { display: grid; gap: var(--sp-2); }
.correction-history-row { display: grid; gap: 5px; padding: var(--sp-2) 0; border-bottom: 1px dashed var(--border); }
.correction-history-row:last-child { border-bottom: 0; }
.correction-history-meta { display: flex; flex-wrap: wrap; gap: 5px var(--sp-2); align-items: center; color: var(--ink-muted); font-size: 11px; }
.correction-history-meta strong { color: var(--ink); font-size: 12px; }
.correction-history-row p { display: grid; grid-template-columns: 70px minmax(0, 1fr); gap: var(--sp-2); margin: 0; color: var(--ink-soft); font-size: 12px; line-height: 1.55; }
.correction-review-panel.is-excluded .correction-history-list { opacity: 0.55; }
.correction-excluded-note { margin: 0; color: var(--danger); font-size: 11px; font-weight: 600; }
.tpl-choice-options { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 4px; }
.tpl-choice-option, .tpl-choice-mode-button { min-width: 0; min-height: 28px; padding: 3px 6px; border: 1px solid var(--border); border-radius: var(--radius-xs); background: var(--card); color: var(--ink-soft); font-size: 12px; font-weight: 700; cursor: pointer; }
.tpl-choice-option:hover, .tpl-choice-mode-button:hover { border-color: var(--primary); color: var(--primary-dark); }
.tpl-choice-option.is-selected { border-color: var(--primary); background: var(--primary); color: var(--on-primary); }
.tpl-choice-multiple-editor { display: flex; align-items: center; gap: 6px; color: var(--ink-muted); font-size: 11px; font-weight: 700; }
.tpl-choice-answer-input { min-width: 0; flex: 1; padding: 5px 7px; text-align: center; text-transform: uppercase; }
.tpl-choice-mode-picker { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px; }
.tpl-choice-mode-button { color: var(--primary-dark); }
.tpl-answer-status { color: var(--danger); font-size: 10px; font-weight: 700; }
.tpl-fill-answer-cell { align-content: start; }
.tpl-solve-answer-cell { align-content: start; grid-column: span 2; }
.tpl-solve-answer-preview { color: var(--ink-soft); font-size: 11px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
.tpl-fill-answer-input { min-width: 0; padding: 5px 7px; }
.tpl-fill-slot-list { display: flex; flex-wrap: wrap; gap: 5px; }
.tpl-fill-slot { display: flex; min-width: 0; flex: 1 1 72px; align-items: center; gap: 4px; color: var(--ink-muted); font-size: 10px; font-weight: 700; }
.tpl-fill-slot .tpl-fill-answer-input { width: 100%; }
.tpl-page { margin-bottom: var(--sp-4); }
.tpl-page-title { font-size: 13px; font-weight: 600; color: var(--ink); margin-bottom: var(--sp-2); padding-bottom: 4px; border-bottom: 1px solid var(--border); }
.tpl-q { margin-bottom: var(--sp-3); padding: var(--sp-3); scroll-margin-top: var(--sp-4); background: var(--surface-subtle); border-radius: var(--radius-md); transition: background 0.15s; }
.tpl-q.is-collapsed { padding-top: var(--sp-2); padding-bottom: var(--sp-2); }
.tpl-q-head { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); }
.tpl-q-toggle { display: flex; min-width: 0; flex: 1; align-items: center; gap: var(--sp-2); padding: 0; border: 0; background: transparent; color: inherit; cursor: pointer; text-align: left; }
.tpl-q-toggle:focus-visible { outline: 2px solid var(--primary); outline-offset: 3px; border-radius: var(--radius-xs); }
.tpl-q-answer-preview { min-width: 0; overflow: hidden; color: var(--ink-soft); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.tpl-q-head-actions { display: inline-flex; align-items: center; gap: var(--sp-2); }
.tpl-q-type { width: auto; padding: 4px var(--sp-2); font-size: 12px; }
.tpl-q-details { display: grid; grid-template-columns: 1fr; gap: var(--sp-2); margin-top: var(--sp-2); padding-top: var(--sp-2); border-top: 1px dashed var(--border); }
.tpl-q .ci-field { margin-bottom: 0; }
.tpl-q-pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-2); }
.tpl-uniform-score-note p { margin: 0; color: var(--primary-dark); font-size: 12px; }
.field-note { font-size: 11px; font-weight: 400; color: var(--ink-muted); }
.scoring-panel { padding: var(--sp-2); border-left: 3px solid var(--primary); border-radius: var(--radius-sm); background: var(--primary-soft); }
.subscore-grid { display: flex; flex-wrap: wrap; align-items: center; gap: var(--sp-2); }
.subscore-item { display: grid; grid-template-columns: auto 90px 20px; align-items: center; gap: 4px; color: var(--ink-soft); font-size: 12px; }
.subscore-item.is-inactive-subscore { opacity: 0.68; }
.subscore-status { display: block; color: var(--warning); font-size: 10px; font-style: normal; line-height: 1.25; white-space: nowrap; }
.subscore-item .form-input { padding: 6px 8px; }
.fill-score-grid { margin-top: var(--sp-2); }
.fill-score-summary { color: var(--primary-dark); font-size: 12px; font-weight: 600; }
.subscore-remove { width: 20px; height: 20px; padding: 0; border: 0; border-radius: 50%; background: rgba(190, 74, 74, 0.12); color: var(--danger); cursor: pointer; }

/* ═══ 身份确认闸门 ═══ */
.identity-card { border: 1px solid var(--border); border-radius: var(--radius-md); padding: var(--sp-3); margin-bottom: var(--sp-3); background: var(--surface-subtle); }
.identity-card { display: grid; grid-template-columns: minmax(190px, 42%) 1fr; gap: var(--sp-3); align-items: start; }
.identity-paper-preview { display: grid; gap: 6px; justify-items: center; }
.identity-paper-preview img { width: 100%; max-height: 260px; object-fit: contain; image-orientation: from-image; border: 1px solid var(--border); border-radius: var(--radius-md); background: #fff; cursor: zoom-in; }
.identity-image-loading { display: grid; width: 100%; min-height: 180px; place-items: center; border: 1px dashed var(--border); border-radius: var(--radius-md); color: var(--ink-muted); font-size: 12px; }
.identity-controls { min-width: 0; }
.identity-head { display: flex; align-items: baseline; gap: var(--sp-3); margin-bottom: var(--sp-2); color: var(--ink); font-size: 13px; }
.identity-head span { color: var(--ink-muted); font-size: 12px; }
.identity-head { flex-direction: column; align-items: stretch; gap: 4px; overflow-wrap: anywhere; }
.identity-mode { display: flex; flex-wrap: wrap; gap: var(--sp-3); color: var(--ink-soft); font-size: 12px; margin: var(--sp-2) 0; }
.identity-mode label { display: inline-flex; align-items: center; gap: 5px; cursor: pointer; }
.identity-mode input { accent-color: var(--primary); }
.identity-create-fields { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-2); }
.identity-options-error { color: var(--danger); }
.dialog-safety-note { float: left; color: var(--ink-muted); font-size: 12px; line-height: 36px; }

/* ═══ Form ═══ */
.form-row { margin-bottom: var(--sp-4); }
.form-label { display: block; font-size: 12px; font-weight: 600; color: var(--ink-muted); margin-bottom: 6px; }
.form-select, .form-input, .form-textarea {
  width: 100%; min-height: 36px; padding: 7px var(--sp-3); border: 1px solid var(--border); border-radius: var(--field-radius);
  font-size: var(--fs-sm); font-family: var(--font-body); color: var(--ink); background: var(--card); outline: none;
  transition: border-color var(--dur-2) var(--ease-out), box-shadow var(--dur-2) var(--ease-out);
}
.form-select:hover:not(:disabled), .form-input:hover:not(:disabled), .form-textarea:hover:not(:disabled) { border-color: color-mix(in srgb, var(--ink) 24%, transparent); }
.form-select:disabled, .form-input:disabled, .form-textarea:disabled { background: var(--surface-subtle); color: var(--ink-muted); cursor: not-allowed; }
.form-textarea { resize: vertical; line-height: 1.5; }
.form-textarea.tpl-answer-autogrow { min-height: calc(3em + 20px); }
@supports (field-sizing: content) {
  .form-textarea.tpl-answer-autogrow { field-sizing: content; overflow-y: hidden; resize: none; max-height: none; }
}
.form-select:focus, .form-input:focus, .form-textarea:focus { border-color: var(--primary); box-shadow: var(--focus-ring); }
.form-hint { margin-top: 6px; font-size: var(--fs-xs); color: var(--ink-muted); line-height: 1.5; }
.number-input { display: inline-flex; align-items: center; border: 1px solid var(--border); border-radius: var(--radius-md); overflow: hidden; }
.number-input button { width: 34px; height: 34px; border: none; background: var(--surface-subtle); color: var(--ink); font-size: 18px; cursor: pointer; }
.number-input button:hover { background: var(--primary-soft); color: var(--primary); }
.number-input .value { width: 48px; text-align: center; font-size: 14px; font-weight: 600; }

/* ═══ Mode cards ═══ */
.mode-cards { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }
.mode-card { border: 1.5px solid var(--border); border-radius: var(--radius-md); padding: var(--sp-3) var(--sp-4); cursor: pointer; transition: all 0.15s; background: var(--card); }
.mode-card:hover { border-color: var(--primary); }
.mode-card.active { border-color: var(--primary); background: var(--primary-soft); box-shadow: var(--focus-ring); }
.mode-card .mode-title { font-size: 14px; font-weight: 600; color: var(--ink); }
.mode-card .mode-desc { font-size: 12px; color: var(--ink-soft); margin-top: 2px; }
.mode-card .mode-sub { font-size: 11px; color: var(--ink-muted); margin-top: 1px; }

/* ═══ Upload area ═══ */
.upload-area { border: 1.5px dashed color-mix(in srgb, var(--primary) 30%, var(--border)); border-radius: var(--radius-lg); padding: 28px var(--sp-4); text-align: center; cursor: pointer; background: var(--card-2); transition: border-color var(--dur-2) var(--ease-out), background-color var(--dur-2) var(--ease-out), box-shadow var(--dur-2) var(--ease-out); }
.upload-area:hover, .upload-area.is-dragover { border-color: var(--primary); background: var(--primary-soft); }
.upload-area.is-dragover { box-shadow: var(--focus-ring); }
.upload-area.is-disabled { cursor: not-allowed; opacity: 0.55; background: var(--surface-subtle); }
.upload-area.is-disabled:hover { border-color: var(--border); background: var(--surface-subtle); }
/* Busy is not disabled: the area stays legible while its sheet loader works through the files. */
.template-upload.is-disabled[aria-busy='true'], .upload-area.is-disabled[aria-busy='true'] { opacity: 1; cursor: progress; }
.upload-area .u-icon { font-size: 30px; color: var(--primary); transition: transform var(--dur-3) var(--ease-emphasized); }
.upload-area.is-dragover .u-icon { transform: translateY(-3px); }
.template-upload .t-icon.is-busy, .upload-area .u-icon.is-busy { margin: 0 auto var(--sp-2); }
.upload-area .u-label { font-size: var(--fs-md); font-weight: var(--fw-semibold); color: var(--ink); margin-top: var(--sp-1); }
.upload-area .u-sub { font-size: var(--fs-xs); color: var(--ink-muted); margin-top: 2px; }
.upload-area .upload-buttons { display: flex; gap: var(--sp-2); justify-content: center; margin-top: var(--sp-3); }
.upload-area .upload-buttons .btn { padding: 8px var(--sp-4); font-size: 13px; }
.upload-limit { margin-top: var(--sp-2); font-size: var(--fs-xs); color: var(--ink-muted); text-align: left; line-height: 1.55; }

/* ═══ Name guide ═══ */
.name-guide { margin-top: var(--sp-3); border: 1px solid var(--line); border-radius: var(--radius-md); overflow: hidden; }
.name-guide-header { display: flex; align-items: center; justify-content: space-between; padding: 10px 14px; background: var(--card-2); cursor: pointer; font-size: var(--fs-sm); font-weight: var(--fw-semibold); color: var(--ink-soft); transition: background-color var(--dur-2) var(--ease-out); }
.name-guide-header:hover { background: var(--fill-2); }
.toggle-icon { font-size: 11px; color: var(--ink-muted); }
.name-guide-content { padding: 14px; background: var(--card); border-top: 1px solid var(--line); animation: ma-rise-in var(--dur-3) var(--ease-emphasized) backwards; }
.name-guide-diagram { display: flex; gap: 16px; align-items: flex-start; }
.paper-preview { position: relative; width: 160px; height: 200px; border: 2px solid #333; border-radius: 4px; background: #fff; flex-shrink: 0; }
.paper-preview .zone { position: absolute; display: flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 600; border: 1.5px dashed; }
.paper-preview .zone-top { top: 0; left: 0; right: 0; height: 25%; background: rgba(76,175,80,0.15); border-color: #4CAF50; color: var(--primary-dark); }
.paper-preview .zone-left { top: 25%; left: 0; bottom: 0; width: 15%; background: rgba(76,175,80,0.15); border-color: #4CAF50; color: var(--primary-dark); }
.paper-preview .zone-rest { top: 25%; left: 15%; right: 0; bottom: 0; background: rgba(244,67,54,0.08); border-color: #f44336; color: var(--danger); text-align: center; line-height: 1.35; }
.name-guide-rules { display: flex; flex-direction: column; gap: 6px; flex: 1; }
.rule { font-size: 12px; padding: 4px 0; }
.rule.good { color: var(--primary-dark); }
.rule.bad { color: var(--danger); }
.name-guide-tip { margin-top: 12px; padding: 10px; background: var(--primary-soft); border-radius: var(--radius-sm); font-size: 12px; color: var(--primary-dark); line-height: 1.5; }

.file-summary { margin-top: var(--sp-2); font-size: 13px; color: var(--ink); display: flex; align-items: center; gap: var(--sp-2); }
.identity-inherit-option { margin-top: var(--sp-3); display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; font-size: 13px; color: var(--ink); }
.identity-inherit-option label { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
.identity-inherit-note { display: block; margin-top: 2px; font-size: var(--fs-xs); color: var(--ink-muted); }

/* ═══ Actions ═══ */
.actions { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-4); justify-content: flex-end; flex-wrap: wrap; }
.actions .form-label { margin: 0 2px 0 0; }
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 36px; padding: 0 var(--sp-4);
  border-radius: var(--btn-radius); font-size: var(--fs-sm); font-weight: var(--fw-semibold); font-family: var(--font-body); cursor: pointer; border: 1px solid transparent;
  transition: transform var(--dur-1) var(--ease-out), box-shadow var(--dur-2) var(--ease-out), background-color var(--dur-2) var(--ease-out), border-color var(--dur-2) var(--ease-out), color var(--dur-2) var(--ease-out);
}
.btn:active:not(:disabled) { transform: scale(0.97); }
.btn-ghost { background: var(--card); border-color: var(--border); color: var(--ink-soft); }
.btn-ghost:hover:not(:disabled) { border-color: color-mix(in srgb, var(--primary) 45%, var(--border)); background: color-mix(in srgb, var(--primary) 5%, var(--card)); color: var(--primary-dark); }
.btn-primary { background: var(--ink-solid); color: var(--on-ink); }
.btn-primary:hover:not(:disabled) { background: color-mix(in srgb, var(--ink-solid) 84%, var(--paper)); }
.btn:disabled { opacity: 0.45; cursor: not-allowed; box-shadow: none; }

/* ═══ Question fields ═══ */
.ci-no { font-size: 13px; font-weight: 600; color: var(--ink); }
.ci-field .form-label { margin-bottom: 3px; }

/* ═══ Result table ═══ */
.result-table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: var(--fs-sm); margin-bottom: var(--sp-3); font-variant-numeric: tabular-nums; }
.result-table th { text-align: left; font-size: var(--fs-xs); font-weight: var(--fw-semibold); letter-spacing: 0.08em; color: var(--ink-faint); padding: 10px 12px; background: transparent; border-bottom: 1px solid var(--rule); white-space: nowrap; }
.result-table td { height: 46px; padding: 8px 12px; border-bottom: 1px solid var(--line); color: var(--ink-soft); transition: background-color var(--dur-2) var(--ease-out); }
.result-table tbody tr { animation: ma-fade-in var(--dur-3) var(--ease-out) backwards; }
.result-table tbody tr:hover td { background: color-mix(in srgb, var(--primary) 4%, var(--card)); }
.result-table .result-index { width: 44px; color: var(--ink-faint); }
.result-table .result-name { color: var(--ink); font-weight: var(--fw-semibold); }
.tag-ok, .tag-fail, .tag-pending { display: inline-flex; align-items: center; padding: 2px 9px; border-radius: 999px; font-weight: var(--fw-semibold); font-size: var(--fs-xs); white-space: nowrap; }
.tag-ok { color: color-mix(in srgb, var(--success) 80%, var(--ink)); background: var(--success-soft); }
.tag-fail { color: var(--danger); background: var(--danger-soft); }
.tag-pending { color: var(--warning); background: var(--warning-soft); }
.tag-muted { display: inline-flex; align-items: center; padding: 2px 9px; border-radius: 999px; background: var(--fill); color: var(--ink-muted); font-size: var(--fs-xs); font-weight: var(--fw-semibold); }
.grading-clock { font-variant-numeric: tabular-nums; }
.orientation-review-hint { margin-bottom: var(--sp-3); padding: var(--sp-2) var(--sp-3); border-left: 3px solid var(--warning); border-radius: var(--radius-sm); background: var(--warning-soft); color: var(--ink-soft); font-size: var(--fs-xs); line-height: 1.6; }
.orientation-preview-stage { display: flex; min-height: 420px; height: min(58vh, 620px); align-items: center; justify-content: center; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-subtle); }
.orientation-preview-stage img { display: block; width: auto; height: auto; max-width: min(100%, 58vh); max-height: min(100%, 58vh); object-fit: contain; image-orientation: from-image; background: #fff; box-shadow: var(--shadow-md); transform-origin: center; transition: transform var(--dur-4) var(--ease-emphasized); }
.orientation-preview-state { display: flex; align-items: center; justify-content: center; gap: var(--sp-2); color: var(--ink-muted); font-size: 12px; }
.orientation-preview-state.is-error { color: var(--danger); }
.orientation-controls { display: flex; align-items: center; justify-content: center; gap: var(--sp-3); margin: var(--sp-3) 0; }
.orientation-controls span { min-width: 92px; color: var(--ink-soft); font-size: 12px; font-weight: 600; text-align: center; }
:global(.manual-review-dialog) { width: min(1920px, calc(100vw - 16px)) !important; height: min(calc(100vh - 12px), 1120px); max-height: calc(100vh - 12px); display: flex; flex-direction: column; margin: 0 auto; padding: 0 !important; overflow: hidden; }
:global(.manual-review-dialog .el-dialog__header) { flex: 0 0 auto; padding: 8px 14px 4px; }
:global(.manual-review-dialog .el-dialog__footer) { flex: 0 0 auto; padding: 4px 14px 6px; }
:global(.manual-review-dialog .el-dialog__body) { flex: 1 1 0; min-height: 0; max-height: none; display: flex; flex-direction: column; overflow: hidden; padding: 0 8px 4px; }
.manual-review-hint { flex: 0 0 auto; overflow: hidden; font-size: var(--fs-2xs); color: var(--ink-muted); line-height: 1.3; padding: 4px 8px; background: var(--surface-subtle); border-radius: var(--radius-sm); margin-bottom: 6px; text-overflow: ellipsis; white-space: nowrap; }
.manual-review-workspace { flex: 1 1 auto; min-height: 0; display: grid; grid-template-columns: minmax(0, 2.25fr) minmax(360px, 0.85fr); gap: 10px; align-items: stretch; overflow: hidden; }
.manual-review-workspace.is-paper-hidden { grid-template-columns: minmax(0, 1fr); }
.manual-review-workspace.is-paper-hidden .manual-review-paper { display: none; }
.manual-review-paper { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; justify-items: center; gap: 3px; min-height: 0; height: 100%; padding: 0; border: 0; border-radius: 0; background: transparent; overflow: hidden; }
.manual-review-pager { width: 100%; display: flex; align-items: center; justify-content: space-between; gap: var(--sp-2); padding: 2px 2px 3px; border-bottom: 1px solid var(--border); }
.manual-review-pager .btn { min-height: 28px; padding: 2px 12px; font-size: var(--fs-xs); }
.manual-review-pager strong { color: var(--ink-soft); font-size: var(--fs-sm); font-variant-numeric: tabular-nums; }
.manual-review-image-stage { min-width: 0; min-height: 0; width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; overflow: hidden; padding: 0; background: var(--surface-subtle); }
.manual-review-paper img { display: block; flex: 0 1 auto; width: auto; height: auto; max-width: 100%; max-height: 100%; object-fit: contain; image-orientation: from-image; border: 1px solid var(--border); border-radius: 4px; background: #fff; box-shadow: 0 5px 16px rgba(48, 56, 46, 0.12); cursor: zoom-in; }
.manual-review-image-actions { display: flex; align-items: center; justify-content: center; gap: 12px; min-height: 22px; }
.manual-review-paper > .link-btn { min-height: 16px; padding: 0; font-size: 10px; line-height: 16px; }
.manual-review-paper-state { display: flex; width: 100%; height: 100%; min-height: 0; align-items: center; justify-content: center; gap: var(--sp-2); color: var(--ink-muted); font-size: 12px; text-align: center; }
.manual-review-paper-state.is-error { color: var(--danger); }
.manual-review-list { min-height: 0; height: 100%; display: flex; flex-direction: column; gap: 6px; overflow-y: auto; overscroll-behavior: contain; padding-right: 4px; }
.manual-review-list-toolbar { position: sticky; z-index: 2; top: 0; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 4px 2px 7px; border-bottom: 1px solid var(--border); background: var(--card); }
.manual-review-list-toolbar > div { min-width: 0; display: flex; align-items: baseline; gap: 8px; }
.manual-review-list-toolbar strong { color: var(--ink); font-size: var(--fs-md); }
.manual-review-list-toolbar span { color: var(--ink-muted); font-size: var(--fs-xs); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.manual-review-paper-toggle { flex: 0 0 auto; min-height: 30px; padding: 3px 12px; font-size: var(--fs-xs); }
.manual-review-item { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 8px; background: var(--card); cursor: pointer; }
.manual-review-item.is-current-page { border-color: rgba(72, 96, 64, 0.5); box-shadow: inset 3px 0 0 var(--primary); }
.manual-review-item.is-pending { border-color: rgba(154, 106, 25, 0.45); background: var(--warning-soft); }
.manual-review-head { display: flex; align-items: center; gap: 7px; margin-bottom: 5px; color: var(--ink); font-size: 13px; }
.manual-review-type { color: var(--ink-muted); font-size: 10px; font-weight: 600; }
.manual-review-evidence { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-2); margin-bottom: var(--sp-2); }
.manual-review-evidence > div { min-width: 0; padding: 6px 8px; background: var(--surface-subtle); border-radius: var(--radius-sm); }
.manual-review-evidence span, .manual-review-controls label > span { display: block; color: var(--ink-muted); font-size: 11px; font-weight: 600; margin-bottom: 3px; }
.manual-review-evidence p { margin: 0; color: var(--ink); font-size: 12px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
.manual-review-diagnostic { display: block; margin-top: 4px; color: var(--warning, #9a6a19); font-size: 10px; line-height: 1.35; }
.manual-review-controls { display: grid; grid-template-columns: minmax(0, 1fr); gap: var(--sp-2); }
.manual-review-controls label { min-width: 0; }
.manual-review-item.is-compact { display: grid; grid-template-columns: 62px minmax(0, 1fr) minmax(0, 1fr) 116px; align-items: center; column-gap: 8px; padding: 6px 8px; }
.manual-review-item.is-compact .manual-review-head { margin: 0; }
.manual-review-item.is-compact .manual-review-evidence { display: contents; }
.manual-review-item.is-compact .manual-review-evidence > div { display: flex; align-items: baseline; gap: 5px; padding: 0; background: transparent; }
.manual-review-item.is-compact .manual-review-evidence span { flex: 0 0 auto; margin: 0; font-size: 10px; }
.manual-review-item.is-compact .manual-review-evidence p { min-width: 0; font-size: 12px; line-height: 1.35; }
.manual-review-item.is-compact .manual-review-controls { display: block; }
.manual-review-item.is-compact .manual-review-controls label > span { display: none; }
.manual-review-item.is-compact .manual-review-controls .form-select { min-height: 28px; padding: 3px 7px; font-size: 11px; }
.manual-review-item.is-solve { padding: 10px; }
.manual-review-item.is-solve .manual-review-evidence > div { min-height: 58px; }
.manual-review-workspace.is-paper-hidden { display: grid; grid-template-columns: minmax(0, 1fr); align-content: start; flex: 0 1 auto; overflow: visible; }
.manual-review-workspace.is-paper-hidden .manual-review-list { display: block; height: auto; max-height: calc(98vh - 152px); overflow-y: auto; overflow-x: hidden; padding-right: 4px; }
.manual-review-sheet { display: flex; min-width: 0; flex-direction: column; gap: 8px; }
.manual-review-sheet-block { min-width: 0; overflow-x: auto; border: 1px solid var(--line); border-radius: var(--radius-md); background: var(--card); }
.manual-review-sheet-heading { display: flex; align-items: baseline; gap: 8px; padding: 8px 10px; border-bottom: 1px solid var(--line); background: var(--surface-subtle); }
.manual-review-sheet-heading strong { color: var(--ink); font-size: var(--fs-sm); }
.manual-review-sheet-heading span { color: var(--ink-muted); font-size: var(--fs-2xs); }
.manual-review-sheet-grid { display: grid; width: max-content; grid-template-columns: 64px repeat(var(--sheet-columns), minmax(52px, max-content)); min-width: 0; }
.manual-review-sheet-label, .manual-review-sheet-cell { min-height: 34px; border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); }
.manual-review-sheet-label { display: flex; align-items: center; justify-content: flex-start; padding: 4px 8px; color: var(--ink-muted); font-size: var(--fs-2xs); font-weight: var(--fw-bold); white-space: nowrap; background: var(--card-2); }
.manual-review-sheet-cell { display: flex; width: 100%; align-items: center; justify-content: center; padding: 4px 6px; color: var(--ink); font-size: var(--fs-xs); text-align: center; transition: background-color var(--dur-2) var(--ease-out); }
button.manual-review-sheet-cell { border-top: 0; border-left: 0; background: var(--card); cursor: pointer; font-family: inherit; }
button.manual-review-sheet-cell:hover { background: var(--surface-subtle); }
.manual-review-sheet-number { font-weight: 700; }
.manual-review-sheet-answer { max-width: 112px; overflow-wrap: anywhere; white-space: normal; }
.manual-review-sheet-answer span { display: block; max-width: 100%; overflow-wrap: anywhere; }
.manual-review-sheet-cell.is-current-page { background: var(--primary-soft); }
.manual-review-sheet-cell.is-pending { background: var(--warning-soft); color: var(--warning); }
.manual-review-sheet-verdict.is-verdict-correct, .manual-review-solve-verdict.is-verdict-correct { background: var(--success-soft); color: var(--success); }
.manual-review-sheet-verdict.is-verdict-partial, .manual-review-solve-verdict.is-verdict-partial { background: var(--warning-soft); color: var(--warning); }
.manual-review-sheet-verdict.is-verdict-wrong, .manual-review-solve-verdict.is-verdict-wrong { background: var(--danger-soft); color: var(--danger); }
.manual-review-sheet-verdict[class*='is-verdict-'] select, .manual-review-solve-verdict[class*='is-verdict-'] select { color: inherit; }
.manual-review-legend { display: flex; align-items: center; justify-content: center; gap: 8px; color: var(--ink-muted); font-size: var(--fs-2xs); white-space: nowrap; }
.manual-review-legend > span { display: inline-flex; align-items: center; gap: 2px; }
.manual-review-legend-symbol { display: inline-flex; width: 15px; height: 15px; align-items: center; justify-content: center; font-size: 14px; line-height: 1; }
.manual-review-legend-symbol.is-unread { color: var(--ink-muted); }
.manual-review-legend-symbol.is-correct { color: var(--success); }
.manual-review-legend-symbol.is-partial { color: var(--warning); }
.manual-review-legend-symbol.is-wrong { color: var(--danger); }
.manual-review-sheet-verdict { padding: 2px 4px; }
.manual-review-sheet-verdict select, .manual-review-solve-verdict select { width: 100%; min-height: 26px; padding: 1px 4px; border: 0; background: transparent; color: var(--ink); font-size: 16px; font-weight: 700; text-align: center; cursor: pointer; }
.manual-review-sheet-verdict select:disabled, .manual-review-solve-verdict select:disabled { cursor: not-allowed; opacity: .7; }
.manual-review-sheet-solve { display: flex; flex-direction: column; gap: 6px; }
.manual-review-solve-row { padding: 10px; border: 1px solid var(--line); border-left: 3px solid var(--primary); border-radius: var(--radius-md); background: var(--card); cursor: pointer; transition: box-shadow var(--dur-2) var(--ease-out), border-color var(--dur-2) var(--ease-out); }
.manual-review-solve-row:hover { box-shadow: var(--shadow-sm); }
.manual-review-solve-row.is-pending { border-left-color: var(--warning); background: var(--warning-soft); }
.manual-review-solve-row.is-current-page { box-shadow: inset 0 -2px 0 var(--primary); }
.manual-review-solve-head { display: flex; align-items: center; gap: 7px; margin-bottom: 6px; }
.manual-review-solve-number { padding: 0; border: 0; background: transparent; color: var(--ink); font-size: var(--fs-sm); font-weight: var(--fw-bold); cursor: pointer; font-family: inherit; }
.manual-review-solve-verdict { display: flex; min-width: 72px; align-items: center; gap: 4px; margin-left: auto; padding: 1px 4px 1px 8px; border-radius: var(--radius-sm); color: var(--ink-muted); font-size: var(--fs-2xs); white-space: nowrap; transition: background-color var(--dur-2) var(--ease-out), color var(--dur-2) var(--ease-out); }
.manual-review-solve-verdict.is-verdict-empty { background: var(--fill); }
.manual-review-solve-verdict select { min-width: 36px; }
.manual-review-solve-evidence { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
.manual-review-solve-evidence > div { min-width: 0; padding: 6px 8px; border-radius: var(--radius-sm); background: var(--surface-subtle); }
.manual-review-solve-evidence span { display: block; margin-bottom: 3px; color: var(--ink-muted); font-size: var(--fs-2xs); font-weight: var(--fw-bold); }
.manual-review-solve-evidence p { margin: 0; color: var(--ink); font-size: var(--fs-xs); line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
:global(.manual-review-dialog.is-paper-hidden) { height: auto !important; }
:global(.manual-review-dialog.is-paper-hidden .el-dialog__body) { flex: 0 1 auto; max-height: calc(98vh - 105px); overflow-y: auto; }
.summary-bar { display: flex; flex-wrap: wrap; gap: var(--sp-2) var(--sp-3); align-items: center; padding: var(--sp-2) 0; font-size: var(--fs-sm); color: var(--ink-soft); }
.summary-bar strong { color: var(--ink); font-family: var(--font-serif); font-size: var(--fs-2xl); font-weight: 500; font-variant-numeric: lining-nums tabular-nums; }
.review-actions { border-top: 1px solid var(--line); margin-top: var(--sp-3); padding-top: var(--sp-3); }
.review-toolbar { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: var(--sp-2); margin-top: var(--sp-2); }
.review-hint { color: var(--ink-muted); font-size: var(--fs-xs); margin-left: auto; }
.row-actions { white-space: nowrap; }
.row-actions .link-btn + .link-btn { margin-left: var(--sp-1); }
.danger-link { color: var(--danger); }
.danger-link:hover:not(:disabled) { color: var(--danger); text-decoration: underline; }
.is-voided { opacity: 0.55; background: var(--surface-subtle); }
/* While a batch awaits review, the results lead the page; setup cards follow. Visual order only. */
.service-alert-global { order: -3; }
.grade-hero { order: -2; }
.progress-card.is-review-focus { order: -1; }

/* ═══ Progress cards: the showcase specimen's chrome — mode pill, phase strip, page counter ═══ */
.mode-pill { padding: 2px 9px; border-radius: 999px; background: var(--fill); color: var(--ink-muted); font-family: var(--font-body); font-size: var(--fs-xs); font-weight: var(--fw-semibold); letter-spacing: 0; white-space: nowrap; }
.card-title-phases { margin-left: auto; }
.card-progress { margin-bottom: var(--sp-4); }

/* ═══ Batch report: the showcase's "本批洞察" dashboard ═══ */
.batch-report-panel { margin-top: var(--sp-5); padding: clamp(18px, 2.4vw, 28px); border: 1px solid var(--line); border-radius: var(--radius-xl); background: var(--paper-raised); animation: ma-rise-in var(--dur-4) var(--ease-emphasized) backwards; }
.batch-report-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px var(--sp-3); padding-bottom: 18px; border-bottom: 1px solid var(--line); }
.batch-report-title-wrap { display: flex; flex-direction: column; min-width: 0; }
.batch-report-eyebrow { color: var(--ink-muted); font-size: var(--fs-xs); }
.batch-report-title-wrap > strong { color: var(--ink); font-family: var(--font-serif); font-size: var(--fs-2xl); font-weight: 500; line-height: 1.35; letter-spacing: -0.005em; }
.batch-report-subtitle { margin-top: 2px; color: var(--ink-muted); font-size: var(--fs-xs); }
.batch-report-head-actions { display: flex; align-items: center; gap: 10px; }
.batch-report-overview { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.1fr); gap: 18px; margin-top: 18px; }
.batch-report-ring-card { display: flex; align-items: center; min-width: 0; padding: 20px; border: 1px solid var(--line); border-radius: var(--radius-lg); background: var(--paper); }
.batch-report-ring-card > .outcome-donut { flex: 1; min-width: 0; }
.batch-report-stats { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.batch-report-insight-grid { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 0.85fr); gap: 18px; margin-top: 18px; }
.batch-report-top-errors { display: grid; gap: 4px; margin: 0; padding: 0; list-style: none; }
.batch-report-top-errors li { display: grid; grid-template-columns: 38px minmax(0, 1fr) auto; align-items: start; gap: 12px; padding: 10px; border-radius: var(--radius-sm); transition: background-color var(--dur-2) var(--ease-out); }
.batch-report-top-errors li:hover { background: var(--fill-2); }
.batch-report-rank { color: var(--chart-wrong); font-family: var(--font-serif); font-size: 24px; line-height: 1.1; font-variant-numeric: lining-nums; }
.batch-report-error-copy { display: flex; flex-direction: column; min-width: 0; }
.batch-report-error-copy strong { color: var(--ink); font-size: var(--fs-md); }
.batch-report-error-copy small { color: var(--ink-muted); font-size: var(--fs-xs); }
.batch-report-error-track { margin-top: 8px; }
.batch-report-question { display: block; margin-top: 6px; overflow-wrap: anywhere; color: var(--ink-muted); font-size: var(--fs-xs); line-height: 1.5; }
.batch-report-error-count { color: var(--ink); font-family: var(--font-serif); font-size: 20px; font-weight: 500; line-height: 1.2; white-space: nowrap; font-variant-numeric: lining-nums tabular-nums; }
.batch-report-error-count small { margin-left: 2px; color: var(--ink-muted); font-family: var(--font-body); font-size: var(--fs-xs); font-weight: 500; }
.batch-report-distributions { display: grid; grid-template-columns: minmax(0, 1fr); margin: 0; }
.batch-report-distribution-block { min-width: 0; }
.batch-report-inline-list.batch-report-bar-list { display: grid; max-height: 360px; gap: 16px; margin: 0; padding: 0 4px 0 0; overflow: auto; list-style: none; }
.batch-report-bar-row { display: grid; gap: 4px; }
.batch-report-bar-label { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; color: var(--ink-soft); font-size: var(--fs-sm); line-height: 1.5; }
.batch-report-bar-label span { min-width: 0; overflow-wrap: anywhere; }
.batch-report-bar-label strong { flex: 0 0 auto; color: var(--ink); font-size: var(--fs-sm); white-space: nowrap; font-variant-numeric: tabular-nums; }
.batch-report-bar-row small { min-width: 0; overflow-wrap: anywhere; color: var(--ink-muted); font-size: var(--fs-xs); }
.batch-report-empty { padding: 20px 12px; border: 1px dashed var(--line); border-radius: var(--radius-md); color: var(--ink-muted); font-size: var(--fs-xs); text-align: center; }
.batch-report-details { margin: 18px 0 0; overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius-lg); background: var(--paper); }
.batch-report-details summary { padding: 12px 18px; color: var(--moss-deep); font-size: var(--fs-sm); font-weight: var(--fw-semibold); cursor: pointer; }
.batch-report-table-wrap { max-height: 360px; overflow: auto; border-top: 1px solid var(--line); }
.batch-report-table { width: 100%; min-width: 620px; border-collapse: collapse; color: var(--ink-soft); font-size: var(--fs-sm); }
.batch-report-table th { position: sticky; z-index: 1; top: 0; background: var(--paper-raised); color: var(--ink); font-family: var(--font-serif); font-size: var(--fs-md); font-weight: 500; text-align: left; }
.batch-report-table th, .batch-report-table td { padding: 10px 18px; border-bottom: 1px solid var(--line); vertical-align: top; }
.batch-report-table tr:last-child td { border-bottom: 0; }
.batch-report-table td strong { color: var(--ink); }
.batch-report-question-cell { min-width: 260px; max-width: 560px; overflow-wrap: anywhere; line-height: 1.5; }
.batch-report-followup { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 0.85fr); gap: 18px; margin-top: 18px; }
.batch-report-teaching-card { position: relative; overflow: hidden; }
.batch-report-teaching-card::before { content: ''; position: absolute; inset: 20px auto 20px 0; width: 3px; border-radius: 0 3px 3px 0; background: var(--moss); }
.batch-report-pending, .batch-report-failed, .batch-report-text, .batch-report-note { margin: 0; color: var(--ink-soft); font-size: var(--fs-md); line-height: 1.85; white-space: pre-wrap; }
.batch-report-pending { display: flex; align-items: center; gap: var(--sp-2); color: var(--ink-muted); white-space: normal; }
.batch-report-failed { color: var(--danger); }
.batch-report-note { margin-top: var(--sp-2); color: var(--warning); font-size: var(--fs-xs); }
.courseware-rec-state { display: flex; align-items: center; gap: var(--sp-2); margin: 0; color: var(--ink-muted); font-size: var(--fs-sm); }
.courseware-rec-list { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.courseware-rec-item { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; border: 1px solid var(--line); border-radius: var(--radius-md); background: var(--paper-raised); }
.courseware-rec-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.courseware-rec-main strong { color: var(--ink); font-family: var(--font-serif); font-size: var(--fs-lg); font-weight: 500; }
.courseware-rec-main small { color: var(--ink-muted); font-size: var(--fs-xs); line-height: 1.5; }
.batch-report-empty-state { display: flex; align-items: center; gap: 14px; margin-top: 18px; padding: 22px 18px; border: 1px dashed var(--rule); border-radius: var(--radius-lg); background: var(--paper); }
.batch-report-empty-state strong { color: var(--ink); font-family: var(--font-serif); font-size: var(--fs-lg); font-weight: 500; }
.batch-report-empty-state p { margin: 3px 0 0; color: var(--ink-muted); font-size: var(--fs-xs); line-height: 1.6; }
.recognize-warning { margin: 0 0 var(--sp-3); padding: var(--sp-2) var(--sp-3); border: 1px solid rgba(154, 106, 25, 0.32); border-radius: var(--radius-sm); background: var(--warning-soft); color: var(--warning); font-size: 12px; line-height: 1.5; }
.recognize-summary { margin: 0 0 var(--sp-3); padding: var(--sp-2) var(--sp-3); border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-subtle); color: var(--ink-soft); font-size: 12px; line-height: 1.5; }
.recognize-error { margin: 0 0 var(--sp-3); padding: var(--sp-2) var(--sp-3); border: 1px solid rgba(190, 74, 74, 0.35); border-radius: var(--radius-sm); background: rgba(190, 74, 74, 0.08); color: var(--danger); font-size: 12px; line-height: 1.5; }

@media (max-width: 900px) {
  .columns { grid-template-columns: 1fr; }
  .identity-card { grid-template-columns: 1fr; }
  .identity-create-fields { grid-template-columns: 1fr; }
  :global(.manual-review-dialog .el-dialog__body) { overflow-y: auto; }
  .manual-review-workspace, .manual-review-evidence, .manual-review-controls { grid-template-columns: minmax(0, 1fr); }
  .manual-review-paper, .manual-review-list { min-width: 0; }
  .manual-review-image-actions, .manual-review-list-toolbar { flex-wrap: wrap; }
  .manual-review-workspace { display: grid; height: auto; overflow: visible; }
  .manual-review-workspace.is-paper-hidden .manual-review-list { display: flex; max-height: none; overflow: visible; }
  .manual-review-workspace.is-paper-hidden .manual-review-item.is-compact,
  .manual-review-workspace.is-paper-hidden .manual-review-item.is-solve { grid-column: auto; }
  .manual-review-paper { height: min(62vh, 620px); min-height: 420px; }
  .manual-review-list { height: auto; overflow: visible; }
  .manual-review-list-toolbar > div:first-child { display: block; }
  .manual-review-list-toolbar > div:first-child > span { display: block; margin-top: 2px; }
  .manual-review-list-toolbar .manual-review-legend { display: flex; margin-top: 0; }
  .manual-review-list-toolbar .manual-review-legend > span { display: inline-flex; margin-top: 0; }
  .manual-review-item.is-compact { display: grid; grid-template-columns: 1fr; row-gap: 6px; }
  .manual-review-item.is-compact .manual-review-head { margin-bottom: 0; }
  .manual-review-item.is-compact .manual-review-evidence { display: grid; }
  .manual-review-item.is-compact .manual-review-evidence > div { display: block; }
  .manual-review-item.is-compact .manual-review-evidence span { display: block; margin-bottom: 3px; }
  .manual-review-item.is-compact .manual-review-controls .form-select { width: 100%; }
  .orientation-preview-stage { min-height: 300px; height: 46vh; }
  .orientation-controls { flex-wrap: wrap; }
  .mode-cards { grid-template-columns: 1fr; }
  .subscore-item { grid-template-columns: auto minmax(72px, 1fr) 20px; flex: 1 1 150px; }
  .batch-report-overview, .batch-report-insight-grid, .batch-report-followup { grid-template-columns: 1fr; }
}

@media (max-width: 640px) {
  .upload-page { padding: 0; }
  .card { padding: var(--sp-4); }
  .tpl-cut-panel { grid-template-columns: 1fr; }
  .tpl-cut-panel > .btn { justify-self: start; }
  .tpl-ans-mode, .tpl-column-mode { grid-template-columns: 1fr; }
  .tpl-answer-section-head { align-items: stretch; flex-direction: column; }
  .tpl-answer-actions { justify-content: flex-start; }
  .tpl-answer-grid { grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }
  .tpl-batch-score { grid-template-columns: auto 80px auto; }
  .tpl-batch-apply { grid-column: 1 / -1; }
  .correction-review-head { align-items: stretch; flex-direction: column; }
  .correction-decision-btn { align-self: flex-start; }
  .correction-current-question, .correction-history-row p { grid-template-columns: 1fr; gap: 3px; }
  .name-guide-diagram { flex-direction: column; align-items: center; }
  .result-table { display: block; overflow-x: auto; white-space: nowrap; }
  .batch-report-panel { padding: 14px; }
  .batch-report-head { align-items: flex-start; flex-direction: column; }
  .batch-report-head-actions { width: 100%; justify-content: space-between; }
  .batch-report-ring-card { padding: 16px; }
  .batch-report-stats { grid-template-columns: minmax(0, 1fr); }
  .batch-report-top-errors li { grid-template-columns: 30px minmax(0, 1fr) auto; gap: 8px; padding-inline: 4px; }
  .card-title-phases { margin-left: 0; }
  .actions { flex-wrap: wrap; }
}

</style>
