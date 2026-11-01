use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::windows::{
        io::{AsRawHandle, FromRawHandle, OwnedHandle},
        process::CommandExt,
    },
    path::Path,
    process::{Child, ChildStdout, Command, Stdio},
    sync::{
        mpsc::{self, Receiver},
        Mutex,
    },
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Manager};
use tauri_plugin_shell::ShellExt;
use windows_sys::Win32::{
    Foundation::INVALID_HANDLE_VALUE,
    System::{
        Diagnostics::ToolHelp::{
            CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
        },
        JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        },
        Threading::{
            OpenThread, ResumeThread, CREATE_NO_WINDOW, CREATE_SUSPENDED, THREAD_SUSPEND_RESUME,
        },
    },
};

const SIDECAR_NAME: &str = "mathagent-sidecar";
const PROTOCOL_VERSION: u32 = 5;
const HEALTH_CHECK_TIMEOUT: Duration = Duration::from_secs(5);
const INITIAL_STATUS_TIMEOUT: Duration = Duration::from_secs(30);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(3);
const TEMPLATE_CANONICALIZATION_TIMEOUT: Duration = Duration::from_secs(20);
const GRACEFUL_STOP_TIMEOUT: Duration = Duration::from_secs(2);
const FORCED_STOP_TIMEOUT: Duration = Duration::from_secs(2);
const MAX_HEALTH_BYTES: usize = 4 * 1024;
const MAX_PROTOCOL_BYTES: usize = 16 * 1024;
const MAX_CANONICAL_IMAGE_BYTES: u64 = 32 * 1024 * 1024;
const MAX_HANDOFF_IMAGE_SLOTS: u64 = 8;

#[derive(Deserialize)]
struct SidecarHealth {
    status: String,
    service: String,
    version: String,
    protocol_version: u32,
}

struct ProcessJob {
    handle: OwnedHandle,
}

impl ProcessJob {
    fn new() -> Result<Self, String> {
        let raw_handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if raw_handle.is_null() {
            return Err("unable to create sidecar process job".to_string());
        }
        let handle = unsafe { OwnedHandle::from_raw_handle(raw_handle) };
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                handle.as_raw_handle(),
                JobObjectExtendedLimitInformation,
                std::ptr::addr_of!(limits).cast(),
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if configured == 0 {
            return Err("unable to configure sidecar process job".to_string());
        }
        Ok(Self { handle })
    }

    fn assign(&self, child: &Child) -> Result<(), String> {
        let assigned =
            unsafe { AssignProcessToJobObject(self.handle.as_raw_handle(), child.as_raw_handle()) };
        if assigned == 0 {
            return Err("unable to assign sidecar to its process job".to_string());
        }
        Ok(())
    }

    fn terminate(&self) -> Result<(), String> {
        let terminated = unsafe { TerminateJobObject(self.handle.as_raw_handle(), 1) };
        if terminated == 0 {
            return Err("unable to terminate sidecar process job".to_string());
        }
        Ok(())
    }
}

struct SidecarProcess {
    child: Child,
    job: ProcessJob,
}

