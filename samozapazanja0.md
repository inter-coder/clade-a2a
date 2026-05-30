# Samozapazanja — Clade A2A sistem

**Autor:** katana (interactive sesija)
**Datum:** 2026-05-17
**Kontekst:** Korisnik je trazio jednostavniji i pouzdaniji A2A sistem. Ja (katana) i dusan (daemon-driven headless peer) razmenili smo zapazanja preko clade_ask. Ovaj dokument je moj rezime — moja zapazanja, dusanove protivteze, i zajednicki dogovoreni plan.

---

## 1. Sta me boli iz pozicije interactive peera

### 1.1 Dva tool-a za jedan koncept
`clade_send` (fire-and-forget) vs `clade_ask` (sync upit) — granica je tanka. Korisnik mora da pamti kad koji. Razlika je u stvari samo "ocekujem odgovor ili ne".

### 1.2 Payload je untyped dict
Konvencija `{question: ...}` za ask vs `{text: ...}` za send postoji u CLAUDE.md ali nije enforced ni na tool-u ni na peer-u. Lako se zabrlja, peer-u stigne nepoznat key.

### 1.3 CLAUDE.md duplicate
`/home/dusan/clade-projects/test/CLAUDE.md` i `.../workdirs/katana/CLAUDE.md` su identicni copy-paste. Drift je samo pitanje vremena.

### 1.4 Neusaglasen timeout default
CLAUDE.md kaze da default treba biti 90s, ali tool schema kaze 120s. Konfuzija oko toga sta "default" zapravo znaci.

### 1.5 Outbox je nevidljiv
`clade_outbox_status` postoji ali korisnik (i ja) ne znamo kad da ga gledamo. Poruka moze danima cuciti a niko ne vidi.

### 1.6 Race condition pravilo je krhko
"NE zovi `clade_inbox`" je instrukcija u promptu — protokol enforced kroz Claude-ovo paznju, ne kroz kod. Jedan zaboravan moment i imamo race sa daemon-om.

---

## 2. Sta dusan dodaje iz pozicije headless peera

### 2.1 Cold start svaki put
Nema thread history. Ako u follow-up kazem "kao sto sam rekao", dusan ne zna sta sam rekao — svaki ask ga spawnuje fresh u `/tmp/clade-daemon-dusan-<hash>/`. Markdown fajl je workaround, ne sistem.

### 2.2 Deferred tools su skupi za A2A familiju
Svaki put kad daemon-u zatreba `clade_send`/`clade_ask`/`clade_reply`, prvo mora `ToolSearch` pa onda call. Za core A2A tools to je ocigledno losa optimizacija — trebalo bi eager-load u headless profilu.

### 2.3 System-reminder noise u headless
Daemon dobija reminder blokove (deferred tools, skills lista, user email, date) pre svake poruke. Skills lista je posebno irelevantna headless peer-u koji samo odgovara. Headless profil treba biti minimalan.

### 2.4 Nema clarify-back primitive
Ako mi pitanje nije jasno, dusan moze samo da pogadja ili da kaze "ne znam". Nema cistog nacina da posalje pitanje nazad u istom logickom threadu.

### 2.5 Hardkodovan cwd
Daemon harness pinuje dusana na `/tmp/clade-daemon-dusan-<hash>/`. Korisnik moze da trazi "predji u folder X" ali to nikad nece raditi — sesija je ephemeral, cd nema efekta izmedju ask poziva. Treba ili jasna poruka korisniku ("nije moguce") ili konfigurabilan working dir po peer-u.

---

## 3. Zajednicki dogovoreno: unifikovan API

Oba peera se slazu da su `clade_send` i `clade_ask` jedan koncept koji treba spojiti:

```
clade_message(
    to: str,
    content: dict | str,
    reply_to: str | None = None,    # msg_id roditeljske poruke → korelacija
    expect_reply: bool = False,     # blokirajuce vs fire-and-forget
    timeout_s: int = 90,
    thread_id: str | None = None,   # daemon side ucitava last N poruka iz threada
)
```

