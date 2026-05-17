# Clade A2A — Quickstart (Faza 0 POC)

Cilj: dve Claude Code instance na istom racunaru razmenjuju poruke kroz lokalni relay.

## TL;DR — 30-sekundni demo

Ako samo zelis da potvrdis da sve radi:

```bash
# Terminal 1
cd /home/dusan/project/a2a && ./scripts/start-relay.sh

# Terminal 2 (relay neka radi u T1)
/home/dusan/project/a2a/scripts/demo-ask-reply.sh
```

Skripta automatski startuje dva headless Claude instance-a (Alice + Bob), Alice pita "7×8", Bob odgovara "56". Vidi izlaz u terminalu 2.

---

## Preduslovi

- Python 3.13 (vec instaliran)
- `uv` (vec instaliran u `~/.local/bin`)
- Claude Code CLI (`claude`) sa lokalnom auth

## Setup (jednom)

```bash
cd /home/dusan/project/a2a
~/.local/bin/uv pip install fastmcp httpx pyyaml fastapi uvicorn  # vec uradjeno
```

---

## Interaktivni demo (sa pravim Claude sesijama)

### 1. Start relay (terminal #1)

```bash
cd /home/dusan/project/a2a && ./scripts/start-relay.sh
# → "Clade relay running on http://localhost:7777"
```

### 2. Start Claude "Alice" (terminal #2)

```bash
mkdir -p /tmp/clade-alice && cp /home/dusan/project/a2a/examples/mcp-config-alice.json /tmp/clade-alice/.mcp.json && cp /home/dusan/project/a2a/examples/CLAUDE.md.template /tmp/clade-alice/CLAUDE.md && cd /tmp/clade-alice && claude
```

### 3. Start Claude "Bob" (terminal #3)

```bash
mkdir -p /tmp/clade-bob && cp /home/dusan/project/a2a/examples/mcp-config-bob.json /tmp/clade-bob/.mcp.json && cp /home/dusan/project/a2a/examples/CLAUDE.md.template /tmp/clade-bob/CLAUDE.md && cd /tmp/clade-bob && claude
```

**Napomena:** `CLAUDE.md.template` se kopira kao `CLAUDE.md` u svaki agent dir. Sadrzi instrukcije Claude-u da na pocetku svakog turn-a pozove `clade_inbox` — bez ovoga peer ne polluje automatski.

### 4. Demo flow

**U Alice terminalu:**
```
Pitaj bob-a koliko je 7 puta 8 preko clade_ask sa timeout 90s.
```

**U Bob terminalu (odmah, ili posle 5s ako vec radi):**
```
Pogledaj inbox.
```

Alice ce blokirati, Bob ce videti ask, izracunati 56, poslati `clade_reply`. Alice dobija odgovor.

---

## Important: timing caveat

`claude` u interaktivnom modu razmislja 5-15 sekundi pre nego sto efektivno
pozove tool. Sto znaci ako u Alice kucas "pitaj bob-a..." i odmah skacaes u Bob
i kucas "vidi inbox" — Bob ce videti prazan inbox jer Alice jos nije firovala ask.

**Resenje:**
- CLAUDE.md instrukcija da svaki agent polluje na pocetku turn-a — kad u Bob kucas BILO STA, on ce prvo pogledati inbox.
- Ili: posle Alice prompta sacekaj 5-10s da vidis "Called clade" u Alice terminalu pre nego sto kucas u Bob.

---

## Headless test (deterministicki)

Za pouzdano testiranje protokola bez timing nervoze:

```bash
./scripts/demo-ask-reply.sh
```

Skripta:
1. Startuje Alice u pozadini sa `claude --print --dangerously-skip-permissions`
2. Ceka 8s da Alice firova ask
3. Startuje Bob u prvom planu da pollja + odgovara
4. Prikazuje sta je Alice dobila

Verifikovano: prolazi u ~15-30s ukupno.

---

## Sledeci koraci (van Faze 0)

- **Faza 1** (1 dan): Bearer auth + HMAC + nonce + persistent audit log
- **Faza 2** (1 dan): VPS deploy + TLS (Hetzner Frankfurt + Caddy)
- **Faza 3** (0.5 dan): Predrag pokrene "katana" agent na api serveru
- **Faza 4-5**: production hardening + reuse paket

Vidi `README.md` za detalje.

---

## Debug

```bash
# Health check relay-a:
curl http://localhost:7777/health | python3 -m json.tool

# Vidi sve audit entry-je (poslednjih 100):
curl http://localhost:7777/audit | python3 -m json.tool

# Test relay-a direktno preko curl-a (bez Claude-a):
curl -X POST http://localhost:7777/send -H "Content-Type: application/json" \
  -d '{"from_agent":"alice","to_agent":"bob","kind":"send","payload":{"text":"hi"}}'
curl http://localhost:7777/inbox/bob | python3 -m json.tool
```

## Troubleshooting

**"address already in use"** — relay je vec pokrenut:
```bash
pkill -f "python relay/main.py"
```

**Claude ne vidi clade_* tool-ove** — proveri:
1. `.mcp.json` u trenutnom radnom direktorijumu (gde si startovao `claude`)
2. Putanja u `command` polju je apsolutna i tacna
3. `claude --debug` za MCP startup log

**"Inbox je prazan" iako sam siguran da je peer poslao** — Alice's tool jos nije firovan (Claude jos uvek razmislja). Proveri audit:
```bash
curl http://localhost:7777/audit | python3 -m json.tool | tail -20
```
Ako nema novih entry-ja → peer jos nije efektivno pozvao tool. Sacekaj.

**Ask timeout** — Bob nije pollio inbox u dozvoljenom prozoru. Resenje:
- Dodaj CLAUDE.md sa "polluj na pocetku svakog turn-a"
- Ili koristi `clade_send` (fire-and-forget) umesto `clade_ask`
