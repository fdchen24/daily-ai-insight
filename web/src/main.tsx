import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import './index.css'
import { applyTheme, readTheme, watchSystemTheme } from './lib/theme'

// 主题在挂载前就定下来，避免首屏先白后黑地闪一下
applyTheme(readTheme())
watchSystemTheme()

// basename 跟着 Vite 的 base 走：Pages 在 /daily-ai-insight/ 下，路由不带前缀会 404
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <App />
    </BrowserRouter>
  </StrictMode>,
)
