import os, re, json, time, random, sys
from datetime import datetime, timezone, timedelta
import requests; from knock import try1080
from playwright.sync_api import sync_playwright

COOKIE = os.environ["DOUYIN_COOKIE"]
SHARE_URLS = [u.split("?")[0].strip() for u in os.environ["DOUYIN_URL"].replace(",", "\n").splitlines() if u.strip()]
WD_URL = os.environ["WEBDAV_URL"].rstrip("/")
WD_USER = os.environ["WEBDAV_USER"]
WD_PASS = os.environ["WEBDAV_PASS"]
FEISHU_WEBHOOK = os.environ["FEISHU_WEBHOOK"]
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "30"))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BJ = timezone(timedelta(hours=8))
DATE = datetime.now(BJ).strftime("%Y-%m-%d")
HIST_FILE = "history.json"

history = {}
if os.path.exists(HIST_FILE):
    try:
        history = json.load(open(HIST_FILE, encoding="utf-8"))
    except Exception:
        history = {}

fails = []
used_names = set()
run_records = []
new_cnt = 0
skip_cnt = 0

def push(title, content, retry=2):
    text = f"{title}\n{content}".replace("<br>", "\n").replace("<b>", "").replace("</b>", "")
    for _ in range(retry + 1):
        try:
            r = requests.post(FEISHU_WEBHOOK,
                json={"msg_type": "text", "content": {"text": text[:3000]}}, timeout=15)
            j = r.json()
            if r.status_code == 200 and j.get("code", j.get("StatusCode", -1)) == 0:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False

def p0(msg):
    push("【抖音P0】" + msg[:60], f"<b>{msg}</b><br>日期:{DATE}<br>处理前系统暂停新增保存。")

def wd(path):
    return f"{WD_URL}/{path}"

def wd_mkdir(path):
    try:
        requests.request("MKCOL", wd(path), auth=(WD_USER, WD_PASS), timeout=30)
    except Exception:
        pass

def wd_put(path, data, retry=3):
    for i in range(retry):
        try:
            r = requests.put(wd(path), data=data, auth=(WD_USER, WD_PASS),
                             headers={"Content-Type": "application/octet-stream"}, timeout=600)
            if r.status_code in (200, 201, 204):
                return True
        except Exception:
            pass
        time.sleep(5 * (i + 1))
    return False

def fetch(url, retry=3):
    for i in range(retry):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"},
                             timeout=300, stream=True)
            if r.status_code == 200:
                return r.content
        except Exception:
            pass
        time.sleep(5)
    return None

def guess_ext(url, default="jpg"):
    m = re.search(r"\.(jpg|jpeg|png|webp|heic|mp4|mp3)(\?|$)", url)
    return m.group(1) if m else default

collected = {}
api_status = []

def on_resp(resp):
    try:
        if "/aweme/v1/web/aweme/post/" in resp.url and resp.status == 200:
            j = resp.json()
            api_status.append(j.get("status_code"))
            for it in j.get("aweme_list") or []:
                collected[it["aweme_id"]] = it
    except Exception:
        pass

