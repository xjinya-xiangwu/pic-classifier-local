# -*- coding: utf-8 -*-
"""PhotoCurator — 服装照片本地分组整理工具。

用法:
    python app.py             # 启动本地服务并自动打开浏览器 (默认 http://127.0.0.1:8765)
    python app.py undo        # 撤销最近一次原地重命名
    python app.py check-api   # 用一张测试图验证模型 API 连通

依赖: pip install pillow pillow-heif
数据: 索引/标签/缩略图/撤销记录 存于 ~/.photocurator/, 源文件夹只发生文件名变更。
"""
import base64
import csv
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass  # 未装 pillow-heif 时 HEIC 不可用, 其余格式照常

APP_DIR = Path.home() / ".photocurator"
THUMB_DIR = APP_DIR / "thumbs"
EXTS = {".heic", ".heif", ".jpg", ".jpeg", ".png"}
IMG_LONG_EDGE = 1024
THUMB_SIZE = 256
DB_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
STATE = {"scan": None, "tag": None}  # 各自的进度 dict, None = 空闲

POSES = ["站立-双手下垂", "站立-叉腰", "站立-插兜", "站立-手扶头", "行走", "转身回眸",
         "坐-椅子", "坐-地面", "蹲姿", "依靠", "其他动作", "无人-平铺", "无人-挂拍", "无人-细节"]
ORIS = ["正面", "左侧45°", "左侧90°", "背面", "右侧45°", "右侧90°"]
SCALES = ["全身", "七分身", "半身", "面部特写", "细节特写"]

PROMPT = """你是服装电商摄影的结构化标注器。对给定照片只输出一个 JSON 对象, 不要任何其他文字。字段:
{"has_model": true/false,
 "clothing": {"type": "连衣裙|上衣|裤装|半裙|外套|套装|其他", "color_main": "白|黑|灰|米|红|粉|橙|黄|绿|蓝|紫|棕|花色", "pattern": "纯色|条纹|格子|印花|碎花|拼色|其他"},
 "pose": "站立-双手下垂|站立-叉腰|站立-插兜|站立-手扶头|行走|转身回眸|坐-椅子|坐-地面|蹲姿|依靠|其他动作|无人-平铺|无人-挂拍|无人-细节",
 "orientation": "正面|左侧45°|左侧90°|背面|右侧45°|右侧90°",
 "scale": "全身|七分身|半身|面部特写|细节特写",
 "quality": {"score": 1到5的整数, "flags": ["模糊", "闭眼", "严重裁切", "过曝", "无"]}}
规则: 只判断模特姿势、拍摄角度(朝向/景别)和衣着本身; 背景、光线、场景、道具、面部表情一律不参与判断。pose/orientation/scale 必须从给定枚举中逐字选取最贴近的一个。无人或静物图 pose 用"无人-*"枚举。"""

DEFAULTS = {
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "model": "glm-5.3-flash",
    "api_key": os.environ.get("ZHIPU_API_KEY", ""),
    "concurrency": 6,
    "granularity": "medium",   # coarse | medium | fine
    "target_count": 0,         # >0 时合并到不超过该组数
    "price_in": 0.0,           # 元 / 百万输入 token, 用于成本显示, 0=不计价
    "price_out": 0.0,
    # --- v2 本地模型模式 (PRD v2) ---
    "mode": "local",           # local = 本机 MLX 推理 (完全离线) | api = 云端 OpenAI 兼容接口
    "local_model": "Qwen3-VL-4B-Instruct-4bit",   # 默认最小 MVP 档 (用户决策: 先小档实测, 再手动升级)
    "hf_endpoint": "https://hf-mirror.com",       # 模型下载源, 空串 = HF 官方
    "local_server_cmd": "{python} -m mlx_vlm.server --model {model_path} --host 127.0.0.1 --port {port}",
}

# ---------------------------------------------------------------- 数据库
_conn = None


