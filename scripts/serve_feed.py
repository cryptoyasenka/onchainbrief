"""Tiny multi-threaded server for the OnchainBrief feed (Railway start command).

Serves the built ./site directory (index.html + briefs), manages SSE clients
for real-time dashboard updates, handles payment-verified custom brief requests,
and starts the background log watcher for fully autonomous operation.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import pathlib
import queue
import re
import sys
import threading
import time
import http.server
import socketserver
from urllib.parse import urlsplit, parse_qs
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import onchainbrief.run as run  # noqa: E402
from onchainbrief.config import (  # noqa: E402
    DEVNET_RPC_URL,
    MAINNET_RPC_URL,
    PAYMENT_CLUSTER,
    PAYMENT_RPC_URL,
    REQUEST_BRIEF_LAMPORTS,
    TARGET_TX_RPC_URL,
)
from onchainbrief.watcher import LogEvent  # noqa: E402
from onchainbrief.filter import watch_config, WATCH_CONFIG_PATH  # noqa: E402


def _rpc_origin(url: str) -> str:
    """https://host[:port] of an RPC URL - the form a CSP connect-src needs."""
    from urllib.parse import urlsplit

    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


def build_csp() -> str:
    """Content-Security-Policy. connect-src is derived from the SAME RPC config
    the feed verifier uses (DEVNET_RPC_URL/MAINNET_RPC_URL), so a Verify-On-Chain
    fetch is never blocked by a host the page is allowed to call."""
    rpc_origins = sorted({
        o for o in (
            _rpc_origin(DEVNET_RPC_URL),
            _rpc_origin(MAINNET_RPC_URL),
            _rpc_origin(PAYMENT_RPC_URL),
            _rpc_origin(TARGET_TX_RPC_URL),
        ) if o
    })
    connect_src = " ".join(["'self'", *rpc_origins])
    return (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        f"connect-src {connect_src}; "
        "base-uri 'none'; frame-ancestors 'none'"
    )

SITE_DIR = pathlib.Path(os.getenv("SITE_DIR", "./site")).resolve()
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")  # Railway requires external bind
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", "4096"))
PAYMENT_LAMPORTS = REQUEST_BRIEF_LAMPORTS  # single source of truth (config.py)
PAYMENT_MAX_AGE_S = int(os.getenv("PAYMENT_MAX_AGE_S", "1800"))
ONDEMAND_QUEUE_MAX = int(os.getenv("ONDEMAND_QUEUE_MAX", "10"))
ONDEMAND_WORKERS = int(os.getenv("ONDEMAND_WORKERS", "1"))
RATE_LIMIT_WINDOW_S = int(os.getenv("REQUEST_RATE_LIMIT_WINDOW_S", "60"))
RATE_LIMIT_PER_IP = int(os.getenv("REQUEST_RATE_LIMIT_PER_IP", "5"))
GLOBAL_RATE_LIMIT = int(os.getenv("REQUEST_GLOBAL_RATE_LIMIT", "20"))
PAYMENTS_LEDGER_PATH = pathlib.Path(
    os.getenv("PAYMENTS_LEDGER_PATH", ".state/payments.json")
)
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "")
# Local operator panel. The watch-config write endpoint is gated by this shared
# secret. EMPTY = panel fully disabled (every /operator route 404s). This is the
# safety default: the server binds 0.0.0.0 on Railway, so a public deploy must
# never expose a write endpoint unless the host explicitly sets OPERATOR_TOKEN.
OPERATOR_TOKEN = os.getenv("OPERATOR_TOKEN", "").strip()
# Hard ceiling so a fat-fingered or hostile min_usd can't be set to absurd values.
OPERATOR_MAX_MIN_USD = 1e12
BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,100}$")
MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# SSE client registry
sse_clients = set()
sse_lock = threading.Lock()
ledger_lock = threading.Lock()
rate_lock = threading.Lock()
rate_events: dict[str, list[float]] = {}
global_rate_events: list[float] = []
demand_jobs: queue.Queue[dict[str, str]] = queue.Queue(maxsize=ONDEMAND_QUEUE_MAX)
workers_started = False

