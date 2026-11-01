use serde::{Deserialize, Serialize};
use std::sync::{Mutex, MutexGuard};
use url::Url;

use crate::sidecar;

const API_ORIGIN: Option<&str> = option_env!("MATHAGENT_API_ORIGIN");
const CREDENTIAL_TARGET: &str = "MathAgent/TeacherDesktop/v1";
const MAX_CREDENTIAL_BYTES: usize = 4096;
static BINDING_OPERATION: Mutex<()> = Mutex::new(());

fn lock_binding_operation() -> Result<MutexGuard<'static, ()>, String> {
    BINDING_OPERATION
        .try_lock()
        .map_err(|_| "desktop binding operation is already in progress".to_string())
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct StoredDesktopCredential {
    server: String,
    device_id: String,
    node_id: String,
    token: String,
    token_expires_at: String,
}

#[derive(Debug, Deserialize)]
struct RegisterResponse {
    device_id: String,
    node_id: String,
    node_token: String,
    token_expires_at: String,
}

#[derive(Debug, Deserialize)]
struct NodeStatusResponse {
    node_id: String,
    device_id: String,
    enabled: bool,
    revoked: bool,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DesktopBindingStatus {
    configured: bool,
    node_id: Option<String>,
    token_expires_at: Option<String>,
}

fn validate_api_origin(value: Option<&str>) -> Result<&str, String> {
    let value = value
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "desktop API origin is not configured".to_string())?;
    let parsed = Url::parse(value).map_err(|_| "desktop API origin is invalid".to_string())?;
    if parsed.scheme() != "https"
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.path() != "/"
        || parsed.query().is_some()
        || parsed.fragment().is_some()
    {
        return Err("desktop API origin is invalid".to_string());
    }
    Ok(value)
}

fn configured_api_origin() -> Result<&'static str, String> {
    validate_api_origin(API_ORIGIN)
}

fn validate_bearer(token: &str) -> Result<&str, String> {
    let token = token.trim();
    if token.len() < 20
        || token.len() > 4096
        || token.bytes().any(|byte| byte.is_ascii_whitespace())
    {
        return Err("teacher login token is invalid".to_string());
    }
    Ok(token)
}

fn request_registration(
    origin: &str,
    bearer: &str,
    device_id: &str,
) -> Result<RegisterResponse, String> {
    let url = format!("{origin}/api/accelerator/desktop/devices/register");
    let response = ureq::post(&url)
        .set("Authorization", &format!("Bearer {bearer}"))
        .set("Content-Type", "application/json")
        .send_json(ureq::json!({
            "device_id": device_id,
            "name": "MathAgent Windows Desktop",
            "token_ttl_days": 90,
        }))
        .map_err(|error| match error {
            ureq::Error::Status(401, _) => "teacher login expired".to_string(),
            ureq::Error::Status(409, _) => "desktop device is already registered".to_string(),
            ureq::Error::Status(_, _) => "desktop registration was rejected".to_string(),
            ureq::Error::Transport(_) => "desktop registration server is unavailable".to_string(),
        })?;
    let payload: RegisterResponse = response
        .into_json()
        .map_err(|_| "desktop registration response was invalid".to_string())?;
    if payload.device_id != device_id
        || payload.node_id.is_empty()
        || payload.node_id.len() > 64
        || payload.node_token.len() < 20
        || payload.node_token.len() > 256
        || payload.token_expires_at.is_empty()
    {
        return Err("desktop registration response was invalid".to_string());
    }
    Ok(payload)
}

fn request_node_status(
    origin: &str,
    bearer: &str,
    node_id: &str,
) -> Result<Option<NodeStatusResponse>, String> {
    let url = format!("{origin}/api/accelerator/desktop/devices/{node_id}");
    let response = match ureq::get(&url)
        .set("Authorization", &format!("Bearer {bearer}"))
        .call()
    {
        Ok(response) => response,
        Err(ureq::Error::Status(401, _)) => return Err("teacher login expired".to_string()),
        Err(ureq::Error::Status(404, _)) => return Ok(None),
        Err(ureq::Error::Status(_, _)) => {
            return Err("desktop device status was rejected".to_string())
        }
        Err(ureq::Error::Transport(_)) => {
            return Err("desktop registration server is unavailable".to_string())
        }
    };
    let payload: NodeStatusResponse = response
        .into_json()
        .map_err(|_| "desktop device status response was invalid".to_string())?;
    if payload.node_id != node_id || payload.device_id.is_empty() || payload.device_id.len() > 64 {
        return Err("desktop device status response was invalid".to_string());
    }
    Ok(Some(payload))
}

