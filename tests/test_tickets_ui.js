// Run with: node tests/test_tickets_ui.js
//
// The board's own logic, without a browser: what the filters keep, how the
// swimlanes split, and what a card and a column actually render. These are the
// parts a Python test cannot reach and a human would otherwise have to click
// through on every change.
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
function constants() {
  const start = script.indexOf('const TK_STATUSES');
  return script.slice(start, script.indexOf('const tkPref'));
}

const ticket = (over) => Object.assign({
  id: 'DC-1', title: 'Login drops the session', body: '', type: 'task',
  status: 'todo', priority: 'medium', project: '/repos/draw-cal',
  projectName: 'draw-cal', assignee: '', sessionId: '', labels: [], due: '',
  reporter: 'you', created: '2026-09-15T09:00:00', updated: '2026-09-15T09:00:00',
  closed: '', activity: [], rank: 1,
}, over);

const TICKETS = [
  ticket({id: 'DC-1', type: 'bug', priority: 'urgent', assignee: 'sonnet-1',
          sessionId: 's-1', status: 'in_progress', title: 'Login drops the session'}),
  ticket({id: 'DC-2', type: 'spike', title: 'Pick a queue', labels: ['infra']}),
  ticket({id: 'AG-1', project: '/repos/AgentGrid', projectName: 'AgentGrid',
          title: 'Tidy the makefile', status: 'review', assignee: 'codex-2'}),
  ticket({id: 'AG-2', project: '/repos/AgentGrid', projectName: 'AgentGrid',
          title: 'Ship it', status: 'done'}),
];

const ctx = vm.createContext({
  esc: s => String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
  ageStr: () => '2h', md: s => s, dueLabel: () => 'today', dueClass: () => 'now',
  tkData: {tickets: TICKETS}, tkOpen: null, query: '',
  tkArea: '', areaData: {areas: [{id: 'a1', name: 'Marketing'}, {id: 'a2', name: 'Engineering'}], members: {}},
  tkProject: '', tkType: '', tkAssignee: '', tkGroup: '', tkHidden: [],
  sessions: [{sessionId: 's-1', title: 'sonnet-1', status: 'working', openable: true}],
});
vm.runInContext(constants(), ctx);
['tkFiltered', 'tkGroupOf', 'tkGroups', 'tkDueHtml', 'tkCardHtml', 'tkBoardHtml',
 'tkListHtml', 'tkEmptyHtml'].forEach(name => vm.runInContext(extract(name), ctx));
vm.runInContext('const tkStamp = iso => String(iso||"").replace("T"," ").slice(0,16);' +
                'const tkAge = () => "2h";' +
                'const tkLive = t => (t.sessionId ? sessions.find(s => s.sessionId === t.sessionId) : null);', ctx);

vm.runInContext(script.slice(script.indexOf('function tkAreaHue'), script.indexOf('const tkSetArea')), ctx);

const ids = list => list.map(t => t.id);
const run = expr => vm.runInContext(expr, ctx);

// ---- filters ---------------------------------------------------------------
assert.deepEqual(ids(run('tkFiltered()')), ['DC-1', 'DC-2', 'AG-1', 'AG-2']);

ctx.tkProject = '/repos/AgentGrid';
assert.deepEqual(ids(run('tkFiltered()')), ['AG-1', 'AG-2'], 'filters by project');
ctx.tkProject = '';

// a work area scopes the board; a ticket whose area was deleted reads as in none
TICKETS[1].area = 'a1'; TICKETS[2].area = 'a2'; TICKETS[3].area = 'deleted';
ctx.tkArea = 'a1';
assert.deepEqual(ids(run('tkFiltered()')), ['DC-2'], 'filters by work area');
ctx.tkArea = '∅';
assert.deepEqual(ids(run('tkFiltered()')), ['DC-1', 'AG-2'], 'no work area is its own choice');
ctx.tkArea = 'a2'; ctx.tkProject = '/repos/draw-cal';
assert.deepEqual(ids(run('tkFiltered()')), [], 'the area stacks with the other filters');
ctx.tkArea = ''; ctx.tkProject = '';
ctx.tkGroup = 'area';
assert.deepEqual(run('tkGroups(tkFiltered())').map(([name, items]) => [name, items.length]),
                 [['Engineering', 1], ['Marketing', 1], ['No work area', 2]], 'lanes by work area');
ctx.tkGroup = '';
assert.ok(run('tkCardHtml(tkData.tickets[1])').includes('Marketing'), 'a card names its work area');
// the same area is the same colour on every card, and a deleted one is neither
assert.equal(run('tkAreaHue("a1")'), run('tkAreaHue("a1")'), 'an area colour is stable');
assert.notEqual(run('tkAreaHue("a1")'), run('tkAreaHue("a2")'), 'two areas are told apart');
assert.equal(run('tkAreaHtml(tkData.tickets[3])'), '', 'a deleted area leaves no dot behind');
const areaList = run('tkListHtml(tkFiltered())');
assert.ok(areaList.includes('<th>Work area</th>'), 'the list has a work area column');
assert.ok(areaList.includes('Engineering'), 'and it is filled in');
assert.equal((areaList.match(/<th[ >]/g) || []).length,
             (areaList.split('<tr class="trow')[1].split('</tr>')[0].match(/<td/g) || []).length,
             'every header has a cell under it');

