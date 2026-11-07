import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles.css'

// The design system's token + reset layers. Imported after our own stylesheet
// so the M3 custom properties are available to every rule above.
import '@adgytec/adgytec-web-ui-components/styles/core.css'
import '@adgytec/adgytec-web-ui-components/styles/defaultTheme.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)