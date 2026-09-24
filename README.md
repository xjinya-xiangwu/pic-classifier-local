# PhotoCurator 本地模型版 (pic-classifier-local)

把一批 500–1000 张服装拍摄照片按 **衣着 + 模特姿势 + 拍摄角度** 自动分组（背景/光线/表情不参与判断），逐组人工挑选 1 张保留，源文件夹内统一重命名（组名_序号，保留张加 `_精选`），全程可撤销。

**v2 与 API 版的区别：识别由跑在你 Mac 上的开源视觉模型完成（MLX），照片完全不出本机、零 API 费用、断网可用。** 云端 API 模式保留为可切换选项（[API 版仓库](https://github.com/xjinya-xiangwu/pic-classifier)）。

## 安装（macOS, 一条命令）

打开「终端」粘贴执行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/xjinya-xiangwu/pic-classifier-local/main/install_mac.sh)
```

> 若 `raw.githubusercontent.com` 打不开：浏览器打开仓库里的 `install_mac.sh` → 原始内容 → 另存为文件，终端执行 `bash ` 拖入该文件回车。

安装器**只装程序本体**（下载代码到 `~/PhotoCuratorLocal` + Python 依赖 + 生成 `/Applications/PhotoCurator Local.app` 并启动，之后启动台双击即可）。**模型不随安装器分发**，见下节。若弹出「命令行开发者工具」安装窗口，点**安装**等 2-5 分钟，脚本自动继续。

## 与 API 版（PhotoCurator.app）的区别与共存

| | **PhotoCurator Local**（本仓库） | PhotoCurator（API 版） |
| --- | --- | --- |
| 应用图标名 | **PhotoCurator Local** | PhotoCurator |
| 程序目录 | ~/PhotoCuratorLocal | ~/PhotoCurator |
| 浏览器地址 | **127.0.0.1:8776**（8776-8785 专用段） | 127.0.0.1:8765 |
| 页面标识 | 标题栏绿色 **Local** 字样 | 无 |

两个版本可同时安装、同时运行、互不干扰（数据目录 `~/.photocurator/` 共用——请勿同时在两版整理同一个文件夹）。**若两个入口打开的页面长得一样，以页面标题和端口为准**：带绿色 Local / 端口 8776+ 的是本地版。曾把本地版代码装进过 ~/PhotoCurator 的用户：重跑本安装命令后以 PhotoCurator Local 为准；要恢复纯 API 版请重跑 [pic-classifier 仓库](https://github.com/xjinya-xiangwu/pic-classifier) 的安装命令。

## 模型一键下载（App 内，首次 3.1GB）

1. 启动后打开 **设置 → 运行模式：本地模型**（默认已选中）；
2. 模型默认 **Qwen3-VL-4B-Instruct-4bit（3.1GB, MVP 档）** → 点 **下载所选**：
   - 程序自动安装本地推理依赖（mlx-vlm，约 300MB，仅 Apple Silicon 可装）；
   - 自动从国内镜像（hf-mirror，可切官方）下载权重，页内进度条，**中断后重下自动续传**，存于 `~/.photocurator/models/`（程序升级不受影响）；
3. 下载完成即可**完全断网使用**。识别时模型服务自动启动（首次加载约 1-3 分钟），处理完成后自动释放内存。

实测效果满意后可在设置中手动升级 8B（5.8GB）/ 30B-A3B（18.3GB，需 48GB+ 内存）；内存不足时程序会拒绝加载并提示（KR-L5）。

## 模型切换与性能对比

设置页的「本地模型」下拉选择其他**已下载**的档位时，下方会即时显示**切换前后对比**：单张识别耗时、按本项目剩余照片数折算的跑图时间、预估分组质量 F1、推理内存占本机内存的比例（超过 75% 安全阈值变红，KR-L5）、JSON 一次通过率与磁盘占用，并标注变化幅度。确认后点 **切换到此模型**：运行中的模型服务立即停止并释放内存，下次识别自动用新模型（已识别的照片不会重跑，只处理剩余未识别部分）。预估值来自 PRD v2 选型预算，真机实测（L1）后会校准。

## 手动下载模型（App 内下载失败时）

App 下载失败的报错会显示在设置页（完整 pip 日志在 `~/.photocurator/mlx_deps_pip.log`）。两种失败分别处理：

**情况 A：MLX 依赖安装失败**（报错含 `pip install -r requirements-mlx.txt`）。多半是 Python 版本过旧（macOS 自带 3.9 不满足 MLX 的 ≥3.10 要求）。处理：
1. 从 https://www.python.org/downloads/ 安装 Python 3.12+（或 `brew install python`）；
2. 终端执行 `rm -rf ~/PhotoCurator/.venv` 删掉旧环境；
3. 重新执行安装命令（会自动选用新版 Python 重建）。
想看 pip 的真实报错可手动执行：`~/PhotoCurator/.venv/bin/pip install -r ~/PhotoCurator/requirements-mlx.txt`

**情况 B：只差模型权重**。用浏览器手动下载后放入指定目录即可（App 会自动识别）：

1. 浏览器打开（默认镜像）：`https://hf-mirror.com/mlx-community/Qwen3-VL-4B-Instruct-4bit`（官方源把域名换成 `huggingface.co`）；
2. 点击「Files and versions」标签，下载页面中的**全部文件**到同一个文件夹：
   - 大文件：`model.safetensors`（3.1GB）、`tokenizer.json`（11MB）
   - 小文件：`config.json`、`generation_config.json`、`chat_template.jinja`、`chat_template.json`、`preprocessor_config.json`、`video_preprocessor_config.json`、`tokenizer_config.json`、`special_tokens_map.json`、`added_tokens.json`、`merges.txt`、`vocab.json`、`model.safetensors.index.json`
   （单个大文件的直链格式：`https://hf-mirror.com/mlx-community/Qwen3-VL-4B-Instruct-4bit/resolve/main/model.safetensors`）
