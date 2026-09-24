# ==============================================================================
# main.py - 抖音视频/图集自动备份与同步工具
#
# 【这个脚本是干嘛的？】
# 这是一个自动帮你备份抖音创作者作品的程序。
# 它可以自动打开抖音网页、找到你关注的博主主页，把他们最新发布的视频、图集、封面
# 以及头像全自动下载下来，保存到你的个人网盘（WebDAV）里，
# 并且还会通过飞书机器人给你发送每日运行报告。
# ==============================================================================

import os, re, json, time, random, sys
from datetime import datetime, timezone, timedelta
import requests
from knock import try1080  # 引入用于尝试获取 1080P 画质视频的辅助函数
from playwright.sync_api import sync_playwright  # 引入 Playwright 网页自动化工具

# ------------------------------------------------------------------------------
# 1. 配置项读取（从系统的环境变量中获取你设置好的密钥和参数）
# ------------------------------------------------------------------------------

# 抖音登录凭证（Cookie），脚本靠它以登录状态访问抖音
COOKIE = os.environ["DOUYIN_COOKIE"]

# 需要备份的抖音博主主页链接列表（支持多条，可以用逗号或换行分隔）
SHARE_URLS = [u.split("?")[0].strip() for u in os.environ["DOUYIN_URL"].replace(",", "\n").splitlines() if u.strip()]

# WebDAV 网盘存储配置（地址、账号、密码）
WD_URL = os.environ["WEBDAV_URL"].rstrip("/")
WD_USER = os.environ["WEBDAV_USER"]
WD_PASS = os.environ["WEBDAV_PASS"]

# 飞书机器人的 Webhook 地址，用于发送通知消息
FEISHU_WEBHOOK = os.environ["FEISHU_WEBHOOK"]

# 每次运行最多下载多少个新作品（默认 30 个）
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "30"))

# 模拟浏览器发请求时的 User-Agent 伪装标识
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# 北京时间时区设置及当前日期
BJ = timezone(timedelta(hours=8))
DATE = datetime.now(BJ).strftime("%Y-%m-%d")

# 本地保存历史记录的文件名（防止重复下载同一个作品）
HIST_FILE = "history.json"

# ------------------------------------------------------------------------------
# 2. 全局状态初始化
# ------------------------------------------------------------------------------

# 读取历史下载记录
history = {}
if os.path.exists(HIST_FILE):
    try:
        history = json.load(open(HIST_FILE, encoding="utf-8"))
    except Exception:
        history = {}

fails = []       # 记录下载失败的项
used_names = set() # 记录本次运行中用到的文件名，防止重名覆盖
new_cnt = 0      # 本次成功保存的新作品数量
skip_cnt = 0     # 本次跳过的已保存作品数量

# ------------------------------------------------------------------------------
# 3. 辅助功能函数（消息通知、网盘操作、文件下载）
# ------------------------------------------------------------------------------

def push(title, content, retry=2):
    """
    【向飞书机器人发送通知】
    把消息发送到你的飞书群里，失败会自动重试。
    """
    text = f"{title}\n{content}".replace("<br>", "\n").replace("<b>", "").replace("</b>", "")
    for _ in range(retry + 1):
        try:
            r = requests.post(
                FEISHU_WEBHOOK,
                json={"msg_type": "text", "content": {"text": text[:3000]}},
                timeout=15
            )
            j = r.json()
            if r.status_code == 200 and j.get("code", j.get("StatusCode", -1)) == 0:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False

def p0(msg):
    """
    【发送紧急报警通知（P0级别）】
    当遇到 Cookie 失效、触发验证码等严重问题时触发。
    """
    push("【抖音报警】 " + msg[:60], f"<b>{msg}</b><br>日期：{DATE}<br>系统已暂停本次保存任务。")

def wd(path):
    """【拼接 WebDAV 网盘完整文件路径】"""
    return f"{WD_URL}/{path}"

def wd_mkdir(path):
    """
    【在 WebDAV 网盘上创建文件夹】
    如果文件夹已存在也不会报错。
    """
    try:
        requests.request("MKCOL", wd(path), auth=(WD_USER, WD_PASS), timeout=30)
    except Exception:
        pass

def wd_put(path, data, retry=3):
    """
    【上传文件内容到 WebDAV 网盘】
    上传失败会自动重试最多 3 次。
    """
    for i in range(retry):
        try:
            r = requests.put(
                wd(path),
                data=data,
                auth=(WD_USER, WD_PASS),
                headers={"Content-Type": "application/octet-stream"},
                timeout=600
            )
            if r.status_code in (200, 201, 204):
                return True
        except Exception:
            pass
        time.sleep(5 * (i + 1))
    return False

