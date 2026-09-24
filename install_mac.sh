#!/bin/bash
# PhotoCurator Local (本地模型版) macOS 一键安装器。
# 用法: 浏览器打开本脚本的 GitHub 页面下载, 或直接:
#   bash <(curl -fsSL https://raw.githubusercontent.com/xjinya-xiangwu/pic-classifier-local/main/install_mac.sh)
# 效果: 下载代码到 ~/PhotoCuratorLocal, 装好依赖, 在 /Applications 生成 "PhotoCurator Local.app" 并启动。
# 与 API 版 (PhotoCurator.app, 端口 8765) 完全独立: 不同目录/应用名/端口 (本版 8776+), 可共存。
# 覆盖模式: 每次运行都自动停止并删除旧程序 (含旧 venv), 下载全新代码重建;
# 模型权重与用户数据在 ~/.photocurator/ (数据目录), 始终保留; 用户照片文件夹从不改动。
# 更新版本: 重新执行一次本命令即可。
set -e

DEST="$HOME/PhotoCuratorLocal"
APP="/Applications/PhotoCurator Local.app"
REPO_TARBALL="https://codeload.github.com/xjinya-xiangwu/pic-classifier-local/tar.gz/refs/heads/main"

echo "==> 0/4 停止正在运行的旧版 Local (如有; 不影响 API 版 PhotoCurator)"
pkill -f "$DEST/.venv/bin/python" 2>/dev/null || true
pkill -f "$APP/Contents/MacOS/PhotoCurator" 2>/dev/null || true
# 更早期版本的本地版与 API 版同目录同名 (~/PhotoCurator / PhotoCurator.app / 端口 8765+),
# 上面的按路径清理够不到它; 用端口探测区分: /api/state 含 "local_model" 的才是本地版, 只杀它
for p in 8765 8766 8767 8768 8769 8770 8771 8772 8773 8774 8775 8776 8777 8778 8779 8780 8781 8782 8783 8784 8785; do
  if curl -fsS "http://127.0.0.1:$p/api/state" 2>/dev/null | grep -q '"local_model"'; then
    lsof -ti tcp:$p -sTCP:LISTEN 2>/dev/null | xargs kill 2>/dev/null || true
  fi
done
sleep 1

# 覆盖模式: 顺带清理更早期版本 (与 API 版共用 ~/PhotoCurator 目录的时代) 的残留程序。
# 仅当该目录的 app.py 含 local_engine (确认是旧本地版) 才删, API 版的目录一律不碰;
# 模型与用户数据在 ~/.photocurator/, 与程序目录无关, 始终保留。
OLDDEST="$HOME/PhotoCurator"
if [ -f "$OLDDEST/app.py" ] && grep -q "local_engine" "$OLDDEST/app.py" 2>/dev/null; then
  echo "    检测到更早期版本的本地版残留 ($OLDDEST), 一并清理..."
  rm -rf "$OLDDEST"
  rm -rf "/Applications/PhotoCurator.app"   # 此时该包必属旧本地版 (旧版与 API 版包同名)
  echo "    已清理。API 版 PhotoCurator 若有安装则不受影响。"
fi

echo "==> 1/4 覆盖安装: 删除旧程序目录并下载最新代码"
# 覆盖模式只删程序本身; 以下资产在数据目录 ~/.photocurator/, 始终保留:
#   已下载的模型 (models/) · 索引数据库 · 缩略图 · 撤销记录 · 清单 CSV; 用户的照片文件夹从不改动。
[ -f "$DEST/app.log" ] && cp "$DEST/app.log" "$HOME/.photocurator/app.log.old" 2>/dev/null || true
rm -rf "$DEST"
mkdir -p "$DEST"
echo "    已保留: 模型与数据 ~/.photocurator/ (含已下载模型), 你的照片不受影响; 上次日志存为 ~/.photocurator/app.log.old"
curl -fsSL "$REPO_TARBALL" | tar -xz -C "$DEST" --strip-components=1

