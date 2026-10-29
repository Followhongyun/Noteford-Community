const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const { waitForRenderer } = require('../desktop/scripts/renderer-ready.cjs');
function child() { return Object.assign(new EventEmitter(), { stdout:new PassThrough(), stderr:new PassThrough(), exitCode:null, killed:false }); }
for (const colored of [false, true]) {
  const text = colored ? 'Local: http://127.0.0.1:\x1b[1m5187\x1b[22m/\n' : 'Local: http://127.0.0.1:5187/\n';
  test(`ready with color=${colored}, every possible chunk boundary`, async () => {
    for (let split=0; split<text.length; split++) {
      const c=child(), ready=waitForRenderer(c,{timeoutMs:100});
      c.stdout.write(text.slice(0,split)); c.stdout.write(text.slice(split)); await ready;
    }
  });
}
test('unready and other URLs time out', async () => {
  const c=child(), ready=waitForRenderer(c,{timeoutMs:20});
  c.stdout.write('ready http://127.0.0.1:51870/');
  await assert.rejects(ready,/timed out/);
});
test('port conflict preserves actual child error', async () => {
  const c=child(), ready=waitForRenderer(c);
  c.stderr.write('Error: Port 5187 is already in use'); c.exitCode=1; c.emit('exit',1,null);
  await assert.rejects(ready,/code 1.*Port 5187 is already in use/);
});
test('spawn error and premature exit fail immediately', async () => {
  for (const event of ['error','exit']) {
    const c=child(), ready=waitForRenderer(c);
    c.emit(event,...(event==='error'?[new Error('ENOENT')]:[2,null]));
    await assert.rejects(ready,event==='error'?/ENOENT/:/code 2/);
  }
});
