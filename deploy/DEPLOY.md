# Clade Relay — Production Deploy (Faza 2)

Step-by-step deploy na VPS sa TLS-om kroz Caddy + persistent Redis backend.

---

## Sta dobijas

- Relay javno dostupan na `https://clade.symappsys.com` (ili tvoj domen)
- Automatski TLS preko Let's Encrypt (besplatno, auto-renewal)
- Redis persistence (state preživljava restart relay-a)
- Health check sa auto-restart kroz Docker
- Caddy security headers (HSTS, no-cache, server fingerprint stripped)

---

## Preduslovi (KORISNIK radi)

1. **VPS** — Hetzner CPX11 Frankfurt (2vCPU, 2GB RAM, €4.51/mesec)
   - Alternativa: bilo koji VPS sa Docker support-om
2. **Domen** — sub-domain pod tvojom kontrolom (npr. `clade.symappsys.com`)
3. **DNS A record** koji pokazuje na VPS public IP — preko Cloudflare,
   namecheap-a, ili sta vec koristis. **VAZNO:** Cloudflare proxy MORA
   biti **isključen** (DNS only, "gray cloud") — TLS originira na Caddy-u,
   ne hocemo dvostruko.

---

## Korak 1 — Provisioning VPS-a

```bash
# Sa lokalne masine (Hetzner CLI):
hcloud server create \
  --name clade-relay \
  --type cpx11 \
  --image debian-12 \
  --location fsn1 \
  --ssh-key <your-key-name>

# Pribavi IP:
hcloud server list
# → koristis ovaj IP za DNS A record
```

Ili preko Hetzner Cloud web konzole — isti rezultat.

---

## Korak 2 — Konfiguracija DNS-a

U Cloudflare (ili tvoj DNS provider):

```
Type: A
Name: clade
Content: <VPS IP>
Proxy status: DNS only (NE Proxied)
TTL: Auto
```

Cekaj 5-15min da propagira. Provera:

```bash
dig +short clade.symappsys.com
# → mora vratiti VPS IP, ne Cloudflare IP
```

---

## Korak 3 — Setup VPS-a (SSH)

```bash
ssh root@<VPS IP>

# Install Docker
apt update && apt install -y curl ca-certificates
curl -fsSL https://get.docker.com | sh

# Verify
docker --version  # → Docker version 27.x
docker compose version  # → Docker Compose version v2.x

# Otvori firewall (UFW)
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp  # HTTP/3
ufw --force enable
```

---

## Korak 4 — Deploy Clade-a

```bash
# Klon repo (ili kopiraj direktorijum)
git clone <your-repo-url> /opt/clade
cd /opt/clade

# Generiši sveze tokene (sacuvaj output negde sigurno — to su secrets!)
./scripts/gen-keys.sh > /tmp/clade-keys.txt
cat /tmp/clade-keys.txt
# → kopiraj sekciju "tokens.json" u deploy/tokens.json
# → secrets iz alice-config.yaml + bob-config.yaml zapamti za peer masine

# Kreiraj tokens.json u deploy/ direktorijumu
# Format:
# {
#   "<token1>": "alice",
#   "<token2>": "bob"
# }
nano deploy/tokens.json  # ili scp sa lokalne masine
chmod 600 deploy/tokens.json

# Edituj Caddyfile da pokazuje na tvoj domen
sed -i 's/clade.symappsys.com/<tvoj-domen>/g' deploy/Caddyfile

# Start stack
cd deploy
docker compose up -d

# Verify
docker compose ps
docker compose logs -f relay
# Treba da vidis:
#   [store] connected to Redis at redis://redis:6379/0
#   [relay] startup — loaded 2 tokens, store=RedisStore
```

---

## Korak 5 — Smoke test

Sa **lokalne masine** (ili bilo gde):

```bash
# Health check
curl https://clade.symappsys.com/health
# Treba da vidis:
# {"ok":true,"phase":2,"known_agents":["alice","bob"],...,"store":{"backend":"redis","redis_ok":true,...}}

# Probaj send sa pogresnim token-om
curl -X POST https://clade.symappsys.com/send -H "Content-Type: application/json" \
  -H "Authorization: Bearer BAD" -d '{}'
# → 401 Unauthorized

# Probaj send sa pravim Alice token-om (ali nepotpunim envelope-om)
curl -X POST https://clade.symappsys.com/send -H "Content-Type: application/json" \
  -H "Authorization: Bearer <alice-token-iz-tokens.json>" \
  -d '{"msg_id":"x","from_agent":"alice","to_agent":"bob","kind":"send","payload":{},"nonce":"test1","timestamp_ms":'$(date +%s%3N)',"hmac":"x"}'
# → 200 ok (HMAC se ne validira na relay strani — to je E2E)
```

