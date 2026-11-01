import { ArrowUpRight, Check, Download as DownloadIcon, Gauge, Monitor, MonitorDown, Moon, Sun } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  BETA_DOWNLOAD_URL,
  BETA_SHA256,
  BETA_VERSION,
  CLIENT_SHA256,
  CLIENT_VERSION,
  DOWNLOAD_URL,
  WEB_APP_URL,
  navigation,
} from "@/config"
import type { AccentTheme, ColorMode, useAppearance } from "@/lib/hooks"
import { cn } from "@/lib/utils"

type Appearance = ReturnType<typeof useAppearance>

const MODES: Array<{ value: ColorMode; label: string; icon: typeof Sun }> = [
  { value: "system", label: "跟随系统", icon: Monitor },
  { value: "light", label: "浅色", icon: Sun },
  { value: "dark", label: "深色", icon: Moon },
]

const THEMES: Array<{ value: AccentTheme; label: string; name: string }> = [
  { value: "song", label: "宋", name: "苔绿" },
  { value: "qing", label: "青", name: "青碧" },
  { value: "dai", label: "黛", name: "黛蓝" },
]

export function Download({ appearance }: { appearance: Appearance }) {
  return (
    <section id="download" className="section download" aria-labelledby="download-title">
      <div className="wrap">
        <div className="download-grid">
          <div data-reveal>
            <p className="kicker">
              <span className="kicker-num">07</span>
              <span>使用方式</span>
            </p>
            <h2 id="download-title" className="display download-title">
              先在网页工作台开始。
              <em>
                需要时，
                <br />
                让 Windows 设备协同加速。
              </em>
            </h2>
            <div className="download-actions">
              <Button asChild size="lg">
                <a href={WEB_APP_URL} target="_blank" rel="noreferrer">
                  进入教师工作台 <ArrowUpRight aria-hidden="true" />
                </a>
              </Button>
              {DOWNLOAD_URL ? (
                <Button asChild size="lg" variant="outline">
                  <a href={DOWNLOAD_URL} download>
                    下载 Windows 客户端 · {CLIENT_VERSION} <DownloadIcon aria-hidden="true" />
                  </a>
                </Button>
              ) : (
                <Button disabled size="lg" variant="outline">
                  正式签名版准备中 <MonitorDown aria-hidden="true" />
                </Button>
              )}
            </div>
            <p className="checksum">SHA-256 · {CLIENT_SHA256}</p>
            <p className="download-note">
              当前安装包暂未数字签名，Windows 可能显示“未知发布者”。如设备策略禁止安装，请使用网页版，不要关闭安全防护。
            </p>
            {BETA_DOWNLOAD_URL && (
              <div className="beta-note">
                <p className="beta-title">未签名 Windows 内测版 · {BETA_VERSION}</p>
                <p>仅供主动选择客户端的教师测试。学校设备若禁止安装，请停止操作并使用网页版；不要关闭 Defender 或组织安全策略。</p>
                <p className="checksum">SHA-256 · {BETA_SHA256}</p>
                <Button asChild variant="outline">
                  <a href={BETA_DOWNLOAD_URL} download>
                    下载未签名内测版 <DownloadIcon aria-hidden="true" />
                  </a>
                </Button>
              </div>
            )}
          </div>

          <aside className="device-card" data-reveal>
            <span className="device-icon" aria-hidden="true">
              <Gauge />
            </span>
            <span className="device-eyebrow">可选能力</span>
            <h3>Windows 本机加速</h3>
            <p>网页版承担完整教师工作流；登录后可绑定当前教师设备，在硬件条件合适时协同处理本地任务。</p>
            <ul>
              <li><Check aria-hidden="true" />Windows 10 22H2 / Windows 11 · x64</li>
              <li><Check aria-hidden="true" />兼容 GPU 时优先使用 GPU</li>
              <li><Check aria-hidden="true" />无需 WSL、Python 或 CUDA Toolkit</li>
            </ul>
          </aside>
        </div>

        <footer className="site-footer">
          <div className="footer-brand">
            <span className="logo-mark" aria-hidden="true">M</span>
            <div>
              <span className="wordmark">
                MathAgent<i>.</i>
              </span>
              <small>专为数学教师设计</small>
            </div>
          </div>

          <nav className="footer-links" aria-label="页脚导航">
            {navigation.map((item) => (
              <a key={item.href} href={item.href}>
                {item.label}
              </a>
            ))}
          </nav>

          <div className="appearance" aria-label="页面外观">
            <div className="appearance-group" role="radiogroup" aria-label="颜色模式">
              {MODES.map(({ value, label, icon: Icon }) => (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={appearance.mode === value}
                  className={cn(appearance.mode === value && "is-active")}
                  onClick={() => appearance.setMode(value)}
                  title={label}
                >
                  <Icon aria-hidden="true" />
                  <span>{label}</span>
                </button>
              ))}
            </div>
            <div className="appearance-group is-swatches" role="radiogroup" aria-label="工作台配色">
              {THEMES.map((theme) => (
                <button
                  key={theme.value}
                  type="button"
                  role="radio"
                  aria-checked={appearance.theme === theme.value}
                  className={cn(`swatch is-${theme.value}`, appearance.theme === theme.value && "is-active")}
                  onClick={() => appearance.setTheme(theme.value)}
                  title={`${theme.label} · ${theme.name}${appearance.resolvedDark ? "（深色模式统一使用鼠尾草绿）" : ""}`}
                >
                  {theme.label}
                </button>
              ))}
            </div>
          </div>

          <p className="footer-copy">© {new Date().getFullYear()} MathAgent</p>
        </footer>
      </div>
    </section>
  )
}
