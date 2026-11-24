/** 机械化校验的结果提示。
 *
 * 只在有问题时出现：元数据与 arXiv 原始记录不一致、链接探活失败、有论断因
 * 查不到来源被删。正常条目不显示任何东西——"一切正常"的横幅没有信息量。
 *
 * `claims` 溯源表（每条数值配原文片段）曾经也渲染在详情页，现已去掉：数值在
 * 落盘前已由 validate.py 在模型实际看到的材料里反查过，查不到的句子直接删，
 * 读者不必再核一遍。数据仍在当期 JSON 的 `claims` 字段里。
 */
import type { ItemValidation } from '../types'

export function ValidationNote({ validation }: { validation?: ItemValidation }) {
  if (!validation) return null
  const issues: string[] = []
  if (!validation.metadata_ok) issues.push('元数据与 arXiv 原始记录有出入')
  if (!validation.links_ok) issues.push('部分链接探活失败')
  if (validation.dropped_claims) issues.push(`${validation.dropped_claims} 条论断因查不到来源被删去`)
  if (validation.downgraded_reason) issues.push(validation.downgraded_reason)
  if (!issues.length) return null

  return (
    <p className="mx-4 rounded-lg border border-amber-500/50 bg-amber-100 px-3 py-2 text-[14px] text-amber-950 dark:bg-amber-950/60 dark:text-amber-100">
      校验提示：{issues.join('；')}
    </p>
  )
}
