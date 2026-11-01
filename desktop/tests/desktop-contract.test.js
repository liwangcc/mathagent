import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { access, readFile } from 'node:fs/promises'
import test from 'node:test'

const desktopFileUrl = (relativePath) =>
  new URL(`../${relativePath}`, import.meta.url)

const readDesktopFile = (relativePath) =>
  readFile(desktopFileUrl(relativePath), 'utf8')

test('Tauri loads only the local desktop frontend bundle', async () => {
  const config = JSON.parse(await readDesktopFile('src-tauri/tauri.conf.json'))

  assert.equal(config.build.beforeDevCommand, 'npm run build:prepare')
  assert.equal(config.build.beforeBuildCommand, 'npm run build:prepare')
  assert.equal(config.build.frontendDist, '../../frontend/dist-desktop')
  assert.equal('devUrl' in config.build, false)
  assert.equal(config.app.windows[0].url, 'index.html')
  assert.equal(config.app.windows[0].backgroundColor, '#f4f5f3')
  assert.match(config.app.security.csp, /connect-src[^;]*https:\/\/mathagent\.com\.cn/)
  assert.doesNotMatch(config.app.security.csp, /connect-src[^;]*https:\/\/www\.mathagent\.com\.cn/)
  assert.deepEqual(config.bundle.externalBin, ['binaries/mathagent-sidecar'])
})

