# astrbot_plugin_vsinger_gacha

B 站「VSinger 收藏集」抽卡插件的 AstrBot 移植版：按官方流通概率抽卡，
全部卡池 / 系列 / 单池三级范围，大隐藏与小隐藏保底、档内未拥有优先、
普通卡镭射款。抽卡结果和图鉴由 **Pillow 本地渲染**（浅色玻璃手账风，
标题/正文字体可换），不依赖浏览器，也不请求任何渲染服务。

积分不内置：直接读写打卡插件
[`astrbot_plugin_azuresignin`](https://github.com/Aoi-Karlin/astrbot_plugin_azuresignin)
的余额，所以打卡和抽卡共用一份积分。

## 安装

1. 把本目录放进 AstrBot 的 `data/plugins/`（或用 AstrBot 的插件市场安装）。
2. `pip install -r requirements.txt`（只有 Pillow）。
3. 同时安装并启用 `astrbot_plugin_azuresignin`（积分来源）。
4. 重启 AstrBot，在 WebUI 里启用插件。

插件自带：卡池数据、423 张缩略图、角标、背景图、两套字体，
**开箱即可抽卡与看图鉴**；原图（1242×1863）和动态卡 mp4 需要额外下载：

```bash
pip install pillow
python tools/prepare_cards.py --dest <AstrBot>/data/plugin_data/astrbot_plugin_vsinger_gacha/cards
```

原图放进 `cards/`（按卡 hash 命名），mp4 放进 `videos/`（同名）。缺原图时
抽卡结果与图鉴会自动用缩略图，不影响使用。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/抽卡 [1-10]` | 抽卡，每抽 10 积分；先出结果图，再把原图打包成合并转发 |
| `/卡池` | 全部抽取范围（00 全部 / 系列 / 单池），`▸` 标出当前范围 |
| `/换池 序号` | 切换本群范围；`00` 全部，也支持写系列名或池名 |
| `/图鉴` | 本人图鉴长图，未收集的卡灰度显示，超长自动分页 |
| `/获取卡 <卡名>` | 发送已拥有卡的原图 |
| `/抽卡帮助` | 玩法说明 |
| `/查积分 [@某人]` | 查询积分 |
| `/设积分 <数量> [@某人]` | 管理员 |
| `/加积分 <数量> [@某人]` | 管理员，负数即扣除 |
| `/给卡 <@某人> <卡名> [数量]` | 管理员直接发卡；普通用户从自己库存转赠 |

## 规则

- 收集进度**全局共用**（换群不丢）；卡池选择**按群独立**。
- 抽取：先按档位总质量选档 → 档内**未拥有优先** → 按 `rate_pct` 加权；
  大隐藏 400 / 小隐藏 40 保底（按「群 + 范围」独立计数）。
- 镭射款：**普通档** 15% 概率，单独计数，图鉴里显示镭射标。
- 动态卡：只要 `videos/<hash>.mp4` 存在就单独补发，不限制档位。
- 概率沿用原平台插件的实现（档内按 `rate_pct` 加权），不是逐池独立复刻；
  不同范围（全部/系列/单池）的分布会有差异。

## 配置（可选）

在 AstrBot 插件配置里可覆盖：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `cardsDir` | `data/.../cards` | 原图目录 |
| `videosDir` | `data/.../videos` | 动态卡目录 |
| `wallpaperDir` | 插件 `assets/wallpapers` | 背景图目录，随机取一张并模糊 |
| `titleFont` | `wenkai.ttf` | 标题字体（`assets/fonts` 下的文件名） |
| `bodyFont` | `yozai.ttf` | 正文字体 |

内置字体：`wenkai.ttf`（霞鹜文楷）、`yozai.ttf`（悠哉字体）、
`smiley.ttf`（得意黑）、`chill.otf`（寒蝉全圆体 Heavy），均为 OFL 授权子集。

## 字体与素材授权

- 字体：SIL Open Font License 1.1（子集化后随插件分发）。
- 卡图与动态卡：版权归 Bilibili / 版权方所有，仅用于个人收藏展示，
  插件与仓库不附带原图，需自行下载。

## 许可

代码 MIT。
