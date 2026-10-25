#!/usr/bin/env python3
"""Repeatable stdio integration test using only a new temporary core and fake sessions."""
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = Path(__file__).resolve().parents[2]
EXPECTED = {
    'room_join', 'room_leave', 'room_who', 'room_read', 'room_say',
    'room_whiteboard_get', 'room_whiteboard_set', 'room_threads', 'room_thread_close',
    'room_moves', 'room_dnd', 'task_new', 'task_list', 'task_claim', 'task_submit',
    'task_close', 'task_park', 'task_revise', 'task_drop', 'task_amend',
}


class Client:
    def __init__(self, process):
        self.process = process
        self.serial = 0
        self.records = []
        self.called = set()

    def write(self, message):
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + '\n').encode())
        self.process.stdin.flush()

    def read(self):
        ready, _, _ = select.select([self.process.stdout], [], [], 35)
        assert ready, 'MCP response timed out'
        line = self.process.stdout.readline()
        assert line, 'MCP exited without response'
        return json.loads(line)

    def rpc(self, method, params=None, error=None):
        self.serial += 1
        message = {'jsonrpc': '2.0', 'id': self.serial, 'method': method}
        if params is not None:
            message['params'] = params
        self.write(message)
        response = self.read()
        self.records.append({'request': message, 'response': response})
        assert response['id'] == self.serial, response
        assert response['jsonrpc'] == '2.0', response
        if error is not None:
            assert response['error']['code'] == error, response
            return response['error']
        assert 'error' not in response, response
        return response['result']

    def tool(self, tool, session='mcp-a', fail=False, **arguments):
        self.called.add(tool)
        result = self.rpc('tools/call', {'name': tool, 'arguments': {'session': session, **arguments}})
        assert result['isError'] == fail, result
        assert len(result['content']) == 1 and result['content'][0]['type'] == 'text', result
        text = result['content'][0]['text']
        return text if fail else json.loads(text)


def direct_error(base_url, path, body):
    request = urllib.request.Request(base_url + path, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=5):
            raise AssertionError('Core unexpectedly allowed the forbidden action')
    except urllib.error.HTTPError as error:
        with error:
            return json.load(error)['error']


def stop(process, eof=False):
    if process is None:
        return
    if eof and process.poll() is None:
        process.stdin.close()
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()  # Only this recorded child PID.
        process.wait(timeout=5)


