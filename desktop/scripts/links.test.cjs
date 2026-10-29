const assert=require('node:assert/strict');
const fs=require('node:fs');
const {stripTypeScriptTypes}=require('node:module');
(async()=>{
 const source=stripTypeScriptTypes(fs.readFileSync(require('node:path').join(__dirname,'../src/links.ts'),'utf8'));
 const {splitLinks,webUrl}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
 const cases=[
 ['https://x.com/creator_one/status/1234567890\n\n示例说明：教程 ~',['https://x.com/creator_one/status/1234567890']],
 ['引用：https://x.com/creator_two/status/9876543210?s=20。',['https://x.com/creator_two/status/9876543210?s=20']],
 ['查看（https://example.com/a?x=1&y=2#part），再看 www.example.org/test 和 x.com/user。',['https://example.com/a?x=1&y=2#part','https://www.example.org/test','https://x.com/user']],
 ['(https://en.wikipedia.org/wiki/Function_(mathematics)).',['https://en.wikipedia.org/wiki/Function_(mathematics)']],
 ['https://example.com/a,https://example.org/b',['https://example.com/a','https://example.org/b']],
 ['https://例子.中国/中文',['https://xn--fsqu00a.xn--fiqs8s/中文']],
 ['联系 user@example.com；javascript:alert(1)；file://example.com/a',[]],
 ['<script>alert(1)</script> https://example.com/?a=%26&b=two',['https://example.com/?a=%26&b=two']],
 ];
 for(const [text,expected] of cases){const parts=splitLinks(text);assert.equal(parts.map(p=>p.text).join(''),text);assert.deepEqual(parts.filter(p=>p.url).map(p=>decodeURI(p.url)),expected);}
 assert.equal(webUrl('javascript:alert(1)'),undefined);
 assert.equal(webUrl('file:///C:/private'),undefined);
 console.log('PASS: 8 link recognition/preservation cases and blocked non-web schemes');
})().catch(e=>{console.error(e);process.exit(1)});
