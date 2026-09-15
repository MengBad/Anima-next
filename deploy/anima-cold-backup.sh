#!/bin/bash
# anima 每日全量冷备：data 目录 tar + 保留 7 份（收敛计划 O2）
set -e
DEST=/opt/anima/backups
mkdir -p "$DEST"
STAMP=$(date +%Y%m%d)
tar -czf "$DEST/anima-data-$STAMP.tar.gz" -C /opt/anima data
ls -1t "$DEST"/anima-data-*.tar.gz | tail -n +8 | xargs -r rm -f
echo "$(date '+%F %T') backup ok: anima-data-$STAMP.tar.gz"
