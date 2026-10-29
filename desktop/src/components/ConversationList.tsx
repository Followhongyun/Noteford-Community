import Avatar from './Avatar'
import Icon from './Icon'
import type { Conversation } from '../types'

function formatListTime(timestamp: number) {
  if (!timestamp) return ''
  const date = new Date(timestamp * 1000)
  const now = new Date()
  if (date.toDateString() === now.toDateString()) {
    return date.toLocaleTimeString('zh-CN', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  }
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (date.toDateString() === yesterday.toDateString()) return '昨天'
  return date.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' })
}

function previewText(row: Conversation) {
  if (row.preview) return row.preview.replace(/^.+:\n/, '')
  const fallback: Record<string, string> = {
    image: '[图片]',
    voice: '[语音]',
    video: '[视频]',
    file: '[文件]',
    mini_program: '[小程序]',
  }
  return row.latest_kind ? fallback[row.latest_kind] || '[' + row.latest_kind + ']' : ''
}

export default function ConversationList({
  rows,
  selected,
  onSelect,
  loading,
}: {
  rows: Conversation[]
  selected: string | null
  onSelect(row: Conversation): void
  loading: boolean
}) {
  return (
    <section className="conversation-panel">
      <div className="conversation-scroll">
        {loading && rows.length === 0 ? (
          <div className="panel-state">正在读取关注列表…</div>
        ) : null}
        {!loading && rows.length === 0 ? (
          <div className="panel-state">
            还没有关注任何联系人或群
          </div>
        ) : null}

        {rows.map(row => (
          <button
            type="button"
            className={'conversation-row ' + (selected === row.username ? 'selected' : '')}
            key={row.username}
            aria-current={selected === row.username ? 'true' : undefined}
            title={row.name}
            onClick={() => onSelect(row)}
          >
            <Avatar name={row.name} id={row.username} src={row.avatar_url} size={32} />
            <div className="conversation-copy">
              <div className="conversation-title-line">
                <span className="conversation-name">
                  {row.is_group ? <Icon name="group" size={13} /> : null}
                  <span className="conversation-label">{row.name}</span>
                </span>
                <span className="conversation-time">
                  {formatListTime(row.latest_timestamp)}
                </span>
              </div>
              <div className="conversation-preview">{previewText(row)}</div>
            </div>
          </button>
        ))}
      </div>
    </section>
  )
}
