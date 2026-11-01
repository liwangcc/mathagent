fn main() {
    println!("cargo:rerun-if-env-changed=MATHAGENT_WEB_FALLBACK_URL");
    tauri_build::build()
}
