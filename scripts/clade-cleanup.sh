#!/bin/bash
# clade-cleanup.sh — nadji + ugasi sve clade daemon procese, lock fajlove i
# stale PID fajlove. Default: prikazi sta bi se uradilo + pita za potvrdu.
#
# Tipican use case: prethodna Claude session ostavila daemon zive u background,
# pa start-<peer>-daemon.sh pukne sa "drugi daemon vec tece (PID X)".
#
# Flags:
#   --yes, -y           Skip confirm prompt
#   --include-relay     Ugasi i clade-relay process + relay.pid (default: ostavi)
#   --dry-run           Samo prikazi, ne diraj nista
#   -h, --help          Ova poruka

set -e

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
DIM='\033[2m'
RESET='\033[0m'

YES=false
INCLUDE_RELAY=false
DRY_RUN=false

for arg in "$@"; do
  case "$arg" in
    --yes|-y) YES=true ;;
    --include-relay) INCLUDE_RELAY=true ;;
    --dry-run) DRY_RUN=true ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg" >&2
      exit 1
      ;;
  esac
done

# ---- Helper: filtriraj pgrep da vrati samo PROCESS-OF-INTEREST pid-ove ----
# Bash subshells koji izvrsavaju komandu sa "agent.daemon" u argv-u takodje
# matche-uju, pa proveravamo da comm= (basename binary-ja) bude python.
_python_pgrep() {
  local pattern="$1"
  pgrep -f "$pattern" 2>/dev/null | while read -r pid; do
    [[ -z "$pid" ]] && continue
    local comm
    comm=$(ps -p "$pid" -o comm= 2>/dev/null || true)
    [[ "$comm" =~ ^python ]] && echo "$pid"
  done
}

# ---- Discovery ----
echo -e "${BOLD}${CYAN}Clade cleanup${RESET}"
echo ""

