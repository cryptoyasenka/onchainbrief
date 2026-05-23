import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from onchainbrief.feed import _parse_brief
from onchainbrief.compose import compose_card

def _first_sentence(text: str) -> str:
    head = text.split(". ", 1)[0].strip()
    return head[:-1] if head.endswith(".") else head

def main():
    briefs_dir = ROOT / "briefs"
    for md_path in briefs_dir.glob("*.md"):
        item = _parse_brief(md_path)
        if not item:
            print(f"Failed to parse {md_path}")
            continue
        
        png_path = briefs_dir / f"{md_path.stem}.png"
        if not png_path.exists():
            print(f"No PNG for {md_path}")
            continue
            
        print(f"Recompositing {png_path.name}...")
        subline = _first_sentence(item.narrative)
        
        attestation = None
        if item.attest_sig:
            attestation = (item.attest_sig, item.attest_cluster)
            
        compose_card(
            base_image=png_path,
            headline=item.headline,
            subline=subline,
            signature=item.signature,
            out_path=png_path,
            attestation=attestation
        )
    print("Done recompositing cards!")

if __name__ == "__main__":
    main()
