# toVedio 优化技术方案

> 基于阿里云百炼官方 API 文档研究（2026-04）与当前代码分析
>
> 文档覆盖：wan2.6-t2i / wan2.6-i2v-flash / wan2.7-i2v / wan2.6-r2v / wan2.1-vace-plus

---

## 一、并发生成优化（P1 最高优先级）

### 现状

当前流水线为**完全串行**：镜1 T2I → 镜1 I2V → 镜2 T2I → 镜2 I2V → …

6 镜最坏情况 = `6 × (T2I 等待 + I2V 等待)` ≈ 6 × (20s + 120s) ≈ **14 分钟**

### 百炼 API 并发能力（来自官方文档）

| 模型 | 地区 | 并发任务上限 | 提交 RPS |
|---|---|---|---|
| `wan2.6-t2i` | 国际（新加坡） | **5** | 5 RPS |
| `wan2.6-t2i` | 中国大陆 | **5** | 1 RPS |
| `wan2.6-i2v-flash` | 国际 / 大陆 | **5** | 5 RPS |
| `wan2.7-i2v` | 北京 / 新加坡 | **5** | 5 RPS |
| `wan2.6-r2v` | 全区 | **5** | 5 RPS |

### 优化方案：两阶段并发提交

```
阶段一（T2I 并发）：批量提交所有镜头的文生图任务
                     ↓ 并发等待（轮询间隔 10s）
阶段二（I2V 并发）：每个镜头图片就绪后立即提交 I2V 任务
                     ↓ 并发等待（轮询间隔 15s）
阶段三（合并）：ffmpeg concat
```

**理论加速比**：串行 14 min → 并发 ≈ 3 min（5 并发下约 4-5x 提速）

### 实现要点

```python
# 伪代码：两阶段并发（基于现有同步轮询改造）
import concurrent.futures, time

# 阶段一：并发提交 T2I
t2i_task_ids = {}
for i, shot in enumerate(shots):
    task_id = create_t2i_task(shot.prompt)
    t2i_task_ids[i] = task_id
    time.sleep(0.2)  # 国际区 5 RPS，每 200ms 一个请求足够

# 并发等待 T2I（10s 间隔）
keyframes = {}
pending = set(t2i_task_ids.keys())
while pending:
    for i in list(pending):
        result = query_t2i_task(t2i_task_ids[i])
        if result["status"] == "SUCCEEDED":
            keyframes[i] = download_image(result["url"])
            pending.remove(i)
        elif result["status"] in ("FAILED", "CANCELED"):
            raise RuntimeError(f"镜头 {i} T2I 失败")
    if pending:
        time.sleep(10)

# 阶段二：T2I 完成即提交 I2V
i2v_task_ids = {}
for i, frame in keyframes.items():
    task_id = create_i2v_task(frame, shots[i].motion_prompt)
    i2v_task_ids[i] = task_id
    time.sleep(0.2)

# 并发等待 I2V（15s 间隔）
segments = {}
pending = set(i2v_task_ids.keys())
while pending:
    for i in list(pending):
        result = query_video_task(i2v_task_ids[i])
        if result["status"] == "SUCCEEDED":
            segments[i] = download_video(result["video_url"])
            pending.remove(i)
        elif result["status"] in ("FAILED", "CANCELED"):
            handle_failure(i)
    if pending:
        time.sleep(15)
```

### 注意事项

- 官方**查询接口 RPS 上限为 20**，6 镜并发轮询远低于此限制，安全
- 超出并发上限时返回 `Throttling.RateQuota`（HTTP 429），需退避重试
- **T2I 和 I2V 任务 URL 有效期仅 24 小时**，需在完成后立即下载到本地

---

## 二、角色一致性：使用 wan2.6-r2v（R2V 模型）

### 现状问题

当前 `--character-sheet-dir` 功能在文生图阶段**完全无效**（`illustration.py:270-275` 静默忽略）。百炼 T2I API 无 IP-Adapter 参数。

### 百炼官方角色一致性方案

#### 方案 A：`wan2.6-r2v`（Reference-to-Video，推荐）

直接从参考图生成视频，跳过 T2I 步骤，**角色外貌一致性由模型保证**。

```python
# 参考图在 prompt 中以 Image1、Image2 等编号引用
payload = {
    "model": "wan2.6-r2v",
    "input": {
        "prompt": "Image1 walks slowly through a snow-covered courtyard at dusk",
        "ref_images": [
            {"url": "https://oss.xxx/character_female.png"}  # 角色定妆图
        ]
    },
    "parameters": {
        "resolution": "720P",
        "duration": 5
    }
}
```

