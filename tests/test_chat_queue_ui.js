// Run with: node tests/test_chat_queue_ui.js
// The chat queue in the composer: a message sent mid-turn waits on the server,
// the queued count opens a panel that shows it, and Edit / Cancel / Remove /
// Undo go through /api/chat/queue so they change what is actually sent. The
// page's own functions run in a sandbox against stub elements and a fake
// server that keeps the queue the way agentgrid/chat.py does.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('agentgrid/static/app.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
function extract(name) {
  const start = script.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `missing ${name}`);
  const next = /\n(?:async )?function /g; next.lastIndex = start + 1;
  const end = next.exec(script)?.index ?? script.length;
  return (script.slice(start - 6, start) === 'async ' ? 'async ' : '') + script.slice(start, end);
}

// --- stub elements: enough of the DOM for the queue's own code --------------
const doc = {activeElement: null};
function element(id, inside = []) {
  return {
    id, inside, hidden: id === 'cQueue' || id === 'cQueueBtn', innerHTML: '', value: '', title: '',
    dataset: {}, attrs: {}, style: {}, classList: {toggle() {}},
    get textContent() { return this.innerHTML.replace(/<[^>]*>/g, ''); },
    set textContent(v) { this.innerHTML = String(v); },
    setAttribute(k, v) { this.attrs[k] = v; },
    focus() { doc.activeElement = this; },
    contains(x) { return !!x && (x === this || (x.inside || []).includes(this.id)); },
    querySelector() { return null; }, querySelectorAll() { return []; },
    setSelectionRange() {}, closest() { return null; },
    insertAdjacentHTML(_, h) { this.innerHTML += h; }, scrollTop: 0, scrollHeight: 0, clientHeight: 0,
  };
}
const elements = {};
const $ = id => elements[id] ||= element(id);
['cInput', 'tScroll', 'cStatus', 'cStatusText', 'cStatusSep', 'cQueueBtn', 'cQueue', 'cQueueList',
 'cStop', 'cModel'].forEach($);
// The list has no parser behind it: an editor exists when its markup does.
const editor = Object.assign(element('editor', ['cQueue', 'cQueueList']),
  {tagName: 'TEXTAREA', dataset: {qact: 'draft'}});
$('cQueueList').querySelector = sel =>
  sel === 'textarea' && elements.cQueueList.innerHTML.includes('<textarea') ? editor : null;

// --- a fake server holding the queue, as agentgrid/chat.py does -------------
const server = {queue: [], seq: 1, removed: new Map(), calls: []};
const state = () => ({running: true, queued: server.queue.length,
                      queue: server.queue.map(q => ({...q, files: [...q.files]}))});
