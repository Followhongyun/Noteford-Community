'use strict';

// Legacy Mac emoji records still contain HTTP URLs. Only these known image
// hosts are upgraded; never disable TLS checks or fall back to cleartext.
const legacyImageHosts = new Set(['wxapp.tc.qq.com', 'vweixinf.tc.qq.com', 'mmbiz.qpic.cn']);
function normalizeMacMediaUrl(value) {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.username || url.password || url.port) return null;
    if (url.protocol === 'http:' && legacyImageHosts.has(url.hostname)) url.protocol = 'https:';
    return url.protocol === 'https:' ? url.toString() : null;
  } catch { return null; }
}
module.exports = {normalizeMacMediaUrl};
