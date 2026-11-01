# MathAgent Desktop Shell and Local Processing Gate

This directory contains the Windows-only Tauri 2 shell, the bounded NVIDIA GPU
and OpenCV CPU preprocessing sidecar, and the local-capacity gate. OCR, grading,
and authoritative data writes remain server responsibilities.

## Current contract

- The sidecar now packages the bounded Windows-native NVIDIA VulkanSift runtime,
  runs a hash-bound known-answer self-test at startup, reports the selected
  compute backend over protocol v5, and fails closed to `windows_cpu_opencv`
  whenever the runtime, correctness gate, geometry contract, or operation fails.
- The desktop Settings page shows `本地加速：NVIDIA GPU` or `本地加速：CPU`
  only when that complete local route is available. IPC and qualification
  failures are reported as unavailable rather than being presented as CPU.
- GPU output remains an untrusted candidate. The server still owns hash binding,
  CPU replay, geometric gates, authoritative artifacts, and database writes.

- The webview loads the local `frontend/dist-desktop` bundle only.
- Webview IPC is limited to `runtime_info`, `open_web_fallback`,
  `get_worker_status`, `set_resource_mode`, `pause_worker`, and
  `resume_worker`, plus the bounded binary-only `canonicalize_template_image`
  command. The template command accepts one JPEG/PNG byte body, creates its own
  temporary workspace, and returns canonical PNG bytes with path-free metadata;
  it never accepts a path from the WebView. `open_web_fallback` accepts no URL
  argument and can only open the HTTPS origin fixed at build time by
  `MATHAGENT_WEB_FALLBACK_URL`. These
  commands do not expose paths, arbitrary shell execution, credentials, or
  environment variables.
- The packaged sidecar supports `--version`, `--health`, `--capacity`, and
  `--serve`. Its bounded JSON-line protocol is version `5`; worker requests use
  correlated request IDs.
- The same packaged sidecar can run as the administrator-owned opportunistic
  platform node with `--platform-node`. This mode accepts only a node that the
  server reports as `access_scope=platform`, reads its token from the dedicated
  Windows Credential Manager target `MathAgent/PlatformAccelerator/v1`, and
  publishes only a path-free, token-free lifecycle summary under
  `%LOCALAPPDATA%\MathAgent\PlatformNode\status.json`.
- `scripts/install-platform-node.ps1` copies the current sidecar to a stable
  per-user location, sends the one-time token over stdin, and installs an
  at-logon Scheduled Task with bounded single-instance restart behavior.
  `platform-node-status.ps1` and `uninstall-platform-node.ps1` inspect and
  remove that task without reading or printing the credential. Registration
  itself remains an explicit admin API operation; the install script never
  creates or silently widens a tenant node.
- Its worker status reports CPU and GPU qualification separately, plus explicit
  `localProcessingAvailable`, `localProcessorState`, and
  `webFallbackRequired` fields. Protocol v5 is rejected if those fields are
  absent or inconsistent.
- New template uploads try local OpenCV canonicalization before network upload.
  The desktop submits the original image, exact canonical PNG bytes, and a
  path-free metadata array bound by source/canonical SHA-256. The server
  validates and freezes those exact bytes. Web clients, paused workers, and
  unavailable local processors omit the candidate fields and use the existing
  server canonicalization fallback.
- Local CPU scheduling only permits `0`, `2`, or `3` workers. A single
  worker is never selected. The first policy requires at least four physical
  cores, 2.5 GiB currently available memory, CPU load below 80 percent, and a
  qualifying two-worker real preprocessing benchmark.
- The sidecar packages both the Windows-native NVIDIA VulkanSift path and the
  OpenCV CPU fallback. The current development bootstrap reads an accelerator
  origin, node ID, and node token from three process environment variables. If
  all three are present, it opens an outbound-only HTTPS heartbeat/lease loop,
  downloads hash-bound source/template assets into a per-job temporary
  directory, uploads aligned PNG and source-mask PNG artifacts, submits only an
  untrusted JSON candidate, and advances completed/failed/remaining counters
  even when one item fails. The server remains authoritative and replays
  geometry before any OCR use.
- Without that protected node configuration, the sidecar still reports
  `server_required` with `handoffState=not_integrated`; detecting a GPU alone
  never claims that production paper jobs are routed locally.
- Closing the main window hides it. The tray can reopen it or exit the whole
  process. The tray pause/resume action controls both capacity decisions and
  the configured outbound handoff loop.
