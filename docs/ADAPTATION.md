# AstrBot 适配说明（必读）

## 1. 运行环境

| 项 | 要求 |
|---|---|
| AstrBot | ≥ 4.29（需 aiocqhttp / qq_official 平台） |
| Python | 3.10+（插件纯 Pillow，无浏览器依赖） |
| 依赖插件 | **astrbot_plugin_azuresignin**（打卡/积分），必须装 |
| 字体 | 插件自带 `assets/fonts/wenkai.ttf`、`yozai.ttf`，无需系统字体 |

## 2. 目录布局（插件 data 目录）

`data/plugin_data/astrbot_plugin_vsinger_gacha/`

```
cards/            # 原图，文件名 = catalog 的 hash（.png/.jpg/.webp）
cards/holo/       # 镭射款 JPG（可选，存在则镭射抽卡发它）
videos/           # 动态卡 mp4，文件名 = hash.mp4
cache/            # 结果图/图鉴临时输出（自动创建）
```

- 缺 `cards/` 时退回插件内置缩略图 `assets/thumbs`（240×343）。
- `cards/` 可以直接软链到已有卡图目录（例：`ln -s /root/qqbot/data/cards2 cards`）。

## 3. 视频发送（OneBot / SnowLuma）

- OneBot 规定 **video 段必须单独成链**，插件已拆成「只发视频」一条。
- SnowLuma 容器只认容器内路径：把 `videos/` 映射进容器的挂载点，
  插件会优先发 `/app/data/videos/<hash>.mp4`（对应宿主 `/root/snowluma/data/videos`）。
- 没有该挂载时回退 base64（大文件可能被协议端拒绝）。

## 4. 平台差异

| 平台 | 行为 |
|---|---|
| aiocqhttp（大号/OneBot） | 结果图 + 合并转发原图/镭射图 + 动态卡 mp4 |
| qq_official（官机） | 横版图鉴压缩到 ≤5 条消息；无合并转发；`[At:xxx] /指令` 由正则兜底 |

## 5. 积分

- 读写同一个 KV：`plugin_id = azure/astrbot_plugin_azuresignin`，键 `user_<uid>.points`。
- 抽卡/摸鱼/蹭饭/红包/打工 都是加减这份余额；不装打卡插件则积分为 0。

## 6. 素材从哪里来

仓库和 Release **不打包** 原图/视频（总计 >2GB），需要自行准备：

```bash
# 已有自编 qqbot 的机器上
ln -s /root/qqbot/data/cards2  <AstrBot>/data/plugin_data/astrbot_plugin_vsinger_gacha/cards
cp -al /root/qqbot/data/cards2/../videos  <AstrBot>/data/plugin_data/.../videos   # 或解压 assets-videos.tar
```

没有现成素材时，`tools/ensure_assets.py` 可以检查并报告缺什么。

## 7. 常见坑

- `message element "video" must be the only segment` → 视频必须单独发送（已处理）。
- 官方 `openid` 会变，长期请用 OneBot（QQ 号）作为用户主键。
- 图鉴是横版多列、按 90 张/页分页；官方平台自动压到 5 页以内。