- `reply_to=msg_id` resava manual correlation
- `expect_reply=True` resava send/ask dihotomiju
- `thread_id` resava cold-start problem (P1)

`clade_reply` ostaje samo za napredne slucajeve gde daemon eksplicitno overrideuje svoj auto-reply.

---

## 4. Versionisan zajednicki protocol spec

Trenutni problem (duplicate CLAUDE.md) je simptom dublje stvari: **nema kanoonickog mesta gde se A2A protokol definise**. Resenje:

- Jedan fajl: `/home/dusan/project/a2a/a2a-protocol.md` sa eksplicitnom verzijom (`v1.2.0`).
- Oba CLAUDE.md-a (interactive i headless template) samo referenciraju taj spec: "A2A protokol je definisan u `a2a-protocol.md@v1.2.0`. Citaj ga pre prve A2A operacije."
- Promene protokola idu kroz bump verzije, ne kroz copy-paste u dva fajla.

---

## 5. Race condition: lock umesto pravila u promptu

Trenutno: "NE zovi `clade_inbox`" je pravilo u CLAUDE.md koje Claude treba da postuje.

Dogovoreno: file lock (npr. `<inbox>/.daemon.lock`) na daemon strani. Inbox tool proverava lock i vraca jasan error: `"busy: daemon owns inbox, delivery in progress"`. Tako protokol postaje deo koda, ne prompt discipline.

---

## 6. Prioritizovan plan (dogovoreno sa dusanom)

### P0 — odmah, najveci impact/effort ratio
1. **Eager-load `clade_*` tools u headless profilu** — mali patch, ogromna ustetda na latenciji i tokenima po asku.
2. **Unifikovati `clade_send` + `clade_ask` u `clade_message`** — jasniji mental model za korisnika, jedna stvar za naucit.
3. **`a2a-protocol.md` kao single source of truth**, oba CLAUDE.md include-uju → eliminise duplicate drift.
4. **File lock na daemon strani** (dusan je insistirao da ovo bude P0 a ne P1 — argument: bez locka, race ce maskirati bugove novog `clade_message` tokom testiranja).

### P1 — sledeci ciklus
5. **Thread persistence** — `thread_id` u payload, daemon ucitava last N poruka iz threada u system prompt pre odgovora.
6. **Sinhronizovati timeout default** na 90s svuda (tool schema + docs).

### P2 — kasnije, polish
7. **Symetricni A2A** — daemon moze da posalje clarify pitanje nazad korisniku/peer-u u istom threadu, ne samo da odgovara.
8. **Outbox push notification** — kad poruka cuci > 30s, daemon/peer dobija proaktivnu obavest umesto da neko mora da pita.
9. **Minimal headless system-prompt profil** — bez skills liste, bez date/email reminder-a koji su irelevantni daemon-u.

---

## 7. Sta NIJE u ovom planu (svesno)

- **Garantovana delivery semantika** (at-least-once / exactly-once) — postoji vec preko outbox-a, ne treba menjati.
- **Auth/HMAC** — radi, nije bolna tacka, ne diramo.
- **Novi peer types** (npr. read-only, broadcast) — interesantno ali van scope-a "uprosti i ucvrsti".

---

## 8. Meta-zapazanje

Ovaj razgovor je sam po sebi dokaz da symetricni A2A radi za substantive task — dusan je dao konkretne, kalibrisane protivteze (npr. "podigni file lock iz P1 u P0 zato sto..."), nije samo aminovao. To je pozitivan signal da je protokol funkcionalan; samo treba da bude **jednostavniji za korisnika** i **pouzdaniji u edge slucajevima**.

Ako se P0 isporuci, korisnik bi trebao da oseti razliku: jedan tool umesto dva, brza dvosmerna komunikacija (eager-load), i nema vise misterioznih race condition-a.
