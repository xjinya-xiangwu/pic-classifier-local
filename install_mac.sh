#!/bin/bash
# PhotoCurator 本地模型版 (pic-classifier-local) macOS 一键安装器。
# 用法: 浏览器打开本脚本的 GitHub 页面下载, 或直接:
#   bash <(curl -fsSL https://raw.githubusercontent.com/xjinya-xiangwu/pic-classifier-local/main/install_mac.sh)
# 效果: 下载代码到 ~/PhotoCurator, 装好依赖(含 MLX 本地推理), 在 /Applications 生成 PhotoCurator.app 并启动。
# 更新版本: 重新执行一次本命令即可 (模型文件存于 ~/.photocurator/models/, 升级不受影响)。
set -e

DEST="$HOME/PhotoCurator"
APP="/Applications/PhotoCurator.app"
REPO_TARBALL="https://codeload.github.com/xjinya-xiangwu/pic-classifier-local/tar.gz/refs/heads/main"

echo "==> 0/4 停止正在运行的旧版本 (如有)"
pkill -f "^./\.venv/bin/python app\.py" 2>/dev/null || true   # 旧版启动器的相对路径进程
pkill -f "$DEST/.venv/bin/python"      2>/dev/null || true   # 新版启动器的绝对路径进程
pkill -f "$APP/Contents/MacOS/PhotoCurator" 2>/dev/null || true
sleep 1

echo "==> 1/4 下载代码到 $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"
curl -fsSL "$REPO_TARBALL" | tar -xz -C "$DEST" --strip-components=1

echo "==> 2/4 准备 Python (缺失时会弹出开发者工具安装窗口, 点\"安装\"后本脚本自动继续)"
until python3 -c "" 2>/dev/null; do
  xcode-select --install 2>/dev/null || true
  echo "    等待 python3 可用... (若弹出安装窗口请点击安装, 约 2-5 分钟)"
  sleep 10
done

echo "==> 3/4 安装依赖 (首次约 1-2 分钟; 仅程序本体, 模型在 App 设置内一键下载)"
cd "$DEST"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q -r requirements.txt

echo "==> 4/4 生成 /Applications/PhotoCurator.app"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>PhotoCurator</string>
  <key>CFBundleDisplayName</key><string>PhotoCurator</string>
  <key>CFBundleExecutable</key><string>PhotoCurator</string>
  <key>CFBundleIdentifier</key><string>local.photocurator.app</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.1</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/PhotoCurator" <<'LAUNCH'
#!/bin/bash
# 双击入口: 确保依赖后启动本地服务, 日志在 ~/PhotoCurator/app.log
cd "$HOME/PhotoCurator" || exit 1
PY="$HOME/PhotoCurator/.venv/bin/python"
if [ ! -x "$PY" ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi
exec "$PY" "$HOME/PhotoCurator/app.py" >> "$HOME/PhotoCurator/app.log" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/PhotoCurator"

# 向 LaunchServices 注册新生成的 App, 保证 Launchpad/Spotlight 能尽快搜到、以后能正常双击
LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
[ -x "$LSREG" ] && "$LSREG" -f "$APP" >/dev/null 2>&1 || true

echo "==> 启动 PhotoCurator (浏览器将自动打开)"
# 直接后台拉起服务并等待端口就绪; 不经过 `open`, 规避老系统上
# `_LSOpenURLsWithCompletionHandler() failed with error -600` 导致 App 启动失败。
nohup "$DEST/.venv/bin/python" "$DEST/app.py" >> "$DEST/app.log" 2>&1 &
URL=""
for i in {1..40}; do
  sleep 0.5
  for p in 8765 8766 8767 8768 8769 8770 8771 8772 8773 8774 8775; do
    if curl -fsS -o /dev/null "http://127.0.0.1:$p/api/state" 2>/dev/null; then URL="http://127.0.0.1:$p"; break; fi
  done
  [ -n "$URL" ] && break
done
echo ""
if [ -n "$URL" ]; then
  echo "完成! 页面应已在浏览器中自动打开: $URL"
  echo "  若浏览器没有自动打开, 把上面的地址复制到浏览器即可。"
else
  echo "警告: 服务 20 秒内未启动, 最近日志如下:"
  tail -n 15 "$DEST/app.log" 2>/dev/null || true
  echo "可重新执行本安装命令再试, 或手动双击 应用程序 中的 PhotoCurator。"
fi
echo ""
echo "以后在 启动台/应用程序 里双击 PhotoCurator 即可。"
echo "  - App 日志: ~/PhotoCurator/app.log"
echo "  - 升级版本: 重新执行本安装命令"
echo "  - 首次扫描时若询问文件夹访问权限, 点\"允许\""