def crawl():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})
        cookies = []
        for kv in COOKIE.split(";"):
            kv = kv.strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                cookies.append({"name": k.strip(), "value": v.strip(), "domain": ".douyin.com", "path": "/"})
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.on("response", on_resp)        
        for u in SHARE_URLS:
            page.goto(u, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_url("**/user/**", timeout=30000)
            except Exception:
                pass
            time.sleep(5)
            page.mouse.move(720, 700)
            page.evaluate("""() => { let b = null; for (const e of document.querySelectorAll('*')) { if (e.scrollHeight > e.clientHeight + 100 && e.clientHeight > 200) { if (!b || e.scrollHeight > b.scrollHeight) b = e; } } window.__sc = b || document.scrollingElement; }""")
            empty = 0
            while empty < 8 and len(collected) < MAX_PER_RUN * 3:
                before = len(collected)
                page.evaluate("window.__sc.scrollTop = window.__sc.scrollHeight")
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(4000)
                empty = 0 if len(collected) > before else empty + 1
        if ("verify" in page.url) or ("captcha" in page.url):
            p0("触发抖音验证码风控:本轮暂停,下一班自动再试。")
            browser.close(); sys.exit(5)
        try:
            page.wait_for_url("**/user/**", timeout=30000)
        except Exception:
            pass
        time.sleep(5)
        if api_status and all(s not in (0,) for s in api_status):
            p0(f"Cookie 疑似失效(API status_code={api_status[0]}),请更新 Secrets 中 DOUYIN_COOKIE。")
            browser.close(); sys.exit(2)
        if not collected:
            body = page.content()
            if "passport" in page.url or ("登录" in body and len(body) < 50000):
                p0("Cookie 疑似失效(弹出登录墙),请更新 DOUYIN_COOKIE。")
                browser.close(); sys.exit(2)
        empty_rounds = 0
        while len(collected) < MAX_PER_RUN * 3 and empty_rounds < 6:
            before = len(collected)
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(2000 + random.randint(500, 2000))
            empty_rounds = empty_rounds + 1 if len(collected) == before else 0
        _b = page.content()
        print(f"[diag] bodylen={len(_b)} api={api_status} items={len(collected)} cap={'验证码' in _b or 'captcha' in _b.lower()}")
        browser.close()

def process(item):
    global new_cnt
    hid = item["aweme_id"]
    cdate = datetime.fromtimestamp(int(item.get("create_time") or 0), BJ).strftime("%Y-%m-%d") if item.get("create_time") else DATE
    safe = re.sub(r"[^\w.-]+", "_", ((item.get("desc") or "")[:40]).strip()) or "untitled"
    aid = f"{cdate}_{safe}"
    n = 1
    while aid in used_names:
        aid = f"{cdate}_{safe}({n})"; n += 1
    used_names.add(aid)
    desc = (item.get("desc") or "")[:40]
    files = []
    gear_info = None
    images = item.get("images") or []
    if images:
        for i, img in enumerate(images):
            urls = img.get("url_list") or img.get("download_url_list") or []
            if not urls:
                continue
            lv = ((img.get("video") or {}).get("play_addr") or {}).get("url_list") or []
            live_ok = False
            if lv:
                vd = fetch(lv[0])
                if vd and wd_put(f"douyin/{DATE}/{aid}_img{i}_live.mp4", vd):
                    files.append(f"douyin/{DATE}/{aid}_img{i}_live.mp4")
                    live_ok = True
                else:
                    fails.append(f"{aid} 图{i} 动态失败")
            if not live_ok:
                data = fetch(re.sub(r"~tplv-[^?]+", "~tplv-dy-aweme-original:jpeg", urls[-1]), retry=1) or fetch(urls[-1])
                if data is None:
                    fails.append(f"{aid} 图{i} 下载失败"); continue
                path = f"douyin/{DATE}/{aid}_img{i}.{guess_ext(urls[-1])}"
                if wd_put(path, data):
                    files.append(path)
                else:
                    fails.append(f"{aid} 图{i} 上传失败")
    else:
        video = item.get("video") or {}
        brs = video.get("bit_rate") or []
        url = None
        if brs:
            best = max(brs, key=lambda b: b.get("bit_rate", 0))
            url = ((best.get("play_addr") or {}).get("url_list") or [None])[0]
            gear_info = {"chosen_gear": best.get("gear_name"), "chosen_bitrate": best.get("bit_rate"),
                         "resolution": f"{video.get('width')}x{video.get('height')}",
                         "all_gears": [[b.get("gear_name"), b.get("bit_rate")] for b in brs]}
        print(f"[quality] {aid} -> {gear_info}")
        if not url:
            url = ((video.get("play_addr") or {}).get("url_list") or [None])[0]
        if url:
            t = try1080(video, gear_info, fetch); vd, gear_info = t[0] or fetch(url), t[1]
            if vd is None:
                fails.append(f"{aid} 视频下载失败")
            elif wd_put(f"douyin/{DATE}/{aid}_video.mp4", vd):
                files.append(f"douyin/{DATE}/{aid}_video.mp4")
            else:
                fails.append(f"{aid} 视频上传失败")
        mu = ((item.get("music") or {}).get("play_url") or {}).get("url_list") or []
        if False:
            md = fetch(mu[0])
            if md and wd_put(f"douyin/{DATE}/{aid}_music.mp3", md):
                files.append(f"douyin/{DATE}/{aid}_music.mp3")
    run_records.append({"aweme_id": hid, "title": desc, "publish_date": cdate,
                        "quality": gear_info, "files": files,
                        "stats": {k: (item.get("statistics") or {}).get(k) for k in ("digg_count", "comment_count", "share_count")}})
    if files:
        history[hid] = {"date": DATE, "files": len(files), "desc": desc}
        new_cnt += 1
        return True
    return False

def main():
    global new_cnt, skip_cnt
    crawl()
    if not collected:
        print("[warn] 首轮空手,45秒后自动重试一次")
        time.sleep(45)
        crawl()
    items = list(collected.values())
    print(f"[info] 抓到作品 {len(items)} 条,历史已存 {len(history)} 条")
    if not items and history:
        p0("异常空列表:本轮未抓到任何作品但历史有记录,可能是风控或主页变动,请检查。")
        sys.exit(3)
    if not items and not history:
        p0("首跑未抓到任何作品:请检查 DOUYIN_URL 与 DOUYIN_COOKIE 是否正确。")
        sys.exit(4)
    wd_mkdir(f"douyin/{DATE}")
    done = 0
    for it in items:
        if done >= MAX_PER_RUN:
            break
        aid = it.get("aweme_id")
        if not aid:
            continue
        if aid in history:
            skip_cnt += 1
            continue
        try:
            if process(it):
                done += 1
                print(f"[ok] {aid} 已保存")
        except Exception as e:
            fails.append(f"{aid} 异常:{e}")
        time.sleep(random.randint(3, 8))
    json.dump(history, open(HIST_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if run_records:
        stamp = datetime.now(BJ).strftime("%Y-%m-%d_%H%M%S")
        summary = {"crawl_time": stamp, "count": len(run_records), "works": run_records}
        wd_put(f"douyin/{DATE}/汇总_{stamp}.json", json.dumps(summary, ensure_ascii=False, indent=2).encode())
    print(f"[info] 本轮:新增{new_cnt} 跳过{skip_cnt} 失败{len(fails)}")
    if new_cnt or fails:
        lines = "<br>".join(f"· {f}" for f in fails[:5]) or "无"
        push(f"【抖音日报】新增{new_cnt} 跳过{skip_cnt} 失败{len(fails)}",
             f"日期:{DATE}<br>新增:{new_cnt} 跳过:{skip_cnt}<br>失败明细:<br>{lines}")

main()
