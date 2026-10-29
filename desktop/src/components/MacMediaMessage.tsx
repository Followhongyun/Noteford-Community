import {useEffect, useRef, useState, type CSSProperties} from 'react'
import type {Message} from '../types'
import {macMediaState} from '../macMediaState'
import Icon from './Icon'

export default function MacMediaMessage({message, libraryId, onPreview, onVideoPreview}: {
  message: Message; libraryId?: string; onPreview(url: string): void; onVideoPreview?(url: string): void
}) {
  const [media, setMedia] = useState(message.media)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [playbackError, setPlaybackError] = useState(false)
  const [ratio, setRatio] = useState<number | null>(null)
  const generation = useRef(0)
  const pending = useRef(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => {
    return () => { generation.current++; clearTimeout(timer.current) }
  }, [])
  useEffect(() => {
    // Conversation polling replaces media objects every few seconds. It must not
    // invalidate a manual download that is still running.
    if (pending.current) return
    setMedia(message.media); setPlaybackError(false)
  }, [message.media?.url, message.media?.status, message.media?.download_error])
  const state = macMediaState(message.type.kind, media)

  async function recheck() {
    if (!libraryId || pending.current || state.expired) return
    pending.current = true
    const request = ++generation.current
    setBusy(true); setNotice('')
    // A stalled service must not leave the card permanently busy; late replies
    // cannot replace a later retry or another message/library's preview.
    timer.current = setTimeout(() => {
      if (request !== generation.current) return
      generation.current++; pending.current = false
      setBusy(false); setNotice('读取超时，已保留现有预览，请重试。')
    }, 245000)
    try {
      const result = await window.noteford.recheckMedia({library_id: libraryId, uid: message.uid})
      if (request !== generation.current) return
      setPlaybackError(false)
      // A transient failure must not discard a still-readable cached preview.
      setMedia(previous => result?.url
        ? {...result, url: `${result.url}${result.url.includes('?') ? '&' : '?'}v=${Date.now()}`}
        : previous?.url ? {...previous, download_error: result?.download_error || 'local_original_unavailable'} : result)
    } catch {
      if (request === generation.current) setNotice('读取失败，已保留现有预览，请重试。')
    } finally {
      if (request === generation.current) {
        clearTimeout(timer.current); pending.current = false; setBusy(false)
      }
    }
  }

  const previewImage = media?.url && (state.thumbnail || (state.full && message.type.kind === 'image'))
  return <div className="media-message-content">
    {previewImage ? <button className={message.type.kind === 'image' ? 'image-message' : 'video-thumbnail'}
      type="button" aria-label={state.thumbnail ? `查看${state.name}缩略图` : '查看图片'}
      style={{'--chat-image-ratio': ratio ?? 1} as CSSProperties}
      onClick={() => onPreview(media!.url!)}>
      <img src={media!.url!} alt={state.thumbnail ? `${state.name}缩略图` : '微信图片'} loading="lazy"
        onError={() => setPlaybackError(true)} onLoad={event => {
          const image = event.currentTarget
          if (image.naturalHeight) setRatio(previous => previous ?? image.naturalWidth / image.naturalHeight)
        }} />
      {state.thumbnail ? <span className={message.type.kind === 'image' ? 'image-quality-label' : 'video-thumb-caption'}>缩略图</span> : null}
    </button> : state.full && media?.url ? onVideoPreview
      ? <button type="button" className="media-video-cover" aria-label="播放视频" onClick={() => onVideoPreview(media.url!)}>
        <video src={media.url} muted preload="metadata" tabIndex={-1} aria-hidden="true" onError={() => setPlaybackError(true)} />
        <span className="video-play-mark"><Icon name="play" size={20} /></span>
      </button>
      : <video className="video-message" controls preload="metadata" src={media.url} onError={() => setPlaybackError(true)} />
      : <div className="media-card unavailable-media"><Icon name={message.type.kind === 'image' ? 'image' : 'video'} /><span>{state.name}</span></div>}
    {notice || playbackError || state.notice ? <p className="media-load-error" role="status">{notice || (playbackError ? '预览加载失败，请重试。' : state.notice)}</p> : null}
    {libraryId && !state.expired && (!state.full || playbackError) ? <button className="media-retry-button" type="button"
      disabled={busy} aria-busy={busy} onClick={() => void recheck()}>{busy ? '正在下载…' : playbackError || notice ? '重试下载' : '下载原件'}</button> : null}
  </div>
}
