#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
#![recursion_limit = "256"]

mod commands;
mod desktop_auth;
mod sidecar;
mod tray;

use tauri::Manager;

#[cfg(windows)]
struct MainWindowSearch {
    process_id: u32,
    hwnd: windows_sys::Win32::Foundation::HWND,
}

#[cfg(windows)]
unsafe extern "system" fn find_main_window(
    hwnd: windows_sys::Win32::Foundation::HWND,
    search: isize,
) -> i32 {
    use windows_sys::Win32::UI::WindowsAndMessaging::{GetWindowThreadProcessId, IsWindowVisible};

    let search = &mut *(search as *mut MainWindowSearch);
    let mut process_id = 0;
    GetWindowThreadProcessId(hwnd, &mut process_id);
    if process_id == search.process_id && IsWindowVisible(hwnd) != 0 {
        search.hwnd = hwnd;
        return 0;
    }

    1
}

#[cfg(windows)]
fn apply_main_window_taskbar_icon() -> std::io::Result<()> {
    use windows_sys::Win32::{
        System::LibraryLoader::GetModuleHandleW,
        System::Threading::GetCurrentProcessId,
        UI::WindowsAndMessaging::{
            EnumWindows, GetClassLongPtrW, GetSystemMetrics, LoadImageW, SendMessageW,
            SetClassLongPtrW, GCLP_HICON, GCLP_HICONSM, ICON_BIG, ICON_SMALL, IMAGE_ICON,
            LR_SHARED, SM_CXICON, SM_CXSMICON, SM_CYICON, SM_CYSMICON, WM_GETICON, WM_SETICON,
        },
    };

    // tauri-build embeds the Windows window icon under this resource ID.
    const WINDOW_ICON_RESOURCE_ID: u16 = 32512;

    let mut search = MainWindowSearch {
        process_id: unsafe { GetCurrentProcessId() },
        hwnd: std::ptr::null_mut(),
    };
    unsafe {
        EnumWindows(
            Some(find_main_window),
            &mut search as *mut MainWindowSearch as isize,
        );
    }
    if search.hwnd.is_null() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "unable to find the visible main window",
        ));
    }

    let module = unsafe { GetModuleHandleW(std::ptr::null()) };
    if module.is_null() {
        return Err(std::io::Error::last_os_error());
    }

    let big_icon = unsafe {
        LoadImageW(
            module,
            WINDOW_ICON_RESOURCE_ID as usize as *const u16,
            IMAGE_ICON,
            GetSystemMetrics(SM_CXICON),
            GetSystemMetrics(SM_CYICON),
            LR_SHARED,
        )
    };
    if big_icon.is_null() {
        return Err(std::io::Error::last_os_error());
    }
    let small_icon = unsafe {
        LoadImageW(
            module,
            WINDOW_ICON_RESOURCE_ID as usize as *const u16,
            IMAGE_ICON,
            GetSystemMetrics(SM_CXSMICON),
            GetSystemMetrics(SM_CYSMICON),
            LR_SHARED,
        )
    };
    if small_icon.is_null() {
        return Err(std::io::Error::last_os_error());
    }

    unsafe {
        SetClassLongPtrW(search.hwnd, GCLP_HICON, big_icon as isize);
        SetClassLongPtrW(search.hwnd, GCLP_HICONSM, small_icon as isize);
        SendMessageW(
            search.hwnd,
            WM_SETICON,
            ICON_BIG as usize,
            big_icon as isize,
        );
        SendMessageW(
            search.hwnd,
            WM_SETICON,
            ICON_SMALL as usize,
            small_icon as isize,
        );
    }
    let window_big_icon = unsafe { SendMessageW(search.hwnd, WM_GETICON, ICON_BIG as usize, 0) };
    let window_small_icon =
        unsafe { SendMessageW(search.hwnd, WM_GETICON, ICON_SMALL as usize, 0) };
    let class_big_icon = unsafe { GetClassLongPtrW(search.hwnd, GCLP_HICON) };
    let class_small_icon = unsafe { GetClassLongPtrW(search.hwnd, GCLP_HICONSM) };
    if window_big_icon == 0
        || window_small_icon == 0
        || class_big_icon == 0
        || class_small_icon == 0
    {
        return Err(std::io::Error::other("unable to apply the taskbar icon"));
    }

    Ok(())
}

fn apply_main_window_icon(app: &tauri::App) -> tauri::Result<()> {
    let Some(icon) = app.default_window_icon().cloned() else {
        return Ok(());
    };
    let Some(window) = app.get_webview_window("main") else {
        return Ok(());
    };

    window.set_icon(icon)?;

    Ok(())
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(sidecar::SidecarState::default())
        .invoke_handler(tauri::generate_handler![
            commands::runtime_info,
            commands::open_web_fallback,
            commands::get_worker_status,
            commands::set_resource_mode,
            commands::pause_worker,
            commands::resume_worker,
            commands::canonicalize_template_image,
            desktop_auth::desktop_binding_status,
            desktop_auth::bind_teacher_desktop,
            desktop_auth::unbind_teacher_desktop,
            desktop_auth::rotate_teacher_desktop,
        ])
        .setup(|app| {
            apply_main_window_icon(app)?;
            tray::install(app)?;
            // Sidecar probing can take several seconds on a cold start. Run it
            // after setup so the WebView can paint the inline startup screen first.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let _ = sidecar::start(&handle);
                let _ = desktop_auth::restore_handoff(&handle);
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building MathAgent desktop");

    #[cfg(windows)]
    let mut taskbar_icon_applied = false;

    app.run(move |app, event| {
        #[cfg(windows)]
        if !taskbar_icon_applied
            && matches!(
                &event,
                tauri::RunEvent::Ready | tauri::RunEvent::MainEventsCleared
            )
        {
            taskbar_icon_applied = apply_main_window_taskbar_icon().is_ok();
        }

        if matches!(
            event,
            tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
        ) {
            sidecar::stop(app);
        }
    });
}
