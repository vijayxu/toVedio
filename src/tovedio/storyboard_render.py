"""分镜 JSON → 文生图 prompt。"""

from __future__ import annotations

from typing import Any


def _style_suffix(style: str) -> str:
    s = (style or "real").strip().lower()
    if s == "anime":
        return "。风格：高质量二次元动画风，线条清晰，色彩分层明确。"
    return "。风格：电影级现实主义，光影自然，质感真实。"


def shot_to_image_prompt(
    shot: dict[str, Any],
    characters: list[dict[str, Any]],
    *,
    style: str = "real",
    characters_on_screen_override: list[str] | None = None,
) -> tuple[str, str]:
    """
    返回 (文生图主 prompt, 降级氛围图用的种子文本)。
    characters_on_screen_override：非 None 时替代 visual.characters_on_screen（用于空镜自动补主角 id 等）。
    """
    visual = shot["visual"]
    scene = shot["scene"]
    prompt_zh = visual["prompt_zh"]
    bg = scene["background_prompt"]
    parts: list[str] = [prompt_zh, f"环境背景：{bg}"]
    cmap = {c["id"]: c for c in characters}
    on_screen_ids = (
        list(characters_on_screen_override)
        if characters_on_screen_override is not None
        else list(visual.get("characters_on_screen") or [])
    )
    on_screen_names: list[str] = []
    for cid in on_screen_ids:
        c = cmap.get(cid)
        if not c:
            continue
        name = str(c.get("name") or "").strip()
        if name:
            on_screen_names.append(name)
        if c.get("appearance"):
            parts.append(f"{c['name']}造型：{c['appearance']}")
    if on_screen_names:
        allow = "、".join(on_screen_names)
        n_chars = len(on_screen_names)
        parts.append(
            f"出镜人物仅限：{allow}（共{n_chars}人）；"
            f"画面中人物总数严格等于{n_chars}人，禁止出现第{n_chars+1}个人或任何旁观者、路人、影子"
        )
    else:
        parts.insert(0, "no people, no humans, no person, no figures, empty scene, no silhouette, no shadow of person")
        parts.append(
            "空镜头：画面内严禁出现任何人物、人形轮廓、人影、人物剪影；"
            "纯环境景色，绝对不能有任何生命体在画面内"
        )
    # 对白镜优先“可读口型”的景别与构图，减少远景+旁白感。
    has_dialogue = any(
        isinstance(line, dict) and str(line.get("kind") or "").strip() == "dialogue"
        for line in (shot.get("lines") or [])
    )
    if has_dialogue:
        if len(on_screen_names) >= 2:
            parts.append("对白镜头建议：双人中近景或过肩镜头，双人同框可辨，避免远景")
        elif len(on_screen_names) == 1:
            parts.append("对白镜头建议：单人近景或中近景，突出口型与表情，避免远景")
        else:
            parts.append("对白镜头建议：中近景，突出说话人物口型与表情，避免远景")
        parts.append("运镜建议：固定机位或轻微推近，不要剧烈摇晃")
    neg = visual.get("negative_prompt")
    if neg:
        parts.append(f"避免：{neg}")
    image_prompt = (
        "。".join(parts)
        + "。单镜头画面，禁止双重曝光、重影叠化、拼贴式多主体重复。"
        + _style_suffix(style)
    )
    lines = shot.get("lines") or []
    mood_seed = " ".join(str(line.get("text", "")) for line in lines) or prompt_zh
    return image_prompt, mood_seed