fn request_revocation(origin: &str, bearer: &str, node_id: &str) -> Result<(), String> {
    let url = format!("{origin}/api/accelerator/desktop/devices/{node_id}/revoke");
    ureq::post(&url)
        .set("Authorization", &format!("Bearer {bearer}"))
        .send_json(ureq::json!({}))
        .map_err(|error| match error {
            ureq::Error::Status(401, _) => "teacher login expired".to_string(),
            ureq::Error::Status(404, _) => "desktop device was not found".to_string(),
            ureq::Error::Status(_, _) => "desktop revocation was rejected".to_string(),
            ureq::Error::Transport(_) => "desktop registration server is unavailable".to_string(),
        })?;
    Ok(())
}

fn request_rotation(origin: &str, bearer: &str, node_id: &str) -> Result<RegisterResponse, String> {
    let url = format!("{origin}/api/accelerator/desktop/devices/{node_id}/rotate");
    let response = ureq::post(&url)
        .set("Authorization", &format!("Bearer {bearer}"))
        .set("Content-Type", "application/json")
        .send_json(ureq::json!({"token_ttl_days": 90}))
        .map_err(|error| match error {
            ureq::Error::Status(401, _) => "teacher login expired".to_string(),
            ureq::Error::Status(404, _) => "desktop device was not found".to_string(),
            ureq::Error::Status(_, _) => "desktop credential rotation was rejected".to_string(),
            ureq::Error::Transport(_) => "desktop registration server is unavailable".to_string(),
        })?;
    let payload: RegisterResponse = response
        .into_json()
        .map_err(|_| "desktop credential rotation response was invalid".to_string())?;
    if payload.node_id != node_id
        || payload.node_token.len() < 20
        || payload.node_token.len() > 256
        || payload.token_expires_at.is_empty()
    {
        return Err("desktop credential rotation response was invalid".to_string());
    }
    Ok(payload)
}

fn configure_sidecar(
    app: &tauri::AppHandle,
    credential: &StoredDesktopCredential,
) -> Result<(), String> {
    sidecar::configure_handoff(
        app,
        &credential.server,
        &credential.node_id,
        &credential.token,
    )?;
    Ok(())
}

fn status_from_credential(credential: &StoredDesktopCredential) -> DesktopBindingStatus {
    DesktopBindingStatus {
        configured: true,
        node_id: Some(credential.node_id.clone()),
        token_expires_at: Some(credential.token_expires_at.clone()),
    }
}

fn replacement_from_rotation(
    origin: &str,
    existing: &StoredDesktopCredential,
    rotated: RegisterResponse,
) -> Result<StoredDesktopCredential, String> {
    if rotated.node_id != existing.node_id {
        return Err("desktop credential rotation response was invalid".to_string());
    }
    replacement_from_registration(origin, &existing.device_id, rotated)
}

fn replacement_from_registration(
    origin: &str,
    device_id: &str,
    registered: RegisterResponse,
) -> Result<StoredDesktopCredential, String> {
    if registered.device_id != device_id
        || registered.node_id.is_empty()
        || registered.node_id.len() > 64
        || registered.node_token.len() < 20
        || registered.node_token.len() > 256
        || registered.token_expires_at.is_empty()
    {
        return Err("desktop credential response was invalid".to_string());
    }
    Ok(StoredDesktopCredential {
        server: origin.to_string(),
        device_id: registered.device_id,
        node_id: registered.node_id,
        token: registered.node_token,
        token_expires_at: registered.token_expires_at,
    })
}

fn new_device_id() -> String {
    format!("win-{}", uuid::Uuid::new_v4().simple())
}

