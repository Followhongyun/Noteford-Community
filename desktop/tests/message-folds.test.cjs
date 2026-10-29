const {test} = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path')
const {stripTypeScriptTypes} = require('node:module')
const moduleUrl = source => 'data:text/javascript;base64,' + Buffer.from(stripTypeScriptTypes(source)).toString('base64')
const visibility = moduleUrl(fs.readFileSync(path.join(__dirname, '../src/message-visibility.ts'), 'utf8'))
const folds = import(moduleUrl(fs.readFileSync(path.join(__dirname, '../src/message-folds.ts'), 'utf8').replace("'./message-visibility'", JSON.stringify(visibility))))
const message = (uid, kind, text = '') => ({uid, type: {kind}, text, timestamp: 1})
const preferences = {foldStickers: true, visibility: {hideRevoke: true, hidePat: true}}

test('single standalone sticker, animation, recall and pat each get a counted fold', async () => {
  const {groupMessages} = await folds
  for (const [kind, label] of [['emoji','1 个表情'],['gif','1 个表情'],['revoke','1 条撤回提示'],['pat','1 条拍一拍']]) {
    const item = message(kind,kind)
    const [group] = groupMessages([item],true,100,preferences)
    assert.equal(group.label,'已折叠：'+label)
    assert.equal(group.messages[0],item)
  }
})
test('adjacent eligible messages combine counts without swallowing normal prose', async () => {
  const {groupMessages} = await folds
  const items = [message('a','text','[微笑]'),message('b','gif'),message('c','system','张三撤回了一条消息'),message('d','system','张三拍了拍李四'),message('e','text','你好[微笑]')]
  const groups = groupMessages(items,true,100,preferences)
  assert.equal(groups.length,2)
  assert.equal(groups[0].label,'已折叠：2 个表情 · 1 条撤回提示 · 1 条拍一拍')
  assert.equal(groups[1].label,undefined)
  assert.deepEqual(groups.flatMap(g=>g.messages),items)
})
test('disabled preferences retain original threshold and controls remain independent', async () => {
  const {groupMessages} = await folds
  assert.equal(groupMessages([message('a','emoji')])[0].label,undefined)
  assert.equal(groupMessages([message('a','pat')],true,100,{visibility:{hideRevoke:true,hidePat:false}})[0].label,undefined)
  assert.match(groupMessages([message('a','emoji'),message('b','gif'),message('c','emoji')])[0].label,/3 个表情/)
})
test('search bypass and located message never lose the original record', async () => {
  const {groupMessages} = await folds
  const items=[message('a','emoji'),message('b','pat')]
  assert.ok(groupMessages(items,false,100,preferences).every(g=>!g.label))
  const groups=groupMessages(items,true,100,{...preferences,revealUid:'a'})
  assert.equal(groups[0].label,undefined)
  assert.equal(groups[1].label,'已折叠：1 条拍一拍')
})
