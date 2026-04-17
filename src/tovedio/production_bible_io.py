"""制作圣经：选角、定妆、搭景；校验、保存与并入分镜。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

from .storyboard_io import _normalize_character_roles, project_root


def production_bible_schema_path() -> Path:
    return project_root() / "docs" / "production_bible.schema.json"


def load_production_bible_schema() -> dict[str, Any]:
    p = production_bible_schema_path()
    if not p.is_file():
        raise FileNotFoundError(f"找不到制作圣经 Schema：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _normalize_location_time_of_day(data: dict[str, Any]) -> None:
    allowed = {"dawn", "day", "dusk", "night", "unknown"}
    synonyms: dict[str, str] = {
        "early_morning": "dawn",
        "morning": "day",
        "afternoon": "day",
        "evening": "dusk",
        "sunset": "dusk",
        "night": "night",
        "midnight": "night",
    }
    for loc in data.get("locations", []):
        if not isinstance(loc, dict):
            continue
        t = loc.get("time_of_day")
        if not isinstance(t, str):
            continue
        key = t.strip().lower().replace(" ", "_").replace("-", "_")
        if key in allowed:
            loc["time_of_day"] = key
        elif key in synonyms:
            loc["time_of_day"] = synonyms[key]
        else:
            loc["time_of_day"] = "unknown"


def normalize_production_bible(data: dict[str, Any]) -> None:
    """就地修正 role / time_of_day 等常见模型变体。"""
    if not isinstance(data, dict):
        return
    _normalize_character_roles({"characters": data.get("characters", [])})
    _normalize_location_time_of_day(data)


def validate_production_bible(data: Any) -> None:
    if isinstance(data, dict):
        normalize_production_bible(data)
    schema = load_production_bible_schema()
    Draft202012Validator(schema).validate(data)


def load_production_bible(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("制作圣经须为 JSON 对象。")
    validate_production_bible(data)
    return data


def save_production_bible(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_location_bible_text(locations: list[dict[str, Any]]) -> str:
    """拼成一段注入文生图的全片场景锁定文案。"""
    if not locations:
        return ""
    parts: list[str] = []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        lab = str(loc.get("label") or loc.get("id") or "").strip()
        lid = str(loc.get("id") or "").strip()
        env = str(loc.get("environment_prompt") or "").strip()
        if not env:
            continue
        mood = str(loc.get("mood") or "").strip()
        tod = str(loc.get("time_of_day") or "").strip()
        extra = ""
        if tod and tod != "unknown":
            extra += f"，时段：{tod}"
        if mood:
            extra += f"，氛围：{mood}"
        parts.append(f"「{lab}」（{lid}{extra}）：{env}")
    if not parts:
        return ""
    return "场景美术锁定（全片须与下列主场景一致，陈设与光色不得无故跳变）：" + "；".join(parts) + "。"


def apply_production_bible_to_storyboard(storyboard: dict[str, Any], bible: dict[str, Any]) -> None:
    """
    用制作圣经中的 characters 覆盖分镜顶层角色表，并校验分镜引用的角色 id 均在圣经内。
    就地修改 storyboard。
    """
    b_chars = bible.get("characters") or []
    valid_ids = {str(c["id"]) for c in b_chars if isinstance(c, dict) and c.get("id")}
    if not valid_ids:
        raise ValueError("制作圣经中无有效角色 id。")
    shots = storyboard.get("shots") or []
    for si, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        vis = shot.get("visual")
        if isinstance(vis, dict):
            for cid in vis.get("characters_on_screen") or []:
                if str(cid) not in valid_ids:
                    raise ValueError(
                        f"镜头 order={shot.get('order')} 的 characters_on_screen 含未知角色 id「{cid}」，"
                        f"不在制作圣经中。请重试分镜生成或修订圣经/分镜 JSON。"
                    )
        for li, line in enumerate(shot.get("lines") or []):
            if not isinstance(line, dict):
                continue
            if line.get("kind") != "dialogue":
                continue
            sp = line.get("speaker_id")
            if sp is not None and str(sp) not in valid_ids:
                raise ValueError(
                    f"镜头 order={shot.get('order')} 对白 speaker_id「{sp}」不在制作圣经角色表中。"
                )
    storyboard["characters"] = copy.deepcopy(b_chars)
