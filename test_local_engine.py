# -*- coding: utf-8 -*-
"""本地引擎测试 (PRD v2 L2): 选型推荐/安装态/下载(打桩)/内存预检/子进程生命周期/本地模式识别全链路。
用一个按新版 mlx_vlm.server (0.7.x) 语义伪装的本地 HTTP 服务模拟推理端, 无需真机 MLX。
运行: python test_local_engine.py
"""
import json
import os
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

# 伪装新版 mlx_vlm.server (0.7.x): 模型缓存按 --model 启动路径精确匹配,
# 请求的 model 字段与预载路径不一致时回 500 (复现并守住"模型已就绪但识别全失败"的回归);
# 每个请求把 model/max_tokens 记到固定文件, 供测试断言请求契约。
fake_srv = tmp / "fake_mlx_server.py"
record_path = tmp / "last_request.json"
fake_srv.write_text(textwrap.dedent("""
    import json, sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    TAG = __TAG_TEXT__
    args = sys.argv
    MODEL_PATH = args[args.index("--model") + 1]
    RECORD = args[args.index("--record") + 1]
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
            self._send({"data": [{"id": MODEL_PATH}]})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            with open(RECORD, "a") as f:
                f.write(json.dumps({"model": body.get("model"), "max_tokens": body.get("max_tokens")}) + "\\n")
            if body.get("model") != MODEL_PATH:
                self._send({"error": {"message": "Model '%s' is not loaded. Preloaded: '%s'"
                                      % (body.get("model"), MODEL_PATH)}}, 500)
                return
            self._send({"choices": [{"message": {"content": TAG}}],
                        "usage": {"prompt_tokens": 640, "completion_tokens": 210}})
    ThreadingHTTPServer(("127.0.0.1", int(args[args.index("--port") + 1])), H).serve_forever()
""").replace("__TAG_TEXT__", json.dumps(json.dumps(TAG, ensure_ascii=False), ensure_ascii=False)), encoding="utf-8")
cfg = pc.save_settings({"mode": "local", "local_model": name,
                        "local_server_cmd": ('{python} "%s" --port {port} --model "{model_path}" --record "%s"'
                                             % (fake_srv, record_path))})
le.ensure_server(cfg)
t0 = time.time()
le.wait_ready(cfg, timeout=60)
assert time.time() - t0 < 50, "就绪等待应远快于超时"
st = le.status()
assert st["phase"] == "ready" and st["port"], f"服务应就绪 (含实测探针通过), 实得 {st}"
probe_recs = [json.loads(x) for x in record_path.read_text().splitlines()]
assert probe_recs and probe_recs[0]["model"] == str(d), "就绪探针必须回传预载路径作为 model 字段"
print(f"OK E1 服务子进程启动并就绪, 实测探针通过 (port {st['port']}, {time.time() - t0:.1f}s)")
record_path.write_text("")  # 清空, 只保留识别阶段的请求记录

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
tagged = pc.db_one("SELECT COUNT(*) FROM photo WHERE status='tagged'")[0]
assert tagged == 5, f"本地模式 5 张应全部打标成功, 实得 {tagged}"
recs = [json.loads(x) for x in record_path.read_text().splitlines()]
assert recs, "识别阶段应发出标注请求"
assert all(r["model"] == str(d) for r in recs), f"识别请求 model 必须是预载路径, 实得 {recs[0]}"
assert all(r["max_tokens"] == 1024 for r in recs), "识别请求必须显式携带 max_tokens 防截断"
stp = pc.state_payload()
assert stp["groups"] and len(stp["groups"]) == 1, "同标签照片应聚为一组"
assert stp["settings"]["mode"] == "local" and stp["local"]["server"]["phase"] == "ready"
print("OK E2 本地模式识别全链路: 5 张打标 → 1 组 (路径匹配 + max_tokens 契约 + v1 分组复用)")

