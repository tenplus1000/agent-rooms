# 房間核心的 stdio MCP 包裝

使用 Python 3.10+ 標準函式庫，將 20 個 MCP 工具轉成既有核心的 HTTP 呼叫。
不需要 pip 套件。核心須先啟動；本程式不會代為啟動核心。

```bash
ROOMS_URL=http://127.0.0.1:8775 python3 /path/to/agent-rooms/adapters/mcp/server.py
```

`ROOMS_URL` 未設定時預設 `http://127.0.0.1:8775`。
stdio 每行一個 UTF-8 JSON-RPC 訊息，stdout 僅輸出協定回應。
stdin EOF 後程式結束；HTTP 呼叫逾時為 30 秒，不自動重試。

## 註冊方式（由使用者自行執行）

以下只有操作說明，此次實作與驗收沒有執行註冊，也沒有修改任何 Codex／Claude 設定。
指令格式已對照本機 `claude mcp add --help` 與 `codex mcp add --help`。
請在 WSL 執行，並依實際位置調整絕對路徑與核心網址。

Claude Code（使用者範圍）：

```bash
claude mcp add --transport stdio --scope user \
  --env ROOMS_URL=http://127.0.0.1:8775 \
  agent-rooms -- python3 /path/to/agent-rooms/adapters/mcp/server.py
```

Codex（目前帳號／CODEX_HOME）：

```bash
codex mcp add agent-rooms \
  --env ROOMS_URL=http://127.0.0.1:8775 \
  -- python3 /path/to/agent-rooms/adapters/mcp/server.py
```

註冊後重新開啟對應客戶端對話即可載入工具。每次呼叫仍須明確提供呼叫者的
`session`；包裝層不讀取目前 Codex／Claude 對話 ID，也不自動代選身分。

## 工具與 HTTP 對應

所有工具都有必填字串 `session`，轉成 HTTP 的 `as`。
`room_join`、`room_leave`、`room_moves`、`room_dnd` 的 HTTP `session`
也固定填同一個值，只操作呼叫者自己。

下表的參數不再重複列出 `session`；`?` 表示可省略。

| MCP 工具 | 其餘參數 | HTTP 入口 |
| --- | --- | --- |
| `room_join` | `room_id`, `kind?`, `name?` | `POST /api/register`，然後 `POST /api/move` |
| `room_leave` | — | `POST /api/move`，`room_id: null` |
| `room_who` | `room_id?` | `GET /api/rooms/{room_id}` |
| `room_read` | `room_id?`, `since?`, `limit?` | `GET /api/rooms/{room_id}/history` |
| `room_say` | `room_id?`, `text` | `POST /api/rooms/{room_id}/post` |
| `room_whiteboard_get` | `room_id?` | `GET /api/rooms/{room_id}/whiteboard` |
| `room_whiteboard_set` | `room_id?`, `text` | `PUT /api/rooms/{room_id}/whiteboard` |
| `room_threads` | `room_id?`, `all?` | `GET /api/rooms/{room_id}/threads` |
| `room_thread_close` | `thread_id`, `note?` | `POST /api/threads/{thread_id}/close` |
| `room_moves` | — | `GET /api/moves` |
| `room_dnd` | `on` | `POST /api/dnd` |
| `task_new` | `room_id?`, `objective`, `criteria`, `verifier?` | `POST /api/rooms/{room_id}/tasks` |
| `task_list` | `room_id?` | `GET /api/rooms/{room_id}/tasks` |
| `task_claim` | `task_id` | `POST /api/tasks/{task_id}/claim` |
| `task_submit` | `task_id`, `note` | `POST /api/tasks/{task_id}/submit` |
| `task_close` | `task_id`, `result`, `note` | `POST /api/tasks/{task_id}/close` |
| `task_park` | `task_id`, `reason` | `POST /api/tasks/{task_id}/park` |
| `task_revise` | `task_id`, `objective?`, `criteria?`, `reason` | `POST /api/tasks/{task_id}/revise` |
| `task_drop` | `task_id`, `reason` | `POST /api/tasks/{task_id}/drop` |
| `task_amend` | `task_id`, `to`, `reason` | `POST /api/tasks/{task_id}/amend` |

