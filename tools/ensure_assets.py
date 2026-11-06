"""检查 AstrBot 插件素材是否齐全（只读）。"""
import argparse
from pathlib import Path

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", help="插件 data 目录，例如 data/plugin_data/astrbot_plugin_vsinger_gacha")
    ap.add_argument("--catalog", default="assets/catalog.json")
    args = ap.parse_args()

    base = Path(args.data_dir)
    catalog_file = Path(__file__).resolve().parent.parent / args.catalog
    import json
    catalog = json.loads(catalog_file.read_text(encoding="utf-8"))

    cards = base / "cards"
    holo = cards / "holo"
    videos = base / "videos"
    def count(d: Path, suffix: str) -> int:
        return sum(1 for _ in d.glob(f"*{suffix}")) if d.is_dir() else 0

    have = sum(1 for h in catalog if any((cards / f"{h}{s}").is_file() for s in (".png", ".jpg", ".webp")))
    print(f"catalog: {len(catalog)} 张")
    print(f"cards:   {have}/{len(catalog)} ({'OK' if have == len(catalog) else '缺图'})")
    print(f"holo:    {count(holo, '.jpg')} 张")
    print(f"videos:  {count(videos, '.mp4')} 个")
    if have < len(catalog):
        print("\n提示：把原图目录软链到 cards/ 即可：")
        print(f"  ln -s /path/to/cards2 {cards}")

if __name__ == "__main__":
    main()
