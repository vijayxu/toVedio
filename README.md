# toVedio

将**小说文本**或**模型产出的分镜/剧本 JSON** 串成一条影视化短视频流水线，最终输出 **MP4**。对外只保留两种成片模式与两种画风：

| 模式 | 含义 |
|------|------|
| `t2v` | 文生视频：阿里云百炼 **Wan2.6-T2V**（`BAILIAN_WAN_T2V_MODEL`，默认 `wan2.6-t2v`） |
| `l2v` | 文生图（百炼 **Wan2.6-T2I**）→ 图生视频（百炼，默认 `wan2.6-i2v-flash`；`--l2v-model` 或 `BAILIAN_WAN_L2V_MODEL` 可改，如 `wan2.7-i2v`）。若提供定妆图（`--character-sheet-dir`），有定妆镜头自动切换 **Wan2.6-R2V**，跳过 T2I 步骤直接生成视频。 |

| 风格 | 含义 |
|------|------|
| `anime` | 动漫风提示词 |
| `real` | 现实风提示词 |

**默认**：`--mode l2v`、`--style real`（与 CLI 一致）。

> **声音生成**：T2V 与 L2V 均通过 prompt 中的**万相声音公式**（人声台词 + 情绪语速 + 音效 + BGM）驱动模型原生生成带声视频，无需额外 TTS 步骤。分镜 JSON 的 `lines[].emotion`、`lines[].speech_rate`、`lines[].sfx_note`、`shot.bgm_note` 字段由 MiniMax 分镜生成时自动填写。无旁白，只有影视化角色对白与背景音，由视频模型原生输出声画同步效果。

## 仓库结构

```
toVedio/
├── run_tovedio.py          # 根目录入口：优先走 src/tovedio/cli.py（兼容回退 demo）
├── requirements.txt
├── .env                    # 环境变量配置（本地唯一配置源）
├── artifacts/              # 默认「过程产物」根目录（见下节；可用 TOVEDIO_ARTIFACT_DIR / --artifact-dir 改路径）
│   ├── tmp/                # 每跑一次生成的临时工作区（帧、片段等），成功后删除
│   ├── staging/            # 极短生命周期辅助文件（如 ffmpeg concat 列表），用完即删
│   └── exports/            # 建议：显式落盘结果放这里（-o、--save-storyboard 等由你指定路径）
├── docs/
│   ├── storyboard.schema.json
│   └── production_bible.schema.json
├── examples/               # 试跑素材（snowy_arc/novel.txt）；分步说明亦见 examples/README.md
│   └── snowy_arc/          # 古偶雪夜救归示例小说 novel.txt
├── src/
│   └── tovedio/                # 主 Python 包
│       ├── cli.py            # 命令行
│       ├── paths.py          # 仓库根、artifacts/tmp|staging 解析
│       ├── pipeline.py       # 分镜、ffmpeg 合成、主流程（三路并发：R2V / T2I+I2V）
│       ├── minimax_client.py # MiniMax（分镜、文生图、制作圣经）
│       ├── illustration.py   # 配图后端（百炼 T2I，异步提交+轮询）
│       ├── storyboard_render.py # 分镜 → 图/视频 prompt（含万相声音公式）
│       └── video_t2v_bailian_kling.py # 百炼视频任务（T2V / L2V / R2V）
└── demo/
    └── __init__.py           # 历史目录占位（核心实现已迁移到 src/tovedio）
```

## 环境要求

- Python **>= 3.10**
- **ffmpeg** / **ffprobe** 在 PATH 中可用
- 能访问所选云 API（百炼；`l2v` 及分镜相关能力还需 MiniMax）

## 安装

```bash
cd /path/to/toVedio
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

在仓库根目录配置 `.env`，**勿提交** `.env`。

## 过程产物目录

- 默认根目录为仓库下的 **`artifacts/`**（实现见 `src/tovedio/paths.py`）。
- **`artifacts/tmp/`**：L2V、配图幻灯等流水线的工作目录；正常结束会 `rmtree`，异常退出时可能残留，可手动清理。
- **`artifacts/staging/`**：concat 列表等小文件；处理完会删除。
- **`artifacts/exports/`**：仓库内预留的「建议输出区」，**不会**自动写入；推荐把成片与 JSON 指到此处，例如  
  `-o artifacts/exports/run1.mp4`、`--save-storyboard artifacts/exports/run1_storyboard.json`。
- **L2V 断点缓存**：使用 `-o path/to/name.mp4` 时，默认在同目录创建 **`name.l2v_cache/`**（`manifest.json` + `seg_0000.mp4`…），下次相同分镜与参数会从下一镜继续；改分镜/模型/风格等会自动清空旧缓存。`--no-l2v-resume` 可强制整片重跑。
- 覆盖路径：**环境变量 `TOVEDIO_ARTIFACT_DIR`**（相对路径按**仓库根**解析）或 **`py -3 run_tovedio.py --artifact-dir D:\runs\tovedio ...`**。
- `.gitignore` 已忽略 `artifacts/tmp/` 与 `artifacts/staging/`，避免把临时文件提交进 Git。

## 快速开始（根目录）

```bash
# 自检：按当前 --mode 检查密钥（t2v 不要求 MINIMAX；l2v 要求两者）
py -3 run_tovedio.py --self-check --mode t2v
py -3 run_tovedio.py --self-check --mode l2v

