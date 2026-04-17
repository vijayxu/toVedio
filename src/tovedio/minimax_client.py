"""调用 MiniMax：与 OpenClaw 一致，优先走 Anthropic 兼容接口（同一密钥通常可用）。"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from jsonschema.exceptions import ValidationError

from .production_bible_io import apply_production_bible_to_storyboard, validate_production_bible
from .storyboard_io import project_root, validate_storyboard

logger = logging.getLogger(__name__)

# 国际站 OpenClaw 默认：https://api.minimax.io/anthropic
# 国内站 / Token Plan（控制台在 platform.minimaxi.com）应对应：https://api.minimaxi.com/anthropic
DEFAULT_ANTHROPIC_BASE_URL = "https://api.minimax.io/anthropic"
DEFAULT_ANTHROPIC_BASE_URL_CN = "https://api.minimaxi.com/anthropic"
DEFAULT_MODEL = "MiniMax-M2.7"

# 分镜 / 无小说剧本的 system prompt 结构示例（原 docs/storyboard.example.json，内联以免仓库再挂示例文件）
_STORYBOARD_STRUCTURE_EXAMPLE_JSON = """{
  "schema_version": "1.0.0",
  "meta": {
    "title": "青石巷试炼（节选）",
    "language": "zh-CN",
    "source": {
      "novel_id": "demo-001",
      "chapter": "第一章",
      "excerpt_range": "开篇至「来了。」"
    },
    "model": {
      "provider": "minimax",
      "model_name": "MiniMax-Text-01"
    }
  },
  "characters": [
    {
      "id": "lin_yuan",
      "name": "林远",
      "role": "protagonist",
      "appearance": "二十五岁男子，黑发束高髻，剑眉薄唇，身着深墨色短打劲装，腰系深棕布带，袖中藏短刃，神情冷静警觉",
      "voice_hint": "male_young_calm"
    }
  ],
  "shots": [
    {
      "shot_id": "s01",
      "order": 0,
      "duration_sec": 3,
      "scene": {
        "label": "青石巷·夜",
        "location": "青石巷巷口",
        "time_of_day": "night",
        "mood": "压抑、悬疑",
        "background_prompt": "夜色中的江南青石巷，屋檐与灯笼，潮湿石板路，电影感冷色调"
      },
      "visual": {
        "shot_type": "establishing",
        "camera": "缓慢横移",
        "prompt_zh": "远景：青石巷口，夜色，一盏摇晃的灯笼，风掠瓦檐，无人影，电影宽画幅",
        "negative_prompt": "text, subtitle, watermark, deformed hands",
        "characters_on_screen": []
      },
      "bgm_note": "低沉弦乐，悬疑氛围，节奏缓慢",
      "lines": [
        {
          "kind": "sfx_note",
          "text": "夜风穿巷，远处灯笼轻摇，石板地面湿润回声"
        }
      ]
    },
    {
      "shot_id": "s02",
      "order": 1,
      "duration_sec": 4,
      "scene": {
        "label": "巷口·停步",
        "location": "青石巷巷口",
        "time_of_day": "night",
        "mood": "紧张",
        "background_prompt": "同青石巷夜景，焦点可落在巷尾灯笼"
      },
      "visual": {
        "shot_type": "medium",
        "camera": "缓慢推近",
        "prompt_zh": "中景：Image1（林远）侧身立于巷口，抬头望向巷尾摇晃的灯笼，侧脸线条清晰，袖中隐约可见短刃轮廓",
        "characters_on_screen": ["lin_yuan"]
      },
      "bgm_note": "弦乐渐紧，低频律动",
      "lines": [
        {
          "kind": "sfx_note",
          "text": "脚步在石板上停顿，衣袍轻微摩擦声"
        }
      ]
    },
    {
      "shot_id": "s03",
      "order": 2,
      "duration_sec": 4,
      "scene": {
        "label": "巷中·对峙",
        "location": "青石巷",
        "time_of_day": "night",
        "mood": "一触即发",
        "background_prompt": "狭窄巷道，灯笼光在地面形成长影"
      },
      "visual": {
        "shot_type": "close_up",
        "camera": "轻微手持感",
        "prompt_zh": "特写：Image1（林远）握紧袖中短刃的手部与下颌线条，背景灯笼虚焦",
        "characters_on_screen": ["lin_yuan"]
      },
      "bgm_note": "弦乐紧张感加剧，低频律动",
      "lines": [
        {
          "kind": "dialogue",
          "speaker_id": "lin_yuan",
          "text": "来了。",
          "source_anchor": "原文对白",
          "emotion": "平静",
          "speech_rate": "缓慢"
        },
        {
          "kind": "sfx_note",
          "text": "金属短刃轻微摩擦布料的细碎声"
        }
      ]
    }
  ]
}"""


def _anthropic_base_url() -> str:
    """显式 MINIMAX_ANTHROPIC_BASE_URL 优先；否则 MINIMAX_USE_CN=1 走国内 API。"""
    explicit = (os.environ.get("MINIMAX_ANTHROPIC_BASE_URL") or "").strip()
    if explicit:
        return explicit
    cn = (os.environ.get("MINIMAX_USE_CN") or "").strip().lower()
    if cn in ("1", "true", "yes", "minimaxi"):
        return DEFAULT_ANTHROPIC_BASE_URL_CN
    return DEFAULT_ANTHROPIC_BASE_URL

_ENV_LOADED = False


def _load_env_file_simple(path: Path) -> None:
    """未安装 python-dotenv 时，解析 KEY=VALUE 行写入 os.environ（不覆盖已有变量）。"""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
        if text.startswith("\ufeff"):
            text = text[1:]
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key:
            cur = os.environ.get(key)
            # Allow .env to fill variables that are unset or set to empty placeholders.
            if cur is None or str(cur).strip() == "":
                os.environ[key] = val


def _ensure_dotenv_loaded() -> None:
    """从仓库根目录 .env 加载；优先 python-dotenv，否则内置简易解析。"""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    env_file = project_root() / ".env"
    if env_file.is_file():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file, override=False)
        except ImportError:
            logger.debug("未安装 python-dotenv，使用内置解析加载 .env")
            _load_env_file_simple(env_file)
    _ENV_LOADED = True


def _get_api_key() -> str | None:
    _ensure_dotenv_loaded()
    key = (os.environ.get("MINIMAX_API_KEY") or "").strip()
    if key in ("", "你的密钥", "your_key_here"):
        return None
    return key or None


def _auth_failed_help() -> str:
    return (
        "MiniMax 返回 401：当前密钥未被该 API 地址接受。\n"
        "· 若账号在国内站 / Token Plan（控制台 platform.minimaxi.com，含 payment/token-plan），"
        "请在 .env 增加一行：MINIMAX_USE_CN=1（将使用 "
        f"{DEFAULT_ANTHROPIC_BASE_URL_CN}），或手动设置 MINIMAX_ANTHROPIC_BASE_URL。\n"
        f"· 国际站默认使用 {DEFAULT_ANTHROPIC_BASE_URL}（与 OpenClaw 国际配置一致）。\n"
        "· 若有 GroupId：MINIMAX_GROUP_ID=数字。"
    )


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def _parse_json_object(content: str) -> dict[str, Any]:
    raw = _strip_fences(content)
    return json.loads(raw)


def _build_system_prompt() -> str:
    example = _STORYBOARD_STRUCTURE_EXAMPLE_JSON
    return f"""你是影视分镜编剧助手，只输出一个 JSON 对象，不要 Markdown，不要代码围栏，不要任何解释文字。
