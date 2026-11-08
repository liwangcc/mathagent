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
import { computed, defineComponent, h, onBeforeUnmount, onMounted, ref, watch, shallowRef } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { notifyProcessingError } from '@/utils/errorNotification'
import GradingProgress from '@/components/GradingProgress.vue'
import GradingQuickDock from '@/components/GradingQuickDock.vue'
import SheetLoader from '@/components/SheetLoader.vue'
import VerdictSwatch from '@/components/VerdictSwatch.vue'
import { UploadFilled } from '@element-plus/icons-vue'
import { useRoute } from 'vue-router'
import api, {
  mergeHardwareAvailabilityStatus,
  normalizeHardwareAvailability,
} from '@/api'
import { appendDesktopCanonicalCandidates } from '@/utils/desktopTemplateCanonicalization'
import { compressImageFiles, validateUploadList } from '@/utils/uploadValidation'
import { pairFilesByFolder, pairFilesBySide, parseSideFilename, sourceDirectory, studentPageIdentityHint } from '@/utils/answerSheetPairing'
import { extractImagesFromDrop, extractImagesFromFileList } from '@/utils/fileExtraction'
import { getUploadSourcePath } from '@/utils/uploadSource'
import { useAuthStore } from '@/stores/auth'
import { renderLatex } from '@/utils/latex'
import HelpButton from '@/components/HelpButton.vue'
import {
  consumeSse,
  createSseEventError,
  createSseResponseError,
  isSseRequestCancelled,
  isSseServiceFault,
  SseTransportError,
} from '@/utils/sse'

const STORAGE_KEY = 'mathagent_answer_sheet_session_id'
const auth = useAuthStore()
const route = useRoute()
const busy = ref(false)
const operationMessage = ref('')
// { current, total } while a stream reports countable progress; drives the busy overlay's page counter.
const operationProgress = ref(null)
const serviceState = ref({ status: 'checking', label: '检测中', message: '正在检查答题卡批阅整条处理链路…' })
const hardwareState = ref(normalizeHardwareAvailability())
const SERVICE_FAULT_HOLD_MS = 60_000
const ACCELERATOR_STREAM_ERROR_STATES = {
  ACCELERATOR_WAITING: {
    hardwareStatus: 'waiting_accelerator',
    serviceStatus: 'queued',
    label: '等待加速节点',
    message: '正在等待客户端或机会式加速节点；已完成进度保留在当前会话，可继续处理。',
  },
  ACCELERATOR_UNAVAILABLE: {
    hardwareStatus: 'unavailable',
    serviceStatus: 'unavailable',
    label: '加速节点不可用',
    message: '加速节点当前不可用，已停止新的图像处理；已完成进度保留在当前会话。',
  },
  ACCELERATOR_FAULT: {
    hardwareStatus: 'fault',
    serviceStatus: 'fault',
    label: '加速节点故障',
    message: '加速节点处理发生故障，批改已停止；已完成进度保留在当前会话。',
  },
}
const availabilityPollTimer = shallowRef(null)
let availabilityCheckPending = false
let serviceFaultHoldUntil = 0
const restoreError = ref('')
const session = ref(null)
const activeStage = ref(0)
const answerSource = ref('embedded')
const sourceFiles = ref([])
const answerFiles = ref([])
const layoutFiles = ref([])
const templateCodeInput = ref('')
const templateCodeHistory = ref([])
const batchSetup = ref({ paper_template_code: '', answer_sheet_template_code: '' })
const paperTemplates = ref([])
const paperTemplateSources = ref([])
const paperRegistration = ref({ template_id: '', alias: '' })
const batches = ref([])
const studentFiles = ref([])
const studentOrderSource = ref({
  source: 'input_order',
  label: '选择顺序',
  message: '未找到可靠的文件序号或时间戳，暂保留本次选择顺序；请检查下面列表。',
})
// The server pairs by file name whenever every file is paired, whatever the
// list order, so the tags and note follow the names rather than the sort.
const studentPairingMode = computed(() => {
  if (pairFilesByFolder(studentFiles.value, captureSourcePath)) return 'folder'
  if (pairFilesBySide(studentFiles.value, captureFileName)) return 'side'
  return 'order'
})
const studentPairingNote = computed(() => ({
  folder: '每个文件夹恰有两张图片，系统按文件夹配对正反面（按模板页型区分）；文件夹名或文件名能在当前班级名单中唯一对应学生时直接确认身份，否则沿用同组已识别的一面。',
  side: '文件名均带正面/反面标记且两两成对，系统按文件名配对正反面；文件名能在当前班级名单中唯一对应学生时直接确认身份，否则沿用同组已识别的一面。',
  order: '若每位学生的文件名或文件夹名都能在当前班级名单中唯一对应且恰有两页，系统按学生配对；否则按相邻页建立正反面页对，正面身份确认后传给同组背面。文件名不能确定身份时，请按顺序上传同一学生的正面和背面（正面在前）。',
}[studentPairingMode.value]))
const fileSelectionProcessingCount = ref(0)
const classStudents = ref([])
const gradingDrafts = ref([])
const questionDrafts = ref([])
const answerRegions = ref([])
const identityRegions = ref([])
const layoutSummary = ref({})
const currentPageId = ref('')
const layoutImageUrl = ref('')
const layoutImageReady = ref(false)
const layoutImageError = ref('')
const activeDraftIndex = ref(0)
const gridSplitCount = ref(4)
const evidenceDialogVisible = ref(false)
const evidenceLoading = ref(false)
const evidenceRecords = ref([])
const evidencePaperId = ref('')
const evidenceFileText = ref('')
const evidenceImageUrl = ref('')
const bubbleGridRows = computed(() => session.value?.bubble_grids || [])
const bubbleGridsUnconfirmed = computed(() => bubbleGridRows.value.filter(grid => grid.state !== 'confirmed').length)

function recognitionTagType(answer) {
  const outcome = String(answer.outcome || '')
  if (outcome === 'correct') return 'success'
  if (outcome === 'wrong') return 'danger'
  if (outcome === 'blank') return 'info'
  return 'warning'
}

function addGridCell(grid) {
  if (!Array.isArray(grid.cells)) grid.cells = []
  if (grid.cells.length >= 8) return
  grid.cells.push({ option: '', bbox: [...(grid.grid_bbox || [0, 0, 0, 0])] })
}

function autoSplitGridCells(grid) {
  const [x, y, w, h] = (grid.grid_bbox || []).map(Number)
  const count = Math.min(8, Math.max(2, Number(gridSplitCount.value) || 4))
  if (!w || !h) {
    ElMessage.warning('请先填写整格范围')
    return
  }
  const letters = 'ABCDEFGH'
  grid.cells = Array.from({ length: count }, (_, index) => {
    const vertical = grid.grid_order === 'top_to_bottom'
    const cellW = vertical ? w : Math.floor(w / count)
    const cellH = vertical ? Math.floor(h / count) : h
    const cellX = vertical ? x : x + Math.floor((w / count) * index)
    const cellY = vertical ? y + Math.floor((h / count) * index) : y
    return { option: letters[index] || String(index + 1), bbox: [cellX, cellY, cellW, cellH] }
  })
}

async function confirmBubbleGridRow(grid) {
  const payload = {
    question_no: String(grid.question_no || ''),
    subquestion_no: String(grid.subquestion_no || ''),
    grid_order: grid.grid_order === 'top_to_bottom' ? 'top_to_bottom' : 'left_to_right',
    cells: (grid.cells || []).map(cell => ({
      option: String(cell.option || '').slice(0, 1),
      bbox: (cell.bbox || []).map(value => Math.round(Number(value))),
    })),
  }
  const result = await run(() => api.answerSheet.confirmBubbleGrids(session.value.session_id, [payload]), '涂卡栅格已确认')
  applySession(result?.session ? result.session : result)
}

