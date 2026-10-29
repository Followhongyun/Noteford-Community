export type SelectionState = 'unselected' | 'following' | 'paused'
export type NavSection = 'following' | 'contacts' | 'content' | 'settings'
export interface LibrarySummary {
  id: string
  display_name: string
  state: 'discovered' | 'ready' | 'locked' | string
  is_current: boolean
  initialized: boolean
  last_error: string | null
  source_root: string
  messages?: number
  conversations?: number
  following_conversations?: number
  avatar_url?: string | null
}

export interface LibraryCandidate {
  id: string
  account: string
  source_root: string
  display_name: string
  avatar_data_url: string | null
  preview_status: 'ready' | 'name' | 'unavailable'
}

export interface AppStatus {
  wechat_login?: {state: 'active' | 'not_running' | 'unknown'; reason: string; checked_at: string | null; network_online: null}
  sync?: {
    state: string; error?: string | null; last_success_at?: string | null; last_triggered_at?: string | null; next_at?: string | null
    scheduler_running?: boolean; interval_minutes?: number; interval_seconds?: number; scheduler_error?: string | null
    stage?: string; failed_conversations?: number; started_at?: string | null
    recent_receipts?: {trigger: string; started_at: string; finished_at: string; duration_seconds: number; outcome: string; failed_conversations: number}[]
  }
  avatar_sync?: {completed_at: string; checked: number; available: number; recovered: number; missing: number} | null
  account: string
  library_id: string | null
  library_display_name: string
  library_state: string
  messages: number
  conversations: number
  contacts: number
  following_conversations: number
  paused_conversations: number
  managed_messages: number
  legacy_unselected_messages: number
  latest_timestamp: number
  latest_time: string | null
  self_identity_resolved: boolean
  self_username?: string | null
  self_display_name: string | null
  self_confidence: number
  archive_last_success_at?: string | null
  archive_message_databases?: number
  recent_media_ready?: number
  recent_media_limited?: number
  media_downloads?: {total: number; downloaded: number; not_downloaded: number; expired: number} | null
}

export interface AppSettings {
  auto_media_max_mb: number
  auto_media_loading: boolean
  parse_stickers: boolean
  sync_interval_seconds: number
  sync_interval_minutes: number
  media_prefetch_days: number
  restart_required?: boolean
}


export interface SelectionStatus {
  state: SelectionState
  has_local_messages: boolean
  local_message_count: number
  source_dbs: string[]
  selected_at: string | null
  updated_at: string | null
  last_refresh_at: string | null
  last_error: string | null
}

export interface Conversation {
  username: string
  chat_table: string
  name: string
  is_group: boolean
  contact_kind?: 'groups' | 'official' | 'people' | null
  message_count: number
  latest_timestamp: number
  latest_time: string | null
  latest_kind: string | null
  preview: string
  avatar_url?: string | null
  selection_state: SelectionState
  has_local_messages: boolean
}

export interface Message {
  record_items?: Message[]
  solitaire_key?: string | null
  payment?: { recipient_is_self: boolean | null; expires_at: number | null; local_state?: 'received' | 'exhausted' | 'expired' | null } | null
  uid: string
  timestamp: number
  time: string | null
  sender: {
    username: string | null
    name: string | null
    from_me: boolean
    avatar_url?: string | null
  }
  type: {
    msg_type: number
    sub_type: number
    kind: string
  }
  text: string
  title: string | null
  url: string | null
  quote: {
    server_id?: string | null
    state?: 'available' | 'unavailable'
    target?: Message | null
    kind?: string | null
    text?: string | null
    sender_name?: string | null
    media_md5?: string | null
    timestamp?: number | null
  } | null
  card: {
    thumbnail_url?: string | null
    cover_url?: string | null
    avatar_url?: string | null
    source_display_name?: string | null
    author_username?: string | null
  } | null
  media: {
    md5: string | null
    status?: string
    state?: string
    kind?: string
    url?: string | null
    bytes?: number | null
    encrypted?: boolean
    note?: string | null
    original_status?: string | null
    download_error?: string | null
  } | null
  status: number | null
}

export interface MessagePage {
  payment_states?: Record<string, 'received' | 'exhausted' | 'expired' | null>
  library_id?: string
  conversation: {
    username: string
    name: string
    is_group: boolean
    avatar_url?: string | null
  }
  messages: Message[]
  next_before_uid?: string | null
  next_before: number | null
  next_offset?: number | null
  selection: SelectionStatus
}

export interface Contact {
  username: string
  alias: string | null
  remark: string | null
  nick_name: string | null
  name: string
  is_group: boolean
  avatar_url?: string | null
  chat_table?: string | null
  can_follow: boolean
  selection_state: SelectionState
}

export interface GroupMember {
  username: string
  name: string
  avatar_url?: string | null
}

export interface GroupMemberPage {
  available: boolean
  members: GroupMember[]
}

