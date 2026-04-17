from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
from typing import Any
import shutil
import subprocess
import textwrap
import threading
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .illustration import download_illustration_png
from .paths import create_temp_workdir, new_staging_path

logger = logging.getLogger(__name__)

WIDTH, HEIGHT = 1280, 720
BG_COLOR = (32, 36, 48)
TEXT_COLOR = (240, 240, 245)
MARGIN = 56
FONT_SIZE = 28


def _windows_font_candidates() -> list[Path]:
    windir = Path(os.environ.get("WINDIR", "C:/Windows"))
    fonts = windir / "Fonts"
    return [
        fonts / "msyh.ttc",
        fonts / "msyhbd.ttc",
        fonts / "simsun.ttc",
        fonts / "simhei.ttf",
        fonts / "msjh.ttc",
    ]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for p in _windows_font_candidates():
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except OSError:
                continue
    for name in ("DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    logger.warning("未找到常见中文字体，使用默认位图字体，中文可能显示为方块。")
    return ImageFont.load_default()


def split_scenes(text: str, max_chars: int) -> list[str]:
    """按空行分段，过长段落再按 max_chars 切分。"""
    text = text.strip()
    if not text:
        return []
    paragraphs = re.split(r"\n\s*\n+", text)
    scenes: list[str] = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        while len(p) > max_chars:
            scenes.append(p[:max_chars])
            p = p[max_chars:]
        if p:
            scenes.append(p)
    return scenes


def render_scene_image(text: str, out_png: Path, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> None:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)
    max_w = WIDTH - 2 * MARGIN
    chars_per_line = max(10, max_w // (FONT_SIZE // 2))
    raw_lines: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        raw_lines.extend(textwrap.wrap(para, width=chars_per_line, break_long_words=True, break_on_hyphens=False))
    if not raw_lines:
        raw_lines = ["（空场景）"]
    y = MARGIN
    line_gap = 8
    for line in raw_lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        h = bbox[3] - bbox[1]
        if y + h > HEIGHT - MARGIN:
            draw.text((MARGIN, y), "…", fill=TEXT_COLOR, font=font)
            break
        draw.text((MARGIN, y), line, fill=TEXT_COLOR, font=font)
        y += h + line_gap
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png, format="PNG")


def _run_ffmpeg(args: list[str]) -> None:
    r = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(f"ffmpeg 失败 (exit {r.returncode}): {err[:2000]}")


def _run_ffprobe(args: list[str]) -> str:
    r = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(f"ffprobe 失败 (exit {r.returncode}): {err[:2000]}")
    return (r.stdout or "").strip()


def ensure_ffprobe() -> str:
    """与 ensure_ffmpeg 同目录，用于探测媒体时长。"""
    path = shutil.which("ffprobe")
    if path:
        return path
    ffmpeg_path = ensure_ffmpeg()
    p = Path(ffmpeg_path)
    sibling = p.parent / ("ffprobe.exe" if p.name.lower() == "ffmpeg.exe" else "ffprobe")
    if sibling.is_file():
        return str(sibling)
    raise RuntimeError(
        "未找到 ffprobe。请与 ffmpeg 一并安装（如 winget install Gyan.FFmpeg），并将 bin 加入 PATH。"
    )


def analyze_mp4_local(path: Path) -> None:
    """ffprobe 打印成片技术信息（纯本地，无 API）。"""
    import json

    probe = ensure_ffprobe()
    r = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "ffprobe 失败")[:2000])
    j = json.loads(r.stdout)
    fmt = j.get("format") or {}
    dur = float(fmt.get("duration") or 0)
    size_b = int(fmt.get("size") or 0)
    streams = j.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    print(f"\n=== 本地成片分析（无 API）：{path} ===")
    print(f"时长: {dur:.2f}s  约 {size_b / 1024 / 1024:.2f} MB")
    if v:
        br = int(v.get("bit_rate") or 0)
        print(
            f"视频: {v.get('codec_name')} {v.get('width')}x{v.get('height')} "
            f"r_frame_rate={v.get('r_frame_rate')} 约 {br // 1000} kbps"
        )
    if a:
        ad = float(a.get("duration") or 0)
        print(
            f"音频: {a.get('codec_name')} {a.get('sample_rate')}Hz "
            f"{a.get('channels')}ch 时长≈{ad:.2f}s"
        )
    else:
        print("音频: 无")
    print("\n相关工程提示：")
    print("  · 尾段长时间静止：可调 TOVEDIO_L2V_MUX_MAX_VIDEO_PAD_SEC（混流末帧上限）、")
    print("    TOVEDIO_I2V_TPAD_MAX_SEC、TOVEDIO_I2V_SEGMENT_MAX_STRETCH 或略减 -s")
    print("  · 剧情与画面不符：py -3 run_tovedio.py --validate-storyboard sb.json --input 小说.txt（免费对照）")
    print("  · 只重生成分镜：--storyboard-only --save-storyboard sb.json --input 小说.txt（仅 MiniMax 分镜费）")


