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


def test_feed_has_no_inline_event_handlers():
    """Frontend hardening (T8): every interactive element wires behaviour
    through a single delegated listener via data-action, not inline onclick/
    on* attributes. Exercise all branches: verify button (attested item),
    lightbox image, filter tabs, and the request form (wallet set)."""
    from onchainbrief.feed import _render, FeedItem

    item = FeedItem(
        headline="Whale move",
        card="ABCsig.png",
        narrative="A large transfer landed.",
        signature="ABCsig111",
        sources=["https://src.test/x"],
        category="Volume",
        attest_sig="ATTESTsig222",
        attest_cluster="mainnet-beta",
        md_name="ABCsig.md",
    )
    h = _render([item], agent_payment_wallet="Wa11etPubKey1111111111111111111111111111111")

    # No inline event-handler attributes anywhere in the rendered page.
    assert "onclick" not in h
    assert "onerror" not in h
    assert "onload" not in h
    assert "javascript:" not in h
    # Behaviour is wired through the delegated handler + data-action hooks.
    assert "addEventListener('click'" in h
    assert "data-action" in h
    assert 'data-action="verify"' in h and 'data-sig="ABCsig111"' in h
    assert 'data-action="lightbox"' in h
    assert "data-action='filter'" in h
    assert "data-action='connect-wallet'" in h
    assert "data-action='submit-request'" in h
    assert "data-action='close-verify'" in h
    assert "data-action='close-lightbox'" in h
    # The modal content shields its children so an inside-click cannot close it.
    assert "data-action='noop'" in h


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


def test_pipeline_attests_pristine_bytes_via_sidecar(tmp_path):
    """T1: the on-chain memo must bind the EXACT public bytes. run_brief hashes
    card+brief, then writes the receipt to a <stem>.attest.json sidecar WITHOUT
    re-composing the files — so a verifier re-hashing the published png+md
    reproduces the memo's sha256. attestor=None leaves the old shape & no
    sidecar."""
    import hashlib
    import json as json_module
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

    def _sha(paths):
        h = hashlib.sha256()
        for p in paths:
            h.update(pathlib.Path(p).read_bytes())
        return h.hexdigest()

    class MockAttestor:
        def __init__(self):
            self.calls = []
            self.hashed_sha = None
        def attest(self, trigger_sig, paths):
            # Hash exactly what we were handed — this is what the memo binds.
            self.hashed_sha = _sha(paths)
            self.calls.append((trigger_sig, [str(p) for p in paths]))
            return Attestation(
                tx_sig="ATTESTSIG" + "z" * 60,
                cluster="devnet",
                memo_payload_sha256="deadbeef",
                artifact_sha256=self.hashed_sha,
            )

    ev = LogEvent("5xSIGabcdefghijklmnop1234567890", ["l"] * 8, ["ProgZ"])
    att = MockAttestor()
    art = run_brief(ev, MockClient(), tmp_path, attestor=att)

    assert art.attestation is not None
    assert att.calls and att.calls[0][0] == ev.signature  # called once, right sig

    # The crux of T1: published files are byte-for-byte what was hashed.
    assert _sha([art.card_path, art.brief_path]) == att.hashed_sha

    # Pristine: no attestation line baked into the brief.
    md = art.brief_path.read_text(encoding="utf-8")
    assert "Attestation tx" not in md

    # Receipt lives in the sidecar instead.
    sidecar = pathlib.Path(art.brief_path).with_suffix(".attest.json")
    assert sidecar.exists()
    data = json_module.loads(sidecar.read_text(encoding="utf-8"))
    assert data["tx_sig"].startswith("ATTESTSIG")
    assert data["cluster"] == "devnet"
    # Sidecar names both hashes unambiguously, and artifact_sha256 reproduces
    # the published bytes (card+brief) — what an on-chain verifier recomputes.
    assert data["memo_payload_sha256"] == "deadbeef"
    assert data["artifact_sha256"] == _sha([art.card_path, art.brief_path])

    # Feed reads the sidecar → still renders the verify button + sig.
    site = build_feed(tmp_path, tmp_path / "site" / "index.html")
    h = site.read_text(encoding="utf-8")
    assert "Verify On-Chain (devnet)" in h
    assert "ATTESTSIG" in h

    # Regression: attestor=None preserves the old shape, no sidecar written.
    art2 = run_brief(ev, MockClient(), tmp_path / "no_attest")
    assert art2.attestation is None
    md2 = art2.brief_path.read_text(encoding="utf-8")
    assert "Attestation tx" not in md2
    assert not pathlib.Path(art2.brief_path).with_suffix(".attest.json").exists()


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