**特性**：
- 支持 1-5 张参考图（角色/场景均可）
- 多角色：`"Image1 and Image2 sit across from each other"`
- 并发上限 5，与 i2v 相同
- 省去 T2I 步骤，直接 T2V with reference

#### 方案 B：`wan2.1-vace-plus`（VACE image_reference 函数）

适用于需要同时控制前景主体与背景的场景。

```python
payload = {
    "model": "wan2.1-vace-plus",
    "input": {
        "function": "image_reference",
        "prompt": "人物走在雪中庭院，衣袂飘动",
        "ref_images_url": [
            "https://oss.xxx/character.png",   # obj：人物
            "https://oss.xxx/courtyard.png"    # bg：背景场景
        ],
        "obj_or_bg": ["obj", "bg"]  # 必须与 ref_images_url 等长
    }
}
```

**VACE ref_images_url 约束**：
- 最多 3 张
- 分辨率 360–2000 px
- 单张 ≤10 MB
- 格式：JPEG/PNG/BMP/TIFF/WEBP

#### 方案 C：`wan2.7-i2v` 首尾帧（已支持，加强一致性）

当前代码已支持 `wan2.7-i2v`，其 `first_frame + last_frame` 模式可约束头尾帧角色外貌：

```python
# wan2.7-i2v 新协议：首帧 + 尾帧控制
payload["input"]["media"] = [
    {"type": "first_frame", "url": character_frame_url},
    {"type": "last_frame",  "url": next_scene_first_frame_url}
]
```

### 推荐决策树

```
有定妆图？
  ├── 是 → 用 wan2.6-r2v（直接生成视频，一致性最佳）
  │         └── 失败/不支持 → 降级到 wan2.7-i2v + 首尾帧
  └── 否 → 继续现有 T2I + I2V 链路
```

---

## 三、wan2.7-i2v 升级与首尾帧利用

### 现状

当前默认 L2V 模型为 `wan2.2-i2v-flash`（不支持 duration 参数），`wan2.7-i2v` 已有代码分支但未默认启用。

### wan2.7 vs wan2.6 对比

| 特性 | wan2.2-i2v-flash | wan2.6-i2v-flash | wan2.7-i2v |
|---|---|---|---|
| duration 参数 | **不支持** | 2-15s | 2-15s |
| 最长时长 | 固定（约 5s） | 15s | 15s |
| 分辨率 | 720P default | 1080P default | 1080P default |
| 首+尾帧 | 否 | 否 | **是** |
| 视频续拍 | 否 | 否 | **是** |
| 音频驱动 | 否 | 是 | 是 |
| API 协议 | 旧（img_url） | 旧（img_url） | **新（media 数组）** |
| 并发上限 | 2 | 5 | 5 |

### 推荐升级方案

1. **默认 L2V 模型改为 `wan2.6-i2v-flash`**（从 wan2.2 升级）
   - 并发上限从 2 → 5（与并发优化协同）
   - 支持 duration 参数，解决当前 `duration customization is not supported` 问题

2. **高质量模式使用 `wan2.7-i2v`**，启用首尾帧约束
   - 当前 `_create_task` 已正确实现 wan2.7 的 `media` 数组协议
   - 建议增加环境变量 `TOVEDIO_L2V_LAST_FRAME=1` 控制是否传入尾帧

3. **wan2.7-i2v 视频续拍**（用于 L2V 链式的替代方案）

```python
# 当前：提取尾帧 PNG → 作为下一镜首帧
# 升级：直接把前一镜 MP4 作为 first_clip 传入 wan2.7-i2v
payload["input"]["media"] = [
    {"type": "first_clip", "url": prev_segment_url}  # 前一镜视频
]
# 优势：无 PNG 压缩损失，视觉过渡更流畅
# 约束：first_clip 必须 2-10s，≤100 MB
```

---

## 四、T2I 图像生成优化：`wan2.6-image` 多参考图模式

### 现状

`illustration.py` 使用 `wan2.6-t2i`（纯文生图），无法传入参考图。

### `wan2.6-image` 多参考图

百炼 `wan2.6-image` 模型支持在 `messages.content` 数组中混合图文，通过 prompt 中的自然语言描述（"参考图1的风格"、"图2的主体"）控制参考意图。

