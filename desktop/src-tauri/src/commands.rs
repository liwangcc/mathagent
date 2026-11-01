use std::{fs, path::PathBuf};

use serde::Serialize;
use url::Url;
use uuid::Uuid;

use crate::sidecar;

const WEB_FALLBACK_URL: Option<&str> = option_env!("MATHAGENT_WEB_FALLBACK_URL");
const MAX_TEMPLATE_SOURCE_BYTES: usize = 20 * 1024 * 1024;
const MAX_TEMPLATE_METADATA_BYTES: usize = 32 * 1024;
const TEMPLATE_ENVELOPE_MAGIC: &[u8; 8] = b"MATHCAN1";

struct TemplateWorkspace {
    root: PathBuf,
    parent: PathBuf,
}

impl TemplateWorkspace {
    fn create() -> Result<Self, String> {
        let parent = std::env::temp_dir().join("cn.mathagent.desktop-template-canonicalization");
        fs::create_dir_all(&parent)
            .map_err(|_| "unable to create local template workspace".to_string())?;
        let parent = parent
            .canonicalize()
            .map_err(|_| "unable to secure local template workspace".to_string())?;
        let root = parent.join(format!("request-{}", Uuid::new_v4()));
        fs::create_dir(&root)
            .map_err(|_| "unable to create local template workspace".to_string())?;
        let root = root
            .canonicalize()
            .map_err(|_| "unable to secure local template workspace".to_string())?;
        if root.parent() != Some(parent.as_path()) {
            let _ = fs::remove_dir(&root);
            return Err("unable to secure local template workspace".to_string());
        }
        Ok(Self { root, parent })
    }
}

impl Drop for TemplateWorkspace {
    fn drop(&mut self) {
        if self.root.parent() == Some(self.parent.as_path())
            && self
                .root
                .file_name()
                .and_then(|value| value.to_str())
                .is_some_and(|value| value.starts_with("request-"))
        {
            let _ = fs::remove_dir_all(&self.root);
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimeInfo {
    runtime: &'static str,
    app_version: String,
    operating_system: &'static str,
    architecture: &'static str,
    web_fallback_configured: bool,
}

impl RuntimeInfo {
    fn current(app_version: impl Into<String>) -> Self {
        Self {
            runtime: "desktop",
            app_version: app_version.into(),
            operating_system: std::env::consts::OS,
            architecture: std::env::consts::ARCH,
            web_fallback_configured: configured_web_fallback_url().is_ok(),
        }
    }
}

fn validate_web_fallback_url(value: Option<&str>) -> Result<&str, String> {
    let value = value
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "web fallback URL is not configured".to_string())?;
    let parsed = Url::parse(value).map_err(|_| "web fallback URL is invalid".to_string())?;
    if parsed.scheme() != "https"
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.path() != "/"
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        return Err("web fallback URL is invalid".to_string());
    }
    Ok(value)
}

fn configured_web_fallback_url() -> Result<&'static str, String> {
    validate_web_fallback_url(WEB_FALLBACK_URL)
}

#[tauri::command]
pub fn runtime_info(app: tauri::AppHandle) -> RuntimeInfo {
    RuntimeInfo::current(app.package_info().version.to_string())
}

#[cfg(windows)]
fn open_configured_web_fallback() -> Result<(), String> {
    use std::{ffi::OsStr, os::windows::ffi::OsStrExt};
    use windows_sys::Win32::UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOWNORMAL};

    let url = configured_web_fallback_url()?;
    let operation = OsStr::new("open")
        .encode_wide()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let target = OsStr::new(url)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect::<Vec<_>>();
    let result = unsafe {
        ShellExecuteW(
            std::ptr::null_mut(),
            operation.as_ptr(),
            target.as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            SW_SHOWNORMAL,
        )
    };
    if result as isize <= 32 {
        return Err("unable to open the configured web fallback".to_string());
    }
    Ok(())
}

#[cfg(not(windows))]
fn open_configured_web_fallback() -> Result<(), String> {
    Err("web fallback is unavailable on this platform".to_string())
}

#[tauri::command]
pub fn open_web_fallback() -> Result<(), String> {
    open_configured_web_fallback()
}

async fn request_worker_status(
    app: tauri::AppHandle,
    command: &'static str,
    mode: Option<String>,
) -> Result<sidecar::WorkerStatus, String> {
    tauri::async_runtime::spawn_blocking(move || match command {
        "get_worker_status" => sidecar::get_worker_status(&app),
        "set_resource_mode" => sidecar::set_resource_mode(
            &app,
            mode.as_deref()
                .ok_or_else(|| "resource mode was unavailable".to_string())?,
        ),
        "pause_worker" => sidecar::pause_worker(&app),
        "resume_worker" => sidecar::resume_worker(&app),
        _ => Err("unsupported worker command".to_string()),
    })
    .await
    .map_err(|_| "worker status task failed".to_string())?
}

#[tauri::command]
pub async fn get_worker_status(app: tauri::AppHandle) -> Result<sidecar::WorkerStatus, String> {
    request_worker_status(app, "get_worker_status", None).await
}

