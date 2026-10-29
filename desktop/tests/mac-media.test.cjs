const {test} = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const {stripTypeScriptTypes} = require('node:module')
const source = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/macMediaState.ts'), 'utf8'))
const state = import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))
const {normalizeMacMediaUrl} = require('../electron/mac-remote-media.cjs')

test('Mac emoji HTTP references upgrade only exact known hosts and never weaken TLS', () => {
  assert.equal(normalizeMacMediaUrl('http://wxapp.tc.qq.com/emoji?token=fixture'), 'https://wxapp.tc.qq.com/emoji?token=fixture')
  assert.equal(normalizeMacMediaUrl('https://mmbiz.qpic.cn/image'), 'https://mmbiz.qpic.cn/image')
  for (const value of ['http://wxapp.tc.qq.com.evil.test/image','http://example.com/image','file:///etc/passwd',
    'http://user:pass@wxapp.tc.qq.com/image','https://wxapp.tc.qq.com:444/image','javascript:alert(1)']) {
    assert.equal(normalizeMacMediaUrl(value), null)
  }
})

test('Mac local originals supersede thumbnails and previous expiry', async () => {
  const {macMediaState} = await state
  for (const kind of ['image', 'video']) {
    const result = macMediaState(kind, {url:'noteford-media://original', status:'ready', download_error:'cdn_expired'})
    assert.equal(result.full, true)
    assert.equal(result.expired, false)
    assert.equal(result.notice, '')
  }
})
test('Mac missing originals retain thumbnails without inventing expiry or deletion', async () => {
  const {macMediaState} = await state
  for (const [kind, status] of [['image','ready_thumbnail'], ['video','thumbnail_only']]) {
    for (const error of ['local_original_unavailable', 'cdn_not_found', 'network_unavailable']) {
      const result = macMediaState(kind, {url:'noteford-media://preview', status, download_error:error})
      assert.equal(result.thumbnail, true)
      assert.equal(result.expired, false)
      assert.match(result.notice, /已显示缩略图/)
      assert.doesNotMatch(result.notice, /过期|清理|删除/)
    }
  }
})
test('Mac only explicit server expiry uses expired wording, thumbnail remains viewable', async () => {
  const {macMediaState} = await state
  const result = macMediaState('image', {url:'noteford-media://preview',status:'ready_thumbnail',download_error:'cdn_expired'})
  assert.equal(result.thumbnail, true)
  assert.equal(result.expired, true)
  assert.match(result.notice, /图片已过期或已被清理/)
})
test('Mac missing preview and decoding/storage failures stay distinguishable', async () => {
  const {macMediaState} = await state
  const missing = macMediaState('image', null)
  assert.equal(missing.thumbnail, false)
  assert.match(missing.notice, /原图暂不可用/)
  assert.match(macMediaState('image', {download_error:'local_decryption_unavailable'}).notice, /解密/)
  assert.match(macMediaState('video', {download_error:'insufficient_space'}).notice, /空间/)
})
