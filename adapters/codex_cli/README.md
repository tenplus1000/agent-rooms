# WSL Codex CLI 轉接器

只用 Python 標準函式庫，連接既有 Codex CLI daemon 的 Unix socket，
透過 `/rpc` WebSocket 傳送 JSON-RPC。適用於 WSL 的 Codex CLI 對話；
桌面版請用相鄰的 `codex_desktop/` 轉接器。

## 用法

```bash
BRIDGE=/path/to/agent-rooms/adapters/codex_cli/codex-cli
$BRIDGE thread/state THREAD_ID
$BRIDGE turn/start THREAD_ID --text '閒置時送入的新訊息'
$BRIDGE turn/steer THREAD_ID --text '執行中補充的訊息'
printf '%s' '也可以從 stdin 讀取文字' | $BRIDGE turn/start THREAD_ID
```

請先在相同 Codex 帳號環境用 `codex resume THREAD_ID` 開啟目標 TUI。
轉接器不啟動 daemon 或 TUI，也不選擇收件者、不處理房間路由或勿擾規則。
只支援非空白純文字，不覆寫對話的模型、工作目錄、權限或指令設定。
無須安裝套件或註冊 MCP；目前直接使用上面的完整路徑即可。

環境變數：

- `CODEX_CLI_SOCKET`：明確指定 daemon socket，優先於預設路徑。
- 預設 socket：`$CODEX_HOME/app-server-control/app-server-control.sock`；
  未設定 `CODEX_HOME` 時使用 `~/.codex/app-server-control/app-server-control.sock`。
  不會搜尋或自動切換到其他帳號的 socket。
- `CODEX_CLI_TIMEOUT`：每筆 daemon RPC 的逾時秒數，預設 `30`，須為正有限值。

## JSON 介面

`rpc` 從 stdin 讀一個完整 JSON 請求，在 stdout 輸出一行 JSON。
支援與命令列相同的三種方法，並非任意 app-server RPC 的轉送入口。
另外支援 `thread/start`（只收 `cwd`）：在 daemon 開一個沒有視窗的新對話，回 `threadId`。
模型、沙盒照帳號設定；核准一律設成 `never`，因為沒有視窗能按核准，需要核准的指令會直接失敗而不是卡住。
新對話在第一輪之前沒有紀錄檔，`turn/start` 會略過 `thread/resume` 直接送。

```bash
printf '%s' '{"id":"delivery-1","method":"turn/steer","params":{"threadId":"THREAD_ID","input":[{"type":"text","text":"補充訊息"}]}}' | "$BRIDGE" rpc
```

成功與桌面版相同：`{"id":...,"result":...}`，exit 0。
CLI 沒有指定 request ID 時回傳 `"id":null`。
錯誤為 `{"error":{"message":...}}`，exit 1。
`turn/start` 的 result 原樣保留 daemon 的 `turn`；
`turn/steer` 原樣保留 `turnId`。接受成功不代表該輪已經完成。

`thread/state` 的 result 範例：

```json
{
  "threadId": "THREAD_ID",
  "threadRuntimeStatus": {"type": "active", "activeFlags": []},
  "isRunning": true,
  "activeTurnId": "TURN_ID"
}
```

`threadRuntimeStatus` 保留 daemon 的狀態物件，可能為 `idle`、`active`、
`notLoaded`、`systemError`。未執行時 `activeTurnId` 為 null；
`notLoaded` 表示此 daemon 沒有載入該對話，不能解讀成其他 daemon 也未執行。
查詢狀態本身不 resume 對話。

`turn/steer` 會查詢並帶入 `expectedTurnId`。RPC 呼叫者也可以提供此欄位，
若與目前 active turn 不符即拒絕送出。兩種 turn 方法都接受可選的
`clientUserMessageId` 並原樣轉送；它只供追蹤，不保證去重。

## 狀態檢查與失敗行為

- 執行中呼叫 `turn/start`：exit 1，提示 `use turn/steer. No message sent.`
- 未執行時呼叫 `turn/steer`：exit 1，提示 `use turn/start. No message sent.`
- active 狀態與 turn ID 無法一致確認、未知狀態或 system error 時不送訊息。
- 送出後逾時、斷線或回應無法解析：exit 1，明確包含「結果不確定，先查紀錄」。
  不重連、不自動重送。請先查看目標 TUI／紀錄，再決定是否另行發送。
- daemon 明確回報的 RPC 錯誤保留原始 message。

流程是 `initialize` → `initialized` → 狀態預檢 →
`thread/resume`（只帶 threadId 與 `excludeTurns: true`）→ 再查狀態 →
`turn/start` 或 `turn/steer`。active turn ID 由
`thread/turns/list` 最新一筆 `inProgress` turn 取得，不載入整段歷史。

預檢與實際送出之間仍有競態；`turn/steer` 的 expectedTurnId 交由 daemon
再次檢查。`turn/start` 沒有原子化的「仍然閒置」條件，不能保證並行呼叫者
同時送入時仍維持單一 start；轉接器不額外加入排程或重試。

## 協定依據

以本機 `codex-cli 0.163.0` 產生的 schema 為實作依據。
可用下列指令在暫存目錄重新取得 schema：

```bash
codex app-server generate-json-schema --out /tmp/codex-cli-schema
```

OpenAI Docs 的 [app-server 文件](https://learn.chatgpt.com/docs/app-server)
說明初始化、thread/read、turn pagination 與 expectedTurnId；
本機 daemon 的 Unix socket `/rpc` 路徑另依本 repo 實驗與此次實測確認。
這是版本相關介面，Codex 更新後應重新驗證。
`codex exec resume` 不用於轉接，因為 daemon 已持有對話 writer。