fn repair_existing_credential(
    app: &tauri::AppHandle,
    origin: &str,
    bearer: &str,
    existing: &StoredDesktopCredential,
) -> Result<DesktopBindingStatus, String> {
    repair_existing_credential_with(
        origin,
        existing,
        |node_id| request_node_status(origin, bearer, node_id),
        |node_id| request_rotation(origin, bearer, node_id),
        |device_id| request_registration(origin, bearer, device_id),
        write_credential,
        |credential| configure_sidecar(app, credential),
    )
}

fn repair_existing_credential_with(
    origin: &str,
    existing: &StoredDesktopCredential,
    get_status: impl FnOnce(&str) -> Result<Option<NodeStatusResponse>, String>,
    rotate: impl FnOnce(&str) -> Result<RegisterResponse, String>,
    register: impl FnOnce(&str) -> Result<RegisterResponse, String>,
    persist: impl FnOnce(&StoredDesktopCredential) -> Result<(), String>,
    configure: impl FnOnce(&StoredDesktopCredential) -> Result<(), String>,
) -> Result<DesktopBindingStatus, String> {
    if existing.server != origin {
        return Err("stored desktop origin does not match this release".to_string());
    }
    let status = get_status(&existing.node_id)?;
    if status.as_ref().is_some_and(|value| {
        value.node_id != existing.node_id || value.device_id != existing.device_id
    }) {
        return Err("desktop device status does not match the stored credential".to_string());
    }
    let replacement = if status.is_some_and(|value| value.enabled && !value.revoked) {
        replacement_from_rotation(origin, existing, rotate(&existing.node_id)?)?
    } else {
        // Revocation keeps the old owner/device row reserved. A fresh identity
        // also handles an owner-scoped 404 without changing the previous owner.
        let device_id = new_device_id();
        replacement_from_registration(origin, &device_id, register(&device_id)?)?
    };
    persist(&replacement)?;
    configure(&replacement)?;
    Ok(status_from_credential(&replacement))
}

#[tauri::command]
pub async fn desktop_binding_status() -> Result<DesktopBindingStatus, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let credential = read_credential()?;
        Ok(match credential {
            Some(value) => DesktopBindingStatus {
                configured: true,
                node_id: Some(value.node_id),
                token_expires_at: Some(value.token_expires_at),
            },
            None => DesktopBindingStatus {
                configured: false,
                node_id: None,
                token_expires_at: None,
            },
        })
    })
    .await
    .map_err(|_| "desktop credential task failed".to_string())?
}

#[tauri::command]
pub async fn bind_teacher_desktop(
    app: tauri::AppHandle,
    bearer_token: String,
) -> Result<DesktopBindingStatus, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let _operation = lock_binding_operation()?;
        let origin = configured_api_origin()?;
        let bearer = validate_bearer(&bearer_token)?;
        if let Some(existing) = read_credential()? {
            if existing.server != origin {
                return Err("stored desktop origin does not match this release".to_string());
            }
            return repair_existing_credential(&app, origin, bearer, &existing);
        }
        let device_id = new_device_id();
        let registered = request_registration(origin, bearer, &device_id)?;
        let credential = replacement_from_registration(origin, &device_id, registered)?;
        write_credential(&credential)?;
        if let Err(error) = configure_sidecar(&app, &credential) {
            let _ = request_revocation(origin, bearer, &credential.node_id);
            let _ = delete_credential();
            return Err(error);
        }
        Ok(DesktopBindingStatus {
            configured: true,
            node_id: Some(credential.node_id),
            token_expires_at: Some(credential.token_expires_at),
        })
    })
    .await
    .map_err(|_| "desktop binding task failed".to_string())?
}

#[tauri::command]
pub async fn unbind_teacher_desktop(
    app: tauri::AppHandle,
    bearer_token: String,
) -> Result<DesktopBindingStatus, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let _operation = lock_binding_operation()?;
        let origin = configured_api_origin()?;
        let bearer = validate_bearer(&bearer_token)?;
        let Some(credential) = read_credential()? else {
            return Ok(DesktopBindingStatus {
                configured: false,
                node_id: None,
                token_expires_at: None,
            });
        };
        if credential.server != origin {
            return Err("stored desktop origin does not match this release".to_string());
        }
        request_revocation(origin, bearer, &credential.node_id)?;
        delete_credential()?;
        let _ = sidecar::clear_handoff(&app);
        Ok(DesktopBindingStatus {
            configured: false,
            node_id: None,
            token_expires_at: None,
        })
    })
    .await
    .map_err(|_| "desktop unbinding task failed".to_string())?
}

