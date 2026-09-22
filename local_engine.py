# -*- coding: utf-8 -*-
"""本地视觉模型引擎 (PRD v2): 模型管理(下载/删除/预检) + MLX 推理服务子进程管理。

- 服务为 App 管理的子进程, OpenAI 兼容 HTTP, 仅监听 127.0.0.1 随机端口 (PRD D2);
- 模型存 ~/.photocurator/models/ (数据目录; 不放安装目录, 避免安装器 rm -rf 时误删);
- 下载用 huggingface_hub snapshot_download, 天然断点续传; 中断 = 关闭 App, 再下自动续传;
- MLX 仅 macOS 可运行: Windows 开发机目录/预检逻辑可跑, 启动服务会失败 —— e2e 用 mock。
"""
import atexit
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path.home() / ".photocurator"   # 与 app.py 保持一致 (避免循环导入)

MODELS = {
    "Qwen3-VL-30B-A3B-Instruct-4bit": {
        "repo": "mlx-community/Qwen3-VL-30B-A3B-Instruct-4bit", "size_gb": 18.3, "min_ram_gb": 48},
    "Qwen3-VL-8B-Instruct-4bit": {
        "repo": "mlx-community/Qwen3-VL-8B-Instruct-4bit", "size_gb": 5.8, "min_ram_gb": 16},
    "Qwen3-VL-4B-Instruct-4bit": {
        "repo": "mlx-community/Qwen3-VL-4B-Instruct-4bit", "size_gb": 3.1, "min_ram_gb": 8},
}
MODELS_DIR = APP_DIR / "models"
SERVER_LOG = APP_DIR / "mlx_server.log"
LOAD_TIMEOUT = 900  # 模型加载健康检查上限 (秒), 18GB 冷读也在 15 分钟内


def model_dir(name: str) -> Path:
    return MODELS_DIR / name


def installed(name: str) -> bool:
    d = model_dir(name)
    return (d / "config.json").exists() and any(d.glob("*.safetensors"))


def physical_ram_gb():
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            return round(int(out.stdout.strip()) / 1e9)
        if sys.platform == "win32":
            import ctypes

            class MEM(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MEM()
            m.dwLength = ctypes.sizeof(MEM)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return round(m.ullTotalPhys / 1e9)
    except Exception:
        pass
    return None  # 未知内存: 预检放行, 交给用户自己判断


def recommend_model(ram_gb=None) -> str:
    """用户决策 (2026-09-22): 优先体积最小的可用 MVP 档, 实测后由用户手动升级。
    内存上限仅作预检拦截 (如 8GB 内存不允许装 30B), 不主动推荐大档。"""
    ram = physical_ram_gb() if ram_gb is None else ram_gb
    if ram is not None and ram < 8:
        return "Qwen3-VL-4B-Instruct-4bit"  # 4B 本身仅需 8GB, 更低配才需要提示
    return "Qwen3-VL-4B-Instruct-4bit"


def disk_free_gb() -> float:
    root = MODELS_DIR if MODELS_DIR.exists() else MODELS_DIR.parent
    return shutil.disk_usage(str(root)).free / 1e9


# ---------------------------------------------------------------- 下载 (后台线程, 关闭 App 即中断, 重下续传)
_dl = {"name": None, "pct": 0.0, "error": None, "done": None, "msg": None}
_dl_lock = threading.Lock()


def _ensure_mlx_deps():
    """模型模块自动化适配 (用户决策 2026-09-22): 安装器只装程序本体;
    用户首次点下载时在此装 MLX 推理依赖, 仅 macOS 可装。"""
    try:
        import mlx_vlm  # noqa: F401
        return
    except ImportError:
        pass
    if sys.platform != "darwin":
        raise RuntimeError("本地推理仅支持 macOS (Apple Silicon); 当前平台无法安装 MLX")
    with _dl_lock:
        _dl["msg"] = "正在安装本地推理依赖 (mlx-vlm, 约 300MB)…"
    req = Path(__file__).parent / "requirements-mlx.txt"
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)], check=True)
    with _dl_lock:
        _dl["msg"] = None


def download_state() -> dict:
    with _dl_lock:
        return dict(_dl)


def start_download(name: str, endpoint: str = ""):
    m = MODELS.get(name)
    if not m:
        raise ValueError(f"未知模型: {name}")
    if installed(name):
        raise ValueError(f"{name} 已安装")
    with _dl_lock:
        if _dl["name"]:
            raise ValueError(f"已有下载进行中: {_dl['name']}")
    free = disk_free_gb()
    need = m["size_gb"] * 1.25
    if free and free < need:
        raise ValueError(f"磁盘空间不足: 需约 {need:.0f}GB (含余量), 当前剩余 {free:.0f}GB")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    if endpoint:
        os.environ["HF_ENDPOINT"] = endpoint  # PRD D6/OpenQ4: 默认 hf-mirror 国内镜像
    with _dl_lock:
        _dl.update(name=name, pct=0.0, error=None, done=None)
    threading.Thread(target=_dl_run, args=(name,), daemon=True).start()


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _dl_run(name: str):
    m = MODELS[name]
    dest = model_dir(name)
    try:
        _ensure_mlx_deps()
        threading.Thread(target=_dl_progress_poll, args=(name, dest, m["size_gb"] * 1e9),
                         daemon=True).start()
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            raise RuntimeError("缺少依赖 huggingface_hub: 请重新运行安装命令")
        snapshot_download(repo_id=m["repo"], local_dir=str(dest))
        if not installed(name):
            raise RuntimeError("下载目录不完整 (缺 config.json/safetensors), 可重新下载续传")
        with _dl_lock:
            _dl["pct"] = 100.0
            _dl["done"] = name
    except Exception as e:
        with _dl_lock:
            _dl["error"] = str(e)[:300]
    finally:
        with _dl_lock:
            _dl["name"] = None


