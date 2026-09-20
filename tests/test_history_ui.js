// Run with: node tests/test_history_ui.js
//
// The History view, checked the way the rest of the UI is: the page's own
// script is parsed, the history section is run in a sandbox with stubbed
// elements, and what reaches the screen is asserted on. What matters here is
// that a conversation nobody is running still reads as openable, that a live
// one is marked so it cannot be deleted by accident, and that the trash says
// how long is left to change your mind.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('agentgrid/static/app.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);

const elements = {};
const element = () => ({textContent: '', hidden: false, innerHTML: '', value: '', title: '',
                        disabled: false, dataset: {}, setAttribute() {}, querySelector: () => null,
                        querySelectorAll: () => [], classList: {add() {}, remove() {}, contains: () => false},
                        addEventListener() {}, scrollTop: 0, scrollHeight: 0});
const ctx = vm.createContext({
  $: id => elements[id] ||= element(),
  esc: s => String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
  ageStr: sec => `${Math.round(sec / 86400)}d ago`,
  blocksHtml: blocks => blocks.map(b => `<div class="msg">${b.text}</div>`).join(''),
  view: 'history', toast() {}, toastUndo() {},
  api: async () => ({}), post: async () => ({}),
  setTimeout, clearTimeout, console, JSON, Date, Math,
});
const start = script.indexOf('let hxData = null, hxSig');
const end = script.indexOf('function bindHistory()', start);
assert.ok(start > 0 && end > start, 'the history section was not found');
vm.runInContext(script.slice(start, end), ctx);

const now = Date.now() / 1000;
const conversation = (over = {}) => Object.assign({
  sessionId: 'aaa', title: 'Fixing the parser', preview: 'try the other branch',
  project: 'one', cwd: '/repo/one', turns: 428, size: 2_400_000,
  modified: now - 40 * 86400, live: false,
}, over);

// `let`/`const` at the top of a vm script live in its lexical scope, not on the
// context object, so module state is set and read by running code in there.
function setState(state) {
  ctx.__s = state;
  vm.runInContext(`
    for (const k of Object.keys(__s)) {
      if (k === 'hxData') hxData = __s[k];
      else if (k === 'hxTrashOn') hxTrashOn = __s[k];
      else if (k === 'hxSig') hxSig = __s[k];
      else if (k === 'hxQuery') hxQuery = __s[k];
      else if (k === 'hxProject') hxProject = __s[k];
      else if (k === 'hxBlocks') hxBlocks = __s[k];
      else if (k === 'hxTotal') hxTotal = __s[k];
      else if (k === 'hxOffset') hxOffset = __s[k];
      else if (k === 'hxHasOlder') hxHasOlder = __s[k];
    }`, ctx);
}
const evalIn = (expr) => vm.runInContext(expr, ctx);

function render(data, trash = false) {
  setState({hxData: data, hxTrashOn: trash, hxSig: ''});
  ctx.renderHistory();
  return elements.hxBody.innerHTML;
}

// --- a row carries what it takes to recognise a conversation ----------------
{
  const out = render({total: 1, projects: [], conversations: [conversation()]});
  assert.ok(out.includes('Fixing the parser'), 'the title is shown');
  assert.ok(out.includes('try the other branch'), 'and the last thing asked');
  assert.ok(out.includes('/repo/one') || out.includes('one'), 'and where it ran');
  assert.ok(out.includes('428 msgs'), 'and how long it is');
  assert.ok(out.includes('2.3 MB'), 'and what it costs on disk');
  assert.ok(out.includes('40d ago'), 'and when it was last touched');
  assert.ok(out.includes('data-hx="aaa"'), 'the row is clickable by session id');
  assert.strictEqual(elements.hxCount.textContent, '1 conversation');
}

