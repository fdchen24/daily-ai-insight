/** 首页与列表页的条目卡片。
 *
 * 卡片上不放"推荐理由"（L1 排序时给的一句话）：它和 TL;DR 说的是同一件事，
 * 两段并排只是让列表更长、更难扫。列表只保留 TL;DR，排序理由留在详情页。
 *
 * 英文原标题保留是刻意的——中文标题是生成的，要让读者能对回原文。
 */
import { Link } from 'react-router-dom'
import { EVIDENCE_LABEL, leadText } from '../lib/fields'
import { toggleFavorite } from '../lib/store'
import type { Item } from '../types'
import { stripMath } from './Math'
import { StarButton, Tag, TypeBadge } from './ui'

const dateOnly = (iso?: string) => (iso ? iso.slice(0, 10) : '')

export function ItemCard({ item, date, favorite, read }:
  { item: Item; date: string; favorite: boolean; read: boolean }) {
  const content = item.content || {}
  const tldr = stripMath(leadText(content))
  const minutes = content.reading_minutes
  const evidence = EVIDENCE_LABEL[item.evidence_level || ''] || item.evidence_level

  return (
    <article className="mx-3 mb-2.5 rounded-xl border border-line bg-surface px-4 py-3.5 shadow-sm">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
            <TypeBadge type={item.recommendation_type} />
            {(item.topics || []).slice(0, 2).map((topic) => (
              <Tag key={topic}>{topic}</Tag>
            ))}
            {evidence && <Tag title="解读所依据的材料层级">{evidence}</Tag>}
            {read && <Tag>已读</Tag>}
          </div>
          <Link to={`/p/${date}/${encodeURIComponent(item.id)}`} className="group block">
            <h3 className={`text-[18px] font-bold leading-snug group-hover:text-accent ${
              read ? 'text-muted' : 'text-body'
            }`}>
              {item.title_zh || item.title}
            </h3>
            <p className="mt-1 line-clamp-1 text-[13px] text-faint">{item.title}</p>
          </Link>
        </div>
        <StarButton on={favorite} onClick={() => toggleFavorite(item, !favorite)} />
      </div>

      {tldr && (
        // TL;DR 不裁行。实测当期 82–145 字（中位 106），手机上三行只够放一半，
        // 卡片扫一遍得点进去才知道讲什么，列表就白列了。卡片因此高一些是值得的。
        <p className="mt-2.5 text-[15px] leading-[1.75] text-body">
          <span className="mr-1.5 rounded bg-accent-soft px-1.5 py-0.5 text-[11px] font-bold tracking-wide text-accent">
            TL;DR
          </span>
          {tldr}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
        <span>{(item.source || []).join(' · ') || 'arxiv'}</span>
        {item.published_at && <span>发布 {dateOnly(item.published_at)}</span>}
        {minutes != null && <span>解读约 {minutes} 分钟</span>}
        {item.quality?.score != null && <span>得分 {item.quality.score}</span>}
        {item.signals?.citation_count ? <span>被引 {item.signals.citation_count}</span> : null}
      </div>
    </article>
  )
}