async function discardBubbleGridRow(grid) {
  const payload = {
    question_no: String(grid.question_no || ''),
    subquestion_no: String(grid.subquestion_no || ''),
  }
  const result = await run(() => api.answerSheet.discardBubbleGrids(session.value.session_id, [payload]), '涂卡栅格已丢弃')
  applySession(result?.session ? result.session : result)
}

async function openEvidenceViewer(paperId) {
  evidencePaperId.value = paperId
  evidenceDialogVisible.value = true
  evidenceLoading.value = true
  evidenceRecords.value = []
  evidenceFileText.value = ''
  if (evidenceImageUrl.value) { URL.revokeObjectURL(evidenceImageUrl.value); evidenceImageUrl.value = '' }
  try {
    const data = await api.answerSheet.studentEvidenceIndex(session.value.session_id, paperId)
    evidenceRecords.value = data?.records || []
  } catch {
    evidenceRecords.value = []
  } finally {
    evidenceLoading.value = false
  }
}

async function openEvidenceFile(record, file) {
  const questionNo = (record.question_nos || [])[0] || ''
  try {
    const value = await api.answerSheet.studentEvidenceFile(session.value.session_id, evidencePaperId.value, questionNo, file.file)
    if (/\.png$/i.test(file.file)) {
      if (evidenceImageUrl.value) URL.revokeObjectURL(evidenceImageUrl.value)
      evidenceFileText.value = ''
      evidenceImageUrl.value = value
    } else {
      if (evidenceImageUrl.value) { URL.revokeObjectURL(evidenceImageUrl.value); evidenceImageUrl.value = '' }
      evidenceFileText.value = value
    }
  } catch {
    ElMessage.error('证据文件读取失败')
  }
}
const currentReviewPaperId = ref('')
const studentPageImageUrl = ref('')
const studentPageImageError = ref('')
const studentImageRequestId = shallowRef(0)
const answerSheetStreamController = shallowRef(null)

const stages = [
  { title: '原始试卷', caption: '语义与答案来源' },
  { title: '教师确认', caption: '题目与评分规则' },
  { title: '空白答题卡', caption: '导入版面依据' },
  { title: '绑定冻结', caption: '区域与页型' },
  { title: '学生答题卡', caption: '识别与批阅' },
]
// Template-code batches take questions and answers from a frozen paper
// template, so the first two stages only show that binding.
const batchStages = [
  { title: '试卷模板', caption: '已冻结题目与答案' },
  { title: '题目答案', caption: '只读核对' },
  ...stages.slice(2),
]
const statusOrder = { draft: 0, source_uploaded: 0, failed: 0, source_recognized: 1, answers_confirmed: 2, layout_draft: 3, frozen: 4, recognizing: 4, review: 4, archived: 4 }
const statusLabels = { draft: '新建草稿', source_uploaded: '材料已上传', source_recognized: '题目已识别', answers_confirmed: '答案已确认', layout_draft: '版面编辑中', frozen: '版面已冻结', recognizing: '学生答题卡识别中', review: '等待复核', archived: '已归档', failed: '处理失败' }
const identityLabels = { name: '姓名', student_no: '学号', exam_no_text: '准考证号文字', qr: '二维码', barcode: '条形码', exam_no_marks: '准考证号涂格', page_number: '页码' }

const FilePicker = defineComponent({
  props: {
    modelValue: Array,
    title: String,
    note: String,
    disabled: Boolean,
    autoOrder: Boolean,
    // Folder, drag-and-drop and ZIP/RAR import through the shared extractor,
    // which keeps each image's relative path for pairing and identity.
    importArchives: Boolean,
    maxFiles: { type: Number, default: 200 },
  },
  emits: ['update:modelValue', 'order-detected'],
  setup(props, { emit }) {
    const selecting = ref(false)
    const dragover = ref(false)
    async function selectFiles(event) {
      const selected = [...(event.target.files || [])]
      event.target.value = ''
      if (selecting.value || props.disabled || !selected.length) return
      await acceptFiles(selected)
    }
    async function importFiles(load) {
      if (selecting.value || props.disabled) return
      selecting.value = true
      fileSelectionProcessingCount.value += 1
      let extracted
      try {
        extracted = await load()
      } catch (error) {
        ElMessage.error(error?.message || '文件导入失败')
        return
      } finally {
        fileSelectionProcessingCount.value = Math.max(0, fileSelectionProcessingCount.value - 1)
        selecting.value = false
      }
      if (!extracted.length) {
        ElMessage.warning('未找到任何图片文件')
        return
      }
      await acceptFiles(extracted)
    }
    function importInput(event) {
      const selected = [...(event.target.files || [])]
      event.target.value = ''
      if (selected.length) importFiles(() => extractImagesFromFileList(selected, { maxFiles: props.maxFiles }))
    }
    function importDrop(event) {
      event.preventDefault()
      dragover.value = false
      importFiles(() => extractImagesFromDrop(event, { maxFiles: props.maxFiles }))
    }
    async function acceptFiles(selected) {
      if (selected.length > props.maxFiles) {
        ElMessage.warning(`单次最多选择 ${props.maxFiles} 个文件`)
      }
      const supported = selected.slice(0, props.maxFiles).filter((file) => (
        ['image/jpeg', 'image/png'].includes(String(file?.type || '').toLowerCase())
      ))
      if (supported.length < Math.min(selected.length, props.maxFiles)) {
        ElMessage.warning('仅支持 JPG、JPEG 和 PNG 图片')
      }
      const result = validateUploadList(supported, { maxFiles: props.maxFiles })
      if (result.errors.length) ElMessage.warning(result.errors[0])
      if (!result.files.length) {
        emit('update:modelValue', [])
        return
      }

      const ordered = props.autoOrder
        ? sortCaptureFiles(result.files)
        : { files: result.files, source: 'input_order', label: '选择顺序', message: '' }
      if (props.autoOrder) {
        result.files = ordered.files
        emit('order-detected', ordered)
      }

      selecting.value = true
      fileSelectionProcessingCount.value += 1
      let failedCount = 0
      try {
        const processed = await compressImageFiles(result.files, 1920, 0.88, {
          onFileError: () => { failedCount += 1 },
        })
        emit('update:modelValue', processed)
        if (failedCount) {
          ElMessage.warning(`${failedCount} 张图片预处理失败，已保留原图；建议重新选择后再试`)
        }
      } catch {
        ElMessage.error('图片预处理未完成，请重新选择')
      } finally {
        fileSelectionProcessingCount.value = Math.max(0, fileSelectionProcessingCount.value - 1)
        selecting.value = false
      }
    }
    return () => props.importArchives ? h('div', {
      class: ['file-picker', 'file-picker-import', (props.disabled || selecting.value) && 'disabled', selecting.value && 'processing', props.modelValue?.length && 'has-files', dragover.value && 'is-dragover'],
      'aria-disabled': (props.disabled || selecting.value) ? 'true' : undefined,
      'aria-busy': selecting.value ? 'true' : undefined,
      onDragover: (event) => { event.preventDefault(); dragover.value = !props.disabled && !selecting.value },
      onDragleave: () => { dragover.value = false },
      onDrop: importDrop,
    }, [
      h(UploadFilled, { class: 'file-picker-icon', 'aria-hidden': 'true' }),
      h('strong', selecting.value ? '正在导入并处理图片' : (props.modelValue?.length ? `已选择 ${props.modelValue.length} 个文件` : props.title)),
      h('span', selecting.value ? '完成后即可上传' : props.note),
      h('div', { class: 'file-picker-actions' }, [
        h('label', { class: 'file-picker-button' }, ['选择文件', h('input', { type: 'file', accept: '.jpg,.jpeg,.png,.zip,.rar', multiple: true, disabled: props.disabled || selecting.value, onChange: importInput })]),
        h('label', { class: 'file-picker-button' }, ['选择文件夹', h('input', { type: 'file', webkitdirectory: true, disabled: props.disabled || selecting.value, onChange: importInput })]),
      ]),
    ]) : h('label', {
      class: ['file-picker', (props.disabled || selecting.value) && 'disabled', selecting.value && 'processing', props.modelValue?.length && 'has-files'],
      'aria-disabled': (props.disabled || selecting.value) ? 'true' : undefined,
      'aria-busy': selecting.value ? 'true' : undefined,
    }, [
      h('input', { type: 'file', accept: 'image/jpeg,image/png', multiple: true, disabled: props.disabled || selecting.value, onChange: selectFiles }),
      h(UploadFilled, { class: 'file-picker-icon', 'aria-hidden': 'true' }),
      h('strong', selecting.value ? '正在逐张处理图片' : (props.modelValue?.length ? `已选择 ${props.modelValue.length} 个文件` : props.title)),
      h('span', selecting.value ? '完成后即可上传' : props.note),
    ])
  },
})