def shot_to_sound_description(
    shot: dict[Any, Any],
    *,
    characters: list[dict[Any, Any]] | None = None,
) -> str:
    """
    按万相声音公式构建声音描述段落：人声 + 音效 + BGM。
    返回空字符串表示本镜无声音描述可用。
    """
    lines = shot.get("lines") or []
    cmap: dict[str, dict[Any, Any]] = {}
    for c in characters or []:
        if isinstance(c, dict) and c.get("id"):
            cmap[str(c["id"])] = c

    # 人声：取所有 dialogue lines
    voice_parts: list[str] = []
    for ln in lines:
        if not isinstance(ln, dict):
            continue
        if (ln.get("kind") or "").strip() != "dialogue":
            continue
        sp = str(ln.get("speaker_id") or "").strip()
        name = str((cmap.get(sp) or {}).get("name") or sp or "角色").strip()
        text = str(ln.get("text") or "").strip().replace("\n", " ")
        emotion = str(ln.get("emotion") or "").strip()
        speech_rate = str(ln.get("speech_rate") or "").strip()
        voice_note_ln = str(ln.get("voice_note") or "").strip()
        # 查 characters voice_hint 作为音色补充
        char_voice = str((cmap.get(sp) or {}).get("voice_hint") or "").strip()
        attrs: list[str] = []
        if emotion:
            attrs.append(emotion)
        if speech_rate:
            attrs.append(f"语速{speech_rate}")
        if voice_note_ln:
            attrs.append(voice_note_ln)
        elif char_voice:
            attrs.append(char_voice)
        attr_str = "，".join(attrs)
        if attr_str:
            voice_parts.append(f'{name}说道：\u201c{text}\u201d，{attr_str}')
        else:
            voice_parts.append(f'{name}说道：\u201c{text}\u201d')

    # 音效：取所有 sfx_note lines
    sfx_parts: list[str] = []
    for ln in lines:
        if not isinstance(ln, dict):
            continue
        if (ln.get("kind") or "").strip() != "sfx_note":
            continue
        text = str(ln.get("text") or "").strip()
        if text:
            sfx_parts.append(text)

    # BGM：shot 级 bgm_note
    bgm = str(shot.get("bgm_note") or "").strip()

    segs: list[str] = []
    if voice_parts:
        segs.append("人声：" + "；".join(voice_parts))
    if sfx_parts:
        segs.append("音效：" + "；".join(sfx_parts))
    if bgm:
        segs.append(f"背景音乐：{bgm}")

    return "。".join(segs) + "。" if segs else ""


def _dialogue_motion_hint(shot: dict[str, Any], characters: list[dict[str, Any]] | None) -> str:
    lines = shot.get("lines") or []
    ds = [ln for ln in lines if isinstance(ln, dict) and (ln.get("kind") or "").strip() == "dialogue"]
    if not ds:
        return ""
    cmap: dict[str, dict[str, Any]] = {}
    for c in characters or []:
        if isinstance(c, dict) and c.get("id"):
            cmap[str(c["id"])] = c
    segs: list[str] = []
    for ln in ds[:2]:
        sp = str(ln.get("speaker_id") or "").strip()
        name = str((cmap.get(sp) or {}).get("name") or sp or "角色").strip()
        tx = str(ln.get("text") or "").strip().replace("\n", " ")
        if len(tx) > 18:
            tx = tx[:18] + "…"
        segs.append(f"{name}开口说“{tx}”")
    return "；".join(segs) + "，口型与停顿自然匹配。"


def shot_to_i2v_motion_prompt(
    shot: dict[str, Any],
    *,
    style: str = "real",
    characters: list[dict[str, Any]] | None = None,
) -> str:
    """图生视频结构化提示词：运动 + 运镜（遵循万相 I2V 推荐公式）。"""
    visual = shot.get("visual") or {}
    cam = (visual.get("camera") or "").strip()
    pz = (visual.get("prompt_zh") or "").strip().replace("\n", " ")
    if len(pz) > 90:
        pz = pz[:90] + "…"
    motion = (
        f"基于首帧内容做自然连续动作演进（{pz}），动作幅度中小，节奏平稳，避免突然跳变"
        if pz
        else "基于首帧内容做自然连续动作演进，动作幅度中小，节奏平稳，避免突然跳变"
    )
    camera = cam or "固定镜头"
    dialogue_hint = _dialogue_motion_hint(shot, characters)
    has_dialogue = any(
        isinstance(line, dict) and str(line.get("kind") or "").strip() == "dialogue"
        for line in (shot.get("lines") or [])
    )
    dialogue_camera = (
        "对白镜头以固定机位或轻推近为主，优先中近景/过肩，确保口型与面部表情可见。"
        if has_dialogue
        else ""
    )
    on_screen = visual.get("characters_on_screen") or []
    if not on_screen:
        no_character_hint = (
            "画面内严禁出现任何人物、人形轮廓、人影、生命体；"
            "延续首帧纯风景，不要生成任何人物；"
        )
    else:
        n = len(on_screen)
        no_character_hint = (
            f"保持首帧人物身份、服装与场景一致；"
            f"画面中人物总数严格保持{n}人，禁止新增或变换任何人物；"
        )
    return (
        f"运动：{motion}。"
        + (f"对白：{dialogue_hint}" if dialogue_hint else "")
        + f"运镜：{camera}，以轻推、轻移或固定为主，避免剧烈甩镜。"
        + dialogue_camera
        + no_character_hint
        + "无字幕、无水印、无画面内文字。"
        + _style_suffix(style)
        + (f"声音：{sound}" if (sound := shot_to_sound_description(shot, characters=characters)) else "")
    )
