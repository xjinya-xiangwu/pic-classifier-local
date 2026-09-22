# -*- coding: utf-8 -*-
"""本地引擎测试 (PRD v2 L2): 选型推荐/安装态/下载(打桩)/内存预检/子进程生命周期/本地模式识别全链路。
用一个伪装成 mlx_vlm.server 的本地 HTTP 服务模拟推理端, 无需真机 MLX 即可验证 OpenAI 兼容链路。
运行: python test_local_engine.py
"""
import json
import shutil
import sys
import tempfile
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import app as pc
import local_engine as le

tmp = Path(tempfile.mkdtemp(prefix="pc_local_"))
le.APP_DIR = tmp
le.MODELS_DIR = tmp / "models"
le.SERVER_LOG = tmp / "mlx_server.log"
pc.APP_DIR = tmp
pc.THUMB_DIR = tmp / "thumbs"
pc.init_db(tmp / "db.sqlite")

TAG = {"has_model": True,
       "clothing": {"type": "连衣裙", "color_main": "白", "pattern": "纯色"},
       "pose": "站立-叉腰", "orientation": "正面", "scale": "全身",
       "quality": {"score": 4, "flags": []}}

# --- A. 档位推荐 (用户决策: MVP 优先, 永远推荐最小可用档 4B) ---
assert le.recommend_model(128) == "Qwen3-VL-4B-Instruct-4bit"
assert le.recommend_model(48) == "Qwen3-VL-4B-Instruct-4bit"
assert le.recommend_model(36) == "Qwen3-VL-4B-Instruct-4bit"
assert le.recommend_model(None) == "Qwen3-VL-4B-Instruct-4bit"
print("OK A 档位推荐: 一律推荐 MVP 档 4B (升级由用户手动选择)")

# --- B. 安装态与删除 ---
name = "Qwen3-VL-4B-Instruct-4bit"
assert not le.installed(name)
d = le.model_dir(name)
d.mkdir(parents=True)
(d / "config.json").write_text("{}")
(d / "model.safetensors").write_bytes(b"x" * 1024)
assert le.installed(name)
cfg = pc.save_settings({"mode": "local", "local_model": name})
assert le.delete_model(name) == {"deleted": name}
assert not d.exists() and not le.installed(name)
print("OK B 安装态检测与删除")

# --- C. 下载: 打桩 snapshot_download 验证状态机 (真实下载在真机/真网验证) ---
le._ensure_mlx_deps = lambda: None  # Windows 测试环境跳过 MLX 依赖安装 (真机上自动安装)


class FakeHub:
    def snapshot_download(self, repo_id, local_dir):
        p = Path(local_dir)
        p.mkdir(parents=True, exist_ok=True)
        time.sleep(0.5)  # 模拟传输中, 让进度轮询跑起来
        (p / "config.json").write_text("{}")
        (p / "model.safetensors").write_bytes(b"x" * 2048)
        return str(p)

sys.modules["huggingface_hub"] = FakeHub()
le.start_download(name, endpoint="https://hf-mirror.com")
for _ in range(100):
    time.sleep(0.2)
    st = le.download_state()
    if st["done"] or st["error"]:
        break
assert st["done"] == name and not st["error"], f"下载应完成, 实得 {st}"
assert le.installed(name), "下载完成后应可检测到已安装"
assert st["name"] is None, "完成后应释放下载锁"
try:
    le.start_download(name)
    raise AssertionError("重复下载应被拒绝")
except ValueError as e:
    assert "已安装" in str(e)
print("OK C 下载状态机: 完成/已安装/重复下载拦截")

# --- D. 内存预检 (KR-L5): 8GB 内存拒载 30B 档 ---
d30 = le.model_dir("Qwen3-VL-30B-A3B-Instruct-4bit")
d30.mkdir(parents=True)
(d30 / "config.json").write_text("{}")
(d30 / "model.safetensors").write_bytes(b"x")
le.physical_ram_gb = lambda: 8
try:
    le.ensure_server({"mode": "local", "local_model": "Qwen3-VL-30B-A3B-Instruct-4bit",
                      "local_server_cmd": "noop"})
    raise AssertionError("低内存加载 30B 应被拒绝")
