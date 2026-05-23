"""Runtime config, loaded from environment (.env is gitignored)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# Standard credit-billed base. Each service hangs off this host.
ACE_API_BASE = "https://api.acedata.cloud"

# x402 settlement lives on the platform host (orders + on-chain pay).
ACE_PLATFORM_BASE = "https://platform.acedata.cloud"

# Endpoint paths — researched from the official AceDataCloud repos/docs
# (SerpAPI README + docs/google_serp_api_integration_guide.md, OpenAIAPI
# README, NanoBanana README; the NanoBanana image contract mirrors the
# older FluxAPI shape this code was originally researched against): these
# are doc-derived, not guesses; the live credit/wrapper shape is still
# confirmed empirically at the first funded call.
ACE_ENDPOINTS = {
    "chat": "/openai/chat/completions",   # OpenAIAPI: choices[0].message.content
    "serp": "/serp/google",               # SerpAPI: body {"query":...}, resp {"organic":[...]}
    "image": "/nano-banana/images",       # NanoBanana (Gemini wrapper): same shape as Flux
}
# Same async/poll convention NanoBanana inherits from the older FluxMCP
# core/client.py. The `IMAGE_*` names are provider-neutral after the move
# to nano-banana so the code reads honestly: paths and model are
# nano-banana, but the protocol shape (sync result OR async task_id → poll)
# is the same we'd reuse if we bind another generative-image endpoint later.
ACE_IMAGE_TASKS_PATH = "/nano-banana/tasks"
IMAGE_MODEL = "nano-banana"      # nano-banana | nano-banana-2 | nano-banana-pro
IMAGE_SIZE = "16:9"              # NanoBanana takes aspect ratio, not WxH
SERP_RESULT_COUNT = 5            # keep ≤10 (cheaper SERP pricing tier)

# x402 pay flow on the platform host (Flow B explicit-order pay).
ACE_ORDER_PAY_PATH = "/api/v1/orders/{order_id}/pay/"

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
                "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
            ),
            solana_ws_url=os.getenv(
                "SOLANA_WS_URL", "wss://api.mainnet-beta.solana.com"
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