def resolve_payment_wallet() -> str:
    configured = os.getenv("AGENT_PAYMENT_WALLET", "").strip()
    if configured:
        return configured
    kp_path = os.getenv("SOLANA_KEYPAIR_PATH", "")
    if kp_path and pathlib.Path(kp_path).exists():
        try:
            from solders.keypair import Keypair
            kp = Keypair.from_json(pathlib.Path(kp_path).read_text(encoding="utf-8"))
            return str(kp.pubkey())
        except Exception as e:
            print(f"[SERVER] Failed to load keypair for payment wallet: {e}")
    return ""


# Resolve Agent Payment Wallet Address. Empty means paid requests are disabled.
agent_payment_wallet = resolve_payment_wallet()

# On-demand flow RPC endpoints (config.py). The payment and the analyzed
# transaction live on different clusters, so they use different RPCs:
#   - PAYMENT_RPC_URL    : verify the user's SOL payment (same cluster the
#     frontend signs on, default devnet).
#   - TARGET_TX_RPC_URL  : fetch the transaction being analyzed (default
#     mainnet, matching the autonomous watcher).
payment_rpc_url = PAYMENT_RPC_URL
target_rpc_url = TARGET_TX_RPC_URL


def broadcast_sse(msg: str):
    """Sends a message to all connected SSE clients."""
    with sse_lock:
        closed_clients = set()
        for q in sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                closed_clients.add(q)
        for c in closed_clients:
            sse_clients.discard(c)


# Bind the pipeline callback to trigger SSE updates
def handle_brief_compiled(sig: str):
    print(f"[SSE] Broadcasting compiled brief for tx: {sig}")
    broadcast_sse(json.dumps({"type": "new_brief", "signature": sig}))

run.ON_BRIEF_COMPILED_CALLBACK = handle_brief_compiled


def _decode_base58(data: str) -> bytes:
    n = 0
    for ch in data:
        idx = _B58_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError("invalid base58")
        n = n * 58 + idx
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(data) - len(data.lstrip("1"))) + raw


def _load_ledger() -> dict:
    try:
        data = json.loads(PAYMENTS_LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"payments": {}, "nonces": {}}
    if not isinstance(data, dict):
        return {"payments": {}, "nonces": {}}
    data.setdefault("payments", {})
    data.setdefault("nonces", {})
    return data


def _save_ledger(data: dict) -> None:
    PAYMENTS_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAYMENTS_LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    tmp.replace(PAYMENTS_LEDGER_PATH)


def _reserve_payment(payment_sig: str, target_sig: str, payer_wallet: str, nonce: str) -> tuple[bool, str]:
    with ledger_lock:
        ledger = _load_ledger()
        if payment_sig in ledger["payments"]:
            return False, "Payment transaction has already been used"
        if nonce in ledger["nonces"]:
            return False, "Payment nonce has already been used"
        record = {
            "target_sig": target_sig,
            "payer_wallet": payer_wallet,
            "nonce": nonce,
            "accepted_at": int(time.time()),
            "status": "queued",
        }
        ledger["payments"][payment_sig] = record
        ledger["nonces"][nonce] = payment_sig
        _save_ledger(ledger)
    return True, "reserved"


def _mark_payment_status(payment_sig: str, status: str) -> None:
    with ledger_lock:
        ledger = _load_ledger()
        if payment_sig in ledger["payments"]:
            ledger["payments"][payment_sig]["status"] = status
            ledger["payments"][payment_sig]["updated_at"] = int(time.time())
            _save_ledger(ledger)


def _release_payment(payment_sig: str, nonce: str) -> None:
    """Undo a reservation so a verified payment isn't permanently consumed when
    it could not be enqueued (queue full). Without this the payment stays
    recorded in the ledger and any retry hits 'already been used' — the user
    paid but can never receive a brief."""
    with ledger_lock:
        ledger = _load_ledger()
        changed = ledger["payments"].pop(payment_sig, None) is not None
        if ledger["nonces"].get(nonce) == payment_sig:
            del ledger["nonces"][nonce]
            changed = True
        if changed:
            _save_ledger(ledger)


def _extract_key(key) -> str:
    return key.get("pubkey", "") if isinstance(key, dict) else str(key)


