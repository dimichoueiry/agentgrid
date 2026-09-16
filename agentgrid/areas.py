"""Persistent work areas; session membership is independent of status and tags."""
import json
import os
import threading
import time
import uuid
from pathlib import Path

PATH = Path.home() / '.agentgrid' / 'areas.json'
_LOCK = threading.RLock()
_PENDING = []


def load():
    with _LOCK:
        try:
            return json.loads(PATH.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'areas': [{'id': name.lower(), 'name': name, 'prompt': ''}
                              for name in ('Engineering', 'Marketing', 'Design', 'Strategy')],
                    'members': {}}


def _save(data):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = PATH.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, PATH)


def update(body):
    with _LOCK:
        data = load()
        action = body.get('action')
        area_id = str(body.get('id') or '')
        area = next((a for a in data['areas'] if a['id'] == area_id), None)
        if action == 'save':
            name = str(body.get('name') or '').strip()
            if not name or len(name) > 60:
                raise ValueError('Use a work area name between 1 and 60 characters.')
            if area_id and not area:
                raise ValueError('Work area no longer exists.')
            if any(a['name'].casefold() == name.casefold() and a != area for a in data['areas']):
                raise ValueError('A work area with that name already exists.')
            prompt = str(body.get('prompt') or '')
            if len(prompt) > 20000:
                raise ValueError('Starter prompt must be under 20,000 characters.')
            if area is None:
                area = {'id': uuid.uuid4().hex}
                data['areas'].append(area)
            area.update(name=name, prompt=prompt)
        elif action == 'assign':
            sid = str(body.get('sessionId') or '')
            if not sid or len(sid) > 256 or (area_id and not area):
                raise ValueError('Choose a valid agent and work area.')
            if area_id:
                data['members'][sid] = area_id
            else:
                data['members'].pop(sid, None)
        elif action == 'delete':
            if not area:
                raise ValueError('Work area no longer exists.')
            data['areas'].remove(area)
            data['members'] = {s: a for s, a in data['members'].items() if a != area_id}
        else:
            raise ValueError('Unknown work area action.')
        _save(data)
        return data


def await_session(area_id, cwd, engine, interactive, job_id, known, started):
    with _LOCK:
        _PENDING.append(dict(area=area_id, cwd=cwd, engine=engine,
                             interactive=interactive, job=job_id,
                             known=set(known), started=started))


def resolve(sessions):
    """Resolve launches without session IDs using the same cwd/time fallback as naming."""
    with _LOCK:
        if not _PENDING:
            return
        data = load()
        changed = False
        for pending in list(_PENDING):
            if time.time() - pending['started'] > 300 or not any(
                    a['id'] == pending['area'] for a in data['areas']):
                _PENDING.remove(pending)
                continue
            for s in sessions:
                if s.session_id in pending['known'] or s.session_id in data['members']:
                    continue
                match = s.job_id == pending['job'] if pending['job'] else (
                    s.cwd == pending['cwd'] and getattr(s, 'engine', 'claude') == pending['engine']
                    and s.started_at / 1000 >= pending['started'] - 5
                    and (not pending['interactive'] or s.kind == 'interactive'))
                if match:
                    data['members'][s.session_id] = pending['area']
                    _PENDING.remove(pending)
                    changed = True
                    break
        if changed:
            _save(data)