```python
# 文生图时带风格参考（如既定世界观场景图）
message_content = [
    {"text": f"参考图1的风格与光线氛围，生成：{scene_prompt}"},
    {"image": style_reference_url}  # 本片已有场景图/定妆图作为风格锚定
]

body = {
    "model": "wan2.6-image",
    "input": {
        "messages": [{"role": "user", "content": message_content}]
    },
    "parameters": {
        "n": 1,
        "size": "1280*720",
        "watermark": False,
        "prompt_extend": True
    }
}
```

**约束**：
- 参考图 240–8000 px，≤10 MB，JPEG/PNG/BMP/WEBP（不含透明通道）
- 最多 4 张参考图（wan2.6-image），wan2.7-image 最多 9 张
- 图片需为公网可访问 URL（或 base64），不支持本地路径直接传入
- 需提前将本地 PNG 上传 OSS 或通过 base64 内嵌

### 本地文件传参

当前 `video_t2v_bailian_kling.py` 已实现 `_png_to_data_url`（base64 data URL），T2I 同样可复用此方案：

```python
def _png_to_data_url(png_path: Path) -> str:
    raw = png_path.read_bytes()
    return "data:image/png;base64," + base64.standard_b64encode(raw).decode("ascii")

# 传入 content 中的 image 字段使用 base64 data URL
{"image": _png_to_data_url(costume_sheet_path)}
```

---

## 五、T2I 异步化（消除 300s 同步超时风险）

### 现状问题

`illustration.py:205`：`urllib.request.urlopen(req, timeout=300)` — T2I 以**同步方式**等待，没有任务 ID，无法续传。

### 官方推荐的 T2I 异步模式

```
POST /api/v1/services/aigc/multimodal-generation/generation
Header: X-DashScope-Async: enable
→ 返回 task_id（立即返回）

GET /api/v1/tasks/{task_id}
→ 轮询，间隔 10s
→ SUCCEEDED 时取 output.choices[0].message.content[].image
```

### 改造方案

```python
def _submit_t2i_task(prompt: str, ...) -> str:
    """提交 T2I 异步任务，返回 task_id。"""
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            ...,
            "X-DashScope-Async": "enable"  # 关键：启用异步
        }
    )
    data = json.loads(urlopen(req).read())
    return data["output"]["task_id"]

def _poll_t2i_task(task_id: str, timeout: float = 120) -> str:
    """轮询至 SUCCEEDED，返回图片 URL。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = query_task(task_id)
        status = result["output"]["task_status"]
        if status == "SUCCEEDED":
            # 解析图片 URL（三条兼容分支可复用现有代码）
            return extract_image_url(result)
        elif status in ("FAILED", "CANCELED"):
            raise RuntimeError(...)
        time.sleep(10)
    raise TimeoutError(...)
```

**优势**：
- 异步 + 并发等待（与第一节并发方案结合）
- 任务 ID 可持久化，重启后可续查
- 彻底消除 300s 单点阻塞

---

## 六、轮询进度反馈

### 现状

`poll_until_done` 每 2s 静默轮询，无任何用户可见输出，90 分钟内用户无法判断状态。

### 改进方案

```python
def poll_until_done(task_id: str, *, label: str = "", timeout_sec: float = 900.0) -> str:
    deadline = time.monotonic() + timeout_sec
    start = time.monotonic()
    last_log = 0.0

    while time.monotonic() < deadline:
        data = query_task(task_id)
        status = (data.get("output", {}).get("task_status") or "").upper()

        now = time.monotonic()
        elapsed = now - start
        # 每 15s 打印一次进度（建议改为 15s 轮询间隔，与官方推荐一致）
        if now - last_log >= 15.0:
            logger.info(
                "百炼任务 %s [%s] 状态：%s，已等待 %.0fs",
                task_id[:12], label, status, elapsed
            )
            last_log = now

        if status == "SUCCEEDED":
            return extract_video_url(data)
        elif status in ("FAILED", "CANCELED"):
            raise RuntimeError(...)

        # 官方推荐：视频任务 15s 间隔（当前为 2s，查询 QPS 浪费）
        time.sleep(15)

    raise TimeoutError(...)
```

**额外改进**：
- 将轮询间隔从 2s 改为视频 15s、图片 10s（与官方推荐一致）
- 减少无效查询，降低触发查询 RPS 限制（20 RPS）的风险
- 对批量并发场景（5 任务同时轮询）尤其重要：2s × 5 = 2.5 QPS，安全；但有冗余

