"""从剧本分镜 JSON 或制作圣经 JSON 读取角色，批量生成定妆三视图（正面/侧面/背面全身像）。

每个角色输出三张 PNG：
  {id}_costume_sheet.png       — 正面全身站姿（主参考）
  {id}_costume_sheet_side.png  — 左侧面全身站姿
  {id}_costume_sheet_back.png  — 背面全身站姿

R2V 调用时三张一起传入 ref_images，让模型从多角度理解角色外观。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .illustration import download_illustration_from_prompt

logger = logging.getLogger(__name__)

# ── appearance 清洗：剔除受伤/血迹描述，定妆照只保留服装本体 ──────────────
_INJURY_RE = re.compile(
    r"[，,]?\s*(?:右肩|左肩|肩部|左臂|右臂|手腕|额角|脸上|胸口)?[^\s，,。]{0,4}"
    r"(?:血染|血迹|血污|血浸|受伤|负伤|旧疤|伤痕|断箭|冷汗|伤口|流血)[^，,。]{0,20}",
    re.UNICODE,
)


def _clean_appearance(appearance: str) -> str:
    """去掉 appearance 里与受伤/血迹有关的片段，让定妆照只呈现正常服装状态。"""
    return _INJURY_RE.sub("", appearance).strip().strip("，,")


# ── 发型提取：跨视角锁定发型，避免侧面/背面自由发挥 ──────────────────────
_HAIR_KW_MAP = [
    ("木簪", "木簪束发"), ("发簪", "发簪束发"), ("发髻", "发髻"),
    ("束发", "束发"), ("辫", "辫发"), ("盘发", "盘发"),
    ("丸子头", "丸子头"), ("马尾", "马尾"), ("短发", "短发"), ("长发", "长发"),
]


def _extract_hair_hint(appearance: str) -> str:
    """从 appearance 中识别发型关键词，生成跨视角锁定描述。"""
    for kw, label in _HAIR_KW_MAP:
        if kw in appearance:
            return f"发型与正面完全一致（{label}），禁止改变发型；"
    return ""


# ── 古装时代锁定：禁止现代服装与现代发型 ────────────────────────────────
_ANCIENT_COSTUME_KW = [
    "劲装", "袄裙", "汉服", "襦裙", "长袍", "道袍", "披风",
    "银丝绦", "腰绦", "木簪", "发簪", "斗篷", "锦袍", "窄袖",
]
_ANCIENT_HAIR_KW = ["束发", "发髻", "髻", "辫", "发冠", "木簪", "发簪", "盘发"]


def _detect_era_hint(appearance: str) -> str:
    """检测古装关键词，返回时代+发型双重锁定描述；非古装返回空串。"""
    if not any(kw in appearance for kw in _ANCIENT_COSTUME_KW):
        return ""
    has_hair = any(kw in appearance for kw in _ANCIENT_HAIR_KW)
    hair_lock = (
        "" if has_hair
        else "发型为中国古代男性束发或发髻（禁止现代短发、板寸、飞机头等当代发型），"
    )
    return (
        f"服装时代锁定：中国古代武侠/古风服饰，{hair_lock}"
        "禁止现代T恤、西装、卫衣、运动服等当代款式；"
        "布料须呈现汉服/古装质感（棉麻/绸缎），领口为交领或立领，袖型与古装一致。"
    )


def effective_characters_on_screen_for_refs(
    shot: dict[str, Any],
    all_characters: list[dict[str, Any]],
    sheet_dir: Path | None,
    *,
    max_refs: int = 3,
) -> list[str]:
    """
    解析本镜用于定妆 subject_reference 的角色 id 列表。
    若分镜已写 characters_on_screen，原样使用；
    若为空，返回空列表（不自动塞角色，避免景色镜头被强行同框）。
    """
    vis = shot.get("visual") or {}
    explicit = [str(x).strip() for x in (vis.get("characters_on_screen") or []) if str(x).strip()]
    if explicit:
        return explicit
    return []


def resolve_costume_sheet_paths(
    sheet_dir: Path | None,
    character_ids: list[str],
    *,
    max_refs: int = 5,
) -> list[Path]:
    """
    按分镜 characters_on_screen 顺序，在 sheet_dir 下查找角色三视图：
      {id}_costume_sheet.png（正面）、_side.png（侧面）、_back.png（背面）。
    三张依次追加，最多 max_refs 张（R2V ref_images 上限 5 张）。
    若仅有正面图也可正常运行（向后兼容）。
    """
    if sheet_dir is None or not sheet_dir.is_dir():
        return []
    out: list[Path] = []
    for raw in character_ids:
        cid = str(raw).strip()
        if not cid:
            continue
        for suffix in ("_costume_sheet.png", "_costume_sheet_side.png", "_costume_sheet_back.png"):
            p = sheet_dir / f"{cid}{suffix}"
            if p.is_file():
                out.append(p)
            if len(out) >= max_refs:
                return out
    return out


def load_characters_from_script_json(path: Path) -> list[dict[str, Any]]:
    """
    读取 JSON 并校验：含 shots 则按分镜 schema；否则按制作圣经 schema。
    返回 characters 列表（每项至少含 id、name；建议含 appearance）。
    """
    from .production_bible_io import validate_production_bible
    from .storyboard_io import validate_storyboard

    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("剧本文件须为 JSON 对象。")
    if "shots" in data:
        validate_storyboard(data)
    else:
        validate_production_bible(data)
    chars = data.get("characters") or []
    if not isinstance(chars, list) or not chars:
        raise ValueError("JSON 中缺少非空的 characters 数组。")
    for c in chars:
        if not isinstance(c, dict) or not str(c.get("id") or "").strip():
            raise ValueError("characters 每项必须为非空对象且含 id。")
    return chars


def _style_guard(style: str) -> str:
    s = (style or "real").strip().lower()
    if s == "anime":
        return (
            "风格锁定：高质量二维动漫角色设定图，统一赛璐璐上色，线稿干净，"
            "面部比例写实偏动漫，非Q版、非3D玩偶、非真人摄影风。"
        )
    return "风格锁定：写实电影人像摄影，真实皮肤与布料质感，非动漫卡通风。"


_VIEW_SPECS = [
    # (文件后缀, 中文视角描述, 构图约束)
    (
        "_costume_sheet.png",
        "正面",
        "角色正面站姿全身像（头顶至脚尖），面朝镜头，双手自然垂放，双脚平行微分，"
        "正脸完整可见，头发正面层次清晰，服装正面所有细节清晰可辨",
    ),
    (
        "_costume_sheet_side.png",
        "左侧面",
        "角色左侧面90度站姿全身像（头顶至脚尖），侧身朝左，侧脸轮廓与鼻梁线条清晰，"
        "发型侧面层次与发尾走向可见，服装侧面剪影与腰部结构清晰",
    ),
    (
        "_costume_sheet_back.png",
        "背面",
        "角色正背面站姿全身像（头顶至脚尖），背对镜头，发型背面形态与发尾完整可见，"
        "服装后背结构、腰带/裙摆/披风后摆等细节清晰，禁止出现正脸",
    ),
]


def _view_prompt(
    name: str,
    appearance: str,
    view_label: str,
    composition_hint: str,
    *,
    style: str,
) -> tuple[str, str]:
    raw_app = (appearance or "").strip()
    app = _clean_appearance(raw_app) or "造型与设定一致，细节清晰可辨"
    era_hint = _detect_era_hint(raw_app)
    hair_hint = _extract_hair_hint(raw_app)
    zh = (
        f"影视定妆三视图——{view_label}，中性深灰摄影棚背景，均匀环境光，无强烈阴影，{_style_guard(style)}"
        f"{era_hint}"
        f"单人出镜，禁止双人同框。人物姓名：{name}。造型与服装：{app}。"
        f"{hair_hint}"
        f"{composition_hint}。"
        "无血迹、无伤口、无污迹，服装干净整洁（定妆参考图，非剧情画面）；"
        "角色为成年设定；姿态自然克制；禁止儿童化与性感化表达；"
        "禁止画面内文字、字幕、水印、Logo；禁止畸形手指；"
        "禁止切换到与上述锁定不一致的画风。"
    )
    mood = f"{name} {view_label} {app}"
    return zh, mood


def _view_prompt_minimal(view_label: str, *, style: str) -> tuple[str, str]:
    """触审时安全降级 prompt。"""
    style_hint = (
        "写实人像摄影，电影级质感，真实皮肤与布料质感，非动漫卡通"
        if (style or "real").strip().lower() == "real"
        else "二维动漫角色立绘，统一赛璐璐上色，干净线稿，非Q版、非3D玩偶"
    )
    zh = (
        f"影视定妆三视图——{view_label}，{style_hint}，中性灰色摄影棚背景，均匀环境光，"
        "单人成年角色全身站姿（头顶至脚尖），中国传统古装人物，衣着层次完整、端庄得体，"
        "姿态自然克制；禁止儿童化与性感化；"
        "禁止画面内任何文字、水印、Logo；禁止畸形手指；禁止裸露或暗示性内容。"
    )
    return zh, f"costume_sheet_{view_label}_minimal_safe"


def _is_minimax_sensitive_block(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "1026" in msg or "new_sensitive" in msg or "sensitive" in msg


def _generate_one_view(
    img_prompt: str,
    mood: str,
    out_png: Path,
    *,
    scene_index: int,
    style: str,
    view_label: str,
    strict_illustration: bool,
) -> None:
    """生成单张视角定妆图，失败时按触审/普通错误两类降级。"""
    try:
        download_illustration_from_prompt(
            img_prompt, mood, out_png,
            scene_index=scene_index, strict_illustration=True, style=style,
        )
    except RuntimeError as e:
        if _is_minimax_sensitive_block(e):
            logger.warning("定妆照 %s（%s视角）触审，改用安全 prompt 重试…", out_png.name, view_label)
            mp, mm = _view_prompt_minimal(view_label, style=style)
            got = download_illustration_from_prompt(
                mp, mm, out_png,
                scene_index=scene_index, strict_illustration=False, style=style,
            )
            if not got and (style or "real").strip().lower() == "anime":
                logger.warning("定妆照 %s（%s视角）动漫安全 prompt 仍触审，改写实兜底…", out_png.name, view_label)
                rp, rm = _view_prompt_minimal(view_label, style="real")
                download_illustration_from_prompt(
                    rp, rm, out_png,
                    scene_index=scene_index, strict_illustration=False, style="real",
                )
        elif strict_illustration:
            raise
        else:
            logger.warning("定妆照 %s（%s视角）在线生成失败（%s），使用本地降级…", out_png.name, view_label, e)
            download_illustration_from_prompt(
                img_prompt, mood, out_png,
                scene_index=scene_index, strict_illustration=False, style=style,
            )


def generate_character_costume_sheets(
    script_json: Path,
    output_dir: Path,
    *,
    style: str = "real",
    strict_illustration: bool = False,
) -> list[Path]:
    """
    为每个角色生成三视图定妆照（正面/左侧面/背面全身站姿），共 3 张 PNG：
      {character_id}_costume_sheet.png
      {character_id}_costume_sheet_side.png
      {character_id}_costume_sheet_back.png
    返回已写入路径列表。
    """
    characters = load_characters_from_script_json(script_json)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    total = len(characters) * len(_VIEW_SPECS)
    idx_global = 0
    for c in characters:
        cid = str(c["id"]).strip()
        name = str(c.get("name") or cid).strip()
        appearance = str(c.get("appearance") or "").strip()
        logger.info("角色 %s（%s）：生成三视图定妆照…", cid, name)
        for suffix, view_label, composition_hint in _VIEW_SPECS:
            out_png = output_dir / f"{cid}{suffix}"
            img_prompt, mood = _view_prompt(name, appearance, view_label, composition_hint, style=style)
            logger.info(
                "  [%d/%d] %s %s视角 → %s",
                idx_global + 1, total, name, view_label, out_png.name,
            )
            _generate_one_view(
                img_prompt, mood, out_png,
                scene_index=idx_global,
                style=style,
                view_label=view_label,
                strict_illustration=strict_illustration,
            )
            written.append(out_png)
            idx_global += 1
    manifest = {
        "source_script": str(script_json.resolve()),
        "style": style,
        "views": ["front", "side", "back"],
        "characters": [
            {
                "id": str(c["id"]),
                "name": c.get("name"),
                "sheets": {
                    "front": f"{c['id']}_costume_sheet.png",
                    "side": f"{c['id']}_costume_sheet_side.png",
                    "back": f"{c['id']}_costume_sheet_back.png",
                },
            }
            for c in characters
        ],
    }
    (output_dir / "character_sheets_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("已写入 %d 张三视图定妆照（%d 角色 × 3 视角）至 %s", len(written), len(characters), output_dir)
    return written
