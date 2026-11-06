import { useAuthStore } from '@/stores/auth'
import { educationProfileRedirect } from '@/utils/educationStage'

// Runs before every navigation (router/index.js): login, session
// verification, admin-only pages and the teaching-profile requirement.
export async function authorizeNavigation(to) {
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
}