def _extract_payer(account_keys: list) -> str:
    for key in account_keys:
        if isinstance(key, dict) and key.get("signer"):
            return str(key.get("pubkey", ""))
    return _extract_key(account_keys[0]) if account_keys else ""


def _extract_memo_text(tx: dict) -> str:
    message = tx.get("transaction", {}).get("message", {})
    for ix in message.get("instructions", []) or []:
        if not isinstance(ix, dict):
            continue
        if ix.get("programId") != MEMO_PROGRAM_ID:
            continue
        parsed = ix.get("parsed")
        if isinstance(parsed, str):
            return parsed
        data = ix.get("data")
        if isinstance(data, str):
            try:
                return _decode_base58(data).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return ""
    for log in tx.get("meta", {}).get("logMessages", []) or []:
        marker = "Program log: Memo (v2):"
        if marker in log:
            return log.split(marker, 1)[1].strip()
    return ""


def _parse_payment_memo(memo_text: str) -> dict:
    if not memo_text:
        raise ValueError("Payment memo is required")
    try:
        body = json.loads(memo_text)
    except ValueError as e:
        raise ValueError("Payment memo must be JSON") from e
    if not isinstance(body, dict):
        raise ValueError("Payment memo must be a JSON object")
    return body


def _validate_signature(value: str) -> bool:
    return isinstance(value, str) and bool(BASE58_RE.fullmatch(value))


def _rate_allowed(client_ip: str) -> bool:
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_S
    with rate_lock:
        global global_rate_events
        global_rate_events = [t for t in global_rate_events if t >= cutoff]
        events = [t for t in rate_events.get(client_ip, []) if t >= cutoff]
        if len(events) >= RATE_LIMIT_PER_IP or len(global_rate_events) >= GLOBAL_RATE_LIMIT:
            rate_events[client_ip] = events
            return False
        events.append(now)
        global_rate_events.append(now)
        rate_events[client_ip] = events
    return True


def _run_demand_job(job: dict[str, str]) -> None:
    target_sig = job["target_sig"]
    payment_sig = job["payment_sig"]
    try:
        print(f"[DEMAND] Fetching logs for signature: {target_sig}")
        target_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                target_sig,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
            ]
        }
        tr = requests.post(target_rpc_url, json=target_payload, timeout=15)
        tr.raise_for_status()
        t_res = tr.json().get("result")
        if not t_res:
            print(f"[DEMAND] Target transaction {target_sig} not found")
            _mark_payment_status(payment_sig, "target-not-found")
            return

        logs = t_res.get("meta", {}).get("logMessages") or []
        program_ids = []
        for ln in logs:
            if ln.startswith("Program ") and " invoke " in ln:
                parts = ln.split()
                if len(parts) >= 2:
                    program_ids.append(parts[1])
        program_ids = sorted(set(program_ids))

        ev = LogEvent(
            signature=target_sig,
            logs=logs,
            program_ids=program_ids,
            slot=t_res.get("slot")
        )

        import asyncio
        pipeline = run.make_real_pipeline(skip_image=False)
        if pipeline is None:
            print("[DEMAND] Running in log-only mode (no API tokens)")
            asyncio.run(run._log_only_pipeline(ev))
        else:
            print(f"[DEMAND] Running live pipeline for custom signature {target_sig}")
            asyncio.run(pipeline(ev))
        _mark_payment_status(payment_sig, "done")
    except Exception as e:
        _mark_payment_status(payment_sig, "failed")
        print(f"[DEMAND-ERROR] Custom brief run failed: {e!r}")


def _demand_worker() -> None:
    while True:
        job = demand_jobs.get()
        try:
            _run_demand_job(job)
        finally:
            demand_jobs.task_done()


def start_demand_workers() -> None:
    global workers_started
    if workers_started:
        return
    workers_started = True
    for idx in range(max(1, ONDEMAND_WORKERS)):
        threading.Thread(
            target=_demand_worker,
            name=f"onchainbrief-demand-{idx + 1}",
            daemon=True,
        ).start()