- Sidecar health probing is bounded to five seconds so a damaged binary cannot
  block desktop startup indefinitely. A missing, timed-out, or contract-invalid
  sidecar does not terminate the desktop shell; the local UI can still offer
  the controlled web fallback.
- The first status request has a bounded 30-second allowance for the packaged
  GPU known-answer test and the real two/three-worker CPU qualification. Later
  control/status requests remain bounded to three seconds.
- A full exit gives the sidecar two seconds to stop cleanly, then terminates
  its complete PyInstaller process tree through a Windows Job Object. Closing
  the desktop process also closes the Job and terminates surviving descendants.

No filesystem, dialog, shell, generic URL-opener, or environment-variable
permission is granted to the webview capability. The production webpage is
opened in the system browser and is never loaded into the privileged Tauri
window. No desktop token is appended to the fallback URL.

The production webpage origin is intentionally not stored in source control.
Release builds may set, for example:

```powershell
$env:MATHAGENT_WEB_FALLBACK_URL = "https://math.example.com"
```

Only a bare HTTPS origin is accepted: credentials, paths, query strings,
fragments, and non-HTTPS schemes are rejected. If no origin is configured, the
runtime reports only `webFallbackConfigured=false` and the UI tells the user to
contact the administrator instead of opening an arbitrary address.

The environment-variable accelerator bootstrap is not a release interface for
teachers. A release still needs the frozen server-side desktop registration
contract plus Windows protected credential storage and native-to-sidecar secret
delivery. Node credentials must not be compiled into the installer, accepted
from arbitrary WebView input, or placed on a command line.

## Native NVIDIA backend

The native GPU implementation is VulkanSift pinned to upstream commit
`04245a5e194c9c1aa2ac07c2d257eedad3c8699d`. The upstream project and bundled
library are MIT licensed; `VulkanSift-LICENSE.txt` and `volk-LICENSE.txt` ship
beside the native runtime. The wrapper accepts only an NVIDIA physical adapter
(`vendorID=0x10DE`) and keeps one resident Vulkan context behind a bounded
single-context queue. Intel and AMD Vulkan devices do not qualify this product
route.

Runtime identities for this source snapshot:

| Component | Size | SHA-256 |
|---|---:|---|
| `vksift_resident.exe` | 228,864 bytes | `1a1e83c9bb8650a6fb782468fc9fcaee9c0d595b60e42aeee29be86e89523fdc` |
| `vulkansift.dll` | 274,432 bytes | `777fe099991ad5a0d564ec17b02da8e785a5321806e1219d2c1fd45a30a1c7cc` |
| `vcruntime140.dll` 14.44.35211.0 | 124,544 bytes | `d5e4d9a3e835fa679450145d6a7d94e36573a509317111904d9b3712c30d9066` |
| known-answer fixture | 3,145,745 bytes | `12d12c9607e5b33292ddda7b9a593c1fb7537c0e96e6903c5740b45ccef15fbd` |

The sidecar verifies these runtime hashes before starting the helper. The
helper and DLL are native Windows x64 PE32+ files. They do not call `wsl.exe`
and do not import CV-CUDA, CuPy, or nvImageCodec. `vulkansift.dll` depends on
Windows UCRT API sets and `VCRUNTIME140.dll`; the latter is packaged with the
sidecar, so a teacher does not need Visual Studio, the Vulkan SDK, CUDA Toolkit,
Python, or development environment variables. A compatible NVIDIA display
driver remains required.

## Build and test

From `desktop/`:

```powershell
npm install
npm run test:contract
Push-Location worker
..\..\venv_local\Scripts\python.exe -m pytest -q
Pop-Location
npm run build:prepare
npm run test:rust
npm run tauri build -- --debug --no-bundle
```

The sidecar build script creates a dedicated ignored virtual environment and
places the target-triple executable under `src-tauri/binaries/`. It does not
depend on a system Python installation at runtime.

Building the Rust shell requires the Rust MSVC toolchain, Visual Studio Build
Tools with the Windows SDK, and WebView2. These are development-machine
requirements only; the packaged application must not require them from a
teacher's computer.

The first successful Rust build must generate and retain `src-tauri/Cargo.lock`
so the pinned direct dependencies and their transitive graph remain reproducible.

## Windows 11 RTX 4060 verification snapshot

