import type { ReactNode } from 'react'
import type { Message } from '../types'
import Icon from './Icon'
import { ExternalLink } from './LinkText'

function dayKey(timestamp: number) {
  const date = new Date(timestamp * 1000)
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

function fileSize(bytes?: number | null) {
  if (bytes == null || bytes < 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function linkSource(message: Message) {
  if (message.url) {
    try { return new URL(message.url).hostname } catch { /* Keep malformed source text out of the label. */ }
  }
  return message.type.kind.startsWith('channel') ? '视频号' : message.type.kind === 'mini_program' ? '小程序' : '链接'
}

export default function ChatResults({ messages, category, renderMedia, onLocate, locating }: {
  messages: Message[]
  category: string
  renderMedia(message: Message): ReactNode
  onLocate(message: Message): void
  locating: boolean
}) {
  const days = new Map<string, Message[]>()
  // The chat API returns chronological pages; result browsing is newest first.
  for (const message of [...messages].sort((a, b) => b.timestamp - a.timestamp || b.uid.localeCompare(a.uid))) {
    const day = dayKey(message.timestamp)
    if (!days.has(day)) days.set(day, [])
    days.get(day)!.push(message)
  }
  return <div className={`chat-results chat-results-${category}`}>
    {[...days].map(([day, items]) => <section className="result-day" key={day} aria-label={day}>
      <h2><time dateTime={day}>{new Date(items[0].timestamp * 1000).toLocaleDateString('zh-CN', {year: 'numeric', month: 'long', day: 'numeric'})}</time></h2>
      <div className={category.split(',').some(kind => ['media', 'image', 'video'].includes(kind)) ? 'result-media-grid' : 'result-list'}>
        {items.map(message => {
          const category = ['image', 'video'].includes(message.type.kind) ? 'media' : message.type.kind === 'file' ? 'file' : 'link'
          const stamp = new Date(message.timestamp * 1000).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false})
          const sender = message.sender.name || (message.sender.from_me ? '我' : '未知发送人')
          const title = message.title || message.url || message.text || (category === 'file' ? '文件' : '链接')
          const extension = category === 'file' ? (/\.([a-z0-9]{1,8})$/i.exec(message.title || '')?.[1]?.toUpperCase() || 'FILE') : ''
          const cover = message.card?.thumbnail_url || message.card?.cover_url
          return category === 'media' ? <article className="result-media-item" key={message.uid} data-message-uid={message.uid} aria-label={`${sender} ${stamp}`}>
            {renderMedia(message)}
            <button type="button" className="result-locate" disabled={locating} title={`${sender} · ${stamp} · 定位到聊天位置`} aria-label="定位到聊天位置" onClick={() => onLocate(message)}><Icon name="chat" size={14} /><span>定位消息</span></button>
          </article> : <article className={`result-row result-${category}-row`} key={message.uid} data-message-uid={message.uid}>
            {category === 'file' ? <span className="result-file-icon" data-extension={extension}><Icon name="file" size={26} /><span>{extension}</span></span> : null}
            <div className="result-row-copy">
              <h3>{category === 'file' ? <button className="result-file-open" type="button" disabled={locating} onClick={() => onLocate(message)}>{title}</button>
                : message.url ? <ExternalLink url={message.url}>{title}</ExternalLink> : <button className="result-file-open" type="button" disabled={locating} onClick={() => onLocate(message)}>{title}</button>}</h3>
              {category === 'link' && message.text && message.text !== title ? <p className="result-link-summary">{message.text}</p> : null}
              <div className="result-row-meta">
                {category === 'file' && fileSize(message.media?.bytes) ? <span>{fileSize(message.media?.bytes)}</span> : null}
                {category === 'link' ? <span className="result-link-source">{linkSource(message)}</span> : null}
                <span>{sender}</span><time dateTime={new Date(message.timestamp * 1000).toISOString()}>{stamp}</time>
              </div>
            </div>
            {category === 'link' ? <span className="result-link-cover">{cover ? <img src={cover} alt="" loading="lazy" onError={event => { event.currentTarget.style.display = 'none' }} /> : <Icon name="link" size={24} />}</span> : null}
            <button type="button" className="result-locate" disabled={locating} title="定位到聊天位置" aria-label="定位到聊天位置" onClick={() => onLocate(message)}><Icon name="chat" size={14} /><span>定位消息</span></button>
          </article>
        })}
      </div>
    </section>)}
  </div>
}
