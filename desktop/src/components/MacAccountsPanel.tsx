import useBackdropDismiss from '../useBackdropDismiss'
import {useEffect, useRef} from 'react'
import Avatar from './Avatar'
import type {LibrarySummary, MacAccountsSnapshot} from '../types'
import {presentConnectCard} from '@noteford/wechat-connect'

export default function MacAccountsPanel({snapshot, libraries, busy, connecting, notice, onClose, onFollow, onSwitch, onConnect, onCancel}: {
  snapshot: MacAccountsSnapshot | null
  libraries: LibrarySummary[]
  busy: boolean
  connecting: string | null
  notice: string
  onClose(): void
  onFollow(enabled: boolean): void
  onSwitch(library: LibrarySummary): void
  onConnect(id?: string): void
  onCancel(): void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => { dialog.current?.showModal() }, [])
  const running = !!snapshot?.capture.running || busy || !!connecting
  const card = presentConnectCard(snapshot?.capture)
  const backdropDismiss = useBackdropDismiss(onClose)
  return <dialog {...backdropDismiss} ref={dialog} className="mac-accounts-panel" aria-labelledby="mac-accounts-title" onCancel={event => { event.preventDefault(); onClose() }}>
    <header><h1 id="mac-accounts-title">微信账号</h1><button className="settings-button" onClick={onClose}>完成</button></header>
    <label className="mac-accounts-follow"><input type="checkbox" checked={snapshot?.follow_wechat ?? true} disabled={!snapshot || running} onChange={e => onFollow(e.target.checked)} />跟随正版微信切换账号</label>
    <p>已连接的账号会自动切换。手动选择账号可查看本地历史，并暂停跟随。</p>
    <div className="mac-accounts-list">
      {libraries.map(library => {
        const source = snapshot?.accounts.find(item => item.library_id === library.id)
        return <div className="mac-account-row" key={library.id}>
          <Avatar id={library.id} name={library.display_name} src={library.avatar_url} size={44}/>
          <div><strong>{library.display_name}</strong><small>{library.is_current ? '当前查看' : '已连接'} · {source?.logged_in ? '微信已登录' : '本地历史'}</small></div>
          <button className="settings-button" disabled={running} onClick={() => onSwitch(library)}>{library.is_current ? '查看此账号' : '切换'}</button>
          <button className="settings-button" disabled={running} onClick={() => onConnect(library.id)}>重新连接</button>
        </div>
      })}
    </div>
    <h2>添加微信账号</h2>
    <p>点击添加后，用要添加的微信扫码登录。登录成功后会自动读取资料，并将头像和昵称加入这里。</p>
    <p>连接期间会临时退出正版微信，完成后自动恢复。已有账号的历史会保留。</p>
    <button className="settings-button settings-save" disabled={running} onClick={() => onConnect()}>添加微信账号</button>
    {(connecting || snapshot?.capture.running) && <div className="mac-account-feedback">
      <p role="status">{snapshot?.capture.phase === 'sync' ? '正在读取账号资料…' : card.actionLabel}{card.status === 'waiting' ? '，请在登录窗口选择账号并完成扫码。' : ''}</p>
      <button className="settings-button" onClick={onCancel}>取消添加</button>
    </div>}
    {notice && <p role="status" className="mac-account-feedback">{notice}</p>}
  </dialog>
}