def _dl_progress_poll(name: str, dest: Path, expected_bytes: float):
    while download_state()["name"] == name:
        try:
            with _dl_lock:
                _dl["pct"] = round(min(99.0, _dir_size(dest) / expected_bytes * 100), 1)
        except Exception:
            pass
        time.sleep(2)


def delete_model(name: str) -> dict:
    st = status()
    if st["phase"] in ("loading", "ready") and st["model"] == name:
        raise ValueError("该模型服务正在运行, 请先停止服务再删除")
    if not installed(name):
        raise ValueError(f"{name} 未安装")
    shutil.rmtree(model_dir(name))
    return {"deleted": name}


# ---------------------------------------------------------------- 推理服务子进程 (PRD D2)
_srv_lock = threading.Lock()
_srv = {"phase": "stopped", "model": None, "port": None, "error": None}  # stopped|loading|ready|failed
_proc = None


def status() -> dict:
    with _srv_lock:
        return dict(_srv)


def server_url() -> str:
    st = status()
    if st["phase"] != "ready":
        raise RuntimeError(f"本地模型服务未就绪 ({st['phase']})" + (f": {st['error']}" if st["error"] else ""))
    return f"http://127.0.0.1:{st['port']}/v1"


def _port_free(p: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", p))
            return True
        except OSError:
            return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _healthy(port: int) -> bool:
    # 任何 HTTP 响应 (含 404) 都证明服务已起; 连接拒绝才算未就绪
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=2)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def ensure_server(cfg: dict):
    """启动模型服务 (幂等, 立即返回); 加载进度经 status()/wait_ready() 跟踪。"""
    global _proc
    name = cfg["local_model"]
    if name not in MODELS:
        raise ValueError(f"未知模型: {name}")
    with _srv_lock:
        if _srv["phase"] in ("loading", "ready") and _srv["model"] == name:
            return
        _stop_locked()
        if not installed(name):
            raise ValueError(f"本地模型 {name} 未下载, 请先在设置中下载")
        ram = physical_ram_gb()
        need = MODELS[name]["min_ram_gb"]
        if ram and ram < need:
            raise ValueError(f"内存不足: {name} 建议 ≥{need}GB 统一内存, 本机 {ram}GB, 请选择更小档位 (KR-L5)")
        port = _free_port()
        cmd = cfg["local_server_cmd"].format(python=sys.executable,
                                             model_path=str(model_dir(name)), port=port)
        log = open(SERVER_LOG, "ab")
        try:
            import shlex
            # Windows 路径: posix 模式吃反斜杠, 非posix 模式保留引号 → 用非posix 再剥掉成对引号
            tokens = [t.strip('"') for t in shlex.split(cmd, posix=False)]
            _proc = subprocess.Popen(tokens, stdout=log, stderr=log)
        except OSError as e:
            log.close()
            raise RuntimeError(f"启动本地推理服务失败: {e}")
        log.close()  # 子进程已继承句柄, 父进程副本关闭避免长期锁住日志文件
        _srv.update(phase="loading", model=name, port=port, error=None)
    threading.Thread(target=_watch, args=(name, port, _proc), daemon=True).start()


def _watch(name: str, port: int, proc):
    deadline = time.time() + LOAD_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:  # 进程提前退出
            tail = _log_tail()
            with _srv_lock:
                _srv.update(phase="failed", error=f"服务进程退出 (code {proc.returncode}): {tail}")
            return
        if _healthy(port):
            with _srv_lock:
                _srv.update(phase="ready", error=None)
            return
        time.sleep(2)
    with _srv_lock:
        _srv.update(phase="failed", error=f"模型加载超时 ({LOAD_TIMEOUT}s), 见日志 {SERVER_LOG}")
    stop_server()


def _log_tail() -> str:
    try:
        return SERVER_LOG.read_text(errors="ignore").strip().splitlines()[-1][:200] if SERVER_LOG.exists() else ""
    except Exception:
        return ""


def wait_ready(cfg: dict, timeout: int = LOAD_TIMEOUT):
    """阻塞直到服务就绪; 失败/超时抛异常。识别线程调用, 阶段经 status() 对前端可见。"""
    name = cfg["local_model"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = status()
        if st["phase"] == "ready" and st["model"] == name:
            return
        if st["phase"] == "failed":
            raise RuntimeError("本地模型服务启动失败: " + str(st["error"]))
        if st["phase"] == "stopped":
            ensure_server(cfg)
        time.sleep(2)
    raise RuntimeError(f"等待模型服务就绪超时 ({timeout}s)")


def stop_server():
    with _srv_lock:
        _stop_locked()


def _stop_locked():
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
    _proc = None
    _srv.update(phase="stopped", model=None, port=None, error=None)


atexit.register(stop_server)


def ui_status(cfg: dict) -> dict:
    dl = download_state()
    return {
        "models": [{"name": n, "size_gb": m["size_gb"], "min_ram_gb": m["min_ram_gb"],
                    "installed": installed(n)} for n, m in MODELS.items()],
        "server": status(),
        "download": dl if (dl["name"] or dl["error"] or dl["done"]) else None,
        "ram_gb": physical_ram_gb(),
        "recommended": recommend_model(),
    }
