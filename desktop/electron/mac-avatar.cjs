'use strict';

const avatarHosts = new Set([
  'wx.qlogo.cn', 'thirdwx.qlogo.cn', 'mmhead.c2c.wechat.com',
  'mmhead.hk.wechat.com', 'wework.qpic.cn', 'wwcdn.weixin.qq.com',
]);

function remoteAvatar(value, register) {
  if (!value?.avatar_remote_url || value.avatar_path) return value;
  let source = null;
  try {
    const url = new URL(value.avatar_remote_url);
    if (url.protocol === 'https:' && avatarHosts.has(url.hostname)
      && !url.username && !url.password && !url.port
      && /^lib-[a-zA-Z0-9_-]+$/.test(value.avatar_library_id || '')) {
      source = register(url.toString(), value.avatar_library_id);
    }
  } catch { /* Invalid source stays a placeholder. */ }
  return {...value, avatar_url: source, avatar_path: undefined,
    avatar_remote_url: undefined, avatar_library_id: undefined};
}

module.exports = {remoteAvatar};
