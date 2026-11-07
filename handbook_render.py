# -*- coding: utf-8 -*-
"""VSinger 收藏集 · 图鉴渲染器（Python/Pillow 版，移植自 Koishi 横版实现）

特点：
- 16:9 横屏：按内容自动推列数，画布补边到 16:9
- 系列/奖池分块，卡片网格；高清缩略图；大/小隐藏 + 镭射角标
- 页眉：音符徽标 + 标题 + 收集进度徽章（字号自适应防出框）+ 进度条
- 背景：等比 cover 铺满 + 白蒙层
"""
from __future__ import annotations

import json
import math
import os
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------- 主题 ----------------
COLOR = {
    'paper': '#ffffff',
    'text': '#1f2a3a',
    'textSub': '#5d6b80',
    'textDim': '#8b98ab',
    'accent': '#4a90d9',
    'panel': (255, 255, 255, 209),
    'panelBorder': (120, 160, 210, 71),
    'cardBg': '#f2f5fa',
}
TIER = {
    40: {'name': '大隐藏', 'color': '#e0607a', 'tag': 'tag_40.webp'},
    30: {'name': '小隐藏', 'color': '#e09a3c', 'tag': 'tag_30.webp'},
    20: {'name': '稀有', 'color': '#4a90d9', 'tag': None},
    10: {'name': '普通', 'color': '#8b98ab', 'tag': None},
}
EXCLUDE_POOLS = {'洛天依福利卡池', '全部奖池'}

# 布局常量（与 Koishi 版一致）
CW, GAP, LABEL, PAD, COLGAP = 160, 12, 58, 36, 20
CH = round(CW * 1.43)
PER_ROW = 6
COLW = PER_ROW * CW + (PER_ROW - 1) * GAP
SERIES_H, POOL_H = 52, 46
PAGE_CARDS = 160
RATIO = 16 / 9


def _tier(v: dict) -> int:
    sc = int(v.get('card_scarcity') or 10)
    return 40 if sc == 40 else 30 if sc == 30 else 20 if sc == 20 else 10


