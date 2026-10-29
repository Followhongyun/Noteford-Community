const {test} = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const {stripTypeScriptTypes} = require('node:module')
const source = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/follow-guide.ts'), 'utf8'))
const guide = import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))
const input = {accountId: 'user-a', libraryIds: ['library-a'], legacyWorkspace: false, hasPriorFollows: false}
function store(entries = []) {
  const values = new Map(entries)
  return {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)}
}

test('first visit is consumed even without completion; another library does not repeat it', async () => {
  const {claimFirstFollowVisit: claim} = await guide
  const storage = store()
  assert.equal(claim(storage, input), true)
  assert.equal(claim(storage, input), false)
  assert.equal(claim(storage, {...input, libraryIds: ['library-b']}), false)
  assert.equal(claim(storage, {...input, accountId: 'user-b'}), true)
})

test('existing follows and old completion markers migrate without showing the introduction', async () => {
  const {claimFirstFollowVisit: claim} = await guide
  const storage = store()
  assert.equal(claim(storage, {...input, hasPriorFollows: true}), false)
  assert.equal(claim(storage, input), false) // Even after unfollowing everything.
  assert.equal(claim(store([['noteford.mac-follow-guide.v1:user-a:library-a', 'done']]), input), false)
})

test('legacy completion belongs only to its migrated workspace', async () => {
  const {claimFirstFollowVisit: claim} = await guide
  const entries = [['noteford.mac-follow-guide.v1:library-a', 'done']]
  assert.equal(claim(store(entries), input), true)
  assert.equal(claim(store(entries), {...input, legacyWorkspace: true}), false)
})
