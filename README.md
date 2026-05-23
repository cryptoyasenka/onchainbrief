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

A local model decides whether an event is worth a brief at all, so every paid
ACE call is bound to a real on-chain trigger (max wash-resistance).

**Empirical proof of the on-chain payment path.** All three services have
been settled live on Base mainnet from the dedicated wallet to ACE's
facilitator: 9 USDC `Transfer` receipts on Base (3 service-types × probe
burn + 6 from the end-to-end demo). Total burn 0.328491 USDC. Live feed
serving the resulting briefs: <https://onchainbrief-production.up.railway.app>.

## Layout

```
src/onchainbrief/
  config.py             env + ACE endpoints (doc-derived from official repos)
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
  e2e_demo.py              live driver — produced the 2 real briefs on the feed
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

## Try it (offline, no funds)

```
python scripts/demo.py            # -> ./demo-out/site/index.html
```

Runs the exact production pipeline against a sample Solana event with a
deterministic transport that returns ACE's documented response shapes. Open
the printed `index.html` to see a real generated brief.

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
- start: `python scripts/serve_feed.py` (binds the platform `$PORT`)

The build is cold-start safe — it deploys a valid page even before the
first brief. `Procfile` provides the same start command for Procfile-based
buildpacks.
