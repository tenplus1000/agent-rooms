"""介面的互動檢查（不在 unittest 裡跑，要有 headless Chrome）：
另開一個用暫存資料的核心（8795），建假房間假成員，用 Chrome 除錯協定點、拖、打字，每步截圖。

  CHROME=/path/to/chrome-headless-shell python3 tests/ui_check.py
截圖在印出來的暫存資料夾裡。
"""
import base64, json, os, socket, subprocess, sys, tempfile, time, urllib.request
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "adapters", "codex_cli"))
from cli_bridge import WS  # noqa: E402

PORT = 8795
data = tempfile.mkdtemp(prefix="rooms-ui-")
OUT = os.path.join(data, "shot")
print("暫存資料與截圖：", data)
env = dict(os.environ, ROOMS_DATA=data, ROOMS_PORT=str(PORT), ROOMS_JUDGE="off", ROOMS_USER_ID="user", ROOMS_USER_NAME="使用者",
           PYTHONPATH=ROOT)
core = subprocess.Popen([sys.executable, "-m", "rooms.server"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.2)
BASE = f"http://127.0.0.1:{PORT}"


def call(method, path, **p):
    p.setdefault("as", "user")
    url = BASE + path
    data_ = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(p)
    else:
        data_ = json.dumps(p).encode()
    req = urllib.request.Request(url, data=data_, method=method, headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())["result"]


import urllib.parse  # noqa: E402
for sid, name in (("a1", "主持甲"), ("a2", "工人A"), ("a3", "工人B"), ("a4", "散客"), ("m", "修房"), ("r", "管家")):
    call("POST", "/api/register", session=sid, kind="claude", name=name)
call("POST", "/api/global", session="m", kind="maintainer", alias="維修")
call("POST", "/api/global", session="r", kind="resident", alias="總管")
r1 = call("POST", "/api/rooms", name="專案甲", owner="a1")["room_id"]
r2 = call("POST", "/api/rooms", name="專案乙")["room_id"]
for sid in ("a1", "a2", "a3"):
    call("POST", "/api/move", session=sid, room_id=r1, **{"as": sid})
call("POST", "/api/touch", session="a4")

import glob  # noqa: E402
H = os.environ.get("CHROME") or next(iter(sorted(glob.glob(os.path.expanduser(
    "~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))), "")
if not H:
    sys.exit("找不到 headless Chrome：用 CHROME=<路徑> 指定")
chrome = subprocess.Popen([H, "--no-sandbox", "--hide-scrollbars", "--remote-debugging-port=9333", "--window-size=1400,900",
                           f"--user-data-dir={data}/chrome", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.5)


class TcpWS(WS):
    def __init__(self, url):
        host, rest = url[len("ws://"):].split("/", 1)
        h, p = host.split(":")
        self.timeout, self.buf, self.serial = 30, b"", 0
        self.sock = socket.create_connection((h, int(p)))
        self.deadline = time.monotonic() + 30
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{rest} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buf:
            self._read()
        self.buf = self.buf.split(b"\r\n\r\n", 1)[1]

    def cdp(self, method, **params):
        self.serial += 1
        rid = self.serial
        self.deadline = time.monotonic() + 30
        self.send({"id": rid, "method": method, "params": params})
        while True:
            msg = self.recv()
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})


page = [t for t in json.loads(urllib.request.urlopen("http://127.0.0.1:9333/json").read()) if t["type"] == "page"][0]
ws = TcpWS(page["webSocketDebuggerUrl"])
ws.cdp("Page.navigate", url=BASE + "/")
time.sleep(2.5)
shot_n = [0]


def shot(name):
    shot_n[0] += 1
    png = base64.b64decode(ws.cdp("Page.captureScreenshot")["data"])
    path = f"{OUT}-{shot_n[0]}-{name}.png"
    open(path, "wb").write(png)
    print("截圖", path)


def js(expr):
    got = ws.cdp("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
    return got.get("result", {}).get("value")


def center(selector):
    return js(f"""(() => {{ const n = document.querySelector({json.dumps(selector)}); if (!n) return null;
        const b = n.getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2]; }})()""")


def mouse(kind, x, y, mods=0):
    ws.cdp("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1, modifiers=mods)


def click(x, y):
    mouse("mousePressed", x, y); mouse("mouseReleased", x, y)


def drag(a, b, mods=0):
    mouse("mouseMoved", *a, mods); mouse("mousePressed", *a, mods)
    for k in range(1, 11):
        mouse("mouseMoved", a[0] + (b[0] - a[0]) * k / 10, a[1] + (b[1] - a[1]) * k / 10, mods)
        time.sleep(0.03)
    mouse("mouseReleased", *b, mods)
    time.sleep(1.2)


def type_text(text):
    ws.cdp("Input.insertText", text=text)


def key(k):
    ws.cdp("Input.dispatchKeyEvent", type="keyDown", key=k, code=k, windowsVirtualKeyCode={"Enter": 13, "Escape": 27}[k])
    ws.cdp("Input.dispatchKeyEvent", type="keyUp", key=k, code=k, windowsVirtualKeyCode={"Enter": 13, "Escape": 27}[k])


results = []


def check(name, ok):
    results.append((name, bool(ok)))
    print("✓" if ok else "✗", name)


try:
    call("POST", f"/api/rooms/{r1}/post", text="@主持甲 我做完第一份了", **{"as": "a2"})   # 打開之後才來的才冒泡泡
    time.sleep(3)
    shot("初始")
    check("看到兩間房", js("document.querySelectorAll('[data-room]').length") == 2)
    check("有泡泡", js("document.querySelectorAll('.bubble').length") >= 1)
    # 點泡泡 → 彈窗
    click(*center(".bubble .txt")); time.sleep(1.2)
    check("彈窗打開", js("document.querySelectorAll('.win').length") == 1)
    check("彈窗有那則訊息", "我做完第一份了" in (js("document.querySelector('.win .log').innerText") or ""))
    shot("彈窗")
    key("Escape"); time.sleep(0.8)
    check("Esc 關彈窗", js("document.querySelectorAll('.win').length") == 0)
    # 選房間 → 輸入框送到那間
    top = js(f"""(() => {{ const g = document.querySelector('[data-room="{r1}"]'); const ps = g.querySelectorAll('polygon'); const p = ps[ps.length - 1];
        const b = p.getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height * 0.3]; }})()""")
    click(*top); time.sleep(0.8)
    check("選了房間，送到顯示專案甲", "專案甲" in js("document.getElementById('targetName').textContent"))
    check("房間操作列出現", js("getComputedStyle(document.getElementById('roombar')).display") == "flex")
    js("document.getElementById('input').focus()")
    type_text("@工人B 你接第二份"); key("Enter"); time.sleep(1.5)
    hist = call("GET", f"/api/rooms/{r1}/history", since=0)
    check("訊息送進專案甲、叫醒工人B", any(e.get("text") == "@工人B 你接第二份" and "a3" in e.get("wake", []) for e in hist))
    shot("選房送出")
    # 拖點換房：工人A 拖到專案乙
    a = center('[data-session="a2"] circle')
    b2 = js(f"""(() => {{ const g = document.querySelector('[data-room="{r2}"]'); const p = g.querySelectorAll('polygon')[0];
        const b = p.getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2]; }})()""")
    drag(a, b2)
    ov = call("GET", "/api/overview")
    check("工人A 拖進專案乙", next(x for x in ov["agents"] if x["session"] == "a2")["room_id"] == r2)
    # 拖到主持位置：工人B 當專案甲的主持
    time.sleep(1.5)
    a = center('[data-session="a3"] circle')
    seat = center(f'[data-seat="{r1}"]')
    drag(a, seat)
    ov = call("GET", "/api/overview")
    check("工人B 當專案甲主持", next(r for r in ov["rooms"] if r["room_id"] == r1)["owner"] == "a3")
    # 散客拖進專案乙
    time.sleep(1.5)
    drag(center('#outside [data-session="a4"]'), b2)
    ov = call("GET", "/api/overview")
    check("外面的散客拖進專案乙", next(x for x in ov["agents"] if x["session"] == "a4")["room_id"] == r2)
    shot("拖拉後")
    # 點常駐 → 模式；送出 → 專屬管道
    click(*center('#outside [data-global="r"]')); time.sleep(0.8)
    check("點總管進入常駐模式", "總管" in js("document.getElementById('targetName').textContent"))
    js("document.getElementById('input').focus()"); type_text("今天先做哪一件？"); key("Enter"); time.sleep(1.8)
    desk = next(r for r in call("GET", "/api/overview")["rooms"] if r.get("desk") == "r")
    check("訊息進總管的專屬管道且叫醒它", any(e.get("text") == "今天先做哪一件？" and e.get("wake") == ["r"]
                                     for e in call("GET", f"/api/rooms/{desk['room_id']}/history", since=0)))
    check("專屬管道不畫成立方體", js("document.querySelectorAll('[data-room]').length") == 2)
    check("送出後打開跟總管的對話視窗", "總管" in (js("document.querySelector('.win header .t') && document.querySelector('.win header .t').textContent") or "")
          or "管家" in (js("document.querySelector('.win header .t') && document.querySelector('.win header .t').textContent") or ""))
    call("POST", f"/api/rooms/{desk['room_id']}/post", text="先做專案甲", **{"as": "r"})
    key("Escape"); time.sleep(3.2)
    check("總管回話時六角形旁冒泡泡", "先做專案甲" in (js("[...document.querySelectorAll('.bubble')].map(b => b.innerText).join('|')") or ""))
    shot("常駐模式")
    key("Escape"); time.sleep(0.5)
    click(*center('#outside [data-global="r"]')); time.sleep(0.8)
    check("再點一次離開模式", "沒選房間" in js("document.getElementById('targetName').textContent"))
    # @ 別房的人：記在對方的房
    js("document.getElementById('input').focus()"); type_text("@工人A 那份怎麼樣"); key("Enter"); time.sleep(1.5)
    check("@ 別房的人記在對方房（專案乙）", any("〔從" in (e.get("text") or "") and "@工人A 那份怎麼樣" in e.get("text", "")
                                          for e in call("GET", f"/api/rooms/{r2}/history", since=0)))
    # 開房間（自製對話框）
    click(*center("#newRoom")); time.sleep(0.6)
    check("開房間是自製對話框", js("document.querySelectorAll('.layer .dialog').length") == 1)
    type_text("專案丙"); key("Enter"); time.sleep(1.8)
    check("開好專案丙", any(r["name"] == "專案丙" for r in call("GET", "/api/overview")["rooms"]))
    shot("開房後")
    # 刪房（空房）：自製確認框
    r3 = next(r["room_id"] for r in call("GET", "/api/overview")["rooms"] if r["name"] == "專案丙")
    js("document.getElementById('roombar').querySelector('.danger').click()"); time.sleep(0.6)
    check("刪房要確認", "刪掉 專案丙" in (js("document.querySelector('.dialog h3') && document.querySelector('.dialog h3').textContent") or ""))
    js("[...document.querySelectorAll('.dialog button')].pop().click()"); time.sleep(1.8)
    check("專案丙刪掉了", not any(r["room_id"] == r3 for r in call("GET", "/api/overview")["rooms"]))
    # 泡泡收起：再來一則訊息 → 泡泡 ✕ → 不見；選房「看對話」叫回來
    call("POST", f"/api/rooms/{r2}/post", text="乙這邊有個問題", **{"as": "a2"}); time.sleep(3)
    n = js("document.querySelectorAll('.bubble').length")
    js("document.querySelector('.bubble .x').click()"); time.sleep(0.8)
    check("泡泡 ✕ 收起", js("document.querySelectorAll('.bubble').length") == n - 1)
    # 慢慢拖房間（中間會碰到每 2.5 秒的重畫）：途中不能彈回去，放下後畫面位置要等於存下的位置
    js("Object.keys(S.wins).forEach(closeWin)"); time.sleep(0.5)

    def room_transform(rid):
        return js("document.querySelector('[data-room=" + json.dumps(rid) + "]').getAttribute('transform')")

    lab = center('[data-room="%s"] .room-label' % r2)
    before = room_transform(r2)
    mouse("mouseMoved", *lab); mouse("mousePressed", *lab)
    jumped = False
    for step in range(1, 31):
        mouse("mouseMoved", lab[0] + step * 4, lab[1] + step * 2); time.sleep(0.11)
        if step > 3 and room_transform(r2) == before:
            jumped = True
    mouse("mouseReleased", lab[0] + 120, lab[1] + 60); time.sleep(3.2)
    check("拖房間途中沒有彈回原位", not jumped)
    pos = next(r for r in call("GET", "/api/overview")["rooms"] if r["room_id"] == r2)["pos"]
    shown = [float(v) for v in room_transform(r2)[len("translate("):-1].split(",")]
    check("放下後畫面位置＝存下的位置", all(abs(a - b) < 0.6 for a, b in zip(shown, pos)))
    # 縮放、平移
    k0 = js("S.view.k")
    ws.cdp("Input.dispatchMouseEvent", type="mouseWheel", x=700, y=450, deltaX=0, deltaY=-300); time.sleep(0.4)
    check("滾輪放大", js("S.view.k") > k0)
    x0 = js("S.view.x"); drag([1300, 800], [1100, 700])
    check("拖空白處平移", abs(js("S.view.x") - x0 - (-200)) < 5)
    # 主持拖離 ★：卸任（先按「全部看得到」，剛才放大平移後房間可能在畫面外）
    js("Object.keys(S.wins).forEach(closeWin); document.getElementById('fit').click()"); time.sleep(1.5)
    seat_dot = center('[data-room="%s"] [data-session="a3"] circle' % r1)
    floor = js(f"""(() => {{ const g = document.querySelector('[data-room="{r1}"]'); const p = g.querySelectorAll('polygon')[0];
        const b = p.getBoundingClientRect(); return [b.left + b.width * 0.3, b.top + b.height * 0.35]; }})()""")
    drag(seat_dot, floor)
    ov = call("GET", "/api/overview")
    check("主持拖離 ★ 就卸任", next(r for r in ov["rooms"] if r["room_id"] == r1)["owner"] == "user")
    # 專屬管道的對話叫得回來：點總管 → 看對話
    click(*center('#outside [data-global="r"]')); time.sleep(0.8)
    js("[...document.querySelectorAll('#roombar button')].find(b => b.textContent === '看對話').click()"); time.sleep(1.5)
    check("常駐模式按「看對話」叫回對話", "今天先做哪一件？" in (js("[...document.querySelectorAll('.win .log')].map(l => l.innerText).join('|')") or ""))
    check("常駐、維修、沒進房的都在狀態列", js("document.querySelectorAll('#outside .gchip').length") == 2)
    # @ 名單：列出全部的人（包含別房的、下線的、沒進房的），可以捲動
    js("document.getElementById('input').value=''; document.getElementById('input').focus()")
    type_text("@"); js("onInput()"); time.sleep(0.4)
    names = js("[...document.querySelectorAll('#suggest div')].map(d => d.firstChild.textContent)") or []
    check("@ 名單有全部的人", all("@" + n in names for n in ("全部", "主持甲", "工人A", "工人B", "散客", "總管", "維修")))
    key("Escape"); js("document.getElementById('input').value=''")
    # Shift＋左鍵選兩間，拖其中一間整組一起搬
    js("Object.keys(S.wins).forEach(closeWin); S.mode = null; document.getElementById('fit').click()"); time.sleep(1.5)
    def top_of(rid):
        return js(f"""(() => {{ const g = document.querySelector('[data-room="{rid}"]'); const ps = g.querySelectorAll('polygon'); const p = ps[ps.length - 1];
            const b = p.getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height * 0.3]; }})()""")
    pos0 = {rid: js(f"roomPos(S.ov.rooms.find(r => r.room_id === '{rid}'), rooms().findIndex(r => r.room_id === '{rid}'))") for rid in (r1, r2)}
    t1 = top_of(r1); mouse("mouseMoved", *t1, 8); mouse("mousePressed", *t1, 8); mouse("mouseReleased", *t1, 8); time.sleep(0.5)
    check("Shift 點一間：加進多選", js("S.multi.size") == 0 and js("S.sel") == r1 or js("S.multi.has('%s')" % r1))
    t2 = top_of(r2); drag(t2, [t2[0] + 80, t2[1] + 40], 8); time.sleep(2.5)
    pos1 = {r["room_id"]: r.get("pos") for r in call("GET", "/api/overview")["rooms"]}
    moved = [round(pos1[rid][0] - pos0[rid][0]) for rid in (r1, r2)] + [round(pos1[rid][1] - pos0[rid][1]) for rid in (r1, r2)]
    check("Shift 選兩間一起拖", moved[0] == moved[1] != 0 and moved[2] == moved[3] != 0)
    click(1300, 720); time.sleep(0.5)
    check("點空白處取消多選", js("S.multi.size") == 0)
    # 拖到別間旁邊會吸上去：兩間接上、共用白板（各自只改自己的）
    call("PUT", f"/api/rooms/{r1}/whiteboard", text="甲的規矩")
    ov = call("GET", "/api/overview"); p1 = next(r for r in ov["rooms"] if r["room_id"] == r1)["pos"]
    p2 = next(r for r in ov["rooms"] if r["room_id"] == r2)["pos"]
    kk = js("S.view.k"); a = center(f'[data-room="{r2}"] polygon[style*="pointer"]')
    want = [p1[0] + 150 * 0.866 + 25, p1[1] + 75 - 20]                        # 差一點點也會吸過去
    drag(a, [a[0] + (want[0] - p2[0]) * kk, a[1] + (want[1] - p2[1]) * kk]); time.sleep(1.5)
    ov = call("GET", "/api/overview")
    check("拖近就吸上去、兩間接上", r2 in next(r for r in ov["rooms"] if r["room_id"] == r1).get("neighbors", {}))
    check("接上的房標出共用白板", js(f"!!document.querySelector('[data-linked=\"{r2}\"]')"))
    shot("吸上去的兩間")
    js("showBoard('%s'), 0" % r2); time.sleep(0.8)
    check("白板框列出隔壁的白板", "甲的規矩" in (js("document.querySelector('.dialog .board-near') && document.querySelector('.dialog .board-near').innerText") or ""))
    shot("接上的房")
    key("Escape"); time.sleep(0.4)
    # 下線的人：狀態列有「下線 N」，打開整理、收起來
    call("POST", "/api/register", session="a5", kind="codex", name="閒人")
    call("POST", "/api/off", session="a5", on=1); time.sleep(3.2)
    check("狀態列出現下線整理", "下線" in (js("document.querySelector('[data-tidy]') && document.querySelector('[data-tidy]').textContent") or ""))
    click(*center('[data-tidy]')); time.sleep(0.6)
    check("整理區列出閒人", js("!!document.querySelector('.tidy-pop [data-session=\"a5\"]')"))
    # 判官建議收起：畫面上排在最上面、寫出依據（判官的結果在測試裡直接塞進總覽）
    js("S.ov.agents.find(a => a.session === 'a5').shelve_hint = {suggest: true, done: 0.9, pending: 0.1, why: '交件了'}; S.tidyFill()")
    check("整理區列出判官建議收起", "交件了" in (js("document.querySelector('.tidy-row.hinted') && document.querySelector('.tidy-row.hinted').innerText") or ""))
    shot("下線整理")
    js("[...document.querySelectorAll('.tidy-pop [data-session=\"a5\"] button')].find(b => b.textContent === '收起來').click()"); time.sleep(1.5)
    check("收起來", next(a for a in call("GET", "/api/overview")["agents"] if a["session"] == "a5")["shelved"])
    key("Escape"); time.sleep(0.4)
    # 房裡的人全部下線：跳確認框，按了就移出並刪房
    for m in call("GET", f"/api/rooms/{r2}")["members"]:
        call("POST", "/api/off", session=m["session"], on=1)
    time.sleep(3.2)
    check("全部下線就跳確認框", "都下線了" in (js("document.querySelector('.dialog h3') && document.querySelector('.dialog h3').textContent") or ""))
    shot("全部下線確認框")
    js("[...document.querySelectorAll('.dialog button')].find(b => b.textContent === '移出並刪房').click()"); time.sleep(1.8)
    check("確認後移出並刪房", all(r["room_id"] != r2 for r in call("GET", "/api/overview")["rooms"]))
    js("showBoard('%s'), 0" % r1); time.sleep(0.8)
    check("白板框可以編輯", js("!!document.querySelector('.dialog textarea.board-edit')"))
    js("document.querySelector('.dialog textarea').value = '規矩：先讀再做'; [...document.querySelectorAll('.dialog button')].pop().click()"); time.sleep(1.2)
    check("白板存檔寫回核心", call("GET", "/api/rooms/%s/whiteboard" % r1) == "規矩：先讀再做")
    shot("結束")
finally:
    chrome.terminate(); core.terminate()
    bad = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(bad)}／{len(results)} 通過", "；沒過：" + "、".join(bad) if bad else "")
