<template>
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
</template>

<script setup>
// The 批改 page's manual decision dialog: the original paper beside its graded answers, where the teacher
// settles the flagged ones.  State and actions come from the page's ctx (useReviewResults).
import { inject } from 'vue'
import { renderAnswerLatex } from '@/utils/latex'
import InkSpinner from '@/components/InkSpinner.vue'
import DrawingAssessment from '@/components/DrawingAssessment.vue'
import { UPLOAD_PAGE } from './uploadPage.js'

const {
  manualDecisionDialogVisible, manualDecisionShowPaper, manualDecisionRow, manualDecisionSaving,
  resetManualDecisionImage, anonymous, manualDecisionPageIndex, manualDecisionImageLoading,
  changeManualDecisionPage, manualDecisionPages, manualDecisionImageUrl, openManualDecisionImage,
  manualDecisionImageError, reviewBusy, downloadCurrentReviewImage, manualDecisionDrafts,
  manualDecisionCompactDrafts, jumpToManualDecisionPage, manualDecisionSolveDrafts,
  manualDecisionHasEditableRows, manualDecisionComplete, saveManualDecision,
} = inject(UPLOAD_PAGE)
</script>

<style scoped src="./page.css"></style>
<style scoped src="./cards.css"></style>
