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
#   --include-setups    Brisi i ~/.clade/setup-server/* (stari setup-i koje
#                       setup-server respawn-uje na svaki start). Relay-i tih
#                       setup-ova se ubijaju preko njihovih relay.pid fajlova.
#   --prune-audit DAYS  Obrise audit + thread_history zapise starije od DAYS
#                       dana iz ~/.clade/*-audit.db, pa VACUUM da oslobodi
#                       prostor. NE dira live daemon-e (samo SQL).
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
INCLUDE_SETUPS=false
DRY_RUN=false
PRUNE_AUDIT_DAYS=""
SETUP_SERVER_DIR="${HOME}/.clade/setup-server"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y) YES=true; shift ;;
    --include-relay) INCLUDE_RELAY=true; shift ;;
    --include-setups) INCLUDE_SETUPS=true; shift ;;
    --prune-audit) PRUNE_AUDIT_DAYS="$2"; shift 2 ;;
    --prune-audit=*) PRUNE_AUDIT_DAYS="${1#*=}"; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      echo "Unknown flag: $1" >&2
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
# v1.5.0: workdir-ovi su sad u ~/.clade/wd-<peer>-* (ne /tmp). SIGKILL ostavi
# orphan, ali daemon na startup ih sam pociscava — ovo je fallback za slucaj
# da user uopste ne pokrene daemon ponovo.
WORKDIRS=$(ls -d ~/.clade/wd-* 2>/dev/null || true)
PID_FILES=$(find ~/clade-projects -maxdepth 3 \( -name "*-daemon.pid" -o -name "relay.pid" \) 2>/dev/null || true)
RELAY_PIDS=""
if [[ "$INCLUDE_RELAY" == "true" ]]; then
  RELAY_PIDS=$(_python_pgrep "uvicorn.*relay\.main|relay\.main:app|clade-relay")
fi

