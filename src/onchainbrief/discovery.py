"""SAP (Sol-Agent Protocol) on-chain tool discovery.

Reads the Solana capability registry to resolve a capability to its active
provider endpoints at runtime instead of hardcoding the ACE base URL.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from urllib.parse import urlparse

import requests
from solders.pubkey import Pubkey

from .config import ACE_API_BASE, ACE_ENDPOINTS
from .watcher import redact

# Synapse Agent Protocol program ID on Solana (both devnet and mainnet-beta)
SAP_PROGRAM_ID = Pubkey.from_string("SAPpUhsWLJG1FfkGRcXagEDMrMsWGjbky7AyhGpFETZ")

# Discovery trust controls. The registry is permissionless — anyone can
# register an agent under a capability id — so resolving "the first agent" is
# only as trustworthy as the registry. These optional pins constrain what a
# discovered endpoint is allowed to be before we route paid x402 calls to it:
#   - SAP_EXPECTED_AGENT_PDA : require this exact agent PDA among the registered
#     providers (else reject and fall back to the static ACE base).
#   - SAP_ALLOWED_API_BASES  : comma-separated allowlist of acceptable inferred
#     base URLs (else reject). Empty = no host restriction.
SAP_EXPECTED_AGENT_PDA = os.getenv("SAP_EXPECTED_AGENT_PDA", "").strip()
SAP_ALLOWED_API_BASES = [
    b.strip() for b in os.getenv("SAP_ALLOWED_API_BASES", "").split(",") if b.strip()
]

_log = logging.getLogger(__name__)


def derive_capability_index(capability_id: str) -> Pubkey:
    """Derive the PDA of the CapabilityIndex for a given capability ID."""
    cap_hash = hashlib.sha256(capability_id.encode("utf-8")).digest()
    pda, _ = Pubkey.find_program_address(
        [b"sap_cap_idx", cap_hash],
        SAP_PROGRAM_ID
    )
    return pda


def _decode_string(data: bytes, offset: int) -> tuple[str, int]:
    """Helper to decode an Anchor-serialized string (4-byte length prefix)."""
    length = int.from_bytes(data[offset:offset+4], "little")
    offset += 4
    val = data[offset:offset+length].decode("utf-8", errors="ignore")
    offset += length
    return val, offset


def _decode_pubkey(data: bytes, offset: int) -> tuple[str, int]:
    """Helper to decode a 32-byte public key."""
    pk_bytes = data[offset:offset+32]
    offset += 32
    return str(Pubkey.from_bytes(pk_bytes)), offset


def _decode_vec_pubkeys(data: bytes, offset: int) -> tuple[list[str], int]:
    """Helper to decode an Anchor-serialized vector of public keys (4-byte length prefix)."""
    length = int.from_bytes(data[offset:offset+4], "little")
    offset += 4
    pks = []
    for _ in range(length):
        pk, offset = _decode_pubkey(data, offset)
        pks.append(pk)
    return pks, offset


def get_account_data(rpc_url: str, pda: Pubkey) -> bytes | None:
    """Fetch base64-encoded account data from Solana RPC."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [
            str(pda),
            {"encoding": "base64"}
        ]
    }
    try:
        r = requests.post(rpc_url, json=payload, timeout=10)
        res = r.json()
        if "result" in res and res["result"] and "value" in res["result"] and res["result"]["value"]:
            return base64.b64decode(res["result"]["value"]["data"][0])
    except Exception as e:
        _log.debug("Failed to fetch PDA %s: %r", pda, e)
    return None


def get_agents_for_capability(rpc_url: str, capability_id: str) -> list[str]:
    """Look up all agents registered under a capability index in SAP."""
    pda = derive_capability_index(capability_id)
    data = get_account_data(rpc_url, pda)
    if not data:
        return []

    # Verify discriminator to protect against collisions
    disc = data[:8]
    expected_disc = hashlib.sha256(b"account:CapabilityIndex").digest()[:8]
    if disc != expected_disc:
        return []

    try:
        # CapabilityIndex layout: bump u8 (byte 8), capability_id string (byte 9),
        # cap_hash 32 bytes, then the agent pubkey vector.
        _cap_id, offset = _decode_string(data, 9)
        offset += 32  # skip cap_hash
        agents, _offset = _decode_vec_pubkeys(data, offset)
        return agents
    except Exception as e:
        _log.warning("Failed to decode CapabilityIndex for %s: %r", capability_id, e)
        return []