struct LiveSidecarProcess {
    process: SidecarProcess,
    responses: Receiver<Vec<u8>>,
    next_request_id: u64,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct GpuAccelerationStatus {
    available: bool,
    enabled: bool,
    backend: String,
    backend_version: String,
    device_name: Option<String>,
    driver_version: Option<String>,
    gpu_time_ms: Option<f64>,
    cpu_time_ms: Option<f64>,
    speedup: Option<f64>,
    projection_max_px: Option<f64>,
    fallback_reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CpuAccelerationStatus {
    available: bool,
    qualified: bool,
    enabled: bool,
    state: String,
    backend: String,
    backend_version: String,
    worker_count: u8,
    hardware_worker_capacity: u8,
    policy_version: String,
    benchmark_version: Option<String>,
    fixture_sha256: Option<String>,
    throughput_per_second: Option<f64>,
    p95_ms: Option<f64>,
    fallback_reason: Option<String>,
    reason_codes: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct HandoffStatus {
    configured: bool,
    running: bool,
    connected: bool,
    state: String,
    backend: String,
    completed: u64,
    failed: u64,
    remaining: u64,
    processing: u64,
    last_error_code: Option<String>,
    last_job_status: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkerStatus {
    capacity_state: String,
    route: String,
    handoff_state: String,
    resource_mode: String,
    worker_count: u8,
    minimum_worker_count: u8,
    hardware_worker_capacity: u8,
    paused: bool,
    preprocessor_available: bool,
    reason_codes: Vec<String>,
    policy_version: String,
    thermal_state: String,
    compute_backend: String,
    gpu_state: String,
    device_name: Option<String>,
    driver_version: Option<String>,
    backend_version: String,
    batch_size: u8,
    processing_time_ms: Option<f64>,
    fallback_reason: Option<String>,
    gpu_acceleration: GpuAccelerationStatus,
    cpu_acceleration: CpuAccelerationStatus,
    local_processing_available: bool,
    local_processor_state: String,
    web_fallback_required: bool,
    handoff: Option<HandoffStatus>,
}

impl WorkerStatus {
    pub fn local_template_processing_allowed(&self) -> bool {
        self.local_processing_available && !self.paused
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct WorkerResponse {
    status: String,
    request_id: Option<String>,
    worker_status: Option<WorkerStatus>,
}

#[derive(Debug, Clone, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct TemplateCanonicalization {
    version: String,
    algorithm: String,
    source_coordinate_space: String,
    canonical_coordinate_space: String,
    source_format: String,
    exif_orientation: u8,
    encoded_width: u32,
    encoded_height: u32,
    source_width: u32,
    source_height: u32,
    source_to_canonical: [[f64; 3]; 3],
    detected_page_quad: [[f64; 2]; 4],
    detection_method: String,
    confidence: f64,
    page_margin_px: u32,
    source_sha256: String,
    canonical_sha256: String,
    canonical_width: u32,
    canonical_height: u32,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct TemplateCanonicalizationResponse {
    status: String,
    request_id: String,
    canonical_path: String,
    canonicalization: TemplateCanonicalization,
}

enum TemplateCanonicalizationRequestError {
    Rejected(String),
    Fatal(String),
}

#[derive(Default)]
pub struct SidecarState {
    process: Mutex<Option<LiveSidecarProcess>>,
}

impl SidecarState {
    pub fn start(&self, app: &AppHandle) -> Result<(), String> {
        let mut process = self
            .process
            .lock()
            .map_err(|_| "sidecar state lock poisoned".to_string())?;
        if process.is_some() {
            return Ok(());
        }

        let plugin_command = app
            .shell()
            .sidecar(SIDECAR_NAME)
            .map_err(|_| "packaged sidecar command was unavailable".to_string())?
            .args(["--serve"]);
        let mut command: Command = plugin_command.into();
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let mut sidecar = spawn_in_job(command)?;
        let stdout = sidecar
            .child
            .stdout
            .take()
            .ok_or_else(|| "packaged sidecar response pipe was unavailable".to_string())?;
        let responses = spawn_response_reader(stdout);
        let startup = responses.recv_timeout(HEALTH_CHECK_TIMEOUT).map_err(|_| {
            let _ = sidecar.job.terminate();
            let _ = wait_for_exit(&mut sidecar.child, FORCED_STOP_TIMEOUT);
            "packaged sidecar startup health check timed out".to_string()
        })?;
        if let Err(error) = validate_health(&startup) {
            let _ = sidecar.job.terminate();
            let _ = wait_for_exit(&mut sidecar.child, FORCED_STOP_TIMEOUT);
            return Err(error);
        }
        let mut live = LiveSidecarProcess {
            process: sidecar,
            responses,
            next_request_id: 0,
        };
        if let Err(error) =
            request_worker_status(&mut live, "get_worker_status", None, INITIAL_STATUS_TIMEOUT)
        {
            let _ = live.process.job.terminate();
            let _ = wait_for_exit(&mut live.process.child, FORCED_STOP_TIMEOUT);
            return Err(error);
        }
        *process = Some(live);
        Ok(())
    }

    pub fn request(
        &self,
        command: &'static str,
        mode: Option<&str>,
    ) -> Result<WorkerStatus, String> {
        let mut state = self
            .process
            .lock()
            .map_err(|_| "sidecar state lock poisoned".to_string())?;
        let result = match state.as_mut() {
            Some(process) => request_worker_status(process, command, mode, REQUEST_TIMEOUT),
            None => Err("packaged sidecar is not running".to_string()),
        };
        if result.is_err() {
            if let Some(mut failed) = state.take() {
                let _ = failed.process.job.terminate();
                let _ = wait_for_exit(&mut failed.process.child, FORCED_STOP_TIMEOUT);
            }
        }
        result
    }

    pub fn configure_handoff(
        &self,
        server: &str,
        node_id: &str,
        token: &str,
    ) -> Result<WorkerStatus, String> {
        let mut state = self
            .process
            .lock()
            .map_err(|_| "sidecar state lock poisoned".to_string())?;
        let result = match state.as_mut() {
            Some(process) => request_worker_status_with_payload(
                process,
                "configure_handoff",
                serde_json::json!({
                    "handoffConfig": {
                        "server": server,
                        "nodeId": node_id,
                        "token": token,
                        "pollSeconds": 5,
                    }
                }),
                REQUEST_TIMEOUT,
            ),
            None => Err("packaged sidecar is not running".to_string()),
        };
        if result.is_err() {
            if let Some(mut failed) = state.take() {
                let _ = failed.process.job.terminate();
                let _ = wait_for_exit(&mut failed.process.child, FORCED_STOP_TIMEOUT);
            }
        }
        result
    }

    pub fn clear_handoff(&self) -> Result<WorkerStatus, String> {
        let mut state = self
            .process
            .lock()
            .map_err(|_| "sidecar state lock poisoned".to_string())?;
        match state.as_mut() {
            Some(process) => request_worker_status_with_payload(
                process,
                "clear_handoff",
                serde_json::json!({}),
                REQUEST_TIMEOUT,
            ),
            None => Err("packaged sidecar is not running".to_string()),
        }
    }

    pub fn canonicalize_template(
        &self,
        workspace_root: &Path,
        source_path: &Path,
        canonical_path: &Path,
    ) -> Result<TemplateCanonicalization, String> {
        let mut state = self
            .process
            .lock()
            .map_err(|_| "sidecar state lock poisoned".to_string())?;
        let result = match state.as_mut() {
            Some(process) => request_template_canonicalization(
                process,
                workspace_root,
                source_path,
                canonical_path,
                TEMPLATE_CANONICALIZATION_TIMEOUT,
            ),
            None => Err(TemplateCanonicalizationRequestError::Fatal(
                "packaged sidecar is not running".to_string(),
            )),
        };
        match result {
            Ok(metadata) => Ok(metadata),
            Err(TemplateCanonicalizationRequestError::Rejected(error)) => Err(error),
            Err(TemplateCanonicalizationRequestError::Fatal(error)) => {
                if let Some(mut failed) = state.take() {
                    let _ = failed.process.job.terminate();
                    let _ = wait_for_exit(&mut failed.process.child, FORCED_STOP_TIMEOUT);
                }
                Err(error)
            }
        }
    }

    pub fn stop(&self) {
        let Ok(mut state) = self.process.lock() else {
            return;
        };
        let Some(mut live) = state.take() else {
            return;
        };
        let process = &mut live.process;

        if let Some(stdin) = process.child.stdin.as_mut() {
            let _ = stdin.write_all(b"{\"command\":\"shutdown\"}\n");
            let _ = stdin.flush();
        }
        if !matches!(
            wait_for_exit(&mut process.child, GRACEFUL_STOP_TIMEOUT),
            Ok(Some(_))
        ) {
            process.child.stdin.take();
            let _ = process.job.terminate();
            let _ = wait_for_exit(&mut process.child, FORCED_STOP_TIMEOUT);
        }
    }
}

fn request_worker_status(
    live: &mut LiveSidecarProcess,
    command: &'static str,
    mode: Option<&str>,
    timeout: Duration,
) -> Result<WorkerStatus, String> {
    let mut payload = serde_json::Map::new();
    if let Some(mode) = mode {
        payload.insert(
            "mode".to_string(),
            serde_json::Value::String(mode.to_string()),
        );
    }
    request_worker_status_with_payload(live, command, serde_json::Value::Object(payload), timeout)
}

fn request_worker_status_with_payload(
    live: &mut LiveSidecarProcess,
    command: &'static str,
    payload: serde_json::Value,
    timeout: Duration,
) -> Result<WorkerStatus, String> {
    live.next_request_id = live.next_request_id.wrapping_add(1).max(1);
    let request_id = live.next_request_id.to_string();
    let mut request = serde_json::json!({
        "command": command,
        "requestId": request_id,
    });
    let fields = payload
        .as_object()
        .ok_or_else(|| "unable to encode sidecar request".to_string())?;
    for (key, value) in fields {
        if key == "command" || key == "requestId" {
            return Err("unable to encode sidecar request".to_string());
        }
        request[key] = value.clone();
    }
    let mut encoded =
        serde_json::to_vec(&request).map_err(|_| "unable to encode sidecar request".to_string())?;
    encoded.push(b'\n');
    let stdin = live
        .process
        .child
        .stdin
        .as_mut()
        .ok_or_else(|| "packaged sidecar request pipe was unavailable".to_string())?;
    stdin
        .write_all(&encoded)
        .and_then(|_| stdin.flush())
        .map_err(|_| "unable to send sidecar request".to_string())?;
    let response = live
        .responses
        .recv_timeout(timeout)
        .map_err(|_| "packaged sidecar request timed out".to_string())?;
    validate_worker_response(&response, &request_id)
}

fn request_template_canonicalization(
    live: &mut LiveSidecarProcess,
    workspace_root: &Path,
    source_path: &Path,
    canonical_path: &Path,
    timeout: Duration,
) -> Result<TemplateCanonicalization, TemplateCanonicalizationRequestError> {
    live.next_request_id = live.next_request_id.wrapping_add(1).max(1);
    let request_id = live.next_request_id.to_string();
    let request = serde_json::json!({
        "command": "canonicalize_template",
        "requestId": request_id,
        "workspaceRoot": workspace_root,
        "sourcePath": source_path,
        "canonicalPath": canonical_path,
    });
    let mut encoded = serde_json::to_vec(&request).map_err(|_| {
        TemplateCanonicalizationRequestError::Fatal(
            "unable to encode sidecar canonicalization request".to_string(),
        )
    })?;
    encoded.push(b'\n');
    let stdin = live.process.child.stdin.as_mut().ok_or_else(|| {
        TemplateCanonicalizationRequestError::Fatal(
            "packaged sidecar request pipe was unavailable".to_string(),
        )
    })?;
    stdin
        .write_all(&encoded)
        .and_then(|_| stdin.flush())
        .map_err(|_| {
            TemplateCanonicalizationRequestError::Fatal(
                "unable to send sidecar canonicalization request".to_string(),
            )
        })?;
    let payload = live.responses.recv_timeout(timeout).map_err(|_| {
        TemplateCanonicalizationRequestError::Fatal(
            "packaged sidecar canonicalization timed out".to_string(),
        )
    })?;
    validate_template_canonicalization_response(&payload, &request_id, canonical_path)
}

fn spawn_response_reader(stdout: ChildStdout) -> Receiver<Vec<u8>> {
    let (sender, receiver) = mpsc::sync_channel(4);
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        loop {
            let mut line = Vec::new();
            match reader
                .by_ref()
                .take((MAX_PROTOCOL_BYTES + 1) as u64)
                .read_until(b'\n', &mut line)
            {
                Ok(0) | Err(_) => break,
                Ok(_) => {
                    while matches!(line.last(), Some(b'\n' | b'\r')) {
                        line.pop();
                    }
                    if line.len() > MAX_PROTOCOL_BYTES {
                        let _ = sender.send(Vec::new());
                        break;
                    }
                    if sender.send(line).is_err() {
                        break;
                    }
                }
            }
        }
    });
    receiver
}

fn spawn_in_job(mut command: Command) -> Result<SidecarProcess, String> {
    let job = ProcessJob::new()?;
    command.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED);
    let mut child = command
        .spawn()
        .map_err(|_| "unable to start packaged sidecar".to_string())?;

    if let Err(error) = job.assign(&child) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
    if let Err(error) = resume_primary_thread(child.id()) {
        let _ = job.terminate();
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }

    Ok(SidecarProcess { child, job })
}

fn resume_primary_thread(process_id: u32) -> Result<(), String> {
    let raw_snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if raw_snapshot == INVALID_HANDLE_VALUE {
        return Err("unable to inspect suspended sidecar process".to_string());
    }
    let snapshot = unsafe { OwnedHandle::from_raw_handle(raw_snapshot) };
    let mut entry = THREADENTRY32::default();
    entry.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;

    let mut has_entry = unsafe { Thread32First(snapshot.as_raw_handle(), &mut entry) } != 0;
    while has_entry {
        if entry.th32OwnerProcessID == process_id {
            let raw_thread = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) };
            if raw_thread.is_null() {
                return Err("unable to open suspended sidecar thread".to_string());
            }
            let thread = unsafe { OwnedHandle::from_raw_handle(raw_thread) };
            if unsafe { ResumeThread(thread.as_raw_handle()) } != 1 {
                return Err("unable to resume suspended sidecar thread".to_string());
            }
            return Ok(());
        }
        has_entry = unsafe { Thread32Next(snapshot.as_raw_handle(), &mut entry) } != 0;
    }

    Err("suspended sidecar thread was unavailable".to_string())
}

fn validate_health(payload: &[u8]) -> Result<(), String> {
    if payload.len() > MAX_HEALTH_BYTES {
        return Err("packaged sidecar health response was invalid".to_string());
    }
    let health: SidecarHealth = serde_json::from_slice(payload)
        .map_err(|_| "packaged sidecar health response was invalid".to_string())?;
    if health.status != "ok"
        || health.service != "mathagent-desktop-sidecar"
        || health.version != env!("CARGO_PKG_VERSION")
        || health.protocol_version != PROTOCOL_VERSION
    {
        return Err("packaged sidecar health contract did not match".to_string());
    }
    Ok(())
}

fn validate_worker_response(payload: &[u8], request_id: &str) -> Result<WorkerStatus, String> {
    let response: WorkerResponse = serde_json::from_slice(payload)
        .map_err(|_| "packaged sidecar worker response was invalid".to_string())?;
    if response.status != "ok" || response.request_id.as_deref() != Some(request_id) {
        return Err("packaged sidecar worker response did not match".to_string());
    }
    let status = response
        .worker_status
        .ok_or_else(|| "packaged sidecar worker response was invalid".to_string())?;
    let allowed_cpu_counts = [0, 2, 3];
    let valid_mode = matches!(status.resource_mode.as_str(), "quiet" | "balanced" | "fast");
    let valid_reasons = status.reason_codes.iter().all(|code| {
        !code.is_empty()
            && code.len() <= 64
            && code
                .bytes()
                .all(|value| value.is_ascii_lowercase() || value.is_ascii_digit() || value == b'_')
    });
    let valid_code = |value: &str| {
        !value.is_empty()
            && value.len() <= 64
            && value
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
    };
    let valid_optional_code =
        |value: Option<&String>| value.map_or(true, |value| valid_code(value));
    let valid_optional_number = |value: Option<f64>| match value {
        Some(number) => number.is_finite() && number >= 0.0,
        None => true,
    };
    let valid_device_name = match status.gpu_acceleration.device_name.as_ref() {
        Some(value) => !value.is_empty() && value.len() <= 128,
        None => true,
    };
    let valid_driver_version = match status.gpu_acceleration.driver_version.as_ref() {
        Some(value) => !value.is_empty() && value.len() <= 64,
        None => true,
    };
    let valid_gpu_status = matches!(
        status.gpu_acceleration.backend.as_str(),
        "windows_cpu_opencv" | "windows_nvidia_gpu"
    ) && !status.gpu_acceleration.backend_version.is_empty()
        && status.gpu_acceleration.backend_version.len() <= 64
        && valid_device_name
        && valid_driver_version
        && valid_optional_code(status.gpu_acceleration.fallback_reason.as_ref())
        && valid_optional_number(status.gpu_acceleration.gpu_time_ms)
        && valid_optional_number(status.gpu_acceleration.cpu_time_ms)
        && valid_optional_number(status.gpu_acceleration.speedup)
        && valid_optional_number(status.gpu_acceleration.projection_max_px)
        && (!status.gpu_acceleration.enabled
            || (status.gpu_acceleration.available
                && status.gpu_acceleration.backend == "windows_nvidia_gpu"
                && status.gpu_acceleration.device_name.is_some()
                && status.gpu_acceleration.fallback_reason.is_none()))
        && (status.gpu_acceleration.enabled
            || (status.gpu_acceleration.backend == "windows_cpu_opencv"
                && status.gpu_acceleration.fallback_reason.is_some()));
    let valid_flat_gpu_status = status.device_name == status.gpu_acceleration.device_name
        && status.driver_version == status.gpu_acceleration.driver_version
        && status.backend_version == status.gpu_acceleration.backend_version
        && status.processing_time_ms == status.gpu_acceleration.gpu_time_ms
        && valid_optional_number(status.processing_time_ms)
        && valid_optional_code(status.fallback_reason.as_ref())
        && match status.gpu_state.as_str() {
            "ready" => {
                !status.paused
                    && status.gpu_acceleration.enabled
                    && status.batch_size == 1
                    && status.fallback_reason.is_none()
            }
            "disabled" => {
                status.paused
                    && status.batch_size == 0
                    && status.fallback_reason.as_deref() == Some("user_paused")
            }
            "self_test_failed" => {
                !status.paused
                    && !status.gpu_acceleration.enabled
                    && status.batch_size == 0
                    && status.fallback_reason.is_some()
            }
            "fallback" => {
                !status.paused
                    && status.gpu_acceleration.available
                    && !status.gpu_acceleration.enabled
                    && status.batch_size == 0
                    && status.fallback_reason.is_some()
            }
            "unavailable" => {
                !status.paused
                    && !status.gpu_acceleration.available
                    && !status.gpu_acceleration.enabled
                    && status.batch_size == 0
                    && status.fallback_reason.is_some()
            }
            _ => false,
        };
    let valid_fixture_sha256 =
        status
            .cpu_acceleration
            .fixture_sha256
            .as_ref()
            .map_or(true, |value| {
                value.len() == 64
                    && value
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
            });
    let valid_benchmark_version = status
        .cpu_acceleration
        .benchmark_version
        .as_ref()
        .map_or(true, |value| value == "cpu-capacity-v2");
    let valid_cpu_metrics = valid_optional_number(status.cpu_acceleration.throughput_per_second)
        && valid_optional_number(status.cpu_acceleration.p95_ms);
    let valid_cpu_status = status.cpu_acceleration.backend == "windows_cpu_opencv"
        && status.cpu_acceleration.backend_version == "opencv-sift-template-registration-v2"
        && status.cpu_acceleration.policy_version == "cpu-capacity-v2"
        && allowed_cpu_counts.contains(&status.cpu_acceleration.worker_count)
        && allowed_cpu_counts.contains(&status.cpu_acceleration.hardware_worker_capacity)
        && status.cpu_acceleration.hardware_worker_capacity == status.hardware_worker_capacity
        && status.cpu_acceleration.worker_count <= status.cpu_acceleration.hardware_worker_capacity
        && valid_fixture_sha256
        && valid_benchmark_version
        && valid_cpu_metrics
        && valid_optional_code(status.cpu_acceleration.fallback_reason.as_ref())
        && status.cpu_acceleration.reason_codes.len() <= 16
        && status
            .cpu_acceleration
            .reason_codes
            .iter()
            .all(|value| valid_code(value))
        && status.cpu_acceleration.enabled == (status.cpu_acceleration.qualified && !status.paused)
        && status.cpu_acceleration.qualified == (status.cpu_acceleration.worker_count >= 2)
        && (!status.cpu_acceleration.qualified || status.cpu_acceleration.available)
        && (!status.cpu_acceleration.available
            || (status.cpu_acceleration.benchmark_version.is_some()
                && status.cpu_acceleration.fixture_sha256.is_some()))
        && (!status.cpu_acceleration.qualified
            || (status.cpu_acceleration.throughput_per_second.unwrap_or(0.0) > 0.0
                && status.cpu_acceleration.p95_ms.unwrap_or(0.0) > 0.0
                && status.cpu_acceleration.reason_codes.is_empty()))
        && match status.cpu_acceleration.state.as_str() {
            "ready" => {
                status.cpu_acceleration.qualified
                    && !status.paused
                    && status.cpu_acceleration.fallback_reason.is_none()
            }
            "unqualified" => {
                status.cpu_acceleration.available
                    && !status.cpu_acceleration.qualified
                    && !status.paused
                    && status.cpu_acceleration.fallback_reason.is_some()
                    && !status.cpu_acceleration.reason_codes.is_empty()
            }
            "unavailable" => {
                !status.cpu_acceleration.available
                    && !status.cpu_acceleration.qualified
                    && !status.paused
                    && status.cpu_acceleration.fallback_reason.is_some()
                    && !status.cpu_acceleration.reason_codes.is_empty()
            }
            "disabled" => {
                status.paused
                    && !status.cpu_acceleration.enabled
                    && status.cpu_acceleration.fallback_reason.as_deref() == Some("user_paused")
            }
            _ => false,
        };
    let valid_local_processor_state = match status.local_processor_state.as_str() {
        "ready" => status.local_processing_available && !status.paused,
        "unavailable" => {
            !status.local_processing_available && status.web_fallback_required && !status.paused
        }
        "paused" => !status.web_fallback_required && status.paused,
        _ => false,
    };
    let valid_compute_backend = match status.compute_backend.as_str() {
        "windows_nvidia_gpu" => {
            status.local_processing_available && status.gpu_acceleration.enabled
        }
        "windows_cpu_opencv" => {
            status.local_processing_available && status.cpu_acceleration.qualified
        }
        "none" => !status.local_processing_available,
        _ => false,
    };
    let valid_local_capability = status.local_processing_available
        == (status.gpu_acceleration.enabled || status.cpu_acceleration.qualified);
    let valid_web_fallback =
        status.web_fallback_required == (!status.local_processing_available && !status.paused);
    let valid_handoff = match status.handoff.as_ref() {
        None => true,
        Some(handoff) => {
            let valid_state = matches!(
                handoff.state.as_str(),
                "starting" | "connecting" | "idle" | "processing" | "paused" | "error" | "stopped"
            );
            let valid_backend = matches!(
                handoff.backend.as_str(),
                "none" | "windows_cpu_opencv" | "windows_nvidia_gpu"
            );
            let valid_error = handoff.last_error_code.as_ref().map_or(true, |value| {
                !value.is_empty()
                    && value.len() <= 64
                    && value.bytes().all(|byte| {
                        byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_'
                    })
            });
            let valid_job = handoff.last_job_status.as_ref().map_or(true, |value| {
                !value.is_empty()
                    && value.len() <= 64
                    && value.bytes().all(|byte| {
                        byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_'
                    })
            });
            handoff.configured
                && valid_state
                && valid_backend
                && valid_error
                && valid_job
                && handoff.processing <= MAX_HANDOFF_IMAGE_SLOTS
                && handoff.remaining <= MAX_HANDOFF_IMAGE_SLOTS
                && handoff.processing <= handoff.remaining
        }
    };
    let local_ready = status.capacity_state == "local_ready"
        && status.preprocessor_available
        && !status.paused
        && status.local_processing_available
        && !status.web_fallback_required
        && status
            .handoff
            .as_ref()
            .is_some_and(|handoff| status.handoff_state == handoff.state)
        && match status.route.as_str() {
            "local_gpu" => {
                status.compute_backend == "windows_nvidia_gpu" && status.worker_count == 1
            }
            "local_cpu" => {
                status.compute_backend == "windows_cpu_opencv" && status.worker_count >= 2
            }
            _ => false,
        };
    let server_required = status.route == "server_required"
        && status.worker_count == 0
        && if status.paused {
            status.capacity_state == "paused"
                && !status.web_fallback_required
                && match status.handoff.as_ref() {
                    Some(handoff) => {
                        handoff.state == "paused" && status.handoff_state == handoff.state
                    }
                    None => status.handoff_state == "not_integrated",
                }
        } else if status.local_processing_available {
            status.capacity_state == "local_ready"
                && !status.web_fallback_required
                && status.handoff.is_none()
                && status.handoff_state == "not_integrated"
        } else {
            status.capacity_state == "server_required"
                && status.web_fallback_required
                && match status.handoff.as_ref() {
                    Some(handoff) => {
                        handoff.state == "paused" && status.handoff_state == handoff.state
                    }
                    None => status.handoff_state == "not_integrated",
                }
        };
    let valid_worker_count = allowed_cpu_counts.contains(&status.worker_count)
        || (status.route == "local_gpu" && status.worker_count == 1);
    if status.minimum_worker_count != 2
        || !valid_worker_count
        || !allowed_cpu_counts.contains(&status.hardware_worker_capacity)
        || !valid_mode
        || !valid_reasons
        || status.policy_version != "cpu-capacity-v2"
        || status.thermal_state != "unknown"
        || status.preprocessor_available != status.local_processing_available
        || !valid_gpu_status
        || !valid_flat_gpu_status
        || !valid_cpu_status
        || !valid_local_processor_state
        || !valid_compute_backend
        || !valid_local_capability
        || !valid_web_fallback
        || !valid_handoff
        || !(local_ready || server_required)
        || status.paused != (status.capacity_state == "paused")
    {
        return Err("packaged sidecar worker contract did not match".to_string());
    }
    Ok(status)
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn valid_canonicalization(metadata: &TemplateCanonicalization) -> bool {
    if metadata.version != "paper-canonical-v1"
        || metadata.algorithm != "opencv_page_quad_v1"
        || metadata.source_coordinate_space != "exif_display_pixels_top_left"
        || metadata.canonical_coordinate_space != "canonical_template_v1"
        || !matches!(metadata.source_format.as_str(), "jpeg" | "png")
        || !(1..=8).contains(&metadata.exif_orientation)
        || [
            metadata.encoded_width,
            metadata.encoded_height,
            metadata.source_width,
            metadata.source_height,
            metadata.canonical_width,
            metadata.canonical_height,
        ]
        .contains(&0)
        || metadata.page_margin_px != 32
        || metadata.canonical_width.max(metadata.canonical_height) != 2064
        || !metadata.confidence.is_finite()
        || !(0.0..=1.0).contains(&metadata.confidence)
        || !matches!(
            metadata.detection_method.as_str(),
            "detected_quadrilateral" | "full_frame_scan"
        )
        || !valid_sha256(&metadata.source_sha256)
        || !valid_sha256(&metadata.canonical_sha256)
        || metadata
            .source_to_canonical
            .iter()
            .flatten()
            .any(|value| !value.is_finite())
        || metadata
            .detected_page_quad
            .iter()
            .flatten()
            .any(|value| !value.is_finite())
    {
        return false;
    }

    let matrix = &metadata.source_to_canonical;
    let determinant = matrix[0][0] * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1])
        - matrix[0][1] * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0])
        + matrix[0][2] * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0]);
    if !determinant.is_finite() || determinant.abs() < 1e-9 {
        return false;
    }

    let margin = metadata.page_margin_px as f64;
    let expected = [
        [margin, margin],
        [metadata.canonical_width as f64 - margin - 1.0, margin],
        [
            metadata.canonical_width as f64 - margin - 1.0,
            metadata.canonical_height as f64 - margin - 1.0,
        ],
        [margin, metadata.canonical_height as f64 - margin - 1.0],
    ];
    for (point, target) in metadata.detected_page_quad.iter().zip(expected) {
        if point[0] < -1.0
            || point[0] > metadata.source_width as f64
            || point[1] < -1.0
            || point[1] > metadata.source_height as f64
        {
            return false;
        }
        let denominator = matrix[2][0] * point[0] + matrix[2][1] * point[1] + matrix[2][2];
        if !denominator.is_finite() || denominator.abs() < 1e-9 {
            return false;
        }
        let projected = [
            (matrix[0][0] * point[0] + matrix[0][1] * point[1] + matrix[0][2]) / denominator,
            (matrix[1][0] * point[0] + matrix[1][1] * point[1] + matrix[1][2]) / denominator,
        ];
        if projected
            .iter()
            .zip(target)
            .any(|(actual, expected)| !actual.is_finite() || (actual - expected).abs() >= 1.0)
        {
            return false;
        }
    }
    true
}

