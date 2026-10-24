# agent-rooms

讓同時開著的多個 coding agent（Claude Code、Codex）在「房間」裡協作。每個 agent 照常在自己的視窗工作；房間負責傳話、叫醒、派任務與驗收。

- **房間**：成員名單、對話、白板、任務。agent 一次只在一間房，只看得到自己所在的那一間。
- **叫醒**：被 @ 的人會被叫醒；沒 @ 任何人的訊息由判官判斷是在找誰。閒著的 agent 也叫得醒，正在做事的會在下一個工具呼叫後收到。
- **任務**：負責人發任務，成員領取、做、交件，由檢查者驗收結案（做的人不能自己結案）。
- **使用者**：可以把任何 agent 移進／移出房間、開勿擾、把做完的成員標成下線；有事需要使用者決定時，房間會亮燈。

## 組成

| 部分 | 位置 | 作用 |
|---|---|---|
| 核心 | `rooms/core.py`、`rooms/server.py` | 房間狀態與 HTTP API（預設 `127.0.0.1:8775`），可用 container 跑 |
| 命令列 | `bin/room` | agent 與使用者操作房間；也是 Claude Code 的 hook |
| 轉送程式 | `bin/rooms-dispatch` | 替房裡的 Codex 監聽，該叫醒時送進它的對話 |
| 轉接器 | `adapters/codex_cli`、`adapters/codex_desktop` | 連到 Codex CLI 的背景服務、Windows 桌面版 Codex |
| MCP | `adapters/mcp` | 給 Codex 用的房間工具（Codex 的沙盒連不到本機網路） |
| 判官 | `rooms/judge.py` | 沒 @ 的訊息問決策模型在找誰 |

只用 Python 標準函式庫（桌面版轉接器另用 Windows 端的 Node）。

## 開始

### 1. 跑核心

```bash
docker compose up -d --build
```

資料存在 `./data`（可用 `ROOMS_DATA_DIR` 改）。不用 container 的話：`bin/rooms-serve`。

### 2. 接上 Claude Code

在 `~/.claude/settings.json` 的 `hooks` 加上：

```json
"UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "/path/to/agent-rooms/bin/room hook", "timeout": 5 }] }],
"PostToolUse":      [{ "hooks": [{ "type": "command", "command": "/path/to/agent-rooms/bin/room hook", "timeout": 5 }] }],
"Stop":             [{ "hooks": [{ "type": "command", "command": "/path/to/agent-rooms/bin/room hook", "asyncRewake": true, "timeout": 86400 }] }]
```

- `UserPromptSubmit`：在房裡的對話每一輪帶上房間規則與累積的訊息。
- `PostToolUse`：做事做到一半，有人找它才插話。
- `Stop`：每一輪結束後在背景等房間，有人找就叫醒它。

不在任何房間的對話不受影響。

### 3. 接上 Codex

註冊 MCP（`~/.codex/config.toml`）：

```toml
[mcp_servers.agent-rooms]
command = "python3"
args = ["/path/to/agent-rooms/adapters/mcp/server.py"]
default_tools_approval_mode = "approve"

[mcp_servers.agent-rooms.env]
ROOMS_URL = "http://127.0.0.1:8775"
```

再在本機跑轉送程式：

```bash
bin/rooms-dispatch
```

Codex 的視窗要接在背景服務上，房間才送得進去：用 `codex` 或 `codex resume <id>` 開，不要帶 `-c`／`--enable`／`--disable`／`--search` 這類參數（帶了會改成獨立執行）。

### 4. 開房間

```bash
bin/room create 我的專案 --user
bin/room move <session id> R1 --user        # 把 agent 移進房間
bin/room rename <session id> 工人1 --user
bin/room role 工人1 "負責寫測試" --user --room R1   # 一句在做什麼，判官靠它判斷
bin/room overview
```

## 環境變數

| 變數 | 用途 |
|---|---|
| `ROOMS_URL` | 核心位址，預設 `http://127.0.0.1:8775` |
| `ROOMS_DATA_DIR` | container 的資料資料夾，預設 `./data` |
| `ROOMS_JUDGE` | `off` 關掉判官（沒 @ 的訊息改成叫醒負責人） |
| `ROOMS_JUDGE_URL`／`ROOMS_JUDGE_MODEL` | 判官端點與模型，預設 OpenRouter 的 `typesafe/jev-1.13` |
| `ROOMS_JUDGE_KEY` | 判官金鑰；沒設就用 `OPENROUTER_API_KEY` |
| `ACC_SESSIONS` | [acc](https://github.com/tenplus1000/acc) 的 session 資料夾：用來判斷使用者剛在哪個對話講過話 |
| `ROOMS_WINDOWS_HOME` | WSL 上的 Windows 使用者資料夾（預設自動找） |
| `ROOMS_CODEX_HOMES` | 用冒號分隔的 Codex 帳號（`CODEX_HOME`）清單，預設 `~/.codex` |
| `ROOMS_QUOTA_HELPER` | 選用：放 `codexquota.py` 的資料夾，用來在多個 Codex 帳號間挑還有額度的 |

轉送程式會讀 repo 根目錄的 `local.env`（不進 git），本機設定可以寫在那裡。

## 測試

```bash
python3 -m unittest discover -s tests
python3 adapters/codex_cli/test_cli.py
python3 adapters/mcp/test_mcp.py
```
