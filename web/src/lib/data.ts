/** 数据读取：全部走静态 JSON，没有后端。
 *
 * 路径前缀用 `import.meta.env.BASE_URL`，这样子路径部署（`/daily-ai-insight/`）
 * 与本地开发（同一个 base）共用一套代码。首页只取 `latest.json`，历史页按年取
 * 索引、按天取当期文件——归档索引按年分片正是为了让首页不下载全部历史。
 */
import type { ArchiveIndex, Daily } from '../types'

const BASE = import.meta.env.BASE_URL

export const dataUrl = (rel: string) => `${BASE}data/${rel}`

const cache = new Map<string, unknown>()

async function getJson<T>(rel: string): Promise<T> {
  const hit = cache.get(rel)
  if (hit) return hit as T
  const resp = await fetch(dataUrl(rel), { cache: 'no-cache' })
  if (!resp.ok) throw new Error(`${rel} 读取失败：HTTP ${resp.status}`)
  const data = (await resp.json()) as T
  cache.set(rel, data)
  return data
}

export const loadLatest = () => getJson<Daily>('daily/latest.json')

/** 按批次日取当期完整内容。`date` 形如 `2026-11-23`。 */
export function loadDay(date: string): Promise<Daily> {
  const [y, m, d] = date.split('-')
  return getJson<Daily>(`daily/${y}/${m}/${d}.json`)
}

export const loadArchiveIndex = (year: number) =>
  getJson<ArchiveIndex>(`archive/index-${year}.json`)

/** 最近若干期的批次日，倒序。跨年时把去年的索引也读一份。 */
export async function recentDates(limit = 7): Promise<string[]> {
  const thisYear = new Date().getFullYear()
  const indexes = await Promise.all(
    [thisYear, thisYear - 1].map((year) =>
      loadArchiveIndex(year).catch(() => null),
    ),
  )
  const days = indexes.flatMap((index) => Object.keys(index?.days || {}))
  return days.sort().reverse().slice(0, limit)
}

/** 发现页与归档搜索的条目池：最近 N 期合并。
 *
 * 静态站点没有全库检索接口，而全历史合并下载会越跑越慢，所以池子封顶在最近
 * 7 期——与离线缓存的期数一致，用户看到的范围和能离线读的范围是同一个。
 */
export async function loadRecent(limit = 7): Promise<Daily[]> {
  const dates = await recentDates(limit)
  const days = await Promise.all(dates.map((date) => loadDay(date).catch(() => null)))
  return days.filter((day): day is Daily => !!day)
}