echo "==> 2/4 准备 Python (MLX 需要 ≥3.10; 缺失时会弹开发者工具安装窗口)"
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "错误: 未找到 ≥3.10 的 Python。macOS 自带的 3.9 无法运行本地模型。"
  echo "请从 https://www.python.org/downloads/ 安装 Python 3.12+ (安装时勾选 Add to PATH),"
  echo "或执行 brew install python 后, 重新运行本安装命令。"
  exit 1
fi
echo "    使用 $PY ($($PY -V 2>&1))"
until "$PY" -c "" 2>/dev/null; do
  xcode-select --install 2>/dev/null || true
  echo "    等待 python3 可用... (若弹出安装窗口请点击安装, 约 2-5 分钟)"
  sleep 10
done
# 旧 venv 若由 <3.10 的 Python 创建, 自动重建
if [ -x "$DEST/.venv/bin/python" ] && ! "$DEST/.venv/bin/python" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
  echo "    检测到旧 venv 使用过旧 Python, 重建..."
  rm -rf "$DEST/.venv"
fi

echo "==> 3/4 安装依赖 (首次约 1-2 分钟; 仅程序本体, 模型在 App 设置内一键下载)"
cd "$DEST"
[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

echo "==> 4/4 生成 $APP"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>PhotoCurator Local</string>
  <key>CFBundleDisplayName</key><string>PhotoCurator Local</string>
  <key>CFBundleExecutable</key><string>PhotoCuratorLocal</string>
  <key>CFBundleIdentifier</key><string>local.photocurator.local</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>2.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/PhotoCuratorLocal" <<'LAUNCH'
#!/bin/bash
# 双击入口: 确保依赖后启动本地服务, 日志在 ~/PhotoCuratorLocal/app.log
cd "$HOME/PhotoCuratorLocal" || exit 1
PY="$HOME/PhotoCuratorLocal/.venv/bin/python"
if [ ! -x "$PY" ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi
exec "$PY" "$HOME/PhotoCuratorLocal/app.py" >> "$HOME/PhotoCuratorLocal/app.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/PhotoCuratorLocal"

# 向 LaunchServices 注册新生成的 App, 保证 Launchpad/Spotlight 能尽快搜到、以后能正常双击
LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[ -x "$LSREG" ] && "$LSREG" -f "$APP" >/dev/null 2>&1 || true

echo "==> 启动 PhotoCurator Local (浏览器将自动打开)"
# 直接后台拉起服务并等待端口就绪; 不经过 `open`, 规避老系统上
# `_LSOpenURLsWithCompletionHandler() failed with error -600` 导致 App 启动失败。
nohup "$DEST/.venv/bin/python" "$DEST/app.py" >> "$DEST/app.log" 2>&1 &
URL=""
for i in {1..40}; do
  sleep 0.5
  for p in 8776 8777 8778 8779 8780 8781 8782 8783 8784 8785; do
    if curl -fsS -o /dev/null "http://127.0.0.1:$p/api/state" 2>/dev/null; then URL="http://127.0.0.1:$p"; break; fi
  done
  [ -n "$URL" ] && break
done
echo ""
if [ -n "$URL" ]; then
  echo "完成! PhotoCurator Local 应已在浏览器中自动打开: $URL"
  echo "  (端口 8776+ 是本地版专用; API 版 PhotoCurator 在 8765, 两者互不影响)"
  echo "  若浏览器没有自动打开, 把上面的地址复制到浏览器即可。"
else
  echo "警告: 服务 20 秒内未启动, 最近日志如下:"
  tail -n 15 "$DEST/app.log" 2>/dev/null || true
  echo "可重新执行本安装命令再试, 或手动双击 应用程序 中的 PhotoCurator Local。"
fi
echo ""
echo "以后在 启动台/应用程序 里双击 PhotoCurator Local 即可 (注意与 API 版 PhotoCurator 是两个图标)。"
echo "  - 覆盖安装: 重新执行本安装命令 = 程序全量更新; 已下载的模型与数据自动保留, 无需重新下载"
echo "  - App 日志: ~/PhotoCuratorLocal/app.log (上次的日志: ~/.photocurator/app.log.old)"
echo "  - 首次扫描时若询问文件夹访问权限, 点\"允许\""
echo "  - 数据目录 ~/.photocurator 为两版共用; 请勿同时在两版中整理同一个文件夹"
