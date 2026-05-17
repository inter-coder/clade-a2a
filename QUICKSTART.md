# Clade A2A — Quickstart (Faza 0 POC)

Cilj: dve Claude Code instance na istom racunaru razmenjuju poruke kroz lokalni relay.

## Preduslovi

- Python 3.13 (vec instaliran)
- `uv` (vec instaliran u `~/.local/bin`)
- Claude Code CLI (`claude`)

## Setup (jednom)

```bash
cd /home/dusan/project/a2a
~/.local/bin/uv pip install -e .  # ili: pokreni deps install (vec uradjeno)
```

## Pokretanje

### 1. Start relay (terminal #1)
```bash
cd /home/dusan/project/a2a
./scripts/start-relay.sh
# → "Clade relay running on http://localhost:7777"
```

### 2. Start Claude "Alice" (terminal #2)
```bash
cd /tmp/clade-alice  # bilo koji prazan dir
cp /home/dusan/project/a2a/examples/mcp-config-alice.json ./.mcp.json
claude
```

U Claude prompt:
```
Pozdrav. Ti si "alice". Imas pristup clade_* tool-ovima preko kojih mozes da komuniciras
sa drugim agentom "bob". Posalji bob-u poruku "ćao bobe, alice ovde" preko clade_send.
```

### 3. Start Claude "Bob" (terminal #3)
```bash
cd /tmp/clade-bob
cp /home/dusan/project/a2a/examples/mcp-config-bob.json ./.mcp.json
claude
```

U Claude prompt:
```
Pozdrav. Ti si "bob". Imas pristup clade_* tool-ovima. Pozovi clade_inbox da vidis nove
poruke od alice-a. Ako vidis "ask" poruku, formulisi odgovor i posalji preko clade_reply.
```

### 4. Demo round-trip
U Alice terminalu:
```
Pitaj bob-a koliko je 2+2 preko clade_ask (timeout 60s). Cekaj odgovor i prikazi ga.
```

U Bob terminalu (nakon par sekundi):
```
Pozovi clade_inbox, vidi pitanje, odgovori preko clade_reply.
```

Alice ce dobiti `{"ok": true, "response": {"answer": "4"}}`.

## Sta sad

Ako gornji demo radi → **Faza 0 zavrsena**. Sledece:
- **Faza 1** (sutra): Bearer auth + HMAC + nonce. Vidi `README.md` roadmap.
- **Faza 2**: Deploy relay na VPS sa TLS.
- **Faza 3**: Predrag pokrene "katana" agenta na api serveru.

## Debug

```bash
# Health check relay-a:
curl http://localhost:7777/health

# Vidi sve poruke u alice-inom inbox-u (NE drenira):
# (nema endpoint za to, ali audit log pokriva):
curl http://localhost:7777/audit | python3 -m json.tool
```

## Troubleshooting

**"address already in use"** — relay je vec pokrenut:
```bash
pkill -f "python relay/main.py"
```

**Claude ne vidi clade_* tool-ove** — proveri:
1. Da li je `.mcp.json` u trenutnom radnom direktorijumu Claude sesije
2. Da li je putanja u `command` apsolutna i tacna
3. `claude --debug` da vidis MCP startup log

**Tool poziv blokira zauvek** — proveri da li je drugi peer aktivan i da li polluje `clade_inbox` periodicno.
