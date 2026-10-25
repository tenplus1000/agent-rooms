"""使用者（房間工具的主人）是誰：不寫死，由設定決定。

  ROOMS_USER_ID     房裡 @ 使用者的寫法、資料裡的身分，例如 alex；預設 user
  ROOMS_USER_NAME   顯示的名字，例如 Alex；預設跟 ROOMS_USER_ID 一樣
  ROOMS_OWNER_LABEL 房間負責人的叫法（給 agent 的規則、畫面上的 ★），例如「主持」「lead」；預設「負責人」
  ROOMS_TZ          命令列與給 agent 的時間用哪個時區（IANA 名稱，例如 Europe/Berlin）；預設跟系統一樣。
                    資料裡一律存 UTC；網頁照瀏覽器所在的時區顯示。

改了 ROOMS_USER_ID 之後，舊資料裡用舊 id 記下的發言和負責人就對不上了，所以第一次設定好就不要再改。
命令列和轉送程式從 repo 根目錄的 local.env 讀（bin/ 的包裝，或 paths.load_local_env），核心的 container 由 compose 帶進來。
"""

import datetime
import os


USER = (os.environ.get("ROOMS_USER_ID", "").strip() or "user").lower()
USER_NAME = os.environ.get("ROOMS_USER_NAME", "").strip() or USER
OWNER_LABEL = os.environ.get("ROOMS_OWNER_LABEL", "").strip() or "負責人"


def local_time(iso, fmt="%m-%d %H:%M"):
    """資料裡的 UTC 時間 → 使用者所在時區（ROOMS_TZ，沒設就用系統時區）。冬令、夏令照時區規則自己換。"""
    try:
        moment = datetime.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    zone = os.environ.get("ROOMS_TZ", "").strip()
    if zone:
        try:
            from zoneinfo import ZoneInfo
            return moment.astimezone(ZoneInfo(zone)).strftime(fmt)
        except Exception:
            pass
    return moment.astimezone().strftime(fmt)
