"""下载卡图并生成缩略图。

用法：
    python tools/prepare_cards.py --dest ~/astrbot/data/plugin_data/astrbot_plugin_vsinger_gacha/cards
    python tools/prepare_cards.py --dest ./cards --thumbs ./assets/thumbs --limit 50

catalog.json 里每张卡带 ``img``（原图 URL），脚本按 hash 命名保存为 .jpg，
并把缩略图（240x343）写进 ``--thumbs`` 目录（插件自带一份，缺了才需要重跑）。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "assets" / "catalog.json"


def fetch(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", required=True, help="原图目录（插件 data/cards）")
    parser.add_argument("--thumbs", default="", help="缩略图输出目录（可选）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 张（调试用）")
    parser.add_argument("--force", action="store_true", help="已有文件也重新下载")
    args = parser.parse_args()

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    dest = Path(args.dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    thumbs = Path(args.thumbs).expanduser() if args.thumbs else None
    if thumbs:
        thumbs.mkdir(parents=True, exist_ok=True)

    keys = list(catalog)
    if args.limit:
        keys = keys[: args.limit]

    ok = skip = fail = 0
    for index, key in enumerate(keys, 1):
        entry = catalog[key]
        target = dest / f"{key}.jpg"
        thumb = thumbs / f"{key}.jpg" if thumbs else None
        if target.is_file() and not args.force and not (thumb and not thumb.is_file()):
            skip += 1
            continue
        url = entry.get("img") or ""
        if not url:
            fail += 1
            continue
        try:
            raw = fetch(url)
            image = Image.open(io.BytesIO(raw)).convert("RGB")
            image.save(target, "JPEG", quality=92)
            if thumb:
                image.resize((240, 343), Image.LANCZOS).save(thumb, "JPEG", quality=84)
            ok += 1
        except Exception as exc:  # noqa: BLE001 - 单张失败不影响整体
            fail += 1
            print(f"[{index}/{len(keys)}] {key} 失败：{exc}", file=sys.stderr)
        if index % 20 == 0:
            print(f"  {index}/{len(keys)} 完成（成功 {ok} / 跳过 {skip} / 失败 {fail}）", flush=True)

    print(f"完成：成功 {ok}，跳过 {skip}，失败 {fail}；原图目录 {dest}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
