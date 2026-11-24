// Vite 配置：子路径部署 + PWA + 本地数据直读
//
// 三件事值得说明：
//
// 1. `base` 必须带仓库名。Pages 把站点放在 `/daily-ai-insight/` 下，base 留
//    默认的 `/` 会让资源、路由与 SW scope 全部指向站点根，PWA 注册直接失败。
// 2. 数据 JSON 不进构建产物。`data/` 里混着候选池与 raw（单批次 8MB、可重算），
//    软链进 public 会被原样拷进 dist。开发期用一段中间件直读仓库里的 `data/`，
//    生产由 deploy job 只拷 `daily/` 与 `archive/` 到 `dist/data/`。
// 3. 静态资源 CacheFirst，数据 JSON NetworkFirst。预缓存的 glob 刻意不含 json：
//    否则当期内容被 SW 缓存接管，用户刷新也看不到新一期。
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { VitePWA } from 'vite-plugin-pwa'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const BASE = '/daily-ai-insight/'
const HERE = path.dirname(fileURLToPath(import.meta.url))

/** 开发期把 `<base>data/**` 映射到仓库的数据目录（DAI_DATA_DIR 可改）。 */
function localData(): Plugin {
  const root = path.resolve(HERE, process.env.DAI_DATA_DIR || '../data')
  // 带不带 base 都认：中间件装在 Vite 内部中间件之前还是之后，决定了 req.url
  // 是否已被剥掉 base，这里两种前缀都匹配，省得依赖插件装载顺序
  const prefixes = [`${BASE}data/`, '/data/']
  return {
    name: 'dai-local-data',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const prefix = prefixes.find((candidate) => req.url?.startsWith(candidate))
        if (!req.url || !prefix) return next()
        const rel = decodeURIComponent(req.url.slice(prefix.length).split('?')[0])
        const file = path.resolve(root, rel)
        // 前缀校验挡掉 ../ 穿越：中间件跑在开发机上，别让页面读到仓库外的文件
        if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) return next()
        res.setHeader('Content-Type', 'application/json; charset=utf-8')
        res.setHeader('Cache-Control', 'no-store')
        fs.createReadStream(file).pipe(res)
      })
    },
  }
}

export default defineConfig({
  base: BASE,
  plugins: [
    react(),
    tailwindcss(),
    localData(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.svg'],
      manifest: {
        name: 'Daily AI Insight · 每日 AI 研究精选',
        short_name: 'AI Insight',
        description: '每天精选 AI 前沿论文、深度综述与经典研究，带数值溯源的中文解读。',
        lang: 'zh-CN',
        start_url: BASE,
        scope: BASE,
        display: 'standalone',
        background_color: '#0b0f17',
        theme_color: '#0b0f17',
        icons: [
          { src: 'icons/icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icons/icon-512.png', sizes: '512x512', type: 'image/png' },
          { src: 'icons/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        clientsClaim: true,
        skipWaiting: true,
        globPatterns: ['**/*.{js,css,html,svg,png,woff2}'],
        navigateFallback: `${BASE}index.html`,
        runtimeCaching: [
          {
            // 近 7 期可离线：条目按天分片，40 条额度够覆盖 latest + 7 期 + 年索引
            urlPattern: ({ url }) => url.pathname.startsWith(`${BASE}data/`),
            handler: 'NetworkFirst',
            options: {
              cacheName: 'dai-data',
              networkTimeoutSeconds: 5,
              expiration: { maxEntries: 40, maxAgeSeconds: 60 * 60 * 24 * 30 },
              cacheableResponse: { statuses: [0, 200] },
            },
          },
        ],
      },
    }),
  ],
})
