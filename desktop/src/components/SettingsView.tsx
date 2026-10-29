import { QrCode } from 'lucide-react'
import wechatCard from '../assets/hongyun-wechat-card.jpg'
import OpenSourceCredits from './OpenSourceCredits'
import type {MessageVisibility} from '../message-visibility'
import Avatar from './Avatar'
import McpConnections from './McpConnections'
import { useEffect, useState, type ReactNode } from 'react'

import type { AppStatus, LibrarySummary } from '../types'

interface SettingsViewProps {
  parseStickers: boolean | null
  onStickerParsingChange(enabled: boolean): void
  messageVisibility: MessageVisibility
  onMessageVisibilityChange(value: MessageVisibility): void
  status: AppStatus | null
  library: LibrarySummary | null
  libraryBusy: boolean
  refreshing: boolean
  onRefresh: () => void
  onUnlock: () => void
  onManageAccounts: () => void
}

function SettingRow({ title, description, children }: { title: string; description?: string; children: ReactNode }) {
  return (
    <div className="preference-row">
      <div className="preference-label">
        <h3>{title}</h3>
        {description ? <p className="preference-description">{description}</p> : null}
      </div>
      <div className="preference-control">{children}</div>
    </div>
  )
}

function formatTime(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString('zh-CN') : '—'
}

