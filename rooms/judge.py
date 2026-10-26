"""判官：成員在房裡講話時，問決策模型（預設 Jev）除了被 @ 的人，還有誰該被叫醒。

每個沒被 @ 的成員問兩題、取高的：在對他說話／要他讀到或去做什麼嗎（QUESTION），
會不會改變他正在做的工作（IMPACT）。state 帶最近幾則對話，看得出是誰派的工作。
題目用真實的房間訊息加只改一處的配對句驗過（中文、全部成員一發問完、門檻 0.5）。
每一發（含失敗）都寫進 <data>/judge.jsonl：題目與答案是之後換模型（例如自架的服務）時拿來比的資料。

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

from rooms.identity import USER_NAME

URL = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"
THRESHOLD = 0.5
QUESTION = "這則訊息是在對「{x}」說話，或需要「{x}」讀到、回應或去做什麼嗎？只問{x}本人，房裡其他成員不算。"
IMPACT = "這則訊息說的事，會不會改變「{x}」正在做的工作（例如要停下、要接手、要配合，或會碰到他的檔案、程序）？只問{x}本人。"


def enabled():
    if os.environ.get("ROOMS_JUDGE", "").strip().lower() == "off":
        return False
    return bool(_key()) or "openrouter.ai" not in _url()


def _url():
    return os.environ.get("ROOMS_JUDGE_URL", "").strip() or URL


def _key():
    return os.environ.get("ROOMS_JUDGE_KEY", "").strip() or os.environ.get("OPENROUTER_API_KEY", "").strip()


def state_of(room_name, roles, speaker, text, mentioned=(), context=()):
    """roles：[(名字, 在做什麼)]，說話的人也列在裡面（模型要知道誰是誰）。
    context：[(誰, 說了什麼)]，這則之前最近的幾則；mentioned：這則 @ 了誰。"""
    lines = [f"房間「{room_name}」的成員（{USER_NAME} 不在房裡，透過負責人看房間）："]
    lines += [f"- {name}：{role}" for name, role in roles]
    if context:
        lines += ["", "最近的對話："] + [f"- {who}：「{said}」" for who, said in context]
    tag = f"（訊息裡 @ 了：{'、'.join(mentioned)}）" if mentioned else "（沒有用 @ 點名任何房裡的成員）"
    lines += ["", f"剛剛「{speaker}」在房裡說了這則訊息{tag}：", f"「{text}」"]
    return "\n".join(lines)


def addressees(log_path, room_name, roles, speaker, text, targets, mentioned=(), context=(), room_id=None, call=None):
    """回 ({名字: 機率}, None)；判官叫不到回 (None, 錯誤)。targets 是要問的成員名字（說話者、被 @ 的除外）。

    call 是這一發的編號：同一個編號也寫進那則發言的 judged，紀錄和房間事件靠它對上（房名會改、同一人會連發）。"""
    state = state_of(room_name, roles, speaker, text, mentioned, context)
    questions = {}
    for i, x in enumerate(targets):
        questions[f"q{i}"] = {"type": "noul", "instructions": QUESTION.format(x=x)}
        questions[f"i{i}"] = {"type": "noul", "instructions": IMPACT.format(x=x)}
    model = os.environ.get("ROOMS_JUDGE_MODEL", "").strip() or MODEL
    row = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "call": call, "room_id": room_id, "room": room_name, "speaker": speaker, "requested_model": model, "state": state, "questions": questions}
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
        probs = {x: max(float(answers[f"q{i}"]["noul"]), float(answers[f"i{i}"]["noul"]))
                 for i, x in enumerate(targets)}
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


# 下線的對話該不該收起來：兩題配對驗過（交件／額度用完、報告寫好／做到一半、目標達成／進行中、
# 測試完／常駐、做完沒後續／問要不要接著做），都在那一處翻面。做完 ≥ 門檻且還有沒做完的 < 門檻才建議收。
DONE = "這個對話手上的工作已經做完、交出去，或明確不用再做了嗎？"
PENDING = "這個對話還有做到一半、或答應了還沒做的事，之後要叫它回來接著做嗎？"


def shelve_hint(log_path, state):
    """回 ({"done": 機率, "pending": 機率}, None)；叫不到回 (None, 錯誤)。每一發都記進 judge.jsonl。"""
    questions = {"done": {"type": "noul", "instructions": DONE}, "pending": {"type": "noul", "instructions": PENDING}}
    model = os.environ.get("ROOMS_JUDGE_MODEL", "").strip() or MODEL
    row = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "kind": "shelve",
           "requested_model": model, "state": state, "questions": questions}
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
        probs = {k: float(got["answers"][k]["noul"]) for k in questions}
        row.update(model=got.get("model"), answers=got["answers"], cost=(got.get("usage") or {}).get("cost"))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        row["error"] = error
    row["elapsed"] = round(time.time() - started, 2)
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return probs, error