// --- a conversation 40 days old is listed exactly like a fresh one ----------
{
  const out = render({total: 2, projects: [], conversations: [
    conversation({sessionId: 'new', title: 'Today', modified: now - 60}),
    conversation({sessionId: 'old', title: 'A month ago', modified: now - 40 * 86400}),
  ]});
  assert.ok(out.includes('data-hx="new"') && out.includes('data-hx="old"'),
            'age decides nothing — the board window used to drop the old one');
}

// --- a live conversation is marked, so Delete can refuse it ------------------
{
  const out = render({total: 2, projects: [], conversations: [
    conversation({sessionId: 'live1', live: true}),
    conversation({sessionId: 'dead1', live: false}),
  ]});
  const rows = out.split('<tr').filter(r => r.includes('data-hx='));
  assert.ok(rows.find(r => r.includes('live1')).includes('hx-live'), 'the running one says so');
  assert.ok(!rows.find(r => r.includes('dead1')).includes('hx-live'), 'the finished one does not');
}

// --- a title is never required to find a row --------------------------------
{
  const out = render({total: 1, projects: [], conversations: [
    conversation({title: '', preview: ''})]});
  assert.ok(out.includes('(untitled)'), 'an untitled conversation is still listed, not hidden');
}

// --- the empty states say which emptiness this is ---------------------------
{
  setState({hxQuery: '', hxProject: ''});
  assert.ok(render({total: 0, projects: [], conversations: []}).includes('No conversations yet'));
  setState({hxQuery: 'nothing'});
  assert.ok(render({total: 0, projects: [], conversations: []}).includes('Nothing matches'),
            'a search that found nothing is not "you have no history"');
  setState({hxQuery: ''});
}

// --- the trash says what is recoverable and for how long --------------------
{
  const out = render({ttlDays: 7, trash: [
    {sessionId: 'aaa', origin: '/p/aaa.jsonl', deletedAt: now - 2 * 86400,
     size: 1_048_576, expiresIn: 5 * 86400}]}, true);
  assert.ok(out.includes('data-hxrestore="aaa"'), 'it can be put back');
  assert.ok(out.includes('data-hxpurge="aaa"'), 'or emptied now');
  assert.ok(out.includes('5d left'), 'and says how long that stays true');
  assert.ok(out.includes('1.0 MB'), 'and what it would reclaim');
  assert.strictEqual(elements.hxCount.textContent, '1 in the trash');
  assert.ok(render({ttlDays: 7, trash: []}, true).includes('trash is empty'));
}

// --- sizes read the way a human reads them ----------------------------------
{
  assert.strictEqual(evalIn('hxSize(0)'), '0 B');
  assert.strictEqual(evalIn('hxSize(900)'), '900 B');
  assert.strictEqual(evalIn('hxSize(2048)'), '2 KB');
  assert.strictEqual(evalIn('hxSize(179 * 1048576)'), '179.0 MB');
}

// --- a repaint with unchanged data leaves the DOM alone ---------------------
{
  const data = {total: 1, projects: [], conversations: [conversation()]};
  render(data);
  elements.hxBody.innerHTML = 'SENTINEL';
  ctx.renderHistory();
  assert.strictEqual(elements.hxBody.innerHTML, 'SENTINEL',
                     'the signature guard skipped a redraw that would change nothing');
}

// --- reading older turns keeps the reader where they were -------------------
{
  setState({hxBlocks: [{text: 'a'}, {text: 'b'}], hxTotal: 5166, hxOffset: 4566, hxHasOlder: true});
  ctx.hxRenderTranscript(false);
  assert.ok(elements.hxOlderRow.innerHTML.includes('4566 of 5166 still above'),
            'the reader is told how much is still above — the old code just stopped at 600');
  setState({hxOffset: 0, hxHasOlder: false});
  ctx.hxRenderTranscript(false);
  assert.ok(elements.hxOlderRow.innerHTML.includes('whole conversation'),
            'and when it reaches the start, it says so');
}

console.log('history ui: all assertions passed');
