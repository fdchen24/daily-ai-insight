/** 应用骨架：顶部栏 + 路由 + 底部导航（Mobile First，桌面上居中成单列）。
 *
 * 详情页路由带批次日（`/p/:date/:id`）：静态站点没有按 id 查询的接口，带上日期
 * 才能直接取 `data/daily/YYYY/MM/DD.json`，深链接也因此可分享、刷新不丢。
 */
import { NavLink, Route, Routes } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { applyTheme, readTheme, type Theme } from './lib/theme'
import Today from './pages/Today'
import Detail from './pages/Detail'
import Discover from './pages/Discover'
import Archive from './pages/Archive'
import Saved from './pages/Saved'

const NAV = [
  { to: '/', label: '今日', icon: '◉' },
  { to: '/discover', label: '发现', icon: '⌕' },
  { to: '/archive', label: '归档', icon: '▤' },
  { to: '/saved', label: '收藏', icon: '★' },
]

const THEME_NEXT: Record<Theme, Theme> = { system: 'light', light: 'dark', dark: 'system' }
const THEME_ICON: Record<Theme, string> = { system: '◐', light: '☀', dark: '☾' }
const THEME_LABEL: Record<Theme, string> = { system: '跟随系统', light: '浅色', dark: '深色' }

function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(readTheme)
  useEffect(() => applyTheme(theme), [theme])
  return (
    <button
      type="button"
      aria-label={`主题：${THEME_LABEL[theme]}`}
      title={`主题：${THEME_LABEL[theme]}`}
      onClick={() => setTheme(THEME_NEXT[theme])}
      className="rounded-full px-2 py-1 text-lg text-muted hover:bg-raised hover:text-body"
    >
      {THEME_ICON[theme]}
    </button>
  )
}

export default function App() {
  return (
    <div className="mx-auto flex min-h-full max-w-2xl flex-col">
      <header className="sticky top-0 z-10 flex items-center gap-2 border-b border-line bg-page/95 px-4 py-3 backdrop-blur">
        <NavLink to="/" className="text-base font-bold tracking-tight text-body">
          Daily <span className="text-accent">AI Insight</span>
        </NavLink>
        <span className="flex-1" />
        <ThemeToggle />
      </header>

      <main className="flex-1 pb-20">
        <Routes>
          <Route path="/" element={<Today />} />
          <Route path="/p/:date/:id" element={<Detail />} />
          <Route path="/discover" element={<Discover />} />
          <Route path="/archive" element={<Archive />} />
          <Route path="/archive/:date" element={<Archive />} />
          <Route path="/saved" element={<Saved />} />
          <Route path="*" element={<Today />} />
        </Routes>
      </main>

      <nav className="pb-safe fixed inset-x-0 bottom-0 z-10 mx-auto flex max-w-2xl border-t border-line bg-surface/98 pt-2 backdrop-blur">
        {NAV.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.to === '/'}
            className={({ isActive }) =>
              `flex flex-1 flex-col items-center gap-0.5 py-1 text-xs font-medium ${
                isActive ? 'text-accent' : 'text-muted'
              }`
            }
          >
            <span className="text-base leading-none">{tab.icon}</span>
            {tab.label}
          </NavLink>
        ))}
      </nav>
    </div>
  )
}
