#!/bin/bash
# Generise sveze Bearer tokene + HMAC shared secrets za alice + bob.
# Output je JSON koji se moze rucno upisati u config fajlove.

set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

python3 - <<'PYEOF'
import secrets
import json

alice_token = secrets.token_urlsafe(32)
bob_token = secrets.token_urlsafe(32)
# Shared secret izmedju alice<->bob — 32 bytes hex
alice_bob_secret = secrets.token_hex(32)

print("# === relay/tokens.json ===")
print(json.dumps({
    alice_token: "alice",
    bob_token: "bob",
}, indent=2))
print()
print("# === examples/alice-config.yaml (HMAC secret pod 'bob' kljucem) ===")
print(f"my_id: alice")
print(f"relay_url: http://localhost:7777")
print(f"bearer_token: {alice_token}")
print(f"peers:")
print(f"  bob: {alice_bob_secret}")
print()
print("# === examples/bob-config.yaml ===")
print(f"my_id: bob")
print(f"relay_url: http://localhost:7777")
print(f"bearer_token: {bob_token}")
print(f"peers:")
print(f"  alice: {alice_bob_secret}  # ISTI secret kao kod alice")
PYEOF