fn validate_template_canonicalization_response(
    payload: &[u8],
    request_id: &str,
    canonical_path: &Path,
) -> Result<TemplateCanonicalization, TemplateCanonicalizationRequestError> {
    let value: serde_json::Value = serde_json::from_slice(payload).map_err(|_| {
        TemplateCanonicalizationRequestError::Fatal(
            "packaged sidecar canonicalization response was invalid".to_string(),
        )
    })?;
    if value.get("status").and_then(serde_json::Value::as_str) == Some("error") {
        let object = value.as_object().ok_or_else(|| {
            TemplateCanonicalizationRequestError::Fatal(
                "packaged sidecar canonicalization response was invalid".to_string(),
            )
        })?;
        let error_code = object
            .get("errorCode")
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default();
        let valid_error = object.len() == 3
            && object.get("requestId").and_then(serde_json::Value::as_str) == Some(request_id)
            && !error_code.is_empty()
            && error_code.len() <= 64
            && error_code
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_');
        if valid_error {
            return Err(TemplateCanonicalizationRequestError::Rejected(
                "local template canonicalization was unavailable".to_string(),
            ));
        }
        return Err(TemplateCanonicalizationRequestError::Fatal(
            "packaged sidecar canonicalization response was invalid".to_string(),
        ));
    }

    let response: TemplateCanonicalizationResponse =
        serde_json::from_value(value).map_err(|_| {
            TemplateCanonicalizationRequestError::Fatal(
                "packaged sidecar canonicalization response was invalid".to_string(),
            )
        })?;
    if response.status != "ok"
        || response.request_id != request_id
        || Path::new(&response.canonical_path) != canonical_path
        || !valid_canonicalization(&response.canonicalization)
    {
        return Err(TemplateCanonicalizationRequestError::Fatal(
            "packaged sidecar canonicalization contract did not match".to_string(),
        ));
    }
    Ok(response.canonicalization)
}