export interface McpConnection {
  allow_sync?: boolean
  scope?: 'selected' | 'all_synced' | 'all_indexed'
  id: string
  label: string
  library_id: string
  conversations: string[]
  enabled: boolean
  created_at: string
}

export interface McpPairBinding { id: string; client_name: string; program: string; grant_client: string; created_at: number; last_used_at: number | null; active: boolean }
export interface RemoteMcpStatus { enabled: boolean; public_url: string; port: number; running: boolean; mcp_url: string; error: string }
export interface RemoteMcpPending { display_id: string; client_name: string; created_at: number; expires_at: number }
export interface RemoteMcpGrant { id: string; client_name: string; grant_client: string; created_at: number; active: boolean }

export interface MediaRange { library_id: string; conversation: string; start: string; end: string | null; automatic?: {running: boolean; completed: number; waiting: number; failures?: {uid: string; timestamp: number; reason: string; code: string; attempts: number; paused: boolean}[]} }

export interface PermissionCard {
  id: string
  title: string
  detail: string
  restartNote: string
  gapNote?: string
  identityNote?: string
  canRestart?: boolean
  required: boolean
  optionalLabel: string
  status: 'granted' | 'not_granted' | 'restart_required'
  actionLabel: string
}

export interface PermissionGuide {
  revision?: number
  active: boolean
  loading: boolean
  title: string
  intro: string
  cards: PermissionCard[]
  canContinue: boolean
  continueLabel: string
  hint: string
  steps: { id: string; label: string }[]
}

export interface WechatConnectCard {
  pageTitle: string
  intro: string
  title: string
  detail: string
  risk: string
  actionLabel: string
  status: 'ready' | 'waiting' | 'captured' | 'verifying' | 'done' | 'failed' | 'blocked' | 'error'
  canContinue: boolean
  canStart: boolean
  busy: boolean
}

export interface AutoCaptureSnapshot {
  running?: boolean
  library_id?: string | null
  readable?: boolean
  already_added?: boolean
  ok?: boolean
  phase?: string
  error?: string | null
  needs_scan?: boolean
  keys_already_valid?: boolean
  scan_status?: string
  stale_reason?: string | null
  started?: boolean
  notes?: string[]
}

export interface MacAccountsSnapshot {
  follow_wechat: boolean
  active_library_id: string | null
  suggested_library_id: string | null
  login_state: 'active' | 'unknown' | 'not_running'
  accounts: {id: string; account: string; display_name: string; library_id: string | null; logged_in: boolean; connected: boolean}[]
  capture: AutoCaptureSnapshot
}

export interface UpdateState {
  status: 'idle' | 'unavailable' | 'checking' | 'current' | 'downloading' | 'preparing' | 'ready' | 'error'
  version: string
  availableVersion: string | null
  progress: number
  error: string | null
}