---

## 七、镜头压缩告警与用户确认

### 现状

`pipeline.py:1568` 超出 `TOVEDIO_MAX_SHOTS`（默认 6）时静默丢弃镜头，仅 INFO 日志。

### 改进方案

```python
def _compress_shots_for_story(shots: list[dict], max_shots: int) -> list[dict]:
    if len(shots) <= max_shots:
        return shots

    # 均匀抽样
    indices = [round(i * (len(shots) - 1) / (max_shots - 1)) for i in range(max_shots)]
    kept = [shots[i] for i in indices]
    dropped_indices = sorted(set(range(len(shots))) - set(indices))

    # 改为 WARNING 级别，列出被丢弃的镜头
    logger.warning(
        "分镜共 %d 镜，超出上限 %d，已压缩至 %d 镜。"
        "被丢弃的镜头索引：%s（第 %s 镜）。"
        "如需保留全部镜头，请设置 TOVEDIO_MAX_SHOTS=%d 或 --max-shots %d。",
        len(shots), max_shots, len(kept),
        dropped_indices,
        [shots[i].get("scene", "")[:20] for i in dropped_indices],
        len(shots), len(shots)
    )
    return kept
```

---

## 八、ffmpeg xfade 大片段分批合并

### 现状

`pipeline.py:833-843`：N 镜时构建 N-1 级 xfade 滤镜链，单条命令。8+ 镜时命令行极长，可能触及限制。

### 改进方案：分批合并（二叉树归并）

```python
def merge_with_xfade(segments: list[Path], crossfade: float) -> Path:
    """超过 BATCH_SIZE 时分批二叉树合并，避免超长滤镜链。"""
    BATCH_SIZE = 6
    if len(segments) <= BATCH_SIZE:
        return _xfade_concat(segments, crossfade)  # 现有逻辑

    # 分批：每 BATCH_SIZE 个合并一次，递归处理
    batches = [segments[i:i+BATCH_SIZE] for i in range(0, len(segments), BATCH_SIZE)]
    intermediate = [_xfade_concat(batch, crossfade) for batch in batches]
    return merge_with_xfade(intermediate, crossfade)  # 递归合并中间结果
```

---

## 九、模型选型建议总结

| 用途 | 当前 | 推荐升级 | 原因 |
|---|---|---|---|
| 文生图 | `wan2.6-t2i` | 保持，或 `wan2.6-image`（有参考图时）| T2I 已是当前最新 2.6 系列 |
| 角色一致性文生图 | 无效（静默忽略） | `wan2.6-r2v` | 官方角色参考视频生成 |
| L2V 默认 | `wan2.2-i2v-flash` | **`wan2.6-i2v-flash`** | 支持 duration，并发 5，1080P |
| L2V 高质量 | `wan2.7-i2v`（可选） | 保持，加启用首尾帧 | 首尾帧约束角色一致性 |
| 链式延续 | 提取尾帧 PNG | `wan2.7-i2v` first_clip | 直接视频续拍，无压缩损失 |

---

## 十一、实施优先级

| 优先级 | 改进项 | 工作量 | 预期收益 |
|---|---|---|---|
| **P0** | 默认 L2V 模型从 `wan2.2` → `wan2.6-i2v-flash` | 1 行环境变量 | 解除 duration 限制，并发上限 5 |
| **P0** | `illustration.py` T2I 改为异步提交（加 `X-DashScope-Async: enable`） | 中 | 消除 300s 阻塞，支持并发等待 |
| **P1** | T2I + I2V 两阶段并发提交 + 轮询 | 大 | 4-5x 整体提速 |
| **P1** | 轮询间隔 2s → 15s（视频）/ 10s（图片） + 进度日志 | 小 | 减少无效请求，用户体验 |
| **P1** | 镜头压缩改 WARNING + 列出丢弃镜头 | 小 | 用户可感知内容损失 |
| **P2** | 有定妆图时切换 `wan2.6-r2v` | 大 | 真正实现角色一致性 |
| **P2** | `wan2.6-image` 多参考图模式（首帧带风格锚定） | 中 | 全片视觉风格统一 |
| **P2** | `wan2.7-i2v` first_clip 视频续拍替代尾帧 PNG | 中 | 镜头间视觉连续性提升 |
| **P3** | ffmpeg xfade 超 6 镜分批合并 | 小 | 长片段稳定性 |
