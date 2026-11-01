import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '@/api'

const TOKEN_KEY = 'mathagent_token'
const USER_KEY = 'mathagent_user'

function normalizeToken(value) {
  if (typeof value !== 'string') return ''
  const normalized = value.trim()
  return normalized && !['undefined', 'null'].includes(normalized.toLowerCase())
    ? normalized
    : ''
}

function loadStoredToken() {
  const token = normalizeToken(localStorage.getItem(TOKEN_KEY))
  if (!token) {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  }
  return token
}

function loadStoredUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || 'null')
  } catch {
    localStorage.removeItem(USER_KEY)
    return null
  }
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref(loadStoredToken())
  const user = ref(loadStoredUser())
  const userVerified = ref(false)
  let sessionRevision = 0
  let meRevision = 0
  let pendingMe = null

  // 班级相关状态
  const classes = ref([])
  const currentClassId = ref(null)
  const maxClasses = ref(4)

  const isLoggedIn = computed(() => !!token.value)
  const currentClass = computed(() =>
    classes.value.find(c => c.id === currentClassId.value) || null
  )
  const currentClassName = computed(() => currentClass.value?.class_name || '全部班级')

  function login(newToken, newUser) {
    const normalizedToken = normalizeToken(newToken)
    if (!normalizedToken) throw new Error('登录响应缺少有效凭据')
    // 清除上一个账号的聊天记录，防止跨账号泄露
    localStorage.removeItem('mathagent_chat_history')
    sessionRevision += 1
    pendingMe = null
    token.value = normalizedToken
    user.value = newUser
    // Login payload/localStorage are only a display cache. /me verifies every new session.
    userVerified.value = false
    classes.value = []
    currentClassId.value = newUser?.current_class_id || null
    localStorage.setItem(TOKEN_KEY, normalizedToken)
    localStorage.setItem(USER_KEY, JSON.stringify(newUser))
  }

  function clearSession() {
    sessionRevision += 1
    pendingMe = null
    token.value = ''
    user.value = null
    userVerified.value = false
    classes.value = []
    currentClassId.value = null
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
    localStorage.removeItem('mathagent_chat_history')
  }

  async function logout() {
    const revision = sessionRevision
    // 先通知后端拉黑当前 token（best-effort，失败也要清本地）
    try {
      if (token.value) await api.auth.logout()
    } catch {
      /* 网络失败不阻塞登出 */
    } finally {
      if (revision === sessionRevision) clearSession()
    }
  }

  async function fetchMe({ force = false } = {}) {
    if (!token.value) return null
    if (pendingMe && !force) return pendingMe
    const revision = sessionRevision
    const requestRevision = ++meRevision
    const requestToken = token.value
    const request = (async () => {
      try {
        const data = await api.auth.me()
        if (revision !== sessionRevision || requestToken !== token.value || requestRevision !== meRevision) return null
        if (!data?.user || typeof data.user !== 'object') throw new Error('用户资料响应无效')
        user.value = data.user
        userVerified.value = true
        currentClassId.value = data.user.current_class_id || null
        localStorage.setItem(USER_KEY, JSON.stringify(data.user))
        return data.user
      } catch (error) {
        if (revision !== sessionRevision || requestToken !== token.value || requestRevision !== meRevision) return null
        userVerified.value = false
        throw error
      } finally {
        if (revision === sessionRevision && requestRevision === meRevision) pendingMe = null
      }
    })()
    pendingMe = request
    return request
  }

  async function updateProfile(payload) {
    const revision = sessionRevision
    const requestToken = token.value
    if (!requestToken) throw new Error('请先登录')
    await api.auth.updateMe(payload)
    if (revision !== sessionRevision || requestToken !== token.value) return null
    // Re-read persisted server state, rather than trusting the submitted default stage.
    await fetchMe({ force: true })
    return revision === sessionRevision ? user.value : null
  }

  async function fetchClasses() {
    const revision = sessionRevision
    try {
      const data = await api.auth.getClasses()
      if (revision !== sessionRevision) return
      classes.value = data.classes || []
      currentClassId.value = data.current_class_id
      maxClasses.value = data.max_classes || 4
    } catch {
      if (revision === sessionRevision) classes.value = []
    }
  }

  async function switchClass(classId) {
    const revision = sessionRevision
    const requestToken = token.value
    await api.auth.switchClass(classId)
    if (revision !== sessionRevision || requestToken !== token.value) return
    currentClassId.value = classId
    // 更新本地存储的用户信息
    if (user.value) {
      user.value.current_class_id = classId
      localStorage.setItem(USER_KEY, JSON.stringify(user.value))
    }
  }

  async function createClass(className) {
    const data = await api.auth.createClass(className)
    await fetchClasses()
    return data
  }

  async function updateClass(classId, className) {
    const data = await api.auth.updateClass(classId, className)
    await fetchClasses()
    return data
  }

  async function deleteClass(classId) {
    const data = await api.auth.deleteClass(classId)
    await fetchClasses()
    // 如果删除的是当前选中的班级，切换到第一个可用班级
    if (currentClassId.value === classId) {
      currentClassId.value = classes.value.length ? classes.value[0].id : null
      if (currentClassId.value && user.value) {
        user.value.current_class_id = currentClassId.value
        localStorage.setItem(USER_KEY, JSON.stringify(user.value))
      }
    }
    return data
  }

  return {
    token, user, userVerified, isLoggedIn,
    classes, currentClassId, currentClass, currentClassName, maxClasses,
    login, logout, clearSession, fetchMe, updateProfile,
    fetchClasses, switchClass, createClass, updateClass, deleteClass,
  }
})