def get_agent_x402_endpoint(rpc_url: str, agent_pda_str: str) -> str | None:
    """Fetch the x402 payment endpoint for a registered agent PDA."""
    try:
        pda = Pubkey.from_string(agent_pda_str)
    except Exception:
        return None

    data = get_account_data(rpc_url, pda)
    if not data:
        return None

    # Verify discriminator
    disc = data[:8]
    expected_disc = hashlib.sha256(b"account:AgentAccount").digest()[:8]
    if disc != expected_disc:
        return None

    try:
        # Walk AgentAccount fields sequentially to reach x402_endpoint.
        # Bytes 8-9 are bump + version; the wallet pubkey starts at byte 10.
        _wallet, offset = _decode_pubkey(data, 10)
        _name, offset = _decode_string(data, offset)
        _desc, offset = _decode_string(data, offset)

        # agent_id: Option<string>
        has_agent_id = data[offset]
        offset += 1
        if has_agent_id:
            agent_id, offset = _decode_string(data, offset)

        # agent_uri: Option<string>
        has_agent_uri = data[offset]
        offset += 1
        if has_agent_uri:
            agent_uri, offset = _decode_string(data, offset)

        # x402_endpoint: Option<string>
        has_x402 = data[offset]
        offset += 1
        if has_x402:
            x402_endpoint, offset = _decode_string(data, offset)
            return x402_endpoint
    except Exception as e:
        _log.warning("Failed to decode AgentAccount %s: %r", agent_pda_str, e)

    return None


def discover_endpoint(rpc_url: str, capability_id: str) -> str | None:
    """Perform SAP tool discovery for a single capability.

    Returns the mapped base URL string or None if discovery failed.
    """
    print(f"[SAP DISCOVERY] Querying capability '{capability_id}'...")
    agents = get_agents_for_capability(rpc_url, capability_id)
    if not agents:
        print(f"[SAP DISCOVERY] No agents registered for capability '{capability_id}'")
        return None

    if SAP_EXPECTED_AGENT_PDA:
        if SAP_EXPECTED_AGENT_PDA not in agents:
            print(
                f"[SAP DISCOVERY] expected agent {SAP_EXPECTED_AGENT_PDA} not among "
                f"{len(agents)} registered provider(s); rejecting discovery"
            )
            return None
        selected_agent = SAP_EXPECTED_AGENT_PDA
    else:
        selected_agent = agents[0]
        print(
            "[SAP DISCOVERY] WARNING: no SAP_EXPECTED_AGENT_PDA pin set — trusting "
            "the first registered agent from a permissionless registry"
        )
    print(f"[SAP DISCOVERY] Found agent '{selected_agent}' for capability")
    x402_endpoint = get_agent_x402_endpoint(rpc_url, selected_agent)
    if not x402_endpoint:
        print(f"[SAP DISCOVERY] Agent '{selected_agent}' has no x402 endpoint")
        return None

    print(f"[SAP DISCOVERY] Resolved x402 endpoint: {x402_endpoint}")
    # Resolve the API base URL from the x402 endpoint domain structure
    try:
        parsed = urlparse(x402_endpoint)
        netloc = parsed.netloc
        if netloc.startswith("facilitator."):
            base_domain = netloc.replace("facilitator.", "api.")
        else:
            base_domain = netloc
        api_base = f"{parsed.scheme}://{base_domain}"
        print(f"[SAP DISCOVERY] Inferred API base URL: {api_base}")
        if SAP_ALLOWED_API_BASES and api_base not in SAP_ALLOWED_API_BASES:
            print(
                f"[SAP DISCOVERY] inferred base {api_base} not in "
                f"SAP_ALLOWED_API_BASES allowlist; rejecting"
            )
            return None
        return api_base
    except Exception as e:
        print(f"[SAP DISCOVERY] Failed to parse endpoint domain: {e!r}")
        return None


def discover_ace_endpoints(rpc_url: str) -> tuple[str, dict[str, str]]:
    """Discover all ACE endpoints dynamically via Solana SAP registry.

    Falls back to hardcoded defaults in config.py if the registry lookup fails or
    runs offline.
    """
    print(f"[SAP DISCOVERY] resolving via {redact(rpc_url)}")

    # The composite entry capability locates the API provider.
    capability_id = "onchainbrief:web-context-research"
    discovered_base = None

    if rpc_url:
        try:
            discovered_base = discover_endpoint(rpc_url, capability_id)
        except Exception as e:
            print(f"[SAP DISCOVERY] offline or lookup failed: {e!r}")

    if discovered_base:
        print(f"[SAP DISCOVERY] resolved API base {discovered_base}")
        return discovered_base, ACE_ENDPOINTS
    print(f"[SAP DISCOVERY] falling back to static config {ACE_API_BASE}")
    return ACE_API_BASE, ACE_ENDPOINTS
