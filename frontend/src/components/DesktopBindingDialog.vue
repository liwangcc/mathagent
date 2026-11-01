<template>
  <el-dialog
    :model-value="state.visible"
    title="本机绑定管理"
    width="min(560px, 94vw)"
    append-to-body
    :close-on-click-modal="false"
    :close-on-press-escape="!state.busy"
    :show-close="!state.busy"
    @update:model-value="onVisibilityChange"
  >
    <p>设置、个人信息和使用引导均在这里管理本机绑定。凭据只保存在 Windows 凭据管理器。</p>
    <el-descriptions :column="1" border>
      <el-descriptions-item label="当前账号">{{ auth.user?.name || auth.user?.username || '未登录' }}</el-descriptions-item>
      <el-descriptions-item label="本机登记">{{ state.binding?.configured ? '已登记' : '未登记' }}</el-descriptions-item>
      <el-descriptions-item label="账号校验">{{ state.verified ? '已校验当前账号' : '尚未校验，请点击绑定或重新绑定' }}</el-descriptions-item>
      <el-descriptions-item label="本机处理">{{ processorStatus }}</el-descriptions-item>
    </el-descriptions>
    <el-alert v-if="state.error" class="binding-notice" type="error" :closable="false" show-icon :title="state.error" />
    <el-alert v-else-if="state.verified && !ready" class="binding-notice" type="warning" :closable="false" show-icon title="账号已校验，本机处理尚未就绪；正在等待连接或硬件自检。可重新检测状态，或返回引导选择云端接管。" />
    <el-alert v-else-if="state.verified && ready" class="binding-notice" type="success" :closable="false" show-icon title="本机绑定已完成，处理器自检和服务器连接均已就绪。" />
    <div class="binding-actions">
      <el-button type="primary" :loading="state.busy" @click="manager.bind()">{{ state.binding?.configured ? '重新绑定并检测' : '绑定并检测' }}</el-button>
      <el-button :disabled="state.busy" @click="manager.refresh()">重新检测状态</el-button>
      <el-popconfirm v-if="state.binding?.configured" title="确定解除本机绑定？本机会停止接收新任务。" width="280" @confirm="manager.unbind()">
        <template #reference><el-button type="danger" plain :disabled="state.busy">解除绑定</el-button></template>
      </el-popconfirm>
    </div>
    <template #footer>
      <el-button :disabled="state.busy" @click="manager.close()">{{ state.source === 'guide' ? '返回使用引导' : '完成' }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, onUnmounted, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { desktopProcessorReady, useDesktopBindingDialog } from '@/composables/useDesktopBindingDialog'

const auth = useAuthStore()
const manager = useDesktopBindingDialog(auth)
const state = manager.state
const ready = computed(() => desktopProcessorReady(state.binding, state.worker))
const processorStatus = computed(() => {
  if (!state.worker) return '尚未读取状态'
  if (state.worker.paused === true) return '已暂停；绑定检测时会恢复'
  if (state.worker.handoff?.lastErrorCode === 'node_auth_failed') return '授权已失效，请重新绑定'
  if (!ready.value) return '暂不可用，云端仍可接管'
  return state.worker.computeBackend === 'windows_nvidia_gpu' ? 'NVIDIA GPU 已就绪' : 'CPU 已就绪'
})

watch(() => state.visible, visible => { if (visible) manager.refresh() })
watch(() => [auth.user?.id, auth.user?.username, auth.token], () => manager.invalidateSession(), { flush: 'sync' })
onUnmounted(() => manager.invalidateSession())

function onVisibilityChange(visible) {
  if (!visible) manager.close()
}
</script>

<style scoped>
p { margin-top: 0; color: var(--ink-muted); line-height: 1.6; }
.binding-notice { margin-top: 16px; }
.binding-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 20px; }
.binding-actions :deep(.el-button + .el-button) { margin-left: 0; }
</style>