function captureFileName(file) {
  const raw = file?.raw || file
  return String(raw?.name || file?.name || '')
}

function captureFileBaseName(file) {
  return captureFileName(file)
    .replace(/\\/g, '/')
    .split('/')
    .pop()
    .replace(/\.ocr-ready-v1\.jpg$/i, '')
    .replace(/\.[^.]+$/, '')
}

function captureFileTimestamp(file) {
  const name = captureFileBaseName(file)
  const match = name.match(/(?:^|[^0-9])(20[0-9]{2})[-_.]?([0-1][0-9])[-_.]?([0-3][0-9])[T _-]?([0-2][0-9])[-_.]?([0-5][0-9])[-_.]?([0-5][0-9])(?:[-_.]([0-9]{1,6}))?(?:[^0-9]|$)/)
  if (!match) return null
  const [, year, month, day, hour, minute, second, fraction = ''] = match
  const date = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second)))
  if (
    date.getUTCFullYear() !== Number(year)
    || date.getUTCMonth() !== Number(month) - 1
    || date.getUTCDate() !== Number(day)
    || date.getUTCHours() !== Number(hour)
    || date.getUTCMinutes() !== Number(minute)
    || date.getUTCSeconds() !== Number(second)
  ) return null
  return `${year}${month}${day}${hour}${minute}${second}${fraction.padEnd(6, '0')}`
}

function captureFileSequence(file) {
  const name = captureFileBaseName(file)
  const matches = [...name.matchAll(/[0-9]+/g)]
  if (!matches.length) return null
  return matches[matches.length - 1][0]
}

function compareNumericStrings(left, right) {
  const a = String(left).replace(/^0+(?=\d)/, '')
  const b = String(right).replace(/^0+(?=\d)/, '')
  if (a.length !== b.length) return a.length - b.length
  return a < b ? -1 : (a > b ? 1 : 0)
}

function sortByCaptureKey(files, getKey, compareKey) {
  const entries = files.map((file, index) => ({ file, index, key: getKey(file) }))
  if (entries.some((entry) => entry.key == null)) return null
  const keys = entries.map((entry) => String(entry.key))
  if (new Set(keys).size !== entries.length) return null
  return entries
    .sort((left, right) => compareKey(left.key, right.key) || left.index - right.index)
    .map((entry) => entry.file)
}

function studentFileSideLabel(file, index) {
  if (studentPairingMode.value === 'folder') return '按页型区分'
  if (studentPairingMode.value === 'side') {
    return parseSideFilename(captureFileName(file))?.side === 'back' ? '背面' : '正面'
  }
  return index % 2 ? '背面' : '正面'
}

// Import path (folder/archive member) for display and identity only.
function captureSourcePath(file) {
  try {
    return getUploadSourcePath(file)
  } catch {
    return ''
  }
}

function studentFileDisplayPath(file) {
  return captureSourcePath(file) || captureFileName(file)
}

function sortCaptureFiles(files) {
  const byFolder = pairFilesByFolder(files, captureSourcePath)
  if (byFolder) {
    return {
      files: byFolder,
      source: 'folder_pairs',
      label: '文件夹配对',
      message: '系统已按文件夹排序。',
    }
  }
  const byName = pairFilesBySide(files, captureFileName)
  if (byName) {
    return {
      files: byName,
      source: 'filename_side_pairs',
      label: '文件名正反面',
      message: '系统已按文件名中的正面/反面标记排序。',
    }
  }
  if (files.some((file) => sourceDirectory(captureSourcePath(file)))) {
    const folders = new Map()
    for (const file of files) {
      const directory = sourceDirectory(captureSourcePath(file))
      folders.set(directory, [...(folders.get(directory) || []), file])
    }
    return {
      files: [...folders.keys()]
        .sort((left, right) => left.localeCompare(right, 'zh-CN', { numeric: true }))
        .flatMap((directory) => sortFlatCaptureFiles(folders.get(directory)).files),
      source: 'folder_order',
      label: '文件夹分组',
      message: '系统已按文件夹分组，文件夹内按文件序号或时间排序；请核对下面列表。',
    }
  }
  return sortFlatCaptureFiles(files)
}

function sortFlatCaptureFiles(files) {
  const byTimestamp = sortByCaptureKey(files, captureFileTimestamp, (left, right) => left < right ? -1 : (left > right ? 1 : 0))
  if (byTimestamp) {
    return {
      files: byTimestamp,
      source: 'filename_timestamp',
      label: '文件名时间戳',
      message: '系统已按文件名中的拍摄时间戳自动排序。',
    }
  }

  const bySequence = sortByCaptureKey(files, captureFileSequence, compareNumericStrings)
  if (bySequence) {
    return {
      files: bySequence,
      source: 'filename_sequence',
      label: '文件名序号',
      message: '系统已按文件名中的连续序号自动排序。',
    }
  }

  const byModifiedTime = sortByCaptureKey(
    files,
    (file) => {
      const raw = file?.raw || file
      const value = Number(raw?.lastModified)
      return Number.isFinite(value) && value > 0 ? String(value).padStart(16, '0') : null
    },
    (left, right) => left < right ? -1 : (left > right ? 1 : 0),
  )
  if (byModifiedTime) {
    return {
      files: byModifiedTime,
      source: 'file_modified_time',
      label: '文件时间',
      message: '系统已按文件修改时间自动排序；如文件被复制或批量导出，请核对下面列表。',
    }
  }

  return {
    files: [...files],
    source: 'input_order',
    label: '选择顺序',
    message: '未找到可靠的文件序号或时间戳，暂保留本次选择顺序；请检查下面列表。',
  }
}
const BboxFields = defineComponent({
  props: { modelValue: Array, disabled: Boolean }, emits: ['update:modelValue'],
  setup(props, { emit }) {
    const labels = ['左', '上', '右', '下']
    return () => h('div', { class: 'bbox-fields' }, labels.map((label, index) => h('label', [
      h('span', label), h('input', { type: 'number', min: 0, max: 1000, value: props.modelValue?.[index], disabled: props.disabled, onInput: (event) => { const next = [...props.modelValue]; next[index] = Number(event.target.value); emit('update:modelValue', next) } }),
    ])))
  },
})