结构必须与下面「示例」同构（字段名一致），并满足校验器（服务端会用 JSON Schema 校验）。

【结构示例】
{example}

硬性规则：
1. schema_version 必须为 "1.0.0"。
2. meta 必须含 title、language（如 zh-CN）；可在 meta.model 中注明 minimax。
3. characters 每项 id 为小写字母开头，可含数字与下划线；必须有 name。appearance 必须包含发型/发色、脸型或五官特征、服装配色三项可复现特征，不少于 8 字；禁止在 appearance 中出现血迹、伤口、流血、死亡、血污等审核敏感词，改用中性描述（如"右肩缠绷带"代替"箭射伤血浸"）。
4. shots 至少 1 条；每条含 shot_id、order、scene（label、background_prompt）、visual（shot_type、prompt_zh）、lines（至少一行）。
   visual.shot_type 只能是枚举之一（勿自创）：establishing, wide, medium, medium_close_up, close_up, extreme_close_up, over_shoulder, insert, transition, other。
   景别选用指导：establishing/wide 用于开场建立环境或场景切换过渡（无需人物特写）；medium/medium_close_up 用于人物行动与对话推进（最常用）；close_up/extreme_close_up 用于情绪爆发、关键道具、高潮细节；over_shoulder 用于双人对话强调关系；insert 用于道具/信件/手势等局部细节插入；transition 用于时间跳跃或空间切换。避免全片都用 medium，应有远→中→近的景别韵律。
