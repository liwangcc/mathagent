import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const readDesktopFile = (relativePath) =>
  readFile(new URL(`../${relativePath}`, import.meta.url), 'utf8')

const readDesktopBytes = (relativePath) =>
  readFile(new URL(`../${relativePath}`, import.meta.url))

test('platform node accepts only explicit platform credentials from stdin', async () => {
  const cli = await readDesktopFile('worker/mathagent_worker/__main__.py')
  const runtime = await readDesktopFile('worker/mathagent_worker/platform_node.py')

  assert.match(cli, /--configure-platform-node/)
  assert.match(cli, /configure_platform_credential\(sys\.stdin\)/)
  assert.match(cli, /reconfigure\(encoding="utf-8-sig", errors="strict"\)/)
  assert.match(runtime, /CREDENTIAL_TARGET = "MathAgent\/PlatformAccelerator\/v1"/)
  assert.match(runtime, /ACCESS_SCOPE = "platform"/)
  assert.match(runtime, /parsed\.scheme != "https"/)
  assert.match(runtime, /CredWriteW/)
  assert.match(runtime, /CredReadW/)
  assert.match(runtime, /CredDeleteW/)
  assert.doesNotMatch(runtime, /print\([^\n]*(?:token|credential)/i)
})

test('platform node reuses sidecar capacity and handoff lifecycle', async () => {
  const runtime = await readDesktopFile('worker/mathagent_worker/platform_node.py')

  assert.match(runtime, /DesktopHandoffRunner/)
  assert.match(runtime, /CpuSupervisor/)
  assert.match(runtime, /_local_policy/)
  assert.match(runtime, /runner\.configure_local_policy/)
  assert.match(runtime, /response\.get\("access_scope"\) != ACCESS_SCOPE/)
  assert.match(runtime, /runner\.stop\(timeout=30\.0\)/)
  assert.match(runtime, /"hardware_unavailable"/)
  assert.match(runtime, /"credential_rejected"/)
})

test('platform node scheduled task restarts and never carries token in arguments', async () => {
  const install = await readDesktopFile('scripts/install-platform-node.ps1')
  const uninstall = await readDesktopFile('scripts/uninstall-platform-node.ps1')
  const status = await readDesktopFile('scripts/platform-node-status.ps1')

  assert.match(install, /accessScope = "platform"/)
  assert.match(install, /RedirectStandardInput = \$true/)
  assert.match(install, /Read-Host "One-time platform node token" -AsSecureString/)
  assert.match(install, /SecureStringToBSTR/)
  assert.match(install, /ZeroFreeBSTR/)
  assert.match(install, /StandardInput\.Write\(\$credential\)/)
  assert.match(install, /\$processStarted = \$false/)
  assert.match(install, /\$processStarted -and -not \$process\.HasExited/)
  assert.match(install, /New-ScheduledTaskTrigger -AtLogOn/)
  assert.match(install, /-LogonType Interactive/)
  assert.match(install, /-RestartCount 999/)
  assert.match(install, /-RestartInterval \(\[TimeSpan\]::FromMinutes\(1\)\)/)
  assert.match(install, /-MultipleInstances IgnoreNew/)
  assert.match(install, /--platform-node --resource-mode \$ResourceMode/)
  assert.doesNotMatch(install, /-Argument[^\n]*(?:NodeToken|NodeId|ApiOrigin)/)
  assert.match(status, /--platform-node-status/)
  assert.match(uninstall, /--clear-platform-node/)
  assert.match(uninstall, /Unregister-ScheduledTask/)
})

test('portable platform node bundle has an explicit lifecycle switch and integrity checks', async () => {
  const setup = await readDesktopFile('scripts/setup-platform-node.ps1')
  const start = await readDesktopFile('scripts/start-platform-node.ps1')
  const stop = await readDesktopFile('scripts/stop-platform-node.ps1')
  const status = await readDesktopFile('scripts/platform-node-status.ps1')
  const bundle = await readDesktopFile('scripts/make-platform-node-bundle.ps1')
  const easySetup = await readDesktopFile('scripts/easy-setup.cmd')

  assert.match(setup, /-NoStart/)
  assert.match(setup, /管理员提供的 HTTPS 服务地址/)
  assert.match(setup, /node-bootstrap\.json/)
  assert.match(start, /Enable-ScheduledTask/)
  assert.match(start, /runtime\.lastErrorCode/)
  assert.match(start, /could not verify its server registration/)
  assert.match(stop, /taskkill\.exe/)
  assert.match(stop, /\/T \/F/)
  assert.match(status, /TaskEnabled/)
  assert.match(bundle, /SHA256SUMS\.txt/)
  assert.match(bundle, /containsCredentials = \$includeCredentials/)
  assert.match(bundle, /Personalized/)
  assert.match(bundle, /node-bootstrap\.json/)
  assert.match(bundle, /IncludeNodeToken/)
  assert.match(bundle, /CredentialsEmbedded/)
  assert.match(bundle, /不会显示/)
  assert.match(setup, /ConvertTo-SecureString/)
  assert.doesNotMatch(setup, /providerProfiles|apiKey/)
  assert.doesNotMatch(bundle, /IncludeProviderProfiles|providerProfiles|apiKey/)
  assert.match(easySetup, /setup-platform-node\.ps1/)
  assert.match(easySetup, /control-platform-node\.cmd/)
})

test('Windows launchers avoid code-page corruption for Chinese instructions', async () => {
  const [easySetup, controlCmd, setup, control, readme] = await Promise.all([
    readDesktopBytes('scripts/easy-setup.cmd'),
    readDesktopBytes('scripts/control-platform-node.cmd'),
    readDesktopBytes('scripts/setup-platform-node.ps1'),
    readDesktopBytes('scripts/control-platform-node.ps1'),
    readDesktopBytes('scripts/PLATFORM_NODE_README.txt'),
  ])

  for (const launcher of [easySetup, controlCmd]) {
    assert.ok([...launcher].every((byte) => byte <= 0x7f))
  }
  for (const file of [setup, control, readme]) {
    assert.deepEqual([...file.subarray(0, 3)], [0xef, 0xbb, 0xbf])
  }
})