#[tauri::command]
pub async fn rotate_teacher_desktop(
    app: tauri::AppHandle,
    bearer_token: String,
) -> Result<DesktopBindingStatus, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let _operation = lock_binding_operation()?;
        let origin = configured_api_origin()?;
        let bearer = validate_bearer(&bearer_token)?;
        let existing =
            read_credential()?.ok_or_else(|| "desktop device is not bound".to_string())?;
        if existing.server != origin {
            return Err("stored desktop origin does not match this release".to_string());
        }
        repair_existing_credential(&app, origin, bearer, &existing)
    })
    .await
    .map_err(|_| "desktop rotation task failed".to_string())?
}

pub fn restore_handoff(app: &tauri::AppHandle) -> Result<bool, String> {
    let _operation = lock_binding_operation()?;
    let Some(credential) = read_credential()? else {
        return Ok(false);
    };
    let origin = configured_api_origin()?;
    restore_handoff_with(origin, &credential, |value| configure_sidecar(app, value))
}

fn restore_handoff_with(
    origin: &str,
    credential: &StoredDesktopCredential,
    configure: impl FnOnce(&StoredDesktopCredential) -> Result<(), String>,
) -> Result<bool, String> {
    if credential.server != origin {
        return Err("stored desktop origin does not match this release".to_string());
    }
    configure(credential)?;
    Ok(true)
}

#[cfg(windows)]
fn wide(value: &str) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    std::ffi::OsStr::new(value)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

#[cfg(windows)]
fn write_credential(value: &StoredDesktopCredential) -> Result<(), String> {
    use windows_sys::Win32::Security::Credentials::{
        CredWriteW, CREDENTIALW, CRED_PERSIST_LOCAL_MACHINE, CRED_TYPE_GENERIC,
    };

    let mut bytes = serde_json::to_vec(value)
        .map_err(|_| "desktop credential could not be encoded".to_string())?;
    if bytes.is_empty() || bytes.len() > MAX_CREDENTIAL_BYTES {
        return Err("desktop credential is invalid".to_string());
    }
    let mut target = wide(CREDENTIAL_TARGET);
    let mut username = wide("teacher_desktop");
    let mut credential = CREDENTIALW {
        Flags: 0,
        Type: CRED_TYPE_GENERIC,
        TargetName: target.as_mut_ptr(),
        Comment: std::ptr::null_mut(),
        LastWritten: Default::default(),
        CredentialBlobSize: bytes.len() as u32,
        CredentialBlob: bytes.as_mut_ptr(),
        Persist: CRED_PERSIST_LOCAL_MACHINE,
        AttributeCount: 0,
        Attributes: std::ptr::null_mut(),
        TargetAlias: std::ptr::null_mut(),
        UserName: username.as_mut_ptr(),
    };
    let result = unsafe { CredWriteW(&mut credential, 0) };
    bytes.fill(0);
    if result == 0 {
        return Err("Windows could not store the desktop credential".to_string());
    }
    Ok(())
}

#[cfg(windows)]
fn read_credential() -> Result<Option<StoredDesktopCredential>, String> {
    use windows_sys::Win32::Foundation::ERROR_NOT_FOUND;
    use windows_sys::Win32::Security::Credentials::{
        CredFree, CredReadW, CREDENTIALW, CRED_TYPE_GENERIC,
    };

    let target = wide(CREDENTIAL_TARGET);
    let mut raw: *mut CREDENTIALW = std::ptr::null_mut();
    let result = unsafe { CredReadW(target.as_ptr(), CRED_TYPE_GENERIC, 0, &mut raw) };
    if result == 0 {
        let error = std::io::Error::last_os_error()
            .raw_os_error()
            .unwrap_or_default() as u32;
        if error == ERROR_NOT_FOUND {
            return Ok(None);
        }
        return Err("Windows could not read the desktop credential".to_string());
    }
    if raw.is_null() {
        return Err("Windows returned an invalid desktop credential".to_string());
    }
    let credential = unsafe { &*raw };
    let size = credential.CredentialBlobSize as usize;
    let result = if size == 0 || size > MAX_CREDENTIAL_BYTES || credential.CredentialBlob.is_null()
    {
        Err("stored desktop credential is invalid".to_string())
    } else {
        let bytes = unsafe { std::slice::from_raw_parts(credential.CredentialBlob, size) };
        serde_json::from_slice(bytes)
            .map(Some)
            .map_err(|_| "stored desktop credential is invalid".to_string())
    };
    unsafe { CredFree(raw.cast()) };
    result
}