declare global {
  interface Window {
    noteford: {
      platform: string
      updateStatus(): Promise<UpdateState>
      checkForUpdates(): Promise<UpdateState>
      installUpdate(): Promise<void>
      onUpdateState(callback: (state: UpdateState) => void): () => void
      nativeCorners?(input: {width: number; height: number; radius: number; scale: number; stroke: number; corners: number}): Promise<{outer: string; stroke: string | null; renderer: 'macos-continuous'} | null>
      hostProfile(): Promise<{
        platform: string
        onboarding: {
          intro: string
          empty: string
          permissions: { pending: boolean; autoRequest: boolean; title: string; message: string }
        }
      }>
      permissionGuide(): Promise<PermissionGuide>
      restartForPermissions(): Promise<{ ok: boolean }>
      onPermissionState(callback: (guide: PermissionGuide) => void): () => void
      grantPermission(input: { id: string }): Promise<{ ok: boolean; status?: 'granted' | 'not_granted'; skipped?: string; guidance?: string; openedSettings?: boolean; timedOut?: boolean }>
      completePermissionGuide(): Promise<{ saved: boolean; skipped?: string }>
      autoCaptureStatus(): Promise<AutoCaptureSnapshot>
      autoCaptureStart(input?: { force?: boolean }): Promise<AutoCaptureSnapshot>
      mcpConnectionStatus(): Promise<{ enabled: boolean }>
      configureMcpConnection(input: { action: 'disconnect' | 'reconnect' }): Promise<{ enabled: boolean }>
      mcpConnections(): Promise<McpConnection[]>
      macAccounts(): Promise<MacAccountsSnapshot>
      configureMacAccounts(input: {follow_wechat: boolean}): Promise<MacAccountsSnapshot>
      cancelMacAccount(): Promise<{ok: boolean}>
      connectMacAccount(input: {library_id?: string}): Promise<AutoCaptureSnapshot>
      macAiStatus(): Promise<{ enabled: boolean; library_id: string | null; scope?: string }>
      configureMacAi(input: { enabled: boolean; library_id: string }): Promise<{ enabled: boolean; library_id: string | null; scope?: string }>
      copyMacAiConnection(input: { format: 'mcp' | 'cli' }): Promise<void>
      createMcpConnection(input: { allow_sync?: boolean; scope?: 'selected' | 'all_synced' | 'all_indexed'; label: string; library_id: string; conversations: string[] }): Promise<McpConnection>
      revokeMcpConnection(input: { id: string }): Promise<void>
      copyMcpConfig(input: { id: string }): Promise<void>
      exportMcpPlugin(input: { id: string }): Promise<string | null>
      skillInstallCommand(input?: { copy?: boolean }): Promise<{ command: string; copied: boolean }>
      copyMcpPrompt(): Promise<{ copied: boolean }>
      mcpPairBindings(): Promise<McpPairBinding[]>
      revokeMcpPairBinding(input: { id: string }): Promise<{ ok: boolean }>
      remoteMcpStatus(): Promise<RemoteMcpStatus>
      configureRemoteMcp(input: { enabled: boolean; public_url: string }): Promise<RemoteMcpStatus>
      remoteMcpPairCode(): Promise<{ code: string; expires_at: number }>
      remoteMcpPending(): Promise<RemoteMcpPending[]>
      remoteMcpApprove(input: { display_id: string; grant_client: string }): Promise<{ ok: boolean }>
      remoteMcpDeny(input: { display_id: string }): Promise<{ ok: boolean }>
      remoteMcpGrants(): Promise<RemoteMcpGrant[]>
      remoteMcpRevoke(input: { id: string }): Promise<{ ok: boolean }>
      status(): Promise<AppStatus>
      saveAutoMediaLimit(maxMb: number): Promise<AppSettings>
      saveAutoMediaLoading(enabled: boolean): Promise<AppSettings>
      saveStickerParsing(enabled: boolean): Promise<AppSettings>
      settings(): Promise<AppSettings>
      saveSyncIntervalSeconds(seconds: number): Promise<AppSettings>
      saveSyncInterval(minutes: number): Promise<AppSettings>
      libraries(): Promise<LibrarySummary[]>
      libraryCandidates(input?: { wechat_root?: string | null }): Promise<LibraryCandidate[]>
      registerLibraryCandidate(input: { library_id: string }): Promise<LibrarySummary>
      switchLibrary(input: { library_id: string; initialize?: boolean }): Promise<{
        library: LibrarySummary
        status: AppStatus
        initialized_now: boolean
        initialization_error: string | null
      }>
      renameLibrary(input: { library_id: string; display_name: string }): Promise<LibrarySummary>
      discoverLibraries(input?: { wechat_root?: string | null }): Promise<LibrarySummary[]>
      pickLibraryRoot(): Promise<string | null>
      conversations(input?: {
        limit?: number
        offset?: number
        query?: string
        state?: SelectionState
      }): Promise<Conversation[]>
      content(input: {link_messages?: boolean; conversation?: string; category?: string; query?: string; offset?: number; limit?: number}): Promise<ContentPage>
      messages(input: { conversation: string; limit?: number; before?: number; before_uid?: string; around_uid?: string; after_uid?: string; at_time?: number; category?: string; query?: string; from_day?: number; to_day?: number; sender?: string; filter_offset?: number }): Promise<MessagePage>
      groupMembers(input: { conversation: string }): Promise<GroupMemberPage>
      avatars(input: {library_id: string; usernames: string[]}): Promise<{username: string; avatar_url: string}[]>
      mediaProgress(input: {library_id: string; request_id: string}): Promise<{received: number; total: number | null; state: string}>
      recheckMedia(input: {library_id: string; uid: string; request_id?: string}): Promise<Message['media']>
      mediaRange(input: {library_id: string; conversation: string}): Promise<MediaRange>
      saveMediaRange(input: MediaRange): Promise<MediaRange>
      saveMediaWindow(input: { days: number }): Promise<AppSettings>
      contacts(input?: { limit?: number; offset?: number; query?: string }): Promise<Contact[]>
      openExternal(input: { url: string }): Promise<boolean>
      saveMedia?(input: {url: string; name?: string}): Promise<boolean>
      revealMedia?(input: {url: string}): Promise<boolean>
      follow(input: { username: string }): Promise<{
        selection: SelectionStatus
        refresh: Record<string, unknown>
      }>
      pause(input: { username: string }): Promise<SelectionStatus>
      refresh(): Promise<{ result: unknown; status: AppStatus }>
    }
  }
}

export {}

export interface ContentSource {is_group?: boolean; sender_username?: string | null; message_uid:string; conversation_id:string; conversation_name:string; sender:string; timestamp:number}
export interface ContentItem {id:string; category:string; title:string; text:string; url:string|null; availability:string; sources:ContentSource[]}
export interface ContentPage {items:ContentItem[];total:number;counts:Record<string,number>;next_offset:number|null}
