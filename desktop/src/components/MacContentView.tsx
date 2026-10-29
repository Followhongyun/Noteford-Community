import useBackdropDismiss from '../useBackdropDismiss'
import {useEffect, useLayoutEffect, useRef, useState, type ReactNode} from 'react'
import {createPortal} from 'react-dom'
import type {ContentItem, ContentPage, ContentSource, Conversation, Message, MessagePage} from '../types'
import Icon from './Icon'
import Avatar from './Avatar'
import ChatView from './ChatView'
import LinkText, {ExternalLink} from './LinkText'
import channelsLogo from '../assets/weixin-channels.svg'

const categories = [['media', '图片与视频'], ['article', '公众号'], ['channel', '视频号'], ['link', '链接'], ['file', '附件']]
const downloadErrors: Record<string, string> = {session_unavailable: '请登录微信后重试', account_mismatch: '请登录对应微信账号后重试', missing_download_reference: '消息缺少原件下载信息', cdn_expired: '原件已过期或被清理', network_unavailable: '网络连接失败', download_timeout: '下载超时', insufficient_space: '磁盘空间不足', local_original_unavailable: '原件暂不可用', integrity_failed: '文件校验失败'}
function Dialog({title, onClose, children, compact = false, actions}: {title: string; onClose(): void; children: ReactNode; compact?: boolean; actions?: ReactNode}) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {const dialog = ref.current; dialog?.showModal(); return () => dialog?.close()}, [])
  const backdropDismiss = useBackdropDismiss(onClose)
  return <dialog {...backdropDismiss} className={`material-dialog${compact ? ' link-detail-dialog' : ''}`} ref={ref} aria-label={title} onCancel={event => {event.preventDefault(); onClose()}}>
    <header><strong>{title}</strong><div className="material-dialog-actions">{actions}<button type="button" aria-label="关闭预览" onClick={onClose}><Icon name="close"/></button></div></header>{children}
  </dialog>
}
function compactText(text: string) {return text.replace(/\r\n?/g, '\n').replace(/\n[ \t]*\n+/g, '\n')}
function sourceLabel(source?: ContentSource) {
  return (source?.is_group ?? source?.conversation_id.endsWith('@chatroom')) ? source?.conversation_name || '群聊' : '私信'
}
type NavigateSource = (source: ContentSource, signal?: AbortSignal) => Promise<void>
function SourceQuote({source, libraryId}: {source: ContentSource; libraryId: string}) {
  const [message, setMessage] = useState<Message | null>(null)
  const [error, setError] = useState(''), [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let alive = true
    setMessage(null); setError('')
    void readMedia(async () => alive ? window.noteford.messages({conversation: source.conversation_id, around_uid: source.message_uid, limit: 1}) : null).then(result => {
      if (!alive || !result) return
      const original = result.messages.find(row => row.uid === source.message_uid)
      if (!original || result.conversation.username !== source.conversation_id || (libraryId && result.library_id !== libraryId)) throw Error('source_changed')
      setMessage(original)
    }).catch(() => {if (alive) setError('原话读取失败')})
    return () => {alive = false}
  }, [source, libraryId, attempt])
  return <section className="link-source-quote">
    <div className="link-source-author"><div className="link-source-person"><Avatar name={message?.sender.name || source.sender} id={message?.sender.username} src={message?.sender.avatar_url} size={32}/><div className="link-person-labels"><strong>{message?.sender.name || source.sender}</strong><small title={sourceLabel(source)}>{sourceLabel(source)}</small></div></div></div>
    {message ? <p className="link-source-text"><LinkText text={compactText(message.text)}/></p> : error ? <p role="alert" className="link-source-status">{error}，<button type="button" onClick={() => setAttempt(value => value + 1)}>重试</button></p> : <p role="status" className="link-source-status">正在读取原话…</p>}
  </section>
}
function LinkDetails({item, libraryId, onClose, onNavigate}: {item: ContentItem; libraryId: string; onClose(): void; onNavigate?: NavigateSource}) {
  const [jumping, setJumping] = useState<number | null>(null)
  const [error, setError] = useState<{index: number; text: string} | null>(null)
  const navigation = useRef<AbortController | null>(null)
  useEffect(() => () => navigation.current?.abort(), [])
  async function navigate(source: ContentSource, index: number) {
    if (!onNavigate || jumping !== null) return
    const controller = new AbortController()
    navigation.current = controller
    setJumping(index); setError(null)
    try {await onNavigate(source, controller.signal); if (!controller.signal.aborted) onClose()}
    catch {if (!controller.signal.aborted) setError({index, text: '无法定位原消息，请重试'})}
    finally {if (!controller.signal.aborted) setJumping(null)}
  }
  const source = item.sources[0]
  return <Dialog title="详情" compact onClose={onClose} actions={<button type="button" className="link-return" title="回到原文" aria-label="回到原文" disabled={!source || !onNavigate || jumping !== null} onClick={() => void navigate(source, 0)}><Icon name={jumping !== null ? 'loading' : 'source'} size={18}/></button>}>
    <div className="link-detail-body">{source && <SourceQuote source={source} libraryId={libraryId}/>} {error && <p role="alert" className="content-error">{error.text}</p>}</div>
  </Dialog>
}
function LinkCard({item, avatar, onDetails, onNavigate}: {item: ContentItem; avatar?: string; onDetails(item: ContentItem): void; onNavigate?: NavigateSource}) {
  const source = item.sources[0]
  const [busy, setBusy] = useState(false), [error, setError] = useState('')
  const navigation = useRef<AbortController | null>(null)
  useEffect(() => () => navigation.current?.abort(), [])
  async function jump() {
    if (!source || !onNavigate || busy) return
    const controller = new AbortController(); navigation.current = controller
    setBusy(true); setError('')
    try {await onNavigate(source, controller.signal)}
    catch {if (!controller.signal.aborted) setError('无法定位原消息，请重试')}
    finally {if (!controller.signal.aborted) setBusy(false)}
  }
  return <article className="material-card link-card" onClick={() => onDetails(item)}>
    <div className="link-card-author"><div className="link-source-person"><Avatar name={source?.sender} id={source?.sender_username} src={avatar} size={28}/><div className="link-person-labels"><strong>{source?.sender || '未知发送者'}</strong><small title={sourceLabel(source)}>{sourceLabel(source)}</small></div></div><button type="button" className="link-return" aria-label={`回到原文：${source?.sender || ''}`} title="回到原文" disabled={!onNavigate || busy} onClick={event => {event.stopPropagation(); void jump()}}><Icon name={busy ? 'loading' : 'source'} size={17}/></button></div>
    <button type="button" className="link-card-open" aria-label={`查看详情：${source?.sender || ''}`}><span className="link-card-text">{compactText(item.text || item.url || '')}</span></button>
    {item.url && <div className="link-card-footer"><ExternalLink url={item.url}>{item.url}</ExternalLink></div>}
    {error && <p role="alert" className="content-error">{error}</p>}
  </article>
}

