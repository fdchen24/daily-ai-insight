// 构建后处理：404 回退 + 把数据拷进产物
//
// 1. **`404.html`**。GitHub Pages 没有 history 回退，直接访问
//    `/daily-ai-insight/p/2026-11-23/arxiv-2509.07295` 会 404。Pages 用
//    `404.html` 兜底，把 `index.html` 原样复制一份即可让 SPA 自己接管路由。
// 2. **只拷 `daily/` 与 `archive/`**。`data/` 里还有候选池与 raw（单批次 8MB、
//    可重算），整目录拷进去会让 Pages 产物膨胀到几十 MB。数据目录由
//    `DAI_DATA_DIR` 指定，CI 里指向 data 分支的工作区。
import fs from 'node:fs'
import path from 'node:path'

const here = path.dirname(new URL(import.meta.url).pathname)
const web = path.resolve(here, '..')
const dist = path.join(web, 'dist')
const dataRoot = path.resolve(web, process.env.DAI_DATA_DIR || '../data')

fs.copyFileSync(path.join(dist, 'index.html'), path.join(dist, '404.html'))
console.log('写出 404.html')

let copied = 0
for (const name of ['daily', 'archive']) {
  const from = path.join(dataRoot, name)
  if (!fs.existsSync(from)) {
    console.warn(`跳过 ${name}/：${from} 不存在`)
    continue
  }
  fs.cpSync(from, path.join(dist, 'data', name), { recursive: true })
  copied += 1
}
console.log(`数据拷入 dist/data/（${copied} 个目录，来源 ${dataRoot}）`)
