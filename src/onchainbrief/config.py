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


@dataclass(frozen=True)
class Settings:
    ace_api_token: str
    ace_api_token_image: str
    ace_x402_private_key: str
    solana_rpc_url: str
    solana_ws_url: str
    solana_keypair_path: str
    attest_cluster: str

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            ace_api_token=os.getenv("ACE_API_TOKEN", ""),
            # Image generation requires a per-Application credential — the
            # account-wide token has no image channel ("No available channel
            # for model … under group default"). Falls back to the account
            # token so chat/serp setups don't have to provide it. The legacy
            # `ACE_API_TOKEN_FLUX` env name is preserved as a secondary
            # fallback so old .env files keep working post-rename.
            ace_api_token_image=os.getenv("ACE_API_TOKEN_NANO", "")
            or os.getenv("ACE_API_TOKEN_FLUX", "")
            or os.getenv("ACE_API_TOKEN", ""),
            ace_x402_private_key=os.getenv("ACE_X402_PRIVATE_KEY", ""),
            solana_rpc_url=os.getenv(
                "SOLANA_RPC_URL", "https://us-1-mainnet.oobeprotocol.ai/rpc"
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
