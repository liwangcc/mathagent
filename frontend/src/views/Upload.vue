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
import { ArrowDown, ArrowRight, UploadFilled } from '@element-plus/icons-vue'
import GradingPhases from '@/components/GradingPhases.vue'
import GradingProgress from '@/components/GradingProgress.vue'
import InkSpinner from '@/components/InkSpinner.vue'
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
import ManualDecisionDialog from './upload/ManualDecisionDialog.vue'
import TemplateEditorDialog from './upload/TemplateEditorDialog.vue'
import GradingResultsCard from './upload/GradingResultsCard.vue'
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
  anonymous, auth, inheritIdentity, fileInputRef, folderInputRef, templateInputRef, fileList,
  phoneCaptureVisible, studentSubmissionVisible, imagePreprocessing, imageExtracting,
  extractionProgress, isDragover, showNameGuide, batchDialogVisible, activeStep, recognizing,
  grading, dailyUsage, serviceState, serviceBlocksNewWork, templateServiceState,
  templateServiceBlocksNewWork, registrationHardwareState, recognitionBlocksNewWork,
  templateColumnMode, templateFiles, templatePreprocessing, recognizingTemplate,
  templateRecognizeMessage, templateRecognizeError, confirmedTemplateId, templatePageCount,
  templateStageLabel, templateDialogVisible, confirmingTemplate, templateBankImported,
  templatePages, templateCutBusy, templateCutResult, templateCutPreviewPages,
  templateCutPreviewError, templateCropGesture, pendingTemplateId, identityDialogVisible,
  identityResolutions, identityOptionsLoading, identityOptionsError, form, batchId, papers,
  recognized, recognitionDone, recognitionAutoArmed, recognitionCanGrade, recognitionError,
  recognitionRecoveryState, recognitionStats, excludingPaperId, currentMessage, questions,
  orientationDialogVisible, orientationPaper, orientationImageUrl, orientationImageLoading,
  orientationImageError, orientationQuarterTurns, orientationSaving, progressList, gradeError,
  summary, gradeHero, workflowSteps, progressCardVisible, gradingPhaseState, dailyUsagePercent,
  templateCutRunToken, recognizeSucceededCount, recognizeFailureCount, recognizeManualReviewCount,
  recognizeCompletedCount, recognizeUnresolvedCount, recognitionResumeVisible,
  recognitionRecoveryIsError, gradablePaperCount, templateLocked, batchConfirmValid,
  templateGeometryReview, templateCanConfirm, setTemplatePreviewEditMode, answerSpecFields,
  identityCanConfirm, openIdentityImage, openRecognizePaperImage, identityReasonHint,
  identityClueLabel, refreshIdentityOptions, onTemplateFileChange, applyTemplateRecognitionResult,
  onRecognizeTemplate, applyTemplateCutState, loadTemplateCutPreviews, loadTemplateCutState,
  onRunTemplateCut, resetTemplateColumns, templatePreviewPageIndex, selectTemplatePreviewPage,
  armTemplateGeometryTool, deleteTemplateSelection, startTemplateHandwritingMove,
  startTemplateHandwritingResize, insertTemplateQuestion, deleteTemplateQuestion,
  openTemplateEditor, onImportTemplateBank, cancelTemplateDialog, resetTemplate, triggerFileUpload,
  triggerFolderUpload, triggerTemplateUpload, processFiles, onFileInputChange, onFolderInputChange,
  onDrop, resetFiles, openPhoneCapture, importPhoneCaptures, openStudentSubmissions,
  importStudentSubmissions, resetOrientationReview, openOrientationReview, rotateOrientationPreview,
  applyOrientationState, confirmOrientationAndRetry, canExcludeRecognitionPaper,
  excludeRecognitionPaper, reset, continueAfterRecognition, startNewBatch, onStartRecognize,
  onBatchConfirm, runRecognize, onConfirmIdentitiesAndGrade, onConfirmGrade, resumeRecognitionBatch,
  dailyAverageCorrectRate, pendingReviewCount, resolvedActiveReviewCount,
} = ctx
</script>

<style scoped src="./upload/page.css"></style>
<style scoped src="./upload/cards.css"></style>
