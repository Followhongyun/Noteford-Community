import { useEffect, useState } from 'react'
import type { LibrarySummary, McpConnection, McpPairBinding } from '../types'
import RemoteMcpPanel from './RemoteMcpPanel'
import MacAiConnections from './MacAiConnections'

export default function McpConnections({ library }: { library: LibrarySummary | null }) {
  const [state, setState] = useState<{ enabled: boolean } | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  useEffect(() => {
    let active = true
    window.noteford.mcpConnectionStatus().then(value => { if (active) setState(value) })
      .catch(() => { if (active) setMessage('无法读取 MCP 连接状态，请重新打开设置') })
    return () => { active = false }
  }, [])
  async function change(action: 'disconnect' | 'reconnect') {
    setBusy(true); setMessage('')
    try {
      setState(await window.noteford.configureMcpConnection({ action }))
      setMessage(action === 'disconnect' ? 'MCP 已断开，授权保留。已开始的同步可能仍会完成。' : '已允许重新连接，下次调用使用当前服务；未自动重试之前的操作。')
    } catch { setMessage('操作未完成，请重新打开设置核对连接状态') }
    finally { setBusy(false) }
  }
  return <section className="settings-group skill-mcp-group" aria-labelledby="settings-mcp-title">
    <h2 id="settings-mcp-title">Skill 与 MCP</h2>
    {window.noteford.platform === 'darwin' ? <MacAiConnections library={library} /> : <PairedMcpConnections library={library} />}
    <div className="skill-mcp-controls" role="group" aria-label="本机连接控制">
      <div className="preference-row">
        <div className="preference-label"><h3>本机连接</h3>
          <p className="preference-description">{state ? (state.enabled ? '允许连接' : '已断开') : '正在读取状态…'}。控制当前账号的本机 AI 调用，保留已有授权。</p>
        </div>
        <div className="preference-control">
          <button className="settings-button" disabled={busy || !state?.enabled} onClick={() => void change('disconnect')}>断开</button>
          <button className="settings-button" disabled={busy || !state} onClick={() => void change('reconnect')}>重新连接</button>
        </div>
      </div>
      <p className="settings-group-note">连接随 Noteford 运行，退出应用后不可用。重新打开后，Skill 会在下次调用时连接当前服务。</p>
      {message && <p className="settings-group-note" role="status">{message}</p>}
    </div>
  </section>
}

function PairedMcpConnections({ library }: { library: LibrarySummary | null }) {
  const [connections, setConnections] = useState<McpConnection[]>([])
  const [bindings, setBindings] = useState<McpPairBinding[]>([])
  const [managing, setManaging] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const api = window.noteford
  const loadBindings = () => api.mcpPairBindings().then(setBindings).catch(() => undefined)
  useEffect(() => {
    let active = true
    api.mcpConnections().then(value => { if (active) setConnections(value) }).catch(() => undefined)
    void loadBindings()
    return () => { active = false }
  }, [])
  async function run(action: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('')
    try { await action() } catch (err) {
      setError(err instanceof Error && err.message ? err.message.replace(/^Error invoking remote method '[^']+': (Error: )?/, '') : '操作失败，请重试')
    } finally { setBusy(false) }
  }
  const active = bindings.filter(item => item.active)
  return <div>
    <div className="preference-row">
      <div className="preference-label"><h3>连接 AI</h3>
        <p className="preference-description">安装 Noteford Skill 后，在 AI 对话中调用即可。首次连接在这里确认，只能搜索和读取已选入 Noteford 的会话历史。</p></div>
      <div className="preference-control">
        <button className="settings-button primary" disabled={busy || !library?.initialized} onClick={() => void run(async () => {
          await api.copyMcpPrompt(); setNotice('安装说明已复制，粘贴给本机 AI 即可')
        })}>复制 Skill 安装说明</button>
      </div>
    </div>
    <p className="settings-group-note mcp-manage-link">
      <button type="button" className="link-button" onClick={() => setManaging(value => !value)}>
        {active.length ? `已连接 ${active.length} 个 AI` : '暂无已连接的 AI'} · {managing ? '收起' : '管理'}
      </button>
    </p>
    {managing ? <div className="mcp-manage-list">
      {bindings.length ? bindings.map(item => <div className="mcp-manage-item" key={item.id}>
        <span title={item.program}>{item.client_name} · {item.active ? '已连接' : '已失效'} · 最近连接 {item.last_used_at ? new Date(item.last_used_at * 1000).toLocaleString() : '—'}</span>
        <button type="button" className="link-button" disabled={busy} onClick={() => void run(async () => { await api.revokeMcpPairBinding({ id: item.id }); await loadBindings(); setNotice('已撤销') })}>撤销</button>
      </div>) : <span className="preference-description">还没有授权过 AI。</span>}
      <RemoteMcpPanel library={library} connections={connections} onConnectionCreated={entry => setConnections(value => [...value, entry])} />
    </div> : null}
    {notice ? <p className="settings-group-note" role="status">{notice}</p> : null}
    {error ? <p className="settings-group-note settings-error" role="alert">{error}</p> : null}
  </div>
}