def media_duration_sec(path: Path) -> float:
    """返回容器时长（秒），失败则抛错。"""
    probe = ensure_ffprobe()
    out = _run_ffprobe(
        [
            probe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    if not out:
        raise RuntimeError(f"无法读取时长：{path}")
    return float(out)


def _extend_video_clone_last_frame(
    ffmpeg_exe: str,
    input_mp4: Path,
    extra_sec: float,
    output_mp4: Path,
) -> None:
    """在片尾用最后一帧冻结延长 extra_sec（Phase A：旁长长于画面时对齐）。"""
    if extra_sec <= 0:
        shutil.copy2(input_mp4, output_mp4)
        return
    es = f"{extra_sec:.3f}"
    _run_ffmpeg(
        [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_mp4),
            "-vf",
            f"tpad=stop_mode=clone:stop_duration={es}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(output_mp4),
        ]
    )


def _l2v_mux_max_video_pad_sec() -> float:
    """
    为对齐 TTS，成片末帧克隆的最大秒数（全片一次，非逐镜）。
    默认 4s：避免旁白远长于画面时出现数十秒「定格」；过大则恢复旧行为。
    设 0 则不垫视频，混流用 -shortest 截断旁白尾部。
    """
    raw = (os.environ.get("TOVEDIO_L2V_MUX_MAX_VIDEO_PAD_SEC") or "4").strip()
    try:
        m = float(raw)
    except ValueError:
        m = 4.0
    return max(0.0, min(600.0, m))




def _crossfade_duration_sec_for_segments(durations: list[float]) -> float:
    """
    镜头之间淡入淡出秒数（最后一步合成，不拖慢单段编码）。
    默认 0.12s：略软化硬切，更贴影视观感；硬切可设 TOVEDIO_CROSSFADE=0。
    多段时长不一致时，用最短镜长约束 d，避免 xfade 非法。
    """
    if not durations:
        return 0.0
    raw = (os.environ.get("TOVEDIO_CROSSFADE") or "0.12").strip()
    try:
        d = float(raw)
    except ValueError:
        d = 0.0
    if d <= 0:
        return 0.0
    shortest = min(durations)
    # Too long dissolve often causes "ghosting/double-exposure" artifact.
    max_d = max(0.05, shortest * 0.18)
    return min(d, max_d)


def _l2v_max_shots() -> int:
    raw = (os.environ.get("TOVEDIO_MAX_SHOTS") or "6").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 6
    return max(4, min(12, n))


def _l2v_chain_default_enabled() -> bool:
    """尾帧链式 L2V：默认开启（架构升级）。环境变量 TOVEDIO_L2V_CHAIN=0 可关闭。"""
    v = (os.environ.get("TOVEDIO_L2V_CHAIN") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _character_sheet_dir_has_sheets(sheet_root: Path | None) -> bool:
    """定妆目录下是否存在至少一张 {id}_costume_sheet.png（与 character_sheets 输出约定一致）。"""
    if sheet_root is None or not sheet_root.is_dir():
        return False
    try:
        return any(sheet_root.glob("*_costume_sheet.png"))
    except OSError:
        return False


def _resolve_l2v_chain_flag(
    l2v_chain: bool | None,
    *,
    sheet_root: Path | None,
) -> bool:
    """
    有定妆 PNG 时强制关闭尾帧链式：链式用上一镜视频尾帧作百炼输入，该帧未经 MiniMax subject_reference，
    必然与定妆不一致（脸漂）。无定妆时：CLI 显式传入优先，否则读 TOVEDIO_L2V_CHAIN。
    """
    if _character_sheet_dir_has_sheets(sheet_root):
        if l2v_chain is True:
            logger.warning(
                "已配置定妆目录（含 *_costume_sheet.png）：尾帧链式与定妆一致性冲突，已强制关闭链式 L2V。"
            )
        return False
    if l2v_chain is not None:
        return bool(l2v_chain)
    return _l2v_chain_default_enabled()


def _l2v_chain_refresh_interval() -> int:
    """
    每 N 镜强制重新文生图关键帧（0=不刷新），防止长链场景漂移。
    例如 TOVEDIO_L2V_CHAIN_REFRESH=3 表示第 0、3、6… 镜用关键帧。
    """
    raw = (os.environ.get("TOVEDIO_L2V_CHAIN_REFRESH") or "0").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 0
    return max(0, min(20, n))


def _l2v_partial_on_fail_enabled() -> bool:
    """L2V 某一镜失败时，是否将已成功片段先合并为部分成片（默认开启）。"""
    v = (os.environ.get("TOVEDIO_L2V_PARTIAL_ON_FAIL") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _is_bailian_l2v_data_inspection_failed(exc: BaseException) -> bool:
    """百炼 L2V 创建任务或任务结果中的内容审核类错误（不重试同输入无意义）。"""
    msg = str(exc)
    if "DataInspectionFailed" in msg:
        return True
    if "inappropriate content" in msg.lower() and ("HTTP 400" in msg or "400" in msg):
        return True
    return False


def _is_bailian_data_inspection_failed(exc: BaseException) -> bool:
    """百炼通用内容审核失败判定（T2V/L2V）。"""
    return _is_bailian_l2v_data_inspection_failed(exc)


def _l2v_chain_inspection_keyframe_retry_enabled() -> bool:
    """
    尾帧链式 L2V 若因内容审核失败，是否自动改本镜关键帧再试一次（默认开启）。
    关闭：TOVEDIO_L2V_CHAIN_INSPECTION_RETRY=0
    """
    v = (os.environ.get("TOVEDIO_L2V_CHAIN_INSPECTION_RETRY") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _l2v_safe_keyframe_t2i_prompt_after_fail(
    shot: dict[str, Any],
    chars: list[Any],
    *,
    style: str,
    location_bible: str,
    characters_on_screen_override: list[str] | None = None,
) -> tuple[str, str]:
    """触审后关键帧重试：带定妆/长叙事触 MiniMax 时，用短文案、无定妆再试，降低 1026 与色场送审。"""
    from .storyboard_render import shot_to_image_prompt

    core, mood = shot_to_image_prompt(
        shot,
        chars,
        style=style,
        characters_on_screen_override=characters_on_screen_override,
    )
    loc = (location_bible or "").strip()
    if len(loc) > 280:
        loc = loc[:280] + "…"
    st = (style or "real").strip().lower()
    style_kw = "写实电影剧照" if st == "real" else "日式动画单帧"
    parts: list[str] = [
        f"16:9，{style_kw}，安静克制，无字幕水印。",
        core,
    ]
    if loc:
        parts.append(f"场景：{loc}")
    parts.append("禁止裸露、血腥、暧昧特写或暴力细节。")
    return "".join(parts), mood


def _l2v_inspection_retry_safe_motion(*, style: str) -> str:
    """
    尾帧链式触审后的 L2V 重试：百炼会同时审核 prompt 与首帧图。
    此处仅用中性运镜描述，避免再带上分镜剧情/圣经/桥接等长中文触发误杀。
    """
    st = (style or "real").strip().lower()
    if st == "anime":
        return (
            "缓慢横移或轻微推拉，角色与环境微动，光影稳定；"
            "无字幕、无水印、无画面内文字；动作克制，构图保持首帧。"
        )
    return (
        "缓慢电影感推镜或轻摇，景深微调，环境细微动态；"
        "无字幕、无水印、无画面内文字；人物动作轻微自然，整体构图与首帧一致。"
    )


def _l2v_minimal_bailian_prompt_enabled(*, cli_flag: bool) -> bool:
    """向百炼 L2V 只送短 motion（不含故事锚点/叙事链等长文本），降低输入侧内容审核误杀。CLI 优先，否则读 TOVEDIO_L2V_MINIMAL_PROMPT。"""
    if cli_flag:
        return True
    v = (os.environ.get("TOVEDIO_L2V_MINIMAL_PROMPT") or "").strip().lower()
    return v in ("1", "true", "yes", "on")



def _l2v_force_keyframe_shot(i: int, refresh_every: int) -> bool:
    if i == 0:
        return True
    if refresh_every <= 0:
        return False
    return i % refresh_every == 0


def _extract_last_frame_png(ffmpeg_exe: str, video_mp4: Path, out_png: Path) -> None:
    """从视频尾部截取一帧 PNG（与项目分辨率一致），供下一镜链式 L2V。"""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    dur = media_duration_sec(video_mp4)
    ss = max(0.0, dur - 0.2)
    vf = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2"
    )
    _run_ffmpeg(
        [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_mp4),
            "-ss",
            f"{ss:.3f}",
            "-vframes",
            "1",
            "-vf",
            vf,
            str(out_png),
        ]
    )


def _chain_tail_motion_prefix(shot: dict) -> str:
    """链式模式下的简短连续性约束（避免长文案触发输入审核）。"""
    sc_lbl = str((shot.get("scene") or {}).get("label") or "").strip()
    loc = f"场景延续：{sc_lbl}。" if sc_lbl else ""
    return (
        "延续上一镜尾帧的时间与空间，不跳切。"
        "人物身份与服装保持一致，动作自然衔接。"
        f"{loc}"
    )


def _series_visual_lock(*, style: str, novel_text: str) -> str:
    """
    全片统一视觉圣经：缓解每镜独立生成导致的"像不同视频拼在一起"。
    """
    first = ""
    for line in novel_text.strip().splitlines():
        t = line.strip()
        if t:
            first = t[:140]
            break
    era_lock = (
        "时代锁定：本片为中国古代（明清风格）写实场景，"
        "室内场景必须是古风木构建筑（木梁、土墙或刷白墙、木榻、油灯/蜡烛），"
        "严禁出现任何现代元素（玻璃窗、灯泡、电器、现代家具、瓷砖、现代门窗）；"
        "室外场景为山林雪地，不得出现现代建筑或公路。"
    )
    if style == "anime":
        lock = (
            "全片视觉锁定：这是同一支连续短片的多个镜头，不是混剪合集；"
            "线稿风格、上色方式、光影逻辑、色相倾向必须全程一致；"
            "禁止中途从平面二次元变成三渲二或写实 CG；"
            "禁止每段换一套完全不同的调色或时代感。"
        )
    else:
        lock = (
            "全片视觉锁定：这是同一支连续短片的多个镜头，不是混剪合集；"
            "摄影质感、对比度、颗粒与调色倾向必须全程一致；"
            "禁止中途像切换到另一支广告片或另一部电影的美术；"
            "灯光逻辑（主光方向、冷暖比）在相邻镜头应可衔接。"
        )
    if first:
        return f"{era_lock}{lock}故事锚点（全片沿用同一世界观与氛围）：{first}"
    return f"{era_lock}{lock}"


_XFADE_BATCH_SIZE = 6  # 超过此值时分批递归合并，避免超长滤镜链


def _xfade_batch_concat(
    ffmpeg_exe: str,
    segments: list[Path],
    output_mp4: Path,
    durations: list[float],
    d: float,
    tmp_dir: Path,
) -> None:
    """对 ≤ _XFADE_BATCH_SIZE 个片段执行单次 xfade filter_complex 合并。"""
    n = len(segments)
    if n == 1:
        _run_ffmpeg(
            [ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(segments[0]), "-c", "copy", str(output_mp4)]
        )
        return
    fc_parts: list[str] = []
    for i in range(1, n):
        prev = "[0:v]" if i == 1 else f"[v{i - 1}]"
        nxt = f"[{i}:v]"
        out = f"v{i}"
        off = round(sum(durations[:i]) - i * d, 6)
        dd = round(d, 6)
        fc_parts.append(
            f"{prev}{nxt}xfade=transition=fade:duration={dd}:offset={off}[{out}]"
        )
    fc = ";".join(fc_parts)
    last = f"v{n - 1}"
    args: list[str] = [ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error"]
    for s in segments:
        args += ["-i", str(s)]
    args += [
        "-filter_complex", fc,
        "-map", f"[{last}]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        str(output_mp4),
    ]
    _run_ffmpeg(args)


def _merge_video_segments(
    ffmpeg_exe: str,
    segments: list[Path],
    output_mp4: Path,
    uniform_segment_duration: float,
    *,
    segment_durations: list[float] | None = None,
    _tmp_dir: Path | None = None,
) -> None:
    """多段 H.264 合成一支成片：可选 xfade 淡化切换；支持每段不同时长。
    xfade 超过 _XFADE_BATCH_SIZE 段时分批递归合并，避免超长滤镜链。
    """
    n = len(segments)
    if n == 0:
        raise ValueError("没有可拼接的视频片段。")
    if segment_durations is not None and len(segment_durations) != n:
        raise ValueError("segment_durations 数量必须与片段数一致。")
    durations = segment_durations if segment_durations is not None else [uniform_segment_duration] * n
    if n == 1:
        _run_ffmpeg(
            [
                ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(segments[0]), "-c", "copy", str(output_mp4),
            ]
        )
        return
    d = _crossfade_duration_sec_for_segments(durations)
    if d <= 0:
        lines = [f"file '{s.as_posix()}'" for s in segments]
        concat_path = new_staging_path(prefix="tovedio_concat_", suffix=".txt")
        try:
            concat_path.write_text("\n".join(lines), encoding="utf-8")
            _run_ffmpeg(
                [
                    ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "concat", "-safe", "0", "-i", str(concat_path),
                    "-c", "copy", str(output_mp4),
                ]
            )
        finally:
            try:
                concat_path.unlink(missing_ok=True)
            except OSError:
                pass
        return

    # xfade 路径：超出批量上限时分批递归合并
    if n <= _XFADE_BATCH_SIZE:
        logger.info(
            "合成成片：%d 段，镜头间淡化 %.2f s（TOVEDIO_CROSSFADE=0 为硬切）…",
            n, d,
        )
        _xfade_batch_concat(ffmpeg_exe, segments, output_mp4, durations, d, output_mp4.parent)
        return

    # 分批：每 _XFADE_BATCH_SIZE 个先合成一段中间文件，再递归合并
    logger.info(
        "合成成片：%d 段超出批量上限 %d，分批 xfade 合并（%.2f s 淡化）…",
        n, _XFADE_BATCH_SIZE, d,
    )
    tmp_dir = _tmp_dir or output_mp4.parent
    batches = [segments[i:i + _XFADE_BATCH_SIZE] for i in range(0, n, _XFADE_BATCH_SIZE)]
    dur_batches = [durations[i:i + _XFADE_BATCH_SIZE] for i in range(0, n, _XFADE_BATCH_SIZE)]
    intermediates: list[Path] = []
    inter_durations: list[float] = []
    for bi, (batch, bdurs) in enumerate(zip(batches, dur_batches)):
        inter = tmp_dir / f"_xfade_inter_{bi:04d}.mp4"
        _xfade_batch_concat(ffmpeg_exe, batch, inter, bdurs, d, tmp_dir)
        intermediates.append(inter)
        # 中间段时长 = 各段时长之和 - (N-1) × crossfade
        merged_dur = max(0.1, sum(bdurs) - (len(bdurs) - 1) * d)
        inter_durations.append(merged_dur)
    # 递归合并中间段
    _merge_video_segments(
        ffmpeg_exe,
        intermediates,
        output_mp4,
        uniform_segment_duration,
        segment_durations=inter_durations,
        _tmp_dir=tmp_dir,
    )
    for p in intermediates:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def _run_ffmpeg_with_heartbeat(args: list[str], label: str, *, interval_sec: float = 10.0) -> None:
    """
    Ken Burns 等重滤镜会长时间无输出，易被误以为卡死；期间每隔 interval_sec 打一行心跳日志。
    """
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(interval_sec):
            if proc.poll() is not None:
                return
            logger.info(
                "仍在编码 %s（ffmpeg 处理中，CPU 占用高属正常，非死机）…",
                label,
            )

    t = threading.Thread(target=_beat, daemon=True)
    t.start()
    try:
        _out, err = proc.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"ffmpeg 编码超过 15 分钟超时：{label}") from None
    finally:
        stop.set()
    if proc.returncode != 0:
        msg = (err or "").strip()
        raise RuntimeError(f"ffmpeg 失败 (exit {proc.returncode}): {msg[:2000]}")


def _find_ffmpeg_windows() -> str | None:
    """winget 安装的 Gyan.FFmpeg 常在用户目录，未必已写入当前会话 PATH。"""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
    if not packages.is_dir():
        return None
    matches = sorted(packages.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"))
    for p in matches:
        if p.is_file():
            return str(p)
    return None


def ensure_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path:
        return path
    if os.name == "nt":
        path = _find_ffmpeg_windows()
        if path:
            return path
    raise RuntimeError(
        "未找到 ffmpeg。请安装（如 winget install Gyan.FFmpeg）并将安装目录下的 bin 加入系统 PATH，"
        "或关闭并重新打开终端后再试。"
    )


def _ken_burns_enabled() -> bool:
    """
    Ken Burns（zoompan）画质好但 CPU 很慢。默认关闭，走静图直出片段（快）。
    需要推镜时设 TOVEDIO_KEN_BURNS=1。
    """
    v = (os.environ.get("TOVEDIO_KEN_BURNS") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _ken_burns_scale_width() -> int:
    """
    Ken Burns 前先把图拉宽的像素上限。过大（如 8000）会导致 ffmpeg 极慢、看似卡死。
    默认 2880 足够支撑轻微推镜；可设 TOVEDIO_KEN_BURNS_SCALE=3200 略增画质。
    """
    raw = (os.environ.get("TOVEDIO_KEN_BURNS_SCALE") or "2880").strip()
    try:
        w = int(raw)
    except ValueError:
        w = 2880
    return max(1920, min(w, 4096))


def _ken_burns_fps() -> int:
    """Ken Burns 输出帧率；默认 15 以减轻 CPU（25 更顺滑但更慢）。可设 TOVEDIO_KEN_BURNS_FPS。"""
    raw = (os.environ.get("TOVEDIO_KEN_BURNS_FPS") or "15").strip()
    try:
        f = int(raw)
    except ValueError:
        f = 15
    return max(8, min(f, 30))


def _cinematic_env_explicit(cinematic_param: bool) -> bool:
    """CLI --cinematic 或 TOVEDIO_CINEMATIC=1。"""
    if cinematic_param:
        return True
    v = (os.environ.get("TOVEDIO_CINEMATIC") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _want_subtle_drift(cinematic_param: bool) -> bool:
    """轻推镜：比 Ken Burns 省算力，减轻静帧幻灯片感。TOVEDIO_SUBTLE_DRIFT=1 可单独开。"""
    if _ken_burns_enabled():
        return False
    if _cinematic_env_explicit(cinematic_param):
        return True
    v = (os.environ.get("TOVEDIO_SUBTLE_DRIFT") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _want_film_grade(cinematic_param: bool) -> bool:
    """轻微对比度/饱和度，偏电影观感。TOVEDIO_FILM_LOOK=1 可单独开。"""
    if _cinematic_env_explicit(cinematic_param):
        return True
    v = (os.environ.get("TOVEDIO_FILM_LOOK") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _film_grade_vf_suffix(cinematic_param: bool) -> str:
    if not _want_film_grade(cinematic_param):
        return ""
    return ",eq=contrast=1.06:brightness=-0.02:saturation=1.08"


def _subtle_drift_fps() -> int:
    """
    轻推镜输出帧率；默认 12。zoompan 逐帧 CPU 开销大，可设 TOVEDIO_SUBTLE_DRIFT_FPS=8 明显加快。
    """
    raw = (os.environ.get("TOVEDIO_SUBTLE_DRIFT_FPS") or "12").strip()
    try:
        f = int(raw)
    except ValueError:
        f = 12
    return max(6, min(f, 24))


def _subtle_drift_scale_w() -> int:
    """
    轻推镜前横向缩放像素上限；默认 2208。越小越快（画质略降），如 TOVEDIO_SUBTLE_DRIFT_SCALE=1600。
    """
    raw = (os.environ.get("TOVEDIO_SUBTLE_DRIFT_SCALE") or "2208").strip()
    try:
        w = int(raw)
    except ValueError:
        w = 2208
    return max(960, min(w, 3200))


def _ffmpeg_png_to_segment(
    ffmpeg_exe: str,
    png: Path,
    out_seg: Path,
    seconds: float,
    *,
    log_ken_burns_hint: bool = False,
    segment_label: str = "",
    cinematic: bool = False,
) -> None:
    """
    单张 PNG → 一支 H.264 片段。
    优先级：Ken Burns（TOVEDIO_KEN_BURNS）> 轻推镜（--cinematic / TOVEDIO_SUBTLE_DRIFT）> 静图冻结。
    """
    if seconds <= 0:
        raise ValueError("每段时长必须大于 0。")
    label = segment_label or "片段"
    x264_fast = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-threads", "0"]
    grade = _film_grade_vf_suffix(cinematic)

    if _ken_burns_enabled():
        if log_ken_burns_hint:
            logger.info(
                "Ken Burns 推镜已启用（较慢）；默认关闭推镜，追求速度请勿设置 TOVEDIO_KEN_BURNS=1"
            )
        fps = _ken_burns_fps()
        d = max(3, int(round(seconds * fps)))
        sw = _ken_burns_scale_width()
        vf = (
            f"scale={sw}:-1,zoompan=z='min(zoom+0.0015,1.5)':d={d}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={WIDTH}x{HEIGHT}:fps={fps}"
            f"{grade}"
        )
        logger.info(
            "正在 ffmpeg 编码 %s（Ken Burns，%.1f s，%d fps，共 %d 帧；首段可能需 30s～数分钟）…",
            label,
            seconds,
            fps,
            d,
        )
        _run_ffmpeg_with_heartbeat(
            [
                ffmpeg_exe,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-loop",
                "1",
                "-i",
                str(png),
                "-vf",
                vf,
                *x264_fast,
                "-pix_fmt",
                "yuv420p",
                str(out_seg),
            ],
            label,
        )
        logger.info("%s 编码完成。", label)
    elif _want_subtle_drift(cinematic):
        fps = _subtle_drift_fps()
        d = max(3, int(round(seconds * fps)))
        sw = _subtle_drift_scale_w()
        vf = (
            f"scale={sw}:-1,zoompan=z='min(zoom+0.0004,1.08)':d={d}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={WIDTH}x{HEIGHT}:fps={fps}"
            f"{grade}"
        )
        logger.info(
            "正在 ffmpeg 编码 %s（轻推镜 %.1f s，%d fps；比 Ken Burns 快）…",
            label,
            seconds,
            fps,
        )
        _run_ffmpeg_with_heartbeat(
            [
                ffmpeg_exe,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-loop",
                "1",
                "-i",
                str(png),
                "-vf",
                vf,
                *x264_fast,
                "-pix_fmt",
                "yuv420p",
                str(out_seg),
            ],
            label,
        )
        logger.info("%s 编码完成。", label)
    else:
        logger.info("正在 ffmpeg 编码 %s（静图，约 %.1f s）…", label, seconds)
        args_static = [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-i",
            str(png),
        ]
        if grade:
            args_static += ["-vf", grade.lstrip(",")]
        args_static += [
            *x264_fast,
            "-t",
            str(seconds),
            "-pix_fmt",
            "yuv420p",
            str(out_seg),
        ]
        _run_ffmpeg(args_static)
        logger.info("%s 编码完成。", label)


def build_video(
    scenes: list[str],
    output_mp4: Path,
    seconds_per_scene: float,
    work_dir: Path | None = None,
    *,
    text_only_slides: bool = False,
    strict_illustration: bool = False,
) -> None:
    """将场景列表渲染为单支 MP4。默认每场景为 AI 配图（失败则氛围色场）；text_only_slides=True 时为纯文字幻灯。"""
    if not scenes:
        raise ValueError("没有可渲染的场景（输入是否为空？）")
    ffmpeg_exe = ensure_ffmpeg()
    slide_font = _load_font(FONT_SIZE) if text_only_slides else None
    tmp = Path(work_dir) if work_dir else create_temp_workdir("tovedio_")
    own_tmp = work_dir is None
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        frames = tmp / "frames"
        frames.mkdir(exist_ok=True)
        segments: list[Path] = []
        nsc = len(scenes)
        for i, scene in enumerate(scenes):
            png = frames / f"scene_{i:04d}.png"
            if text_only_slides:
                render_scene_image(scene, png, slide_font)
            else:
                download_illustration_png(
                    scene,
                    png,
                    scene_index=i,
                    strict_illustration=strict_illustration,
                )
            seg = tmp / f"seg_{i:04d}.mp4"
            _ffmpeg_png_to_segment(
                ffmpeg_exe,
                png,
                seg,
                seconds_per_scene,
                log_ken_burns_hint=(i == 0),
                segment_label=f"场景 {i + 1}/{nsc}",
            )
            segments.append(seg)
        output_mp4.parent.mkdir(parents=True, exist_ok=True)
        _merge_video_segments(ffmpeg_exe, segments, output_mp4, seconds_per_scene)
    finally:
        if own_tmp and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def run_from_txt(
    input_txt: Path,
    output_mp4: Path,
    seconds_per_scene: float = 3.0,
    max_chars: int = 400,
    *,
    text_only_slides: bool = False,
    strict_illustration: bool = False,
) -> None:
    raw = input_txt.read_text(encoding="utf-8")
    scenes = split_scenes(raw, max_chars)
    if not scenes:
        raise ValueError("输入文件为空或仅含空白，无法生成视频。")
    build_video(
        scenes,
        output_mp4,
        seconds_per_scene,
        text_only_slides=text_only_slides,
        strict_illustration=strict_illustration,
    )


def _env_tts_enabled() -> bool:
    v = (os.environ.get("TOVEDIO_TTS") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def tts_enabled_from_env() -> bool:
    """供 CLI 使用：`.env` 中 `TOVEDIO_TTS=1` 时视为开启 TTS。"""
    return _env_tts_enabled()


def _env_i2v_enabled() -> bool:
    v = (os.environ.get("TOVEDIO_I2V") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def i2v_enabled_from_env() -> bool:
    """`.env` 中 `TOVEDIO_I2V=1` 时视为开启图生视频。"""
    return _env_i2v_enabled()


def _resolve_i2v_stride(cli_stride: int | None) -> int:
    if cli_stride is not None:
        return max(0, cli_stride)
    raw = (os.environ.get("TOVEDIO_I2V_STRIDE") or "3").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 3


def _shot_should_use_i2v(
    shot_index: int,
    shot: dict,
    *,
    enable_i2v: bool,
    stride: int,
) -> bool:
    if not enable_i2v:
        return False
    pref = (shot.get("media_preference") or "auto").strip().lower()
    if pref == "image":
        return False
    if pref == "video":
        return True
    if stride <= 0:
        return False
    return shot_index % stride == 0


def _i2v_tpad_max_sec() -> float:
    """单段末帧克隆最长秒数；超出则该镜成片略短于分镜目标，避免长时间定格。0=不垫（仅慢放）。"""
    raw = (os.environ.get("TOVEDIO_I2V_TPAD_MAX_SEC") or "1.0").strip()
    try:
        m = float(raw)
    except ValueError:
        m = 1.0
    return max(0.0, min(10.0, m))


def _normalize_video_segment_for_merge(
    ffmpeg_exe: str,
    input_mp4: Path,
    out_mp4: Path,
    target_duration_sec: float,
) -> None:
    """
    将 I2V 输出统一为项目分辨率/FPS，并按分镜时长截断或延长。
    偏短时优先 setpts 慢放（上限由 TOVEDIO_I2V_SEGMENT_MAX_STRETCH 控制），
    剩余时长再用末帧克隆，减少长时间「定格」卡顿感。
    """
    if target_duration_sec <= 0:
        raise ValueError("target_duration_sec 必须大于 0。")
    dur = media_duration_sec(input_mp4)
    vf_base = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps=25"
    )
    vf = vf_base
    if dur < target_duration_sec - 0.05 and dur > 0.05:
        raw_max = (os.environ.get("TOVEDIO_I2V_SEGMENT_MAX_STRETCH") or "1.32").strip()
        try:
            max_stretch = float(raw_max)
        except ValueError:
            max_stretch = 1.32
        max_stretch = max(1.0, min(2.0, max_stretch))
        need_ratio = target_duration_sec / dur
        cap_ratio = min(need_ratio, max_stretch)
        stretch_to = dur * cap_ratio
        if cap_ratio > 1.02:
            factor = round(stretch_to / dur, 6)
            vf = f"{vf_base},setpts=PTS*{factor}"
            remaining = max(0.0, target_duration_sec - stretch_to)
        else:
            remaining = target_duration_sec - dur
        tpad_max = _i2v_tpad_max_sec()
        tpad_use = min(remaining, tpad_max) if tpad_max > 0 else 0.0
        if tpad_use > 0.05:
            vf += f",tpad=stop_mode=clone:stop_duration={tpad_use:.3f}"
        elif tpad_use > 0.001:
            vf += f",tpad=stop_mode=clone:stop_duration={tpad_use:.3f}"
        if remaining - tpad_use > 0.2:
            logger.info(
                "单段末帧垫长上限 %.2fs，本段仍缺 %.2fs，该镜成片将略短于目标（减轻长时间静帧）",
                tpad_max,
                remaining - tpad_use,
            )
    elif dur < target_duration_sec - 0.05:
        need_pad = target_duration_sec - max(dur, 0.0)
        tpad_max = _i2v_tpad_max_sec()
        pad = min(need_pad, tpad_max) if tpad_max > 0 else 0.0
        if pad > 0.001:
            vf = f"{vf_base},tpad=stop_mode=clone:stop_duration={pad:.3f}"
        else:
            vf = vf_base
        if need_pad - pad > 0.2:
            logger.info(
                "单段末帧垫长上限 %.2fs，本段仍缺 %.2fs，该镜成片将略短于目标",
                tpad_max,
                need_pad - pad,
            )
    args: list[str] = [
        ffmpeg_exe,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_mp4),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-an",
    ]
    if dur > target_duration_sec + 0.05:
        args += ["-t", str(target_duration_sec)]
    args += [str(out_mp4)]
    _run_ffmpeg(args)


def _reencode_segment_uniform_fps(
    ffmpeg_exe: str,
    input_mp4: Path,
    out_mp4: Path,
    *,
    fps: int = 25,
) -> None:
    """将任意片段重编码为固定 fps（与 I2V 归一化一致，便于 xfade concat）。"""
    dur = media_duration_sec(input_mp4)
    _run_ffmpeg(
        [
            ffmpeg_exe,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_mp4),
            "-vf",
            f"fps={fps}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-t",
            str(dur),
            str(out_mp4),
        ]
    )


def _shot_duration_sec(shot: dict, default: float) -> float:
    """分镜可选 duration_sec；无效则回退为 CLI -s。夹在 0.8～120 s。"""
    raw = shot.get("duration_sec")
    if raw is None:
        return default
    try:
        t = float(raw)
    except (TypeError, ValueError):
        return default
    if t < 0.8:
        return 0.8
    if t > 120.0:
        return 120.0
    return t


def _dialogue_shot_min_duration_sec() -> float:
    """
    对白镜头最小时长（秒）。默认 4.0s，保障口型/表情有可见时长。
    可用 TOVEDIO_DIALOGUE_MIN_SEC 调整（0=关闭下限）。
    """
    raw = (os.environ.get("TOVEDIO_DIALOGUE_MIN_SEC") or "4").strip()
    try:
        v = float(raw)
    except ValueError:
        v = 4.0
    return max(0.0, min(12.0, v))


def _apply_dialogue_duration_floor(shot: dict, dur: float) -> float:
    lines = shot.get("lines") or []
    has_dialogue = any(
        isinstance(line, dict) and str(line.get("kind") or "").strip() == "dialogue" for line in lines
    )
    if not has_dialogue:
        return dur
    floor = _dialogue_shot_min_duration_sec()
    if floor <= 0:
        return dur
    return max(dur, floor)


def _build_character_bible(characters: list[dict]) -> str:
    """把角色外观汇总成短文本，注入每镜 prompt 以增强跨镜一致性。"""
    lines: list[str] = []
    for c in characters:
        name = str(c.get("name") or c.get("id") or "").strip()
        app = str(c.get("appearance") or "").strip()
        if not name:
            continue
        if app:
            lines.append(
                f"{name}：{app}；"
                f"全片中{name}的年龄、面容、服装必须与此描述完全一致，"
                f"禁止变成其他年龄段或其他服装的人物"
            )
        else:
            lines.append(f"{name}：外观在全片保持一致")
    if not lines:
        return ""
    return (
        "角色连续性设定（全片强制）：" + "；".join(lines) +
        "。每个角色的性别、年龄段、服装配色在全片每一帧必须保持一致，"
        "即使远景或背光也不能变换成另一个人。"
    )


def _build_shot_bridge(prev_shot: dict | None, curr_shot: dict) -> str:
    """生成相邻镜头衔接提示，减少跳场、跳轴和动作断裂。"""
    if prev_shot is None:
        return (
            "首镜要求：建立空间方位与人物站位，给出清晰环境信息与主体动作起点；"
            "构图稳定，避免信息过载。"
        )
    prev_scene = (prev_shot.get("scene") or {}).get("label") or ""
    curr_scene = (curr_shot.get("scene") or {}).get("label") or ""
    if str(prev_scene).strip() == str(curr_scene).strip():
        return (
            "与上一镜头连续：保持同一空间轴线与光线方向，延续人物朝向和动作趋势；"
            "景别可从远到中再到近，不要突然反打跳轴。"
        )
    return (
        "场景切换要求：先给过渡感（如空镜/跟随移动），再进入新场景主体；"
        "保留上一镜头动作或视线的动机承接，避免硬切跳时空。"
    )


def _shot_lines_text(shot: dict | None) -> str:
    if not shot:
        return ""
    lines = shot.get("lines") or []
    parts: list[str] = []
    for line in lines:
        t = str((line or {}).get("text") or "").strip()
        if t:
            parts.append(t)
    return " ".join(parts)


def _build_story_chain_hint(prev_shot: dict | None, curr_shot: dict, next_shot: dict | None) -> str:
    """给每镜注入因果叙事钩子，避免成为互不相关的片段。"""
    prev_text = _shot_lines_text(prev_shot)[:90]
    curr_text = _shot_lines_text(curr_shot)[:110]
    next_text = _shot_lines_text(next_shot)[:90]
    if prev_text and next_text:
        return (
            f"叙事链：承接上一镜[{prev_text}]，本镜聚焦[{curr_text}]，"
            f"并为下一镜[{next_text}]建立明确动作因果。"
        )
    if next_text:
        return f"叙事链：本镜聚焦[{curr_text}]，并自然引出下一镜[{next_text}]。"
    if prev_text:
        return f"叙事链：承接上一镜[{prev_text}]，完成当前事件[{curr_text}]的结果落点。"
    return f"叙事链：作为开场建立镜头，清楚交代当前事件[{curr_text}]的起点。"


def _phase_for_pos(i: int, total: int) -> str:
    if total <= 1:
        return "开场"
    p = i / max(1, total - 1)
    if p < 0.25:
        return "开场"
    if p < 0.65:
        return "推进"
    if p < 0.88:
        return "高潮"
    return "收束"


def _story_beat_label(phase: str, idx: int) -> str:
    mapping = {
        "开场": "建立人物与目标",
        "推进": "冲突升级与行动",
        "高潮": "关键对抗与抉择",
        "收束": "结果与情绪落点",
    }
    return f"{phase}#{idx + 1}:{mapping.get(phase, '情节推进')}"


def _build_story_beats_from_shots(shots: list[dict], *, max_beats: int = 8) -> tuple[list[dict], list[int]]:
    """
    基于 shots 生成稳定 story beats（阶段化事件链），并返回每个 shot 对应的 beat 索引。
    这是"结构层"：让后续每镜生成不再是孤立任务。
    """
    n = len(shots)
    if n == 0:
        return [], []
    beat_count = min(max(1, n), max_beats)
    beat_to_shot: list[tuple[int, int]] = []
    for b in range(beat_count):
        start = (b * n) // beat_count
        end = ((b + 1) * n) // beat_count - 1
        if end < start:
            end = start
        beat_to_shot.append((start, min(end, n - 1)))
    shot_to_beat = [0] * n
    beats: list[dict] = []
    for bi, (s, e) in enumerate(beat_to_shot):
        for si in range(s, e + 1):
            shot_to_beat[si] = bi
        phase = _phase_for_pos(bi, beat_count)
        lead_text = _shot_lines_text(shots[s])[:80]
        beats.append(
            {
                "id": f"beat_{bi + 1:02d}",
                "phase": phase,
                "label": _story_beat_label(phase, bi),
                "lead_text": lead_text,
                "shot_range": (s, e),
            }
        )
    return beats, shot_to_beat


def _compress_shots_for_story(shots: list[dict], *, max_shots: int = 8) -> list[dict]:
    """若分镜过多则压缩为最多 max_shots，降低随机漂移并强化主线。"""
    n = len(shots)
    if n <= max_shots:
        return shots
    keep_idx: list[int] = []
    for i in range(max_shots):
        idx = round(i * (n - 1) / max(1, max_shots - 1))
        if not keep_idx or idx != keep_idx[-1]:
            keep_idx.append(idx)
    keep_set = set(keep_idx)
    dropped_indices = sorted(set(range(n)) - keep_set)
    out = [shots[i] for i in keep_idx]
    dropped_summaries = [
        f"#{i}「{str((shots[i].get('scene') or {}).get('label') or shots[i].get('shot_id') or i)[:20]}」"
        for i in dropped_indices
    ]
    logger.warning(
        "分镜共 %d 镜，超出上限 %d，已压缩至 %d 镜。"
        "被丢弃的镜头（%d 个）：%s。"
        "如需保留全部镜头，请设置 TOVEDIO_MAX_SHOTS=%d 或 --max-shots %d。",
        n, max_shots, len(out),
        len(dropped_indices), "、".join(dropped_summaries),
        n, n,
    )
    return out


def build_video_from_storyboard(
    storyboard: dict,
    output_mp4: Path,
    seconds_per_shot: float,
    work_dir: Path | None = None,
    *,
    strict_illustration: bool = False,
    enable_tts: bool = False,
    cinematic: bool = False,
    enable_i2v: bool = False,
    i2v_stride: int | None = None,
) -> None:
    """
    根据 MiniMax 分镜 JSON 逐镜配图并合成 MP4；
    enable_tts 时按角色 voice_hint 生成 TTS 并混流；
    enable_i2v 时对选定镜做图生视频（失败则回退静图）；
    cinematic 开轻推镜+轻微调色（仅静图片段）。
    """
    from .illustration import download_illustration_from_prompt
    from .storyboard_render import shot_to_image_prompt, shot_to_i2v_motion_prompt
    from .timeline_edl import ProjectTimeline, TimelineAudio, TimelineClip

    shots = sorted(storyboard["shots"], key=lambda x: x["order"])
    if not shots:
        raise ValueError("分镜中没有镜头。")
    chars = storyboard.get("characters") or []
    stride = _resolve_i2v_stride(i2v_stride)
    ffmpeg_exe = ensure_ffmpeg()
    tmp = Path(work_dir) if work_dir else create_temp_workdir("tovedio_sb_")
    own_tmp = work_dir is None
    timeline = ProjectTimeline()
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        frames = tmp / "frames"
        frames.mkdir(exist_ok=True)
        segments: list[Path] = []
        shot_durations: list[float] = []
        nsh = len(shots)
        for i, shot in enumerate(shots):
            dur = _apply_dialogue_duration_floor(shot, _shot_duration_sec(shot, seconds_per_shot))
            shot_durations.append(dur)
            png = frames / f"shot_{i:04d}.png"
            img_prompt, mood_seed = shot_to_image_prompt(shot, chars)
            _ = download_illustration_from_prompt(
                img_prompt,
                mood_seed,
                png,
                scene_index=i,
                strict_illustration=strict_illustration,
            )
            seg = tmp / f"seg_{i:04d}.mp4"
            sid = str(shot.get("shot_id") or f"shot_{i}")
            want_i2v = _shot_should_use_i2v(i, shot, enable_i2v=enable_i2v, stride=stride)
            used_i2v_ok = False
            if want_i2v:
                try:
                    from .video_i2v_minimax import run_i2v_to_mp4

                    raw_i2v = tmp / f"i2v_raw_{i:04d}.mp4"
                    motion = shot_to_i2v_motion_prompt(shot, characters=chars)
                    raw_dur = (os.environ.get("TOVEDIO_I2V_DURATION") or "6").strip()
                    try:
                        dur_hint = int(float(raw_dur))
                    except ValueError:
                        dur_hint = 6
                    dur_hint = max(1, min(10, dur_hint))
                    logger.info("镜头 %d/%d：图生视频（I2V）…", i + 1, nsh)
                    run_i2v_to_mp4(
                        png,
                        motion,
                        raw_i2v,
                        tmp,
                        duration_hint_sec=dur_hint,
                        use_cache=True,
                    )
                    _normalize_video_segment_for_merge(ffmpeg_exe, raw_i2v, seg, dur)
                    ad = media_duration_sec(seg)
                    if ad > 0:
                        shot_durations[i] = ad
                    used_i2v_ok = True
                    timeline.clips.append(
                        TimelineClip(
                            shot_id=sid,
                            order=i,
                            media="video",
                            source_path=str(seg.resolve()),
                            duration_sec=dur,
                            i2v_task_id=None,
                            i2v_fallback=False,
                        )
                    )
                except Exception as e:
                    raise RuntimeError(
                        f"I2V 失败，已停止出片（镜头 {i + 1}/{nsh}, shot_id={sid}）：{e}"
                    ) from e
            else:
                _ffmpeg_png_to_segment(
                    ffmpeg_exe,
                    png,
                    seg,
                    dur,
                    log_ken_burns_hint=(i == 0),
                    segment_label=f"镜头 {i + 1}/{nsh}",
                    cinematic=cinematic,
                )
                timeline.clips.append(
                    TimelineClip(
                        shot_id=sid,
                        order=i,
                        media="image",
                        source_path=str(seg.resolve()),
                        duration_sec=dur,
                        i2v_task_id=None,
                        i2v_fallback=False,
                    )
                )
            if enable_i2v and not used_i2v_ok:
                uni = tmp / f"seg_{i:04d}_fps.mp4"
                _reencode_segment_uniform_fps(ffmpeg_exe, seg, uni)
                try:
                    seg.unlink()
                except OSError:
                    pass
                shutil.move(str(uni), str(seg))
            segments.append(seg)
        output_mp4.parent.mkdir(parents=True, exist_ok=True)
        silent_path = tmp / "silent.mp4"
        _merge_video_segments(
            ffmpeg_exe,
            segments,
            silent_path,
            seconds_per_shot,
            segment_durations=shot_durations,
        )

        try:
            timeline.save_json(tmp / "timeline_edl.json")
        except OSError:
            pass

        shutil.move(str(silent_path), str(output_mp4))
    finally:
        if own_tmp and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def run_from_txt_minimax(
    input_txt: Path,
    output_mp4: Path,
    seconds_per_shot: float = 3.0,
    *,
    save_storyboard_path: Path | None = None,
    strict_illustration: bool = False,
    enable_tts: bool = False,
    cinematic: bool = False,
    enable_i2v: bool = False,
    i2v_stride: int | None = None,
) -> None:
    from .minimax_client import generate_storyboard
    from .storyboard_io import save_storyboard

    raw = input_txt.read_text(encoding="utf-8")
    data = generate_storyboard(raw)
    if save_storyboard_path:
        save_storyboard(data, save_storyboard_path)
    build_video_from_storyboard(
        data,
        output_mp4,
        seconds_per_shot,
        strict_illustration=strict_illustration,
        enable_tts=enable_tts,
        cinematic=cinematic,
        enable_i2v=enable_i2v,
        i2v_stride=i2v_stride,
    )


def run_from_txt_minimax_t2v_direct(
    input_txt: Path,
    output_mp4: Path,
    *,
    duration_hint_sec: int | None = None,
    style: str = "real",
    bailian_t2v_model: str | None = None,
) -> None:
    """文本直出文生视频（T2V），使用阿里云百炼 Wan 文生视频模型（默认 wan2.6-t2v）。"""
    from .video_t2v_bailian_kling import resolved_t2v_model, run_t2v_to_mp4

    raw = input_txt.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError("输入文件为空或仅含空白，无法生成视频。")
    hint = duration_hint_sec
    if hint is None:
        raw_env = (os.environ.get("MINIMAX_T2V_DURATION") or "6").strip()
        try:
            hint = int(float(raw_env))
        except ValueError:
            hint = 6
    hint = max(1, min(10, int(hint)))
    style_prompt = (
        "风格化：高质量二维动画风，线条清晰，赛璐璐上色，角色设计统一。"
        if style == "anime"
        else "风格化：写实电影风，真实光影与材质细节，禁止动漫与Q版。"
    )
    story_snippet = raw.replace("\n", " ").strip()
    if len(story_snippet) > 1000:
        story_snippet = story_snippet[:1000] + "…"
    structured_prompt = (
        "请按万相结构化提示词生成单段连续视频。\n"
        "基础公式：主体 + 场景 + 运动。\n"
        "进阶公式：主体描述 + 场景描述 + 运动描述 + 美学控制 + 风格化。\n"
        f"主体与剧情素材：{story_snippet}\n"
        "美学控制：电影镜头语言，景别与机位清晰，运镜克制自然，禁止突兀跳切。\n"
        f"{style_prompt}\n"
        "一致性约束：同名角色外观保持一致；双人同框需可区分；无字幕、无水印、无画面内文字。"
    )
    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    t2v_m = (bailian_t2v_model or resolved_t2v_model()).strip()
    logger.info("T2V 直出模式：提交百炼文生视频任务（model=%s, style=%s, duration=%ss）…", t2v_m, style, hint)
    run_t2v_to_mp4(
        structured_prompt,
        output_mp4,
        duration_hint_sec=hint,
        model=bailian_t2v_model,
    )


def _merge_t2v_segments_with_audio(
    ffmpeg_exe: str,
    segments: list[Path],
    output_mp4: Path,
) -> None:
    """按硬切拼接多段 T2V，保留每段自带音频（对白/环境声）。"""
    if not segments:
        raise ValueError("没有可拼接的 T2V 片段。")
    if len(segments) == 1:
        shutil.copy2(segments[0], output_mp4)
        return
    concat_path = new_staging_path(prefix="tovedio_t2v_concat_", suffix=".txt")
    try:
        concat_path.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in segments),
            encoding="utf-8",
        )
        _run_ffmpeg(
            [
                ffmpeg_exe,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "32000",
                "-ac",
                "1",
                str(output_mp4),
            ]
        )
    finally:
        try:
            concat_path.unlink(missing_ok=True)
        except OSError:
            pass


def _shot_to_t2v_prompt(
    shot: dict[str, Any],
    chars: list[dict[str, Any]],
    *,
    style: str,
    series_lock: str,
    location_bible: str = "",
) -> str:
    def _sanitize(s: str) -> str:
        t = (s or "").strip()
        if not t:
            return ""
        replacements = (
            ("血迹", "可疑痕迹"),
            ("血污", "污痕"),
            ("流血", "受伤"),
            ("发黑", "暗沉"),
            ("箭簇", "异物"),
            ("伤口", "伤处"),
            ("杀意", "戒备"),
        )
        for a, b in replacements:
            t = t.replace(a, b)
        return t

    vis = shot.get("visual") or {}
    scene = shot.get("scene") or {}
    char_by_id = {str(c.get("id") or "").strip(): c for c in chars if isinstance(c, dict)}
    on_screen = [str(x).strip() for x in (vis.get("characters_on_screen") or []) if str(x).strip()]
    cast_parts: list[str] = []
    for cid in on_screen:
        c = char_by_id.get(cid)
        if not c:
            continue
        nm = str(c.get("name") or cid).strip()
        ap = str(c.get("appearance") or "").strip()
        if ap:
            cast_parts.append(f"{nm}：{ap[:100]}")
        else:
            cast_parts.append(nm)
    cast = "；".join(cast_parts) if cast_parts else "本镜为空镜，无人物出场。"
    line_parts: list[str] = []
    for line in shot.get("lines") or []:
        if not isinstance(line, dict):
            continue
        tx = str(line.get("text") or "").strip()
        if not tx:
            continue
        if str(line.get("kind") or "").strip() == "dialogue":
            sp = str(line.get("speaker_id") or "").strip()
            sn = str((char_by_id.get(sp) or {}).get("name") or sp or "角色").strip()
            line_parts.append(f"{sn}：{_sanitize(tx)}")
    from .storyboard_render import shot_to_sound_description as _sound_desc
    sound_desc = _sound_desc(shot, characters=chars) or (
        "；".join(line_parts[:3]) + "。" if line_parts else "无明确对白，保留环境声。"
    )
    style_hint = (
        "风格化：高质量二维动画风，线稿清晰，赛璐璐上色，角色设计统一。"
        if style == "anime"
        else "风格化：写实电影风，真实光影与材质细节，禁止动漫与Q版。"
    )
    return (
        "请按万相结构化提示词生成单段连续文生视频。\n"
        "公式：主体描述 + 场景描述 + 运动描述 + 美学控制 + 风格化 + 声音描述。\n"
        f"主体：{cast}\n"
        f"场景：{_sanitize(str(scene.get('background_prompt') or '').strip())}\n"
        f"画面：{_sanitize(str(vis.get('prompt_zh') or '').strip())}\n"
        f"运镜：{str(vis.get('camera') or '固定机位').strip()}\n"
        f"声音：{sound_desc}\n"
        f"{style_hint}\n"
        f"{series_lock}\n"
        f"{location_bible}\n"
        "约束：同名角色外观一致；不出现字幕、水印、画面内文字；动作连续自然。"
    )


def run_from_storyboard_json_t2v(
    storyboard_json: Path,
    output_mp4: Path,
    seconds_per_shot: float = 4.0,
    *,
    style: str = "real",
    bailian_t2v_model: str | None = None,
    production_bible_path: Path | None = None,
) -> None:
    """从已有分镜 JSON 逐镜执行 T2V，并拼成整片（保留每镜原生声音）。"""
    from .production_bible_io import apply_production_bible_to_storyboard, build_location_bible_text, load_production_bible
    from .storyboard_io import load_storyboard, validate_storyboard
    from .video_t2v_bailian_kling import resolved_t2v_model, run_t2v_to_mp4

    data = load_storyboard(storyboard_json)
    validate_storyboard(data)
    bible: dict[str, Any] | None = None
    if production_bible_path is not None:
        p = production_bible_path
        if not p.is_file():
            raise FileNotFoundError(f"找不到制作圣经：{p}")
        bible = load_production_bible(p)
        apply_production_bible_to_storyboard(data, bible)
        validate_storyboard(data)
        logger.info("已从 JSON 加载分镜并套用制作圣经：%s", p)

    shots = sorted(data["shots"], key=lambda x: x["order"])
    if not shots:
        raise ValueError("分镜中没有镜头。")
    chars = data.get("characters") or []
    base_lock = _series_visual_lock(style=style, novel_text=_storyboard_plain_text_for_lock(data))
    bible_lock = (bible.get("series_visual_lock") or "").strip() if bible else ""
    series_lock = f"{bible_lock}。{base_lock}" if bible_lock else base_lock
    location_bible = build_location_bible_text(bible.get("locations") or []) if bible else ""
    t2v_model = (bailian_t2v_model or resolved_t2v_model()).strip()
    ffmpeg_exe = ensure_ffmpeg()

    tmp = create_temp_workdir("tovedio_t2v_sb_")
    try:
        segments: list[Path] = []
        for i, shot in enumerate(shots):
            dur = _apply_dialogue_duration_floor(shot, _shot_duration_sec(shot, seconds_per_shot))
            hint = int(max(1, min(10, round(dur))))
            prompt = _shot_to_t2v_prompt(
                shot,
                chars,
                style=style,
                series_lock=series_lock,
                location_bible=location_bible,
            )
            seg = tmp / f"seg_{i:04d}.mp4"
            logger.info(
                "镜头 %d/%d：T2V（%s，目标 %ss）…",
                i + 1,
                len(shots),
                t2v_model,
                hint,
            )
            try:
                run_t2v_to_mp4(
                    prompt,
                    seg,
                    duration_hint_sec=hint,
                    model=bailian_t2v_model,
                )
            except RuntimeError as e:
                if not _is_bailian_data_inspection_failed(e):
                    raise
                # 触审时自动降级一次：尽量保留镜头目标，但去掉剧情细节与风险词。
                fallback_prompt = (
                    "请生成单段连续视频，内容健康合规。\n"
                    f"场景：{str((shot.get('scene') or {}).get('label') or '雪夜环境').strip()}。\n"
                    f"画面：{str(((shot.get('visual') or {}).get('shot_type') or 'medium')).strip()}，"
                    f"{str(((shot.get('visual') or {}).get('camera') or '固定机位')).strip()}，"
                    "动作克制自然，人物与环境微动。\n"
                    f"{style_hint if (style_hint := ('风格化：高质量二维动画风。' if style == 'anime' else '风格化：写实电影风。')) else ''}\n"
                    "约束：无字幕无水印，无血腥暴力细节。"
                )
                logger.warning(
                    "镜头 %d/%d：T2V 触发内容审核，已自动切换中性降级 prompt 重试一次…",
                    i + 1,
                    len(shots),
                )
                run_t2v_to_mp4(
                    fallback_prompt,
                    seg,
                    duration_hint_sec=hint,
                    model=bailian_t2v_model,
                )
            segments.append(seg)
        output_mp4.parent.mkdir(parents=True, exist_ok=True)
        _merge_t2v_segments_with_audio(ffmpeg_exe, segments, output_mp4)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def _storyboard_plain_text_for_lock(data: dict[str, Any]) -> str:
    """从分镜拼一段文本，供全片 series_visual_lock 的故事锚点（无小说时用）。"""
    parts: list[str] = []
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    t = str(meta.get("title") or "").strip()
    if t:
        parts.append(t)
    for shot in sorted(data.get("shots") or [], key=lambda x: int(x.get("order", 0))):
        if not isinstance(shot, dict):
            continue
        for line in shot.get("lines") or []:
            if not isinstance(line, dict):
                continue
            tx = str(line.get("text") or "").strip()
            if tx:
                parts.append(tx)
    out = "\n".join(parts).strip()
    return out if out else "（剧本摘要）"


def _resolved_character_sheet_dir(explicit: Path | None) -> Path | None:
    """
    CLI --character-sheet-dir 优先；否则读 TOVEDIO_CHARACTER_SHEET_DIR。
    目录须含 ``{character_id}_costume_sheet.png``（与 --character-sheets-only 输出一致）。
    """
    if explicit is not None:
        if explicit.is_dir():
            logger.info("定妆参考图目录：%s", explicit)
            return explicit
        logger.warning("指定的定妆参考目录不存在，将不使用 subject_reference：%s", explicit)
        return None
    raw = (os.environ.get("TOVEDIO_CHARACTER_SHEET_DIR") or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if p.is_dir():
        logger.info("定妆参考图目录（TOVEDIO_CHARACTER_SHEET_DIR）：%s", p)
        return p
    logger.warning("TOVEDIO_CHARACTER_SHEET_DIR 不是有效目录，已忽略：%s", p)
    return None


def _l2v_output_cache_dir(output_mp4: Path) -> Path:
    """与成片同目录：{stem}.l2v_cache/，用于断点续跑已生成的 seg。"""
    p = output_mp4.resolve()
    return p.parent / f"{p.stem}.l2v_cache"


def _l2v_clear_cache_dir(cache_dir: Path) -> None:
    if not cache_dir.is_dir():
        return
    for child in cache_dir.iterdir():
        try:
            if child.is_file():
                child.unlink()
        except OSError:
            pass


def _l2v_run_fingerprint(
    shots: list[dict[str, Any]],
    *,
    l2v_model: str,
    style: str,
    seconds_per_shot: float,
    l2v_chain: bool,
    chain_refresh: int,
    l2v_minimal_motion: bool,
    strict_illustration: bool,
    bible: dict[str, Any] | None,
    character_sheet_dir: Path | None,
) -> str:
    """参数或分镜变化时指纹变，避免误用旧 seg。"""
    h = hashlib.sha256()
    h.update(json.dumps(shots, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    img_model = (os.environ.get("BAILIAN_IMAGE_MODEL") or "wan2.6-t2i").strip()
    ill_backend = (os.environ.get("ILLUSTRATION_BACKEND") or "auto").strip().lower()
    max_shots_env = (os.environ.get("TOVEDIO_MAX_SHOTS") or "").strip()
    h.update(
        f"|{l2v_model}|{style}|{seconds_per_shot}|{int(l2v_chain)}|{chain_refresh}|"
        f"{int(l2v_minimal_motion)}|{int(strict_illustration)}|{img_model}|{ill_backend}|{max_shots_env}".encode()
    )
    if bible:
        h.update(json.dumps(bible, sort_keys=True, ensure_ascii=False).encode())
    else:
        h.update(b"|bible_none|")
    if character_sheet_dir is not None and character_sheet_dir.is_dir():
        h.update(str(character_sheet_dir.resolve()).encode())
        for p in sorted(character_sheet_dir.glob("*_costume_sheet.png")):
            try:
                st = p.stat()
                h.update(f"|{p.name}|{st.st_mtime_ns}|{st.st_size}".encode())
            except OSError:
                pass
    else:
        h.update(b"|sheet_none|")
    return h.hexdigest()


def _l2v_collect_cached_segments(
    cache_dir: Path,
    tmp: Path,
    fingerprint: str,
    nsh: int,
    base_durations: list[float],
    *,
    ignore_fingerprint: bool = False,
) -> tuple[dict[int, Path], list[float]] | None:
    """
    按镜头粒度收集可复用缓存：
    - manifest 指纹匹配且 n_shots 一致（ignore_fingerprint=True 时跳过指纹校验）
    - seg_XXXX.mp4 存在且可读的镜头将被复用（不要求从 0 连续）
    返回 ({index: tmp_seg_path}, shot_durations_from_manifest_or_base)。
    """
    man_path = cache_dir / "manifest.json"
    if not man_path.is_file():
        return None
    try:
        man = json.loads(man_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if ignore_fingerprint:
        if int(man.get("n_shots", -1)) != nsh:
            logger.warning("--ignore-fingerprint：缓存 n_shots=%s 与当前 %d 不符，跳过缓存。", man.get("n_shots"), nsh)
            return None
        logger.info("--ignore-fingerprint：跳过指纹校验，直接复用现有 seg 文件。")
    elif man.get("fingerprint") != fingerprint or int(man.get("n_shots", -1)) != nsh:
        return None
    raw_durs = man.get("shot_durations")
    if not isinstance(raw_durs, list) or len(raw_durs) != nsh:
        return None
    merged = [float(raw_durs[i]) if i < len(raw_durs) else base_durations[i] for i in range(nsh)]
    cached: dict[int, Path] = {}
    for j in range(nsh):
        src = cache_dir / f"seg_{j:04d}.mp4"
        if not src.is_file() or src.stat().st_size < 500:
            continue
        dst = tmp / f"seg_{j:04d}.mp4"
        shutil.copy2(src, dst)
        cached[j] = dst
    if not cached:
        return None
    logger.info(
        "L2V 镜头级续跑：已复用缓存 %d/%d 镜（缺失镜头将仅重跑缺口，%s）",
        len(cached),
        nsh,
        cache_dir,
    )
    return cached, merged


def _l2v_write_cache_manifest(
    cache_dir: Path,
    fingerprint: str,
    nsh: int,
    saved_segments: int,
    shot_durations: list[float],
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": fingerprint,
        "n_shots": nsh,
        "saved_segments": saved_segments,
        "shot_durations": shot_durations,
    }
    (cache_dir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_l2v_prepared_storyboard(
    data: dict[str, Any],
    output_mp4: Path,
    seconds_per_shot: float = 3.0,
    *,
    novel_text_for_series_lock: str,
    save_storyboard_path: Path | None = None,
    strict_illustration: bool = False,
    style: str = "real",
    l2v_chain: bool | None = None,
    bailian_l2v_model: str | None = None,
    l2v_minimal_bailian_prompt: bool = False,
    rerun_shot_indices: set[int] | None = None,
    bible: dict[str, Any] | None = None,
    character_sheet_dir: Path | None = None,
    l2v_resume: bool = True,
    compress_shots: bool = True,
    ignore_fingerprint: bool = False,
) -> None:
    """分镜 JSON 已就绪时：配图、百炼 L2V、拼接、可选 TTS。默认按 -o 成片路径旁 {stem}.l2v_cache 断点续跑。
    有定妆目录且含 costume_sheet PNG 时强制关闭尾帧链式，避免脸漂（见 _resolve_l2v_chain_flag）。
    """
    from .character_sheets import (
        effective_characters_on_screen_for_refs,
        resolve_costume_sheet_paths,
    )
    from .illustration import download_illustration_from_prompt
    from .production_bible_io import build_location_bible_text
    from .storyboard_io import save_storyboard
    from .storyboard_render import shot_to_i2v_motion_prompt, shot_to_image_prompt
    from .video_t2v_bailian_kling import l2v_duration_cap, resolved_l2v_model, run_l2v_to_mp4

    if save_storyboard_path:
        save_storyboard(data, save_storyboard_path)
    shots = sorted(data["shots"], key=lambda x: x["order"])
    if compress_shots:
        max_shots = _l2v_max_shots()
        shots = _compress_shots_for_story(shots, max_shots=max_shots)
    if not shots:
        raise ValueError("分镜中没有镜头。")
    beats, shot_to_beat = _build_story_beats_from_shots(shots, max_beats=min(8, len(shots)))
    chars = data.get("characters") or []
    character_bible = _build_character_bible(chars)
    bible_lock = (bible.get("series_visual_lock") or "").strip() if bible else ""
    base_lock = _series_visual_lock(style=style, novel_text=novel_text_for_series_lock)
    series_lock = f"{bible_lock}。{base_lock}" if bible_lock else base_lock
    location_bible = build_location_bible_text(bible.get("locations") or []) if bible else ""
    sheet_root = _resolved_character_sheet_dir(character_sheet_dir)
    l2v_chain = _resolve_l2v_chain_flag(l2v_chain, sheet_root=sheet_root)
    if _character_sheet_dir_has_sheets(sheet_root):
        logger.info(
            "定妆目录已配置：尾帧链式 L2V 已禁用，每镜均走 MiniMax 关键帧并尽量挂 subject_reference，"
            "避免链式尾帧导致人物与定妆漂移。"
        )
    chain_refresh = _l2v_chain_refresh_interval()
    l2v_minimal_motion = _l2v_minimal_bailian_prompt_enabled(cli_flag=l2v_minimal_bailian_prompt)
    l2v_model = (bailian_l2v_model or resolved_l2v_model()).strip()
    fingerprint = _l2v_run_fingerprint(
        shots,
        l2v_model=l2v_model,
        style=style,
        seconds_per_shot=seconds_per_shot,
        l2v_chain=bool(l2v_chain),
        chain_refresh=chain_refresh,
        l2v_minimal_motion=l2v_minimal_motion,
        strict_illustration=strict_illustration,
        bible=bible,
        character_sheet_dir=sheet_root,
    )
    cache_dir = _l2v_output_cache_dir(output_mp4)
    if l2v_resume and not ignore_fingerprint:
        mp = cache_dir / "manifest.json"
        if mp.is_file():
            try:
                om = json.loads(mp.read_text(encoding="utf-8"))
                if om.get("fingerprint") != fingerprint:
                    logger.info("L2V 缓存与当前参数/分镜不一致，已清空：%s", cache_dir)
                    _l2v_clear_cache_dir(cache_dir)
            except (json.JSONDecodeError, OSError):
                _l2v_clear_cache_dir(cache_dir)

    ffmpeg_exe = ensure_ffmpeg()
    tmp = create_temp_workdir("tovedio_l2v_")
    try:
        frames = tmp / "frames"
        frames.mkdir(parents=True, exist_ok=True)
        nsh = len(shots)
        shot_durations = [
            _apply_dialogue_duration_floor(shots[k], _shot_duration_sec(shots[k], seconds_per_shot))
            for k in range(nsh)
        ]
        segments: list[Path] = []
        prev_shot: dict[str, Any] | None = None
        prev_tail_png: Path | None = None
        cached_segments: dict[int, Path] = {}
        if l2v_resume:
            resumed = _l2v_collect_cached_segments(
                cache_dir, tmp, fingerprint, nsh, shot_durations,
                ignore_fingerprint=ignore_fingerprint,
            )
            if resumed is not None:
                cached_segments, shot_durations = resumed
        if rerun_shot_indices:
            for ridx in sorted(rerun_shot_indices):
                if ridx in cached_segments:
                    cached_segments.pop(ridx, None)
                    logger.info("镜头 %d：按指定强制重跑（忽略该镜缓存）", ridx + 1)

        logger.info(
            "百炼图生视频模型：%s（CLI --l2v-model 或 .env BAILIAN_WAN_L2V_MODEL 可覆盖）",
            l2v_model,
        )
        if l2v_chain:
            logger.info(
                "L2V 尾帧链式已启用（TOVEDIO_L2V_CHAIN=0 或 --no-l2v-chain 可关闭；"
                "TOVEDIO_L2V_CHAIN_REFRESH=N 可每 N 镜刷新关键帧）。"
            )
        if l2v_minimal_motion:
            logger.info(
                "L2V 百炼精简文案已开启（--l2v-minimal-prompt 或 TOVEDIO_L2V_MINIMAL_PROMPT=1）："
                "仅提交短运镜描述，降低输入审核风险；跨镜叙事提示减弱。"
            )

        # ------------------------------------------------------------------ #
        # 路径选择：链式模式保持串行（尾帧依赖），非链式走两阶段并发           #
        # ------------------------------------------------------------------ #
        from .illustration import submit_t2i_task, poll_t2i_task_to_png
        from .video_t2v_bailian_kling import submit_l2v_task as _submit_l2v, poll_video_task_to_file

        dur_cap = l2v_duration_cap(model=l2v_model)

        # 预计算每镜的 img_prompt / motion / ref_paths / prompt_override
        def _build_shot_prompts(i: int, prev_shot_ref: dict | None) -> tuple[str, str, str, list[Any], list[str] | None]:
            """返回 (img_prompt, mood_seed, motion, ref_paths, prompt_override)"""
            shot = shots[i]
            next_shot: dict | None = shots[i + 1] if i + 1 < nsh else None
            vis = shot.get("visual") or {}
            explicit_screen = [str(x).strip() for x in (vis.get("characters_on_screen") or []) if str(x).strip()]
            effective_ids = effective_characters_on_screen_for_refs(shot, chars, sheet_root, max_refs=3)
            ref_paths_i = resolve_costume_sheet_paths(sheet_root, effective_ids) if sheet_root else []
            prompt_override_i: list[str] | None = (list(effective_ids) if (not explicit_screen and effective_ids) else None)
            img_prompt, mood_seed = shot_to_image_prompt(shot, chars, style=style, characters_on_screen_override=prompt_override_i)
            if location_bible:
                img_prompt = f"{img_prompt}。{location_bible}"
            bridge = _build_shot_bridge(prev_shot_ref, shot)
            story_chain = _build_story_chain_hint(prev_shot_ref, shot, next_shot)
            beat_idx = shot_to_beat[i] if i < len(shot_to_beat) else 0
            beat = beats[beat_idx] if beats else {"label": "主线推进", "phase": "推进", "lead_text": ""}
            beat_hint = (f"当前剧情阶段：{beat.get('phase')}；事件节点：{beat.get('label')}；本镜要服务该事件，不要偏离主线。")
            n_on_screen = len(explicit_screen) if explicit_screen else (len(effective_ids) if effective_ids else 0)
            if n_on_screen == 0:
                person_count_rule = "本镜为空镜/纯风景，画面内绝对禁止出现任何人物、人影、人形轮廓。"
            else:
                person_count_rule = (
                    f"本镜出镜人数严格为{n_on_screen}人，禁止出现第{n_on_screen+1}个路人或无关人物；"
                    f"禁止将同一角色生成为老年人或不同性别。"
                )
            continuity_rules = (
                f"镜头语言：保持叙事连贯，角色外观在全片一致；"
                f"双人同框明确左右位置与朝向，禁止双胞胎脸；"
                f"只呈现本镜核心事件，不引入无关新信息。{person_count_rule}"
            )
            if character_bible:
                img_prompt = f"{img_prompt}。{series_lock}。{character_bible}。{beat_hint}。{bridge}。{story_chain}。{continuity_rules}"
            else:
                img_prompt = f"{img_prompt}。{series_lock}。{beat_hint}。{bridge}。{story_chain}。{continuity_rules}"
            motion = shot_to_i2v_motion_prompt(shot, style=style, characters=chars)
            if not l2v_minimal_motion:
                motion = f"{motion}。补充约束：运镜与动作保持克制自然，避免突变、明显跳帧、无动机大幅动作。"
            return img_prompt, mood_seed, motion, ref_paths_i, prompt_override_i

        if l2v_chain:
            # ------ 链式串行（原逻辑保留）------ #
            for i in range(nsh):
                shot = shots[i]
                next_shot: dict | None = shots[i + 1] if i + 1 < nsh else None
                dur = shot_durations[i]
                if i in cached_segments:
                    seg = cached_segments[i]
                    segments.append(seg)
                    tail_png = tmp / f"chain_tail_{i:04d}.png"
                    _extract_last_frame_png(ffmpeg_exe, seg, tail_png)
                    prev_tail_png = tail_png
                    prev_shot = shot
                    logger.info("镜头 %d/%d：复用缓存片段 seg_%04d.mp4", i + 1, nsh, i)
                    continue
                png = frames / f"shot_{i:04d}.png"
                img_prompt, mood_seed, motion_base, ref_paths, prompt_override = _build_shot_prompts(i, prev_shot)
                img_prompt_for_t2i = img_prompt
                use_keyframe = prev_tail_png is None or _l2v_force_keyframe_shot(i, chain_refresh)
                inspection_retry = False
                raw_l2v = tmp / f"l2v_raw_{i:04d}.mp4"
                dur_hint = max(1, min(dur_cap, int(round(dur))))

                while True:
                    use_kf_now = use_keyframe or inspection_retry
                    if use_kf_now:
                        dl_prompt = img_prompt_for_t2i
                        if ref_paths:
                            dl_prompt = (
                                "【已附角色定妆参考图（subject_reference），须保持参考中人物的面部轮廓、发型与年龄段，"
                                "远景或大场景时人物可较小但须可辨认为同一参考角色，并与本镜场景、站位、服装动作自然融合，勿变成他人。】"
                                + img_prompt_for_t2i
                            )
                            if inspection_retry:
                                logger.warning("镜头 %d/%d：尾帧链式 L2V 被百炼内容审核拒绝（多为上一段视频尾帧图触审），正在生成本镜关键帧并重试…", i + 1, nsh)
                                logger.info("镜头 %d/%d：关键帧文生图附带 %d 张 MiniMax subject_reference（定妆，重试）", i + 1, nsh, len(ref_paths))
                            else:
                                logger.info("镜头 %d/%d：关键帧文生图附带 %d 张 MiniMax subject_reference（定妆）", i + 1, nsh, len(ref_paths))
                        elif inspection_retry:
                            logger.warning("镜头 %d/%d：尾帧链式 L2V 被百炼内容审核拒绝，正在生成本镜关键帧并重试…", i + 1, nsh)
                        got_img = download_illustration_from_prompt(
                            dl_prompt, mood_seed, png, scene_index=i,
                            strict_illustration=strict_illustration, style=style,
                            subject_reference_paths=ref_paths if ref_paths else None,
                        )
                        if inspection_retry and not got_img and not strict_illustration:
                            sp, sm = _l2v_safe_keyframe_t2i_prompt_after_fail(shot, chars, style=style, location_bible=location_bible or "", characters_on_screen_override=prompt_override)
                            logger.warning("镜头 %d/%d：触审重试文生图未产出联网配图，已改用无定妆、精简画面描述再试…", i + 1, nsh)
                            got2 = download_illustration_from_prompt(sp, sm, png, scene_index=i, strict_illustration=False, style=style, subject_reference_paths=None)
                            if not got2:
                                logger.warning("镜头 %d/%d：精简无定妆文生图仍失败，将继续用降级图尝试百炼 L2V。", i + 1, nsh)
                        l2v_input_png = png
                    else:
                        assert prev_tail_png is not None
                        l2v_input_png = prev_tail_png

                    motion = motion_base
                    if inspection_retry:
                        motion = _l2v_inspection_retry_safe_motion(style=style)
                        logger.info("镜头 %d/%d：触审重试 L2V 已改用极简运镜文案（不含剧情/圣经），降低百炼输入审核风险。", i + 1, nsh)
                    else:
                        if not use_kf_now and prev_tail_png is not None:
                            motion = f"{_chain_tail_motion_prefix(shot)}{motion_base}"

                    if use_kf_now:
                        logger.info("镜头 %d/%d：L2V（%s → %s）…", i + 1, nsh, "关键帧重试" if inspection_retry else "关键帧", l2v_model)
                    else:
                        logger.info("镜头 %d/%d：L2V（尾帧链式 → %s）…", i + 1, nsh, l2v_model)

                    try:
                        from .video_t2v_bailian_kling import run_l2v_to_mp4
                        run_l2v_to_mp4(l2v_input_png, motion, raw_l2v, duration_hint_sec=dur_hint, model=bailian_l2v_model, label=f"镜头{i+1}/{nsh}")
                        seg = tmp / f"seg_{i:04d}.mp4"
                        _normalize_video_segment_for_merge(ffmpeg_exe, raw_l2v, seg, dur)
                        ad = media_duration_sec(seg)
                        if ad > 0:
                            shot_durations[i] = ad
                        tail_png = tmp / f"chain_tail_{i:04d}.png"
                        _extract_last_frame_png(ffmpeg_exe, seg, tail_png)
                        prev_tail_png = tail_png
                        segments.append(seg)
                        prev_shot = shot
                        if l2v_resume:
                            cache_dir.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(seg, cache_dir / f"seg_{i:04d}.mp4")
                            _l2v_write_cache_manifest(cache_dir, fingerprint, nsh, i + 1, shot_durations)
                        break
                    except (OSError, RuntimeError) as e:
                        if (
                            _l2v_chain_inspection_keyframe_retry_enabled()
                            and not use_keyframe and not inspection_retry
                            and prev_tail_png is not None
                            and isinstance(e, RuntimeError)
                            and _is_bailian_l2v_data_inspection_failed(e)
                        ):
                            inspection_retry = True
                            continue
                        if segments and _l2v_partial_on_fail_enabled():
                            partial = output_mp4.with_name(f"{output_mp4.stem}.partial-{len(segments)}of{nsh}{output_mp4.suffix}")
                            output_mp4.parent.mkdir(parents=True, exist_ok=True)
                            logger.warning("镜头 %d/%d 失败；此前 %d 镜已成功（调用已计费）。正在写入部分成片：%s", i + 1, nsh, len(segments), partial)
                            _merge_video_segments(ffmpeg_exe, segments, partial, seconds_per_shot, segment_durations=shot_durations[:len(segments)])
                        raise
        else:
            # ------ 非链式：三路并发 ------ #
            # 有定妆图的镜头 → R2V（wan2.6-r2v，一步到位）
            # 无角色的空镜  → T2V（wan2.6-t2v，直接文生视频，跳过 T2I）
            # 有角色但无定妆图（降级）→ T2I → I2V（两步）
            import time as _time
            from .video_t2v_bailian_kling import submit_r2v_task, create_t2v_task

            needs = [i for i in range(nsh) if i not in cached_segments]

            # 三路分流
            needs_r2v: list[int] = []
            needs_t2v: list[int] = []   # 空镜：characters_on_screen 为空
            needs_t2i: list[int] = []   # 有角色但无定妆图（降级）
            shot_ref_paths: dict[int, list[Any]] = {}
            for i in needs:
                _, _, _, ref_paths, _ = _build_shot_prompts(i, None)
                on_screen = [str(x).strip() for x in ((shots[i].get("visual") or {}).get("characters_on_screen") or []) if str(x).strip()]
                if ref_paths:
                    needs_r2v.append(i)
                    shot_ref_paths[i] = ref_paths
                elif not on_screen:
                    needs_t2v.append(i)
                else:
                    needs_t2i.append(i)

            logger.info(
                "非链式并发：%d 镜有定妆图 → R2V，%d 镜空镜 → T2V，%d 镜有角色无定妆 → T2I+I2V",
                len(needs_r2v), len(needs_t2v), len(needs_t2i),
            )

            # --- R2V 并发提交 ---
            r2v_task_ids: dict[int, str] = {}
            for i in needs_r2v:
                img_prompt, _mood, motion, ref_paths, _po = _build_shot_prompts(i, None)
                from .storyboard_render import shot_to_sound_description as _sound_desc
                sound = _sound_desc(shots[i], characters=chars)
                r2v_prompt = f"{img_prompt}。{motion}" + (f"。{sound}" if sound else "")
                dur = shot_durations[i]
                dur_hint = max(1, min(dur_cap, int(round(dur))))
                try:
                    tid = submit_r2v_task(ref_paths, r2v_prompt, duration_sec=dur_hint)
                    r2v_task_ids[i] = tid
                    logger.info("镜头 %d/%d：R2V 任务已提交 task_id=%s", i + 1, nsh, tid[:12])
                except RuntimeError as e:
                    raise RuntimeError(f"镜头 {i+1} R2V 任务提交失败：{e}") from e
                _time.sleep(0.2)

            # --- T2V 并发提交（空镜，跳过 T2I） ---
            t2v_task_ids: dict[int, str] = {}
            for i in needs_t2v:
                img_prompt, _mood, motion, _ref_paths, _po = _build_shot_prompts(i, None)
                from .storyboard_render import shot_to_sound_description as _sound_desc
                sound = _sound_desc(shots[i], characters=chars)
                t2v_prompt = f"{img_prompt}。{motion}" + (f"。{sound}" if sound else "")
                dur = shot_durations[i]
                dur_hint = max(1, min(dur_cap, int(round(dur))))
                try:
                    tid = create_t2v_task(t2v_prompt, duration_sec=dur_hint)
                    t2v_task_ids[i] = tid
                    logger.info("镜头 %d/%d：T2V 任务已提交（空镜）task_id=%s", i + 1, nsh, tid[:12])
                except RuntimeError as e:
                    raise RuntimeError(f"镜头 {i+1} T2V 任务提交失败：{e}") from e
                _time.sleep(0.2)

            # --- T2I 同步下载（有角色但无定妆图的降级路径）---
            keyframe_pngs: dict[int, Path] = {}
            for i in needs_t2i:
                img_prompt, mood_seed, _motion, _ref_paths, _po = _build_shot_prompts(i, None)
                png = frames / f"shot_{i:04d}.png"
                logger.info("镜头 %d/%d：T2I 同步生成关键帧（有角色但无定妆图）…", i + 1, nsh)
                shot_neg = str((shots[i].get("visual") or {}).get("negative_prompt") or "")
                try:
                    from .illustration import download_illustration_from_prompt as _dl_t2i
                    _dl_t2i(img_prompt, mood_seed, png, scene_index=i, strict_illustration=True,
                            style=style, negative_prompt=shot_neg)
                    keyframe_pngs[i] = png
                except RuntimeError as e:
                    raise RuntimeError(f"镜头 {i+1} T2I 失败：{e}") from e

            # --- 并发轮询 R2V ---
            raw_videos: dict[int, Path] = {}
            pending_r2v = set(r2v_task_ids.keys())
            while pending_r2v:
                for i in list(pending_r2v):
                    raw_r2v = tmp / f"l2v_raw_{i:04d}.mp4"
                    try:
                        poll_video_task_to_file(r2v_task_ids[i], raw_r2v, label=f"R2V镜头{i+1}/{nsh}")
                        raw_videos[i] = raw_r2v
                        pending_r2v.discard(i)
                    except RuntimeError as e:
                        raise RuntimeError(f"镜头 {i+1} R2V 失败：{e}") from e
                if pending_r2v:
                    _time.sleep(15.0)

            # --- 并发轮询 T2V（空镜）---
            pending_t2v = set(t2v_task_ids.keys())
            while pending_t2v:
                for i in list(pending_t2v):
                    raw_t2v = tmp / f"l2v_raw_{i:04d}.mp4"
                    try:
                        poll_video_task_to_file(t2v_task_ids[i], raw_t2v, label=f"T2V空镜{i+1}/{nsh}")
                        raw_videos[i] = raw_t2v
                        pending_t2v.discard(i)
                    except RuntimeError as e:
                        raise RuntimeError(f"镜头 {i+1} T2V 失败：{e}") from e
                if pending_t2v:
                    _time.sleep(15.0)

            # --- 并发提交 I2V（有角色无定妆图降级路径）---
            i2v_task_ids: dict[int, str] = {}
            for i in needs_t2i:
                png = keyframe_pngs[i]
                _img_prompt, _mood_seed, motion, _ref_paths, _po = _build_shot_prompts(i, None)
                dur = shot_durations[i]
                dur_hint = max(1, min(dur_cap, int(round(dur))))
                try:
                    tid = _submit_l2v(png, motion, model=bailian_l2v_model, duration_sec=dur_hint)
                    i2v_task_ids[i] = tid
                    logger.info("镜头 %d/%d：I2V 任务已提交 task_id=%s", i + 1, nsh, tid[:12])
                except RuntimeError as e:
                    raise RuntimeError(f"镜头 {i+1} I2V 任务提交失败：{e}") from e
                _time.sleep(0.2)

            # --- 并发轮询 I2V ---
            pending_i2v = set(i2v_task_ids.keys())
            while pending_i2v:
                for i in list(pending_i2v):
                    raw_l2v = tmp / f"l2v_raw_{i:04d}.mp4"
                    try:
                        poll_video_task_to_file(i2v_task_ids[i], raw_l2v, label=f"镜头{i+1}/{nsh}")
                        raw_videos[i] = raw_l2v
                        pending_i2v.discard(i)
                    except RuntimeError as e:
                        raise RuntimeError(f"镜头 {i+1} I2V 失败：{e}") from e
                if pending_i2v:
                    _time.sleep(15.0)
            logger.info("全部 %d 个视频片段已下载，开始归一化…", len(raw_videos))

            # 归一化并按顺序收集 segments
            for i in range(nsh):
                if i in cached_segments:
                    segments.append(cached_segments[i])
                    continue
                raw_l2v = raw_videos[i]
                dur = shot_durations[i]
                seg = tmp / f"seg_{i:04d}.mp4"
                _normalize_video_segment_for_merge(ffmpeg_exe, raw_l2v, seg, dur)
                ad = media_duration_sec(seg)
                if ad > 0:
                    shot_durations[i] = ad
                segments.append(seg)
                if l2v_resume:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(seg, cache_dir / f"seg_{i:04d}.mp4")
                    _l2v_write_cache_manifest(cache_dir, fingerprint, nsh, i + 1, shot_durations)
        output_mp4.parent.mkdir(parents=True, exist_ok=True)
        silent_merged = tmp / "l2v_merged_silent.mp4"
        _merge_video_segments(
            ffmpeg_exe,
            segments,
            silent_merged,
            seconds_per_shot,
            segment_durations=shot_durations,
        )
        shutil.move(str(silent_merged), str(output_mp4))
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def run_from_storyboard_json(
    storyboard_json: Path,
    output_mp4: Path,
    seconds_per_shot: float = 3.0,
    *,
    save_storyboard_path: Path | None = None,
    strict_illustration: bool = False,
    style: str = "real",
    l2v_chain: bool | None = None,
    bailian_l2v_model: str | None = None,
    l2v_minimal_bailian_prompt: bool = False,
    rerun_shot_indices: set[int] | None = None,
    production_bible_path: Path | None = None,
    character_sheet_dir: Path | None = None,
    l2v_resume: bool = True,
    ignore_fingerprint: bool = False,
) -> None:
    """从已有分镜/剧本 JSON 直接跑 L2V 成片（不再调用小说生成分镜）。"""
    from .production_bible_io import apply_production_bible_to_storyboard, load_production_bible
    from .storyboard_io import load_storyboard, validate_storyboard

    data = load_storyboard(storyboard_json)
    validate_storyboard(data)
    bible: dict[str, Any] | None = None
    if production_bible_path is not None:
        p = production_bible_path
        if not p.is_file():
            raise FileNotFoundError(f"找不到制作圣经：{p}")
        bible = load_production_bible(p)
        apply_production_bible_to_storyboard(data, bible)
        validate_storyboard(data)
        logger.info("已从 JSON 加载分镜并套用制作圣经：%s", p)
    lock = _storyboard_plain_text_for_lock(data)
    run_l2v_prepared_storyboard(
        data,
        output_mp4,
        seconds_per_shot,
        novel_text_for_series_lock=lock,
        save_storyboard_path=save_storyboard_path,
        strict_illustration=strict_illustration,
        style=style,
        l2v_chain=l2v_chain,
        bailian_l2v_model=bailian_l2v_model,
        l2v_minimal_bailian_prompt=l2v_minimal_bailian_prompt,
        rerun_shot_indices=rerun_shot_indices,
        bible=bible,
        character_sheet_dir=character_sheet_dir,
        l2v_resume=l2v_resume,
        compress_shots=False,
        ignore_fingerprint=ignore_fingerprint,
    )


def run_from_txt_minimax_l2v_wan(
    input_txt: Path,
    output_mp4: Path,
    seconds_per_shot: float = 3.0,
    *,
    save_storyboard_path: Path | None = None,
    strict_illustration: bool = False,
    style: str = "real",
    l2v_chain: bool | None = None,
    bailian_l2v_model: str | None = None,
    l2v_minimal_bailian_prompt: bool = False,
    rerun_shot_indices: set[int] | None = None,
    production_bible_path: Path | None = None,
    character_sheet_dir: Path | None = None,
    l2v_resume: bool = True,
) -> None:
    """
    分镜 + 百炼文生图 + 百炼图生视频（默认模型 wan2.2-i2v-flash，CLI --l2v-model 或环境变量 BAILIAN_WAN_L2V_MODEL 可切换）。

    尾帧链式 L2V：无定妆目录时默认开启（第 2 镜起可用上一镜尾帧作图生视频输入，动作更连贯）。
    若已配置 character_sheet_dir 且含 ``*_costume_sheet.png``，**强制关闭**链式，每镜走关键帧，
    避免尾帧未过定妆参考导致脸漂（与「不接受脸漂」策略一致）。

    l2v_minimal_bailian_prompt：仅向百炼提交短运镜文案（不含全片故事锚点与叙事链），可缓解输入侧 DataInspectionFailed；
    若仍失败，多为首帧图触审，需弱化文生图或换题材。

    默认生成 MiniMax TTS 旁白并混流（--no-l2v-tts 或 TOVEDIO_L2V_TTS=0 可关）。

    production_bible_path：可选；若提供则加载制作圣经（选角/定妆/搭景），分镜生成后强制沿用其中
    characters，并在文生图提示中注入场景锁定，减少每次重跑人物与场景漂移。

    character_sheet_dir：可选；目录内 ``{character_id}_costume_sheet.png`` 作为关键帧语义参考目录，
    用于保证角色与服装一致性并抑制跨镜漂移。
    """
    from .minimax_client import generate_storyboard
    from .production_bible_io import apply_production_bible_to_storyboard, load_production_bible
    from .storyboard_io import validate_storyboard

    raw = input_txt.read_text(encoding="utf-8")
    bible: dict[str, Any] | None = None
    if production_bible_path is not None:
        p = production_bible_path
        if not p.is_file():
            raise FileNotFoundError(f"找不到制作圣经：{p}")
        bible = load_production_bible(p)
        logger.info("已加载制作圣经（锁定选角/定妆/主场景）：%s", p)

    data = generate_storyboard(raw, style=style, production_bible=bible)
    if bible is not None:
        apply_production_bible_to_storyboard(data, bible)
        validate_storyboard(data)
    run_l2v_prepared_storyboard(
        data,
        output_mp4,
        seconds_per_shot,
        novel_text_for_series_lock=raw,
        save_storyboard_path=save_storyboard_path,
        strict_illustration=strict_illustration,
        style=style,
        l2v_chain=l2v_chain,
        bailian_l2v_model=bailian_l2v_model,
        l2v_minimal_bailian_prompt=l2v_minimal_bailian_prompt,
        rerun_shot_indices=rerun_shot_indices,
        bible=bible,
        character_sheet_dir=character_sheet_dir,
        l2v_resume=l2v_resume,
    )