# 文生视频（百炼 T2V；novel.txt 请换为你的 UTF-8 小说路径）
py -3 run_tovedio.py novel.txt -o artifacts/exports/out.mp4 --mode t2v --style real

# 文生图 → 图生视频（默认模式，MiniMax + 百炼 L2V）
py -3 run_tovedio.py novel.txt -o artifacts/exports/out.mp4 --mode l2v --style anime

# 使用仓库内自带古偶示例（分步完整命令见本文「从小说到成片」一节）
py -3 run_tovedio.py examples/snowy_arc/novel.txt -o artifacts/exports/snowy_arc.mp4 --mode l2v --style real
```

也可直接按模块路径运行（第一个参数为小说路径，相对路径相对当前工作目录）：

```bash
py -3 -m src.tovedio.cli .\novel.txt -o .\artifacts\exports\out.mp4 --mode l2v --style real
```

## 从小说到成片（分步完整命令）

以下均在**仓库根目录**执行。示例小说路径为 `examples/snowy_arc/novel.txt`，请按需替换为你的 UTF-8 文本路径；输出路径也可自定义。

### 步骤 0（可选）— 环境自检

检查 Python、ffmpeg/ffprobe、`l2v` 所需的 MiniMax 与百炼密钥（不读小说、不出片）。

```bash
py -3 run_tovedio.py --self-check --mode l2v
```

### 步骤 1（可选）— 制作圣经

从小说生成制作圣经 JSON（角色表、主场景 `locations`、全片视觉锁定）。**仅 MiniMax**，不成分镜、不出片。

```bash
py -3 run_tovedio.py --production-bible-only -i examples/snowy_arc/novel.txt --save-production-bible artifacts/exports/snowy_arc_bible.json --style real
```

### 步骤 2 — 生成分镜 JSON

**未使用制作圣经：**

```bash
py -3 run_tovedio.py --storyboard-only --input examples/snowy_arc/novel.txt --save-storyboard artifacts/exports/snowy_arc_storyboard.json --style real
```

**已在步骤 1 生成圣经时（推荐）：**

```bash
py -3 run_tovedio.py --storyboard-only --input examples/snowy_arc/novel.txt --production-bible artifacts/exports/snowy_arc_bible.json --save-storyboard artifacts/exports/snowy_arc_storyboard.json --style real
```

**仅 MiniMax**，不配图、不调百炼、不 TTS。生成后可编辑 `snowy_arc_storyboard.json`。

### 步骤 3（可选）— 校验分镜与原文

零 API，本地 Schema 与启发式对照。

```bash
py -3 run_tovedio.py --validate-storyboard artifacts/exports/snowy_arc_storyboard.json --input examples/snowy_arc/novel.txt
```

### 步骤 4（可选）— 角色定妆三视图

按分镜（或制作圣经）JSON 中的 `characters` 生成每角色三张定妆 PNG（正面/左侧面/背面全身站姿）。默认使用**百炼文生图**。

```bash
py -3 run_tovedio.py --character-sheets-only -i artifacts/exports/snowy_arc_storyboard.json --save-character-dir artifacts/exports/char_sheets --style real
```

输出文件：
```
char_sheets/
  {角色id}_costume_sheet.png       ← 正面全身像（主参考）
  {角色id}_costume_sheet_side.png  ← 左侧面全身像
  {角色id}_costume_sheet_back.png  ← 背面全身像
  character_sheets_manifest.json
