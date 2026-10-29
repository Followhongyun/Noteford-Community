import {
  CircleAlert, CirclePlay, BookOpen, Copy, Clock, Eye, EyeOff, Download, ExternalLink, MessageSquareText, ArrowLeft, ArrowRight, ChevronRight, ClipboardList, MapPin, Shield, Radio, UserRound, LayoutList, CircleCheck, LoaderCircle, Check, FileText, Filter, Gift, Image, Link,
  MessageCircle, Mic, Play, Plus, RefreshCw, Search, Settings, Users, Video, Wallet, X,
  type LucideIcon,
} from 'lucide-react'

const icons = {
  alert: CircleAlert,
  copy: Copy,
  clock: Clock,
  eye: Eye,
  eyeOff: EyeOff,
  download: Download,
  external: ExternalLink,
  source: MessageSquareText,
  back: ArrowLeft,
  forward: ArrowRight,
  chevron: ChevronRight,
  solitaire: ClipboardList,
  location: MapPin,
  permission: Shield,
  official: Radio,
  publication: BookOpen,
  person: UserRound,
  list: LayoutList,
  followed: CircleCheck,
  loading: LoaderCircle,
  chat: MessageCircle,
  check: Check,
  close: X,
  contacts: Users,
  file: FileText,
  filter: Filter,
  gift: Gift,
  group: Users,
  image: Image,
  link: Link,
  play: Play,
  circlePlay: CirclePlay,
  add: Plus,
  refresh: RefreshCw,
  search: Search,
  settings: Settings,
  video: Video,
  voice: Mic,
  wallet: Wallet,
} satisfies Record<string, LucideIcon>

export default function Icon({ name, size = 22, className = '' }: { name: keyof typeof icons; size?: number; className?: string }) {
  const Component = icons[name]
  return <Component className={`icon ${className}`} size={size} strokeWidth={1.7} aria-hidden="true" data-icon-source="lucide" />
}