def verify_and_trigger_brief(
    target_sig: str, payment_sig: str, payer_wallet: str = ""
) -> tuple[bool, str, int]:
    """Verify a payment and enqueue one bounded on-demand brief job.

    Returns (ok, message, http_status). The payment is only consumed in the
    ledger once it is successfully enqueued: if the queue is full after
    reservation the reservation is released so the user can retry the same
    paid tx."""
    if not agent_payment_wallet:
        return False, "Paid brief requests are disabled: payment wallet is not configured", 503
    if not _validate_signature(target_sig):
        return False, "Invalid target Solana transaction signature", 400
    if not _validate_signature(payment_sig):
        return False, "Invalid payment Solana transaction signature", 400
    if payer_wallet and not _validate_signature(payer_wallet):
        return False, "Invalid payer wallet", 400
    # Fail fast before spending an RPC call on a payment we can't service. The
    # definitive guard against consuming the payment is the release-on-Full
    # below; this only avoids reserve→release churn in the common full case.
    if demand_jobs.full():
        return False, "Brief queue is full; try again later", 503
    try:
        # 1. Fetch payment transaction from Solana RPC
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                payment_sig,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
            ]
        }
        r = requests.post(payment_rpc_url, json=payload, timeout=15)
        r.raise_for_status()
        res = r.json().get("result")

        if not res:
            return False, "Payment transaction not found on-chain", 400

        if res.get("meta", {}).get("err") is not None:
            return False, "Payment transaction failed on-chain", 400

        block_time = res.get("blockTime")
        if block_time and time.time() - int(block_time) > PAYMENT_MAX_AGE_S:
            return False, "Payment transaction is too old", 400

        # Check balance changes for recipient
        account_keys = res["transaction"]["message"]["accountKeys"]
        actual_payer = _extract_payer(account_keys)
        if payer_wallet and actual_payer and payer_wallet != actual_payer:
            return False, "Payment signer does not match connected wallet", 400

        recipient_index = -1
        for idx, key in enumerate(account_keys):
            key_str = _extract_key(key)
            if key_str == agent_payment_wallet:
                recipient_index = idx
                break

        if recipient_index == -1:
            return False, f"Recipient wallet {agent_payment_wallet} not involved in transaction", 400

        pre_balance = res["meta"]["preBalances"][recipient_index]
        post_balance = res["meta"]["postBalances"][recipient_index]
        diff = post_balance - pre_balance

        if diff < PAYMENT_LAMPORTS:
            return False, f"Insufficient payment. Expected {PAYMENT_LAMPORTS} lamports, recipient got {diff} lamports", 400

        memo = _parse_payment_memo(_extract_memo_text(res))
        if memo.get("app") != "onchainbrief":
            return False, "Payment memo app mismatch", 400
        if memo.get("target_sig") != target_sig:
            return False, "Payment memo target does not match request", 400
        nonce = str(memo.get("nonce") or "")
        if not nonce or len(nonce) > 128:
            return False, "Payment memo nonce is missing or invalid", 400

        ok, msg = _reserve_payment(payment_sig, target_sig, actual_payer or payer_wallet, nonce)
        if not ok:
            # Replay of an already-used payment/nonce — a conflict, not a retry.
            return False, msg, 409

    except Exception as e:
        return False, f"Failed to verify payment transaction: {e!r}", 502

    try:
        demand_jobs.put_nowait({
            "target_sig": target_sig,
            "payment_sig": payment_sig,
            "payer_wallet": payer_wallet,
        })
    except queue.Full:
        # Release the reservation so the same paid tx can be retried once the
        # queue drains — otherwise the payment is consumed but never serviced.
        _release_payment(payment_sig, nonce)
        return False, "Brief queue is full; try again later", 503
    return True, "Payment verified. Brief analysis queued.", 200