test('desktop IPC exposes only runtime metadata, binding, and bounded worker controls', async () => {
  const main = await readDesktopFile('src-tauri/src/main.rs')
  const commands = await readDesktopFile('src-tauri/src/commands.rs')

  assert.match(main, /commands::runtime_info/)
  assert.match(main, /commands::open_web_fallback/)
  assert.match(main, /commands::get_worker_status/)
  assert.match(main, /commands::set_resource_mode/)
  assert.match(main, /commands::pause_worker/)
  assert.match(main, /commands::resume_worker/)
  assert.match(main, /commands::canonicalize_template_image/)
  assert.match(main, /desktop_auth::desktop_binding_status/)
  assert.match(main, /desktop_auth::bind_teacher_desktop/)
  assert.match(main, /desktop_auth::unbind_teacher_desktop/)
  assert.match(main, /desktop_auth::rotate_teacher_desktop/)
  assert.match(main, /\.plugin\(tauri_plugin_shell::init\(\)\)/)
  assert.match(main, /std::thread::spawn/)
  assert.match(main, /let _ = sidecar::start\(&handle\)/)
  assert.match(main, /let _ = desktop_auth::restore_handoff\(&handle\)/)
  assert.doesNotMatch(main, /let _ = sidecar::start\(app\.handle\(\)\)/)
  assert.doesNotMatch(main, /sidecar::start\([^;]+\?;/)
  assert.doesNotMatch(main, /choose_paper|upload|shell_command/)
  assert.match(commands, /pub fn runtime_info\(/)
  assert.match(commands, /pub fn open_web_fallback\(\) -> Result<\(\), String>/)
  assert.doesNotMatch(commands, /pub fn open_web_fallback\([^)]*(?:url|String)/i)
  assert.match(commands, /option_env!\("MATHAGENT_WEB_FALLBACK_URL"\)/)
  assert.match(commands, /parsed\.scheme\(\) != "https"/)
  assert.match(commands, /parsed\.query\(\)\.is_some\(\)/)
  assert.match(commands, /parsed\.fragment\(\)\.is_some\(\)/)
  assert.match(commands, /web_fallback_configured/)
  assert.match(commands, /pub async fn get_worker_status\(/)
  assert.match(commands, /pub async fn set_resource_mode\(/)
  assert.match(commands, /pub async fn pause_worker\(/)
  assert.match(commands, /pub async fn resume_worker\(/)
  assert.match(commands, /pub async fn canonicalize_template_image\(/)
  assert.match(commands, /InvokeBody::Raw/)
  assert.match(commands, /MAX_TEMPLATE_SOURCE_BYTES/)
  assert.doesNotMatch(commands, /canonicalize_template_image\([^)]*(?:path|String)/i)
})

test('desktop registration stores node credentials natively and configures sidecar only over stdin', async () => {
  const auth = await readDesktopFile('src-tauri/src/desktop_auth.rs')
  const sidecar = await readDesktopFile('src-tauri/src/sidecar.rs')
  const cargo = await readDesktopFile('src-tauri/Cargo.toml')

  assert.match(auth, /option_env!\("MATHAGENT_API_ORIGIN"\)/)
  assert.match(auth, /parsed\.scheme\(\) != "https"/)
  assert.match(auth, /CredWriteW/)
  assert.match(auth, /CredReadW/)
  assert.match(auth, /CredDeleteW/)
  assert.match(auth, /CRED_PERSIST_LOCAL_MACHINE/)
  assert.match(auth, /sidecar::configure_handoff/)
  assert.doesNotMatch(auth, /pub struct DesktopBindingStatus[\s\S]{0,300}(?:token:|node_token)/)
  assert.match(sidecar, /"configure_handoff"/)
  assert.match(sidecar, /"clear_handoff"/)
  assert.match(sidecar, /"handoffConfig"/)
  assert.match(sidecar, /\.stdin\(Stdio::piped\(\)\)/)
  assert.doesNotMatch(sidecar, /MATHAGENT_ACCELERATOR_TOKEN/)
  assert.match(cargo, /"Win32_Security_Credentials"/)
})

test('rebinding an existing desktop credential repairs stale server authorization', async () => {
  const auth = await readDesktopFile('src-tauri/src/desktop_auth.rs')
  const guide = await readFile(
    new URL('../../frontend/src/components/UsageGuideDialog.vue', import.meta.url),
    'utf8',
  )
  const manager = await readFile(
    new URL('../../frontend/src/composables/useDesktopBindingDialog.js', import.meta.url),
    'utf8',
  )

  assert.match(auth, /fn request_node_status\(/)
  assert.match(auth, /\/api\/accelerator\/desktop\/devices\//)
  assert.match(auth, /ureq::get\(&url\)/)
  assert.match(auth, /Err\(ureq::Error::Status\(404, _\)\) => return Ok\(None\)/)
  assert.match(auth, /repair_existing_credential_with\(/)
  assert.match(auth, /\|node_id\| request_node_status\(origin, bearer, node_id\)/)
  assert.match(auth, /\|node_id\| request_rotation\(origin, bearer, node_id\)/)
  assert.match(auth, /\|device_id\| request_registration\(origin, bearer, device_id\)/)
  assert.match(auth, /get_status\(&existing\.node_id\)\?/)
  assert.match(manager, /invoke\('bind_teacher_desktop'/)
  assert.match(guide, /bindingManager\.open\('guide'\)/)
  assert.match(guide, /handoff\?\.connected === true/)
})

test('production release build requires explicit validated API and web HTTPS origins', async () => {
  const release = await readDesktopFile('scripts/build-release.ps1')
  const packageJson = JSON.parse(await readDesktopFile('package.json'))

  assert.match(release, /Parameter\(Mandatory = \$true\)/)
  assert.match(release, /Assert-HttpsOrigin "ApiOrigin"/)
  assert.match(release, /Assert-HttpsOrigin "WebOrigin"/)
  assert.match(release, /\$env:MATHAGENT_API_ORIGIN/)
  assert.match(release, /\$env:VITE_API_ORIGIN = \$ValidatedApiOrigin/)
  assert.match(release, /Remove-Item Env:\\VITE_API_ORIGIN/)
  assert.match(release, /\$env:MATHAGENT_WEB_FALLBACK_URL/)
  assert.match(release, /function Find-Cargo/)
  assert.match(release, /Get-Command cargo\.exe/)
  assert.match(release, /D:\\DevTools\\Rust\\rustup\\toolchains/)
  assert.match(release, /\[Version\]'1\.85\.0'/)
  assert.match(release, /Test-Path -LiteralPath \$rustc -PathType Leaf/)
  assert.match(release, /Cargo = \$candidate/)
  assert.match(release, /Rustc = \$rustc/)
  assert.match(release, /Sort-Object Version -Descending/)
  assert.match(release, /complete Rust MSVC toolchain/)
  assert.match(release, /\$env:CARGO = \$RustToolchain\.Cargo/)
  assert.match(release, /\$env:RUSTC = \$RustToolchain\.Rustc/)
  assert.match(release, /Split-Path -Parent \$RustToolchain\.Cargo/)
  assert.match(release, /\$env:Path = \$PreviousPath/)
  assert.match(release, /\$env:CARGO = \$PreviousCargo/)
  assert.match(release, /\$env:RUSTC = \$PreviousRustc/)
  assert.match(release, /npx tauri build/)
  assert.equal(packageJson.scripts['build:release'], 'powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-release.ps1')
})

test('desktop shell and packaged sidecar use one release version', async () => {
  const packageJson = JSON.parse(await readDesktopFile('package.json'))
  const packageLock = JSON.parse(await readDesktopFile('package-lock.json'))
  const tauriConfig = JSON.parse(await readDesktopFile('src-tauri/tauri.conf.json'))
  const cargoManifest = await readDesktopFile('src-tauri/Cargo.toml')
  const cargoLock = await readDesktopFile('src-tauri/Cargo.lock')
  const workerManifest = await readDesktopFile('worker/pyproject.toml')
  const workerPackage = await readDesktopFile('worker/mathagent_worker/__init__.py')
  const sidecarBuild = await readDesktopFile('scripts/build-sidecar.ps1')
  const version = packageJson.version
  const escapedVersion = version.replaceAll('.', '\\.')

  assert.equal(packageLock.version, version)
  assert.equal(packageLock.packages[''].version, version)
  assert.equal(tauriConfig.version, version)
  assert.match(cargoManifest, new RegExp(`^version = "${escapedVersion}"$`, 'm'))
  assert.match(cargoLock, new RegExp(`name = "mathagent-desktop"\\r?\\nversion = "${escapedVersion}"`))
  assert.match(workerManifest, new RegExp(`^version = "${escapedVersion}"$`, 'm'))
  assert.match(workerPackage, new RegExp(`__version__ = "${escapedVersion}"`))
  assert.match(sidecarBuild, /\$ExpectedVersion = \[string\]\$DesktopPackage\.version/)
  assert.match(sidecarBuild, /\$Health\.version -ne \$ExpectedVersion/)
  assert.match(sidecarBuild, /\$PackagedVersion\.Trim\(\) -ne \$ExpectedVersion/)
})

test('desktop guide offers only the fixed web fallback for unavailable processors', async () => {
  const guide = await readFile(
    new URL('../../frontend/src/components/UsageGuideDialog.vue', import.meta.url),
    'utf8',
  )

  assert.match(guide, /desktopWorkerStatus\.value\?\.webFallbackRequired === true/)
  assert.match(guide, /desktopWorkerStatus\.value\?\.paused !== true/)
  assert.match(guide, /runtimeInfo\?\.webFallbackConfigured/)
  assert.match(guide, /desktopInvoke\('open_web_fallback'\)/)
  assert.match(guide, /本机处理器暂不可用/)
  assert.match(guide, /暂时无法打开网页版/)
  assert.doesNotMatch(guide, /window\.open|https?:\/\//)
})

test('desktop settings surface the bounded GPU self-test without exposing paths', async () => {
  const settings = await readFile(
    new URL('../../frontend/src/views/Settings.vue', import.meta.url),
    'utf8',
  )

  assert.match(settings, /本地加速/)
  assert.match(settings, /NVIDIA GPU/)
  assert.match(settings, /window\.__TAURI_INTERNALS__\?\.invoke/)
  assert.match(settings, /desktopInvoke\('get_worker_status'\)/)
  assert.match(settings, /computeBackend: 'none'/)
  assert.match(settings, /localProcessingAvailable: false/)
  assert.match(settings, /本地加速不可用/)
  assert.match(settings, /GPU 启动自检/)
  assert.match(settings, /异常时自动回退 CPU/)
  assert.match(settings, /handoff/)
  assert.match(settings, /completed/)
  assert.match(settings, /failed/)
  assert.match(settings, /remaining/)
  assert.doesNotMatch(settings, /MATHAGENT_ACCELERATOR_TOKEN|credential|artifactPath|node_token|lease_id|source_url|template_url/)
})

test('capabilities keep filesystem, dialog, and shell permissions disabled', async () => {
  const capability = JSON.parse(
    await readDesktopFile('src-tauri/capabilities/default.json'),
  )

  assert.deepEqual(capability.permissions, [])
})

test('Windows bundles use the MathAgent icon for the app and installer', async () => {
  const config = JSON.parse(await readDesktopFile('src-tauri/tauri.conf.json'))
  const sidecarBuild = await readDesktopFile('scripts/build-sidecar.ps1')
  const approvedIconHashes = new Map([
    ['icons/app-icon-source.png', '743587800f4d2d4527d74bfaba3738d13f8773ba253d9825555d4e82ecdbedd5'],
    ['icons/32x32.png', '6759aea622e161e718191aeb73bd0db430e01a8e1ff93b6c60b5586cbb5c44a5'],
    ['icons/128x128.png', 'fff7f8e8500b068bdeedbc4ea08ee9b94dca89057421ac17dff51e980f996b2e'],
    ['icons/icon.ico', '63f0b4b9e6602b035012e096c626949040f991e5580c056897f24f72dda8cbdd'],
  ])
  const iconPaths = [
    'icons/32x32.png',
    'icons/128x128.png',
    'icons/icon.ico',
  ]

  assert.deepEqual(config.bundle.icon, iconPaths)
  assert.equal(config.bundle.windows.nsis.installerIcon, 'icons/icon.ico')
  assert.equal(config.bundle.windows.nsis.uninstallerIcon, 'icons/icon.ico')
  assert.match(sidecarBuild, /--icon \$Icon/)
  assert.match(sidecarBuild, /vcruntime140\.dll/)
  assert.match(sidecarBuild, /Microsoft-VC-Runtime-NOTICE\.txt/)
  await Promise.all(
    [...approvedIconHashes].map(async ([path, expectedHash]) => {
      const url = desktopFileUrl(`src-tauri/${path}`)
      await access(url)
      const digest = createHash('sha256').update(await readFile(url)).digest('hex')
      assert.equal(digest, expectedHash)
    }),
  )
})

test('the main window and Windows taskbar receive the bundled icon at runtime', async () => {
  const main = await readDesktopFile('src-tauri/src/main.rs')
  const cargo = await readDesktopFile('src-tauri/Cargo.toml')

  assert.match(main, /default_window_icon\(\)/)
  assert.match(main, /get_webview_window\("main"\)/)
  assert.match(main, /window\.set_icon\(icon\)/)
  assert.match(main, /WINDOW_ICON_RESOURCE_ID: u16 = 32512/)
  assert.match(main, /EnumWindows/)
  assert.match(main, /IsWindowVisible/)
  assert.match(main, /SetClassLongPtrW\(search\.hwnd, GCLP_HICON/)
  assert.match(main, /SetClassLongPtrW\(search\.hwnd, GCLP_HICONSM/)
  assert.match(main, /WM_SETICON,\s+ICON_BIG/)
  assert.match(main, /WM_SETICON,\s+ICON_SMALL/)
  assert.match(main, /SM_CXICON/)
  assert.match(main, /SM_CXSMICON/)
  assert.match(main, /tauri::RunEvent::MainEventsCleared/)
  assert.match(cargo, /"Win32_System_LibraryLoader"/)
  assert.match(cargo, /"Win32_UI_WindowsAndMessaging"/)
})

test('NSIS automatically creates the desktop shortcut after installation', async () => {
  const config = JSON.parse(await readDesktopFile('src-tauri/tauri.conf.json'))
  const hooks = await readDesktopFile('src-tauri/installer-hooks.nsh')

  assert.equal(config.bundle.windows.nsis.installerHooks, 'installer-hooks.nsh')
  assert.match(hooks, /!macro NSIS_HOOK_POSTINSTALL/)
  assert.match(hooks, /\$NoShortcutMode <> 1/)
  assert.match(hooks, /Call CreateOrUpdateDesktopShortcut/)
  assert.match(hooks, /CreateShortcut "\$DESKTOP\\\$\{PRODUCTNAME\}\.lnk"/)
})

test('sidecar startup and forced exit are bounded and process-tree aware', async () => {
  const sidecar = await readDesktopFile('src-tauri/src/sidecar.rs')

  assert.match(sidecar, /HEALTH_CHECK_TIMEOUT/)
  assert.match(sidecar, /INITIAL_STATUS_TIMEOUT/)
  assert.match(sidecar, /REQUEST_TIMEOUT/)
  assert.match(sidecar, /MAX_PROTOCOL_BYTES/)
  assert.match(sidecar, /sync_channel\(4\)/)
  assert.match(sidecar, /request_worker_status\(process, command, mode, REQUEST_TIMEOUT\)/)
  assert.match(sidecar, /recv_timeout\(timeout\)/)
  assert.match(sidecar, /"requestId"/)
  assert.match(sidecar, /stdout\(Stdio::piped\(\)\)/)
  assert.match(sidecar, /JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE/)
  assert.match(sidecar, /AssignProcessToJobObject/)
  assert.match(sidecar, /TerminateJobObject/)
  assert.match(sidecar, /windows_nvidia_gpu/)
  assert.match(sidecar, /gpu_acceleration/)
  assert.match(sidecar, /const PROTOCOL_VERSION: u32 = 5/)
  assert.match(sidecar, /"canonicalize_template"/)
  assert.match(sidecar, /TEMPLATE_CANONICALIZATION_TIMEOUT/)
  assert.match(sidecar, /let allowed_cpu_counts = \[0, 2, 3\]/)
  assert.match(sidecar, /cpu_acceleration/)
  assert.match(sidecar, /local_processing_available/)
  assert.match(sidecar, /local_processor_state/)
  assert.match(sidecar, /web_fallback_required/)
  assert.doesNotMatch(sidecar, /process\.child\.kill\(\)/)
})
