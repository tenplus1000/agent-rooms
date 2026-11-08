#!/bin/sh
# WSL 裡用 root 跑：wsl.exe -u root sh <這個檔的路徑>
set -e
here=$(dirname "$0")
install -m 755 "$here/drop-cache-if-big" /usr/local/sbin/drop-cache-if-big
install -m 644 "$here/drop-cache.service" "$here/drop-cache.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now drop-cache.timer
systemctl list-timers drop-cache.timer --no-pager | head -2