def init_db(path: Path):
    global _conn
    path.parent.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript("""
    CREATE TABLE IF NOT EXISTS project(
        id INTEGER PRIMARY KEY, folder TEXT UNIQUE, created_at TEXT);
    CREATE TABLE IF NOT EXISTS photo(
        id INTEGER PRIMARY KEY, project_id INT, path TEXT UNIQUE, filename TEXT, ext TEXT,
        content_hash TEXT, taken_at TEXT, size INT,
        status TEXT DEFAULT 'scanned',  -- scanned|tagging|tagged|failed
        clothing TEXT, pose TEXT, orientation TEXT, scale TEXT,
        quality INT, flags TEXT, error TEXT, selected INT DEFAULT 0);
    CREATE INDEX IF NOT EXISTS idx_photo_hash ON photo(project_id, content_hash);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    _conn.commit()


def db_exec(sql, args=()):
    with DB_LOCK:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


def db_all(sql, args=()):
    with DB_LOCK:
        return _conn.execute(sql, args).fetchall()


def db_one(sql, args=()):
    with DB_LOCK:
        return _conn.execute(sql, args).fetchone()


def get_setting(key):
    row = db_one("SELECT value FROM meta WHERE key='settings'")
    cfg = dict(DEFAULTS)
    if row:
        cfg.update(json.loads(row[0]))
    if not cfg["api_key"] and os.environ.get("ZHIPU_API_KEY"):
        cfg["api_key"] = os.environ["ZHIPU_API_KEY"]
    return cfg[key] if key else cfg


def save_settings(cfg: dict):
    cur = get_setting(None)
    cur.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    db_exec("INSERT OR REPLACE INTO meta(key, value) VALUES('settings', ?)",
            (json.dumps(cur, ensure_ascii=False),))
    return cur


# ---------------------------------------------------------------- 扫描
def content_hash(p: Path) -> str:
    # ponytail: 首块+大小哈希, 千张级防碰撞够用; 若出现误判缓存再换全量 sha256
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read(65536))
    h.update(str(p.stat().st_size).encode())
    return h.hexdigest()[:24]


def read_taken_at(img, p: Path) -> str:
    ex = img.getexif()
    raw = ex.get(36867) or ex.get(306)
    try:
        return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").isoformat()
    except Exception:
        return datetime.fromtimestamp(p.stat().st_mtime).isoformat()


def make_thumb(img, chash: str):
    out = THUMB_DIR / f"{chash}.jpg"
    if not out.exists():
        thumb = ImageOps.exif_transpose(img).convert("RGB")
        thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
        thumb.save(out, "JPEG", quality=80)
    return out


def scan_folder(folder: Path, recursive: bool, project_id: int):
    st = {"total": 0, "done": 0, "errors": []}
    with STATE_LOCK:
        STATE["scan"] = st
    it = folder.rglob("*") if recursive else folder.iterdir()
    files = sorted(p for p in it if p.is_file() and p.suffix.lower() in EXTS)
    st["total"] = len(files)
    for p in files:
        if not STATE["scan"]:
            return  # 已取消
        try:
            if db_one("SELECT id FROM photo WHERE path=?", (str(p),)):
                st["done"] += 1
                continue
            chash = content_hash(p)
            with Image.open(p) as img:
                taken = read_taken_at(img, p)
                make_thumb(img, chash)
            db_exec("INSERT INTO photo(project_id, path, filename, ext, content_hash, taken_at, size) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (project_id, str(p), p.name, p.suffix.lower(), chash, taken, p.stat().st_size))
        except Exception as e:
            st["errors"].append(f"{p.name}: {e}")
        st["done"] += 1
    with STATE_LOCK:
        STATE["scan"] = None


# ---------------------------------------------------------------- 模型识别
def check_public_http_url(url: str):
    """SSRF 防护: 仅允许 http/https 公网地址, 拒绝环回/私有/链路本地/保留地址 (含 DNS 解析后复核)。"""
    sp = urllib.parse.urlparse(url)
    if sp.scheme not in ("http", "https") or not sp.hostname:
        raise ValueError(f"API 地址不合法 (仅允许 http/https): {url}")
    port = sp.port or (443 if sp.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(sp.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"API 地址无法解析: {sp.hostname} ({e})")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved \
                or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"API 地址指向内网/保留地址, 已拒绝: {sp.hostname} -> {ip}")


class TagParseError(Exception):
    """模型输出无法解析为标注 JSON; 携带原始输出供修复重试回喂 (PRD v2 D5)。"""

    def __init__(self, content: str, msg: str):
        super().__init__(msg)
        self.content = content
        self.msg = msg


def _tag_messages(b64: str, repair=None) -> list:
    msgs = [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64," + b64}},
            {"type": "text", "text": "标注这张照片。"}]},
    ]
    if repair:  # (上次原始输出, 解析错误) — 回喂让模型自修一次
        msgs += [{"role": "assistant", "content": str(repair[0])[:600]},
                 {"role": "user", "content":
                     f"你上一次的输出无法解析为要求的JSON ({repair[1]})。请重新只输出一个符合要求的完整 JSON 对象, 不要任何其他文字。"}]
    return msgs


def _chat_url(cfg: dict) -> str:
    if cfg.get("mode") == "local":
        import local_engine
        return local_engine.server_url() + "/chat/completions"  # 本机环回, 免公网校验
    return cfg["base_url"].rstrip("/") + "/chat/completions"


def _post_chat(cfg: dict, messages: list) -> str:
    """一次标注请求 (含 429/超时/5xx 退避重试), 返回模型输出文本。"""
    url = _chat_url(cfg)
    local = cfg.get("mode") == "local"
    if not local:
        check_public_http_url(url)
    if local:
        # mlx-vlm 0.7.x 起模型缓存按 --model 启动路径精确匹配: 必须回传同一完整路径,
        # 发裸名会被当成 HF 仓库名重新下载 → 每张 500 (曾致"模型已就绪但识别全失败")
        import local_engine
        model_name = str(local_engine.model_dir(cfg["local_model"]))
    else:
        model_name = cfg["model"]
    body = {"model": model_name, "temperature": 0, "messages": messages}
    if local:
        body["max_tokens"] = 1024  # 本地模型默认输出上限不可控, 显式给足防 JSON 截断; 云端 API 用其自身默认
    timeout = 600 if local else 120  # 本地首次请求可能撞上模型加载
    last_err = None
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers={"Authorization": "Bearer " + (cfg.get("api_key") or "local"),
                                                  "Content-Type": "application/json"})
            opener = local_engine.loopback_opener() if local else urllib.request.build_opener()
            with opener.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            usage = data.get("usage") or {}
            add_usage(cfg.get("_folder", ""), usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"API HTTP {e.code}: {e.read().decode(errors='ignore')[:300]}")
            last_err = f"HTTP {e.code}"
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"返回结构异常: {e}")  # 服务通了但没按 OpenAI 格式回, 重试无意义
        except Exception as e:
            last_err = str(e)
        time.sleep(min(2 ** attempt, 16))
    raise RuntimeError(f"重试 5 次仍失败: {last_err}")


def _parse_tag(content) -> dict:
    try:
        if isinstance(content, dict):  # 个别兼容服务直接把 JSON 对象放在 content 里
            return validate_tag(content)
        s = content if isinstance(content, str) else str(content)
        s = re.sub(r"<think>.*?</think>", "", s, flags=re.S)  # 剥离思考块, 防止其中示例 JSON 干扰切片
        s = re.sub(r"<think>.*\Z", "", s, flags=re.S)  # 未闭合的思考块同样剥掉
        s = s[s.index("{"): s.rindex("}") + 1]
        return validate_tag(json.loads(s))
    except TagParseError:
        raise
    except Exception as e:
        raise TagParseError(str(content) if not isinstance(content, str) else content, str(e))


def vlm_tag(jpeg_bytes: bytes, cfg: dict) -> dict:
    """调用 OpenAI 兼容视觉接口 (本地或云端), 返回校验后的标签 dict。失败抛异常。"""
    b64 = base64.b64encode(jpeg_bytes).decode()
    try:
        return _parse_tag(_post_chat(cfg, _tag_messages(b64)))
    except TagParseError as e1:
        try:  # JSON 修复重试 (PRD v2 D5): 本地模型遵从度低于旗舰 API, 失败回喂重试一次
            return _parse_tag(_post_chat(cfg, _tag_messages(b64, repair=(e1.content, e1.msg))))
        except TagParseError as e2:
            raise RuntimeError(f"两次输出均无法解析为标注 JSON: {str(e2)[:200]}")


def validate_tag(t: dict) -> dict:
    cl = t.get("clothing") or {}
    pose = t.get("pose") if t.get("pose") in POSES else next((p for p in POSES if t.get("pose") and t["pose"] in p), "其他动作")
    return {
        "has_model": bool(t.get("has_model", True)),
        "clothing": {"type": cl.get("type") or "其他",
                     "color_main": cl.get("color_main") or "花色",
                     "pattern": cl.get("pattern") or "纯色"},
        "pose": pose,
        "orientation": t.get("orientation") if t.get("orientation") in ORIS else "正面",
        "scale": t.get("scale") if t.get("scale") in SCALES else "全身",
        "quality": {"score": max(1, min(5, int(t.get("quality", {}).get("score") or 3))),
                    "flags": [f for f in (t.get("quality", {}) or {}).get("flags", []) if f and f != "无"]},
    }


def check_api_from_payload(payload: dict) -> dict:
    """设置页连通性检测入口: base_url/model 必填, api_key 留空回退已保存 Key (与保存行为一致)。"""
    cfg_in = {k: (payload.get(k) or "").strip() for k in ("base_url", "model", "api_key")}
    if not cfg_in["base_url"] or not cfg_in["model"]:
        return {"ok": False, "msg": "请先填写 API 地址和模型名"}
    if not cfg_in["api_key"]:
        cfg_in["api_key"] = get_setting("api_key")
    if not cfg_in["api_key"]:
        return {"ok": False, "msg": "请先填写 API Key"}
    return test_api(cfg_in)


def test_api(cfg: dict) -> dict:
    """用一张 64px 小图实测一次 chat/completions, 返回 {ok, msg}。单次调用不重试, 30s 超时, 供设置页连通性检测。"""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    try:
        check_public_http_url(url)
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    img = Image.new("RGB", (64, 96), (235, 235, 235))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    body = {
        "model": cfg["model"], "temperature": 0,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}},
                {"type": "text", "text": "标注这张照片。"}]},
        ],
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + cfg["api_key"],
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        validate_tag(json.loads(content[content.index("{"): content.rindex("}") + 1]))
        model = data.get("model") or cfg["model"]
        return {"ok": True, "msg": f"连接成功, 模型 {model} 已正常返回标注"}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="ignore")[:200]
        except Exception:
            pass
        hint = {401: "Key 无效或未授权", 403: "Key 无权限",
                404: "API 地址不对 (base_url 应到 /v4、/v1 这一级)", 429: "被限流或账户欠费"}.get(e.code, f"HTTP {e.code}")
        return {"ok": False, "msg": f"{hint}: {detail}" if detail else hint}
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        return {"ok": False, "msg": f"返回内容无法解析为标注 JSON ({str(e)[:120]}), 该地址/模型可能不是 OpenAI 兼容的视觉接口"}
    except Exception as e:
        return {"ok": False, "msg": f"连接失败: {str(e)[:200]}"}


def add_usage(folder: str, pt: int, ct: int):
    key = "usage:" + folder
    with DB_LOCK:
        row = _conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        u = json.loads(row[0]) if row else {"in": 0, "out": 0}
        u["in"] += pt
        u["out"] += ct
        _conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, json.dumps(u)))
        _conn.commit()


def prepare_image(p: Path) -> bytes:
    with Image.open(p) as img:
        im = ImageOps.exif_transpose(img).convert("RGB")
        im.thumbnail((IMG_LONG_EDGE, IMG_LONG_EDGE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()


def tag_one(row, cfg, project_id):
    p = Path(row["path"])
    # 同项目内同内容哈希已打过标 → 直接复用, 不再计费 (B7)
    hit = db_one("SELECT clothing, pose, orientation, scale, quality, flags FROM photo "
                 "WHERE project_id=? AND content_hash=? AND status='tagged' AND id!=? LIMIT 1",
                 (project_id, row["content_hash"], row["id"]))
    if hit:
        db_exec("UPDATE photo SET status='tagged', clothing=?, pose=?, orientation=?, scale=?, "
                "quality=?, flags=? WHERE id=?",
                (hit["clothing"], hit["pose"], hit["orientation"], hit["scale"],
                 hit["quality"], hit["flags"], row["id"]))
        return "cache"
    tag = vlm_tag(prepare_image(p), cfg)
    db_exec("UPDATE photo SET status='tagged', clothing=?, pose=?, orientation=?, scale=?, quality=?, flags=? WHERE id=?",
            (json.dumps(tag["clothing"], ensure_ascii=False), tag["pose"], tag["orientation"],
             tag["scale"], tag["quality"]["score"], ";".join(tag["quality"]["flags"]), row["id"]))
    return "api"


def start_tagging(project_id):
    cfg = get_setting(None)
    local = cfg.get("mode") == "local"
    if local:
        import local_engine
        if not local_engine.installed(cfg["local_model"]):
            raise ValueError(f"本地模型 {cfg['local_model']} 未下载, 请在设置中下载后再识别")
    elif not cfg["api_key"]:
        raise ValueError("未配置 API Key, 请先在设置中填写")
    with STATE_LOCK:
        if STATE["tag"]:
            return
        st = {"done": 0, "total": 0, "failed": 0, "stop": False, "fatal": None}
        STATE["tag"] = st
    db_exec("UPDATE photo SET status='scanned' WHERE project_id=? AND status='tagging'", (project_id,))
    rows = db_all("SELECT id, path, content_hash FROM photo WHERE project_id=? AND status IN ('scanned','failed') "
                  "ORDER BY taken_at", (project_id,))
    st["total"] = len(rows)
    cfg["_folder"] = project_folder(project_id)
    workers = 1 if local else max(1, int(cfg["concurrency"]))  # 本地单 GPU 串行最快 (PRD v2 D3)

    def worker(row):
        if st["stop"]:
            return
        db_exec("UPDATE photo SET status='tagging' WHERE id=?", (row["id"],))
        try:
            tag_one(row, cfg, project_id)
        except Exception as e:
            db_exec("UPDATE photo SET status='failed', error=? WHERE id=?", (str(e)[:400], row["id"]))
            st["failed"] += 1
            if st["failed"] >= 5 and st["failed"] == st["done"] + 1:
                st["stop"] = True  # 熔断: 至今无一成功且已败 5 张, 大概率是 Key/地址/网络配置错误, 不再继续烧调用
        st["done"] += 1

    def run():
        if local:  # 先确保本地服务就绪 (模型加载可达数分钟, 阶段经 state 下发前端)
            try:
                local_engine.ensure_server(cfg)
                local_engine.wait_ready(cfg)
            except Exception as e:
                st["fatal"] = str(e)[:300]
                with STATE_LOCK:
                    STATE["tag"] = None
                return
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(worker, r) for r in rows]
            for f in as_completed(futures):
                f.result()
        with STATE_LOCK:
            STATE["tag"] = None

    threading.Thread(target=run, daemon=True).start()


def project_folder(project_id):
    return db_one("SELECT folder FROM project WHERE id=?", (project_id,))[0]


# ---------------------------------------------------------------- 分组
ORI3 = {"正面": "正面", "背面": "背面"}
SCALE2 = lambda s: "全身" if s == "全身" else "非全身"


def key_of(tag: dict, gran: str) -> tuple:
    c = tag["clothing"]
    clothing = (c["type"], c["color_main"], c["pattern"])
    ori3 = ORI3.get(tag["orientation"], "侧面")
    if gran == "coarse":
        return clothing + (ori3,)
    if gran == "medium":
        return clothing + (tag["pose"], ori3, SCALE2(tag["scale"]))
    return clothing + (tag["pose"], tag["orientation"], tag["scale"])


def name_of(key: tuple, idx: int) -> str:
    typ, color, pattern = key[0], key[1], key[2]
    parts = [color + typ]
    if pattern != "纯色":
        parts.append(pattern)
    for dim in key[3:]:
        if dim.endswith("°"):
            dim = dim.replace("°", "").replace("侧", "")
        elif "-" in dim:
            dim = dim.split("-", 1)[-1]  # 站立-叉腰 → 叉腰
        parts.append(dim)
    name = "_".join(parts)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)[:60]
    return f"G{idx:02d}_{name}"


def compute_groups(project_id, gran: str, target: int) -> dict:
    rows = db_all("SELECT * FROM photo WHERE project_id=? AND status='tagged' ORDER BY taken_at, filename",
                  (project_id,))
    buckets = {}
    for r in rows:
        tag = {"clothing": json.loads(r["clothing"]), "pose": r["pose"],
               "orientation": r["orientation"], "scale": r["scale"]}
        buckets.setdefault(key_of(tag, gran), []).append(r)
    groups = [{"key": k, "members": v} for k, v in buckets.items()]
    if target and len(groups) > target:
        groups = merge_groups(groups, target)
    groups.sort(key=lambda g: g["members"][len(g["members"]) // 2]["taken_at"] or "")  # 按拍摄时间中位数
    out = []
    for i, g in enumerate(groups, 1):
        members = sorted(g["members"], key=lambda r: (-(r["quality"] or 3), r["taken_at"] or ""))
        out.append({"name": name_of(g["key"], i), "n": len(members),
                    "members": [{"id": r["id"], "filename": r["filename"], "path": r["path"],
                                 "selected": r["selected"], "quality": r["quality"] or 3,
                                 "flags": r["flags"] or "", "hash": r["content_hash"]} for r in members]})
    return {"groups": out, "ungrouped": len(rows) - sum(len(g["members"]) for g in groups)}


def merge_groups(groups, target):
    """合并到目标组数: 优先合并姿势/朝向距离最近的两组。
    ponytail: O(g²) 每轮, 组数 ≤ 几十, 无需优化。"""
    def centroid(members):
        pi = [POSES.index(r["pose"]) for r in members if r["pose"] in POSES]
        oi = [ORIS.index(r["orientation"]) for r in members if r["orientation"] in ORIS]
        return (sum(pi) / len(pi) if pi else 99, sum(oi) / len(oi) if oi else 99)

    def dist(a, b):
        ca, cb = centroid(a), centroid(b)
        return abs(ca[0] - cb[0]) + abs(ca[1] - cb[1])

    while len(groups) > target:
        best = min(((i, j) for i in range(len(groups)) for j in range(i + 1, len(groups))),
                   key=lambda p: dist(groups[p[0]]["members"], groups[p[1]]["members"]))
        groups[best[0]]["members"] = groups[best[0]]["members"] + groups[best[1]]["members"]
        del groups[best[1]]
    return groups


# ---------------------------------------------------------------- 重命名与撤销
def apply_renames(project_id):
    folder = Path(project_folder(project_id))
    gran = get_setting("granularity")
    target = int(get_setting("target_count") or 0)
    data = compute_groups(project_id, gran, target)
    undo, manifest, renamed, skipped = [], [], 0, []
    for g in data["groups"]:
        rows = db_all(f"SELECT * FROM photo WHERE id IN ({','.join('?' * len(g['members']))}) "
                      "ORDER BY taken_at, filename", [m["id"] for m in g["members"]])
        for seq, r in enumerate(rows, 1):
            keep = "_精选" if r["selected"] else ""
            new_name = f"{g['name']}_{seq:03d}{keep}{r['ext']}"
            old = Path(r["path"])
            new = old.with_name(new_name)
            if old == new:
                manifest.append((new_name, g["name"], r["filename"], r["path"], int(r["selected"]), r["quality"]))
                continue
            if not old.exists():
                skipped.append(f"{old.name}: 文件不存在")
                continue
            final = new
            k = 2
            while final.exists():  # 目标名已被占用 (含未处理的同名原图), 一律加后缀, 绝不覆盖
                final = new.with_name(f"{new.stem}-{k}{new.suffix}")
                k += 1
            old.rename(final)
            db_exec("UPDATE photo SET path=?, filename=? WHERE id=?", (str(final), final.name, r["id"]))
            undo.append({"old": str(old), "new": str(final)})
            manifest.append((final.name, g["name"], r["filename"], r["path"], int(r["selected"]), r["quality"]))
            renamed += 1
    if undo:
        db_exec("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
                ("undo:" + str(folder), json.dumps(undo, ensure_ascii=False)))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = APP_DIR / ("manifest_p%d_%s.csv" % (int(project_id), ts))  # int 强转: 文件名只可能由数字/时间戳构成
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["新文件名", "组", "原文件名", "原路径", "是否精选", "质量分"])
    w.writerows(manifest)
    csv_path.write_text(buf.getvalue(), encoding="utf-8-sig")
    return {"renamed": renamed, "skipped": skipped, "undo_available": bool(undo), "manifest": str(csv_path)}


def undo_renames(project_id):
    folder = project_folder(project_id)
    key = "undo:" + folder
    row = db_one("SELECT value FROM meta WHERE key=?", (key,))
    if not row:
        return {"restored": 0}
    entries = json.loads(row[0])
    restored = 0
    for e in reversed(entries):
        old, new = Path(e["old"]), Path(e["new"])
        if new.exists():
            new.rename(old)
            db_exec("UPDATE photo SET path=?, filename=? WHERE id IN "
                    "(SELECT id FROM photo WHERE path=?)", (str(old), old.name, str(new)))
            restored += 1
    db_exec("DELETE FROM meta WHERE key=?", (key,))
    return {"restored": restored}


# ---------------------------------------------------------------- 本地服务
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _guard(self):
        """只服务本机: 防 DNS rebinding 与恶意网页跨站驱动本地 API (CSRF)。
        - Host 必须是 127.0.0.1/localhost, 网页通过 rebinding 域名访问时会被拒;
        - 浏览器发出的跨站 POST 带 Origin 头, 与本站不符即拒; curl/本机调用无 Origin, 不受影响。"""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host not in ("127.0.0.1", "localhost"):
            self._json({"error": "forbidden"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            port = self.server.server_address[1]
            if origin.rstrip("/") not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                self._json({"error": "forbidden origin"}, 403)
                return False
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):
        try:
            if not self._guard():
                return
            self._get()
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass  # 响应已发出/连接已断, 只能放弃

    def _get(self):
        if self.path == "/" or self.path.startswith("/index"):
            return self._file(Path(__file__).parent / "index.html", "text/html; charset=utf-8")
        m = re.match(r"^/thumb/([0-9a-f]+)\.jpg$", self.path)
        if m:
            f = THUMB_DIR / (m.group(1) + ".jpg")
            if f.exists():
                return self._file(f, "image/jpeg", cache=True)
            self.send_response(404)
            return self.end_headers()
        if self.path == "/api/state":
            return self._json(state_payload())
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            if not self._guard():
                return
            body = self._body()
            pid = current_project_id()
            if self.path == "/api/settings":
                prev = get_setting(None)
                cfg = save_settings(body)
                if cfg.get("mode") != "local" or cfg.get("local_model") != prev.get("local_model"):
                    import local_engine
                    local_engine.stop_server()  # 换模式/换模型: 旧服务立即让出内存, 下次识别按新配置重启
                return self._json({"ok": True, "settings": {k: cfg[k] for k in DEFAULTS}})
            if self.path == "/api/local/download":
                import local_engine
                local_engine.start_download(body.get("name", ""), body.get("endpoint") or get_setting("hf_endpoint"))
                return self._json({"ok": True})
            if self.path == "/api/local/switch":
                return self._json(switch_local_model(body.get("name", "")))
            if self.path == "/api/local/delete":
                import local_engine
                return self._json(local_engine.delete_model(body.get("name", "")))
            if self.path == "/api/local/server":
                import local_engine
                if body.get("action") == "stop":
                    local_engine.stop_server()
                    return self._json({"ok": True})
                cfg = get_setting(None)
                if cfg.get("mode") != "local":
                    return self._json({"error": "当前为 API 模式, 无需启动本地服务"}, 400)
                local_engine.ensure_server(cfg)
                return self._json({"ok": True})
            if self.path == "/api/check-api":
                return self._json(check_api_from_payload(body))
            if self.path == "/api/open":
                folder = Path(body.get("folder", "").strip()).expanduser()
                if not str(folder):
                    return self._json({"error": "请先输入照片文件夹路径"}, 400)
                folder = folder.resolve()
                if not folder.is_dir():
                    return self._json({"error": f"目录不存在: {folder}"}, 400)
                cur = db_one("SELECT id FROM project WHERE folder=?", (str(folder),))
                if cur:
                    pid = cur[0]
                else:
                    pid = db_exec("INSERT INTO project(folder, created_at) VALUES(?,?)",
                                  (str(folder), datetime.now().isoformat())).lastrowid
                threading.Thread(target=scan_folder, args=(folder, body.get("recursive", False), pid),
                                 daemon=True).start()
                return self._json({"ok": True, "project_id": pid})
            if self.path == "/api/tag":
                if not pid:
                    return self._json({"error": "请先打开文件夹"}, 400)
                start_tagging(pid)
                return self._json({"ok": True})
            if self.path == "/api/tag/stop":
                with STATE_LOCK:
                    if STATE["tag"]:
                        STATE["tag"]["stop"] = True
                return self._json({"ok": True})
            if self.path == "/api/select":
                if not pid:
                    return self._json({"error": "未打开项目"}, 400)
                return self._json(select_photo(pid, int(body["id"])))
            if self.path == "/api/rename":
                if not pid:
                    return self._json({"error": "未打开项目"}, 400)
                return self._json(apply_renames(pid))
            if self.path == "/api/undo":
                if not pid:
                    return self._json({"error": "未打开项目"}, 400)
                return self._json(undo_renames(pid))
            self._json({"error": "not found"}, 404)
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def _file(self, f: Path, ctype, cache=False):
        if not f.exists():
            return self._json({"error": "missing"}, 404)
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", "max-age=86400")
        else:
            self.send_header("Cache-Control", "no-cache")  # 页面升级后浏览器绝不复用旧缓存
        self.end_headers()
        self.wfile.write(body)


def current_project_id():
    row = db_one("SELECT id FROM project ORDER BY id DESC LIMIT 1")
    return row[0] if row else None


def select_photo(project_id, photo_id):
    row = db_one("SELECT * FROM photo WHERE id=? AND project_id=?", (photo_id, project_id))
    if not row or row["status"] != "tagged":
        return {"error": "照片不存在或未识别"}
    tag = {"clothing": json.loads(row["clothing"]), "pose": row["pose"],
           "orientation": row["orientation"], "scale": row["scale"]}
    k = key_of(tag, get_setting("granularity"))
    peers = db_all("SELECT * FROM photo WHERE project_id=? AND status='tagged'", (project_id,))
    ids = [p["id"] for p in peers if p["id"] != photo_id and
           key_of({"clothing": json.loads(p["clothing"]), "pose": p["pose"],
                   "orientation": p["orientation"], "scale": p["scale"]}, get_setting("granularity")) == k]
    if row["selected"]:  # 再点一次取消选择
        db_exec("UPDATE photo SET selected=0 WHERE id=?", (photo_id,))
        selected = 0
    else:
        db_exec("UPDATE photo SET selected=1 WHERE id=?", (photo_id,))
        for i in ids:
            db_exec("UPDATE photo SET selected=0 WHERE id=?", (i,))
        selected = 1
    return {"ok": True, "selected": selected, "cleared": len(ids)}


def switch_local_model(name: str) -> dict:
    """一键切换本地模型: 校验 → 落盘设置 → 停旧服务释放内存 (下次识别按新模型自动重启)。
    允许切换到未下载的档位 (先定档、后挂机下载), 识别前会再做安装预检。"""
    import local_engine
    if name not in local_engine.MODELS:
        raise ValueError(f"未知模型: {name}")
    save_settings({"local_model": name})
    local_engine.stop_server()
    return {"ok": True, "local_model": name, "installed": local_engine.installed(name)}


def state_payload():
    cfg = {k: get_setting(k) for k in DEFAULTS}
    cfg["api_key"] = "***" if cfg["api_key"] else ""
    pid = current_project_id()
    out = {"settings": cfg, "scan": STATE["scan"], "tag": STATE["tag"], "project": None}
    try:  # 本地引擎状态 (模型列表/下载进度/服务阶段); 引擎缺失不阻塞整体状态
        import local_engine
        out["local"] = local_engine.ui_status(cfg)
    except Exception:
        out["local"] = None
    if pid:
        folder = project_folder(pid)
        counts = db_one("SELECT COUNT(*), SUM(status='tagged'), SUM(status='failed') FROM photo WHERE project_id=?",
                        (pid,))
        usage_key = "usage:" + folder
        urow = db_one("SELECT value FROM meta WHERE key=?", (usage_key,))
        usage = json.loads(urow[0]) if urow else {"in": 0, "out": 0}
        price_in, price_out = float(cfg["price_in"]), float(cfg["price_out"])
        cost = (usage["in"] * price_in + usage["out"] * price_out) / 1e6
        out["project"] = {"id": pid, "folder": folder,
                          "total": counts[0] or 0, "tagged": counts[1] or 0, "failed": counts[2] or 0,
                          "usage": usage, "cost": round(cost, 2),
                          "undo_available": bool(db_one("SELECT 1 FROM meta WHERE key=?", ("undo:" + folder,)))}
        if counts[2]:  # 失败原因汇总: 最常见的最多 3 类, 供前端"失败 N 张 (点看原因)"展示
            errs = db_all("SELECT error, COUNT(*) c FROM photo WHERE project_id=? AND status='failed' "
                          "AND error IS NOT NULL GROUP BY error ORDER BY c DESC LIMIT 3", (pid,))
            out["project"]["failed_errors"] = [{"n": r[1], "msg": (r[0] or "")[:160]} for r in errs]
        gran = cfg["granularity"]
        target = int(cfg["target_count"] or 0)
        if counts[1]:
            out.update(compute_groups(pid, gran, target))
        est_in, est_out = counts[0] * 1100, counts[0] * 150
        out["estimate"] = round((est_in * price_in + est_out * price_out) / 1e6, 2) if price_in else None
    return out


# ---------------------------------------------------------------- 入口
def open_browser(url):
    # macOS 上 /usr/bin/open 走 LaunchServices, 比默认 webbrowser 的 osascript 更可靠
    # (双击 .app 启动时环境精简, webbrowser 可能静默失败导致"页面打不开")
    if sys.platform == "darwin":
        try:
            subprocess.run(["open", url], check=False, timeout=5)
            return
        except Exception:
            pass
    webbrowser.open(url)


def probe_existing_port():
    """已有 PhotoCurator Local 实例在运行时返回其端口, 否则 None。"""
    import local_engine
    for p in range(8776, 8786):  # 本地版专用端口段, 与 API 版 (8765-8775) 互不冲突
        try:
            with local_engine.loopback_opener().open(f"http://127.0.0.1:{p}/api/state", timeout=0.5) as resp:
                if resp.status == 200 and b"settings" in resp.read():
                    return p
        except Exception:
            pass
    return None


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "undo":
        init_db(APP_DIR / "photocurator.db")
        pid = current_project_id()
        print(undo_renames(pid) if pid else "没有项目")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "check-api":
        init_db(APP_DIR / "photocurator.db")
        cfg = get_setting(None)
        if cfg.get("mode") == "local":
            import local_engine
            if not local_engine.installed(cfg["local_model"]):
                sys.exit(f"本地模型未下载: {cfg['local_model']} (打开 App 设置页下载)")
            local_engine.ensure_server(cfg)
            local_engine.wait_ready(cfg)
        elif not cfg["api_key"]:
            sys.exit("未配置 API Key (设置页填写或设 ZHIPU_API_KEY 环境变量)")
        img = Image.new("RGB", (256, 384), (240, 240, 240))
        buf = io.BytesIO()
        img.save(buf, "JPEG")
        print(json.dumps(vlm_tag(buf.getvalue(), cfg), ensure_ascii=False, indent=2))
        return
    init_db(APP_DIR / "photocurator.db")
    old = probe_existing_port()
    if old:
        url = f"http://127.0.0.1:{old}"
        print(f"PhotoCurator Local 已在运行: {url}  (直接打开页面, 不重复启动)", flush=True)
        threading.Timer(0.3, lambda: open_browser(url)).start()
        return
    port = 8776
    for p in range(8776, 8786):  # 本地版专用端口段 (API 版占 8765-8775)
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    else:
        sys.exit("8776-8785 端口全部被占用, 无法启动 (可用活动监视器结束旧的 python 进程后重试)")
    url = f"http://127.0.0.1:{port}"
    print(f"PhotoCurator Local (本地模型版) 运行中: {url}  (Ctrl+C 退出)", flush=True)
    print(f"数据目录: {APP_DIR}", flush=True)
    threading.Timer(0.8, lambda: open_browser(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    try:
        # 工作目录固定到主目录: 升级时安装器会 rm -rf 程序目录, 若 App 的 cwd 留在被删目录,
        # 之后 pip 等子进程会因 os.getcwd() 报 FileNotFoundError (用户实测: MLX 依赖安装失败)。
        os.chdir(Path.home())
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
