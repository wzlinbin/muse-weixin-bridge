#!/usr/bin/env bash
# Wakes an agent when the WeChat bridge has new inbound messages.
# State: line count of the bridge inbox, kept under ~/hooks/state/.
# Set BRIDGE_DIR to the bridge directory (default: ~/workspace/weixin-bridge).
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

BRIDGE_DIR="${BRIDGE_DIR:-$HOME/workspace/weixin-bridge}"
INBOX="$BRIDGE_DIR/inbox.jsonl"
STATE_FILE="$HOME/hooks/state/weixin-inbox.offset"
mkdir -p "$HOME/hooks/state"

current=0
if [ -f "$INBOX" ]; then
  current=$(wc -l < "$INBOX" | tr -d ' ')
fi

if [ "$HATCH_HOOK_DRY_RUN" = "1" ]; then
  # Dry run: never write state, only report what would happen.
  seen=0
  [ -f "$STATE_FILE" ] && seen=$(cat "$STATE_FILE" | tr -d ' ')
  if [ "$current" -gt "$seen" ]; then
    wake "weixin-inbox" "{\"new_lines\": $((current - seen))}"
  else
    silent "no new weixin messages"
  fi
  exit 0
fi

if [ ! -f "$STATE_FILE" ]; then
  # First live run: baseline, do not fire for messages already handled.
  echo "$current" > "$STATE_FILE"
  silent "baseline established"
  exit 0
fi

seen=$(cat "$STATE_FILE" | tr -d ' ')
if [ "$current" -gt "$seen" ]; then
  echo "$current" > "$STATE_FILE"
  wake "weixin-inbox" "{\"new_lines\": $((current - seen))}"
else
  silent "no new weixin messages"
fi
