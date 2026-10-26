"""Offline tests: no real daemon or conversation is contacted."""
import base64
import hashlib
import io
import json
import struct
import unittest
from unittest.mock import patch

import cli_bridge as bridge


class FakeDaemon:
    def __init__(self, statuses=('idle',), turn_status='inProgress'):
        self.statuses = iter(statuses)
        self.kind = 'idle'
        self.turn_status = turn_status
        self.calls = []
        self.closed = False

    def call(self, method, params, mutation=False):
        self.calls.append((method, params, mutation))
        if method == 'thread/read':
            self.kind = next(self.statuses, self.kind)
            return {'thread': {'status': {'type': self.kind}}}
        if method == 'thread/turns/list':
            return {'data': [{'id': 'turn-1', 'status': self.turn_status}]}
        if method == 'turn/start':
            return {'turn': {'id': 'turn-new', 'status': 'inProgress'}}
        if method == 'turn/steer':
            return {'turnId': params['expectedTurnId']}
        return {}

    def send(self, obj):
        self.calls.append((obj['method'], {}, False))

    def close(self):
        self.closed = True


def request(method, **extra):
    params = {'threadId': 'test-thread'}
    if method != 'thread/state':
        params['input'] = [{'type': 'text', 'text': '暗號'}]
    return {'id': 'request-7', 'method': method, 'params': {**params, **extra}}


def frame(payload, opcode=1, final=True):
    size = len(payload)
    header = bytes([opcode | (128 if final else 0)])
    length = bytes([size]) if size < 126 else b'\x7e' + struct.pack('>H', size) if size < 65536 else b'\x7f' + struct.pack('>Q', size)
    return header + length + payload


