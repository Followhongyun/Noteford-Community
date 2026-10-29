import { useEffect, useRef, useState } from 'react'
import type { LibrarySummary } from '../types'
import Icon from './Icon'

export default function MacAiConnections({ library }: { library: LibrarySummary | null }) {
  const [command, setCommand] = useState('')
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    let active = true
    window.noteford.skillInstallCommand().then(result => { if (active) setCommand(result.command) })
      .catch(() => { if (active) setError('无法读取安装命令，请重新打开设置') })
    return () => { active = false; if (timer.current) clearTimeout(timer.current) }
  }, [])
  async function copyCommand() {
    setBusy(true); setError('')
    try {
      const result = await window.noteford.skillInstallCommand({ copy: true })
      if (!result.copied) throw Error('copy_failed')
      setCommand(result.command); setCopied(true)
      if (timer.current) clearTimeout(timer.current)
      timer.current = setTimeout(() => setCopied(false), 3000)
    } catch { setError('复制失败，请重试') }
    finally { setBusy(false) }
  }
  return <div>
    <div className="skill-install-command">
      <code>{command || '正在读取安装命令…'}</code>
      <button type="button" className="settings-button" aria-label={copied ? '已复制' : '复制安装命令'} title={copied ? '已复制' : '复制安装命令'} disabled={busy || !command || !library?.initialized} onClick={() => void copyCommand()}>
        <Icon name={copied ? 'check' : 'copy'} size={18}/>
      </button>
    </div>
    {error && <p className="settings-group-note settings-error" role="alert">{error}</p>}
  </div>
}
