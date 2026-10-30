# agent-rooms

[English](README.md) | 中文

讓同時開著的多個 coding agent（Claude Code、Codex）在「房間」裡協作。每個 agent 照常在自己的視窗工作；房間負責傳話、叫醒、派任務與驗收。

- **房間**：成員名單、對話、白板、任務。agent 一次只在一間房，只看得到自己所在的那一間。
- **叫醒**：被 @ 的人會被叫醒；判官再看最近的對話，判斷還有誰該知道（派工的人、事情會碰到的人）。只 @ 使用者的訊息也會叫醒負責人，由負責人轉告。閒著的 agent 也叫得醒，正在做事的會在下一個工具呼叫後收到。
- **任務**：負責人發任務，成員領取、做、交件，由檢查者驗收結案（做的人不能自己結案）。
- **使用者**：可以把任何 agent 移進／移出房間、開勿擾、把做完的成員標成下線；有事需要使用者決定時，房間會亮燈。
- **額度**：畫面左邊列出這台機器上每個 Claude／Codex 帳號的 5 小時與 7 天用量、什麼時候重置；開新成員時挑還有額度的帳號。

## 組成

| 部分 | 位置 | 作用 |
|---|---|---|
| 核心 | `rooms/core.py`、`rooms/server.py` | 房間狀態與 HTTP API（預設 `127.0.0.1:8775`），可用 container 跑 |
| 命令列 | `bin/room` | agent 與使用者操作房間；也是 Claude Code 的 hook |
| 轉送程式 | `bin/rooms-dispatch` | 替房裡的 Codex 監聽，該叫醒時送進它的對話 |
| 轉接器 | `adapters/codex_cli`、`adapters/codex_desktop` | 連到 Codex CLI 的背景服務、Windows 桌面版 Codex |
| MCP | `adapters/mcp` | 給 Codex 用的房間工具（Codex 的沙盒連不到本機網路） |
| 判官 | `rooms/judge.py` | 沒 @ 的訊息問決策模型在找誰 |
| 本機工具 | 畫面右側的小球 | 本機在跑的網頁（儀表板、資料庫與檢視介面、助手工具）登記進房間：開窄窗、開成分頁、接到房間、看程式圖（模板見 `contrib/tool-templates.md`） |
| 本機小幫手（選配） | `bin/room local` | 本機有 acc 時，點成員多一個「acc」鈕看它的目標與紀錄；有判斷層時列出等你決定的建議（`rooms/local.py`） |

只用 Python 標準函式庫（桌面版轉接器另用 Windows 端的 Node）。

## 開始

### 1. 跑核心

```bash
docker compose up -d --build
```

資料存在 `./data`（可用 `ROOMS_DATA_DIR` 改）。不用 container 的話：`bin/rooms-serve`。

### 2. 接上 Claude Code 和 Codex（一行）

```bash
python3 contrib/add-room-hooks.py
```

它找出這台機器上每一個 Claude 帳號（`~/.claude`、`~/.claude-*`，WSL 上還有 Windows 的 `%USERPROFILE%\.claude`）和每一個 Codex 帳號（`~/.codex`、`~/.codex-*`），補上缺的 hook 與 MCP 設定；已經接好的不重複加，改之前備份成 `.bak`。多了新帳號就再跑一次。下面是它做的事，想自己設定可以照著做。

#### Claude Code 的 hook

在 `~/.claude/settings.json` 的 `hooks` 加上：

```json
"UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "/path/to/agent-rooms/bin/room hook", "timeout": 5 }] }],
"PostToolUse":      [{ "hooks": [{ "type": "command", "command": "/path/to/agent-rooms/bin/room hook", "timeout": 5 }] }],
"Stop":             [{ "hooks": [{ "type": "command", "command": "/path/to/agent-rooms/bin/room hook", "asyncRewake": true, "timeout": 2000000 }] }],
"SessionStart":     [{ "hooks": [{ "type": "command", "command": "/path/to/agent-rooms/bin/room hook", "asyncRewake": true, "timeout": 2000000 }] }]
```

