"""阿里云百炼视频生成：T2V / L2V 创建任务 -> 轮询 -> 下载。

L2V 默认模型为 wan2.2-i2v-flash，可通过环境变量 BAILIAN_WAN_L2V_MODEL 或 CLI --l2v-model 切换（如 wan2.6-i2v-flash、wan2.7-i2v）。
wan2.7-i2v 使用百炼新版协议（input.media + first_frame），由本模块自动分支。

单次成片内每一镜（或 T2V 整段）在限流/5xx/网络/轮询超时/任务执行失败时可按 TOVEDIO_BAILIAN_VIDEO_MAX_ATTEMPTS 自动重试；
内容审核（DataInspectionFailed）与 4xx 参数/鉴权类错误不重试，避免无效计费。
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .minimax_client import _ensure_dotenv_loaded

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
_DEFAULT_T2V_MODEL = "wan2.6-t2v"
_DEFAULT_L2V_MODEL = "wan2.6-i2v-flash"
_POLL_INTERVAL_SEC = 15.0
_POLL_TIMEOUT_SEC = 900.0


def _max_bailian_video_attempts() -> int:
    raw = (os.environ.get("TOVEDIO_BAILIAN_VIDEO_MAX_ATTEMPTS") or "3").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 3
    return max(1, min(10, n))


def _bailian_retry_delay_sec(attempt_index: int) -> float:
    """第 attempt_index 次重试前等待（0=首次重试前），指数退避，上限 60s。"""
    raw = (os.environ.get("TOVEDIO_BAILIAN_RETRY_DELAY_SEC") or "2").strip()
    try:
        base = float(raw)
    except ValueError:
        base = 2.0
    return min(60.0, base * (2**attempt_index))


def _is_retryable_bailian_error(exc: BaseException) -> bool:
    """内容审核、鉴权、参数类错误不重试；限流、5xx、网络、轮询超时、任务执行失败可重试。"""
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, OSError):
        return True
    if not isinstance(exc, RuntimeError):
        return False
    msg = str(exc)
    if "duration customization is not supported" in msg.lower():
        return False
    if "DataInspectionFailed" in msg or "inappropriate content" in msg.lower():
        return False
    if "InvalidApiKey" in msg or "No API-key" in msg or "No API key" in msg:
        return False
    if any(x in msg for x in ("HTTP 401", "HTTP 403", "HTTP 404")):
        return False
    if "HTTP 400" in msg:
        return False
    if "HTTP 429" in msg:
        return True
    for code in (500, 502, 503, 504):
        if f"HTTP {code}" in msg:
            return True
    if "网络错误" in msg:
        return True
    if "百炼 Wan2.6 任务失败" in msg or "百炼 Wan2.6 成功但无视频 URL" in msg:
        return True
    if "轮询超时" in msg:
        return True
    if "查询 HTTP" in msg or "创建任务 HTTP" in msg:
        return False
    return False


def _bailian_video_with_retries(label: str, once: Callable[[], None]) -> None:
    max_att = _max_bailian_video_attempts()
    last_exc: BaseException | None = None
    for attempt in range(max_att):
        try:
            if attempt > 0:
                assert last_exc is not None
                if not _is_retryable_bailian_error(last_exc):
                    raise last_exc
                delay = _bailian_retry_delay_sec(attempt - 1)
                logger.warning(
                    "百炼 %s 第 %d/%d 次尝试失败，%.1fs 后重试：%s",
                    label,
                    attempt,
                    max_att,
                    delay,
                    last_exc,
                )
                time.sleep(delay)
            once()
            if attempt > 0:
                logger.info("百炼 %s 第 %d 次尝试成功", label, attempt + 1)
            return
        except (OSError, TimeoutError, RuntimeError) as e:
            last_exc = e
            if attempt + 1 >= max_att or not _is_retryable_bailian_error(e):
                raise
    assert last_exc is not None
    raise last_exc


def _api_key() -> str | None:
    _ensure_dotenv_loaded()
    key = (os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if key in ("", "your_key_here", "你的密钥"):
        return None
    return key or None


def _base_url() -> str:
    return (os.environ.get("DASHSCOPE_BASE_URL") or _DEFAULT_BASE_URL).strip().rstrip("/")


def _png_to_data_url(png_path: Path) -> str:
    import base64
    raw = png_path.read_bytes()
    return "data:image/png;base64," + base64.standard_b64encode(raw).decode("ascii")


def _get_image_url_for_r2v(png_path: Path) -> str:
    """
    获取 PNG 的可公网访问 URL，供 R2V reference_urls 使用。
    优先读取伴随文件 .src_url（由 download_illustration_from_prompt 在生成时写入的 OSS URL）；
    若不存在则上传至百炼文件服务并返回 fileid:// 形式（注：wan2.6-r2v 目前不支持 fileid，
    此时会报错提示用户重新生成定妆照以写入 .src_url）。
    """
    src_url_file = png_path.with_suffix(".src_url")
    if src_url_file.is_file():
        url = src_url_file.read_text(encoding="utf-8").strip()
        if url.startswith("http"):
            logger.debug("R2V 参考图 URL（.src_url）：%s → %s", png_path.name, url[:60])
            return url
    # 降级：上传文件拿 fileid（部分模型可能不支持）
    logger.warning(
        "未找到 %s 的伴随 .src_url，将上传文件。建议重新生成定妆照以写入 .src_url。",
        png_path.name,
    )
    return _upload_image_to_bailian(png_path)
    """
    将本地 PNG 上传至百炼文件服务，返回可公网访问的 https:// URL。
    接口：POST /compatible-mode/v1/files（OpenAI 兼容端点），不计费。
    """
    key = _api_key()
    if not key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法上传图片。")
    upload_url = f"{_base_url()}/compatible-mode/v1/files"
    raw = png_path.read_bytes()
    boundary = "----BailianUploadBoundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="purpose"\r\n\r\n'
        f"file-extract\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{png_path.name}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode("utf-8") + raw + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        upload_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        raise RuntimeError(f"百炼文件上传 HTTP {e.code}：{err_body or e.reason}") from e
    url = result.get("url") or (result.get("data") or {}).get("url")
    if not url:
        # DashScope 文件 API 返回 file_id，引用格式为 fileid://
        file_id = result.get("id") or (result.get("data") or {}).get("id")
        if file_id:
            url = f"fileid://{file_id}"
        else:
            raise RuntimeError(f"百炼文件上传未返回 URL 或 file_id：{str(result)[:400]}")
    logger.debug("百炼文件上传成功：%s → %s", png_path.name, url)
    return str(url)


def _model_accepts_duration_parameter(model: str) -> bool:
    """
    wan2.2-i2v 系列不支持 parameters.duration，传入会异步任务失败：
    duration customization is not supported
    wan2.6 及以上均支持。
    """
    m = model.strip()
    return not m.startswith("wan2.2-i2v")


def _create_task(
    prompt: str,
    *,
    image_path: Path | None,
    model: str,
    duration_sec: int | None,
    mode_label: str,
) -> str:
    key = _api_key()
    if not key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法调用阿里云百炼 Wan2.6 视频生成。")
    m = model.strip()
    wan27_i2v = m.startswith("wan2.7-i2v")
    url = f"{_base_url()}/api/v1/services/aigc/video-generation/video-synthesis"
    payload: dict = {
        "model": m,
        "input": {"prompt": prompt.strip()},
        "parameters": {},
    }
    if image_path is not None:
        data_url = _png_to_data_url(image_path)
        if wan27_i2v:
            # 万相 2.7 图生视频仅支持新版协议（与 img_url 旧字段不兼容）
            payload["input"]["media"] = [{"type": "first_frame", "url": data_url}]
        else:
            payload["input"]["img_url"] = data_url
            payload["input"]["image_url"] = data_url
    if (
        duration_sec is not None
        and duration_sec > 0
        and _model_accepts_duration_parameter(m)
    ):
        dur_cap = 15 if wan27_i2v else 10
        payload["parameters"]["duration"] = int(min(duration_sec, dur_cap))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            pass
        raise RuntimeError(f"百炼 Wan2.6 {mode_label} 创建任务 HTTP {e.code}：{err_body or e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"百炼 Wan2.6 {mode_label} 网络错误：{e}") from e

    data = json.loads(raw)
    task_id = (
        data.get("output", {}).get("task_id")
        or data.get("task_id")
        or data.get("id")
    )
    if not task_id:
        raise RuntimeError(f"百炼 Wan2.6 {mode_label} 响应无 task_id：{str(data)[:800]}")
    logger.info("百炼 Wan2.6 %s 任务已创建：task_id=%s model=%s", mode_label, task_id, m)
    return str(task_id)


def resolved_t2v_model() -> str:
    """当前生效的百炼文生视频模型（与 create_t2v_task 解析规则一致）。"""
    return (os.environ.get("BAILIAN_WAN_T2V_MODEL") or _DEFAULT_T2V_MODEL).strip()


def create_t2v_task(prompt: str, *, model: str | None = None, duration_sec: int | None = None) -> str:
    m = (model or os.environ.get("BAILIAN_WAN_T2V_MODEL") or _DEFAULT_T2V_MODEL).strip()
    return _create_task(
        prompt,
        image_path=None,
        model=m,
        duration_sec=duration_sec,
        mode_label="T2V",
    )


def resolved_l2v_model() -> str:
    """当前生效的百炼图生视频模型（供日志；与 create_l2v_task 解析规则一致）。"""
    return (os.environ.get("BAILIAN_WAN_L2V_MODEL") or _DEFAULT_L2V_MODEL).strip()


def l2v_duration_cap(*, model: str | None = None) -> int:
    """与 create_l2v_task 中 duration 上限一致，供 pipeline 截断镜头时长提示。"""
    m = (model or resolved_l2v_model()).strip()
    return 15 if m.startswith("wan2.7-i2v") else 10


def create_l2v_task(
    first_frame_png: Path,
    prompt: str,
    *,
    model: str | None = None,
    duration_sec: int | None = None,
) -> str:
    m = (model or resolved_l2v_model()).strip()
    return _create_task(
        prompt,
        image_path=first_frame_png,
        model=m,
        duration_sec=duration_sec,
        mode_label="L2V",
    )


def query_task(task_id: str) -> dict:
    key = _api_key()
    if not key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY。")
    url = f"{_base_url()}/api/v1/tasks/{urllib.parse.quote(task_id)}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            pass
        raise RuntimeError(f"百炼 Wan2.6 查询 HTTP {e.code}：{err_body or e.reason}") from e


def poll_until_done(task_id: str, *, timeout_sec: float | None = None, label: str = "") -> str:
    """轮询至成功，返回视频下载 URL。每 15s 打印一次进度日志。"""
    deadline = time.monotonic() + (timeout_sec if timeout_sec is not None else _POLL_TIMEOUT_SEC)
    start = time.monotonic()
    last_log = time.monotonic() - _POLL_INTERVAL_SEC  # 首轮立即打印一次
    last_status = ""
    while time.monotonic() < deadline:
        data = query_task(task_id)
        output = data.get("output") or {}
        task_status = (output.get("task_status") or output.get("status") or "").strip().upper()
        last_status = task_status
        now = time.monotonic()
        elapsed = now - start
        if now - last_log >= _POLL_INTERVAL_SEC:
            tag = f" [{label}]" if label else ""
            logger.info(
                "百炼任务 %s%s 状态：%s，已等待 %.0fs",
                task_id[:12], tag, task_status or "PENDING", elapsed,
            )
            last_log = now
        if task_status in ("FAILED", "CANCELED", "CANCELLED"):
            msg = output.get("message") or data.get("message") or str(data)[:300]
            raise RuntimeError(f"百炼 Wan2.6 任务失败：{msg}")
        if task_status in ("SUCCEEDED", "SUCCESS"):
            video_url = (
                output.get("video_url")
                or output.get("video_url_list", [None])[0]
                or output.get("results", [{}])[0].get("url")
            )
            if video_url:
                logger.info(
                    "百炼任务 %s%s 完成，耗时 %.0fs",
                    task_id[:12], f" [{label}]" if label else "", elapsed,
                )
                return str(video_url)
            raise RuntimeError(f"百炼 Wan2.6 成功但无视频 URL：{str(data)[:500]}")
        time.sleep(_POLL_INTERVAL_SEC)
    raise TimeoutError(f"百炼 Wan2.6 轮询超时（{timeout_sec or _POLL_TIMEOUT_SEC}s），最后状态：{last_status}")


def download_to_file(url: str, out_path: Path) -> None:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=300) as resp:
        out_path.write_bytes(resp.read())
    logger.info("已下载百炼 Wan2.6 视频：%s（%d 字节）", out_path, out_path.stat().st_size)


_DEFAULT_R2V_MODEL = "wan2.6-r2v"


def resolved_r2v_model() -> str:
    return (os.environ.get("BAILIAN_WAN_R2V_MODEL") or _DEFAULT_R2V_MODEL).strip()


def _create_r2v_task(
    ref_image_paths: list[Path],
    prompt: str,
    *,
    duration_sec: int | None = None,
    model: str | None = None,
) -> str:
    """提交 wan2.6-r2v 参考生视频异步任务，返回 task_id。

    ref_image_paths: 1-5 张定妆/参考图 PNG，在 prompt 中以 Image1、Image2… 引用角色。
    """
    if not ref_image_paths:
        raise ValueError("R2V 需要至少 1 张参考图。")
    key = _api_key()
    if not key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，无法调用百炼 R2V。")
    m = (model or resolved_r2v_model()).strip()
    url = f"{_base_url()}/api/v1/services/aigc/video-generation/video-synthesis"
    logger.info("获取 %d 张定妆参考图 URL…", len(ref_image_paths[:5]))
    reference_urls = [_get_image_url_for_r2v(p) for p in ref_image_paths[:5]]
    payload: dict = {
        "model": m,
        "input": {
            "prompt": prompt.strip(),
            "reference_urls": reference_urls,
        },
        "parameters": {},
    }
    if duration_sec is not None and duration_sec > 0:
        payload["parameters"]["duration"] = int(min(duration_sec, 10))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            pass
        raise RuntimeError(f"百炼 R2V 创建任务 HTTP {e.code}：{err_body or e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"百炼 R2V 网络错误：{e}") from e
    data = json.loads(raw)
    task_id = (
        data.get("output", {}).get("task_id")
        or data.get("task_id")
        or data.get("id")
    )
    if not task_id:
        raise RuntimeError(f"百炼 R2V 响应无 task_id：{str(data)[:800]}")
    logger.info("百炼 R2V 任务已创建：task_id=%s model=%s refs=%d", task_id, m, len(ref_image_paths))
    return str(task_id)


def submit_r2v_task(
    ref_image_paths: list[Path],
    prompt: str,
    *,
    model: str | None = None,
    duration_sec: int | None = None,
) -> str:
    """提交 R2V 异步任务，立即返回 task_id（不阻塞等待结果）。"""
    for p in ref_image_paths:
        if not p.is_file():
            raise ValueError(f"R2V 参考图不存在：{p}")
    return _create_r2v_task(ref_image_paths, prompt, duration_sec=duration_sec, model=model)


def submit_l2v_task(
    first_frame_png: Path,
    prompt: str,
    *,
    model: str | None = None,
    duration_sec: int | None = None,
) -> str:
    """提交 L2V 异步任务，立即返回 task_id（不阻塞等待结果）。"""
    if not first_frame_png.is_file():
        raise ValueError(f"L2V 首帧图不存在：{first_frame_png}")
    m = (model or resolved_l2v_model()).strip()
    return _create_task(prompt, image_path=first_frame_png, model=m, duration_sec=duration_sec, mode_label="L2V")


def poll_video_task_to_file(task_id: str, out_mp4: Path, *, label: str = "") -> None:
    """轮询已提交的视频任务至完成，下载结果写入 out_mp4。"""
    video_url = poll_until_done(task_id, label=label)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    download_to_file(video_url, out_mp4)


def run_t2v_to_mp4(prompt: str, out_mp4: Path, *, duration_hint_sec: int | None = None, model: str | None = None, label: str = "") -> Path:
    if not prompt.strip():
        raise ValueError("文生视频 prompt 为空。")

    def _once() -> None:
        task_id = create_t2v_task(prompt, model=model, duration_sec=duration_hint_sec)
        video_url = poll_until_done(task_id, label=label or "T2V")
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        download_to_file(video_url, out_mp4)

    _bailian_video_with_retries("T2V", _once)
    return out_mp4


def run_l2v_to_mp4(
    first_frame_png: Path,
    prompt: str,
    out_mp4: Path,
    *,
    duration_hint_sec: int | None = None,
    model: str | None = None,
    label: str = "",
) -> Path:
    if not first_frame_png.is_file():
        raise ValueError(f"L2V 首帧图不存在：{first_frame_png}")

    def _once() -> None:
        task_id = create_l2v_task(first_frame_png, prompt, model=model, duration_sec=duration_hint_sec)
        video_url = poll_until_done(task_id, label=label or "L2V")
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        download_to_file(video_url, out_mp4)

    _bailian_video_with_retries("L2V", _once)
    return out_mp4

