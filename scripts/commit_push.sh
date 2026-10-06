#!/usr/bin/env bash
# 提交並推送「產出檔」（index.html / dashboard_state.json / history_records）。
#
# 為什麼不用 rebase：workflow 改用 fetch-depth: 1（淺層 checkout）後，rebase 找不到共同祖先會出錯。
# 這些檔案是「由本次執行完整產生」的，衝突時一律以本次產出為準，所以改成：
#   抓遠端最新 → 暫存產出檔 → reset 到遠端最新 → 還原產出檔 → commit → push（被拒絕就重試）。
# 這樣不會留下半殘的 rebase / 合併標記，也不會吞掉錯誤。
set -uo pipefail
export TZ=Asia/Taipei

BRANCH="${GITHUB_REF_NAME:-main}"
MAX_RETRY=5
OUTPUTS=(index.html dashboard_state.json history_records)

git config user.name  'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'

for i in $(seq 1 "$MAX_RETRY"); do
  echo "── 第 $i/$MAX_RETRY 次嘗試推送（分支 $BRANCH）──"

  if ! git fetch --quiet --depth=1 origin "$BRANCH"; then
    echo "⚠️ fetch 失敗，稍後重試"; sleep $((RANDOM % 8 + 3)); continue
  fi

  TMP="$(mktemp -d)"
  for f in "${OUTPUTS[@]}"; do [ -e "$f" ] && cp -r "$f" "$TMP/"; done
  git reset --hard --quiet "origin/$BRANCH"
  for f in "${OUTPUTS[@]}"; do [ -e "$TMP/$f" ] && cp -r "$TMP/$f" ./; done
  rm -rf "$TMP"

  for f in "${OUTPUTS[@]}"; do [ -e "$f" ] && git add -- "$f"; done

  if git diff --staged --quiet; then
    echo "ℹ️ 沒有新的產出需要推送。"
    exit 0
  fi

  git commit --quiet -m "自動更新當沖紀錄 [$(date +'%Y-%m-%d %H:%M')]"

  if git push --quiet origin "HEAD:$BRANCH"; then
    echo "✅ 已推送（第 $i 次嘗試）"
    exit 0
  fi
  echo "⚠️ push 被拒絕（遠端有新變更），重新同步後再試..."
  sleep $((RANDOM % 8 + 3))
done

echo "❌ 重試 $MAX_RETRY 次仍無法推送，請檢查 Actions 紀錄！"
exit 1