ctx.tkType = 'bug';
assert.deepEqual(ids(run('tkFiltered()')), ['DC-1'], 'filters by type');
ctx.tkType = '';

ctx.tkAssignee = 'sonnet-1';
assert.deepEqual(ids(run('tkFiltered()')), ['DC-1'], 'filters by assignee');
ctx.tkAssignee = '∅';
assert.deepEqual(ids(run('tkFiltered()')), ['DC-2', 'AG-2'], 'unassigned is its own choice');
ctx.tkAssignee = '';

ctx.query = 'queue';
assert.deepEqual(ids(run('tkFiltered()')), ['DC-2'], 'search reaches the title');
ctx.query = 'infra';
assert.deepEqual(ids(run('tkFiltered()')), ['DC-2'], 'search reaches the labels');
ctx.query = 'ag-1';
assert.deepEqual(ids(run('tkFiltered()')), ['AG-1'], 'search reaches the id, case-free');
ctx.query = '';

// ---- swimlanes -------------------------------------------------------------
ctx.tkGroup = 'project';
assert.deepEqual(run('tkGroups(tkFiltered())').map(([name, items]) => [name, items.length]),
                 [['AgentGrid', 2], ['draw-cal', 2]], 'lanes are one per project, sorted');
ctx.tkGroup = 'assignee';
const lanes = run('tkGroups(tkFiltered())').map(([name]) => name);
assert.deepEqual(lanes, ['codex-2', 'sonnet-1', 'Unassigned'],
                 'Unassigned sorts last, not alphabetically');
ctx.tkGroup = '';

// ---- a card ----------------------------------------------------------------
const card = run('tkCardHtml(tkData.tickets[0])');
assert.ok(card.includes('data-id="DC-1"'), 'the card carries its id for drag and click');
assert.ok(card.includes('draggable="true"'), 'a card can be dragged between columns');
assert.ok(card.includes('Login drops the session'), 'the title is on the card');
assert.ok(card.includes('sonnet-1'), 'so is the assignee');
assert.ok(card.includes('‼'), 'urgent is marked');
assert.ok(card.includes('tlive'), 'a live session shows as working');
const plain = run('tkCardHtml(tkData.tickets[1])');
assert.ok(plain.includes('unassigned'), 'an unclaimed ticket says so');
assert.ok(!plain.includes('tlive'), 'a ticket with no session has no working dot');

// escaping: a title is never markup
ctx.tkData.tickets.push(ticket({id: 'DC-9', title: '<img src=x onerror=alert(1)>'}));
assert.ok(!run('tkCardHtml(tkData.tickets[4])').includes('<img'), 'titles are escaped');
ctx.tkData.tickets.pop();

// ---- the board -------------------------------------------------------------
const shown = run('TK_STATUSES');
const board = run(`tkBoardHtml(tkFiltered(), TK_STATUSES)`);
assert.equal(shown.length, 5, 'five columns: backlog, todo, in progress, review, done');
for (const [, name] of shown) assert.ok(board.includes(name), `${name} column is drawn`);
assert.ok(board.includes('class="tcol-body"'), 'each column has a body to drop into');
// a card sits in the column its status names: split the bodies apart and look
const bodies = Object.fromEntries(board.split('class="tcol-body"').slice(1).map(chunk => {
  const status = /data-status="([a-z_]+)"/.exec(chunk);
  return [status ? status[1] : '?', chunk];
}));
assert.ok(bodies.in_progress.includes('DC-1'), 'the working ticket is In progress');
assert.ok(!bodies.in_progress.includes('DC-2'), 'and nothing else is');
assert.ok(bodies.review.includes('AG-1'), 'the one waiting on a human is In review');
assert.ok(bodies.done.includes('AG-2'), 'and the finished one is Done');

// ---- list layout and empty states ------------------------------------------
const list = run('tkListHtml(tkFiltered())');
assert.ok(list.includes('<table'), 'the list layout is a table');
assert.ok(list.includes('DC-1') && list.includes('AG-2'), 'every ticket has a row');
assert.ok(run('tkEmptyHtml(true)').includes('Nothing matches'), 'filtered-to-nothing says so');
assert.ok(run('tkEmptyHtml(false)').includes('ag ticket new'),
          'the first-run empty state teaches the agent command');

console.log('Ticket board: filters, swimlanes, cards, columns, list and empty states passed');
