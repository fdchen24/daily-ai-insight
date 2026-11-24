/** 公用小组件：标签、加载、错误与空态。颜色一律走语义色板（见 index.css）。 */
import type { ReactNode } from 'react'
import { TYPE_LABEL } from '../lib/fields'
import type { RecommendationType } from '../types'

// 类型色块用实色而非浅底：第一版的 bg-sky-100 在深色下几乎看不见
const TYPE_STYLE: Record<RecommendationType, string> = {
  frontier: 'bg-accent text-white',
  survey: 'bg-violet-600 text-white',
  classic: 'bg-amber-600 text-white',
  github: 'bg-emerald-600 text-white',
}

export function TypeBadge({ type }: { type: RecommendationType }) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs font-semibold ${TYPE_STYLE[type] ?? TYPE_STYLE.frontier}`}>
      {TYPE_LABEL[type] ?? type}
    </span>
  )
}

export function Tag({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className="rounded border border-line px-1.5 py-0.5 text-xs text-muted"
    >
      {children}
    </span>
  )
}

export function Loading({ what = '内容' }: { what?: string }) {
  return (
    <div className="flex items-center gap-2 px-4 py-16 text-sm text-muted">
      <span className="size-2.5 animate-ping rounded-full bg-accent" />
      正在加载{what}…
    </div>
  )
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="mx-4 my-8 rounded-lg border border-rose-400/60 bg-rose-50 p-4 text-sm text-rose-900 dark:bg-rose-950/60 dark:text-rose-100">
      <p className="font-semibold">读不到数据</p>
      <p className="mt-1 break-all">{message}</p>
      <p className="mt-2 opacity-80">
        本地开发请确认仓库 <code>data/</code> 下已有产物（先跑一次 scripts.run_daily）。
      </p>
    </div>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="px-4 py-14 text-center text-sm text-muted">{children}</p>
}

export function SectionTitle({ children, count }: { children: ReactNode; count?: number }) {
  return (
    <h2 className="flex items-center gap-2 px-4 pt-7 pb-2">
      <span className="h-4 w-1 rounded bg-accent" />
      <span className="text-base font-bold text-body">{children}</span>
      {count != null && <span className="text-sm font-normal text-faint">{count}</span>}
    </h2>
  )
}

/** 已读开关：详情页自动记已读（停留或读过半），这个按钮用来手动补记或撤回。 */
export function ReadButton({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      title={on ? '点一下标为未读' : '点一下标为已读'}
      onClick={onClick}
      className={`rounded border px-1.5 py-0.5 text-xs transition ${
        on
          ? 'border-accent bg-accent-soft font-medium text-accent'
          : 'border-line text-muted hover:border-accent hover:text-accent'
      }`}
    >
      {on ? '✓ 已读' : '标为已读'}
    </button>
  )
}

export function StarButton({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label={on ? '取消收藏' : '收藏'}
      aria-pressed={on}
      onClick={(event) => {
        event.preventDefault()
        event.stopPropagation()
        onClick()
      }}
      className={`shrink-0 rounded-full p-2 text-xl leading-none transition ${
        on ? 'text-amber-400' : 'text-faint hover:text-amber-400'
      }`}
    >
      {on ? '★' : '☆'}
    </button>
  )
}
