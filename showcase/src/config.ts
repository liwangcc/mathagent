export const WEB_APP_URL = import.meta.env.VITE_WEB_APP_URL || "https://mathagent.com.cn"
export const DOWNLOAD_URL = import.meta.env.VITE_DOWNLOAD_URL?.trim() || "/downloads/MathAgent_0.1.12_x64-setup.exe"
export const CLIENT_VERSION = "0.1.12"
export const CLIENT_SHA256 = "7e29e6333df3d59633ad58e6cc4425bd140a43879e51d4323308106bd572f0c4"
export const BETA_DOWNLOAD_URL = import.meta.env.VITE_BETA_DOWNLOAD_URL?.trim()
export const BETA_VERSION = "0.1.7"
export const BETA_SHA256 = "a27149a1f305a95d5b7160c1dbe9afad031043084c1e3eecf0fc6238625377ca"

export const navigation = [
  { label: "批阅流程", href: "#flow" },
  { label: "批阅方式", href: "#modes" },
  { label: "人工复核", href: "#review" },
  { label: "学情洞察", href: "#insights" },
  { label: "课件题库", href: "#library" },
  { label: "可信边界", href: "#trust" },
]