function AlbumMenu({position, onClose, children}: {position: {x: number; y: number}; onClose(): void; children: ReactNode}) {
  const ref = useRef<HTMLDivElement>(null)
  const close = useRef(onClose)
  close.current = onClose
  useLayoutEffect(() => {
    const menu = ref.current!
    const {width, height} = menu.getBoundingClientRect()
    menu.style.left = `${Math.max(8, Math.min(position.x, window.innerWidth - width - 8))}px`
    menu.style.top = `${Math.max(8, Math.min(position.y, window.innerHeight - height - 8))}px`
  })
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const menuElement = ref.current
    menuElement?.focus()
    const dismiss = () => close.current()
    const outside = (event: PointerEvent) => {if (!ref.current?.contains(event.target as Node)) dismiss()}
    const scroll = (event: Event) => {if (!ref.current?.contains(event.target as Node)) dismiss()}
    const escape = (event: KeyboardEvent) => {if (event.key === 'Escape') {event.preventDefault(); dismiss()}}
    window.addEventListener('keydown', escape)
    window.addEventListener('pointerdown', outside)
    window.addEventListener('scroll', scroll, true)
    window.addEventListener('resize', dismiss)
    window.addEventListener('noteford-album-menu', dismiss)
    return () => {
      window.removeEventListener('keydown', escape)
      window.removeEventListener('pointerdown', outside)
      window.removeEventListener('scroll', scroll, true)
      window.removeEventListener('resize', dismiss)
      window.removeEventListener('noteford-album-menu', dismiss)
      if (document.activeElement === document.body || document.activeElement === menuElement || menuElement?.contains(document.activeElement)) previous?.focus()
    }
  }, [])
  return createPortal(<div ref={ref} className="album-context" role="menu" aria-label="素材信息与操作" tabIndex={-1} style={{left: position.x, top: position.y}} onContextMenu={event => event.preventDefault()} onKeyDown={event => {
    if (event.key === 'Escape' || event.key === 'Tab') {event.preventDefault(); onClose(); return}
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const buttons = Array.from(ref.current!.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'))
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : event.key === 'ArrowDown' ? (index + 1) % buttons.length : (index - 1 + buttons.length) % buttons.length
    buttons[next]?.focus()
  }}>{children}</div>, document.body)
}
// Bound local media reads so opening a large album cannot flood the service.
let activeReads = 0
const readQueue: (() => void)[] = []
function readMedia<T>(read: () => Promise<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const run = () => {activeReads++; void read().then(resolve, reject).finally(() => {activeReads--; readQueue.shift()?.()})}
    if (activeReads < 4) run(); else readQueue.push(run)
  })
}
function fileStyle(name: string) {
  const extension = name.includes('.') ? name.split('.').pop()!.toUpperCase() : 'FILE'
  const kind = extension === 'PDF' ? 'pdf' : ['DOC', 'DOCX'].includes(extension) ? 'document' : ['XLS', 'XLSX', 'CSV'].includes(extension) ? 'sheet' : ['ZIP', 'RAR', '7Z', 'TAR', 'GZ'].includes(extension) ? 'archive' : ['DMG', 'PKG', 'EXE'].includes(extension) ? 'app' : 'text'
  return {extension: extension.length <= 8 ? extension : 'FILE', kind}
}
function linkCopy(item: ContentItem) {
  const label = item.category === 'article' ? '公众号' : item.category === 'channel' ? '视频号' : '链接'
  const title = item.title.trim() || label
  // Keep the full address in the title tooltip; do not repeat it as a summary.
  let summary = item.text.trim()
  if (item.url) summary = summary.split(item.url).join('').trim()
  if (summary.startsWith(title)) summary = summary.slice(title.length).replace(/^[\s：:，,。·—-]+/, '')
  if (/^(\[?(公众号|视频号|链接|分享|文章)\]?|https?:\/\/\S*)$/.test(summary)) summary = ''
  return {label, title, summary}
}
// Shares the lifetime and authorization scope of App's content cache.
let assetScope = 0
const assetCaches = new WeakMap<Map<string, ContentPage>, MaterialAssets>()
type MaterialAssets = {id: number; libraryId: string; messages: Map<string, {message: Message; at: number}>; pending: Map<string, Promise<Message | null>>; downloads: Map<string, Promise<Message['media']>>; downloadIds: Map<string, string>; avatars: Record<string, string>; avatarAt: Map<string, number>; avatarPending: Promise<void> | null; images: Map<string, HTMLImageElement>}
function materialAssets(cache: Map<string, ContentPage>): MaterialAssets {
  let assets = assetCaches.get(cache)
  if (!assets) {assets = {id: ++assetScope, libraryId: '', messages: new Map(), pending: new Map(), downloads: new Map(), downloadIds: new Map(), avatars: {}, avatarAt: new Map(), avatarPending: null, images: new Map()}; assetCaches.set(cache, assets)}
  return assets
}
function assetKey(source: ContentSource) {return JSON.stringify([source.conversation_id, source.message_uid])}
function warmImage(assets: MaterialAssets, url?: string | null) {
  if (!url || assets.images.has(url)) return
  if (assets.images.size >= 80) assets.images.delete(assets.images.keys().next().value!)
  const image = new Image()
  assets.images.set(url, image)
  image.src = url
  image.onerror = () => assets.images.delete(url)
}
function rememberMessage(assets: MaterialAssets, source: ContentSource, message: Message) {
  const key = assetKey(source)
  assets.messages.delete(key); assets.messages.set(key, {message, at: Date.now()})
  if (assets.messages.size > 1000) assets.messages.delete(assets.messages.keys().next().value!)
  warmImage(assets, message.card?.cover_url || message.card?.thumbnail_url)
  if (['image', 'emoji', 'gif'].includes(message.type.kind) || ['ready_thumbnail', 'thumbnail_only'].includes(message.media?.status || '')) warmImage(assets, message.media?.url)
}
function readMaterial(assets: MaterialAssets, source: ContentSource, force = false): Promise<Message | null> {
  const key = assetKey(source), cached = assets.messages.get(key)
  if (!force && cached && Date.now() - cached.at < 60000) return Promise.resolve(cached.message)
  const pending = assets.pending.get(key)
  if (pending) return pending
  const request = readMedia(async () => {
    const result = await window.noteford.messages({conversation: source.conversation_id, around_uid: source.message_uid, limit: 1})
    if (result.library_id !== assets.libraryId || result.conversation.username !== source.conversation_id) return null
    const message = result.messages.find(row => row.uid === source.message_uid) || null
    if (message) rememberMessage(assets, source, message)
    return message
  }).finally(() => assets.pending.delete(key))
  assets.pending.set(key, request)
  return request
}
async function warmAvatars(assets: MaterialAssets, items: ContentItem[]): Promise<void> {
  if (assets.avatarPending) {await assets.avatarPending; return warmAvatars(assets, items)}
  const request = fetchAvatars(assets, items)
  assets.avatarPending = request
  try {await request} finally {assets.avatarPending = null}
}
async function fetchAvatars(assets: MaterialAssets, items: ContentItem[]) {
  const names = [...new Set(items.flatMap(item => item.sources.map(source => source.sender_username)).filter((name): name is string => !!name))].filter(name => Date.now() - (assets.avatarAt.get(name) || 0) > 60000)
  for (let offset = 0; offset < names.length; offset += 2000) {
    const batch = names.slice(offset, offset + 2000)
    const rows = await window.noteford.avatars({library_id: assets.libraryId, usernames: batch})
    for (const row of rows) if (row.avatar_url) {assets.avatars[row.username] = row.avatar_url; warmImage(assets, row.avatar_url)}
    for (const name of batch) assets.avatarAt.set(name, Date.now())
    while (assets.avatarAt.size > 4000) {const oldest = assets.avatarAt.keys().next().value!; assets.avatarAt.delete(oldest); delete assets.avatars[oldest]}
  }
}
function Material({item, avatar, assets, libraryId, revision, onSource, onPreview}: {item: ContentItem; avatar?: string; assets: MaterialAssets; libraryId: string; revision: number; onSource(source: ContentSource): void; onPreview(url: string, video: boolean, title: string): void}) {
  const element = useRef<HTMLElement>(null)
  const [visible, setVisible] = useState(false)
  const [menu, setMenu] = useState<{x: number; y: number} | null>(null)
  const cachedMessage = item.sources[0] && assets.messages.get(assetKey(item.sources[0]))?.message
  const [media, setMedia] = useState<Message['media']>(cachedMessage?.media || null)
  const [channelMessage, setChannelMessage] = useState<Message | null>(cachedMessage || null)
  const lastRevision = useRef(revision)
  const [busy, setBusy] = useState(() => !!item.sources[0] && assets.downloads.has(assetKey(item.sources[0]))), [notice, setNotice] = useState(''), [broken, setBroken] = useState(false)
  const [downloadProgress, setDownloadProgress] = useState(0)
  const pending = useRef(false), version = useRef(0)
  const source = item.sources[0]
  const downloadable = ['image', 'video', 'file'].includes(item.category)
  const file = item.category === 'file' ? fileStyle(item.title) : null
  const channel = item.category === 'channel'
  const publication = item.category === 'article'
  const album = item.category === 'image' || item.category === 'video'
  const link = !album && !file ? linkCopy(item) : null
  const ready = media?.status === 'ready' && !!media.url
  const thumbnail = album && !!media?.url && ['ready_thumbnail', 'thumbnail_only'].includes(media.status || '')
  useEffect(() => {
    const observer = new IntersectionObserver(entries => setVisible(entries[0].isIntersecting), {rootMargin: '200px'})
    if (element.current) observer.observe(element.current)
    return () => observer.disconnect()
  }, [])
  useEffect(() => {
    if (!visible || (!downloadable && !channel && !publication) || !source || !libraryId) return
    let alive = true
    const read = async () => {
      if (pending.current) return
      const id = version.current
      try {
        const original = await readMaterial(assets, source, lastRevision.current !== revision)
        lastRevision.current = revision
        if (alive && id === version.current && original) {
          setMedia(original.media || null)
          if (channel || publication) setChannelMessage(original)
        }
      } catch { if (alive) setNotice('预览读取失败，可重试下载') }
    }
    void read()
    const timer = ready || channel || publication ? undefined : setInterval(() => void read(), 30000)
    return () => {alive = false; clearInterval(timer)}
  }, [visible, downloadable, channel, publication, source?.conversation_id, source?.message_uid, libraryId, revision, ready])
  useEffect(() => {
    if (source && assets.downloads.has(assetKey(source))) void download()
    return () => {version.current++}
  }, [])
  useEffect(() => {
    if (!busy || !source) return
    let alive = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const request_id = assets.downloadIds.get(assetKey(source))
        if (request_id) {
          const progress = await window.noteford.mediaProgress({library_id: libraryId, request_id})
          if (alive) setDownloadProgress(progress.total ? Math.min(100, Math.max(0, progress.received / progress.total * 100)) : 0)
        }
      } catch { /* The download result remains authoritative if progress is unavailable. */ }
      if (alive) timer = setTimeout(() => void poll(), 200)
    }
    void poll()
    return () => {alive = false; clearTimeout(timer)}
  }, [busy, assets, source, libraryId])
  async function download() {
    if (pending.current || !source || !libraryId) return
    pending.current = true; const id = ++version.current
    setBusy(true); setNotice(''); setDownloadProgress(0)
    try {
      const key = assetKey(source)
      let request = assets.downloads.get(key)
      if (!request) {
        const request_id = crypto.randomUUID()
        assets.downloadIds.set(key, request_id)
        request = window.noteford.recheckMedia({library_id: libraryId, uid: source.message_uid, request_id}).then(next => {
          const cached = assets.messages.get(key)?.message
          if (cached) rememberMessage(assets, source, {...cached, media: next?.url ? next : cached.media})
          return next
        }).finally(() => {assets.downloads.delete(key); assets.downloadIds.delete(key)})
        assets.downloads.set(key, request)
      }
      const next = await request
      if (id !== version.current) return
      setMedia(previous => next?.url ? next : previous?.url ? {...previous, download_error: next?.download_error} : next)
      setBroken(false)
      if (next?.status !== 'ready') setNotice(downloadErrors[next?.download_error || ''] || '原件暂未取得，可稍后重试')
    } catch { if (id === version.current) setNotice('下载失败，请重试') }
    finally {pending.current = false; if (id === version.current) setBusy(false)}
  }
  async function save() {
    if (!ready || !media?.url || busy) return
    setBusy(true); setNotice('')
    try {if (await window.noteford.saveMedia?.({url: media.url, name: item.category === 'file' ? item.title : undefined})) setNotice('已保存')}
    catch {setNotice('保存失败，请重试')}
    finally {setBusy(false)}
  }
  const byline = source && <span className="material-byline" title={source.conversation_name}>{source.conversation_name}</span>
  const downloadLabel = busy ? '处理中…' : ready && !broken ? '另存为' : notice || broken ? '重试下载' : '下载原件'
  function openMenu(x: number, y: number) {
    window.dispatchEvent(new Event('noteford-album-menu'))
    setMenu({x, y})
  }
  if (publication) {
    const cover = channelMessage?.card?.thumbnail_url || channelMessage?.card?.cover_url
    const title = channelMessage?.title || item.title
    const copy = linkCopy({...item, title, text: channelMessage?.text || item.text})
    const publisher = channelMessage?.card?.source_display_name || '公众号'
    return <article ref={element} className="material-card publication-card">
      <button type="button" className="publication-open" aria-label={`打开公众号文章：${title}`} onClick={() => {
        if (item.url) void window.noteford.openExternal({url: item.url}).catch(() => setNotice('文章打开失败'))
        else if (source) onSource(source)
      }}>
        <h2 title={title}>{title}</h2>
        <div className="publication-summary"><p>{copy.summary}</p>{cover && !broken ? <img src={cover} alt="文章封面" loading="lazy" onError={() => setBroken(true)}/> : <span className="publication-cover-placeholder" aria-hidden="true"/>}</div>
        <div className="publication-name"><Icon name="publication" size={18}/><span>{publisher}</span></div>
      </button>
      {notice && <p role="status" className="material-notice">{notice}</p>}
    </article>
  }
  if (channel) {
    const cover = channelMessage?.card?.cover_url || channelMessage?.card?.thumbnail_url
    const author = channelMessage?.title || item.title || '视频号'
    return <article ref={element} className="material-card channel-material-card">
      <button type="button" className="channel-material-cover" aria-label={`查看视频号：${author}`} onClick={() => {
        if (item.url) void window.noteford.openExternal({url: item.url}).catch(() => setNotice('视频号打开失败'))
        else if (source) onSource(source)
      }}>
        {cover && !broken ? <img className="channel-material-image" src={cover} alt="视频号封面" loading="lazy" onError={() => setBroken(true)}/> : <span className="channel-material-placeholder">{channelMessage?.text || '暂无视频号封面'}</span>}
        <span className="channel-material-play"><Icon name="circlePlay" size={54}/></span>
        <span className="channel-material-author"><img src={channelsLogo} alt="视频号"/><span>{author}</span></span>
      </button>
      {notice && <p role="status" className="material-notice">{notice}</p>}
    </article>
  }
  return <article ref={element} className={`material-card${album ? ' album-card' : file ? ' attachment-card' : ' reference-card'}`}>
    {album ? <button className="album-cover" type="button" disabled={busy || !libraryId || !source} aria-busy={busy} title={notice || undefined} aria-label={`${busy ? '正在下载' : !ready || broken ? '下载' : '预览'}${item.category === 'image' ? '图片' : '视频'}：${item.title}`} aria-haspopup="menu" aria-expanded={!!menu} onContextMenu={event => {event.preventDefault(); openMenu(event.clientX, event.clientY)}} onKeyDown={event => {
      if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) {event.preventDefault(); const rect = event.currentTarget.getBoundingClientRect(); openMenu(rect.left + 12, rect.top + 12)}
    }} onClick={() => {if (!ready || broken) void download(); else if (media?.url) onPreview(media.url, item.category === 'video', item.title)}}>
      {media?.url && !broken ? item.category === 'video' && ready ? <video src={media.url} muted preload="metadata" aria-hidden="true" onError={() => setBroken(true)}/> : <img src={media.url} loading="lazy" alt={item.title} onError={() => setBroken(true)}/> : null}
      {busy ? <span className="album-download-state" role="progressbar" aria-label="下载进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(downloadProgress)}>
        <span className="album-download-progress" aria-hidden="true" style={{background: `conic-gradient(white ${downloadProgress}%, #ffffff55 0)`}}/>
      </span>
        : !ready || broken ? <span className="album-download-state"><Icon name={media?.download_error === 'cdn_expired' || notice ? 'alert' : 'download'} size={24}/></span>
        : item.category === 'video' ? <span className="album-play"><Icon name="play" size={24}/></span> : null}
      {!busy && (notice || media?.download_error === 'cdn_expired') && (!ready || broken) && <span className="album-download-notice" role="status">{media?.download_error === 'cdn_expired' ? '文件已失效' : '下载失败，点击重试'}</span>}
    </button> : file ? <div className="attachment-heading">
      <div className={`attachment-icon attachment-icon--${file.kind}`}><Icon name="file" size={27}/><span>{file.extension}</span></div>
      <div className="attachment-copy"><h2 title={item.title}>{item.title.replace(/\.[^.]+$/, '') || item.title}</h2>{source && <div className="attachment-sender"><Avatar name={source.sender} id={source.sender_username} src={avatar} size={22}/><span title={source.sender}>{source.sender}</span></div>}</div>
      {source && <div className="material-icon-actions"><button type="button" title="查看原消息" aria-label="查看原消息" onClick={() => onSource(source)}><Icon name="source" size={17}/></button></div>}
    </div> : link && <div className="reference-heading">
      <div className={`reference-icon reference-icon--${item.category}`}>{item.category === 'channel' ? <img src={channelsLogo} alt="" className="channels-brand"/> : <Icon name={item.category === 'article' ? 'official' : 'link'} size={25}/>}<span>{link.label}</span></div>
      <div className="reference-copy"><h2 title={link.title}>{link.title}</h2>{byline}{link.summary && <p title={link.summary}>{link.summary}</p>}</div>
    </div>}
    {album ? menu && <AlbumMenu position={menu} onClose={() => setMenu(null)}>
      <div className="album-context-info"><div className="album-context-meta">{byline}{source && <time title={new Date(source.timestamp * 1000).toLocaleString('zh-CN')} dateTime={new Date(source.timestamp * 1000).toISOString()}>{new Date(source.timestamp * 1000).toLocaleString('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false})}</time>}</div><span>{busy ? '处理中…' : ready && !broken ? '已下载' : thumbnail ? '仅缩略图' : '未下载'}</span></div>
      {source && <button role="menuitem" type="button" onClick={() => {setMenu(null); onSource(source)}}><Icon name="source" size={17}/>查看原消息</button>}
      <button role="menuitem" type="button" disabled={busy || !libraryId || !source} onClick={() => void (ready && !broken ? save() : download())}><Icon name={busy ? 'loading' : 'download'} size={17} className={busy ? 'material-spin' : ''}/>{downloadLabel}</button>
      {item.sources.length > 1 && <div className="album-context-sources"><span>其他来源</span>{item.sources.slice(1).map(s => <button role="menuitem" type="button" key={s.message_uid} onClick={() => {setMenu(null); onSource(s)}}><Icon name="source" size={17}/><span>{s.conversation_name} · {s.sender}</span></button>)}</div>}
      {notice && <p role="status" className="material-notice">{notice}</p>}
    </AlbumMenu> : <div className="material-info">
      <div className="material-footer">
        {source && (file ? <span className="material-byline" title={sourceLabel(source)}>{sourceLabel(source)}</span> : <time dateTime={new Date(source.timestamp * 1000).toISOString()}>{new Date(source.timestamp * 1000).toLocaleDateString('zh-CN')}</time>)}
        <div className="material-icon-actions">
          {!file && source && <button type="button" title="查看原消息" aria-label="查看原消息" onClick={() => onSource(source)}><Icon name="source" size={17}/></button>}
          {link && item.url && <button type="button" title="打开原文" aria-label="打开原文" onClick={() => void window.noteford.openExternal({url: item.url!}).catch(() => setNotice('链接打开失败'))}><Icon name="external" size={17}/></button>}
          {downloadable && <button type="button" title={downloadLabel} aria-label={downloadLabel} aria-busy={busy} disabled={busy || !libraryId} onClick={() => void (ready && !broken ? save() : download())}><Icon name={busy ? 'loading' : 'download'} size={17} className={busy ? 'material-spin' : ''}/></button>}
        </div>
      </div>
      {item.sources.length > 1 && <details><summary><Icon name="chevron" size={12}/>{item.sources.length} 处来源</summary>{item.sources.slice(1).map(s => <button type="button" key={s.message_uid} onClick={() => onSource(s)}>{s.conversation_name} · {s.sender}</button>)}</details>}
      {notice && <p role="status" className="material-notice">{notice}</p>}
    </div>}
  </article>
}

export default function MacContentView({toolbarHost, cache: sharedCache, onNavigateSource, viewState}: {toolbarHost?: HTMLElement | null; cache?: Map<string, ContentPage>; viewState?: {category: string; query: string; conversation: string}; onNavigateSource?: NavigateSource}) {
  const localCache = useRef(new Map<string, ContentPage>())
  const cache = sharedCache || localCache.current
  const assets = materialAssets(cache)
  const [category, setCategory] = useState(viewState?.category || 'media'), [query, setQuery] = useState(viewState?.query || ''), [conversation, setConversation] = useState(viewState?.conversation || '')
  useEffect(() => {if (viewState) Object.assign(viewState, {category, query, conversation})}, [viewState, category, query, conversation])
  const [page, setPage] = useState<ContentPage | null>(() => cache.get(JSON.stringify([category, query, conversation])) || null), [busy, setBusy] = useState(false), [error, setError] = useState(''), [revision, setRevision] = useState(0)
  const [mediaLimit, setMediaLimit] = useState(0)
  const [sources, setSources] = useState<Conversation[]>([]), [libraryId, setLibraryId] = useState(assets.libraryId), [automatic, setAutomatic] = useState<boolean | null>(null), [saving, setSaving] = useState(false)
  const [, setAvatarRevision] = useState(0)
  useLayoutEffect(() => {assets.libraryId = libraryId}, [assets, libraryId])
  useEffect(() => {
    if (!libraryId || !page) return
    let alive = true
    void warmAvatars(assets, page.items.filter(item => ['link', 'file'].includes(item.category)))
      .then(() => {if (alive) setAvatarRevision(value => value + 1)}).catch(() => {})
    return () => {alive = false}
  }, [libraryId, page, assets])
  useEffect(() => {
    if (!libraryId || busy || query || conversation) return
    let alive = true
    // One category at a time; warm only the first screen, never video payloads.
    void (async () => {
      for (const [nextCategory] of categories) {
        if (!alive || nextCategory === category) continue
        const key = JSON.stringify([nextCategory, '', ''])
        let next = cache.get(key)
        if (!next) {
          next = await window.noteford.content({link_messages: true, category: nextCategory, query: '', conversation: '', limit: 100})
          if (!alive) return
          if (!cache.has(key)) cache.set(key, next)
          if (cache.size > 8) cache.delete(cache.keys().next().value!)
        }
        await warmAvatars(assets, next.items.slice(0, 24))
        for (const item of next.items.slice(0, 24)) {
          if (!alive) return
          if (item.category !== 'link' && item.sources[0]) await readMaterial(assets, item.sources[0])
        }
      }
    })().catch(() => {})
    return () => {alive = false}
  }, [libraryId, busy, category, query, conversation, cache, assets])
  const [detail, setDetail] = useState<ContentItem | null>(null)
  const [preview, setPreview] = useState<{url: string; video: boolean; title: string} | null>(null)
  const [source, setSource] = useState<ContentSource | null>(null), [context, setContext] = useState<MessagePage | null>(null), [contextError, setContextError] = useState(''), [contextBusy, setContextBusy] = useState(false)
  const generation = useRef(0), contextGeneration = useRef(0)
  useEffect(() => {
    let alive = true
    void window.noteford.libraries().then(rows => {if (alive) {assets.libraryId = (rows.find(row => row.is_current) || rows[0])?.id || ''; setLibraryId(assets.libraryId)}}).catch(() => {if (alive) setError('账号读取失败，请重新打开素材页')})
    void window.noteford.settings().then(settings => {if (alive) {setAutomatic(settings.auto_media_loading); setMediaLimit(settings.auto_media_max_mb || 0)}}).catch(() => {if (alive) setError('自动下载设置读取失败')})
    void (async () => {
      const rows: Conversation[] = []; let offset = 0
      while (alive) {const batch = await window.noteford.conversations({offset, limit: 500}); rows.push(...batch); if (batch.length < 500) break; offset += batch.length}
      if (alive) setSources(rows.filter(row => ['following', 'paused'].includes(row.selection_state)))
    })().catch(() => {if (alive) setError('来源列表读取失败')})
    return () => {alive = false; contextGeneration.current++}
  }, [])
  useLayoutEffect(() => {
    let alive = true; const id = ++generation.current
    const key = JSON.stringify([category, query, conversation])
    const cached = cache.get(key)
    setPage(cached || null)
    setBusy(true); setError('')
    const current = () => alive && id === generation.current
    const remember = (result: ContentPage) => {
      cache.delete(key); cache.set(key, result)
      if (cache.size > 8) cache.delete(cache.keys().next().value!)
    }
    const load = async () => {
      try {
        let result = await window.noteford.content({link_messages: true, category, query, conversation, limit: 100})
        if (!current()) return
        // Show the first screen immediately; publish the full list in one update.
        // During revalidation keep the complete cached list on screen.
        if (!cached) {setPage(result); remember(result)}
        const items = [...result.items]
        const seen = new Set(items.map(item => item.id))
        while (result.next_offset != null) {
          const offset = result.next_offset
          result = await window.noteford.content({link_messages: true, category, query, conversation, offset, limit: 100})
          if (!current()) return
          for (const item of result.items) if (!seen.has(item.id)) {seen.add(item.id); items.push(item)}
          if (result.next_offset != null && result.next_offset <= offset) throw Error('invalid_content_cursor')
        }
        const complete = {...result, items}
        remember(complete); setPage(complete)
      } catch {if (current()) setError('素材读取失败，已有内容已保留，请刷新重试')}
      finally {if (current()) setBusy(false)}
    }
    // Only text input is debounced; category switches start immediately.
    const timer = setTimeout(() => void load(), query ? 200 : 0)
    return () => {alive = false; clearTimeout(timer)}
  }, [category, query, conversation, revision, cache])
  async function openSource(value: ContentSource) {
    const id = ++contextGeneration.current; setSource(value); setContext(null); setContextError(''); setContextBusy(false)
    try {const result = await window.noteford.messages({conversation: value.conversation_id, around_uid: value.message_uid, limit: 60}); if (id === contextGeneration.current) {if (!result.messages.some(message => message.uid === value.message_uid)) throw Error(); setContext(result)}}
    catch {if (id === contextGeneration.current) setContextError('原消息读取失败，请关闭后重试')}
  }
  async function older() {
    if (!source || !context?.next_before || contextBusy) return
    const id = contextGeneration.current; setContextBusy(true)
    try {const next = await window.noteford.messages({conversation: source.conversation_id, before: context.next_before, before_uid: context.next_before_uid || undefined, limit: 60}); if (id === contextGeneration.current) setContext({...next, messages: [...next.messages, ...context.messages]})}
    catch {if (id === contextGeneration.current) setContextError('更早消息读取失败')}
    finally {if (id === contextGeneration.current) setContextBusy(false)}
  }
  async function saveLimit(limit: number) {
    if (saving) return; setSaving(true)
    try {const result = await window.noteford.saveAutoMediaLimit(limit); setMediaLimit(result.auto_media_max_mb)}
    catch {setError('自动下载大小保存失败')}
    finally {setSaving(false)}
  }
  async function toggleAutomatic(enabled: boolean) {
    if (saving) return; setSaving(true)
    try {const result = await window.noteford.saveAutoMediaLoading(enabled); setAutomatic(result.auto_media_loading)}
    catch {setError('自动下载设置保存失败')}
    finally {setSaving(false)}
  }
  return <main className="content-screen mac-materials" aria-labelledby="workspace-page-title">
    {toolbarHost && createPortal(<div className="material-title-actions"><label><input type="checkbox" role="switch" aria-label="自动下载图片、视频和附件" checked={automatic === true} disabled={automatic === null || saving} onChange={event => void toggleAutomatic(event.target.checked)}/>自动下载</label><select aria-label="自动下载大小" title="超过所选大小可手动下载；全部下载不限大小" value={mediaLimit} disabled={automatic !== true || saving} onChange={event => void saveLimit(Number(event.target.value))}><option value={20}>20MB 以下</option><option value={30}>30MB 以下</option><option value={50}>50MB 以下</option><option value={0}>全部下载</option></select><button className="mac-page-action" disabled={busy} onClick={() => setRevision(value => value + 1)}><Icon name="refresh" size={16}/>刷新</button></div>, toolbarHost)}
    <div className="material-filters"><nav aria-label="素材分类">{categories.map(([key, label]) => <button key={key} type="button" aria-pressed={category === key} onClick={() => setCategory(key)}>{label}</button>)}</nav>
      <div className="material-filter-inputs"><select aria-label="筛选素材来源" value={conversation} onChange={event => setConversation(event.target.value)}><option value="">全部来源</option>{sources.map(row => <option key={row.username} value={row.username}>{row.name}</option>)}</select><label><Icon name="search" size={16}/><input aria-label="搜索素材" placeholder="搜索标题、内容或来源" value={query} onChange={event => setQuery(event.target.value)}/></label></div>
    </div>
    {error && <p role="alert" className="content-error">{error}</p>}
    <div className="material-scroll"><div className={`material-grid${category === 'media' || category === 'channel' ? ' album-grid' : ''}`}>
      {page?.items.map(item => item.category === 'link' ? <LinkCard key={item.id} item={item} avatar={assets.avatars[item.sources[0]?.sender_username || '']} onDetails={setDetail} onNavigate={onNavigateSource}/> : <Material key={`${assets.id}:${item.id}`} assets={assets} item={item} avatar={assets.avatars[item.sources[0]?.sender_username || '']} libraryId={libraryId} revision={revision} onSource={value => void openSource(value)} onPreview={(url, video, title) => setPreview({url, video, title})}/>)}
    </div>{!page?.items.length && <div className="content-empty" role="status">{busy ? '正在读取素材…' : error ? '素材暂不可用' : '没有符合条件的素材'}{!busy && (query || conversation) && <button className="content-reset" onClick={() => {setQuery(''); setConversation('')}}>清除筛选</button>}</div>}
      {busy && !!page?.items.length && <p className="content-more" role="status">正在更新素材…</p>}
    </div>
    {detail && <LinkDetails item={detail} libraryId={libraryId} onClose={() => setDetail(null)} onNavigate={onNavigateSource}/>}
    {preview && <Dialog title={preview.title} onClose={() => setPreview(null)}><div className="material-preview">{preview.video ? <video src={preview.url} controls autoPlay/> : <img src={preview.url} alt={preview.title}/>}</div></Dialog>}
    {source && <Dialog title="原消息" onClose={() => {contextGeneration.current++; setSource(null)}}>{contextError ? <p role="alert">{contextError}</p> : <ChatView page={context} loading={!context} loadingOlder={contextBusy} onLoadOlder={() => void older()} libraryId={libraryId}/>}</Dialog>}
  </main>
}
