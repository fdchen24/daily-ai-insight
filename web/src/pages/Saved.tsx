/** 收藏与阅读记录：收藏列表、最近阅读、导出与导入。
 *
 * 导出/导入是 P0 而不是锦上添花：Safari 对未安装到主屏的站点，7 天无访问就
 * 清除 IndexedDB（DESIGN 7.3），收藏会静默消失。页面常驻一条风险提示，收藏
 * 过 10 条且 14 天没导出时提示升级为醒目样式。
 */
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMarks } from '../lib/hooks'
import { TYPE_LABEL } from '../lib/fields'
import { exportBackup, getMeta, importBackup, toggleFavorite } from '../lib/store'
import type { Item } from '../types'
import { Empty, SectionTitle, StarButton } from '../components/ui'

const DAY = 24 * 60 * 60 * 1000

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
  URL.revokeObjectURL(url)
}

export default function Saved() {
  const { marks, ready } = useMarks()
  const [lastExport, setLastExport] = useState<string | null>(null)
  const [message, setMessage] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    getMeta<string>('last_export_at').then((value) => setLastExport(value ?? null))
  }, [marks])

  const rows = Object.values(marks)
  const favorites = rows
    .filter((mark) => mark.favorite_at)
    .sort((a, b) => (b.favorite_at || '').localeCompare(a.favorite_at || ''))
  const recent = rows
    .filter((mark) => mark.read_at)
    .sort((a, b) => (b.read_at || '').localeCompare(a.read_at || ''))
    .slice(0, 20)

  const overdue =
    favorites.length >= 10 &&
    (!lastExport || Date.now() - Date.parse(lastExport) > 14 * DAY)

  async function onExport() {
    const backup = await exportBackup()
    download(`daily-ai-insight-收藏-${backup.exported_at.slice(0, 10)}.json`, JSON.stringify(backup, null, 2))
    setMessage(`已导出 ${backup.marks.length} 条记录`)
  }

  async function onImport(file: File) {
    try {
      const count = await importBackup(JSON.parse(await file.text()))
      setMessage(`已合并 ${count} 条记录（同条目取较新的时间）`)
    } catch (err) {
      setMessage(`导入失败：${(err as Error).message}`)
    }
  }

  if (!ready) return <Empty>正在读本机记录…</Empty>

  return (
    <>
      <div className="px-4 pt-4">
        <h1 className="text-xl font-bold tracking-tight">收藏与阅读记录</h1>
        <p
          className={`mt-2 rounded-md px-3 py-2 text-[13px] ${
            overdue
              ? 'bg-amber-100 text-amber-900 dark:bg-amber-950/60 dark:text-amber-100'
              : 'border border-line bg-raised text-muted'
          }`}
        >
          收藏保存在本机浏览器，建议添加到主屏并定期导出。
          {lastExport ? `上次导出 ${lastExport.slice(0, 10)}。` : '还没有导出过。'}
          {overdue && ' 已超过 14 天未导出，现在导一份。'}
        </p>

        <div className="mt-3 flex gap-2">
          <button
            type="button"
            onClick={onExport}
            className="rounded-full bg-accent px-3.5 py-2 text-sm font-semibold text-white hover:opacity-90"
          >
            导出 JSON
          </button>
          <button
            type="button"
            onClick={() => fileInput.current?.click()}
            className="rounded-full px-3.5 py-2 text-sm font-medium text-body ring-1 ring-line"
          >
            导入备份
          </button>
          <input
            ref={fileInput}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) void onImport(file)
              event.target.value = ''
            }}
          />
        </div>
        {message && <p className="mt-2 text-xs text-muted">{message}</p>}
      </div>

      <SectionTitle count={favorites.length}>收藏</SectionTitle>
      {favorites.map((mark) => (
        <SavedRow
          key={mark.id}
          id={mark.id}
          snapshot={mark.snapshot}
          at={mark.favorite_at}
          favorite
        />
      ))}
      {!favorites.length && <Empty>还没有收藏。在条目右上角点 ☆ 即可。</Empty>}

      <SectionTitle count={recent.length}>最近阅读</SectionTitle>
      {recent.map((mark) => (
        <SavedRow
          key={mark.id}
          id={mark.id}
          snapshot={mark.snapshot}
          at={mark.read_at}
          progress={mark.progress}
        />
      ))}
      {!recent.length && <Empty>还没有阅读记录。</Empty>}
    </>
  )
}

function SavedRow({ id, snapshot, at, favorite, progress }: {
  id: string
  snapshot?: { title: string; title_zh?: string; type: string; announce_date?: string; links?: Item['links'] }
  at?: string | null
  favorite?: boolean
  progress?: number
}) {
  const date = snapshot?.announce_date
  const title = snapshot?.title_zh || snapshot?.title || id
  return (
    <div className="flex items-start gap-2 mx-3 mb-2 rounded-xl border border-line bg-surface px-4 py-3.5">
      <div className="min-w-0 flex-1">
        {date ? (
          <Link
            to={`/p/${date}/${encodeURIComponent(id)}`}
            className="font-semibold text-body hover:text-accent"
          >
            {title}
          </Link>
        ) : (
          <span className="font-medium">{title}</span>
        )}
        <p className="mt-0.5 text-xs text-muted">
          {snapshot?.type ? TYPE_LABEL[snapshot.type as keyof typeof TYPE_LABEL] : ''}
          {date ? ` · ${date}` : ''}
          {at ? ` · ${at.slice(0, 10)}` : ''}
          {progress != null ? ` · 读到 ${Math.round(progress * 100)}%` : ''}
        </p>
      </div>
      {favorite && snapshot && (
        <StarButton
          on
          onClick={() =>
            toggleFavorite(
              { id, title: snapshot.title, title_zh: snapshot.title_zh,
                recommendation_type: snapshot.type, arxiv_announce_date: date,
                links: snapshot.links || {} } as unknown as Item,
              false,
            )
          }
        />
      )}
    </div>
  )
}
