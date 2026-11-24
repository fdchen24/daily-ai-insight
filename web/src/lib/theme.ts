/** 深色模式三态：跟随系统 / 亮 / 暗。
 *
 * 偏好存 localStorage 而不是 IndexedDB——丢了只是回到跟随系统，没有数据损失，
 * 不值得为它等一次异步读取（首屏会闪一下白底）。
 */
export type Theme = 'system' | 'light' | 'dark'

const KEY = 'dai-theme'
const query = () => window.matchMedia('(prefers-color-scheme: dark)')

export function readTheme(): Theme {
  const saved = localStorage.getItem(KEY)
  return saved === 'light' || saved === 'dark' || saved === 'system' ? saved : 'system'
}

export function applyTheme(theme: Theme): void {
  localStorage.setItem(KEY, theme)
  const dark = theme === 'dark' || (theme === 'system' && query().matches)
  document.documentElement.classList.toggle('dark', dark)
}

/** 跟随系统时要响应系统切换，否则用户在白天改了系统主题，页面不动。 */
export function watchSystemTheme(): () => void {
  const media = query()
  const handler = () => {
    if (readTheme() === 'system') applyTheme('system')
  }
  media.addEventListener('change', handler)
  return () => media.removeEventListener('change', handler)
}
