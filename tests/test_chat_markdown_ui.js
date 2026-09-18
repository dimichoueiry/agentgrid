// Run with: node tests/test_chat_markdown_ui.js
//
// The chat's markdown renderer, run out of app.html in a sandbox: what each
// block becomes, that nothing a model or a user types can become markup of its
// own, that half-written (streaming or clipped) text still renders sanely, and
// that Claude and Codex -- live or read back from disk -- go through the one
// same path.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('agentgrid/static/app.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);                       // the whole page still parses

function extract(name) {
  const start = script.indexOf(`function ${name}(`);
  assert.ok(start > 0, `${name} is gone from app.html`);
  const next = /\n(?:async )?function /g; next.lastIndex = start + 1;
  const end = next.exec(script)?.index ?? script.length;
  return script.slice(start, end);
}
function section(from, to) {
  const a = script.indexOf(from), b = script.indexOf(to, a);
  assert.ok(a >= 0 && b > a, `section ${from} is gone from app.html`);
  return script.slice(a, b);
}

const ctx = vm.createContext({document: {getElementById: () => null}});
vm.runInContext(section('const esc =', '// A slash command'), ctx);          // the real escaper
vm.runInContext(section('/* chat markdown.', '// ticket keys become links'), ctx);
const md = s => ctx.md(s);
const body = s => md(s).replace(/^<div class="md">/, '').replace(/<\/div>$/, '');

// every tag closes, in order -- a list or table that leaks its tags would
// swallow the rest of the transcript
function balanced(out) {
  const stack = [];
  for (const m of out.matchAll(/<(\/?)([a-z][a-z0-9]*)\b[^>]*>/gi)) {
    const name = m[2].toLowerCase();
    if (['br', 'hr', 'input'].includes(name)) continue;
    if (!m[1]) stack.push(name);
    else if (stack.pop() !== name) return false;
  }
  return stack.length === 0;
}
// only the renderer's own tags and attributes ever appear
const TAGS = new Set(('div p br strong em del code pre a span ul ol li input blockquote hr ' +
  'h1 h2 h3 h4 h5 h6 table thead tbody tr th td').split(' '));