DAEMON_PIDS=$(_python_pgrep "agent\.daemon")
LOCKS=$(ls ~/.clade/*-daemon.lock 2>/dev/null || true)
PID_FILES=$(find ~/clade-projects -maxdepth 3 \( -name "*-daemon.pid" -o -name "relay.pid" \) 2>/dev/null || true)
RELAY_PIDS=""
if [[ "$INCLUDE_RELAY" == "true" ]]; then
  RELAY_PIDS=$(_python_pgrep "uvicorn.*relay\.main|relay\.main:app|clade-relay")
fi

# ---- Prikaz ----
if [[ -n "$DAEMON_PIDS" ]]; then
  echo -e "${YELLOW}Daemon procesi:${RESET}"
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    cmdline=$(ps -p "$pid" -o args= 2>/dev/null | head -c 140)
    echo -e "  ${DIM}PID${RESET} $pid  ${DIM}${cmdline}${RESET}"
  done <<< "$DAEMON_PIDS"
else
  echo -e "${GREEN}✓${RESET} nema daemon procesa"
fi

echo ""
if [[ -n "$LOCKS" ]]; then
  echo -e "${YELLOW}Lock fajlovi:${RESET}"
  while read -r lock; do
    [[ -z "$lock" ]] && continue
    pid_in_lock=$(cat "$lock" 2>/dev/null || echo "?")
    if kill -0 "$pid_in_lock" 2>/dev/null; then
      status="${RED}(zivi)${RESET}"
    else
      status="${GREEN}(stale)${RESET}"
    fi
    echo -e "  $lock → PID $pid_in_lock $status"
  done <<< "$LOCKS"
else
  echo -e "${GREEN}✓${RESET} nema lock fajlova"
fi

echo ""
if [[ -n "$PID_FILES" ]]; then
  echo -e "${YELLOW}PID fajlovi u clade-projects/:${RESET}"
  while read -r pidfile; do
    [[ -z "$pidfile" ]] && continue
    pid_in_file=$(cat "$pidfile" 2>/dev/null || echo "?")
    if kill -0 "$pid_in_file" 2>/dev/null; then
      status="${RED}(zivi)${RESET}"
    else
      status="${GREEN}(stale)${RESET}"
    fi
    echo -e "  $pidfile → PID $pid_in_file $status"
  done <<< "$PID_FILES"
else
  echo -e "${GREEN}✓${RESET} nema PID fajlova"
fi

if [[ "$INCLUDE_RELAY" == "true" ]]; then
  echo ""
  if [[ -n "$RELAY_PIDS" ]]; then
    echo -e "${YELLOW}Relay procesi:${RESET}"
    while read -r pid; do
      [[ -z "$pid" ]] && continue
      cmdline=$(ps -p "$pid" -o args= 2>/dev/null | head -c 140)
      echo -e "  ${DIM}PID${RESET} $pid  ${DIM}${cmdline}${RESET}"
    done <<< "$RELAY_PIDS"
  else
    echo -e "${GREEN}✓${RESET} nema relay procesa"
  fi
fi

# ---- Sta uraditi ----
if [[ -z "$DAEMON_PIDS" ]] && [[ -z "$LOCKS" ]] && [[ -z "$PID_FILES" ]] && [[ -z "$RELAY_PIDS" ]]; then
  echo ""
  echo -e "${GREEN}${BOLD}Sve je vec cisto.${RESET}"
  exit 0
fi

echo ""
if [[ "$DRY_RUN" == "true" ]]; then
  echo -e "${DIM}[dry-run] gore je sta bi se uradilo — nista nije dirnuto.${RESET}"
  exit 0
fi

if [[ "$YES" != "true" ]]; then
  if [[ "$INCLUDE_RELAY" == "true" ]]; then
    echo -e "${BOLD}Ugasiti sve gore (daemon + relay procesi + lock + PID fajlovi)?${RESET}"
  else
    echo -e "${BOLD}Ugasiti daemon procese + lock + PID fajlove? (relay ne diram, pokreni sa --include-relay ako i njega zelis)${RESET}"
  fi
  read -rp "[y/N] " CONFIRM
  if [[ "${CONFIRM,,}" != "y" ]]; then
    echo "Prekidam."
    exit 0
  fi
fi

# ---- Cleanup ----
echo ""

if [[ -n "$DAEMON_PIDS" ]]; then
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    if kill -TERM "$pid" 2>/dev/null; then
      echo -e "${GREEN}✓${RESET} SIGTERM → PID $pid"
    fi
  done <<< "$DAEMON_PIDS"
  # Daj 2s za clean shutdown (daemon's release_lock u finally bloku)
  sleep 2
  # Force kill ako jos zivi
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null && echo -e "${YELLOW}⚠${RESET} SIGKILL → PID $pid"
    fi
  done <<< "$DAEMON_PIDS"
fi

if [[ "$INCLUDE_RELAY" == "true" ]] && [[ -n "$RELAY_PIDS" ]]; then
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    kill -TERM "$pid" 2>/dev/null && echo -e "${GREEN}✓${RESET} SIGTERM → relay PID $pid"
  done <<< "$RELAY_PIDS"
fi

# Lock fajlovi se najcesce sami brisu kroz daemon release_lock, ali za stale
# (procesi vec mrtvi) bi ostali — ovde forsiramo
if [[ -n "$LOCKS" ]]; then
  while read -r lock; do
    [[ -z "$lock" ]] && continue
    [[ -e "$lock" ]] && rm -f "$lock" && echo -e "${GREEN}✓${RESET} rm $lock"
  done <<< "$LOCKS"
fi

if [[ -n "$PID_FILES" ]]; then
  while read -r pidfile; do
    [[ -z "$pidfile" ]] && continue
    # Cuvaj relay.pid ako relay nije bio target — moze pokazivati na ziv proces
    if [[ "$(basename "$pidfile")" == "relay.pid" ]] && [[ "$INCLUDE_RELAY" != "true" ]]; then
      pid_in_file=$(cat "$pidfile" 2>/dev/null || echo "")
      if kill -0 "$pid_in_file" 2>/dev/null; then
        echo -e "${DIM}↳ ostavljam $pidfile (relay PID $pid_in_file zivi, ne diram bez --include-relay)${RESET}"
        continue
      fi
    fi
    rm -f "$pidfile" && echo -e "${GREEN}✓${RESET} rm $pidfile"
  done <<< "$PID_FILES"
fi

echo ""
echo -e "${GREEN}${BOLD}Cleanup gotov.${RESET}"
