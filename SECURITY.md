# Security Notes

## x402 Signing Policy

A signed EIP-3009 `TransferWithAuthorization` is a bearer instrument: whoever
holds it can pull `value` USDC from the agent wallet on the named chain. Before
signing any 402 challenge, `onchainbrief.x402_client._validate_accept` enforces:

- **Amount cap** — `maxAmountRequired` must be positive and ≤ `X402_MAX_AMOUNT_REQUIRED`
  (default `1_000_000` atomic = 1 USDC; ~10x the largest observed real call,
  chat at 0.095215 USDC — see `.planning/X402-EVIDENCE.md`).
- **Chain** — `extra.chainId` must equal `X402_EXPECTED_CHAIN_ID` (default 8453, Base).
- **Network / scheme** — must be `base` / `exact`.
- **Asset** — when present, must match `X402_EXPECTED_ASSET` (USDC on Base), case-insensitive.
- **payTo allowlist** — optional (`X402_ALLOWED_PAY_TO`, comma-separated); empty = accept any,
  because the facilitator address can rotate.
- **verifyingContract** — optional pin (`X402_EXPECTED_VERIFYING_CONTRACT`).

A violation raises `X402Error` and aborts the request, so a compromised or
misconfigured facilitator becomes a hard failure rather than a silent wallet
drain. All thresholds are env-overridable for higher-cost services.

## Accepted Dependency Risk

`sap/` is a manual Solana SAP registration shim, not part of the long-running
Railway HTTP server. It loads a local keypair and signs transactions only when
an operator explicitly runs `npm run sap` or `npm run sap:mainnet`.

As of 2026-05-26, `npm audit` still reports moderate advisories through the
Solana JavaScript stack (`@solana/web3.js` -> `jayson` -> `uuid`, plus Anchor
transitives). The available `npm audit fix` path suggests invalid/breaking
versions for this Solana stack, and the latest SDK line introduces additional
high-severity SPL-token transitive findings. The selected version,
`@oobe-protocol-labs/synapse-sap-sdk@0.13.0`, keeps `npm run sap:plan` working
and avoids the high-severity regression observed on `0.18.1`.

The remaining advisories are all **moderate** and confined to this operator-only
tooling; there are **zero high/critical** findings. CI gate: `npm run audit:ci`
(`npm audit --audit-level=high`) fails the build only on high/critical, so a new
serious advisory is caught while the bounded moderate set is explicitly accepted.

Controls:
- Keep `sap/` out of the production web server/runtime.
- Use `npm ci` so `package-lock.json` is honored.
- Run `npm run audit:ci` before any mainnet SAP operation (must exit 0).
- Re-test newer SDK/web3 releases when npm publishes a sane fixed path.
- **Next review: 2026-08-26** (or sooner if `audit:ci` starts failing).

Re-confirmed on **2026-05-30**: `npm run audit:ci` still exits 0 — the advisory
set remains **6 moderate, 0 high/critical**, all confined to the operator-only
`sap/` tooling. No clean upstream upgrade path has appeared; the accepted-risk
posture above is unchanged.