def _fit(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    text = str(text or '')
    try:
        if draw.textlength(text, font=font) <= max_w:
            return text
        t = text
        while t and draw.textlength(t + '…', font=font) > max_w:
            t = t[:-1]
        return t + '…'
    except Exception:
        return text[:30]


def _rounded(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new('L', img.size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle((0, 0, img.size[0] - 1, img.size[1] - 1), radius=radius, fill=255)
    out = img.convert('RGBA')
    out.putalpha(mask)
    return out


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    iw, ih = img.size
    s = max(w / iw, h / ih)
    nw, nh = max(1, round(iw * s)), max(1, round(ih * s))
    im = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return im.crop((left, top, left + w, top + h))


def _panel(draw, box, radius, fill, outline=None):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline or COLOR['panelBorder'], width=1)


def _note_icon(R: int) -> Image.Image:
    """半径 R 的蓝色圆 + 白色音符（k=R*0.52，与 Koishi 一致）"""
    size = R * 2
    im = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((0, 0, size - 1, size - 1), fill=COLOR['accent'])
    k = R * 0.52
    cx = cy = R
    hx, hy = cx - k * 0.34, cy + k * 0.52
    d.ellipse((hx - k * 0.42, hy - k * 0.32, hx + k * 0.42, hy + k * 0.32), fill='white')
    sx = hx + k * 0.36
    d.rectangle((sx - k * 0.07, cy - k * 0.62, sx + k * 0.07, hy), fill='white')
    d.polygon([(sx, cy - k * 0.62), (sx + k * 0.62, cy - k * 0.48), (sx + k * 0.46, cy - k * 0.10), (sx, cy - k * 0.34)], fill='white')
    return im


def render_handbook(catalog: dict, act_names: dict, uid: str, name: str,
                    owned: dict, laser: dict, out_dir: str,
                    assets_dir: str, thumbs_dirs: list[str], wallpaper_dir: str | None) -> list[str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    assets = Path(assets_dir)
    font_title = assets / 'fonts' / 'wenkai.ttf'
    font_body = assets / 'fonts' / 'yozai.ttf'
    f_title = ImageFont.truetype(str(font_title), 30)
    f_body = ImageFont.truetype(str(font_body), 16)

    # 分组
    acts: dict[str, dict[str, list]] = {}
    for h, v in catalog.items():
        pools = [p for p in (v.get('pool_list') or []) if p and p not in EXCLUDE_POOLS]
        if not pools:
            continue
        act_id = str(v.get('act_id') or '')
        act = act_names.get(act_id) or v.get('act_name') or f'系列 {act_id}'
        pool = str(pools[0])
        acts.setdefault(act, {}).setdefault(pool, []).append((h, v))

    blocks = []
    for act, pools in acts.items():
        first = True
        for pool, lst in pools.items():
            lst.sort(key=lambda x: -float(x[1].get('rate_pct') or 0))
            rows = max(1, math.ceil(len(lst) / PER_ROW))
            hgt = (SERIES_H if first else 0) + POOL_H + rows * (CH + LABEL) + 8
            blocks.append({'act': act, 'pool': pool, 'list': lst, 'series': first, 'h': hgt})
            first = False

    # 分页（块不拆）
    pages, cur, cnt = [], [], 0
    for b in blocks:
        if cur and cnt + len(b['list']) > PAGE_CARDS:
            pages.append(cur); cur = []; cnt = 0
        cur.append(b); cnt += len(b['list'])
    if cur:
        pages.append(cur)

    total = len(catalog)
    collected = sum(1 for h in catalog if owned.get(h))
    dup = sum(max(0, int(n or 0) - 1) for n in owned.values())

    # 素材加载
    tag_imgs = {}
    for t in TIER.values():
        if t['tag']:
            p = assets / 'tags' / t['tag']
            if p.is_file():
                tag_imgs[t['tag']] = Image.open(p).convert('RGBA')
    holo = assets / 'tags' / 'tag_holo.png'

    thumb_cache: dict[str, Image.Image | None] = {}

    def thumb(h: str):
        if h in thumb_cache:
            return thumb_cache[h]
        im = None
        for d in thumbs_dirs:
            for ext in ('.jpg', '.png', '.webp'):
                p = Path(d) / f'{h}{ext}'
                if p.is_file():
                    try:
                        im = Image.open(p).convert('RGB')
                    except Exception:
                        im = None
                    break
            if im:
                break
        thumb_cache[h] = im
        return im

    walls = []
    if wallpaper_dir:
        wd = Path(wallpaper_dir)
        if wd.is_dir():
            walls = [p for p in wd.iterdir() if p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')]

    files = []
    for pi, page in enumerate(pages, 1):
        total_h = sum(b['h'] for b in page)
        best_n, best_err = 1, 1e18
        for n in range(1, 15):
            w = PAD * 2 + n * COLW + (n - 1) * COLGAP
            h = 380 + total_h / n
            err = abs(w / h - RATIO)
            if err < best_err:
                best_err, best_n = err, n
        n = best_n
        cols = [[] for _ in range(n)]
        heights = [0] * n
        for b in page:
            i = min(range(n), key=lambda k: heights[k])
            cols[i].append(b); heights[i] += b['h']

        wc = PAD * 2 + n * COLW + (n - 1) * COLGAP
        hc = 380 + max(heights)
        W, H = wc, hc
        if wc / hc > RATIO:
            H = round(wc / RATIO)
        else:
            W = round(hc * RATIO)

        canvas = Image.new('RGB', (W, H), (238, 243, 250))
        if walls:
            try:
                wall = Image.open(random.choice(walls)).convert('RGB')
                canvas.paste(_cover(wall, W, H), (0, 0))
            except Exception:
                pass
        veil = Image.new('RGBA', (W, H))
        dv = ImageDraw.Draw(veil)
        for y in range(H):
            t = y / max(1, H - 1)
            a = int(255 * (0.62 * (1 - t) + 0.46 * t))
            dv.line([(0, y), (W, y)], fill=(255, 255, 255, a))
        canvas = Image.alpha_composite(canvas.convert('RGBA'), veil).convert('RGB')
        draw = ImageDraw.Draw(canvas)

        # 页眉
        R = max(30, min(180, round(34 * max(1.0, min(2.6, W / 1600)))))
        canvas.paste(_note_icon(R), (PAD, 26), _note_icon(R))
        head_y = 26
        title_size = max(30, R)
        f_title_big = ImageFont.truetype(str(font_title), title_size)
        subtitle_size = max(16, round(R * 0.5))
        f_sub = ImageFont.truetype(str(font_body), subtitle_size)
        badge_w = max(180, min(760, round(W * 0.17)))
        badge_h = round(badge_w * 0.5)
        reserved = (badge_w + 24) if True else 24
        title = _fit(draw, 'VSinger 收藏集 · 我的图鉴', f_title_big, PAD + W - (R * 2 + 18) - reserved)
        draw.text((PAD + R * 2 + 18, head_y + max(0, R - 24)), title, font=f_title_big, fill=COLOR['text'])
        draw.text((PAD + R * 2 + 18, head_y + max(40, R * 2 - 8)),
                  f'{name}　已收集 {collected}/{total}　重复 {dup} 张' + (f'　第 {pi}/{len(pages)} 页' if len(pages) > 1 else ''),
                  font=f_sub, fill=COLOR['textSub'])
        # 徽章
        bx = PAD + (W - PAD * 2) - badge_w
        _panel(draw, (bx, head_y - 4, bx + badge_w, head_y - 4 + badge_h), max(14, round(badge_h * 0.22)), (255, 255, 255, 242))
        badge_text = f'{collected}/{total}'
        fs = round(badge_h * 0.46)
        while fs > 12 and draw.textlength(badge_text, font=ImageFont.truetype(str(font_body), fs)) > badge_w * 0.8:
            fs -= 2
        f_badge = ImageFont.truetype(str(font_body), fs)
        draw.text((bx + badge_w * 0.1, head_y + badge_h * 0.06 - 4), badge_text, font=f_badge, fill=COLOR['accent'])
        f_badge_sub = ImageFont.truetype(str(font_body), round(badge_h * 0.2))
        draw.text((bx + badge_w * 0.1, head_y + badge_h * 0.6 - 4), '收集进度', font=f_badge_sub, fill=COLOR['textSub'])
        pb_h = max(5, round(badge_h * 0.08))
        pby = head_y + badge_h - pb_h - 8
        draw.rounded_rectangle((bx + badge_w * 0.08, pby, bx + badge_w * 0.92, pby + pb_h), radius=pb_h / 2, fill=(214, 228, 244))
        if total:
            pw = badge_w * 0.84 * (collected / total)
            if pw > 6:
                draw.rounded_rectangle((bx + badge_w * 0.08, pby, bx + badge_w * 0.08 + pw, pby + pb_h), radius=pb_h / 2, fill=COLOR['accent'])
        head_bottom = max(head_y + R * 2 + 28, head_y - 4 + badge_h + 24)
        y_top = max(head_bottom, 150)

        f_name = ImageFont.truetype(str(font_body), 17)
        f_meta = ImageFont.truetype(str(font_body), 13)
        f_series = ImageFont.truetype(str(font_title), 30)
        f_pool = ImageFont.truetype(str(font_body), 22)

        for ci, col in enumerate(cols):
            x = PAD + ci * (COLW + COLGAP)
            y = y_top
            for b in col:
                if b['series']:
                    _panel(draw, (x, y, x + COLW, y + SERIES_H - 6), 14, (255, 255, 255, 230))
                    draw.rounded_rectangle((x + 12, y + 12, x + 18, y + SERIES_H - 22), radius=3, fill=COLOR['accent'])
                    draw.text((x + 26, y + 8), _fit(draw, b['act'], f_series, COLW - 30), font=f_series, fill=COLOR['text'])
                    y += SERIES_H
                got = sum(1 for h, _ in b['list'] if owned.get(h))
                _panel(draw, (x, y + 1, x + COLW, y + POOL_H - 8), 12, (255, 255, 255, 217))
                draw.text((x + 12, y + 8), _fit(draw, b['pool'], f_pool, COLW - 130), font=f_pool, fill=COLOR['accent'])
                draw.text((x + COLW - 12 - draw.textlength(f'{got}/{len(b["list"])}', font=f_meta), y + 11),
                          f'{got}/{len(b["list"])}', font=f_meta, fill=COLOR['textDim'])
                y += POOL_H
                for i, (h, v) in enumerate(b['list']):
                    cx = x + (i % PER_ROW) * (CW + GAP)
                    cy = y + (i // PER_ROW) * (CH + LABEL)
                    im = thumb(h)
                    if im:
                        card = _cover(im, CW, CH)
                    else:
                        card = Image.new('RGB', (CW, CH), (226, 233, 242))
                    card = _rounded(card, 10)
                    canvas.paste(card, (cx, cy), card)
                    tier = _tier(v)
                    tc = TIER[tier]['color']
                    owned_flag = bool(owned.get(h))
                    # 描边：档位色（未拥有更细更淡）
                    draw.rounded_rectangle((cx + 1, cy + 1, cx + CW - 2, cy + CH - 2), radius=9,
                                           outline=tc if owned_flag else (150, 165, 185), width=3 if owned_flag else 2)
                    tag = TIER[tier].get('tag')
                    if tag and tag in tag_imgs:
                        # ★ 角标：宽 = 卡宽 0.52，贴左上角并裁进圆角
                        t = tag_imgs[tag]
                        tw = round(CW * 0.52)
                        th = round(t.height * tw / t.width)
                        tagimg = t.resize((tw, th), Image.LANCZOS)
                        layer = Image.new('RGBA', (CW, CH), (0, 0, 0, 0))
                        layer.paste(tagimg, (0, 0), tagimg)
                        mask = Image.new('L', (CW, CH), 0)
                        ImageDraw.Draw(mask).rounded_rectangle((0, 0, CW - 1, CH - 1), radius=10, fill=255)
                        layer.putalpha(Image.composite(layer.getchannel('A'), Image.new('L', (CW, CH), 0), mask))
                        canvas.paste(layer, (cx, cy), layer)
                    if laser.get(h) and tier == 10 and holo.is_file():
                        # ★ 镭射：只对普通档，宽 = 卡宽 0.36，居中贴底
                        try:
                            hi = Image.open(holo).convert('RGBA')
                            hw = round(CW * 0.36)
                            hh = round(hi.height * hw / hi.width)
                            himg = hi.resize((hw, hh), Image.LANCZOS)
                            canvas.paste(himg, (cx + (CW - hw) // 2, cy + CH - hh - 6), himg)
                        except Exception:
                            pass
                    n_owned = int(owned.get(h) or 0)
                    if n_owned > 1:
                        # ★ 数量：右上角圆角胶囊 40x22
                        pill = Image.new('RGBA', (40, 22), (0, 0, 0, 0))
                        pd = ImageDraw.Draw(pill)
                        pd.rounded_rectangle((0, 0, 39, 21), radius=8, fill=(31, 42, 58, 209))
                        # ★ 悠哉字体没有 × 字形 → 用 DejaVu；没有就退 xN
                        cnt_text = f'×{n_owned}'
                        f_cnt = None
                        for cand in ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                                     '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
                            if Path(cand).is_file():
                                try:
                                    f_cnt = ImageFont.truetype(cand, 14)
                                    break
                                except Exception:
                                    f_cnt = None
                        if f_cnt is None:
                            cnt_text = f'x{n_owned}'
                            f_cnt = ImageFont.truetype(str(font_body), 14)
                        try:
                            if f_cnt.getlength(cnt_text) > 34:
                                cnt_text = f'x{n_owned}'
                                f_cnt = ImageFont.truetype(str(font_body), 13)
                        except Exception:
                            pass
                        pd.text((4, 2), cnt_text, font=f_cnt, fill='#8cffaa')
                        canvas.paste(pill, (cx + CW - 46, cy + 6), pill)
                    nm = _fit(draw, v.get('card_name') or '', f_name, CW)
                    draw.text((cx + (CW - draw.textlength(nm, font=f_name)) / 2, cy + CH + 8), nm,
                              font=f_name, fill=COLOR['text'])
                    rt = float(v.get('rate_pct') or 0)
                    meta = f'{TIER[tier]["name"]} {rt:g}%'
                    f_meta2 = ImageFont.truetype(str(font_body), 13)
                    draw.text((cx + (CW - draw.textlength(meta, font=f_meta2)) / 2, cy + CH + 40), meta, font=f_meta2, fill=tc)
                y += math.ceil(len(b['list']) / PER_ROW) * (CH + LABEL) + 8

        draw.text((PAD, H - 26), f'VSinger 收藏集 · 图鉴 {pi}/{len(pages)}', font=ImageFont.truetype(str(font_body), 14), fill=COLOR['textDim'])
        out = out_dir / f'handbook_{uid}_{pi}.jpg'
        canvas.save(out, quality=86, optimize=True)
        files.append(str(out))
    return files