pub fn read_canonical_image(
    canonical_path: &Path,
    metadata: &TemplateCanonicalization,
) -> Result<Vec<u8>, String> {
    let file_size = fs::metadata(canonical_path)
        .map_err(|_| "local canonical template was unavailable".to_string())?
        .len();
    if file_size == 0 || file_size > MAX_CANONICAL_IMAGE_BYTES {
        return Err("local canonical template size was invalid".to_string());
    }
    let bytes = fs::read(canonical_path)
        .map_err(|_| "local canonical template was unavailable".to_string())?;
    let png_signature = [137, 80, 78, 71, 13, 10, 26, 10];
    if bytes.len() < 24
        || bytes[..8] != png_signature
        || &bytes[12..16] != b"IHDR"
        || u32::from_be_bytes(bytes[16..20].try_into().unwrap()) != metadata.canonical_width
        || u32::from_be_bytes(bytes[20..24].try_into().unwrap()) != metadata.canonical_height
    {
        return Err("local canonical template image was invalid".to_string());
    }
    let digest = format!("{:x}", Sha256::digest(&bytes));
    if digest != metadata.canonical_sha256 {
        return Err("local canonical template hash did not match".to_string());
    }
    Ok(bytes)
}

fn wait_for_exit(
    child: &mut Child,
    timeout: Duration,
) -> Result<Option<std::process::ExitStatus>, String> {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Ok(Some(status)),
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(25));
            }
            Ok(None) => return Ok(None),
            Err(_) => return Err("unable to observe packaged sidecar exit".to_string()),
        }
    }
}

