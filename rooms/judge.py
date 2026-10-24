"""判官：房間裡沒 @ 任何人的訊息，問決策模型（預設 Jev）是在找誰。

題目照 ~/judge-experiments/room-addressee 第一輪驗過的版本（中文、全部成員一發問完、門檻 0.5）。
每一發（含失敗）都寫進 <data>/judge.jsonl：題目與答案是之後換模型（例如另一台機器的服務）時拿來比的資料。

環境變數：
  ROOMS_JUDGE=off        關掉判官（沒 @ 的訊息退回叫負責人）
  ROOMS_JUDGE_URL        端點，預設 OpenRouter 的 decisions
  ROOMS_JUDGE_MODEL      模型，預設 typesafe/jev-1.13
  ROOMS_JUDGE_KEY        金鑰；沒設就用 OPENROUTER_API_KEY
  ROOMS_JUDGE_TIMEOUT    秒數，預設 8
"""

import datetime
import json
import os
import time
import urllib.request

URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"
THRESHOLD = 0.5
QUESTION = "這則訊息是在對「{x}」說話，或需要「{x}」讀到、回應或去做什麼嗎？只問{x}本人，房裡其他成員不算。"


def enabled():
    if os.environ.get("ROOMS_JUDGE", "").strip().lower() == "off":
        return False
    return bool(_key()) or "openrouter.ai" not in _url()


def _url():
    return os.environ.get("ROOMS_JUDGE_URL", "").strip() or URL


def _key():
    return os.environ.get("ROOMS_JUDGE_KEY", "").strip() or os.environ.get("OPENROUTER_API_KEY", "").strip()


def state_of(room_name, roles, speaker, text):
    """roles：[(名字, 在做什麼)]，說話的人也列在裡面（模型要知道誰是誰）。"""
    lines = [f"房間 {room_name} 的成員："]
    lines += [f"- {name}：{role}" for name, role in roles]
    lines += ["", f"剛剛「{speaker}」在房裡說了這則訊息（沒有用 @ 點名任何人）：", f"「{text}」"]
    return "\n".join(lines)


def addressees(log_path, room_name, roles, speaker, text, targets):
    """回 ({名字: 機率}, None)；判官叫不到回 (None, 錯誤)。targets 是要問的成員名字（說話者除外）。"""
    state = state_of(room_name, roles, speaker, text)
    questions = {f"q{i}": {"type": "noul", "instructions": QUESTION.format(x=x)} for i, x in enumerate(targets)}
    model = os.environ.get("ROOMS_JUDGE_MODEL", "").strip() or MODEL
    row = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "room": room_name, "speaker": speaker, "requested_model": model, "state": state, "questions": questions}
    started = time.time()
    probs, error = None, None
    try:
        headers = {"Content-Type": "application/json"}
        if _key() and "openrouter.ai" in _url():
            headers["Authorization"] = f"Bearer {_key()}"
        body = json.dumps({"model": model, "state": state, "questions": questions}).encode("utf-8")
        req = urllib.request.Request(_url(), data=body, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=float(os.environ.get("ROOMS_JUDGE_TIMEOUT", "8"))) as resp:
            got = json.loads(resp.read().decode("utf-8"))
        answers = got["answers"]
        probs = {x: float(answers[f"q{i}"]["noul"]) for i, x in enumerate(targets)}
        row.update(model=got.get("model"), answers=answers, cost=(got.get("usage") or {}).get("cost"))
    except Exception as exc:          # 不重試；退回由呼叫的一方處理
        error = f"{type(exc).__name__}: {exc}"
        row["error"] = error
    row["elapsed"] = round(time.time() - started, 2)
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return probs, error