5. 每条 shot 的 lines 必须是至少含一个对象的数组，禁止 lines: []；禁止写 kind=narration 行（旁白不进视频声轨）；无对白时写一行 kind=sfx_note 描述环境音/动作音效；每镜建议同时附一行 kind=sfx_note。
6. lines 里 kind 为 dialogue 时必须含 speaker_id，且对应 characters[].id；对白行建议填写 emotion（如平静/紧张/激动/悲伤）与 speech_rate；speech_rate 只能填三个值之一：「急促」「缓慢」「正常」，禁止填其他词（如轻柔/低沉/温和均不合法）。
6b. 每条 shot 建议填写 bgm_note（字符串），描述本镜背景音乐氛围，如「低沉弦乐，悬疑」「轻快钢琴，温馨」。
7. lines[].text 必须来自用户给出的小说原文（对白与叙述），不得编造剧情。
8. visual.prompt_zh、scene.background_prompt 只描述画面与场景，不要把整段小说正文贴进画面描述里。
9. 每条 shot 建议填写 duration_sec（秒）：纯空镜/环境建立镜 2～3 秒；普通动作镜 3～5 秒；重要情绪/对白镜 4～8 秒；避免每镜等长像幻灯片。
   对白时长约束：duration_sec 必须 ≥ 本镜所有 dialogue 行 text 字数之和 × 0.35（例如台词共 20 字则 duration_sec ≥ 7）；若计算结果超过 10，必须拆分为两镜，不得把多句对白硬塞进一镜；单镜 dialogue 条数建议不超过 2 条。