pub fn start(app: &AppHandle) -> Result<(), String> {
    app.state::<SidecarState>().start(app)
}

pub fn stop(app: &AppHandle) {
    app.state::<SidecarState>().stop();
}

pub fn get_worker_status(app: &AppHandle) -> Result<WorkerStatus, String> {
    app.state::<SidecarState>()
        .request("get_worker_status", None)
}

pub fn set_resource_mode(app: &AppHandle, mode: &str) -> Result<WorkerStatus, String> {
    app.state::<SidecarState>()
        .request("set_resource_mode", Some(mode))
}

pub fn pause_worker(app: &AppHandle) -> Result<WorkerStatus, String> {
    app.state::<SidecarState>().request("pause_worker", None)
}

pub fn resume_worker(app: &AppHandle) -> Result<WorkerStatus, String> {
    app.state::<SidecarState>().request("resume_worker", None)
}

pub fn configure_handoff(
    app: &AppHandle,
    server: &str,
    node_id: &str,
    token: &str,
) -> Result<WorkerStatus, String> {
    app.state::<SidecarState>()
        .configure_handoff(server, node_id, token)
}

pub fn clear_handoff(app: &AppHandle) -> Result<WorkerStatus, String> {
    app.state::<SidecarState>().clear_handoff()
}

pub fn canonicalize_template(
    app: &AppHandle,
    workspace_root: &Path,
    source_path: &Path,
    canonical_path: &Path,
) -> Result<TemplateCanonicalization, String> {
    app.state::<SidecarState>()
        .canonicalize_template(workspace_root, source_path, canonical_path)
}

