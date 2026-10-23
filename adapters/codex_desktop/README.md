# Windows Codex desktop entry point

The running desktop already exposes `\\.\pipe\codex-ipc`. It is a framed local IPC router, **not** the CLI daemon WebSocket. This adapter connects to that existing router and discovers the owner of the requested desktop chat. It does not launch a second agent, edit transcripts, or open a TCP port.

## WSL usage

```bash
BRIDGE=/home/<you>/agent-rooms/adapters/codex_desktop/codex-desktop
$BRIDGE thread/owner THREAD_ID
$BRIDGE thread/settings THREAD_ID
$BRIDGE thread/state THREAD_ID
$BRIDGE turn/steer THREAD_ID --text 'Message while the agent is working'
$BRIDGE turn/start THREAD_ID --text 'Message when the agent is idle'
```

For programmatic callers, pipe one UTF-8 JSON request to `$BRIDGE rpc` and parse one JSON response. Example:

```json
{"id":"delivery-1","method":"turn/steer","params":{"threadId":"THREAD_ID","input":[{"type":"text","text":"Message"}]}}
```

Success returns `{"id":...,"result":...}`; errors return `{"error":{"message":...}}` with a nonzero exit code. Input currently supports plain text only. No model/permission changes are made: start inherits the chat's settings. `thread/state` returns the desktop snapshot, including `threadRuntimeStatus`.

## Routing

| Adapter method | Desktop IPC method |
|---|---|
| thread/owner | thread-owner-discovery |
| thread/settings | thread-follower-read-model-settings |
| thread/state | thread-stream-following-changed → snapshot |
| turn/start | thread-follower-start-turn → existing desktop turn/start |
| turn/steer | thread-follower-steer-turn → existing desktop turn/steer |

Frames: 4-byte little-endian UTF-8 JSON length, followed by JSON bytes. Initialize assigns a client ID. Owner discovery routes to the actual open desktop session. The bridge acts only as a follower and never claims ownership. Closing a browser panel is distinct from closing the desktop process; an available owner is required. A missing owner fails explicitly.

This is an internal, versioned desktop protocol, verified against installed app 26.1013.4894.0. Revalidate after desktop updates. It requires the Windows user's permission to access the named pipe; a sandboxed agent may need normal execution approval. No trust or sandbox checks are disabled.

Mutations have no automatic retries. If a timeout/disconnection occurs after submission, check the target transcript before resending. `clientUserMessageId` may be supplied for traceability; do not assume it guarantees deduplication. The active/idle preflight is advisory and may race with a finishing turn; desktop still validates the actual request.

The executable paths in the WSL launcher match 使用者's current machine. It uses Windows Node via WSL interop and converts the adjacent JS file path with `wslpath -w`; no third-party packages are required.

## Verification

- External owner discovery and live snapshot succeeded for chat `00000000-0000-0000-0000-000000000000`.
- `turn/steer` delivered marker `desktop-bridge-steer-20261023` into its running turn; the model received it as a new message, and IPC returned turn ID `00000000-0000-0000-0000-000000000000`.
- WSL `turn/steer` also delivered marker `desktop-bridge-wsl-20261023` through the installed Linux launcher into the same running desktop turn.
- Idle `turn/start` passed on 2026-10-23 at 22:59 Europe/Berlin: after the previous turn finished, the WSL launcher submitted marker `desktop-bridge-start-20261023` and the same desktop chat started turn `00000000-0000-0000-0000-000000000000`. The model received the marker in that new turn. Exit code was 0, with no stderr. The one-shot verifier exited after sending; no monitoring service remains.
- Machine-readable results are in `verification.json`. No other chat was used or modified for these delivery tests.
