/** 公式渲染：把 `$...$` 与 `$$...$$` 交给 KaTeX，其余按纯文本输出。
 *
 * 为什么需要它：解读里的损失函数、维度、下标以前是纯文本直出，屏幕上长成
 * `L_RECA=L(fθ(concat(t_template,hv)),I)` 这种一眼看不出结构的东西。解读 prompt
 * 现在要求公式写成 LaTeX 并用 `$` 包起来，这里负责把它画出来。
 *
 * 旧数据没有 `$`，会原样走纯文本分支——不会报错，只是没有公式效果，重跑解读
 * 后才有（解读缓存键已带 prompt 哈希，改了 prompt 就会重算）。
 */
import { useMemo } from 'react'
import katex from 'katex'

// 先吃 $$...$$ 再吃 $...$；`\$` 转义的美元符号不当分隔符
const SEGMENT = /\$\$([\s\S]+?)\$\$|(?<!\\)\$([^$\n]+?)(?<!\\)\$/g

interface Segment {
  kind: 'text' | 'inline' | 'block'
  value: string
}

function split(text: string): Segment[] {
  const out: Segment[] = []
  let last = 0
  for (const match of text.matchAll(SEGMENT)) {
    const start = match.index ?? 0
    if (start > last) out.push({ kind: 'text', value: text.slice(last, start) })
    out.push(
      match[1] != null
        ? { kind: 'block', value: match[1] }
        : { kind: 'inline', value: match[2] },
    )
    last = start + match[0].length
  }
  if (last < text.length) out.push({ kind: 'text', value: text.slice(last) })
  return out
}

function render(tex: string, display: boolean): string {
  try {
    // trust 默认 false：禁掉 \href、\includegraphics 等能注入内容的命令
    return katex.renderToString(tex.trim(), {
      displayMode: display,
      throwOnError: false,
      strict: 'ignore',
      output: 'html',
    })
  } catch {
    return ''
  }
}

/** 带公式的文本。公式画不出来就退回显示原始 LaTeX，不让整段消失。 */
export function RichText({ text, className }: { text: string; className?: string }) {
  const segments = useMemo(() => split(text), [text])
  if (segments.length === 1 && segments[0].kind === 'text')
    return <span className={className}>{text}</span>

  return (
    <span className={className}>
      {segments.map((segment, index) => {
        if (segment.kind === 'text') return <span key={index}>{segment.value}</span>
        const html = render(segment.value, segment.kind === 'block')
        if (!html) return <code key={index}>{segment.value}</code>
        return segment.kind === 'block' ? (
          <span key={index} className="math-block block" dangerouslySetInnerHTML={{ __html: html }} />
        ) : (
          <span key={index} dangerouslySetInnerHTML={{ __html: html }} />
        )
      })}
    </span>
  )
}

/** 纯文本预览（卡片摘要、列表标题）：去掉 `$`，公式位置留原样文字。 */
export function stripMath(text: string): string {
  return text.replace(/\$\$?/g, '')
}
