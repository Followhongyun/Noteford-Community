import {useEffect, useRef} from 'react'

/** A bottom sentinel inside the list's scroll container. */
export default function AutoLoadMore({cursor, busy, onLoad}: {
  cursor: string | number
  busy: boolean
  onLoad(): Promise<void>
}) {
  const element = useRef<HTMLDivElement>(null)
  const load = useRef(onLoad)
  const attempted = useRef<string | number | null>(null)
  load.current = onLoad
  useEffect(() => {
    const target = element.current
    if (!target || busy) return
    let active = true
    const observer = new IntersectionObserver(entries => {
      if (!active) return
      if (!entries[0].isIntersecting) { attempted.current = null; return }
      // Re-renders and busy transitions must not retry a failed or stalled cursor.
      // Leaving and re-entering the edge permits an intentional retry.
      if (attempted.current === cursor) return
      attempted.current = cursor
      void load.current()
    }, {root: target.parentElement, rootMargin: '0px 0px 240px 0px'})
    observer.observe(target)
    return () => { active = false; observer.disconnect() }
  }, [cursor, busy])
  return <div ref={element} className="auto-load-more" role="status">{busy ? '正在加载…' : null}</div>
}
