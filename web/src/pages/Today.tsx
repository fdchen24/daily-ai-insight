/** 首页 · 今日精选：当期内容按前沿 / 综述 / 经典分组。
 *
 * 日期一律显示数据自带的 `arxiv_announce_date`，不显示"今天"：流水线失败时
 * 不覆盖 `latest.json`（DESIGN 8.2），这时页面上写"今天"就是在撒谎。
 */
import { loadLatest } from '../lib/data'
import { useAsync, useMarks } from '../lib/hooks'
import { TYPE_LABEL } from '../lib/fields'
import { ItemCard } from '../components/ItemCard'
import { Empty, ErrorBox, Loading, SectionTitle } from '../components/ui'
import type { RecommendationType } from '../types'

const ORDER: RecommendationType[] = ['frontier', 'survey', 'classic', 'github']

export default function Today() {
  const { data, error, loading } = useAsync(loadLatest, [])
  const { marks } = useMarks()

  if (loading) return <Loading what="当期内容" />
  if (error || !data) return <ErrorBox message={error || '当期数据为空'} />

  const date = data.arxiv_announce_date
  const stale = date < new Date().toISOString().slice(0, 10)

  return (
    <>
      <div className="px-4 pt-5">
        <h1 className="text-2xl font-bold tracking-tight text-body">{date} 批次</h1>
        <p className="mt-1.5 text-sm text-muted">
          共 {data.items.length} 篇 ·{' '}
          {ORDER.filter((type) => data.counts[type])
            .map((type) => `${TYPE_LABEL[type]} ${data.counts[type]}`)
            .join(' · ')}
        </p>
        {stale && (
          <p className="mt-2.5 rounded-lg border border-line bg-raised px-3 py-2 text-[14px] text-muted">
            这是最近一期已完成的批次（arXiv 按公告日出刊，周末不公告）。
          </p>
        )}
        {data.notes.map((note, index) => (
          <p
            key={index}
            className="mt-2.5 rounded-lg border border-accent/40 bg-accent-soft px-3 py-2 text-[14px] text-body"
          >
            {note}
          </p>
        ))}
      </div>

      {ORDER.map((type) => {
        const group = data.items.filter((item) => item.recommendation_type === type)
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

      {!data.items.length && <Empty>当期没有通过校验的条目。</Empty>}
    </>
  )
}
