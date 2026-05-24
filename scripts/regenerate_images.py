import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from onchainbrief.config import Settings
from onchainbrief.ace_client import AceClient
from onchainbrief.ace_brief_client import AceBriefClient
from onchainbrief.feed import _parse_brief
from onchainbrief.compose import compose_card

def _first_sentence(text: str) -> str:
    head = text.split(". ", 1)[0].strip()
    return head[:-1] if head.endswith(".") else head

def main():
    settings = Settings.load()
    transport = AceClient(settings)
    client = AceBriefClient(transport)
    briefs_dir = ROOT / "briefs"

    for md_path in briefs_dir.glob("*.md"):
        if md_path.stem in ("3KWgi7jPuD9FBUMf", "4RWQZAZW99cAKW4N"):
            print(f"Skipping {md_path.name} (already regenerated in modern style)")
            continue

        item = _parse_brief(md_path)
        if not item:
            print(f"Failed to parse {md_path}")
            continue

        print(f"\n--- Processing {md_path.name} ---")
        headline = item.headline
        print(f"Headline: {headline}")

        # Construct prompt matching updated modern 3D geometric style
        prompt = (
            f"Abstract modern 3D geometric concept for: {headline}. "
            "Matte clay shapes, frosted glass blocks, neutral sophisticated colors of charcoal, silver, and muted bronze. "
            "Soft clean studio lighting, high depth-of-field background blur, modern tech branding design, no text"
        )
        print(f"Prompt: {prompt}")

        print("Requesting new image from ACE...")
        try:
            image_bytes = client.image(prompt)
            print("Successfully received image bytes via credit client.")
        except Exception as e:
            print(f"Failed to generate image via credit client: {e}")
            if "sufficient" in str(e).lower() or "used_up" in str(e).lower() or "403" in str(e).lower():
                print("Attempting fallback to on-chain X402 payment client...")
                try:
                    from onchainbrief.x402_client import X402Client
                    x402_transport = X402Client(settings)
                    x402_client = AceBriefClient(x402_transport)
                    image_bytes = x402_client.image(prompt)
                    print("Successfully received image bytes via x402 client.")
                except Exception as x_err:
                    print(f"Fallback to x402 also failed: {x_err}")
                    continue
            else:
                continue

        png_path = briefs_dir / f"{md_path.stem}.png"
        print(f"Composing card to {png_path}...")
        
        attestation = None
        if item.attest_sig:
            attestation = (item.attest_sig, item.attest_cluster)

        subline = _first_sentence(item.narrative)
        compose_card(
            base_image=image_bytes,
            headline=headline,
            subline=subline,
            signature=item.signature,
            out_path=png_path,
            attestation=attestation
        )
        print(f"Updated card for {md_path.stem}")

    print("\nAll cards regenerated successfully!")

if __name__ == "__main__":
    main()
