const {test} = require('node:test');
const assert = require('node:assert/strict');
const {remoteAvatar} = require('../electron/mac-avatar.cjs');

test('Mac avatar source is lazy, library scoped, and limited to known avatar hosts', () => {
  const calls = [];
  const register = (url, library) => { calls.push({url, library}); return 'noteford-remote://asset/test'; };
  const value = {username: 'person', avatar_path: null, avatar_remote_url: 'https://wx.qlogo.cn/a', avatar_library_id: 'lib-test'};
  assert.equal(remoteAvatar(value, register).avatar_url, 'noteford-remote://asset/test');
  assert.deepEqual(calls, [{url:'https://wx.qlogo.cn/a', library:'lib-test'}]);
  for (const url of ['http://wx.qlogo.cn/a', 'https://wx.qlogo.cn.evil.test/a', 'https://user:pass@wx.qlogo.cn/a', 'https://wx.qlogo.cn:444/a']) {
    assert.equal(remoteAvatar({...value, avatar_remote_url:url}, register).avatar_url, null);
  }
  assert.equal(remoteAvatar({...value, avatar_library_id:'lib-test/../../other'}, register).avatar_url, null);
  assert.equal(calls.length, 1);
  const local = {...value, avatar_path:'/local/avatar.png'};
  assert.equal(remoteAvatar(local, register), local);
});
