/** 解读字段的顺序与中文标题，按推荐类型分三套（prompt 模板各自的输出字段）。
 *
 * `why_read` 不进字段表：它是卡片上的 TL;DR，详情页由 Detail 单独排在正文前，
 * 不再作为"为何值得读"重复出现一节。
 */
import type { RecommendationType } from '../types'

export interface FieldSpec {
  key: string
  label: string
}

const FRONTIER: FieldSpec[] = [
  { key: 'task', label: '任务与问题' },
  { key: 'findings', label: '问题根因' },
  // 方法分两节：先给不做这个子方向的人一段不含符号的核心想法，再给要判断
  // 能不能复用的人技术实现。`method` 是 2026-11-27 之前的旧字段名，归档里
  // 还有一批那种结构的条目，留着它们才不会掉成无标题的兜底节。
  { key: 'method_idea', label: '方法 · 核心想法' },
  { key: 'method_how', label: '方法 · 技术实现' },
  { key: 'method', label: '方法设计' },
  { key: 'results', label: '实验结果' },
  { key: 'limitations', label: '局限' },
]

const SURVEY: FieldSpec[] = [
  { key: 'scope', label: '覆盖范围' },
  { key: 'evolution', label: '发展脉络' },
  { key: 'approaches', label: '主要路线' },
  { key: 'key_differences', label: '关键差异' },
  { key: 'bottlenecks', label: '瓶颈' },
  { key: 'future_directions', label: '未来方向' },
]

const CLASSIC: FieldSpec[] = [
  { key: 'historical_context', label: '历史背景' },
  { key: 'problem_then', label: '当时的问题' },
  { key: 'contributions', label: '主要贡献' },
  { key: 'core_idea', label: '核心思想' },
  { key: 'influence', label: '后续影响' },
  { key: 'modern_reading', label: '当下读法' },
]

const GITHUB: FieldSpec[] = [
  { key: 'problem', label: '解决什么问题' },
  { key: 'features', label: '核心功能' },
  { key: 'architecture', label: '技术架构' },
  { key: 'vs_alternatives', label: '对比现有方案' },
  { key: 'usage', label: '安装与使用' },
  { key: 'scope_limits', label: '适用与局限' },
]

const BY_TYPE: Record<RecommendationType, FieldSpec[]> = {
  frontier: FRONTIER,
  survey: SURVEY,
  classic: CLASSIC,
  github: GITHUB,
}

/** 取字段表。模板换了字段名也不会白屏：实际存在的键按原样兜底补在后面。 */
export function fieldsFor(type: RecommendationType,
                          content: Record<string, unknown>): FieldSpec[] {
  const spec = BY_TYPE[type] ?? FRONTIER
  // why_read / why_today 已由详情页单独渲染，兜底时要排除，否则会多出一节
  const known = new Set([...spec.map((f) => f.key),
                         'reading_minutes', 'why_read', 'why_today'])
  const extra = Object.keys(content)
    .filter((k) => !known.has(k))
    .map((k) => ({ key: k, label: k }))
  return [...spec.filter((f) => content[f.key] != null), ...extra]
}

export const TYPE_LABEL: Record<RecommendationType, string> = {
  frontier: '前沿',
  survey: '综述',
  classic: '经典',
  github: '开源',
}

export const EVIDENCE_LABEL: Record<string, string> = {
  abstract: '仅摘要',
  intro_conclusion: '引言结论',
  fulltext: '全文',
}

/** 摘要式的一句话：卡片上显示"为何值得读"的前若干字。 */
export function leadText(content: Record<string, unknown>): string {
  for (const key of ['why_read', 'why_today']) {
    const value = content[key]
    if (typeof value === 'string' && value) return value
  }
  return ''
}