#[cfg(test)]
mod tests {
    use super::{
        spawn_in_job, validate_health, validate_template_canonicalization_response,
        validate_worker_response,
    };
    use std::{
        os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
        path::Path,
        process::{Command, Stdio},
        time::{Duration, Instant},
    };
    use windows_sys::Win32::{
        Foundation::{HANDLE, WAIT_OBJECT_0},
        System::Threading::{OpenProcess, WaitForSingleObject, PROCESS_SYNCHRONIZE},
    };

    const JOB_TEST_ROLE: &str = "MATHAGENT_JOB_TEST_ROLE";
    const JOB_TEST_MARKER: &str = "MATHAGENT_JOB_TEST_MARKER";
    const JOB_TEST_NAME: &str = "MATHAGENT_JOB_TEST_NAME";

    #[derive(Clone, Copy)]
    enum JobStop {
        Terminate,
        Close,
    }

    #[test]
    fn accepts_the_frozen_health_contract() {
        let payload = format!(
            "{{\"status\":\"ok\",\"service\":\"mathagent-desktop-sidecar\",\"version\":\"{}\",\"protocol_version\":5}}",
            env!("CARGO_PKG_VERSION")
        );

        assert_eq!(validate_health(payload.as_bytes()), Ok(()));
    }

    #[test]
    fn rejects_a_mismatched_health_contract() {
        let payload = b"{\"status\":\"ok\",\"service\":\"other\",\"version\":\"0.1.0\",\"protocol_version\":5}";

        assert_eq!(
            validate_health(payload),
            Err("packaged sidecar health contract did not match".to_string())
        );
    }

    #[test]
    fn rejects_the_previous_protocol_version() {
        let payload = format!(
            "{{\"status\":\"ok\",\"service\":\"mathagent-desktop-sidecar\",\"version\":\"{}\",\"protocol_version\":4}}",
            env!("CARGO_PKG_VERSION")
        );

        assert_eq!(
            validate_health(payload.as_bytes()),
            Err("packaged sidecar health contract did not match".to_string())
        );
    }

    #[test]
    fn accepts_path_free_template_canonicalization_metadata() {
        let canonical_path = r"C:\Users\teacher\AppData\Local\Temp\canonical.png";
        let payload = serde_json::json!({
            "status": "ok",
            "requestId": "8",
            "canonicalPath": canonical_path,
            "canonicalization": {
                "version": "paper-canonical-v1",
                "algorithm": "opencv_page_quad_v1",
                "source_coordinate_space": "exif_display_pixels_top_left",
                "canonical_coordinate_space": "canonical_template_v1",
                "source_format": "png",
                "exif_orientation": 1,
                "encoded_width": 1548,
                "encoded_height": 2000,
                "source_width": 1548,
                "source_height": 2000,
                "source_to_canonical": [[1.0, 0.0, 32.0], [0.0, 1.0, 32.0], [0.0, 0.0, 1.0]],
                "detected_page_quad": [[0.0, 0.0], [1547.0, 0.0], [1547.0, 1999.0], [0.0, 1999.0]],
                "detection_method": "full_frame_scan",
                "confidence": 0.9,
                "page_margin_px": 32,
                "source_sha256": "a".repeat(64),
                "canonical_sha256": "b".repeat(64),
                "canonical_width": 1612,
                "canonical_height": 2064
            }
        });
        let encoded = serde_json::to_vec(&payload).unwrap();

        assert!(validate_template_canonicalization_response(
            &encoded,
            "8",
            Path::new(canonical_path),
        )
        .is_ok());
    }

    #[test]
    fn rejects_template_metadata_that_contains_a_source_path() {
        let canonical_path = r"C:\Users\teacher\AppData\Local\Temp\canonical.png";
        let mut payload = serde_json::json!({
            "status": "ok",
            "requestId": "8",
            "canonicalPath": canonical_path,
            "canonicalization": {
                "version": "paper-canonical-v1",
                "algorithm": "opencv_page_quad_v1",
                "source_coordinate_space": "exif_display_pixels_top_left",
                "canonical_coordinate_space": "canonical_template_v1",
                "source_format": "png",
                "exif_orientation": 1,
                "encoded_width": 1548,
                "encoded_height": 2000,
                "source_width": 1548,
                "source_height": 2000,
                "source_to_canonical": [[1.0, 0.0, 32.0], [0.0, 1.0, 32.0], [0.0, 0.0, 1.0]],
                "detected_page_quad": [[0.0, 0.0], [1547.0, 0.0], [1547.0, 1999.0], [0.0, 1999.0]],
                "detection_method": "full_frame_scan",
                "confidence": 0.9,
                "page_margin_px": 32,
                "source_sha256": "a".repeat(64),
                "canonical_sha256": "b".repeat(64),
                "canonical_width": 1612,
                "canonical_height": 2064
            }
        });
        payload["canonicalization"]["source_image"] =
            serde_json::Value::String(r"C:\secret.png".to_string());
        let encoded = serde_json::to_vec(&payload).unwrap();

        assert!(validate_template_canonicalization_response(
            &encoded,
            "8",
            Path::new(canonical_path),
        )
        .is_err());
    }