10. 若出现 2 个及以上人物，必须为每个角色写出显著不同的 appearance（至少包含：发型/发色、脸型或五官特征、服装配色中的任意三项），禁止“同脸同发型同服装”。
11. 多人同镜时，visual.prompt_zh 必须明确区分人物位置与朝向（例如“左侧A、右侧B”），避免生成双胞胎脸。
12. 镜头连续性：相邻 shots 必须有动作/视线/空间上的承接，不要突然跳地点或跳时间；若场景切换，先给过渡镜头（如空镜/移动镜头）。
13. 运镜连续性：避免连续镜头完全重复构图；遵守基本轴线与景别递进（远景→中景→近景）以增强叙事连贯性。
14. 节奏优先连贯而非碎切：同一段事件优先用较少镜头表达，避免过度切镜；建议单镜 3～6 秒并给出清晰动机衔接。
15. 禁止在单个镜头 visual.prompt_zh 中描述“双重曝光/叠影/多层人像重叠”等效果，除非用户原文明确要求梦境或回忆。
16. 人物数量约束：默认仅允许出现 characters 中已定义角色；不得添加“路人/群众/士兵”等未命名额外人物，除非原文明确写出且必须出镜。
17. visual.characters_on_screen 必须只包含已定义角色 id；若本镜不需要人物，可为空，但不得用“远处人群”替代。
18. 叙事结构必须完整：shots 按 order 形成“起-承-转-合”（开场建立、冲突升级、高潮对决、结果落点），禁止随机拼贴。
19. 时间线单向推进：不得无提示倒叙；相邻镜头应体现同一事件链的因果关系（上一镜动作导致下一镜结果）。
20. 每个镜头只表达一个核心事件，不要在单镜中堆砌多个无关动作或地点。
21. 全片视觉一致性：所有 shots 必须使用同一套美术与灯光逻辑（色相、对比、时代感、渲染风格），禁止中段突然换成另一种画风或像混剪多支无关短片。
22. 关键情节不得丢：原文中出现的人物关系、关键动作、道具、地点（如老人、雨伞、公交站、对话对象）必须在某一镜的 visual.prompt_zh 或 scene.background_prompt 中明确可辨；禁止用纯空镜/纯风景/无人物场景替代原文要求出现人物或互动的句子。
23. 句段对齐：原文按句号或换行形成的事件单元，应在 shots 中有对应呈现（可适度合并相邻短句为一镜，但不得跳过整句核心事件）；合并时须在 visual.prompt_zh 写全本镜包含的全部关键元素。
24. 禁止「叙事蒸发」：若原文写两人同行、送别、对峙等，画面中必须能看见相应人数与关系，不得只生成单人背影或无关环境。
25. 角色图像引用（R2V 兼容）：visual.prompt_zh 中引用角色时，以该镜 characters_on_screen 数组的下标顺序编号（与全局 characters 列表无关）——本镜第 1 个角色写「Image1（角色名）」，第 2 个写「Image2（角色名）」，以此类推（最多 Image3）；禁止只写角色名而不写 Image 标记。示例：characters_on_screen 为 ["wen_heng"] 时，该角色是本镜唯一角色，写「Image1（温蘅）」而非「Image2」。
26. 台词长度自检：写完每条 dialogue 行后，默数其 text 字数；同一镜内所有 dialogue 字数合计若超过 duration_sec ÷ 0.35，必须删减台词或拆镜，确保声音能在视频时长内自然说完；单条 dialogue text 建议不超过 25 字。
"""


def _build_screenplay_system_prompt() -> str:
    example = _STORYBOARD_STRUCTURE_EXAMPLE_JSON
    return f"""你是影视编剧与分镜师，只输出一个 JSON 对象，不要 Markdown，不要代码围栏，不要任何解释文字。
当前任务：**无用户小说原文**，请你**原创**一部适合剪成短视频（约 60～90 秒体量）的完整小故事，并以与下面「示例」**同构**的分镜 JSON 输出（字段名与分镜 schema 一致）。

【结构示例】
{example}

