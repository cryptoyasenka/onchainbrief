# OnchainBrief — SAP registration

Registers the OnchainBrief agent on Solana via the Synapse Agent
Protocol (SAP). Self-contained TypeScript project — `npm install` here,
not at the repo root.

## What this does

Six on-chain capabilities are published under one agent identity so
that any SAP-discovery client can find OnchainBrief, see what it
offers, and quote its x402 price per call.

The six capabilities mirror the runtime pipeline 1:1:

| Capability id              | Description                                            |
|----------------------------|--------------------------------------------------------|
| `onchain_event_to_brief`   | End-to-end composite — the headline capability         |
| `solana_event_watch`       | Solana logsSubscribe + local-LLM triage                |
| `web_context_research`     | SERP context gathering (ACE Serp API)                  |
| `event_narrative`          | Factual narrative writing (ACE Chat API)               |
| `trading_card_render`      | Branded visual generation (ACE Image API + compose)    |
| `artifact_onchain_attest`  | Solana Memo-program attestation of the finished brief  |

All capabilities are priced identically at **0.001 SOL per call,
settled via x402** (`SettlementMode.X402`, `TokenType.Sol`), 60
calls/min, 100k calls/epoch.

## Setup

```
cd sap
npm install
```

Then point at a dedicated keypair via one of:

```
# Preferred — Solana CLI JSON byte-array file (keep it OUTSIDE the repo):
SAP_KEYPAIR_PATH=/path/to/solana-keypair.json

# OR a base58 secret key (sponsor-compatible env name):
SAP_PRIVATE_KEY=<base58 secret key>
```

Put either in a `.env` file next to this README (or export them in
your shell). The script never generates a keypair — you bring your
own. The same dedicated keypair powers attestation, so a single
funded address covers both submission requirements.

## Commands

```
npm run sap:plan        # Dry-run — prints the full registration plan, no TX
npm run sap             # Devnet registration (free SOL via airdrop)
npm run sap:mainnet     # Mainnet registration (real SOL — ~0.05 expected)
```

Devnet is the default. The `--mainnet` flag (or `npm run sap:mainnet`)
switches to mainnet-beta for Synapse Explorer visibility.

The script is idempotent: re-running it after a successful registration
fetches the existing agent account, skips the register step, and
re-attempts publish + indexing for any tools/indexes that were not yet
created. Safe to retry after partial failures.

## Verifying

After a successful run the script prints a Solana Explorer link to the
agent PDA. Click it to see the on-chain account from any block
explorer; the same address appears in SAP discovery for any consumer
that queries by capability or protocol.

The agent's protocols list is `["x402", "solana"]` — both reflect what
this submission actually does (x402 payment settlement + native Solana
on-chain operations).
