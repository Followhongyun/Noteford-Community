const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {saveMedia} = require('../electron/media-save.cjs');
test('save registered media, cancel without writing, reject foreign paths and schemes', async () => {
  const root = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'noteford-save-')));
  try {
    const source = path.join(root, 'original.png'), target = path.join(root, 'saved.png');
    await fs.writeFile(source, 'original bytes');
    const options = {mediaMap: new Map([['known', source]]), isAllowedLocalMediaPath: file => file === source,
      showSaveDialog: async options => {assert.equal(options.defaultPath, 'image.png'); return {canceled: false, filePath: target}}};
    assert.equal(await saveMedia({url: 'noteford-media://asset/known', name: '../image'}, options), true);
    assert.equal(await fs.readFile(target, 'utf8'), 'original bytes');
    await fs.writeFile(target, 'keep');
    assert.equal(await saveMedia({url: 'noteford-media://asset/known'}, {...options, showSaveDialog: async () => ({canceled: true})}), false);
    assert.equal(await fs.readFile(target, 'utf8'), 'keep');
    for (const url of ['file:///known', 'https://asset/known', 'noteford-media://other/known', 'noteford-media://asset/missing']) {
      await assert.rejects(saveMedia({url}, {...options, showSaveDialog: () => assert.fail('dialog must not open')}));
    }
    await assert.rejects(saveMedia({url: 'noteford-media://asset/known'}, {...options, isAllowedLocalMediaPath: () => false}));
    let allowed = true;
    await assert.rejects(saveMedia({url: 'noteford-media://asset/known'}, {...options, isAllowedLocalMediaPath: () => allowed, showSaveDialog: async () => {
      allowed = false; return {canceled: false, filePath: path.join(root, 'other.png')};
    }}));
    assert.equal(await fs.readFile(target, 'utf8'), 'keep');
  } finally {await fs.rm(root, {recursive: true, force: true})}
});