const completedStage = computed(() => statusOrder[session.value?.status] ?? 0)
const isBatch = computed(() => Boolean(session.value?.batch_contract_version))
const visibleStages = computed(() => (isBatch.value ? batchStages : stages))
const canCreate = computed(() => !!auth.currentClassId && !!batchSetup.value.paper_template_code)
const hardwareBlocksNewImageWork = computed(() => ['unavailable', 'fault'].includes(hardwareState.value.status))
const serviceBlocksNewWork = computed(() => (
  ['checking', 'unavailable', 'fault'].includes(serviceState.value.status)
  || hardwareBlocksNewImageWork.value
))
const workflowReadonly = computed(() => serviceBlocksNewWork.value && session.value?.status !== 'archived')
const canDelete = computed(() => !serviceBlocksNewWork.value && ['draft', 'failed'].includes(session.value?.status))
const statusLabel = computed(() => statusLabels[session.value?.status] || session.value?.status)
const sourceLocked = computed(() => !['draft', 'failed'].includes(session.value?.status))
const answersLocked = computed(() => !['source_recognized', 'failed'].includes(session.value?.status))
const layoutFrozen = computed(() => ['frozen', 'recognizing', 'review', 'archived'].includes(session.value?.status))
const canUploadSource = computed(() => sourceFiles.value.length && (answerSource.value !== 'separate' || answerFiles.value.length))
const fileSelectionProcessing = computed(() => fileSelectionProcessingCount.value > 0)
const answerSourceLabel = computed(() => ({ embedded: '试卷内含答案', separate: '另传答案文件', ai_generated: 'AI 生成候选' })[answerSource.value])
const answerSpecs = computed(() => session.value?.answer_spec_snapshot?.length ? session.value.answer_spec_snapshot : questionDrafts.value)
const currentPage = computed(() => session.value?.answer_sheet_pages?.find(page => page.page_id === currentPageId.value))
const visibleAnswerRegions = computed(() => answerRegions.value.filter(region => region.page_id === currentPageId.value))
const visibleIdentityRegions = computed(() => identityRegions.value.filter(region => region.page_id === currentPageId.value))
const currentPageHasUnconfirmedRegions = computed(() => (
  !layoutFrozen.value
  && session.value?.layout_localization?.provider_route === 'qwen3.8max'
  && [...visibleAnswerRegions.value, ...visibleIdentityRegions.value].some(region => !region.teacher_confirmed)
))
const currentDraft = computed(() => gradingDrafts.value[activeDraftIndex.value] || {})
const currentDraftResult = computed(() => currentDraft.value.result || {})
const currentDraftPages = computed(() => (currentDraft.value.paper_ids || []).map((paperId) => (
  session.value?.student_pages?.find(page => page.paper_id === paperId) || { paper_id: paperId }
)))
const currentReviewPageIndex = computed(() => {
  const index = currentDraftPages.value.findIndex(page => page.paper_id === currentReviewPaperId.value)
  return index >= 0 ? index : 0
})
const currentReviewPage = computed(() => currentDraftPages.value[currentReviewPageIndex.value] || null)
const canArchiveDrafts = computed(() => gradingDrafts.value.length > 0 && gradingDrafts.value.every(
  draft => Number(draft.result?.needs_review_count || 0) === 0,
))
const sourceRecognitionResumable = computed(() => [
  ...(session.value?.source_exam_pages || []),
  ...(session.value?.answer_pages || []),
].some(page => page?.recognition_checkpoint))
const studentRecognitionResumable = computed(() => (session.value?.student_pages || []).some(
  page => page?.recognition_checkpoint,
))
const gradingDraftResumable = computed(() => Boolean(
  session.value?.asset_lifecycle?.grading_draft_checkpoint?.drafts?.length,
))
const layoutAcceleration = computed(() => {
  const state = session.value?.asset_lifecycle?.layout_acceleration
  return state && typeof state === 'object' ? state : null
})
const layoutAccelerationMessage = computed(() => String(
  session.value?.error
  || ({
    waiting_accelerator: '页面已保存，正在等待客户端或机会式加速节点。',
    unavailable: '当前没有可用加速节点，服务器不会回退处理图像。',
    fault: '加速图像处理发生故障，页面仍已保存。',
  }[layoutAcceleration.value?.status])
  || '页面图像处理尚未完成。',
))
const studentUpload = computed(() => session.value?.student_upload || {})
const studentUploadActive = computed(() => ['uploading', 'processing'].includes(
  String(studentUpload.value.status || ''),
))
const studentUploadProcessing = computed(() => studentUpload.value.status === 'processing')
const studentUploadCanProcess = computed(() => studentUploadActive.value && (
  studentUploadProcessing.value || Number(studentUpload.value.uploaded_count || 0) > 0
))

