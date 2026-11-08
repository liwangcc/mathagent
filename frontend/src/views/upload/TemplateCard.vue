<template>
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
</template>

<script setup>
// The 批改 page's template card (left column): importing the multi-page template that is this batch's answer
// authority, recognizing it, its review summary and the actions that open the editor or confirm it.  State and
// actions come from the page's ctx (useTemplate* composables).
import { inject } from 'vue'
import { UploadFilled } from '@element-plus/icons-vue'
import InkSpinner from '@/components/InkSpinner.vue'
import SheetLoader from '@/components/SheetLoader.vue'
import { IMPORT_ACCEPT, IMPORT_FORMATS } from '@/utils/fileExtraction'
import HelpButton from '@/components/HelpButton.vue'
import { UPLOAD_PAGE } from './uploadPage.js'

const {
  templateStageLabel, form, templateLocked, templateColumnMode, confirmedTemplateId,
  templatePageCount, templateBankImported, openTemplateEditor, recognizing, grading,
  confirmingTemplate, resetTemplate, templatePreprocessing, templateServiceBlocksNewWork,
  triggerTemplateUpload, templateInputRef, onTemplateFileChange, templateFiles, recognizingTemplate,
  onRecognizeTemplate, templateServiceState, templateRecognizeMessage, templateRecognizeError,
} = inject(UPLOAD_PAGE)
</script>

<style scoped src="./page.css"></style>
<style scoped src="./cards.css"></style>
