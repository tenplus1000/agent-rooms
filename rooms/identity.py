"""使用者（房間工具的主人）是誰：不寫死，由設定決定。

  ROOMS_USER_ID     房裡 @ 使用者的寫法、資料裡的身分，例如 alex；預設 user
  ROOMS_USER_NAME   顯示的名字，例如 Alex；預設跟 ROOMS_USER_ID 一樣

改了 ROOMS_USER_ID 之後，舊資料裡用舊 id 記下的發言和負責人就對不上了，所以第一次設定好就不要再改。
命令列和轉送程式從 repo 根目錄的 local.env 讀（bin/ 的包裝，或 paths.load_local_env），核心的 container 由 compose 帶進來。
"""

import os


USER = (os.environ.get("ROOMS_USER_ID", "").strip() or "user").lower()
USER_NAME = os.environ.get("ROOMS_USER_NAME", "").strip() or USER
