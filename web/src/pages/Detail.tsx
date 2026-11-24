/** 详情页：完整解读 + 元数据与链接。
 *
 * 已读是本机状态（IndexedDB），分两条路写：停留满 8 秒或滚过半页自动记一次，
 * 标题下的按钮可以手动补记或撤回。不在进入瞬间就记——点错一条马上退出，
 * 记成读过会让"未读"失去意义；也不只靠按钮——每条都要手点，没人会点。
 *
 * 滚动时记阅读进度（节流到 1 秒）：阅读进度是本机状态，没必要每帧写一次
 * IndexedDB。
 *
 * 不显示"入选理由"（L1 排序时给的一句话）与 claims 溯源表：前者和 TL;DR 讲
 * 同一件事，后者是给校验层用的，正文里的数值已经由它反查过，读者再看一遍
 * 原文片段只是把页面拉长。两者都还在 JSON 里，要查随时能查。
 */
import { useEffect, useRef } from 'react'
import { Link, useParams } from 'react-router-dom'
import { loadDay } from '../lib/data'
import { useAsync, useMarks } from '../lib/hooks'
import { EVIDENCE_LABEL, leadText } from '../lib/fields'
import { markRead, setProgress, setRead, toggleFavorite } from '../lib/store'
import { ValidationNote } from '../components/Validation'
import { RichText } from '../components/Math'
import { Reading } from '../components/Reading'
import { ErrorBox, Loading, ReadButton, StarButton, Tag, TypeBadge } from '../components/ui'
import type { Item } from '../types'

// 自动记已读的两个门槛：停留时长与滚动深度，任一满足即算读过
const DWELL_MS = 8000
const READ_PROGRESS = 0.5

const LINK_LABEL: Record<string, string> = {
  paper: '论文页',
  pdf: 'PDF',
  code: '代码',
  project: '项目页',
}

function Links({ links }: { links: Item['links'] }) {
  const rows = Object.entries(links || {}).filter(([, url]) => !!url)
  if (!rows.length) return null
  return (
    <div className="flex flex-wrap gap-2 px-4 pt-3">
      {rows.map(([key, url]) => (
        <a
          key={key}
          href={url as string}
          target="_blank"
          rel="noreferrer noopener"
          className="rounded-full bg-accent px-3.5 py-2 text-sm font-semibold text-white hover:opacity-90"
        >
          {LINK_LABEL[key] || key} ↗
        </a>
      ))}
    </div>
  )
}

export default function Detail() {
  const { date = '', id = '' } = useParams()
  const itemId = decodeURIComponent(id)
  const { data, error, loading } = useAsync(() => loadDay(date), [date])
  const { marks } = useMarks()
  const lastSaved = useRef(0)
  const autoMarked = useRef(false)

  const item = data?.items.find((row) => row.id === itemId)

  useEffect(() => {
    if (!item) return
    autoMarked.current = false
    // 只自动记一次：之后用户手动标为未读，不该被下一次滚动又顶回已读
    const auto = () => {
      if (autoMarked.current) return
      autoMarked.current = true
      markRead(item)
    }
    const timer = window.setTimeout(auto, DWELL_MS)
    const onScroll = () => {
      const now = Date.now()
      if (now - lastSaved.current < 1000) return
      lastSaved.current = now
      const height = document.body.scrollHeight - window.innerHeight
      const progress = height > 0 ? Math.min(1, window.scrollY / height) : 1
      setProgress(item.id, progress)
      if (progress >= READ_PROGRESS) auto()
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      window.clearTimeout(timer)
      window.removeEventListener('scroll', onScroll)
    }
  }, [item?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) return <Loading what="解读" />
  if (error) return <ErrorBox message={error} />
  if (!item) return <ErrorBox message={`${date} 批次里没有条目 ${itemId}`} />

  const favorite = !!marks[item.id]?.favorite_at
  const read = !!marks[item.id]?.read_at
  const minutes = item.content?.reading_minutes
  // 单位优先用解读从论文首页抄回的那份：OpenAlex 不收录当天公告的预印本，
  // 它那份对前沿条目基本是空的，且消歧偶有错标
  const fromPaper = item.affiliations || []
  const institutions = fromPaper.length ? fromPaper : item.signals?.institutions || []
  const institutionSource = fromPaper.length ? '论文首页' : 'OpenAlex'
  const citations = item.signals?.citation_count
  const tldr = leadText(item.content || {})

  return (
    <article>
      <div className="px-4 pt-5">
        <Link to="/" className="text-xs text-muted hover:text-accent">
          ← {date} 批次
        </Link>
        <div className="mt-2.5 flex items-start gap-2">
          <h1 className="flex-1 text-[24px] font-bold leading-snug text-body">
            {item.title_zh || item.title}
          </h1>
          <StarButton on={favorite} onClick={() => toggleFavorite(item, !favorite)} />
        </div>
        <p className="mt-1.5 text-[15px] text-muted">{item.title}</p>
        <p className="mt-2 line-clamp-2 text-[13px] text-faint">
          {(item.authors || []).join('、')}
        </p>
        {/* 单位可能错标（首页抄录或 OpenAlex 消歧），所以 title 里写明出处 */}
        {!!institutions.length && (
          <p className="mt-1 text-[13px] text-muted" title={`作者单位（${institutionSource}）`}>
            {institutions.join(' · ')}
          </p>
        )}
        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
          <TypeBadge type={item.recommendation_type} />
          {(item.topics || []).map((topic) => (
            <Tag key={topic}>{topic}</Tag>
          ))}
          {citations != null && (
            <Tag title="OpenAlex 引用数">引用 {citations.toLocaleString('en-US')}</Tag>
          )}
          {item.evidence_level && (
            <Tag title="解读所依据的材料层级">
              {EVIDENCE_LABEL[item.evidence_level] || item.evidence_level}
            </Tag>
          )}
          {minutes != null && <Tag>约 {minutes} 分钟</Tag>}
          <ReadButton on={read} onClick={() => setRead(item, !read)} />
        </div>
        {tldr && (
          <p className="mt-3.5 rounded-lg border border-line bg-raised px-3.5 py-3 text-[15px] leading-[1.8] text-body">
            <span className="mr-1.5 rounded bg-accent-soft px-1.5 py-0.5 text-[11px] font-bold tracking-wide text-accent">
              TL;DR
            </span>
            <RichText text={tldr} />
          </p>
        )}
      </div>

      <Links links={item.links} />

      <Reading type={item.recommendation_type} content={item.content || {}} />

      <ValidationNote validation={item.validation} />
      {item.abstract && (
        <details className="mx-4 my-5 rounded-lg border border-line bg-raised p-3.5 text-[15px]">
          <summary className="cursor-pointer font-medium text-muted">英文原摘要</summary>
          <p className="mt-2 leading-[1.8] text-body">{item.abstract}</p>
        </details>
      )}
    </article>
  )
}
