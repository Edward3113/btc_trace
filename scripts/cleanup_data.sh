#!/usr/bin/env bash
# Free disk space used by btc_trace's large working files.
#
# Lists what can be deleted and how much space it uses. Nothing is deleted unless you
# pass --yes. Everything listed can be regenerated from your node; your reports,
# published pages and findings are never touched.
#
#   scripts/cleanup_data.sh            # dry run: show what would be freed
#   scripts/cleanup_data.sh --yes      # delete the default set
#   scripts/cleanup_data.sh --all      # also list the reveal-scan results
#   scripts/cleanup_data.sh --all --yes
#
# Works from any folder inside the project, and with the bash that ships with macOS.

set -euo pipefail

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; }

DELETE=0
ALL=0
for arg in "$@"; do
  case "$arg" in
    --yes) DELETE=1 ;;
    --all) ALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# Always work from the project's top folder, wherever the script is run from.
cd "$(dirname "$0")/.."
if ! grep -q '^name = "btc-trace"' pyproject.toml 2>/dev/null; then
  echo "this does not look like the btc_trace project folder; stopping" >&2
  exit 1
fi

CACHE="${BTC_TRACE_CACHE:-.btc_trace_cache}"
shopt -s nullglob

TOTAL_KB=0
COUNT=0
TARGETS=()

human() {  # kilobytes -> "1.2 GB" / "340 MB" / "12 KB"
  awk -v kb="$1" 'BEGIN {
    if (kb >= 1048576) printf "%.1f GB", kb / 1048576;
    else if (kb >= 1024) printf "%.0f MB", kb / 1024;
    else printf "%d KB", kb }'
}

consider() {  # consider LABEL WHY PATH...
  local label="$1" why="$2"
  shift 2
  [ "$#" -eq 0 ] && return 0
  local kb
  kb=$(du -sk "$@" | awk '{ s += $1 } END { print s + 0 }')
  printf '  %9s  %s\n' "$(human "$kb")" "$label"
  local p
  for p in "$@"; do printf '             %s\n' "$p"; done
  printf '             %s\n\n' "$why"
  TOTAL_KB=$((TOTAL_KB + kb))
  COUNT=$((COUNT + $#))
  TARGETS+=("$@")
}

if [ "$DELETE" -eq 1 ]; then
  echo "btc_trace cleanup"
else
  echo "btc_trace cleanup (dry run: nothing is deleted)"
fi
echo

consider "UTXO snapshots" \
  "Your results are in reports/. dump-utxos writes a fresh snapshot (at a newer block) if you need one." \
  data/utxo-*.dat
consider "Address lists built from snapshots" \
  "Rebuilt from a snapshot in about 10 minutes by reveal-scan." \
  data/*.targets.npy data/*.tmp.npy
consider "Reveal-scan progress" \
  "Only needed to resume an interrupted reveal-scan." \
  "$CACHE"/reveal-*
consider "Trace and exposure progress" \
  "Only needed to resume an interrupted trace or exposure run, or to make a rerun faster." \
  "$CACHE"/*.scan.json "$CACHE"/*.blocks.jsonl "$CACHE"/*.tmp
if [ "$ALL" -eq 1 ]; then
  consider "Reveal-scan results" \
    "Needed to rerun utxo-stats --revealed. Making them again means a new reveal-scan of several hours." \
    data/*.revealed.npy data/*.revealed.json
fi

# (A counter, not ${#TARGETS[@]}: macOS's bash 3.2 treats an empty array as unset.)
if [ "$COUNT" -eq 0 ]; then
  echo "Nothing to clean up."
  exit 0
fi

echo "  ---------"
if [ "$DELETE" -eq 1 ]; then
  rm -rf -- "${TARGETS[@]}"
  printf '  %9s  freed.\n' "$(human "$TOTAL_KB")"
else
  printf '  %9s  would be freed. Run again with --yes to delete.\n' "$(human "$TOTAL_KB")"
fi
echo
echo "Kept: reports/, docs/, findings/, data/sanctioned_xbt.json and the dump details (data/utxo-*.json)."
if [ "$ALL" -eq 0 ]; then
  echo "Also kept: reveal-scan results (data/*.revealed.npy); add --all to include them."
fi