#[tauri::command]
pub async fn set_resource_mode(
    app: tauri::AppHandle,
    mode: String,
) -> Result<sidecar::WorkerStatus, String> {
    if !matches!(mode.as_str(), "quiet" | "balanced" | "fast") {
        return Err("invalid resource mode".to_string());
    }
    request_worker_status(app, "set_resource_mode", Some(mode)).await
}

#[tauri::command]
pub async fn pause_worker(app: tauri::AppHandle) -> Result<sidecar::WorkerStatus, String> {
    request_worker_status(app, "pause_worker", None).await
}

#[tauri::command]
pub async fn resume_worker(app: tauri::AppHandle) -> Result<sidecar::WorkerStatus, String> {
    request_worker_status(app, "resume_worker", None).await
}

fn template_source_extension(bytes: &[u8]) -> Result<&'static str, String> {
    if bytes.starts_with(&[137, 80, 78, 71, 13, 10, 26, 10]) {
        Ok("png")
    } else if bytes.starts_with(&[0xff, 0xd8, 0xff]) {
        Ok("jpg")
    } else {
        Err("template source must be JPEG or PNG".to_string())
    }
}

fn build_template_envelope(
    metadata: &sidecar::TemplateCanonicalization,
    png: &[u8],
) -> Result<Vec<u8>, String> {
    let metadata_json = serde_json::to_vec(metadata)
        .map_err(|_| "unable to encode local template metadata".to_string())?;
    if metadata_json.is_empty() || metadata_json.len() > MAX_TEMPLATE_METADATA_BYTES {
        return Err("local template metadata size was invalid".to_string());
    }
    let metadata_length = u32::try_from(metadata_json.len())
        .map_err(|_| "local template metadata size was invalid".to_string())?;
    let mut envelope = Vec::with_capacity(12 + metadata_json.len() + png.len());
    envelope.extend_from_slice(TEMPLATE_ENVELOPE_MAGIC);
    envelope.extend_from_slice(&metadata_length.to_le_bytes());
    envelope.extend_from_slice(&metadata_json);
    envelope.extend_from_slice(png);
    Ok(envelope)
}

fn canonicalize_template_bytes(
    app: &tauri::AppHandle,
    source_bytes: &[u8],
) -> Result<Vec<u8>, String> {
    if source_bytes.is_empty() || source_bytes.len() > MAX_TEMPLATE_SOURCE_BYTES {
        return Err("template source size was invalid".to_string());
    }
    let extension = template_source_extension(source_bytes)?;
    let status = sidecar::get_worker_status(app)?;
    if !status.local_template_processing_allowed() {
        return Err("local template processing is unavailable".to_string());
    }

    let workspace = TemplateWorkspace::create()?;
    let source_path = workspace.root.join(format!("source.{extension}"));
    let canonical_path = workspace.root.join("canonical.png");
    fs::write(&source_path, source_bytes)
        .map_err(|_| "unable to stage local template source".to_string())?;
    let metadata =
        sidecar::canonicalize_template(app, &workspace.root, &source_path, &canonical_path)?;
    let canonical = sidecar::read_canonical_image(&canonical_path, &metadata)?;
    build_template_envelope(&metadata, &canonical)
}

#[tauri::command]
pub async fn canonicalize_template_image(
    app: tauri::AppHandle,
    request: tauri::ipc::Request<'_>,
) -> Result<tauri::ipc::Response, String> {
    let source_bytes = match request.body() {
        tauri::ipc::InvokeBody::Raw(bytes) => bytes.clone(),
        tauri::ipc::InvokeBody::Json(_) => {
            return Err("template source must use binary IPC".to_string())
        }
    };
    tauri::async_runtime::spawn_blocking(move || canonicalize_template_bytes(&app, &source_bytes))
        .await
        .map_err(|_| "local template processing task failed".to_string())?
        .map(tauri::ipc::Response::new)
}

#[cfg(test)]
mod tests {
    use super::{template_source_extension, validate_web_fallback_url, RuntimeInfo};

    #[test]
    fn runtime_info_contains_only_non_sensitive_build_metadata() {
        let info = RuntimeInfo::current("0.1.0");

        assert_eq!(info.runtime, "desktop");
        assert_eq!(info.app_version, "0.1.0");
        assert!(!info.operating_system.is_empty());
        assert!(!info.architecture.is_empty());
    }

    #[test]
    fn web_fallback_accepts_only_an_https_origin() {
        assert_eq!(
            validate_web_fallback_url(Some("https://math.example.com")),
            Ok("https://math.example.com")
        );
        assert_eq!(
            validate_web_fallback_url(Some("https://math.example.com:8443/")),
            Ok("https://math.example.com:8443/")
        );

        for invalid in [
            None,
            Some(""),
            Some("http://math.example.com"),
            Some("https://user@math.example.com"),
            Some("https://math.example.com/login"),
            Some("https://math.example.com/?token=secret"),
            Some("https://math.example.com/#login"),
        ] {
            assert!(validate_web_fallback_url(invalid).is_err());
        }
    }

    #[test]
    fn template_source_detection_accepts_only_jpeg_and_png_signatures() {
        assert_eq!(
            template_source_extension(&[137, 80, 78, 71, 13, 10, 26, 10]),
            Ok("png")
        );
        assert_eq!(template_source_extension(&[0xff, 0xd8, 0xff]), Ok("jpg"));
        assert!(template_source_extension(b"GIF89a").is_err());
    }
}