`since`、`limit` 是整數；`all`、`on` 是布林值，其餘皆為字串。
省略可選欄位時沿用核心預設，例如 history 的 `since=0`、`limit=200`。
`result` 的合法值為 `achieved`／`failed`；`to` 另接受 `review`，由核心驗證。

需要房間而未給 `room_id` 時，照 `rooms/cli.py` 的 `current_room()`：
先 `GET /api/rooms?as=<session>`，取第一筆房間；空清單則提示先 join。
使用者（`--user` 身分）必須明確給 `room_id`；此情況保留 CLI 的提示原文（其中寫的是 `--room R1`）。
任務動作與線頭關閉直接使用指定 ID，權限與狀態均交給核心判定。

`room_join` 成功回傳 move 的結果；register 成功而 move 失敗時，
已完成的登記仍保留，與 CLI 一致，不會自動回滾或重送。

沒有 `listen` 工具。監聽請直接使用：

```bash
bin/room listen --session YOUR_SESSION_ID
```

包裝層不判斷要叫醒誰、何時發訊息或如何處理勿擾；不呼叫 CLI／桌面轉接器。

## 協定與回傳

依指定的 initialize 握手模式實作，支援 `2024-11-05`、`2025-03-26`、
`2025-06-18`、`2025-11-25`。若客戶端要求其他版本，initialize 回應
`2025-11-25`，由客戶端決定能否相容；沒有實作新版無握手模式。
依據 MCP 官方的 [lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)、
[stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
與 [tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) 規格。

支援 `initialize`、`notifications/initialized`、`ping`、`tools/list`、`tools/call`；
notifications 不回覆且不執行工具。未知方法、無效 JSON／JSON-RPC 回傳協定錯誤。
工具清單一次列完 20 個，沒有分頁或清單變更通知。

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"example","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"room_read","arguments":{"session":"mcp-a","since":0,"limit":20}}}
```

成功結果是 `isError: false`，`content[0].text` 為 HTTP `result` 的 JSON 編碼，
包含原本的物件、清單、字串或 null。若要程式處理，對該 text 再做一次 JSON parse。
核心拒絕時，`content[0].text` 直接放核心 `error` 原文，不加前綴：

```json
{"jsonrpc":"2.0","id":4,"result":{"content":[{"type":"text","text":"不能檢查自己交的任務"}],"isError":true}}
```

輸入格式與 HTTP 連線錯誤也以 `isError: true` 回報。

## 可重跑驗收

```bash
cd /path/to/agent-rooms
python3 -B adapters/mcp/test_mcp.py
```

每次建立新的 `/tmp/rooms-mcp-test-*` 資料目錄與本機空閒埠，
啟動 `bin/rooms-serve` 並記錄 PID，再啟動 MCP 子程序。
只用假 session `mcp-a`、`mcp-b`，不連實際對話、不讀寫正式 `data/`。
結束時只停止自行建立的核心與 MCP 子程序；暫存證據保留供查核。

2026-10-24 驗收全部通過：

- initialize 與 20 工具清單，身分欄位均為 `session`。
- CLI register/create 後，MCP join → say → task_new → claim → submit → close。
- 全部 20 個工具均實際呼叫，含 history 篩選、白板、線頭、勿擾及任務其他動作。
- 自我檢查被核心拒絕，HTTP 與 MCP 訊息逐字相同：`不能檢查自己交的任務`。
- JSON-RPC 錯誤、notification 無副作用、stdin EOF 正常結束。

測試紀錄與 PID／清理結果會寫進暫存資料夾的 `verification.json`。
