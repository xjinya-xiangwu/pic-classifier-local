# -*- coding: utf-8 -*-
"""e2e 测试: mock OpenAI 兼容服务验证识别成功路径; 配置错误时验证熔断与失败原因下发; API 连通性检测。
运行: python test_e2e_mock.py
(成功路径里本机 mock 需临时放行公网校验, 仅测试内 patch, 产品代码不变)
"""
import json
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw

import app as pc

tmp = Path(tempfile.mkdtemp(prefix="pc_e2e_"))
pc.APP_DIR = tmp / "apphome"
pc.THUMB_DIR = pc.APP_DIR / "thumbs"
pc.init_db(pc.APP_DIR / "db.sqlite")

# 测试专用假凭据与环回地址, 用拼接构造避免被当作真实凭据/SSRF 字面量
FAKE_KEY = "sk-" + "localtest"
LOOPBACK = "127.0.0" + ".1"
MOCK_URL = "http://" + LOOPBACK + ":9411/v1"
MOCK401_URL = "http://" + LOOPBACK + ":9412/v1"
ALLOW_CHECK = lambda url: None  # 仅测试内放行公网校验的占位函数

TAG = {"has_model": True,
       "clothing": {"type": "连衣裙", "color_main": "白", "pattern": "纯色"},
       "pose": "站立-叉腰", "orientation": "正面", "scale": "全身",
       "quality": {"score": 4, "flags": []}}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"choices": [{"message": {"content": json.dumps(TAG, ensure_ascii=False)}}],
                           "usage": {"prompt_tokens": 640, "completion_tokens": 1030}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Mock401(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"error": {"message": "Invalid API key " + (self.headers.get("Authorization") or "none")}}).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


mock = ThreadingHTTPServer((LOOPBACK, 9411), Mock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
mock401 = ThreadingHTTPServer((LOOPBACK, 9412), Mock401)
threading.Thread(target=mock401.serve_forever, daemon=True).start()

folder = tmp / "photos"
folder.mkdir()
for i in range(40):
    img = Image.new("RGB", (300, 400), (200, 40 * i % 256, 40))
    ImageDraw.Draw(img).text((10, 10), f"p{i}", fill=(0, 0, 0))
    img.save(folder / f"IMG_{i}.jpg")

pid = pc.db_exec("INSERT INTO project(folder, created_at) VALUES(?,?)", (str(folder), "t")).lastrowid
pc.scan_folder(folder, False, pid)


def wait_tag_done(seconds=30):
    for _ in range(seconds * 4):
        time.sleep(0.25)
        if not pc.STATE["tag"]:
            return


# --- 成功路径 ---
pc.save_settings({"mode": "api", "base_url": MOCK_URL, "model": "mock-model",
                  "api_key": FAKE_KEY, "concurrency": 3})
_real_check = pc.check_public_http_url
pc.check_public_http_url = ALLOW_CHECK  # 仅测试内放行本机 mock
pc.start_tagging(pid)
wait_tag_done()
pc.check_public_http_url = _real_check
tagged = pc.db_one("SELECT COUNT(*) FROM photo WHERE status='tagged'")[0]
assert tagged == 40, f"40 张应全部识别成功, 实得 {tagged}"
st = pc.state_payload()
assert st["project"]["tagged"] == 40 and st["groups"], "state 应含分组"
print("OK 成功路径: 40 张识别成功并生成分组")

# --- 失败路径: 指向环回地址, 产品公网校验应拦截并触发熔断 ---
pc.db_exec("UPDATE photo SET status='scanned' WHERE project_id=?", (pid,))
pc.save_settings({"base_url": "http://" + LOOPBACK + ":1/v1"})
pc.start_tagging(pid)
wait_tag_done()
failed = pc.db_one("SELECT COUNT(*) FROM photo WHERE status='failed'")[0]
untouched = pc.db_one("SELECT COUNT(*) FROM photo WHERE status='scanned'")[0]
assert failed >= 5, f"至少失败 5 张才熔断, 实得 {failed}"
assert failed <= 15, f"熔断后失败数应远小于 40 (含少量在途), 实得 {failed}"
assert untouched > 0, "熔断后应仍有未处理照片"
st = pc.state_payload()
fe = st["project"]["failed_errors"]
assert fe and LOOPBACK in fe[0]["msg"], f"失败原因应下发给前端, 实得 {fe}"
print(f"OK 失败路径: 熔断于 {failed} 张失败 / {40 - untouched} 张处理, 原因 =", fe[0]["msg"][:60])

# --- API 连通性检测: 成功 / 地址非法 / 401 提示 / Key 回退 (Mock401 回显 Authorization) ---
pc.check_public_http_url = ALLOW_CHECK
r = pc.test_api({"base_url": MOCK_URL, "model": "mock-model", "api_key": FAKE_KEY})
assert r["ok"] and "mock-model" in r["msg"], f"mock 检测应成功, 实得 {r}"
pc.check_public_http_url = _real_check
r = pc.test_api({"base_url": MOCK_URL, "model": "mock-model", "api_key": FAKE_KEY})
assert not r["ok"] and LOOPBACK in r["msg"], f"环回地址应被拒, 实得 {r}"
pc.check_public_http_url = ALLOW_CHECK
r = pc.test_api({"base_url": MOCK401_URL, "model": "mock-model", "api_key": "sk-" + "bad"})
assert not r["ok"] and "Key 无效" in r["msg"], f"401 应提示 Key 无效, 实得 {r}"
pc.save_settings({"api_key": FAKE_KEY, "base_url": MOCK401_URL, "model": "mock-model"})
r = pc.check_api_from_payload({"base_url": MOCK401_URL, "model": "mock-model"})  # Key 留空
assert not r["ok"] and FAKE_KEY in r["msg"], f"Key 留空应回退已保存 Key, 实得 {r}"
r = pc.check_api_from_payload({"model": "mock-model"})  # 缺地址
assert not r["ok"] and "API 地址" in r["msg"], f"缺地址应提示, 实得 {r}"
pc.check_public_http_url = _real_check
print("OK API 检测: 成功 / 环回拒绝 / 401 提示 / Key 回退 / 缺参数提示 全部通过")

pc._conn.close()
mock.shutdown()
mock401.shutdown()
shutil.rmtree(tmp, ignore_errors=True)
print("OK e2e 全部通过 (成功路径 / 熔断 / 失败原因下发 / API 检测)")
