"""Offline unit tests for the pure logic (no network, no funds, no ACE)."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

import io  # noqa: E402

from PIL import Image  # noqa: E402

from onchainbrief import throttle  # noqa: E402
from onchainbrief.compose import (  # noqa: E402
    _strip_period,
    compose_card,
    write_brief_markdown,
)
from onchainbrief.filter import _heuristic  # noqa: E402
from onchainbrief.watcher import LogEvent, SolanaLogWatcher, redact  # noqa: E402


def test_redact_strips_url_secrets():
    assert "***" in redact("wss://x.helius-rpc.com/?api-key=SECRET123")
    assert "SECRET123" not in redact("https://x.com/v2/SECRET123/rpc")


def test_parse_ignores_non_notifications():
    assert SolanaLogWatcher._parse({"result": 1, "id": 1}) is None


def test_parse_extracts_event_and_skips_errors():
    base = {
        "method": "logsNotification",
        "params": {
            "result": {
                "context": {"slot": 42},
                "value": {
                    "signature": "sigABC",
                    "err": None,
                    "logs": ["Program P1 invoke [1]", "Program P2 invoke [2]"],
                },
            }
        },
    }
    ev = SolanaLogWatcher._parse(base)
    assert ev and ev.signature == "sigABC" and ev.slot == 42
    assert ev.program_ids == ["P1", "P2"]

    base["params"]["result"]["value"]["err"] = {"InstructionError": []}
    assert SolanaLogWatcher._parse(base) is None


def test_heuristic_filter():
    noisy = LogEvent("s", ["l"] * 8, ["A", "B"])
    quiet = LogEvent("s", ["l"], ["A"])
    assert _heuristic(noisy) is True
    assert _heuristic(quiet) is False


def test_strip_period():
    assert _strip_period("hello.") == "hello"
    assert _strip_period("hello") == "hello"


def test_compose_card_and_brief(tmp_path):
    buf = io.BytesIO()
    Image.new("RGB", (320, 180), (20, 40, 80)).save(buf, "PNG")
    card = compose_card(
        buf.getvalue(), "Big Move.", "A notable flow on a new program",
        "5xSIGabcdefghijklmnop1234567890", tmp_path / "c.png",
    )
    assert card.exists() and card.stat().st_size > 2000
    md = write_brief_markdown(
        "Big Move.", "Narrative body.", ["https://x.test"],
        "5xSIGabcdefghijklmnop1234567890", card, tmp_path / "b.md",
    )
    text = md.read_text(encoding="utf-8")
    assert "# Big Move" in text and text.count("# Big Move.") == 0
    assert "solscan.io/tx/5xSIG" in text


def test_pipeline_calls_three_services_and_writes_artifacts(tmp_path):
    from onchainbrief.pipeline import SerpResult, run_brief

    calls = []

    class MockClient:
        def serp(self, q):
            calls.append("serp")
            return SerpResult("ctx summary", ["https://src.test/a"])

        def chat(self, p):
            calls.append("chat")
            return "First sentence. Second. Third."

        def image(self, p):
            calls.append("image")
            buf = io.BytesIO()
            Image.new("RGB", (320, 180), (10, 30, 60)).save(buf, "PNG")
            return buf.getvalue()

    ev = LogEvent("5xSIGabcdefghijklmnop1234567890", ["l"] * 8, ["ProgAAA"])
    art = run_brief(ev, MockClient(), tmp_path)

    assert calls == ["serp", "chat", "image"]  # 3 distinct services, in order
    assert len(set(calls)) == 3
    assert art.card_path.exists() and art.card_path.stat().st_size > 2000
    assert art.brief_path.exists()
    body = art.brief_path.read_text(encoding="utf-8")
    assert "src.test/a" in body and "solscan.io/tx/5xSIG" in body


def test_feed_renders_briefs(tmp_path):
    from onchainbrief.feed import build_feed

    buf = io.BytesIO()
    Image.new("RGB", (320, 180), (10, 30, 60)).save(buf, "PNG")
    img = buf.getvalue()
    for i, sig in enumerate(["AAAsig111", "BBBsig222"]):
        c = compose_card(img, f"Move {i}", "sub line", sig,
                          tmp_path / f"{sig}.png")
        write_brief_markdown(f"Move {i}", f"Narrative {i}.",
                             [f"https://src{i}.test/x"], sig, c,
                             tmp_path / f"{sig}.md")

    out = build_feed(tmp_path, tmp_path / "site" / "index.html")
    h = out.read_text(encoding="utf-8")
    assert h.startswith("<!doctype html>") and h.rstrip().endswith("</html>")
    assert "Move 0" in h and "Move 1" in h
    assert "AAAsig111.png" in h and "src1.test/x" in h
    assert "solscan.io/tx/BBBsig222" in h


def test_feed_empty_dir_is_valid(tmp_path):
    from onchainbrief.feed import build_feed

    h = build_feed(tmp_path, tmp_path / "i.html").read_text(encoding="utf-8")
    assert "No briefs yet" in h and h.rstrip().endswith("</html>")


def test_throttle_caps_per_day(tmp_path, monkeypatch):
    monkeypatch.setattr(throttle, "STATE_PATH", tmp_path / "t.json")
    monkeypatch.setattr(throttle, "DAILY_CAP", 2)
    assert throttle.try_consume() is True
    assert throttle.try_consume() is True
    assert throttle.try_consume() is False
    assert throttle.remaining_today() == 0


def test_build_site_script_renders(tmp_path):
    import build_site

    buf = io.BytesIO()
    Image.new("RGB", (320, 180), (10, 30, 60)).save(buf, "PNG")
    sig = "ZZZsig999abcdefghij1234567890"
    c = compose_card(buf.getvalue(), "Site Move", "sub", sig,
                      tmp_path / f"{sig}.png")
    write_brief_markdown("Site Move", "Narrative body.",
                         ["https://src.test/x"], sig, c,
                         tmp_path / f"{sig}.md")

    out = tmp_path / "site" / "index.html"
    rc = build_site.main(["--briefs", str(tmp_path), "--out", str(out)])
    assert rc == 0 and out.exists()
    h = out.read_text(encoding="utf-8")
    assert h.startswith("<!doctype html>") and h.rstrip().endswith("</html>")
    assert "Site Move" in h and "solscan.io/tx/ZZZsig999" in h


def test_build_site_missing_briefs_dir_is_cold_start_safe(tmp_path):
    import build_site

    briefs = tmp_path / "nope"
    out = tmp_path / "site" / "index.html"
    rc = build_site.main(["--briefs", str(briefs), "--out", str(out)])
    assert rc == 0 and briefs.is_dir()  # auto-created, no crash
    h = out.read_text(encoding="utf-8")
    assert "No briefs yet" in h and h.rstrip().endswith("</html>")


# --- AceBriefClient adapter (doc-derived shapes, offline) -------------------


def _png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (320, 180), (12, 28, 52)).save(buf, "PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self, body=None, status=200, content=b""):
        self._b = body
        self.status_code = status
        self.content = content
        self.text = str(body)

    def json(self):
        if self._b is None:
            raise ValueError("no json")
        return self._b


class _FakeTransport:
    """Returns the *documented* ACE response shapes (SerpAPI/OpenAIAPI/NanoBanana)."""

    def __init__(self, *, image_async=False):
        self.image_async = image_async
        self._polls = 0
        self.calls = []

    def call(self, service, payload):
        self.calls.append(service)
        if service == "serp":
            return _Resp({"organic": [
                {"title": "Acme", "link": "https://acme.test/a",
                 "snippet": "Acme launched a new program on Solana."},
                {"title": "News", "link": "https://news.test/b",
                 "snippet": "Sizable flow observed."},
            ]})
        if service == "chat":
            return _Resp({"choices": [
                {"message": {"role": "assistant",
                             "content": "Lead sentence. Second. Third."}}
            ]})
        if service == "image":
            if self.image_async:
                return _Resp({"success": True, "task_id": "tsk_123",
                              "data": []})
            return _Resp({"success": True, "task_id": "tsk_123",
                          "data": [{"image_url": "https://cdn.test/i.png"}]})
        raise AssertionError(service)

    def image_task(self, task_id):
        assert task_id == "tsk_123"
        self._polls += 1
        if self._polls < 2:
            return _Resp({"success": True, "state": "processing", "data": []})
        return _Resp({"success": True, "state": "completed",
                      "data": [{"image_url": "https://cdn.test/i.png"}]})


def test_ace_brief_client_parses_documented_shapes():
    from onchainbrief.ace_brief_client import AceBriefClient
    from onchainbrief.pipeline import SerpResult

    png = _png_bytes()
    c = AceBriefClient(_FakeTransport(),
                       image_fetch=lambda u: _Resp(content=png))
    sr = c.serp("solana acme")
    assert isinstance(sr, SerpResult)
    assert "Acme launched" in sr.summary and "Sizable flow" in sr.summary
    assert sr.sources == ["https://acme.test/a", "https://news.test/b"]
    assert c.chat("hi").startswith("Lead sentence")
    assert c.image("a card") == png


def test_ace_brief_client_serp_defensive_shapes():
    """A bare-list body and a dict-shaped 'organic' must not crash serp()."""
    from onchainbrief.ace_brief_client import AceBriefClient
    from onchainbrief.pipeline import SerpResult

    class BareList:
        def call(self, s, p):
            return _Resp([{"snippet": "bare array context",
                           "link": "https://bare.test/x"}])

        def image_task(self, t):  # pragma: no cover
            raise AssertionError

    sr = AceBriefClient(BareList()).serp("q")
    assert isinstance(sr, SerpResult)
    assert "bare array context" in sr.summary
    assert sr.sources == ["https://bare.test/x"]

    class DictOrganic:
        def call(self, s, p):
            return _Resp({"organic": {"unexpected": "dict"}})

        def image_task(self, t):  # pragma: no cover
            raise AssertionError

    sr2 = AceBriefClient(DictOrganic()).serp("q")  # must not raise
    assert isinstance(sr2, SerpResult)
    assert sr2.sources == []


def test_ace_brief_client_image_async_poll():
    from onchainbrief.ace_brief_client import AceBriefClient

    png = _png_bytes()
    t = _FakeTransport(image_async=True)
    c = AceBriefClient(t, image_fetch=lambda u: _Resp(content=png),
                       sleep=lambda _s: None, poll_interval=0, poll_attempts=5)
    assert c.image("a card") == png
    assert t._polls >= 2  # polled until the task completed


def test_ace_brief_client_raises_on_api_error():
    from onchainbrief.ace_brief_client import AceBriefClient, AceBriefError

    class Bad:
        def call(self, s, p):
            return _Resp({"success": False,
                          "error": {"code": "x", "message": "bad token"}})

        def image_task(self, t):  # pragma: no cover
            raise AssertionError

    try:
        AceBriefClient(Bad()).chat("hi")
        raise AssertionError("expected AceBriefError")
    except AceBriefError as e:
        assert "bad token" in str(e)


def test_handle_survives_pipeline_error_and_once_stops(tmp_path, monkeypatch):
    """A failed ACE call must NOT crash the watcher; --once must stop it."""
    import asyncio

    from onchainbrief import run, throttle

    monkeypatch.setattr(throttle, "STATE_PATH", tmp_path / "t.json")
    monkeypatch.setattr(throttle, "DAILY_CAP", 5)

    ev = LogEvent("sigX", ["l"] * 8, ["A", "B"])  # heuristic-worthy

    class FakeWatcher:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    async def boom(_ev):
        raise RuntimeError("ACE token rejected")

    w = FakeWatcher()
    # Must NOT raise out of handle (resilience boundary), and --once stops.
    asyncio.run(run.handle(ev, boom, once=True, watcher=w, spends=True))
    assert w.stopped is True
    assert throttle.remaining_today() == 4  # slot stays consumed on failure

    # spends=False (log-only) must not touch the throttle at all.
    seen = []

    async def ok(e):
        seen.append(e.signature)

    asyncio.run(run.handle(ev, ok, once=False, watcher=None, spends=False))
    assert seen == ["sigX"]
    assert throttle.remaining_today() == 4  # unchanged — log-only never spends


def test_end_to_end_agent_flow_offline(tmp_path, monkeypatch):
    """watcher._parse -> filter -> throttle -> AceBriefClient -> feed.

    The whole agent, proven with zero funds / network / token.
    """
    from onchainbrief import throttle
    from onchainbrief.ace_brief_client import AceBriefClient
    from onchainbrief.feed import build_feed
    from onchainbrief.filter import is_brief_worthy
    from onchainbrief.pipeline import run_brief
    from onchainbrief.watcher import SolanaLogWatcher

    monkeypatch.setattr(throttle, "STATE_PATH", tmp_path / "t.json")
    monkeypatch.setattr(throttle, "DAILY_CAP", 1)

    notif = {
        "method": "logsNotification",
        "params": {"result": {
            "context": {"slot": 7},
            "value": {
                "signature": "5xSIGe2ee2eebeefbeefbeef1234567890",
                "err": None,
                "logs": [f"Program P{i} invoke [1]" for i in range(1, 4)]
                        + ["log"] * 6,
            },
        }},
    }
    ev = SolanaLogWatcher._parse(notif)
    assert ev and is_brief_worthy(ev) is True      # heuristic: notable
    assert throttle.try_consume() is True           # budget available
    assert throttle.try_consume() is False          # cap respected

    png = _png_bytes()
    client = AceBriefClient(_FakeTransport(),
                            image_fetch=lambda u: _Resp(content=png))
    briefs = tmp_path / "briefs"
    art = run_brief(ev, client, briefs)
    assert art.card_path.exists() and art.brief_path.exists()

    site = build_feed(briefs, tmp_path / "site" / "index.html")
    h = site.read_text(encoding="utf-8")
    assert h.startswith("<!doctype html>") and h.rstrip().endswith("</html>")
    assert "solscan.io/tx/5xSIGe2ee2ee" in h
    assert "acme.test/a" in h or "news.test/b" in h


# --- P4a: on-chain attestation (Memo program) -------------------------------


def test_attest_payload_and_memo_ix(tmp_path):
    """Payload schema + sha256 determinism + memo ix targets Memo program."""
    import hashlib
    import json as _json

    from onchainbrief.attest import (
        APP_ID,
        CAP_ID,
        MEMO_PROGRAM_ID,
        PAYLOAD_VERSION,
        _artifact_sha256,
        _build_memo_ix,
        _payload_for,
    )

    a = tmp_path / "a.bin"; a.write_bytes(b"hello")
    b = tmp_path / "b.bin"; b.write_bytes(b"world")
    h1 = _artifact_sha256([a, b])
    h2 = _artifact_sha256([a, b])
    assert h1 == h2  # deterministic
    assert h1 == hashlib.sha256(b"helloworld").hexdigest()

    ts = "2026-05-20T12:00:00+00:00"
    p1 = _payload_for("TRIG", h1, ts=ts)
    p2 = _payload_for("TRIG", h1, ts=ts)
    assert p1 == p2  # same inputs => same bytes
    obj = _json.loads(p1)
    assert obj == {
        "v": PAYLOAD_VERSION, "app": APP_ID, "cap": CAP_ID,
        "trigger": "TRIG", "sha256": h1, "ts": ts,
    }
    # Sorted-keys + no whitespace => byte-stable for verifiers.
    assert b" " not in p1 and p1.startswith(b'{"app":')

    ix = _build_memo_ix(p1)
    assert ix.program_id == MEMO_PROGRAM_ID
    assert bytes(ix.data) == p1
    assert list(ix.accounts) == []  # memo needs no extra signers


def test_attestor_send_is_mocked_and_failure_returns_none(tmp_path):
    """Inject a fake RPC client; assert send returns sig and failures => None."""
    from solders.hash import Hash
    from solders.keypair import Keypair

    from onchainbrief.attest import Attestor

    kp = Keypair()
    kp_path = tmp_path / "kp.json"
    kp_path.write_text(kp.to_json(), encoding="utf-8")

    class _Val:
        def __init__(self, v): self.value = v

    sent_signatures = []

    class FakeClient:
        def get_latest_blockhash(self):
            return _Val(type("BH", (), {"blockhash": Hash.default()})())

        def send_transaction(self, tx):
            sent_signatures.append(tx)
            return _Val("FAKESIG" + "x" * 60)

        def confirm_transaction(self, sig):
            return _Val(True)

    art = tmp_path / "a.bin"; art.write_bytes(b"payload")
    a = Attestor("http://stub", kp_path, cluster="devnet", client=FakeClient())
    r = a.attest("TRIGSIG", [art])
    assert r is not None
    assert r.tx_sig.startswith("FAKESIG")
    assert r.cluster == "devnet"
    assert len(sent_signatures) == 1

    # Resilience: any send error => None, never raises (must not block publish).
    class BoomClient:
        def get_latest_blockhash(self):
            return _Val(type("BH", (), {"blockhash": Hash.default()})())

        def send_transaction(self, tx):
            raise RuntimeError("RPC offline")

        def confirm_transaction(self, sig):  # pragma: no cover
            raise AssertionError

    a2 = Attestor("http://stub", kp_path, cluster="devnet", client=BoomClient())
    assert a2.attest("TRIGSIG", [art]) is None


def test_pipeline_threads_attestation_into_artifacts(tmp_path):
    """With attestor: card/brief/feed carry the receipt; without: unchanged."""
    from onchainbrief.attest import Attestation
    from onchainbrief.feed import build_feed
    from onchainbrief.pipeline import SerpResult, run_brief

    class MockClient:
        def serp(self, q):
            return SerpResult("ctx", ["https://src.test/a"])
        def chat(self, p):
            return "Lead. Second. Third."
        def image(self, p):
            buf = io.BytesIO()
            Image.new("RGB", (320, 180), (10, 30, 60)).save(buf, "PNG")
            return buf.getvalue()

    class MockAttestor:
        def __init__(self):
            self.calls = []
        def attest(self, trigger_sig, paths):
            self.calls.append((trigger_sig, [str(p) for p in paths]))
            return Attestation(
                tx_sig="ATTESTSIG" + "z" * 60,
                cluster="devnet",
                payload_sha256="deadbeef",
            )

    ev = LogEvent("5xSIGabcdefghijklmnop1234567890", ["l"] * 8, ["ProgZ"])
    att = MockAttestor()
    art = run_brief(ev, MockClient(), tmp_path, attestor=att)

    assert art.attestation is not None
    assert att.calls and att.calls[0][0] == ev.signature  # called once, right sig
    md = art.brief_path.read_text(encoding="utf-8")
    assert "Attestation tx `ATTESTSIGzz" in md
    assert "?cluster=devnet" in md

    site = build_feed(tmp_path, tmp_path / "site" / "index.html")
    h = site.read_text(encoding="utf-8")
    assert "SAP Attestation (devnet)" in h
    assert "ATTESTSIG" in h
    assert "?cluster=devnet" in h

    # Regression: attestor=None preserves the old shape.
    art2 = run_brief(ev, MockClient(), tmp_path / "no_attest")
    assert art2.attestation is None
    md2 = art2.brief_path.read_text(encoding="utf-8")
    assert "Attestation tx" not in md2


def test_attestor_returns_none_on_bad_keypair_path(tmp_path):
    """Bad keypair path must surface as ctor failure (caller handles)."""
    from onchainbrief.attest import Attestor

    try:
        Attestor("http://stub", tmp_path / "missing.json", cluster="devnet",
                 client=object())
        raise AssertionError("expected error")
    except (FileNotFoundError, OSError):
        pass


# --- x402 EIP-712 signing — regression lock against silent encoding drift ----


def test_x402_sign_x_payment_is_byte_stable(monkeypatch):
    """Lock the EIP-712 typed-data + X-PAYMENT base64 against regression.

    Inputs are pinned: known throwaway private key (NEVER used in prod),
    known accept block (Base USDC chat-tier amount), monkeypatched time and
    nonce. Any change to the typed-data structure, JSON separators, or base64
    encoding will flip the expected string. Catches the class of bug that
    would have made all 9 live settlements fail silently on the next run.
    """
    from eth_account import Account

    from onchainbrief import x402_client

    # 0x11..11 — deterministic, not a real key.
    acct = Account.from_key("0x" + "11" * 32)
    accept = {
        "network": "base",
        "scheme": "exact",
        "payTo": "0x4F0E2D3477a1B94CF33d16E442CEe4733dadCeE7",
        "maxAmountRequired": "95215",
        "maxTimeoutSeconds": 600,
        "extra": {
            "name": "USD Coin",
            "version": "2",
            "chainId": 8453,
            "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        },
    }

    class _FixedTime:
        @staticmethod
        def time():
            return 1700000000

    monkeypatch.setattr(x402_client, "time", _FixedTime)
    monkeypatch.setattr(
        x402_client.secrets, "token_bytes", lambda _n: bytes.fromhex("aa" * 32)
    )

    expected = (
        "eyJ4NDAyVmVyc2lvbiI6Miwic2NoZW1lIjoiZXhhY3QiLCJuZXR3b3JrIjoiYmFzZSIsInBheWxvYWQ"
        "iOnsic2lnbmF0dXJlIjoiMHhhNjA0YWUwMDJlYTUzNzIyY2YwNGI3ZGNhYmI2NGZiZmFmMWE5Zjcw"
        "NDEwYTFjNzA2OWZiNzQ4NjM0NjVkYzM4NTQzZTM1MzdlYWIxYzlhZWFlMjlhY2FiMjIzOWI2ZDIy"
        "NmIwOWFmNDU1OGJkYjE5MGQ1ZWZmNjJjZjExNmVjYjFjIiwiYXV0aG9yaXphdGlvbiI6eyJmcm9t"
        "IjoiMHgxOUU3RTM3NkU3QzIxM0I3RTdlN2U0NmNjNzBBNWREMDg2REFmZjJBIiwidG8iOiIweDRG"
        "MEUyRDM0NzdhMUI5NENGMzNkMTZFNDQyQ0VlNDczM2RhZENlRTciLCJ2YWx1ZSI6Ijk1MjE1Iiwi"
        "dmFsaWRBZnRlciI6IjAiLCJ2YWxpZEJlZm9yZSI6IjE3MDAwMDA2MDAiLCJub25jZSI6IjB4YWFh"
        "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFh"
        "YWFhYSJ9fX0="
    )
    out = x402_client._sign_x_payment(acct, accept)
    assert out == expected


def test_ace_brief_client_image_async_poll_transient_failures():
    from onchainbrief.ace_brief_client import AceBriefClient

    png = _png_bytes()
    calls = []

    class FailingTransport:
        def call(self, service, payload):
            return _Resp({"success": True, "task_id": "tsk_123", "data": []})

        def image_task(self, task_id):
            calls.append(len(calls))
            if len(calls) == 1:
                raise RuntimeError("Temporary network failure")
            if len(calls) == 2:
                return _Resp("Internal Server Error", status=500)
            return _Resp({"success": True, "state": "completed",
                          "data": [{"image_url": "https://cdn.test/i.png"}]})

    c = AceBriefClient(
        FailingTransport(),
        image_fetch=lambda u: _Resp(content=png),
        sleep=lambda _s: None,
        poll_interval=0,
        poll_attempts=5,
    )
    assert c.image("a card") == png
    assert len(calls) == 3  # Two failures, then success


def test_throttle_preserves_history(tmp_path, monkeypatch):
    import json
    from onchainbrief import throttle

    monkeypatch.setattr(throttle, "STATE_PATH", tmp_path / "t.json")
    monkeypatch.setattr(throttle, "DAILY_CAP", 5)

    # Pre-populate history for a different day
    state_file = tmp_path / "t.json"
    state_file.write_text(json.dumps({"2026-05-22": 3}))

    # Consume for today
    assert throttle.try_consume() is True
    
    # Read file and verify BOTH days exist
    data = json.loads(state_file.read_text())
    assert data["2026-05-22"] == 3
    today_str = throttle._today()
    assert data[today_str] == 1


def test_watcher_handshake_success_and_failure(monkeypatch):
    import asyncio
    import json
    from onchainbrief.watcher import SolanaLogWatcher, LogEvent

    # Mock websocket object
    class MockWS:
        def __init__(self, responses):
            self.responses = responses
            self.sent = []
            self.closed = False

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            if not self.responses:
                # Keep blocking to simulate no more messages
                await asyncio.sleep(10)
                return "{}"
            return self.responses.pop(0)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.responses:
                raise StopAsyncIteration
            return self.responses.pop(0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.closed = True

    # Test successful handshake
    responses = [
        json.dumps({"jsonrpc": "2.0", "result": 1001, "id": 1}),
        json.dumps({"jsonrpc": "2.0", "result": 1002, "id": 2}),
        json.dumps({
            "method": "logsNotification",
            "params": {
                "result": {
                    "context": {"slot": 123},
                    "value": {
                        "signature": "sig123",
                        "err": None,
                        "logs": ["Program P1 invoke [1]"],
                    }
                }
            }
        })
    ]

    ws_mock = MockWS(responses)
    
    # Mock websockets.connect
    def mock_connect(*args, **kwargs):
        return ws_mock

    import websockets
    monkeypatch.setattr(websockets, "connect", mock_connect)

    received = []
    async def dummy_handler(ev: LogEvent):
        received.append(ev)
        watcher.stop()  # Stop the loop

    watcher = SolanaLogWatcher("wss://test", ["P1", "P2"], dummy_handler)
    
    # Run the watcher. It should connect, verify subscription, receive notification, and stop.
    asyncio.run(watcher.run())

    assert len(ws_mock.sent) == 2  # Sent 2 subscription frames
    assert len(received) == 1
    assert received[0].signature == "sig123"

    # Test failing handshake (rejection error)
    bad_responses = [
        json.dumps({"jsonrpc": "2.0", "error": {"code": -32602, "message": "Invalid params"}, "id": 1}),
    ]
    ws_mock_bad = MockWS(bad_responses)
    monkeypatch.setattr(websockets, "connect", lambda *a, **kw: ws_mock_bad)

    watcher_bad = SolanaLogWatcher("wss://test", ["P1"], dummy_handler)
    
    # Run watcher_bad. It should attempt connection, fail handshake, raise an exception,
    # print reconnect message, and loop. Since we don't want it to loop forever in test,
    # we can stop it inside an exception or mock the sleep.
    sleep_calls = []
    async def mock_sleep(s):
        sleep_calls.append(s)
        watcher_bad.stop()  # Stop loop on reconnect sleep

    monkeypatch.setattr(asyncio, "sleep", mock_sleep)
    asyncio.run(watcher_bad.run())
    
    # Checked that it tried to connect, failed, slept, and stopped.
    assert len(sleep_calls) == 1


def test_compose_card_headline_and_subline_overflow(tmp_path):
    buf = io.BytesIO()
    Image.new("RGB", (320, 180), (20, 40, 80)).save(buf, "PNG")
    
    # Very long headline + subline that wraps to > 2 lines
    headline = "This is a super extremely long headline that should exceed 24 characters and trigger scale down"
    subline = "This is a very long subline sentence that wraps to multiple lines because it has lots of words and characters and therefore should trigger truncation with ellipsis on the second line."
    
    card = compose_card(
        buf.getvalue(), headline, subline,
        "5xSIGabcdefghijklmnop1234567890", tmp_path / "c_overflow.png"
    )
    assert card.exists()


def test_x402_client_retry(monkeypatch):
    import time
    from onchainbrief.x402_client import X402Client, X402Error
    
    class MockSession:
        def __init__(self):
            self.headers = {}
            self.post_calls = 0

        def post(self, url, json=None, timeout=None, headers=None):
            self.post_calls += 1
            if self.post_calls == 1:
                # First attempt, return 402 challenge
                challenge = {
                    "accepts": [
                        {
                            "network": "base",
                            "scheme": "exact",
                            "payTo": "0x4F0E2D3477a1B94CF33d16E442CEe4733dadCeE7",
                            "maxAmountRequired": "1000",
                            "extra": {
                                "name": "USD Coin",
                                "version": "2",
                                "chainId": "8453",
                                "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                            }
                        }
                    ]
                }
                return _Resp(challenge, status=402)
            elif self.post_calls == 2:
                # Second attempt, simulate 502 gateway error on payment retry
                return _Resp({}, status=502)
            elif self.post_calls == 3:
                # Third attempt (first retry iteration), return 402 challenge
                challenge = {
                    "accepts": [
                        {
                            "network": "base",
                            "scheme": "exact",
                            "payTo": "0x4F0E2D3477a1B94CF33d16E442CEe4733dadCeE7",
                            "maxAmountRequired": "1000",
                            "extra": {
                                "name": "USD Coin",
                                "version": "2",
                                "chainId": "8453",
                                "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                            }
                        }
                    ]
                }
                return _Resp(challenge, status=402)
            elif self.post_calls == 4:
                # Fourth attempt (payment retry), return 200 OK
                return _Resp({"success": True}, status=200)
            return _Resp({}, status=500)

    # Disable sleeping to make test run fast
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    
    from onchainbrief.config import Settings
    settings = Settings(
        ace_api_token="t",
        ace_api_token_image="t2",
        ace_x402_private_key="0x" + "aa" * 32,
        solana_rpc_url="",
        solana_ws_url="",
        solana_keypair_path="",
        attest_cluster="devnet"
    )
    
    client = X402Client(settings=settings)
    mock_s = MockSession()
    monkeypatch.setattr(client, "_s", mock_s)

    res = client.call("chat", {"test": 123})
    assert res.status_code == 200
    assert mock_s.post_calls == 4

