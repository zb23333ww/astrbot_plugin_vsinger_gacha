"""VSinger 收藏集抽卡（AstrBot / Pillow 渲染）。

移植自 Koishi 版 vsinger-gacha：按官方流通概率抽卡，三级抽取范围，
大隐藏/小隐藏保底、档内未拥有优先、普通卡镭射款；积分直接读写
``astrbot_plugin_azuresignin``（Azure 打卡）的余额，不内置签到。

所有图片由 Pillow 在本地绘制（浅色玻璃手账风），不依赖浏览器/网络。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image as MsgImage, Node, Nodes, Plain, Video
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import sp

PLUGIN_NAME = "astrbot_plugin_vsinger_gacha"
PKG_DIR = Path(__file__).parent
ASSETS = PKG_DIR / "assets"

# ---------------------------------------------------------------- 常量

#: 积分来源：Azure 打卡插件的 plugin_id（author/name 小写）
SIGNIN_PLUGIN_ID = "azure/astrbot_plugin_azuresignin"

COST = 10
MAX_DRAW = 10
EXCLUDE_POOLS = frozenset({"洛天依福利卡池", "全部奖池"})

#: 档位：40 大隐藏 / 30 小隐藏 / 20 稀有 / 10 普通
TIER_NAME = {40: "大隐藏", 30: "小隐藏", 20: "稀有", 10: "普通"}
TIER_COLOR = {40: (224, 96, 122), 30: (224, 154, 60), 20: (74, 144, 217), 10: (139, 152, 171)}

PITY_BIG = 400
PITY_HID = 40
LASER_RATE = 0.15

ACT_NAMES = {
    "171": "天依游学记系列",
    "282": "Vsinger集",
    "102550": "洛天依·收藏集",
    "106977": "乐正绫单人集",
    "108477": "洛天依·珍藏集",
    "112860": "vsinger",
    "113552": "洛天依·新像集",
    "113562": "言和·珍藏集",
}

ALL_POOLS = "全部卡池"

#: 设计令牌（与 Koishi 版统一）
PAPER = (255, 255, 255)
TEXT = (31, 42, 58)
TEXT_SUB = (93, 107, 128)
TEXT_DIM = (139, 152, 171)
ACCENT = (74, 144, 217)
PANEL = (255, 255, 255, 200)
CARD_BG = (242, 245, 250)
RADIUS = 18


# ---------------------------------------------------------------- 字体

class Fonts:
    """按字号缓存字体；标题用文楷，正文用悠哉。"""

    def __init__(self, title_file: Path | None, body_file: Path | None, fallback: Path | None):
        self.title_file = title_file or fallback
        self.body_file = body_file or fallback
        self._cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    def _load(self, kind: str, size: int) -> ImageFont.FreeTypeFont:
        key = (kind, size)
        if key in self._cache:
            return self._cache[key]
        path = self.title_file if kind == "title" else self.body_file
        try:
            font = ImageFont.truetype(str(path), size) if path else ImageFont.load_default()
        except OSError:
            font = ImageFont.load_default()
        self._cache[key] = font
        return font

    def title(self, size: int) -> ImageFont.FreeTypeFont:
        return self._load("title", size)

    def body(self, size: int) -> ImageFont.FreeTypeFont:
        return self._load("body", size)


def _text_w(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> str:
    """按像素宽度截断并加省略号。"""
    text = str(text or "")
    if _text_w(draw, text, font) <= max_w:
        return text
    while text and _text_w(draw, text + "…", font) > max_w:
        text = text[:-1]
    return text + "…" if text else ""


# ---------------------------------------------------------------- 渲染

class Renderer:
    """浅色玻璃手账风渲染器。"""

    def __init__(self, fonts: Fonts, tags_dir: Path, wallpapers_dir: Path):
        self.fonts = fonts
        self.tags_dir = tags_dir
        self.wallpapers = self._list_wallpapers(wallpapers_dir)
        self._tag_cache: dict[str, Image.Image | None] = {}

    @staticmethod
    def _list_wallpapers(directory: Path) -> list[Path]:
        try:
            return sorted(
                p for p in directory.iterdir()
                if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
            )
        except OSError:
            return []

    def _tag(self, name: str) -> Image.Image | None:
        if name in self._tag_cache:
            return self._tag_cache[name]
        image = None
        path = self.tags_dir / name
        try:
            if path.is_file():
                image = Image.open(path).convert("RGBA")
        except OSError:
            image = None
        self._tag_cache[name] = image
        return image

    # ------------------------------------------------------ 基础组件

    def background(self, width: int, height: int) -> Image.Image:
        canvas = Image.new("RGB", (width, height), (238, 243, 250))
        if self.wallpapers:
            try:
                source = Image.open(random.choice(self.wallpapers)).convert("RGB")
                # 铺满 + 轻模糊
                scale = max(width / source.width, height / source.height)
                resized = source.resize(
                    (max(1, int(source.width * scale)), max(1, int(source.height * scale))),
                    Image.LANCZOS,
                )
                left = (resized.width - width) // 2
                top = (resized.height - height) // 2
                cropped = resized.crop((left, top, left + width, top + height))
                canvas = cropped.filter(ImageFilter.GaussianBlur(14))
            except OSError:
                pass
        overlay = Image.new("RGBA", (width, height), (255, 255, 255, 214))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
        return canvas

    @staticmethod
    def round_rect(draw: ImageDraw.ImageDraw, box, radius: int, fill=None, outline=None, width: int = 1):
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)

    def panel(self, canvas: Image.Image, box, radius: int = RADIUS, alpha: int = 200):
        """白色半透明圆角面板（带柔和阴影）。"""
        x0, y0, x1, y1 = box
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (x0, y0 + 4, x1, y1 + 4), radius=radius, fill=(60, 90, 130, 40)
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))
        canvas.alpha_composite(shadow)
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            box, radius=radius, fill=(255, 255, 255, alpha), outline=(120, 160, 210, 70), width=1
        )
        canvas.alpha_composite(layer)

    def note(self, draw: ImageDraw.ImageDraw, cx: int, cy: int, radius: int):
        """圆形徽标里的音符（椭圆头 + 竖杆 + 旗）。"""
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=ACCENT)
        k = radius * 0.52
        hx, hy = cx - k * 0.34, cy + k * 0.52
        draw.ellipse((hx - k * 0.42, hy - k * 0.32, hx + k * 0.42, hy + k * 0.32), fill=(255, 255, 255))
        sx = hx + k * 0.36
        draw.rectangle((sx - k * 0.07, cy - k * 0.62, sx + k * 0.07, hy), fill=(255, 255, 255))
        draw.polygon(
            [
                (sx, cy - k * 0.62),
                (sx + k * 0.62, cy - k * 0.40),
                (sx + k * 0.46, cy - k * 0.08),
                (sx, cy - k * 0.34),
            ],
            fill=(255, 255, 255),
        )

    def header(self, canvas: Image.Image, title: str, subtitle: str, badge: str = "", badge_sub: str = "", progress: float = 0.0) -> int:
        """绘制页眉，返回页眉底部 Y。"""
        draw = ImageDraw.Draw(canvas)
        width = canvas.width
        pad = 40
        radius = max(24, min(44, int(width * 0.012)))
        cx, cy = pad + radius, 34 + radius
        self.note(draw, cx, cy, radius)

        title_size = max(34, int(radius * 1.15))
        title_font = self.fonts.title(title_size)
        sub_font = self.fonts.body(max(18, int(title_size * 0.55)))
        draw.text((cx + radius + 18, 30), _fit(draw, title, title_font, width - (cx + radius + 18) - 380), font=title_font, fill=TEXT)
        draw.text((cx + radius + 18, 30 + title_size + 10), subtitle, font=sub_font, fill=TEXT_SUB)

        bottom = 34 + radius * 2 + 24
        if badge:
            bw = max(170, min(420, int(width * 0.14)))
            bh = int(bw * 0.5)
            bx = width - pad - bw
            by = 30
            self.panel(canvas, (bx, by, bx + bw, by + bh), radius=16, alpha=240)
            draw = ImageDraw.Draw(canvas)
            badge_font = self.fonts.title(int(bh * 0.5))
            draw.text((bx + bw * 0.1, by + bh * 0.06), badge, font=badge_font, fill=ACCENT)
            if badge_sub:
                draw.text((bx + bw * 0.1, by + bh * 0.6), badge_sub, font=self.fonts.body(int(bh * 0.22)), fill=TEXT_SUB)
            pbar = (bx + bw * 0.08, by + bh - 16, bx + bw * 0.92, by + bh - 10)
            draw.rounded_rectangle(pbar, radius=3, fill=(74, 144, 217, 40))
            if progress > 0:
                p_x1 = pbar[0] + (pbar[2] - pbar[0]) * max(0.0, min(1.0, progress))
                draw.rounded_rectangle((pbar[0], pbar[1], p_x1, pbar[3]), radius=3, fill=ACCENT)
            bottom = max(bottom, by + bh + 18)
        return bottom

    def card(
        self,
        canvas: Image.Image,
        box,
        entry: dict,
        image_path: Path | None,
        owned: int = 1,
        laser: int = 0,
        size: tuple[int, int] = (200, 286),
    ):
        """画一张卡（圆角图 + 档位描边 + 角标 + 名称/概率）。"""
        x, y = box
        w, h = size
        tier = int(entry.get("card_scarcity") or 10)
        color = TIER_COLOR.get(tier, TIER_COLOR[10])

        tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if image_path and image_path.is_file():
            try:
                art = Image.open(image_path).convert("RGB")
                art = art.resize((w, h), Image.LANCZOS)
                if not owned:
                    # 未拥有：灰度 + 白蒙层（还能看出是哪张卡）
                    gray = art.convert("L").convert("RGB")
                    art = Image.blend(gray, Image.new("RGB", (w, h), (255, 255, 255)), 0.45)
                mask = Image.new("L", (w, h), 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, w, h), radius=14, fill=255)
                tile.paste(art, (0, 0), mask)
            except OSError:
                pass
        draw = ImageDraw.Draw(tile)
        self.round_rect(draw, (1, 1, w - 2, h - 2), 14, outline=color if owned else (150, 165, 185), width=3 if owned else 2)

        tag_name = "tag_40.webp" if tier == 40 else "tag_30.webp" if tier == 30 else ""
        tag = self._tag(tag_name) if tag_name else None
        if tag is not None:
            tw = int(w * 0.52)
            th = int(tag.height * tw / tag.width)
            tile.alpha_composite(tag.resize((tw, th), Image.LANCZOS), (0, 0))

        if laser and tier == 10:   # 镭射只属于普通档
            holo = self._tag("tag_holo.png")
            if holo is not None:
                hw = int(w * 0.36)
                hh = int(holo.height * hw / holo.width)
                tile.alpha_composite(holo.resize((hw, hh), Image.LANCZOS), ((w - hw) // 2, h - hh - 6))

        if owned > 1:
            badge_font = self.fonts.body(18)
            draw.rounded_rectangle((w - 58, 8, w - 8, 34), radius=10, fill=(31, 42, 58, 210))
            draw.text((w - 52, 10), f"×{owned}", font=badge_font, fill=(140, 255, 170))

        canvas.alpha_composite(tile, (x, y))

        draw = ImageDraw.Draw(canvas)
        name_font = self.fonts.body(20)
        meta_font = self.fonts.body(16)
        draw.text((x, y + h + 8), _fit(draw, entry.get("card_name", ""), name_font, w), font=name_font, fill=TEXT if owned else TEXT_DIM)
        meta = f"{TIER_NAME.get(tier, '普通')} {entry.get('rate_pct', '')}%"
        draw.text((x, y + h + 34), _fit(draw, meta, meta_font, w), font=meta_font, fill=color if owned else TEXT_DIM)

    def footer(self, canvas: Image.Image, text: str):
        draw = ImageDraw.Draw(canvas)
        draw.text((40, canvas.height - 34), text, font=self.fonts.body(15), fill=TEXT_DIM)


# ---------------------------------------------------------------- 插件

class VsingerGacha(Star):
    """收藏集抽卡主类。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.conf = config or {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.cards_dir = Path(self.conf.get("cardsDir") or self.data_dir / "cards")
        self.videos_dir = Path(self.conf.get("videosDir") or self.data_dir / "videos")
        self.thumb_dir = ASSETS / "thumbs"
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.catalog = self._load_catalog()
        self.pool_index, self.act_members = self._build_pool_index()

        title_file = ASSETS / "fonts" / (self.conf.get("titleFont") or "wenkai.ttf")
        body_file = ASSETS / "fonts" / (self.conf.get("bodyFont") or "yozai.ttf")
        fallback = ASSETS / "fonts" / "wenkai.ttf"
        self.fonts = Fonts(title_file, body_file, fallback)
        self.renderer = Renderer(self.fonts, ASSETS / "tags", Path(self.conf.get("wallpaperDir") or ASSETS / "wallpapers"))
        self._lock = asyncio.Lock()

        logger.info(
            f"[vsinger-gacha] 卡库 {len(self.catalog)} 张 / {len(self.pool_index)} 个奖池，"
            f"积分来源 {SIGNIN_PLUGIN_ID}"
        )

    # ------------------------------------------------------ 数据

    def _load_catalog(self) -> dict[str, dict]:
        path = ASSETS / "catalog.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error(f"[vsinger-gacha] 读取 catalog 失败：{exc}")
            return {}
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, dict)}

    def _build_pool_index(self) -> tuple[dict[str, list[tuple[str, dict]]], set[str]]:
        index: dict[str, list[tuple[str, dict]]] = {}
        acts: set[str] = set()
        for key, entry in self.catalog.items():
            try:
                rate = float(entry.get("rate_pct") or 0)
            except (TypeError, ValueError):
                continue
            if rate <= 0:
                continue
            acts.add(str(entry.get("act_id") or ""))
            for pool in entry.get("pool_list") or []:
                if not pool or pool in EXCLUDE_POOLS:
                    continue
                index.setdefault(pool, []).append((key, entry))
        return index, acts

    async def _load_state(self) -> dict:
        data = await self.get_kv_data("state", {})
        if not isinstance(data, dict):
            data = {}
        return {
            "owned": data.get("owned") if isinstance(data.get("owned"), dict) else {},
            "laser": data.get("laser") if isinstance(data.get("laser"), dict) else {},
            "pity": data.get("pity") if isinstance(data.get("pity"), dict) else {},
            "pools": data.get("pools") if isinstance(data.get("pools"), dict) else {},
        }

    async def _save_state(self, state: dict) -> None:
        await self.put_kv_data("state", state)

    # ------------------------------------------------------ 积分（azuresignin）

    async def _get_points(self, uid: str) -> int:
        data = await sp.get_async("plugin", SIGNIN_PLUGIN_ID, f"user_{uid}", {})
        if not isinstance(data, dict):
            return 0
        try:
            return int(data.get("points", 0) or 0)
        except (TypeError, ValueError):
            return 0

    async def _add_points(self, uid: str, delta: int) -> int:
        key = f"user_{uid}"
        data = await sp.get_async("plugin", SIGNIN_PLUGIN_ID, key, {})
        if not isinstance(data, dict):
            data = {}
        try:
            current = int(data.get("points", 0) or 0)
        except (TypeError, ValueError):
            current = 0
        data["points"] = max(0, current + delta)
        await sp.put_async("plugin", SIGNIN_PLUGIN_ID, key, data)
        return int(data["points"])

    # ------------------------------------------------------ 选择 / 抽卡

    @staticmethod
    def _chat_key(event: AstrMessageEvent) -> str:
        return getattr(event, "unified_msg_origin", None) or "default"

    def _resolve(self, selection: str) -> dict:
        names = set(self.pool_index)
        if selection in ("", ALL_POOLS, "全部") or selection not in names | set(ACT_NAMES.values()):
            return {"mode": "all", "label": ALL_POOLS, "cards": [kv for pool in names for kv in self.pool_index[pool]]}
        for act_id, name in ACT_NAMES.items():
            if name == selection:
                # 按卡自身的 act_id 归属，避免多系列卡被第一张带偏
                cards = [
                    (key, entry) for key, entry in self.catalog.items()
                    if str(entry.get("act_id")) == act_id
                    and any(p and p not in EXCLUDE_POOLS for p in (entry.get("pool_list") or []))
                ]
                if cards:
                    return {"mode": "series", "label": selection, "cards": cards}
        if selection in self.pool_index:
            return {"mode": "single", "label": selection, "cards": self.pool_index[selection]}
        return {"mode": "all", "label": ALL_POOLS, "cards": [kv for pool in names for kv in self.pool_index[pool]]}

    def _draw(self, scope: dict, uid: str, owned: dict[str, dict], pity: dict) -> tuple[str, dict, int, bool]:
        """按官方机制抽一张：档位质量选档 → 档内未拥有优先 → 保底 → 镭射。"""
        cards = scope["cards"]
        by_tier: dict[int, list[tuple[str, dict]]] = {40: [], 30: [], 20: [], 10: []}
        for key, entry in cards:
            tier = int(entry.get("card_scarcity") or 10)
            by_tier.setdefault(10 if tier not in TIER_NAME else tier, []).append((key, entry))

        def mass(tier: int) -> float:
            return sum(float(e.get("rate_pct") or 0.01) or 0.01 for _, e in by_tier[tier])

        pkey = f"{scope['label']}"
        pt = pity.setdefault(uid, {}).setdefault(pkey, {"big": 0, "hid": 0})
        big_ready = by_tier[40] and pt.get("big", 0) >= PITY_BIG - 1
        hid_ready = pt.get("hid", 0) >= PITY_HID - 1

        if big_ready:
            tier = 40
        elif hid_ready:
            tiers = [t for t in (40, 30) if by_tier[t]]
            if len(tiers) == 1:
                tier = tiers[0]
            else:
                weights = [mass(t) for t in tiers]
                tier = random.choices(tiers, weights=weights)[0]
        else:
            tiers = [t for t in (40, 30, 20, 10) if by_tier[t]]
            tier = random.choices(tiers, weights=[mass(t) for t in tiers])[0]

        pool = by_tier[tier]
        mine = owned.setdefault(uid, {})
        unowned = [(k, e) for k, e in pool if not mine.get(k)]
        pool = unowned or pool
        weights = [max(0.01, float(e.get("rate_pct") or 0.01)) for _, e in pool]
        key, entry = random.choices(pool, weights=weights)[0]

        mine[key] = int(mine.get(key, 0) or 0) + 1
        if tier == 40:
            pt["big"] = 0
        else:
            pt["big"] = int(pt.get("big", 0) or 0) + 1
        if tier in (30, 40):
            pt["hid"] = 0
        else:
            pt["hid"] = int(pt.get("hid", 0) or 0) + 1

        laser = tier == 10 and random.random() < LASER_RATE
        return key, entry, tier, laser

    # ------------------------------------------------------ 渲染入口

    def _thumb(self, key: str) -> Path | None:
        for suffix in (".jpg", ".png", ".webp"):
            for base in (self.thumb_dir, self.cards_dir):
                path = base / f"{key}{suffix}"
                if path.is_file():
                    return path
        return None

    def _render_result(self, rows: list[dict], title: str, badge: str, badge_sub: str, progress: float) -> Path:
        n = len(rows)
        per_row = n if n <= 3 else min(5, n)
        rows_n = (n + per_row - 1) // per_row
        cw = 300 if n == 1 else 200
        ch = int(cw * 1.43)
        gap, label, pad = 20, 62, 44
        width = max(720, pad * 2 + per_row * cw + (per_row - 1) * gap + 40)
        head = 140
        height = head + rows_n * (ch + label + gap) + pad

        canvas = self.renderer.background(width, height).convert("RGBA")
        head_bottom = self.renderer.header(canvas, title, f"共 {n} 张 ｜ 抽卡结果", badge, badge_sub, progress)

        px0, py0 = pad, head_bottom - 10
        px1 = width - pad
        py1 = py0 + rows_n * (ch + label + gap) + 10 - gap
        self.renderer.panel(canvas, (px0, py0, px1, py1), radius=28, alpha=190)
        draw = ImageDraw.Draw(canvas)
        draw.text((pad, head_bottom - 4), "", font=self.fonts.body(12))

        total_w = per_row * cw + (per_row - 1) * gap
        x0 = px0 + (px1 - px0 - total_w) // 2
        for i, row in enumerate(rows):
            cx = x0 + (i % per_row) * (cw + gap)
            cy = py0 + 20 + (i // per_row) * (ch + label + gap)
            self.renderer.card(
                canvas, (cx, cy), row["entry"], self._thumb(row["key"]),
                owned=row["owned"], laser=row["laser"], size=(cw, ch),
            )

        self.renderer.footer(canvas, "VSinger 收藏集 · 天依抽卡手账")
        out = self.cache_dir / f"result_{uuid.uuid4().hex}.jpg"
        canvas.convert("RGB").save(out, quality=90)
        return out

    def _render_handbook(self, uid: str, name: str, owned: dict, laser: dict, wallpaper_note: str = "") -> list[Path]:
        """按系列/池分组渲染图鉴，超长自动分页。"""
        groups: dict[str, dict[str, list[tuple[str, dict]]]] = {}
        for key, entry in self.catalog.items():
            pools = [p for p in entry.get("pool_list") or [] if p and p not in EXCLUDE_POOLS] or [p for p in entry.get("pool_list") or [] if p]
            if not pools:
                continue
            act = ACT_NAMES.get(str(entry.get("act_id") or ""), f"act {entry.get('act_id')}")
            groups.setdefault(act, {}).setdefault(pools[0], []).append((key, entry))

        cw, ch = 240, 343
        gap, label = 24, 74
        cols = 6
        pad = 48
        width = pad * 2 + cols * cw + (cols - 1) * gap
        block_head = 52
        series_h = 58

        # 先按 36 张/页切分（按池整块放）
        pages: list[list[tuple[str, str, list[tuple[str, dict]]]]] = []
        current: list[tuple[str, str, list[tuple[str, dict]]]] = []
        budget = 0
        for act, pool_map in groups.items():
            for pool, cards in pool_map.items():
                cards.sort(key=lambda kv: -(float(kv[1].get("rate_pct") or 0)))
                if current and budget + len(cards) > 36:
                    pages.append(current)
                    current, budget = [], 0
                current.append((act, pool, cards))
                budget += len(cards)
                if budget >= 36:
                    pages.append(current)
                    current, budget = [], 0
        if current:
            pages.append(current)

        outputs: list[Path] = []
        total_cards = len(self.catalog)
        collected = sum(1 for k in self.catalog if owned.get(k))
        for index, page in enumerate(pages, 1):
            rows_n = sum((len(c) + cols - 1) // cols for _, _, c in page)
            height = 150 + rows_n * (ch + label) + sum(series_h + block_head for _ in page) + 80
            canvas = self.renderer.background(width, height).convert("RGBA")
            head_bottom = self.renderer.header(
                canvas,
                "VSinger 收藏集 · 我的图鉴",
                f"{name}　已收集 {collected}/{total_cards}　重复 {sum(max(0, int(v) - 1) for v in owned.values())} 张",
                badge=f"{collected}/{total_cards}",
                badge_sub="收集进度",
                progress=collected / total_cards if total_cards else 0,
            )
            y = max(head_bottom, 150)
            prev_act = None
            for act, pool, cards in page:
                if act != prev_act:
                    self.renderer.panel(canvas, (pad, y, width - pad, y + series_h - 10), radius=16, alpha=235)
                    draw = ImageDraw.Draw(canvas)
                    draw.rectangle((pad + 14, y + 14, pad + 19, y + series_h - 24), fill=ACCENT)
                    draw.text((pad + 30, y + 12), act, font=self.fonts.title(28), fill=TEXT)
                    y += series_h
                    prev_act = act
                got = sum(1 for k, _ in cards if owned.get(k))
                self.renderer.panel(canvas, (pad, y + 2, width - pad, y + 2 + block_head - 12), radius=14, alpha=225)
                draw = ImageDraw.Draw(canvas)
                draw.text((pad + 16, y + 8), _fit(draw, pool, self.fonts.body(24), width - pad * 2 - 160), font=self.fonts.body(24), fill=ACCENT)
                draw.text((width - pad - 90, y + 10), f"{got}/{len(cards)}", font=self.fonts.body(20), fill=TEXT_SUB)
                y += block_head
                for i, (key, entry) in enumerate(cards):
                    cx = pad + (i % cols) * (cw + gap)
                    cy = y + (i // cols) * (ch + label)
                    self.renderer.card(
                        canvas, (cx, cy), entry, self._thumb(key),
                        owned=int(owned.get(key, 0) or 0),
                        laser=int(laser.get(key, 0) or 0),
                        size=(cw, ch),
                    )
                y += ((len(cards) + cols - 1) // cols) * (ch + label)
            self.renderer.footer(canvas, f"VSinger 收藏集 · 图鉴 {index}/{len(pages)}")
            out = self.cache_dir / f"handbook_{uuid.uuid4().hex}.jpg"
            canvas.convert("RGB").save(out, quality=88)
            outputs.append(out)
        return outputs

    # ------------------------------------------------------ 指令

    def _self_uin(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_self_id() or "0")
        except Exception:
            return "0"

    def _forward(self, event: AstrMessageEvent, nodes: list[Node]):
        return event.chain_result([Nodes(nodes=nodes)])

    @filter.command("抽卡")
    async def cmd_draw(self, event: AstrMessageEvent):
        """抽卡，默认 1 张，最多 10 张。"""
        raw = re.sub(r"^.*?抽卡\s*", "", event.message_str or "").strip()
        if raw and not raw.isdigit():
            yield event.plain_result(self._help_text(event.is_admin()))
            return
        count = max(1, min(MAX_DRAW, int(raw) if raw else 1))
        uid = event.get_sender_id()
        need = count * COST

        async with self._lock:
            points = await self._get_points(uid)
            if points < need:
                yield event.plain_result(
                    f"💠 积分不足\n需要 {need} 分（{count} 抽 × {COST}），你现在 {points} 分\n\n发 /打卡 可以赚取积分"
                )
                return
            state = await self._load_state()
            scope = self._resolve(state["pools"].get(self._chat_key(event), ALL_POOLS))
            if not scope["cards"]:
                yield event.plain_result("当前奖池没有可抽的卡，发 /换池 换一个")
                return
            left = await self._add_points(uid, -need)
            rows = []
            for _ in range(count):
                key, entry, tier, laser = self._draw(scope, uid, state["owned"], state["pity"])
                if laser and tier == 10:   # 镭射只属于普通档
                    laser_map = state["laser"].setdefault(uid, {})
                    laser_map[key] = int(laser_map.get(key, 0) or 0) + 1
                rows.append({
                    "key": key,
                    "entry": entry,
                    "owned": int(state["owned"][uid].get(key, 0) or 0),
                    "laser": laser,
                    "tier": tier,
                })
            await self._save_state(state)

        title = f"{event.get_sender_name() or '你'} 抽了 {count} 张（-{need} 分，剩 {left}）"
        try:
            image = self._render_result(rows, title, str(left), "当前积分", 0.0)
        except Exception:
            logger.error("[vsinger-gacha] 结果图渲染失败", exc_info=True)
            image = None

        if image:
            yield event.image_result(str(image))
        else:
            lines = [f"{TIER_NAME.get(r['tier'], '普通')}｜{r['entry'].get('card_name', '')}｜{r['entry'].get('rate_pct', '')}%" for r in rows]
            yield event.plain_result(title + "\n" + "\n".join(lines))

        # 合并转发：原图 + 文字（无合并转发能力时降级为直接发图）
        name, uin = "收藏集", self._self_uin(event)
        nodes: list[Node] = []
        for row in rows:
            origin = None
            for suffix in (".jpg", ".png", ".webp"):
                p = self.cards_dir / f"{row['key']}{suffix}"
                if p.is_file():
                    origin = p
                    break
            caption = f"{row['entry'].get('card_name', '')} · {TIER_NAME.get(row['tier'], '普通')}"
            if row["laser"]:
                caption += " · 镭射款"
            if row["owned"] > 1:
                caption += f" · ×{row['owned']}"
            content = [Plain(caption)]
            if origin:
                content.append(MsgImage.fromFileSystem(str(origin)))
            nodes.append(Node(content=content, name=name, uin=uin))
        try:
            if nodes:
                yield self._forward(event, nodes)
        except Exception:
            logger.warning("[vsinger-gacha] 合并转发失败，改逐张发送", exc_info=True)
            for row in rows:
                p = self._thumb(row["key"])
                if p:
                    yield event.image_result(str(p))

        # 动态卡：只要视频文件存在就单独补发（不限制档位）
        for row in rows:
            video = self.videos_dir / f"{row['key']}.mp4"
            if video.is_file():
                yield event.chain_result([
                    Plain(f"🎬 {row['entry'].get('card_name', '')} · {TIER_NAME.get(row['tier'], '')}"),
                    Video.fromFileSystem(str(video)),
                ])

    def _pool_table(self) -> list[tuple[int, str, str]]:
        """生成 00 全部 / 系列 / 池 的三级编号表：(序号, 类型, 名称)。"""
        table: list[tuple[int, str, str]] = [(0, "all", ALL_POOLS)]
        series: dict[str, list[str]] = {}
        for pool, cards in self.pool_index.items():
            act_id = str(cards[0][1].get("act_id") or "")
            act = ACT_NAMES.get(act_id, f"act {act_id}")
            series.setdefault(act, []).append(pool)
        index = 0
        for act, pools in series.items():
            index += 1
            table.append((index, "series", act))
            for pool in pools:
                index += 1
                table.append((index, "pool", pool))
        return table

    @filter.command("卡池")
    async def cmd_pools(self, event: AstrMessageEvent):
        """列出全部奖池与当前选择（编号与 /换池 对齐）。"""
        if not self.pool_index:
            yield event.plain_result("卡池数据为空：请确认 assets/catalog.json 存在")
            return
        state = await self._load_state()
        current = state["pools"].get(self._chat_key(event), ALL_POOLS)
        table = self._pool_table()
        owned = state["owned"].get(event.get_sender_id(), {})

        lines = [f"🎴 抽取范围（当前：{current}）", "", f"00. 全部卡池（{len(self.catalog)} 张）", ""]
        for idx, kind, name in table[1:]:
            if kind == "series":
                cards = [kv for pool, items in self.pool_index.items() if ACT_NAMES.get(str(items[0][1].get("act_id") or ""), f"act {items[0][1].get('act_id')}") == name for kv in items]
                got = sum(1 for k, _ in cards if owned.get(k))
                lines.append(f"{idx:02d}. 【系列】{name}（{len(cards)} 张，已收 {got}）")
            else:
                cards = self.pool_index[name]
                got = sum(1 for k, _ in cards if owned.get(k))
                lines.append(f"   {idx:02d}. {name}（{len(cards)} 张，已收 {got}）")
        lines += ["", "切换：/换池 序号（00 = 全部卡池）", "说明：/抽卡帮助"]

        name, uin = "收藏集", self._self_uin(event)
        nodes = [Node(content=[Plain("\n".join(lines[i:i + 18]))], name=name, uin=uin) for i in range(0, len(lines), 18)]
        try:
            yield self._forward(event, nodes)
        except Exception:
            yield event.plain_result("\n".join(lines))

    @filter.command("换池")
    async def cmd_switch(self, event: AstrMessageEvent):
        """切换本群抽卡范围。"""
        target = re.sub(r"^.*?换池\s*", "", event.message_str or "").strip()
        if not target:
            yield event.plain_result("用法：/换池 序号（先发 /卡池 看列表）")
            return
        table = self._pool_table()
        chosen = None
        if target.isdigit():
            idx = int(target)
            chosen = next((n for i, _, n in table if i == idx), None)
            if chosen is None:
                yield event.plain_result(f"序号超出范围（0-{len(table) - 1}）")
                return
        elif target in (ALL_POOLS, "全部", "0", "00"):
            chosen = ALL_POOLS
        else:
            hits = [n for _, _, n in table if n == target] or [n for _, _, n in table if target in n]
            if len(hits) != 1:
                hint = "、".join(hits[:6]) if hits else "没有匹配的奖池"
                yield event.plain_result(f"{hint}\n用法：/换池 序号")
                return
            chosen = hits[0]

        async with self._lock:
            state = await self._load_state()
            state["pools"][self._chat_key(event)] = chosen
            await self._save_state(state)
        scope = self._resolve(chosen)
        detail = f"{len(scope['cards'])} 张卡" if scope["mode"] == "single" else f"{len(self.pool_index)} 个奖池"
        yield event.plain_result(f"✅ 已切换到「{chosen}」（{detail}）")

    @filter.command("图鉴")
    async def cmd_handbook(self, event: AstrMessageEvent):
        """生成本人图鉴（浅色玻璃长图，超长自动分页）。"""
        if not self.catalog:
            yield event.plain_result("卡库为空，请确认 assets/catalog.json")
            return
        state = await self._load_state()
        uid = event.get_sender_id()
        owned = state["owned"].get(uid, {})
        laser = state["laser"].get(uid, {})
        name = event.get_sender_name() or uid
        try:
            pages = self._render_handbook(uid, name, owned, laser)
        except Exception:
            logger.error("[vsinger-gacha] 图鉴渲染失败", exc_info=True)
            yield event.plain_result("图鉴渲染失败，查看日志获取详情")
            return
        if not pages:
            yield event.plain_result("图鉴为空")
            return
        if len(pages) == 1:
            yield event.image_result(str(pages[0]))
            return
        fname, uin = "收藏集", self._self_uin(event)
        nodes = [
            Node(content=[Plain(f"第 {i}/{len(pages)} 页"), MsgImage.fromFileSystem(str(p))], name=fname, uin=uin)
            for i, p in enumerate(pages, 1)
        ]
        try:
            yield self._forward(event, nodes)
        except Exception:
            for p in pages:
                yield event.image_result(str(p))

    @filter.command("获取卡")
    async def cmd_get_card(self, event: AstrMessageEvent):
        """发送自己已拥有的卡的原图。"""
        keyword = re.sub(r"^.*?获取卡\s*", "", event.message_str or "").strip()
        if not keyword:
            yield event.plain_result("用法：/获取卡 卡名")
            return
        state = await self._load_state()
        owned = state["owned"].get(event.get_sender_id(), {})
        hits = [
            (key, entry) for key, entry in self.catalog.items()
            if keyword in str(entry.get("card_name", "")) and owned.get(key)
        ]
        if not hits:
            yield event.plain_result(f"你还没有名字含「{keyword}」的卡")
            return
        if len(hits) > 1:
            names = "、".join(e.get("card_name", "") for _, e in hits[:8])
            yield event.plain_result(f"匹配到多张，请写更完整：{names}")
            return
        key, entry = hits[0]
        path = None
        for suffix in (".jpg", ".png", ".webp"):
            p = self.cards_dir / f"{key}{suffix}"
            if p.is_file():
                path = p
                break
        if path:
            yield event.image_result(str(path))
        else:
            thumb = self._thumb(key)
            if thumb:
                yield event.image_result(str(thumb))
        tier = int(entry.get("card_scarcity") or 10)
        yield event.plain_result(
            f"🎴 {entry.get('card_name', '')}\n档位：{TIER_NAME.get(tier, '普通')} ｜ 概率：{entry.get('rate_pct')}% ｜ 你拥有 ×{owned.get(key, 0)}"
        )

    @filter.command("抽卡帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """抽卡玩法说明。"""
        yield event.plain_result(self._help_text(event.is_admin()))

    def _help_text(self, is_admin: bool = False) -> str:
        lines = [
            "🎴 收藏集抽卡 · 使用说明",
            "",
            f"/抽卡 [{1}-{MAX_DRAW}]     抽卡（{COST} 分/抽）",
            "/卡池             全部抽取范围（合并消息）",
            "/换池 00          全部卡池",
            "/换池 01          整个系列",
            "/换池 12          单个奖池",
            "/图鉴             我的图鉴长图（合并消息）",
            "/获取卡 <卡名>    取出已拥有的卡",
            "",
            "【规则】",
            "  · 收集进度全局共用；卡池选择每个群独立",
            "  · 按官方概率；档内未拥有优先；大隐藏 400 / 隐藏 40 保底",
            "  · 重复抽到会累加，图鉴显示 ×N",
            "  · 普通卡 15% 概率出镭射款",
            "  · 有动图的卡会额外发 mp4",
            f"  · 当前卡库 {len(self.catalog)} 张 / {len(self.pool_index)} 个奖池",
            "",
            "【档位】★★★★★大隐藏 / ★★★★小隐藏 / ★★★稀有 / ★★普通",
            "【积分】由打卡插件提供：发 /打卡 赚取",
        ]
        if is_admin:
            lines += [
                "",
                "【管理员】",
                "/设积分 <数量> [@某人]",
                "/加积分 <数量> [@某人]（负数即扣除）",
                "/查积分 [@某人]",
                "/给卡 <@某人> <卡名> [数量]",
            ]
        return "\n".join(lines)

    # ------------------------------------------------------ 管理员

    @staticmethod
    def _target(event: AstrMessageEvent, text: str) -> tuple[str, str]:
        uid = event.get_sender_id()
        for component in event.message_obj.message:
            if isinstance(component, At) and str(component.qq) != "all":
                uid = str(component.qq)
                break
        return uid, re.sub(r"@\S+\s*", "", text).strip()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设积分")
    async def cmd_set_points(self, event: AstrMessageEvent):
        """把某人积分设为指定值。"""
        tail = re.sub(r"^.*?设积分\s*", "", event.message_str or "").strip()
        target, rest = self._target(event, tail)
        match = re.match(r"^(\d+)", rest)
        if not match:
            yield event.plain_result("用法：/设积分 <数量> [@某人]")
            return
        amount = int(match.group(1))
        async with self._lock:
            current = await self._get_points(target)
            left = await self._add_points(target, amount - current)
        yield event.plain_result(f"✅ 已将 {target} 的积分设为 {left}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("加积分")
    async def cmd_add_points(self, event: AstrMessageEvent):
        """增减积分，负数即扣除。"""
        tail = re.sub(r"^.*?加积分\s*", "", event.message_str or "").strip()
        target, rest = self._target(event, tail)
        match = re.match(r"^([+-]?\d+)", rest)
        if not match:
            yield event.plain_result("用法：/加积分 <数量> [@某人]（负数即扣除）")
            return
        delta = int(match.group(1))
        async with self._lock:
            left = await self._add_points(target, delta)
        sign = f"+{delta}" if delta >= 0 else str(delta)
        yield event.plain_result(f"✅ 已为 {target} 调整 {sign} 积分，当前余额 {left}")

    @filter.command("查积分")
    async def cmd_query_points(self, event: AstrMessageEvent):
        """查询积分（可 @ 别人）。"""
        tail = re.sub(r"^.*?查积分\s*", "", event.message_str or "").strip()
        target, _ = self._target(event, tail)
        points = await self._get_points(target)
        yield event.plain_result(f"💎 {target} 当前积分：{points}")

    @filter.command("给卡")
    async def cmd_give_card(self, event: AstrMessageEvent):
        """管理员直接发卡；普通用户从自己库存转赠。"""
        tail = re.sub(r"^.*?给卡\s*", "", event.message_str or "").strip()
        target, rest = self._target(event, tail)
        if target == event.get_sender_id():
            yield event.plain_result("用法：/给卡 <@某人> <卡名> [数量]")
            return
        qty = 1
        m = re.search(r"\s+[x×*]?\s*(\d+)$", rest)
        if m:
            qty = max(1, int(m.group(1)))
            rest = rest[: m.start()].strip()
        if not rest:
            yield event.plain_result("用法：/给卡 <@某人> <卡名> [数量]")
            return
        hits = [(k, e) for k, e in self.catalog.items() if e.get("card_name") == rest] or [
            (k, e) for k, e in self.catalog.items() if rest in str(e.get("card_name", ""))
        ]
        if not hits:
            yield event.plain_result(f"卡库里没有名字含「{rest}」的卡")
            return
        if len(hits) > 1:
            names = "、".join(e.get("card_name", "") for _, e in hits[:8])
            yield event.plain_result(f"匹配到多张，请写更完整：{names}")
            return
        key, entry = hits[0]
        sender = event.get_sender_id()
        admin = event.is_admin()
        async with self._lock:
            state = await self._load_state()
            mine = state["owned"].setdefault(sender, {})
            if not admin:
                if int(mine.get(key, 0) or 0) < qty:
                    yield event.plain_result(f"你只有 ×{int(mine.get(key, 0) or 0)} 张，不够转赠")
                    return
                mine[key] = int(mine[key]) - qty
            theirs = state["owned"].setdefault(target, {})
            theirs[key] = int(theirs.get(key, 0) or 0) + qty
            await self._save_state(state)
        yield event.plain_result(
            f"✅ 已把 {entry.get('card_name', '')} ×{qty} 给 {target}\n该用户现有 ×{theirs[key]}"
        )