硬性规则（在「无小说」前提下替代常规「忠实原著」规则）：
1. schema_version 必须为 "1.0.0"；meta 含 title、language（如 zh-CN）。
2. characters 每项 id 小写字母开头；必须有 name、appearance（含发型/发色、脸型/五官特征、服装配色三项，不少于八字）；appearance 禁止含血迹/伤口/流血等敏感词，改用中性描述。
3. shots 6 条左右（最多 8）；每条含 shot_id、order、scene（label、background_prompt）、visual（shot_type、prompt_zh）、lines（至少一行）。
4. lines 禁止 kind=narration；可为原创 dialogue 或 sfx_note；dialogue 须口语自然、适合配音；禁止大段抄袭知名作品原文。
5. visual.shot_type 只能是枚举之一：establishing, wide, medium, medium_close_up, close_up, extreme_close_up, over_shoulder, insert, transition, other。景别应有远→中→近的韵律，避免全片都用 medium。
6. lines 里 kind 为 dialogue 时必须含 speaker_id，且对应 characters[].id；对白行填写 emotion 与 speech_rate；speech_rate 只能填「急促」「缓慢」「正常」三者之一，禁止填其他词。
7. 全片故事、人物、场景均为本次原创，但须自洽、可拍、避免过度暴力色情与真人指向。
8. 叙事结构完整：起承转合清晰；相邻镜头有动作或空间承接；每条 shot 填写 bgm_note。
9. 多角色时 appearance 显著区分；双人同镜写明左右站位。
10. 全片视觉与时代感一致，禁止中段换画风。
11. 角色图像引用：按本镜 characters_on_screen 数组下标顺序编号（与全局 characters 列表无关）——本镜第 1 个角色写「Image1（角色名）」，第 2 个写「Image2（角色名）」；单人镜头只有 Image1，禁止写 Image2。
12. 对白时长约束：duration_sec ≥ 本镜所有 dialogue text 字数之和 × 0.35；单镜 dialogue 条数不超过 2 条，超出时拆镜。
"""


def _build_production_bible_system_prompt() -> str:
    ex = """{
  "schema_version": "1.0.0",
  "meta": { "title": "示例", "language": "zh-CN" },
  "series_visual_lock": "当代小城清晨至傍晚，自然光为主，低饱和暖色点缀；禁止赛博霓虹与古装混用。",
  "characters": [
    {
      "id": "protagonist_01",
      "name": "示例主角",
      "role": "protagonist",
      "appearance": "二十七八岁女性，中长发深棕微卷，圆脸，米色针织开衫与深蓝直筒裤，平底皮鞋，身形清瘦。",
      "voice_hint": "温柔女声，语速适中"
    }
  ],
  "locations": [
    {
      "id": "loc_interior_01",
      "label": "小书店内景",
      "time_of_day": "day",
      "mood": "安静温暖",
      "environment_prompt": "临街小书店，浅木书架满墙，木地板有阳光条纹，收银台靠里侧，无杂乱海报，无可见书名文字。"
    }
  ]
}"""
    return f"""你是影视筹备统筹，只输出一个 JSON 对象，不要 Markdown，不要代码围栏，不要解释文字。
输出须满足服务端 JSON Schema（制作圣经 v1），结构与下例同构：

【结构示例】
{ex}

硬性规则：
1. schema_version 必须为 "1.0.0"。
2. meta 必须含 title、language（如 zh-CN）。
3. characters：至少 1 人；每项 id 小写字母开头，可含数字与下划线；必须有 name、appearance（不少于 8 字，含发型/脸型或五官/服装色系等可复现特征）。
4. locations：至少 1 处主场景；每项 id 小写字母开头；label + environment_prompt（不少于 8 字）描述空间、材质、光线、时代，不要写剧情动作与对白。
5. series_visual_lock：一条全片视觉锁定（时代、色相、镜头质感、禁忌），可为中文；勿与 appearance 逐字重复。
6. 角色与场景必须来自用户小说，禁止编造原文不存在的主要人物或主场景；路人非重点不必写入 characters。
"""


def _anthropic_text_content(message: Any) -> str:
    """从 Anthropic Messages API 响应中拼接文本块；兼容 content 为空或块为 dict。"""
    if message is None:
        return ""

    def _blocks(obj: Any) -> Any:
        if isinstance(obj, dict):
            return obj.get("content")
        return getattr(obj, "content", None)

    blocks = _blocks(message)
    if blocks is None:
        nested = getattr(message, "message", None) if not isinstance(message, dict) else message.get("message")
        if nested is not None:
            blocks = _blocks(nested)
    if blocks is None:
        logger.warning(
            "MiniMax/Anthropic 响应缺少 content，stop_reason=%s",
            getattr(message, "stop_reason", None)
            if not isinstance(message, dict)
            else message.get("stop_reason"),
        )
        return ""
    if not isinstance(blocks, (list, tuple)):
        if isinstance(blocks, str):
            return blocks
        return str(blocks)

    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            continue
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _get_anthropic_client() -> Any:
    from anthropic import Anthropic

    key = _get_api_key()
    if not key:
        env_file = project_root() / ".env"
        raise RuntimeError(
            "未配置 MiniMax Key。请在仓库根目录创建 .env：MINIMAX_API_KEY=你的密钥\n"
            f"（路径：{env_file}）"
        )
    base = _anthropic_base_url()
    logger.info("MiniMax Anthropic 基地址：%s", base)
    gid = (os.environ.get("MINIMAX_GROUP_ID") or "").strip()
    kwargs: dict[str, Any] = {"api_key": key, "base_url": base}
    if gid:
        kwargs["default_query"] = {"GroupId": gid}
    return Anthropic(**kwargs)


def _is_auth_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status == 401:
        return True
    body = str(exc).lower()
    return "401" in body or "unauthorized" in body or "invalid api key" in body


def generate_production_bible(
    novel_text: str,
    *,
    model: str | None = None,
    style: str = "real",
    max_retries: int = 2,
) -> dict[str, Any]:
    """
    根据小说正文生成「制作圣经」JSON：选角、定妆、主场景搭景；不含分镜 shots。
    """
    if not novel_text.strip():
        raise ValueError("小说正文为空。")
    client = _get_anthropic_client()
    model_name = model or os.environ.get("MINIMAX_MODEL", DEFAULT_MODEL)
    system = _build_production_bible_system_prompt()
    style_hint = "动漫风（二次元）" if style == "anime" else "现实风（电影写实）"
    base_user = f"""以下为小说原文，请只输出制作圣经 JSON（characters + locations + series_visual_lock 等），不要输出 shots。