    #[test]
    fn accepts_a_fail_closed_worker_status() {
        let payload = br#"{
            "status":"ok",
            "requestId":"7",
            "workerStatus":{
                "capacityState":"server_required",
                "route":"server_required",
                "handoffState":"not_integrated",
                "resourceMode":"balanced",
                "workerCount":0,
                "minimumWorkerCount":2,
                "hardwareWorkerCapacity":3,
                "paused":false,
                "preprocessorAvailable":false,
                "reasonCodes":["preprocessor_unavailable"],
                "policyVersion":"cpu-capacity-v2",
                "thermalState":"unknown",
                "computeBackend":"none",
                "gpuState":"unavailable",
                "deviceName":null,
                "driverVersion":null,
                "backendVersion":"vulkansift-04245a5e-usac-accurate-v1",
                "batchSize":0,
                "processingTimeMs":null,
                "fallbackReason":"gpu_runtime_missing",
                "gpuAcceleration":{
                    "available":false,
                    "enabled":false,
                    "backend":"windows_cpu_opencv",
                    "backendVersion":"vulkansift-04245a5e-usac-accurate-v1",
                    "deviceName":null,
                    "driverVersion":null,
                    "gpuTimeMs":null,
                    "cpuTimeMs":null,
                    "speedup":null,
                    "projectionMaxPx":null,
                    "fallbackReason":"gpu_runtime_missing"
                },
                "cpuAcceleration":{
                    "available":false,
                    "qualified":false,
                    "enabled":false,
                    "state":"unavailable",
                    "backend":"windows_cpu_opencv",
                    "backendVersion":"opencv-sift-template-registration-v2",
                    "workerCount":0,
                    "hardwareWorkerCapacity":3,
                    "policyVersion":"cpu-capacity-v2",
                    "benchmarkVersion":null,
                    "fixtureSha256":null,
                    "throughputPerSecond":null,
                    "p95Ms":null,
                    "fallbackReason":"preprocessor_unavailable",
                    "reasonCodes":["preprocessor_unavailable"]
                },
                "localProcessingAvailable":false,
                "localProcessorState":"unavailable",
                "webFallbackRequired":true,
                "handoff":null
            }
        }"#;

