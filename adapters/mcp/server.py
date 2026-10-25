#!/usr/bin/env python3
"""stdio MCP facade for the existing rooms HTTP API; standard library only."""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from rooms import paths  # noqa: E402

paths.load_local_env()   # Codex 直接啟動這支、不經過 bin/，要自己讀 local.env 才知道使用者是誰
from rooms import identity  # noqa: E402

VERSIONS = ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25')


def field(kind, description):
    return {'type': kind, 'description': description}


SESSION = field('string', '呼叫者自己的 session id，對應 HTTP as。')
ROOM = field('string', 'room_id；省略時查詢自己目前所在房間。使用者必須指定。')
TEXT = field('string', '原文。')
NOTE = field('string', '說明或驗證紀錄。')
REASON = field('string', '原因。')
TASK = field('string', '任務 task_id，例如 T1。')


def tool(name, description, properties=None, required=()):
    return {'name': name, 'description': description, 'inputSchema': {
        'type': 'object', 'properties': {'session': SESSION, **(properties or {})},
        'required': ['session', *required], 'additionalProperties': False}}


TOOLS = [
    tool('room_join', '登記並移動自己進房間。', {'room_id': ROOM, 'kind': TEXT, 'name': TEXT}, ('room_id',)),
    tool('room_leave', '移動自己離開房間。'),
    tool('room_who', '讀取房間與成員。', {'room_id': ROOM}),
    tool('room_read', '讀取房間 history，不推進監聽游標。', {
        'room_id': ROOM, 'since': field('integer', '只取 seq 大於 since 的事件，預設 0。'),
        'limit': field('integer', '事件數量，預設 200。')}),
    tool('room_say', '向房間發送文字。', {'room_id': ROOM, 'text': TEXT}, ('text',)),
    tool('room_whiteboard_get', '讀取白板。', {'room_id': ROOM}),
    tool('room_whiteboard_set', '設定白板。', {'room_id': ROOM, 'text': TEXT}, ('text',)),
    tool('room_threads', '列出線頭。', {'room_id': ROOM, 'all': field('boolean', 'true 包含已關閉線頭，預設 false。')}),
    tool('room_thread_close', '關閉指定線頭。', {'thread_id': field('string', '線頭 thread_id，例如 L1。'), 'note': NOTE}, ('thread_id',)),
    tool('room_moves', '查詢自己的移動紀錄。'),
    tool('room_dnd', '設定自己的勿擾狀態。', {'on': field('boolean', 'true 開啟；false 關閉。')}, ('on',)),
    tool('task_new', '建立任務。', {'room_id': ROOM, 'objective': TEXT, 'criteria': TEXT, 'verifier': field('string', '檢查者 session；省略時由核心選用房間負責人。')}, ('objective', 'criteria')),
    tool('task_list', '列出房間任務。', {'room_id': ROOM}),
    tool('task_claim', '領取任務。', {'task_id': TASK}, ('task_id',)),
    tool('task_submit', '提交任務供檢查。', {'task_id': TASK, 'note': NOTE}, ('task_id', 'note')),
    tool('task_close', '檢查並結案。', {'task_id': TASK, 'result': field('string', 'achieved 或 failed，由核心驗證。'), 'note': NOTE}, ('task_id', 'result', 'note')),
    tool('task_park', '暫停任務。', {'task_id': TASK, 'reason': REASON}, ('task_id', 'reason')),
    tool('task_revise', '改寫任務。', {'task_id': TASK, 'objective': TEXT, 'criteria': TEXT, 'reason': REASON}, ('task_id', 'reason')),
    tool('task_drop', '放棄任務。', {'task_id': TASK, 'reason': REASON}, ('task_id', 'reason')),
    tool('task_amend', '更正結案。', {'task_id': TASK, 'to': field('string', 'achieved、failed 或 review，由核心驗證。'), 'reason': REASON}, ('task_id', 'to', 'reason')),
]
BY_NAME = {t['name']: t for t in TOOLS}
ROOM_ROUTES = {
    'room_who': ('GET', ''), 'room_read': ('GET', '/history'),
    'room_say': ('POST', '/post'), 'room_whiteboard_get': ('GET', '/whiteboard'),
    'room_whiteboard_set': ('PUT', '/whiteboard'), 'room_threads': ('GET', '/threads'),
    'task_new': ('POST', '/tasks'), 'task_list': ('GET', '/tasks'),
}


class ToolError(Exception):
    pass


class ProtocolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def http(method, path, **params):
    base = os.environ.get('ROOMS_URL', 'http://127.0.0.1:8775').rstrip('/')
    url = base + path
    data = None
    if method == 'GET':
        query = {k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()}
        if query:
            url += '?' + urllib.parse.urlencode(query)
    else:
        data = json.dumps(params, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            payload = json.load(error)
    # Core errors must reach the caller verbatim, without a prefix or rewrite.
    if not payload.get('ok'):
        raise ToolError(payload['error'])
    return payload['result']


def current_room(session, given=None):
    if given:
        return given
    rooms = http('GET', '/api/rooms', **{'as': session})
    if session == identity.USER:
        raise ToolError('使用者要指定房間：--room R1')
    if not rooms:
        raise ToolError('你現在不在任何房間。先 room join <房間>')
    return rooms[0]['room_id']


def validate(name, arguments):
    schema = BY_NAME[name]['inputSchema']
    if not isinstance(arguments, dict):
        raise ToolError('arguments 必須是 object')
    missing = set(schema['required']) - arguments.keys()
    unknown = arguments.keys() - schema['properties'].keys()
    if missing:
        raise ToolError('缺少參數：' + ', '.join(sorted(missing)))
    if unknown:
        raise ToolError('未知參數：' + ', '.join(sorted(unknown)))
    types = {'string': str, 'integer': int, 'boolean': bool}
    for key, value in arguments.items():
        kind = schema['properties'][key]['type']
        if type(value) is not types[kind]:
            raise ToolError(f'{key} 必須是 {kind}')


def invoke(name, arguments):
    validate(name, arguments)
    params = dict(arguments)
    session = params.pop('session')
    actor = {'as': session}
    if name == 'room_join':
        room_id = params.pop('room_id')
        http('POST', '/api/register', session=session, **params)
        return http('POST', '/api/move', session=session, room_id=room_id, **actor)
    if name == 'room_leave':
        return http('POST', '/api/move', session=session, room_id=None, **actor)
    if name in ('room_moves', 'room_dnd'):
        method, path = ('GET', '/api/moves') if name == 'room_moves' else ('POST', '/api/dnd')
        return http(method, path, session=session, **params, **actor)
    if name in ROOM_ROUTES:
        room_id = current_room(session, params.pop('room_id', None))
        method, suffix = ROOM_ROUTES[name]
        path = '/api/rooms/' + urllib.parse.quote(room_id, safe='') + suffix
    elif name == 'room_thread_close':
        path = '/api/threads/' + urllib.parse.quote(params.pop('thread_id'), safe='') + '/close'
        method = 'POST'
    else:
        path = '/api/tasks/' + urllib.parse.quote(params.pop('task_id'), safe='') + '/' + name.removeprefix('task_')
        method = 'POST'
    return http(method, path, **params, **actor)


def tool_result(text, error=False):
    return {'content': [{'type': 'text', 'text': text}], 'isError': error}


class Server:
    def __init__(self):
        self.initialized = False

    def dispatch(self, method, params):
        if method == 'initialize':
            if (not isinstance(params.get('protocolVersion'), str)
                    or not isinstance(params.get('capabilities'), dict)
                    or not isinstance(params.get('clientInfo'), dict)):
                raise ProtocolError(-32602, 'initialize requires protocolVersion, capabilities, clientInfo')
            version = params['protocolVersion']
            self.initialized = True
            return {'protocolVersion': version if version in VERSIONS else VERSIONS[-1],
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': {'name': 'agent-rooms', 'version': '1.0.0'}}
        if method == 'ping':
            return {}
        if method not in ('tools/list', 'tools/call'):
            raise ProtocolError(-32601, 'Method not found: ' + method)
        if not self.initialized:
            raise ProtocolError(-32000, 'Initialize first')
        if method == 'tools/list':
            if params.get('cursor') is not None:
                raise ProtocolError(-32602, 'This tool list has no pagination cursor')
            return {'tools': TOOLS}
        name = params.get('name')
        if not isinstance(name, str) or name not in BY_NAME:
            raise ProtocolError(-32602, 'Unknown tool')
        try:
            result = invoke(name, params.get('arguments', {}))
            return tool_result(json.dumps(result, ensure_ascii=False))
        except ToolError as error:
            return tool_result(str(error), error=True)
        except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError) as error:
            return tool_result('房間 HTTP 呼叫失敗：' + str(error), error=True)

    def handle(self, message):
        request_id = message.get('id') if isinstance(message, dict) else None
        try:
            if (not isinstance(message, dict) or message.get('jsonrpc') != '2.0'
                    or not isinstance(message.get('method'), str)
                    or ('id' in message and type(request_id) not in (str, int))):
                raise ProtocolError(-32600, 'Invalid Request')
            # Notifications never execute tools and never generate a response.
            if 'id' not in message:
                return None
            params = message.get('params', {})
            if not isinstance(params, dict):
                raise ProtocolError(-32602, 'params must be an object')
            result = self.dispatch(message['method'], params)
            return {'jsonrpc': '2.0', 'id': request_id, 'result': result}
        except ProtocolError as error:
            return {'jsonrpc': '2.0', 'id': request_id if type(request_id) in (str, int) else None,
                    'error': {'code': error.code, 'message': str(error)}}


def main():
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    server = Server()
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        else:
            response = server.handle(message)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
