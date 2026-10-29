import AutoLoadMore from './AutoLoadMore'
import MacFileMessage from './MacFileMessage'
import UnifiedChatSearch from './UnifiedChatSearch'
import ChatResults from './ChatResults'
import MacMediaMessage from './MacMediaMessage'
import {QuotePreview, QuoteDialog} from './QuotePreview'
import {defaultVisibility, type MessageVisibility} from '../message-visibility'
import { createPortal } from 'react-dom'
import { useContext, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import Avatar from './Avatar'
import LinkText, {ExternalLink} from './LinkText'
import { groupMessages } from '../message-folds'
import Icon from './Icon'
import type { GroupMember, GroupMemberPage, MessagePage, Message } from '../types'
import { StickerParsingContext } from '../sticker-parsing'

function formatStamp(timestamp: number) {
  return new Date(timestamp * 1000).toLocaleString('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function shouldShowTime(previous: Message | undefined, current: Message) {
  if (!previous) return true
  return current.timestamp - previous.timestamp >= 300
}

async function latestSolitaireMessages(conversation: string, libraryId: string | undefined, keys: string[], cancelled: () => boolean) {
  const latest: Record<string, Message> = {}
  const pending = new Set(keys)
  let offset = 0
  for (let batch = 0; pending.size && batch < 100; batch++) {
    const result = await window.noteford.messages({conversation, category: 'solitaire', limit: 100, filter_offset: offset})
    if (cancelled()) return latest
    if ((libraryId && result.library_id !== libraryId) || result.conversation.username !== conversation) throw new Error('会话已变化，请重新打开接龙')
    for (const item of [...result.messages].reverse()) {
      if (item.solitaire_key && pending.delete(item.solitaire_key)) latest[item.solitaire_key] = item
    }
    if (result.next_offset == null) break
    if (result.next_offset <= offset) throw new Error('无法继续查找接龙')
    offset = result.next_offset
  }
  return latest
}

function ChatImage({url, thumbnail, loading, expired, onPreview}: {
  url: string; thumbnail: boolean; loading: boolean; expired: boolean; onPreview(url: string): void
}) {
  // Keep the first preview's proportions when its full-resolution file arrives.
  const [ratio, setRatio] = useState<number | null>(null)
  return <button className="image-message" disabled={loading || expired} aria-busy={loading}
    aria-label={expired ? '文件过期或被删除' : thumbnail ? '加载图片' : '查看图片'} type="button"
    style={{'--chat-image-ratio': ratio ?? 1} as CSSProperties}
    onClick={() => onPreview(url)}>
    <img src={url} alt="微信图片" loading="lazy" onLoad={event => {
      const image = event.currentTarget
      if (image.naturalWidth > 0 && image.naturalHeight > 0) {
        setRatio(previous => previous ?? image.naturalWidth / image.naturalHeight)
      }
    }} />
    {thumbnail ? <span className="image-quality-label">{expired ? '文件过期或被删除' : loading ? '加载中…' : '加载'}</span> : null}
  </button>
}

function Announcement({text}: {text: string}) {
  const [expanded, setExpanded] = useState(false)
  const [overflows, setOverflows] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const bodyId = useId()
  useLayoutEffect(() => {
    const body = bodyRef.current
    if (!body) return
    const measure = () => setOverflows(body.scrollHeight > parseFloat(getComputedStyle(body).lineHeight) * 6 + 1)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(body)
    return () => observer.disconnect()
  }, [text])
  return <div className="announcement-card" ref={cardRef}>
    <div className="announcement-label">群公告</div>
    <div className="announcement-title">
      <div id={bodyId} ref={bodyRef} className={`announcement-body${expanded ? '' : ' is-collapsed'}`}>
        <LinkText text={text} />
      </div>
    </div>
    {overflows ? <button type="button" className="announcement-toggle" aria-expanded={expanded} aria-controls={bodyId}
      onClick={() => {
        setExpanded(!expanded)
        if (expanded) requestAnimationFrame(() => cardRef.current?.scrollIntoView({block: 'nearest'}))
      }}>{expanded ? '收起' : '展开全文'}</button> : null}
  </div>
}

function MacSticker({message, onPreview}: {message: Message; onPreview(url: string): void}) {
  const source = message.media?.url || message.card?.thumbnail_url || message.card?.cover_url
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => { setFailed(false); setAttempt(0) }, [source])
  if (!source || failed) return <div className="media-message-content">
    <span>{message.text && message.text !== '[表情]' ? message.text : '表情暂不可用'}</span>
    {source ? <button className="media-retry-button" type="button" onClick={() => { setAttempt(value => value + 1); setFailed(false) }}>重试</button> : null}
  </div>
  return <button type="button" className="sticker-message" aria-label="查看表情" onClick={() => onPreview(source)}>
    <img key={attempt} src={source} alt={message.text || '表情'} loading="lazy" onError={() => setFailed(true)} />
  </button>
}

function RichContent({
  message,
  onPreview,
  onLatest,
  latestSolitaireUid,
  loading = false,
  expired = false,
  onVideoPreview,
}: {
  message: Message
  onPreview(url: string): void
  onLatest?(message: Message): void
  latestSolitaireUid?: string
  loading?: boolean
  expired?: boolean
  onVideoPreview?(url: string): void
}) {

  const parseStickers = useContext(StickerParsingContext)
  const kind = message.type.kind
  const media = message.media

  if (window.noteford.platform === 'darwin' && ['merged_messages', 'message_history'].includes(kind)) {
    return <details className="announcement-card"><summary>{message.title || '聊天记录'}</summary>
      {message.record_items?.length ? <div className="mac-record-items">{message.record_items.map(item =>
        <section className="mac-record-item" key={item.uid}>
          <p className="mac-record-author">{item.sender.name || '未知联系人'}{item.time ? ` · ${item.time}` : ''}</p>
          {['image', 'video'].includes(item.type.kind)
            ? <MacMediaMessage message={item} onPreview={onPreview} onVideoPreview={onVideoPreview} />
            : <RichContent message={item} onPreview={onPreview} onVideoPreview={onVideoPreview} />}
        </section>)}</div>
        : <div className="message-text"><LinkText text={message.text || '聊天记录正文尚未同步。'} /></div>}
    </details>
  }

  if (kind === 'quote') {
    return <div className="quote-current"><LinkText text={message.text || '引用回复'} /></div>
  }

  if (kind === 'emoji' || kind === 'gif') {
    if (!parseStickers) return <div className="message-text">[表情]</div>
    if (window.noteford.platform === 'darwin') return <MacSticker key={message.uid} message={message} onPreview={onPreview} />
    const sticker = message.card?.thumbnail_url || message.card?.cover_url
    if (sticker) {
      return (
        <button
          type="button"
          className="sticker-message"
          onClick={() => onPreview(sticker)}
        >
          <img src={sticker} alt="表情" loading="lazy" />
        </button>
      )
    }
    return <div className="message-text">[表情]</div>
  }

  if (kind === 'transfer') {
    return (
      <div className="payment-card transfer-card">
        <div className="payment-icon"><Icon name="wallet" size={20} /></div>
        <div className="payment-copy">
          <strong>{message.title || '微信转账'}</strong>
          <span><LinkText text={message.text || '微信转账'} /></span>
        </div>
        <div className="payment-foot">微信转账</div>
      </div>
    )
  }

  if (kind === 'red_envelope') {
    return (
      <div className="payment-card red-packet-card">
        <div className="red-packet-icon"><Icon name="gift" size={20} /></div>
        <div className="payment-copy">
          <strong><LinkText text={message.text || '领取红包'} /></strong>
          <span>微信红包</span>
        </div>
        <div className="payment-foot">微信红包</div>
      </div>
    )
  }

  if (kind === 'group_chat_reference') {
    const lines = (message.text || message.title || '群接龙').split('\n').map(line => line.trim()).filter(Boolean)
    if (/^#?接龙$/.test(lines[0] || '')) lines.shift()
    const title = lines[0] || '群接龙'
    const entries = lines.slice(1)
    return (
      <div className="solitaire-shell"><details className="solitaire-card">
        <summary className="solitaire-head">
          <span className="solitaire-heading"><span className="solitaire-label">群接龙</span><strong><LinkText text={title === '群接龙' ? '查看接龙内容' : title} /></strong></span>
          <span className="solitaire-actions">
            <span className="solitaire-toggle"><span className="solitaire-expand">展开</span><span className="solitaire-collapse">收起</span></span>
            {onLatest && message.solitaire_key ? latestSolitaireUid === message.uid
              ? <span className="solitaire-current" onClick={event => { event.preventDefault(); event.stopPropagation() }}>当前最新</span>
              : <button type="button" className="solitaire-latest" onClick={event => { event.preventDefault(); event.stopPropagation(); onLatest(message) }}>跳到最新</button>
              : null}
          </span>
        </summary>
        <div className="solitaire-body">{entries.length ? entries.map((line, index) => <p key={index}><LinkText text={line} /></p>) : <p><LinkText text={title === '群接龙' ? '这条消息尚未包含接龙名单。' : title} /></p>}</div>
      </details></div>
    )
  }

  if (kind === 'group_announcement') {
    return <Announcement key={message.uid} text={message.text || '公告正文尚未同步，请在微信查看。'} />
  }

  if (kind === 'contact_card') {
    return (
      <div className="contact-card-message">
        {message.card?.avatar_url ? (
          <img
            className="contact-card-avatar"
            src={message.card.avatar_url}
            alt=""
            loading="lazy"
          />
        ) : (
          <div className="contact-card-avatar placeholder">人</div>
        )}
        <div className="contact-card-copy">
          <strong>{message.title || '联系人名片'}</strong>
          <span>个人名片</span>
        </div>
      </div>
    )
  }

  if (kind === 'location') {
    return (
      <div className="location-card">
        <div className="location-copy">
          <strong>{message.title || '位置'}</strong>
          <span><LinkText text={message.text || '位置消息'} /></span>
        </div>
        <div className="location-map-placeholder"><Icon name="location" size={28} /></div>
      </div>
    )
  }

  if (kind === 'image') {
    if (media?.url && (media.status === 'ready' || media.status === 'ready_thumbnail')) {
      return (
        <ChatImage key={message.uid} url={media.url} thumbnail={media.status === 'ready_thumbnail'}
          loading={loading} expired={expired} onPreview={onPreview} />
      )
    }
    return (
      <div className="media-card unavailable-media">
        <Icon name="image" />
        <div>
          <strong>图片</strong>
          <span>
            {media?.status?.startsWith('encrypted_')
              ? '图片已定位，暂未解密'
              : media?.status === 'wxgf_container'
                ? '图片原始容器暂不支持预览'
              : '本地图片不可用'}
          </span>
        </div>
      </div>
    )
  }

  if (kind === 'voice') {
    if (media?.url && media.status === 'ready_audio') {
      return (
        <audio
          className="voice-player"
          controls
          preload="metadata"
          src={media.url}
        />
      )
    }
    return (
      <div className="voice-card">
        <Icon name="voice" />
        <span>语音消息</span>
      </div>
    )
  }

  if (kind === 'video') {
    if (media?.url && media.status === 'ready' && onVideoPreview) {
      return <button type="button" className="media-video-cover" aria-label="播放视频" onClick={() => onVideoPreview(media.url!)}>
        <video src={media.url} muted preload="metadata" tabIndex={-1} aria-hidden="true" />
        <span className="video-play-mark"><Icon name="play" size={20} /></span>
      </button>
    }
    if (media?.url && media.status === 'ready') {
      return (
        <video
          className="video-message"
          controls
          preload="metadata"
          src={media.url}
        />
      )
    }
    if (media?.url && media.status === 'thumbnail_only') {
      return (
        <button
          className="video-thumbnail"
          disabled={loading || expired}
          aria-busy={loading}
          aria-label={expired ? '文件过期或被删除' : '加载视频'}
          type="button"
          onClick={() => onPreview(media.url!)}
        >
          <img src={media.url} alt="视频缩略图" loading="lazy" />
          {!expired && <span className="video-play-mark"><Icon name="play" size={20} /></span>}
          <span className="video-thumb-caption">{expired ? '文件过期或被删除' : loading ? '加载中…' : '加载'}</span>
        </button>
      )
    }
    return (
      <div className="media-card unavailable-media">
        <Icon name="video" />
        <div>
          <strong>视频</strong>
          <span>本地视频不可用</span>
        </div>
      </div>
    )
  }

  if (kind === 'file') {
    return (
      <div className="media-card file-card">
        <Icon name="file" />
        <div>
          <strong>{message.title || '文件'}</strong>
          <span>{media?.status === 'ready' ? '文件已保存在本机' : '本地文件记录'}</span>
          {window.noteford.platform === 'darwin' && media?.status === 'ready' && media.url
            ? <button type="button" onClick={() => void window.noteford.revealMedia?.({url: media.url!})}>在访达中查看</button> : null}
        </div>
      </div>
    )
  }

  if (
    kind === 'channel_video'
    || kind === 'channel_forward'
    || kind === 'channel_show_card'
  ) {
    const cover = message.card?.cover_url || message.card?.thumbnail_url
    return (
      <div className="channel-card">
        {cover ? (
          <img
            className="channel-cover"
            src={cover}
            alt="视频号封面"
            loading="lazy"
          />
        ) : null}
        <div className="channel-meta">
          {message.card?.avatar_url ? (
            <img
              className="channel-avatar"
              src={message.card.avatar_url}
              alt=""
              loading="lazy"
            />
          ) : null}
          <div>
            <div className="channel-label">视频号</div>
            <div className="channel-title">{message.title || '视频号内容'}</div>
          </div>
        </div>
        <div className="channel-desc"><LinkText text={message.text || '视频号分享'} /></div>
        <div className="channel-foot">视频号</div>
      </div>
    )
  }

  if (['link', 'mini_program', 'merged_messages'].includes(kind)) {
    const cover = message.card?.cover_url || message.card?.thumbnail_url
    return (
      <div className={'rich-card ' + (cover ? 'with-cover' : '')}>
        <div className="rich-card-copy">
          <div className="rich-card-title">
            {window.noteford.platform === 'darwin' && kind === 'mini_program' && message.card?.avatar_url ? <img className="channel-avatar" src={message.card.avatar_url} alt="小程序图标" loading="lazy" /> : null}
            {message.url ? <ExternalLink url={message.url}>{message.title || message.text || message.url}</ExternalLink> : <LinkText text={message.title || message.text || '微信内容'} />}
          </div>
          {message.text && message.text !== message.title ? (
            window.noteford.platform === 'darwin' && message.text.includes('\n\n')
              ? <details><summary>展开全部内容</summary><div className="message-text"><LinkText text={message.text} /></div></details>
              : <div className="rich-card-desc"><LinkText text={message.text} /></div>
          ) : null}
          <div className="rich-card-foot">
            <Icon name="link" size={14} />
            {window.noteford.platform === 'darwin' ? ({link: '链接', mini_program: '小程序', merged_messages: '聊天记录'} as Record<string, string>)[kind] : kind.replaceAll('_', ' ')}
          </div>
        </div>
        {cover ? (
          <img
            className="rich-card-cover"
            src={cover}
            alt=""
            loading="lazy"
          />
        ) : null}
      </div>
    )
  }

  if (window.noteford.platform === 'darwin' && kind === 'app') {
    return <div className="message-text"><LinkText text={message.text || message.title || '[暂不支持的消息]'} /><small>此消息格式尚未完整支持</small></div>
  }
  return <div className="message-text"><LinkText text={message.text || '[' + kind + ']'} /></div>
}

const mediaDownloadErrors: Record<string, string> = {
  local_original_unavailable: '本机尚未找到原文件；微信取得文件后可在此重新加载。',
  session_unavailable: '暂未读取到下载凭据，请重试；微信已登录时也可能出现此问题。',
  account_mismatch: '请在电脑版微信登录这条消息所属的账号，再重试。',
  download_busy: '另一个媒体正在下载，请稍后重试。',
  network_unavailable: '网络连接未成功，已保留现有预览，请重试。',
  download_timeout: '下载超时，已保留现有预览，请重试。',
  cdn_rejected: '服务器拒绝了下载请求，原因尚未确认。',
  cdn_not_found: '当前下载地址未返回文件，已暂停自动重试。',
  cdn_expired: '云端文件已过期或被删除。',
  missing_download_reference: '这条消息缺少独立下载所需的信息。',
  integrity_failed: '文件校验未通过，未替换现有预览，请重试。',
  invalid_response: '下载数据不完整或格式异常，未替换现有预览，请重试。',
  too_large: '文件超过单次下载的 128 MB 上限。',
  insufficient_space: '磁盘剩余空间不足，请释放空间后重试。',
  storage_unavailable: '无法保存下载文件，请检查磁盘后重试。',
  unsupported_download_format: '文件已取得，但当前格式暂不能预览。',
}

function MediaMessage({message, libraryId, onPreview, onVideoPreview}: {message: Message; libraryId?: string; onPreview(url: string): void; onVideoPreview?(url: string): void}) {
  const Component = window.noteford.platform === 'darwin' ? MacMediaMessage : LegacyMediaMessage
  return <Component key={`${libraryId}:${message.uid}`} message={message} libraryId={libraryId} onPreview={onPreview} onVideoPreview={onVideoPreview} />
}

function LegacyMediaMessage({message, libraryId, onPreview, onVideoPreview}: {message: Message; libraryId?: string; onPreview(url: string): void; onVideoPreview?(url: string): void}) {
  const [media, setMedia] = useState(message.media)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [playbackError, setPlaybackError] = useState(false)
  const alive = useRef(true)
  const pending = useRef(false)
  const generation = useRef(0)
  useEffect(() => { generation.current++; pending.current = false; setBusy(false); setNotice('') }, [libraryId, message.uid])
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => { setMedia(message.media); setPlaybackError(false) }, [message.media])
  const status = media?.status
  const expired = status !== 'ready' && media?.download_error === 'cdn_expired'
  async function recheck() {
    if (!libraryId || pending.current || expired) return
    pending.current = true
    const requestGeneration = generation.current
    setBusy(true); setNotice('')
    try {
      const result = await window.noteford.recheckMedia({library_id: libraryId, uid: message.uid})
      if (!alive.current || requestGeneration !== generation.current) return
      setPlaybackError(false)
      setMedia(result?.url ? {...result, url: `${result.url}${result.url.includes('?') ? '&' : '?'}v=${Date.now()}`} : result)
      setNotice(result?.download_error ? (mediaDownloadErrors[result.download_error] || '获取失败，请稍后重试。') : result?.status === 'ready' ? '完整文件已就绪。' : '暂未获得可预览的完整文件。')
    } catch {
      if (alive.current && requestGeneration === generation.current) setNotice('获取失败，请检查本地服务后重试。')
    } finally {
      if (requestGeneration === generation.current) {
        pending.current = false
        if (alive.current) setBusy(false)
      }
    }
  }
  const hasPreview = !!media?.url && ['ready', 'ready_thumbnail', 'thumbnail_only'].includes(status || '')
  const visibleNotice = expired ? '' : notice || (media?.download_error ? mediaDownloadErrors[media.download_error] || '获取失败，请稍后重试。' : '')
  return <div className="media-message-content">
    {hasPreview ? <div onErrorCapture={() => setPlaybackError(true)}><RichContent message={{...message, media}} loading={busy} expired={expired} onVideoPreview={onVideoPreview} onPreview={status === 'ready' ? onPreview : () => void recheck()} /></div>
      : <button className="media-card media-load-card" type="button" disabled={busy || !libraryId || expired} aria-busy={busy} aria-label={expired ? '文件过期或被删除' : message.type.kind === 'image' ? '加载图片' : '加载视频'} onClick={() => void recheck()}>
        <Icon name={message.type.kind === 'image' ? 'image' : 'video'} /><span>{expired ? '文件过期或被删除' : busy ? '加载中…' : '加载'}</span>
      </button>}
    {playbackError && libraryId && !expired ? <button className="media-retry-button" type="button" disabled={busy} onClick={() => void recheck()}>{busy ? '加载中…' : '重新加载'}</button> : null}
    {visibleNotice && (status !== 'ready' || playbackError) ? <p className="media-load-error" role="status">{visibleNotice}</p> : null}
  </div>
}

export function MessageRow({
  message,
  isGroup,
  showTime = true,
  onPreview,
  libraryId,
  onLatest,
  latestSolitaireUid,
  onQuote,
  highlighted = false,
}: {
  message: Message
  isGroup: boolean
  showTime?: boolean
  onLatest?(message: Message): void
  latestSolitaireUid?: string
  onQuote?(message: Message): void
  highlighted?: boolean
  libraryId?: string
  onPreview(url: string): void
}) {
  const system = ['system', 'pat', 'revoke'].includes(message.type.kind)
  const bareContent = [
    'image',
    'video',
    'emoji',
    'gif',
    'transfer',
    'red_envelope',
    'group_chat_reference',
    'group_announcement',
    'contact_card',
    'location',
  ].includes(message.type.kind)

  return (
    <div className={`message-row-block${highlighted ? ' quote-highlight' : ''}`} data-message-uid={message.uid} tabIndex={highlighted ? -1 : undefined}>
      {showTime ? (
        <div className="time-divider">{formatStamp(message.timestamp)}</div>
      ) : null}

      {system ? (
        <div className="system-message">
          <LinkText text={message.text || '[系统消息]'} />
        </div>
      ) : (
        <div
          className={
            'message-row '
            + (message.sender.from_me ? 'from-me' : 'from-other')
          }
        >
          {!message.sender.from_me ? (
            <Avatar
              name={message.sender.name}
              id={message.sender.username}
              src={message.sender.avatar_url}
              size={36}
            />
          ) : null}

          <div className="message-stack">
            {!message.sender.from_me && isGroup ? (
              <div className="sender-name">
                {message.sender.name
                  || message.sender.username
                  || '未知成员'}
              </div>
            ) : null}

            <div
              className={
                'message-bubble '
                + (message.sender.from_me ? 'mine' : 'theirs')
                + (bareContent ? ' media-bubble' : '')
              }
            >
              {window.noteford.platform === 'darwin' && message.type.kind === 'file' ? <MacFileMessage key={`${libraryId}:${message.uid}`} message={message} libraryId={libraryId}/> : ['image', 'video'].includes(message.type.kind) ? <MediaMessage key={`${libraryId}:${message.uid}`} message={message} libraryId={libraryId} onPreview={onPreview} /> : <RichContent
                message={message}
                onLatest={onLatest}
                latestSolitaireUid={latestSolitaireUid}
                onPreview={onPreview}
              />}
            </div>
            {message.quote ? <QuotePreview message={message} onOpen={onQuote} /> : null}
          </div>

          {message.sender.from_me ? (
            <Avatar
              name={message.sender.name || '我'}
              id={message.sender.username || 'self'}
              src={message.sender.avatar_url}
              size={36}
            />
          ) : null}
        </div>
      )}
    </div>
  )
}

export default function ChatView({
  parseStickers = false,
  messageVisibility = defaultVisibility,
  page,
  loading,
  loadingOlder,
  onLoadOlder,
  libraryId,
  toolbarHost,
  focusMessageUid,
  onFocusHandled,
}: {
  parseStickers?: boolean
  messageVisibility?: MessageVisibility
  page: MessagePage | null
  loading: boolean
  loadingOlder: boolean
  onLoadOlder(): Promise<void> | void
  libraryId?: string
  toolbarHost?: HTMLElement | null
  focusMessageUid?: string | null
  onFocusHandled?(): void
}) {
  const endRef = useRef<HTMLDivElement | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const followBottom = useRef(true)
  const lastTail = useRef<string | undefined>(undefined)
  const memberRequest = useRef(0)
  const lastConversation = useRef<string | null>(null)
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [videoPreviewUrl, setVideoPreviewUrl] = useState<string | null>(null)
  const [foldTime, setFoldTime] = useState(() => Date.now() / 1000)
  useEffect(() => {
    const timer = window.setInterval(() => setFoldTime(Date.now() / 1000), 60_000)
    return () => window.clearInterval(timer)
  }, [])
  const [jumpPage, setJumpPage] = useState<MessagePage | null>(null)
  const [jumpNotice, setJumpNotice] = useState('')
  const [quoteMessage, setQuoteMessage] = useState<Message | null>(null)
  const [quoteHistory, setQuoteHistory] = useState<Message[]>([])
  const [quoteLoading, setQuoteLoading] = useState(false)
  const quoteRequest = useRef(0)
  const [highlightUid, setHighlightUid] = useState<string | null>(null)
  const [contextBusy, setContextBusy] = useState(false)
  const [contextHasOlder, setContextHasOlder] = useState(true)
  const [contextHasNewer, setContextHasNewer] = useState(true)
  const jumpRequest = useRef(0)
  const jumpOrigin = useRef(0)
  const jumpOriginFollow = useRef(false)
  const quoteOriginUid = useRef<string | null>(null)
  const [category, setCategory] = useState('all')
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [selectedMembers, setSelectedMembers] = useState<GroupMember[]>([])
  const [fromTime, setFromTime] = useState<number | undefined>()
  const [toTime, setToTime] = useState<number | undefined>()
  const [toNow, setToNow] = useState(false)
  const invalidTime = fromTime != null && toTime != null && fromTime >= toTime
  const [memberPage, setMemberPage] = useState<GroupMemberPage | null>(null)
  const [memberLoading, setMemberLoading] = useState(false)
  const [memberError, setMemberError] = useState('')
  const [filteredPage, setFilteredPage] = useState<MessagePage | null>(null)
  const [filterLoading, setFilterLoading] = useState(false)
  const [filterOlder, setFilterOlder] = useState(false)
  const [filterError, setFilterError] = useState('')
  const filterRequest = useRef(0)
  const conversation = page?.conversation.username

  const activeFilter = category !== 'all' || Boolean(debouncedSearch.trim() || selectedMembers.length || fromTime != null || toTime != null)

  useEffect(() => {
    jumpRequest.current++
    setJumpPage(null)
    setContextHasOlder(true); setContextHasNewer(true)
    setVideoPreviewUrl(null)
    setPreviewUrl(null)
    setJumpNotice('')
    setQuoteMessage(null)
    setQuoteHistory([])
    setQuoteLoading(false)
    quoteRequest.current++
    setHighlightUid(null)
    setContextBusy(false)
    return () => { jumpRequest.current++; quoteRequest.current++ }
  }, [conversation, libraryId, activeFilter, category, debouncedSearch, selectedMembers, fromTime, toTime])

  function closeQuote() {
    quoteRequest.current++
    jumpRequest.current++
    setContextBusy(false)
    setQuoteMessage(null)
    setQuoteHistory([])
    setQuoteLoading(false)
    setJumpNotice('')
  }

  async function openQuote(message: Message, nested = false) {
    jumpRequest.current++
    setContextBusy(false)
    const request = ++quoteRequest.current
    setQuoteMessage(message)
    setJumpNotice('')
    if (!nested) {
      setQuoteHistory([])
      if (!jumpPage) quoteOriginUid.current = message.uid
    }
    setQuoteLoading(false)
    if (!conversation || !message.quote?.server_id || message.quote.state) return
    setQuoteLoading(true)
    try {
      const result = await window.noteford.messages({conversation, limit: 1, around_uid: message.uid})
      if (request !== quoteRequest.current) return
      if ((libraryId && result.library_id !== libraryId) || result.conversation.username !== conversation) throw new Error('quote_changed')
      const resolved = result.messages.find(item => item.uid === message.uid)
      if (resolved) setQuoteMessage(resolved)
    } catch { if (request === quoteRequest.current) setJumpNotice('读取引用失败，请重试') }
    finally { if (request === quoteRequest.current) setQuoteLoading(false) }
  }

  async function jumpToQuoted(message: Message) {
    if (message.quote?.target) await jumpToMessage(message.quote.target)
  }

  async function jumpToTime(timestamp: number): Promise<boolean> {
    if (!conversation || timestamp > Date.now() / 1000) return false
    const request = ++jumpRequest.current
    setContextBusy(true)
    setJumpNotice('')
    try {
      const result = await window.noteford.messages({conversation, limit: 60, at_time: timestamp})
      if (request !== jumpRequest.current) return false
      if ((libraryId && result.library_id !== libraryId) || result.conversation.username !== conversation) return false
      const target = result.messages.find(message => message.timestamp >= timestamp)
      if (!target) { setJumpNotice('该时间之后没有已同步的消息'); return false }
      return Boolean(await jumpToMessage(target))
    } catch {
      if (request === jumpRequest.current) setJumpNotice('无法定位此时间，请重试')
      return false
    } finally { if (request === jumpRequest.current) setContextBusy(false) }
  }

  async function jumpToMessage(target: Pick<Message, 'uid'>) {
    if (!conversation) return
    const request = ++jumpRequest.current
    if (!jumpPage) {
      jumpOrigin.current = scrollRef.current?.scrollTop || 0
      jumpOriginFollow.current = followBottom.current
    }
    setContextBusy(true)
    setJumpNotice('')
    try {
      const context = await window.noteford.messages({conversation, limit: 60, around_uid: target.uid})
      if (request !== jumpRequest.current) return
      if ((libraryId && context.library_id !== libraryId) || context.conversation.username !== conversation || !context.messages.some(item => item.uid === target.uid)) throw new Error('quote_context_changed')
      followBottom.current = false
      setContextHasOlder(true); setContextHasNewer(true)
      setJumpPage(context)
      setHighlightUid(target.uid)
      setQuoteMessage(null)
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        if (request !== jumpRequest.current) return
        const row = [...(scrollRef.current?.querySelectorAll<HTMLElement>('[data-message-uid]') || [])].find(node => node.dataset.messageUid === target.uid)
        row?.closest('details')?.setAttribute('open', '')
        row?.scrollIntoView({block: 'center'})
        row?.focus({preventScroll: true})
      }))
      return true
    } catch {
      if (request === jumpRequest.current) setJumpNotice('无法定位原消息，请重试')
      return false
    } finally { if (request === jumpRequest.current) setContextBusy(false) }
  }

  async function loadContext(direction: 'before' | 'after') {
    if (!jumpPage || !conversation || contextBusy) return
    const request = ++jumpRequest.current
    const anchor = direction === 'before' ? jumpPage.messages[0] : jumpPage.messages.at(-1)
    if (!anchor) return
    setContextBusy(true)
    try {
      const result = await window.noteford.messages({conversation, limit: 60, ...(direction === 'before' ? {before_uid: anchor.uid} : {after_uid: anchor.uid})})
      if (request !== jumpRequest.current) return
      if ((libraryId && result.library_id !== libraryId) || result.conversation.username !== conversation) throw new Error('context_changed')
      setJumpPage(current => current ? {...current, messages: direction === 'before' ? [...result.messages, ...current.messages] : [...current.messages, ...result.messages]} : current)
      if (direction === 'before' && !result.messages.length) setContextHasOlder(false)
      if (direction === 'after' && !result.messages.length) setContextHasNewer(false)
      setJumpNotice(result.messages.length ? '' : direction === 'before' ? '没有更早的消息' : '已到最新消息')
    } catch { if (request === jumpRequest.current) setJumpNotice('读取失败，请重试') }
    finally { if (request === jumpRequest.current) setContextBusy(false) }
  }

  function returnToOrigin() {
    jumpRequest.current++
    followBottom.current = false
    setContextBusy(false)
    setHighlightUid(null)
    setJumpNotice('')
    setJumpPage(null)
    window.requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({top: jumpOrigin.current})
      const row = [...(scrollRef.current?.querySelectorAll<HTMLElement>('[data-message-uid]') || [])].find(node => node.dataset.messageUid === quoteOriginUid.current)
      row?.querySelector<HTMLButtonElement>('.quote-preview, .result-file-open, .result-locate')?.focus({preventScroll: true})
      followBottom.current = jumpOriginFollow.current
    })
  }

  async function jumpToLatest(message: Message) {
    if (!conversation || !message.solitaire_key) return
    const request = ++jumpRequest.current
    if (!jumpPage) {
      jumpOrigin.current = scrollRef.current?.scrollTop || 0
      jumpOriginFollow.current = followBottom.current
      quoteOriginUid.current = null
    }
    setJumpNotice('正在查找最新接龙…')
    try {
      const latest = (await latestSolitaireMessages(conversation, libraryId, [message.solitaire_key], () => request !== jumpRequest.current))[message.solitaire_key]
      if (request !== jumpRequest.current) return
      if (!latest) throw new Error('未找到可确认的同一接龙记录')
      const context = await window.noteford.messages({conversation, limit: 100, around_uid: latest.uid})
      if (request !== jumpRequest.current) return
      if ((libraryId && context.library_id !== libraryId) || context.conversation.username !== conversation) throw new Error('会话已变化，请重新打开接龙')
      const target = latest
      setJumpPage(context.messages.some(item => item.uid === target.uid) ? context : {...context, messages: [target], next_before: null})
      setContextHasOlder(true); setContextHasNewer(true)
      setJumpNotice('')
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        if (request !== jumpRequest.current) return
        const row = [...(scrollRef.current?.querySelectorAll<HTMLElement>('[data-message-uid]') || [])].find(node => node.dataset.messageUid === target.uid)
        const details = row?.querySelector('details')
        if (details) details.open = true
        row?.scrollIntoView({block: 'center'})
        row?.querySelector<HTMLElement>('summary')?.focus({preventScroll: true})
      }))
    } catch (cause) {
      if (request === jumpRequest.current) setJumpNotice(cause instanceof Error ? cause.message : '查找失败，请重试')
    }
  }

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search), 260)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    setCategory('all')
    setSearch('')
    setDebouncedSearch('')
    setSelectedMembers([]); setFromTime(undefined); setToTime(undefined); setToNow(false)
    setMemberPage(null)
    setMemberLoading(false)
    setMemberError('')
    memberRequest.current += 1
    setFilteredPage(null)
    setFilterError('')
  }, [conversation, libraryId])

  useEffect(() => {
    ++filterRequest.current
    setFilterError(''); setFilterOlder(false); setFilterLoading(false)
    if (!conversation || !activeFilter || invalidTime) {
      setFilteredPage(null)
      return
    }
    let cancelled = false
    setFilterLoading(true)
    setFilteredPage(null)
    void window.noteford.messages({
      conversation, limit: 100,
      category: category === 'all' ? undefined : category,
      query: debouncedSearch.trim() || undefined,
      sender: selectedMembers.map(member => member.username).join(',') || undefined,
      from_day: fromTime, to_day: toTime,
    }).then(result => {
      if (cancelled) return
      setFilteredPage(result)
      setFilterError('')
      scrollRef.current?.scrollTo({ top: 0 })
    }).catch(cause => {
      if (!cancelled) setFilterError(cause instanceof Error ? cause.message : String(cause))
    }).finally(() => { if (!cancelled) setFilterLoading(false) })
    return () => { cancelled = true; ++filterRequest.current }
  }, [libraryId, conversation, activeFilter, category, debouncedSearch, selectedMembers, fromTime, toTime])

  const hasMemberPage = memberPage !== null
  useEffect(() => {
    if (window.noteford.platform !== 'darwin' || !conversation || !hasMemberPage) return
    let cancelled = false
    let inFlight = false
    const refresh = async () => {
      if (inFlight || document.hidden) return
      inFlight = true
      try {
        const result = await window.noteford.groupMembers({conversation})
        if (cancelled) return
        setMemberPage(result)
        setSelectedMembers(current => {
          const byId = new Map(result.members.map(member => [member.username, member]))
          const next = current.map(member => byId.get(member.username) || member)
          return JSON.stringify(current) === JSON.stringify(next) ? current : next
        })
      } catch { /* Keep the last complete member list and retry. */ }
      finally { inFlight = false }
    }
    const timer = window.setInterval(() => void refresh(), 15000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [libraryId, conversation, hasMemberPage])

  async function loadMembers() {
    if (!conversation || memberPage || memberLoading) return
    const request = ++memberRequest.current
    setMemberLoading(true)
    setMemberError('')
    try {
      const result = await window.noteford.groupMembers({ conversation })
      if (request === memberRequest.current) setMemberPage(result)
    } catch (cause) {
      if (request === memberRequest.current) {
        setMemberError(cause instanceof Error ? cause.message : String(cause))
      }
    } finally {
      if (request === memberRequest.current) setMemberLoading(false)
    }
  }

  async function loadOlderFiltered() {
    if (!conversation || invalidTime || filteredPage?.next_offset == null || filterOlder) return
    const request = filterRequest.current
    setFilterOlder(true); setFilterError('')
    try {
      const older = await window.noteford.messages({
        conversation, limit: 100, filter_offset: filteredPage.next_offset,
        category: category === 'all' ? undefined : category,
        query: debouncedSearch.trim() || undefined,
        sender: selectedMembers.map(member => member.username).join(',') || undefined,
      from_day: fromTime, to_day: toTime,
      })
      if (request !== filterRequest.current) return
      setFilteredPage(current => current ? {
        ...current,
        messages: [...older.messages, ...current.messages],
        next_offset: older.next_offset,
      } : older)
    } catch (cause) {
      if (request === filterRequest.current) setFilterError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      if (request === filterRequest.current) setFilterOlder(false)
    }
  }

  useLayoutEffect(() => {
    if (!page || activeFilter || jumpPage) return
    const changedConversation = lastConversation.current !== page.conversation.username
    const tail = page.messages.at(-1)?.uid
    if (changedConversation || (tail !== lastTail.current && followBottom.current)) {
      const scroll = scrollRef.current
      if (scroll) scroll.scrollTop = scroll.scrollHeight
      followBottom.current = true
    }
    lastConversation.current = page.conversation.username
    lastTail.current = tail
  }, [page, activeFilter, jumpPage])

  useEffect(() => {
    if (!focusMessageUid || !page?.messages.some(message => message.uid === focusMessageUid)) return
    // Apply after conversation/filter resets, then center the actual source row.
    let cancelled = false
    const timer = window.setTimeout(() => {
      followBottom.current = false
      setJumpPage(page)
      setHighlightUid(focusMessageUid)
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        if (cancelled) return
        const row = [...(scrollRef.current?.querySelectorAll<HTMLElement>('[data-message-uid]') || [])].find(node => node.dataset.messageUid === focusMessageUid)
        row?.closest('details')?.setAttribute('open', '')
        row?.scrollIntoView({block: 'center'})
        row?.focus({preventScroll: true})
        onFocusHandled?.()
      }))
    }, 0)
    return () => {cancelled = true; window.clearTimeout(timer)}
  }, [focusMessageUid, page, activeFilter, category, debouncedSearch, selectedMembers, fromTime, toTime])

  useEffect(() => {
    if (!previewUrl) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPreviewUrl(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [previewUrl])

  const [solitaireLatest, setSolitaireLatest] = useState<{scope: string, messages: Record<string, Message>}>({scope: '', messages: {}})
  const solitaireScope = JSON.stringify([libraryId, conversation])
  useEffect(() => {
    let cancelled = false
    const displayed = jumpPage || (activeFilter ? filteredPage : page)
    const keys = [...new Set((displayed?.messages || []).map(message => message.solitaire_key).filter((key): key is string => Boolean(key)))]
    if (!conversation || !keys.length) return
    void latestSolitaireMessages(conversation, libraryId, keys, () => cancelled)
      .then(messages => { if (!cancelled) setSolitaireLatest({scope: solitaireScope, messages}) })
      .catch(() => { if (!cancelled) setSolitaireLatest({scope: solitaireScope, messages: {}}) })
    return () => { cancelled = true }
  }, [page, jumpPage, filteredPage, activeFilter, conversation, libraryId, solitaireScope])

  const shownPage = jumpPage || (activeFilter ? filteredPage : page)
  const categoryResults = category !== 'all' && !jumpPage
  const historyScope = JSON.stringify([libraryId, conversation, Boolean(jumpPage), category, debouncedSearch,
    selectedMembers.map(member => member.username), fromTime, toTime])
  const historyRequest = useRef<object | null>(null)
  const historyAnchor = useRef<{scope: string; head?: string; uid?: string; offset: number; height: number; top: number} | null>(null)
  const lastScrollTop = useRef(0)
  const historyBusy = loading || filterLoading || (jumpPage ? contextBusy : activeFilter ? filterOlder : loadingOlder)
  const hasOlder = !categoryResults && (jumpPage ? contextHasOlder : activeFilter ? shownPage?.next_offset != null : Boolean(page?.next_before))

  useLayoutEffect(() => {
    historyRequest.current = null
    historyAnchor.current = null
    lastScrollTop.current = scrollRef.current?.scrollTop || 0
  }, [historyScope])

  useLayoutEffect(() => {
    const scroll = scrollRef.current
    const anchor = historyAnchor.current
    if (!scroll || !anchor || anchor.scope !== historyScope || anchor.head === shownPage?.messages[0]?.uid) return
    const row = [...scroll.querySelectorAll<HTMLElement>('[data-message-uid]')].find(node => node.dataset.messageUid === anchor.uid)
    // Compare the visible row position, so native scroll anchoring isn't applied twice.
    scroll.scrollTop = row
      ? scroll.scrollTop + row.getBoundingClientRect().top - scroll.getBoundingClientRect().top - anchor.offset
      : anchor.top + scroll.scrollHeight - anchor.height
    lastScrollTop.current = scroll.scrollTop
    historyAnchor.current = null
  }, [shownPage, historyScope])

  function loadEarlierAtTop() {
    const scroll = scrollRef.current
    if (!scroll || scroll.scrollTop > 160 || !hasOlder || historyBusy || historyRequest.current) return
    const top = scroll.getBoundingClientRect().top
    const row = [...scroll.querySelectorAll<HTMLElement>('[data-message-uid]')]
      .find(node => node.getBoundingClientRect().bottom > top && node.getClientRects().length > 0)
    const anchor = {scope: historyScope, head: shownPage?.messages[0]?.uid, uid: row?.dataset.messageUid,
      offset: row ? row.getBoundingClientRect().top - top : 0, height: scroll.scrollHeight, top: scroll.scrollTop}
    historyAnchor.current = anchor
    const request = {}
    historyRequest.current = request
    followBottom.current = false
    const load = jumpPage ? loadContext('before') : activeFilter ? loadOlderFiltered() : onLoadOlder()
    void Promise.resolve(load).finally(() => {
      // Keep the guard through React's commit and the resulting scroll event.
      window.requestAnimationFrame(() => {
        if (historyRequest.current === request) historyRequest.current = null
        if (historyAnchor.current === anchor) historyAnchor.current = null
      })
    })
  }

  if (!page && loading) {
    return (
      <main className="chat-main chat-content-only empty-chat">
        <div className="empty-mark">正在读取消息…</div>
      </main>
    )
  }

  if (!page) {
    return <main className="chat-main chat-content-only" />
  }

  const messages = shownPage?.messages || []

  function clearFilters() {
    setCategory('all'); setSelectedMembers([]); setFromTime(undefined); setToTime(undefined); setToNow(false)
    setSearch(''); setDebouncedSearch('')
  }

  const toolbar = <div className="chat-tools unified-tools">
    <UnifiedChatSearch key={`${libraryId}:${conversation}`} search={search} onSearch={setSearch}
      category={category} onCategory={setCategory} members={selectedMembers} onMembers={setSelectedMembers}
      from={fromTime} to={toTime} onFrom={setFromTime} onTo={value => {setToTime(value);setToNow(false)}}
      toNow={toNow} onNow={() => {setToTime(Math.floor(Date.now()/1000));setToNow(true)}}
      onLocateTime={jumpToTime} onRange={() => {if (jumpPage || contextBusy) returnToOrigin()}}
      isGroup={page.conversation.is_group} memberPage={memberPage} memberLoading={memberLoading}
      memberError={memberError} onOpen={() => void loadMembers()} onClear={clearFilters}/>

  </div>

  return (
    <StickerParsingContext value={parseStickers}><main className={"chat-main chat-content-only" + (toolbarHost ? " has-shared-toolbar" : "")}>
      {toolbarHost ? createPortal(toolbar, toolbarHost) : toolbar}
      <div className={"messages-scroll" + (categoryResults ? " results-scroll" : "")} role="region" aria-label="聊天消息" tabIndex={0} data-highlight-uid={highlightUid || undefined} ref={scrollRef} onScroll={event => {
        const scroll = event.currentTarget
        followBottom.current = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight <= 64
        const movingUp = scroll.scrollTop < lastScrollTop.current
        lastScrollTop.current = scroll.scrollTop
        if (movingUp) loadEarlierAtTop()
      }} onWheel={event => { if (event.deltaY < 0) loadEarlierAtTop() }}>
        {jumpPage ? <button className="solitaire-return" onClick={returnToOrigin}>返回原会话位置</button> : null}

        {jumpNotice && !quoteMessage ? <div role="status">{jumpNotice}</div> : null}
        {filterError ? <div className="chat-filter-state">{filterError}</div> : null}
        {filterLoading ? <div className="chat-filter-state">正在筛选…</div> : null}
        {activeFilter && !invalidTime && !jumpPage && !filterLoading && !filterError && messages.length === 0 ? <div className="chat-filter-state" role="status"><strong>没有匹配的消息</strong><button type="button" onClick={clearFilters}>返回全部消息</button></div> : null}
        {!activeFilter && messages.length === 0 ? (
          <div className="selective-empty quiet">
            <div className="selective-empty-title">
              {page.selection.state === 'following'
                ? '暂无本地消息'
                : '此会话尚未关注'}
            </div>
            <div className="selective-empty-copy">
              {page.selection.state === 'following'
                ? '后续新消息会按需增量更新。'
                : '需要持续读取时，请在通讯录中关注这个联系人或群。'}
            </div>
          </div>
        ) : null}

        {!categoryResults && historyBusy && !loading && !filterLoading ? (
          <div className="load-older-wrap" role="status">正在加载更早消息…</div>
        ) : null}

        {categoryResults ? <ChatResults messages={messages} category={category} locating={contextBusy}
          onLocate={message => { quoteOriginUid.current = message.uid; void jumpToMessage(message) }}
          renderMedia={message => <MediaMessage message={message} libraryId={libraryId} onPreview={setPreviewUrl} onVideoPreview={setVideoPreviewUrl} />}
        /> : groupMessages(messages, !activeFilter || !!jumpPage, foldTime, {foldStickers: !parseStickers, visibility: messageVisibility, revealUid: highlightUid}).map((group, index, groups) => {
          const message = group.messages[0]
          const previous = index > 0 ? groups[index - 1].messages.at(-1) : undefined
          if (group.label) return (
            <div className="message-row-block" key={`${libraryId}:${page.conversation.username}:${message.uid}:fold`}>
              <details className="message-fold">
                <summary title="点击展开或收起"><span className="message-fold-label">{group.label}<Icon name="chevron" size={14} /></span></summary>
                <StickerParsingContext value={true}><div className="message-fold-content">{group.messages.map((item, itemIndex) => <MessageRow key={item.uid} message={item} isGroup={page.conversation.is_group} showTime={shouldShowTime(itemIndex > 0 ? group.messages[itemIndex - 1] : previous, item)} onPreview={setPreviewUrl} libraryId={libraryId} onQuote={message => void openQuote(message)} highlighted={item.uid === highlightUid} />)}</div></StickerParsingContext>
              </details>
            </div>
          )
          return (
            <div key={message.uid} className={activeFilter && !jumpPage ? 'filtered-message-result' : undefined}>
            <MessageRow
              message={message}
              highlighted={message.uid === highlightUid}
              isGroup={page.conversation.is_group}
              showTime={shouldShowTime(previous, message)}
              onPreview={setPreviewUrl}
              libraryId={libraryId}
              onLatest={jumpToLatest}
              latestSolitaireUid={solitaireLatest.scope === solitaireScope && message.solitaire_key ? solitaireLatest.messages[message.solitaire_key]?.uid : undefined}
              onQuote={message => void openQuote(message)}
            />
            {activeFilter && !jumpPage ? <button className="result-locate" aria-label="定位到聊天位置" disabled={contextBusy} onClick={() => { quoteOriginUid.current = message.uid; void jumpToMessage(message) }}><Icon name="chat" size={14}/>定位消息</button> : null}
            </div>
          )
        })}
        {categoryResults && shownPage?.next_offset != null ? <AutoLoadMore key={historyScope} cursor={shownPage.next_offset} busy={filterLoading || filterOlder} onLoad={loadOlderFiltered}/> : null}
        {jumpPage && contextHasNewer ? <AutoLoadMore key={historyScope} cursor={jumpPage.messages.at(-1)?.uid || ''} busy={contextBusy} onLoad={() => loadContext('after')}/> : null}
        <div ref={endRef} />
      </div>

      {quoteMessage?.quote ? <QuoteDialog media={['image', 'video'].includes(quoteMessage.quote.kind || '')} sender={quoteMessage.quote.sender_name || '引用消息'} onClose={closeQuote}>
        <div className="quote-dialog-body">
          {quoteMessage.quote.target ? ['image', 'video'].includes(quoteMessage.quote.target.type.kind)
            ? <MediaMessage message={quoteMessage.quote.target} libraryId={libraryId} onPreview={url => { closeQuote(); setPreviewUrl(url) }} />
            : <RichContent message={quoteMessage.quote.target} onPreview={url => { closeQuote(); setPreviewUrl(url) }} />
            : <div className="quote-snapshot"><LinkText text={quoteMessage.quote.text || '[消息]'} /></div>}
          {quoteMessage.quote.target?.quote ? <QuotePreview message={quoteMessage.quote.target} onOpen={message => { setQuoteHistory(history => [...history, quoteMessage]); void openQuote(message, true) }} /> : null}
        </div>
        <footer>
          {quoteHistory.length ? <button type="button" onClick={() => { quoteRequest.current++; setQuoteLoading(false); setQuoteMessage(quoteHistory.at(-1)!); setQuoteHistory(history => history.slice(0, -1)); setJumpNotice('') }}>返回上一层</button> : null}
          {quoteMessage.quote.target ? <button type="button" disabled={contextBusy} onClick={() => void jumpToQuoted(quoteMessage)}>{contextBusy ? '定位中…' : '定位到聊天位置'}</button> : <span>{quoteLoading ? '读取中…' : '原消息未同步或已不可用'}</span>}
          {jumpNotice ? <span role="status">{jumpNotice}</span> : null}
        </footer>
      </QuoteDialog> : null}
      {videoPreviewUrl ? <QuoteDialog sender="视频" media closeLabel="关闭视频" onClose={() => setVideoPreviewUrl(null)}>
        <div className="result-video-preview"><video src={videoPreviewUrl} controls autoPlay preload="metadata" /></div>
      </QuoteDialog> : null}
      {previewUrl ? (
        <button
          className="media-lightbox"
          type="button"
          onClick={() => setPreviewUrl(null)}
        >
          <img src={previewUrl} alt="媒体预览" />
        </button>
      ) : null}
    </main></StickerParsingContext>
  )
}