def fetch(url, retry=3):
    """
    【从网上下载文件/视频/图片的数据】
    带伪装请求头，下载失败会自动重试。
    """
    for i in range(retry):
        try:
            r = requests.get(
                url,
                headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"},
                timeout=300,
                stream=True
            )
            if r.status_code == 200:
                return r.content
        except Exception:
            pass
        time.sleep(5)
    return None

def guess_ext(url, default="jpg"):
    """
    【猜测文件后缀名】
    根据 URL 里的文件名尝试提取 .jpg/.mp4/.webp 等后缀，猜不到就用默认的。
    """
    m = re.search(r"\.(jpg|jpeg|png|webp|heic|mp4|mp3)(\?|$)", url)
    return m.group(1) if m else default

# ------------------------------------------------------------------------------
# 4. 抖音网页抓取与数据监听
# ------------------------------------------------------------------------------

collected = {}  # 存放抓取到的作品数据（按作品 ID 字典去重）
api_status = [] # 记录抖音接口返回的状态码

def on_resp(resp):
    """
    【网络请求监听器】
    当浏览器在后台访问抖音作品列表接口时，自动拦截并解析返回的 JSON 数据。
    """
    try:
        if "/aweme/v1/web/aweme/post/" in resp.url and resp.status == 200:
            j = resp.json()
            api_status.append(j.get("status_code"))
            for it in j.get("aweme_list") or []:
                collected[it["aweme_id"]] = it
    except Exception:
        pass

