const {test} = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const {stripTypeScriptTypes} = require('node:module')
const source = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/sync-countdown.ts'), 'utf8'))
const formatter = import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))

test('countdown uses minutes, adds hours when needed and follows the same deadline after resume', async () => {
  const {syncCountdown} = await formatter
  const now = Date.parse('2026-10-20T01:00:00Z')
  for (const [minutes, expected] of [[1, '01:00'], [5, '05:00'], [10, '10:00'], [59, '59:00'], [60, '1:00:00'], [65, '1:05:00'], [1440, '24:00:00']]) {
    assert.equal(syncCountdown(new Date(now + minutes * 60000).toISOString(), now), expected)
  }
  assert.equal(syncCountdown(new Date(now + 599000).toISOString(), now), '09:59')
  const deadline = new Date(now + 5 * 60000).toISOString()
  assert.equal(syncCountdown(deadline, now + 60000), '04:00')
  assert.equal(syncCountdown(deadline, now + 299999), '00:01')
  assert.equal(syncCountdown(deadline, now + 300000), '00:00')
  assert.equal(syncCountdown(deadline, now + 3600000), '00:00')
})

test('missing and invalid schedules never invent a countdown', async () => {
  const {syncCountdown} = await formatter
  for (const deadline of [undefined, null, '', 'invalid']) {
    assert.equal(syncCountdown(deadline, Date.now()), '--:--')
  }
})
