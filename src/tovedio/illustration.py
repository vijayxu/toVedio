"""根据场景文案生成「画面」：强制使用阿里云百炼文生图。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from .minimax_client import _ensure_dotenv_loaded

logger = logging.getLogger(__name__)

WIDTH = 1280
HEIGHT = 720

# 无 POLLINATIONS_API_KEY 时跳过 gen 网关，只提示一次（避免每镜刷屏）
_GEN_SKIP_LOGGED = False

_MAX_SCENE_CHARS = 420
_STYLE_SUFFIX_REAL = (
    ", cinematic realistic film shot, atmospheric lighting, emotional storytelling, "
    "wide composition, shallow depth of field, highly detailed, 8k, "
    "no text, no subtitles, no letters, no watermark"
)

_STYLE_SUFFIX_ANIME = (
    ", high quality anime style, clean line art, cel shading, vibrant color grading, "
    "cinematic composition, no text, no subtitles, no letters, no watermark"
)


def _style_suffix(style: str) -> str:
    return _STYLE_SUFFIX_ANIME if (style or "real").strip().lower() == "anime" else _STYLE_SUFFIX_REAL


def scene_to_visual_prompt(scene_text: str, *, style: str = "real") -> str:
    one_line = " ".join(scene_text.split())
    if len(one_line) > _MAX_SCENE_CHARS:
        one_line = one_line[:_MAX_SCENE_CHARS] + "…"
    return f"Chinese novel scene, story moment: {one_line}{_style_suffix(style)}"


def _pollinations_legacy_url(prompt: str, seed: int) -> str:
    """经典 URL 配图（官方文档中的 image.pollinations.ai/prompt/…）。"""
    encoded = urllib.parse.quote(prompt, safe="")
    return (
        f"https://image.pollinations.ai/prompt/{encoded}"
        f"?width={WIDTH}&height={HEIGHT}&nologo=true&enhance=true&seed={seed}"
    )


def _pollinations_gen_url(prompt: str, seed: int) -> str:
    """统一网关 GET /image/{prompt}，见 https://enter.pollinations.ai/api/docs/llm.txt"""
    encoded = urllib.parse.quote(prompt, safe="")
    return (
        f"https://gen.pollinations.ai/image/{encoded}"
        f"?model=flux&width={WIDTH}&height={HEIGHT}&seed={seed}&enhance=true"
    )


def _pollinations_api_key() -> str:
    return (os.environ.get("POLLINATIONS_API_KEY") or os.environ.get("POLLINATIONS_KEY") or "").strip()


def _pollinations_url_order() -> list[str]:
    """
    逗号分隔：legacy（image…/prompt）、gen（gen…/image）。
    默认仅 legacy；需要 gen 时请设置 POLLINATIONS_API_KEY 并显式写 legacy,gen。
    """
    raw = (os.environ.get("POLLINATIONS_URL_ORDER") or "legacy").strip().lower()
    parts = [p.strip() for p in raw.replace(" ", "").split(",") if p.strip()]
    out: list[str] = []
    aliases = {"image": "legacy", "gateway": "gen"}
    for p in parts:
        p = aliases.get(p, p)
        if p not in ("legacy", "gen"):
            continue
        if p not in out:
            out.append(p)
    return out if out else ["legacy"]


def _effective_pollinations_order() -> list[str]:
    """无 API Key 时不请求 gen，避免 401。"""
    global _GEN_SKIP_LOGGED
    base = _pollinations_url_order()
    if _pollinations_api_key():
        return base
    filtered = [x for x in base if x != "gen"]
    if len(filtered) < len(base) and not _GEN_SKIP_LOGGED:
        logger.warning(
            "未设置 POLLINATIONS_API_KEY，已跳过 gen.pollinations.ai（无密钥会返回 401）。"
            "需要统一网关时在 https://enter.pollinations.ai 申请密钥并设置 POLLINATIONS_URL_ORDER=legacy,gen"
        )
        _GEN_SKIP_LOGGED = True
    return filtered if filtered else ["legacy"]


def _pollinations_request_headers(for_gen: bool) -> dict[str, str]:
    h: dict[str, str] = {
        "User-Agent": "toVedio/0.2 (novel-to-shortvideo demo)",
    }
    if for_gen:
        key = _pollinations_api_key()
        if key:
            h["Authorization"] = f"Bearer {key}"
    return h


