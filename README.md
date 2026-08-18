# notetransfer

把视频链接变成结构化笔记素材：**下载 → 转写 → 关键帧抽取 → 图文时间轴对齐 → 交给 AI 整理入库**。

支持抖音、小红书、YouTube、Bilibili、X 等平台（yt-dlp 覆盖 1800+ 站点，另有两级兜底通路）。

```
链接 ──▶ 采集（三级降级）──▶ 本地转写 ──▶ 抽帧+去重 ──▶ manifest.json ──▶ AI 写笔记 ──▶ 知识库
```

## 为什么不用现成方案

| 方案 | 平台 | 截图 | 直接入库 | 成本 |
|---|---|---|---|---|
| 商用同步服务 | 多 | 有 | 有 | 按条计费 |
| BiliNote | B站/抖音/YT | 有 | 需手动导出 | 自备 LLM Key |
| AI-Video-Transcriber | 30+ | 无 | 无 | 自备 LLM Key |
| **notetransfer** | yt-dlp 全覆盖 + 兜底 | 有（含时间轴对齐） | 有 | 无需 LLM Key |

核心差异：本项目只产出**素材与对齐信息**，"理解与整理"交给调用方的 AI Agent。Agent 是多模态的，能真正看图并按转写上下文配图，质量上限高于固定模板；同时不需要额外配置任何 LLM API。

## 安装

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # 仅在需要浏览器兜底通路时
brew install ffmpeg                     # macOS；Linux 用 apt install ffmpeg
```

## 使用

```bash
# 直接丢链接，或丢一整段带口令的分享文本
python -m notetransfer.cli "9.23 复制打开抖音 https://v.douyin.com/XXXX/ :7pm"

# 常用选项
python -m notetransfer.cli <url> \
  --out /tmp/work --model medium --max-frames 12 --keep-video

# 平台全部失效时的人工兜底
python -m notetransfer.cli <url> --local-file ~/Downloads/video.mp4
```

成功时 stdout 输出 JSON，关键字段：

```json
{
  "status": "ok",
  "manifest": "/tmp/notetransfer_x/manifest.json",
  "frames": 12,
  "segments": 107,
  "warnings": []
}
```

`manifest.json` 中每一帧都带 `context` 字段——该时间点前后 15 秒的转写文本，供写笔记时判断这张图属于哪个章节：

```json
{
  "frames": [
    {"path": ".../b7643bd266_03_t112s.jpg", "timestamp": 112.0,
     "context": "第二步要切块，这一步非常关键。很多知识库不好用..."}
  ]
}
```

### 退出码

| 码 | 含义 | 处理 |
|---|---|---|
| 0 | 素材就绪（可能带 warnings） | 读 manifest 写笔记 |
| 1 | 未预期错误 | 看 stderr 的 run.stages |
| 2 | **质量闸门拒绝** | 不要写笔记，先修问题 |
| 3 | 该链接已处理过 | 加 `--force` 重跑 |

## 设计要点

**三级采集降级**：yt-dlp → 无头浏览器嗅探 → 人工提供文件。第三级不是可选项：平台持续对抗抓取，只依赖自家正则的流水线终将失效。

**质量闸门**：空转写、无音轨、无帧等情况直接以退出码 2 拒绝交付。最危险的失败不是报错，而是"看起来成功"——比如抓到无声视频流，最终产出一篇通顺但内容为空的笔记。

**图文时间轴对齐**：帧不只带时间戳，还带该时刻的转写上下文。没有这层关联，配图只能靠猜；在幻灯片式视频上碰巧有效，遇到穿插剪辑就会错配，而自信的错误图注比没有图更有害。

**逐帧精确重截**：候选帧按 dHash 去重后，用 `-ss <精确时间>` 逐帧重新截取，文件名与时间戳强绑定，不依赖 ffmpeg 日志与目录列表的顺序假设。

**命名空间隔离**：帧文件名带 URL 哈希前缀（`b7643bd266_03_t112s.jpg`），避免处理第二个视频时覆盖前一个视频已入库的截图。

**流式落盘**：大文件用 curl 分块写盘，不在内存缓冲整个视频。

**运行串行化**：文件锁保证同时只有一个转写任务，避免多实例抢 CPU。

## 使用边界与合规

本项目面向**个人学习与知识管理**场景。使用者需自行遵守：

- 仅处理自己有权访问的公开内容，**不做批量抓取**，不高频请求（建议每小时 ≤ 10 条）
- 生成的笔记**仅供个人存档**，不二次分发、不去除作者署名、不用于商业用途
- 遵守各平台服务条款与著作权法；浏览器兜底通路仅用于突破个人访问场景下的技术限制
- 若在企业环境规模化使用，请先完成合规评估

采集层默认保留原始链接与作者信息，便于溯源与署名。

## 项目结构

```
notetransfer/
├── notetransfer/
│   ├── common.py       结构化日志、运行锁、探测与哈希工具
│   ├── acquire.py      三级采集（yt-dlp / 浏览器嗅探 / 本地文件）
│   ├── transcribe.py   faster-whisper 转写 + 词表预热 + 上下文提取
│   ├── frames.py       场景检测、感知哈希去重、逐帧精确重截
│   └── cli.py          编排、质量闸门、幂等台账
├── docs/
│   ├── ARCHITECTURE.md 技术方案与踩坑记录
│   └── AUDIT.md        方案审计与修复对照表
└── tests/
```

## 许可

MIT（见 LICENSE）。第三方组件许可：yt-dlp (Unlicense)、faster-whisper (MIT)、Playwright (Apache-2.0)、ffmpeg (LGPL/GPL)。
