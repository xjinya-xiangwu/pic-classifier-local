#!/bin/bash
# PhotoCurator Local (本地模型版) macOS 一键安装器。
# 用法: 浏览器打开本脚本的 GitHub 页面下载, 或直接:
#   bash <(curl -fsSL https://raw.githubusercontent.com/xjinya-xiangwu/pic-classifier-local/main/install_mac.sh)
# 效果: 下载代码到 ~/PhotoCuratorLocal, 装好依赖, 在 /Applications 生成 "PhotoCurator Local.app" 并启动。
# 与 API 版 (PhotoCurator.app, 端口 8765) 完全独立: 不同目录/应用名/端口 (本版 8776+), 可共存。
# 更新版本: 重新执行一次本命令即可 (模型文件存于 ~/.photocurator/models/, 升级不受影响)。
set -e

DEST="$HOME/PhotoCuratorLocal"
APP="/Applications/PhotoCurator Local.app"
REPO_TARBALL="https://codeload.github.com/xjinya-xiangwu/pic-classifier-local/tar.gz/refs/heads/main"

echo "==> 0/4 停止正在运行的旧版 Local (如有; 不影响 API 版 PhotoCurator)"
pkill -f "$DEST/.venv/bin/python" 2>/dev/null || true
pkill -f "$APP/Contents/MacOS/PhotoCurator" 2>/dev/null || true
sleep 1

echo "==> 1/4 下载代码到 $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"
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
echo "  - App 日志: ~/PhotoCuratorLocal/app.log"
echo "  - 升级版本: 重新执行本安装命令"
echo "  - 首次扫描时若询问文件夹访问权限, 点\"允许\""
echo "  - 数据目录 ~/.photocurator 为两版共用; 请勿同时在两版中整理同一个文件夹"