def crawl():
    """
    【核心抓取逻辑：用无头浏览器打开抖音主页并模拟滚动下滑】
    """
    with sync_playwright() as p:
        # 启动无头 Chrome 浏览器
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})

        # 将配置好的 Cookie 写入浏览器上下文中
        cookies = []
        for kv in COOKIE.split(";"):
            kv = kv.strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                cookies.append({"name": k.strip(), "value": v.strip(), "domain": ".douyin.com", "path": "/"})
        ctx.add_cookies(cookies)

        page = ctx.new_page()
        # 监听页面的所有网络响应
        page.on("response", on_resp)

        # 循环打开每一个博主的主页
        for u in SHARE_URLS:
            page.goto(u, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_url("**/user/**", timeout=30000)
            except Exception:
                pass
            time.sleep(5)

            # 找到页面真正的滚动区域（适配抖音网页版各种结构）
            page.mouse.move(720, 700)
            page.evaluate("""() => { let b = null; for (const e of document.querySelectorAll('*')) { if (e.scrollHeight > e.clientHeight + 100 && e.clientHeight > 200) { if (!b || e.scrollHeight > b.scrollHeight) b = e; } } window.__sc = b || document.scrollingElement; }""")

            # 循环向下滚动页面以加载更多历史作品
            empty = 0
            while empty < 8 and len(collected) < MAX_PER_RUN * 3:
                before = len(collected)
                page.evaluate("window.__sc.scrollTop = window.__sc.scrollHeight")
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(4000)
                empty = 0 if len(collected) > before else empty + 1

        # 检查是否触碰到了风险控制（验证码页面）
        if ("verify" in page.url) or ("captcha" in page.url):
            p0("触发抖音验证码/风控限制，系统已自动暂停本次运行。")
            browser.close(); sys.exit(5)

        try:
            page.wait_for_url("**/user/**", timeout=30000)
        except Exception:
            pass
        time.sleep(5)

        # 检查 Cookie 是否过期（接口返回非 0 状态码）
        if api_status and all(s not in (0,) for s in api_status):
            p0(f"抖音 Cookie 疑似失效（接口状态码={api_status[0]}），请更新 DOUYIN_COOKIE 环境变量。")
            browser.close(); sys.exit(2)

        # 检查是否被弹出了强制登录框
        if not collected:
            body = page.content()
            if "passport" in page.url or ("登录" in body and len(body) < 50000):
                p0("抖音 Cookie 疑似失效（页面弹出了登录拦截），请更新 DOUYIN_COOKIE 环境变量。")
                browser.close(); sys.exit(2)

        # 补充滚动几轮确保拿到最新数据
        empty_rounds = 0
        while len(collected) < MAX_PER_RUN * 3 and empty_rounds < 6:
            before = len(collected)
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(2000 + random.randint(500, 2000))
            empty_rounds = empty_rounds + 1 if len(collected) == before else 0

        _b = page.content()
        _cap = ("验证码" in _b) or ("captcha" in _b.lower())
        print(f"[diag] bodylen={len(_b)} api={api_status} items={len(collected)} cap={_cap}")
        browser.close()

# ------------------------------------------------------------------------------
# 5. 作品元数据处理函数（获取作者名、封面、头像管理）
# ------------------------------------------------------------------------------

def author_of(item):
    """
    【获取并清理作者昵称】
    去除昵称里的特殊非法字符，避免做文件夹名称时报错。
    """
    return re.sub(r"[^\w.-]+", "_", (((item.get("author") or {}).get("nickname")) or "unknown"))[:30] or "unknown"

def cover_of(item):
    """
    【提取作品的封面图链接和格式】
    """
    cover_url = None
    cover_ext = "jpg"
    if item.get("video"):
        cover_urls = ((item["video"].get("origin_cover") or item["video"].get("cover") or {}).get("url_list")) or []
        if cover_urls:
            cover_url = cover_urls[0]
    images = item.get("images") or []
    if images:
        c_urls = images[0].get("url_list") or []
        if c_urls:
            cover_url = re.sub(r"~tplv-[^?]+", "~tplv-dy-aweme-original:jpeg", c_urls[-1])
            cover_ext = guess_ext(c_urls[-1])
    return cover_url, cover_ext

def housekeep(item):
    """
    【日常清理与更新：创建博主文件夹、同步博主最新高清头像、补齐未保存的封面】
    """
    hid = item["aweme_id"]
    nick = author_of(item)
    folder = f"douyin/{nick}"
    wd_mkdir(folder)
    wd_mkdir(f"{folder}/封面")

    # 获取并备份博主头像（如果头像更新了，会自动下载新的）
    _au = (item.get("author") or {})
    _av = _au.get("avatar_larger") or _au.get("avatar_medium") or _au.get("avatar_thumb") or {}
    av_uri = _av.get("uri") or ""
    av_url = re.sub(r"/\d+x\d+/", "/1080x1080/", (_av.get("url_list") or [""])[0])
    if av_uri and history.get("_avatars", {}).get(nick) != av_uri:
        stamp = datetime.now(BJ).strftime("%Y-%m-%d")
        if av_url and wd_put(f"{folder}/avatar_{stamp}.jpg", fetch(av_url, retry=1)):
            history.setdefault("_avatars", {})[nick] = av_uri

    # 如果历史记录里这个作品还没有封面，补保存封面
    rec = history.get(hid)
    if isinstance(rec, dict) and not rec.get("cover"):
        cdate = datetime.fromtimestamp(int(item.get("create_time") or 0), BJ).strftime("%Y-%m-%d") if item.get("create_time") else DATE
        safe = re.sub(r"[^\w.-]+", "_", ((item.get("desc") or "")[:40]).strip()) or "untitled"
        cover_url, cover_ext = cover_of(item)
        if cover_url:
            cd = fetch(cover_url, retry=1)
            if cd and wd_put(f"{folder}/封面/{cdate}_{safe}.{cover_ext}", cd):
                rec["cover"] = True

# ------------------------------------------------------------------------------
# 6. 单个作品下载处理逻辑（包含视频/图集/动图/音轨下载及网盘上传）
# ------------------------------------------------------------------------------

def process(item):
    """
    【处理单个作品的下载全流程】
    """
    global new_cnt
    hid = item["aweme_id"]

    # 格式化发布日期与安全的文件夹/文件名
    cdate = datetime.fromtimestamp(int(item.get("create_time") or 0), BJ).strftime("%Y-%m-%d") if item.get("create_time") else DATE
    safe = re.sub(r"[^\w.-]+", "_", ((item.get("desc") or "")[:40]).strip()) or "untitled"
    aid = f"{cdate}_{safe}"

    # 防止重名，重复时自动在文件名末尾加上 (1), (2)
    n = 1
    while aid in used_names:
        aid = f"{cdate}_{safe}({n})"; n += 1
    used_names.add(aid)

    desc = (item.get("desc") or "")[:40]
    files = []
    gear_info = None
    nick = author_of(item)
    folder = f"douyin/{nick}"

    wd_mkdir(folder)
    wd_mkdir(f"{folder}/封面")

    images = item.get("images") or []
    if images:
        # ---------------- 图集作品处理 ----------------
        for i, img in enumerate(images):
            urls = img.get("url_list") or img.get("download_url_list") or []
            if not urls:
                continue

            # 优先检查是否是动图/实况图片（Live Photo）视频
            lv = ((img.get("video") or {}).get("play_addr") or {}).get("url_list") or []
            live_ok = False
            if lv:
                vd = fetch(lv[0])
                if vd and wd_put(f"{folder}/{aid}_img{i}_live.mp4", vd):
                    files.append(f"{folder}/{aid}_img{i}_live.mp4")
                    live_ok = True
                else:
                    fails.append(f"{aid} 图{i} 动图/LivePhoto保存失败")

            # 如果不是动图，下载原图
            if not live_ok:
                data = fetch(re.sub(r"~tplv-[^?]+", "~tplv-dy-aweme-original:jpeg", urls[-1]), retry=1) or fetch(urls[-1])
                if data is None:
                    fails.append(f"{aid} 图{i} 原图下载失败"); continue
                path = f"{folder}/{aid}_img{i}.{guess_ext(urls[-1])}"
                if wd_put(path, data):
                    files.append(path)
                else:
                    fails.append(f"{aid} 图{i} 网盘上传失败")
    else:
        # ---------------- 视频作品处理 ----------------
        video = item.get("video") or {}
        brs = video.get("bit_rate") or []
        url = None

        # 挑选最高码率/画质的视频播放链接
        if brs:
            best = max(brs, key=lambda b: b.get("bit_rate", 0))
            url = ((best.get("play_addr") or {}).get("url_list") or [None])[0]
            gear_info = {
                "chosen_gear": best.get("gear_name"),
                "chosen_bitrate": best.get("bit_rate"),
                "resolution": f"{video.get('width')}x{video.get('height')}",
                "all_gears": [[b.get("gear_name"), b.get("bit_rate")] for b in brs]
            }
        print(f"[quality] {aid} -> {gear_info}")

        if not url:
            url = ((video.get("play_addr") or {}).get("url_list") or [None])[0]

        if url:
            # 尝试调用 knock.py 里的 try1080 抓取 1080P 超高清画质
            t = try1080(video, gear_info, fetch)
            vd, gear_info = t[0] or fetch(url), t[1]
            if vd is None:
                fails.append(f"{aid} 视频下载失败")
            elif wd_put(f"{folder}/{aid}_video.mp4", vd):
                files.append(f"{folder}/{aid}_video.mp4")
            else:
                fails.append(f"{aid} 视频网盘上传失败")

        # 音乐背景音轨处理（留空可选扩展）
        mu = ((item.get("music") or {}).get("play_url") or {}).get("url_list") or []
        if False:
            md = fetch(mu[0])
            if md and wd_put(f"{folder}/{aid}_music.mp3", md):
                files.append(f"{folder}/{aid}_music.mp3")

    # 保存作品封面
    cover_ok = False
    cover_url, cover_ext = cover_of(item)
    if cover_url:
        cd = fetch(cover_url, retry=1)
        if cd and wd_put(f"{folder}/封面/{aid}.{cover_ext}", cd):
            cover_ok = True

    # 如果有文件保存成功，记入历史记录
    if files:
        history[hid] = {"date": DATE, "files": len(files), "desc": desc, "cover": cover_ok}
        new_cnt += 1
        return True
    return False

# ------------------------------------------------------------------------------
# 7. 主程序入口与整体运行流程
# ------------------------------------------------------------------------------

def main():
    global new_cnt, skip_cnt

    # 1. 开始第一轮抓取
    crawl()

    # 如果第一轮没抓到任何作品，自动等待 45 秒后重试一次
    if not collected:
        print("[warn] Empty first round, auto-retrying in 45s")
        time.sleep(45)
        crawl()

    items = list(collected.values())
    print(f"[info] Fetched {len(items)} items, history has {len(history)} items")

    # 异常防御：如果抓取结果为空且有历史记录，说明可能触发风控或主页异常
    if not items and history:
        p0("抓取作品列表为空：未获取到任何作品，但本地已有历史记录。可能触发了抖音风控或博主主页结构变化。")
        sys.exit(3)
    if not items and not history:
        p0("首次运行未获取到任何作品：请检查 DOUYIN_URL 与 DOUYIN_COOKIE 配置是否正确。")
        sys.exit(4)

    # 2. 遍历抓取到的作品列表，逐个对比历史记录并下载
    done = 0
    for it in items:
        if done >= MAX_PER_RUN:
            break
        aid = it.get("aweme_id")
        if not aid:
            continue

        housekeep(it) # 基础维护（更新博主头像、封面）

        # 如果这个作品已经下载过，直接跳过
        if aid in history:
            skip_cnt += 1
            continue

        try:
            # 处理并下载新作品
            if process(it):
                done += 1
                print(f"[ok] {aid} saved")
        except Exception as e:
            fails.append(f"{aid} 处理异常：{e}")

        # 随机暂停 3~8 秒，避免下载过快被抖音服务器封禁
        time.sleep(random.randint(3, 8))

    # 3. 将最新的历史记录保存回本地文件
    json.dump(history, open(HIST_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[info] This run: New {new_cnt}, Skipped {skip_cnt}, Failed {len(fails)}")

    # 4. 根据运行结果发送飞书通知
    if new_cnt or fails:
        lines = "<br>".join(f"- {f}" for f in fails[:5]) or "无"
        push(f"【抖音备份日报】 新增 {new_cnt} 个，跳过 {skip_cnt} 个，失败 {len(fails)} 个",
             f"运行日期：{DATE}<br>新增备份：{new_cnt} 个<br>跳过重复：{skip_cnt} 个<br>失败明细：<br>{lines}")
    else:
        push("【抖音备份日报】", f"本次运行未发现新发布的作品，所有作品均已备份或跳过，系统运行正常。<br>运行日期：{DATE}")

if __name__ == "__main__":
    main()
