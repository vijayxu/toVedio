# 示例内容

本目录与源码、运行产物分离，仅供试跑管线时参考。

## 项目结构速览

本仓库主要目录职责如下（完整说明见根目录 `README.md` 的「仓库结构」）：

| 路径 | 作用 |
|------|------|
| `run_tovedio.py` | 根目录 CLI 入口脚本 |
| `src/tovedio/` | 核心实现（命令行、主流程、模型调用、路径管理等） |
| `examples/` | 示例素材与示例用法（你当前所在目录） |
| `docs/` | JSON Schema（分镜与制作圣经字段约束） |
| `artifacts/` | 过程目录与建议输出目录（如 `exports/`） |
| `.env` | 本地环境变量配置（API Key 与可选参数） |

| 子目录 | 说明 |
|--------|------|
| `snowy_arc/` | 古偶短篇节选：雪夜、腹黑受伤男主 × 温柔女主救归，人物与情节见该目录下 `novel.txt`。 |

以下命令均在**仓库根目录**执行；将 `examples/snowy_arc/novel.txt` 换成你的小说路径即可。输出建议落在 `artifacts/exports/`。环境变量见仓库根 `.env` / `README.md`。

---

## 从小说到成片：拆分步骤（命令 + 说明）

下表为**推荐顺序**；`mode=l2v` 为默认图生视频链路（文生图 → 百炼图生视频）。**T2V 文生视频**只有「小说直出」一条捷径，见文末。

| 步骤 | 命令 | 做什么 | 需要 API | 主要产出 |
|:----:|------|--------|----------|----------|
| **0（可选）** | `py -3 run_tovedio.py --self-check --mode l2v` | 检查 Python、ffmpeg/ffprobe、密钥是否就绪 | 无 | 终端自检报告 |
| **1（可选）** | 见下方 | **制作圣经**：从小说抽出稳定的人物表、主场景（`locations`）、全片视觉锁定文案 | 仅 **MiniMax** | `snowy_arc_bible.json` |
| **2** | 见下方 | **分镜**：把小说变成结构化剧本 JSON（镜头、画面描述、台词/旁白） | 仅 **MiniMax** | `snowy_arc_storyboard.json` |
| **3（可选）** | 见下方 | **校验分镜**：对照原文做启发式检查 | 无 | 终端输出 |
| **4（可选）** | 见下方 | **定妆照**：按 JSON 里 `characters` 为每个角色生成一张参考 PNG | **MiniMax**（文生图） | `*_costume_sheet.png` + manifest |
| **5** | 见下方 | **成片（L2V）**：按分镜配图 → 百炼图生视频 → 拼接 → 默认混 TTS | **MiniMax + 百炼** | `.mp4` |

### 步骤 0（可选）— 环境自检

```bash
py -3 run_tovedio.py --self-check --mode l2v
```

**说明**：不读小说、不调视频。确认本机 Python≥3.10、ffmpeg/ffprobe 可用；`l2v` 模式下会检查 **MINIMAX** 与 **百炼** 密钥是否配置。若只做分镜/圣经可改用 `--mode t2v`（不要求 MiniMax，但仍会检查百炼）。

---

### 步骤 1（可选）— 制作圣经

```bash
py -3 run_tovedio.py --production-bible-only -i examples/snowy_arc/novel.txt --save-production-bible artifacts/exports/snowy_arc_bible.json --style real
```

**说明**：一次 **MiniMax** 调用，生成符合 `docs/production_bible.schema.json` 的 JSON：`characters`（选角与外貌锚点）、`locations`（主场景环境描述）、`series_visual_lock`（全片美术方向）。**不成分镜、不出片**。  
**建议放在分镜之前**，这样步骤 2 可加 `--production-bible` 让人物与场景和圣经对齐。若你已先有分镜，仍可后补本步，在步骤 5 用 `--production-bible` 锁场景与视觉。

---

### 步骤 2 — 生成分镜（结构化剧本）

**未使用圣经时：**