const ATTRS = new Set('class href target rel title data-lang data-slug start style type disabled checked'.split(' '));
function onlyOwnMarkup(out) {
  for (const m of out.matchAll(/<\/?([a-z][a-z0-9]*)([^>]*)>/gi)) {
    if (!TAGS.has(m[1].toLowerCase())) return `tag <${m[1]}>`;
    for (const a of m[2].matchAll(/([a-z-]+)(?:="([^"]*)")?/gi)) {
      if (!ATTRS.has(a[1].toLowerCase())) return `attribute ${a[1]}`;
      if (a[1] === 'href' && !/^(?:https?:|mailto:|#$)/i.test(a[2])) return `href ${a[2]}`;
      if (a[1] === 'style' && !/^text-align:(?:left|right|center)$/.test(a[2])) return `style ${a[2]}`;
    }
  }
  return '';
}

// ---- paragraphs: the blob bug ------------------------------------------------
assert.equal(body('First line.\nsecond line.\n\nNew paragraph.'),
  '<p>First line.<br>second line.</p><p>New paragraph.</p>',
  'a blank line splits paragraphs and a single newline is kept as a break');
assert.equal(body('\n\n  lead and trail  \n\n'), '<p>lead and trail</p>', 'edge whitespace is dropped');
assert.equal(md(''), '<div class="md"></div>', 'empty input renders an empty block');
assert.equal(body('a\r\nb'), '<p>a<br>b</p>', 'CRLF is a newline');

// ---- headings and rules --------------------------------------------------------
assert.equal(body('## Plan\nDo it.'), '<h2>Plan</h2><p>Do it.</p>', 'an ATX heading ends the paragraph above');
assert.equal(body('### Done ###'), '<h3>Done</h3>', 'closing hashes are dropped');
assert.equal(body('# C#'), '<h1>C#</h1>', 'a hash inside the title stays');
assert.equal(body('#hashtag and #5'), '<p>#hashtag and #5</p>', 'no space, no heading');
assert.equal(body('Title\n====='), '<h1>Title</h1>', 'setext heading');
assert.equal(body('a\n\n---\n\nb'), '<p>a</p><hr><p>b</p>', 'a rule');
assert.equal(body('a\n***\nb'), '<p>a</p><hr><p>b</p>', 'a rule interrupts a paragraph');

// ---- lists ---------------------------------------------------------------------
assert.equal(body('Steps:\n1. one\n2. two'),
  '<p>Steps:</p><ol><li><p>one</p></li><li><p>two</p></li></ol>', 'an ordered list after a lead-in line');
assert.equal(body('3. c\n4. d'), '<ol start="3"><li><p>c</p></li><li><p>d</p></li></ol>', 'numbering keeps its start');
assert.equal(body('- a\n* b\n+ c'), '<ul><li><p>a</p></li><li><p>b</p></li><li><p>c</p></li></ul>', 'every bullet marker');
assert.equal(body('- a\n  - b\n    - c\n- d'),
  '<ul><li><p>a</p><ul><li><p>b</p><ul><li><p>c</p></li></ul></li></ul></li><li><p>d</p></li></ul>', 'nesting by indent');
assert.equal(body('1. Step\n  - sub a\n  - sub b\n2. Next'),
  '<ol><li><p>Step</p><ul><li><p>sub a</p></li><li><p>sub b</p></li></ul></li><li><p>Next</p></li></ol>',
  'two spaces under "1. " still nests (a common slip)');
assert.equal(body('1. a\n\n2. b'), '<ol class="loose"><li><p>a</p></li><li><p>b</p></li></ol>',
  'blank lines between items keep one list, marked loose');
assert.equal(body('- a\n- b'), '<ul><li><p>a</p></li><li><p>b</p></li></ul>', 'no blank lines, tight');
assert.equal(body('- a long item\nthat wraps'), '<ul><li><p>a long item<br>that wraps</p></li></ul>', 'lazy continuation');
assert.equal(body('- a\n\nafter'), '<ul><li><p>a</p></li></ul><p>after</p>', 'an unindented paragraph ends the list');
assert.equal(body('- a\n1. b'), '<ul><li><p>a</p></li></ul><ol><li><p>b</p></li></ol>', 'a new list type starts a new list');
assert.equal(body('1. Run:\n   ```bash\n   npm test\n\n   npm run build\n   ```\n2. Ship'),
  '<ol><li><p>Run:</p><pre class="t-code" data-lang="bash"><code>npm test\n\nnpm run build</code></pre></li>' +
  '<li><p>Ship</p></li></ol>', 'a fence inside an item stays in it, blank lines and all, and the list stays tight');
assert.equal(body('- [ ] todo\n- [x] done'),
  '<ul><li class="task"><p><input type="checkbox" disabled>todo</p></li>' +
  '<li class="task"><p><input type="checkbox" disabled checked>done</p></li></ul>', 'task list');
assert.equal(body('2024. was a year\n1.5 million'), '<ol start="2024"><li><p>was a year<br>1.5 million</p></li></ol>',
  'a list is a list at block start');
assert.equal(body('In\n2024. it shipped'), '<p>In<br>2024. it shipped</p>',
  'an ordered item not starting at 1 does not cut a paragraph');
assert.equal(body('**bold** lead\n- item'), '<p><strong>bold</strong> lead</p><ul><li><p>item</p></li></ul>',
  'bold at line start is not a bullet');

// ---- code ------------------------------------------------------------------------
assert.equal(body('```js\nconst a = "<b>" && 1;\n**not bold** [x](https://y)\n```'),
  '<pre class="t-code" data-lang="js"><code>const a = &quot;&lt;b&gt;&quot; &amp;&amp; 1;\n' +
  '**not bold** [x](https://y)</code></pre>', 'a fence is escaped and never formatted');
assert.equal(body('~~~\n```\ninner\n```\n~~~'), '<pre class="t-code"><code>```\ninner\n```</code></pre>',
  'a tilde fence holds backtick fences');
assert.equal(body('````md\n```\nx\n```\n````'), '<pre class="t-code" data-lang="md"><code>```\nx\n```</code></pre>',
  'a longer fence closes only on its own length');
assert.equal(body('Use `npm test` and ``a ` b`` and `<i>`.'),
  '<p>Use <code>npm test</code> and <code>a ` b</code> and <code>&lt;i&gt;</code>.</p>', 'code spans');
assert.equal(body('run ```npm i``` now'), '<p>run <code>npm i</code> now</p>', 'a one-line triple-backtick is inline code');
assert.equal(body('`**x**` and `@alice` and `https://a.b`'),
  '<p><code>**x**</code> and <code>@alice</code> and <code>https://a.b</code></p>', 'nothing is formatted inside code');

// ---- inline ------------------------------------------------------------------------
assert.equal(body('**b** *i* _i_ ***bi*** ~~s~~ __b__'),
  '<p><strong>b</strong> <em>i</em> <em>i</em> <strong><em>bi</em></strong> <del>s</del> <strong>b</strong></p>', 'emphasis');
assert.equal(body('snake_case_name and 2 * 3 * 4 and a*b*c'), '<p>snake_case_name and 2 * 3 * 4 and a*b*c</p>',
  'identifiers and arithmetic are left alone');
assert.equal(body('\\*literal\\* and \\_x\\_'), '<p>*literal* and _x_</p>', 'backslash escapes');
assert.equal(body('[docs](https://ex.com/a_b "Docs")'),
  '<p><a href="https://ex.com/a_b" target="_blank" rel="noreferrer" title="Docs">docs</a></p>', 'a link');
assert.equal(body('[**bold** `x`](https://ex.com)'),
  '<p><a href="https://ex.com" target="_blank" rel="noreferrer"><strong>bold</strong> <code>x</code></a></p>',
  'a link label keeps its formatting');
assert.equal(body('[Foo](https://en.wikipedia.org/wiki/Foo_(bar))'),
  '<p><a href="https://en.wikipedia.org/wiki/Foo_(bar)" target="_blank" rel="noreferrer">Foo</a></p>',
  'balanced parens in a url');
assert.equal(body('See https://ex.com/x. Or (https://ex.com/y), or **https://ex.com/z**'),
  '<p>See <a href="https://ex.com/x" target="_blank" rel="noreferrer">https://ex.com/x</a>. ' +
  'Or (<a href="https://ex.com/y" target="_blank" rel="noreferrer">https://ex.com/y</a>), ' +
  'or <strong><a href="https://ex.com/z" target="_blank" rel="noreferrer">https://ex.com/z</a></strong></p>',
  'a bare url leaves its sentence punctuation behind');
assert.equal(body('Edited [app.html](/Users/me/repo/app.html:2040)'),
  '<p>Edited <span class="t-ref" title="/Users/me/repo/app.html:2040">app.html</span></p>',
  'a local file link (how Codex cites files) keeps its words and goes nowhere');
assert.equal(body('![diagram](https://ex.com/d.png)'),
  '<p><a href="https://ex.com/d.png" target="_blank" rel="noreferrer">diagram</a></p>', 'an image is never fetched');
assert.equal(body('ask @alice. mail bob@ex.com'),
  '<p>ask <a href="#" class="mention" data-slug="alice">@alice</a>. mail bob@ex.com</p>',
  'a mention links; an email does not');
assert.equal(body('[@alice](https://ex.com)'),
  '<p><a href="https://ex.com" target="_blank" rel="noreferrer">@alice</a></p>', 'no mention inside a link');

// ---- quotes and tables -----------------------------------------------------------
assert.equal(body('> note\n> - one\n> - two'),
  '<blockquote><p>note</p><ul><li><p>one</p></li><li><p>two</p></li></ul></blockquote>', 'a quote holds blocks');
assert.equal(body('Compare:\n| Name | Count |\n|:--|--:|\n| `a\\|b` | **2** |\n| short |'),
  '<p>Compare:</p><table class="t-tbl"><thead><tr><th style="text-align:left">Name</th>' +
  '<th style="text-align:right">Count</th></tr></thead><tbody>' +
  '<tr><td style="text-align:left"><code>a|b</code></td><td style="text-align:right"><strong>2</strong></td></tr>' +
  '<tr><td style="text-align:left">short</td><td style="text-align:right"></td></tr></tbody></table>',
  'a table right under a line, with alignment, formatted cells, an escaped pipe and a ragged row');
assert.equal(body('| a | b |\n|---|---|\n| x<br>y | z |'),
  '<table class="t-tbl"><thead><tr><th>a</th><th>b</th></tr></thead><tbody>' +
  '<tr><td>x<br>y</td><td>z</td></tr></tbody></table>', '<br> in a cell is a line break');
assert.ok(!md('```\n| a | b |\n|---|---|\n```').includes('<table'), 'a table inside a fence stays code');
assert.equal(body('x | y'), '<p>x | y</p>', 'a pipe alone is not a table');

// ---- safety ------------------------------------------------------------------------
const HOSTILE = [
  '<img src=x onerror=alert(1)>', '<script>alert(1)</script>', '<a href="javascript:alert(1)">x</a>',
  '[x](javascript:alert(1))', '[x](JaVaScRiPt:alert(1))', '[x](data:text/html,<script>alert(1)</script>)',
  '[x](https://ex.com/"onmouseover="alert(1))', '[x](https://ex.com "t\\" onmouseover=\\"alert(1)")',
  '![x](javascript:alert(1))', '<https://ex.com/"><img src=x>', 'https://ex.com/<script>',
  '| <b>h</b> |\n|---|\n| <iframe> |', '```"><script>alert(1)</script>\ncode\n```',
  '# <svg onload=alert(1)>', '> <style>*{}</style>', '- [x] <input autofocus onfocus=alert(1)>',
  '\uE0000\uE001 held? \uE000 \uE001 \uE002', '[a](`https://ex.com`)', '@<b>x</b>', '`</code><script>`',
];
for (const s of HOSTILE) {
  const out = md(s);
  assert.equal(onlyOwnMarkup(out), '', `hostile input made foreign markup: ${JSON.stringify(s)} -> ${out}`);
  assert.ok(balanced(out), `unbalanced tags for ${JSON.stringify(s)}: ${out}`);
  assert.ok(!/[\uE000-\uE002]/.test(out), `a sentinel leaked for ${JSON.stringify(s)}`);
}
assert.ok(md('[x](javascript:alert(1))').includes('<span class="t-ref"'), 'an unsafe scheme gets no href');

// ---- streaming: every prefix of a real reply renders cleanly -----------------------
const REPLY = [
  '## Summary', '', 'The fix is in **two** parts: see `md()` and [the docs](https://ex.com/docs).', '',
  '1. Parse blocks', '   - fences first', '   - then lists', '2. Render inline:', '   ```js',
  '   const x = a * b;', '   ```', '', '| File | Change |', '|---|:-:|', '| app.html | `+120` |', '',
  '> Note: ~~old~~ new', '', '- [x] tests', '- [ ] review', '', '---', 'Done — ping @alice.',
].join('\n');
for (let n = 0; n <= REPLY.length; n++) {
  const out = md(REPLY.slice(0, n));
  assert.ok(balanced(out), `prefix ${n} left tags open: ${out}`);
  assert.equal(onlyOwnMarkup(out), '', `prefix ${n}`);
}
assert.ok(body('Here:\n```js\nconst x = 1;\n**not bold**').endsWith(
  '<pre class="t-code" data-lang="js"><code>const x = 1;\n**not bold**</code></pre>'),
  'an unclosed fence (mid-stream) is code to the end, not prose');
assert.equal(body('**half bold and [half link](https://ex'), '<p>**half bold and [half link](<a href="https://ex" ' +
  'target="_blank" rel="noreferrer">https://ex</a></p>', 'unclosed inline syntax stays literal');
assert.equal(body('| a | b |\n|---|---|'), '<table class="t-tbl"><thead><tr><th>a</th><th>b</th></tr></thead>' +
  '<tbody></tbody></table>', 'a table whose rows have not arrived yet');
assert.equal(body('Items:\n-'), '<p>Items:<br>-</p>', 'a bare dash mid-stream does not flash a heading or list');

// ---- pathological input stays fast (no regex blow-up) --------------------------------
for (const [name, s] of [['stars', '**a '.repeat(8000)], ['ems', '*a'.repeat(15000)],
  ['brackets', '['.repeat(3000) + '[x]'.repeat(3000)], ['ticks', '`` `'.repeat(5000)],
  ['heading', '# ' + 'a '.repeat(20000) + '#'], ['long list', '- item\n'.repeat(5000)],
  ['deep nesting', Array.from({length: 200}, (_, i) => ' '.repeat(i * 2) + '- x').join('\n')]]) {
  const t0 = Date.now(); md(s);
  assert.ok(Date.now() - t0 < 1500, `${name} took ${Date.now() - t0}ms`);
}

// ---- one path for Claude and Codex, live and saved -----------------------------------
// blocksHtml renders a transcript read back from disk; chatOnEvent renders the
// live stream. Both must put the same md() output in the body, for both engines.
const appended = [];
vm.runInContext([section('const ATTACHED_TAIL', 'function userBodyHtml('), extract('userBodyHtml'),
  extract('cleanSlash'), extract('blocksHtml'), extract('chatOnEvent')].join('\n'), ctx);
Object.assign(ctx, {workflowExportButton: () => '', filePills: () => '', traceHtml: () => '',
  chatAppend: h => { appended.push(h); return null; }});
const bodyOf = h => h.slice(h.indexOf('<div class="body">'), h.lastIndexOf('</div>'));
const bodies = [];
for (const engine of ['claude', 'codex']) {
  ctx.chatEngine = engine;
  const saved = ctx.blocksHtml([{kind: 'assistant', text: REPLY}]);
  ctx.chatOnEvent({type: 'assistant_message', text: REPLY});
  const live = appended.pop();
  assert.ok(saved.includes(md(REPLY)) && live.includes(md(REPLY)), `${engine}: both paths use md()`);
  assert.equal(bodyOf(saved), bodyOf(live), `${engine}: live and saved render the same`);
  assert.ok(saved.includes(engine === 'codex' ? '>CODEX<' : '>CLAUDE<'), `${engine}: the speaker is named`);
  bodies.push(bodyOf(saved));
  const user = ctx.blocksHtml([{kind: 'user', text: 'line one\nline two'}]);
  assert.ok(user.includes('<p>line one<br>line two</p>'), `${engine}: a user turn keeps its line breaks`);
}
assert.equal(bodies[0], bodies[1], 'Claude and Codex bodies are identical');

console.log('Chat markdown: blocks, lists, code, inline, tables, safety, streaming, speed and one path passed');
