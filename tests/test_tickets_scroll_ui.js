// Run with: node tests/test_tickets_scroll_ui.js
//
// The ticket board keeps your place (AG-13). It is polled every 3s, and each
// column of the board scrolls on its own. A redraw replaces the columns, and a
// new column starts at the top -- so a board redrawn on every poll threw the
// reader back up whatever column they were reading. Here the page's own
// tkTake / renderTickets run against a stub #tkBody that behaves the way a
// browser does: every write of its markup builds fresh columns, scrolled to 0.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync('agentgrid/static/app.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);

function extract(name) {
  const start = script.indexOf(`function ${name}(`);
  assert.ok(start > 0, `${name} is gone from app.html`);
  const next = /\n(?:async )?function /g; next.lastIndex = start + 1;
  const end = next.exec(script)?.index ?? script.length;
  return script.slice(start, end);
}

// --- a #tkBody whose columns are rebuilt, at scroll 0, on every write --------
const unesc = s => s.replace(/&(amp|lt|gt|quot|#39);/g, (_, e) =>
  ({amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'"}[e]));
function makeBody() {
  let markup = '', cols = [];
  return {
    scrollTop: 0, scrollLeft: 0, writes: 0,
    get innerHTML() { return markup; },
    set innerHTML(v) {
      markup = String(v); this.writes++;
      cols = [...markup.matchAll(/class="tcol-body" data-status="([^"]*)"(?: data-group="([^"]*)")?/g)]
        .map(m => ({dataset: m[2] === undefined ? {status: m[1]} : {status: m[1], group: unesc(m[2])},
                    scrollTop: 0}));
    },
    querySelectorAll(sel) { return sel === '.tcol-body' ? cols : []; },
  };
}
const body = makeBody();
const els = {tkBody: body};
const $ = id => els[id] ||= {dataset: {}, innerHTML: '', textContent: '', title: ''};
const col = (status, group) => body.querySelectorAll('.tcol-body')
  .find(c => c.dataset.status === status && (c.dataset.group ?? '') === (group ?? ''));

// --- the page's board code, with its state as globals -----------------------
const ticket = (over) => Object.assign({
  id: 'AG-1', title: 'A ticket', body: '', type: 'task', status: 'todo', priority: 'medium',
  project: '/repos/AgentGrid', projectName: 'AgentGrid', assignee: '', sessionId: '',
  labels: [], due: '', reporter: 'you', created: '2026-09-15T09:00:00',
  updated: '2026-09-15T09:00:00', closed: '', activity: [], rank: 1,
}, over);
const TICKETS = Array.from({length: 12}, (_, i) =>
  ticket({id: `AG-${i + 1}`, title: `Ticket ${i + 1}`, rank: i + 1,
          status: i < 10 ? 'todo' : 'in_progress',
          project: i % 2 ? '/repos/AgentGrid' : '/repos/draw-cal',
          projectName: i % 2 ? 'AgentGrid' : 'draw-cal'}));
const payload = (tickets = TICKETS) => JSON.parse(JSON.stringify({tickets, sessions: [], knownProjects: []}));

const ctx = vm.createContext({
  $, esc: s => String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
  ageStr: () => '2h', md: s => s, dueLabel: () => 'today', dueClass: () => 'now',
  renderTicketSelects() {}, setFilterCount() {},
  tkData: null, tkSig: '', tkPanelSig: '', tkRaw: '', tkOpen: null, tkDragging: false,
  tkLayout: 'board', tkGroup: '', tkArea: '', tkProject: '', tkType: '', tkAssignee: '',
  tkHidden: [], query: '', activeArea: '', areaData: {areas: [], members: {}},
  sessions: [{sessionId: 's-1', title: 'sonnet-1', status: 'working'}],
});
vm.runInContext(script.slice(script.indexOf('const TK_STATUSES'), script.indexOf('const tkPref')), ctx);
vm.runInContext('const tkStamp = iso => String(iso||"").replace("T"," ").slice(0,16);' +
                'const tkAge = () => "2h";' +
                'const tkLive = t => (t.sessionId ? sessions.find(s => s.sessionId === t.sessionId) : null);', ctx);
vm.runInContext(script.slice(script.indexOf('function tkAreaHue'), script.indexOf('const tkSetArea')), ctx);
['tkTake', 'tkFiltered', 'tkGroupOf', 'tkGroups', 'tkDueHtml', 'tkCardHtml', 'tkBoardHtml',
 'tkListHtml', 'tkEmptyHtml', 'renderTickets', 'tkScrollKeep', 'tkScrollRestore']
  .forEach(name => vm.runInContext(extract(name), ctx));
const run = expr => vm.runInContext(expr, ctx);
const poll = data => { ctx.__data = data; run('tkTake(__data); renderTickets()'); };

// --- a poll that brings nothing new leaves the board alone -------------------
poll(payload());
assert.equal(body.writes, 1, 'the first poll draws the board');
assert.ok(col('todo'), 'the To do column is drawn');
col('todo').scrollTop = 480;                 // the reader scrolls down To do
body.scrollLeft = 120;                       // and across a narrow window
poll(payload());
poll(payload());
assert.equal(body.writes, 1, 'an unchanged poll does not rebuild the board');
assert.equal(col('todo').scrollTop, 480, 'so the column stays where it was read');

// --- a real change redraws, but every column keeps its place -----------------
col('in_progress').scrollTop = 60;
const moved = TICKETS.map(t => t.id === 'AG-3' ? {...t, title: 'Renamed by an agent', updated: '2026-09-18T20:00:00'} : t);
poll(payload(moved));
assert.equal(body.writes, 2, 'a changed ticket redraws the board');
assert.ok(body.innerHTML.includes('Renamed by an agent'), 'with the change in it');
assert.equal(col('todo').scrollTop, 480, 'To do keeps its scroll across the redraw');
assert.equal(col('in_progress').scrollTop, 60, 'and so does every other column');
assert.equal(col('done').scrollTop, 0, 'a column never scrolled stays at the top');
assert.equal(body.scrollLeft, 120, 'the board keeps its sideways scroll');

// --- redraws the reader did not cause keep the place too ---------------------
ctx.sessions = [{sessionId: 's-1', title: 'sonnet-1', status: 'idle'}];   // an agent goes idle
run('renderTickets()');
assert.equal(body.writes, 3, 'a session changing state redraws');
assert.equal(col('todo').scrollTop, 480, 'without moving the column');
ctx.tkOpen = 'AG-5';                                                      // a ticket opens
run('renderTickets()');
assert.equal(body.writes, 4);
assert.equal(col('todo').scrollTop, 480, 'opening a ticket leaves the column where it was');

// --- swimlanes: each lane's column keeps its own place -----------------------
ctx.tkGroup = 'project'; ctx.tkOpen = null;
run('renderTickets()');
const lanes = [...new Set(body.querySelectorAll('.tcol-body').map(c => c.dataset.group))];
assert.equal(lanes.length, 2, 'two project lanes');
col('todo', lanes[0]).scrollTop = 200;
col('todo', lanes[1]).scrollTop = 35;
poll(payload(moved.map(t => t.id === 'AG-4' ? {...t, updated: '2026-09-18T20:05:00'} : t)));
assert.equal(col('todo', lanes[0]).scrollTop, 200, 'each lane keeps its own scroll');
assert.equal(col('todo', lanes[1]).scrollTop, 35);

// --- the ticket panel is only forced to redraw when the board changed ---------
ctx.tkPanelSig = 'drawn';
poll(payload(JSON.parse(ctx.tkRaw).tickets));
assert.equal(ctx.tkPanelSig, 'drawn', 'an unchanged poll leaves the open ticket alone');
poll(payload(TICKETS));
assert.equal(ctx.tkPanelSig, '', 'a changed board lets the open ticket catch up');

console.log('Ticket board scroll: unchanged polls skip the redraw; redraws keep every column, lane and sideways scroll');