async function post(path, body) {
  server.calls.push([path, body]);
  if (path === '/api/chat') {
    const id = `q${++server.seq}`;
    server.queue.push({id, message: body.message, files: [], posture: body.posture, model: body.model});
    return {ok: true, id, queued: true};
  }
  assert.equal(path, '/api/chat/queue');
  const i = server.queue.findIndex(q => q.id === body.id);
  if (body.action === 'restore') {
    if (!server.removed.has(body.id)) return {error: 'gone', gone: true, ...state()};
    const [at, q] = server.removed.get(body.id); server.removed.delete(body.id);
    server.queue.splice(at, 0, q);
    return {ok: true, ...state()};
  }
  if (i < 0) return {error: 'That message already started, or Stop cleared it.', gone: true, ...state()};
  if (body.action === 'edit') server.queue[i].message = body.message;
  if (body.action === 'remove') server.removed.set(body.id, [i, server.queue.splice(i, 1)[0]]);
  if (body.action === 'move') server.queue.splice(body.index, 0, server.queue.splice(i, 1)[0]);
  return {ok: true, ...state()};
}
const toasts = [], undos = [];
const ctx = vm.createContext({
  $, document: doc, post, api: async () => state(),
  esc: t => String(t ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
  md: t => `<p>${t}</p>`, localStorage: {getItem: () => null, setItem() {}},
  toast: m => toasts.push(m), toastUndo: (m, fn) => undos.push([m, fn]),
  chatGrow() {}, hideSlash() {}, hideAtFiles() {}, saveChatModel() {}, chatRenderAtt() {},
  chatClearRefs() {}, selectedModel: () => '', CUSTOM_MODEL: '__custom__',
  URL: {revokeObjectURL() {}}, setTimeout,
});
const section = script.slice(script.indexOf('let chatSrc = null'), script.indexOf('// called from openSession'));
vm.runInContext(section, ctx);
vm.runInContext(['fileBadge', 'filePills', 'chatAppend', 'chatOnEvent', 'chatSetStatus', 'chatSyncState',
  'chatQueueReset', 'chatSetQueue', 'chatToggleQueue', 'uploadName', 'cqRowHtml', 'chatRenderQueue',
  'chatQueueIndexOfRow', 'cqRefocus', 'cqStartEdit', 'cqCancelEdit', 'cqSave', 'cqRemove', 'cqRestore',
  'cqMoveUp', 'cqReply', 'cqRescue', 'chatEchoStarted', 'cqClick', 'cqInput', 'cqKeydown',
  'chatSend'].map(extract).join('\n'), ctx);
vm.runInContext('openId = "s1"; chatPosture = "auto"; chatModel = ""; chatAtt = []; chatRefs = [];', ctx);
const run = code => vm.runInContext(code, ctx);
// a click on a row's control, as the delegated listener on #cQueue sees it
const click = (qid, qact) => {
  const row = {dataset: {qid}};
  return run('cqClick')({target: {closest: s => s === '[data-qact]' ? {dataset: {qact}, closest: () => row} : null}});
};
const settle = () => new Promise(r => setImmediate(r));
const list = () => elements.cQueueList.innerHTML;
const count = () => elements.cQueueBtn.hidden ? 0 : Number(/^(\d+) queued/.exec(elements.cQueueBtn.textContent)[1]);

(async () => {
  // --- the button is wired: the queued count toggles the panel --------------
  assert.ok(/\$\("cQueueBtn"\)\.addEventListener\("click", \(\) => chatToggleQueue\(\)\)/.test(script));
  assert.ok(/\$\("cQueue"\)\.addEventListener\("click", cqClick\)/.test(script));
  assert.ok(/\$\("cQueue"\)\.addEventListener\("keydown", cqKeydown\)/.test(script));
  assert.ok(/\$\("cQueueList"\)\.addEventListener\("input", cqInput\)/.test(script));

  // --- a send mid-turn waits in the queue, not in the transcript -------------
  run('chatRunning = true');
  elements.cInput.value = 'Also update the <README>';
  await run('chatSend()'); await settle();
  assert.equal(elements.tScroll.innerHTML, '', 'a waiting message is not echoed mid-turn');
  assert.equal(server.queue.length, 1);
  assert.equal(count(), 1);
  assert.equal(elements.cQueueBtn.textContent, '1 queued▴');
  assert.equal(elements.cQueue.hidden, false, 'the queue opens to show where the message went');
  const [first] = server.queue;

  // --- the queued count opens the queue, and the message is shown ------------
  run('chatToggleQueue()');                           // what a click on the count does
  assert.equal(elements.cQueue.hidden, true);
  assert.equal(elements.cQueueBtn.attrs['aria-expanded'], 'false');
  assert.equal(elements.cQueueBtn.title, 'Show queued messages — edit or remove them before they send');
  run('chatToggleQueue()');
  assert.equal(elements.cQueue.hidden, false);
  assert.equal(elements.cQueueBtn.attrs['aria-expanded'], 'true');
  assert.ok(list().includes('Also update the &lt;README&gt;'), 'the text is shown, escaped');
  assert.ok(list().includes('aria-label="Edit queued message 1"'));
  assert.ok(list().includes('aria-label="Remove queued message 1"'));
  assert.ok(!list().includes('data-qact="up"'), 'the first message has nowhere to move up to');

  // --- Cancel leaves the original, and sends nothing -------------------------
  click(first.id, 'edit');
  assert.ok(list().includes('<textarea'), 'Edit opens an editor');
  assert.equal(editor.value, 'Also update the <README>', 'the editor holds the full text');
  assert.equal(doc.activeElement, editor, 'focus lands in the editor');
  editor.value = 'something else';
  run('cqInput')({target: editor});
  click(first.id, 'cancel');
  assert.ok(!list().includes('<textarea'));
  assert.ok(list().includes('Also update the &lt;README&gt;'));
  assert.equal(server.queue[0].message, 'Also update the <README>');
  assert.equal(server.calls.filter(([p]) => p === '/api/chat/queue').length, 0);
  // Escape in the editor cancels too, and stays inside the queue
  click(first.id, 'edit');
  let stopped = false;
  run('cqKeydown')({key: 'Escape', target: editor, preventDefault() {}, stopPropagation() { stopped = true; }});
  assert.ok(stopped, 'Escape must not reach the document, which closes the session panel');
  assert.ok(!list().includes('<textarea'));
  assert.equal(elements.cQueue.hidden, false, 'cancelling an edit keeps the queue open');

  // --- an edit reaches the queue the server will send from -------------------
  click(first.id, 'edit');
  editor.value = '  Also update the CHANGELOG  ';
  run('cqInput')({target: editor});
  run('cqKeydown')({key: 'Enter', metaKey: true, target: editor, preventDefault() {}, stopPropagation() {}});
  await settle();
  assert.deepEqual(server.calls.at(-1), ['/api/chat/queue',
    {sessionId: 's1', action: 'edit', id: first.id, message: 'Also update the CHANGELOG'}]);
  assert.equal(server.queue[0].message, 'Also update the CHANGELOG', 'the edit is what will be sent');
  assert.ok(list().includes('Also update the CHANGELOG') && !list().includes('README'));
  // an edit that empties a text-only message is refused before it is sent
  const calls = server.calls.length;
  click(first.id, 'edit');
  editor.value = '   '; run('cqInput')({target: editor});
  click(first.id, 'save'); await settle();
  assert.equal(server.calls.length, calls);
  assert.equal(toasts.at(-1), "A queued message can't be empty — remove it instead.");
  click(first.id, 'cancel');

  // --- Remove updates the count; Undo puts it back where it was --------------
  elements.cInput.value = 'Then run the tests';
  await run('chatSend()'); await settle();
  assert.equal(count(), 2);
  const second = server.queue[1];
  assert.ok(list().includes('aria-label="Move queued message 2 up"'));
  click(first.id, 'remove');
  assert.equal(count(), 1, 'the count drops at once');
  await settle();
  assert.deepEqual(server.queue.map(q => q.id), [second.id]);
  assert.equal(count(), 1);
  assert.ok(!list().includes('CHANGELOG') && list().includes('Then run the tests'));
  assert.equal(undos.at(-1)[0], 'Removed the queued message');
  undos.at(-1)[1](); await settle();
  assert.deepEqual(server.queue.map(q => q.id), [first.id, second.id], 'Undo restores it in place');
  assert.equal(count(), 2);

  // --- Move up reorders what is sent -----------------------------------------
  click(second.id, 'up'); await settle();
  assert.deepEqual(server.queue.map(q => q.id), [second.id, first.id]);
  assert.ok(list().indexOf('Then run the tests') < list().indexOf('CHANGELOG'));

  // --- a repaint with unchanged data leaves the DOM (and focus) alone --------
  elements.cQueueList.innerHTML = 'browser-normalized';
  run('chatRenderQueue()');
  assert.equal(elements.cQueueList.innerHTML, 'browser-normalized');
  run('cqSig = ""; chatRenderQueue()');

  // --- Escape outside the editor closes the queue, focus back on the count ---
  doc.activeElement = Object.assign(element('rm', ['cQueue', 'cQueueList']), {dataset: {qact: 'remove'}});
  stopped = false;
  run('cqKeydown')({key: 'Escape', target: doc.activeElement, preventDefault() {}, stopPropagation() { stopped = true; }});
  assert.ok(stopped);
  assert.equal(elements.cQueue.hidden, true);
  assert.equal(doc.activeElement, elements.cQueueBtn);

  // --- a message that waited is shown when it starts, not before -------------
  run('chatRunning = false');
  server.queue.shift();
  run('chatOnEvent')({type: 'queue', queue: state().queue,
    started: {id: second.id, message: 'Then run the tests', files: ['20260918-101010-abc123-shot.png']}});
  assert.ok(elements.tScroll.innerHTML.includes('<div class="msg user"><div class="who">YOU</div><div class="body"><p>Then run the tests</p>'));
  assert.ok(elements.tScroll.innerHTML.includes('<span class="nm">shot.png</span>'), 'files show by the name picked');
  assert.equal(run('chatRunning'), true, 'its turn holds the disk refresh like a send does');
  assert.equal(count(), 1);

  // --- an edit that loses the race keeps the words ----------------------------
  run('chatToggleQueue(true)');
  click(first.id, 'edit');
  editor.value = 'my careful edit'; run('cqInput')({target: editor});
  elements.cInput.value = '';
  server.queue.shift();
  run('chatOnEvent')({type: 'queue', queue: [], started: {id: first.id, message: 'Also update the CHANGELOG', files: []}});
  assert.equal(elements.cInput.value, 'my careful edit', 'the edit is handed back in the message box');
  assert.ok(toasts.at(-1).includes('left the queue before your edit was saved'));
  assert.equal(count(), 0, 'an empty queue hides the count');
  assert.equal(elements.cQueue.hidden, true, 'and closes the panel');

  // --- Stop empties it; switching sessions forgets it -------------------------
  server.queue.push({id: 'q9', message: 'later', files: [], posture: 'auto', model: ''});
  await run('chatSyncState()');
  assert.equal(count(), 1);
  run('chatOnEvent')({type: 'queue', queue: [], started: null});
  assert.equal(count(), 0);
  await run('chatSyncState()');
  assert.equal(count(), 1);
  run('chatQueueReset()');
  assert.equal(count(), 0, "another session's queue never shows here");
  assert.equal(elements.cQueue.hidden, true);

  console.log('Chat queue UI: open, show, edit, cancel, remove, undo, move, start and race checks passed');
})().catch(e => { console.error(e); process.exitCode = 1; });
