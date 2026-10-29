const test = require('node:test');
const assert = require('node:assert/strict');
const { createLocalConsent } = require('../desktop/electron/local-consent.cjs');

function fixture(confirm, fail = false) {
  const calls = [];
  let pending = true;
  const request = { request_id: 'r1', client_name: 'Codex', program: 'C:/Codex.exe', grant_client: null };
  const options = {
    pairing: {
      requests: async () => pending ? [{ request_id: 'r1' }] : [],
      review: async () => { pending = false; return request; },
      approve: async (...args) => { calls.push(['approve', ...args]); if (fail) throw Error('expired'); },
      deny: async id => calls.push(['deny', id]),
    },
    connections: { create: input => { calls.push(['create', input]); return { id: 'grant' }; }, revoke: id => calls.push(['revoke', id]) },
    listLibraries: async () => [{ id: 'original', is_current: true }],
    confirm, onError: () => calls.push(['error']),
  };
  return { calls, options };
}
test('denial never creates a grant', async () => {
  const f = fixture(async () => false);
  await createLocalConsent(f.options)();
  assert.deepEqual(f.calls, [['deny', 'r1']]);
});
test('account is captured before confirmation and watcher retries do not duplicate dialogs', async () => {
  let answer;
  let prompts = 0;
  const f = fixture(async (_, library) => { prompts++; assert.equal(library.id, 'original'); return new Promise(resolve => { answer = resolve; }); });
  const refresh = createLocalConsent(f.options);
  const first = refresh();
  await new Promise(setImmediate);
  f.options.listLibraries = async () => [{ id: 'different', is_current: true }];
  await refresh();
  answer(true);
  await first;
  assert.equal(prompts, 1);
  assert.equal(f.calls[0][1].library_id, 'original');
  assert.deepEqual(f.calls[1], ['approve', 'r1', 'grant']);
});
test('expired approval revokes a newly created grant', async () => {
  const f = fixture(async () => true, true);
  await createLocalConsent(f.options)();
  assert(f.calls.some(call => call[0] === 'revoke' && call[1] === 'grant'));
});
