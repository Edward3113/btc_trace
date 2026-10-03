#!/usr/bin/env bash
# node_check.sh — read-only health check of a Bitcoin Core node over JSON-RPC.
# Prompts for the RPC URL and credentials; nothing sensitive is stored in this file.
# Usage: ./node_check.sh   (or set BTC_URL / BTC_USER / BTC_PASS in the environment first)

set -euo pipefail

# Prompt for anything not already set in the environment.
if [[ -z "${BTC_URL:-}" ]]; then
  read -r -p "RPC URL (the LAN address StartOS lists for the RPC interface): " BTC_URL
fi
if [[ -z "${BTC_USER:-}" ]]; then
  read -r -p "RPC user: " BTC_USER
fi
if [[ -z "${BTC_PASS:-}" ]]; then
  read -r -s -p "RPC password: " BTC_PASS
  echo
fi

# Send one JSON-RPC call with no parameters. On a connection or HTTP failure,
# print curl's reason and stop, instead of passing an empty reply to the parser.
rpc() {
  local out
  if ! out=$(curl --silent --show-error --fail-with-body --connect-timeout 10 \
      --user "$BTC_USER:$BTC_PASS" \
      -H 'content-type: text/plain;' \
      --data-binary "{\"jsonrpc\":\"1.0\",\"id\":\"chk\",\"method\":\"$1\",\"params\":[]}" \
      "$BTC_URL" 2>&1); then
    echo "Could not complete '$1': $out" >&2
    echo "Check the RPC interface address and port shown in StartOS." >&2
    exit 1
  fi
  printf '%s' "$out"
}

# Pretty-print the "result" field, or show the RPC error if there is one.
show() {
  python3 -c '
import json, sys
data = json.load(sys.stdin)
if data.get("error"):
    print("RPC error:", data["error"])
    sys.exit(1)
result = data["result"]
keys = sys.argv[1:]
if keys:
    result = {k: result.get(k) for k in keys}
print(json.dumps(result, indent=2))
' "$@"
}

echo "== Node software (getnetworkinfo) =="
reply=$(rpc getnetworkinfo)
printf '%s' "$reply" | show version subversion

echo
echo "== Chain state (getblockchaininfo) =="
reply=$(rpc getblockchaininfo)
printf '%s' "$reply" | show chain blocks headers pruned initialblockdownload verificationprogress size_on_disk

echo
echo "== Indexes (getindexinfo) =="
reply=$(rpc getindexinfo)
printf '%s' "$reply" | show

echo
echo "Done. Redact the hostname before sharing this output."