整体美术方向：{style_hint}（写入 series_visual_lock 与各场景 environment_prompt 中）。

---
{novel_text.strip()}
---
"""
    last_err: str | None = None
    for attempt in range(max_retries):
        user_content = base_user
        if last_err:
            user_content = base_user + "\n\n上一次输出未通过校验，请只输出修正后的完整 JSON：\n" + last_err
        logger.info(
            "调用 MiniMax 生成制作圣经（Anthropic 兼容，model=%s，第 %s 次）…",
            model_name,
            attempt + 1,
        )
        try:
            resp = client.messages.create(
                model=model_name,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": user_content}],
                temperature=0.25,
            )
        except Exception as e:
            if _is_auth_error(e):
                raise RuntimeError(_auth_failed_help()) from e
            raise
        content = _anthropic_text_content(resp)
        if not content:
            last_err = "模型返回空内容（请重试或缩短原文）"
            continue
        content = re.sub(r"<think>[\s\S]*?</think>", "", content, flags=re.IGNORECASE)
        content = re.sub(r"<thinking>[\s\S]*?</thinking>", "", content, flags=re.IGNORECASE)
        try:
            data = _parse_json_object(content)
        except json.JSONDecodeError as e:
            last_err = f"JSON 解析失败：{e}"
            continue
        try:
            validate_production_bible(data)
        except ValidationError as e:
            last_err = str(e)
            logger.warning("制作圣经 Schema 校验失败：%s", last_err)
            continue
        data.setdefault("meta", {})
        if isinstance(data["meta"], dict):
            data["meta"].setdefault("model", {})
            if isinstance(data["meta"]["model"], dict):
                data["meta"]["model"]["provider"] = "minimax"
                data["meta"]["model"]["model_name"] = model_name
        return data
    raise RuntimeError(f"MiniMax 制作圣经生成失败：{last_err}")


def generate_storyboard(
    novel_text: str,
    *,
    model: str | None = None,
    style: str = "real",
    max_retries: int = 2,
    production_bible: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    根据小说正文调用 MiniMax 生成分镜 JSON，并通过 jsonschema 校验。
    使用 Anthropic 兼容接口（与 OpenClaw 默认一致）。
    若传入 production_bible，分镜须原样沿用其中的 characters 定义，且对白/出镜 id 不得超出圣经角色表。
    """
    if not novel_text.strip():
        raise ValueError("小说正文为空。")
    client = _get_anthropic_client()
    model_name = model or os.environ.get("MINIMAX_MODEL", DEFAULT_MODEL)
    system = _build_system_prompt()
    style_hint = "动漫风（二次元）" if style == "anime" else "现实风（电影写实）"
    base_user = f"""以下为小说原文，请生成分镜 JSON。
额外要求：
1) 整体画面风格统一为「{style_hint}」。
2) 输出应是可直接剪成短片的连续故事，不是氛围图集合。
3) 镜头数量控制在 6 个左右（最多 8），优先保证因果连贯与故事完整；宁少勿碎。
4) 相邻镜头必须写出动作承接（谁做了什么，导致下一镜发生什么）。
5) 不要新增原文没有的剧情支线、人物或场景群像。
6) 原文里写到的具体人物、道具、地点（尤其老人、儿童、车辆、站牌、雨伞等），必须在分镜里落到画面描述中，不得省略或用「模糊街景」代替。
7) 若原文某句包含多个视觉要素（如雨+伞+送人+公交站），负责该句的镜头必须把这些要素写进 visual.prompt_zh，保证文生图能画出来。

---
{novel_text.strip()}
---
"""
    if production_bible is not None:
        lock = (production_bible.get("series_visual_lock") or "").strip()
        bible_payload = {
            "characters": production_bible.get("characters") or [],
            "locations": production_bible.get("locations") or [],
            **({"series_visual_lock": lock} if lock else {}),
        }
        bible_json = json.dumps(bible_payload, ensure_ascii=False, indent=2)
        base_user += f"""

【制作圣经（必须遵守）】
以下为已锁定的选角、定妆与主场景。你输出的分镜 JSON 必须同时满足：
A) 顶层 characters 数组与下面【圣经 JSON】中的 characters **完全一致**（同一顺序、同一 id、name、appearance、voice_hint、role 逐字拷贝，禁止改写 appearance）。
B) visual.characters_on_screen 与对白 speaker_id 只能使用上述 id。
C) 每镜 scene.background_prompt 须与圣经中某一主场景的 environment_prompt 美术逻辑相容（同一空间类型与时代），可补充天气、机位与局部陈设，但不得推翻主场景光色与建筑类型。
D) 不要把 locations 数组写进分镜输出（分镜 schema 无 locations 顶层字段）。

【圣经 JSON】
{bible_json}
"""
    last_err: str | None = None
    for attempt in range(max_retries):
        user_content = base_user
        if last_err:
            user_content = (
                base_user
                + "\n\n上一次输出未通过校验，请只输出修正后的完整 JSON：\n"
                + last_err
            )
        logger.info(
            "调用 MiniMax 生成分镜（Anthropic 兼容，model=%s，第 %s 次）…",
            model_name,
            attempt + 1,
        )
        try:
            resp = client.messages.create(
                model=model_name,
                max_tokens=16384,
                system=system,
                messages=[{"role": "user", "content": user_content}],
                temperature=0.3,
            )
        except Exception as e:
            if _is_auth_error(e):
                raise RuntimeError(_auth_failed_help()) from e
            raise
        content = _anthropic_text_content(resp)
        if not content:
            sr = getattr(resp, "stop_reason", None)
            mid = getattr(resp, "id", None)
            logger.warning(
                "MiniMax 返回 200 但无文本：stop_reason=%s id=%s（服务端瞬时或内容被过滤时可重试）",
                sr,
                mid,
            )
            last_err = "模型返回空内容（content 为空，请重试或换短文本/检查控制台）"
            continue
        content = re.sub(r"<redacted_thinking>[\s\S]*?</redacted_thinking>", "", content, flags=re.IGNORECASE)
        content = re.sub(r"<thinking>[\s\S]*?</thinking>", "", content, flags=re.IGNORECASE)
        try:
            data = _parse_json_object(content)
        except json.JSONDecodeError as e:
            last_err = f"JSON 解析失败：{e}"
            logger.warning("%s", last_err)
            continue
        try:
            validate_storyboard(data)
        except ValidationError as e:
            last_err = str(e)
            logger.warning("Schema 校验失败：%s", last_err)
            continue
        data.setdefault("meta", {})
        if isinstance(data["meta"], dict):
            data["meta"].setdefault("model", {})
            if isinstance(data["meta"]["model"], dict):
                data["meta"]["model"]["provider"] = "minimax"
                data["meta"]["model"]["model_name"] = model_name
        return data
    raise RuntimeError(f"MiniMax 分镜生成失败：{last_err}")


