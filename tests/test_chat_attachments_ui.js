// Run with: node tests/test_chat_attachments_ui.js
// The composer's attachment logic: what a drop hands over, how refusals show,
// how a sent file is echoed, and how a turn read back from disk renders.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('agentgrid/static/app.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
function extract(name) {
  const start = script.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `missing ${name}`);
  const next = /\n(?:async )?function /g; next.lastIndex = start + 1;
  const end = next.exec(script)?.index ?? script.length;
  return (script.slice(start - 6, start) === 'async ' ? 'async ' : '') + script.slice(start, end);
}
function constLine(name) {
  const m = new RegExp(`\\nconst ${name} = [^\\n]+\\n`).exec(script);
  assert.ok(m, `missing const ${name}`);
  return m[0];
}
const elements = {cAtt: {hidden: true, innerHTML: ''}, panel: {classList: {contains: c => c === 'on'}}};
const uploads = [];
const ctx = vm.createContext({
  $: id => elements[id] ||= {hidden: true, value: '', focus() {}},
  esc: t => String(t ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
  md: t => `<p>${t}</p>`, cleanSlash: t => t,
  openId: 's1', chatDisabled: false, chatAtt: [], chatAttSeq: 0,
  URL: {createObjectURL: () => 'blob:x', revokeObjectURL() {}},
  FileReader: class { readAsDataURL() { this.result = 'data:;base64,eA=='; this.onload(); } },
  api: async (path, o) => { const body = JSON.parse(o.body); uploads.push(body);
    return body.name === 'bad.exe' ? {error: "bad.exe can't be attached."}
      : {path: `/h/.agentgrid/uploads/s1/20260917-101010-abc123-${body.name}`, kind: 'text'}; },
});
vm.runInContext([
  constLine('MAX_ATTACH_BYTES'), constLine('PREVIEW_TYPES'), constLine('ATTACHED_TAIL'),
  ...['chatCanAttach', 'fmtBytes', 'fileBadge', 'filePills', 'chatAddFiles', 'filesFromDrop',
      'chatAttError', 'chatUpload', 'chatAttFail', 'blobToDataUrl', 'chatRenderAtt',
      'userBodyHtml'].map(extract),
].join('\n'), ctx);
const state = () => vm.runInContext('chatAtt', ctx);

(async () => {
  // a drop marks folders apart from files
  const file = {name: 'notes.md', size: 12, type: 'text/markdown'};
  const dropped = ctx.filesFromDrop({items: [
    {kind: 'file', webkitGetAsEntry: () => ({isDirectory: true, name: 'src'}), getAsFile: () => ({name: 'src'})},
    {kind: 'file', webkitGetAsEntry: () => ({isDirectory: false}), getAsFile: () => file},
    {kind: 'string', getAsFile: () => null},
  ]});
  assert.deepEqual(dropped.map(f => f.folder ?? f.name), ['src', 'notes.md']);

  // a folder, an empty file and an oversize file never upload; each says why
  ctx.chatAddFiles([{folder: 'src'}, {name: 'empty.txt', size: 0, type: ''},
                    {name: 'huge.mov', size: 11 * 1024 * 1024, type: 'video/quicktime'}]);
  assert.equal(uploads.length, 0);
  assert.deepEqual(state().map(a => a.error), [
    'src is a folder — attach the files inside it instead.',
    'empty.txt is empty.',
    'huge.mov is 11.0 MB — the limit is 10.0 MB per file.']);
  assert.ok(elements.cAtt.innerHTML.includes('chip bad'));
  vm.runInContext('chatAtt = []', ctx);

  // a good file gets a path and a type badge; a refused one keeps the server's reason
  await ctx.chatUpload(file);
  await ctx.chatUpload({name: 'bad.exe', size: 5, type: 'application/octet-stream'});
  const [ok, bad] = state();
  assert.equal(ok.path, '/h/.agentgrid/uploads/s1/20260917-101010-abc123-notes.md');
  assert.equal(ok.url, null);
  assert.equal(bad.path, null);
  assert.equal(bad.error, "bad.exe can't be attached.");
  assert.ok(elements.cAtt.innerHTML.includes('<span class="ftype">md</span>'));
  assert.ok(elements.cAtt.innerHTML.includes('bad.exe can&#39;t be attached.'));

  // nothing is accepted while the composer is unavailable
  ctx.chatDisabled = true;
  ctx.chatAddFiles([file]);
  assert.equal(uploads.length, 2);
  ctx.chatDisabled = false;

  // a turn read back from disk shows its uploads as pills, named as picked
  const turn = 'check these\n\nAttached files:\n' +
    '- /Users/me/.agentgrid/uploads/s1/20260917-101010-abc123-spec.pdf\n' +
    '- /Users/me/.agentgrid/uploads/s1/20260917-101011-def456-shot.png';
  const out = ctx.userBodyHtml(turn);
  assert.ok(out.startsWith('<p>check these</p><div class="fpills">'));
  assert.ok(out.includes('<span class="nm">spec.pdf</span>'));
  assert.ok(out.includes('<span class="nm">shot.png</span>'));
  assert.ok(!out.includes('/Users/me'));
  // file-only turns lose the nudge line too
  assert.equal(ctx.userBodyHtml('Please look at the attached file(s):\n- /x/.agentgrid/uploads/s/20260917-101010-abc123-a.txt'),
    '<div class="fpills"><span class="fpill" title="a.txt"><span class="ftype">txt</span><span class="nm">a.txt</span></span></div>');
  // a message that merely talks about attachments renders as written
  const prose = 'Attached files:\n- /etc/hosts';
  assert.equal(ctx.userBodyHtml(prose), `<p>${prose}</p>`);
  assert.equal(ctx.userBodyHtml('plain words'), '<p>plain words</p>');

  console.log('Chat attachments: drop parsing, refusals, uploads and transcript pills passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
