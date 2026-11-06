"""在每台機器上找 OpenRouter key：檔案、執行中的程式、容器。由 keyswitch 經 stdin 送過去跑，
只輸出路徑與尾三碼。只用標準函式庫；只讀不改。"""
import glob
import json
import os
import re
import subprocess

KEY = re.compile(rb"sk-or-v1-[0-9a-f]{64}")
SKIP = {".git", "node_modules", "__pycache__", ".cache", "site-packages", ".venv", "venv", "dist-packages",
        "proc", "sys", "dev", "snap", "overlay2", "containerd", "models", "weights", "checkpoints"}
SKIP_EXT = re.compile(r"\.(mp3|wav|flac|ogg|mp4|mov|mkv|png|jpe?g|webp|gif|zip|gz|zst|tar|7z|safetensors|bin|pt|pth|"
                      r"onnx|gguf|parquet|arrow|exe|dll|so|iso|vhdx|sf2|sf3|mid|pdf)$", re.I)
MAX_SIZE = 12 * 2 ** 20


def main(payload):
    files, count = {}, 0
    for root in payload["roots"]:
        for folder, dirs, names in os.walk(os.path.expanduser(root), followlinks=False):
            dirs[:] = [d for d in dirs if d not in SKIP]
            for name in names:
                path = os.path.join(folder, name)
                if SKIP_EXT.search(name):
                    continue
                try:
                    if os.path.islink(path) or not os.path.isfile(path) or not 0 < os.path.getsize(path) <= MAX_SIZE:
                        continue
                    with open(path, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                count += 1
                tails = sorted({k[-3:].decode() for k in KEY.findall(data)})
                if tails:
                    files[path] = tails
    procs = {}
    for env in glob.glob("/proc/[0-9]*/environ"):
        pid = env.split("/")[2]
        try:
            with open(env, "rb") as fh:
                items = fh.read().split(b"\0")
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")[:80]
        except OSError:
            continue
        for item in items:
            found = KEY.search(item)
            if found:
                procs.setdefault(found.group()[-3:].decode(), []).append(cmd)
    containers = []
    try:
        ids = subprocess.run(["docker", "ps", "-aq"], capture_output=True, text=True, timeout=20).stdout.split()
        for cid in ids:
            info = json.loads(subprocess.run(["docker", "inspect", cid], capture_output=True, text=True, timeout=20).stdout)[0]
            for item in info["Config"].get("Env") or []:
                found = KEY.search(item.encode())
                if found:
                    containers.append({"name": info["Name"].lstrip("/"), "var": item.split("=")[0],
                                       "tail": found.group()[-3:].decode()})
    except Exception:
        pass
    print(json.dumps({"scanned": count, "files": files, "containers": containers,
                      "procs": {t: len(c) for t, c in procs.items()}}, ensure_ascii=False))
