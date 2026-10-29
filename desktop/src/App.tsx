import {loadVisibility, visibilityKey, type MessageVisibility} from './message-visibility'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Avatar from './components/Avatar'
import ChatView from './components/ChatView'
import ContactDirectory from './components/ContactDirectory'
import ContentView from './components/ContentView'
import NavigationHistory from './components/NavigationHistory'
import useNavigationHistory from './useNavigationHistory'
import ConversationList from './components/ConversationList'
import Icon from './components/Icon'
import LibrarySwitcher from './components/LibrarySwitcher'
import { presentConnectCard } from '@noteford/wechat-connect'
import { GRANT_UI_TIMEOUT_MS, reduceGrantUi } from '@noteford/permission-guide'
import PermissionGuide from './components/PermissionGuide'
import SettingsView from './components/SettingsView'
import SidebarSync from './components/SidebarSync'
import { claimFirstFollowVisit } from './follow-guide'
import type {
  AppStatus,
  AutoCaptureSnapshot,
  Conversation,
  ContentPage,
  ContentSource,
  LibraryCandidate,
  LibrarySummary,
  MessagePage,
  NavSection,
  PermissionGuide as PermissionGuideModel,
  } from './types'

function useDebounced<T>(value: T, delay = 260) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay)
    return () => window.clearTimeout(timer)
  }, [value, delay])
  return debounced
}

async function readDirectory(query = ''): Promise<Conversation[]> {
  const first = await window.noteford.conversations({limit: 500, query: query || undefined})
  if (window.noteford.platform !== 'darwin') return first
  const rows = new Map(first.map(row => [row.username, row]))
  let count = first.length
  let offset = count
  while (count === 500) {
    const page = await window.noteford.conversations({limit: 500, offset, query: query || undefined})
    for (const row of page) rows.set(row.username, row)
    count = page.length
    offset += count
  }
  return [...rows.values()]
}

