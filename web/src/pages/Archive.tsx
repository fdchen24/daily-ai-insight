/** 历史归档：按年索引列出批次日，点进去看那一期的完整条目。
 *
 * 索引按年分片（DESIGN 8.3），所以这里一次只下载一个年份的索引文件；条目正文
 * 只在点开某一期时才取，首页与归档列表都不会把全部历史拖下来。
 */
import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { loadArchiveIndex, loadDay } from '../lib/data'
import { useAsync, useMarks } from '../lib/hooks'
import { TYPE_LABEL } from '../lib/fields'
import { ItemCard } from '../components/ItemCard'
import { Empty, ErrorBox, Loading, SectionTitle } from '../components/ui'
import type { RecommendationType } from '../types'

const ORDER: RecommendationType[] = ['frontier', 'survey', 'classic', 'github']

function DayView({ date }: { date: string }) {
  const { data, error, loading } = useAsync(() => loadDay(date), [date])
  const { marks } = useMarks()

  if (loading) return <Loading what={`${date} 批次`} />
  if (error) return <ErrorBox message={error} />

  return (
    <>
      <div className="px-4 pt-4">
        <Link to="/archive" className="text-xs text-muted hover:text-accent">
          ← 归档
        </Link>
        <h1 className="mt-2 text-xl font-bold tracking-tight">{date} 批次</h1>
        <p className="mt-1.5 text-sm text-muted">
          共 {data?.items.length || 0} 篇
        </p>
      </div>
      {ORDER.map((type) => {
        const group = (data?.items || []).filter((item) => item.recommendation_type === type)
        if (!group.length) return null
        return (
          <section key={type}>
            <SectionTitle count={group.length}>{TYPE_LABEL[type]}</SectionTitle>
            {group.map((item) => (
              <ItemCard
                key={item.id}
                item={item}
                date={date}
                favorite={!!marks[item.id]?.favorite_at}
                read={!!marks[item.id]?.read_at}
              />
            ))}
          </section>
        )
      })}
    </>
  )
}

export default function Archive() {
  const { date } = useParams()
  const [year, setYear] = useState(new Date().getFullYear())
  const { data, error, loading } = useAsync(() => loadArchiveIndex(year), [year])

  if (date) return <DayView date={date} />
  if (loading) return <Loading what="归档索引" />
  if (error) return <ErrorBox message={error} />

  const days = Object.entries(data?.days || {}).sort((a, b) => b[0].localeCompare(a[0]))

  return (
    <>
      <div className="flex items-center gap-2 px-4 pt-4">
        <h1 className="flex-1 text-xl font-bold tracking-tight">{year} 年归档</h1>
        <button
          type="button"
          onClick={() => setYear(year - 1)}
          className="rounded-full border border-line px-2.5 py-1 text-sm text-muted hover:text-accent"
        >
          ← {year - 1}
        </button>
        {year < new Date().getFullYear() && (
          <button
            type="button"
            onClick={() => setYear(year + 1)}
            className="rounded-full border border-line px-2.5 py-1 text-sm text-muted hover:text-accent"
          >
            {year + 1} →
          </button>
        )}
      </div>

      <ul className="mt-3">
        {days.map(([day, row]) => (
          <li
            key={day}
            className="mx-3 mb-2 rounded-xl border border-line bg-surface px-4 py-3.5"
          >
            <Link to={`/archive/${day}`} className="group block">
              <div className="flex items-baseline gap-2">
                <span className="text-[17px] font-bold text-body group-hover:text-accent">
                  {day}
                </span>
                <span className="text-xs text-muted">
                  {ORDER.filter((type) => row.counts[type])
                    .map((type) => `${TYPE_LABEL[type]} ${row.counts[type]}`)
                    .join(' · ')}
                </span>
              </div>
              <p className="mt-1.5 line-clamp-2 text-[14px] text-muted">
                {row.titles.slice(0, 4).join(' / ')}
              </p>
            </Link>
          </li>
        ))}
      </ul>

      {!days.length && <Empty>{year} 年还没有归档。</Empty>}
    </>
  )
}
