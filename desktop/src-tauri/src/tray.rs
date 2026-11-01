use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    App, Manager,
};

const OPEN_ID: &str = "open";
const PAUSE_ID: &str = "pause-placeholder";
const QUIT_ID: &str = "quit";

pub fn install(app: &App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, OPEN_ID, "打开 MathAgent", true, None::<&str>)?;
    let pause = MenuItem::with_id(app, PAUSE_ID, "暂停（Phase 1 占位）", false, None::<&str>)?;
    let quit = MenuItem::with_id(app, QUIT_ID, "彻底退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &pause, &quit])?;

    let mut tray = TrayIconBuilder::with_id("mathagent")
        .tooltip("MathAgent")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            OPEN_ID => show_main_window(app),
            QUIT_ID => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if matches!(
                event,
                TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                }
            ) {
                show_main_window(tray.app_handle());
            }
        });

    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

fn show_main_window(app: &tauri::AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        return;
    };
    let _ = window.show();
    let _ = window.unminimize();
    let _ = window.set_focus();
}