export default function SettingsView(props: SettingsViewProps) {
  const mac = window.noteford.platform === 'darwin'
  const [syncMinutes, setSyncMinutes] = useState('')
  const [syncTenSeconds, setSyncTenSeconds] = useState(false)
  const [syncBusy, setSyncBusy] = useState(false)
  const [syncNotice, setSyncNotice] = useState('')
  const [syncError, setSyncError] = useState(false)
  const [permissionHelp, setPermissionHelp] = useState('')
  const [permissionBusy, setPermissionBusy] = useState(false)
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 5000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => {
    let cancelled = false
    window.noteford.settings().then(value => { if (!cancelled) { setSyncTenSeconds(value.sync_interval_seconds === 10); setSyncMinutes(String(value.sync_interval_seconds === 10 ? 5 : value.sync_interval_minutes)) } }).catch(() => { if (!cancelled) { setSyncError(true); setSyncNotice('读取失败，请重新打开设置') } })
    return () => { cancelled = true }
  }, [])
  async function saveSync(selectedMinutes = Number(syncMinutes), tenSeconds = false) {
    const minutes = selectedMinutes
    if (!tenSeconds && (!Number.isSafeInteger(minutes) || minutes < 5 || minutes > 1440 || minutes % 5)) { setSyncError(true); setSyncNotice('间隔须为 5 分钟的整数倍，范围为 5 分钟至 24 小时'); return }
    setSyncBusy(true); setSyncError(false)
    try {
      const value = await (tenSeconds ? window.noteford.saveSyncIntervalSeconds(10) : window.noteford.saveSyncInterval(minutes))
      setSyncTenSeconds(value.sync_interval_seconds === 10)
      if (value.sync_interval_seconds !== 10) setSyncMinutes(String(value.sync_interval_minutes))
      setSyncNotice('已保存')
    } catch { setSyncError(true); setSyncNotice('保存失败，请重试') }
    finally { setSyncBusy(false) }
  }
  async function restoreReadPermission() {
    setPermissionBusy(true)
    try {
      await window.noteford.grantPermission({id: 'full_disk'})
      setPermissionHelp('在系统设置中开启 Noteford 的完全磁盘访问权限，然后重启 Noteford。已有资料会保留。')
    } catch { setPermissionHelp('无法打开系统设置，请在“隐私与安全”的“完全磁盘访问权限”中检查 Noteford。') }
    finally { setPermissionBusy(false) }
  }
  const [autoMedia, setAutoMedia] = useState<boolean | null>(null)
  const [mediaLimit, setMediaLimit] = useState(0)
  const [mediaSaving, setMediaSaving] = useState(false)
  const [mediaNotice, setMediaNotice] = useState('')
  const [stickerSaving, setStickerSaving] = useState(false)
  const [stickerNotice, setStickerNotice] = useState('')
  const onStickerParsingChange = props.onStickerParsingChange
  useEffect(() => {
    let cancelled = false
    window.noteford.settings().then(value => {
      if (!cancelled) {
        setAutoMedia(value.auto_media_loading)
        setMediaLimit(value.auto_media_max_mb || 0)
        onStickerParsingChange(value.parse_stickers !== false)
      }
    }).catch(() => { if (!cancelled) { setMediaNotice('读取设置失败，请重新打开设置'); setStickerNotice('读取设置失败，请重新打开设置') } })
    return () => { cancelled = true }
  }, [onStickerParsingChange])
  async function saveStickerParsing(enabled: boolean) {
    setStickerSaving(true); setStickerNotice('')
    try {
      const value = await window.noteford.saveStickerParsing(enabled)
      onStickerParsingChange(value.parse_stickers)
    } catch { setStickerNotice('保存失败，已保留原设置，请重试') }
    finally { setStickerSaving(false) }
  }
  async function saveMediaLimit(limit: number) {
    setMediaSaving(true); setMediaNotice('')
    try { const value = await window.noteford.saveAutoMediaLimit(limit); setMediaLimit(value.auto_media_max_mb) }
    catch { setMediaNotice('保存失败，请重试') }
    finally { setMediaSaving(false) }
  }
  async function saveAutoMedia(enabled: boolean) {
    setMediaSaving(true); setMediaNotice('')
    try { const v = await window.noteford.saveAutoMediaLoading(enabled); setAutoMedia(v.auto_media_loading) }
    catch { setMediaNotice('保存失败，请重试') }
    finally { setMediaSaving(false) }
  }
  const { status, library } = props
  const sync = status?.sync
  const name = status?.library_display_name || library?.display_name || '微信资料库'
  const login = status?.library_id === library?.id ? status?.wechat_login : undefined
  const loginStale = !!login?.checked_at && now - Date.parse(login.checked_at) > 45000
  const loginLabel = !library ? '尚未添加账号' : !login ? '检测中…' : loginStale ? '检测结果已过期' : login.state === 'active' ? '已登录' : login.state === 'not_running' ? '微信未运行' : '无法确认登录'


  return (
    <main className="settings-workspace" aria-labelledby={mac ? 'workspace-page-title' : undefined}>
      {!mac && <header className="settings-page-header">
        <div>
          <h1>设置</h1>
        </div>
      </header>}

      <div className="settings-scroll">
        <section className="settings-group" aria-labelledby="settings-library-title">
          <h2 id="settings-library-title">微信账号</h2>
          {mac ? <div className="mac-settings-account">
            <Avatar name={name} id={library?.id || 'account'} src={library?.avatar_url} size={44}/>
            <div className="mac-settings-account-info"><strong title={name}>{name}</strong><p>{status?.following_conversations.toLocaleString() ?? '—'} 个关注会话 · {status?.managed_messages.toLocaleString() ?? '—'} 条消息</p></div>
            <span className="mac-settings-account-status" role="status" title={login?.checked_at ? `检测时间：${formatTime(login.checked_at)}` : undefined}>{loginLabel}</span>
          </div> : <SettingRow title="微信状态">
            <span role="status" title={login?.checked_at ? `检测时间：${formatTime(login.checked_at)}` : undefined}>{loginLabel}</span>
          </SettingRow>}
        </section>

        <section className="settings-group" aria-labelledby="settings-sync-title">
          <div className="settings-group-heading">
            <h2 id="settings-sync-title">同步</h2>
            <div className="settings-sync-actions">
            {window.noteford.platform !== 'darwin' && <button className="settings-button" onClick={props.onManageAccounts}>管理账号</button>}
            {library && !library.initialized ? (
              <button className="settings-button" disabled={props.libraryBusy} onClick={props.onUnlock}>{props.libraryBusy ? '正在解锁…' : '重新尝试解锁'}</button>
            ) : <button className="settings-button" disabled={props.refreshing} onClick={props.onRefresh}>{props.refreshing ? '同步中…' : '立即同步'}</button>}
            </div>
          </div>
          {library && !library.initialized ? <p className="settings-group-note settings-error">请先在电脑端微信登录这个账号，再尝试解锁。</p> : null}
          <SettingRow title="同步间隔" description={mac ? '选择后自动保存，最长 24 小时。' : '按每轮开始时间计时。应用留在托盘时继续同步；电脑休眠后，恢复时补同步。'}>
            <div className="sync-duration-picker" role="group" aria-label="同步间隔（时:分:秒）">
              <select aria-label="同步间隔小时" title="小时" value={syncTenSeconds ? 0 : syncMinutes ? Math.floor(Number(syncMinutes) / 60) : ''} disabled={syncBusy || !syncMinutes || syncTenSeconds}
                onChange={event => { const hours = Number(event.target.value); void saveSync(hours === 24 ? 1440 : Math.max(5, hours * 60 + Number(syncMinutes) % 60)) }}>
                {!syncMinutes && <option value="">--</option>}
                {Array.from({length: 25}, (_, hour) => <option key={hour} value={hour}>{String(hour).padStart(2, '0')}</option>)}
              </select><span aria-hidden="true">:</span>
              <select aria-label="同步间隔分钟" title="分钟" value={syncTenSeconds ? 0 : syncMinutes ? Number(syncMinutes) % 60 : ''} disabled={syncBusy || !syncMinutes || syncTenSeconds || Number(syncMinutes) === 1440}
                onChange={event => void saveSync(Math.floor(Number(syncMinutes) / 60) * 60 + Number(event.target.value))}>
                {!syncMinutes && <option value="">--</option>}
                {Array.from({length: 12}, (_, index) => index * 5).map(minute => <option key={minute} value={minute} disabled={minute === 0 && Number(syncMinutes) < 60}>{String(minute).padStart(2, '0')}</option>)}
              </select><span aria-hidden="true">:</span>
              <select aria-label="同步间隔秒" title="秒" value={syncTenSeconds ? 10 : 0} disabled={syncBusy || !syncMinutes}
                onChange={event => void saveSync(Number(syncMinutes), Number(event.target.value) === 10)}>
                <option value={0}>00</option><option value={10}>10</option>
              </select>
            </div>
          </SettingRow>
          <div className={`settings-feedback${syncError ? ' settings-error' : ''}`} role={syncError ? 'alert' : 'status'}>{syncNotice}</div>
          <SettingRow title="同步状态">
            <div className="settings-sync-times">
              <span>最近完整同步：{formatTime(sync?.last_success_at)}</span>
              <span>下次同步：{sync?.next_at ? formatTime(sync.next_at) : '等待调度'}</span>
            </div>
            {mac && sync?.error === 'source_permission_required' && <button className="settings-button" disabled={permissionBusy} onClick={() => void restoreReadPermission()}>{permissionBusy ? '正在打开…' : '恢复读取权限'}</button>}
          </SettingRow>
          {permissionHelp && <p className="settings-group-note" role="status">{permissionHelp} <button className="settings-button" onClick={() => void window.noteford.restartForPermissions().catch(() => setPermissionHelp('重启失败，请正常退出并重新打开 Noteford。'))}>重启 Noteford</button></p>}

        </section>


        <section className="settings-group" aria-labelledby="settings-media-title">
          <h2 id="settings-media-title">媒体与表情</h2>
          <SettingRow title={mac ? '自动下载图片、视频和附件' : '自动加载图片和视频'}>
            <label className="settings-switch-control">
              <input type="checkbox" role="switch" aria-label={mac ? '自动下载图片、视频和附件' : '自动加载图片和视频'} checked={autoMedia === true} disabled={autoMedia === null || mediaSaving} onChange={e => void saveAutoMedia(e.target.checked)} />
              <span className="settings-switch-track" />
            </label>
          </SettingRow>
          <SettingRow title="自动下载大小" description="超过所选大小的文件仍可手动下载。选择全部下载时不限制文件大小。">
            <select aria-label="自动下载大小" value={mediaLimit} disabled={autoMedia !== true || mediaSaving} onChange={event => void saveMediaLimit(Number(event.target.value))}>
              <option value={20}>20MB 以下</option><option value={30}>30MB 以下</option><option value={50}>50MB 以下</option><option value={0}>全部下载</option>
            </select>
          </SettingRow>
          {mediaNotice ? <p className="settings-group-note settings-error" role="alert">{mediaNotice}</p> : null}
          <SettingRow title="隐藏表情" description="开启后，单独发送的表情图片和动图显示为 [表情]，不影响文字中的表情。">
            <label className="settings-switch-control">
              <input type="checkbox" role="switch" aria-label="隐藏表情" checked={props.parseStickers === false}
                disabled={autoMedia === null || props.parseStickers === null || stickerSaving} onChange={event => void saveStickerParsing(!event.target.checked)} />
              <span className="settings-switch-track" />
            </label>
          </SettingRow>
          {stickerNotice ? <p className="settings-group-note settings-error" role="alert">{stickerNotice}</p> : null}
          <SettingRow title="媒体统计">
            <dl className="settings-media-summary" aria-label="媒体下载">
              <div><dt>媒体总数：</dt><dd>{status?.media_downloads?.total.toLocaleString() ?? '—'}</dd></div>
              <div><dt>已下载：</dt><dd>{status?.media_downloads?.downloaded.toLocaleString() ?? '—'}</dd></div>
              <div><dt>未下载：</dt><dd>{status?.media_downloads?.not_downloaded.toLocaleString() ?? '—'}</dd></div>
              <div><dt>文件已过期或已被清理：</dt><dd>{status?.media_downloads?.expired.toLocaleString() ?? '—'}</dd></div>
            </dl>
          </SettingRow>
        </section>

        <section className="settings-group" aria-labelledby="settings-message-title">
          <h2 id="settings-message-title">聊天显示</h2>
          <SettingRow title="隐藏撤回提示">
            <label className="settings-switch-control">
              <input type="checkbox" role="switch" aria-label="隐藏撤回提示" checked={props.messageVisibility.hideRevoke} onChange={event => props.onMessageVisibilityChange({...props.messageVisibility, hideRevoke: event.target.checked})} />
              <span className="settings-switch-track" />
            </label>
          </SettingRow>
          <SettingRow title="隐藏拍一拍">
            <label className="settings-switch-control">
              <input type="checkbox" role="switch" aria-label="隐藏拍一拍" checked={props.messageVisibility.hidePat} onChange={event => props.onMessageVisibilityChange({...props.messageVisibility, hidePat: event.target.checked})} />
              <span className="settings-switch-track" />
            </label>
          </SettingRow>
        </section>

        <McpConnections key={library?.id || 'none'} library={library} />

        <section className="settings-group community-about" aria-labelledby="settings-updates-title">
          <div>
            <h2 id="settings-updates-title">关于 Noteford 开源版</h2>
            <p className="preference-description">后续更新请加入洪云会员群获取。</p>
          </div>
          <CommunityContact />
        </section>
        {!mac && <OpenSourceCredits />}
      </div>


    </main>
  )
}

function CommunityContact() {
  const [open, setOpen] = useState(false)
  return <div className="community-contact" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false) }}
    onKeyDown={event => { if (event.key === 'Escape') { setOpen(false); event.stopPropagation() } }}>
    <button className="settings-button community-contact-button" aria-label="查看洪云微信名片" aria-expanded={open}
      aria-controls="community-wechat-card" onFocus={() => setOpen(true)} onClick={() => setOpen(true)}>
      <QrCode size={20} strokeWidth={1.8} aria-hidden="true" />
    </button>
    <div id="community-wechat-card" className="community-contact-preview" hidden={!open}>
      <img src={wechatCard} alt="洪云YUN 微信名片，扫码添加好友" width="1036" height="1320" />
    </div>
  </div>
}