# v1.4.6: discovery setup-server dir-ova (~/.clade/setup-server/*/) za pruning
SETUPS=""
if [[ "$INCLUDE_SETUPS" == "true" ]] && [[ -d "$SETUP_SERVER_DIR" ]]; then
  for d in "$SETUP_SERVER_DIR"/*/; do
    [[ -d "$d" ]] || continue
    SETUPS+="${d%/}"$'\n'
  done
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
if [[ -n "$WORKDIRS" ]]; then
  echo -e "${YELLOW}Daemon workdir-i u ~/.clade/wd-*:${RESET}"
  while read -r d; do
    [[ -z "$d" ]] && continue
    sz=$(du -sh "$d" 2>/dev/null | awk '{print $1}')
    echo -e "  $d  ${DIM}($sz)${RESET}"
  done <<< "$WORKDIRS"
else
  echo -e "${GREEN}✓${RESET} nema orphan workdir-ova u ~/.clade/wd-*"
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

if [[ "$INCLUDE_SETUPS" == "true" ]]; then
  echo ""
  if [[ -n "$SETUPS" ]]; then
    echo -e "${YELLOW}Setup-server dir-ovi za brisanje:${RESET}"
    while read -r d; do
      [[ -z "$d" ]] && continue
      pname=$(python3 -c "import json,sys; j=json.load(open('$d/setup.json')); print(j.get('project_name','?'))" 2>/dev/null || echo "?")
      relay_pid=$(cat "$d/relay.pid" 2>/dev/null || echo "")
      relay_status="?"
      if [[ -n "$relay_pid" ]]; then
        if kill -0 "$relay_pid" 2>/dev/null; then
          relay_status="${RED}relay PID $relay_pid zivi${RESET}"
        else
          relay_status="${GREEN}relay PID $relay_pid mrtav${RESET}"
        fi
      fi
      echo -e "  $d  ${DIM}(project='$pname', $relay_status)${RESET}"
    done <<< "$SETUPS"
  else
    echo -e "${GREEN}✓${RESET} nema setup dir-ova u $SETUP_SERVER_DIR"
  fi
fi

# ---- Sta uraditi ----
if [[ -z "$DAEMON_PIDS" ]] && [[ -z "$LOCKS" ]] && [[ -z "$PID_FILES" ]] && [[ -z "$RELAY_PIDS" ]] && [[ -z "$SETUPS" ]] && [[ -z "$WORKDIRS" ]]; then
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
  what="daemon procese + lock + PID fajlove"
  [[ "$INCLUDE_RELAY" == "true" ]] && what+=" + relay procesi"
  [[ "$INCLUDE_SETUPS" == "true" ]] && what+=" + setup-server dir-ovi (+ njihovi relay-i)"
  echo -e "${BOLD}Ugasiti $what?${RESET}"
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

if [[ -n "$WORKDIRS" ]]; then
  while read -r d; do
    [[ -z "$d" ]] && continue
    # Skip ako neki daemon je vlasnik (lock pokazuje na zivi PID iste pee-r-a).
    # Konzervativan: ne brisemo workdir-e dok nismo ubili daemons.
    rm -rf "$d" && echo -e "${GREEN}✓${RESET} rm -rf $d"
  done <<< "$WORKDIRS"
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

# v1.4.6+: --include-setups — ubij relay-e tih setup-a (preko relay.pid) pa rm -rf dir
# v1.4.7: PRE kill, sanity proveri da PID zaista pripada relay procesu (ima
# `relay.main` u argv). Bez ovog, stale relay.pid sa recikliranim PID-om bi
# ubio nesto random na sistemu (potencijalno opasno).
if [[ "$INCLUDE_SETUPS" == "true" ]] && [[ -n "$SETUPS" ]]; then
  while read -r d; do
    [[ -z "$d" ]] && continue
    relay_pid=$(cat "$d/relay.pid" 2>/dev/null || echo "")
    if [[ -n "$relay_pid" ]] && kill -0 "$relay_pid" 2>/dev/null; then
      pid_args=$(ps -p "$relay_pid" -o args= 2>/dev/null || echo "")
      if echo "$pid_args" | grep -qE "relay\.main|clade-relay|uvicorn"; then
        kill -TERM "$relay_pid" 2>/dev/null && echo -e "${GREEN}✓${RESET} SIGTERM → setup relay PID $relay_pid"
        sleep 1
        kill -0 "$relay_pid" 2>/dev/null && kill -KILL "$relay_pid" 2>/dev/null
      else
        echo -e "${YELLOW}⚠${RESET} PID $relay_pid postoji ali ARGV ne sadrzi relay.main — SKIPPING (verovatno reciklirani PID)"
        echo -e "  ${DIM}argv: ${pid_args:0:120}${RESET}"
      fi
    fi
    rm -rf "$d" && echo -e "${GREEN}✓${RESET} rm -rf $d"
  done <<< "$SETUPS"
fi

# v1.7.0 (C7): audit pruning
if [[ -n "$PRUNE_AUDIT_DAYS" ]]; then
  if ! [[ "$PRUNE_AUDIT_DAYS" =~ ^[0-9]+$ ]]; then
    echo -e "${RED}ERROR: --prune-audit zahteva pozitivan ceo broj, dobio '$PRUNE_AUDIT_DAYS'${RESET}" >&2
    exit 1
  fi
  echo ""
  echo -e "${BOLD}Audit pruning (zapisi stariji od ${PRUNE_AUDIT_DAYS} dana):${RESET}"
  cutoff_ms=$(python3 -c "import time; print(int((time.time() - ${PRUNE_AUDIT_DAYS}*86400) * 1000))")
  for db in ~/.clade/*-audit.db; do
    [[ -f "$db" ]] || continue
    if [[ "$DRY_RUN" == "true" ]]; then
      n_a=$(python3 -c "import sqlite3; c=sqlite3.connect('$db'); print(c.execute('SELECT COUNT(*) FROM audit WHERE ts_ms < ?', ($cutoff_ms,)).fetchone()[0])" 2>/dev/null)
      n_t=$(python3 -c "import sqlite3; c=sqlite3.connect('$db'); print(c.execute('SELECT COUNT(*) FROM thread_history WHERE ts_ms < ?', ($cutoff_ms,)).fetchone()[0])" 2>/dev/null)
      sz=$(stat -c%s "$db" 2>/dev/null)
      echo -e "  ${DIM}[dry-run]${RESET} $db: bi obrisao $n_a audit + $n_t thread (size $sz)"
    else
      python3 <<PYEOF
import sqlite3
c = sqlite3.connect("$db")
n_a = c.execute("DELETE FROM audit WHERE ts_ms < ?", ($cutoff_ms,)).rowcount
n_t = c.execute("DELETE FROM thread_history WHERE ts_ms < ?", ($cutoff_ms,)).rowcount
c.commit()
c.execute("VACUUM")
c.close()
print(f"  ✓ $db: obrisao {n_a} audit + {n_t} thread, VACUUM gotov")
PYEOF
    fi
  done
fi

echo ""
echo -e "${GREEN}${BOLD}Cleanup gotov.${RESET}"