def _picsum_fallback_enabled() -> bool:
    """默认关闭：与剧情无关的随机网图。仅当显式 ILLUSTRATION_FALLBACK_PICSUM=1 时启用。"""
    v = (os.environ.get("ILLUSTRATION_FALLBACK_PICSUM") or "0").strip().lower()
    return v in ("1", "true", "yes", "on")


def _fetch_picsum_placeholder_png(scene_index: int, seed_text: str) -> bytes:
    """https://picsum.photos/ 按 seed 固定一张图，作最后手段。"""
    seed = (abs(hash(seed_text)) % 1_000_000_000) + scene_index * 7919
    url = f"https://picsum.photos/seed/{seed}/{WIDTH}/{HEIGHT}"
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "toVedio/0.2 (novel-to-shortvideo demo)"},
    )
    with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
        raw = resp.read()
    img = Image.open(BytesIO(raw))
    img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _illustration_backend() -> str:
    """兼容旧日志字段：当前固定为 bailian。"""
    _ensure_dotenv_loaded()
    return "bailian"


def _bailian_api_key() -> str | None:
    _ensure_dotenv_loaded()
    key = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if key in ("", "your_key_here", "你的密钥"):
        return None
    return key


def _bailian_image_base_url() -> str:
    return (os.environ.get("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com").strip().rstrip("/")


def _fetch_bailian_t2i_png_from_prompt(full_prompt: str, scene_index: int, *, style: str = "real", negative_prompt: str = "") -> tuple[bytes, str]:
    key = _bailian_api_key()
    if not key:
        raise RuntimeError("百炼文生图需要配置 DASHSCOPE_API_KEY。")
    prompt = full_prompt.strip()
    if len(prompt) > 1800:
        prompt = prompt[:1800] + "…"
    if "no watermark" not in prompt.lower():
        prompt = prompt + _style_suffix(style)
    model = (os.environ.get("BAILIAN_IMAGE_MODEL") or "wan2.6-t2i").strip()
    seed = 1000 + scene_index * 17
    body: dict[str, object] = {
        "model": model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": prompt}],
                }
            ]
        },
        "parameters": {
            "n": 1,
            "size": f"{WIDTH}*{HEIGHT}",
            "watermark": False,
            "prompt_extend": True,
            "seed": seed,
            **({"negative_prompt": negative_prompt} if negative_prompt.strip() else {}),
        },
    }
    base_url = _bailian_image_base_url()
    submit_url = f"{base_url}/api/v1/services/aigc/multimodal-generation/generation"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    ctx = ssl.create_default_context()

    # 同步调用（不带 X-DashScope-Async，等待模型直接返回结果）
    req = urllib.request.Request(
        submit_url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        raise RuntimeError(f"百炼文生图提交 HTTP {e.code}：{err_body or e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接百炼文生图接口：{e}") from e

    try:
        result_data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError("百炼文生图返回非 JSON。") from e
    if result_data.get("code"):
        raise RuntimeError(
            f"百炼文生图失败（code={result_data.get('code')}）：{result_data.get('message') or result_data}"
        )

    # 提取图片 URL（兼容两种响应格式）
    # 格式1: output.results[].url
    # 格式2: output.choices[].message.content[].image
    img_url: str | None = None
    out = result_data.get("output") or {}
    results = out.get("results") or []
    img_url = next(
        (r.get("url") for r in results if isinstance(r, dict) and r.get("url")),
        None,
    )
    if not img_url:
        for choice in (out.get("choices") or []):
            if not isinstance(choice, dict):
                continue
            content = (choice.get("message") or {}).get("content") or []
            for item in content:
                if isinstance(item, dict) and item.get("image"):
                    img_url = str(item["image"])
                    break
            if img_url:
                break
    if not img_url:
        raise RuntimeError(f"百炼文生图无图片 URL：{str(result_data)[:400]}")
    logger.info("百炼文生图完成：scene=%d model=%s", scene_index + 1, model)

    # 下载图片
    dl_req = urllib.request.Request(img_url, method="GET")
    try:
        with urllib.request.urlopen(dl_req, timeout=120, context=ctx) as dl_resp:
            return dl_resp.read(), img_url
    except urllib.error.URLError as e:
        raise RuntimeError(f"百炼文生图下载失败：{e}") from e


# ---------------------------------------------------------------------------
# 并发辅助：独立的提交 / 轮询接口（供 pipeline 两阶段并发使用）
# ---------------------------------------------------------------------------

def submit_t2i_task(full_prompt: str, scene_index: int, *, style: str = "real") -> str:
    """提交百炼文生图异步任务，立即返回 task_id（不阻塞等待结果）。"""
    key = _bailian_api_key()
    if not key:
        raise RuntimeError("百炼文生图需要配置 DASHSCOPE_API_KEY。")
    prompt = full_prompt.strip()
    if len(prompt) > 1800:
        prompt = prompt[:1800] + "…"
    if "no watermark" not in prompt.lower():
        prompt = prompt + _style_suffix(style)
    model = (os.environ.get("BAILIAN_IMAGE_MODEL") or "wan2.6-t2i").strip()
    seed = 1000 + scene_index * 17
    body: dict[str, object] = {
        "model": model,
        "input": {
            "messages": [{"role": "user", "content": [{"text": prompt}]}]
        },
        "parameters": {
            "n": 1,
            "size": f"{WIDTH}*{HEIGHT}",
            "watermark": False,
            "prompt_extend": True,
            "seed": seed,
        },
    }
    submit_url = f"{_bailian_image_base_url()}/api/v1/services/aigc/multimodal-generation/generation"
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        submit_url, data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        raise RuntimeError(f"百炼文生图提交 HTTP {e.code}：{err_body or e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接百炼文生图接口：{e}") from e
    try:
        submit_data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError("百炼文生图提交返回非 JSON。") from e
    if submit_data.get("code"):
        raise RuntimeError(
            f"百炼文生图提交失败（code={submit_data.get('code')}）：{submit_data.get('message') or submit_data}"
        )
    task_id = (submit_data.get("output") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"百炼文生图提交未返回 task_id：{str(submit_data)[:400]}")
    logger.info("百炼文生图任务已提交：task_id=%s model=%s scene=%d", task_id, model, scene_index + 1)
    return str(task_id)


def poll_t2i_task_to_png(task_id: str, out_png: Path, scene_index: int) -> None:
    """轮询已提交的文生图任务至完成，下载结果写入 out_png。"""
    key = _bailian_api_key()
    if not key:
        raise RuntimeError("百炼文生图需要配置 DASHSCOPE_API_KEY。")
    base_url = _bailian_image_base_url()
    query_url = f"{base_url}/api/v1/tasks/{urllib.parse.quote(task_id)}"
    ctx = ssl.create_default_context()
    _T2I_POLL_INTERVAL = 10.0
    _T2I_POLL_TIMEOUT = 300.0
    deadline = time.monotonic() + _T2I_POLL_TIMEOUT
    start = time.monotonic()
    last_log = time.monotonic() - _T2I_POLL_INTERVAL
    status = ""
    while time.monotonic() < deadline:
        qreq = urllib.request.Request(
            query_url, method="GET",
            headers={"Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(qreq, timeout=30, context=ctx) as qresp:
                qraw = qresp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise RuntimeError(f"百炼文生图查询 HTTP {e.code}：{err_body or e.reason}") from e
        data = json.loads(qraw)
        out = data.get("output") or {}
        status = (out.get("task_status") or "").strip().upper()
        now = time.monotonic()
        elapsed = now - start
        if now - last_log >= _T2I_POLL_INTERVAL:
            logger.info(
                "百炼文生图 %s [scene %d] 状态：%s，已等待 %.0fs",
                task_id[:12], scene_index + 1, status or "PENDING", elapsed,
            )
            last_log = now
        if status in ("FAILED", "CANCELED"):
            msg = out.get("message") or data.get("message") or str(data)[:300]
            raise RuntimeError(f"百炼文生图任务失败：{msg}")
        if status == "SUCCEEDED":
            logger.info(
                "百炼文生图 %s [scene %d] 完成，耗时 %.0fs",
                task_id[:12], scene_index + 1, elapsed,
            )
            image_url: str | None = None
            if isinstance(out.get("choices"), list) and out.get("choices"):
                choice0 = out["choices"][0] or {}
                msg_obj = choice0.get("message") or {}
                content = msg_obj.get("content") or []
                for item in content:
                    if isinstance(item, dict) and item.get("image"):
                        image_url = str(item["image"])
                        break
            if not image_url:
                if isinstance(out.get("images"), list) and out.get("images"):
                    img0 = out["images"][0]
                    if isinstance(img0, dict) and img0.get("url"):
                        image_url = str(img0["url"])
                    elif isinstance(img0, str):
                        image_url = img0
            if not image_url and out.get("image_url"):
                image_url = str(out.get("image_url"))
            if not image_url:
                raise RuntimeError(f"百炼文生图成功但未返回图片 URL：{str(data)[:800]}")
            req_img = urllib.request.Request(
                image_url,
                headers={"User-Agent": "toVedio/0.2 (novel-to-shortvideo demo)"},
            )
            with urllib.request.urlopen(req_img, timeout=120, context=ctx) as r2:
                raw_img = r2.read()
            try:
                img = Image.open(BytesIO(raw_img))
                img = img.convert("RGB")
                out_png.parent.mkdir(parents=True, exist_ok=True)
                buf = BytesIO()
                img.save(buf, format="PNG")
                out_png.write_bytes(buf.getvalue())
                return
            except OSError as e:
                raise RuntimeError("百炼文生图返回内容无法解析为图像。") from e
        time.sleep(_T2I_POLL_INTERVAL)
    raise TimeoutError(
        f"百炼文生图轮询超时（{_T2I_POLL_TIMEOUT}s），task_id={task_id}，最后状态：{status}"
    )


def _fetch_illustration_png_from_prompt(
    full_prompt: str,
    scene_index: int,
    *,
    style: str = "real",
    subject_reference: list[dict[str, str]] | None = None,
    negative_prompt: str = "",
) -> tuple[bytes, str]:
    """固定使用百炼文生图。返回 (png_bytes, src_url)。"""
    backend = _illustration_backend()
    logger.info("配图后端：%s", backend)
    if subject_reference:
        logger.warning(
            "当前配图后端为百炼，不支持 subject_reference，已忽略 %d 张定妆参考图。",
            len(subject_reference),
        )
    return _fetch_bailian_t2i_png_from_prompt(full_prompt, scene_index, style=style, negative_prompt=negative_prompt)


def download_illustration_from_prompt(
    image_prompt: str,
    mood_seed_text: str,
    out_png: Path,
    scene_index: int,
    delay_s: float = 1.5,
    *,
    strict_illustration: bool = False,
    style: str = "real",
    subject_reference_paths: Sequence[Path] | None = None,
    negative_prompt: str = "",
) -> bool:
    """
    与 download_illustration_png 相同，但使用已拼好的文生图 prompt。
    subject_reference_paths：本地定妆 PNG 路径列表；当前百炼后端会忽略该参数。
    返回：联网文生图成功并已写入 out_png 为 True；降级为色场/Picsum 等为 False。
    同时将图片源 URL 写入 out_png.with_suffix('.src_url')，供 R2V 直接引用。
    """
    if delay_s > 0 and scene_index > 0:
        time.sleep(delay_s)
    logger.info("镜头 %s：尝试生成配图（联网）…", scene_index + 1)
    subject_reference: list[dict[str, str]] | None = None
    if subject_reference_paths:
        n = sum(1 for p in subject_reference_paths if p.is_file())
        if n > 0:
            logger.info("镜头 %s：检测到 %d 张定妆参考图（百炼配图阶段将忽略）。", scene_index + 1, n)
    try:
        data, src_url = _fetch_illustration_png_from_prompt(
            image_prompt, scene_index, style=style, subject_reference=subject_reference,
            negative_prompt=negative_prompt,
        )
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(data)
        # 伴随文件：存 OSS 源 URL，供 R2V 直接引用（避免重复上传）
        out_png.with_suffix(".src_url").write_text(src_url, encoding="utf-8")
        logger.info("镜头 %s：已获取配图。", scene_index + 1)
        return True
    except RuntimeError as e:
        raise RuntimeError(f"镜头 {scene_index + 1} 在线配图失败：{e}") from e


def download_illustration_png(
    scene_text: str,
    out_png: Path,
    scene_index: int,
    delay_s: float = 1.5,
    *,
    strict_illustration: bool = False,
    style: str = "real",
) -> None:
    """
    固定走百炼 T2I 文生图；失败直接报错。
    """
    if delay_s > 0 and scene_index > 0:
        time.sleep(delay_s)
    logger.info("场景 %s：尝试生成剧情配图（联网）…", scene_index + 1)
    try:
        data, src_url = _fetch_illustration_png_from_prompt(
            scene_to_visual_prompt(scene_text, style=style), scene_index, style=style
        )
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(data)
        out_png.with_suffix(".src_url").write_text(src_url, encoding="utf-8")
        logger.info("场景 %s：已获取 AI 配图。", scene_index + 1)
    except RuntimeError as e:
        raise RuntimeError(f"场景 {scene_index + 1} 在线配图失败：{e}") from e
