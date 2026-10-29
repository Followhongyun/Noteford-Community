import { useEffect, useState } from 'react'
import type { AppStatus } from '../types'
import { syncCountdown } from '../sync-countdown'
import Icon from './Icon'

export default function SidebarSync({ sync, refreshing, disabled, onSync }: {
  sync: AppStatus['sync']; refreshing: boolean; disabled: boolean; onSync(): void
}) {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const tick = () => setNow(Date.now())
    const timer = window.setInterval(tick, 1000)
    document.addEventListener('visibilitychange', tick)
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', tick) }
  }, [])
  const busy = refreshing || sync?.state === 'running'
  const countdown = disabled || sync?.scheduler_running === false ? '--:--' : syncCountdown(sync?.next_at, now)
  const deadline = sync?.next_at ? Date.parse(sync.next_at) : NaN
  const detail = !disabled && sync?.scheduler_running !== false && Number.isFinite(deadline) ? `下次同步：${new Date(deadline).toLocaleString('zh-CN')}` : '等待同步计划'
  return <button className="sidebar-sync" title={busy ? '正在同步全部本机资料…' : `立即同步全部本机资料\n${detail}`}
    aria-label={busy ? '同步中' : `立即同步，${countdown}`} aria-busy={busy} disabled={busy || disabled} onClick={onSync}>
    <Icon name="refresh" className={busy ? 'sync-spinning' : ''} />
    <span className="sync-countdown">{busy ? '…' : countdown}</span>
  </button>
}