def generate_screenplay_storyboard(
    *,
    brief: str,
    style: str = "real",
    model: str | None = None,
    max_retries: int = 2,
    production_bible: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    无小说正文：根据梗概/约束 brief 让模型原创故事并输出标准分镜 JSON（即「剧本」形态，可直接 save_storyboard）。
    """
    b = (brief or "").strip()
    if not b:
        raise ValueError("剧本梗概 brief 为空。")
    client = _get_anthropic_client()
    model_name = model or os.environ.get("MINIMAX_MODEL", DEFAULT_MODEL)
    system = _build_screenplay_system_prompt()
    style_hint = "动漫风（二次元）" if style == "anime" else "现实风（电影写实）"
    base_user = f"""请按系统规则原创短片并输出分镜 JSON。

【风格】{style_hint}

【创作梗概 / 约束（请在本意图内发挥，不要偏离题材与基调）】
{b}

【体量】约 6～8 个镜头；每镜 lines 写清旁白或对白，便于后续 TTS。
"""
    if production_bible is not None:
        lock = (production_bible.get("series_visual_lock") or "").strip()
        bible_payload = {
            "characters": production_bible.get("characters") or [],
            "locations": production_bible.get("locations") or [],
            **({"series_visual_lock": lock} if lock else {}),
        }
        bible_json = json.dumps(bible_payload, ensure_ascii=False, indent=2)
        base_user += f"""

【制作圣经（必须遵守）】
分镜顶层 characters 必须与下列 JSON 中的 characters **完全一致**（逐字段拷贝）；出镜与对白 id 不得超出表内；场景描述与 locations 的 environment_prompt 美术逻辑相容。不要把 locations 写入顶层输出。

【圣经 JSON】
{bible_json}
"""
    last_err: str | None = None
    for attempt in range(max_retries):
        user_content = base_user
        if last_err:
            user_content = base_user + "\n\n上一次输出未通过校验，请只输出修正后的完整 JSON：\n" + last_err
        logger.info(
            "调用 MiniMax 生成原创剧本分镜（Anthropic 兼容，model=%s，第 %s 次）…",
            model_name,
            attempt + 1,
        )
        try:
            resp = client.messages.create(
                model=model_name,
                max_tokens=16384,
                system=system,
                messages=[{"role": "user", "content": user_content}],
                temperature=0.45,
            )
        except Exception as e:
            if _is_auth_error(e):
                raise RuntimeError(_auth_failed_help()) from e
            raise
        content = _anthropic_text_content(resp)
        if not content:
            last_err = "模型返回空内容（请重试）"
            continue
        content = re.sub(r"<think>[\s\S]*?</think>", "", content, flags=re.IGNORECASE)
        content = re.sub(r"<thinking>[\s\S]*?</thinking>", "", content, flags=re.IGNORECASE)
        try:
            data = _parse_json_object(content)
        except json.JSONDecodeError as e:
            last_err = f"JSON 解析失败：{e}"
            continue
        try:
            validate_storyboard(data)
        except ValidationError as e:
            last_err = str(e)
            logger.warning("Schema 校验失败：%s", last_err)
            continue
        if production_bible is not None:
            try:
                apply_production_bible_to_storyboard(data, production_bible)
                validate_storyboard(data)
            except ValueError as e:
                last_err = str(e)
                continue
        data.setdefault("meta", {})
        if isinstance(data["meta"], dict):
            data["meta"].setdefault("model", {})
            if isinstance(data["meta"]["model"], dict):
                data["meta"]["model"]["provider"] = "minimax"
                data["meta"]["model"]["model_name"] = model_name
        return data
    raise RuntimeError(f"MiniMax 剧本分镜生成失败：{last_err}")
