<template>
  <div class="students-page page-shell">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <div class="card-head-left">
            <el-input
              v-model="keyword"
              placeholder="搜索姓名 / 学号"
              clearable
              style="max-width: 240px"
              @keyup.enter="load"
            >
              <template #prefix><el-icon><Search /></el-icon></template>
            </el-input>
            <el-button type="primary" @click="load">查询</el-button>
            <HelpButton
              title="学生列表"
              content="列表按当前账号可见班级显示学生。学号用于区分同名学生，统计按钮会打开该学生的正确率与错题分布。"
            />
          </div>
          <div class="card-head-right">
            <HelpButton title="添加学生" content="可手动录入姓名和学号，也可在班级管理中用 Excel 或图片批量导入。姓名相同的学生请填写学号确认身份。" />
            <el-button type="success" @click="openDialog()">添加学生</el-button>
          </div>
        </div>
      </template>

      <el-table :data="rows" v-loading="loading" stripe :default-sort="{ prop: 'student_no', order: 'ascending' }" :header-cell-style="{ background: 'var(--surface-subtle)', color: 'var(--ink-muted)' }" :row-style="{ cursor: 'default' }" :cell-style="{ transition: 'background 0.2s' }">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column prop="name" label="姓名" />
        <el-table-column prop="student_no" label="学号" sortable :sort-method="sortStudentNo" />
        <el-table-column prop="class_name" label="班级" />
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button text type="primary" size="small" @click="openDialog(row)">编辑</el-button>
            <el-button text type="info" size="small" @click="viewStats(row)">统计</el-button>
            <el-popconfirm title="确定删除该学生？" @confirm="onDelete(row.id)">
              <template #reference>
                <el-button text type="danger" size="small">删除</el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无数据" />
        </template>
      </el-table>
    </el-card>

    <!-- 添加/编辑对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editId ? '编辑学生' : '添加学生'"
      width="500px"
      destroy-on-close
    >
      <el-form :model="studentForm" label-width="80px" :rules="rules" ref="formRef">
        <el-form-item label="姓名" prop="name">
          <el-input v-model="studentForm.name" placeholder="请输入姓名" />
        </el-form-item>
        <el-form-item label="学号" prop="student_no">
          <el-input v-model="studentForm.student_no" placeholder="选填，可留空" />
        </el-form-item>
        <el-form-item label="班级" prop="class_id">
          <el-select v-model="studentForm.class_id" placeholder="请选择班级" style="width: 100%">
            <el-option
              v-for="cls in auth.classes"
              :key="cls.id"
              :label="cls.class_name"
              :value="cls.id"
            />
          </el-select>
          <div v-if="auth.classes.length === 0" class="no-class-tip">
            还没有班级，请先在顶部「班级」菜单中创建
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import api from '@/api'
import { useAuthStore } from '@/stores/auth'
import { compareStudentNumbers } from '@/utils/studentSort'
import HelpButton from '@/components/HelpButton.vue'

const router = useRouter()
const auth = useAuthStore()
const keyword = ref('')
const rows = ref([])
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const editId = ref(null)
const formRef = ref(null)

const studentForm = reactive({
  name: '',
  student_no: '',
  class_id: null,
})

const rules = {
  name: [{ required: true, message: '请输入姓名', trigger: 'blur' }],
  student_no: [],  // 学号改为选填，不阻断添加
  class_id: [{ required: true, message: '请选择班级', trigger: 'change' }],
}

async function load() {
  loading.value = true
  try {
    const data = await api.student.list({ keyword: keyword.value || undefined })
    rows.value = data.items || data.students || data || []
  } catch {
    rows.value = []
  } finally {
    loading.value = false
  }
}

function openDialog(row) {
  if (row) {
    editId.value = row.id
    studentForm.name = row.name || ''
    studentForm.student_no = row.student_no || ''
    studentForm.class_id = row.class_id || null
  } else {
    editId.value = null
    studentForm.name = ''
    studentForm.student_no = ''
    // 默认选中当前切换的班级
    studentForm.class_id = auth.currentClassId || null
  }
  dialogVisible.value = true
}

async function onSave() {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }

  saving.value = true
  try {
    const payload = {
      name: studentForm.name,
      student_no: studentForm.student_no,
      class_id: studentForm.class_id,
    }
    if (editId.value) {
      await api.student.update(editId.value, payload)
      ElMessage.success('更新成功')
    } else {
      await api.student.create(payload)
      ElMessage.success('添加成功')
    }
    dialogVisible.value = false
    load()
    auth.fetchClasses()  // 刷新班级学生数
  } catch {
    /* error handled by interceptor */
  } finally {
    saving.value = false
  }
}

async function onDelete(id) {
  try {
    await api.student.remove(id)
    ElMessage.success('已删除')
    load()
    auth.fetchClasses()  // 刷新班级学生数
  } catch {
    /* error handled by interceptor */
  }
}

function sortStudentNo(a, b) {
  return compareStudentNumbers(a.student_no, b.student_no)
}

function viewStats(row) {
  router.push(`/stats/${row.id}`)
}

onMounted(() => {
  load()
  auth.fetchClasses()
})
</script>

<style scoped>
.no-class-tip {
  color: var(--warning);
  font-size: var(--fs-xs);
  margin-top: 4px;
}

@media (max-width: 640px) {
  .card-head {
    align-items: stretch;
    flex-direction: column;
  }

  .card-head-left,
  .card-head-left .el-input {
    width: 100%;
    max-width: none !important;
  }
}
</style>