except ValueError as e:
    assert "内存不足" in str(e)
le.physical_ram_gb = lambda: None
le.delete_model(name)  # 4B 已装 (测试C), 删掉以测"未下载"拦截
try:
    le.ensure_server({"mode": "local", "local_model": "Qwen3-VL-4B-Instruct-4bit",
                      "local_server_cmd": "noop {port}"})
    raise AssertionError("未下载模型应被拒绝")
except ValueError as e:
    assert "未下载" in str(e)
print("OK D 内存预检与未下载拦截")

# --- E. 子进程生命周期 + 本地模式识别全链路 ---
# 重新安装 4B 假模型 (D2 测"未下载"时删掉了)
d.mkdir(parents=True, exist_ok=True)
(d / "config.json").write_text("{}")
(d / "model.safetensors").write_bytes(b"x")
assert le.installed(name)
# 伪装 mlx_vlm.server: 接受 --port, GET /v1/models 返回 200, POST /v1/chat/completions 返回标注 JSON
fake_srv = tmp / "fake_mlx_server.py"
fake_srv.write_text(textwrap.dedent("""
    import json, sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    TAG = __TAG_TEXT__  # 与真实 mlx server 一致: content 是 JSON 字符串
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        def do_GET(self):
            self._send({"data": [{"id": "fake-vlm"}]})
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._send({"choices": [{"message": {"content": TAG}}],
                        "usage": {"prompt_tokens": 640, "completion_tokens": 210}})
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[sys.argv.index("--port") + 1])), H).serve_forever()
""").replace("__TAG_TEXT__", json.dumps(json.dumps(TAG, ensure_ascii=False), ensure_ascii=False)), encoding="utf-8")
cfg = pc.save_settings({"mode": "local", "local_model": name,
                        "local_server_cmd": '{python} "{model_path}/../../fake_mlx_server.py" --port {port}'})
le.ensure_server(cfg)
t0 = time.time()
le.wait_ready(cfg, timeout=30)
assert time.time() - t0 < 25, "就绪等待应远快于超时"
st = le.status()
assert st["phase"] == "ready" and st["port"], f"服务应就绪, 实得 {st}"
print(f"OK E1 服务子进程启动并就绪 (port {st['port']}, {time.time() - t0:.1f}s)")

# 识别: 5 张图, mode=local, 服务指向伪装推理端
folder = tmp / "photos"
folder.mkdir()
for i in range(5):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (300, 400), (220, 220, 220))
    ImageDraw.Draw(img).text((10, 10), f"p{i}", fill=(0, 0, 0))
    img.save(folder / f"IMG_{i}.jpg")
pid = pc.db_exec("INSERT INTO project(folder, created_at) VALUES(?,?)", (str(folder), "t")).lastrowid
pc.scan_folder(folder, False, pid)
pc.start_tagging(pid)
for _ in range(120):
    time.sleep(0.25)
    if not pc.STATE["tag"]:
        break
assert not pc.STATE["tag"], "识别应结束"
errs = pc.db_all("SELECT filename, error FROM photo WHERE status='failed' LIMIT 3")
for f, e in errs:
    print("失败样例:", f, "->", (e or "")[:200])
tagged = pc.db_one("SELECT COUNT(*) FROM photo WHERE status='tagged'")[0]
assert tagged == 5, f"本地模式 5 张应全部打标成功, 实得 {tagged}"
stp = pc.state_payload()
assert stp["groups"] and len(stp["groups"]) == 1, "同标签照片应聚为一组"
assert stp["settings"]["mode"] == "local" and stp["local"]["server"]["phase"] == "ready"
print("OK E2 本地模式识别全链路: 5 张打标 → 1 组 (OpenAI 兼容契约, v1 分组复用)")

# --- F. 重复启动幂等 + 停止 ---
port_before = le.status()["port"]
le.ensure_server(cfg)
assert le.status()["port"] == port_before, "重复启动应幂等"
le.stop_server()
assert le.status()["phase"] == "stopped"
print("OK F 服务幂等与停止")

pc._conn.close()
le.stop_server()
shutil.rmtree(tmp, ignore_errors=True)
print("OK 本地引擎测试全部通过 (A-F)")
