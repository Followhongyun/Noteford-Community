const {test} = require('node:test');
const assert = require('node:assert/strict');
const {parseRange} = require('../electron/media-range.cjs');

test('video head, seek and tail byte ranges', () => {
  assert.deepEqual(parseRange('bytes=0-31', 100), {start: 0, end: 31});
  assert.deepEqual(parseRange('bytes=50-', 100), {start: 50, end: 99});
  assert.deepEqual(parseRange('bytes=-20', 100), {start: 80, end: 99});
  assert.deepEqual(parseRange('bytes=90-150', 100), {start: 90, end: 99});
  assert.equal(parseRange(null, 100), null);
});
test('invalid ranges are rejected, never silently clamped to unrelated bytes', () => {
  for (const value of ['bytes=100-', 'bytes=20-10', 'bytes=-0', 'bytes=-', 'bytes=9007199254740992-', 'invalid']) {
    assert.equal(parseRange(value, 100), false);
  }
  assert.equal(parseRange('bytes=0-', 0), false);
});