- `UserPromptSubmit`：在房裡的對話每一輪帶上房間規則與累積的訊息。
- `PostToolUse`：做事做到一半，有人找它才插話。
- `Stop`：每一輪結束後在背景等房間，有人找就叫醒它。Claude Code 會在 `timeout` 到時結束 hook，所以等待時間設成約 23 天（再長會讓 JavaScript 的計時器溢位、馬上結束）。視窗閒著卻沒有 hook 在等時，轉送程式會亮燈說送不到；在那個視窗打一句話就接回來。
- `SessionStart`：剛開或 resume、還沒跑過任何一輪的對話也先在背景等著（沒有它，重開的視窗要等使用者打一句話才收得到房間訊息）。

不在任何房間的對話不受影響。

Windows 上的 Claude（例如桌面版）跑在 Windows，hook 要經 `wsl.exe` 轉進 WSL：同樣三個 hook，`command` 改成
`MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu -- python3 <win_bridge.py> /path/to/agent-rooms/bin/room hook`。
`win_bridge.py` 要把 hook JSON 裡 `C:\...` 的路徑（`cwd`、`transcript_path`）換成 `/mnt/c/...` 再交給 `room hook`；房間看到 `/mnt/` 開頭的紀錄路徑，就會給它 `wsl.exe` 版的房間指令。

#### Codex 的 MCP 與轉送程式

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

要開機就自動跑、掛了自己重開，用 `contrib/rooms-dispatch.service`（systemd 使用者服務，安裝方法寫在檔案開頭）。

本機有裝 acc（或判斷層，設 `ROOMS_JUDGMENT_LAYER`）的話，另外跑 `bin/room local`（或 `contrib/rooms-local.service`）：它綁 `127.0.0.1:8777`，房間畫面直接連，只認 `ROOMS_LOCAL_ORIGINS` 列的頁面來源。沒跑就什麼都不出現，房間照常用。

Codex 的視窗要接在背景服務上，房間才送得進去：用 `codex` 或 `codex resume <id>` 開，不要帶 `-c`／`--enable`／`--disable`／`--search` 這類參數（帶了會改成獨立執行）。

### 3. 開房間

```bash
bin/room create 我的專案 --user
bin/room move <session id> R1 --user        # 把 agent 移進房間
bin/room rename <session id> 工人1 --user
bin/room role 工人1 "負責寫測試" --user --room R1   # 一句在做什麼，判官靠它判斷
bin/room overview
bin/room delete R3 --user                       # 刪房間：房裡要先沒人，紀錄封存在 data/rooms/deleted/
```

負責人（或使用者）還可以：

```bash
bin/room spawn 工人2 "請先讀白板再開始" --role "負責寫文件" --cwd ~/my-project   # 開一個新的 Codex 成員
bin/room on 工人1                     # 把下線的人叫回來
```

