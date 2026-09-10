"""OpenRouter text execution and discovery via a fixed HTTPS API endpoint.

No filesystem/shell tools are exposed. Workflow specialists receive the supplied
brief and handoffs. The coordinator's delegation is validated by TeamRun.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from agentgrid import credentials

BASE_URL = 'https://openrouter.ai/api/v1'
MAX_OUTPUT_TOKENS = 8192
_cache = (0., [])
_cache_lock = threading.Lock()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('OpenRouter returned an unexpected redirect.')


def request(path, key, body=None):
    if not key:
        raise ValueError('Connect OpenRouter in Providers before running this step.')
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data,
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                 'X-OpenRouter-Title': 'AgentGrid'})
    try:
        return urllib.request.build_opener(NoRedirect()).open(req, timeout=30)
    except urllib.error.HTTPError as error:
        reasons = {401: 'The OpenRouter key was rejected.', 402: 'OpenRouter has insufficient credits.',
                   429: 'OpenRouter rate limit reached. Try again later.'}
        raise ValueError(reasons.get(error.code, f'OpenRouter request failed (HTTP {error.code}).')) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError('Could not reach OpenRouter. Check your connection and try again.') from None


def check_connection(key=None):
    with request('/key', key or credentials.get_key()) as response:
        payload = json.load(response)
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError('OpenRouter returned an invalid key status.')
    return {k: data.get(k) for k in ('limit', 'limit_remaining', 'usage', 'is_free_tier')}


def model_catalog(refresh=False):
    global _cache
    key = credentials.get_key()
    if not key:
        return []
    with _cache_lock:
        if not refresh and time.monotonic() - _cache[0] < 300:
            return list(_cache[1])
    with request('/models', key) as response:
        data = json.load(response)
    if not isinstance(data, dict) or not isinstance(data.get('data'), list):
        raise ValueError('OpenRouter returned an invalid model catalog.')
    models = []
    for entry in data['data']:
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), str):
            continue
        architecture = entry.get('architecture')
        architecture = architecture if isinstance(architecture, dict) else {}
        output = architecture.get('output_modalities') or ['text']
        if 'text' not in output:
            continue
        models.append({'id': entry['id'], 'name': str(entry.get('name') or entry['id']),
                       'contextLength': entry.get('context_length'), 'pricing': entry.get('pricing', {}),
                       'tools': 'tools' in (entry.get('supported_parameters') or [])})
    models.sort(key=lambda x: x['name'].lower())
    with _cache_lock:
        _cache = (time.monotonic(), models)
    return models


def clear_cache():
    global _cache
    with _cache_lock:
        _cache = (0., [])


def sse_events(response):
    """SSE framing, including comments and multiline data fields."""
    lines = []
    for raw in response:
        line = raw.decode('utf-8').rstrip('\r\n')
        if not line:
            if lines:
                yield '\n'.join(lines)
                lines = []
        elif line.startswith('data:'):
            lines.append(line[5:].lstrip(' '))
    if lines:
        yield '\n'.join(lines)


def run_turn(room, message, model, attachments=None):
    if room._api_cancel.is_set():
        return
    if not model.strip():
        room._emit({'type': 'error', 'message': 'Choose an exact OpenRouter model ID for this step.'})
        return
    if attachments:
        room._emit({'type': 'error', 'message': 'OpenRouter steps currently accept text only.'})
        return
    with room._lock:
        room._running = True
    room._emit({'type': 'turn_started', 'model': model, 'engine': 'openrouter'})
    messages = room._api_history + [{'role': 'user', 'content': message}]
    text, usage, complete = '', {}, False
    try:
        key = credentials.get_key()
        with request('/chat/completions', key, {'model': model, 'messages': messages, 'stream': True,
                     'max_tokens': MAX_OUTPUT_TOKENS}) as response:
            room._api_response = response
            if room._api_cancel.is_set():
                return
            for data in sse_events(response):
                if room._api_cancel.is_set():
                    return
                if data == '[DONE]':
                    complete = True
                    break
                chunk = json.loads(data)
                if chunk.get('error'):
                    raise ValueError('OpenRouter reported a generation error. Check the model and provider status.')
                if isinstance(chunk.get('usage'), dict):
                    usage = chunk['usage']
                choices = chunk.get('choices') or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get('finish_reason') in ('length', 'content_filter', 'error', 'tool_calls'):
                    raise ValueError('OpenRouter did not finish a text response (output limit, filter, or unsupported tool request).')
                delta = choice.get('delta', {}).get('content')
                if isinstance(delta, str) and delta:
                    text += delta
                    room._emit({'type': 'assistant_delta', 'text': delta})
            if not complete:
                raise ValueError('OpenRouter disconnected before completing the response.')
        if not text.strip():
            raise ValueError('OpenRouter returned no text.')
        room._api_history = messages + [{'role': 'assistant', 'content': text}]
        room._emit({'type': 'assistant_message', 'text': text})
        room._emit({'type': 'turn_done', 'ok': True, 'result': text,
                    'stats': {'usage': usage, 'costUsd': usage.get('cost')}})
    except (ValueError, OSError, urllib.error.URLError, TypeError, AttributeError):
        if not room._api_cancel.is_set():
            # No raw transport/provider response is surfaced: it could contain
            # request headers or reflected source text, including credentials.
            import sys
            error = sys.exc_info()[1]
            message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else 'OpenRouter response could not be read.'
            room._emit({'type': 'error', 'message': message})
    finally:
        room._api_response = None
        with room._lock:
            room._running = False
