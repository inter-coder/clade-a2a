# Clade Relay — Production Deploy (Faza 2)

Dve varijante deploy-a, biras prema network setup-u:

| Varijanta | Kad | TLS | Domen | Caddy |
|---|---|---|---|---|
| **LAN/VPN** | Peer-ovi u istoj mrezi (npr. preko WireGuard) | NE (VPN enkriptuje) | NE (samo IP) | NE |
| **Public VPS** | Peer-ovi na razlicitim kontinentima | DA (Let's Encrypt) | DA | DA |

---

# Varijanta A — LAN/VPN deployment (najjednostavnija)

**Use case:** Dušan dolazi preko VPN-a do Predragovog api servera. Relay tece
na api serveru ili na bilo kojoj masini u LAN-u. Saobracaj je vec enkriptovan
na VPN sloju, pa nije potreban aplikacioni TLS. Bearer + HMAC ostaju aktivni
(defense in depth).

## Preduslovi (KORISNIK radi)

1. **LAN ili VPN konekcija** izmedju svih peer masina (vec imas — WireGuard
   ili OpenVPN tunel do api.katana-home.win)
2. **Host masina** u toj mrezi sa Docker-om gde ce tece relay
   - Moze biti api server, lokalna masina, ili posebna VM
3. **Stabilan IP** te masine u LAN-u/VPN-u (npr. `10.0.0.5`)

## Korak 1 — Setup host masine

Na masini gde ce tece relay:

```bash
# Install Docker (ako vec nije)
curl -fsSL https://get.docker.com | sh

# Klon repo
git clone <your-repo-url> /opt/clade
cd /opt/clade

# Generiši sveze tokene
./scripts/gen-keys.sh > /tmp/clade-keys.txt
cat /tmp/clade-keys.txt
# Kopiraj sekciju "relay/tokens.json" u deploy/tokens.json:
cat > deploy/tokens.json <<'EOF'
{
  "<alice-token>": "alice",
  "<bob-token>": "bob"
}
EOF
chmod 600 deploy/tokens.json
```

## Korak 2 — Deploy stack-a

```bash
cd deploy
docker compose -f docker-compose.lan.yml up -d

# Verify
docker compose -f docker-compose.lan.yml ps
docker compose -f docker-compose.lan.yml logs -f relay
# Treba da vidis:
#   [store] connected to Redis at redis://redis:6379/0
#   [relay] startup — loaded 2 tokens, store=RedisStore
```

## Korak 3 — Restrikcija pristupa (preporuceno)

Ako relay slusa na svim interfejsima (`0.0.0.0:7777`), firewall ga ogranici samo
na LAN/VPN:

```bash
# UFW (Debian/Ubuntu) — dozvoli samo iz LAN/VPN subneta
ufw allow from 10.0.0.0/24 to any port 7777 proto tcp
ufw deny 7777
ufw enable

# Ili — promeni docker-compose.lan.yml da slusa samo na VPN interface-u:
# ports: ["10.0.0.5:7777:7777"]
# (gde je 10.0.0.5 IP host masine u VPN-u)
# pa restart: docker compose -f docker-compose.lan.yml up -d --force-recreate relay
```

## Korak 4 — Smoke test sa peer masine

Sa lokalne masine (Dušan), preko VPN-a:

```bash
# Health check
curl http://10.0.0.5:7777/health
# {"ok":true,"phase":2,"known_agents":["alice","bob"],...,"store":{"backend":"redis",...}}

# Sa pravim bearer token-om (smoke test, HMAC random — relay nece odbiti)
curl -X POST http://10.0.0.5:7777/send -H "Content-Type: application/json" \
  -H "Authorization: Bearer <alice-token>" \
  -d "{\"msg_id\":\"x\",\"from_agent\":\"alice\",\"to_agent\":\"bob\",\"kind\":\"send\",\"payload\":{},\"nonce\":\"smoke-$(date +%s)\",\"timestamp_ms\":$(date +%s%3N),\"hmac\":\"x\"}"
# {"ok":true,"msg_id":"x"}
```

## Korak 5 — Agent config update

Na svakoj peer masini, edituj agent config da pokazuje na LAN IP:

```yaml
# ~/.clade/alice-config.yaml
my_id: alice
relay_url: http://10.0.0.5:7777    # ← LAN IP, ne localhost
bearer_token: <iz tokens.json za alice>
peers:
  bob: <ISTI shared secret kao kod bob-a>
audit_db: ~/.clade/alice-audit.db
```

Onda update `examples/mcp-config-alice.json` da koristi taj config:

```json
{
  "mcpServers": {
    "clade": {
      "command": "/opt/clade/.venv/bin/python",
      "args": ["/opt/clade/agent/main.py"],
      "env": {
        "CLADE_CONFIG": "/home/<user>/.clade/alice-config.yaml"
      }
    }
  }
}
```

---

# Varijanta B — Public VPS deployment (TLS + domen)

**Use case:** Peer-ovi su na razlicitim mrezama bez zajednickog VPN-a. Treba
ti javni endpoint koji moze niko da pogodi (osim sa validnim Bearer-om).

## Preduslovi

1. VPS (npr. Hetzner CPX11 Frankfurt, €4.51/mesec)
2. Sub-domen pod tvojom kontrolom (npr. `clade.symappsys.com`)
3. DNS A record → VPS IP (**Cloudflare: gray cloud / DNS only**, nikako proxied)

## Korak 1 — Provisioning + DNS

```bash
# Hetzner CLI:
hcloud server create --name clade-relay --type cpx11 --image debian-12 \
  --location fsn1 --ssh-key <your-key>
hcloud server list  # pribavi IP
```

U Cloudflare-u: A record `clade` → `<VPS IP>`, Proxy = **DNS only**.

```bash
dig +short clade.symappsys.com  # → mora vratiti VPS IP, ne Cloudflare
```

## Korak 2 — Docker + firewall

```bash
ssh root@<VPS IP>
apt update && apt install -y curl ca-certificates
curl -fsSL https://get.docker.com | sh
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp
ufw --force enable
```

## Korak 3 — Deploy

```bash
git clone <your-repo-url> /opt/clade
cd /opt/clade

./scripts/gen-keys.sh > /tmp/clade-keys.txt
# Kopiraj sekciju "relay/tokens.json" u deploy/tokens.json:
nano deploy/tokens.json
chmod 600 deploy/tokens.json

# Edituj Caddyfile da pokazuje na tvoj domen:
sed -i 's/clade.symappsys.com/<your-domain>/g' deploy/Caddyfile

cd deploy
docker compose up -d  # koristi docker-compose.yml (sa Caddy-jem)

# Caddy ce automatski uzeti Let's Encrypt cert za <your-domain>
# Prvi put moze potrajati 30-60s
```

## Korak 4 — Smoke test

```bash
curl https://<your-domain>/health
# {"ok":true,"phase":2,...,"store":{"backend":"redis","redis_ok":true,...}}
```

## Korak 5 — Agent config

Isti kao LAN varijanta, samo `relay_url: https://<your-domain>` umesto IP-a.

---

# Operativno (oba deploy-a)

### Update relay-a posle code change-a

```bash
ssh root@<host>
cd /opt/clade
git pull
cd deploy
docker compose build relay
docker compose up -d relay      # public varijanta
# ILI:
docker compose -f docker-compose.lan.yml up -d relay   # LAN varijanta
```

### Rotacija token-a

```bash
# Lokalno (na DEVELOPER masini, ne na VPS-u):
./scripts/gen-keys.sh > /tmp/new-keys.txt

# Edituj deploy/tokens.json sa novim mapping-om (mogu paralelno da postoje
# stari + novi tokeni dok ne distribuiras nove svim peer-ovima)
scp deploy/tokens.json root@<host>:/opt/clade/deploy/tokens.json

# Restart relay (Redis state se cuva — inbox-i ostaju, nonce cache se brise
# ali za to je 5min TTL pa nije bitno):
docker compose restart relay

# Distribuiraj nove tokene + HMAC secrets peer-ovima VAN-KANALNO
# (ne preko mejla, koristi Signal/in person/itd)
```

### Backup Redis-a

```bash
docker run --rm -v deploy_redis-data:/data -v /tmp:/backup alpine \
  tar czf /backup/redis-backup-$(date +%F).tar.gz -C /data .
```

### Monitoring

```bash
# Logs (tail):
docker compose logs -f --tail=100 relay

# Health + metrike:
curl http://<host>:7777/health | python3 -m json.tool      # LAN
curl https://<domain>/health | python3 -m json.tool        # Public

# Audit (samo authenticated agent-i mogu citati):
curl http://<host>:7777/audit -H "Authorization: Bearer <token>" | python3 -m json.tool
```

---

# Troubleshooting

**TLS ne radi prvi put (Public)** — Caddy ne moze da dobije Let's Encrypt cert
ako DNS jos nije propagiran. Sacekaj 15min:
```bash
docker compose logs caddy | grep -i "obtaining"
docker compose restart caddy
```

**Relay ne moze do Redis-a** — proveri:
```bash
docker compose logs redis
docker compose exec redis redis-cli ping
docker compose exec relay env | grep REDIS_URL
```

**Replay rejection neocekivano** — clock skew izmedju peer-a i host-a (>5min).
Postavi NTP:
```bash
timedatectl status
systemctl enable --now systemd-timesyncd
```

**Inbox full (503)** — INBOX_MAX = 1000 po agent-u. Peer ne polluje dovoljno
cesto. Resenje: dodaj CLAUDE.md instrukciju da polluje cesce, ili povecaj
INBOX_MAX u `relay/main.py`.

**LAN deploy: agent ne vidi relay** — proveri firewall + da li je relay
bound na pravi interface (`docker compose exec relay ss -tlnp`).

---

# Sledeci korak (Faza 3)

- Predrag pokrene "katana" agenta na api serveru sa skriptom iz Korak 5
- End-to-end test: Frontend-Claude pita Katana-Claude "koliko ABA igraca u staging-u"
- CLAUDE.md za Katana-Claude sa scope-om (read-only DB pristup, roadmap doc itd.)