Measured on 2026-08-01 on the current Windows x64 development machine with an
`NVIDIA GeForce RTX 4060 Laptop GPU`, driver `591.74`:

- the packaged protocol-v3 sidecar selected `windows_nvidia_gpu` with
  `gpuState=ready`, while the independently qualified CPU fallback selected
  three OpenCV workers;
- the startup known-answer test measured a maximum CPU/GPU/known-transform
  projection difference of `0.263763 px`;
- the final debug-bundle sidecar is `61,912,295` bytes with SHA-256
  `31d1a57de66e2becf410a49df5ca8640cee41ac336fc460ad8b42302bc2797f0`;
- it passed `--health`, `--capacity`, GPU self-test, and the real resident
  helper probe with `PATH` restricted to Windows system directories;
- the PyInstaller archive contains the resident helper, VulkanSift DLL,
  `vcruntime140.dll`, deterministic fixture, and notices. It does not contain
  the retired one-shot `vksift_extract.exe`;
- missing helper, DLL/initialization failure, GPU timeout, and non-NVIDIA
  metadata each failed closed to OpenCV CPU. When CPU was also disallowed, the
  worker returned `cpu_fallback_not_qualified` for the upper layer to route to
  `server_required`.

Paired timings use the same deterministic fixture, warm-up, and output scope:

| Scope | GPU P50 / P95 | CPU P50 / P95 |
|---|---:|---:|
| Decode, resize, SIFT, feature decode | 126.714 / 158.961 ms | 447.856 / 623.525 ms |
| Cached-template match and homography candidate | 177.564 / 207.182 ms | 569.787 / 624.026 ms |
| Candidate plus warp, exact source-mask, PNG, and artifact hash | 426.316 / 545.303 ms | 1,031.899 / 1,191.613 ms |

The complete local preprocessing P50 was about `2.42x` faster in this fixture.
This excludes upload, server replay, OCR, grading, and archive writes, so it is
not evidence that end-to-end grading is 2.42 times faster.

The current native API truthfully uses batch size `1`. Sequential grouping at
1/4/8 produced about `6.312`, `5.978`, and `5.048` items/s respectively; it did
not demonstrate a Batch 4 or Batch 8 benefit. WDDM observed roughly 135 MiB of
whole-device movement during the run, but did not expose trustworthy
per-process peak VRAM, so that number is not reported as exact helper usage.

Safe transformed-fixture coverage included 90/180/270 degree rotations,
horizontal mirror, left/right/bottom crops, blur, occlusion, and perspective.
CPU and GPU agreed on answer-region completeness; the largest successful
projection difference was `1.24 px`, and both rejected the mirror. This is
deterministic fixture evidence, not a substitute for a representative teacher
photo corpus.

RTX 4060 Laptop GPU is real-machine verified. RTX 2060 and RTX 3060 remain
architecture targets and are not real-machine certified.

## Evidence still required before release acceptance

Run the debug binary on real Windows 10 22H2 x64 and Windows 11 x64 machines.
For each system, record a screenshot and process listing that show:

1. the local Vue interface is visible;
2. closing the window leaves the tray application and sidecar running;
3. the tray open action restores the window;
4. the tray exit action removes both processes.

Also record cold-start time, unpacked/bundled size, whether elevation was
requested, Defender results, and any file-lock behavior. These are feasibility
measurements, not production-release evidence.

Production task routing is integrated for both tenant-bound teacher desktops
and an explicitly admin-created platform node. The teacher client registers,
rotates, revokes, and restores its own protected Credential Manager binding;
the platform node uses the separate install scripts and credential target
described above. Both routes use the same hash-bound lease and artifact
contract and never pass node tokens through the WebView, command line, or
logs.

Release acceptance still requires real production inputs and evidence:

1. Supply the production API and web HTTPS origins as non-secret release
   inputs; do not guess them or read them from a developer `.env` file.
2. Register the shared machine through the admin-only node API with
   `access_scope=platform`, then deliver its one-time token directly to the
   install script's secure prompt.
3. Verify a real production heartbeat, lease, template job, result, and
   teacher-visible unavailable state. Unit and local HTTP tests are not a
   substitute for that deployed check.
4. Record restart/logon recovery, Credential Manager persistence, task crash
   restart, hardware-pressure pause, and uninstall behavior on the intended
   Windows host.

Phase 1 artifacts may be unsigned for testing. They must not be described or
distributed as production releases until the Phase 6 signing flow exists.