# --- E3. 就绪探针真失败路径: 指向"端口通但请求必败"的假服务 → 状态应 failed 且带原因 ---
bad_srv = tmp / "bad_mlx_server.py"
bad_srv.write_text(textwrap.dedent("""
    import json, sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
            self._send({"data": []})  # 健康检查通过
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._send({"error": {"message": "Model not loaded"}}, 500)  # 实际请求必败
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[sys.argv.index("--port") + 1])), H).serve_forever()
"""), encoding="utf-8")
le.stop_server()
cfg_bad = pc.save_settings({"local_server_cmd": '{python} "%s" --port {port}' % bad_srv})
le.ensure_server(cfg_bad)
for _ in range(60):
    time.sleep(0.5)
    st_bad = le.status()
    if st_bad["phase"] in ("failed", "ready"):
        break
assert st_bad["phase"] == "failed", f"端口通但请求必败的服务不得显示就绪, 实得 {st_bad}"
assert "实测请求失败" in st_bad["error"], f"失败原因应透出, 实得 {st_bad['error']}"
print(f"OK E3 假就绪拦截: 探针发现请求必败 → failed ({st_bad['error'][:60]}…)")
le.stop_server()

# --- F. 重复启动幂等 + 停止 ---
le.ensure_server(cfg)
port_before = le.status()["port"]
le.ensure_server(cfg)
assert le.status()["port"] == port_before, "重复启动应幂等"
le.stop_server()
assert le.status()["phase"] == "stopped"
print("OK F 服务幂等与停止")

# --- F2. 加载中被主动停止 (换模型/手动停) 不得误报 failed (竞态回归) ---
# 假服务只 sleep 不起 HTTP: _healthy 永远失败 → phase 停在 loading; 此时 stop_server,
# _watch 下一轮循环必须因 phase==stopped 直接退出, 而不是把进程退出误报成 failed
hang_srv = tmp / "hang_server.py"
hang_srv.write_text("import time\ntime.sleep(300)\n", encoding="utf-8")
cfg_hang = pc.save_settings({"local_server_cmd": '{python} "%s" --port {port}' % hang_srv})
le.ensure_server(cfg_hang)
for _ in range(20):
    time.sleep(0.3)
    if le.status()["phase"] == "loading":
        break
assert le.status()["phase"] == "loading", "假服务不响应时应停在 loading"
le.stop_server()
time.sleep(3)  # 给 _watch 足够轮次走到误报分支 (若无守卫会置 failed)
st_f2 = le.status()
assert st_f2["phase"] == "stopped" and not st_f2["error"], f"主动停止后不得误报 failed, 实得 {st_f2}"
print("OK F2 竞态守卫: 加载中主动停止 → 保持 stopped, 无误报")

# --- G. 模型切换 + 性能预估元数据 (切换前后对比功能的数据源) ---
st = le.ui_status({"mode": "local"})
m4 = next(m for m in st["models"] if m["name"] == "Qwen3-VL-4B-Instruct-4bit")
for f in ("infer_ram_gb", "sec_per_img", "f1_est", "json_first_pass", "desc", "ram_ok"):
    assert f in m4, f"ui_status 缺少对比字段 {f}"
assert 0 < m4["f1_est"] < 1 and m4["sec_per_img"] > 0 and m4["infer_ram_gb"] > 0
m30 = next(m for m in st["models"] if "30B" in m["name"])
assert m30["f1_est"] > m4["f1_est"] and m30["infer_ram_gb"] > m4["infer_ram_gb"], "大档应质量更高、内存更大"
r = pc.switch_local_model("Qwen3-VL-8B-Instruct-4bit")
assert r["ok"] and pc.get_setting("local_model") == "Qwen3-VL-8B-Instruct-4bit"
assert le.status()["phase"] == "stopped", "切换后旧服务应停止释放内存"
try:
    pc.switch_local_model("no-such-model")
    raise AssertionError("未知模型应被拒绝")
except ValueError as e:
    assert "未知模型" in str(e)
print("OK G 模型切换: 设置落盘 + 停旧服务 + 未知模型拦截 + 对比元数据下发")

pc._conn.close()
le.stop_server()
shutil.rmtree(tmp, ignore_errors=True)
print("OK 本地引擎测试全部通过 (A-G)")
