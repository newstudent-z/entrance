#!/usr/bin/env python3
# 错题本 v2 — 以「存下来、随手翻、总结知识点」为核心，不搞强制复习打卡
# 零依赖：Python 标准库 + SQLite。重点科目：数学一（兼顾 408 / 英语一 / 政治）
import base64, hashlib, json, os, re, secrets, shutil, sqlite3, time, urllib.request, urllib.error
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# AI 服务商（均为 OpenAI 兼容接口）。2026-10 核实：DeepSeek 当前 deepseek-flash(V4.1 Flash) 原生支持看图；
# deepseek-v4-pro 不支持视觉；旧的 deepseek-chat / deepseek-reasoner 已于 2026-07 退役，不要再用。
AI_PROVIDERS = {
    "deepseek": {"base": "https://api.deepseek.com", "model": "deepseek-flash", "vision": True, "label": "DeepSeek V4.1 Flash（能看题图）"},
    "qwen": {"base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-vl-max", "vision": True, "label": "通义千问 VL（能看题图）"},
}
AI_SYSTEM = ("你是一位耐心的考研数学辅导老师。基于给出的题目（可能含图片）和已有解析进行讲解："
             "先指出考点，再分步讲清思路与关键步骤，最后点出易错点。若已有解析与你的想法冲突，以已有解析为准并说明原因。"
             "用中文回答，公式用 $...$ 包裹，分点清晰、讲人话，不要空话套话。学生追问时结合上文回答。")

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("CUOTI_DB", os.path.join(BASE, "data", "cuoti.db"))
UPLOAD_DIR = os.environ.get("CUOTI_UPLOAD", os.path.join(BASE, "uploads"))
PORT = int(os.environ.get("CUOTI_PORT", "18080"))
PASSWORD = os.environ.get("CUOTI_PASSWORD", "")
SETUP_CODE = os.environ.get("CUOTI_SETUP_CODE", "")  # 首次设置密码用的一次性设置码（部署时生成，给用户本人）
os.makedirs(os.path.dirname(DB), exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
SESSIONS = {}

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS items(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT NOT NULL, chapter TEXT DEFAULT '', knowledge TEXT DEFAULT '',
          title TEXT NOT NULL, source TEXT DEFAULT '', my_answer TEXT DEFAULT '',
          answer TEXT DEFAULT '', note TEXT DEFAULT '', reason TEXT DEFAULT '',
          difficulty INTEGER DEFAULT 3, image TEXT DEFAULT '', answer_image TEXT DEFAULT '',
          status TEXT DEFAULT 'open', pinned INTEGER DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT DEFAULT '')""")
        cols = [r["name"] for r in c.execute("PRAGMA table_info(items)")]
        if "answer_image" not in cols:  # 老库迁移：补答案图片字段，不动已有数据
            c.execute("ALTER TABLE items ADD COLUMN answer_image TEXT DEFAULT ''")
        c.execute("""CREATE TABLE IF NOT EXISTS knowledge_notes(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          subject TEXT DEFAULT '', knowledge TEXT UNIQUE NOT NULL,
          summary TEXT DEFAULT '', updated_at TEXT DEFAULT '')""")
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT DEFAULT '')")
        c.execute("""CREATE TABLE IF NOT EXISTS ai_chats(
          id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
          role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)""")

def get_setting(k, default=""):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        return r["value"] if r and r["value"] else default

def put_setting(k, v):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))

def ai_config():
    prov = get_setting("ai_provider", "deepseek")
    if prov not in AI_PROVIDERS: prov = "deepseek"
    return prov, get_setting("ai_key", ""), get_setting("ai_model", "") or AI_PROVIDERS[prov]["model"]

def ai_call(messages):
    prov, key, model = ai_config()
    if not key:
        raise RuntimeError("NO_KEY")
    cfg = AI_PROVIDERS[prov]
    payload = json.dumps({"model": model, "messages": messages, "temperature": 0.3}).encode()
    req = urllib.request.Request(cfg["base"] + "/chat/completions", data=payload, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
            data = json.loads(r.read())
        msg = data["choices"][0]["message"]
        return msg.get("content") or msg.get("reasoning_content") or "（AI 这次没返回内容，再问一次试试）"
    except urllib.error.HTTPError as e:
        try: msg = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception: msg = ""
        raise RuntimeError(f"AI 服务返回 {e.code}：{msg or '请求失败'}")
    except Exception as e:
        raise RuntimeError(f"连不上 AI 服务：{e}")

def stored_hash():
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone()
        return r["value"] if r else ""

def set_password(pw):
    salt = secrets.token_hex(8)
    h = hashlib.sha256((salt + pw).encode()).hexdigest()
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES('password_hash',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (salt + ":" + h,))

def verify_password(pw):
    if PASSWORD:
        return pw == PASSWORD
    sh = stored_hash()
    if not sh:
        return False
    salt, h = sh.split(":", 1)
    return hashlib.sha256((salt + (pw or "")).encode()).hexdigest() == h

def auth_mode():
    if PASSWORD: return "env"
    return "db" if stored_hash() else "setup"

def today(): return date.today().isoformat()

HTML = r"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>我的错题本 · 数学为主</title><style>
*{box-sizing:border-box}body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f6f7f9;color:#222}
header{background:#1f3a5f;color:#fff;padding:14px 16px;position:sticky;top:0;z-index:9}
header b{font-size:19px}header span{opacity:.8;font-size:13px;margin-left:8px}
nav{display:flex;gap:8px;padding:10px 16px;flex-wrap:wrap}
nav button{border:1px solid #ccc;background:#fff;border-radius:20px;padding:7px 16px;cursor:pointer;font-size:14px}
nav button.on{background:#1f3a5f;color:#fff;border-color:#1f3a5f}
main{max-width:880px;margin:0 auto;padding:6px 16px 40px}
.card{background:#fff;border-radius:12px;padding:14px 16px;margin:10px 0;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.card h3{margin:6px 0 8px;font-size:16px}
.meta{color:#666;font-size:12px;line-height:1.8}
.pill{display:inline-block;background:#eef2f7;border-radius:10px;padding:1px 9px;margin:2px 4px 2px 0;font-size:12px}
.pill.k{background:#e7f0e4;color:#2e5d2e}.pill.r{background:#fdecec;color:#b3261e}.pill.open{background:#fff3cd}.pill.done{background:#d1e7dd}
img.q{max-width:min(100%,540px);max-height:360px;width:auto;height:auto;object-fit:contain;display:block;margin:8px auto;border:1px solid #e3e8ee;border-radius:8px;background:#fff;cursor:zoom-in}
details{margin-top:6px}summary{cursor:pointer;color:#1f3a5f;font-size:14px}
.actions{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.actions button{border:1px solid #ddd;background:#fafafa;border-radius:8px;padding:7px 13px;cursor:pointer;font-size:13px}
input,select,textarea{width:100%;padding:9px;border:1px solid #ccc;border-radius:8px;margin:5px 0 10px;font-size:15px}
label{font-size:13px;color:#555}.row{display:flex;gap:10px}.row>div{flex:1}
.primary{background:#1f3a5f;color:#fff;border:0;width:100%;padding:12px;border-radius:8px;font-size:16px;cursor:pointer}
.stat{display:flex;gap:10px;flex-wrap:wrap}.stat div{flex:1;min-width:100px;background:#fff;border-radius:12px;padding:14px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.stat b{font-size:24px;display:block}
.empty{color:#888;text-align:center;padding:28px}
.note{background:#fffbe6;border-radius:8px;padding:9px 12px;margin-top:8px;font-size:14px;white-space:pre-wrap}
.bar{height:9px;background:#eef2f7;border-radius:5px;overflow:hidden}.bar i{display:block;height:100%;background:#1f3a5f}
.srow{display:flex;align-items:center;gap:10px;margin:9px 0;font-size:14px}
.srow .sl{flex:0 1 44%;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#33475c}
.srow .st{flex:1;background:#edf1f6;border-radius:6px;height:10px;overflow:hidden;display:block}
.srow .st i{display:block;height:100%;border-radius:6px;background:#2e6fb7}
.srow .st i.rsn{background:#e67e22}
.srow .st i.wk{background:#d35450}
.srow .sv{flex:none;min-width:28px;text-align:right;font-weight:700;color:#5b6b7d}
.wkrow{padding:9px 2px;border-bottom:1px dashed #e6ecf2;cursor:pointer}
.wkrow:last-child{border-bottom:0}
.wkrow:active{background:#f4f8fc}
@media (max-width:640px){.row{flex-direction:column}.row>div{width:100%}input,select,textarea{min-height:46px;font-size:16px}}
#a-imgbtns button,#a-aimgbtns button{border:1px solid #ddd;background:#fafafa;border-radius:8px;padding:9px 13px;cursor:pointer;font-size:14px}
.sec{font-weight:700;font-size:15px;color:#1f3a5f;border-left:4px solid #1f3a5f;padding-left:9px;margin:18px 0 6px}
.uploader{display:flex;flex-direction:column;align-items:center;gap:3px;border:2px dashed #aebfd2;border-radius:14px;padding:22px 12px;background:linear-gradient(180deg,#f7fafd,#eef4fa);cursor:pointer;text-align:center;margin:5px 0 10px;transition:all .15s}
.uploader:active{transform:scale(.99);border-color:#1f3a5f;background:#e6eff8}
.uploader .u-ico{font-size:32px;line-height:1.2}
.uploader .u-main{font-weight:700;color:#1f3a5f;font-size:15px}
.uploader .u-sub{font-size:12px;color:#7a8794;font-weight:400}
.uploader.compact{padding:10px}.uploader.compact .u-ico{font-size:20px}.uploader.compact .u-main{font-size:13px}
.u-btns{display:flex;gap:10px;margin-top:6px}
.u-btns button{border:0;border-radius:22px;padding:10px 22px;font-size:15px;cursor:pointer;background:#1f3a5f;color:#fff}
.u-btns button.gal{background:#fff;color:#1f3a5f;border:1.5px solid #1f3a5f}
.uploader.compact .u-btns button{padding:7px 16px;font-size:13px}
.ablock{background:#eef7ee;border:1px solid #cfe3cf;border-radius:10px;padding:10px 12px;margin-top:10px}
.ablock-h{font-weight:700;color:#2e5d2e;margin-bottom:5px}
.ablock.empty-a{background:#fafafa;border:1px dashed #bbb;color:#888;font-weight:400}
.edbtn{border:0;border-radius:9px;padding:12px 20px;font-size:15px;cursor:pointer;background:#fff;color:#222}
.aichip{border:1px solid #c4d2e2;background:#fff;color:#1f3a5f;border-radius:16px;padding:6px 12px;font-size:13px;cursor:pointer}
.aib{max-width:88%;padding:10px 13px;border-radius:13px;margin:8px 0;font-size:14.5px;line-height:1.65;white-space:normal;word-break:break-word}
.aib.u{background:#1f3a5f;color:#fff;margin-left:auto;border-bottom-right-radius:4px}
.aib.a{background:#fff;border:1px solid #e1e7ee;margin-right:auto;border-bottom-left-radius:4px}
.aib .aih{font-weight:700;margin:6px 0 2px}
.aib code{background:#eef2f7;border-radius:4px;padding:0 4px;font-size:13px}
.math{font-family:Georgia,'Times New Roman','Songti SC',serif;font-size:1.04em}
.mathblk{display:block;text-align:center;padding:10px 6px;margin:5px 0;overflow-x:auto;background:#fbfcfe;border:1px solid #edf1f6;border-radius:9px;font-size:16.5px;line-height:1.5}
.frac{display:inline-flex;flex-direction:column;text-align:center;vertical-align:middle;margin:0 .12em}
.frac>.num{border-bottom:1.6px solid currentColor;padding:0 .28em;font-size:.8em;line-height:1.3}
.frac>.den{padding:0 .28em;font-size:.8em;line-height:1.3}
.op{display:inline-flex;flex-direction:column;align-items:center;vertical-align:middle;line-height:1;margin:0 .04em}
.op>.opsym{font-size:1.28em;line-height:1.04}
.op>.oplim{font-size:.66em;line-height:1.15}
.op.lim>.opsym{font-size:.9em}
.sqrt{border-top:1.6px solid currentColor;padding:0 .12em}
#ai-log{min-height:0;overscroll-behavior:contain;-webkit-overflow-scrolling:touch}
#ai-modal{overscroll-behavior:none}
.edbtn.go{background:#2e7d32;color:#fff}
#login{max-width:360px;margin:60px auto}
</style></head><body>
<header><b>📕 我的错题本</b><span>存下来 · 随手翻 · 攒知识点</span></header>
<div id="login" class="card" style="display:none"><h3>登录</h3><input type="password" id="pw" placeholder="密码"><button class="primary" onclick="login()">进入</button></div>
<div id="setup" class="card" style="display:none;max-width:420px;margin:60px auto"><h3>首次设置</h3><p class="meta">输入部署时给你的「设置码」，再设一个你自己的登录密码（至少 6 位）。密码只存哈希，设完设置码即失效。</p><input id="s-code" placeholder="设置码"><input type="password" id="s-pw" placeholder="设置登录密码"><button class="primary" onclick="setupPw()">设置并进入</button></div>
<div id="app" style="display:none">
<nav>
<button data-t="lib" class="on">📚 错题库</button>
<button data-t="know">🧩 知识点总结</button>
<button data-t="add">➕ 存一道错题</button>
<button data-t="stats">📊 薄弱点</button>
<button data-t="ai">⚙️ 设置</button>
<button onclick="randomPick()">🎲 随机翻几道</button>
<button onclick="location='api/export'">⬇ 导出备份</button>
<button onclick="changePw()">🔑 改密码</button>
</nav><main>
<section id="p-lib">
<div class="card">
<div class="row"><div><label>科目</label><select id="f-sub"><option value="">全部</option></select></div>
<div><label>板块</label><select id="f-ch"><option value="">全部</option></select></div>
<div><label>状态</label><select id="f-st"><option value="">全部</option><option value="open">还没弄懂</option><option value="done">已弄懂</option></select></div></div>
<input id="f-q" placeholder="🔍 搜题目 / 知识点 / 来源 / 我的总结…">
</div>
<div id="libList"></div></section>
<section id="p-know" style="display:none"><div class="meta" style="padding:4px 2px">按出错次数排序——排前面的就是你的薄弱点。每个知识点下面可以写自己的总结：公式、结论、易错提醒。</div><div id="knowList"></div></section>
<section id="p-add" style="display:none"><div class="card">
<input type="hidden" id="a-id">
<div class="row"><div><label>科目</label><select id="a-sub"></select></div>
<div><label>板块</label><select id="a-ch"></select></div></div>
<label>知识点（关键！如：洛必达法则 / 矩阵的秩 / 条件概率）</label><input id="a-know" list="knowDatalist"><datalist id="knowDatalist"></datalist>
<div class="sec">📖 题目</div>
<label>题目标题 / 题干文字</label><textarea id="a-title" rows="3"></textarea>
<label>题目照片</label><div class="uploader" id="up-q"><span class="u-ico">📷</span><span class="u-main">添加题目照片</span><div class="u-btns"><button type="button" onclick="document.getElementById('a-img-cam').click()">📸 拍照</button><button type="button" class="gal" onclick="document.getElementById('a-img-gal').click()">🖼 图库选择</button></div><span class="u-sub">选完可以裁剪，只留下题目本身</span></div><input type="file" id="a-img-cam" accept="image/*" capture="environment" hidden><input type="file" id="a-img-gal" accept="image/*" hidden><div id="a-imgprev"></div>
<div id="a-imgbtns" style="display:none;gap:8px;margin:2px 0 10px;flex-wrap:wrap">
<button type="button" onclick="edOpenCurrentQ()">✂️ 裁剪 / 旋转题目图</button>
<button type="button" onclick="document.getElementById('a-img-cam').click()">📸 重拍</button>
<button type="button" onclick="document.getElementById('a-img-gal').click()">🖼 换图库图</button>
<button type="button" onclick="removePhotoQ()" style="color:#b3261e">🗑 删除照片</button>
</div>
<div class="sec">✍️ 我当时的作答</div>
<label>我当时怎么写的、错在哪一步</label><textarea id="a-my" rows="2"></textarea>
<div class="sec">✅ 正确答案 / 解析</div>
<label>答案照片</label><div class="uploader" id="up-a"><span class="u-ico">📝</span><span class="u-main">添加答案照片</span><div class="u-btns"><button type="button" onclick="document.getElementById('a-aimg-cam').click()">📸 拍照</button><button type="button" class="gal" onclick="document.getElementById('a-aimg-gal').click()">🖼 图库选择</button></div><span class="u-sub">拍答案册或解析页，同样可以裁剪</span></div><input type="file" id="a-aimg-cam" accept="image/*" capture="environment" hidden><input type="file" id="a-aimg-gal" accept="image/*" hidden><div id="a-aimgprev"></div>
<div id="a-aimgbtns" style="display:none;gap:8px;margin:2px 0 10px;flex-wrap:wrap">
<button type="button" onclick="edOpenCurrentA()">✂️ 裁剪 / 旋转答案图</button>
<button type="button" onclick="document.getElementById('a-aimg-cam').click()">📸 重拍</button>
<button type="button" onclick="document.getElementById('a-aimg-gal').click()">🖼 换图库图</button>
<button type="button" onclick="removePhotoA()" style="color:#b3261e">🗑 删除照片</button>
</div>
<label>答案 / 解析文字</label><textarea id="a-ans" rows="3" placeholder="最终答案 + 关键步骤，方便以后快速对照"></textarea>
<div class="sec">🧩 归纳</div>
<label>来源</label><input id="a-src" list="srcList" placeholder="如：880题 P120 / 23年真题第18题"><datalist id="srcList"><option value="历年真题"></option><option value="880题"></option><option value="660题"></option><option value="1800题"></option><option value="王道"></option><option value="模拟卷"></option><option value="课程例题"></option></datalist>
<div class="row"><div><label>错因</label><select id="a-reason"><option>概念不清</option><option>计算粗心</option><option>思路错误</option><option>审题失误</option><option>公式/定理记错</option><option>方法没想到</option><option>时间不够</option><option>其他</option></select></div>
<div><label>难度</label><select id="a-diff"><option>1</option><option>2</option><option selected>3</option><option>4</option><option>5</option></select></div></div>
<label>我的总结（这道题教会我的：一句话也行）</label><textarea id="a-note" rows="2" placeholder="如：用洛必达前先查导数比的极限存不存在"></textarea>
<button class="primary" onclick="saveItem()">保存</button>
<button id="a-cancel" style="display:none;width:100%;margin-top:8px;padding:10px;border-radius:8px;border:1px solid #ccc;background:#fff;cursor:pointer" onclick="resetForm()">取消编辑</button>
</div></section>
<section id="p-ai" style="display:none"><div class="card">
<h3 style="margin-top:2px">🤖 AI 讲解</h3>
<p class="meta">在错题卡片上点「🤖 问 AI」，AI 会看着这道题的照片和解析直接给你讲解，不用再截图去别的 App 问。</p>
<label>AI 服务商</label><select id="ai-prov"></select>
<p class="meta" id="ai-provnote"></p>
<label>模型</label><input id="ai-model">
<label>API Key（只存在你自己服务器上）</label><input id="ai-key" type="password" placeholder="粘贴 API Key">
<p class="meta" id="ai-keystate"></p>
<button class="primary" onclick="saveAISettings()">保存设置</button>
<div class="note" style="margin-top:14px">📝 拿 Key 的方法（二选一）：<br><b>DeepSeek</b>：打开 platform.deepseek.com 登录 → 左侧「API Keys」→ 创建 → 复制粘贴到上面。按量付费，账户里要有余额，讲一道题只要几厘钱。<br><b>通义千问</b>：打开百炼控制台 bailian.console.aliyun.com（你的阿里云账号）→ 找「API Key」→ 创建 → 复制 sk- 开头的那串。新账号一般有免费额度。</div>
</div>
<div class="card" style="margin-top:12px"><h3 style="margin-top:2px">👤 账号与数据</h3>
<div class="actions" style="margin-top:4px"><button onclick="chPwd()">🔑 修改登录密码</button><a href="api/export"><button type="button">⬇ 导出备份</button></a></div>
<p class="meta">导出的是题目文字、解析和知识点总结（JSON 文件），照片不在里面；照片都在服务器的 uploads 目录里，以后要整体搬家时找我打包。</p></div>
<div class="card" style="margin-top:12px"><h3 style="margin-top:2px">💾 存储空间</h3><div class="stat" id="storeBox"></div><p class="meta">照片都存在你自己的服务器上，这里只是让你心里有数——离存满还远得很，不用操心。</p></div></section>
<section id="p-stats" style="display:none"><div class="stat" id="statBox"></div>
<div class="card"><h3 style="margin-top:2px">🎯 最薄弱的知识点</h3><p class="meta" style="margin-top:-4px">按「还没弄懂」的数量排，点任意一行直接去看那些题。</p><div id="weakBox"></div></div>
<div class="card"><h3 style="margin-top:2px">📚 按板块分布</h3><div id="chBox"></div></div>
<div class="card"><h3 style="margin-top:2px">🔍 按错因分布</h3><div id="reasonBox"></div></div></section>
</main></div>
<div id="ai-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:97;align-items:flex-end;justify-content:center">
<div style="background:#f6f7f9;width:100%;max-width:760px;height:88vh;height:88dvh;border-radius:16px 16px 0 0;display:flex;flex-direction:column;overflow:hidden">
<div style="background:#1f3a5f;color:#fff;padding:13px 16px;display:flex;justify-content:space-between;align-items:center"><b>🤖 AI 讲解</b><span id="ai-title" style="font-size:13px;opacity:.85;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:55%"></span><button onclick="closeAI()" style="background:none;border:0;color:#fff;font-size:19px;cursor:pointer">✕</button></div>
<div id="ai-log" style="flex:1;overflow-y:auto;padding:14px"></div>
<div style="padding:8px 12px 0;display:flex;gap:7px;flex-wrap:wrap">
<button class="aichip" onclick="askAI('请讲解这道题：考点是什么、应该怎么一步步做')">讲解这道题</button>
<button class="aichip" onclick="askAI('原来的解析我看不懂，换个更通俗的讲法，最好打个比方')">看不懂解析，换个讲法</button>
<button class="aichip" onclick="askAI('这道题最容易错在哪？考试时怎么避开')">易错点在哪</button>
<button class="aichip" onclick="askAI('给我一道同类型、同难度的题练练，先只给题不要给答案')">来道同类题</button>
</div>
<div style="display:flex;gap:8px;padding:10px 12px 14px"><textarea id="ai-input" rows="2" placeholder="直接问，比如：第二步为什么能这样变形？" style="flex:1;margin:0"></textarea><button onclick="askAI()" style="background:#1f3a5f;color:#fff;border:0;border-radius:9px;padding:0 20px;font-size:15px;cursor:pointer">发送</button></div>
</div></div>
<div id="viewer" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.94);z-index:98;flex-direction:column;align-items:center;justify-content:center" onclick="this.style.display='none'">
<img id="viewer-img" style="max-width:96vw;max-height:90vh;border-radius:6px">
<div style="color:#aaa;font-size:13px;margin-top:12px">点任意处关闭 · 想看清细节可双指放大页面</div></div>
<div id="editor" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.9);z-index:99;flex-direction:column;align-items:center;justify-content:center;padding:12px">
<div style="color:#fff;font-size:14px;margin-bottom:10px;text-align:center">✂️ 拖动框内移动选区 · 拖四角缩放 · 在框外按住拖动可重画选区</div>
<canvas id="ed-canvas" style="touch-action:none;border-radius:6px;background:#333"></canvas>
<div style="display:flex;gap:10px;margin-top:16px;flex-wrap:wrap;justify-content:center">
<button class="edbtn" onclick="edRotate()">🔄 旋转 90°</button>
<button class="edbtn" onclick="edCancel()">取消</button>
<button class="edbtn go" onclick="edConfirm()">✅ 就用这个裁剪</button>
</div></div>
<script>
const CHAPTERS={
"数学":["高数·函数极限连续","高数·一元微分学","高数·一元积分学","高数·多元微分学","高数·二重积分","高数·微分方程","高数·无穷级数","线代·行列式与矩阵","线代·向量与方程组","线代·特征值与二次型","概率·随机变量与分布","概率·数字特征","概率·统计基础"],
"408":["数据结构","计算机组成原理","操作系统","计算机网络"],
"英语":["阅读","完形","新题型","翻译","写作","词汇"],
"政治":["马原","毛中特","史纲","思修法基","时政"]};
let itemsCache={},Q={data:"",src:"",removed:false},A={data:"",src:"",removed:false};
const $=id=>document.getElementById(id);
const esc=s=>String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/\n/g,"<br>");
async function api(u,o={}){const r=await fetch(u,o);if(r.status===401){showAuth();throw 0}return r.json()}
async function showAuth(){try{const s=await(await fetch("api/auth-state")).json();$("app").style.display="none";if(s.mode==="setup"){$("setup").style.display="block";$("login").style.display="none"}else{$("login").style.display="block";$("setup").style.display="none"}}catch(e){$("login").style.display="block"}}
async function login(){const r=await fetch("api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({password:$("pw").value})});if(!r.ok)return alert("密码不对");boot()}
async function setupPw(){const r=await fetch("api/setup",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({code:$("s-code").value.trim(),password:$("s-pw").value})});if(!r.ok)return alert("设置码不对，或密码不足 6 位");boot()}
async function changePw(){const o=prompt("输入当前密码");if(o===null)return;const n=prompt("输入新密码（至少 6 位）");if(!n)return;const r=await fetch("api/change-password",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({old:o,new:n})});alert(r.ok?"密码已修改":(await r.json()).error||"修改失败")}
function opts(sel,list,all){sel.innerHTML=(all?'<option value="">全部</option>':"")+list.map(x=>`<option>${x}</option>`).join("")}
function fillCh(a,b){opts(b,CHAPTERS[a.value]||[],true)}
$("a-sub").onchange=()=>opts($("a-ch"),CHAPTERS[$("a-sub").value]);
$("f-sub").onchange=()=>{opts($("f-ch"),CHAPTERS[$("f-sub").value]||Object.values(CHAPTERS).flat(),true);loadLib()};
$("f-ch").onchange=loadLib;$("f-st").onchange=loadLib;$("f-q").oninput=loadLib;
function upState(prev,on){const el=$(prev==="a-imgprev"?"up-q":"up-a");if(el)el.classList.toggle("compact",on)}
function slotShow(prev,btns,src){$(prev).innerHTML=`<img class="q" onclick="viewImg(this.src)" src="${src}">`;$(btns).style.display="flex";upState(prev,true)}
function slotSet(slot,prev,btns,data){slot.data=data;slot.src=data;slot.removed=false;slotShow(prev,btns,data)}
function slotFile(slot,prev,btns,file){if(!file)return;const done=data=>slotSet(slot,prev,btns,data);
if(window.createImageBitmap){createImageBitmap(file,{imageOrientation:"from-image"}).then(bmp=>{const cv=document.createElement("canvas");cv.width=bmp.width;cv.height=bmp.height;cv.getContext("2d").drawImage(bmp,0,0);edOpen(cv.toDataURL("image/jpeg",.92),done)}).catch(()=>edOpen(URL.createObjectURL(file),done))}
else edOpen(URL.createObjectURL(file),done)}
function slotEdit(slot,prev,btns){if(slot.src)edOpen(slot.src,data=>slotSet(slot,prev,btns,data))}
function slotRemove(slot,prev,btns){slot.removed=true;slot.data="";slot.src="";$(prev).innerHTML="";$(btns).style.display="none";upState(prev,false)}
function slotLoad(slot,prev,btns,path){slot.data="";slot.removed=false;if(path){slot.src=path.replace(/^\//,"");slotShow(prev,btns,slot.src)}else{slot.src="";$(prev).innerHTML="";$(btns).style.display="none";upState(prev,false)}}
function slotReset(slot,prev,btns){slot.data="";slot.src="";slot.removed=false;$(prev).innerHTML="";$(btns).style.display="none";upState(prev,false)}
function bindFile(id,slot,prev,btns){$(id).onchange=e=>{const f=e.target.files[0];e.target.value="";slotFile(slot,prev,btns,f)}}
bindFile("a-img-cam",Q,"a-imgprev","a-imgbtns");bindFile("a-img-gal",Q,"a-imgprev","a-imgbtns");
bindFile("a-aimg-cam",A,"a-aimgprev","a-aimgbtns");bindFile("a-aimg-gal",A,"a-aimgprev","a-aimgbtns");
function viewImg(src){$("viewer-img").src=src;$("viewer").style.display="flex"}
function edOpenCurrentQ(){slotEdit(Q,"a-imgprev","a-imgbtns")}
function edOpenCurrentA(){slotEdit(A,"a-aimgprev","a-aimgbtns")}
function removePhotoQ(){slotRemove(Q,"a-imgprev","a-imgbtns")}
function removePhotoA(){slotRemove(A,"a-aimgprev","a-aimgbtns")}
document.querySelectorAll("nav button[data-t]").forEach(b=>b.onclick=()=>{show(b.dataset.t)});
function show(t){document.querySelectorAll("nav button[data-t]").forEach(x=>x.classList.toggle("on",x.dataset.t===t));["lib","know","add","stats","ai"].forEach(x=>$("p-"+x).style.display=x===t?"":"none");if(t==="lib")loadLib();if(t==="know")loadKnow();if(t==="stats")loadStats();if(t==="ai")loadAISettings()}
function card(it){itemsCache[it.id]=it;return `<div class="card">
<span class="pill">${it.subject} · ${esc(it.chapter)}</span>${it.knowledge?`<span class="pill k">🧩 ${esc(it.knowledge)}</span>`:""}<span class="pill r">${esc(it.reason)}</span><span class="pill ${it.status==="done"?"done":"open"}">${it.status==="done"?"✅ 已弄懂":"❓ 还没弄懂"}</span>${it.pinned?'<span class="pill">⭐ 典型题</span>':""}
<h3>${esc(it.title)}</h3>${it.image?`<img class="q" onclick="viewImg(this.src)" src="${(it.image||"").replace(/^\//,"")}">`:""}
<div class="meta">${it.source?"来源："+esc(it.source)+"　":""}难度 ${"★".repeat(it.difficulty)}　${it.created_at} 存入</div>
${it.my_answer?`<p style="margin:8px 0 0"><b>✍️ 我当时：</b>${esc(it.my_answer)}</p>`:""}
${(it.answer||it.answer_image)?`<div class="ablock"><div class="ablock-h">✅ 正确答案 / 解析</div>${it.answer_image?`<img class="q" onclick="viewImg(this.src)" src="${(it.answer_image||"").replace(/^\//,"")}">`:""}${it.answer?`<div>${esc(it.answer)}</div>`:""}</div>`:`<div class="ablock empty-a">💡 还没补答案 — 点「✏️ 编辑」把答案照片或解析补上，对照着看才有效</div>`}
${it.note?`<div class="note">📌 我的总结：${esc(it.note)}</div>`:""}
<div class="actions">
<button onclick="toggleStatus(${it.id})">${it.status==="done"?"↩ 标回没弄懂":"✅ 标记弄懂了"}</button>
<button onclick="togglePin(${it.id})">${it.pinned?"取消典型":"⭐ 标为典型题"}</button>
<button onclick="openAI(${it.id})">🤖 问 AI</button>
<button onclick="editItem(${it.id})">✏️ 编辑</button>
<button onclick="delItem(${it.id})" style="color:#b3261e">删除</button></div></div>`}
async function loadLib(){const p=new URLSearchParams({subject:$("f-sub").value,chapter:$("f-ch").value,status:$("f-st").value,q:$("f-q").value});const d=await api("api/items?"+p);$("libList").innerHTML=d.length?d.map(card).join(""):'<div class="card empty">这里还没有错题。做错一道，就顺手存一道。</div>'}
async function randomPick(){const d=await api("api/items?status=open&random=5");show("lib");$("libList").innerHTML=(d.length?'<div class="meta" style="padding:2px">🎲 随机抽到的「还没弄懂」的题，随便翻翻：</div>'+d.map(card).join(""):'<div class="card empty">没有「还没弄懂」的题了，挺好。</div>')}
async function toggleStatus(id){await api(`api/items/${id}/toggle`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({field:"status"})});loadLib()}
async function togglePin(id){await api(`api/items/${id}/toggle`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({field:"pinned"})});loadLib()}
async function delItem(id){if(confirm("确定删除这道错题？")){await api("api/items/"+id,{method:"DELETE"});loadLib()}}
function editItem(id){const it=itemsCache[id];if(!it)return;$("a-id").value=id;opts($("a-sub"),Object.keys(CHAPTERS));$("a-sub").value=it.subject;opts($("a-ch"),CHAPTERS[it.subject]);$("a-ch").value=it.chapter;$("a-know").value=it.knowledge;$("a-title").value=it.title;$("a-src").value=it.source;$("a-reason").value=it.reason;$("a-diff").value=it.difficulty;$("a-my").value=it.my_answer;$("a-ans").value=it.answer;$("a-note").value=it.note;slotLoad(Q,"a-imgprev","a-imgbtns",it.image);slotLoad(A,"a-aimgprev","a-aimgbtns",it.answer_image);$("a-cancel").style.display="";show("add");window.scrollTo(0,0)}
function resetForm(){$("a-id").value="";$("a-sub").value="数学";opts($("a-ch"),CHAPTERS["数学"]);$("a-reason").value="概念不清";$("a-diff").value="3";["a-know","a-title","a-src","a-my","a-ans","a-note"].forEach(i=>$(i).value="");["a-img-cam","a-img-gal","a-aimg-cam","a-aimg-gal"].forEach(i=>$(i).value="");slotReset(Q,"a-imgprev","a-imgbtns");slotReset(A,"a-aimgprev","a-aimgbtns");$("a-cancel").style.display="none"}
async function saveItem(){if(!$("a-title").value.trim())return alert("先写题目");const chs=CHAPTERS[$("a-sub").value]||[];if(!chs.includes($("a-ch").value)){$("a-ch").value=chs[0]||""}const body={subject:$("a-sub").value,chapter:$("a-ch").value,knowledge:$("a-know").value.trim(),title:$("a-title").value,source:$("a-src").value,reason:$("a-reason").value,difficulty:+$("a-diff").value,my_answer:$("a-my").value,answer:$("a-ans").value,note:$("a-note").value,image_base64:Q.removed?"__REMOVE__":Q.data,answer_image_base64:A.removed?"__REMOVE__":A.data};const id=$("a-id").value;await api(id?"api/items/"+id:"api/items",{method:id?"PUT":"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});resetForm();show("lib")}
async function loadKnow(){const d=await api("api/knowledge");$("knowList").innerHTML=d.length?d.map(k=>`<div class="card"><span class="pill k">🧩 ${esc(k.knowledge)}</span><span class="pill">${esc(k.subject)}</span><span class="pill r">错了 ${k.total} 次</span>${k.open?`<span class="pill open">还没弄懂 ${k.open} 道</span>`:'<span class="pill done">都弄懂了</span>'}<textarea id="ks-${k.id||0}" rows="3" placeholder="写这个知识点的总结：核心公式 / 结论 / 易错点…">${esc(k.summary||"")}</textarea><div class="actions"><button onclick="saveKnow('${esc(k.knowledge).replace(/'/g,"\\'")}','${k.id||0}')">💾 保存总结</button><button onclick="jumpKnow('${esc(k.knowledge).replace(/'/g,"\\'")}')">看这个知识点下的错题 →</button></div></div>`).join(""):'<div class="card empty">还没有知识点。存错题时把「知识点」填上，这里会自动汇总。</div>';$("knowDatalist").innerHTML=d.map(k=>`<option value="${esc(k.knowledge)}">`).join("")}
async function saveKnow(name,idx){const all=await api("api/knowledge");const k=all.find(x=>x.knowledge===name);const ta=document.querySelector(`textarea[id^="ks-"]`);let summary="";document.querySelectorAll("#knowList .card").forEach((c,i)=>{if(all[i]&&all[i].knowledge===name)summary=c.querySelector("textarea").value});await api("api/knowledge",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({knowledge:name,subject:k?k.subject:"",summary})});alert("总结已保存")}
function jumpKnow(name){show("lib");$("f-q").value=name;loadLib()}
async function loadStats(){const s=await api("api/stats");const pct=s.total?Math.round(s.done/s.total*100):0;
$("statBox").innerHTML=`<div>错题总数<b>${s.total}</b></div><div>还没弄懂<b style="color:#c0392b">${s.open}</b></div><div>已弄懂<b style="color:#1e8449">${s.done}</b><span class="meta">掌握率 ${pct}%</span></div><div>涉及知识点<b>${s.knowledge_count}</b></div>`;
const ks=await api("api/knowledge");const weak=ks.filter(k=>k.total>0).sort((a,b)=>(b.open-a.open)||(b.total-a.total)).slice(0,8);const wmax=Math.max(1,...weak.map(k=>k.total));
$("weakBox").innerHTML=weak.length?weak.map(k=>`<div class="wkrow" onclick="jumpKnow('${esc(k.knowledge).replace(/'/g,"\\'")}')"><div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px"><span style="font-weight:600">🧩 ${esc(k.knowledge)}</span><span style="flex:none;font-size:13px;color:#5b6b7d">错 ${k.total} 次${k.open?` · <span style="color:#c0392b;font-weight:700">还没弄懂 ${k.open}</span>`:' · <span style="color:#1e8449">已攻克</span>'}</span></div><div class="srow" style="margin:5px 0 0"><span class="st" style="flex:1"><i class="wk" style="width:${Math.max(6,k.total/wmax*100)}%"></i></span></div></div>`).join(""):'<p class="meta">还没有标知识点的错题。存题时把「知识点」填上，这里就能看出你最弱在哪。</p>';
const rows=(obj,cls)=>{const e=Object.entries(obj).filter(([,v])=>v>0).sort((a,b)=>b[1]-a[1]);const m=Math.max(1,...e.map(([,v])=>v));return e.length?e.map(([k,v])=>`<div class="srow"><span class="sl">${esc(k)}</span><span class="st"><i class="${cls}" style="width:${Math.max(5,v/m*100)}%"></i></span><span class="sv">${v}</span></div>`).join(""):'<p class="meta">暂无数据</p>'};
$("chBox").innerHTML=rows(s.by_chapter,"");$("reasonBox").innerHTML=rows(s.by_reason,"rsn")}
async function loadStorage(){try{const s=await api("api/stats");if(!s.storage)return;const st=s.storage;$("storeBox").innerHTML=`<div>题目照片<b>${st.photos} 张</b></div><div>照片占用<b>${st.photos_mb} MB</b></div><div>平均每张<b>${st.avg_kb} KB</b></div><div>磁盘剩余<b>${st.disk_free_gb} GB</b></div><div>估算还能存<b>约 ${st.est_more.toLocaleString()} 张</b></div>`}catch(e){}}
/* ---------- AI 讲解 ---------- */
let aiItemId=null,aiBusy=false,aiProviders={};
const TEXSYM={infty:"∞",to:"→",rightarrow:"→",leftarrow:"←",Rightarrow:"⇒",Leftarrow:"⇐",Leftrightarrow:"⇔",cdot:"·",times:"×",div:"÷",pm:"±",mp:"∓",le:"≤",ge:"≥",ne:"≠",neq:"≠",approx:"≈",sim:"~",equiv:"≡",pi:"π",alpha:"α",beta:"β",gamma:"γ",delta:"δ",Delta:"Δ",epsilon:"ε",varepsilon:"ε",theta:"θ",lambda:"λ",mu:"μ",sigma:"σ",tau:"τ",phi:"φ",varphi:"φ",omega:"ω",Omega:"Ω",partial:"∂",in:"∈",notin:"∉",subset:"⊂",subseteq:"⊆",cup:"∪",cap:"∩",forall:"∀",exists:"∃",ldots:"…",cdots:"⋯",dots:"…",prime:"′",circ:"°",deg:"°",angle:"∠",perp:"⊥",parallel:"∥",triangle:"△",sqrt:"√",emptyset:"∅",infty2:"∞",limits:"",nolimits:"",displaystyle:"",textstyle:"",limits2:""};
const TEXBIG={int:"∫",oint:"∮",sum:"∑",prod:"∏",bigcup:"⋃",bigcap:"⋂"};
function texHtml(src){let s=String(src),i=0;const escH=t=>t.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
function render(ch){return ch.map(c=>typeof c==="string"?c:`<span class="op${c.lim?" lim":""}">${c.sup?`<span class="oplim">${c.sup}</span>`:""}<span class="opsym">${c.sym}</span>${c.sub?`<span class="oplim">${c.sub}</span>`:""}</span>`).join("")}
function seq(end){const ch=[];while(i<s.length){if(end&&s[i]===end)break;const u=unit();for(const x of u)ch.push(x);
while(i<s.length&&(s[i]==="^"||s[i]==="_")){const sup=s[i]==="^";i++;const a=render(unit());const last=ch[ch.length-1];
if(last&&typeof last==="object"){if(sup)last.sup=a;else last.sub=a}else ch.push(sup?`<sup>${a}</sup>`:`<sub>${a}</sub>`)}
if(end&&s[i]===end)break}return ch}
function arg(){while(s[i]===" ")i++;return render(unit())}
function unit(){const ch=[];if(i>=s.length)return ch;const c=s[i];
if(c==="{"){i++;const inner=seq("}");if(s[i]==="}")i++;return inner}
if(c==="\\"){i++;let name="";while(i<s.length&&/[a-zA-Z]/.test(s[i]))name+=s[i++];
if(!name){const nc=s[i++]||"";if(nc==="\\")ch.push("<br>");else if(nc==="{")ch.push("{");else if(nc==="}")ch.push("}");else if(nc==="%")ch.push("%");else if(nc==="$")ch.push("$");else if(nc==="_")ch.push("_");else if(nc==="#")ch.push("#");else if(nc==="&")ch.push("&amp;");return ch}
if(name==="frac"||name==="dfrac"||name==="tfrac"){const a=arg(),b=arg();ch.push(`<span class="frac"><span class="num">${a}</span><span class="den">${b}</span></span>`);return ch}
if(name==="sqrt"){let root="";if(s[i]==="["){i++;let r="";while(i<s.length&&s[i]!=="]")r+=s[i++];i++;root=`<sup style="font-size:.62em">${escH(r)}</sup>`}const a=arg();ch.push(`${root}√<span class="sqrt">${a}</span>`);return ch}
if(name==="overline"){ch.push(`<span style="text-decoration:overline">${arg()}</span>`);return ch}
if(name==="underline"){ch.push(`<span style="text-decoration:underline">${arg()}</span>`);return ch}
if(name==="text"||name==="textrm"||name==="textbf"||name==="textit"||name==="mathrm"||name==="mathbf"||name==="mathit"||name==="operatorname"){const a=arg();ch.push(name==="textbf"?`<b>${a}</b>`:a);return ch}
if(name==="begin"||name==="end"){arg();return ch}
if(name==="left"||name==="right"||name==="big"||name==="Big"||name==="bigg"||name==="Bigg")return ch;
if(TEXBIG[name]){ch.push({sym:TEXBIG[name],sup:"",sub:""});return ch}
if(name==="lim"){ch.push({sym:"lim",sup:"",sub:"",lim:1});return ch}
if(name in TEXSYM){if(TEXSYM[name])ch.push(escH(TEXSYM[name]));return ch}
ch.push(escH(name));return ch}
if(c===" "){i++;ch.push(" ");return ch}
i++;ch.push(escH(c));return ch}
return render(seq(null))}
function md(s){const blocks=[];let t=String(s||"").replace(/\\\[([\s\S]*?)\\\]|\$\$([\s\S]*?)\$\$/g,(m,a,b)=>{blocks.push(`<span class="mathblk"><span class="math">${texHtml((a!==undefined?a:b).trim())}</span></span>`);return "\n\u0001"+(blocks.length-1)+"\u0001\n"});
const segRender=line=>line.split(/(\$[^$\n]+\$|\\\([\s\S]*?\\\)|\u0001\d+\u0001)/g).map(p=>{if(!p)return "";if(p[0]==="\u0001")return blocks[+p.slice(1,-1)];if(p[0]==="$")return `<span class="math">${texHtml(p.slice(1,-1))}</span>`;if(p.startsWith("\\("))return `<span class="math">${texHtml(p.slice(2,-2))}</span>`;return esc(p).replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>").replace(/`([^`]+)`/g,"<code>$1</code>")}).join("");
let out="";for(const ln of t.split("\n")){if(/^#{1,4}\s/.test(ln))out+=`<div class="aih">${segRender(ln.replace(/^#{1,4}\s*/,""))}</div>`;else if(/^\s*(-{3,}|\*{3,})\s*$/.test(ln))out+='<div style="border-top:1px solid #e3e8ef;margin:9px 0"></div>';else if(!ln.trim())out+='<div style="height:6px"></div>';else out+=segRender(ln)+"<br>"}return out.replace(/(<br>)+$/,"")}
function aiBubble(role,html){const d=document.createElement("div");d.className="aib "+(role==="user"?"u":"a");d.innerHTML=html;$("ai-log").appendChild(d);$("ai-log").scrollTop=1e7;return d}
async function openAI(id){aiItemId=id;const it=itemsCache[id];$("ai-title").textContent=it?it.title:"";document.body.style.overflow="hidden";$("ai-modal").style.display="flex";$("ai-log").innerHTML="";aiBubble("assistant","把这道题交给我吧——我能看到题目照片和已有解析。点下面的快捷问题，或者直接打字问我。");try{const h=await api("api/items/"+id+"/ai-history");h.forEach(m=>aiBubble(m.role,m.role==="user"?esc(m.content):md(m.content)))}catch(e){}}
function closeAI(){$("ai-modal").style.display="none";document.body.style.overflow=""}
async function askAI(preset){if(aiBusy||!aiItemId)return;const q=(preset||$("ai-input").value).trim();if(!q)return;$("ai-input").value="";aiBubble("user",esc(q));aiBusy=true;const tmp=aiBubble("assistant","🤔 正在看题思考…");try{const r=await api("api/ai/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({item_id:aiItemId,message:q})});if(r.error){tmp.innerHTML="⚠️ "+esc(r.error)}else{tmp.innerHTML=md(r.reply)}}catch(e){tmp.innerHTML="⚠️ 请求失败，再试一次"}aiBusy=false;$("ai-log").scrollTop=1e7}
async function loadAISettings(){try{const s=await api("api/ai/settings");aiProviders=s.providers;$("ai-prov").innerHTML=Object.entries(s.providers).map(([k,v])=>`<option value="${k}">${v.label}</option>`).join("");$("ai-prov").value=s.provider;$("ai-model").value=s.model;$("ai-key").value="";$("ai-keystate").textContent=s.has_key?"✅ 已保存 Key（内容不回显；要更换直接粘贴新的再保存）":"⚠️ 还没有保存 Key，AI 讲解暂时用不了";aiProvNote();loadStorage()}catch(e){}}
function aiProvNote(){const p=aiProviders[$("ai-prov").value];if(!p)return;$("ai-provnote").textContent=p.vision?"✅ 这个服务商能看懂题目和答案的照片，讲解会结合图片。":"⚠️ 注意：这个服务商看不到图片，只能根据题目文字和你填的解析讲解。";$("ai-model").placeholder=p.model}
$("ai-prov").onchange=()=>{const p=aiProviders[$("ai-prov").value];if(p)$("ai-model").value=p.model;aiProvNote()};
async function saveAISettings(){await api("api/ai/settings",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({provider:$("ai-prov").value,model:$("ai-model").value.trim(),api_key:$("ai-key").value.trim()})});alert("已保存");loadAISettings()}
/* ---------- 图片编辑器：裁剪 + 旋转（纯原生实现，无第三方库） ---------- */
let edCB=null,edImg=null,edRot=0,edCrop=null,edScale=1,edDrag=null;
function edOpen(src,cb){edCB=cb;edRot=0;const im=new Image();im.onload=()=>{edImg=im;$("editor").style.display="flex";edResetCrop();edDraw()};im.onerror=()=>alert("图片加载失败");im.src=src}
function edRotated(){const c=document.createElement("canvas");const r=((edRot%360)+360)%360;const swap=r===90||r===270;c.width=swap?edImg.naturalHeight:edImg.naturalWidth;c.height=swap?edImg.naturalWidth:edImg.naturalHeight;const x=c.getContext("2d");x.translate(c.width/2,c.height/2);x.rotate(r*Math.PI/180);x.drawImage(edImg,-edImg.naturalWidth/2,-edImg.naturalHeight/2);return c}
function edResetCrop(){const rc=edRotated();const maxW=Math.min(innerWidth*.94,720),maxH=innerHeight*.56;edScale=Math.min(maxW/rc.width,maxH/rc.height);const cv=$("ed-canvas");cv.width=Math.max(50,Math.round(rc.width*edScale));cv.height=Math.max(50,Math.round(rc.height*edScale));const w=cv.width*.88,h=cv.height*.88;edCrop={x:(cv.width-w)/2,y:(cv.height-h)/2,w:w,h:h}}
function edDraw(){const cv=$("ed-canvas"),x=cv.getContext("2d");const rc=edRotated();x.clearRect(0,0,cv.width,cv.height);x.drawImage(rc,0,0,cv.width,cv.height);x.fillStyle="rgba(0,0,0,.55)";x.beginPath();x.rect(0,0,cv.width,cv.height);x.rect(edCrop.x,edCrop.y,edCrop.w,edCrop.h);x.fill("evenodd");x.strokeStyle="#fff";x.lineWidth=2;x.strokeRect(edCrop.x,edCrop.y,edCrop.w,edCrop.h);x.fillStyle="#fff";edCorners().forEach(p=>{x.fillRect(p[0]-7,p[1]-7,14,14)});x.strokeStyle="rgba(255,255,255,.35)";x.lineWidth=1;[1,2].forEach(i=>{x.beginPath();x.moveTo(edCrop.x+edCrop.w*i/3,edCrop.y);x.lineTo(edCrop.x+edCrop.w*i/3,edCrop.y+edCrop.h);x.stroke();x.beginPath();x.moveTo(edCrop.x,edCrop.y+edCrop.h*i/3);x.lineTo(edCrop.x+edCrop.w,edCrop.y+edCrop.h*i/3);x.stroke()})}
function edCorners(){const c=edCrop;return[[c.x,c.y],[c.x+c.w,c.y],[c.x,c.y+c.h],[c.x+c.w,c.y+c.h]]}
function edPos(e){const r=$("ed-canvas").getBoundingClientRect();return{x:(e.clientX-r.left)*($("ed-canvas").width/r.width),y:(e.clientY-r.top)*($("ed-canvas").height/r.height)}}
function edHit(p){const cs=edCorners(),names=["tl","tr","bl","br"];for(let i=0;i<4;i++){if(Math.abs(p.x-cs[i][0])<24&&Math.abs(p.y-cs[i][1])<24)return names[i]}const c=edCrop;if(p.x>c.x&&p.x<c.x+c.w&&p.y>c.y&&p.y<c.y+c.h)return"move";return"new"}
$("ed-canvas").addEventListener("pointerdown",e=>{e.preventDefault();const p=edPos(e);edDrag={mode:edHit(p),sx:p.x,sy:p.y,orig:{...edCrop}};$("ed-canvas").setPointerCapture(e.pointerId)});
$("ed-canvas").addEventListener("pointermove",e=>{if(!edDrag)return;const p=edPos(e),cv=$("ed-canvas"),o=edDrag.orig,dx=p.x-edDrag.sx,dy=p.y-edDrag.sy;let c={...o};const clamp=()=>{c.w=Math.max(30,c.w);c.h=Math.max(30,c.h);c.x=Math.min(Math.max(0,c.x),cv.width-20);c.y=Math.min(Math.max(0,c.y),cv.height-20);if(c.x+c.w>cv.width)c.w=cv.width-c.x;if(c.y+c.h>cv.height)c.h=cv.height-c.y};
if(edDrag.mode==="move"){c.x=o.x+dx;c.y=o.y+dy;clamp();if(c.x!==o.x+dx)c.x=Math.max(0,Math.min(o.x+dx,cv.width-c.w));c.x=Math.max(0,Math.min(c.x,cv.width-c.w));c.y=Math.max(0,Math.min(c.y,cv.height-c.h))}
else if(edDrag.mode==="new"){c={x:Math.min(edDrag.sx,p.x),y:Math.min(edDrag.sy,p.y),w:Math.abs(dx),h:Math.abs(dy)};clamp()}
else{const m=edDrag.mode;if(m.includes("r"))c.w=o.w+dx;if(m.includes("l")){c.x=o.x+dx;c.w=o.w-dx}if(m.includes("b"))c.h=o.h+dy;if(m.includes("t")){c.y=o.y+dy;c.h=o.h-dy}clamp();if(c.w===30&&(m.includes("l")))c.x=o.x+o.w-30;if(c.h===30&&(m.includes("t")))c.y=o.y+o.h-30}
edCrop=c;edDraw()});
$("ed-canvas").addEventListener("pointerup",()=>{edDrag=null});
function edRotate(){edRot+=90;edResetCrop();edDraw()}
function edCancel(){$("editor").style.display="none";edCB=null}
function edConfirm(){const rc=edRotated();const sx=edCrop.x/edScale,sy=edCrop.y/edScale,sw=edCrop.w/edScale,sh=edCrop.h/edScale;const k=Math.min(1,1600/Math.max(sw,sh));const out=document.createElement("canvas");out.width=Math.max(1,Math.round(sw*k));out.height=Math.max(1,Math.round(sh*k));out.getContext("2d").drawImage(rc,sx,sy,sw,sh,0,0,out.width,out.height);const data=out.toDataURL("image/jpeg",.82);$("editor").style.display="none";if(edCB)edCB(data);edCB=null}
async function boot(){try{await api("api/stats");$("login").style.display="none";$("setup").style.display="none";$("app").style.display="block";opts($("a-sub"),Object.keys(CHAPTERS));opts($("a-ch"),CHAPTERS["数学"]);opts($("f-sub"),Object.keys(CHAPTERS),1);opts($("f-ch"),Object.values(CHAPTERS).flat(),1);loadLib();loadKnow()}catch(e){}}
boot();
</script></body></html>"""

def authed(h):
    if not (PASSWORD or SETUP_CODE or stored_hash()): return True  # 未配任何密码手段时才开放（仅本地调试）
    tok = ""
    for part in (h.headers.get("Cookie") or "").split(";"):
        if part.strip().startswith("sid="): tok = part.strip()[4:]
    return tok in SESSIONS and SESSIONS[tok] > time.time()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")
    def start_session(self):
        tok = secrets.token_urlsafe(32); SESSIONS[tok] = time.time() + 30*86400
        body = b'{"ok":true}'; self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", f"sid={tok}; Path=/; HttpOnly; Max-Age={30*86400}")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def save_image(self, b64):
        if b64 and b64.startswith("data:image"):
            raw = base64.b64decode(b64.split(",", 1)[1])
            name = hashlib.md5(raw).hexdigest()[:16] + ".jpg"
            open(os.path.join(UPLOAD_DIR, name), "wb").write(raw)
            return "/uploads/" + name
        return ""
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/":
            body = HTML.encode(); self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if u.path.startswith("/uploads/"):
            p = os.path.join(UPLOAD_DIR, os.path.basename(u.path))
            if os.path.exists(p):
                data = open(p, "rb").read(); self.send_response(200)
                self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data); return
            self.send_json({"error": "not found"}, 404); return
        if u.path == "/api/auth-state":
            self.send_json({"mode": auth_mode()}); return
        if not authed(self): self.send_json({"error": "unauthorized"}, 401); return
        if u.path == "/api/ai/settings":
            prov, key, model = ai_config()
            self.send_json({"provider": prov, "model": model, "has_key": bool(key),
                            "providers": {k: {"label": v["label"], "model": v["model"], "vision": v["vision"]} for k, v in AI_PROVIDERS.items()}}); return
        mh = re.match(r"/api/items/(\d+)/ai-history", u.path)
        if mh:
            with db() as c:
                rows = c.execute("SELECT role,content,created_at FROM ai_chats WHERE item_id=? ORDER BY id", (mh.group(1),)).fetchall()
            self.send_json([dict(r) for r in rows]); return
        with db() as c:
            if u.path == "/api/items":
                sql = "SELECT * FROM items WHERE 1=1"; args = []
                for key, col in (("subject","subject"),("chapter","chapter"),("status","status")):
                    if q.get(key, [""])[0]: sql += f" AND {col}=?"; args.append(q[key][0])
                if q.get("q", [""])[0]:
                    kw = "%" + q["q"][0] + "%"
                    sql += " AND (title LIKE ? OR knowledge LIKE ? OR source LIKE ? OR note LIKE ? OR answer LIKE ?)"; args += [kw]*5
                if q.get("random", [""])[0]:
                    sql += " ORDER BY RANDOM() LIMIT ?"; args.append(int(q["random"][0]))
                else:
                    sql += " ORDER BY pinned DESC, id DESC"
                self.send_json([dict(r) for r in c.execute(sql, args)]); return
            if u.path == "/api/knowledge":
                # 聚合：以 items 里的知识点为准，左连总结表
                rows = c.execute("""SELECT i.knowledge AS knowledge, MAX(i.subject) AS subject,
                    COUNT(*) AS total, SUM(CASE WHEN i.status='open' THEN 1 ELSE 0 END) AS open,
                    kn.id AS id, COALESCE(kn.summary,'') AS summary
                    FROM items i LEFT JOIN knowledge_notes kn ON kn.knowledge=i.knowledge
                    WHERE i.knowledge!='' GROUP BY i.knowledge ORDER BY total DESC""").fetchall()
                self.send_json([dict(r) for r in rows]); return
            if u.path == "/api/stats":
                total = c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
                op = c.execute("SELECT COUNT(*) n FROM items WHERE status='open'").fetchone()["n"]
                kc = c.execute("SELECT COUNT(DISTINCT knowledge) n FROM items WHERE knowledge!=''").fetchone()["n"]
                bc = {r["chapter"]: r["n"] for r in c.execute("SELECT chapter,COUNT(*) n FROM items GROUP BY chapter")}
                br = {r["reason"]: r["n"] for r in c.execute("SELECT reason,COUNT(*) n FROM items GROUP BY reason")}
                photos = 0; pbytes = 0
                for fn in os.listdir(UPLOAD_DIR):
                    fp = os.path.join(UPLOAD_DIR, fn)
                    if os.path.isfile(fp): photos += 1; pbytes += os.path.getsize(fp)
                avg = (pbytes // photos) if photos else 300 * 1024
                free = shutil.disk_usage("/").free
                self.send_json({"total": total, "open": op, "done": total-op, "knowledge_count": kc, "by_chapter": bc, "by_reason": br,
                                "storage": {"photos": photos, "photos_mb": round(pbytes/1048576, 1), "avg_kb": round(avg/1024),
                                            "disk_free_gb": round(free/1073741824, 1), "est_more": int(free // max(avg, 1))}}); return
            if u.path == "/api/export":
                data = {"items": [dict(r) for r in c.execute("SELECT * FROM items ORDER BY id")],
                        "knowledge_notes": [dict(r) for r in c.execute("SELECT * FROM knowledge_notes ORDER BY id")]}
                body = json.dumps(data, ensure_ascii=False, indent=2).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", "attachment; filename=cuotiben-backup.json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_json({"error": "not found"}, 404)
    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/login":
            d = self.body_json()
            if not verify_password(d.get("password") or ""): self.send_json({"error": "wrong password"}, 403); return
            self.start_session(); return
        if u.path == "/api/setup":
            d = self.body_json()
            if auth_mode() != "setup": self.send_json({"error": "already set up"}, 400); return
            if not SETUP_CODE or d.get("code") != SETUP_CODE: self.send_json({"error": "bad setup code"}, 403); return
            if len(d.get("password") or "") < 6: self.send_json({"error": "password too short"}, 400); return
            set_password(d["password"]); self.start_session(); return
        if not authed(self): self.send_json({"error": "unauthorized"}, 401); return
        d = self.body_json()
        if u.path == "/api/change-password":
            if PASSWORD: self.send_json({"error": "密码由服务器环境变量管理，页面内不可改"}, 400); return
            if not verify_password(d.get("old") or ""): self.send_json({"error": "当前密码不对"}, 403); return
            if len(d.get("new") or "") < 6: self.send_json({"error": "新密码至少 6 位"}, 400); return
            set_password(d["new"]); self.send_json({"ok": True}); return
        if u.path == "/api/ai/chat":
            item_id = int(d.get("item_id") or 0); q = (d.get("message") or "").strip()
            if not q: self.send_json({"error": "先输入问题"}, 400); return
            with db() as c:
                it = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
                if not it: self.send_json({"error": "题目不存在"}, 404); return
                hist = [dict(r) for r in c.execute("SELECT role,content FROM ai_chats WHERE item_id=? ORDER BY id DESC LIMIT 10", (item_id,))][::-1]
            prov, key, model = ai_config()
            if not key:
                self.send_json({"error": "还没配置 AI 的 API Key。点顶部「⚙️ 设置」，按里面的步骤拿一个 DeepSeek 或通义千问的 Key 填上就能用了。"}, 400); return
            vision = AI_PROVIDERS[prov]["vision"]
            ctx = (f"题目：{it['title']}\n科目板块：{it['subject']} · {it['chapter']}\n知识点：{it['knowledge'] or '（未填）'}\n"
                   f"来源：{it['source'] or '（未填）'}\n我当时的作答/错误：{it['my_answer'] or '（未填）'}\n"
                   f"已有解析（以此为准）：{it['answer'] or '（未填，可能只有图片）'}\n我的总结：{it['note'] or '（未填）'}")
            messages = [{"role": "system", "content": AI_SYSTEM}]
            for h in hist: messages.append({"role": h["role"], "content": h["content"]})
            parts = [{"type": "text", "text": ctx + "\n\n我的问题：" + q}]
            n_img = 0
            if vision:
                for field in ("image", "answer_image"):
                    p = it[field] or ""
                    fp = os.path.join(UPLOAD_DIR, os.path.basename(p)) if p else ""
                    if fp and os.path.exists(fp):
                        b64 = base64.b64encode(open(fp, "rb").read()).decode()
                        parts.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}); n_img += 1
            if vision and n_img:
                messages.append({"role": "user", "content": parts})
            else:
                messages.append({"role": "user", "content": ctx + "\n\n我的问题：" + q})
            try:
                reply = ai_call(messages)
            except RuntimeError as e:
                self.send_json({"error": str(e)}, 502); return
            with db() as c:
                c.execute("INSERT INTO ai_chats(item_id,role,content,created_at) VALUES(?,?,?,?)", (item_id, "user", q, today()))
                c.execute("INSERT INTO ai_chats(item_id,role,content,created_at) VALUES(?,?,?,?)", (item_id, "assistant", reply, today()))
            self.send_json({"reply": reply}); return
        m = re.match(r"/api/items/(\d+)/toggle", u.path)
        with db() as c:
            if u.path == "/api/items":
                ab64 = d.get("answer_image_base64") or ""
                aimg = "" if ab64 in ("", "__REMOVE__") else self.save_image(ab64)
                cur = c.execute("""INSERT INTO items(subject,chapter,knowledge,title,source,my_answer,answer,note,reason,difficulty,image,answer_image,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (d.get("subject",""), d.get("chapter",""), d.get("knowledge",""), d.get("title",""),
                     d.get("source",""), d.get("my_answer",""), d.get("answer",""), d.get("note",""),
                     d.get("reason",""), int(d.get("difficulty",3)), self.save_image(d.get("image_base64") or ""), aimg, today(), today()))
                self.send_json({"id": cur.lastrowid}); return
            if m:
                it = c.execute("SELECT * FROM items WHERE id=?", (m.group(1),)).fetchone()
                if not it: self.send_json({"error": "not found"}, 404); return
                if d.get("field") == "status":
                    c.execute("UPDATE items SET status=?,updated_at=? WHERE id=?",
                              ("done" if it["status"]=="open" else "open", today(), m.group(1)))
                else:
                    c.execute("UPDATE items SET pinned=?,updated_at=? WHERE id=?",
                              (0 if it["pinned"] else 1, today(), m.group(1)))
                self.send_json({"ok": True}); return
        self.send_json({"error": "not found"}, 404)
    def do_PUT(self):
        if not authed(self): self.send_json({"error": "unauthorized"}, 401); return
        u = urlparse(self.path); d = self.body_json()
        m = re.match(r"/api/items/(\d+)", u.path)
        with db() as c:
            if m:
                it = c.execute("SELECT * FROM items WHERE id=?", (m.group(1),)).fetchone()
                if not it: self.send_json({"error": "not found"}, 404); return
                b64v = d.get("image_base64") or ""
                if b64v == "__REMOVE__": img = ""
                elif b64v: img = self.save_image(b64v)
                else: img = it["image"]
                ab64 = d.get("answer_image_base64") or ""
                if ab64 == "__REMOVE__": aimg = ""
                elif ab64: aimg = self.save_image(ab64)
                else: aimg = it["answer_image"] if "answer_image" in it.keys() else ""
                c.execute("""UPDATE items SET subject=?,chapter=?,knowledge=?,title=?,source=?,my_answer=?,answer=?,note=?,reason=?,difficulty=?,image=?,answer_image=?,updated_at=? WHERE id=?""",
                          (d.get("subject", it["subject"]), d.get("chapter", it["chapter"]), d.get("knowledge", it["knowledge"]),
                           d.get("title", it["title"]), d.get("source", it["source"]), d.get("my_answer", it["my_answer"]),
                           d.get("answer", it["answer"]), d.get("note", it["note"]), d.get("reason", it["reason"]),
                           int(d.get("difficulty", it["difficulty"])), img, aimg, today(), m.group(1)))
                self.send_json({"ok": True}); return
            if u.path == "/api/knowledge":
                c.execute("""INSERT INTO knowledge_notes(subject,knowledge,summary,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(knowledge) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at""",
                          (d.get("subject",""), d.get("knowledge",""), d.get("summary",""), today()))
                self.send_json({"ok": True}); return
            if u.path == "/api/ai/settings":
                prov = d.get("provider", "qwen")
                if prov not in AI_PROVIDERS: self.send_json({"error": "未知服务商"}, 400); return
                put_setting("ai_provider", prov)
                if d.get("model"): put_setting("ai_model", d["model"].strip())
                if d.get("api_key"): put_setting("ai_key", d["api_key"].strip())
                self.send_json({"ok": True}); return
        self.send_json({"error": "not found"}, 404)
    def do_DELETE(self):
        if not authed(self): self.send_json({"error": "unauthorized"}, 401); return
        m = re.match(r"/api/items/(\d+)", urlparse(self.path).path)
        if m:
            with db() as c: c.execute("DELETE FROM items WHERE id=?", (m.group(1),))
            self.send_json({"ok": True}); return
        self.send_json({"error": "not found"}, 404)

if __name__ == "__main__":
    init_db()
    print(f"错题本 v2 已启动: http://0.0.0.0:{PORT}  (密码保护: {'开' if PASSWORD else '关'})")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