def _validate_operator_config(data: dict) -> tuple[dict | None, str]:
    """Coerce/validate an operator panel payload into a watch-config patch.

    Only known keys survive; unknown keys are ignored. Returns (patch, "") on
    success or (None, reason) on rejection. min_usd must be a finite, in-range
    number; the three switches are coerced to bool.
    """
    if not isinstance(data, dict):
        return None, "Body must be a JSON object"
    patch: dict = {}
    if "min_usd" in data:
        try:
            m = float(data["min_usd"])
        except (TypeError, ValueError):
            return None, "min_usd must be a number"
        if m != m or m < 0 or m > OPERATOR_MAX_MIN_USD:  # NaN or out of range
            return None, "min_usd out of range"
        patch["min_usd"] = m
    for key in ("deploys", "governance", "security"):
        if key in data:
            patch[key] = bool(data[key])
    if not patch:
        return None, "No recognized settings in body"
    return patch, ""


def _write_watch_config(patch: dict) -> dict:
    """Merge `patch` into the on-disk watch-config override and persist it
    atomically. Returns the full override dict now on disk. The agent reads this
    file live (filter.watch_config), so the change applies without a restart."""
    path = pathlib.Path(WATCH_CONFIG_PATH)
    current: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, ValueError):
            current = {}
    current.update(patch)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return current


def _operator_page_html(token: str, cfg: dict) -> str:
    """Minimal local control panel. Served only to a request that already proved
    the token, so embedding it for the POST is safe. No external assets."""
    min_usd = int(cfg.get("min_usd") or 0)
    checked = {k: "checked" if cfg.get(k, True) else "" for k in ("deploys", "governance", "security")}
    tok = json.dumps(token)  # safe JS string literal
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>OnchainBrief — Operator</title><style>"
        "body{font-family:system-ui,sans-serif;background:#030712;color:#f3f4f6;"
        "max-width:480px;margin:40px auto;padding:0 20px}"
        "h1{font-size:20px}label{display:block;margin:16px 0 6px;font-size:13px;color:#9ca3af}"
        "input[type=number]{width:100%;padding:10px;border-radius:8px;border:1px solid #333;"
        "background:#0b1220;color:#fff;font-size:15px}"
        ".row{display:flex;align-items:center;gap:8px;margin:10px 0}"
        "button{margin-top:20px;width:100%;padding:12px;border:0;border-radius:8px;"
        "background:#8b5cf6;color:#fff;font-weight:600;font-size:14px;cursor:pointer}"
        "#msg{margin-top:14px;font-size:13px;min-height:18px}"
        ".muted{color:#9ca3af;font-size:12px;line-height:1.5}</style></head><body>"
        "<h1>Agent watch settings</h1>"
        "<p class=muted>Local operator panel. Changes are written to the agent's "
        "live config and take effect on the next event — no restart.</p>"
        "<label for=min_usd>Minimum swap/transfer size (USD)</label>"
        f"<input type=number id=min_usd min=0 step=1 value='{min_usd}'>"
        f"<div class=row><input type=checkbox id=deploys {checked['deploys']}>"
        "<label for=deploys style='margin:0'>Watch program deploys / upgrades</label></div>"
        f"<div class=row><input type=checkbox id=governance {checked['governance']}>"
        "<label for=governance style='margin:0'>Watch governance actions</label></div>"
        f"<div class=row><input type=checkbox id=security {checked['security']}>"
        "<label for=security style='margin:0'>Watch security / admin actions</label></div>"
        "<button id=save>Save</button><div id=msg></div>"
        "<script>"
        f"const TOKEN={tok};"
        "document.getElementById('save').onclick=async()=>{"
        "const body={min_usd:parseFloat(document.getElementById('min_usd').value)||0,"
        "deploys:document.getElementById('deploys').checked,"
        "governance:document.getElementById('governance').checked,"
        "security:document.getElementById('security').checked};"
        "const m=document.getElementById('msg');m.textContent='Saving…';m.style.color='#9ca3af';"
        "try{const r=await fetch('/operator/config',{method:'POST',"
        "headers:{'Content-Type':'application/json','X-Operator-Token':TOKEN},"
        "body:JSON.stringify(body)});const j=await r.json();"
        "if(r.ok){m.textContent='Saved. Live config updated.';m.style.color='#34d399';}"
        "else{m.textContent=j.error||'Error';m.style.color='#f87171';}"
        "}catch(e){m.textContent=String(e);m.style.color='#f87171';}};"
        "</script></body></html>"
    )


class CustomFeedHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=()",
        )
        self.send_header("Content-Security-Policy", build_csp())
        # Transport + isolation hardening. HSTS is safe behind Railway's TLS
        # termination; COOP severs any cross-origin opener relationship. Both
        # are additive — they do not change how the self-contained page renders.
        self.send_header(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        # proof.json is the audit trail and index.html is a live feed; ask
        # clients to revalidate so a verifier never reads a stale card set.
        served = urlsplit(self.path).path
        if served.endswith((".json", ".html")) or served.endswith("/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _check_operator_token(self) -> bool:
        """Constant-time match of the supplied token against OPERATOR_TOKEN.
        Accepts an X-Operator-Token header (POST) or ?token= query (GET page)."""
        if not OPERATOR_TOKEN:
            return False
        supplied = self.headers.get("X-Operator-Token", "")
        if not supplied:
            qs = parse_qs(urlsplit(self.path).query)
            supplied = (qs.get("token") or [""])[0]
        return hmac.compare_digest(supplied, OPERATOR_TOKEN)

    def do_GET(self):
        path = urlsplit(self.path).path
        if self.path == "/api/sse":
            self.handle_sse()
        elif path == "/operator":
            self.handle_operator_page()
        else:
            super().do_GET()

    def do_OPTIONS(self):
        if self.path == "/api/request-brief":
            origin = self.headers.get("Origin", "")
            if ALLOWED_ORIGIN and origin != ALLOWED_ORIGIN:
                self.send_error(403, "Origin not allowed")
                return
            self.send_response(204)
            self._send_cors_headers()
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        if self.path == "/api/request-brief":
            self.handle_request_brief()
        elif self.path == "/operator/config":
            self.handle_operator_config()
        else:
            self.send_error(404, "Not Found")

    def handle_operator_page(self):
        # Disabled feature is invisible: 404, not 401, so a public deploy reveals
        # nothing about an operator panel that the host never enabled.
        if not OPERATOR_TOKEN:
            self.send_error(404, "Not Found")
            return
        if not self._check_operator_token():
            self.send_error(401, "Unauthorized")
            return
        page = _operator_page_html(OPERATOR_TOKEN, watch_config()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def handle_operator_config(self):
        if not OPERATOR_TOKEN:
            self.send_error(404, "Not Found")
            return
        if not self._check_operator_token():
            self.send_json_error("Unauthorized", 401)
            return
        client_ip = self.client_address[0] if self.client_address else "unknown"
        if not _rate_allowed(client_ip):
            self.send_json_error("Rate limit exceeded", 429)
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_json_error("Content-Length is required", 411)
            return
        if content_length <= 0:
            self.send_json_error("Request body is required", 400)
            return
        if content_length > MAX_REQUEST_BYTES:
            self.send_json_error("Request body too large", 413)
            return
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.send_json_error("Invalid JSON body", 400)
            return
        patch, reason = _validate_operator_config(data)
        if patch is None:
            self.send_json_error(reason, 400)
            return
        try:
            _write_watch_config(patch)
        except OSError as e:
            self.send_json_error(f"Could not persist config: {e}", 500)
            return
        # Echo the effective config (env + the override just written).
        self.send_json_response({"status": "ok", "config": watch_config()})

    def _send_cors_headers(self):
        if ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)

    def handle_sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self._send_cors_headers()
        self.end_headers()

        q = queue.Queue(maxsize=10)
        with sse_lock:
            sse_clients.add(q)

        try:
            self.wfile.write(b": ok\n\n")
            self.wfile.flush()

            while True:
                try:
                    msg = q.get(timeout=15.0)
                    self.wfile.write(f"data: {msg}\n\n".encode('utf-8'))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with sse_lock:
                sse_clients.discard(q)

    def handle_request_brief(self):
        try:
            origin = self.headers.get("Origin", "")
            if ALLOWED_ORIGIN and origin != ALLOWED_ORIGIN:
                self.send_json_error("Origin not allowed", 403)
                return
            client_ip = self.client_address[0] if self.client_address else "unknown"
            if not _rate_allowed(client_ip):
                self.send_json_error("Rate limit exceeded", 429)
                return
            try:
                content_length = int(self.headers.get('Content-Length', ""))
            except ValueError:
                self.send_json_error("Content-Length is required", 411)
                return
            if content_length <= 0:
                self.send_json_error("Request body is required", 400)
                return
            if content_length > MAX_REQUEST_BYTES:
                self.send_json_error("Request body too large", 413)
                return
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode('utf-8'))
            except (UnicodeDecodeError, ValueError):
                self.send_json_error("Invalid JSON body", 400)
                return
            
            target_sig = data.get("signature")
            payment_sig = data.get("payment_signature")
            payer_wallet = data.get("payer_wallet", "")
            
            if not target_sig or not payment_sig:
                self.send_json_error("Missing target signature or payment_signature", 400)
                return
            
            ok, msg, status = verify_and_trigger_brief(target_sig, payment_sig, payer_wallet)
            if ok:
                self.send_json_response({"status": "processing", "message": msg})
            else:
                self.send_json_error(msg, status)
        except Exception as e:
            print(f"[REQUEST-ERROR] {e!r}")
            self.send_json_error("Internal server error", 500)

    def send_json_response(self, data, status=200):
        response_bytes = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response_bytes)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(response_bytes)

    def send_json_error(self, message, status=400):
        self.send_json_response({"error": message}, status)


