import React from "react"
import ReactDOM from "react-dom/client"

import App from "./App"
import { applyStoredAppearance } from "./lib/hooks"
import "./index.css"
import "./styles/hero.css"
import "./styles/evening.css"
import "./styles/atlas.css"
import "./styles/sections.css"

applyStoredAppearance()

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
