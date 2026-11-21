import { useEffect, useState } from 'react'
import { ModelsPage } from './pages/ModelsPage'
import { PlanPage } from './pages/PlanPage'
import { ResultsPage } from './pages/ResultsPage'
import { api } from './services/api'

const PAGES = { models: 'Models', plan: 'Plan run', results: 'Results' } as const
type Page = keyof typeof PAGES

// The page lives in the URL hash (#plan): reload and back/forward keep you where you were,
// without a router dependency for two pages.
function pageFromHash(): Page {
  const h = window.location.hash.slice(1)
  return h in PAGES ? (h as Page) : 'models'
}

export default function App() {
  const [page, setPage] = useState<Page>(pageFromHash)

  useEffect(() => {
    const onHash = () => setPage(pageFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  return (
    <div className="app">
      <header>
        <h1>evalkit</h1>
        <nav aria-label="main">
          {(Object.keys(PAGES) as Page[]).map((p) => (
            <a key={p} href={`#${p}`} aria-current={p === page ? 'page' : undefined}>{PAGES[p]}</a>
          ))}
        </nav>
      </header>
      <main>
        {page === 'models' && <ModelsPage client={api} />}
        {page === 'plan' && <PlanPage client={api} />}
        {page === 'results' && <ResultsPage client={api} />}
      </main>
    </div>
  )
}