#[cfg(windows)]
fn delete_credential() -> Result<(), String> {
    use windows_sys::Win32::Foundation::ERROR_NOT_FOUND;
    use windows_sys::Win32::Security::Credentials::{CredDeleteW, CRED_TYPE_GENERIC};
    let target = wide(CREDENTIAL_TARGET);
    let result = unsafe { CredDeleteW(target.as_ptr(), CRED_TYPE_GENERIC, 0) };
    if result == 0 {
        let error = std::io::Error::last_os_error()
            .raw_os_error()
            .unwrap_or_default() as u32;
        if error != ERROR_NOT_FOUND {
            return Err("Windows could not delete the desktop credential".to_string());
        }
    }
    Ok(())
}

#[cfg(not(windows))]
fn write_credential(_value: &StoredDesktopCredential) -> Result<(), String> {
    Err("desktop credential storage is unavailable".to_string())
}

#[cfg(not(windows))]
fn read_credential() -> Result<Option<StoredDesktopCredential>, String> {
    Ok(None)
}

#[cfg(not(windows))]
fn delete_credential() -> Result<(), String> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    const TEST_ORIGIN: &str = "https://desktop.example.invalid";

    fn existing_credential() -> StoredDesktopCredential {
        StoredDesktopCredential {
            server: TEST_ORIGIN.to_string(),
            device_id: "win-old-device".to_string(),
            node_id: "old-node".to_string(),
            token: "synthetic-old-token-0123456789".to_string(),
            token_expires_at: "2099-01-01T00:00:00".to_string(),
        }
    }

    fn response(device_id: &str, node_id: &str) -> RegisterResponse {
        RegisterResponse {
            device_id: device_id.to_string(),
            node_id: node_id.to_string(),
            node_token: "synthetic-new-token-0123456789".to_string(),
            token_expires_at: "2099-02-01T00:00:00".to_string(),
        }
    }

    fn node_status(enabled: bool, revoked: bool) -> NodeStatusResponse {
        NodeStatusResponse {
            node_id: "old-node".to_string(),
            device_id: "win-old-device".to_string(),
            enabled,
            revoked,
        }
    }

    struct RepairMock {
        status: Result<Option<NodeStatusResponse>, String>,
        rotation: Option<Result<RegisterResponse, String>>,
        registration: Option<Result<RegisterResponse, String>>,
        fail_persist: bool,
        fail_configure: bool,
    }

    impl RepairMock {
        fn new(status: Result<Option<NodeStatusResponse>, String>) -> Self {
            Self {
                status,
                rotation: None,
                registration: None,
                fail_persist: false,
                fail_configure: false,
            }
        }

        fn run(
            self,
            existing: &StoredDesktopCredential,
        ) -> (
            Result<DesktopBindingStatus, String>,
            Vec<String>,
            Option<StoredDesktopCredential>,
        ) {
            let Self { status, rotation, registration, fail_persist, fail_configure } = self;
            let events = RefCell::new(Vec::new());
            let saved = RefCell::new(None);
            let result = repair_existing_credential_with(
                TEST_ORIGIN,
                existing,
                |node_id| {
                    assert_eq!(node_id, existing.node_id);
                    events.borrow_mut().push("status".to_string());
                    status
                },
                |node_id| {
                    events.borrow_mut().push("rotate".to_string());
                    rotation.unwrap_or_else(|| Ok(response(&existing.device_id, node_id)))
                },
                |device_id| {
                    events.borrow_mut().push(format!("register:{device_id}"));
                    registration.unwrap_or_else(|| Ok(response(device_id, "new-node")))
                },
                |credential| {
                    events.borrow_mut().push("persist".to_string());
                    if fail_persist {
                        return Err("synthetic storage error".to_string());
                    }
                    *saved.borrow_mut() = Some(credential.clone());
                    Ok(())
                },
                |credential| {
                    events.borrow_mut().push("configure".to_string());
                    assert_eq!(saved.borrow().as_ref().unwrap().node_id, credential.node_id);
                    if fail_configure {
                        return Err("synthetic sidecar error".to_string());
                    }
                    Ok(())
                },
            );
            (result, events.into_inner(), saved.into_inner())
        }
    }

    #[test]
    fn active_and_expired_existing_devices_rotate_without_registration() {
        for expires_at in ["2099-01-01T00:00:00", "2020-01-01T00:00:00"] {
            let mut existing = existing_credential();
            existing.token_expires_at = expires_at.to_string();
            let (result, events, saved) =
                RepairMock::new(Ok(Some(node_status(true, false)))).run(&existing);
            let status = result.unwrap();
            let saved = saved.unwrap();
            assert_eq!(events, ["status", "rotate", "persist", "configure"]);
            assert!(status.configured);
            assert_eq!(saved.node_id, existing.node_id);
            assert_eq!(saved.device_id, existing.device_id);
            assert_ne!(saved.token, existing.token);
        }
    }

    #[test]
    fn revoked_disabled_missing_and_other_owner_404_register_fresh_identity() {
        for status in [
            Some(node_status(false, true)),
            Some(node_status(true, true)),
            Some(node_status(false, false)),
            None, // Missing node.
            None, // The owner-scoped API also uses 404 for another account's node.
        ] {
            let existing = existing_credential();
            let (result, events, saved) = RepairMock::new(Ok(status)).run(&existing);
            assert!(result.unwrap().configured);
            let saved = saved.unwrap();
            assert_ne!(saved.device_id, existing.device_id);
            assert_ne!(saved.node_id, existing.node_id);
            assert!(saved.device_id.starts_with("win-"));
            assert!(uuid::Uuid::parse_str(&saved.device_id[4..]).is_ok());
            assert_eq!(events, ["status", &format!("register:{}", saved.device_id), "persist", "configure"]);
        }
    }

    #[test]
    fn status_errors_never_register_rotate_or_persist() {
        for error in ["teacher login expired", "status rejected", "network unavailable", "invalid status"] {
            let (result, events, saved) =
                RepairMock::new(Err(error.to_string())).run(&existing_credential());
            assert_eq!(result.unwrap_err(), error);
            assert_eq!(events, ["status"]);
            assert!(saved.is_none());
        }
    }

    #[test]
    fn status_identity_mismatch_is_not_treated_as_a_missing_device() {
        for change_device in [true, false] {
            let mut status = node_status(false, true);
            if change_device {
                status.device_id = "different-device".to_string();
            } else {
                status.node_id = "different-node".to_string();
            }
            let (result, events, saved) =
                RepairMock::new(Ok(Some(status))).run(&existing_credential());
            assert!(result.unwrap_err().contains("does not match"));
            assert_eq!(events, ["status"]);
            assert!(saved.is_none());
        }
    }

    #[test]
    fn rotation_errors_never_fall_back_to_registration_or_storage() {
        for error in ["teacher login expired", "not found", "revoked", "network unavailable"] {
            let mut mock = RepairMock::new(Ok(Some(node_status(true, false))));
            mock.rotation = Some(Err(error.to_string()));
            let (result, events, saved) = mock.run(&existing_credential());
            assert_eq!(result.unwrap_err(), error);
            assert_eq!(events, ["status", "rotate"]);
            assert!(saved.is_none());
        }
    }

    #[test]
    fn rotated_identity_and_credential_fields_are_validated_before_storage() {
        for invalid in ["device", "node", "token", "expiry"] {
            let mut value = response("win-old-device", "old-node");
            match invalid {
                "device" => value.device_id = "different-device".to_string(),
                "node" => value.node_id = "different-node".to_string(),
                "token" => value.node_token = "short".to_string(),
                _ => value.token_expires_at.clear(),
            }
            let mut mock = RepairMock::new(Ok(Some(node_status(true, false))));
            mock.rotation = Some(Ok(value));
            let (result, events, saved) = mock.run(&existing_credential());
            assert!(result.is_err());
            assert_eq!(events, ["status", "rotate"]);
            assert!(saved.is_none());
        }
    }

    #[test]
    fn fresh_registration_error_or_mismatched_response_does_not_overwrite_old_credential() {
        for failure in [
            Err("synthetic registration failure".to_string()),
            Ok(response("win-old-device", "new-node")),
        ] {
            let mut mock = RepairMock::new(Ok(None));
            mock.registration = Some(failure);
            let (result, events, saved) = mock.run(&existing_credential());
            assert!(result.is_err());
            assert_eq!(events.len(), 2);
            assert!(events[1].starts_with("register:win-"));
            assert!(saved.is_none());
        }
    }

    #[test]
    fn persistence_failure_never_configures_sidecar() {
        let mut mock = RepairMock::new(Ok(Some(node_status(true, false))));
        mock.fail_persist = true;
        let (result, events, saved) = mock.run(&existing_credential());
        assert!(result.is_err());
        assert_eq!(events, ["status", "rotate", "persist"]);
        assert!(saved.is_none());
    }

    #[test]
    fn sidecar_failure_retains_new_native_credential_for_normal_restore() {
        let mut mock = RepairMock::new(Ok(Some(node_status(true, false))));
        mock.fail_configure = true;
        let (result, events, saved) = mock.run(&existing_credential());
        assert!(result.is_err());
        assert_eq!(events, ["status", "rotate", "persist", "configure"]);
        assert!(saved.is_some());
    }

    #[test]
    fn wrong_origin_fails_before_any_registration_or_credential_change() {
        let mut existing = existing_credential();
        existing.server = "https://different.example.invalid".to_string();
        let (result, events, saved) = RepairMock::new(Ok(None)).run(&existing);
        assert!(result.is_err());
        assert!(events.is_empty());
        assert!(saved.is_none());
    }

    #[test]
    fn ordinary_restore_only_configures_the_stored_credential() {
        let existing = existing_credential();
        let mut calls = 0;
        assert!(restore_handoff_with(TEST_ORIGIN, &existing, |value| {
            calls += 1;
            assert_eq!(value.token, existing.token);
            assert_eq!(value.node_id, existing.node_id);
            Ok(())
        }).unwrap());
        assert_eq!(calls, 1);
        assert!(restore_handoff_with("https://wrong.example.invalid", &existing, |_| {
            panic!("wrong-origin restore must not configure the sidecar")
        }).is_err());
    }

    #[test]
    fn binding_mutations_cannot_reenter_the_credential_operation() {
        let guard = lock_binding_operation().unwrap();
        assert!(std::thread::spawn(|| lock_binding_operation().is_err()).join().unwrap());
        drop(guard);
        assert!(lock_binding_operation().is_ok());
    }

    #[test]
    fn incomplete_or_non_boolean_status_payload_is_not_a_missing_device() {
        for invalid in [
            r#"{"node_id":"old-node","device_id":"win-old-device","enabled":true}"#,
            r#"{"node_id":"old-node","device_id":"win-old-device","enabled":"unknown","revoked":false}"#,
            "null",
        ] {
            assert!(serde_json::from_str::<NodeStatusResponse>(invalid).is_err());
        }
    }

    #[test]
    fn api_origin_accepts_only_a_bare_https_origin() {
        assert_eq!(
            validate_api_origin(Some("https://api.math.example.com")),
            Ok("https://api.math.example.com")
        );
        for invalid in [
            None,
            Some(""),
            Some("http://api.math.example.com"),
            Some("https://user@api.math.example.com"),
            Some("https://api.math.example.com/api"),
            Some("https://api.math.example.com/?token=x"),
        ] {
            assert!(validate_api_origin(invalid).is_err());
        }
    }

    #[test]
    fn bearer_validation_rejects_whitespace_and_short_values() {
        assert!(validate_bearer("short").is_err());
        assert!(validate_bearer("valid-token-value-0123456789").is_ok());
        assert!(validate_bearer("valid token value 0123456789").is_err());
    }
}
