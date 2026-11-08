<template>
  <div class="class-table-page page-shell">
    <el-card shadow="never" class="toolbar-card">
      <div class="toolbar">
        <div class="toolbar-left">
          <div class="class-meta">
            <div class="class-title-row">
              <h1>{{ className }}</h1>
              <HelpButton
                title="班级概览"
                content="这里显示当前班级规模和有效统计覆盖。先确认顶部导航的班级，再进行批阅、热力图或报告操作。"
              />
            </div>
            <div class="class-chips">
              <span class="class-count num">{{ students.length }} 名学生</span>
              <el-tag v-if="coverage.observation_students" type="info" size="small">
                有效统计 {{ coverage.effective_students }} / {{ coverage.class_size }} · 观察 {{ coverage.observation_students }}
              </el-tag>
              <el-tag type="info" size="small" class="profile-stage-label">{{ historyStageLabel(educationStage) }} · 跟随个人资料</el-tag>
            </div>
          </div>
        </div>
        <div class="toolbar-actions">
          <div class="action-group">
            <el-button :icon="Refresh" @click="loadAll">刷新</el-button>
            <el-button text type="primary" :icon="Edit" :disabled="!activeClassId" @click="openEditCurrentClass">
              编辑班级
            </el-button>
            <el-button
              text
              type="primary"
              :icon="Plus"
              :disabled="auth.classes.length >= auth.maxClasses"
              data-tour="class-create"
              @click="openCreateClass"
            >
              添加班级
            </el-button>
            <el-popconfirm title="删除后，班内学生会变为“未分班”（历史批阅与成绩保留，可再分配到其他班级）。确定删除当前班级？" width="300" @confirm="deleteCurrentClass">
              <template #reference>
                <el-button text type="danger" :icon="Delete" :disabled="!activeClassId">删除班级</el-button>
              </template>
            </el-popconfirm>
            <HelpButton
              title="班级操作"
              content="可编辑或删除当前班级、添加学生；更多菜单包含月考成绩导入、知识点热力图和报告入口。删除班级前请确认其中的学生归属。"
            />
          </div>
          <span class="toolbar-divider" aria-hidden="true" />
          <div class="action-group">
          <el-button type="primary" :icon="Plus" :disabled="!activeClassId" data-tour="student-add" @click="studentDialogVisible = true">
            添加学生
          </el-button>
          <el-dropdown :disabled="!activeClassId" trigger="click" @command="onMoreCommand">
            <el-button :icon="MoreFilled">
              更多<el-icon class="el-icon--right"><ArrowDown /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="exam"><el-icon><TrendCharts /></el-icon>导入月考成绩</el-dropdown-item>
                <el-dropdown-item command="heatmap"><el-icon><DataAnalysis /></el-icon>查看知识点热力图</el-dropdown-item>
                <el-dropdown-item command="reports"><el-icon><Document /></el-icon>报告与家长信</el-dropdown-item>
                <el-dropdown-item command="submissions"><el-icon><Link /></el-icon>学生上传链接</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          </div>
        </div>
      </div>
    </el-card>

    <section v-if="activeClassId" class="risk-section" aria-label="风险分组">
      <div class="risk-section-head">
        <span>风险分组</span>
        <small>点击卡片筛选下方表格，再次点击取消</small>
        <HelpButton
          title="风险分组"
          content="风险卡片按错题率、纠错率和观察模式聚合学生。点击卡片只会筛选下方表格，不会修改学生档案。"
        />
      </div>
      <div class="risk-board ma-stagger" :style="{ '--risk-columns': riskGroups.length }">
        <button
          v-for="group in riskGroups"
          :key="group.key"
          type="button"
          class="risk-panel"
          :class="[group.className, { 'is-active': riskFilter === group.key }]"
          :aria-pressed="riskFilter === group.key ? 'true' : 'false'"
          @click="applyRiskFilter(group.key)"
        >
          <span>{{ group.label }}</span>
          <strong class="num">{{ group.students.length }}</strong>
          <small>{{ group.description }}</small>
          <em>{{ group.preview }}</em>
        </button>
      </div>
    </section>

    <el-card shadow="never" class="table-card">
      <template #header>
        <div class="table-head">
          <div>
            <span class="section-title">班级表格</span>
            <span class="section-subtitle">{{ tableSubtitle }}</span>
          </div>
          <HelpButton
            title="班级表格"
            content="可按风险分组或姓名、学号筛选。点击学生行查看详情，观察模式学生不会进入有效班级统计。"
          />
          <div class="table-tools">
            <el-segmented
              v-model="riskFilter"
              :options="riskFilterOptions"
              size="small"
            />
            <el-input
              v-model="keyword"
              clearable
              placeholder="搜索姓名 / 学号"
              class="search-input"
            >
              <template #prefix><el-icon><Search /></el-icon></template>
            </el-input>
          </div>
        </div>
      </template>

      <el-alert
        v-if="!activeClassId"
        type="info"
        show-icon
        :closable="false"
        title="请先在顶部班级切换中选择班级，或在班级管理中添加班级"
        class="empty-alert"
      />

      <el-table
        v-else
        v-loading="loading"
        :data="filteredStudents"
        stripe
        class="class-table"
        :header-cell-style="{ background: 'var(--surface-subtle)', color: 'var(--ink-muted)' }"
        :row-style="{ cursor: 'pointer' }"
        @row-click="openStudent"
      >
        <el-table-column prop="student_no" label="学号" width="80" sortable :sort-method="sortByStudentNo" />
        <el-table-column prop="exam_no" label="准考证号" min-width="110" />
        <el-table-column label="姓名" min-width="100" sortable :sort-method="sortByName">
          <template #default="{ row }">
            <div class="student-cell">
              <strong>{{ row.name }}</strong>
              <span
                class="status-dot"
                :class="`status-dot--${row.observation_mode ? 'observation' : row._risk.key}`"
                :title="row.observation_mode ? '观察模式' : (row.risk_reason || row._risk.label)"
                :aria-label="row.observation_mode ? '观察模式' : row._risk.label"
                role="img"
              />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="错题率" width="124" sortable :sort-method="sortByWrongRate">
          <template #default="{ row }">
            <span v-if="row.has_records === false" class="rate-empty">暂无批阅</span>
            <div v-else class="rate-cell">
              <el-progress
                :percentage="toPercent(row.wrong_rate)"
                :stroke-width="8"
                :show-text="false"
                :color="rateColor(row.wrong_rate, true)"
              />
              <span>{{ formatRate(row.wrong_rate) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="纠错率" width="124" sortable :sort-method="sortByCorrectionRate">
          <template #default="{ row }">
            <span v-if="row.has_records === false" class="rate-empty">—</span>
            <span v-else-if="row.has_mistakes === false" class="rate-empty" title="暂无错题，无需纠错">无错题</span>
            <div v-else class="rate-cell">
              <el-progress
                :percentage="toPercent(row.correction_rate)"
                :stroke-width="8"
                :show-text="false"
                :color="rateColor(row.correction_rate, false)"
              />
              <span>{{ formatRate(row.correction_rate) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="薄弱点" width="88" prop="weakness_count" sortable>
          <template #default="{ row }">
            <el-tag :type="Number(row.weakness_count || 0) > 0 ? 'warning' : 'success'" size="small">
              {{ row.weakness_count || 0 }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近月考" min-width="130" sortable :sort-method="sortByLatestScore">
          <template #default="{ row }">
            <div class="exam-cell">
              <span>{{ row.latest_exam_score ?? '-' }}</span>
              <small>{{ row.latest_exam_name || '暂无记录' }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="300" fixed="right">
          <template #default="{ row }">
            <div class="action-btns">
              <el-button text type="primary" size="small" @click.stop="openStudent(row)">详情</el-button>
              <el-button text type="warning" size="small" @click.stop="editStudent(row)">编辑</el-button>
              <el-button text type="info" size="small" @click.stop="toggleObservation(row)">
                {{ row.observation_mode ? '恢复统计' : '设为观察' }}
              </el-button>
              <el-popconfirm title="确定将该学生移出班级？" @confirm="removeStudent(row)">
                <template #reference>
                  <el-button text type="danger" size="small" @click.stop>移出</el-button>
                </template>
              </el-popconfirm>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无学生">
            <el-button type="primary" :icon="Plus" @click="studentDialogVisible = true">添加学生</el-button>
          </el-empty>
        </template>
      </el-table>
    </el-card>

    <el-dialog v-model="classDialogVisible" :title="editingClassId ? '编辑班级' : '添加班级'" width="420px">
      <el-form label-width="76px" @submit.prevent>
        <el-form-item label="班级名称">
          <el-input
            v-model="classFormName"
            placeholder="例如：24级数控1班"
            maxlength="50"
            show-word-limit
            @keyup.enter="saveClass"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="classDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingClass" @click="saveClass">
          {{ editingClassId ? '保存班级' : '添加班级' }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="studentDialogVisible" title="添加学生" width="860px" destroy-on-close>
      <el-alert
        title="如果班级中有同名学生，请填写学号，以便准确确定身份。"
        type="info"
        show-icon
        :closable="false"
        class="student-identity-tip"
      />
      <el-tabs v-model="studentImportMode">
        <el-tab-pane label="手动录入" name="manual">
          <div class="manual-list">
            <div v-for="(item, index) in manualStudents" :key="index" class="manual-row">
              <el-input v-model="item.student_no" placeholder="学号(选填)" />
              <el-input v-model="item.exam_no" placeholder="准考证号(选填)" />
              <el-input v-model="item.name" placeholder="姓名" />
              <el-input v-model="item.phone" placeholder="家长手机号(选填)" />
              <el-button :icon="Delete" :disabled="manualStudents.length === 1" @click="manualStudents.splice(index, 1)" />
            </div>
          </div>
          <el-button text type="primary" :icon="Plus" @click="manualStudents.push({ student_no: '', exam_no: '', name: '', phone: '' })">
            继续添加
          </el-button>
        </el-tab-pane>
        <el-tab-pane label="Excel 导入" name="excel">
          <el-upload
            drag
            :auto-upload="false"
            :limit="1"
            accept=".xlsx"
            :on-change="onStudentExcelChange"
            :on-remove="() => { studentExcelFile = null; studentExcelPreview = null }"
          >
            <el-icon class="upload-icon"><UploadFilled /></el-icon>
            <div class="upload-text">选择学生名单 Excel（.xlsx）</div>
            <template #tip><div class="upload-tip">按表头识别：学号、准考证号、姓名、家长手机（准考证号、手机可选）。旧版 .xls 请先另存为 .xlsx。先预览，确认无误再导入。</div></template>
          </el-upload>
          <div v-if="studentExcelPreview" class="import-preview">
            <p class="import-preview__summary">将新增 <strong>{{ studentExcelPreview.added }}</strong> 人，跳过 <strong>{{ studentExcelPreview.skipped }}</strong> 人</p>
            <p class="import-preview__columns">识别到的列：{{ columnText(studentExcelPreview.columns) }}</p>
            <el-table :data="studentExcelPreview.preview" size="small" border max-height="260">
              <el-table-column prop="row" label="行" width="56" />
              <el-table-column prop="student_no" label="学号" min-width="90" />
              <el-table-column prop="name" label="姓名" min-width="90" />
              <el-table-column prop="phone" label="家长手机" min-width="110" />
              <el-table-column label="结果" min-width="180">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.status === 'add' ? 'success' : 'warning'">{{ row.status === 'add' ? '新增' : '跳过' }}</el-tag>
                  <small v-if="row.reason" class="import-preview__reason">{{ row.reason }}</small>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-tab-pane>
        <el-tab-pane label="图片识别" name="image">
          <el-upload
            drag
            :auto-upload="false"
            :limit="1"
            accept="image/*"
            :on-change="onStudentImageChange"
            :on-remove="() => { studentImageFile = null; recognizedStudents = [] }"
          >
            <el-icon class="upload-icon"><Camera /></el-icon>
            <div class="upload-text">上传名单图片</div>
            <template #tip><div class="upload-tip">识别后先确认，再写入学生表</div></template>
          </el-upload>
          <div v-if="recognizedStudents.length" class="recognized-box">
            <div class="recognized-title">识别结果</div>
            <el-table :data="recognizedStudents" size="small" border>
              <el-table-column label="学号">
                <template #default="{ row }"><el-input v-model="row.student_no" size="small" maxlength="50" /></template>
              </el-table-column>
              <el-table-column label="准考证号">
                <template #default="{ row }"><el-input v-model="row.exam_no" size="small" maxlength="64" /></template>
              </el-table-column>
              <el-table-column label="姓名">
                <template #default="{ row }"><el-input v-model="row.name" size="small" maxlength="50" /></template>
              </el-table-column>
              <el-table-column label="家长手机">
                <template #default="{ row }"><el-input v-model="row.phone" size="small" maxlength="20" /></template>
              </el-table-column>
              <el-table-column width="56" align="center">
                <template #default="{ $index }">
                  <el-button text type="danger" :icon="Delete" aria-label="删除这一行" @click="recognizedStudents.splice($index, 1)" />
                </template>
              </el-table-column>
            </el-table>
            <div class="upload-tip">识别结果可直接修改或删除整行，确认无误后再保存。</div>
          </div>
        </el-tab-pane>
      </el-tabs>
      <template #footer>
        <el-button @click="studentDialogVisible = false">取消</el-button>
        <el-button
          v-if="studentImportMode === 'image' && !recognizedStudents.length"
          type="primary"
          :loading="recognizing"
          :disabled="recognizing || !studentImageFile"
          @click="recognizeImage"
        >
          识别名单
        </el-button>
        <el-button v-else type="primary" :loading="savingStudents" :disabled="studentImportMode === 'excel' && studentExcelPreview && !studentExcelPreview.added" @click="saveStudents">
          {{ studentImportMode !== 'excel' ? '保存学生' : studentExcelPreview ? `确认导入 ${studentExcelPreview.added} 人` : '预览导入' }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editStudentVisible" title="编辑学生" width="420px" destroy-on-close>
      <el-form label-width="88px">
        <el-form-item label="姓名">
          <el-input v-model="editStudentForm.name" maxlength="50" />
        </el-form-item>
        <el-form-item label="学号">
          <el-input v-model="editStudentForm.student_no" maxlength="50" />
        </el-form-item>
        <el-form-item label="准考证号">
          <el-input v-model="editStudentForm.exam_no" placeholder="保留前导零" maxlength="64" />
        </el-form-item>
        <el-form-item label="家长手机">
          <el-input v-model="editStudentForm.phone" placeholder="家长手机号（后四位用于家长信校验）" maxlength="20" />
        </el-form-item>
        <el-form-item label="提交密码">
          <div class="submit-password" data-test="submit-password">
            <span class="submit-password-state">
              {{ submitPassword.loading ? '读取中…' : (submitPassword.set ? '已设置' : '未设置') }}
            </span>
            <el-input
              v-model="submitPassword.value"
              type="password"
              show-password
              maxlength="32"
              autocomplete="new-password"
              :placeholder="submitPassword.set ? '输入新密码以更换' : '选填，4–32 位'"
            />
            <el-button size="small" :loading="submitPassword.saving" :disabled="submitPassword.loading" @click="saveSubmitPassword">
              {{ submitPassword.set ? '更换' : '设置' }}
            </el-button>
            <el-button v-if="submitPassword.set" size="small" text type="danger" :disabled="submitPassword.saving" @click="clearSubmitPassword">清除</el-button>
          </div>
          <div class="submit-password-hint">
            只在需要时设置。设置后，该学生用“学生上传链接”提交时必须填这个密码；未设置的学生不用填。密码保存后无法查看，忘了就重新设置。
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editStudentVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingEdit" @click="saveEditStudent">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="examDialogVisible" title="导入月考成绩" width="520px" destroy-on-close>
      <el-form label-width="88px">
        <el-form-item label="考试名称">
          <el-input v-model="examName" placeholder="例如：期中考" maxlength="100" />
        </el-form-item>
        <el-form-item label="考试日期">
          <el-date-picker v-model="examDate" type="date" value-format="YYYY-MM-DD" placeholder="默认今天；用于成绩趋势排序" />
        </el-form-item>
        <el-form-item label="成绩文件">
          <el-upload
            drag
            :auto-upload="false"
            :limit="1"
            accept=".xlsx"
            :on-change="onExamFileChange"
            :on-remove="() => { examFile = null; examPreview = null }"
          >
            <el-icon class="upload-icon"><UploadFilled /></el-icon>
            <div class="upload-text">选择月考成绩 Excel（.xlsx）</div>
            <template #tip><div class="upload-tip">列：学号 / 姓名 / 成绩 / 可选考试名。同名学生请填写学号；学号与姓名不一致的行不会导入。</div></template>
          </el-upload>
        </el-form-item>
      </el-form>
      <div v-if="examPreview" class="import-preview">
        <p class="import-preview__summary">
          将导入 <strong>{{ examPreview.matched }}</strong> 人
          <template v-if="examPreview.exam_names?.length">（{{ examPreview.exam_names.join('、') }}）</template>；
          未匹配 {{ examPreview.unmatched?.length || 0 }}，跳过 {{ examPreview.skipped?.length || 0 }}，重复 {{ examPreview.duplicates?.length || 0 }}
        </p>
        <p class="import-preview__columns">识别到的列：{{ columnText(examPreview.columns) }}</p>
        <ul v-if="examIssues.length" class="import-preview__issues">
          <li v-for="issue in examIssues" :key="issue">{{ issue }}</li>
        </ul>
      </div>
      <template #footer>
        <el-button @click="examDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="importingExam" :disabled="examPreview && !examPreview.matched" @click="importExamScores">
          {{ examPreview ? `确认导入 ${examPreview.matched} 人` : '预览导入' }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="heatmapVisible" title="知识点热力图" width="760px">
      <div v-loading="heatmapLoading" class="heatmap-dialog">
        <el-empty v-if="!heatmapLoading && !heatmapTree.length" description="暂无热力图数据" />
        <el-collapse v-else>
          <el-collapse-item
            v-for="chapter in heatmapTree"
            :key="`${chapter.education_stage}-${chapter.catalog_version}-${chapter.chapter}`"
            :title="`${historyStageLabel(chapter.education_stage)} · ${chapter.chapter || '未分章'} · ${chapter.wrong_students || 0} 人`"
          >
            <div class="teaching-panel" :class="`priority-${chapter.priority || 'low'}`">
              <el-tag size="small" effect="dark" :type="priorityMeta(chapter.priority).type">
                {{ priorityMeta(chapter.priority).label }}
              </el-tag>
              <span>{{ chapter.teaching_suggestion || '暂无讲评建议' }}</span>
            </div>
            <div v-for="topic in chapter.topics || []" :key="topic.topic" class="topic-block">
              <div class="topic-title">{{ topic.topic || '未知知识点' }} · {{ topic.wrong_students || 0 }} 人 <el-button size="small" text type="primary" @click="openCoursewareRecommendation(chapter.chapter, topic.topic)">查看匹配课件</el-button></div>
              <div class="topic-students">{{ knowledgeTopicStudents(topic).join('、') || '-' }}</div>
            </div>
          </el-collapse-item>
        </el-collapse>
      </div>
    </el-dialog>

    <StudentSubmissionDialog
      v-model="submissionDialogVisible"
      :class-id="activeClassId"
      :class-name="className"
    />
  </div>
</template>

<script setup>
import { computed, reactive, ref, shallowRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowDown, Camera, DataAnalysis, Delete, Document, Edit, Link, MoreFilled,
  Plus, Refresh, Search, TrendCharts, UploadFilled,
} from '@element-plus/icons-vue'
import api from '@/api'
import { normalizeEducationStage, historyStageLabel } from '@/utils/educationStage'
import { useAuthStore } from '@/stores/auth'
import { compareStudentNumbers } from '@/utils/studentSort'
import { knowledgeTopicStudents } from '@/utils/knowledgeHeatmap'
import HelpButton from '@/components/HelpButton.vue'
import StudentSubmissionDialog from '@/components/StudentSubmissionDialog.vue'
import { useClassManagement } from './class-table/useClassManagement.js'
import { useStudentEditing } from './class-table/useStudentEditing.js'
import { useStudentImport } from './class-table/useStudentImport.js'
import { useExamImport } from './class-table/useExamImport.js'
import { useKnowledgeHeatmap } from './class-table/useKnowledgeHeatmap.js'
import { useMoreMenu } from './class-table/useMoreMenu.js'
import { useClassTableLifecycle } from './class-table/useClassTableLifecycle.js'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const educationStage = computed(() => normalizeEducationStage(auth.user?.education_stage))
const profileScope = computed(() => JSON.stringify([auth.user?.id, auth.token, educationStage.value]))

const activeClassId = ref(null)
const className = ref('未选择班级')
const students = ref([])
const coverage = reactive({ class_size: 0, effective_students: 0, observation_students: 0 })
const loading = ref(false)
const keyword = ref('')
const riskFilter = ref('all')

const studentsGeneration = shallowRef(0)

const classScopeGeneration = shallowRef(0)

function captureClassScope() {
  const scope = profileScope.value
  const classId = activeClassId.value
  const generation = classScopeGeneration.value
  return { classId, current: () => scope === profileScope.value && classId === activeClassId.value && generation === classScopeGeneration.value }
}

const riskFilterOptions = [
  { label: '全部', value: 'all' },
  { label: '观察模式', value: 'observation' },
  { label: '重点关注', value: 'attention' },
  { label: '持续跟进', value: 'watch' },
  { label: '表现良好', value: 'stable' },
  { label: '暂无数据', value: 'no_data' },
]

const studentsWithRisk = computed(() =>
  students.value.map((student) => ({
    ...student,
    _risk: riskMeta(student),
  }))
)

const filteredStudents = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  return studentsWithRisk.value.filter((s) => {
    const matchesRisk = riskFilter.value === 'all' || s._risk.key === riskFilter.value
    const matchesKeyword = !q ||
      String(s.name || '').toLowerCase().includes(q) ||
      String(s.student_no || '').toLowerCase().includes(q) ||
      String(s.exam_no || '').toLowerCase().includes(q)
    return matchesRisk && matchesKeyword
  })
})

const riskGroups = computed(() => {
  const groups = [
    {
      key: 'observation',
      label: '观察模式',
      description: '继续批阅，但不参与正式风险与统计判断',
      className: 'risk-observation',
    },
    {
      key: 'attention',
      label: '重点关注',
      description: '错题率高或未纠正薄弱点较多',
      className: 'risk-danger',
    },
    {
      key: 'watch',
      label: '持续跟进',
      description: '有薄弱点，但整体仍可控',
      className: 'risk-warn',
    },
    {
      key: 'stable',
      label: '表现良好',
      description: '纠错推进较好，近期风险较低',
      className: 'risk-good',
    },
    {
      key: 'no_data',
      label: '暂无数据',
      description: '还没有已归档的批阅，暂不判断风险',
      className: 'risk-neutral',
    },
  ]
  return groups.map((group) => {
    const groupStudents = studentsWithRisk.value.filter((student) => student._risk.key === group.key)
    const names = groupStudents.slice(0, 3).map((student) => student.name).filter(Boolean)
    return {
      ...group,
      students: groupStudents,
      preview: names.length ? names.join('、') : '暂无学生',
    }
  }).filter((group) => group.key !== 'no_data' || group.students.length)
})

const tableSubtitle = computed(() => {
  const active = riskFilterOptions.find((item) => item.value === riskFilter.value)?.label || '全部'
  return `${active} · 最近批改窗口、纠错率和薄弱点`
})

function resolveInitialClass() {
  const routeClassId = Number(route.params.classId)
  if (routeClassId) return routeClassId
  if (auth.currentClassId) return auth.currentClassId
  return auth.classes[0]?.id || null
}

async function loadClassesIfNeeded() {
  const scope = profileScope.value
  if (!auth.classes.length) {
    await auth.fetchClasses()
  }
  if (scope !== profileScope.value) return
  if (!activeClassId.value) {
    activeClassId.value = resolveInitialClass()
  }
  if (activeClassId.value && auth.currentClassId !== activeClassId.value) {
    await auth.switchClass(activeClassId.value)
  }
}

async function loadStudents() {
  const scope = profileScope.value
  const classId = activeClassId.value
  const generation = ++studentsGeneration.value
  const current = () => scope === profileScope.value && classId === activeClassId.value && generation === studentsGeneration.value
  resetStudents()
  if (!classId || !auth.token || !educationStage.value) {
    loading.value = false
    return
  }
  loading.value = true
  try {
    const stage = educationStage.value
    const data = await api.classroom.students(classId, { education_stage: stage })
    if (!current()) return
    students.value = data.students || []
    Object.assign(coverage, data.coverage || {
      class_size: students.value.length,
      effective_students: students.value.filter((item) => !item.observation_mode).length,
      observation_students: students.value.filter((item) => item.observation_mode).length,
    })
    className.value = data.class_name || auth.classes.find((c) => c.id === classId)?.class_name || '当前班级'
  } catch {
    if (current()) resetStudents()
  } finally {
    if (current()) loading.value = false
  }
}

async function loadAll() {
  const scope = profileScope.value
  if (!auth.token || !educationStage.value) return
  await loadClassesIfNeeded()
  if (scope !== profileScope.value) return
  await loadStudents()
}

function resetStudents() {
  students.value = []
  className.value = '未选择班级'
  Object.assign(coverage, { class_size: 0, effective_students: 0, observation_students: 0 })
}

async function toggleObservation(row) {
  const { classId, current } = captureClassScope()
  try {
    if (row.observation_mode) {
      await ElMessageBox.confirm(
        '恢复后仅新产生的数据重新纳入统计，历史观察期数据不会回填。',
        `恢复 ${row.name} 的统计`,
        { type: 'warning', confirmButtonText: '确认恢复' },
      )
      if (!current()) return
      await api.classroom.setObservation(classId, row.id, {
        enabled: false,
        reason: row.observation_reason || '教师结束观察',
        note: '教师从班级表恢复统计',
      })
      if (!current()) return
      ElMessage.success('已恢复；后续新数据重新纳入统计')
    } else {
      const { value } = await ElMessageBox.prompt(
        '观察期仍正常批阅并保留结果，但不进入班级统计、待订正队列或家长报告。请输入原因。',
        `将 ${row.name} 设为观察模式`,
        { inputPattern: /\S+/, inputErrorMessage: '必须填写原因', confirmButtonText: '启用观察' },
      )
      if (!current()) return
      await api.classroom.setObservation(classId, row.id, {
        enabled: true,
        reason: value.trim(),
        note: '教师从班级表启用',
      })
      if (!current()) return
      ElMessage.success('观察模式已启用')
    }
    await loadStudents()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') throw error
  }
}

function applyRiskFilter(key) {
  riskFilter.value = riskFilter.value === key ? 'all' : key
}

function toPercent(value) {
  const n = Number(value || 0)
  return Math.max(0, Math.min(100, Math.round(n * 1000) / 10))
}

function formatRate(value) {
  return `${toPercent(value).toFixed(1)}%`
}

function rateColor(value, badHigh) {
  const n = Number(value || 0)
  if (badHigh) {
    if (n >= 0.35) return 'var(--danger)'
    if (n >= 0.18) return 'var(--warning)'
    return 'var(--success)'
  }
  if (n >= 0.7) return 'var(--success)'
  if (n >= 0.4) return 'var(--warning)'
  return 'var(--danger)'
}

function riskMeta(row) {
  const riskMap = {
    observation: { key: 'observation', label: '观察模式', tagType: 'info' },
    attention: { key: 'attention', label: '重点关注', tagType: 'danger' },
    watch: { key: 'watch', label: '持续跟进', tagType: 'warning' },
    stable: { key: 'stable', label: '表现良好', tagType: 'success' },
    no_data: { key: 'no_data', label: '暂无数据', tagType: 'info' },
  }
  return riskMap[row.risk_level] || riskMap.stable
}

function sortByName(a, b) {
  return String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN')
}

function sortByStudentNo(a, b) {
  return compareStudentNumbers(a.student_no, b.student_no)
}

function sortByWrongRate(a, b) {
  return Number(a.wrong_rate || 0) - Number(b.wrong_rate || 0)
}

function sortByCorrectionRate(a, b) {
  return Number(a.correction_rate || 0) - Number(b.correction_rate || 0)
}

function sortByLatestScore(a, b) {
  return Number(a.latest_exam_score ?? -1) - Number(b.latest_exam_score ?? -1)
}

// The rest of the page lives in one composable per part (views/class-table/use*.js). Each one gets ctx,
// holding what the earlier parts defined; it calls functions of later parts through ctx.
const ctx = {
  route, router, auth, educationStage, profileScope, activeClassId, className, loading,
  studentsGeneration, classScopeGeneration, captureClassScope, resolveInitialClass, loadStudents,
  loadAll, resetStudents,
}
Object.assign(ctx, useClassManagement(ctx))
Object.assign(ctx, useStudentEditing(ctx))
Object.assign(ctx, useStudentImport(ctx))
Object.assign(ctx, useExamImport(ctx))
Object.assign(ctx, useKnowledgeHeatmap(ctx))
Object.assign(ctx, useMoreMenu(ctx))
Object.assign(ctx, useClassTableLifecycle(ctx))
const {
  classDialogVisible, classFormName, editingClassId, savingClass, openCreateClass,
  openEditCurrentClass, saveClass, deleteCurrentClass, editStudentVisible, editStudentForm,
  savingEdit, submitPassword, openStudent, editStudent, saveSubmitPassword, clearSubmitPassword,
  saveEditStudent, removeStudent, studentDialogVisible, studentImportMode, manualStudents,
  studentExcelFile, studentExcelPreview, studentImageFile, recognizedStudents, recognizing,
  savingStudents, columnText, onStudentExcelChange, onStudentImageChange, recognizeImage,
  saveStudents, examDialogVisible, examName, examFile, examDate, examPreview, importingExam,
  examIssues, onExamFileChange, importExamScores, heatmapVisible, heatmapLoading, heatmapTree,
  priorityMeta, openCoursewareRecommendation, submissionDialogVisible, onMoreCommand,
} = ctx
</script>

<style scoped src="./class-table/class-table.css"></style>
