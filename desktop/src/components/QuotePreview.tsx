import useBackdropDismiss from '../useBackdropDismiss'
import { useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import type { Message } from '../types'
import { StickerParsingContext } from '../sticker-parsing'
import Icon from './Icon'

const names: Record<string, string> = {
  image: '图片', video: '视频', voice: '语音', file: '文件', emoji: '表情', gif: '表情',
  link: '链接', open_link: '链接', webview: '链接', location: '位置', contact_card: '名片',
  merged_messages: '聊天记录', channel_video: '视频号', mini_program: '小程序',
  group_chat_reference: '接龙', transfer: '转账', red_envelope: '红包', quote: '引用回复', revoke: '已撤回',
}

export function QuotePreview({ message, onOpen }: { message: Message; onOpen?(message: Message): void }) {
  const parseStickers = useContext(StickerParsingContext)
  const quote = message.quote
  const target = quote?.target
  const kind = quote?.kind || target?.type.kind || 'text'
  const stickerDisabled = !parseStickers && ['emoji', 'gif'].includes(kind)
  const [imageFailed, setImageFailed] = useState(false)
  const image = !stickerDisabled && !imageFailed && ['image', 'video', 'emoji', 'gif'].includes(kind) ? (target?.media?.url || target?.card?.thumbnail_url || target?.card?.cover_url) : null
  useEffect(() => setImageFailed(false), [target?.media?.url, message.uid])
  if (!quote) return null
  const text = stickerDisabled ? '[表情]' : quote.text || target?.title || target?.text || `[${names[kind] || '消息'}]`
  return <button className="quote-preview" type="button" disabled={!onOpen} onClick={() => onOpen?.(message)} aria-label={`查看引用：${quote.sender_name || '消息'}，${names[kind] || '文字'}`}>
    <span className="quote-preview-copy">
      <span className="quote-preview-sender">{quote.sender_name || target?.sender.name || '引用消息'}: </span>
      <span className="quote-preview-text">{text}</span>
    </span>
    {image ? <span className="quote-preview-media"><img src={image} alt="" loading="lazy" onError={() => setImageFailed(true)} />{kind === 'video' ? <Icon name="play" size={16} /> : null}</span>
      : null}
  </button>
}

export function QuoteDialog({ sender, media = false, closeLabel = '关闭引用', onClose, children }: { sender: string; closeLabel?: string; media?: boolean; onClose(): void; children: ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => { const element = dialog.current; element?.showModal(); return () => element?.close() }, [])
  const backdropDismiss = useBackdropDismiss(onClose)
  return <dialog {...backdropDismiss} ref={dialog} className={`quote-dialog${media ? ' quote-dialog-media' : ''}`} aria-labelledby="quote-dialog-title" onCancel={event => { event.preventDefault(); onClose() }}>
    <header><h2 id="quote-dialog-title">{sender}</h2><button type="button" onClick={onClose} aria-label={closeLabel}><Icon name="close" size={20} /></button></header>
    {children}
  </dialog>
}
