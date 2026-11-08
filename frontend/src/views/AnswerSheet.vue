<template>
  <div
    class="answer-sheet-page"
    :class="{
      'is-editor-stage': session && activeStage === 3,
      'is-review-stage': session && activeStage === 4 && gradingDrafts.length,
      'workflow-readonly': workflowReadonly,
    }"
  >
    <div v-if="busy" class="busy-veil" role="status" aria-live="polite">
      <div class="busy-panel">
        <GradingProgress
          v-if="operationProgress"
          :current="operationProgress.current"
          :total="operationProgress.total"
          unit=""
          :message="operationMessage || '正在处理，进度会自动保存'"
          hint="进度会自动保存，中断后可在本批次继续"
        />
        <div v-else class="busy-plate">
          <SheetLoader :size="88" />
          <p><strong>{{ operationMessage || '正在处理，进度会自动保存' }}</strong><span>进度会自动保存，中断后可在本批次继续</span></p>
        </div>
      </div>
    </div>
    <GradingQuickDock />
    <section class="page-header">
      <div class="page-heading">
        <h1>答题卡批阅</h1>
        <p>原始试卷提供题目语义，空白答题卡提供版面，学生答题卡才是批阅对象。</p>
        <HelpButton
          title="答题卡批阅"
          content="按阶段完成：先识别原始试卷，再确认题目答案、绑定空白答题卡版面，最后上传学生答题卡并复核批阅结果。"
        />
      </div>
      <el-tag
        class="service-state"
        :type="serviceState.status === 'available' ? 'success' : (serviceState.status === 'queued' ? 'warning' : (serviceState.status === 'checking' ? 'info' : 'danger'))"
        effect="plain"
      >{{ serviceState.label }}</el-tag>
      <div v-if="session" class="session-meta">
        <el-tag effect="plain">{{ session.teaching_context?.education_stage === 'primary' ? '小学数学' : session.teaching_context?.education_stage === 'junior' ? '初中数学' : session.teaching_context?.education_stage === 'senior' ? '高中数学' : '历史会话 · 学段未标记' }}</el-tag>
        <el-tag effect="plain">{{ statusLabel }}</el-tag>
        <span v-if="session.batch_label" :title="session.batch_label">批次 {{ session.batch_label }}</span>
        <span>会话 {{ session.session_id }}</span>
        <el-button text :disabled="busy" @click="closeSession">批次列表</el-button>
        <el-button text type="danger" :disabled="!canDelete" @click="removeSession">删除草稿</el-button>
      </div>
    </section>

    <el-alert
      v-if="serviceState.status !== 'available'"
      :title="serviceState.message"
      :type="serviceState.status === 'queued' ? 'warning' : (serviceState.status === 'checking' ? 'info' : 'error')"
      show-icon
      :closable="false"
      class="notice service-notice"
    />
    <el-alert
      v-if="restoreError"
      :title="restoreError"
      type="warning"
      show-icon
      :closable="false"
      class="notice"
    />
    <el-alert
      v-if="session?.error"
      :title="session.error"
      type="error"
      show-icon
      :closable="false"
      class="notice"
    />

    <nav v-if="session" class="stage-strip" aria-label="答题卡批阅阶段">
      <button
        v-for="(item, index) in visibleStages"
        :key="item.title"
        type="button"
        class="stage-item"
        :class="{ active: activeStage === index, done: completedStage > index }"
        :disabled="!canOpenStage(index)"
        :aria-current="activeStage === index ? 'step' : undefined"
        @click="openStage(index)"
      >
        <span>{{ completedStage > index ? '✓' : index + 1 }}</span>
        <strong>{{ item.title }}</strong>
        <small>{{ item.caption }}</small>
      </button>
    </nav>

    <section v-if="!session" id="grading-start-anchor" class="work-card create-card">
      <div class="section-heading">
        <div>
          <span class="section-index">开始</span>
          <h2>建立本次批阅批次</h2>
          <p>批次绑定已冻结的试卷模板；班级取顶部导航的当前班级。批次以“答题卡模板号 + 时间戳”存贮，刷新后可继续。</p>
        </div>
        <HelpButton title="建立批阅批次" content="试卷模板来自教师已确认的详细批改模板，登记一次即可在多个批次复用。答题卡模板可在此选择，也可稍后在“空白答题卡”阶段套用或新建。" />
      </div>
      <el-form label-position="top" class="setup-grid">
        <el-form-item label="当前班级（顶部导航切换，批次自动继承）">
          <el-input :model-value="auth.currentClassName" disabled />
        </el-form-item>
        <el-form-item label="试卷模板（必选）">
          <el-select
            v-model="batchSetup.paper_template_code"
            filterable
            clearable
            class="full-width"
            placeholder="选择已登记的试卷模板"
            no-data-text="暂无试卷模板，请先在下方登记"
            :disabled="busy"
          >
            <el-option
              v-for="item in paperTemplates"
              :key="item.template_code"
              :label="paperTemplateLabel(item)"
              :value="item.template_code"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="答题卡模板（可选，也可稍后选择或新建）">
          <el-select
            v-model="batchSetup.answer_sheet_template_code"
            filterable
            allow-create
            default-first-option
            clearable
            class="full-width"
            placeholder="输入模板码，或从历史记录中选择"
            :disabled="busy"
          >
            <el-option
              v-for="item in templateCodeHistory"
              :key="item.template_code"
              :label="templateHistoryLabel(item)"
              :value="item.template_code"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="template-code-panel">
        <div class="template-code-copy">
          <strong>登记新的试卷模板</strong>
          <span>从已确认的详细批改模板冻结题目、答案与评分规则；同一内容重复登记得到同一模板号。</span>
        </div>
        <div class="template-code-actions">
          <el-select
            v-model="paperRegistration.template_id"
            filterable
            clearable
            class="template-code-select"
            placeholder="选择已确认的详细批改模板"
            no-data-text="暂无已确认的详细批改模板"
            :disabled="busy"
          >
            <el-option
              v-for="item in paperTemplateSources"
              :key="item.template_id"
              :label="paperSourceLabel(item)"
              :value="item.template_id"
            />
          </el-select>
          <el-input v-model="paperRegistration.alias" maxlength="40" placeholder="别名（可选）" class="paper-alias-input" :disabled="busy" />
          <el-button type="primary" plain :disabled="busy || !paperRegistration.template_id || serviceBlocksNewWork" @click="registerPaperTemplate">登记试卷模板</el-button>
        </div>
      </div>
      <div class="footer-actions">
        <el-button type="primary" :disabled="!canCreate || serviceBlocksNewWork" @click="createSession">创建批次并继续</el-button>
      </div>
      <div class="batch-list">
        <div class="batch-list-heading">
          <strong>最近批次</strong>
          <span>批次标识 = 答题卡模板号 + 时间戳；未选答题卡模板的批次在套用或生成模板码后获得标识。</span>
        </div>
        <el-empty v-if="!batches.length" description="还没有批次" :image-size="56" />
        <div v-else class="batch-rows">
          <button
            v-for="item in batches"
            :key="item.session_id"
            type="button"
            class="batch-row"
            :disabled="busy"
            @click="openBatch(item.session_id)"
          >
            <strong>{{ item.batch_label || '待绑定答题卡模板' }}</strong>
            <span>试卷模板 {{ item.paper_template_code || '—' }}</span>
            <span>{{ statusLabels[item.status] || item.status }}</span>
            <small>{{ formatBatchTime(item.created_at) }}</small>
          </button>
        </div>
      </div>
    </section>

    <template v-else>
      <section v-if="isBatch" v-show="activeStage === 0" class="work-card">
        <div class="section-heading">
          <div><span class="section-index">阶段 1</span><h2>试卷模板</h2><p>题目、答案与评分规则来自已冻结的试卷模板，批次内不再上传原始试卷或修改答案。</p></div>
          <HelpButton title="阶段 1：试卷模板" content="试卷模板在登记时冻结 answer_spec_hash；判分前会再次核对，模板与批次快照不一致时禁止判分。" />
        </div>
        <div class="saved-line">已绑定试卷模板 {{ session.paper_template_code }}，共 {{ session.answer_spec_snapshot?.length || 0 }} 题，答案已冻结。</div>
        <div class="footer-actions">
          <el-button type="primary" @click="activeStage = 1">查看题目与答案</el-button>
        </div>
      </section>
      <section v-else v-show="activeStage === 0" class="work-card">
        <div class="section-heading">
          <div><span class="section-index">阶段 1</span><h2>原始试卷与答案来源</h2><p>这里只提取题干、答案和评分规则，不作为学生答题卡的配准模板。</p></div>
          <HelpButton title="阶段 1：答案来源" content="原始试卷用于识别题目语义。答案可来自试卷、单独答案图片或 AI 候选；AI 候选必须经过教师确认。" />
        </div>
        <el-form label-position="top">
          <el-form-item label="答案来源">
            <el-radio-group v-model="answerSource" :disabled="sourceLocked">
              <el-radio-button value="embedded">试卷内含答案</el-radio-button>
              <el-radio-button value="separate">另传答案文件</el-radio-button>
              <el-radio-button value="ai_generated">AI 生成候选</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <div class="upload-grid">
            <FilePicker v-model="sourceFiles" :max-files="20" title="选择原始试卷图片" note="支持 JPG、JPEG、PNG，最多 20 页" :disabled="sourceLocked || fileSelectionProcessing" />
            <FilePicker v-if="answerSource === 'separate'" v-model="answerFiles" :max-files="20" title="选择答案文件图片" note="题号必须与原始试卷一致，最多 20 页" :disabled="sourceLocked || fileSelectionProcessing" />
          </div>
        </el-form>
        <div v-if="session.source_exam_pages?.length" class="saved-line">已保存 {{ session.source_exam_pages.length }} 页原始试卷，答案来源：{{ answerSourceLabel }}</div>
        <div class="footer-actions">
          <el-button v-if="!sourceLocked" :disabled="!canUploadSource || fileSelectionProcessing" @click="uploadSource">上传材料</el-button>
          <el-button v-if="session.status === 'source_uploaded' || (session.status === 'failed' && session.source_exam_pages?.length)" type="primary" :loading="busy" :disabled="busy" @click="recognizeSource">{{ busy ? '识别中...' : (sourceRecognitionResumable ? '继续识别题目' : '识别题目') }}</el-button>
          <el-button v-if="completedStage >= 1" type="primary" @click="activeStage = 1">检查识别草稿</el-button>
        </div>
      </section>

      <section v-show="activeStage === 1" class="work-card">
        <div class="section-heading">
          <div><span class="section-index">阶段 2</span><h2>教师确认题目与答案</h2><p>教师确认是权威边界。请逐题检查题型、答案、分值和评分规则。</p></div>
          <el-tag type="info">{{ questionDrafts.length }} 题</el-tag>
          <HelpButton title="阶段 2：确认题目与答案" content="逐题确认题号、题型、标准答案、分值和评分方式。保存后这些内容会作为本次批阅的唯一依据。" />
        </div>
        <el-empty v-if="!questionDrafts.length" description="请先完成原始试卷识别" />
        <div v-else class="question-list">
          <article v-for="(question, index) in questionDrafts" :key="index" class="question-card">
            <div class="question-number">第 <el-input v-model="question.question_no" :disabled="answersLocked" /> 题</div>
            <div class="question-grid">
              <el-form-item label="题型">
                <el-select v-model="question.question_type" :disabled="answersLocked">
                  <el-option label="选择题" value="choice" /><el-option label="填空题" value="fill" /><el-option label="解答题" value="solve" />
                </el-select>
              </el-form-item>
              <el-form-item v-if="question.question_type === 'choice'" label="选择类型">
                <el-select v-model="question.choice_mode" :disabled="answersLocked">
                  <el-option label="待教师确认" value="unknown" />
                  <el-option label="单选题" value="single" />
                  <el-option label="多选题（全对全得）" value="multiple" />
                </el-select>
              </el-form-item>
              <el-form-item label="满分（考试必填）"><el-input-number v-model="question.max_score" :min="0" :max="1000" :disabled="answersLocked" /></el-form-item>
              <el-form-item label="评分方式">
                <el-select v-model="question.grading_method" :disabled="answersLocked">
                  <el-option v-if="question.question_type === 'solve'" label="按题意判分" value="" />
                  <el-option label="精确匹配" value="exact" /><el-option label="等价答案" value="equivalent" /><el-option label="按过程评分" value="process" />
                </el-select>
              </el-form-item>
            </div>
            <el-form-item label="题干"><el-input v-model="question.question_text" type="textarea" :rows="2" :disabled="answersLocked" /></el-form-item>
            <el-form-item label="标准答案"><el-input v-model="question.standard_answer" type="textarea" :rows="2" :disabled="answersLocked" /></el-form-item>
            <el-form-item label="解题步骤 / 评分规则（每行一条）"><el-input v-model="question.solution_steps_text" type="textarea" :rows="2" :disabled="answersLocked" /></el-form-item>
          </article>
        </div>
        <div class="footer-actions">
          <el-button @click="activeStage = 0">上一步</el-button>
          <el-button v-if="!answersLocked" type="primary" :loading="busy" :disabled="busy || !questionDrafts.length" @click="confirmAnswers">{{ busy ? '保存中...' : '确认题目与答案' }}</el-button>
          <el-button v-else type="primary" @click="activeStage = 2">上传空白答题卡</el-button>
        </div>
      </section>

      <section v-show="activeStage === 2" class="work-card">
        <div class="section-heading">
          <div><span class="section-index">阶段 3</span><h2>选择答题卡模板</h2><p>可直接复用共享模板码，也可上传新的空白答题卡并由 qwen3.8max 粗定位。</p></div>
          <HelpButton title="阶段 3：答题卡模板" content="用过的模板码会出现在历史下拉框中。复用模板码不会调用模型；首次上传只由模型提出区域草稿，仍需教师逐项确认。" />
        </div>
        <div class="template-code-panel">
          <div class="template-code-copy">
            <strong>使用已有模板码</strong>
            <span>全校教师可共享；历史记录只显示当前账号使用过的模板。</span>
          </div>
          <div class="template-code-actions">
            <el-select
              v-model="templateCodeInput"
              filterable
              allow-create
              default-first-option
              clearable
              class="template-code-select"
              placeholder="输入模板码，或从历史记录中选择"
              :disabled="layoutFrozen || busy"
            >
              <el-option
                v-for="item in templateCodeHistory"
                :key="item.template_code"
                :label="templateHistoryLabel(item)"
                :value="item.template_code"
              />
            </el-select>
            <el-button
              type="primary"
              plain
              :disabled="layoutFrozen || busy || !String(templateCodeInput || '').trim()"
              @click="useTemplateCode"
            >直接套用</el-button>
          </div>
        </div>
        <div class="template-or"><span>或首次建立模板</span></div>
        <FilePicker v-model="layoutFiles" :max-files="20" title="选择空白答题卡图片" note="支持自制及其他平台版式；上传后自动粗定位，按页面顺序选择，最多 20 页" :disabled="layoutFrozen || fileSelectionProcessing" />
        <div v-if="session.answer_sheet_pages?.length" class="saved-line">已保存 {{ session.answer_sheet_pages.length }} 页空白答题卡</div>
        <el-alert
          v-if="layoutAcceleration"
          :title="layoutAccelerationMessage"
          :type="layoutAcceleration.status === 'fault' ? 'error' : 'warning'"
          :closable="false"
          show-icon
        />
        <div class="footer-actions">
          <el-button @click="activeStage = 1">上一步</el-button>
          <el-button v-if="layoutAcceleration" :loading="busy" :disabled="busy" @click="processLayoutAcceleration">{{ busy ? '处理中...' : '继续处理已保存页面' }}</el-button>
          <el-button v-if="!layoutFrozen" type="primary" :loading="busy" :disabled="busy || !layoutFiles.length || fileSelectionProcessing" @click="uploadLayout">{{ busy ? '处理中...' : '上传并智能定位' }}</el-button>
          <el-button v-else type="primary" @click="activeStage = 4">进入学生答题卡阶段</el-button>
        </div>
      </section>

      <section v-show="activeStage === 3" class="work-card editor-card">
        <div class="section-heading">
          <div><span class="section-index">阶段 4</span><h2>版面绑定与冻结</h2><p>坐标范围为 0–1000。表单编辑可靠保存，预览框同步显示当前位置和大小。</p></div>
          <el-tag :type="layoutSummary.can_freeze ? 'success' : 'warning'">{{ layoutSummary.can_freeze ? '可冻结' : '待完善' }}</el-tag>
          <HelpButton title="阶段 4：版面绑定与冻结" content="把每道题绑定到答案区域，并标出姓名、学号等身份区域。确认无漏题、冲突和越界后再冻结，冻结后不能继续修改。" />
        </div>
        <div v-if="currentPage" class="page-tabs">
          <el-button v-for="page in session.answer_sheet_pages" :key="page.page_id" :type="currentPageId === page.page_id ? 'primary' : ''" plain @click="currentPageId = page.page_id">第 {{ page.index + 1 }} 页</el-button>
        </div>
        <div v-if="currentPage" class="editor-layout">
          <div class="paper-stage">
            <div v-if="layoutImageUrl" :key="layoutImageUrl" class="paper-image-surface">
              <img :src="layoutImageUrl" alt="空白答题卡当前页" @load="layoutImageReady = true" @error="layoutImageReady = false; layoutImageError = '答题卡图像无法显示，请重新打开当前页。'" />
              <template v-if="layoutImageReady">
                <div v-for="region in visibleAnswerRegions" :key="region.region_id" class="region-box answer-box" :style="bboxStyle(region.bbox)">题 {{ region.question_no }}</div>
                <div v-for="region in visibleIdentityRegions" :key="region.region_id" class="region-box identity-box" :style="bboxStyle(region.bbox)">{{ identityLabels[region.identity_type] }}</div>
              </template>
            </div>
            <div v-if="!layoutImageUrl || layoutImageError" class="image-loading">{{ layoutImageError || '正在读取受保护的答题卡图像…' }}</div>
          </div>
          <aside class="region-panel">
            <h3>答案区域 <HelpButton title="答案区域" content="每个答案区域对应一道题或一个小问。区域坐标必须覆盖学生实际作答位置，重复或漏绑都会阻止冻结。" /></h3>
            <div v-for="(region, index) in answerRegions" :key="region.region_id" class="region-form">
              <div class="region-form-head"><strong>{{ region.region_id }}</strong><el-button text type="danger" :disabled="layoutFrozen" @click="answerRegions.splice(index, 1)">删除</el-button></div>
              <div class="compact-grid">
                <el-select v-model="region.page_id" :disabled="layoutFrozen"><el-option v-for="page in session.answer_sheet_pages" :key="page.page_id" :label="`第 ${page.index + 1} 页`" :value="page.page_id" /></el-select>
                <el-select v-model="region.question_no" :disabled="layoutFrozen" @change="syncRegionType(region)"><el-option v-for="q in answerSpecs" :key="q.question_no" :label="`第 ${q.question_no} 题`" :value="q.question_no" /></el-select>
                <el-select v-model="region.region_type" :disabled="layoutFrozen"><el-option v-for="option in regionTypeOptions(region.question_no)" :key="option.value" :label="option.label" :value="option.value" /></el-select>
                <el-input v-model="region.subquestion_no" placeholder="小问（可空）" :disabled="layoutFrozen" />
              </div>
              <BboxFields v-model="region.bbox" :disabled="layoutFrozen" />
              <el-checkbox v-model="region.required" :disabled="layoutFrozen">必答区域</el-checkbox>
              <el-checkbox v-model="region.teacher_confirmed" :disabled="layoutFrozen">教师已确认</el-checkbox>
            </div>
            <el-button v-if="!layoutFrozen" plain class="wide-button" @click="addAnswerRegion">新增答案区域</el-button>

            <h3>身份区域 <HelpButton title="身份区域" content="身份区域用于读取姓名、学号或准考证号。至少保留可确认学生身份的区域，识别不确定时仍需教师复核。" /></h3>
            <div v-for="(region, index) in identityRegions" :key="region.region_id" class="region-form identity-form">
              <div class="region-form-head"><strong>{{ region.region_id }}</strong><el-button text type="danger" :disabled="layoutFrozen" @click="identityRegions.splice(index, 1)">删除</el-button></div>
              <div class="compact-grid">
                <el-select v-model="region.page_id" :disabled="layoutFrozen"><el-option v-for="page in session.answer_sheet_pages" :key="page.page_id" :label="`第 ${page.index + 1} 页`" :value="page.page_id" /></el-select>
                <el-select v-model="region.identity_type" :disabled="layoutFrozen"><el-option v-for="(label, value) in identityLabels" :key="value" :label="label" :value="value" /></el-select>
              </div>
              <BboxFields v-model="region.bbox" :disabled="layoutFrozen" />
              <el-input-number v-if="region.identity_type === 'exam_no_marks'" v-model="region.config.digits" :min="1" :max="32" :disabled="layoutFrozen" />
              <el-checkbox v-model="region.teacher_confirmed" :disabled="layoutFrozen">教师已确认</el-checkbox>
            </div>
            <el-button v-if="!layoutFrozen" plain class="wide-button" @click="addIdentityRegion">新增身份区域</el-button>
          </aside>
        </div>
        <div class="layout-summary">
          <span>已绑定 {{ layoutSummary.bound_count || 0 }} / {{ layoutSummary.question_count || answerSpecs.length }} 题</span>
          <span>身份区域 {{ layoutSummary.identity_region_count || identityRegions.length }} 个</span>
          <span v-if="layoutSummary.missing_questions?.length">漏绑定：{{ layoutSummary.missing_questions.join('、') }}</span>
          <span v-if="layoutSummary.conflicts?.length">冲突 {{ layoutSummary.conflicts.length }} 组</span>
        </div>
        <div v-if="session.shared_template_code" class="published-template">
          <div>
            <span>本版面模板码</span>
            <strong>{{ session.shared_template_code }}</strong>
            <small>模板码已加入历史记录，其他老师可直接输入复用。</small>
          </div>
          <el-button plain @click="copyTemplateCode">复制模板码</el-button>
        </div>
        <div class="bubble-grid-panel">
          <h3>涂卡栅格（选择题图卡） <HelpButton title="涂卡栅格" content="模型只提议整格范围；选项格由老师调整后显式确认，确认后随模板冻结复用。未确认的栅格会阻止版面冻结与判分（fail-closed）。" /></h3>
          <el-alert
            v-if="bubbleGridRows.length && bubbleGridsUnconfirmed"
            :title="`还有 ${bubbleGridsUnconfirmed} 组涂卡栅格未确认，将阻止版面冻结与判分`"
            type="warning"
            :closable="false"
            show-icon
          />
          <div class="footer-actions">
            <span>选项格均分数量</span>
            <el-input-number v-model="gridSplitCount" size="small" :min="2" :max="8" controls-position="right" />
          </div>
          <el-empty v-if="!bubbleGridRows.length" description="暂无涂卡栅格：请先在答案区域标注选择题涂卡区并重新粗定位" :image-size="60" />
          <article v-for="(grid, index) in bubbleGridRows" :key="`${grid.question_no}-${grid.subquestion_no}-${index}`" class="question-card">
            <div class="region-form-head">
              <strong>第 {{ grid.question_no }}{{ grid.subquestion_no ? `.${grid.subquestion_no}` : '' }} 题</strong>
              <el-tag size="small" :type="grid.state === 'confirmed' ? 'success' : 'warning'">{{ grid.state === 'confirmed' ? `已确认${grid.adjusted ? '（已调整）' : ''}` : '模型提议' }}</el-tag>
              <el-tag v-if="grid.allow_multiple" size="small" type="info">多选</el-tag>
              <el-text v-if="grid.proposal?.confidence != null" size="small" type="info">置信度 {{ grid.proposal.confidence }}</el-text>
            </div>
            <div class="footer-actions">
              <span>整格范围 x,y,w,h</span>
              <el-input-number v-for="(value, boxIndex) in grid.grid_bbox" :key="boxIndex" v-model="grid.grid_bbox[boxIndex]" size="small" :min="0" controls-position="right" />
              <el-select v-model="grid.grid_order" size="small" style="width: 120px">
                <el-option label="左右排列" value="left_to_right" />
                <el-option label="上下排列" value="top_to_bottom" />
              </el-select>
              <el-button size="small" plain @click="autoSplitGridCells(grid)">按网格均分生成选项格</el-button>
            </div>
            <div v-for="(cell, cellIndex) in grid.cells" :key="cellIndex" class="footer-actions">
              <el-input v-model="cell.option" size="small" maxlength="1" style="width: 64px" placeholder="选项" />
              <el-input-number v-for="(value, boxIndex) in cell.bbox" :key="boxIndex" v-model="cell.bbox[boxIndex]" size="small" :min="0" controls-position="right" />
              <el-button text type="danger" size="small" @click="grid.cells.splice(cellIndex, 1)">移除</el-button>
            </div>
            <div class="footer-actions">
              <el-button size="small" :disabled="(grid.cells || []).length >= 8" @click="addGridCell(grid)">加选项格</el-button>
              <el-button size="small" type="primary" :disabled="(grid.cells || []).length < 2" :loading="busy" @click="confirmBubbleGridRow(grid)">{{ grid.state === 'confirmed' ? '重新确认' : '确认此栅格' }}</el-button>
              <el-button size="small" type="danger" plain :loading="busy" @click="discardBubbleGridRow(grid)">丢弃</el-button>
            </div>
          </article>
        </div>
        <div class="footer-actions">
          <el-button @click="activeStage = 2">上一步</el-button>
          <el-button
            v-if="!layoutFrozen && session.answer_sheet_pages?.length && session.layout_localization?.provider_route !== 'qwen3.8max'"
            plain
            :loading="busy"
            @click="locateLayoutRegions"
          >重新粗定位</el-button>
          <el-button
            v-if="currentPageHasUnconfirmedRegions"
            plain
            @click="confirmCurrentPageRegions"
          >确认本页定位</el-button>
          <el-button v-if="!layoutFrozen" :disabled="!answerRegions.length || !identityRegions.length" @click="saveLayout">保存版面</el-button>
          <el-button v-if="!layoutFrozen" type="primary" :disabled="!layoutSummary.can_freeze" @click="freezeLayout">冻结版面</el-button>
          <el-button v-else-if="!session.shared_template_code" type="primary" :loading="busy" @click="publishTemplateCode(false)">生成模板码</el-button>
          <el-button v-else type="primary" @click="activeStage = 4">下一步</el-button>
        </div>
      </section>

      <section v-show="activeStage === 4" class="work-card">
        <div class="section-heading">
          <div><span class="section-index">阶段 5</span><h2>学生答题卡与批阅</h2><p>学生页将经过方向、页型、身份、完整性、配准和答案合同门禁。</p></div>
          <HelpButton title="阶段 5：学生答题卡与批阅" content="按文件顺序上传学生答题卡。系统会逐页检查方向、身份和答案完整性，待复核页需要老师确认后才可归档。" />
        </div>
        <el-alert v-if="!layoutFrozen" title="请先冻结答题卡版面" type="warning" :closable="false" show-icon />
        <template v-else>
          <template v-if="!gradingDrafts.length">
            <el-alert
              class="student-pairing-tip"
              type="info"
              :closable="false"
              show-icon
              title="正反面归属"
              description="每位学生一个文件夹（文件夹内放正反两页，如 收集.zip/张三/1.jpg、2.jpg），或文件名带“正面/反面”标记（如 张三_正面.jpg、张三_反面.jpg）时，系统自动配对正反面，并用文件夹名或文件名在班级名单中确认身份；文件名不能确定身份时，请按顺序上传同一学生的正面和背面（正面在前），背面沿用正面身份。"
            />
            <FilePicker
              v-model="studentFiles"
              :max-files="200"
              title="导入学生答题卡"
              note="可选择图片或文件夹，也可拖入文件夹或 ZIP/RAR 压缩包；按文件夹、文件序号或时间自动排序，排序后可核对；最多 200 张"
              auto-order
              import-archives
              @order-detected="studentOrderSource = $event"
              :disabled="session.status === 'archived' || studentUploadProcessing || fileSelectionProcessing"
            />
            <div v-if="studentFiles.length" class="student-order-panel">
              <div class="student-order-head">
                <strong>待上传物理顺序</strong>
                <el-tag size="small" effect="plain">{{ studentOrderSource.label }}</el-tag>
                <span>{{ studentOrderSource.message }}{{ studentPairingNote }}</span>
              </div>
              <ol class="student-order-list">
                <li v-for="(file, index) in studentFiles" :key="`${file.name}-${index}`">
                  <span class="student-order-index">{{ index + 1 }}</span>
                  <span class="student-order-name" :title="studentFileDisplayPath(file)">{{ studentFileDisplayPath(file) }}</span>
                  <el-tag size="small" effect="plain">{{ studentFileSideLabel(file, index) }}</el-tag>
                  <div class="student-order-actions">
                    <el-button text size="small" :disabled="index === 0 || studentUploadActive" @click="moveStudentFile(index, -1)">上移</el-button>
                    <el-button text size="small" :disabled="index === studentFiles.length - 1 || studentUploadActive" @click="moveStudentFile(index, 1)">下移</el-button>
                    <el-button text type="danger" size="small" :disabled="studentUploadActive" @click="removeStudentFile(index)">移除</el-button>
                  </div>
                </li>
              </ol>
            </div>
            <div v-if="studentUploadActive" class="student-upload-progress">
              <div class="student-upload-progress-head">
                <div>
                  <strong>{{ studentUploadProcessing ? '图片处理中' : '上传批次可继续' }}</strong>
                  <span>
                    已持久保存 {{ Number(studentUpload.uploaded_count || 0) }} 页
                    · 已处理 {{ Number(studentUpload.processed_count || 0) }} 页
                    · {{ formatBytes(studentUpload.total_bytes) }}
                  </span>
                </div>
                <el-tag :type="studentUploadProcessing ? 'warning' : 'info'" effect="plain">
                  {{ studentUploadProcessing ? '处理中' : '上传中' }}
                </el-tag>
              </div>
              <GradingProgress
                v-if="studentUploadProcessing"
                class="student-upload-meter"
                verb="已处理"
                unit="页"
                :current="Number(studentUpload.processed_count || 0)"
                :total="Number(studentUpload.uploaded_count || 0)"
                :live="false"
              />
              <small>{{ studentUploadProcessing ? '处理进度已保存，可从当前批次继续。' : '已保存页不会因断线丢失；再次选择文件时会自动去重。' }}</small>
            </div>
            <div class="footer-actions">
              <el-button
                type="primary"
                :loading="busy"
                :disabled="busy || !studentFiles.length || studentUploadProcessing || fileSelectionProcessing"
                @click="uploadStudents"
              >{{ busy ? '处理中...' : (studentUploadActive ? '继续上传并处理' : '上传并处理') }}</el-button>
              <el-button
                v-if="studentUploadCanProcess && (!studentFiles.length || studentUploadProcessing)"
                type="primary"
                plain
                :loading="busy"
                :disabled="busy"
                @click="processStudentUpload()"
              >{{ busy ? '处理中...' : (studentUploadProcessing ? '继续处理' : '处理已上传页') }}</el-button>
              <el-button
                v-if="session.student_pages?.length"
                type="primary"
                :loading="busy"
                :disabled="busy || studentUploadActive"
                @click="recognizeStudents"
              >{{ busy ? '识别中...' : (studentRecognitionResumable ? '继续识别' : '执行识别') }}</el-button>
              <el-button
                v-if="studentUploadActive"
                text
                type="danger"
                @click="restartStudentUpload"
              >丢弃本批并重新开始</el-button>
            </div>
            <div v-if="session.student_pages?.length" class="question-list">
              <article v-for="page in session.student_pages" :key="page.paper_id" class="question-card">
                <div class="region-form-head"><strong>{{ page.filename || page.paper_id }}</strong><el-tag :type="page.review_reasons?.length ? 'warning' : 'success'">{{ page.review_reasons?.length ? '待复核' : '已就绪' }}</el-tag></div>
                <el-text class="student-identity-hint" size="small" :type="studentPageIdentityHint(page, session.student_pages).type">{{ studentPageIdentityHint(page, session.student_pages).text }}</el-text>
                <div v-if="!['auto_passed','teacher_confirmed'].includes(page.orientation_status)" class="footer-actions">
                  <span>方向未通过：</span><el-button v-for="turn in [0,1,2,3]" :key="turn" size="small" @click="confirmOrientation(page.paper_id, turn)">{{ turn * 90 }}°</el-button>
                </div>
                <el-form v-if="session.status === 'review'" label-position="top">
                  <el-form-item label="确认学生"><el-select v-model="page.review_student_id" filterable><el-option v-for="student in classStudents" :key="student.id" :label="`${student.name}（${student.exam_no || student.student_no || '无编号'}）`" :value="student.id" /></el-select></el-form-item>
                  <el-form-item v-for="answer in page.answers || []" :key="answer.region_id">
                    <template #label>
                      <span>第 {{ answer.question_no }} 题答案
                        <el-tag v-if="answer.label || answer.outcome" size="small" effect="plain" :type="recognitionTagType(answer)">{{ answer.label || answer.outcome }}</el-tag>
                      </span>
                    </template>
                    <el-input v-model="answer.answer" :placeholder="answer.reason || '确认或修正学生答案'" />
                    <div>
                      <el-text v-if="answer.reason" size="small" type="warning">{{ answer.reason }}</el-text>
                      <el-text v-if="(answer.answer_options || []).length || (answer.marked_options || []).length" size="small" type="info">
                        　选项 {{ (answer.answer_options || []).join(' / ') || '—' }}　学生涂记 {{ (answer.marked_options || []).join(' / ') || '无' }}
                      </el-text>
                    </div>
                  </el-form-item>
                  <el-button type="primary" :loading="busy" :disabled="busy" @click="confirmStudentReview(page)">{{ busy ? '保存中...' : '确认本页身份与答案' }}</el-button>
                </el-form>
              </article>
            </div>
            <div v-if="session.status === 'review'" class="footer-actions"><el-button type="primary" :loading="busy" :disabled="busy" @click="buildDraft">{{ busy ? '生成中...' : (gradingDraftResumable ? '继续生成批阅草稿' : '生成统一批阅草稿') }}</el-button></div>
          </template>

          <div v-else class="grading-review">
            <div v-if="gradingDrafts.length > 1" class="review-student-tabs" aria-label="选择学生批阅草稿">
              <button
                v-for="(draft, index) in gradingDrafts"
                :key="draft.student_id"
                type="button"
                :class="{ active: activeDraftIndex === index }"
                @click="setActiveDraft(index)"
              >
                {{ draft.result?.student_name || `学生 ${index + 1}` }}
              </button>
            </div>

            <div class="review-workbench">
              <section class="student-paper-pane" aria-label="完整学生答题卡">
                <header class="review-pane-head">
                  <div class="review-pane-title"><strong>完整学生答题卡</strong><span>{{ currentDraftResult.student_name || '当前学生' }}</span></div>
                  <div class="review-pager">
                    <button type="button" aria-label="上一页" :disabled="currentReviewPageIndex <= 0" @click="moveReviewPage(-1)">‹</button>
                    <span><b>{{ currentReviewPageIndex + 1 }}</b> / {{ currentDraftPages.length }}</span>
                    <button type="button" aria-label="下一页" :disabled="currentReviewPageIndex >= currentDraftPages.length - 1" @click="moveReviewPage(1)">›</button>
                  </div>
                </header>
                <div class="student-paper-scroll">
                  <img
                    v-if="studentPageImageUrl"
                    :src="studentPageImageUrl"
                    :alt="`${currentDraftResult.student_name || '学生'}答题卡第 ${currentReviewPageIndex + 1} 页`"
                  />
                  <div v-else-if="studentPageImageError" class="student-image-state error">{{ studentPageImageError }}</div>
                  <div v-else class="student-image-state">正在读取受保护的完整答题卡…</div>
                </div>
                <footer class="student-paper-meta">
                  <span>{{ currentReviewPage?.filename || currentReviewPaperId }}</span>
                  <span>上传原图 · 第 {{ currentReviewPageIndex + 1 }} 页</span>
                  <el-button size="small" plain @click="openEvidenceViewer(currentReviewPaperId)">转录证据</el-button>
                </footer>
              </section>

              <aside class="grading-result-pane" aria-label="系统批阅结果">
                <header class="review-result-head">
                  <div class="review-metric"><strong>{{ formatDraftMetric(currentDraftResult) }}</strong><span>系统批阅结果</span></div>
                  <div class="review-counts">
                    <span><VerdictSwatch verdict="correct" />正确 <b>{{ currentDraftResult.correct_count || 0 }}</b></span>
                    <span><VerdictSwatch verdict="partial" />部分 <b>{{ currentDraftResult.partial_count || 0 }}</b></span>
                    <span><VerdictSwatch verdict="wrong" />错误 <b>{{ currentDraftResult.wrong_count || 0 }}</b></span>
                    <span v-if="currentDraftResult.needs_review_count" class="is-pending"><VerdictSwatch verdict="pending" />待复核 <b>{{ currentDraftResult.needs_review_count }}</b></span>
                  </div>
                </header>
                <div class="review-result-scroll">
                  <article
                    v-for="(detail, index) in currentDraftResult.details || []"
                    :key="`${reviewDetailQuestionNo(detail, index)}-${index}`"
                    class="review-detail-card"
                    :class="[
                      reviewDetailClass(detail),
                      { active: (detail.paper_ids || []).includes(currentReviewPaperId) },
                    ]"
                    @click="openDetailPage(detail)"
                  >
                    <span class="review-detail-top">
                      <strong>第 {{ reviewDetailQuestionNo(detail, index) }} 题</strong>
                      <i>{{ reviewDetailLabel(detail) }}</i>
                      <em v-if="detail.max_score != null">{{ detail.awarded_score ?? '—' }} / {{ detail.max_score }} 分</em>
                    </span>
                    <span v-if="detail.question" class="review-question" v-html="renderReviewText(detail.question)" />
                    <span class="review-answer-row"><label>学生答案</label><span v-html="renderReviewText(detail.student_answer || '未作答')" /></span>
                    <span class="review-answer-row"><label>标准答案</label><span v-html="renderReviewText(detail.standard_answer || detail.correct_answer || '—')" /></span>
                    <span v-if="reviewDetailAnalysis(detail)" class="review-analysis" v-html="renderReviewText(reviewDetailAnalysis(detail))" />
                    <div v-if="session.status === 'review'" class="teacher-decision" @click.stop>
                      <span>人工判定</span>
                      <el-radio-group v-model="detail.result" size="small">
                        <el-radio-button value="correct">正确</el-radio-button>
                        <el-radio-button value="partial">部分正确</el-radio-button>
                        <el-radio-button value="wrong">错误</el-radio-button>
                      </el-radio-group>
                      <el-input
                        v-model="detail.teacher_note"
                        maxlength="500"
                        placeholder="改判说明（可选）"
                      />
                    </div>
                  </article>
                  <el-empty v-if="!currentDraftResult.details?.length" description="暂无逐题批阅结果" />
                </div>
                <footer id="grading-summary-anchor" class="review-result-footer">
                  <span>点击题目可跳到该题所在答题卡页</span>
                  <div>
                    <el-button v-if="session.status === 'review'" @click="gradingDrafts = []">返回答案复核</el-button>
                    <el-button v-if="session.status === 'review'" @click="saveDraftDecision">保存人工判定</el-button>
                    <el-button
                      v-if="session.status === 'review'"
                      type="primary"
                      :disabled="!canArchiveDrafts"
                      @click="archiveDrafts"
                    >确认全部并归档</el-button>
                    <el-tag v-else type="success">已归档</el-tag>
                  </div>
                </footer>
              </aside>
            </div>
          </div>
        </template>
      </section>
      <el-dialog v-model="evidenceDialogVisible" title="转录与识别证据" width="720px">
        <div v-if="evidenceLoading" class="student-image-state">正在读取证据清单…</div>
        <el-empty v-else-if="!evidenceRecords.length" description="本页暂无已登记的转录证据" />
        <div v-for="record in evidenceRecords" :key="record.directory" class="question-card">
          <div class="region-form-head">
            <strong>第 {{ (record.question_nos || []).join('、') }} 题</strong>
            <el-text size="small" type="info">{{ record.directory }}</el-text>
          </div>
          <ul>
            <li v-for="file in record.files" :key="file.file">
              <el-button text type="primary" size="small" @click="openEvidenceFile(record, file)">{{ file.file }}</el-button>
              <el-tag size="small" effect="plain">{{ file.kind }}</el-tag>
              <el-text size="small" type="info">{{ file.bytes }} B</el-text>
            </li>
          </ul>
        </div>
        <div v-if="evidenceImageUrl"><img :src="evidenceImageUrl" alt="转录证据图" style="max-width: 100%" /></div>
        <pre v-if="evidenceFileText" style="white-space: pre-wrap; word-break: break-all">{{ evidenceFileText }}</pre>
        <template #footer>
          <el-button @click="evidenceDialogVisible = false">关闭</el-button>
        </template>
      </el-dialog>
    </template>
  </div>
