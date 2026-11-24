/** 当期 JSON 的数据形状（scripts/archive.py 的 `publish` 产物，DESIGN 8.6）。
 *
 * 解读正文落在 `content` 上，字段名随推荐类型变（前沿 / 综述 / 经典三套模板），
 * 所以这里用索引签名而不是写死七个字段；字段顺序与中文标题在 lib/fields.ts。
 */

export type RecommendationType = 'frontier' | 'survey' | 'classic' | 'github'
export type EvidenceLevel = 'abstract' | 'intro_conclusion' | 'fulltext'

export interface Claim {
  claim: string
  numbers: string[]
  source_span: string | null
  quote: string | null
  verified: boolean
}

export interface ItemValidation {
  metadata_ok: boolean
  links_ok: boolean
  links_present: string[]
  dropped_claims: number
  downgraded_reason: string | null
  warnings: unknown[]
}

export interface Quality {
  dimensions: Record<string, number>
  tier: 'A' | 'B' | 'C'
  score: number
  rank_in_pool?: number
  l1_reason?: string
  l2_reasons?: Record<string, string>
  evidence_level?: EvidenceLevel
}

export interface Signals {
  hf_upvotes?: number
  hf_trending?: boolean
  citation_count?: number
  influential_citation_count?: number | null
  reference_count?: number
  venue?: string | null
  // OpenAlex authorships 里消歧后的作者单位，按作者顺序去重，最多 5 个
  institutions?: string[]
  repo_stars?: number
  repo_stars_7d?: number
  [key: string]: unknown
}

export interface Item {
  id: string
  content_type: string
  source: string[]
  arxiv_id?: string
  arxiv_version?: string
  title: string
  title_zh?: string
  authors: string[]
  // L2 解读从论文首页作者块抄回的单位。比 signals.institutions 可信也更全：
  // OpenAlex 不收录当天公告的预印本，前沿条目几乎只有这个来源有值
  affiliations?: string[]
  abstract?: string
  published_at?: string
  updated_at?: string
  arxiv_announce_date?: string
  primary_category?: string
  categories?: string[]
  comment?: string | null
  doi?: string | null
  links: { paper?: string | null; pdf?: string | null; code?: string | null; project?: string | null }
  signals?: Signals
  channel?: string
  topics?: string[]
  quality?: Quality
  recommendation_type: RecommendationType
  recommended_at?: string
  evidence_level?: EvidenceLevel
  material_note?: string | null
  claims?: Claim[]
  validation?: ItemValidation
  content: Record<string, unknown> & { reading_minutes?: number }
}

export interface Daily {
  arxiv_announce_date: string
  generated_at: string
  counts: Partial<Record<RecommendationType, number>>
  notes: string[]
  items: Item[]
  stats?: Record<string, unknown>
  validation?: Record<string, number>
}

export interface ArchiveDay {
  counts: Partial<Record<RecommendationType, number>>
  total: number
  generated_at: string
  titles: string[]
}

export interface ArchiveIndex {
  year: number
  days: Record<string, ArchiveDay>
  updated_at: string
}

/** 解读里 `findings` / `contributions` 这类"要点 + 展开"的数组行。 */
export interface Point {
  point: string
  detail: string
}
