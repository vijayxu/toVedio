"""加载与校验 docs/storyboard.schema.json。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

# (mtime, schema)：文件更新后自动失效，避免长驻进程或旧缓存读到过期枚举。
_SCHEMA_CACHE: tuple[float, dict[str, Any]] | None = None


def project_root() -> Path:
    """src/tovedio -> 仓库根 toVedio。"""
    return Path(__file__).resolve().parent.parent.parent


def schema_path() -> Path:
    return project_root() / "docs" / "storyboard.schema.json"


def load_schema() -> dict[str, Any]:
    global _SCHEMA_CACHE
    p = schema_path()
    if not p.is_file():
        raise FileNotFoundError(f"找不到分镜 Schema：{p}")
    mtime = p.stat().st_mtime
    if _SCHEMA_CACHE is not None and _SCHEMA_CACHE[0] == mtime:
        return _SCHEMA_CACHE[1]
    schema = json.loads(p.read_text(encoding="utf-8"))
    _SCHEMA_CACHE = (mtime, schema)
    return schema


import logging

logger = logging.getLogger(__name__)

_DIALOGUE_SEC_PER_CHAR = 0.35   # 每字约需时长（秒）
_DIALOGUE_MAX_DURATION = 10.0   # 单镜视频上限（秒）
_DIALOGUE_TRUNCATE_AT = 25      # 单条台词超过此字数时，在自然标点处截断


def _truncate_dialogue_text(text: str, max_chars: int) -> str:
    """在 max_chars 以内找最后一个自然标点截断；找不到则硬截。"""
    if len(text) <= max_chars:
        return text
    # 优先在标点处截断
    puncts = "，。！？；、…"
    cut = max_chars
    for i in range(max_chars - 1, max(max_chars - 10, 0) - 1, -1):
        if i < len(text) and text[i] in puncts:
            cut = i + 1
            break
    return text[:cut]


def _normalize_dialogue_duration(data: dict[str, Any]) -> None:
    """
    对每个 shot：
    1. 若所有 dialogue 字数合计 × 0.35 > duration_sec 且结果 ≤ 10，直接调大 duration_sec。
    2. 若所需时长 > 10s，截断最长的 dialogue 行到合理字数，再把 duration_sec 调到实际需要值。
    """
    for shot in data.get("shots", []):
        if not isinstance(shot, dict):
            continue
        lines = shot.get("lines") or []
        dialogues = [
            ln for ln in lines
            if isinstance(ln, dict) and (ln.get("kind") or "").strip() == "dialogue"
        ]
        if not dialogues:
            continue
        total_chars = sum(len(str(ln.get("text") or "")) for ln in dialogues)
        needed = total_chars * _DIALOGUE_SEC_PER_CHAR
        current = float(shot.get("duration_sec") or 0)
        if needed <= current:
            continue
        if needed <= _DIALOGUE_MAX_DURATION:
            # 只需调大 duration_sec
            shot["duration_sec"] = round(needed + 0.5)
            logger.debug(
                "shot %s：台词 %d 字需 %.1fs，duration_sec 从 %.0f 调整为 %s",
                shot.get("shot_id", "?"), total_chars, needed, current, shot["duration_sec"],
            )
        else:
            # 超过 10s：截断最长的 dialogue
            longest = max(dialogues, key=lambda ln: len(str(ln.get("text") or "")))
            original = str(longest.get("text") or "")
            # 计算截断到多少字后总时长 ≤ 10s
            other_chars = total_chars - len(original)
            max_chars = max(1, int((_DIALOGUE_MAX_DURATION / _DIALOGUE_SEC_PER_CHAR) - other_chars))
            max_chars = min(max_chars, _DIALOGUE_TRUNCATE_AT)
            truncated = _truncate_dialogue_text(original, max_chars)
            longest["text"] = truncated
            new_total = other_chars + len(truncated)
            new_needed = new_total * _DIALOGUE_SEC_PER_CHAR
            shot["duration_sec"] = min(_DIALOGUE_MAX_DURATION, round(new_needed + 0.5))
            logger.warning(
                "shot %s：台词原 %d 字（需 %.1fs > 10s），已截断最长台词 %d→%d 字，duration_sec 调为 %.0f",
                shot.get("shot_id", "?"), total_chars, needed,
                len(original), len(truncated), shot["duration_sec"],
            )


def _normalize_image_numbering(data: dict[str, Any]) -> None:
    """
    按每镜 characters_on_screen 数组下标重新编号 prompt_zh 里的 Image\\d+（角色名）标记。
    例：characters_on_screen=["wen_heng"]，温蘅是本镜第1个角色，
    将 prompt_zh 中的 Image2（温蘅）修正为 Image1（温蘅）。
    """
    # 构建 character_id -> name 映射
    id_to_name: dict[str, str] = {}
    for ch in data.get("characters", []):
        if isinstance(ch, dict) and ch.get("id") and ch.get("name"):
            id_to_name[str(ch["id"]).strip()] = str(ch["name"]).strip()

    for shot in data.get("shots", []):
        if not isinstance(shot, dict):
            continue
        vis = shot.get("visual")
        if not isinstance(vis, dict):
            continue
        on_screen = [str(x).strip() for x in (vis.get("characters_on_screen") or []) if str(x).strip()]
        if not on_screen:
            continue
        prompt = str(vis.get("prompt_zh") or "")
        if "Image" not in prompt:
            continue

        # 构建本镜正确的 name -> ImageN 映射（按 characters_on_screen 顺序）
        correct: dict[str, str] = {}
        for idx, cid in enumerate(on_screen, start=1):
            name = id_to_name.get(cid, "")
            if name:
                correct[name] = f"Image{idx}"

        # 替换 prompt_zh 中所有 Image\d+（name） 为正确编号
        def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
            name = m.group(1)
            right = correct.get(name)
            if right and right != m.group(0).split("（")[0]:
                logger.debug(
                    "shot %s：%s（%s）→ %s（%s）",
                    shot.get("shot_id", "?"), m.group(0).split("（")[0], name, right, name,
                )
                return f"{right}（{name}）"
            return m.group(0)

        vis["prompt_zh"] = re.sub(r"Image\d+（([^）]+)）", _replace, prompt)


def _normalize_empty_shot_lines(data: dict[str, Any]) -> None:
    """lines 为空数组时补一行旁白，满足 minItems: 1。"""
    for shot in data.get("shots", []):
        if not isinstance(shot, dict):
            continue
        lines = shot.get("lines")
        if isinstance(lines, list) and len(lines) > 0:
            continue
        scene = shot["scene"] if isinstance(shot.get("scene"), dict) else {}
        vis = shot["visual"] if isinstance(shot.get("visual"), dict) else {}
        label = (scene.get("label") or "").strip()
        pz = (vis.get("prompt_zh") or "").strip()
        fragment = (label or (pz[:80] if pz else "") or "本镜").replace("\n", " ")
        if len(fragment) > 200:
            fragment = fragment[:200] + "…"
        shot["lines"] = [{"kind": "narration", "text": f"（本镜无对白：{fragment}）"}]


def _normalize_character_roles(data: dict[str, Any]) -> None:
    """角色 role 常见同义写法 -> schema 枚举（避免模型输出 supporter 等与枚举字面不一致）。"""
    try:
        allowed = set(
            load_schema()["properties"]["characters"]["items"]["properties"]["role"]["enum"]
        )
    except (KeyError, TypeError, AttributeError):
        allowed = {
            "protagonist",
            "supporting",
            "antagonist",
            "narrator",
            "extra",
            "unknown",
        }
    synonyms: dict[str, str] = {
        # -> supporting
        "supporter": "supporting",
        "support": "supporting",
        "support_role": "supporting",
        "supporting_character": "supporting",
        "ally": "supporting",
        "allies": "supporting",
        "sidekick": "supporting",
        "friend": "supporting",
        "teammate": "supporting",
        "partner": "supporting",
        "companion": "supporting",
        "helper": "supporting",
        "assistant": "supporting",
        "mentor": "supporting",
        "secondary": "supporting",
        "side_character": "supporting",
        "minor_character": "supporting",
        # -> protagonist
        "lead": "protagonist",
        "hero": "protagonist",
        "main": "protagonist",
        "main_character": "protagonist",
        "primary": "protagonist",
        # -> antagonist
        "villain": "antagonist",
        "enemy": "antagonist",
        "foe": "antagonist",
        "rival": "antagonist",
        "antagonistic": "antagonist",
        # -> narrator
        "narration": "narrator",
        "voiceover": "narrator",
        "voice_over": "narrator",
        "omniscient": "narrator",
        # -> extra
        "background": "extra",
        "crowd": "extra",
        "walk_on": "extra",
        "walkon": "extra",
        "cameo": "extra",
        "bit_part": "extra",
    }
    for ch in data.get("characters", []):
        if not isinstance(ch, dict):
            continue
        r = ch.get("role")
        if not isinstance(r, str):
            continue
        key = r.strip().lower().replace(" ", "_").replace("-", "_")
        if not key:
            ch.pop("role", None)
            continue
        if key in allowed:
            ch["role"] = key
            continue
        if key in synonyms and synonyms[key] in allowed:
            ch["role"] = synonyms[key]
            continue
        # 模型常输出枚举外英文词；无法识别时归为 unknown，避免反复校验失败与重复计费
        ch["role"] = "unknown"


def _normalize_scene_time_of_day(data: dict[str, Any]) -> None:
    """scene.time_of_day 常见英文变体 -> schema 枚举 dawn/day/dusk/night/unknown。"""
    try:
        allowed = set(
            load_schema()["properties"]["shots"]["items"]["properties"]["scene"]["properties"][
                "time_of_day"
            ]["enum"]
        )
    except (KeyError, TypeError, AttributeError):
        allowed = {"dawn", "day", "dusk", "night", "unknown"}
    synonyms: dict[str, str] = {
        "early_morning": "dawn",
        "sunrise": "dawn",
        "dawn_break": "dawn",
        "morning": "day",
        "noon": "day",
        "midday": "day",
        "afternoon": "day",
        "daytime": "day",
        "evening": "dusk",
        "sunset": "dusk",
        "twilight": "dusk",
        "golden_hour": "dusk",
        "dusk_time": "dusk",
        "night": "night",
        "midnight": "night",
        "late_night": "night",
        "nighttime": "night",
    }
    for shot in data.get("shots", []):
        if not isinstance(shot, dict):
            continue
        scene = shot.get("scene")
        if not isinstance(scene, dict):
            continue
        t = scene.get("time_of_day")
        if not isinstance(t, str):
            continue
        key = t.strip().lower().replace(" ", "_").replace("-", "_")
        if not key:
            scene.pop("time_of_day", None)
            continue
        if key in allowed:
            scene["time_of_day"] = key
            continue
        if key in synonyms and synonyms[key] in allowed:
            scene["time_of_day"] = synonyms[key]
            continue
        scene["time_of_day"] = "unknown"


def normalize_storyboard(data: dict[str, Any]) -> None:
    """
    在 jsonschema 校验前就地修正常见模型输出，避免景别词与枚举不完全一致导致重试。
    若本地 schema 仍为旧版（无 medium_close_up），会降级到已有枚举。
    """
    if not isinstance(data, dict):
        return
    _normalize_empty_shot_lines(data)
    _normalize_character_roles(data)
    _normalize_scene_time_of_day(data)
    _normalize_dialogue_duration(data)
    _normalize_image_numbering(data)
    schema = load_schema()
    try:
        allowed = set(
            schema["properties"]["shots"]["items"]["properties"]["visual"]["properties"][
                "shot_type"
            ]["enum"]
        )
    except (KeyError, TypeError, AttributeError):
        return
    if not allowed:
        return
    for shot in data.get("shots", []):
        vis = shot.get("visual")
        if not isinstance(vis, dict):
            continue
        st = vis.get("shot_type")
        if not isinstance(st, str):
            continue
        raw = st.strip()
        if raw in allowed:
            continue
        key = raw.lower().replace(" ", "_").replace("-", "_")
        if key in allowed:
            vis["shot_type"] = key
            continue
        # 常见同义 / 变体
        if key in ("medium_close_up", "mediumcloseup", "mcu"):
            vis["shot_type"] = (
                "medium_close_up" if "medium_close_up" in allowed else "medium"
            )
        elif key in ("extreme_close_up", "extremecloseup", "ecu", "big_close_up"):
            vis["shot_type"] = (
                "extreme_close_up" if "extreme_close_up" in allowed else "close_up"
            )
        elif key in ("establishing_shot", "master_shot"):
            vis["shot_type"] = "establishing" if "establishing" in allowed else "wide"
        else:
            vis["shot_type"] = "other"


def validate_storyboard(data: Any) -> None:
    """校验失败抛出 jsonschema.ValidationError。"""
    if isinstance(data, dict):
        normalize_storyboard(data)
    schema = load_schema()
    Draft202012Validator(schema).validate(data)


def save_storyboard(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_storyboard(path: Path) -> dict[str, Any]:
    """读取分镜 JSON（不自动校验；调用方应再 validate_storyboard）。"""
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("分镜文件须为 JSON 对象。")
    return data


def print_storyboard_diagnostics(novel_text: str, data: dict[str, Any]) -> None:
    """
    打印分镜摘要，并对原文按句做简单「是否在画面描述中出现」的启发式对照（非严格 NLP）。
    """
    blob = json.dumps(data, ensure_ascii=False)
    shots = sorted(data.get("shots") or [], key=lambda x: int(x.get("order", 0)))
    chars = data.get("characters") or []
    print("\n=== 分镜自检（与原文粗对照，不调用 API）===")
    print(f"标题：{(data.get('meta') or {}).get('title', '')}  角色数：{len(chars)}  镜头数：{len(shots)}")
    for i, sh in enumerate(shots, start=1):
        sc = sh.get("scene") or {}
        vis = sh.get("visual") or {}
        lbl = (sc.get("label") or "").strip()
        st = (vis.get("shot_type") or "").strip()
        pz = (vis.get("prompt_zh") or "")[:72].replace("\n", " ")
        if len((vis.get("prompt_zh") or "")) > 72:
            pz += "…"
        cos = vis.get("characters_on_screen") or []
        print(f"  [{i}] {lbl} | {st} | 出镜id={cos}")
        print(f"      画面：{pz}")
    # 按句号/换行切原文短句，检查是否出现在分镜 JSON 串中（弱指标）
    fragments = [t.strip() for t in re.split(r"[。\n]+", novel_text) if len(t.strip()) >= 4]
    missing: list[str] = []
    for frag in fragments[:24]:
        if frag in blob:
            continue
        # 允许子串：取前 8 字看是否在 blob
        head = frag[:10] if len(frag) >= 10 else frag
        if head and head not in blob:
            missing.append(frag[:40] + ("…" if len(frag) > 40 else ""))
    if missing:
        print("\n以下原文句段在分镜 JSON 字面中未出现（可能仍被概括进画面，请人工扫一眼 JSON）：")
        for m in missing[:12]:
            print(f"  · {m}")
    else:
        print("\n原文前若干短句均能在分镜 JSON 中找到字面或子串（弱通过）。")
