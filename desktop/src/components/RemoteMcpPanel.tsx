import { useCallback, useEffect, useState } from 'react'
import type { LibrarySummary, McpConnection, RemoteMcpGrant, RemoteMcpPending, RemoteMcpStatus } from '../types'

const NEW_ALL = '__new_all_synced__'

export default function RemoteMcpPanel({ library, connections, onConnectionCreated }: {
  library: LibrarySummary | null
  connections: McpConnection[]
  onConnectionCreated: (entry: McpConnection) => void
}) {
  const [status, setStatus] = useState<RemoteMcpStatus | null>(null)
  const [url, setUrl] = useState('')
  const [code, setCode] = useState<{ code: string, expires_at: number } | null>(null)
  const [pending, setPending] = useState<RemoteMcpPending[]>([])
  const [grants, setGrants] = useState<RemoteMcpGrant[]>([])
  const [choice, setChoice] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [now, setNow] = useState(() => Date.now() / 1000)
  const api = window.noteford

  const refresh = useCallback(async () => {
    const value = await api.remoteMcpStatus()
    setStatus(value)
    if (value.running) {
      setPending(await api.remoteMcpPending().catch(() => []))
      setGrants(await api.remoteMcpGrants().catch(() => []))
    } else { setPending([]); setGrants([]) }
  }, [api])

  useEffect(() => { void refresh().then(() => undefined).catch(() => setError('读取远程连接状态失败')) }, [refresh])
  useEffect(() => { api.remoteMcpStatus().then(value => setUrl(value.public_url)).catch(() => undefined) }, [api])
  useEffect(() => {
    if (!status?.running) return
    const timer = window.setInterval(() => { setNow(Date.now() / 1000); void refresh().catch(() => undefined) }, 2000)
    return () => window.clearInterval(timer)
  }, [status?.running, refresh])

  async function run(action: () => Promise<void>) {
    setBusy(true); setError('')
    try { await action() } catch (err) { setError(err instanceof Error ? err.message.replace(/^Error invoking remote method '[^']+': (Error: )?/, '') : '操作失败') } finally { setBusy(false) }
  }
  const usable = connections.filter(item => item.enabled && item.library_id === library?.id)
  const left = code ? Math.max(0, Math.round(code.expires_at - now)) : 0

  return <div className="remote-mcp">
    <div className="preference-row">
      <div className="preference-label"><h3>远程连接（云端 AI）</h3>
        <p className="preference-description">默认关闭。开启后，云端 AI 通过公网 HTTPS 地址连接，必须输入本页生成的验证码并经你确认会话范围后才能只读访问。仅在 Noteford 运行时可用。</p></div>
      <div className="preference-control">
        <label className="switch"><input type="checkbox" aria-label="开启远程连接" checked={Boolean(status?.enabled)} disabled={busy}
          onChange={event => void run(async () => { setStatus(await api.configureRemoteMcp({ enabled: event.target.checked, public_url: url })); setCode(null) })} /> {status?.enabled ? (status.running ? '运行中' : '已开启，未运行') : '已关闭'}</label>
      </div>
    </div>
    <div className="preference-row">
      <div className="preference-label"><h3>公网地址</h3><p className="preference-description">Cloudflare Tunnel 等提供的 https 域名，转发到本机 127.0.0.1:{status?.port ?? 8765}。</p></div>
      <div className="preference-control">
        <input aria-label="公网 HTTPS 地址" value={url} placeholder="https://noteford.example.com" disabled={busy} onChange={event => setUrl(event.target.value)} />
        <button className="settings-button" disabled={busy || url === status?.public_url} onClick={() => void run(async () => setStatus(await api.configureRemoteMcp({ enabled: Boolean(status?.enabled), public_url: url })))}>保存</button>
      </div>
    </div>
    {status?.running ? <>
      <div className="preference-row">
        <div className="preference-label"><h3>MCP 地址</h3><p className="preference-description"><code>{status.mcp_url}</code>（在 AI 里添加这个地址）</p></div>
        <div className="preference-control">
          <button className="settings-button primary" disabled={busy} onClick={() => void run(async () => setCode(await api.remoteMcpPairCode()))}>生成验证码</button>
        </div>
      </div>
      {code && left > 0 ? <p className="remote-mcp-code" role="status">验证码 <strong>{code.code.slice(0, 4)}-{code.code.slice(4)}</strong> · {Math.floor(left / 60)}:{String(left % 60).padStart(2, '0')} 后失效，仅可使用一次</p> : null}
      {pending.map(item => <div className="preference-row remote-mcp-pending" key={item.display_id}>
        <div className="preference-label"><h3>「{item.client_name}」请求访问资料</h3><p className="preference-description">验证码已通过。新建连接可读全部已同步资料并刷新同步，不能修改关注、设置或删除；沿用连接保留原权限。不认识请拒绝。</p></div>
        <div className="preference-control">
          <select aria-label="授权范围" value={choice[item.display_id] ?? NEW_ALL} disabled={busy} onChange={event => setChoice(value => ({ ...value, [item.display_id]: event.target.value }))}>
            <option value={NEW_ALL}>新建：{library?.display_name ?? '当前账号'} 全部已同步资料 + 刷新同步</option>
            {usable.map(conn => <option key={conn.id} value={conn.id}>沿用连接「{conn.label}」（{conn.scope === 'all_indexed' ? '全部已同步资料' : conn.scope === 'all_synced' ? '已选会话历史' : `${conn.conversations.length} 个授权会话`}）</option>)}
          </select>
          <button className="settings-button primary" disabled={busy || !library} onClick={() => void run(async () => {
            let grant = choice[item.display_id] ?? NEW_ALL
            if (grant === NEW_ALL) {
              if (!library) return
              const entry = await api.createMcpConnection({ label: `远程：${item.client_name}`.slice(0, 80), library_id: library.id, conversations: [], scope: 'all_indexed', allow_sync: true })
              onConnectionCreated(entry); grant = entry.id
            }
            await api.remoteMcpApprove({ display_id: item.display_id, grant_client: grant }); await refresh()
          })}>确认授权</button>
          <button className="settings-button" disabled={busy} onClick={() => void run(async () => { await api.remoteMcpDeny({ display_id: item.display_id }); await refresh() })}>拒绝</button>
        </div>
      </div>)}
      {grants.map(item => <div className="preference-row" key={item.id}>
        <div className="preference-label"><h3>{item.client_name}</h3><p className="preference-description">远程授权 · {item.active ? '有效' : '已失效'} · {new Date(item.created_at * 1000).toLocaleString()}</p></div>
        <div className="preference-control"><button className="settings-button" disabled={busy} onClick={() => void run(async () => { await api.remoteMcpRevoke({ id: item.id }); await refresh() })}>撤销</button></div>
      </div>)}
    </> : null}
    {status?.error ? <p className="settings-group-note settings-error">{status.error}</p> : null}
    {error ? <p className="settings-group-note settings-error" role="alert">{error}</p> : null}
  </div>
}
