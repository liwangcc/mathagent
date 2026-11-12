<template>
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

    <section v-if="templatePendingAnswerEntries.length" class="tpl-answer-review" aria-label="标准答案生成中" role="status">
      <div class="tpl-layout-review-alert tpl-answer-review-row">
        <strong>{{ templatePendingAnswersNotice(templatePendingAnswerEntries) }}</strong>
        <button type="button" class="btn btn-ghost" @click="openTemplateQuestion(templatePendingAnswerEntries[0])">去查看</button>
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
            <span v-if="templateAnswerPending(q)" class="tag-pending">标准答案生成中</span>
            <span v-else-if="q.answer_failed" class="tag-pending" :title="q.answer_failed">标准答案未生成，请填写</span>
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
      <button class="btn btn-primary" @click="onConfirmTemplate" :disabled="confirmingTemplate || importingTemplateBank || templateCutBusy || templateAnswersPending" :title="templateAnswersPending ? '标准答案生成完成后才能保存' : null">
        {{ confirmingTemplate ? '保存中...' : (templateAnswersPending ? '标准答案生成中…' : '保存模板（全部页）') }}
      </button>
    </template>
  </el-dialog>
</template>

<script setup>
// The 批改 page's template editor dialog: the recognized template's pages with their geometry editor, the
// answer sections and the problem lists, saving this batch's authority and importing into the question bank
// as separate actions.  State and actions come from the page's ctx (useTemplate* composables).
import { inject } from 'vue'
import {
  choiceContractValid,
  templateAnswerText,
  templateFillAnswerSlots,
  templateMissingAnswerSlots,
  templateMissingAnswerNotice,
  templateAnswerPending,
  templatePendingAnswersNotice,
  templateMaxFlagNotice,
  keepTemplateMaxFlags,
  templateQuestionIdentity,
  isTemplateDirectoryLeaf,
} from '@/utils/templateReview'
import { templateSlotBoxes, templateSlotLabel } from '@/utils/templateSlots'
import DrawingReferenceEditor from '@/components/DrawingReferenceEditor.vue'
import TemplateGeometryLegend from '@/components/TemplateGeometryLegend.vue'
import { templateBoxStyle, templateQuestionAnchorBBox, templateVisibleAnswerBoxes } from '@/utils/templateGeometry'
import {
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
} from '@/utils/templateDrawing'
import { UPLOAD_PAGE } from './uploadPage.js'

const {
  templateDialogVisible, templateEditorBusy, templateMaxFlagReviewEntries, openTemplateMaxFlags,
  templatePendingRemovalEntries, templateMissingAnswerEntries, openTemplateMissingAnswers,
  templateEditorProblemEntries, openTemplateProblem, templateCutImageType, form, templateCutBusy,
  pendingTemplateId, templateServiceBlocksNewWork, onRunTemplateCut, templateCutProgress,
  templateCutResult, templateCutPreviewError, templateGeometryReview, templateCutPreviewPages,
  templatePreviewPageIndex, templatePageEditingLocked, resetTemplateColumns, templatePageNeedsSave,
  templatePreviewQuestionIdentity, templateReferenceQuestionEditBlockReason, insertTemplateQuestion,
  deleteTemplateQuestion, templateLayoutConflictNotice, templateGeometryHint,
  templateReferenceEditors, setTemplateReferenceEditor, templateReferenceCanvasVisible,
  setTemplateStrokeSelection, selectTemplateDrawingReference, moveTemplateGeometry,
  finishTemplateGeometry, cancelTemplateGeometry, TEMPLATE_GEOMETRY_TOOLS, armTemplateGeometryTool,
  deleteTemplateSelection, selectTemplateGeometryTarget, templateSlotNeedsAnswer,
  templateSlotMaxFlagged, templateProblemBoxes, locateTemplateReviewSlot, templateSlotAnswer,
  correctTemplateSlotPosition, clearTemplateSelection, startTemplateDividerMove,
  startTemplateQuestionMove, startTemplateQuestionResize, startTemplateAnswerMove,
  startTemplateAnswerResize, startTemplateGeometryGesture, startTemplateDrawingLabelMove,
  templateGeometryToolLabel, startTemplateGeometryCreation, templateChoiceEntries,
  templateChoiceSingleCount, templateChoiceMultipleCount, templateChoicePendingCount,
  setTemplateDetailOpen, templateChoiceScore, openTemplateQuestion, templatePages,
  templateFillEntries, templateFillBatchEntryCount, templateFillScore, templateCustomFillCount,
  templateSolveEntries, templateCutPageOriginalCount, templateQuestionDomId,
  setTemplateAnswerDrawingKind, openTemplateDrawingReference, toggleQuestionCorrection,
  templatePreviewPagePosition, selectTemplatePreviewPage, confirmingTemplate, cancelTemplateDialog,
  onImportTemplateBank, importingTemplateBank, templateBankImported, templateCanConfirm,
  onConfirmTemplate, templatePendingAnswerEntries, templateAnswersPending,
} = inject(UPLOAD_PAGE)
</script>

<style scoped>
/* No styles of its own: this component shares Upload.vue's scope id (vite.config.js), so the page's
   scoped styles (upload/page.css, upload/cards.css) apply to it. */
</style>
