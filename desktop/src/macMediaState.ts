import type {Message} from './types'

// Mac message reads already try local originals before returning a thumbnail.
// A missing local file is never evidence that Tencent has expired it.
export function macMediaState(kind: string, media: Message['media']) {
  const image = kind === 'image'
  const full = media?.status === 'ready' && !!media.url
  const thumbnail = !!media?.url && ['ready_thumbnail', 'thumbnail_only'].includes(media.status || '')
  const expired = !full && media?.download_error === 'cdn_expired'
  const name = image ? '图片' : '视频'
  const original = image ? '原图' : '完整视频'
  const reasons: Record<string, string> = {
    account_mismatch: '请登录对应微信账号后重试',
    session_unavailable: '请登录微信后重试',
    missing_download_reference: '消息缺少原件下载信息',
    local_decryption_unavailable: `${original}暂无法解密`,
    local_media_unreadable: `${original}暂无法读取`,
    unsupported_download_format: `${original}格式暂不支持`,
    insufficient_space: '磁盘空间不足',
    storage_unavailable: '暂无法保存文件',
    too_large: '文件超过读取上限',
    network_unavailable: '网络连接失败',
    download_timeout: '加载超时',
    download_busy: '文件正在处理',
    integrity_failed: '文件校验失败',
    invalid_response: '文件数据异常',
  }
  const reason = full ? '' : expired ? `${name}已过期或已被清理`
    : reasons[media?.download_error || ''] || `${original}暂不可用`
  return {full, thumbnail, expired, name, reason,
    caption: thumbnail ? '缩略图' : name,
    notice: full ? '' : `${thumbnail ? '已显示缩略图；' : ''}${reason}${expired ? '。' : '，可稍后重试。'}`}
}
