"""VSinger 收藏集抽卡（AstrBot / Pillow 渲染）。

移植自 Koishi 版 vsinger-gacha：按官方流通概率抽卡，三级抽取范围，
大隐藏/小隐藏保底、档内未拥有优先、普通卡镭射款；积分直接读写
``astrbot_plugin_azuresignin``（Azure 打卡）的余额，不内置签到。

所有图片由 Pillow 在本地绘制（浅色玻璃手账风），不依赖浏览器/网络。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time
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
        """白色半透明圆角面板（阴影只画在面板局部，避免整画布分配）。"""
        x0, y0, x1, y1 = (int(v) for v in box)
        w, h = max(2, x1 - x0), max(2, y1 - y0)
        pad = 24
        shadow = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            (pad, pad + 4, pad + w, pad + h + 4), radius=radius, fill=(60, 90, 130, 40)
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(8))
        canvas.alpha_composite(shadow, (x0 - pad, y0 - pad))
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            (0, 0, w - 1, h - 1), radius=radius, fill=(255, 255, 255, alpha), outline=(120, 160, 210, 70), width=1
        )
        canvas.alpha_composite(layer, (x0, y0))

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
        radius = max(30, min(200, int(34 * max(1.0, min(2.6, width / 1600)))))
        cx, cy = pad + radius, 34 + radius
        self.note(draw, cx, cy, radius)

        title_size = max(34, int(radius * 1.0))
        title_font = self.fonts.title(title_size)
        sub_font = self.fonts.body(max(18, int(title_size * 0.5)))
        reserved = (min(760, int(width * 0.17)) + 24) if badge else 24
        draw.text((cx + radius + 18, 30),
                  _fit(draw, title, title_font, width - (cx + radius + 18) - reserved),
                  font=title_font, fill=TEXT)
        draw.text((cx + radius + 18, 30 + title_size + 10), subtitle, font=sub_font, fill=TEXT_SUB)

        bottom = 34 + radius * 2 + 28
        if badge:
            bw = max(180, min(760, int(width * 0.17)))
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
            draw.rounded_rectangle(pbar, radius=3, fill=(214, 228, 244))
            if progress > 0:
                p_x1 = pbar[0] + (pbar[2] - pbar[0]) * max(0.0, min(1.0, progress))
                draw.rounded_rectangle((pbar[0], pbar[1], p_x1, pbar[3]), radius=3, fill=ACCENT)
            bottom = max(bottom, by + bh + 24)
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
        show_count: bool = True,
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

        if laser and tier == 10:
            holo = self._tag("tag_holo.png")
            if holo is not None:
                hw = int(w * 0.36)
                hh = int(holo.height * hw / holo.width)
                tile.alpha_composite(holo.resize((hw, hh), Image.LANCZOS), ((w - hw) // 2, h - hh - 6))

        if owned > 1 and show_count:
            badge_font = self.fonts.body(18)
            draw.rounded_rectangle((w - 58, 8, w - 8, 34), radius=10, fill=(31, 42, 58, 210))
            draw.text((w - 52, 10), f"×{owned}", font=badge_font, fill=(140, 255, 170))

        canvas.alpha_composite(tile, (x, y))

        draw = ImageDraw.Draw(canvas)
        name_font = self.fonts.body(max(15, min(24, int(w * 0.085))))
        meta_font = self.fonts.body(max(13, min(18, int(w * 0.068))))
        draw.text((x, y + h + 8), _fit(draw, entry.get("card_name", ""), name_font, w), font=name_font, fill=TEXT if owned else TEXT_DIM)
        meta = f"{TIER_NAME.get(tier, '普通')} {entry.get('rate_pct', '')}%"
        if laser and tier == 10:
            meta += " · 镭射款"
        draw.text((x, y + h + 40), _fit(draw, meta, meta_font, w), font=meta_font, fill=color if owned else TEXT_DIM)

    def footer(self, canvas: Image.Image, text: str):
        draw = ImageDraw.Draw(canvas)
        draw.text((40, canvas.height - 34), text, font=self.fonts.body(15), fill=TEXT_DIM)


# ---------------------------------------------------------------- 插件

class VsingerGacha(Star):
    """收藏集抽卡主类。"""

    #: AstrBot 正常加载时会注入；独立插件宿主（如 astrbot-daemon）没有时用这个兜底
    plugin_id = "zb23333ww/astrbot_plugin_vsinger_gacha"

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
        self.forward = bool(self.conf.get("forward", True))   # OneBot 默认合并转发；官方平台走单独分支
        # QQ 官方机器人被动回复：同一个 msg_id 最多 5 条
        self.max_messages = max(1, int(self.conf.get("maxMessages", 5) or 5))
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
            "minigames": data.get("minigames") if isinstance(data.get("minigames"), dict) else {},
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

    def _is_official(self, event: AstrMessageEvent) -> bool:
        """是否 QQ 官方平台（被动回复最多 5 条，需要压缩）。"""
        try:
            name = str(event.get_platform_name() or "")
        except Exception:
            name = ""
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        return "qq_official" in name or "qq_official" in origin

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
        gap, label, pad = 20, 78, 44
        width = max(720, pad * 2 + per_row * cw + (per_row - 1) * gap + 40)
        head = 140
        height = head + rows_n * (ch + label + gap) + pad + 70

        canvas = self.renderer.background(width, height).convert("RGBA")
        head_bottom = self.renderer.header(canvas, title, f"共 {n} 张 ｜ 抽卡结果", badge, badge_sub, progress)

        px0, py0 = pad, head_bottom - 10
        px1 = width - pad
        py1 = py0 + rows_n * (ch + label + gap) + 50
        self.renderer.panel(canvas, (px0, py0, px1, py1), radius=28, alpha=190)
        draw = ImageDraw.Draw(canvas)
        draw.text((pad, head_bottom - 4), "", font=self.fonts.body(12))

        total_w = per_row * cw + (per_row - 1) * gap
        x0 = px0 + (px1 - px0 - total_w) // 2
        for i, row in enumerate(rows):
            cx = x0 + (i % per_row) * (cw + gap)
            cy = py0 + 20 + (i // per_row) * (ch + label + gap)
            art = self._laser_image(row["key"]) if row.get("laser") else None
            art = art or self._thumb(row["key"])
            self.renderer.card(
                canvas, (cx, cy), row["entry"], art,
                owned=row["owned"], laser=row["laser"], size=(cw, ch),
                show_count=False,
            )

        self.renderer.footer(canvas, "VSinger 收藏集 · 天依抽卡手账")
        out = self.cache_dir / f"result_{uuid.uuid4().hex}.jpg"
        canvas.convert("RGB").save(out, quality=90)
        return out


    def _render_handbook_flat(self, uid: str, name: str, owned: dict, laser: dict,
                              cols: int = 24, rows_per_page: int = 5) -> list[Path]:
        """扁平横图图鉴：不分池，纯网格（官方机器人 5 条消息内装完）。"""
        ordered: list[tuple[str, dict]] = []
        for act_id, act_name in ACT_NAMES.items():
            act_cards = [
                (k, e) for k, e in self.catalog.items()
                if str(e.get("act_id")) == act_id
                and any(p and p not in EXCLUDE_POOLS for p in (e.get("pool_list") or []))
            ]
            act_cards.sort(key=lambda kv: -(float(kv[1].get("rate_pct") or 0)))
            ordered.extend(act_cards)
        # 兜底：不在 ACT_NAMES 里的卡
        seen = {k for k, _ in ordered}
        ordered.extend((k, e) for k, e in self.catalog.items() if k not in seen)

        if cols <= 16:
            cw, gap, label, pad = 240, 20, 70, 40
        else:
            cw, gap, label, pad = 170, 16, 56, 36
        ch = int(cw * 1.43)
        width = pad * 2 + cols * cw + (cols - 1) * gap
        per_page = cols * rows_per_page
        total = len(ordered)
        collected = sum(1 for k in self.catalog if owned.get(k))
        duplicates = sum(max(0, int(v) - 1) for v in owned.values())
        pages: list[Path] = []
        for page_index in range(0, total, per_page):
            chunk = ordered[page_index:page_index + per_page]
            rows = (len(chunk) + cols - 1) // cols
            height = 150 + rows * (ch + label) + 60
            canvas = self.renderer.background(width, height).convert("RGBA")
            head_bottom = self.renderer.header(
                canvas,
                "VSinger 收藏集 · 我的图鉴",
                f"{name}　已收集 {collected}/{total}　重复 {duplicates} 张",
                badge=f"{collected}/{total}",
                badge_sub="收集进度",
                progress=collected / total if total else 0,
            )
            y0 = max(head_bottom, 150)
            for i, (key, entry) in enumerate(chunk):
                cx = pad + (i % cols) * (cw + gap)
                cy = y0 + (i // cols) * (ch + label)
                self.renderer.card(
                    canvas, (cx, cy), entry, self._thumb(key),
                    owned=int(owned.get(key, 0) or 0),
                    laser=int(laser.get(key, 0) or 0),
                    size=(cw, ch),
                )
            self.renderer.footer(canvas, f"VSinger 收藏集 · 图鉴 {page_index // per_page + 1}/{max(1, (total + per_page - 1) // per_page)}")
            out = self.cache_dir / f"handbook_{uuid.uuid4().hex}.jpg"
            canvas.convert("RGB").save(out, quality=88)
            pages.append(out)
        return pages

    def _render_handbook(self, uid: str, name: str, owned: dict, laser: dict, wallpaper_note: str = "",
                         page_cards: int = 60, cols: int = 6) -> list[Path]:
        """按系列/池分组渲染图鉴，超长自动分页。"""
        groups: dict[str, dict[str, list[tuple[str, dict]]]] = {}
        for key, entry in self.catalog.items():
            pools = [p for p in entry.get("pool_list") or [] if p and p not in EXCLUDE_POOLS] or [p for p in entry.get("pool_list") or [] if p]
            if not pools:
                continue
            act = ACT_NAMES.get(str(entry.get("act_id") or ""), f"act {entry.get('act_id')}")
            groups.setdefault(act, {}).setdefault(pools[0], []).append((key, entry))

        # 列数多 → 横图（官方机器人一次只有 5 条消息，宽图能少分页）
        if cols <= 8:
            cw, gap, label, pad = 180, 14, 66, 40
        elif cols <= 16:
            cw, gap, label, pad = 200, 18, 64, 40
        else:
            cw, gap, label, pad = 170, 16, 56, 36
        ch = int(cw * 1.43)
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
                if current and budget + len(cards) > page_cards:
                    pages.append(current)
                    current, budget = [], 0
                current.append((act, pool, cards))
                budget += len(cards)
                if budget >= page_cards:
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

    @staticmethod
    def _strip_at_prefix(event: AstrMessageEvent) -> None:
        """把消息开头的 [At:xxx] 去掉（官方群 @ 机器人后再发指令）。"""
        import re as _re
        text = str(getattr(event, "message_str", "") or "")
        cleaned = _re.sub(r"^(?:\s*\[At:[^\]]+\])+\s*", "", text)
        if cleaned != text:
            event.message_str = cleaned
            try:
                event.is_at_or_wake_command = True
            except Exception:
                pass

    @filter.regex(r"^\s*\[At:[^\]]+\]\s*/?(?:抽卡|卡池|换池|图鉴|获取卡|查积分|摸鱼|蹭饭|每日红包|打工状态|打工)")
    async def cmd_at_dispatch(self, event: AstrMessageEvent):
        """兜底入口：只处理「[At:机器人] /指令」，处理完立刻 stop_event 防重复。"""
        original = str(getattr(event, "message_str", "") or "")
        self._strip_at_prefix(event)
        if original == str(getattr(event, "message_str", "") or ""):
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text.startswith("/"):
            text = "/" + text.lstrip("/")
        # ★★★ 2026-11-07【修复双抽卡】✓
        #   【用户反馈】"机器人还在走双抽奖插件（同一个 /抽卡 回两次）"
        #   【根因】@兜底入口在剥离前缀后【手动调用了一遍 handler】，
        #          而主指令管道也会正常执行 → 同一个指令跑两次 ✗
        #   【修法】这里只负责剥掉 [At:xxx] 前缀并标记为可唤醒指令，
        #          交给 AstrBot 的指令管道统一执行（不再手动分发）✓
        #   注意：不要 yield，也不要 stop_event（否则会把主管道拦掉）。
        return

    def _render_handbook_packed(self, uid: str, name: str, owned: dict, laser: dict,
                                ncol: int = 3, per_row: int = 6, page_cards: int = 90,
                                card_w: int = 160) -> list[Path]:
        """原版横版布局：奖池块打包成 ncol 个竖直列并排，并分页（每页 page_cards 张）。"""
        cw, gap, label, pad = card_w, 12, 58, 36
        ch = int(cw * 1.43)
        col_w = per_row * cw + (per_row - 1) * gap
        col_gap = 20
        scale = max(1.0, cw / 140)
        series_h = int(52 * scale)
        pool_h = int(46 * scale)
        series_font = max(24, int(cw * 0.22))
        pool_font = max(20, int(cw * 0.16))
        count_font = max(18, int(cw * 0.12))

        # 1) 组装块
        blocks: list[dict] = []
        for act_id, act_name in ACT_NAMES.items():
            act_cards = [
                (k, e) for k, e in self.catalog.items()
                if str(e.get("act_id")) == act_id
                and any(p and p not in EXCLUDE_POOLS for p in (e.get("pool_list") or []))
            ]
            if not act_cards:
                continue
            pools: dict[str, list[tuple[str, dict]]] = {}
            for k, e in act_cards:
                pool = next((p for p in (e.get("pool_list") or []) if p and p not in EXCLUDE_POOLS), "其他")
                pools.setdefault(pool, []).append((k, e))
            first = True
            for pool, cards in pools.items():
                cards.sort(key=lambda kv: -(float(kv[1].get("rate_pct") or 0)))
                rows = (len(cards) + per_row - 1) // per_row
                blocks.append({
                    "act": act_name, "series": first, "pool": pool, "cards": cards,
                    "h": (series_h if first else 0) + pool_h + rows * (ch + label) + 8,
                })
                first = False

        # 2) 按卡数分页（块不拆）
        pages: list[list[dict]] = []
        cur: list[dict] = []
        cur_cards = 0
        for b in blocks:
            if cur and cur_cards + len(b["cards"]) > page_cards:
                pages.append(cur)
                cur, cur_cards = [], 0
            cur.append(b)
            cur_cards += len(b["cards"])
        if cur:
            pages.append(cur)

        collected = sum(1 for k in self.catalog if owned.get(k))
        total = len(self.catalog)
        duplicates = sum(max(0, int(v) - 1) for v in owned.values())
        outputs: list[Path] = []
        for page_index, page_blocks in enumerate(pages, 1):
            # 3) 页内打包成 ncol 列
            total_h = sum(b["h"] for b in page_blocks)
            target = max(400, total_h / max(1, ncol))
            cols: list[list[dict]] = []
            col: list[dict] = []
            col_h = 0
            for b in page_blocks:
                if col and col_h + b["h"] > target and len(cols) < ncol - 1:
                    cols.append(col)
                    col, col_h = [], 0
                col.append(b)
                col_h += b["h"]
            if col:
                cols.append(col)

            width = pad * 2 + len(cols) * col_w + (len(cols) - 1) * col_gap
            height = 150 + max(sum(b["h"] for b in c) for c in cols) + 60
            canvas = self.renderer.background(width, height).convert("RGBA")
            head_bottom = self.renderer.header(
                canvas,
                "VSinger 收藏集 · 我的图鉴",
                f"{name}　已收集 {collected}/{total}　重复 {duplicates} 张"
                + (f"　第 {page_index}/{len(pages)} 页" if len(pages) > 1 else ""),
                badge=f"{collected}/{total}",
                badge_sub="收集进度",
                progress=collected / max(1, total),
            )
            y_top = max(head_bottom, 150)
            for ci, col in enumerate(cols):
                x = pad + ci * (col_w + col_gap)
                y = y_top
                for b in col:
                    if b["series"]:
                        self.renderer.panel(canvas, (x, y, x + col_w, y + series_h - 6), radius=14, alpha=235)
                        d = ImageDraw.Draw(canvas)
                        d.rectangle((x + 12, y + 12, x + 18, y + series_h - 16), fill=ACCENT)
                        d.text((x + 28, y + max(4, (series_h - series_font) // 2 - 4)), b["act"], font=self.fonts.title(series_font), fill=TEXT)
                        y += series_h
                    got = sum(1 for k, _ in b["cards"] if owned.get(k))
                    self.renderer.panel(canvas, (x, y + 1, x + col_w, y + pool_h - 8), radius=12, alpha=225)
                    d = ImageDraw.Draw(canvas)
                    d.text((x + 12, y + max(3, (pool_h - pool_font) // 2 - 6)), _fit(d, b["pool"], self.fonts.body(pool_font), col_w - 150), font=self.fonts.body(pool_font), fill=ACCENT)
                    d.text((x + col_w - 110, y + max(3, (pool_h - count_font) // 2 - 5)), f"{got}/{len(b['cards'])}", font=self.fonts.body(count_font), fill=TEXT_SUB)
                    y += pool_h
                    for i, (key, entry) in enumerate(b["cards"]):
                        cx = x + (i % per_row) * (cw + gap)
                        cy = y + (i // per_row) * (ch + label)
                        self.renderer.card(
                            canvas, (cx, cy), entry, self._thumb(key),
                            owned=int(owned.get(key, 0) or 0),
                            laser=int(laser.get(key, 0) or 0),
                            size=(cw, ch),
                        )
                    y += ((len(b["cards"]) + per_row - 1) // per_row) * (ch + label) + 8
            self.renderer.footer(canvas, f"VSinger 收藏集 · 图鉴 {page_index}/{len(pages)}")
            out = self.cache_dir / f"handbook_{uuid.uuid4().hex}.jpg"
            canvas.convert("RGB").save(out, quality=85)
            outputs.append(out)
        return outputs

    def _laser_image(self, card_hash: str) -> Path | None:
        """镭射款：优先返回 B 站原版镭射 JPG（cards/holo/<hash>.jpg）。"""
        holo = self.cards_dir / "holo" / f"{card_hash}.jpg"
        if holo.is_file():
            return holo
        # 没有原版镭射图时才回退：原图叠角标
        origin = None
        for suffix in (".jpg", ".png", ".webp"):
            candidate = self.cards_dir / f"{card_hash}{suffix}"
            if candidate.is_file():
                origin = candidate
                break
        if origin is None:
            return None
        target = self.cache_dir / f"laser_{card_hash}.jpg"
        if target.is_file():
            return target
        tag = ASSETS / "tags" / "tag_holo.png"
        try:
            base = Image.open(origin).convert("RGBA")
            if tag.is_file():
                holo_tag = Image.open(tag).convert("RGBA")
                hw = int(base.width * 0.36)
                hh = int(holo_tag.height * hw / holo_tag.width)
                holo_tag = holo_tag.resize((hw, hh), Image.LANCZOS)
                base.alpha_composite(holo_tag, ((base.width - hw) // 2, base.height - hh - int(base.height * 0.02)))
            base.convert("RGB").save(target, quality=90)
            return target
        except Exception:
            logger.warning("[vsinger-gacha] 镭射图合成失败：%s", card_hash, exc_info=True)
            return None

    def _video_component(self, card_hash: str):
        """返回动态卡视频组件（优先容器路径），没有视频返回 None。"""
        video = self.videos_dir / f"{card_hash}.mp4"
        if not video.is_file():
            return None
        try:
            if Path("/root/snowluma/data/videos").exists():
                container = f"/app/data/videos/{video.name}"
                return Video(container, path=container)
            b64 = base64.b64encode(video.read_bytes()).decode()
            return Video.fromBase64(b64)
        except Exception:
            logger.warning("[vsinger-gacha] 视频组件构造失败：%s", video.name, exc_info=True)
            return None

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

        title = f"{event.get_sender_name() or '你'} 抽了 {count} 张 | -{need} 分"
        try:
            image = await asyncio.to_thread(self._render_result, rows, title, str(left), "当前积分", 0.0)
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
            if row.get("laser"):
                laser_path = self._laser_image(row["key"])
                if laser_path:
                    origin = laser_path
            if origin:
                content.append(MsgImage.fromFileSystem(str(origin)))
            nodes.append(Node(content=content, name=name, uin=uin))
        if self._is_official(event):
            # 官方被动回复有 5 条上限：结果图已经包含全部信息，原图不再逐张发
            videos_left = self.max_messages - 1
            for row in rows:
                video = self.videos_dir / f"{row['key']}.mp4"
                if video.is_file() and videos_left > 0:
                    videos_left -= 1
                    yield event.chain_result([
                        Plain(f"🎬 {row['entry'].get('card_name', '')} · {TIER_NAME.get(row['tier'], '')}"),
                        Video.fromFileSystem(str(video)),
                    ])
            return

        if nodes and self.forward:
            try:
                yield self._forward(event, nodes)
            except Exception:
                logger.warning("[vsinger-gacha] 合并转发失败，改逐张发送", exc_info=True)
                for row in rows:
                    p = self._thumb(row["key"])
                    if p:
                        yield event.image_result(str(p))
        else:
            # 不支持合并转发的平台（如 QQ 官方机器人）：逐张发
            for row in rows:
                p = None
                for suffix in (".jpg", ".png", ".webp"):
                    candidate = self.cards_dir / f"{row['key']}{suffix}"
                    if candidate.is_file():
                        p = candidate
                        break
                if row.get("laser"):
                    p = self._laser_image(row["key"]) or p   # B 站原版镭射 JPG
                p = p or self._thumb(row["key"])
                if p:
                    yield event.image_result(str(p))

        # 动态卡：只要视频文件存在就单独补发（base64，避免容器读不到宿主路径）
        for row in rows:
            video = self.videos_dir / f"{row['key']}.mp4"
            if not video.is_file():
                continue
            comp = self._video_component(row["key"])
            if comp is None:
                continue
            # ★★★ 2026-11-07【视频改回 qqbot 的原始段发法】✓
            #   【用户反馈】"之前 qqbot 抽到 21MB 动图发得很快，现在发不完"
            #   【根因】组件的 Video 段经 AstrBot 转换后，SnowLuma 侧上传可能被截断 ✗
            #   【修法】直接 bot.call_action 发原始 {type:'video', data:{file: 容器路径}}，
            #          与 qqbot 旧实现一致；失败再退回组件链 ✓
            sent = False
            try:
                bot = getattr(event, "bot", None)
                gid = event.get_group_id()
                uid = event.get_sender_id()
                container = f"/app/data/videos/{row['key']}.mp4"
                if bot is not None:
                    for _ in range(2):
                        try:
                            if gid:
                                await bot.call_action("send_group_msg", {
                                    "group_id": int(gid),
                                    "message": [{"type": "video", "data": {"file": container}}],
                                })
                            else:
                                await bot.call_action("send_private_msg", {
                                    "user_id": int(uid),
                                    "message": [{"type": "video", "data": {"file": container}}],
                                })
                            sent = True
                            break
                        except Exception:
                            logger.warning("[vsinger-gacha] 原始视频段发送失败，重试", exc_info=True)
            except Exception:
                logger.warning("[vsinger-gacha] 原始视频段发送异常", exc_info=True)
            if not sent:
                # OneBot 要求 video 必须单独成链；直接发，不额外发文字
                yield event.chain_result([comp])

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

        if self.forward:
            name, uin = "收藏集", self._self_uin(event)
            nodes = [Node(content=[Plain("\n".join(lines[i:i + 18]))], name=name, uin=uin) for i in range(0, len(lines), 18)]
            try:
                yield self._forward(event, nodes)
                return
            except Exception:
                logger.warning("[vsinger-gacha] 卡池合并转发失败，改文本", exc_info=True)
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
        page_cards = 60
        cols = 6
        # ★★★ 2026-11-07【图鉴改为纯 Python(Pillow) 渲染】✓
        #   【用户需求】"JS 转 Python，彻底去掉 Node 依赖"
        #   【实现】handbook_render.py：16:9 自适应列数、系列/奖池分块、
        #          高清缩略图、角标/镭射/×N、页眉进度徽章（字号自适应）、背景 cover ✓
        #   【兜底】Python 渲染失败 → Node(旧 Koishi CLI) → 内置渲染 ✓
        if not self._is_official(event):
            try:
                import tempfile, importlib
                from . import handbook_render as _hb_mod
                importlib.reload(_hb_mod)   # ★ 每次渲染前重载，改渲染器不用重启 AstrBot
                _py_handbook = _hb_mod.render_handbook
                out_dir = tempfile.mkdtemp(prefix='handbook_py_')
                thumbs = [str(ASSETS / 'thumbs'), '/root/handbook-cards-hi']
                wall = '/root/bg-landscape' if Path('/root/bg-landscape').is_dir() else str(ASSETS / 'wallpapers')
                files = await asyncio.to_thread(
                    _py_handbook, self.catalog, ACT_NAMES, str(uid), name or str(uid),
                    owned, laser, out_dir, str(ASSETS), thumbs, wall,
                )
                if files:
                    if len(files) == 1:
                        yield event.image_result(files[0])
                        return
                    if self.forward and len(files) > 1:
                        nodes = [
                            Node(content=[Plain(f'第 {i}/{len(files)} 页'), MsgImage.fromFileSystem(p)],
                                 name='收藏集', uin=self._self_uin(event))
                            for i, p in enumerate(files, 1)
                        ]
                        yield self._forward(event, nodes)
                        return
                    for p in files[: self.max_messages]:
                        yield event.image_result(p)
                    return
            except Exception:
                logger.warning('[vsinger-gacha] Python 图鉴渲染失败，尝试 Node 回退', exc_info=True)

        # —— 下面保留 Node / 内置渲染作为兜底 ——
        if self._is_official(event):
            pages = await asyncio.to_thread(self._render_handbook_packed, uid, name, owned, laser, 3, 6, 90, 160)
            for p in pages[: self.max_messages]:
                yield event.image_result(str(p))
            return
        try:
            pages = await asyncio.to_thread(self._render_handbook, uid, name, owned, laser, "", page_cards, cols)
            if self._is_official(event):
                # 块切分会让页数略多于估算值；超过 5 条时自动放大每页张数重排
                for _ in range(4):
                    if len(pages) <= self.max_messages:
                        break
                    page_cards = int(page_cards * 1.3) + 10
                    pages = await asyncio.to_thread(self._render_handbook, uid, name, owned, laser, "", page_cards, cols)
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
        if self.forward:
            fname, uin = "收藏集", self._self_uin(event)
            nodes = [
                Node(content=[Plain(f"第 {i}/{len(pages)} 页"), MsgImage.fromFileSystem(str(p))], name=fname, uin=uin)
                for i, p in enumerate(pages, 1)
            ]
            try:
                yield self._forward(event, nodes)
                return
            except Exception:
                logger.warning("[vsinger-gacha] 图鉴合并转发失败，改逐张发送", exc_info=True)
        if self._is_official(event):
            limit = self.max_messages
            for p in pages[:limit]:
                yield event.image_result(str(p))
            if len(pages) > limit:
                yield event.plain_result(f"图鉴较长，已合并为 {limit} 页发送")
            return
        for i, p in enumerate(pages, 1):
            yield event.plain_result(f"第 {i}/{len(pages)} 页")
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
            candidate = self.cards_dir / f"{key}{suffix}"
            if candidate.is_file():
                path = candidate
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
        video = self._video_component(key)
        if video is not None:
            yield event.chain_result([video])

    @filter.command("摸鱼")
    async def cmd_moyu(self, event: AstrMessageEvent):
        """摸鱼：10 分钟一次，+1~4 分。"""
        uid = event.get_sender_id()
        now = int(time.time())
        async with self._lock:
            state = await self._load_state()
            mg = state.setdefault("minigames", {})
            rec = mg.setdefault(uid, {})
            last = int(rec.get("moyu", 0) or 0)
            if now - last < 600:
                wait = 600 - (now - last)
                yield event.plain_result(f"🐟 刚摸过了，{wait//60} 分 {wait%60} 秒后再来")
                return
            gain = random.randint(1, 4)
            rec["moyu"] = now
            await self._save_state(state)
            left = await self._add_points(uid, gain)
        yield event.plain_result(f"🐟 摸了会鱼：+{gain} 分（当前 {left}）")

    @filter.command("蹭饭")
    async def cmd_eat(self, event: AstrMessageEvent):
        """蹭饭：30 分钟一次，+3~8 分。"""
        uid = event.get_sender_id()
        now = int(time.time())
        async with self._lock:
            state = await self._load_state()
            mg = state.setdefault("minigames", {})
            rec = mg.setdefault(uid, {})
            last = int(rec.get("eat", 0) or 0)
            if now - last < 1800:
                wait = 1800 - (now - last)
                yield event.plain_result(f"🍚 刚蹭过饭了，{wait//60} 分钟后再来")
                return
            gain = random.randint(3, 8)
            rec["eat"] = now
            await self._save_state(state)
            left = await self._add_points(uid, gain)
        yield event.plain_result(f"🍚 蹭到一顿饭：+{gain} 分（当前 {left}）")

    @filter.command("每日红包")
    async def cmd_bonus(self, event: AstrMessageEvent):
        """每日红包：每天一次，+5~20 分。"""
        uid = event.get_sender_id()
        today = time.strftime("%Y-%m-%d")
        async with self._lock:
            state = await self._load_state()
            mg = state.setdefault("minigames", {})
            rec = mg.setdefault(uid, {})
            if rec.get("bonus_day") == today:
                yield event.plain_result("🧧 今天已经领过红包啦，明天再来")
                return
            gain = random.randint(5, 20)
            rec["bonus_day"] = today
            await self._save_state(state)
            left = await self._add_points(uid, gain)
        yield event.plain_result(f"🧧 每日红包：+{gain} 分（当前 {left}）")

    @filter.command("打工")
    async def cmd_work(self, event: AstrMessageEvent):
        """打工：开始/结算（12 小时最多 200 分）。"""
        uid = event.get_sender_id()
        now = int(time.time())
        async with self._lock:
            state = await self._load_state()
            mg = state.setdefault("minigames", {})
            rec = mg.setdefault(uid, {})
            start = int(rec.get("work_start", 0) or 0)
            if not start:
                rec["work_start"] = now
                await self._save_state(state)
                yield event.plain_result("💼 开始打工：12 小时最多 200 分，再发 /打工 结算并停止")
                return
            elapsed = min(12 * 3600, max(0, now - start))
            gain = min(200, int(elapsed / (12 * 3600) * 200))
            rec["work_start"] = 0
            await self._save_state(state)
            if gain <= 0:
                yield event.plain_result("💼 结算：还不到 1 分，下次多挂一会儿吧")
                return
            left = await self._add_points(uid, gain)
        yield event.plain_result(f"💼 结算：+{gain} 分（工时 {elapsed//3600} 小时 {elapsed%3600//60} 分，当前 {left}）")

    @filter.command("打工状态")
    async def cmd_work_status(self, event: AstrMessageEvent):
        """查看打工进度。"""
        uid = event.get_sender_id()
        state = await self._load_state()
        rec = (state.get("minigames") or {}).setdefault(uid, {})
        start = int(rec.get("work_start", 0) or 0)
        if not start:
            yield event.plain_result("💼 没在打工：发 /打工 开始")
            return
        elapsed = min(12 * 3600, max(0, int(time.time()) - start))
        gain = min(200, int(elapsed / (12 * 3600) * 200))
        yield event.plain_result(
            f"💼 已打工 {elapsed//3600} 小时 {elapsed%3600//60} 分，预计 {gain} 分（上限 200）"
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
            "/获取卡 <卡名>    取出已拥有的卡（动态卡附 mp4）",
            "/打卡 /查积分     打卡赚积分（azuresignin）",
            "/摸鱼 /蹭饭 /每日红包   小额积分",
            "/打工 /打工状态   挂机攒分（12h 上限 200）",
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
