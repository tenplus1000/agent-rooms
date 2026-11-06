"""在每台機器上跑的小程式：看或改那台機器上的 key。由 keyswitch 經 stdin 送過去跑（python3 -），
key 只走 stdin、不上命令列；輸出只有尾三碼。只用標準函式庫。"""
import glob
import json
import os
import re
import subprocess
import tempfile


def tail(text):
    return text[-3:] if text else ""


def scrub(text, keys):
    for k in keys:
        text = text.replace(k, "<key>")
    return re.sub(r"sk-or-v1-[0-9a-f]{64}", "<key>", text)[-300:]


def read_tails(path, keys):
    """檔案裡認得的 key 的尾碼，照出現順序、去重。"""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if not any(k in text for k in keys):
        return []
    found = sorted((text.find(k), tail(k)) for k in keys if k in text)
    return [t for _, t in found]


def write_atomic(path, text):
    mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o600
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".key-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def set_vars(text, wanted):
    """把 KEY=值 這幾行換掉，其他行不動；沒有這一行就加在最後。"""
    lines = text.splitlines()
    for name, value in wanted.items():
        hit = [i for i, line in enumerate(lines) if line.startswith(name + "=")]
        for i in hit:
            lines[i] = f"{name}={value}"
        if not hit:
            lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def var_value(text, name):
    for line in text.splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def apply_file(target, active, keys):
    path = os.path.expanduser(target["path"])
    if target.get("mode") == "raw":              # 整個檔就是一把 key
        write_atomic(path, active + "\n")
        return
    if target.get("var"):                        # 指定的那一行用選的這把；有備用行就放另一把
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        wanted = {target["var"]: active}
        if target.get("backup_var"):
            others = [k for k in keys if k != active]
            wanted[target["backup_var"]] = others[0] if others else active
        write_atomic(path, set_vars(text, wanted))
        return
    with open(path, encoding="utf-8") as fh:     # 換掉檔案裡出現的 key1／key2，其他內容不動
        text = fh.read()
    if not any(k in text for k in keys):
        raise ValueError("檔案裡沒有認得的 key")
    for k in keys:
        if k != active:
            text = text.replace(k, active)
    write_atomic(path, text)


def proc_tails(needle, var):
    out = set()
    for env in glob.glob("/proc/[0-9]*/environ"):
        pid = env.split("/")[2]
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
            if needle not in cmd or "python3 -" == cmd.strip():
                continue
            with open(env, "rb") as fh:
                for item in fh.read().split(b"\0"):
                    if item.startswith(var.encode() + b"="):
                        out.add(tail(item.split(b"=", 1)[1].decode("utf-8", "replace")))
        except OSError:
            continue
    return sorted(out)


def docker_tail(name, var):
    got = subprocess.run(["docker", "inspect", name, "-f", "{{json .Config.Env}}"],
                         capture_output=True, text=True, timeout=20)
    for item in json.loads(got.stdout or "[]"):
        if item.startswith(var + "="):
            return [tail(item.split("=", 1)[1])]
    return []


def look(target, keys):
    """這個目標現在實際用哪把：回尾碼清單；每個來源都查。"""
    seen, problems = [], []
    if target.get("path"):
        try:
            if target.get("var"):
                with open(os.path.expanduser(target["path"]), encoding="utf-8") as fh:
                    seen.append(tail(var_value(fh.read(), target["var"])) or "?")
            else:
                seen += read_tails(os.path.expanduser(target["path"]), keys) or ["?"]
        except OSError:
            problems.append("檔案讀不到")
    check = target.get("verify") or {}
    try:
        if check.get("proc"):
            found = proc_tails(check["proc"], check["var"])
            if found:
                seen += found
            elif not check.get("optional"):
                problems.append("程式沒在跑")
        if check.get("docker"):
            found = docker_tail(check["docker"], check["var"])
            if found:
                seen += found
            else:
                problems.append("容器沒有這個變數")
    except Exception as exc:
        problems.append(f"查不到：{type(exc).__name__}")
    return sorted(set(seen)), problems


def main(payload):
    keys, active = payload["keys"], payload.get("active")
    out = []
    for target in payload["targets"]:
        row = {"id": target["id"]}
        if payload["op"] == "apply":
            try:
                if target.get("path"):
                    apply_file(target, active, keys)
            except Exception as exc:
                row["error"] = scrub(f"{type(exc).__name__}: {exc}", keys)
        out.append(row)
    if payload["op"] == "apply":                 # 檔案都寫完才重啟：同一個服務可能讀好幾個檔
        ran = {}
        for target, row in zip(payload["targets"], out):
            cmd = target.get("restart")
            if not cmd or row.get("error"):
                continue
            if target.get("verify") and look(target, keys)[0] == [tail(active)]:
                continue                                 # 已經是這把了：不用打斷它
            if cmd not in ran:
                got = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=180)
                ran[cmd] = "" if got.returncode == 0 else scrub(f"重啟沒做或失敗：{got.stdout}{got.stderr}", keys)
            if ran[cmd]:
                row["error"] = ran[cmd]
    for target, row in zip(payload["targets"], out):
        row["tails"], row["problems"] = look(target, keys)
    print(json.dumps(out, ensure_ascii=False))