class FakeSocket:
    def __init__(self, response=b'', fail=None):
        self.response = response
        self.fail = fail
        self.writes = []
        self.closed = False

    def settimeout(self, value):
        pass

    def connect(self, path):
        pass

    def sendall(self, data):
        self.writes.append(data)
        if data.startswith(b'GET /rpc'):
            key = data.split(b'Sec-WebSocket-Key: ')[1].split(b'\r\n')[0]
            accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
            self.response = (b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n' + self.response)

    def recv(self, size):
        if not self.response and self.fail:
            raise self.fail
        data, self.response = self.response[:size], self.response[size:]
        return data

    def close(self):
        self.closed = True


class AdapterTests(unittest.TestCase):
    def execute(self, daemon, req):
        with patch.object(bridge, 'WS', return_value=daemon):
            return bridge.execute(req)

    def test_state_active_id_without_resume(self):
        daemon = FakeDaemon(('active',))
        state = self.execute(daemon, request('thread/state'))
        self.assertTrue(state['isRunning'])
        self.assertEqual(state['activeTurnId'], 'turn-1')
        self.assertNotIn('thread/resume', [c[0] for c in daemon.calls])
        self.assertTrue(daemon.closed)

    def test_start_inherits_settings(self):
        daemon = FakeDaemon()
        self.execute(daemon, request('turn/start'))
        self.assertIn(('thread/resume', {'threadId': 'test-thread', 'excludeTurns': True}, False), daemon.calls)
        self.assertEqual(daemon.calls[-1], ('turn/start', {
            'threadId': 'test-thread', 'input': [{'type': 'text', 'text': '暗號', 'text_elements': []}]}, True))

    def test_steer_supplies_current_turn_id(self):
        daemon = FakeDaemon(('active',))
        result = self.execute(daemon, request('turn/steer', clientUserMessageId='marker'))
        self.assertEqual(result, {'turnId': 'turn-1'})
        self.assertEqual(daemon.calls[-1][1]['expectedTurnId'], 'turn-1')
        self.assertEqual(daemon.calls[-1][1]['clientUserMessageId'], 'marker')

    def test_guards_do_not_send(self):
        for method, status, error in [('turn/start', 'active', 'turn/steer'), ('turn/steer', 'idle', 'turn/start')]:
            with self.subTest(method=method):
                daemon = FakeDaemon((status,))
                with self.assertRaisesRegex(bridge.BridgeError, error):
                    self.execute(daemon, request(method))
                self.assertFalse(any(c[2] for c in daemon.calls))
                self.assertNotIn('thread/resume', [c[0] for c in daemon.calls])

    def test_changed_state_after_resume_blocks_start(self):
        daemon = FakeDaemon(('idle', 'active'))
        with self.assertRaisesRegex(bridge.BridgeError, 'use turn/steer'):
            self.execute(daemon, request('turn/start'))
        self.assertFalse(any(c[2] for c in daemon.calls))

    def test_stale_expected_id_and_inconsistent_snapshot_fail_closed(self):
        for daemon, req in [(FakeDaemon(('active',)), request('turn/steer', expectedTurnId='old')),
                            (FakeDaemon(('active',), 'completed'), request('turn/steer'))]:
            with self.assertRaises(bridge.BridgeError):
                self.execute(daemon, req)
            self.assertFalse(any(c[2] for c in daemon.calls))

    def test_member_model_and_effort_reach_the_turn(self):
        daemon = FakeDaemon(('idle', 'idle'))
        self.execute(daemon, request('turn/start', model='gpt-5.6-sol', effort='medium'))
        sent = [c[1] for c in daemon.calls if c[0] == 'turn/start']
        self.assertEqual((sent[0]['model'], sent[0]['effort']), ('gpt-5.6-sol', 'medium'))

    def test_invalid_input_never_connects(self):
        with patch.object(bridge, 'WS') as ws:
            for req in [None, {}, request('turn/start', input=[]), request('turn/start', model=3), request('turn/start', effort=''),
                        request('turn/steer', expectedTurnId=3)]:
                with self.assertRaises(bridge.BridgeError):
                    bridge.execute(req)
            ws.assert_not_called()

    def test_rpc_envelopes(self):
        with patch.object(bridge, 'WS', return_value=FakeDaemon()), patch('sys.stdin', io.StringIO(json.dumps(request('thread/state')))), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(bridge.main(['rpc']), 0)
            self.assertEqual(json.loads(out.getvalue())['id'], 'request-7')
        with patch('sys.stdin', io.StringIO('bad JSON')), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(bridge.main(['rpc']), 1)
            self.assertEqual(set(json.loads(out.getvalue())), {'error'})


class TransportTests(unittest.TestCase):
    def connect(self, sock):
        with patch.object(bridge.socket, 'socket', return_value=sock):
            return bridge.WS('/unused', timeout=1)

    def test_fragmented_message_with_ping_and_notifications(self):
        payload = json.dumps({'id': 1, 'result': {'text': '好'}}).encode()
        sock = FakeSocket(frame(b'{"method":"event"}') + frame(payload[:9], final=False) + frame(b'ping', opcode=9) + frame(payload[9:], opcode=0))
        ws = self.connect(sock)
        self.assertEqual(ws.call('test', {}), {'text': '好'})
        self.assertEqual(sock.writes[-1][0], 0x8a)  # masked pong
        self.assertTrue(sock.writes[-1][1] & 128)
        ws.close()

    def test_extended_lengths(self):
        for size in (200, 70000):
            data = {'id': 1, 'result': 'x' * size}
            sock = FakeSocket(frame(json.dumps(data).encode()))
            ws = self.connect(sock)
            self.assertEqual(ws.call('test', {'text': 'x' * size}), data['result'])
            self.assertTrue(sock.writes[-1][1] & 128)
            ws.close()

    def test_timeout_and_disconnect_after_send_never_retry(self):
        for fail in (None, TimeoutError('timeout')):
            sock = FakeSocket(fail=fail)
            ws = self.connect(sock)
            with self.assertRaisesRegex(bridge.BridgeError, '結果不確定，先查紀錄'):
                ws.call('turn/start', {}, mutation=True)
            self.assertEqual(len(sock.writes), 2)  # handshake plus exactly one mutation
            ws.close()

    def test_rpc_error_is_reported_verbatim(self):
        sock = FakeSocket(frame(b'{"id":1,"error":{"message":"turn mismatch"}}'))
        ws = self.connect(sock)
        with self.assertRaisesRegex(bridge.RPCError, '^turn mismatch$'):
            ws.call('turn/steer', {}, mutation=True)
        self.assertEqual(len(sock.writes), 2)
        ws.close()


if __name__ == '__main__':
    unittest.main()