- `spawn` 在背景服務開一個新的 Codex 對話（沒有視窗），帳號照 `ROOMS_SPAWN_ACCOUNTS` 的順序挑還有額度的，也可以用 `--account` 指定。它進房後收到第一則訊息就開始做；想看它在做什麼，用輸出的 `codex resume <id>` 接上。
- 下線、還沒收起來的對話，轉送程式會帶著它的目標與對話最後一段請判官判「做完了、不用再叫回來」嗎；畫面的整理區最上面列出建議收起的，附依據，可以一鍵收起或按「留著」。
- 負責人在 `say` 裡 @別房負責人的名字（或房號）會送到對方房、只叫醒那位負責人；`hosts` 列出各房負責人。
- 畫面上把房間拖到另一間的某一面旁邊會吸上去（地板四個方向或上下疊）：接上的兩間 `board` 會一起列出對方的白板，只能看、各自只改自己的；拖開就解除。
- `--model`、`--effort` 只套在這個成員：Codex 成員開對話時帶模型、之後每一輪都帶模型與推理強度（開之前先問那個帳號有沒有這個模型、支不支援這個推理強度），不改帳號的 `config.toml`。
- `--account` 寫的是 `ROOMS_CLAUDE_HOMES` 裡的帳號時開的是 Claude 成員：可加 `--model`、`--effort`（low～max）、`--permission-mode`（預設 `auto`）。它沒有視窗，每一批訊息由轉送程式跑一次 `claude -p` 接著同一個對話，在 `--cwd` 裡讀寫；房間指令一律放行，回話用 `room say`。每一輪的輸出存在 `~/.local/state/agent-rooms/claude-members/logs/`，想接上看就用輸出的 `claude --resume <id>`。
- 下線的人不會被叫醒。由負責人或使用者 `on` 叫回來的，視窗關著也會在背景載入那個對話送進去；本人回到視窗打字只是回到線上。
- 使用者正在跟某個 Codex 講話時，房間等那一輪跑完再送，不插進去；它閒著就馬上送。
- `bin/room quota` 列出每個帳號現在的額度與在跑幾個成員；`spawn` 會一起附上，同一個帳號已經有好幾個在跑時提醒（不擋）。成員的上下文用到七成，轉送程式會提醒負責人換人。
- 背景工作跑完要叫醒自己：在指令結尾 `room say "@自己的名字 …"`。連續被自己叫醒三次、中間沒有別人的訊息就不再叫，避免空轉；沒叫醒的那一則 5 分鐘後重試，連失敗三次才放棄。
- 負責人點名找別房時，判官在背景看其他房相不相關，跟點名的結果記在一起，只記錄、不改送到哪裡。

### 畫面

瀏覽器開 `http://127.0.0.1:8775`：每間房是一個透明立方體，裡面的點是房裡的對話（★ 是負責人的位置）；全域角色和沒進房的對話固定在上面的狀態列。滾輪以游標為中心縮放、拖空白處移動畫面（手機雙指捏合），「全部看得到」把所有房間放進畫面。

- 點房間的頂面選它，底下唯一的輸入框就打進那間；沒選房間就是跟常駐代理講；點常駐或維修進入跟它講話的模式，再點一下離開。`@名字` 不限房：@ 到別房的人，訊息記在對方的房。
- 房裡有新訊息會冒泡泡；點泡泡彈出對話視窗，關掉變回泡泡，泡泡的 ✕ 暫時收起；選了房間（或點常駐、維修）按「看對話」叫回來。
- 拖房名搬房間（位置會記住）；把點拖到別的房間就是換房、拖到 ★ 就是換負責人、負責人拖離 ★ 就卸任、拖到狀態列就是離開房間；點一下點可以改名、寫在做什麼、上下線、勿擾；開房、改房名、刪房（要先清空）都在畫面上。
- 對話視窗打開就停在最新一則，往上捲才載入更早的；「搜尋」打字就標出這間房所有符合的訊息（Enter 往前、Shift+Enter 往後）。
- 附件：在畫面任何地方直接貼上截圖，或把檔案拖到底下的輸入列，就附在下一則訊息；agent 用 `room say "…" --file <路徑>` 附檔，收到的人看到的是檔案在這台機器上的路徑。
- 多選：按住 Ctrl 點房間加選，Ctrl＋拖曳框選；選到的房間可以一起搬、一起刪（要先清空）、一起接上。
- 左邊的額度欄可以收起來；帳號照自動找到的列出，也可以在上面新增、修改（資料夾、提醒線）、刪除（存在 `~/.config/agent-rooms/accounts.json`，每台機器自己的）。
- 右邊的小球是登記的本機工具：點開成窄窗、展開成分頁、拖到房間上就接到那間房（房裡的人用 `room tools` 看得到、`room tools --map <代號>` 看程式圖）；有啟動指令的可以在畫面上啟動、停止、重開（要跑 `bin/room local`）。
- 從 8776 打開的話會先要鑰匙。互動檢查：`python3 tests/ui_check.py`（要有 headless Chrome）。

### 全域角色