def main():
    base = Path(tempfile.mkdtemp(prefix='rooms-mcp-test-'))
    core = mcp = client = None
    report = {'tempDir': str(base), 'status': 'incomplete'}
    core_log = (base / 'core.log').open('wb')
    mcp_log = (base / 'mcp.stderr').open('wb')
    try:
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        base_url = f'http://127.0.0.1:{port}'
        env = {**os.environ, 'ROOMS_HOST': '127.0.0.1', 'ROOMS_PORT': str(port),
               'ROOMS_DATA': str(base / 'data'), 'ROOMS_URL': base_url,
               'PYTHONDONTWRITEBYTECODE': '1'}
        core = subprocess.Popen([str(REPO / 'bin/rooms-serve')], cwd=base, env=env,
                                stdout=core_log, stderr=subprocess.STDOUT)
        report.update(corePid=core.pid, url=base_url)
        print(f'Core PID={core.pid}; ROOMS_DATA={env["ROOMS_DATA"]}; ROOMS_URL={base_url}', flush=True)
        deadline = time.monotonic() + 10
        while True:
            assert core.poll() is None, (base / 'core.log').read_text()
            try:
                with urllib.request.urlopen(base_url + '/api/overview', timeout=1) as response:
                    assert json.load(response)['result'] == {'rooms': [], 'agents': [], 'globals': {}}
                break
            except (urllib.error.URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        mcp = subprocess.Popen([sys.executable, '-B', str(REPO / 'adapters/mcp/server.py')],
                               cwd=base, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=mcp_log, bufsize=0)
        client = Client(mcp)
        report['mcpPid'] = mcp.pid
        # The requested initialization-based protocol is explicitly negotiated.
        init = client.rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                       'clientInfo': {'name': 'rooms-test', 'version': '1'}})
        assert init['protocolVersion'] == '2025-11-25'
        assert init['capabilities'] == {'tools': {'listChanged': False}}
        client.write({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        listed = client.rpc('tools/list')['tools']
        assert len(listed) == 20 and {tool['name'] for tool in listed} == EXPECTED
        for tool in listed:
            assert 'session' in tool['inputSchema']['required']
            assert 'as' not in tool['inputSchema']['properties']
        report['tools'] = sorted(EXPECTED)
        print('PASS initialize -> tools/list: 20 tools; session identity; no listen', flush=True)

        def cli(*args):
            result = subprocess.run([str(REPO / 'bin/room'), *args], cwd=base, env=env,
                                    capture_output=True, text=True, timeout=10, check=True)
            assert not result.stderr, result.stderr
            return json.loads(result.stdout)

        assert cli('register', '--session', 'mcp-a')['session'] == 'mcp-a'
        room_id = cli('create', 'MCP 測試', '--session', 'mcp-a')['room_id']
        for session in ('mcp-a', 'mcp-b'):
            joined = client.tool('room_join', session, room_id=room_id)
            assert joined['session'] == session and joined['room_id'] == room_id
        post = client.tool('room_say', text='MCP stdio 測試\n第二行 @mcp-b')
        assert post['from'] == 'mcp-a' and post['room_id'] == room_id
        task = client.tool('task_new', objective='完成 MCP 測試', criteria='mcp-b 提交，由 mcp-a 檢查')
        task_id = task['id']
        assert task['verifier'] == 'mcp-a'
        assert client.tool('task_claim', 'mcp-b', task_id=task_id)['status'] == 'active'
        assert client.tool('task_submit', 'mcp-b', task_id=task_id, note='已測試 stdio 流程')['status'] == 'review'
        assert client.tool('task_close', task_id=task_id, result='achieved', note='檢查通過')['status'] == 'achieved'
        print(f'PASS CLI register/create {room_id} -> MCP join(a,b)/say/new/claim/submit/close {task_id}', flush=True)

        who = client.tool('room_who')
        assert {member['session'] for member in who['members']} == {'mcp-a', 'mcp-b'}
        history = client.tool('room_read', since=post['seq'] - 1, limit=1)
        assert len(history) == 1 and history[0] == post
        assert client.tool('room_whiteboard_set', text='白板\nMCP') is None
        assert client.tool('room_whiteboard_get', 'mcp-b', room_id=room_id) == '白板\nMCP'
        # mcp-b 領任務、交件就算回應了 @，提及的線頭已經自動關掉；再 @ 一次開一條新的來測關閉
        client.tool('room_say', text='再確認一次 @mcp-b')
        threads = client.tool('room_threads')
        mention = next(item for item in threads if item['kind'] == 'mention')
        assert client.tool('room_thread_close', 'mcp-b', thread_id=mention['id'], note='已處理')['closed']
        assert not client.tool('room_threads', **{'all': False})
        assert len(client.tool('room_threads', **{'all': True})) >= 2
        assert client.tool('room_moves', 'mcp-b')[-1]['to'] == room_id
        assert client.tool('room_dnd', 'mcp-b', on=True)['dnd'] is True
        assert client.tool('room_say', 'mcp-b', text='勿擾中照指令發送')['from'] == 'mcp-b'
        assert client.tool('room_dnd', 'mcp-b', on=False)['dnd'] is False
        assert client.tool('task_list')[0]['id'] == task_id
        assert client.tool('task_amend', task_id=task_id, to='failed', reason='測試更正')['status'] == 'failed'
        extra = client.tool('task_new', objective='暫停流程', criteria='測試全部入口')['id']
        client.tool('task_claim', 'mcp-b', task_id=extra)
        assert client.tool('task_park', 'mcp-b', task_id=extra, reason='測試暫停')['status'] == 'parked'
        changed = client.tool('task_revise', task_id=extra, objective='改寫後目標', criteria='新條件', reason='測試改寫')
        assert changed['objective'] == '改寫後目標' and changed['criteria'] == '新條件'
        assert client.tool('task_drop', task_id=extra, reason='測試取消')['status'] == 'dropped'
        print('PASS who/read(since,limit)/whiteboard/threads(all)/close/moves/dnd/list/amend/park/revise/drop', flush=True)

        own = client.tool('task_new', objective='自我檢查禁止測試', criteria='核心必須拒絕', verifier='mcp-b')['id']
        client.tool('task_claim', 'mcp-b', task_id=own)
        client.tool('task_submit', 'mcp-b', task_id=own, note='只用來驗證拒絕')
        original = direct_error(base_url, f'/api/tasks/{own}/close',
                                {'as': 'mcp-b', 'result': 'achieved', 'note': '自我檢查'})
        forwarded = client.tool('task_close', 'mcp-b', fail=True, task_id=own,
                                result='achieved', note='自我檢查')
        assert original == forwarded == '不能檢查自己交的任務'
        report['coreError'] = {'original': original, 'forwarded': forwarded, 'isError': True}
        print(f'PASS core error verbatim, isError=true: {forwarded}', flush=True)
        assert client.tool('room_leave', 'mcp-b')['room_id'] is None
        assert client.tool('room_who', 'mcp-b', fail=True) == '你現在不在任何房間。先 room join <房間>'
        assert client.tool('room_read', 'mcp-b', fail=True, room_id=room_id) == '你不在這間房，看不到它的內容'
        assert client.called == EXPECTED
        print('PASS all 20 tools exercised; current-room lookup and core room boundary preserved', flush=True)

        client.rpc('server/discover', error=-32601)
        client.rpc('tools/call', {'name': 'listen', 'arguments': {}}, error=-32602)
        client.process.stdin.write(b'{broken-json\n')
        client.process.stdin.flush()
        assert client.read()['error']['code'] == -32700
        client.process.stdin.write(b'[]\n')
        client.process.stdin.flush()
        assert client.read()['error']['code'] == -32600
        # Notifications must not cause tool side effects or produce replies.
        client.write({'jsonrpc': '2.0', 'method': 'tools/call',
                      'params': {'name': 'room_say', 'arguments': {'session': 'mcp-a', 'text': 'MUST-NOT-SEND'}}})
        assert client.rpc('ping') == {}
        assert all(event.get('text') != 'MUST-NOT-SEND' for event in client.tool('room_read'))
        assert client.tool('room_dnd', fail=True, on='true') == 'on 必須是 boolean'
        print('PASS JSON-RPC errors, notification silence, ping and parameter types', flush=True)
        report['status'] = 'passed'
    finally:
        stop(mcp, eof=True)
        stop(core)
        core_log.close()
        mcp_log.close()
        report['cleanup'] = {'coreStopped': core is None or core.poll() is not None,
                             'mcpStopped': mcp is None or mcp.poll() is not None,
                             'mcpExitCode': mcp.returncode if mcp else None}
        if client:
            (base / 'transcript.json').write_text(json.dumps(client.records, ensure_ascii=False, indent=2) + '\n')
        (base / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('Cleanup: only recorded core/MCP children stopped; report=' + str(base / 'verification.json'), flush=True)
    assert mcp.returncode == 0, mcp.returncode
    assert not (base / 'mcp.stderr').read_text(), (base / 'mcp.stderr').read_text()
    print('PASS all checks; MCP exited cleanly on stdin EOF', flush=True)


if __name__ == '__main__':
    main()