```bash
py -3 run_tovedio.py --storyboard-only --input examples/snowy_arc/novel.txt --save-storyboard artifacts/exports/snowy_arc_storyboard.json --style real
```

**已在步骤 1 生成圣经时（推荐）：**

```bash
py -3 run_tovedio.py --storyboard-only --input examples/snowy_arc/novel.txt --production-bible artifacts/exports/snowy_arc_bible.json --save-storyboard artifacts/exports/snowy_arc_storyboard.json --style real
```

**说明**：**仅 MiniMax** 生成分镜 JSON（`shots`、`lines`、`visual` 等），**不配图、不调百炼、不 TTS**。生成后可人工编辑 JSON，再进入后续步骤。

---

### 步骤 3（可选）— 校验分镜与原文

```bash
py -3 run_tovedio.py --validate-storyboard artifacts/exports/snowy_arc_storyboard.json --input examples/snowy_arc/novel.txt
```

**说明**：本地 JSON Schema 校验 + 与原文的启发式对照，**零 API**。适合在出片前快速扫一眼是否偏题或缺镜头。

---

### 步骤 4（可选）— 角色定妆照

```bash
py -3 run_tovedio.py --character-sheets-only -i artifacts/exports/snowy_arc_storyboard.json --save-character-dir artifacts/exports/char_sheets --style real
```

**说明**：读取分镜（或制作圣经）里的 `characters`，为每个 `id` 生成 `{id}_costume_sheet.png`，供步骤 5 **图生图关键帧**时作角色语义参考。默认使用 **百炼文生图**；也可用制作圣经 JSON 作 `-i`。清单见 `character_sheets_manifest.json`。

---

### 步骤 5 — 从分镜生成成片（L2V）

**基础（仅分镜）：**

```bash
py -3 run_tovedio.py --from-storyboard artifacts/exports/snowy_arc_storyboard.json -o artifacts/exports/snowy_arc.mp4 --style real
```

**带上圣经 + 定妆（推荐在已做步骤 1、4 时使用）：**

```bash
py -3 run_tovedio.py --from-storyboard artifacts/exports/snowy_arc_storyboard.json -o artifacts/exports/snowy_arc.mp4 --style real --production-bible artifacts/exports/snowy_arc_bible.json --character-sheet-dir artifacts/exports/char_sheets
```

**说明**：**不再读小说**。按分镜逐镜文生图 → **百炼** 图生视频 → ffmpeg 拼接；默认按分镜文本做 **MiniMax TTS** 并混音。需要 **MiniMax + 百炼**。  
**仅支持 `mode=l2v`（默认）**。不需要旁白可加 `--no-l2v-tts`。更多参数见 `py -3 run_tovedio.py -h`。

---

## 捷径：小说一步到成片（不分步）

等价于在一条命令里完成「小说 → 分镜 → 配图 → 视频 → TTS」（不写中间 JSON 除非你加 `--save-storyboard`）。

```bash
py -3 run_tovedio.py examples/snowy_arc/novel.txt -o artifacts/exports/snowy_arc.mp4 --mode l2v --style real
```

可选同时落盘分镜：`--save-storyboard artifacts/exports/snowy_arc_storyboard.json`。若已有圣经，可加 `--production-bible artifacts/exports/snowy_arc_bible.json`；若有定妆目录，可加 `--character-sheet-dir artifacts/exports/char_sheets`。

---

## 捷径：文生视频（T2V，无「分镜 JSON」文件）

整段小说一次性送 **百炼 Wan 文生视频**，不经过分镜文件与图生视频链路：

```bash
py -3 run_tovedio.py examples/snowy_arc/novel.txt -o artifacts/exports/snowy_arc_t2v.mp4 --mode t2v --style real
```

**说明**：主要需要 **百炼**；**不需要** MiniMax。与上表「拆分 L2V」流程不同，无法使用本仓库的分镜/圣经/定妆 JSON 文件驱动多镜头拼接。

---

## 字段说明

- 分镜：`docs/storyboard.schema.json`  
- 制作圣经：`docs/production_bible.schema.json`