3. 在 Finder 中按 `Cmd+Shift+G` 输入 `~/.photocurator/models/`，新建文件夹 `Qwen3-VL-4B-Instruct-4bit`，把下载的文件全部放进去；
4. 回到 App 设置页，模型应显示「已安装」，点「启动模型服务」或直接开始识别即可。

其他模型的 8B / 30B-A3B 手动下载同理（多分片模型需下载全部 `model-0000X-of-0000Y.safetensors` 分片）。

## 使用流程

1. 首页输入照片文件夹路径（Finder 里 `Cmd+Option+C` 复制路径）→ **扫描**（heic/heif/jpg/jpeg/png，可选含子文件夹）；
2. **开始识别**（本地串行处理，千张约 1-3 小时挂机；中断续跑、同内容哈希不重复推理）；
3. 左侧组列表逐组点选保留张（蓝框 = AI 推荐；键盘 `←→` 移动、`空格` 选中、`回车` 下一组）；
4. **完成验收 → 原地重命名** → 不满意点 **撤销上次重命名** 一键还原。

## 与 API 版的差异

| | 本地版 (v2) | API 版 (v1) |
| --- | --- | --- |
| 隐私 | 照片不出本机 | 压缩图发送云端 |
| 成本 | 0 | 千张约 ¥5-15 |
| 速度 | 千张 1-3 小时（挂机，无需盯着） | 约 20 分钟 |
| 首次准备 | 下载 3.1GB 模型 | 填 API Key |

## macOS 常见拦截与解决

| 现象 | 原因 | 解决 |
| --- | --- | --- |
| 「无法执行，因为没有正确的访问权限」 | 文件从 Windows/微信传来丢失可执行位 | 用上面安装命令；或 `chmod +x run.command` 后双击 |
| 「无法验证开发者 / 已损坏」 | macOS 隔离标记 | 系统设置 → 隐私与安全性 → 「仍要打开」；或 `xattr -dr com.apple.quarantine .` |
| 页面打开但按钮无反应 | 旧版前端缺陷（已修复）；新版若仍有问题会显示红色错误横幅 | 重新执行安装命令升级；仍异常请截图红色横幅反馈 |
| 「开始识别」后大量失败 | 本地模式：模型未下载/加载失败；API 模式：Key 与地址不配套等 | 点统计栏「失败 N 张 (点看原因)」查看报错，修正后重新识别（已失败照片自动重试） |
| 首次扫描询问「"python"想要访问文件夹」 | macOS 访问控制 | 点「允许」 |
| 报错含 `$'\r': command not found` | Windows 换行符污染 | `git clone` 本仓库，勿用微信传文件 |

## 数据位置

程序：`~/PhotoCurator/`；模型与数据：`~/.photocurator/`（模型 `models/`、索引与撤销记录、清单 CSV、服务日志）。源文件夹只发生**文件名变更**，不移动、不删除、不改内容。

## 测试

```
python test_smoke.py          # 全链路 (模拟标签): 扫描→分组→选片→重命名→撤销
python test_e2e_mock.py       # API 模式 e2e + 熔断 + 连通检测
python test_local_engine.py   # 本地引擎: 档位/下载/预检/子进程/本地模式识别全链路 (伪装推理端, 无需真机)
```
