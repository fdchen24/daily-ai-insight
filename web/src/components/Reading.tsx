/** 解读正文。字段顺序与标题来自 lib/fields，值可能是字符串或"要点+展开"数组。
 *
 * 只有一种版本：原先的简版 / 详细版切换（DESIGN 7.4）已去掉——解读本身就按
 * "任务与问题 → 根因 → 方法 → 结果 → 局限"分了节，想跳读直接滚，多一个开关
 * 只是让人每次进来先做一次无谓的选择。
 *
 * 正文一律过 `RichText`：`$...$` 里的内容交给 KaTeX，公式才不会以
 * `L_RECA=L(fθ(...))` 这种纯文本形态出现。
 */
import type { Point, RecommendationType } from '../types'
import { fieldsFor } from '../lib/fields'
import { RichText } from './Math'

function PointList({ rows }: { rows: Point[] }) {
  return (
    <ol className="mt-2 space-y-2.5">
      {rows.map((row, index) => (
        <li key={index} className="rounded-lg border border-line bg-raised px-3.5 py-3">
          <p className="font-bold text-body">
            <span className="mr-1.5 text-accent">{index + 1}.</span>
            <RichText text={row.point} />
          </p>
          {row.detail && (
            <p className="mt-1.5 text-[16px] leading-[1.8] text-body">
              <RichText text={row.detail} />
            </p>
          )}
        </li>
      ))}
    </ol>
  )
}

function isPointList(value: unknown): value is Point[] {
  return Array.isArray(value) && value.every((row) => row && typeof row === 'object' && 'point' in row)
}

export function Reading({ type, content }:
  { type: RecommendationType; content: Record<string, unknown> }) {
  const fields = fieldsFor(type, content)

  return (
    <div className="space-y-6 px-4 py-5">
      {fields.map((field) => {
        const value = content[field.key]
        return (
          <section key={field.key}>
            <h3 className="flex items-center gap-2 text-[15px] font-bold text-body">
              <span className="h-3.5 w-1 rounded bg-accent" />
              {field.label}
            </h3>
            {isPointList(value) ? (
              <PointList rows={value} />
            ) : Array.isArray(value) ? (
              <ul className="mt-2 list-disc space-y-1.5 pl-6 text-[16px] leading-[1.8]">
                {value.map((row, index) => (
                  <li key={index}>
                    <RichText text={String(row)} />
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-[16px] leading-[1.85] text-body">
                <RichText text={String(value)} />
              </p>
            )}
          </section>
        )
      })}
    </div>
  )
}