        let status = validate_worker_response(payload, "7").expect("valid worker status");
        assert_eq!(status.worker_count, 0);
        assert_eq!(status.hardware_worker_capacity, 3);
    }

    #[test]
    fn accepts_ready_gpu_before_and_after_handoff_integration() {
        let mut payload = serde_json::json!({
            "status": "ok",
            "requestId": "8",
            "workerStatus": {
                "capacityState": "local_ready",
                "route": "server_required",
                "handoffState": "not_integrated",
                "resourceMode": "balanced",
                "workerCount": 0,
                "minimumWorkerCount": 2,
                "hardwareWorkerCapacity": 3,
                "paused": false,
                "preprocessorAvailable": true,
                "localProcessingAvailable": true,
                "localProcessorState": "ready",
                "webFallbackRequired": false,
                "reasonCodes": [],
                "policyVersion": "cpu-capacity-v2",
                "thermalState": "unknown",
                "computeBackend": "windows_nvidia_gpu",
                "gpuState": "ready",
                "deviceName": "NVIDIA GeForce RTX 4060 Laptop GPU",
                "driverVersion": "591.74",
                "backendVersion": "vulkansift-04245a5e-usac-accurate-v1",
                "batchSize": 1,
                "processingTimeMs": 6.8,
                "fallbackReason": null,
                "cpuAcceleration": {
                    "available": true,
                    "qualified": true,
                    "enabled": true,
                    "state": "ready",
                    "backend": "windows_cpu_opencv",
                    "backendVersion": "opencv-sift-template-registration-v2",
                    "workerCount": 3,
                    "hardwareWorkerCapacity": 3,
                    "policyVersion": "cpu-capacity-v2",
                    "benchmarkVersion": "cpu-capacity-v2",
                    "fixtureSha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "throughputPerSecond": 9.2,
                    "p95Ms": 326.0,
                    "fallbackReason": null,
                    "reasonCodes": []
                },
                "gpuAcceleration": {
                    "available": true,
                    "enabled": true,
                    "backend": "windows_nvidia_gpu",
                    "backendVersion": "vulkansift-04245a5e-usac-accurate-v1",
                    "deviceName": "NVIDIA GeForce RTX 4060 Laptop GPU",
                    "driverVersion": "591.74",
                    "gpuTimeMs": 6.8,
                    "cpuTimeMs": null,
                    "speedup": null,
                    "projectionMaxPx": 5.282679,
                    "fallbackReason": null
                },
                "handoff": null
            }
        });
        let encoded = serde_json::to_vec(&payload).expect("encode ready GPU status");
        assert!(validate_worker_response(&encoded, "8").is_ok());

        let worker_status = payload
            .get_mut("workerStatus")
            .and_then(serde_json::Value::as_object_mut)
            .expect("worker status object");
        worker_status.insert("route".into(), serde_json::json!("local_gpu"));
        worker_status.insert("handoffState".into(), serde_json::json!("idle"));
        worker_status.insert("workerCount".into(), serde_json::json!(1));
        worker_status.insert(
            "handoff".into(),
            serde_json::json!({
                "configured": true,
                "running": true,
                "connected": true,
                "state": "idle",
                "backend": "windows_nvidia_gpu",
                "completed": 0,
                "failed": 0,
                "remaining": 0,
                "processing": 0,
                "lastErrorCode": null,
                "lastJobStatus": null
            }),
        );
        let encoded = serde_json::to_vec(&payload).expect("encode integrated GPU status");
        assert!(validate_worker_response(&encoded, "8").is_ok());
    }

    #[test]
    fn rejects_unqualified_cpu_claimed_as_local_processing() {
        let payload = br#"{
            "status":"ok",
            "requestId":"9",
            "workerStatus":{
                "capacityState":"local_ready",
                "route":"server_required",
                "handoffState":"not_integrated",
                "resourceMode":"balanced",
                "workerCount":0,
                "minimumWorkerCount":2,
                "hardwareWorkerCapacity":3,
                "paused":false,
                "preprocessorAvailable":true,
                "reasonCodes":["benchmark_p95_too_high"],
                "policyVersion":"cpu-capacity-v2",
                "thermalState":"unknown",
                "computeBackend":"windows_cpu_opencv",
                "gpuState":"unavailable",
                "deviceName":null,
                "driverVersion":null,
                "backendVersion":"vulkansift-04245a5e-usac-accurate-v1",
                "batchSize":0,
                "processingTimeMs":null,
                "fallbackReason":"gpu_runtime_missing",
                "gpuAcceleration":{
                    "available":false,
                    "enabled":false,
                    "backend":"windows_cpu_opencv",
                    "backendVersion":"vulkansift-04245a5e-usac-accurate-v1",
                    "deviceName":null,
                    "driverVersion":null,
                    "gpuTimeMs":null,
                    "cpuTimeMs":null,
                    "speedup":null,
                    "projectionMaxPx":null,
                    "fallbackReason":"gpu_runtime_missing"
                },
                "cpuAcceleration":{
                    "available":true,
                    "qualified":false,
                    "enabled":false,
                    "state":"unqualified",
                    "backend":"windows_cpu_opencv",
                    "backendVersion":"opencv-sift-template-registration-v2",
                    "workerCount":0,
                    "hardwareWorkerCapacity":3,
                    "policyVersion":"cpu-capacity-v2",
                    "benchmarkVersion":"cpu-capacity-v2",
                    "fixtureSha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "throughputPerSecond":0.5,
                    "p95Ms":4000.0,
                    "fallbackReason":"benchmark_p95_too_high",
                    "reasonCodes":["benchmark_p95_too_high"]
                },
                "localProcessingAvailable":true,
                "localProcessorState":"ready",
                "webFallbackRequired":false,
                "handoff":null
            }
        }"#;

        assert_eq!(
            validate_worker_response(payload, "9"),
            Err("packaged sidecar worker contract did not match".to_string())
        );
    }

    #[test]
    fn rejects_a_single_worker_status() {
        let payload = br#"{
            "status":"ok",
            "requestId":"7",
            "workerStatus":{
                "capacityState":"local_ready",
                "route":"local_cpu",
                "handoffState":"not_required",
                "resourceMode":"balanced",
                "workerCount":1,
                "minimumWorkerCount":2,
                "hardwareWorkerCapacity":2,
                "paused":false,
                "preprocessorAvailable":true,
                "reasonCodes":[],
                "policyVersion":"cpu-capacity-v2",
                "thermalState":"unknown",
                "computeBackend":"windows_cpu_opencv",
                "gpuState":"unavailable",
                "deviceName":null,
                "driverVersion":null,
                "backendVersion":"vulkansift-04245a5e-usac-accurate-v1",
                "batchSize":0,
                "processingTimeMs":null,
                "fallbackReason":"gpu_runtime_missing",
                "gpuAcceleration":{
                    "available":false,
                    "enabled":false,
                    "backend":"windows_cpu_opencv",
                    "backendVersion":"vulkansift-04245a5e-usac-accurate-v1",
                    "deviceName":null,
                    "driverVersion":null,
                    "gpuTimeMs":null,
                    "cpuTimeMs":null,
                    "speedup":null,
                    "projectionMaxPx":null,
                    "fallbackReason":"gpu_runtime_missing"
                },
                "cpuAcceleration":{
                    "available":true,
                    "qualified":true,
                    "enabled":true,
                    "state":"ready",
                    "backend":"windows_cpu_opencv",
                    "backendVersion":"opencv-sift-template-registration-v2",
                    "workerCount":2,
                    "hardwareWorkerCapacity":2,
                    "policyVersion":"cpu-capacity-v2",
                    "benchmarkVersion":"cpu-capacity-v2",
                    "fixtureSha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "throughputPerSecond":1.2,
                    "p95Ms":2000.0,
                    "fallbackReason":null,
                    "reasonCodes":[]
                },
                "localProcessingAvailable":true,
                "localProcessorState":"ready",
                "webFallbackRequired":false,
                "handoff":null
            }
        }"#;

        assert_eq!(
            validate_worker_response(payload, "7"),
            Err("packaged sidecar worker contract did not match".to_string())
        );
    }

    #[test]
    fn terminate_job_terminates_process_tree() {
        run_helper_if_requested();
        assert_job_stops_process_tree(
            "sidecar::tests::terminate_job_terminates_process_tree",
            "terminate",
            JobStop::Terminate,
        );
    }

    #[test]
    fn closing_job_terminates_process_tree() {
        run_helper_if_requested();
        assert_job_stops_process_tree(
            "sidecar::tests::closing_job_terminates_process_tree",
            "close",
            JobStop::Close,
        );
    }

    fn run_helper_if_requested() {
        match std::env::var(JOB_TEST_ROLE).as_deref() {
            Ok("child") => run_child_helper(),
            Ok("grandchild") => wait_forever(),
            Ok(role) => panic!("unexpected job test role: {role}"),
            Err(_) => {}
        }
    }

    fn assert_job_stops_process_tree(test_name: &str, suffix: &str, stop: JobStop) {
        let marker = std::env::temp_dir().join(format!(
            "mathagent-job-object-{}-{suffix}.pid",
            std::process::id(),
        ));
        let _ = std::fs::remove_file(&marker);
        let mut command = Command::new(std::env::current_exe().expect("test executable path"));
        command
            .args(["--exact", test_name, "--nocapture"])
            .env(JOB_TEST_ROLE, "child")
            .env(JOB_TEST_MARKER, &marker)
            .env(JOB_TEST_NAME, test_name)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());

        let process = spawn_in_job(command).expect("spawn job-managed helper");
        assert!(wait_for_file(&marker, Duration::from_secs(5)));
        let grandchild_id = std::fs::read_to_string(&marker)
            .expect("read grandchild process id")
            .parse::<u32>()
            .expect("parse grandchild process id");
        let grandchild = open_process_for_wait(grandchild_id);
        let super::SidecarProcess { mut child, job } = process;

        match stop {
            JobStop::Terminate => job.terminate().expect("terminate helper job"),
            JobStop::Close => drop(job),
        }
        assert_eq!(
            unsafe { WaitForSingleObject(child.as_raw_handle(), 5_000) },
            WAIT_OBJECT_0
        );
        assert_eq!(
            unsafe { WaitForSingleObject(grandchild.as_raw_handle(), 5_000) },
            WAIT_OBJECT_0
        );
        let _ = child.wait();
        let _ = std::fs::remove_file(marker);
    }

    fn run_child_helper() -> ! {
        let marker = std::env::var_os(JOB_TEST_MARKER).expect("job test marker path");
        let test_name = std::env::var(JOB_TEST_NAME).expect("job test name");
        let grandchild = Command::new(std::env::current_exe().expect("test executable path"))
            .args(["--exact", test_name.as_str(), "--nocapture"])
            .env(JOB_TEST_ROLE, "grandchild")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn grandchild helper");
        std::fs::write(marker, grandchild.id().to_string()).expect("write grandchild process id");
        wait_forever()
    }

    fn wait_forever() -> ! {
        loop {
            std::thread::sleep(Duration::from_secs(60));
        }
    }

    fn wait_for_file(path: &std::path::Path, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if path.is_file() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        path.is_file()
    }

    fn open_process_for_wait(process_id: u32) -> OwnedHandle {
        let raw_handle: HANDLE = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, process_id) };
        assert!(!raw_handle.is_null(), "open grandchild process for waiting");
        unsafe { OwnedHandle::from_raw_handle(raw_handle) }
    }
}
