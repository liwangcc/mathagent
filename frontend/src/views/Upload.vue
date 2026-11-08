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
              @image-ready="q && markTemplateReferenceImageReady(page, q, $event)"
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
import { ref, reactive, computed, watch, nextTick, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
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
import api from '@/api'
import { countGradablePapers, isExcludedPaper, normalizeBatchReadiness } from '@/utils/batchReadiness'
import { gradingPhases } from '@/utils/gradingPhases'
import { formatGradingDuration } from '@/utils/gradingTimer'
import { IMPORT_ACCEPT, IMPORT_FORMATS } from '@/utils/fileExtraction'
import {
  batchReportStatusText,
  batchReportStatusTone,
  reportPercent,
  reportKnowledgeName,
  reportKnowledgeChapter,
  reportDistributionPercent,
} from '@/utils/batchReportView'
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
  templateAnswerText,
  templateChoiceConflictEntries,
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
} from '@/utils/templateReview'
import { templateSlotBoxes, templateSlotLabel } from '@/utils/templateSlots'
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
  templateBoxStyle,
  templateQuestionAnchorBBox,
  templateVisibleAnswerBoxes,
  mergeTemplateAnswerRegionSources,
  templateRegionCandidateUsable,
} from '@/utils/templateGeometry'
import {
  defaultGradingMethod,
  toStringList,
  markTemplateQuestionInput,
  markTemplateAnswerManualEdit,
  isCustomFill,
  fillScoreTotal,
  onFillScoringModeChange,
  onBlankScoresChange,
  onBlankCountChange,
  onQuestionTypeChange,
  choiceModeLabel,
  setTemplateChoiceMode,
  setSingleChoiceAnswer,
  choiceOptions,
  onChoiceAnswerChange,
  onChoiceAnswerInput,
  onTemplateFillSlotChange,
  onTemplateAnswerSlotsInput,
  addSubquestionScore,
  removeSubquestionScore,
} from '@/utils/answerSpecEditor'
import {
  updateDrawingReference,
  templateDrawingReferenceRegionsFor,
  markTemplateReferenceImageReady,
  templateDrawingReferenceSummary,
  templateDrawingReferenceSnapshot,
} from '@/utils/templateDrawing'
import {
  requiredHardwareState,
  ACCELERATOR_UNAVAILABLE_MESSAGE,
  paperOcrPending,
  paperOcrPendingLabel,
  paperOcrFailed,
  paperOcrNeedsReview,
  paperOcrSucceeded,
  recognitionIncompleteMessage,
  orientationReady,
  orientationReasonText,
} from '@/utils/recognitionStatus'
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
const TEMPLATE_SERVICE_UNAVAILABLE_MESSAGE = '模板识别链路当前不可用，请稍后再试。'
const ACCELERATOR_AUTO_RESUME_DELAY_MS = 3000
const ACCELERATOR_AUTO_RESUME_LIMIT = 6
// Transient OCR failures (provider capacity, malformed output) keep one durable
// server-side retry. Spend it automatically after a short cool-down instead of
// leaving the teacher at an incomplete batch.
const OCR_RETRY_AUTO_RESUME_DELAY_MS = 15000
const availabilityPollTimer = shallowRef(null)
let availabilityCheckPending = false
const serviceFaultHoldUntil = shallowRef(0)
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

