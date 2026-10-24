"""One-shot JSON-RPC bridge to an existing Codex CLI daemon (stdlib only)."""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import struct
import sys
import time

MAX_MESSAGE = 32 * 1024 * 1024
UNKNOWN = '結果不確定，先查紀錄。Do not retry automatically.'


class BridgeError(Exception):
    pass


class RPCError(BridgeError):
    pass


class WS:
    def __init__(self, path, timeout=30):
        self.timeout = timeout
        self.buf = b''
        self.serial = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.deadline = time.monotonic() + timeout
            self._remaining()
            self.sock.connect(path)
            key = base64.b64encode(os.urandom(16)).decode('ascii')
            self.sock.sendall((
                'GET /rpc HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                f'Sec-WebSocket-Key: {key}\r\n\r\n'
            ).encode('ascii'))
            while b'\r\n\r\n' not in self.buf:
                if len(self.buf) > 16384:
                    raise BridgeError('WebSocket handshake too large')
                self._read()
            head, self.buf = self.buf.split(b'\r\n\r\n', 1)
            lines = head.decode('ascii').split('\r\n')
            headers = {name.lower(): value.strip() for name, value in
                       (line.split(':', 1) for line in lines[1:])}
            accept = base64.b64encode(hashlib.sha1(
                (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')
            ).digest()).decode('ascii')
            if (lines[0].split()[1] != '101'
                    or headers.get('sec-websocket-accept') != accept
                    or headers.get('upgrade', '').lower() != 'websocket'
                    or 'upgrade' not in [v.strip().lower() for v in
                                         headers.get('connection', '').split(',')]):
                raise BridgeError('Invalid WebSocket upgrade response')
        except Exception:
            self.close()
            raise

    def close(self):
        self.sock.close()

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Daemon request timed out')
        self.sock.settimeout(remaining)

    def _read(self):
        self._remaining()
        part = self.sock.recv(65536)
        if not part:
            raise EOFError('Daemon disconnected')
        self.buf += part

    def _take(self, size):
        while len(self.buf) < size:
            self._read()
        data, self.buf = self.buf[:size], self.buf[size:]
        return data

    def _frame(self, data, opcode=1):
        if len(data) > MAX_MESSAGE:
            raise BridgeError('Request too large')
        mask = os.urandom(4)
        size = len(data)
        length = (bytes([0x80 | size]) if size < 126 else
                  b'\xfe' + struct.pack('>H', size) if size < 65536 else
                  b'\xff' + struct.pack('>Q', size))
        frame = bytes([0x80 | opcode]) + length + mask
        frame += bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._remaining()
        self.sock.sendall(frame)

    def send(self, obj):
        self._frame(json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def recv(self):
        message = bytearray()
        fragmented = False
        while True:
            self._remaining()
            b0, b1 = self._take(2)
            opcode, final, size = b0 & 15, bool(b0 & 128), b1 & 127
            if b0 & 112 or b1 & 128:
                raise BridgeError('Invalid server WebSocket frame')
            if size == 126:
                size = struct.unpack('>H', self._take(2))[0]
            elif size == 127:
                size = struct.unpack('>Q', self._take(8))[0]
            if size > MAX_MESSAGE or len(message) + size > MAX_MESSAGE:
                raise BridgeError('Daemon message too large')
            if opcode >= 8 and (not final or size > 125):
                raise BridgeError('Invalid WebSocket control frame')
            payload = self._take(size)
            if opcode == 8:
                raise EOFError('Daemon closed WebSocket')
            if opcode == 9:
                self._frame(payload, opcode=10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1) or (opcode == 0) != fragmented:
                raise BridgeError('Unexpected WebSocket data frame')
            message.extend(payload)
            if final:
                result = json.loads(message)
                if not isinstance(result, dict):
                    raise BridgeError('Daemon returned a non-object message')
                return result
            fragmented = True

    def call(self, method, params, mutation=False):
        self.serial += 1
        request_id = self.serial
        self.deadline = time.monotonic() + self.timeout
        try:
            self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
            while True:
                msg = self.recv()
                # Notifications and server requests are for the attached TUI.
                if msg.get('id') != request_id or 'method' in msg:
                    continue
                if 'error' in msg:
                    raise RPCError(msg['error'].get('message', str(msg['error'])))
                if 'result' not in msg:
                    raise BridgeError('Daemon response has no result')
                return msg['result']
        except RPCError:
            raise
        except Exception as error:
            if mutation:
                raise BridgeError(f'{error or type(error).__name__}; {UNKNOWN}') from error
            raise


def state(ws, thread_id):
    thread = ws.call('thread/read', {'threadId': thread_id, 'includeTurns': False})['thread']
    status = thread['status']
    kind = status['type']
    if kind not in ('idle', 'active', 'notLoaded', 'systemError'):
        raise BridgeError('Unknown thread status; no message sent')
    turn_id = None
    if kind == 'active':
        turns = ws.call('thread/turns/list', {
            'threadId': thread_id, 'limit': 1, 'sortDirection': 'desc', 'itemsView': 'notLoaded'
        })['data']
        if not turns or turns[0]['status'] != 'inProgress' or not turns[0].get('id'):
            raise BridgeError('Active turn could not be confirmed; no message sent. Query state again.')
        turn_id = turns[0]['id']
    return {'threadId': thread_id, 'threadRuntimeStatus': status,
            'isRunning': kind == 'active', 'activeTurnId': turn_id}


def guard(method, current, expected=None):
    kind = current['threadRuntimeStatus']['type']
    if method == 'turn/start' and kind == 'active':
        raise BridgeError('Thread is active; use turn/steer. No message sent.')
    if method == 'turn/steer' and kind != 'active':
        raise BridgeError('Thread is not active; use turn/start. No message sent.')
    if kind == 'systemError':
        raise BridgeError('Thread has a system error; no message sent.')
    if expected is not None and expected != current['activeTurnId']:
        raise BridgeError('expectedTurnId does not match the active turn; no message sent.')


def connect():
    codex_home = Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser()
    path = os.environ.get('CODEX_CLI_SOCKET', str(codex_home / 'app-server-control/app-server-control.sock'))
    timeout = float(os.environ.get('CODEX_CLI_TIMEOUT', '30'))
    if not math.isfinite(timeout) or timeout <= 0:
        raise BridgeError('CODEX_CLI_TIMEOUT must be a finite positive number')
    ws = WS(os.path.expanduser(path), timeout)
    try:
        ws.call('initialize', {'clientInfo': {'name': 'agent-rooms-cli-bridge', 'version': '1.0'}})
        ws.send({'jsonrpc': '2.0', 'method': 'initialized'})
    except Exception:
        ws.close()
        raise
    return ws


def start_thread(params):
    """Open a new daemon-owned thread with no window attached.

    Only cwd is given; model and instructions come from the account's config.
    The sandbox is workspace-write, like a window opened in a trusted folder (the daemon
    does not apply folder trust here and would fall back to read-only).
    Approvals are set to never: with no window, nobody could answer a prompt, so a
    command that needs one fails instead of waiting forever.
    """
    if set(params) - {'cwd'}:
        raise BridgeError('Unsupported parameters: ' + ', '.join(sorted(set(params) - {'cwd'})))
    cwd = params.get('cwd')
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise BridgeError('cwd must be an existing absolute directory')
    ws = connect()
    try:
        got = ws.call('thread/start', {'cwd': cwd, 'approvalPolicy': 'never', 'sandbox': 'workspace-write'},
                      mutation=True)
    finally:
        ws.close()
    return {'threadId': got['thread']['id'], 'cwd': got.get('cwd'), 'model': got.get('model'),
            'sandbox': got.get('sandbox'), 'approvalPolicy': got.get('approvalPolicy')}


def execute(request):
    if not isinstance(request, dict):
        raise BridgeError('Request must be a JSON object')
    method = request.get('method')
    if method not in ('thread/state', 'turn/start', 'turn/steer', 'thread/start'):
        raise BridgeError('Unsupported bridge method')
    params = request.get('params', {})
    if not isinstance(params, dict):
        raise BridgeError('params must be an object')
    if method == 'thread/start':
        return start_thread(params)
    thread_id = params.get('threadId')
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise BridgeError('threadId required')
    allowed = {'threadId'}
    if method != 'thread/state':
        allowed |= {'input', 'clientUserMessageId'}
    if method == 'turn/start':
        allowed.add('sandbox')
    if params.get('sandbox', 'workspace-write') != 'workspace-write':
        raise BridgeError('sandbox can only be workspace-write')
    if method == 'turn/steer':
        allowed.add('expectedTurnId')
    if set(params) - allowed:
        raise BridgeError('Unsupported parameters: ' + ', '.join(sorted(set(params) - allowed)))
    for key in ('expectedTurnId', 'clientUserMessageId'):
        if key in params and (not isinstance(params[key], str) or not params[key].strip()):
            raise BridgeError(f'{key} must be a non-empty string')
    inputs = params.get('input')
    if method != 'thread/state':
        if (not isinstance(inputs, list) or not inputs or any(
                not isinstance(i, dict) or i.get('type') != 'text'
                or not isinstance(i.get('text'), str) or not i['text'].strip() for i in inputs)):
            raise BridgeError('This adapter currently accepts non-empty text input only')
        inputs = [{'type': 'text', 'text': i['text'], 'text_elements': []} for i in inputs]
    ws = connect()
    try:
        current = state(ws, thread_id)
        if method == 'thread/state':
            return current
        guard(method, current, params.get('expectedTurnId'))
        # Rejoin the daemon-owned thread; do not override any of its settings.
        try:
            ws.call('thread/resume', {'threadId': thread_id, 'excludeTurns': True})
        except RPCError as error:
            # A thread opened by thread/start has no rollout until its first turn;
            # it is already loaded in the daemon, so it can take a turn as is.
            if 'no rollout found' not in str(error) or current['threadRuntimeStatus']['type'] != 'idle':
                raise
        current = state(ws, thread_id)
        guard(method, current, params.get('expectedTurnId'))
        if current['threadRuntimeStatus']['type'] not in ('idle', 'active'):
            raise BridgeError('Thread is not ready; no message sent.')
        outgoing = {'threadId': thread_id, 'input': inputs}
        if 'clientUserMessageId' in params:
            outgoing['clientUserMessageId'] = params['clientUserMessageId']
        if 'sandbox' in params:
            # Threads opened before thread/start set a sandbox were read-only; this turn
            # and the ones after it get workspace-write, with network like a trusted window.
            outgoing['sandboxPolicy'] = {'type': 'workspaceWrite', 'networkAccess': True}
        if method == 'turn/steer':
            outgoing['expectedTurnId'] = current['activeTurnId']
        return ws.call(method, outgoing, mutation=True)
    finally:
        ws.close()


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BridgeError(message)


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument('method', choices=['thread/state', 'turn/start', 'turn/steer', 'rpc'])
        parser.add_argument('thread_id', nargs='?')
        parser.add_argument('--text', help='For turn methods, omit to read text from stdin.')
        args = parser.parse_args(argv)
        if args.method == 'rpc':
            request = json.load(sys.stdin)
        else:
            if not args.thread_id:
                raise BridgeError('thread_id is required')
            request = {'method': args.method, 'params': {'threadId': args.thread_id}}
            if args.method.startswith('turn/'):
                text = args.text if args.text is not None else sys.stdin.read()
                request['params']['input'] = [{'type': 'text', 'text': text}]
        result = execute(request)
        print(json.dumps({'id': request.get('id'), 'result': result}, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({'error': {'message': str(error) or type(error).__name__}}, ensure_ascii=False))
        return 1