def main() -> int:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    index = SITE_DIR / "index.html"
    if not index.exists():
        index.write_text(
            "<!doctype html><html><body>No briefs yet — run the build "
            "step (scripts/build_site.py).</body></html>",
            encoding="utf-8",
        )

    if agent_payment_wallet:
        start_demand_workers()
        print(
            f"[SERVER] Paid requests enabled: queue={ONDEMAND_QUEUE_MAX}, "
            f"workers={max(1, ONDEMAND_WORKERS)}"
        )
        # The payment ledger is a local JSON file: durable across restarts but
        # not across an ephemeral redeploy, and single-instance only (multiple
        # replicas could replay a payment). Warn loudly when it sits under the
        # ephemeral .state dir so a scaled deploy doesn't silently lose replay
        # protection. Point PAYMENTS_LEDGER_PATH at a persistent volume to fix.
        if ".state" in PAYMENTS_LEDGER_PATH.parts:
            print(
                f"[SERVER] WARNING: payment ledger at {PAYMENTS_LEDGER_PATH} is "
                "under .state (ephemeral on Railway, single-instance only). "
                "Set PAYMENTS_LEDGER_PATH to a persistent volume for durable, "
                "multi-instance-safe replay protection."
            )
    else:
        print("[SERVER] Paid requests disabled: AGENT_PAYMENT_WALLET is not configured")

    if OPERATOR_TOKEN:
        print(f"[SERVER] Operator panel ENABLED at /operator (token-gated, writes {WATCH_CONFIG_PATH})")
    else:
        print("[SERVER] Operator panel disabled: set OPERATOR_TOKEN to enable /operator")

    # 1. Background Solana log watcher if WATCH_PROGRAM_IDS is defined
    watch_programs = os.getenv("WATCH_PROGRAM_IDS", "")
    if watch_programs:
        def start_watcher():
            print(f"[SERVER] Starting background Solana log watcher for: {watch_programs}")
            import asyncio
            try:
                watcher = run.build_watcher()
                asyncio.run(watcher.run())
            except Exception as e:
                print(f"[SERVER-WATCHER-ERROR] Watcher crashed: {e!r}")
        
        threading.Thread(target=start_watcher, daemon=True).start()

    # 2. Multi-threaded HTTP Server
    class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True

    handler = lambda *args, **kwargs: CustomFeedHandler(
        *args, directory=str(SITE_DIR), **kwargs
    )

    with ThreadingServer((HOST, PORT), handler) as httpd:
        print(f"OnchainBrief feed on http://{HOST}:{PORT} (dir={SITE_DIR})")
        print(f"[SERVER] Payment recipient wallet: {agent_payment_wallet or 'disabled'}")
        print(f"[SERVER] Payment verification: {PAYMENT_CLUSTER} ({payment_rpc_url})")
        print(f"[SERVER] Analyzed-tx fetch RPC: {target_rpc_url}")
        with contextlib.suppress(KeyboardInterrupt):
            httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