全域角色不用待在任何房間，每間房都看得到，每一輪開頭會收到各房間的現況（負責人、成員狀態、暫停、亮燈、最後幾則）。它們可以移人、改名、上下線、改房名、解除暫停（`resume`）、調整互相 @ 的上限（`hops <房號> <次數|off>`）、開房並指定負責人（`create <房名> --owner <名字>`）、`spawn`；發言一律是自己，不能用使用者的身分。由使用者指定：

```bash
bin/room global <名字> maintainer --alias 維修 --user    # 維修：專門改房間工具本身
bin/room global <名字> resident --alias 管家 --user      # 常駐型代理：掌握全局、派房，不改程式
bin/room global <名字> none --user                       # 取消
```

| 種類 | 什麼時候叫醒它 |
|---|---|
| `maintainer` | 被 @（名字或叫法）、房間暫停、訊息送不到 |
| `resident` | 被 @、有人 @ 使用者（在等使用者的事）、房間暫停 |

有維修時，給所有成員的規則會寫明「房間工具壞了找 @<維修的叫法>」。`resident` 給常駐型代理用（任何接得上 hook 或 MCP 的代理都行），房間只提供位置與權限；「不改程式」寫在它的規則裡，真的要擋得靠代理自己的權限設定。

### 從網路連進來

核心有兩個入口：本機的 `127.0.0.1:8775` 不驗證，只給同一台電腦上的程式用；`127.0.0.1:8776` 一律要鑰匙，給從網路轉進來的請求（例如 `tailscale serve` 把 tailnet 上的埠轉到這裡）。轉進來的請求看起來也是本機，所以兩個入口用不同的埠分開，不靠來源位址判斷。

```bash
bin/room key <名字> --out ~/.config/agent-rooms/keys/<名字>.key --user   # 發鑰匙（只在本機），原文只寫進權限 600 的檔案
bin/room key <名字> --revoke --user                                      # 收回
curl -H "Authorization: Bearer $(cat <鑰匙檔>)" http://127.0.0.1:8776/api/overview
```

遠端的身分由鑰匙決定，請求裡自稱的 `as` 不算數；只能用自己的身分監聽、確認收到；發鑰匙、指定全域角色只能在本機做。核心只存鑰匙的雜湊。常駐型代理不能刪房間（刪房只限使用者和維修）。

## 環境變數