function markServiceFault(message) {
  serviceFaultHoldUntil.value = Date.now() + SERVICE_FAULT_HOLD_MS
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

// 批改阶段状态
const progressList = ref([])
const gradeTotal = ref(0)
const gradeError = ref('')
const summary = ref(null)
const reviewRestoredLabel = ref('')
const coursewareRecommendations = ref([])
const coursewareRecommendationState = ref('idle')
const coursewareRecommendationQuery = ref({})
const coursewareRecommendationKey = shallowRef('')
const batchReport = ref({
  status: 'not_started',
  text: '',
  stats: {},
  error: '',
  updated_at: '',
})

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

const templateRecognizeController = shallowRef(null)
const templateConfirmController = shallowRef(null)
const templateCutController = shallowRef(null)

// Run token whose finished state was already applied.  Polling can observe the
// completion before the POST resolves; rendering it again would rebuild every
// page preview (refetching images and revoking blobs) on each tick.
const templateCutCompletedRunToken = shallowRef(null)
const templateCutRunToken = shallowRef(0)

const recognizeController = shallowRef(null)
const gradeController = shallowRef(null)

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

function recoverRecognitionConnection(targetBatchId, payload) {
  if (!payload || String(payload.batch_id || '') !== String(targetBatchId || '')
    || String(batchId.value || '') !== String(targetBatchId || '')
    || !['recognizing_remaining', 'recognition_interrupted'].includes(payload.status)
    || recognitionRecoveryState.value.status !== 'available') return false
  // This owned snapshot proves reachability, not spare model/hardware capacity.
  serviceFaultHoldUntil.value = 0
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

function templateReferenceImagesReady(page) {
  return templateDrawingReferenceQuestions(page).every((question) => page.referenceImageReadyNumbers?.[String(question.question_no)])
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

function requireTemplateService() {
  if (!templateServiceBlocksNewWork.value) return true
  ElMessage.error(templateServiceState.value.message || '模板识别链路当前不可用，请稍后再试。')
  return false
}

async function loadDailyUsage() {
  try {
    const data = await api.usage.stats()
    dailyUsage.value = data?.daily_images || data?.monthly_images || null
  } catch {
    dailyUsage.value = null
  }
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
      && Date.now() < serviceFaultHoldUntil.value
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

// The rest of the page lives in one composable per part (views/upload/use*.js). Each one gets ctx,
// holding what the earlier parts defined; it calls functions of later parts through ctx.
const ctx = {
  anonymous, auth, router, REVIEW_BATCH_STORAGE_PREFIX, ORIENTATION_BATCH_STORAGE_PREFIX,
  TEMPLATE_RECOGNITION_STORAGE_PREFIX, inheritIdentity, fileInputRef, folderInputRef,
  templateInputRef, fileList, phoneCaptureVisible, studentSubmissionVisible, imagePreprocessing,
  imageExtracting, extractionProgress, isDragover, batchDialogVisible, activeStep, recognizing,
  grading, gradingClockStart, gradingClockNow, serviceState, availabilityPollTimer,
  recognitionBlocksNewWork, markServiceFault, templateMode, templateColumnMode, templateFiles,
  templatePreprocessing, recognizingTemplate, templateRecognizeMessage, templateRecognizeError,
  confirmedTemplateId, templatePageCount, templateTeachingContext, templateDialogVisible,
  confirmingTemplate, importingTemplateBank, templateBankImported, templatePages,
  templateCorrectionCandidatesLoading, templateCutBusy, templateCutResult, templateCutProgress,
  templateCutActionId, templateCutImageType, templateCutPreviewPages, templateCutPreviewError,
  templateCropGesture, templateReferenceEditorInstances, templateActivePreviewPage,
  TEMPLATE_GEOMETRY_TOOLS, templateEditorBusy, templateQuestionEditing, templateChoiceScore,
  templateFillScore, pendingTemplateId, identityDialogVisible, identityResolutions,
  identityOptionsLoading, identityOptionsError, form, batchId, papers, recognized, recognitionDone,
  recognitionAutoArmed, recognitionCanGrade, recognitionError, recognitionRecoveryState,
  excludingPaperId, currentMessage, questions, orientationDialogVisible, orientationPaper,
  orientationImageUrl, orientationImageLoading, orientationImageError, orientationQuarterTurns,
  orientationSaving, orientationActionId, progressList, gradeTotal, gradeError, summary,
  reviewRestoredLabel, coursewareRecommendations, coursewareRecommendationState,
  coursewareRecommendationQuery, coursewareRecommendationKey, batchReport, reviewBusy,
  reviewImagesDownloading, reviewArchived, reviewRevision, manualDecisionDialogVisible,
  manualDecisionRow, manualDecisionDrafts, manualDecisionSaving, manualDecisionImageUrl,
  manualDecisionImageLoading, manualDecisionImageError, manualDecisionPages,
  manualDecisionPageIndex, manualDecisionShowPaper, templateRecognizeController,
  templateConfirmController, templateCutController, templateCutCompletedRunToken,
  templateCutRunToken, recognizeController, gradeController, gradablePaperCount,
  templateReviewPendingPageIndexes, templateDiagnosticPendingPageIndexes,
  clearTemplateResolvedGeometryDiagnostics, templateGeometryReview, templateCanConfirm,
  templateChoiceConflicts, needsIdentityResolution, identityCanConfirm,
  resetRecognitionCompletionState, resetRecognitionRecoveryState, recoverRecognitionConnection,
  clearRecognitionAutoResume, scheduleRetryableOcrAutoResume, updateRecognitionRecoveryState,
  applyRecognitionDone, templateQuestionSignature, templateReferenceQuestionEditBlockReason,
  templateDrawingReferenceSaveSnapshot, clearSavedTemplateDrawingReferenceChanges,
  templateDrawingReferenceQuestions, templateReferenceImagesReady, setTemplatePreviewEditMode,
  answerSpecFields, questionScoringValid, syncTemplateBatchScores, applyTemplateBatchScores,
  revokeIdentityImageUrls, prepareIdentityResolutions, identityResolutionPayload, batchConfirmValid,
  requireBatchService, requireRecognitionService, requireTemplateService, loadDailyUsage,
  refreshAvailability,
}
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
const {
  onTemplateFileChange, applyTemplateRecognitionResult, onRecognizeTemplate,
  templateCutPageOriginalCount, applyTemplateCutState, loadTemplateCutPreviews,
  loadTemplateCutState, onRunTemplateCut, templatePreviewQuestionIdentity,
  templateLayoutConflictNotice, resetTemplateColumns, templateSlotAnswer,
  correctTemplateSlotPosition, templatePageEditingLocked, templatePreviewPageIndex,
  templatePreviewPagePosition, selectTemplatePreviewPage, templateGeometryToolLabel,
  selectTemplateGeometryTarget, templateGeometryHint, setTemplateReferenceEditor,
  clearTemplateSelection, setTemplateStrokeSelection, armTemplateGeometryTool,
  startTemplateGeometryCreation, deleteTemplateSelection, startTemplateGeometryGesture,
  startTemplateDividerMove, startTemplateHandwritingMove, startTemplateHandwritingResize,
  startTemplateQuestionMove, startTemplateQuestionResize, startTemplateAnswerMove,
  startTemplateAnswerResize, startTemplateDrawingLabelMove, moveTemplateGeometry,
  finishTemplateGeometry, setTemplateAnswerDrawingKind, cancelTemplateGeometry,
  templatePageNeedsSave, insertTemplateQuestion, deleteTemplateQuestion, onConfirmTemplate,
  templateEditorProblems, toggleQuestionCorrection, openTemplateEditor, onImportTemplateBank,
  cancelTemplateDialog, resetTemplate, triggerFileUpload, triggerFolderUpload,
  triggerTemplateUpload, processFiles, onFileInputChange, onFolderInputChange, onDrop, resetFiles,
  openPhoneCapture, importPhoneCaptures, openStudentSubmissions, importStudentSubmissions,
  resetOrientationReview, openOrientationReview, rotateOrientationPreview, applyOrientationState,
  confirmOrientationAndRetry, canExcludeRecognitionPaper, excludeRecognitionPaper, reset,
  continueAfterRecognition, startNewBatch, onStartRecognize, onBatchConfirm, runRecognize,
  onConfirmIdentitiesAndGrade, onConfirmGrade, refreshRecognitionStateAfterFailure,
  continueRefreshedRecognition, resumeRecognitionBatch, dailyAverageCorrectRate, batchReportStats,
  batchReportTotalQuestions, batchReportNeedsReviewCount, batchReportJudgedCount,
  batchReportErrorTotal, batchReportPrimaryValue, batchReportPrimaryLabel,
  batchReportErrorRatePercent, batchReportDiagnosticErrorRateText, batchReportOutcomeSegments,
  batchReportTopErrors, batchReportKnowledgePoints, batchReportTopErrorMax,
  batchReportKnowledgeErrorMax, openCourseware, hasActiveReviewRows, pendingReviewCount,
  resolvedActiveReviewCount, manualDecisionCompactDrafts, manualDecisionSolveDrafts,
  manualDecisionHasEditableRows, manualDecisionComplete, openManualDecision,
  resetManualDecisionImage, changeManualDecisionPage, jumpToManualDecisionPage,
  openManualDecisionImage, saveManualDecision, voidOneReview, voidAllReviews, archiveAllReviews,
  downloadStudentErrors, downloadReviewImage, downloadCurrentReviewImage, downloadAllReviewImages,
  downloadAllErrors, downloadReviewSummary,
} = ctx
</script>

<style scoped src="./upload/page.css"></style>
<style scoped src="./upload/cards.css"></style>
