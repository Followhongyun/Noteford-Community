import Avatar from './Avatar'
import Icon from './Icon'
import type { LibrarySummary } from '../types'

function stateLabel(library: LibrarySummary) {
  if (library.state === 'ready') return library.is_current ? '当前资料库' : '可用'
  if (library.state === 'locked') return '待解锁'
  return library.initialized ? '可用' : '未初始化'
}

export default function LibrarySwitcher({
  libraries,
  open,
  busy,
  onSwitch,
  onAdd,
  onSettings,
  avatarSize = 34,
}: {
  libraries: LibrarySummary[]
  open: boolean
  busy: boolean
  onSwitch(library: LibrarySummary): void
  onAdd(): void
  onSettings(): void
  avatarSize?: number
}) {
  const current = libraries.find(item => item.is_current) || libraries[0] || null

  return (
    <div className="library-switcher">
      <button
        type="button"
        className={'library-avatar-button ' + (open ? 'open' : '')}
        onClick={onSettings}
        title={window.noteford.platform === 'darwin' ? `${current?.display_name || '微信账号'} · 设置` : '设置'}
        aria-label={window.noteford.platform === 'darwin' ? `打开设置，当前账号 ${current?.display_name || '微信账号'}` : '打开设置'}
      >
        <Avatar
          name={current?.display_name || '微'}
          id={current?.id || 'noteford'}
          src={current?.avatar_url}
          size={avatarSize}
        />
        <span className="library-current-name">{current?.display_name || '选择账号'}</span>
      </button>
      {window.noteford.platform === 'darwin' && <span className="mac-account-caption">设置</span>}

      {open ? (
        <div className="library-popover">
          <div className="library-popover-title">微信资料库</div>
          <div className="library-popover-list">
            {libraries.map(library => (
              <button
                type="button"
                className={'library-option ' + (library.is_current ? 'current' : '')}
                key={library.id}
                disabled={busy}
                onClick={() => onSwitch(library)}
              >
                <Avatar
                  name={library.display_name}
                  id={library.id}
                  src={library.avatar_url}
                  size={36}
                />
                <span className="library-option-copy">
                  <strong>{library.display_name}</strong>
                  <small>
                    {stateLabel(library)}
                    {library.following_conversations
                      ? ' · 关注 ' + library.following_conversations
                      : ''}
                  </small>
                </span>
                {library.is_current ? <span className="library-check"><Icon name="check" size={18} /></span> : null}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="library-add"
            onClick={onAdd}
            disabled={busy}
          >
            <Icon name="add" size={18} />
            <span>添加本地微信资料库</span>
          </button>
        </div>
      ) : null}
    </div>
  )
}
