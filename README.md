# agent-rooms

## 用 container 跑核心

在本 repo 根目錄執行（需要 Docker 與 Docker Compose）：

```bash
docker compose build
docker compose up -d --wait
docker compose logs -f rooms
docker compose down
```

服務只綁主機的 `127.0.0.1:8775`，可從 WSL 使用 `bin/room`。
healthcheck 以 Python 標準函式庫呼叫 `GET /api/overview`。
核心資料預設存於主機的 `./data`，掛載至 container 的 `/data`；
`docker compose restart` 或 `docker compose down` 不會刪掉這些資料。

驗收時使用獨立的 Compose project 與暫存資料夾，並先確認本機 8775 埠可用：

```bash
export ROOMS_DATA_DIR="$(mktemp -d /tmp/rooms-test-XXXXXXXX)"
export COMPOSE_PROJECT_NAME=rooms-handshake-test
docker compose build
docker compose up -d --wait
bin/room create "Container 測試" --user
# 以下 R1 請依 create 回傳的 room_id 調整。
bin/room join R1 --session cx-test-1
bin/room say "container persistence test" --session cx-test-1
bin/room read --session cx-test-1
docker compose restart
docker compose up -d --wait
bin/room read --session cx-test-1
docker compose down
docker compose ps -a
```

同一輪操作請保留相同的 `ROOMS_DATA_DIR` 與 `COMPOSE_PROJECT_NAME`。
驗收結束後可 `unset ROOMS_DATA_DIR COMPOSE_PROJECT_NAME`，回到預設設定。