</template>

<script setup>
import GradingProgress from '@/components/GradingProgress.vue'
import GradingQuickDock from '@/components/GradingQuickDock.vue'
import SheetLoader from '@/components/SheetLoader.vue'
import VerdictSwatch from '@/components/VerdictSwatch.vue'
import { studentPageIdentityHint } from '@/utils/answerSheetPairing'
import HelpButton from '@/components/HelpButton.vue'
import { useAnswerSheetState } from './answer-sheet/useAnswerSheetState.js'
import { useStudentFileOrder } from './answer-sheet/useStudentFileOrder.js'
import { useAnswerSheetPickers } from './answer-sheet/useAnswerSheetPickers.js'
import { useBubbleGrids } from './answer-sheet/useBubbleGrids.js'
import { useAnswerSheetSession } from './answer-sheet/useAnswerSheetSession.js'
import { useAnswerSheetSetup } from './answer-sheet/useAnswerSheetSetup.js'
import { useAnswerSheetStudents } from './answer-sheet/useAnswerSheetStudents.js'
import { useAnswerSheetLifecycle } from './answer-sheet/useAnswerSheetLifecycle.js'

// The rest of the page lives in one composable per part (views/answer-sheet/use*.js). Each one gets ctx,
// holding what the earlier parts defined; it calls functions of later parts through ctx.
const ctx = {}
Object.assign(ctx, useAnswerSheetState(ctx))
Object.assign(ctx, useStudentFileOrder(ctx))
Object.assign(ctx, useAnswerSheetPickers(ctx))
Object.assign(ctx, useBubbleGrids(ctx))
Object.assign(ctx, useAnswerSheetSession(ctx))
Object.assign(ctx, useAnswerSheetSetup(ctx))
Object.assign(ctx, useAnswerSheetStudents(ctx))
Object.assign(ctx, useAnswerSheetLifecycle(ctx))
const {
  auth, route, busy, operationMessage, operationProgress, serviceState, hardwareState, restoreError,
  session, activeStage, answerSource, sourceFiles, answerFiles, layoutFiles, templateCodeInput,
  templateCodeHistory, batchSetup, paperTemplates, paperTemplateSources, paperRegistration, batches,
  studentFiles, studentOrderSource, studentPairingNote, classStudents, gradingDrafts,
  questionDrafts, answerRegions, identityRegions, layoutSummary, currentPageId, layoutImageUrl,
  layoutImageReady, layoutImageError, activeDraftIndex, gridSplitCount, evidenceDialogVisible,
  evidenceLoading, evidenceRecords, evidenceFileText, evidenceImageUrl, currentReviewPaperId,
  studentPageImageUrl, studentPageImageError, statusLabels, identityLabels, completedStage, isBatch,
  visibleStages, canCreate, serviceBlocksNewWork, workflowReadonly, canDelete, statusLabel,
  sourceLocked, answersLocked, layoutFrozen, canUploadSource, fileSelectionProcessing,
  answerSourceLabel, answerSpecs, currentPage, visibleAnswerRegions, visibleIdentityRegions,
  currentPageHasUnconfirmedRegions, currentDraftResult, currentDraftPages, currentReviewPageIndex,
  currentReviewPage, canArchiveDrafts, sourceRecognitionResumable, studentRecognitionResumable,
  gradingDraftResumable, layoutAcceleration, layoutAccelerationMessage, studentUpload,
  studentUploadActive, studentUploadProcessing, studentUploadCanProcess, studentFileSideLabel,
  studentFileDisplayPath, FilePicker, BboxFields, bubbleGridRows, bubbleGridsUnconfirmed,
  recognitionTagType, addGridCell, autoSplitGridCells, confirmBubbleGridRow, discardBubbleGridRow,
  openEvidenceViewer, openEvidenceFile, canOpenStage, openStage, formatBatchTime,
  paperTemplateLabel, paperSourceLabel, registerPaperTemplate, createSession, openBatch,
  closeSession, uploadSource, recognizeSource, confirmAnswers, templateHistoryLabel,
  useTemplateCode, locateLayoutRegions, confirmCurrentPageRegions, uploadLayout,
  processLayoutAcceleration, saveLayout, publishTemplateCode, freezeLayout, copyTemplateCode,
  removeSession, regionTypeOptions, syncRegionType, addAnswerRegion, addIdentityRegion, bboxStyle,
  moveStudentFile, removeStudentFile, processStudentUpload, uploadStudents, restartStudentUpload,
  confirmOrientation, recognizeStudents, confirmStudentReview, buildDraft, saveDraftDecision,
  archiveDrafts, setActiveDraft, moveReviewPage, openDetailPage, formatBytes, formatDraftMetric,
  reviewDetailQuestionNo, reviewDetailClass, reviewDetailLabel, reviewDetailAnalysis,
  renderReviewText,
} = ctx
</script>

<style scoped src="./answer-sheet/answer-sheet.css"></style>