| 變數 | 用途 |
|---|---|
| `ROOMS_USER_ID` | 使用者在房裡的 id：房裡寫 `@<id>` 找使用者，命令列可用 `--<id>` 代替 `--user`；預設 `user`。設定後就不要再改，舊資料用的是當時的 id |
| `ROOMS_USER_NAME` | 使用者顯示的名字，寫進給 agent 的規則和通知；預設跟 `ROOMS_USER_ID` 一樣 |
| `ROOMS_OWNER_LABEL` | 房間負責人的叫法，用在給 agent 的規則和畫面上的 ★（例如「主持」「lead」）；預設「負責人」 |
| `ROOMS_MAX_HOPS` | agent 之間連續互相 @ 幾次就暫停房間、等使用者出現；預設 60，0＝不限。每間房可用 `room hops` 另設 |
| `ROOMS_CLAUDE_OPEN` | Claude 成員的視窗關了又有人找它時，轉送程式用這個指令開新視窗接回對話；`{script}` 換成要跑的腳本、`{title}` 換成名字。沒設時在 WSL 用 Windows Terminal 開 WSL 分頁，其他系統不開只記錄 |
| `ROOMS_WSL_DISTRO` | 上面那個 WSL 分頁用哪個發行版；預設目前的發行版，再不然 Ubuntu |
| `ROOMS_TZ` | 命令列和給 agent 的時間用哪個時區（IANA 名稱，例如 `Europe/Berlin`）；預設跟系統一樣。資料一律存 UTC，網頁照瀏覽器所在的時區顯示 |
| `ROOMS_URL` | 核心位址，預設 `http://127.0.0.1:8775` |
| `ROOMS_DATA_DIR` | container 的資料資料夾，預設 `./data` |
| `ROOMS_JUDGE` | `off` 關掉判官（沒 @ 的訊息改成叫醒負責人） |
| `ROOMS_JUDGE_URL`／`ROOMS_JUDGE_MODEL` | 判官端點與模型，預設 OpenRouter 的 `typesafe/jev-1.13` |
| `ROOMS_JUDGE_KEY` | 判官金鑰；沒設就用 `OPENROUTER_API_KEY` |
| `ACC_SESSIONS` | [acc](https://github.com/tenplus1000/acc) 的 session 資料夾：用來判斷使用者剛在哪個對話講過話 |
| `ROOMS_WINDOWS_HOME` | WSL 上的 Windows 使用者資料夾（預設自動找） |
| `ROOMS_CODEX_HOMES` | 用冒號分隔的 Codex 帳號（`CODEX_HOME`）清單，預設 `~/.codex`；每項可寫成 `名字=路徑`，名字給 `spawn --account` 用 |
| `ROOMS_SPAWN_ACCOUNTS` | `spawn` 挑帳號的順序（名字，冒號分隔），沒額度就換下一個；預設照 `ROOMS_CODEX_HOMES` |
| `ROOMS_CLAUDE_HOMES` | 用冒號分隔的 Claude 帳號（`CLAUDE_CONFIG_DIR`），格式同 `ROOMS_CODEX_HOMES`，預設 `claude=~/.claude`；`spawn --account` 寫這裡的名字就開 Claude 成員 |
| `ROOMS_QUOTA_HELPER` | 選用：放 `codexquota.py` 的資料夾，改用它判斷 Codex 帳號還有沒有額度；沒設就用房間自己的（`rooms/quota.py`） |
| `ROOMS_ACCOUNTS_FILE` | 額度欄的帳號清單，預設 `~/.config/agent-rooms/accounts.json`；沒有這個檔就自動找 |
| `ROOMS_CODEX_BIN`／`ROOMS_CLAUDE_BIN` | 指定 `codex`／`claude` 執行檔；預設照帳號裡的或 PATH 上的 |
| `ROOMS_SPAWN_WARN` | 同一個帳號已經有幾個成員在跑時，`spawn` 提醒；預設 2 |
| `ROOMS_CONTEXT_WARN`／`ROOMS_CLAUDE_CONTEXT` | 成員上下文用到幾 % 提醒負責人（預設 70）；Claude 成員的上下文大小（預設 200000） |
| `ROOMS_JUDGE_TIMEOUT` | 判官逾時秒數，預設 8 |
| `ROOMS_HOST`／`ROOMS_PORT`／`ROOMS_REMOTE_PORT` | 核心綁的位址、本機埠（8775）、要鑰匙的埠（compose 開 8776，0＝不開） |
| `ROOMS_LOCAL_PORT`／`ROOMS_LOCAL_ORIGINS` | 本機小幫手的埠（8777）、它接受的頁面來源（預設 8775 的兩種寫法） |
| `ROOMS_ACC_REPO`／`ROOMS_JUDGMENT_LAYER` | 本機小幫手找 acc 的 repo、判斷層的資料夾 |
| `ROOMS_DESKTOP_SESSIONS` | Windows 桌面版 Codex 的對話紀錄資料夾（預設自動找） |
| `ROOMS_RECORD_TURNS` | 設成 `1` 才記：每個本機 Claude 對話每一輪最後說的話與使用者下一句，存在 `<data>/turns/`（權限 600，只在這台機器）；預設不記 |

命令列、轉送程式、MCP 和核心的 container 都會讀 repo 根目錄的 `local.env`（不進 git），本機設定寫在那裡。

## 測試

```bash
python3 -m unittest discover -s tests
python3 adapters/codex_cli/test_cli.py
python3 adapters/mcp/test_mcp.py
python3 tests/ui_check.py        # 畫面互動檢查，要有 headless Chrome
```

## 授權

MIT，見 [LICENSE](LICENSE)。如果這個工具對你有幫助，歡迎在你的作品或文章裡提一下、附上連結。
