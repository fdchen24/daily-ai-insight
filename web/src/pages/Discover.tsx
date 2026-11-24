/** 发现页 · 研究方向：按主题浏览，支持关键词搜索、类型筛选与排序。
 *
 * 条目池是最近 7 期合并（lib/data 的 `loadRecent`）：静态站点没有检索接口，
 * 全历史下载会越跑越慢，而 7 期正好与离线缓存范围一致。
 */
import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { loadRecent } from '../lib/data'
import { useAsync, useMarks } from '../lib/hooks'
import { TYPE_LABEL } from '../lib/fields'
import { ItemCard } from '../components/ItemCard'
import { Empty, ErrorBox, Loading } from '../components/ui'
import type { Item, RecommendationType } from '../types'

type Sort = 'score' | 'citations' | 'date'

const SORTS: { key: Sort; label: string }[] = [
  { key: 'score', label: '质量' },
  { key: 'citations', label: '热度' },
  { key: 'date', label: '时间' },
]

const TYPES: RecommendationType[] = ['frontier', 'survey', 'classic', 'github']

export default function Discover() {
  const { data, error, loading } = useAsync(() => loadRecent(7), [])
  const { marks } = useMarks()
  const [query, setQuery] = useState('')
  const [topic, setTopic] = useState<string | null>(null)
  const [type, setType] = useState<RecommendationType | null>(null)
  const [sort, setSort] = useState<Sort>('score')

  const pool = useMemo(() => {
    const rows: { item: Item; date: string }[] = []
    for (const day of data || [])
      for (const item of day.items) rows.push({ item, date: day.arxiv_announce_date })
    return rows
  }, [data])

  const topics = useMemo(() => {
    const counts = new Map<string, number>()
    for (const { item } of pool)
      for (const name of item.topics || []) counts.set(name, (counts.get(name) || 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [pool])

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase()
    const hit = ({ item }: { item: Item }) =>
      !needle ||
      [item.title, item.title_zh, (item.authors || []).join(' '), item.abstract]
        .some((text) => (text || '').toLowerCase().includes(needle))
    const rows = pool.filter(
      (row) =>
        hit(row) &&
        (!topic || (row.item.topics || []).includes(topic)) &&
        (!type || row.item.recommendation_type === type),
    )
    const key = (item: Item) =>
      sort === 'score'
        ? item.quality?.score || 0
        : sort === 'citations'
          ? (item.signals?.citation_count || 0) + (item.signals?.hf_upvotes || 0)
          : Date.parse(item.published_at || '') || 0
    return rows.sort((a, b) => key(b.item) - key(a.item))
  }, [pool, query, topic, type, sort])

  if (loading) return <Loading what="条目池" />
  if (error) return <ErrorBox message={error} />

  return (
    <>
      <div className="space-y-3 px-4 pt-4">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索标题、作者或摘要"
          className="w-full rounded-lg bg-surface px-3 py-2.5 text-[15px] text-body ring-1 ring-line outline-none placeholder:text-faint focus:ring-2 focus:ring-accent"
        />

        <div className="flex flex-wrap gap-1.5 text-xs">
          <Chip on={!type} onClick={() => setType(null)}>全部类型</Chip>
          {TYPES.filter((name) => pool.some((row) => row.item.recommendation_type === name)).map(
            (name) => (
              <Chip key={name} on={type === name} onClick={() => setType(name)}>
                {TYPE_LABEL[name]}
              </Chip>
            ),
          )}
          <span className="w-px" />
          {SORTS.map((option) => (
            <Chip key={option.key} on={sort === option.key} onClick={() => setSort(option.key)}>
              按{option.label}
            </Chip>
          ))}
        </div>

        <div className="flex flex-wrap gap-1.5 text-xs">
          <Chip on={!topic} onClick={() => setTopic(null)}>全部方向</Chip>
          {topics.map(([name, count]) => (
            <Chip key={name} on={topic === name} onClick={() => setTopic(name)}>
              {name} {count}
            </Chip>
          ))}
        </div>

        <p className="text-xs text-muted">
          共 {pool.length} 条（最近 {data?.length || 0} 期），命中 {shown.length} 条
        </p>
      </div>

      {shown.map(({ item, date }) => (
        <ItemCard
          key={`${date}-${item.id}`}
          item={item}
          date={date}
          favorite={!!marks[item.id]?.favorite_at}
          read={!!marks[item.id]?.read_at}
        />
      ))}
      {!shown.length && <Empty>没有命中的条目，换个关键词或清掉筛选。</Empty>}
    </>
  )
}

function Chip({ on, onClick, children }:
  { on: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-full px-2.5 py-1 ${
        on
          ? 'bg-accent text-white'
          : 'bg-surface text-muted ring-1 ring-line hover:text-body'
      }`}
    >
      {children}
    </button>
  )
}
