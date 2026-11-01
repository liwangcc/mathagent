import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { educationProfileRedirect } from '@/utils/educationStage'

const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/Login.vue'),
    meta: { public: true },
  },
  {
    // Opened on a phone from the grading page's QR code; authorized only by
    // the session token in the URL fragment, never by a login.
    path: '/capture/:sessionId',
    name: 'phone-capture',
    component: () => import('@/views/PhoneCapture.vue'),
    meta: { public: true, standalone: true, title: '手机拍摄' },
  },
  {
    // A class upload link students open without an account; the server checks
    // the name + student number they type against the class roster.
    path: '/submit/:token',
    name: 'student-submit',
    component: () => import('@/views/StudentSubmit.vue'),
    meta: { public: true, standalone: true, title: '提交试卷照片' },
  },
  {
    path: '/',
    component: () => import('@/views/Layout.vue'),
    children: [
      { path: '', name: 'home', component: () => import('@/views/Home.vue'), meta: { title: '首页' } },
      { path: 'upload', name: 'upload', component: () => import('@/views/Upload.vue'), meta: { title: '试卷批改' } },
      { path: 'anonymous-grade', name: 'anonymous-grade', component: () => import('@/views/AnonymousGrade.vue'), meta: { title: '无身份批阅' } },
      { path: 'answer-sheet', name: 'answer-sheet', component: () => import('@/views/AnswerSheet.vue'), meta: { title: '答题卡批阅' } },
      { path: 'students', name: 'students', component: () => import('@/views/ClassTable.vue'), meta: { title: '班级管理' } },
      { path: 'students/legacy', name: 'students-legacy', component: () => import('@/views/Students.vue'), meta: { title: '学生管理' } },
      { path: 'class/:classId/students', name: 'class-students', component: () => import('@/views/ClassTable.vue'), meta: { title: '班级管理' } },
      { path: 'student/:studentId/detail', name: 'student-detail', component: () => import('@/views/StudentDetail.vue'), meta: { title: '学生详情' } },
      { path: 'reports', name: 'reports', component: () => import('@/views/Reports.vue'), meta: { title: '成绩报告' } },
      { path: 'bank', name: 'bank', component: () => import('@/views/Bank.vue'), meta: { title: '题库管理' } },
      { path: 'exercise', name: 'exercise', component: () => import('@/views/Exercise.vue'), meta: { title: '出题练习' } },
      { path: 'courseware', name: 'courseware', component: () => import('@/views/Courseware.vue'), meta: { title: '课件库' } },
      { path: 'bank/photo-import', name: 'bank-photo-import', component: () => import('@/views/BankPhotoImport.vue'), meta: { title: '拍照导入题库' } },
      { path: 'bank/past-papers', name: 'past-papers', component: () => import('@/views/PastPapers.vue'), meta: { title: '整卷真题' } },
      { path: 'pending-answers', name: 'pending-answers', component: () => import('@/views/PendingAnswers.vue'), meta: { title: '待入库' } },
      { path: 'stats', name: 'stats', component: () => import('@/views/Stats.vue'), meta: { title: '统计分析' } },
      { path: 'grade-records', name: 'grade-records', component: () => import('@/views/GradeRecords.vue'), meta: { title: '批改记录' } },
      { path: 'stats/:id', name: 'stats-detail', component: () => import('@/views/Stats.vue'), meta: { title: '统计分析' } },
      { path: 'profile', name: 'profile', component: () => import('@/views/Profile.vue'), meta: { title: '个人资料' } },
      { path: 'settings', name: 'settings', component: () => import('@/views/Settings.vue'), meta: { title: '系统设置', admin: true } },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  // A phone may still hold an expired teacher login; verifying it would 401
  // and bounce the capture page to /login, so standalone pages skip auth.
  if (to.meta.standalone) return true
  const auth = useAuthStore()
  if (!to.meta.public && !auth.token) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (auth.token && !auth.userVerified) {
    try {
      await auth.fetchMe()
      // An account switch while /me was pending must not verify the new session.
      if (auth.token && !auth.userVerified) return false
    } catch {
      auth.clearSession()
      if (!to.meta.public) {
        return { name: 'login', query: { redirect: to.fullPath } }
      }
    }
  }
  if (!to.meta.public && !auth.token) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && auth.token) {
    return { path: '/' }
  }
  // 仪表盘仅管理员可访问
  if (to.meta.admin && auth.user?.role !== 'admin') {
    return { path: '/' }
  }
  const profileRedirect = educationProfileRedirect(to, auth)
  if (profileRedirect) return profileRedirect
})

export default router