export default function App() {
  const productStorageKey = `${visibilityKey}:community`
  const [nav, setNav] = useState<NavSection>('following')
  const [messageVisibility, setMessageVisibility] = useState(() => loadVisibility(productStorageKey, false))
  const [parseStickers, setParseStickers] = useState<boolean | null>(null)
  const stickerRequest = useRef(0)
  const changeStickerParsing = useCallback((enabled: boolean) => {
    stickerRequest.current++
    setParseStickers(enabled)
  }, [])
  useEffect(() => {
    let cancelled = false
    const request = ++stickerRequest.current
    window.noteford.settings().then(value => {
      if (!cancelled && request === stickerRequest.current) setParseStickers(value.parse_stickers !== false)
    }).catch(() => {
      if (!cancelled && request === stickerRequest.current) setError('表情设置读取失败，暂不加载表情。请打开设置重试。')
    })
    return () => { cancelled = true }
  }, [])
  function changeMessageVisibility(next: MessageVisibility) {
    try {
      localStorage.setItem(productStorageKey, JSON.stringify(next))
      setMessageVisibility(next)
    } catch { setError('显示设置保存失败，请重试。') }
  }
  const [status, setStatus] = useState<AppStatus | null>(null)
  const [libraries, setLibraries] = useState<LibrarySummary[]>([])
  const [libraryMenuOpen, setLibraryMenuOpen] = useState(false)
  const [followGuideLibrary, setFollowGuideLibrary] = useState<string | null>(null)
  const followGuideChecked = useRef(false)
  const accountSwitching = useRef(false)
  const accountEpoch = useRef(0)
  const [libraryBusy, setLibraryBusy] = useState(false)
  const [selectingCandidateId, setSelectingCandidateId] = useState<string | null>(null)
  const [libraryCandidates, setLibraryCandidates] = useState<LibraryCandidate[]>([])
  const [scanningLibraries, setScanningLibraries] = useState(false)
  const [candidateScanComplete, setCandidateScanComplete] = useState(false)
  const [onboardingCopy, setOnboardingCopy] = useState({
    intro: '已从微信记录的数据位置查找账号。选择账号后才建立本地只读资料库；读取昵称和头像需要对应账号正在电脑端登录。',
    empty: '未找到微信资料。请确认已在电脑端登录微信，或手动选择微信数据所在目录。',
    permissions: { pending: false, autoRequest: false, title: '', message: '' },
  })
  const [permissionGuide, storePermissionGuide] = useState<PermissionGuideModel | null>(null)
  const setPermissionGuide = useCallback((next: PermissionGuideModel) => {
    storePermissionGuide(previous => (previous?.revision ?? 0) > (next.revision ?? 0) ? previous : next)
  }, [])
  const [grantUi, setGrantUi] = useState<{ grantingId: string | null; notice: string; noticeId: string | null }>({ grantingId: null, notice: '', noticeId: null })
  const grantGeneration = useRef(0)
  const [connectSnap, setConnectSnap] = useState<AutoCaptureSnapshot | null>(null)
  const [onboardingStep, setOnboardingStep] = useState<'permissions' | 'connect' | 'account'>('account')

  const [followingRows, setFollowingRows] = useState<Conversation[]>([])
  const [directoryRows, setDirectoryRows] = useState<Conversation[]>([])
  const [navigationAnchor, setNavigationAnchor] = useState<string | null>(null)
  const [focusMessageUid, setFocusMessageUid] = useState<string | null>(null)
  const [selected, setSelected] = useState<Conversation | null>(null)
  const [page, setPage] = useState<MessagePage | null>(null)
  const messageRequest = useRef(0)
  const olderRequest = useRef(false)
  const pageRef = useRef(page)
  pageRef.current = page

  const [directorySearch, setDirectorySearch] = useState('')
  const [loadingFollowing, setLoadingFollowing] = useState(false)
  const [loadingDirectory, setLoadingDirectory] = useState(false)
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const syncInFlight = useRef(false)
  const refreshMessages = useRef<() => Promise<void>>(async () => {})
  const [selectionBusyUsername, setSelectionBusyUsername] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const avatarRevision = useRef('')
  const debouncedDirectorySearch = useDebounced(directorySearch)
  const currentLibrary = libraries.find(item => item.is_current) || libraries[0] || null
  const contentViewState = useMemo(() => ({category: 'media', query: '', conversation: ''}), ['community', currentLibrary?.id])
  const contentSelection = followingRows.map(row => `${row.username}:${row.selection_state}`).sort().join('\n')
  const contentCache = useMemo(() => new Map<string, ContentPage>(), ['community', 'local', currentLibrary?.id, contentSelection])
  const loadStatus = useCallback(async () => {
    const epoch = accountEpoch.current
    const next = await window.noteford.status()
    if (epoch !== accountEpoch.current) return next
    setStatus(next)
    return next
  }, [])

  const loadLibraries = useCallback(async () => {
    const epoch = accountEpoch.current
    const rows = await window.noteford.libraries()
    if (epoch !== accountEpoch.current) return []
    setLibraries(rows)
    return rows
  }, [])

  const scanCandidates = useCallback(async (wechatRoot?: string) => {
    setScanningLibraries(true)
    setCandidateScanComplete(false)
    try {
      const rows = await window.noteford.libraryCandidates({ wechat_root: wechatRoot || null })
      setLibraryCandidates(rows)
      setCandidateScanComplete(true)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
      setCandidateScanComplete(true)
    } finally {
      setScanningLibraries(false)
    }
  }, [])

  const loadFollowing = useCallback(async (query = '') => {
    const epoch = accountEpoch.current
    setLoadingFollowing(true)
    try {
      const rows = await window.noteford.conversations({
        limit: 500,
        query: query || undefined,
        state: 'following',
      })
      if (epoch !== accountEpoch.current) return []
      setFollowingRows(rows)
      setError(null)
      return rows
    } catch (cause) {
      if (epoch !== accountEpoch.current) return []
      setError(cause instanceof Error ? cause.message : String(cause))
      return []
    } finally {
      setLoadingFollowing(false)
    }
  }, [])

  const loadDirectory = useCallback(async (query = '') => {
    const epoch = accountEpoch.current
    setLoadingDirectory(true)
    try {
      const rows = await readDirectory(query)
      if (epoch !== accountEpoch.current) return []
      setDirectoryRows(rows)
      setError(null)
      return rows
    } catch (cause) {
      if (epoch !== accountEpoch.current) return []
      setError(cause instanceof Error ? cause.message : String(cause))
      return []
    } finally {
      setLoadingDirectory(false)
    }
  }, [])

  const openConversation = useCallback(async (row: Conversation) => {
    const request = ++messageRequest.current
    setNavigationAnchor(null)
    setFocusMessageUid(null)
    setSelected(row)
    setPage(null)
    setLoadingMessages(true)
    try {
      const next = await window.noteford.messages({
        conversation: row.username,
        limit: 100,
      })
      if (request !== messageRequest.current) return
      setPage(next)
      setError(null)
    } catch (cause) {
      if (request !== messageRequest.current) return
      setPage(null)
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      if (request === messageRequest.current) setLoadingMessages(false)
    }
  }, [])

  async function navigateToContentSource(source: ContentSource, signal?: AbortSignal) {
    const epoch = accountEpoch.current
    const request = ++messageRequest.current
    const library = currentLibrary?.id
    const row = [...followingRows, ...directoryRows].find(row => row.username === source.conversation_id) || (await readDirectory()).find(row => row.username === source.conversation_id)
    if (!row) throw Error('conversation_unavailable')
    const next = await window.noteford.messages({conversation: source.conversation_id, around_uid: source.message_uid, limit: 100})
    if (signal?.aborted || epoch !== accountEpoch.current || request !== messageRequest.current) return
    if (next.library_id !== library || next.conversation.username !== source.conversation_id || !next.messages.some(message => message.uid === source.message_uid)) throw Error('source_unavailable')
    setSelected(row)
    setPage(next)
    setNavigationAnchor(source.message_uid)
    setFocusMessageUid(source.message_uid)
    setLoadingMessages(false)
    setError(null)
    setNav('following')
  }

  const loadOlder = useCallback(async () => {
    if (!page?.next_before || !selected || olderRequest.current) return
    const request = messageRequest.current
    olderRequest.current = true
    setLoadingOlder(true)
    try {
      const older = await window.noteford.messages({
        conversation: selected.username,
        limit: 100,
        before: page.next_before,
        before_uid: page.next_before_uid || undefined,
      })
      if (request !== messageRequest.current || older.library_id !== page.library_id) return
      setPage(current => {
        if (!current || current.conversation.username !== older.conversation.username || current.library_id !== older.library_id) return current
        const known = new Set(current.messages.map(message => message.uid))
        return {...current, messages: [...older.messages.filter(message => !known.has(message.uid)), ...current.messages], next_before: older.next_before, next_before_uid: older.next_before_uid}
      })
    } catch { setError('历史消息读取失败，请重试') }
    finally { olderRequest.current = false; setLoadingOlder(false) }
  }, [page, selected])

  const reloadLists = useCallback(async () => {
    const [following, directory] = await Promise.all([
      loadFollowing(),
      loadDirectory(directorySearch),
    ])
    return { following, directory }
  }, [directorySearch, loadFollowing, loadDirectory])

  const refreshHostProfile = useCallback(async () => {
    const profile = await window.noteford.hostProfile()
    setOnboardingCopy(profile.onboarding)
    return profile
  }, [])

  const grantPermission = useCallback(async (id: string) => {
    const generation = ++grantGeneration.current
    setGrantUi(reduceGrantUi({ grantingId: null, notice: '' }, { type: 'start', id }))
    const timer = window.setTimeout(() => {
      if (grantGeneration.current !== generation) return
      setGrantUi(state => reduceGrantUi(state, { type: 'timeout' }))
    }, GRANT_UI_TIMEOUT_MS)
    try {
      const result = await window.noteford.grantPermission({ id })
      if (grantGeneration.current !== generation) return
      setGrantUi(reduceGrantUi({ grantingId: id, notice: '' }, {
        type: 'result',
        id,
        status: result?.status,
        guidance: result?.guidance,
        openedSettings: result?.openedSettings,
      }))
      setPermissionGuide(await window.noteford.permissionGuide())
    } catch {
      if (grantGeneration.current !== generation) return
      setGrantUi(state => reduceGrantUi(state, { type: 'timeout' }))
    } finally {
      window.clearTimeout(timer)
    }
  }, [])

  const openAccountStep = useCallback(() => {
    setOnboardingStep('account')
    if (libraries.length === 0) {
      setNav('settings')
      void scanCandidates()
    }
  }, [libraries.length, scanCandidates])

  const continueFromPermissions = useCallback(async () => {
    if (!permissionGuide?.canContinue) return
    if (onboardingStep === 'connect' && !presentConnectCard(connectSnap).canContinue) return
    try {
      const result = await window.noteford.completePermissionGuide()
      if (!result?.saved) {
        setPermissionGuide(await window.noteford.permissionGuide())
        return
      }
    } catch {
      setGrantUi({ grantingId: null, notice: '无法确认权限，请稍后重试。', noticeId: 'automation' })
      return
    }
    if (onboardingStep === 'connect') openAccountStep()
    else setOnboardingStep('connect')
  }, [connectSnap, onboardingStep, openAccountStep, permissionGuide, setPermissionGuide])

  const startWechatScan = useCallback(async () => {
    if (!presentConnectCard(connectSnap).canStart) return
    try {
      setConnectSnap(await window.noteford.connectMacAccount({}))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '没有开始扫码，请重试。')
    }
  }, [connectSnap])


  useEffect(() => {
    void refreshHostProfile().catch(() => {})
  }, [refreshHostProfile])

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const [foundLibraries, , rows] = await Promise.all([
          loadLibraries(),
          loadStatus(),
          loadFollowing(),
        ])
        if (cancelled) return
        let needsGuide = false
        let needsConnect = false
        if (window.noteford.platform === 'darwin' && foundLibraries.length === 0) {
          setOnboardingStep('permissions')
          try {
            const guide = await window.noteford.permissionGuide()
            if (cancelled) return
            setPermissionGuide(guide)
            needsGuide = guide.active && !guide.canContinue
          } catch {
            needsGuide = true
          }
          if (!needsGuide) {
            try {
              const snap = await window.noteford.autoCaptureStatus()
              if (cancelled) return
              setConnectSnap(snap)
              needsConnect = !presentConnectCard(snap).canContinue
            } catch {
              needsConnect = true
            }
          }
        } else if (window.noteford.platform === 'darwin') {
          try {
            const snap = await window.noteford.autoCaptureStatus()
            if (!cancelled) {
              setConnectSnap(snap)
              needsConnect = false
            }
          } catch { /* 已有资料库时，取钥状态读不到就不挡阅读。 */ }
        }
        setOnboardingStep(needsGuide ? 'permissions' : needsConnect ? 'connect' : 'account')
        if (foundLibraries.length === 0 && !needsGuide && !needsConnect) {
          setNav('settings')
          void scanCandidates()
        } else if (foundLibraries.length === 0) {
          setNav('settings')
        }
        if (rows.length > 0 && messageRequest.current === 0) await openConversation(rows[0])
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause))
      }
    })()
    return () => { cancelled = true }
  }, [loadLibraries, loadStatus, loadFollowing, openConversation, scanCandidates])

  useEffect(() => {
    if (!currentLibrary?.id) return
    let cancelled = false
    let inFlight = false
    const refreshFollowing = async () => {
      if (inFlight || document.hidden) return
      inFlight = true
      try {
        const rows = await window.noteford.conversations({
          limit: 500,
          state: 'following',
        })
        if (cancelled) return
        const latestStatus = await window.noteford.status()
        if (cancelled) return
        setStatus(latestStatus)
        setFollowingRows(current => {
          if (window.noteford.platform === 'darwin') return rows
          const avatars = new Map(current.map(row => [row.username, row.avatar_url]))
          return rows.map(row => ({...row, avatar_url: avatars.get(row.username) || row.avatar_url}))
        })
      } catch { /* Preserve the last list and retry without interrupting reading. */ }
      finally { inFlight = false }
    }
    void refreshFollowing()
    const timer = window.setInterval(() => void refreshFollowing(), window.noteford.platform === 'darwin' ? 2000 : 15000)
    const onVisible = () => { if (!document.hidden) void refreshFollowing() }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      cancelled = true
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [currentLibrary?.id, page?.conversation.username, page?.messages.at(-1)?.uid])

  useEffect(() => {
    if (window.noteford.platform !== 'darwin' || !currentLibrary?.id) return
    let cancelled = false
    void Promise.all([
      window.noteford.libraries(),
      nav === 'contacts' ? readDirectory(debouncedDirectorySearch) : Promise.resolve(null),
    ]).then(([nextLibraries, rows]) => {
      if (cancelled) return
      setLibraries(nextLibraries)
      if (rows) setDirectoryRows(rows)
    }).catch(() => { /* Retry after the next successful archive update. */ })
    return () => { cancelled = true }
  }, [currentLibrary?.id, status?.archive_last_success_at, status?.messages, status?.conversations, nav, debouncedDirectorySearch])

  useEffect(() => {
    if (nav !== 'contacts') return
    void loadDirectory(debouncedDirectorySearch)
  }, [nav, debouncedDirectorySearch, loadDirectory])

  useEffect(() => {
    if (!currentLibrary?.id) return
    let cancelled = false
    let inFlight = false
    const libraryId = currentLibrary.id
    const refreshAvatars = async () => {
      if (inFlight) return
      inFlight = true
      try {
        const next = await window.noteford.status()
        const revision = next.avatar_sync?.completed_at
        const key = `${libraryId}:${revision}`
        if (cancelled || next.library_id !== libraryId || !revision || avatarRevision.current === key) return
        const usernames = [...new Set([...(followingRows.map(row => row.username)), ...(page?.messages.map(message => message.sender.username).filter((name): name is string => Boolean(name)) || [])])]
        if (!usernames.length) return
        const rows = await window.noteford.avatars({library_id: libraryId, usernames})
        if (cancelled) return
        const urls = new Map(rows.filter(row => row.avatar_url).map(row => [row.username, `${row.avatar_url}?v=${encodeURIComponent(revision)}`]))
        setFollowingRows(current => current.map(row => ({...row, avatar_url: urls.get(row.username) || row.avatar_url})))
        setPage(current => current ? {...current, messages: current.messages.map(message => ({...message, sender: {...message.sender, avatar_url: urls.get(message.sender.username || '') || message.sender.avatar_url}}))} : current)
        setStatus(next)
        avatarRevision.current = key
      } catch { /* Retry on next tick without interrupting reading. */ }
      finally { inFlight = false }
    }
    void refreshAvatars()
    const timer = window.setInterval(() => void refreshAvatars(), 15000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [currentLibrary?.id, followingRows, page])

  useEffect(() => {
    if (!selected || !currentLibrary?.id) return
    let cancelled = false
    let inFlight = false
    const conversation = selected.username
    const libraryId = currentLibrary.id
    const checkMessageUpdates = async () => {
      if (inFlight || document.hidden) return
      inFlight = true
      try {
        const next = await window.noteford.messages({conversation, limit: 100})
        if (cancelled || next.library_id !== libraryId || next.conversation.username !== conversation) return
        const tail = pageRef.current?.conversation.username === conversation ? pageRef.current.messages.at(-1) : undefined
        // Read through a busy interval before appending, so a 100-message page cannot leave a gap.
        let before = next.next_before
        let beforeUid = next.next_before_uid
        while (tail && next.messages.length && !next.messages.some(message => message.uid === tail.uid)
          && next.messages[0].timestamp >= tail.timestamp && before) {
          const older = await window.noteford.messages({conversation, limit: 100, before, before_uid: beforeUid || undefined})
          if (cancelled || older.library_id !== libraryId || older.conversation.username !== conversation) return
          next.messages = [...older.messages, ...next.messages]
          if (!older.messages.length || (older.next_before_uid ? older.next_before_uid === beforeUid : older.next_before === before)) break
          before = older.next_before
          beforeUid = older.next_before_uid
        }
        const metadata = new Map(next.messages.map(message => [message.uid, message.payment]))
        const mediaByUid = new Map(next.messages.map(message => [message.uid, message.media]))
        const senders = new Map(next.messages.map(message => [message.uid, message.sender]))
        const records = new Map(next.messages.map(message => [message.uid, message.record_items]))
        setPage(current => {
          if (!current || current.conversation.username !== conversation) return current
          let changed = false
          const messages = current.messages.map(message => {
            const hasState = Object.prototype.hasOwnProperty.call(next.payment_states || {}, message.uid)
            if (!metadata.has(message.uid) && !hasState) return message
            let payment = metadata.has(message.uid) ? metadata.get(message.uid) : message.payment
            if (hasState) payment = {...(payment || {recipient_is_self: null, expires_at: null}), local_state: next.payment_states![message.uid]}
            const media = mediaByUid.has(message.uid) ? mediaByUid.get(message.uid) ?? null : message.media
            const sender = window.noteford.platform === 'darwin' ? senders.get(message.uid) || message.sender : message.sender
            const recordItems = window.noteford.platform === 'darwin' ? records.get(message.uid) : message.record_items
            if (JSON.stringify(message.payment) === JSON.stringify(payment) && JSON.stringify(message.media) === JSON.stringify(media) && JSON.stringify(message.sender) === JSON.stringify(sender) && JSON.stringify(message.record_items) === JSON.stringify(recordItems)) return message
            changed = true
            return {...message, payment, media, sender, ...(window.noteford.platform === 'darwin' ? {record_items: recordItems} : {})}
          })
          const known = new Set(messages.map(message => message.uid))
          const last = messages.at(-1)
          const appended = next.messages.filter(message => !known.has(message.uid) && (!last || message.timestamp >= last.timestamp))
          const conversationInfo = window.noteford.platform === 'darwin' ? next.conversation : current.conversation
          const renamed = JSON.stringify(conversationInfo) !== JSON.stringify(current.conversation)
          return changed || appended.length || renamed ? {...current, conversation: conversationInfo, messages: [...messages, ...appended]} : current
        })
      } catch { /* Keep existing messages; retry without treating failure as unclaimable. */ }
      finally { inFlight = false }
    }
    refreshMessages.current = checkMessageUpdates
    const timer = window.setInterval(() => void checkMessageUpdates(), window.noteford.platform === 'darwin' ? 3000 : 15000)
    const onVisible = () => { if (!document.hidden) void checkMessageUpdates() }
    document.addEventListener('visibilitychange', onVisible)
    return () => { cancelled = true; refreshMessages.current = async () => {}; window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisible) }
  }, [selected?.username, currentLibrary?.id])

  const switchLibrary = useCallback(async (library: LibrarySummary) => {
    if (window.noteford.platform === 'darwin') {
      if (accountSwitching.current) return
      accountSwitching.current = true
      accountEpoch.current++
    }
    if (library.is_current && library.initialized) {
      accountSwitching.current = false
      setLibraryMenuOpen(false)
      return
    }

    messageRequest.current++
    if (window.noteford.platform === 'darwin') {
      setDirectorySearch('')
      setStatus(null)
    }
    setLibraryBusy(true)
    setNavigationAnchor(null)
    setFocusMessageUid(null)
    setSelected(null)
    setPage(null)
    setFollowingRows([])
    setDirectoryRows([])

    try {
      const result = await window.noteford.switchLibrary({
        library_id: library.id,
        initialize: true,
      })
      setStatus(result.status)
      await loadLibraries()
      const rows = await loadFollowing('')
      if (nav === 'contacts') {
        await loadDirectory('')
      }
      if (nav !== 'contacts' && rows.length > 0) await openConversation(rows[0])
      setLibraryMenuOpen(false)
      if (result.initialization_error) {
        setError('已切换资料库，但轻量目录暂未解锁。请先在微信中登录该账号后再刷新。')
      } else {
        setError(null)
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      accountSwitching.current = false
      setLibraryBusy(false)
    }

  }, [nav, loadLibraries, loadFollowing, loadDirectory, openConversation])

  const finishFollowGuide = (enterConversation = true) => {
    if (!currentLibrary) return
    setFollowGuideLibrary(null)
    setNav(enterConversation ? 'following' : 'contacts')
    if (enterConversation && followingRows[0]) void openConversation(followingRows[0])
  }

  const addLibrary = useCallback(async () => {
    setLibraryBusy(true)
    try {
      const root = await window.noteford.pickLibraryRoot()
      if (!root) return
      const before = new Set(libraries.map(item => item.id))
      const rows = await window.noteford.discoverLibraries({ wechat_root: root })
      setLibraries(rows)
      const added = rows.filter(item => !before.has(item.id))
      if (added.length === 1) {
        await switchLibrary(added[0])
      } else if (added.length === 0) {
        setError('这个目录里没有发现新的微信资料库。')
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLibraryBusy(false)
    }
  }, [libraries, switchLibrary])

  const browseForCandidates = useCallback(async () => {
    try {
      const root = await window.noteford.pickLibraryRoot()
      if (root) await scanCandidates(root)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [scanCandidates])

  const chooseCandidate = useCallback(async (candidate: LibraryCandidate) => {
    setLibraryBusy(true)
    setSelectingCandidateId(candidate.id)
    try {
      await window.noteford.registerLibraryCandidate({ library_id: candidate.id })
      const rows = await loadLibraries()
      const selectedLibrary = rows.find(item => item.id === candidate.id)
      if (!selectedLibrary) throw new Error('添加资料库后未找到所选账号')
      await switchLibrary(selectedLibrary)
      const updated = await loadLibraries()
      setNav(updated.find(item => item.id === candidate.id)?.state === 'ready' ? 'contacts' : 'settings')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setLibraryBusy(false)
      setSelectingCandidateId(null)
    }
  }, [loadLibraries, switchLibrary])

  const followRow = useCallback(async (row: Conversation) => {
    setSelectionBusyUsername(row.username)
    try {
      await window.noteford.follow({ username: row.username })
      const { following, directory } = await reloadLists()
      await Promise.all([loadStatus(), loadLibraries()])
      const replacement = directory.find(item => item.username === row.username)
        || following.find(item => item.username === row.username)
        || row
      if (nav !== 'contacts') await openConversation(replacement)
      setError(null)
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause)
      setError(window.noteford.platform === 'darwin'
        ? /FileNotFoundError|source_unavailable/.test(message)
          ? '本机资料暂不可用，请在设置中检查同步状态后重试。'
          : `关注操作未完成，请重试。${message.includes('账号已切换') ? '账号已变化，请重新打开关注管理。' : ''}`
        : message)
    } finally {
      setSelectionBusyUsername(null)
    }
  }, [reloadLists, loadStatus, loadLibraries, openConversation, nav])

  const pauseRow = useCallback(async (row: Conversation) => {
    setSelectionBusyUsername(row.username)
    try {
      await window.noteford.pause({ username: row.username })
      const { following, directory } = await reloadLists()
      await Promise.all([loadStatus(), loadLibraries()])
      const replacement = directory.find(item => item.username === row.username)
        || following.find(item => item.username === row.username)
        || row
      if (nav !== 'contacts' && selected?.username === row.username) {
        await openConversation(replacement)
      }
      setError(null)
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause)
      setError(window.noteford.platform === 'darwin'
        ? /FileNotFoundError|source_unavailable/.test(message)
          ? '本机资料暂不可用，请在设置中检查同步状态后重试。'
          : `关注操作未完成，请重试。${message.includes('账号已切换') ? '账号已变化，请重新打开关注管理。' : ''}`
        : message)
    } finally {
      setSelectionBusyUsername(null)
    }
  }, [reloadLists, loadStatus, loadLibraries, openConversation, selected, nav])

  const changeFollowBatch = useCallback(async (rows: Conversation[], following: boolean, signal: AbortSignal) => {
    const epoch = accountEpoch.current
    const failed: string[] = []
    for (const row of rows) {
      if (signal.aborted || epoch !== accountEpoch.current) {
        failed.push(row.username)
        continue
      }
      try {
        if (following) await window.noteford.follow({username: row.username})
        else await window.noteford.pause({username: row.username})
      } catch {
        failed.push(row.username)
      }
    }
    if (epoch === accountEpoch.current) {
      await Promise.all([reloadLists(), loadStatus(), loadLibraries()])
    }
    return failed
  }, [reloadLists, loadStatus, loadLibraries])

  const refresh = useCallback(async () => {
    if (syncInFlight.current) return
    syncInFlight.current = true
    setRefreshing(true)
    try {
      const result = await window.noteford.refresh()
      setStatus(result.status)
      await loadLibraries()
      await reloadLists()
      await refreshMessages.current()
      setError(null)
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause)
      setError(message.includes('refresh_busy') ? '正在同步，请稍候。' : `同步失败：${message}`)
    } finally {
      syncInFlight.current = false
      setRefreshing(false)
    }
  }, [loadLibraries, reloadLists])

  const history = useNavigationHistory(
    JSON.stringify(['community', 'local', currentLibrary?.id, libraryBusy]),
    JSON.stringify([nav, nav === 'following' ? selected?.username : null, nav === 'following' ? navigationAnchor : null]),
    {nav, selected, page, navigationAnchor},
    destination => {
      messageRequest.current++
      setLoadingMessages(false)
      setLoadingOlder(false)
      setSelected(destination.selected)
      setPage(destination.page)
      setNavigationAnchor(destination.navigationAnchor)
      setFocusMessageUid(destination.nav === 'following' ? destination.navigationAnchor : null)
      setNav(destination.nav)
      setError(null)
      if (destination.nav === 'following' && destination.selected && !destination.page) void openConversation(destination.selected)
    },
  )
  const historyControls = <NavigationHistory {...history} disabled={libraryBusy || !currentLibrary}/>

  const [toolbarHost, setToolbarHost] = useState<HTMLDivElement | null>(null)
  const macLayout = window.noteford?.platform === 'darwin'
  const macPageTitle = macLayout && libraries.length > 0 && !libraryBusy
    ? nav === 'content' ? '素材' : nav === 'contacts' ? '关注管理' : nav === 'settings' ? '设置' : null
    : null
  const macStandalonePage = macPageTitle === '关注管理' || macPageTitle === '素材' || macPageTitle === '设置'
  const showPermissionCover = macLayout && onboardingStep !== 'account'
  const connectCard = presentConnectCard(connectSnap)

  useEffect(() => {
    const accountId = 'community'
    if (!macLayout || !accountId || followGuideChecked.current || !currentLibrary?.initialized
        || libraryBusy || showPermissionCover || status?.library_id !== currentLibrary.id) return
    followGuideChecked.current = true
    try {
      if (claimFirstFollowVisit(localStorage, {
        accountId,
        libraryIds: libraries.map(library => library.id),
        legacyWorkspace: false,
        hasPriorFollows: status.following_conversations > 0 || status.paused_conversations > 0
          || libraries.some(library => (library.following_conversations || 0) > 0),
      })) {
        setFollowGuideLibrary(currentLibrary.id)
        setNav('contacts')
      }
    } catch { /* Unavailable preference storage must not block reading. */ }
  }, [macLayout, 'community', false, currentLibrary, libraries, libraryBusy, showPermissionCover, status])

  useEffect(() => {
    if (followGuideLibrary && (nav !== 'contacts' || currentLibrary?.id !== followGuideLibrary)) {
      setFollowGuideLibrary(null)
    }
  }, [nav, currentLibrary?.id, followGuideLibrary])

  useEffect(() => {
    if (!showPermissionCover) return
    let cancelled = false
    let inFlight = false
    const receive = (guide: PermissionGuideModel) => {
      if (cancelled) return
      setPermissionGuide(guide)
      setGrantUi(state => {
        if (!state.noticeId) return state
        const card = guide.cards.find(item => item.id === state.noticeId)
        if (card?.status === 'granted') return { grantingId: null, notice: '', noticeId: null }
        return state
      })
    }
    const unsubscribe = window.noteford.onPermissionState(receive)
    const refresh = () => {
      if (inFlight) return
      inFlight = true
      void window.noteford.permissionGuide().then(receive).catch(() => {}).finally(() => { inFlight = false })
    }
    refresh()
    const timer = window.setInterval(refresh, 1000)
    window.addEventListener('focus', refresh)
    return () => {
      cancelled = true
      window.clearInterval(timer)
      window.removeEventListener('focus', refresh)
      unsubscribe()
    }
  }, [showPermissionCover, setPermissionGuide])

  useEffect(() => {
    if (!macLayout || onboardingStep === 'permissions') return
    let cancelled = false
    const tick = () => {
      void window.noteford.autoCaptureStatus().then(snap => {
        if (cancelled) return
        setConnectSnap(snap)
        const card = presentConnectCard(snap)
        if (snap.library_id && snap.readable && libraries.length === 0) {
          void loadLibraries().then(() => { if (!cancelled) { setOnboardingStep('account'); setNav('contacts'); void loadDirectory(); void loadStatus().catch(() => {}) } }).catch(() => {})
        }
        if (libraries.length === 0 && onboardingStep === 'account' && snap.needs_scan && !card.canContinue && card.status !== 'blocked') {
          setOnboardingStep('connect')
        }
      }).catch(() => {})
    }
    tick()
    const timer = window.setInterval(tick, onboardingStep === 'connect' ? 1000 : 4000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [macLayout, onboardingStep, libraries.length, loadLibraries, loadDirectory, loadStatus])
  const librarySwitcher = (
    <LibrarySwitcher
      onSettings={() => { setLibraryMenuOpen(false); setNav('settings') }}
      libraries={libraries}
      open={!macLayout && libraryMenuOpen}
      busy={libraryBusy}
      onSwitch={library => void switchLibrary(library)}
      onAdd={() => void addLibrary()}
      avatarSize={macLayout ? 36 : 34}
    />
  )
  const primaryNav = (
    <nav className="rail-nav" aria-label="主要导航">
      <button title="会话" aria-label="会话" className={nav === 'following' ? 'active' : ''} aria-current={nav === 'following' ? 'page' : undefined} onClick={() => setNav('following')}>
        <Icon name="chat" />{macLayout && <span className="mac-nav-label">会话</span>}
      </button>
      <button title="关注管理" aria-label="关注管理" className={nav === 'contacts' ? 'active' : ''} aria-current={nav === 'contacts' ? 'page' : undefined} onClick={() => setNav('contacts')}>
        <Icon name="contacts" />{macLayout && <span className="mac-nav-label">关注</span>}
      </button>
      {!macLayout && <button title={refreshing ? '正在同步所有关注会话…' : '立即同步所有关注会话'} aria-label="立即同步" aria-busy={refreshing} disabled={refreshing || libraryBusy || !currentLibrary} onClick={() => void refresh()}>
        <Icon name="refresh" className={refreshing ? 'sync-spinning' : ''} />
      </button>}
      <button title="内容筛选" aria-label="内容筛选" className={nav === 'content' ? 'active' : ''} aria-current={nav === 'content' ? 'page' : undefined} onClick={() => setNav('content')}>
        <Icon name="filter" />{macLayout && <span className="mac-nav-label">素材</span>}
      </button>
    </nav>
  )

  return (
    <div className="app-frame">
      {showPermissionCover ? (
        <PermissionGuide
          guide={permissionGuide}
          grantingId={grantUi.grantingId}
          grantNotice={grantUi.notice}
          grantNoticeId={grantUi.noticeId}
          step={onboardingStep === 'connect' ? 'connect' : 'permissions'}
          connect={connectCard}
          onGrant={id => void grantPermission(id)}
          onRestart={() => {
            void window.noteford.restartForPermissions().catch(() => {
              setGrantUi({ grantingId: null, notice: '请退出后重新打开 Noteford。', noticeId: 'full_disk' })
            })
          }}
          onStartScan={() => void startWechatScan()}
          onStep={next => { if (next === 'permissions') setOnboardingStep('permissions') }}
          onContinue={() => void continueFromPermissions()}
        />
      ) : <>
      {macLayout ? (
        <div className="mac-sidebar">
          {primaryNav}
          <div className="rail-nav mac-sync-nav">
            <SidebarSync sync={status?.library_id === currentLibrary?.id ? status?.sync : undefined}
              refreshing={refreshing} disabled={libraryBusy || !currentLibrary} onSync={() => void refresh()} />
          </div>
          {librarySwitcher}
        </div>
      ) : null}
      <div className={`app-shell${macStandalonePage ? ' mac-follow-management' : ''}`}>
      <header className="app-toolbar">
        {macLayout ? !macStandalonePage && <div className="toolbar-leading">{historyControls}<h1>会话</h1><span className="mac-following-count">{followingRows.length} 个会话</span></div> : primaryNav}
        <div className={`conversation-toolbar${macPageTitle ? ' mac-page-toolbar' : ''}`} ref={setToolbarHost}>
          {!macLayout && historyControls}
          {macPageTitle && <div className="workspace-title-group">{historyControls}<h1 id="workspace-page-title">{macPageTitle}</h1></div>}
        </div>
        {macLayout ? null : <div className="window-controls-space" aria-hidden="true" />}
      </header>
      <aside className="rail">
        <ConversationList
          rows={followingRows}
          selected={nav === 'following' ? selected?.username || null : null}
          onSelect={row => { setNav('following'); void openConversation(row) }}
          loading={loadingFollowing}
        />
        {macLayout ? null : <div className="rail-bottom">{librarySwitcher}</div>}
      </aside>

      {macLayout && libraryBusy ? <main className="library-onboarding"><p role="status">正在切换账号…</p></main> : libraries.length === 0 || selectingCandidateId !== null ? (
        <main className="library-onboarding" data-scan-complete={candidateScanComplete}>
          <div className="library-onboarding-card">
            <div className="library-onboarding-heading">
              <div>
                <h1>选择要读取的微信账号</h1>
                <p>{onboardingCopy.intro}</p>
              </div>
              <button type="button" onClick={() => void scanCandidates()} disabled={scanningLibraries || libraryBusy}>
                重新查找
              </button>
            </div>
            {scanningLibraries ? <p className="library-scan-state">正在查找账号并读取头像与昵称，请稍候…</p> : null}
            {!scanningLibraries && candidateScanComplete && libraryCandidates.length > 0 ? (
              <p className="library-scan-state">找到 {libraryCandidates.length} 个账号，已读到 {libraryCandidates.filter(item => item.preview_status !== 'unavailable').length} 个昵称。</p>
            ) : null}
            {!scanningLibraries && candidateScanComplete && libraryCandidates.length === 0 ? (
              <p className="library-scan-state">{onboardingCopy.empty}</p>
            ) : null}
            <div className="library-candidate-list">
              {libraryCandidates.map(candidate => (
                <button
                  type="button"
                  className="library-candidate"
                  key={candidate.id}
                  onClick={() => void chooseCandidate(candidate)}
                  disabled={libraryBusy || scanningLibraries}
                >
                  <Avatar name={candidate.display_name} id={candidate.id} src={candidate.avatar_data_url} size={54} />
                  <span className="library-candidate-copy">
                    <strong>{candidate.display_name}</strong>
                    <small>{candidate.account}</small>
                    {candidate.preview_status === 'unavailable' ? (
                      <em>暂未读到头像和昵称，可登录该账号后重新查找</em>
                    ) : null}
                  </span>
                  <span className="library-candidate-action">{selectingCandidateId === candidate.id ? '正在建立…' : <>选择 <Icon name="forward" size={15} /></>}</span>
                </button>
              ))}
            </div>
            <button type="button" className="library-browse" onClick={() => void browseForCandidates()} disabled={scanningLibraries || libraryBusy}>
              没找到想要的账号？手动选择微信数据目录
            </button>
          </div>
        </main>
      ) : nav === 'contacts' ? (
        <ContactDirectory
          key={currentLibrary?.id || ''}
          onBatch={changeFollowBatch}
          guide={macLayout && followGuideLibrary === currentLibrary?.id ? {name: currentLibrary?.display_name || '微信账号', following: status?.following_conversations || 0, reading: connectSnap?.running || status?.sync?.state === 'running', onDone: () => finishFollowGuide(), onSkip: () => finishFollowGuide(false)} : undefined}
          rows={directoryRows}
          search={directorySearch}
          loading={loadingDirectory}
          busyUsername={selectionBusyUsername}
          onSearch={setDirectorySearch}
          onFollow={row => void followRow(row)}
          onPause={row => void pauseRow(row)}
        />
      ) : nav === 'content' ? (
        <ContentView key={currentLibrary?.id || ''} cache={contentCache} viewState={contentViewState} onNavigateSource={navigateToContentSource} toolbarHost={macLayout ? toolbarHost : undefined} />
      ) : nav === 'settings' ? (
        <SettingsView
          parseStickers={parseStickers}
          onStickerParsingChange={changeStickerParsing}
          messageVisibility={messageVisibility}
          onMessageVisibilityChange={changeMessageVisibility}
          onManageAccounts={() => setLibraryMenuOpen(value => !value)}
          status={status}
          library={currentLibrary}
          libraryBusy={libraryBusy}
          refreshing={refreshing}
          onRefresh={() => void refresh()}
          onUnlock={() => currentLibrary && void switchLibrary(currentLibrary)}
        />
      ) : (
        <ChatView
          parseStickers={parseStickers === true}
          messageVisibility={messageVisibility}
          focusMessageUid={focusMessageUid}
          onFocusHandled={() => setFocusMessageUid(null)}
          key={currentLibrary?.id}
          libraryId={currentLibrary?.id}
          toolbarHost={toolbarHost}
          page={page}
          loading={loadingMessages}
          loadingOlder={loadingOlder}
          onLoadOlder={loadOlder}
        />
      )}

      </div>
      </>}
      {error ? (
        <div className="error-toast" onClick={() => setError(null)}>
          {error}
        </div>
      ) : null}
    </div>
  )
}
