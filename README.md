# OnchainBrief

An autonomous agent that watches Solana mainnet, and when a notable on-chain
event lands, turns it into a short sourced brief plus a generated visual, then
attests the artifact on-chain (one Memo-program tx per published brief,
binding the artifact's sha256 + trigger tx to a verifiable receipt).

The content pipeline uses three distinct Ace Data Cloud services, each call
settled on-chain via x402 (USDC on Base):

1. **Search** (`/serp/google`) — pull fresh context for the token/program.
2. **Chat** (`/openai/chat/completions`) — write a tight narrative from it.
3. **Image** (`/nano-banana/images`) — render a "trading card" visual.

A local triage step decides whether an event is worth a brief at all — a
local LLM when one is configured, otherwise a deterministic heuristic over
the decoded instruction type and program id. Either way every paid ACE call
is bound to a real on-chain trigger (max wash-resistance), never synthetic
volume.

**Empirical proof of the on-chain payment path.** All three services have
been settled live on Base mainnet from the dedicated wallet to ACE's
facilitator `0x4F0E2D34…dadCeE7`. The live feed serves four briefs across the
deployment and volume categories; each brief's three ACE calls (search / chat
/ image) are individual USDC `Transfer` receipts on Base — twelve settlements
behind the published feed — and each brief is independently attested on Solana
**mainnet** via the Memo program (one tx per brief, linked from its card, and
reproducible from the published artifacts' SHA-256). Reproduce the Base
settlements with
`python scripts/fetch_basescan_tx.py`. Live feed:
<https://onchainbrief-production.up.railway.app>.

## Try it now (offline, no funds, ~10s)

```
python scripts/demo.py            # -> ./demo-out/site/index.html
```

Runs the exact production pipeline against a sample Solana event with a
deterministic transport that returns ACE's documented response shapes — no
token, no wallet, no network needed. Open the printed `index.html` to see a
real generated brief and its trading-card visual. This is the fastest way to
see what the agent produces; the live on-chain paths (x402 settlement, Memo
attestation, SAP registration) are each verifiable separately below.

## Layout

```
src/onchainbrief/
  config.py             env + ACE endpoints
  watcher.py            Solana logsSubscribe watcher (reconnect, redaction)
  filter.py             local-LLM "brief-worthy?" triage (free; heuristic fallback)
  throttle.py           per-day spend gate (gitignored state)
  ace_client.py         credit-billed transport (dev + cost measurement)
  x402_client.py        x402 on-chain transport (Base/USDC) — production path
  ace_brief_client.py   BriefClient over either transport (parses ACE shapes)
  pipeline.py           event -> serp -> chat -> image -> compose -> attest
  compose.py            trading-card PNG + brief markdown (+ solscan provenance)
  attest.py             per-brief Solana Memo-program attestation (devnet/mainnet)
  feed.py               static index.html (Railway-servable, zero deps)
  run.py                watcher -> filter -> throttle -> pipeline (creds-guarded)
scripts/
  demo.py                  offline end-to-end proof (no funds/token/network)
  e2e_demo.py              live driver — produced the feed briefs (x402-settled, mainnet-attested)
  build_site.py            render the static feed (cold-start safe)
  serve_feed.py            stdlib static server (Railway start command)
  measure_credits.py       one real call per service, records actual cost
  attest_devnet_check.py   real devnet airdrop + 1 attest tx (proves path)
  probe_x402_manual.py     three-service x402 round-trip (captured 3 tx hashes)
  fetch_basescan_tx.py     reproducer — queries Base for the USDC Transfers
sap/                       TypeScript SAP on-chain agent registration shim
  src/register.ts          registers 6 capabilities under one agent identity
tests/test_core.py         offline tests (incl. full e2e agent flow + attest)
```

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # fill ACE_API_TOKEN; never commit .env
```

Secrets live only in `.env` (gitignored). The Solana keypair used for the
on-chain attestation stays outside this repo.

## Verify the attestation path (free, devnet)

```
python scripts/attest_devnet_check.py
```

Loads the keypair from `SOLANA_KEYPAIR_PATH`, tops it up from the public
devnet faucet if needed, and sends one real Memo-program tx using the same
`Attestor` the runtime uses in production. Prints the solscan explorer URL
with `?cluster=devnet`. Flip `ATTEST_CLUSTER=mainnet-beta` for the real
submission run (sub-cent per memo).

## Verify the SAP registration plan (free, no funds, no network writes)

```
cd sap
npm install
npm run sap:plan
```

Prints the full on-chain registration plan: six capabilities under the
agent name **OnchainBrief** (`onchainbrief:event-to-brief`,
`onchainbrief:solana-event-watch`, `onchainbrief:web-context-research`,
`onchainbrief:event-narrative`, `onchainbrief:trading-card-render`,
`onchainbrief:artifact-onchain-attest`), priced at 0.001 SOL per call
with x402 settlement, plus the exact transaction list that `npm run sap`
would send. Capability IDs use the canonical on-chain
`<protocol>:<kebab-name>` form; the on-chain validator rejects
underscore form as `InvalidCapabilityFormat (6026)`. The same
`onchainbrief:event-to-brief` value is written into every per-brief
Memo attestation payload (`attest.CAP_ID`) so external verifiers can
match the receipt against the on-chain capability registry 1:1.

Devnet registration runs with `npm run sap` (free airdrop in-script);
mainnet-beta with `npm run sap:mainnet` (~0.05 SOL real). Keypair
source: `SAP_KEYPAIR_PATH` (Solana CLI JSON byte-array) or
`SAP_PRIVATE_KEY` (base58). Both share the same dedicated address as
`SOLANA_KEYPAIR_PATH` by design.

**Live on Solana mainnet.** The agent is registered: PDA
`DsTZa5xY4sF8y3JFdE53B8T9xEsYtvntEUggm6FwMgVi` (tx
`56XsT7T4xaererxMzJ554L4FGARWaPEysRaV6bT5ikR3xJudEmmqfmsLjxqZUBB3wKxoUH4XsZmpWvccKPhRGoKe`).
Open `explorer.solana.com/address/DsTZa5xY4sF8y3JFdE53B8T9xEsYtvntEUggm6FwMgVi`
to read the on-chain profile — six capabilities, both protocols, pricing
tier, x402 endpoint, active. The script is idempotent: a re-run confirms
the existing agent rather than double-registering.

## Run the agent (live)

```
export ACE_API_TOKEN=...            # credit/dev mode
export ACE_X402_PRIVATE_KEY=...     # add this for the x402 on-chain path
export WATCH_PROGRAM_IDS=Prog1,Prog2
python -m onchainbrief.run          # add --once for a single brief
```

With no creds the agent runs safe (logs candidates, never spends).

## Deploy (Railway — not Vercel)

`railway.toml` builds the static feed and serves it:

- build: `pip install -r requirements.txt && python scripts/build_site.py`
- start: `python scripts/build_site.py && python scripts/serve_feed.py`
  (re-renders the feed, then binds the platform `$PORT`)

The build is cold-start safe — it deploys a valid page even before the
first brief. The start step re-renders before serving, so the live feed always
reflects whatever briefs are present at boot. `Procfile` provides the same
start command for Procfile-based buildpacks.

### Generated artifacts (`briefs/`, `site/`)

`briefs/` holds the curated launch feed — the `.md` + `.png` + `.attest.json`
attestation sidecars that the live pipeline / `e2e_demo.py` produced — and
`site/` is what `build_site.py` renders from them. **Both are committed**, so a
fresh Railway deploy serves the real feed immediately (the deploy regenerates
`site/` from `briefs/` on every release). To regenerate locally, run
`python scripts/demo.py` (offline) or the gated `e2e_demo.py` (paid) and open
`./site/index.html`.

A published brief is **immutable**: its bytes are hashed into the on-chain
Memo, so it must never be hand-edited after attestation (that would break the
verifier). For runtime-added on-demand briefs — or to scale beyond a single
instance — point `BRIEFS_DIR` + `SITE_DIR` (and `SITE_HTML`) at a Railway
persistent volume; the runtime rebuild then picks up whatever the volume holds,
including briefs added by paid on-demand requests between deploys.

### Payment ledger durability

The on-demand paid-brief endpoint records consumed payments in
`PAYMENTS_LEDGER_PATH` (default `.state/payments.json`). This is a **local
JSON file**: it survives process restarts but **not** an ephemeral-filesystem
redeploy, and it is **single-instance only** — running multiple server replicas
would let a payment be replayed across instances. For the bounty's single
Railway instance this is sufficient; for horizontal scale, point
`PAYMENTS_LEDGER_PATH` at a persistent volume or replace `_load_ledger` /
`_save_ledger` with a shared store (Redis/Postgres). The server logs a warning
at startup when the ledger lives under `.state`.
