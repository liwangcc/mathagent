import { Download } from "@/components/Download"
import { EveningScene } from "@/components/EveningScene"
import { Hero } from "@/components/Hero"
import { InkAtlas } from "@/components/InkAtlas"
import { Insights } from "@/components/Insights"
import { Library } from "@/components/Library"
import { Modes } from "@/components/Modes"
import { Review } from "@/components/Review"
import { SiteHeader } from "@/components/SiteHeader"
import { Trust } from "@/components/Trust"
import { Workflow } from "@/components/Workflow"
import { useAppearance, useRevealOnScroll } from "@/lib/hooks"

function App() {
  const appearance = useAppearance()
  useRevealOnScroll()

  return (
    <>
      <a href="#main" className="skip-link">
        跳到正文
      </a>
      <SiteHeader appearance={appearance} />
      <main id="main">
        <Hero />
        <EveningScene />
        <InkAtlas />
        <Workflow />
        <Modes />
        <Review />
        <Insights />
        <Library />
        <Trust />
        <Download appearance={appearance} />
      </main>
    </>
  )
}

export default App
