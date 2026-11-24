/** 本机状态：收藏、已读、阅读进度，存 IndexedDB。
 *
 * 两个设计点：
 *
 * 1. **收藏连标题一起存快照**。只存 id 的话，当期 JSON 被 SW 淘汰或条目滚出
 *    归档后，收藏页就只剩一串 id。快照很小（标题 + 链接 + 类型），换来收藏页
 *    离线也能用。
 * 2. **记录上次导出时间**。iOS 对未安装到主屏的站点，7 天无访问就清除
 *    script-writable storage（DESIGN 7.3），收藏会静默丢失。导出是唯一的保命
 *    手段，所以这里存 `last_export_at`，由 UI 在超期时提示。
 */
import type { Item, RecommendationType } from '../types'

const DB_NAME = 'daily-ai-insight'
const DB_VERSION = 1
const MARKS = 'marks'
const META = 'meta'

export interface MarkSnapshot {
  title: string
  title_zh?: string
  type: RecommendationType
  announce_date?: string
  links?: Item['links']
}

export interface Mark {
  id: string
  favorite_at?: string | null
  read_at?: string | null
  progress?: number
  snapshot?: MarkSnapshot
}

let dbPromise: Promise<IDBDatabase> | null = null

function openDb(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise
  dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(MARKS)) db.createObjectStore(MARKS, { keyPath: 'id' })
      if (!db.objectStoreNames.contains(META)) db.createObjectStore(META)
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
  return dbPromise
}

function tx<T>(store: string, mode: IDBTransactionMode,
               run: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return openDb().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const t = db.transaction(store, mode)
        const req = run(t.objectStore(store))
        req.onsuccess = () => resolve(req.result)
        req.onerror = () => reject(req.error)
      }),
  )
}

// 订阅：收藏状态散落在多个页面，改一处要让所有页面跟着重渲染
const listeners = new Set<() => void>()
export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}
const notify = () => listeners.forEach((fn) => fn())

export async function allMarks(): Promise<Record<string, Mark>> {
  const rows = await tx<Mark[]>(MARKS, 'readonly', (s) => s.getAll() as IDBRequest<Mark[]>)
  return Object.fromEntries(rows.map((row) => [row.id, row]))
}

function snapshotOf(item: Item): MarkSnapshot {
  return {
    title: item.title,
    title_zh: item.title_zh,
    type: item.recommendation_type,
    announce_date: item.arxiv_announce_date,
    links: item.links,
  }
}

async function patch(item: Item | string, change: (mark: Mark) => Mark): Promise<void> {
  const id = typeof item === 'string' ? item : item.id
  const current = (await tx<Mark | undefined>(MARKS, 'readonly', (s) => s.get(id))) ?? { id }
  if (typeof item !== 'string') current.snapshot = snapshotOf(item)
  await tx(MARKS, 'readwrite', (s) => s.put(change(current)))
  notify()
}

export const toggleFavorite = (item: Item, on: boolean) =>
  patch(item, (mark) => ({ ...mark, favorite_at: on ? new Date().toISOString() : null }))

export const markRead = (item: Item) =>
  patch(item, (mark) => ({ ...mark, read_at: mark.read_at || new Date().toISOString() }))

/** 手动开关已读。详情页的自动记已读有停留门槛，但点错进来还是会被记上，
 *  所以留一个能撤回的开关；标为未读只清时间戳，收藏与阅读进度不动。 */
export const setRead = (item: Item, on: boolean) =>
  patch(item, (mark) => ({
    ...mark,
    read_at: on ? mark.read_at || new Date().toISOString() : null,
  }))

export const setProgress = (id: string, progress: number) =>
  patch(id, (mark) => ({ ...mark, progress: Math.round(progress * 100) / 100 }))

export const getMeta = <T>(key: string) =>
  tx<T | undefined>(META, 'readonly', (s) => s.get(key))

export async function setMeta(key: string, value: unknown): Promise<void> {
  await tx(META, 'readwrite', (s) => s.put(value, key))
  notify()
}

// ------------------------------------------------------------ 导出与导入

export interface Backup {
  app: 'daily-ai-insight'
  version: 1
  exported_at: string
  marks: Mark[]
}

export async function exportBackup(): Promise<Backup> {
  const marks = Object.values(await allMarks())
  const now = new Date().toISOString()
  await setMeta('last_export_at', now)
  return { app: 'daily-ai-insight', version: 1, exported_at: now, marks }
}

/** 导入按条目合并而不是整库替换：换机器导入旧备份不该抹掉本机新收藏。
 *  同一条目取两边较新的时间戳。返回写入条数。 */
export async function importBackup(raw: unknown): Promise<number> {
  const backup = raw as Partial<Backup>
  if (!backup || backup.app !== 'daily-ai-insight' || !Array.isArray(backup.marks))
    throw new Error('不是本站导出的备份文件')
  const existing = await allMarks()
  const newer = (a?: string | null, b?: string | null) =>
    !a ? b || null : !b ? a : a > b ? a : b
  for (const mark of backup.marks as Mark[]) {
    if (!mark?.id) continue
    const old = existing[mark.id]
    const merged: Mark = {
      id: mark.id,
      favorite_at: newer(old?.favorite_at, mark.favorite_at),
      read_at: newer(old?.read_at, mark.read_at),
      progress: Math.max(old?.progress ?? 0, mark.progress ?? 0) || undefined,
      snapshot: mark.snapshot ?? old?.snapshot,
    }
    await tx(MARKS, 'readwrite', (s) => s.put(merged))
  }
  notify()
  return backup.marks.length
}