def _policy_accept(**overrides):
    """A 402 accept block that passes the default signing policy; override one
    field per rejection case."""
    accept = {
        "scheme": "exact",
        "network": "base",
        "payTo": "0x4F0E2D3477a1B94CF33d16E442CEe4733dadCeE7",
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "maxAmountRequired": "95215",
        "extra": {
            "name": "USD Coin",
            "version": "2",
            "chainId": 8453,
            "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        },
    }
    accept.update(overrides)
    return accept


def test_x402_validate_accept_enforces_spending_policy(monkeypatch):
    """T3: a signed EIP-3009 authorization is a bearer instrument, so the client
    must refuse to sign a challenge that breaks the spending policy — oversized
    amount, wrong chain/asset/network, or a payTo outside an allowlist."""
    import pytest

    from onchainbrief import x402_client

    # Happy path: a real chat-tier challenge signs without complaint.
    x402_client._validate_accept(_policy_accept())

    # Oversized amount (2 USDC > 1 USDC default cap).
    with pytest.raises(x402_client.X402Error, match="exceeds cap"):
        x402_client._validate_accept(_policy_accept(maxAmountRequired="2000000"))

    # Non-positive / malformed amount.
    with pytest.raises(x402_client.X402Error):
        x402_client._validate_accept(_policy_accept(maxAmountRequired="0"))

    # Wrong chain (Ethereum mainnet instead of Base).
    with pytest.raises(x402_client.X402Error, match="chainId"):
        x402_client._validate_accept(
            _policy_accept(extra={"chainId": 1, "name": "x", "version": "2",
                                  "verifyingContract": "0x0"})
        )

    # Wrong asset.
    with pytest.raises(x402_client.X402Error, match="asset"):
        x402_client._validate_accept(
            _policy_accept(asset="0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        )

    # Wrong network / scheme.
    with pytest.raises(x402_client.X402Error, match="network"):
        x402_client._validate_accept(_policy_accept(network="solana"))
    with pytest.raises(x402_client.X402Error, match="scheme"):
        x402_client._validate_accept(_policy_accept(scheme="upto"))

    # payTo allowlist: when set, only listed recipients pass.
    monkeypatch.setattr(
        x402_client, "X402_ALLOWED_PAY_TO",
        "0x1111111111111111111111111111111111111111",
    )
    with pytest.raises(x402_client.X402Error, match="allowlist"):
        x402_client._validate_accept(_policy_accept())
    # The default facilitator is rejected; an allowlisted one passes.
    x402_client._validate_accept(
        _policy_accept(payTo="0x1111111111111111111111111111111111111111")
    )


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
        solana_tx_rpc_url="",
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


def test_sap_tool_discovery_success(monkeypatch):
    from onchainbrief import discovery
    from solders.pubkey import Pubkey
    import hashlib

    call_log = []
    
    cap_id_bytes = b"onchainbrief:web-context-research"
    cap_hash = hashlib.sha256(cap_id_bytes).digest()
    agent_pk = Pubkey.new_unique()
    
    cap_data = bytearray()
    cap_data.extend(bytes.fromhex("80426314855a676f")) # disc
    cap_data.append(254) # bump
    cap_data.extend(len(cap_id_bytes).to_bytes(4, "little")) # len
    cap_data.extend(cap_id_bytes) # string
    cap_data.extend(cap_hash) # hash
    cap_data.extend((1).to_bytes(4, "little")) # vec length 1
    cap_data.extend(bytes(agent_pk)) # agent Pubkey
    cap_data.append(0) # total pages
    cap_data.extend((1779320538).to_bytes(8, "little")) # last updated
    
    agent_data = bytearray()
    agent_data.extend(bytes.fromhex("f177458ce9097032")) # disc
    agent_data.append(253) # bump
    agent_data.append(1) # version
    agent_data.extend(bytes(Pubkey.new_unique())) # wallet
    agent_data.extend((12).to_bytes(4, "little"))
    agent_data.extend(b"OnchainBrief") # name
    agent_data.extend((4).to_bytes(4, "little"))
    agent_data.extend(b"desc") # desc
    agent_data.append(0) # agent_id option None
    agent_data.append(0) # agent_uri option None
    agent_data.append(1) # x402 option Some
    endpoint_bytes = b"https://facilitator.acedata.cloud/.well-known/x402"
    agent_data.extend(len(endpoint_bytes).to_bytes(4, "little"))
    agent_data.extend(endpoint_bytes)
    agent_data.append(1) # is_active

    def mock_get_account_data(rpc_url, pda):
        call_log.append(pda)
        if len(call_log) == 1:
            return bytes(cap_data)
        else:
            return bytes(agent_data)

    monkeypatch.setattr(discovery, "get_account_data", mock_get_account_data)

    base, endpoints = discovery.discover_ace_endpoints("http://mock_rpc")
    assert base == "https://api.acedata.cloud"
    assert len(call_log) == 2


def test_discovery_banner_redacts_rpc_key(monkeypatch, capsys):
    """The startup banner must never print a raw api_key (it lands in CI logs)."""
    from onchainbrief import discovery

    def offline(rpc_url, pda):
        raise RuntimeError("offline")

    monkeypatch.setattr(discovery, "get_account_data", offline)

    # Fake token deliberately WITHOUT a live secret-key prefix: tests/ ships in
    # the public allowlist and scrub_public_repo.py tripwires on that prefix.
    secret_url = "https://staging.oobeprotocol.ai:8080/rpc?api_key=FAKEKEY_deadbeef0123"
    discovery.discover_ace_endpoints(secret_url)
    out = capsys.readouterr().out
    assert "FAKEKEY_deadbeef0123" not in out
    assert "api_key=***" in out


def test_enrich_event_routes_decode_to_tx_rpc(monkeypatch):
    """The getTransaction round-trip must go to tx_rpc_url (keyless archival),
    not the primary rpc_url (OOBE free tier can't serve getTransaction)."""
    import types
    from onchainbrief import txfacts

    seen = {}

    def fake_fetch(sig, rpc_url, *, timeout=20.0):
        seen["sig"] = sig
        seen["rpc_url"] = rpc_url
        return None  # miss → leaves facts None, skips the price fetch (no network)

    monkeypatch.setattr(txfacts, "fetch_transaction", fake_fetch)

    ev = types.SimpleNamespace(signature="SIG_ABC", facts=None)
    txfacts.enrich_event(ev, "https://oobe.example/rpc?api_key=sk", tx_rpc_url="https://decode.example")
    assert seen["sig"] == "SIG_ABC"
    assert seen["rpc_url"] == "https://decode.example"

    # Fallback: no tx_rpc_url → the decode uses the primary rpc_url.
    seen.clear()
    txfacts.enrich_event(ev, "https://primary.example", tx_rpc_url=None)
    assert seen["rpc_url"] == "https://primary.example"


def test_extract_facts_governance_by_program_id():
    """A SPL-Governance tx with no token/SOL move and no English log hints is
    still classified governance (program-id beats the activity fallback)."""
    from onchainbrief.txfacts import extract_facts

    gov = "GovER5Lthms3bLBqWub97yVrMmEogzX7xNjdXpPPCVZw"
    result = {
        "transaction": {"message": {"accountKeys": [
            {"pubkey": "Voter1111111111111111111111111111111111111", "signer": True, "writable": True},
            {"pubkey": gov, "signer": False, "writable": False},
        ]}},
        "meta": {
            "fee": 5000,
            # payer delta is exactly the fee → net SOL move is zero
            "preBalances": [1000000, 0], "postBalances": [995000, 0],
            "preTokenBalances": [], "postTokenBalances": [],
            # deliberately NO "vote"/"proposal"/etc. words — only program-id can classify
            "logMessages": [
                f"Program {gov} invoke [1]",
                "Program log: Instruction: 5",
                f"Program {gov} success",
            ],
        },
    }
    facts = extract_facts(result)
    assert facts.kind == "governance"


def test_extract_facts_security_op_fills_security_category():
    """An SPL-Token setAuthority tx that moves no value is classified security
    and routes to the Security feed tab (which the facts-first path otherwise
    never fills)."""
    from onchainbrief.txfacts import SPL_TOKEN_PROGRAM, extract_facts
    from onchainbrief.filter import categorize_event
    from onchainbrief.watcher import LogEvent

    result = {
        "transaction": {"message": {
            "accountKeys": [
                {"pubkey": "Admin111111111111111111111111111111111111", "signer": True, "writable": True},
                {"pubkey": SPL_TOKEN_PROGRAM, "signer": False, "writable": False},
            ],
            "instructions": [
                {"programId": SPL_TOKEN_PROGRAM,
                 "parsed": {"type": "setAuthority", "info": {"authorityType": "mintTokens"}}},
            ],
        }},
        "meta": {
            "fee": 5000,
            "preBalances": [1000000, 0], "postBalances": [995000, 0],  # net SOL = fee only
            "preTokenBalances": [], "postTokenBalances": [],  # no token move
            "logMessages": ["Program log: Instruction: SetAuthority"],
        },
    }
    facts = extract_facts(result)
    assert facts.kind == "security"
    assert "authority" in facts.summary.lower()

    ev = LogEvent("5xSECsetauth1234567890", [], [SPL_TOKEN_PROGRAM], facts=facts)
    assert categorize_event(ev) == "Security"


def test_pipeline_skip_image(tmp_path):
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
            return b""

    ev = LogEvent("5xSIGabcdefghijklmnop1234567890", ["l"] * 8, ["ProgAAA"])
    art = run_brief(ev, MockClient(), tmp_path, skip_image=True)

    # serp and chat are called, but image is skipped!
    assert calls == ["serp", "chat"]
    assert art.card_path is None
    assert art.brief_path.exists()
    
    body = art.brief_path.read_text(encoding="utf-8")
    assert "![card]" not in body  # no card tag in the markdown file
    assert "src.test/a" in body
    assert "solscan.io/tx/5xSIG" in body


def test_categorize_event():
    from onchainbrief.filter import categorize_event
    from onchainbrief.watcher import LogEvent

    # 1. Security Alert / Admin Action
    ev_sec = LogEvent("sig_sec", ["Program log: Instruction: SetAuthority", "Program log: set authority"], ["program_sec"])
    assert categorize_event(ev_sec) == "Security"

    # 2. Deployment
    ev_dep = LogEvent("sig_dep", ["Program log: Instruction: Initialize", "Program log: upgrade program"], ["BPFLoaderUpgradeab1e11111111111111111111111"])
    assert categorize_event(ev_dep) == "Deployment"

    # 3. Volume / DeFi
    ev_vol = LogEvent("sig_vol", ["Program log: Instruction: Swap", "Program log: heavy flow"], ["jup6l81tlcpaaygrnwexj3xxlwbmh4nd54xctxsqsbf"])
    assert categorize_event(ev_vol) == "Volume"

    # 4. Governance
    ev_gov = LogEvent("sig_gov", ["Program log: Instruction: Vote", "Program log: proposal council"], ["program_gov"])
    assert categorize_event(ev_gov) == "Governance"

    # 5. Activity (default)
    ev_act = LogEvent("sig_act", ["Program log: Instruction: Transfer", "Routine log message"], ["program_other"])
    assert categorize_event(ev_act) == "Activity"


def test_txfacts_sol_transfer():
    from onchainbrief.txfacts import extract_facts

    result = {
        "blockTime": 1716000000,
        "meta": {
            "err": None, "fee": 5000,
            "preBalances": [10_000_000_000, 1_000_000_000, 0],
            "postBalances": [4_999_995_000, 6_000_000_000, 0],
            "logMessages": [
                "Program 11111111111111111111111111111111 invoke [1]",
                "Program log: Instruction: Transfer",
            ],
        },
        "transaction": {"message": {"accountKeys": [
            {"pubkey": "SenderWalletAddr1111111111111111111111111"},
            {"pubkey": "RecvWalletAddr22222222222222222222222222"},
            {"pubkey": "11111111111111111111111111111111"},
        ]}},
    }
    f = extract_facts(result, sol_price_usd=150.0)
    assert f.kind == "whale_transfer"
    assert f.asset == "SOL"
    assert abs(f.amount_native - 5.0) < 0.01      # 5 SOL net moved
    assert abs(f.amount_usd - 750.0) < 1.0        # 5 * 150
    assert f.from_addr.startswith("SenderWallet")
    assert f.to_addr.startswith("RecvWallet")
    assert "SOL" in f.summary and "→" in f.summary


def test_txfacts_usdc_transfer():
    from onchainbrief.txfacts import extract_facts

    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    result = {
        "meta": {
            "err": None, "fee": 5000,
            "preBalances": [1_000_000, 2_000_000],
            "postBalances": [995_000, 2_000_000],
            "preTokenBalances": [
                {"owner": "Whale1", "mint": usdc,
                 "uiTokenAmount": {"uiAmount": 1_500_000.0}},
                {"owner": "Dest1", "mint": usdc,
                 "uiTokenAmount": {"uiAmount": 0.0}},
            ],
            "postTokenBalances": [
                {"owner": "Whale1", "mint": usdc,
                 "uiTokenAmount": {"uiAmount": 0.0}},
                {"owner": "Dest1", "mint": usdc,
                 "uiTokenAmount": {"uiAmount": 1_500_000.0}},
            ],
            "logMessages": ["Program log: Instruction: TransferChecked"],
        },
        "transaction": {"message": {"accountKeys": ["Whale1", "Dest1"]}},
    }
    f = extract_facts(result)
    assert f.kind == "token_transfer"
    assert f.asset == "USDC"
    assert abs(f.amount_native - 1_500_000.0) < 1
    assert abs(f.amount_usd - 1_500_000.0) < 1      # stablecoin: usd == amount
    assert f.from_addr == "Whale1" and f.to_addr == "Dest1"
    assert "1.50M USDC" in f.summary


def test_txfacts_deploy_and_swap_and_empty():
    from onchainbrief.txfacts import extract_facts

    deploy = {
        "meta": {"err": None, "fee": 5000, "preBalances": [1], "postBalances": [1],
                 "logMessages": [
                     "Program BPFLoaderUpgradeab1e11111111111111111111111 invoke [1]"]},
        "transaction": {"message": {"accountKeys": ["acc"]}},
    }
    assert extract_facts(deploy).kind == "deploy"

    jup = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    swap = {
        "meta": {
            "err": None, "fee": 5000, "preBalances": [1, 1], "postBalances": [1, 1],
            "preTokenBalances": [{"owner": "T", "mint": usdc,
                                  "uiTokenAmount": {"uiAmount": 0.0}}],
            "postTokenBalances": [{"owner": "T", "mint": usdc,
                                   "uiTokenAmount": {"uiAmount": 50000.0}}],
            "logMessages": [f"Program {jup} invoke [1]",
                            "Program log: Instruction: Route"],
        },
        "transaction": {"message": {"accountKeys": ["T", "x"]}},
    }
    fs = extract_facts(swap)
    assert fs.kind == "swap"
    assert fs.program_name == "Jupiter Aggregator"
    assert "via Jupiter Aggregator" in fs.summary

    # malformed / empty input must not raise
    assert extract_facts(None).kind == "activity"
    assert extract_facts({}).kind == "activity"


def test_deploy_via_cpi_names_upgraded_program_not_executor():
    """F2: a Squads VaultTransactionExecute that CPIs the loader to upgrade
    another program must be classified by the loader log - 'upgraded', naming
    the upgraded program - not as the executor 'deploying' itself."""
    from onchainbrief.txfacts import extract_facts

    squads = "SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf"
    loader = "BPFLoaderUpgradeab1e11111111111111111111111"
    target = "B1REA6TxnuQTcXVK94cEZgEv3e9nq8f6VMEjjryyLBHT"
    result = {
        "meta": {"err": None, "fee": 5000, "preBalances": [1], "postBalances": [1],
                 "logMessages": [
                     f"Program {squads} invoke [1]",
                     "Program log: Instruction: VaultTransactionExecute",
                     f"Program {loader} invoke [2]",
                     f"Upgraded program {target}",
                     f"Program {loader} success",
                     f"Program {squads} success"]},
        "transaction": {"message": {"accountKeys": ["acc"],
                                    "instructions": [{"programId": squads}]}},
    }
    f = extract_facts(result)
    assert f.kind == "deploy"
    assert f.deploy_verb == "upgraded"
    assert f.program_id == target  # the upgraded program, not the Squads executor
    assert target in f.summary and "upgraded" in f.summary

    # The loader appearing only as an inner CPI without a deploy/upgrade log is
    # NOT a deploy (it is incidental to whatever the top-level program did).
    incidental = {
        "meta": {"err": None, "fee": 5000, "preBalances": [1], "postBalances": [1],
                 "logMessages": [
                     f"Program {squads} invoke [1]",
                     f"Program {loader} invoke [2]",
                     f"Program {loader} success",
                     f"Program {squads} success"]},
        "transaction": {"message": {"accountKeys": ["acc"],
                                    "instructions": [{"programId": squads}]}},
    }
    assert extract_facts(incidental).kind != "deploy"


def test_txfacts_infra_program_never_headline():
    """ComputeBudget/System are invoked by ~every tx — they must never become
    the headline entity, else every brief reads 'via Compute Budget'."""
    from onchainbrief.txfacts import extract_facts

    cb = "ComputeBudget111111111111111111111111111111"
    jup = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
    tok = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    # ComputeBudget invoked first, Jupiter second → Jupiter is the entity.
    swap = {
        "meta": {
            "err": None, "fee": 5000, "preBalances": [1, 1], "postBalances": [1, 1],
            "preTokenBalances": [{"owner": "T", "mint": usdc,
                                  "uiTokenAmount": {"uiAmount": 0.0}}],
            "postTokenBalances": [{"owner": "T", "mint": usdc,
                                   "uiTokenAmount": {"uiAmount": 50000.0}}],
            "logMessages": [f"Program {cb} invoke [1]",
                            f"Program {jup} invoke [1]"],
        },
        "transaction": {"message": {"accountKeys": ["T", "x"]}},
    }
    assert extract_facts(swap).program_name == "Jupiter Aggregator"

    # Only infra programs present → no fabricated entity, no 'via ...' tail.
    plain = {
        "meta": {
            "err": None, "fee": 5000, "preBalances": [1], "postBalances": [1],
            "preTokenBalances": [{"owner": "A", "mint": usdc,
                                  "uiTokenAmount": {"uiAmount": 0.0}}],
            "postTokenBalances": [{"owner": "A", "mint": usdc,
                                   "uiTokenAmount": {"uiAmount": 1000.0}}],
            "logMessages": [f"Program {cb} invoke [1]", f"Program {tok} invoke [1]"],
        },
        "transaction": {"message": {"accountKeys": ["A"]}},
    }
    f = extract_facts(plain)
    assert f.program_name == ""
    assert "via" not in f.summary


def test_governance_brief_names_protocol_not_computebudget():
    """Regression: a real SPL-Governance tx is [ComputeBudget, GovER…]; without
    Governance in KNOWN_PROGRAMS, program_name stayed empty and _facts_block fell
    back to programs[0]=ComputeBudget, so the LLM wrote 'GOVERNANCE EVENT IN
    COMPUTE BUDGET'. The headline entity must be SPL Governance, with the action
    captured, and ComputeBudget must never reach the facts handed to the LLM."""
    from onchainbrief.txfacts import extract_facts
    from onchainbrief.pipeline import _facts_block, _query_for
    from onchainbrief.watcher import LogEvent

    cb = "ComputeBudget111111111111111111111111111111"
    gov = "GovER5Lthms3bLBqWub97yVrMmEogzX7xNjdXpPPCVZw"
    result = {
        "transaction": {"message": {"accountKeys": [
            {"pubkey": "Signer11111111111111111111111111111111111", "signer": True, "writable": True},
            {"pubkey": gov, "signer": False, "writable": False},
        ]}},
        "meta": {
            "fee": 5000,
            "preBalances": [1000000, 0], "postBalances": [995000, 0],  # net SOL = fee only
            "preTokenBalances": [], "postTokenBalances": [],
            "logMessages": [
                f"Program {cb} invoke [1]",
                f"Program {cb} success",
                f"Program {gov} invoke [1]",
                "Program log: GOVERNANCE-INSTRUCTION: SignOffProposal",
                f"Program {gov} success",
            ],
        },
    }
    f = extract_facts(result)
    assert f.kind == "governance"
    assert f.program_name == "SPL Governance"
    assert f.program_id == gov
    assert f.instruction == "SignOffProposal"
    assert "SPL Governance" in f.summary and "SignOffProposal" in f.summary
    assert "Compute Budget" not in f.summary and cb not in f.summary

    ev = LogEvent("5xGOVsignoff1234567890", [], [gov], facts=f)
    block = _facts_block(ev)
    assert "SPL Governance" in block
    assert "ComputeBudget" not in block and cb not in block
    assert "SPL Governance" in _query_for(ev)


def test_facts_block_never_emits_infra_program():
    """Defense-in-depth: even when no program is recognised, _facts_block must
    not surface an infra program id - it omits the Program line instead."""
    from onchainbrief.txfacts import extract_facts
    from onchainbrief.pipeline import _facts_block
    from onchainbrief.watcher import LogEvent

    cb = "ComputeBudget111111111111111111111111111111"
    sysprog = "11111111111111111111111111111111"
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    # Pure infra tx that still moved a stablecoin (token_transfer) but exposes no
    # real protocol — program_name and program_id are both empty.
    result = {
        "meta": {
            "err": None, "fee": 5000, "preBalances": [1], "postBalances": [1],
            "preTokenBalances": [{"owner": "A", "mint": usdc, "uiTokenAmount": {"uiAmount": 0.0}}],
            "postTokenBalances": [{"owner": "A", "mint": usdc, "uiTokenAmount": {"uiAmount": 1000.0}}],
            "logMessages": [f"Program {cb} invoke [1]", f"Program {sysprog} invoke [1]"],
        },
        "transaction": {"message": {"accountKeys": ["A"]}},
    }
    f = extract_facts(result)
    assert f.program_name == "" and f.program_id == ""
    block = _facts_block(LogEvent("5xINFRAonly1234567890", [], [cb], facts=f))
    assert "ComputeBudget" not in block and "- Program:" not in block


def test_no_facts_fallback_skips_infra_program():
    """Regression for the offline-demo leak: when a tx is NOT decoded (facts is
    None) the deterministic fallbacks must not name an infra program. The watcher
    emits program_ids alphabetically sorted, so System '111…'/ComputeBudget sort
    to [0] — the fallback must pick the first NON-infra id (here Jupiter)."""
    from onchainbrief.pipeline import _headline_for, _facts_block, _query_for
    from onchainbrief.watcher import LogEvent

    cb = "ComputeBudget111111111111111111111111111111"
    sysprog = "11111111111111111111111111111111"
    jup = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
    # As the watcher delivers it: sorted(set(...)) → System, ComputeBudget, Jupiter
    program_ids = sorted({cb, sysprog, jup})
    assert program_ids[0] == sysprog  # the trap: infra sorts first

    ev = LogEvent("5xNODECODE1234567890", [], program_ids, facts=None)
    assert _headline_for(ev) == f"{jup[:8]} on-chain move"
    assert "ComputeB" not in _headline_for(ev) and sysprog not in _headline_for(ev)
    block = _facts_block(ev)
    assert jup in block and cb not in block and sysprog not in block
    assert jup in _query_for(ev)

    # All-infra, undecoded → no fabricated entity anywhere.
    ev2 = LogEvent("5xINFRANODECODE12345", [], sorted({cb, sysprog}), facts=None)
    assert _headline_for(ev2) == "ON-CHAIN ACTIVITY"
    assert "- Program:" not in _facts_block(ev2)


def test_serve_feed_endpoints(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    import requests

    recipient = "E" * 44
    target_sig = "B" * 88
    payment_sig = "D" * 88
    failed_payment_sig = "F" * 88
    payer_wallet = "C" * 44
    
    monkeypatch.setenv("AGENT_PAYMENT_WALLET", recipient)
    monkeypatch.setenv("ATTEST_CLUSTER", "devnet")
    monkeypatch.setenv("ATTEST_RPC_URL", "https://api.devnet.solana.com")
    monkeypatch.setenv("PAYMENTS_LEDGER_PATH", str(tmp_path / "payments.json"))
    
    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
        
    import serve_feed
    monkeypatch.setattr(serve_feed, "agent_payment_wallet", recipient)
    monkeypatch.setattr(serve_feed, "payment_rpc_url", "https://api.devnet.solana.com")
    monkeypatch.setattr(serve_feed, "target_rpc_url", "https://api.devnet.solana.com")
    monkeypatch.setattr(serve_feed, "PAYMENTS_LEDGER_PATH", tmp_path / "payments.json")
    
    from serve_feed import verify_and_trigger_brief
    
    mock_rpc_calls = []
    
    class MockResponse:
        def __init__(self, json_data, status_code=200):
            self.json_data = json_data
            self.status_code = status_code
        def json(self):
            return self.json_data
        def raise_for_status(self):
            pass

    def mock_post(url, json=None, **kwargs):
        mock_rpc_calls.append(json)
        method = json.get("method")
        if method == "getTransaction":
            sig = json["params"][0]
            if sig == payment_sig:
                return MockResponse({
                    "result": {
                        "meta": {
                            "err": None,
                            "preBalances": [10000000, 5000000],
                            "postBalances": [9000000, 6000000]
                        },
                        "transaction": {
                            "message": {
                                "accountKeys": [
                                    {"pubkey": payer_wallet, "signer": True},
                                    {"pubkey": recipient, "signer": False},
                                ],
                                "instructions": [
                                    {
                                        "programId": serve_feed.MEMO_PROGRAM_ID,
                                        "parsed": json_module.dumps({
                                            "app": "onchainbrief",
                                            "target_sig": target_sig,
                                            "nonce": "nonce-1",
                                        }),
                                    }
                                ],
                            }
                        }
                    }
                })
            elif sig == failed_payment_sig:
                return MockResponse({
                    "result": {
                        "meta": {
                            "err": {"InstructionError": [0, "DummyError"]}
                        }
                    }
                })
            elif sig == target_sig:
                return MockResponse({
                    "result": {
                        "slot": 42,
                        "meta": {
                            "logMessages": ["Program log: Instruction: Initialize", "Program BPFLoaderUpgradeab1e11111111111111111111111 invoke [1]"]
                        }
                    }
                })
        return MockResponse({"result": None})

    import json as json_module
    monkeypatch.setattr(requests, "post", mock_post)
    monkeypatch.setenv("AGENT_PAYMENT_WALLET", recipient)
    
    ok, msg, status = verify_and_trigger_brief(target_sig, payment_sig, payer_wallet)
    assert ok is True
    assert status == 200
    assert "Brief analysis queued" in msg

    ok_replay, msg_replay, status_replay = verify_and_trigger_brief(target_sig, payment_sig, payer_wallet)
    assert ok_replay is False
    assert status_replay == 409
    assert "already been used" in msg_replay

    ok_fail, msg_fail, status_fail = verify_and_trigger_brief(target_sig, failed_payment_sig, payer_wallet)
    assert ok_fail is False
    assert status_fail == 400
    assert "failed on-chain" in msg_fail


def test_queue_full_releases_payment_for_retry(tmp_path, monkeypatch):
    """T4: when the on-demand queue is full the verified payment must NOT be
    consumed — the reservation is released so the same paid tx can be retried
    once the queue drains. Regression for a paid request that could never be
    serviced."""
    import sys
    import json as json_module
    from pathlib import Path
    import queue as queue_module
    import requests

    recipient = "E" * 44
    target_sig = "B" * 88
    payment_sig = "D" * 88
    payer_wallet = "C" * 44

    monkeypatch.setenv("AGENT_PAYMENT_WALLET", recipient)
    monkeypatch.setenv("ATTEST_CLUSTER", "devnet")
    monkeypatch.setenv("ATTEST_RPC_URL", "https://api.devnet.solana.com")
    monkeypatch.setenv("PAYMENTS_LEDGER_PATH", str(tmp_path / "payments.json"))

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))

    import serve_feed
    monkeypatch.setattr(serve_feed, "agent_payment_wallet", recipient)
    monkeypatch.setattr(serve_feed, "payment_rpc_url", "https://api.devnet.solana.com")
    monkeypatch.setattr(serve_feed, "target_rpc_url", "https://api.devnet.solana.com")
    monkeypatch.setattr(serve_feed, "PAYMENTS_LEDGER_PATH", tmp_path / "payments.json")
    # Reports not-full at the early fast-path check, but raises on put_nowait —
    # forces the reserve→Full→release path (the actual fix), not the early
    # short-circuit, so the test proves the reservation is undone.
    class FullOnPut:
        def full(self):
            return False
        def put_nowait(self, item):
            raise queue_module.Full
    monkeypatch.setattr(serve_feed, "demand_jobs", FullOnPut())

    class MockResponse:
        def __init__(self, json_data):
            self.json_data = json_data
        def json(self):
            return self.json_data
        def raise_for_status(self):
            pass

    def mock_post(url, json=None, **kwargs):
        return MockResponse({
            "result": {
                "meta": {
                    "err": None,
                    "preBalances": [10000000, 5000000],
                    "postBalances": [9000000, 6000000],
                },
                "transaction": {
                    "message": {
                        "accountKeys": [
                            {"pubkey": payer_wallet, "signer": True},
                            {"pubkey": recipient, "signer": False},
                        ],
                        "instructions": [
                            {
                                "programId": serve_feed.MEMO_PROGRAM_ID,
                                "parsed": json_module.dumps({
                                    "app": "onchainbrief",
                                    "target_sig": target_sig,
                                    "nonce": "nonce-1",
                                }),
                            }
                        ],
                    }
                },
            }
        })

    monkeypatch.setattr(requests, "post", mock_post)

    ok, msg, status = serve_feed.verify_and_trigger_brief(
        target_sig, payment_sig, payer_wallet
    )
    assert ok is False
    assert status == 503
    assert "queue is full" in msg

    # The crux of T4: the payment + nonce were released, not consumed.
    ledger = json_module.loads(
        (tmp_path / "payments.json").read_text(encoding="utf-8")
    ) if (tmp_path / "payments.json").exists() else {"payments": {}, "nonces": {}}
    assert payment_sig not in ledger.get("payments", {})
    assert "nonce-1" not in ledger.get("nonces", {})


def test_request_amount_consistent_across_ui_and_server():
    """The on-demand price has one source (REQUEST_BRIEF_LAMPORTS). Regression
    guard against the rendered button / JS amount drifting from the lamports the
    server actually requires — a mismatch silently rejects every paid request."""
    import importlib
    from pathlib import Path

    from onchainbrief import feed
    from onchainbrief.config import REQUEST_BRIEF_LAMPORTS, lamports_to_sol_str

    sol = lamports_to_sol_str(REQUEST_BRIEF_LAMPORTS)
    html = feed._render([], agent_payment_wallet="A" * 43)
    # Button label and the JS transfer both render the same SOL figure.
    assert f"Request Brief ({sol} " in html
    assert f"sendPaymentTx(userWallet, {sol})" in html

    root = Path(__file__).resolve().parents[1]
    if str(root / "scripts") not in sys.path:
        sys.path.insert(0, str(root / "scripts"))
    serve_feed = importlib.import_module("serve_feed")
    # Server's required lamports == the single config source the UI rendered.
    assert serve_feed.PAYMENT_LAMPORTS == REQUEST_BRIEF_LAMPORTS
    assert abs(float(sol) * 1_000_000_000 - REQUEST_BRIEF_LAMPORTS) < 1


def test_token_amount_falls_back_to_string_and_raw():
    """uiAmount is null for some mints/encodings; the SPL-move detector must
    still recover the size from uiAmountString, then from the raw atomic
    amount/decimals pair, before giving up — otherwise a real transfer reads
    as zero and the move is silently dropped."""
    from onchainbrief.txfacts import _token_move

    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    # uiAmount is None but the string form carries the size.
    via_string = {
        "meta": {
            "preTokenBalances": [
                {"owner": "RECIP", "mint": usdc,
                 "uiTokenAmount": {"uiAmount": None, "uiAmountString": "0"}},
            ],
            "postTokenBalances": [
                {"owner": "RECIP", "mint": usdc,
                 "uiTokenAmount": {"uiAmount": None, "uiAmountString": "123.45"}},
            ],
        }
    }
    move = _token_move(via_string)
    assert move is not None
    amount, mint, _send_owner, recv_owner = move
    assert abs(amount - 123.45) < 1e-9
    assert mint == usdc and recv_owner == "RECIP"

    # No uiAmount, no string — only the raw atomic amount + decimals (1.5 USDC).
    via_raw = {
        "meta": {
            "preTokenBalances": [
                {"owner": "RECIP", "mint": usdc,
                 "uiTokenAmount": {"uiAmount": None, "amount": "0", "decimals": 6}},
            ],
            "postTokenBalances": [
                {"owner": "RECIP", "mint": usdc,
                 "uiTokenAmount": {"uiAmount": None, "amount": "1500000", "decimals": 6}},
            ],
        }
    }
    move_raw = _token_move(via_raw)
    assert move_raw is not None
    assert abs(move_raw[0] - 1.5) < 1e-9


def test_feed_rpc_hosts_are_covered_by_csp():
    """The browser verifier fetch must never be CSP-blocked: every Solana RPC
    origin the generated feed JS hits has to appear in the server's
    connect-src. Regression for the mainnet publicnode host being absent from
    the CSP while feed.py used it for mainnet verify."""
    import re as _re
    from urllib.parse import urlsplit
    from onchainbrief.feed import _render
    import serve_feed

    html_out = _render([])
    rpc_urls: list[str] = []
    # Only the right-hand side of `const rpcUrl = …;` — not every line that
    # merely references the variable (e.g. the unpkg web3 fetch).
    for rhs in _re.findall(r"rpcUrl\s*=\s*([^;]+);", html_out):
        rpc_urls += _re.findall(r"'(https://[^']+)'", rhs)
    assert rpc_urls, "expected at least one rpcUrl in the generated feed JS"

    csp = serve_feed.build_csp()
    connect_src = next(
        d for d in csp.split(";") if d.strip().startswith("connect-src")
    )
    for url in rpc_urls:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        assert origin in connect_src, f"{origin} missing from CSP connect-src"