```

**人物与定妆（R2V 路径）：** 成片使用 `--character-sheet-dir` 且目录内已有 `*_costume_sheet.png` 时，**有定妆图的镜头自动走 wan2.6-R2V**（参考生视频），将定妆 PNG 作为参考图直接生成视频，跳过 T2I 步骤；无定妆图的镜头仍走 T2I → I2V 两阶段。两路并发提交、并发轮询，互不阻塞。R2V 保证角色外观与定妆 PNG 一致，避免脸漂。若某镜 `characters_on_screen` 为空，将保持空镜，不自动塞角色。

### 步骤 5 — 从分镜生成成片（L2V）

**仅分镜、无圣经与定妆：**

```bash
py -3 run_tovedio.py --from-storyboard artifacts/exports/snowy_arc_storyboard.json -o artifacts/exports/snowy_arc.mp4 --style real
```

**带上圣经与定妆目录（推荐在已执行步骤 1、4 时使用）：**

```bash
py -3 run_tovedio.py --from-storyboard artifacts/exports/snowy_arc_storyboard.json -o artifacts/exports/snowy_arc.mp4 --style real --production-bible artifacts/exports/snowy_arc_bible.json --character-sheet-dir artifacts/exports/char_sheets
```

默认**百炼文生图 + 百炼图生视频**；声音由万相模型原生生成（对白、音效、BGM 均通过 prompt 描述控制）。更多参数见 `py -3 run_tovedio.py -h`。

### 捷径：小说一步到成片（L2V）

```bash
py -3 run_tovedio.py examples/snowy_arc/novel.txt -o artifacts/exports/snowy_arc.mp4 --mode l2v --style real
```

可选同时保存分镜：`--save-storyboard artifacts/exports/snowy_arc_storyboard.json`；若有圣经/定妆可加 `--production-bible ...`、`--character-sheet-dir ...`。

### 捷径：小说一步文生视频（T2V）

不经过分镜 JSON 与图生视频链路，整段送百炼 Wan。**主要只需百炼**。

```bash
py -3 run_tovedio.py examples/snowy_arc/novel.txt -o artifacts/exports/snowy_arc_t2v.mp4 --mode t2v --style real
```

更细的步骤说明与字段约定见 **`examples/README.md`** 与 **`docs/*.schema.json`**。

## 进阶工作流（CLI 摘要）

除「小说 → 成片」外，还支持拆步与复用 JSON：

| 场景 | 说明 |
|------|------|
| `--storyboard-only` + `--save-storyboard` | 仅从小说生成分镜 JSON（MiniMax），不配图、不百炼 |
| `--validate-storyboard` + `--input` 小说 | 校验分镜 JSON 与原文对照（零 API） |
| `--production-bible-only` + `--save-production-bible` | 仅从小说生成制作圣经（选角/场景等，一次 MiniMax） |
| `-i 小说 -o out.mp4 --production-bible bible.json` | 成片或分镜时锁定制作圣经 |
| `--screenplay-only` + `--save-storyboard` | 无小说：按 `--pitch` 或梗概文件原创剧本并写分镜 JSON |
| `--from-storyboard script.json -o out.mp4` | 从已有分镜/剧本 JSON 直接跑成片（`mode=l2v` 或 `mode=t2v`） |
| `--character-sheets-only` + `--save-character-dir` | 从剧本 JSON 的 `characters` 生成定妆 PNG |
| `--character-sheet-dir` / `TOVEDIO_CHARACTER_SHEET_DIR` | L2V 中：有定妆图的镜头走 **R2V**，无定妆图镜头走 T2I+I2V；两路并发互不阻塞 |
| `--analyze-video out.mp4` | ffprobe 分析成片（零 API） |

常用调参：`--seconds`、`--strict-illustration`、`--no-l2v-chain`、`--l2v-minimal-prompt`、`--rerun-shots`、`--t2v-model` / `--l2v-model`、`--bailian-max-attempts`。详见 `py -3 run_tovedio.py -h`。

说明：`--from-storyboard` 按 JSON 原镜头数执行，不再自动压缩镜头；若某镜 `characters_on_screen` 为空，将保持空镜，不自动塞角色。

文生图模型可用 `--illustration-model NAME` 在命令行覆盖（设置 `BAILIAN_IMAGE_MODEL`，默认 `wan2.6-t2i`）。
文生图后端可用 `--illustration-backend {auto|bailian}` 指定（当前固定走 `bailian`）。

## .env 配置（最少）

```env
# t2v / l2v 都需要
DASHSCOPE_API_KEY=你的百炼密钥

# l2v 分镜生成需要
# MINIMAX_API_KEY=你的MiniMax密钥

# 百炼视频模型（可选，有默认值）
# BAILIAN_WAN_T2V_MODEL=wan2.6-t2v
# BAILIAN_WAN_L2V_MODEL=wan2.6-i2v-flash
# BAILIAN_WAN_R2V_MODEL=wan2.6-r2v

# 百炼文生图模型（可选）
# BAILIAN_IMAGE_MODEL=wan2.6-t2i
```

更多可选项（百炼基址与模型名、重试次数、L2V 链式、配图后端等）见根目录 **`.env`** 注释。

## 说明与约定

- **真视频硬失败**：任一步视频生成失败即终止，不回退为「静图凑成片」。
- `style` 仅通过提示词约束，不同模型遵循程度会有差异。
- 分镜与制作圣经的字段约束见 `docs/*.schema.json`；调用 MiniMax 生成分镜/剧本时，**结构示例 JSON 内联在** `src/tovedio/minimax_client.py` 的 `_STORYBOARD_STRUCTURE_EXAMPLE_JSON`，仓库中不再附带 `storyboard.example.json`。
- Windows 下若缺少中文字体，Pillow 渲染字幕类画面时可能出现方块字；可安装常见黑体/雅黑。
- 配图行为受 `ILLUSTRATION_BACKEND`（`auto` / `bailian`）与 `BAILIAN_IMAGE_MODEL` 影响，详见 `illustration.py` 与 `.env`。
