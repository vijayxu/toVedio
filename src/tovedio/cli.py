"""命令行（精简版）：小说文本转影视化短视频（双模式 + 双风格）。"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .minimax_client import _ensure_dotenv_loaded, _get_api_key
from .pipeline import (
    analyze_mp4_local,
    ensure_ffmpeg,
    ensure_ffprobe,
    run_from_storyboard_json,
    run_from_storyboard_json_t2v,
    run_from_txt_minimax_l2v_wan,
    run_from_txt_minimax_t2v_direct,
)

logger = logging.getLogger(__name__)


def _print_check(ok: bool, label: str, detail: str) -> None:
    tag = "[OK]" if ok else "[FAIL]"
    print(f"{tag} {label}: {detail}")


def _python_ok() -> tuple[bool, str]:
    vi = sys.version_info
    if (vi.major, vi.minor) >= (3, 10):
        return True, f"{vi.major}.{vi.minor}.{vi.micro}"
    return False, f"当前 {vi.major}.{vi.minor}.{vi.micro}，需要 >= 3.10"


def _run_self_check(*, input_path: Path | None, need_minimax_key: bool, need_dashscope_key: bool) -> int:
    print("=== toVedio 自检（self-check）===")
    failed = 0

    py_ok, py_msg = _python_ok()
    _print_check(py_ok, "Python 版本", py_msg)
    if not py_ok:
        failed += 1

    try:
        ffmpeg_exe = ensure_ffmpeg()
        _print_check(True, "ffmpeg", f"可用：{ffmpeg_exe}")
    except RuntimeError as e:
        _print_check(False, "ffmpeg", str(e))
        failed += 1

    try:
        ffprobe_exe = ensure_ffprobe()
        _print_check(True, "ffprobe", f"可用：{ffprobe_exe}")
    except RuntimeError as e:
        _print_check(False, "ffprobe", str(e))
        failed += 1

    if input_path is not None:
        input_ok = input_path.is_file()
        _print_check(input_ok, "输入文件", f"{input_path}")
        if not input_ok:
            print(f"      建议：确认路径存在后重试，例如：py -3 run_tovedio.py \"{input_path}\" -o out.mp4")
            failed += 1
    else:
        _print_check(True, "输入文件", "未提供（self-check 模式可跳过）")

    if need_minimax_key:
        key_ok = bool(_get_api_key())
        if key_ok:
            _print_check(True, "MINIMAX_API_KEY", "已配置")
        else:
            _print_check(False, "MINIMAX_API_KEY", "未配置（mode=l2v 需要）")
            print("      建议：在仓库根目录创建 .env 并设置：MINIMAX_API_KEY=你的密钥")
            failed += 1
    if need_dashscope_key:
        dash_ok = _bailian_key_ok()
        _print_check(dash_ok, "DASHSCOPE_API_KEY", "已配置" if dash_ok else "未配置（百炼 Wan2.6 需要）")
        if not dash_ok:
            print("      建议：在仓库根目录创建 .env 并设置：DASHSCOPE_API_KEY=你的百炼密钥")
            failed += 1

    if failed:
        print("\n下一步建议（Windows）：")
        print("  1) 安装 ffmpeg：winget install Gyan.FFmpeg")
        print("  2) 配置 MiniMax：在 .env 填写 MINIMAX_API_KEY")
        print("  3) 重试自检：py -3 run_tovedio.py --self-check")
        return 2

    print("\n下一步命令示例：")
    print("  py -3 run_tovedio.py --self-check")
    print("  py -3 run_tovedio.py novel.txt -o out.mp4 --mode t2v --style real")
    print("  py -3 run_tovedio.py novel.txt -o out.mp4 --mode t2v --style anime")
    print("  py -3 run_tovedio.py novel.txt -o out.mp4 --mode l2v --style real")
    print("  py -3 run_tovedio.py novel.txt -o out.mp4 --mode l2v --style anime")
    print("  py -3 run_tovedio.py --analyze-video out.mp4")
    print("  py -3 run_tovedio.py --storyboard-only --input novel.txt --save-storyboard sb.json")
    print("  py -3 run_tovedio.py --validate-storyboard sb.json --input novel.txt")
    print("  py -3 run_tovedio.py --production-bible-only -i novel.txt --save-production-bible bible.json")
    print("  py -3 run_tovedio.py -i novel.txt -o out.mp4 --production-bible bible.json")
    print("  py -3 run_tovedio.py --screenplay-only --save-storyboard script.json --pitch \"雨夜便利店\"")
    print("  py -3 run_tovedio.py --from-storyboard script.json -o out.mp4")
    print("  py -3 run_tovedio.py --character-sheets-only -i storyboard.json --save-character-dir artifacts/exports/char_sheets")
    print("  py -3 run_tovedio.py --from-storyboard storyboard.json -o out.mp4 --character-sheet-dir artifacts/exports/char_sheets")
    return 0


def _bailian_key_ok() -> bool:
    _ensure_dotenv_loaded()
    key = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    return key not in ("", "your_key_here", "你的密钥")


def _run_light_preflight(
    *,
    input_path: Path | None,
    need_minimax_key: bool,
    need_bailian_key: bool,
    require_input_file: bool = True,
) -> int:
    if require_input_file:
        if input_path is None or not input_path.is_file():
            ip = input_path if input_path is not None else Path("(未指定)")
            print(
                f"错误：找不到输入文件：{ip}\n"
                "建议（Windows）：确认路径后重试，例如：py -3 run_tovedio.py novel.txt -o out.mp4 --mode t2v --style real",
                file=sys.stderr,
            )
            return 2
    try:
        ensure_ffmpeg()
        ensure_ffprobe()
    except RuntimeError as e:
        print(
            f"错误：{e}\n"
            "建议（Windows）：先执行 winget install Gyan.FFmpeg，然后重新打开终端后重试。",
            file=sys.stderr,
        )
        return 2
    if need_minimax_key and not _get_api_key():
        print(
            "错误：未配置 MINIMAX_API_KEY（真视频模式需要）。\n"
            "建议：在仓库根目录 .env 填写 MINIMAX_API_KEY=你的密钥，"
            "再执行：py -3 run_tovedio.py novel.txt -o out.mp4",
            file=sys.stderr,
        )
        return 2
    if need_bailian_key and not _bailian_key_ok():
        print(
            "错误：未配置 DASHSCOPE_API_KEY（Wan2.6 模式需要）。\n"
            "建议：在仓库根目录 .env 填写 DASHSCOPE_API_KEY=你的百炼密钥，"
            "再执行：py -3 run_tovedio.py novel.txt -o out.mp4 --mode t2v --style real",
            file=sys.stderr,
        )
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="小说转影视化短视频：仅保留两模式（t2v / l2v）与两风格（anime / real）。",
    )
    parser.add_argument("input", type=Path, nargs="?", help="输入 .txt 路径（UTF-8）；也可用 -i/--input")
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        dest="input_file",
        default=None,
        metavar="TXT",
        help="输入小说 .txt（与位置参数二选一；若同时给出，以此为准）",
    )
    parser.add_argument("-o", "--output", type=Path, help="输出 .mp4 路径")
    parser.add_argument(
        "-s",
        "--seconds",
        type=float,
        default=4.0,
        help="默认时长秒数（t2v 作为目标时长；l2v 作为镜头默认时长）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="过程产物根目录（临时工作区、ffmpeg concat 列表等）；默认仓库下 artifacts/；等价于设置 TOVEDIO_ARTIFACT_DIR",
    )
    parser.add_argument(
        "--strict-illustration",
        action="store_true",
        help="在线配图失败时直接报错，不使用氛围色场降级",
    )
    parser.add_argument(
        "--save-storyboard",
        type=Path,
        default=None,
        metavar="PATH",
        help="将模型输出的分镜 JSON 保存到该路径，便于核对",
    )
    parser.add_argument("--mode", choices=("t2v", "l2v"), default="l2v", help="视频生成模式：t2v=文生视频；l2v=文生图后图生视频（连贯性更好，默认）")
    parser.add_argument("--style", choices=("anime", "real"), default="real", help="成片风格：anime=动漫风；real=现实风")
    parser.add_argument(
        "--t2v-model",
        type=str,
        default=None,
        metavar="NAME",
        help="mode=t2v 时指定百炼文生视频模型（覆盖环境变量 BAILIAN_WAN_T2V_MODEL，默认 wan2.6-t2v）",
    )
    parser.add_argument(
        "--l2v-model",
        type=str,
        default=None,
        metavar="NAME",
        help="mode=l2v 时指定百炼图生视频模型（覆盖环境变量 BAILIAN_WAN_L2V_MODEL）",
    )
    parser.add_argument(
        "--illustration-model",
        type=str,
        default=None,
        metavar="NAME",
        help="指定百炼文生图模型名（覆盖环境变量 BAILIAN_IMAGE_MODEL，默认 wan2.6-t2i）",
    )
    parser.add_argument(
        "--illustration-backend",
        choices=("auto", "bailian"),
        default=None,
        metavar="NAME",
        help="指定配图后端（覆盖环境变量 ILLUSTRATION_BACKEND；当前固定为 bailian）",
    )
    parser.add_argument(
        "--bailian-max-attempts",
        type=int,
        default=None,
        metavar="N",
        help="百炼视频（T2V/L2V）每镜/每次任务最大尝试次数，含首次（默认 3，等同最多 2 次重试；见 TOVEDIO_BAILIAN_VIDEO_MAX_ATTEMPTS）",
    )
    parser.add_argument(
        "--no-l2v-chain",
        action="store_true",
        help="关闭尾帧链式 L2V（每镜独立关键帧）。有定妆目录时链式本即强制关闭；无定妆时与默认开启链式相反",
    )
    parser.add_argument(
        "--l2v-minimal-prompt",
        action="store_true",
        help="向百炼 L2V 仅提交短运镜文案（不附带故事锚点/叙事链），缓解输入侧 DataInspectionFailed；仍失败多为首帧图触审",
    )
    parser.add_argument(
        "--no-l2v-resume",
        action="store_true",
        help="关闭 L2V 断点续跑（默认开启：在 -o 成片旁 {stem}.l2v_cache 复用已成功镜头 seg）",
    )
    parser.add_argument(
        "--rerun-shots",
        type=str,
        default=None,
        metavar="LIST",
        help="仅重跑指定镜头（1-based，逗号分隔，如 1,3），其余镜头仍尽量复用缓存（需未关闭 l2v-resume）",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="执行双模式环境自检（Python/ffmpeg/ffprobe/供应商密钥），不渲染视频",
    )
    parser.add_argument(
        "--analyze-video",
        type=Path,
        default=None,
        metavar="MP4",
        help="仅 ffprobe 分析已有成片（零 API 费用）",
    )
    parser.add_argument(
        "--storyboard-only",
        action="store_true",
        help="只生成分镜 JSON（需 --input 与 --save-storyboard）；不配图、不百炼、不 TTS，仅 MiniMax 分镜调用",
    )
    parser.add_argument(
        "--validate-storyboard",
        type=Path,
        default=None,
        metavar="JSON",
        help="校验分镜 JSON 并打印与原文的启发式对照（需同时 --input 小说；零 API）",
    )
    parser.add_argument(
        "--production-bible",
        type=Path,
        default=None,
        metavar="JSON",
        help="加载制作圣经（选角/定妆/主场景）；l2v 或 --storyboard-only 时锁定人物与场景描述",
    )
    parser.add_argument(
        "--production-bible-only",
        action="store_true",
        help="仅生成制作圣经 JSON（需 -i 与 --save-production-bible）；一次 MiniMax，不含分镜与视频",
    )
    parser.add_argument(
        "--save-production-bible",
        type=Path,
        default=None,
        metavar="PATH",
        help="写入制作圣经路径（配合 --production-bible-only）",
    )
    parser.add_argument(
        "--screenplay-only",
        action="store_true",
        help="无小说：由模型原创剧本并输出分镜 JSON（需 --save-storyboard）；可用 --pitch 与可选 -i 梗概文件",
    )
    parser.add_argument(
        "--pitch",
        type=str,
        default=None,
        metavar="TEXT",
        help="一句话/一小段创作方向（配合 --screenplay-only；不传则用内置温和默认）",
    )
    parser.add_argument(
        "--from-storyboard",
        type=Path,
        default=None,
        metavar="JSON",
        help="跳过小说：从已有分镜/剧本 JSON 直接跑成片（mode=l2v 或 mode=t2v；需 -o；可不提供小说 -i）",
    )
    parser.add_argument(
        "--character-sheets-only",
        action="store_true",
        help="仅根据剧本/分镜/制作圣经 JSON 中的 characters 生成定妆照 PNG（需 -i JSON 与 --save-character-dir）",
    )
    parser.add_argument(
        "--save-character-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="定妆照输出目录（配合 --character-sheets-only）",
    )
    parser.add_argument(
        "--character-sheet-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="定妆 PNG 目录（{角色id}_costume_sheet.png）；L2V 关键帧走 MiniMax subject_reference；或设 TOVEDIO_CHARACTER_SHEET_DIR",
    )
    args = parser.parse_args()
    if args.input_file is not None:
        args.input = args.input_file
    if args.artifact_dir is not None:
        os.environ["TOVEDIO_ARTIFACT_DIR"] = str(args.artifact_dir.expanduser().resolve())
    if args.illustration_model is not None:
        m = str(args.illustration_model).strip()
        os.environ["BAILIAN_IMAGE_MODEL"] = m
    if args.illustration_backend is not None:
        os.environ["ILLUSTRATION_BACKEND"] = str(args.illustration_backend).strip()

    rerun_shot_indices: set[int] | None = None
    if args.rerun_shots:
        rerun_shot_indices = set()
        for part in str(args.rerun_shots).replace(" ", "").split(","):
            if not part:
                continue
            try:
                n = int(part)
            except ValueError:
                print(f"错误：--rerun-shots 含非法值：{part}", file=sys.stderr)
                return 2
            if n <= 0:
                print(f"错误：--rerun-shots 仅支持从 1 开始的镜头序号：{n}", file=sys.stderr)
                return 2
            rerun_shot_indices.add(n - 1)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    if args.self_check:
        return _run_self_check(
            input_path=args.input,
            need_minimax_key=(args.mode == "l2v"),
            need_dashscope_key=True,
        )
    if args.analyze_video is not None:
        p = args.analyze_video
        if not p.is_file():
            print(f"错误：找不到视频：{p}", file=sys.stderr)
            return 2
        try:
            ensure_ffprobe()
            analyze_mp4_local(p)
        except RuntimeError as e:
            print(f"错误：{e}", file=sys.stderr)
            return 1
        return 0
    if args.character_sheets_only:
        if args.storyboard_only or args.screenplay_only or args.production_bible_only:
            print(
                "错误：--character-sheets-only 不要与 --storyboard-only / --screenplay-only / --production-bible-only 同时使用",
                file=sys.stderr,
            )
            return 2
        if args.input is None or not args.input.is_file():
            print(
                "错误：--character-sheets-only 需要剧本/分镜 JSON（位置参数或 -i）\n"
                "示例：py -3 run_tovedio.py --character-sheets-only -i storyboard.json --save-character-dir artifacts/exports/char_sheets",
                file=sys.stderr,
            )
            return 2
        if args.save_character_dir is None:
            print(
                "错误：--character-sheets-only 必须配合 --save-character-dir DIR",
                file=sys.stderr,
            )
            return 2
        try:
            ensure_ffmpeg()
        except RuntimeError:
            pass
        from .character_sheets import generate_character_costume_sheets

        paths = generate_character_costume_sheets(
            args.input,
            args.save_character_dir,
            style=args.style,
            strict_illustration=args.strict_illustration,
        )
        print(f"\n已生成 {len(paths)} 张定妆照：")
        for p in paths:
            print(f"  · {p}")
        print(f"清单：{args.save_character_dir / 'character_sheets_manifest.json'}")
        return 0
    if args.validate_storyboard is not None:
        if args.input is None or not args.input.is_file():
            print(
                "错误：--validate-storyboard 需要同时指定小说文件，例如：\n"
                "  py -3 run_tovedio.py --validate-storyboard sb.json --input novel.txt",
                file=sys.stderr,
            )
            return 2
        jp = args.validate_storyboard
        if not jp.is_file():
            print(f"错误：找不到分镜 JSON：{jp}", file=sys.stderr)
            return 2
        from .storyboard_io import load_storyboard, print_storyboard_diagnostics, validate_storyboard

        raw = args.input.read_text(encoding="utf-8")
        data = load_storyboard(jp)
        validate_storyboard(data)
        print_storyboard_diagnostics(raw, data)
        print(f"\nSchema 校验通过：{jp}")
        return 0
    if args.production_bible_only:
        if args.storyboard_only:
            print("错误：不要同时使用 --production-bible-only 与 --storyboard-only", file=sys.stderr)
            return 2
        if args.input is None or not args.input.is_file():
            print(
                "错误：--production-bible-only 需要小说路径（位置参数或 -i）\n"
                "示例：py -3 run_tovedio.py --production-bible-only -i novel.txt --save-production-bible bible.json",
                file=sys.stderr,
            )
            return 2
        if args.save_production_bible is None:
            print(
                "错误：--production-bible-only 必须配合 --save-production-bible PATH",
                file=sys.stderr,
            )
            return 2
        if not _get_api_key():
            print("错误：未配置 MINIMAX_API_KEY", file=sys.stderr)
            return 2
        from .minimax_client import generate_production_bible
        from .production_bible_io import save_production_bible

        raw = args.input.read_text(encoding="utf-8")
        data = generate_production_bible(raw, style=args.style)
        save_production_bible(data, args.save_production_bible)
        print(f"\n已写入制作圣经：{args.save_production_bible}（可多次用于 --production-bible 生成视频）。")
        return 0
    if args.screenplay_only:
        if args.storyboard_only or args.production_bible_only:
            print(
                "错误：--screenplay-only 不要与 --storyboard-only 或 --production-bible-only 同时使用",
                file=sys.stderr,
            )
            return 2
        if args.save_storyboard is None:
            print(
                "错误：--screenplay-only 必须配合 --save-storyboard PATH\n"
                "示例：py -3 run_tovedio.py --screenplay-only --save-storyboard script.json --pitch \"雨夜便利店\"",
                file=sys.stderr,
            )
            return 2
        if not _get_api_key():
            print("错误：未配置 MINIMAX_API_KEY", file=sys.stderr)
            return 2
        preflight_sp = _run_light_preflight(
            input_path=args.input,
            need_minimax_key=True,
            need_bailian_key=False,
            require_input_file=False,
        )
        if preflight_sp != 0:
            return preflight_sp
        from .minimax_client import generate_screenplay_storyboard
        from .production_bible_io import load_production_bible
        from .storyboard_io import print_storyboard_diagnostics, save_storyboard

        pitch = (args.pitch or "").strip()
        extra = ""
        if args.input is not None and args.input.is_file():
            extra = args.input.read_text(encoding="utf-8").strip()
        if extra and pitch:
            brief = f"{pitch}\n\n【补充说明】\n{extra}"
        elif extra:
            brief = extra
        elif pitch:
            brief = pitch
        else:
            brief = (
                "原创一部当代都市温情短篇：日常小事、人物不超过三人、场景清晰可拍，"
                "结局略带余味，适合约一分钟短视频；避免暴力、色情与真人指向。"
            )
        bible = None
        if args.production_bible is not None:
            bp = args.production_bible
            if not bp.is_file():
                print(f"错误：找不到制作圣经：{bp}", file=sys.stderr)
                return 2
            bible = load_production_bible(bp)
        data = generate_screenplay_storyboard(brief=brief, style=args.style, production_bible=bible)
        save_storyboard(data, args.save_storyboard)
        print_storyboard_diagnostics(brief, data)
        print(f"\n已写入剧本分镜：{args.save_storyboard}（可用 --from-storyboard 生成视频）。")
        return 0
    if args.storyboard_only:
        if args.input is None or not args.input.is_file():
            print("错误：--storyboard-only 需要 --input 小说路径", file=sys.stderr)
            return 2
        if args.save_storyboard is None:
            print(
                "错误：--storyboard-only 必须配合 --save-storyboard PATH\n"
                "示例：py -3 run_tovedio.py --storyboard-only --input novel.txt --save-storyboard sb.json",
                file=sys.stderr,
            )
            return 2
        if not _get_api_key():
            print("错误：未配置 MINIMAX_API_KEY（分镜仍走 MiniMax）", file=sys.stderr)
            return 2
        from .minimax_client import generate_storyboard
        from .production_bible_io import apply_production_bible_to_storyboard, load_production_bible
        from .storyboard_io import print_storyboard_diagnostics, save_storyboard, validate_storyboard

        raw = args.input.read_text(encoding="utf-8")
        bible = None
        if args.production_bible is not None:
            bp = args.production_bible
            if not bp.is_file():
                print(f"错误：找不到制作圣经：{bp}", file=sys.stderr)
                return 2
            bible = load_production_bible(bp)
        data = generate_storyboard(raw, style=args.style, production_bible=bible)
        if bible is not None:
            apply_production_bible_to_storyboard(data, bible)
            validate_storyboard(data)
        save_storyboard(data, args.save_storyboard)
        print_storyboard_diagnostics(raw, data)
        print(f"\n已写入 {args.save_storyboard}（未调用配图 / 百炼 / TTS）。")
        return 0
    if args.from_storyboard is not None:
        if args.output is None:
            print(
                "错误：--from-storyboard 需要输出路径 -o/--output\n"
                "示例：py -3 run_tovedio.py --from-storyboard script.json -o out.mp4",
                file=sys.stderr,
            )
            return 2
        sb = args.from_storyboard
        if not sb.is_file():
            print(f"错误：找不到分镜 JSON：{sb}", file=sys.stderr)
            return 2
        preflight_sb = _run_light_preflight(
            input_path=args.input,
            need_minimax_key=(args.mode == "l2v"),
            need_bailian_key=True,
            require_input_file=False,
        )
        if preflight_sb != 0:
            return preflight_sb
        if args.bailian_max_attempts is not None:
            n = max(1, min(10, int(args.bailian_max_attempts)))
            os.environ["TOVEDIO_BAILIAN_VIDEO_MAX_ATTEMPTS"] = str(n)
        try:
            if args.mode == "t2v":
                run_from_storyboard_json_t2v(
                    sb,
                    args.output,
                    seconds_per_shot=args.seconds,
                    style=args.style,
                    bailian_t2v_model=args.t2v_model,
                    production_bible_path=args.production_bible,
                )
            else:
                run_from_storyboard_json(
                    sb,
                    args.output,
                    seconds_per_shot=args.seconds,
                    save_storyboard_path=args.save_storyboard,
                    strict_illustration=args.strict_illustration,
                    style=args.style,
                    l2v_chain=(False if args.no_l2v_chain else None),
                    bailian_l2v_model=args.l2v_model,
                    l2v_minimal_bailian_prompt=args.l2v_minimal_prompt,
                    rerun_shot_indices=rerun_shot_indices,
                    production_bible_path=args.production_bible,
                    character_sheet_dir=args.character_sheet_dir,
                    l2v_resume=(not args.no_l2v_resume),
                )
        except ValueError as e:
            print(f"错误：{e}", file=sys.stderr)
            return 1
        except RuntimeError as e:
            print(f"错误：{e}", file=sys.stderr)
            return 1
        except OSError as e:
            print(f"错误：{e}", file=sys.stderr)
            return 1
        except FileNotFoundError as e:
            print(f"错误：{e}", file=sys.stderr)
            return 2
        logger.info("已生成：%s", args.output)
        return 0
    if args.input is None:
        print(
            "错误：缺少输入文件。\n"
            "示例：py -3 run_tovedio.py novel.txt -o out.mp4\n"
            "若仅有剧本 JSON：py -3 run_tovedio.py --from-storyboard script.json -o out.mp4",
            file=sys.stderr,
        )
        return 2
    if args.output is None:
        print(
            "错误：缺少输出路径（-o/--output）。\n"
            "示例：py -3 run_tovedio.py novel.txt -o out.mp4",
            file=sys.stderr,
        )
        return 2
    preflight_rc = _run_light_preflight(
        input_path=args.input,
        need_minimax_key=(args.mode == "l2v"),
        need_bailian_key=True,
        require_input_file=True,
    )
    if preflight_rc != 0:
        return preflight_rc
    if args.bailian_max_attempts is not None:
        n = max(1, min(10, int(args.bailian_max_attempts)))
        os.environ["TOVEDIO_BAILIAN_VIDEO_MAX_ATTEMPTS"] = str(n)
    try:
        if args.mode == "t2v":
            run_from_txt_minimax_t2v_direct(
                args.input,
                args.output,
                duration_hint_sec=int(max(1, min(10, round(args.seconds)))),
                style=args.style,
                bailian_t2v_model=args.t2v_model,
            )
        else:
            run_from_txt_minimax_l2v_wan(
                args.input,
                args.output,
                seconds_per_shot=args.seconds,
                save_storyboard_path=args.save_storyboard,
                strict_illustration=args.strict_illustration,
                style=args.style,
                l2v_chain=(False if args.no_l2v_chain else None),
                bailian_l2v_model=args.l2v_model,
                l2v_minimal_bailian_prompt=args.l2v_minimal_prompt,
                rerun_shot_indices=rerun_shot_indices,
                production_bible_path=args.production_bible,
                character_sheet_dir=args.character_sheet_dir,
                l2v_resume=(not args.no_l2v_resume),
            )
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    logger.info("已生成：%s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