---

## Korak 6 — Agent na lokalnoj masini (peer setup)

Posle relay deploy-a, na tvojoj devel masini (a kasnije na Predragovom serveru):

```bash
# Update agent config da pokazuje na produkciju
# examples/alice-config.yaml:
cat > ~/.clade/alice-config.yaml <<'YAML'
my_id: alice
relay_url: https://clade.symappsys.com
bearer_token: <iz tokens.json za alice>
peers:
  bob: <ISTI shared secret kao u bob-config.yaml>
audit_db: ~/.clade/audit.db
YAML
chmod 600 ~/.clade/alice-config.yaml

# Probaj
cd /home/dusan/project/a2a
CLADE_CONFIG=~/.clade/alice-config.yaml .venv/bin/python -c "
import asyncio, sys
sys.path.insert(0, '.')
from agent.main import _make_envelope, _auth_headers, cfg
import httpx
async def main():
    env = _make_envelope('send', 'bob', {'text': 'ping from local'})
    async with httpx.AsyncClient() as c:
        r = await c.post(f'{cfg.relay_url}/send', json=env, headers=_auth_headers(), timeout=10)
    print(r.status_code, r.text)
asyncio.run(main())
"
# → 200 ok
```

---

## Korak 7 — Predrag agent (kad bude vreme)

Iste komande, samo:
- Drugi config (bob-config.yaml) i drugi bearer_token
- Pokrece se na njegovom api serveru gde Claude Code MCP server moze da ga spawn-uje
- ISTI shared HMAC secret kao u alice-config.yaml (to je shared secret pair)

---

## Operativno

### Update relay-a posle code change-a

```bash
ssh root@<VPS>
cd /opt/clade
git pull
cd deploy
docker compose build relay
docker compose up -d relay
```

### Rotacija token-a

```bash
# Lokalno
./scripts/gen-keys.sh > /tmp/new-keys.txt
# Edituj /opt/clade/deploy/tokens.json sa novim mapping-om
# Distribuiraj nove tokene + secrets peer-ovima van-kanalno (NE preko mejla)
# Restart relay-a:
docker compose restart relay
```

### Backup Redis-a

```bash
# Redis ima auto-snapshot na 60s ako se 1 kljuc promenio (--save 60 1)
# + appendonly file (AOF). Backup volume:
docker run --rm -v deploy_redis-data:/data -v /tmp:/backup alpine \
  tar czf /backup/redis-backup-$(date +%F).tar.gz -C /data .
```

### Monitoring

```bash
# Logs (tail):
docker compose logs -f --tail=100

# Metrike:
curl https://clade.symappsys.com/health | jq

# Audit:
curl https://clade.symappsys.com/audit -H "Authorization: Bearer <any-valid-token>" | jq
```

---

## Troubleshooting

**TLS ne radi prvi put** — Caddy ne moze da dobije Let's Encrypt cert ako DNS jos nije propagiran. Sacekaj 15min, restart `docker compose restart caddy`.

**Relay ne moze do Redis-a** — proveri:
```bash
docker compose logs redis
docker compose exec redis redis-cli ping
```

**Replay rejection neocekivano** — moguci uzrok je clock skew izmedju peer-a i VPS-a (>5min). Postavi NTP na obe masine:
```bash
timedatectl status
systemctl enable --now systemd-timesyncd
```

**Inbox full** — INBOX_MAX = 1000 po agent-u. Ako stigne tu, peer ne polluje
dovoljno cesto. Resenje: polluj cesce, ili povecaj INBOX_MAX u `relay/main.py`.

---

## Sledeci korak (Faza 3)

- Predrag pokrene "katana" agenta na api serveru sa skriptom iz Korak 7
- End-to-end test: Frontend-Claude pita Katana-Claude "koliko ABA igraca u staging-u"
- CLAUDE.md na strani Katana-Claude-a sa scope-om (read-only DB, roadmap doc)

Vidi root `README.md` za detaljnu Faza 3 listu.
