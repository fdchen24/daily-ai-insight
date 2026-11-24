/** 把 IndexedDB 里的收藏/已读读成 React 状态，并在任意页面改动后同步刷新。 */
import { useCallback, useEffect, useState } from 'react'
import { allMarks, subscribe, type Mark } from './store'

export function useMarks() {
  const [marks, setMarks] = useState<Record<string, Mark>>({})
  const [ready, setReady] = useState(false)

  const reload = useCallback(() => {
    allMarks()
      .then(setMarks)
      .catch(() => setMarks({}))
      .finally(() => setReady(true))
  }, [])

  useEffect(() => {
    reload()
    return subscribe(reload)
  }, [reload])

  return { marks, ready }
}

/** 异步数据的通用 hook：加载中 / 失败 / 成功三态，页面不必各写一遍。 */
export function useAsync<T>(load: () => Promise<T>, deps: unknown[]) {
  const [state, setState] = useState<{ data?: T; error?: string; loading: boolean }>({
    loading: true,
  })
  useEffect(() => {
    let alive = true
    setState({ loading: true })
    load()
      .then((data) => alive && setState({ data, loading: false }))
      .catch((err) => alive && setState({ error: String(err?.message || err), loading: false }))
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return state
}
