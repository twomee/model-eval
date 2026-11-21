import { ModelsPage } from './pages/ModelsPage'
import { api } from './services/api'

export default function App() {
  return (
    <div className="app">
      <header>
        <h1>evalkit</h1>
        <nav aria-label="main">
          <span aria-current="page">Models</span>
        </nav>
      </header>
      <main>
        <ModelsPage client={api} />
      </main>
    </div>
  )
}
