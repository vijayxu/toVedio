"""MiniMax 视频生成：I2V / T2V 创建任务 → 轮询 → 下载；本地缓存。"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .minimax_client import _ensure_dotenv_loaded, _get_api_key

logger = logging.getLogger(__name__)

_DEFAULT_I2V_MODEL = "MiniMax-Hailuo-2.3"
_DEFAULT_T2V_MODEL = "MiniMax-Hailuo-02"
_POLL_INTERVAL_SEC = 2.0
_POLL_TIMEOUT_SEC = 900.0


def _api_origin() -> str:
    explicit = (os.environ.get("MINIMAX_VIDEO_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    cn = (os.environ.get("MINIMAX_USE_CN") or "").strip().lower()
    if cn in ("1", "true", "yes", "minimaxi"):
        return "https://api.minimaxi.com"
    return "https://api.minimax.io"


def _group_query_suffix() -> str:
    gid = (os.environ.get("MINIMAX_GROUP_ID") or "").strip()
    if not gid:
        return ""
    return "?" + urllib.parse.urlencode({"GroupId": gid})


def _png_to_data_url(png_path: Path) -> str:
    raw = png_path.read_bytes()
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _cache_key_for_i2v(png_path: Path, prompt: str, model: str) -> str:
    h = hashlib.sha256()
    h.update(png_path.read_bytes())
    h.update(b"\0")
    h.update(prompt.encode("utf-8"))
    h.update(b"\0")
    h.update(model.encode("utf-8"))
    return h.hexdigest()[:24]


def cached_i2v_path(cache_dir: Path, png_path: Path, prompt: str, model: str) -> Path:
    return cache_dir / f"i2v_{_cache_key_for_i2v(png_path, prompt, model)}.mp4"


def _create_video_task(
    prompt: str,
    *,
    first_frame_png: Path | None = None,
    model: str | None = None,
    duration_sec: int | None = None,
    resolution: str | None = None,
    model_env_name: str = "MINIMAX_I2V_MODEL",
    default_model: str = _DEFAULT_I2V_MODEL,
    mode_label: str = "I2V",
) -> str:
    """提交视频生成任务，返回 task_id。"""
    _ensure_dotenv_loaded()
    key = _get_api_key()
    if not key:
        raise RuntimeError("未配置 MINIMAX_API_KEY，无法调用视频生成。")
    m = (model or os.environ.get(model_env_name) or default_model).strip()
    origin = _api_origin()
    url = f"{origin}/v1/video_generation{_group_query_suffix()}"
    payload: dict = {
        "model": m,
        "prompt": prompt.strip() or "subtle camera movement, cinematic",
    }
    if first_frame_png is not None:
        payload["first_frame_image"] = _png_to_data_url(first_frame_png)
    if duration_sec is not None and duration_sec > 0:
        payload["duration"] = int(min(duration_sec, 10))
    res = (resolution or os.environ.get("MINIMAX_I2V_RESOLUTION") or "").strip()
    if res:
        payload["resolution"] = res
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
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
        raise RuntimeError(f"{mode_label} 创建任务 HTTP {e.code}：{err_body or e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"{mode_label} 网络错误：{e}") from e

    data = json.loads(raw)
    base_resp = data.get("base_resp") or {}
    if base_resp.get("status_code") not in (0, None) and base_resp.get("status_code") != 0:
        msg = base_resp.get("status_msg") or str(data)
        raise RuntimeError(f"{mode_label} 创建失败：{msg}")

    task_id = data.get("task_id") or (data.get("data") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"{mode_label} 响应无 task_id：{str(data)[:800]}")
    logger.info("%s 任务已创建：task_id=%s model=%s", mode_label, task_id, m)
    return str(task_id)


def create_i2v_task(
    png_path: Path,
    prompt: str,
    *,
    model: str | None = None,
    duration_sec: int | None = None,
    resolution: str | None = None,
) -> str:
    """提交图生视频任务，返回 task_id。"""
    return _create_video_task(
        prompt,
        first_frame_png=png_path,
        model=model,
        duration_sec=duration_sec,
        resolution=resolution,
        model_env_name="MINIMAX_I2V_MODEL",
        default_model=_DEFAULT_I2V_MODEL,
        mode_label="I2V",
    )


def create_t2v_task(
    prompt: str,
    *,
    model: str | None = None,
    duration_sec: int | None = None,
    resolution: str | None = None,
) -> str:
    """提交文生视频任务，返回 task_id。"""
    return _create_video_task(
        prompt,
        first_frame_png=None,
        model=model,
        duration_sec=duration_sec,
        resolution=resolution,
        model_env_name="MINIMAX_T2V_MODEL",
        default_model=_DEFAULT_T2V_MODEL,
        mode_label="T2V",
    )


def query_i2v_task(task_id: str) -> dict:
    """查询任务状态；返回含 status、file_id 等字段的 dict。"""
    _ensure_dotenv_loaded()
    key = _get_api_key()
    if not key:
        raise RuntimeError("未配置 MINIMAX_API_KEY。")
    origin = _api_origin()
    q = urllib.parse.urlencode({"task_id": task_id})
    url = f"{origin}/v1/query/video_generation?{q}"
    if (os.environ.get("MINIMAX_GROUP_ID") or "").strip():
        url += "&" + urllib.parse.urlencode(
            {"GroupId": (os.environ.get("MINIMAX_GROUP_ID") or "").strip()}
        )
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            pass
        raise RuntimeError(f"I2V 查询 HTTP {e.code}：{err_body or e.reason}") from e
    return json.loads(raw)


def _status_is_failed(status: str | None) -> bool:
    if not status:
        return False
    s = status.strip().lower()
    return s in ("fail", "failed", "error", "cancelled", "canceled")


def poll_i2v_until_done(task_id: str, *, timeout_sec: float | None = None) -> str | int:
    """
    轮询直到成功（返回 file_id）或失败/超时。
    file_id 可能为 str 或 int，由下载接口消费。
    """
    deadline = time.monotonic() + (timeout_sec if timeout_sec is not None else _POLL_TIMEOUT_SEC)
    last_status = ""
    while time.monotonic() < deadline:
        data = query_i2v_task(task_id)
        base_resp = data.get("base_resp") or {}
        if base_resp.get("status_code") not in (0, None) and base_resp.get("status_code") != 0:
            msg = base_resp.get("status_msg") or str(data)
            raise RuntimeError(f"I2V 查询业务失败：{msg}")

        status = data.get("status") or (data.get("data") or {}).get("status")
        last_status = str(status or "")
        file_id = data.get("file_id")
        if file_id is None:
            file_id = (data.get("data") or {}).get("file_id")

        if _status_is_failed(str(status)):
            err = data.get("error") or base_resp.get("status_msg") or last_status
            raise RuntimeError(f"I2V 任务失败：{err}")

        if file_id is not None:
            st = str(status or "").strip().lower()
            if st in ("processing", "queueing", "preparing", "waiting", "running"):
                pass
            else:
                logger.info("I2V 任务成功：task_id=%s file_id=%s", task_id, file_id)
                return file_id

        time.sleep(_POLL_INTERVAL_SEC)

    raise TimeoutError(f"I2V 轮询超时（{timeout_sec or _POLL_TIMEOUT_SEC}s），最后状态：{last_status}")


def retrieve_file_download_url(file_id: str | int) -> str:
    """GET /v1/files/retrieve 取 download_url。"""
    _ensure_dotenv_loaded()
    key = _get_api_key()
    if not key:
        raise RuntimeError("未配置 MINIMAX_API_KEY。")
    origin = _api_origin()
    q = urllib.parse.urlencode({"file_id": str(file_id)})
    url = f"{origin}/v1/files/retrieve?{q}"
    if (os.environ.get("MINIMAX_GROUP_ID") or "").strip():
        url += "&" + urllib.parse.urlencode(
            {"GroupId": (os.environ.get("MINIMAX_GROUP_ID") or "").strip()}
        )
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:2000]
        except Exception:
            pass
        raise RuntimeError(f"I2V 文件查询 HTTP {e.code}：{err_body or e.reason}") from e
    data = json.loads(raw)
    base_resp = data.get("base_resp") or {}
    if base_resp.get("status_code") not in (0, None) and base_resp.get("status_code") != 0:
        msg = base_resp.get("status_msg") or str(data)
        raise RuntimeError(f"I2V 取文件失败：{msg}")
    file_obj = data.get("file") or data.get("data") or {}
    if isinstance(file_obj, dict):
        u = file_obj.get("download_url") or file_obj.get("url")
        if u:
            return str(u)
    u = data.get("download_url")
    if u:
        return str(u)
    raise RuntimeError(f"I2V 无下载 URL：{str(data)[:600]}")


def download_url_to_file(url: str, out_path: Path) -> None:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=300) as resp:
        out_path.write_bytes(resp.read())
    logger.info("已下载 I2V 视频：%s（%d 字节）", out_path, out_path.stat().st_size)


def run_i2v_to_mp4(
    png_path: Path,
    prompt: str,
    out_mp4: Path,
    work_dir: Path,
    *,
    model: str | None = None,
    duration_hint_sec: int | None = None,
    use_cache: bool = True,
) -> Path:
    """
    完整流程：缓存命中则直接复制；否则创建任务 → 轮询 → 下载。
    """
    _ensure_dotenv_loaded()
    m = (model or os.environ.get("MINIMAX_I2V_MODEL") or _DEFAULT_I2V_MODEL).strip()
    cache_dir = work_dir / "i2v_cache"
    if use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cached_i2v_path(cache_dir, png_path, prompt, m)
        if cached.is_file() and cached.stat().st_size > 1000:
            logger.info("I2V 使用本地缓存：%s", cached)
            out_mp4.parent.mkdir(parents=True, exist_ok=True)
            import shutil

            shutil.copy2(cached, out_mp4)
            return out_mp4

    task_id = create_i2v_task(
        png_path,
        prompt,
        model=m,
        duration_sec=duration_hint_sec,
    )
    file_id = poll_i2v_until_done(task_id)
    url = retrieve_file_download_url(file_id)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    download_url_to_file(url, out_mp4)

    if use_cache:
        try:
            import shutil

            shutil.copy2(out_mp4, cached_i2v_path(cache_dir, png_path, prompt, m))
        except OSError:
            pass
    return out_mp4


def run_t2v_to_mp4(
    prompt: str,
    out_mp4: Path,
    work_dir: Path,
    *,
    model: str | None = None,
    duration_hint_sec: int | None = None,
) -> Path:
    """完整流程：创建 T2V 任务 → 轮询 → 下载。"""
    _ensure_dotenv_loaded()
    task_id = create_t2v_task(
        prompt,
        model=model,
        duration_sec=duration_hint_sec,
    )
    file_id = poll_i2v_until_done(task_id)
    url = retrieve_file_download_url(file_id)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    download_url_to_file(url, out_mp4)
    return out_mp4