function normalizeQuestion(raw) {
  const steps = Array.isArray(raw.solution_steps) ? raw.solution_steps : []
  const type = ['choice', 'fill', 'solve'].includes(raw.question_type) ? raw.question_type : 'solve'
  const choiceMode = type === 'choice' && ['single', 'multiple'].includes(raw.choice_mode) ? raw.choice_mode : 'unknown'
  return { ...raw, question_no: String(raw.question_no || raw.question_number || ''), question_text: String(raw.question_text || raw.content || ''), question_type: type, choice_mode: choiceMode, standard_answer: String(raw.standard_answer || raw.answer || ''), max_score: raw.max_score ?? null, grading_method: raw.grading_method || ({ choice: 'exact', fill: 'equivalent', solve: '' }[type]), solution_steps_text: steps.join('\n') }
}
function applySession(data, { chooseStage = true } = {}) {
  session.value = data
  if (data.shared_template_code) templateCodeInput.value = data.shared_template_code
  answerSource.value = data.answer_source || 'embedded'
  questionDrafts.value = (data.answer_spec_snapshot?.length ? data.answer_spec_snapshot : data.source_question_drafts || []).map(normalizeQuestion)
  answerRegions.value = structuredClone(data.answer_regions || [])
  identityRegions.value = structuredClone(data.identity_regions || [])
  gradingDrafts.value = structuredClone(data.grouping_decisions || [])
  activeDraftIndex.value = Math.min(activeDraftIndex.value, Math.max(gradingDrafts.value.length - 1, 0))
  const draftPages = gradingDrafts.value[activeDraftIndex.value]?.paper_ids || []
  currentReviewPaperId.value = draftPages.includes(currentReviewPaperId.value) ? currentReviewPaperId.value : (draftPages[0] || '')
  layoutSummary.value = summarizeLayout()
  currentPageId.value = currentPageId.value && data.answer_sheet_pages?.some(page => page.page_id === currentPageId.value) ? currentPageId.value : (data.answer_sheet_pages?.[0]?.page_id || '')
  localStorage.setItem(STORAGE_KEY, data.session_id)
  if (chooseStage) activeStage.value = completedStage.value
}
function summarizeLayout() {
  const specs = session.value?.answer_spec_snapshot || []
  const required = new Set(specs.map(item => String(item.question_no)))
  const bound = new Set(answerRegions.value.filter(item => item.teacher_confirmed).map(item => String(item.question_no)))
  const conflicts = []
  for (let i = 0; i < answerRegions.value.length; i += 1) for (let j = i + 1; j < answerRegions.value.length; j += 1) {
    const a = answerRegions.value[i]; const b = answerRegions.value[j]
    if (a.page_id !== b.page_id || a.question_no === b.question_no) continue
    const overlap = Math.max(0, Math.min(a.bbox[2], b.bbox[2]) - Math.max(a.bbox[0], b.bbox[0])) * Math.max(0, Math.min(a.bbox[3], b.bbox[3]) - Math.max(a.bbox[1], b.bbox[1]))
    if (overlap) conflicts.push([a.region_id, b.region_id])
  }
  const missing = [...required].filter(no => !bound.has(no))
  return { question_count: required.size, bound_count: bound.size, missing_questions: missing, conflicts, identity_region_count: identityRegions.value.length, can_freeze: !missing.length && !conflicts.length && identityRegions.value.length > 0 && identityRegions.value.every(item => item.teacher_confirmed) }
}
function canOpenStage(index) { return !!session.value && index <= Math.max(completedStage.value, 0) }
function openStage(index) { if (canOpenStage(index)) activeStage.value = index }
function markServiceFault(message) {
  serviceFaultHoldUntil = Date.now() + SERVICE_FAULT_HOLD_MS
  serviceState.value = {
    status: 'fault',
    label: '链路故障',
    message: message || '答题卡批阅链路发生故障，已保存内容仍可查看；请稍后重试。',
  }
}
function requireAnswerSheetService() {
  if (!serviceBlocksNewWork.value) return true
  ElMessage.error(serviceState.value.message || '答题卡批阅链路当前不可用，请稍后再试。')
  return false
}
function normalizeServiceError(error) {
  const detail = error?.response?.data?.detail
  const detailObject = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail : null
  const axiosCode = String(error?.code || '')
  const transportFailure = ['ECONNABORTED', 'ETIMEDOUT', 'ERR_NETWORK'].includes(axiosCode)
  return {
    message: String(error?.message || ''),
    status: Number(error?.response?.status || error?.status) || 0,
    code: String(error?.response?.data?.code || error?.response?.data?.error || detailObject?.code || detailObject?.error || axiosCode),
    sseFailureKind: transportFailure ? 'transport' : error?.sseFailureKind,
  }
}
function applyAcceleratorStreamError(error) {
  const acceleratorState = ACCELERATOR_STREAM_ERROR_STATES[String(error?.code || '').trim().toUpperCase()]
  if (!acceleratorState) return false
  const serverMessage = String(error?.message || '').trim()
  const message = serverMessage && !/保存|保留/.test(serverMessage)
    ? `${serverMessage} 已完成进度保留在当前会话，可在节点恢复后继续处理。`
    : (serverMessage || acceleratorState.message)
  hardwareState.value = normalizeHardwareAvailability({
    hardware: { status: acceleratorState.hardwareStatus, message },
  })
  if (acceleratorState.serviceStatus === 'fault') {
    serviceFaultHoldUntil = Date.now() + SERVICE_FAULT_HOLD_MS
  }
  serviceState.value = {
    status: acceleratorState.serviceStatus,
    label: acceleratorState.label,
    message,
  }
  if (acceleratorState.serviceStatus === 'queued') ElMessage.warning(message)
  else notifyProcessingError(error, message)
  return true
}
async function run(task, success) {
  busy.value = true
  try {
    const data = await task()
    if (success) ElMessage.success(success)
    return data
  } catch (error) {
    if (isSseServiceFault(normalizeServiceError(error))) {
      markServiceFault('答题卡批阅链路中断，已保存内容仍可查看；请稍后重试，若持续发生请联系管理员。')
    }
    throw error
  } finally {
    busy.value = false
  }
}
async function runAnswerSheetStream(request, fallbackMessage, successMessage, onProgress) {
  if (!requireAnswerSheetService()) return null
  if (busy.value) return null
  busy.value = true
  operationMessage.value = fallbackMessage
  const controller = new AbortController()
  answerSheetStreamController.value = controller
  try {
    const response = await request(controller.signal)
    if (!response.ok) throw await createSseResponseError(response, fallbackMessage)
    let completed = null
    await consumeSse(response, (event, data) => {
      if (event === 'progress') {
        const current = Number(data?.current)
        const total = Number(data?.total)
        operationMessage.value = data?.message || fallbackMessage
        operationProgress.value = Number.isFinite(current) && Number.isFinite(total) && total > 0
          ? { current, total }
          : null
        if (onProgress) onProgress(data)
      } else if (event === 'done') {
        completed = data
      } else if (event === 'error') {
        throw createSseEventError(data, fallbackMessage)
      }
    })
    if (!completed) throw new SseTransportError(`${fallbackMessage}连接已结束，但未收到完成状态`)
    if (successMessage) ElMessage.success(successMessage)
    return completed
  } catch (error) {
    if (isSseRequestCancelled(error, controller.signal)) return null
    try {
      const latest = await api.answerSheet.getSession(session.value.session_id)
      applySession(latest, { chooseStage: false })
    } catch {
      // Keep the last local snapshot if the status endpoint is also unreachable.
    }
    const normalizedError = normalizeServiceError(error)
    const acceleratorHandled = applyAcceleratorStreamError(normalizedError)
    if (!acceleratorHandled && isSseServiceFault(normalizedError, { signal: controller.signal })) {
      markServiceFault('答题卡批阅链路中断，已完成进度已保存；请稍后重试，若持续发生请联系管理员。')
    }
    if (!acceleratorHandled) notifyProcessingError(error, error?.message || fallbackMessage)
    return null
  } finally {
    if (answerSheetStreamController.value === controller) answerSheetStreamController.value = null
    operationMessage.value = ''
    operationProgress.value = null
    busy.value = false
  }
}
async function refreshAvailability() {
  if (availabilityCheckPending) return
  availabilityCheckPending = true
  try {
    const state = await api.answerSheet.availability()
    const hardware = normalizeHardwareAvailability(state)
    hardwareState.value = hardware
    const labels = { available: '可用', queued: '需排队', unavailable: '暂不可用', fault: '链路故障' }
    const messages = {
      available: '答题卡批阅服务当前可用。',
      queued: '答题卡批阅当前需排队；可以准备三类材料，提交后系统会等待准入。',
      unavailable: '答题卡批阅链路当前不可用，请稍后重试或联系管理员。',
      fault: '答题卡批阅链路发生故障，请稍后重试或联系管理员。',
    }
    if (!labels[state?.status]) {
      markServiceFault('答题卡批阅链路状态响应异常，请稍后重试或联系管理员。')
      return
    }
    const effectiveStatus = mergeHardwareAvailabilityStatus(state.status, hardware.status)
    const hardwareServiceStatus = mergeHardwareAvailabilityStatus('available', hardware.status)
    const effectiveMessage = String(hardwareServiceStatus === effectiveStatus
      ? (hardware.message || state.message || messages[effectiveStatus])
      : (state.message || hardware.message || messages[effectiveStatus]))
    if (
      ['available', 'queued'].includes(effectiveStatus)
      && serviceState.value.status === 'fault'
      && Date.now() < serviceFaultHoldUntil
    ) return
    if (effectiveStatus === 'fault') {
      markServiceFault(effectiveMessage || messages.fault)
      return
    }
    serviceState.value = {
      status: effectiveStatus,
      label: String(effectiveStatus === state.status ? (state.label || labels[effectiveStatus]) : labels[effectiveStatus]),
      message: effectiveMessage,
    }
  } catch {
    hardwareState.value = normalizeHardwareAvailability({
      hardware: { status: 'fault', message: '无法获取本地加速节点状态。' },
    })
    markServiceFault('无法获取答题卡批阅链路状态，请检查网络或联系管理员。')
  } finally {
    availabilityCheckPending = false
  }
}
function formatBatchTime(value) {
  const date = new Date(value || '')
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', { hour12: false })
}
function paperTemplateLabel(item) {
  const name = String(item?.alias || item?.exam_name || '').trim()
  return `${name ? `${name}　` : ''}${item.template_code}　${item.question_count}题`
}
function paperSourceLabel(item) {
  const name = String(item?.exam_name || '').trim() || `详细批改模板 ${item.template_id}`
  const confirmed = formatBatchTime(item?.confirmed_at)
  return `${name}　${item.question_count}题${confirmed ? `　确认于 ${confirmed}` : ''}`
}
async function loadBatchChoices() {
  const [templates, sources, listed] = await Promise.allSettled([
    api.answerSheet.paperTemplates(),
    api.answerSheet.paperTemplateSources(),
    api.answerSheet.listSessions(),
  ])
  const items = (result, key) => (
    result.status === 'fulfilled' && Array.isArray(result.value?.[key]) ? result.value[key] : []
  )
  paperTemplates.value = items(templates, 'templates')
  paperTemplateSources.value = items(sources, 'sources')
  batches.value = items(listed, 'batches')
}
async function registerPaperTemplate() {
  if (!requireAnswerSheetService()) return
  const { template_id: templateId, alias } = paperRegistration.value
  if (!templateId) return
  const data = await run(
    () => api.answerSheet.registerPaperTemplate(templateId, String(alias || '').trim()),
    '试卷模板已登记，可在多个批次复用',
  )
  paperRegistration.value = { template_id: '', alias: '' }
  await loadBatchChoices()
  if (data?.template?.template_code) batchSetup.value.paper_template_code = data.template.template_code
}
async function createSession() {
  if (!requireAnswerSheetService()) return
  // The batch inherits the global current class on the server; there is no
  // per-batch class or grading-type choice.  Questions and answers come from
  // the selected frozen paper template.
  const payload = { paper_template_code: batchSetup.value.paper_template_code }
  const sheetCode = String(batchSetup.value.answer_sheet_template_code || '').trim().toUpperCase()
  if (sheetCode) payload.answer_sheet_template_code = sheetCode
  const data = await run(() => api.answerSheet.createSession(payload), '答题卡批次已创建')
  batchSetup.value = { paper_template_code: '', answer_sheet_template_code: '' }
  applySession(data)
  if (sheetCode) void loadTemplateCodeHistory()
}
async function openBatch(sessionId) {
  const data = await run(() => api.answerSheet.getSession(sessionId))
  restoreError.value = ''
  applySession(data)
}
function closeSession() {
  // Only leaves the batch view; the batch stays stored and listed.
  localStorage.removeItem(STORAGE_KEY)
  session.value = null
  activeStage.value = 0
  restoreError.value = ''
  void loadBatchChoices()
}
async function uploadSource() {
  if (!requireAnswerSheetService()) return
  const form = new FormData()
  sourceFiles.value.forEach(file => form.append('source_files', file))
  answerFiles.value.forEach(file => form.append('answer_files', file))
  form.append('answer_source', answerSource.value)
  applySession(await run(() => api.answerSheet.uploadSource(session.value.session_id, form), '材料已安全保存'))
}
async function recognizeSource() {
  const data = await runAnswerSheetStream(
    signal => api.answerSheet.recognizeSourceStream(session.value.session_id, signal),
    '正在识别原始试卷',
    '识别完成，请教师检查',
  )
  if (!data?.session) return
  applySession(data.session)
  activeStage.value = 1
}
async function confirmAnswers() { const questions = questionDrafts.value.map(({ solution_steps_text, ...item }) => ({ ...item, solution_steps: solution_steps_text.split('\n').map(value => value.trim()).filter(Boolean) })); applySession(await run(() => api.answerSheet.confirmAnswers(session.value.session_id, questions), '题目与答案已确认')); activeStage.value = 2 }
function templateHistoryLabel(item) {
  const counts = item?.question_counts || {}
  const types = [
    counts.choice ? `选${counts.choice}` : '',
    counts.fill ? `填${counts.fill}` : '',
    counts.solve ? `解${counts.solve}` : '',
  ].filter(Boolean).join(' · ')
  return `${item.template_code}　${item.page_count}页 / ${item.question_count}题${types ? `　${types}` : ''}`
}
async function loadTemplateCodeHistory() {
  try {
    const data = await api.answerSheet.templateHistory()
    templateCodeHistory.value = Array.isArray(data?.templates) ? data.templates : []
  } catch {
    templateCodeHistory.value = []
  }
}
async function useTemplateCode() {
  if (!requireAnswerSheetService()) return
  const code = String(templateCodeInput.value || '').trim().toUpperCase()
  if (!code) return
  const response = await run(
    () => api.answerSheet.useTemplateCode(session.value.session_id, code),
    '模板码已套用，无需模型重新定位',
  )
  applySession(response.session, { chooseStage: false })
  templateCodeInput.value = response.template.template_code
  await loadTemplateCodeHistory()
  activeStage.value = 3
}
async function locateLayoutRegions() {
  operationMessage.value = 'qwen3.8max 正在粗定位答题区域'
  try {
    const response = await run(() => api.answerSheet.locateLayout(session.value.session_id))
    applySession(response.session, { chooseStage: false })
    layoutSummary.value = response.summary
    const missing = response.localization?.missing_questions || []
    if (missing.length) {
      ElMessage.warning(`粗定位已完成，仍需手工补充第 ${missing.join('、')} 题`)
    } else {
      ElMessage.success('粗定位已完成，请逐项检查并勾选教师已确认')
    }
    return response
  } finally {
    operationMessage.value = ''
  }
}
function confirmCurrentPageRegions() {
  for (const region of [...visibleAnswerRegions.value, ...visibleIdentityRegions.value]) {
    region.teacher_confirmed = true
  }
  ElMessage.success('本页定位已确认，保存版面后即可冻结')
}
async function uploadLayout() {
  if (!requireAnswerSheetService()) return
  const data = await run(async () => {
    const form = new FormData()
    layoutFiles.value.forEach(file => form.append('files', file))
    await appendDesktopCanonicalCandidates(form, layoutFiles.value)
    return api.answerSheet.uploadLayout(session.value.session_id, form)
  })
  if (!data) return
  applySession(data)
  if (data.accelerator || data.asset_lifecycle?.layout_acceleration) {
    ElMessage.warning(data.accelerator?.message || layoutAccelerationMessage.value)
    activeStage.value = 2
    return
  }
  ElMessage.success('空白答题卡已上传，正在进行粗定位')
  activeStage.value = 3
  try {
    await locateLayoutRegions()
  } catch {
    ElMessage.warning('空白页已保留，可稍后重试粗定位或直接手工绑定')
  }
}
async function processLayoutAcceleration() {
  if (!session.value?.session_id || busy.value) return
  const data = await run(
    () => api.answerSheet.processLayout(session.value.session_id),
  )
  if (!data) return
  applySession(data, { chooseStage: false })
  if (data.accelerator || data.asset_lifecycle?.layout_acceleration) {
    ElMessage.warning(data.accelerator?.message || layoutAccelerationMessage.value)
    activeStage.value = 2
    return
  }
  ElMessage.success('空白答题卡页面处理完成，正在进行粗定位')
  activeStage.value = 3
  try {
    await locateLayoutRegions()
  } catch {
    ElMessage.warning('空白页已保留，可稍后重试粗定位或直接手工绑定')
  }
}
async function saveLayout() { const response = await run(() => api.answerSheet.saveLayout(session.value.session_id, { answer_regions: answerRegions.value, identity_regions: identityRegions.value }), '版面草稿已保存'); applySession(response.session, { chooseStage: false }); layoutSummary.value = response.summary }
async function publishTemplateCode(advance = false) {
  const response = await run(
    () => api.answerSheet.publishTemplateCode(session.value.session_id),
    '模板码已生成并加入历史记录',
  )
  applySession(response.session, { chooseStage: false })
  templateCodeInput.value = response.template.template_code
  await loadTemplateCodeHistory()
  if (advance) activeStage.value = 4
  return response
}
async function freezeLayout() {
  applySession(await run(() => api.answerSheet.freezeLayout(session.value.session_id), '版面已冻结'), { chooseStage: false })
  try {
    await publishTemplateCode(true)
  } catch {
    activeStage.value = 3
    ElMessage.warning('版面已经冻结，但模板码生成失败；可在当前页面重试')
  }
}
async function copyTemplateCode() {
  const code = String(session.value?.shared_template_code || '')
  if (!code) return
  try {
    await navigator.clipboard.writeText(code)
    ElMessage.success('模板码已复制')
  } catch {
    ElMessage.warning(`请手动复制：${code}`)
  }
}
function updateStudentUploadProgress(data) {
  if (data?.session?.session_id) {
    applySession(data.session, { chooseStage: false })
    return
  }
  if (!session.value || !studentUpload.value.upload_id) return
  const progress = data?.student_upload && typeof data.student_upload === 'object'
    ? data.student_upload
    : data
  const next = { ...studentUpload.value, status: 'processing' }
  for (const field of ['uploaded_count', 'processed_count', 'total_bytes']) {
    const value = Number(progress?.[field])
    if (Number.isFinite(value) && value >= 0) next[field] = value
  }
  if (!Number.isFinite(Number(progress?.processed_count))) {
    const current = Number(progress?.current)
    if (Number.isFinite(current) && current >= 0) next.processed_count = current
  }
  session.value = { ...session.value, student_upload: next }
}
function moveStudentFile(index, offset) {
  if (studentUploadActive.value) return
  const nextIndex = index + offset
  if (index < 0 || nextIndex < 0 || nextIndex >= studentFiles.value.length) return
  const next = [...studentFiles.value]
  const [file] = next.splice(index, 1)
  next.splice(nextIndex, 0, file)
  studentFiles.value = next
  studentOrderSource.value = {
    source: 'manual',
    label: '手动调整',
    message: '老师已调整当前列表，系统将按下面的最终顺序上传。',
  }
}
function removeStudentFile(index) {
  if (studentUploadActive.value || index < 0 || index >= studentFiles.value.length) return
  studentFiles.value = studentFiles.value.filter((_file, fileIndex) => fileIndex !== index)
  studentOrderSource.value = {
    source: 'manual',
    label: '手动调整',
    message: '老师已调整当前列表，系统将按下面的最终顺序上传。',
  }
}
async function processStudentUpload(uploadId = studentUpload.value.upload_id) {
  const durableUploadId = String(uploadId || '')
  if (!durableUploadId) return
  const data = await runAnswerSheetStream(
    signal => api.answerSheet.processStudentUploadStream(session.value.session_id, durableUploadId, signal),
    '正在处理已上传的学生答题卡',
    '学生答题卡已完成预处理',
    updateStudentUploadProgress,
  )
  const completedSession = data?.session || (data?.session_id ? data : null)
  if (completedSession) applySession(completedSession, { chooseStage: false })
}
async function uploadStudents() {
  if (!requireAnswerSheetService()) return
  if (busy.value) return
  const files = [...studentFiles.value]
  if (!files.length || studentUploadProcessing.value) return
  let durableUploadId = ''
  busy.value = true
  operationMessage.value = studentUploadActive.value ? '正在恢复学生答题卡上传' : '正在创建学生答题卡上传批次'
  try {
    const started = await api.answerSheet.startStudentUpload(session.value.session_id, false)
    applySession(started, { chooseStage: false })
    durableUploadId = String(started?.student_upload?.upload_id || '')
    if (!durableUploadId) throw new Error('服务器未返回可恢复的上传批次')
    for (const [index, file] of files.entries()) {
      operationMessage.value = '正在持久保存学生答题卡'
      operationProgress.value = { current: index, total: files.length }
      const latest = await api.answerSheet.uploadStudents(
        session.value.session_id,
        durableUploadId,
        file,
        captureSourcePath(file),
      )
      applySession(latest, { chooseStage: false })
    }
    studentFiles.value = []
    ElMessage.success('学生答题卡已逐页持久保存')
  } catch (error) {
    try {
      const latest = await api.answerSheet.getSession(session.value.session_id)
      applySession(latest, { chooseStage: false })
    } catch {
      // Keep the last page-level checkpoint if the session endpoint is unreachable.
    }
    if (isSseServiceFault(normalizeServiceError(error))) {
      markServiceFault('学生答题卡上传中断，已保存页不会丢失；链路恢复后可继续上传。')
    }
    if (!error?.response) notifyProcessingError(error, error?.message || '学生答题卡上传失败')
    return
  } finally {
    operationMessage.value = ''
    operationProgress.value = null
    busy.value = false
  }
  await processStudentUpload(durableUploadId)
}
async function restartStudentUpload() {
  if (!requireAnswerSheetService()) return
  await ElMessageBox.confirm(
    '已持久保存但尚未完成处理的页面将被丢弃，确定重新开始吗？',
    '重新开始学生答题卡上传',
    { type: 'warning' },
  )
  const restarted = await run(
    () => api.answerSheet.startStudentUpload(session.value.session_id, true),
    '已建立新的学生答题卡上传批次',
  )
  applySession(restarted, { chooseStage: false })
}
async function confirmOrientation(paperId, turns) {
  if (!requireAnswerSheetService()) return
  applySession(await run(() => api.answerSheet.confirmOrientation(session.value.session_id, paperId, turns), '方向已确认'), { chooseStage: false })
}
async function recognizeStudents() {
  const data = await runAnswerSheetStream(
    signal => api.answerSheet.recognizeStudentsStream(session.value.session_id, signal),
    '正在识别学生答题卡',
    '识别完成，请处理待复核项',
  )
  if (!data?.session) return
  applySession(data.session, { chooseStage: false })
  // Identity candidates come from the class the batch inherited at creation,
  // even if the teacher has since switched the global current class.
  classStudents.value = await api.classroom.students(session.value.class_id || auth.currentClassId)
}
async function confirmStudentReview(page) { const answers = Object.fromEntries((page.answers || []).map(item => [item.question_no, item.answer])); applySession(await run(() => api.answerSheet.confirmStudentReview(session.value.session_id, page.paper_id, { student_id: page.review_student_id || page.identity?.student_id, answers }), '本页已确认'), { chooseStage: false }) }
async function buildDraft() {
  const data = await runAnswerSheetStream(
    signal => api.answerSheet.gradingDraftStream(session.value.session_id, signal),
    '正在生成批阅草稿',
    '批阅草稿已生成',
  )
  if (!data) return
  session.value.review_revision = data.review_revision
  session.value.grouping_decisions = data.drafts || []
  const lifecycle = { ...(session.value.asset_lifecycle || {}) }
  delete lifecycle.grading_draft_checkpoint
  session.value.asset_lifecycle = lifecycle
  gradingDrafts.value = data.drafts || []
  setActiveDraft(0)
}
async function saveDraftDecision() {
  const draft = currentDraft.value
  const details = draft.result?.details || []
  if (!details.length || details.some(detail => !['correct', 'partial', 'wrong'].includes(detail.result))) {
    ElMessage.warning('请先为全部题目选择正确、部分正确或错误')
    return
  }
  const payload = {
    student_id: draft.student_id,
    review_revision: session.value.review_revision,
    decisions: details.map((detail, index) => ({
      question_no: String(reviewDetailQuestionNo(detail, index)),
      result: detail.result,
      note: String(detail.teacher_note || ''),
    })),
  }
  try {
    await run(
      () => api.answerSheet.gradingDecision(session.value.session_id, payload),
      '人工判定已保存',
    )
  } catch {
    const latest = await api.answerSheet.getSession(session.value.session_id)
    applySession(latest, { chooseStage: false })
    ElMessage.warning('批阅草稿已更新，已载入服务器最新版本')
    return
  }
  const latest = await api.answerSheet.getSession(session.value.session_id)
  applySession(latest, { chooseStage: false })
}
async function archiveDrafts() { await run(() => api.answerSheet.archive(session.value.session_id), '答题卡批阅结果已归档'); applySession(await api.answerSheet.getSession(session.value.session_id), { chooseStage: false }) }
async function removeSession() { await ElMessageBox.confirm('删除后本次草稿及已上传文件无法恢复，确定继续吗？', '删除答题卡草稿', { type: 'warning' }); await run(() => api.answerSheet.deleteSession(session.value.session_id), '草稿已删除'); localStorage.removeItem(STORAGE_KEY); session.value = null; activeStage.value = 0; void loadBatchChoices() }
function regionTypeOptions(questionNo) { const type = answerSpecs.value.find(item => String(item.question_no) === String(questionNo))?.question_type; if (type === 'choice') return [{ label: '标准涂卡', value: 'choice_mark' }, { label: '手写字母', value: 'choice_handwritten' }]; if (type === 'fill') return [{ label: '填空', value: 'fill' }]; return [{ label: '解答', value: 'solve' }] }
function syncRegionType(region) { region.region_type = regionTypeOptions(region.question_no)[0]?.value || 'solve'; const question = answerSpecs.value.find(item => String(item.question_no) === String(region.question_no)); region.allow_multiple = question?.choice_mode === 'multiple' }
function addAnswerRegion() { const question = answerSpecs.value.find(item => !answerRegions.value.some(region => region.question_no === item.question_no)) || answerSpecs.value[0]; if (!question || !currentPageId.value) return; const region = { region_id: `answer_${Date.now()}`, page_id: currentPageId.value, question_no: String(question.question_no), subquestion_no: '', bbox: [100, 100, 900, 200], region_type: 'solve', reading_order: answerRegions.value.length + 1, required: true, teacher_confirmed: false, choice_options: ['A', 'B', 'C', 'D'], allow_multiple: question.choice_mode === 'multiple' }; syncRegionType(region); answerRegions.value.push(region) }
function addIdentityRegion() { if (!currentPageId.value) return; identityRegions.value.push({ region_id: `identity_${Date.now()}`, page_id: currentPageId.value, bbox: [50, 30, 450, 100], identity_type: 'name', teacher_confirmed: false, config: {} }) }
function bboxStyle(bbox) { return { left: `${bbox[0] / 10}%`, top: `${bbox[1] / 10}%`, width: `${(bbox[2] - bbox[0]) / 10}%`, height: `${(bbox[3] - bbox[1]) / 10}%` } }
function setActiveDraft(index) {
  activeDraftIndex.value = index
  currentReviewPaperId.value = gradingDrafts.value[index]?.paper_ids?.[0] || ''
}
function moveReviewPage(offset) {
  const next = currentReviewPageIndex.value + offset
  if (next >= 0 && next < currentDraftPages.value.length) currentReviewPaperId.value = currentDraftPages.value[next].paper_id
}
function openDetailPage(detail) {
  const paperIds = detail.paper_ids || []
  if (!paperIds.length || paperIds.includes(currentReviewPaperId.value)) return
  currentReviewPaperId.value = paperIds[0]
}
function formatNumber(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  return Number.isInteger(number) ? String(number) : number.toFixed(1)
}
function formatBytes(value) {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  if (bytes < 1024) return `${Math.round(bytes)} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
function formatDraftMetric(result) {
  if (Number(result?.needs_review_count || 0) > 0) return '待教师复核'
  if (result?.max_score != null) return `${formatNumber(result?.score)} / ${formatNumber(result?.max_score)} 分`
  const rate = Number(result?.correct_rate)
  return Number.isFinite(rate) ? `${Math.round(rate * 100)}%` : '待教师复核'
}
function reviewDetailQuestionNo(detail, index) { return detail.question_no || detail.question_number || detail.parent_question_no || index + 1 }
function reviewDetailClass(detail) { return ['correct', 'partial', 'wrong', 'needs_review'].includes(detail.result) ? detail.result : 'needs_review' }
function reviewDetailLabel(detail) { return ({ correct: '正确', partial: '部分正确', wrong: '错误', needs_review: '待复核' })[reviewDetailClass(detail)] }
function reviewDetailAnalysis(detail) { return detail.explanation || detail.hint || '' }
function renderReviewText(value) {
  const normalized = String(value || '').replaceAll(String.fromCharCode(13), '')
  return normalized.split(String.fromCharCode(10)).map(line => renderLatex(line)).join('<br>')
}

watch([answerRegions, identityRegions], () => { if (session.value?.status === 'layout_draft') layoutSummary.value = summarizeLayout() }, { deep: true })
watch([() => session.value?.session_id, currentPageId, () => currentPage.value?.working_sha256], async ([sessionId, pageId], _previous, onCleanup) => {
  let cancelled = false
  onCleanup(() => { cancelled = true })
  if (layoutImageUrl.value) URL.revokeObjectURL(layoutImageUrl.value)
  layoutImageUrl.value = ''
  layoutImageReady.value = false
  layoutImageError.value = ''
  if (!sessionId || !pageId) return
  try {
    const nextUrl = await api.answerSheet.layoutPageImage(sessionId, pageId)
    if (cancelled) {
      URL.revokeObjectURL(nextUrl)
      return
    }
    layoutImageUrl.value = nextUrl
  } catch {
    if (!cancelled) layoutImageError.value = '答题卡图像读取失败，请重新打开当前页。'
  }
})
watch([() => session.value?.session_id, currentReviewPaperId], async ([sessionId, paperId]) => {
  const requestId = ++studentImageRequestId.value
  if (studentPageImageUrl.value) URL.revokeObjectURL(studentPageImageUrl.value)
  studentPageImageUrl.value = ''
  studentPageImageError.value = ''
  if (!sessionId || !paperId) return
  try {
    const nextUrl = await api.answerSheet.studentPageImage(sessionId, paperId)
    if (requestId !== studentImageRequestId.value) {
      URL.revokeObjectURL(nextUrl)
      return
    }
    studentPageImageUrl.value = nextUrl
  } catch {
    if (requestId === studentImageRequestId.value) studentPageImageError.value = '当前答题卡页面读取失败，请重试。'
  }
})
onMounted(async () => {
  void refreshAvailability()
  void loadTemplateCodeHistory()
  void loadBatchChoices()
  availabilityPollTimer.value = window.setInterval(refreshAvailability, 15000)
  if (!auth.classes.length) void auth.fetchClasses().catch(() => {})
  const saved = String(route.query.session || localStorage.getItem(STORAGE_KEY) || '')
  if (!saved) return
  busy.value = true
  try {
    applySession(await api.answerSheet.getSession(saved))
    restoreError.value = route.query.session ? '已打开该条正式记录关联的答题卡原卷。' : '已恢复上次未完成的答题卡会话。'
  } catch {
    if (!route.query.session) localStorage.removeItem(STORAGE_KEY)
    restoreError.value = '答题卡会话无法恢复，可能已删除或不属于当前账号。'
  } finally {
    busy.value = false
  }
})
onBeforeUnmount(() => {
  if (availabilityPollTimer.value) window.clearInterval(availabilityPollTimer.value)
  if (answerSheetStreamController.value) answerSheetStreamController.value.abort()
  studentImageRequestId.value += 1
  if (layoutImageUrl.value) URL.revokeObjectURL(layoutImageUrl.value)
  if (studentPageImageUrl.value) URL.revokeObjectURL(studentPageImageUrl.value)
})
</script>

<style scoped src="./answer-sheet/answer-sheet.css"></style>
