"""Runtime config, loaded from environment (.env is gitignored)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# Standard credit-billed base. Each service hangs off this host.
ACE_API_BASE = "https://api.acedata.cloud"

# Endpoint paths
ACE_ENDPOINTS = {
    "chat": "/openai/chat/completions",
    "serp": "/serp/google",
    "image": "/nano-banana/images",
}
ACE_IMAGE_TASKS_PATH = "/nano-banana/tasks"
IMAGE_MODEL = "nano-banana"
IMAGE_SIZE = "16:9"
SERP_RESULT_COUNT = 5



# x402 settles on Base mainnet in USDC; facilitator covers gas.
X402_NETWORK = "base"

# x402 signing-policy guardrails. The client validates each 402 challenge
# against these BEFORE signing an EIP-3009 TransferWithAuthorization, so a
# malicious or misconfigured facilitator can't make us authorize an oversized
# transfer, the wrong chain, an unexpected token, or an unapproved recipient.
# The default cap (1 USDC atomic = 1_000_000) sits ~10x above the largest
# observed real call (chat = 0.095215 USDC) — see .planning/X402-EVIDENCE.md —
# and is env-overridable for higher-cost services.
X402_MAX_AMOUNT_REQUIRED = int(os.getenv("X402_MAX_AMOUNT_REQUIRED", "1000000"))
X402_EXPECTED_CHAIN_ID = int(os.getenv("X402_EXPECTED_CHAIN_ID", "8453"))
X402_EXPECTED_ASSET = os.getenv(
    "X402_EXPECTED_ASSET", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
)
# Comma-separated allowlist of accepted payTo addresses; empty = accept any
# (the facilitator address can rotate, so pinning it is opt-in).
X402_ALLOWED_PAY_TO = os.getenv("X402_ALLOWED_PAY_TO", "")
# Optional EIP-712 verifyingContract pin (the USDC token contract); empty = skip.
X402_EXPECTED_VERIFYING_CONTRACT = os.getenv("X402_EXPECTED_VERIFYING_CONTRACT", "")

# Single source of truth for the Solana RPC endpoints the browser verifier hits.
# Both the generated feed JS (feed.py) and the server CSP connect-src
# (scripts/serve_feed.py) read these, so a verify fetch is never CSP-blocked by
# a host the page is allowed to call. api.mainnet-beta is rate-limited and lacks
# permissive CORS, so the keyless publicnode archival RPC is the mainnet default.
DEVNET_RPC_URL = os.getenv("DEVNET_RPC_URL", "https://api.devnet.solana.com")
MAINNET_RPC_URL = os.getenv("MAINNET_RPC_URL", "https://solana-rpc.publicnode.com")


@dataclass(frozen=True)
class Settings:
    ace_api_token: str
    ace_api_token_image: str
    ace_x402_private_key: str
    solana_rpc_url: str
    solana_tx_rpc_url: str
    solana_ws_url: str
    solana_keypair_path: str
    attest_cluster: str

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            ace_api_token=os.getenv("ACE_API_TOKEN", ""),
            # Image generation requires a per-Application credential - the
            # account-wide token has no image channel ("No available channel
            # for model … under group default"). Falls back to the account
            # token so chat/serp setups don't have to provide it. The legacy
            # `ACE_API_TOKEN_FLUX` env name is preserved as a secondary
            # fallback so old .env files keep working post-rename.
            ace_api_token_image=os.getenv("ACE_API_TOKEN_NANO", "")
            or os.getenv("ACE_API_TOKEN_FLUX", "")
            or os.getenv("ACE_API_TOKEN", ""),
            ace_x402_private_key=os.getenv("ACE_X402_PRIVATE_KEY", ""),
            # Keyless public default so a fresh clone works out of the box; real
            # deploys override SOLANA_RPC_URL with the keyed OOBE Synapse URL in
            # .env (matches run.py / e2e_demo.py, which already default to this).
            solana_rpc_url=os.getenv(
                "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
            ),
            # getTransaction RPC (OOBE free tier can't serve it - see txfacts).
            # Keyless archival fallback by default.
            solana_tx_rpc_url=os.getenv(
                "SOLANA_TX_RPC_URL", "https://solana-rpc.publicnode.com"
            ),
            solana_ws_url=os.getenv(
                "SOLANA_WS_URL", "wss://us-1-mainnet.oobeprotocol.ai/ws"
            ),
            solana_keypair_path=os.getenv("SOLANA_KEYPAIR_PATH", ""),
            attest_cluster=os.getenv("ATTEST_CLUSTER", "devnet"),
        )

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise RuntimeError(
                f"Missing required env: {', '.join(missing)} — see .env.example"
            )
