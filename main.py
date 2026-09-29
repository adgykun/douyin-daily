# ==============================================================================
# main.py - 抖音视频/图集自动备份与同步工具
#
# 【这个脚本是干嘛的？】
# 这是一个自动帮你备份抖音创作者作品的程序。
# 它可以自动打开抖音网页、找到你关注的博主主页，把他们最新发布的视频、图集、封面
# 以及头像全自动下载下来，保存到你的个人网盘（WebDAV）里，
# 并且还会通过飞书机器人给你发送每日运行报告。
# ==============================================================================

import os, re, json, time, random, sys, tempfile
from datetime import datetime, timezone, timedelta
import requests
from knock import try1080  # 引入用于尝试获取 1080P 画质视频的辅助函数
from playwright.sync_api import sync_playwright  # 引入 Playwright 网页自动化工具

# ------------------------------------------------------------------------------
# 1. 配置项读取（从系统的环境变量中获取你设置好的密钥和参数）
# ------------------------------------------------------------------------------

# 抖音登录凭证（Cookie），脚本靠它以登录状态访问抖音
COOKIE = os.environ.get("DOUYIN_COOKIE", "")

# 全局 Cookie 失效状态标识（若配置为空或运行中检测到异常则设为 True）
is_cookie_invalid = not bool(COOKIE.strip())

def parse_douyin_urls(raw_text):
    """
    【解析并清洗博主主页链接】
    支持单条/多条链接，自动从分享文字、空格或杂质字符中提取合法 HTTP/HTTPS 网址，
    兼容逗号（中英文）、分号（中英文）、顿号、句号、感叹号、括号、换行、空格等多种分隔符。
    """
    if not raw_text:
        return []
    # 使用正则表达式匹配出所有 http:// 或 https:// 链接（排除中英文常见标点与界定符）
    found = re.findall(r'https?://[^\s,\n\r，;；"\'<>（）()【】《》「」『』“”‘’。！？：、]+', raw_text)
    urls = []
    for u in found:
        # 移除参数 query 及末尾常见的标点或符号
        clean_u = u.split("?")[0].rstrip(".,;:;!?，；！？。：、\"'()（）[]【】{}<>《》「」『』")
        if "douyin.com" not in clean_u:
            continue
        if clean_u and clean_u not in urls:
            urls.append(clean_u)
    return urls

# 需要备份的抖音博主主页链接列表
SHARE_URLS = parse_douyin_urls(os.environ.get("DOUYIN_URL", ""))

# WebDAV 网盘存储配置（地址、账号、密码）
WD_URL = os.environ.get("WEBDAV_URL", "").rstrip("/")
WD_USER = os.environ.get("WEBDAV_USER", "")
WD_PASS = os.environ.get("WEBDAV_PASS", "")

# 飞书机器人的 Webhook 地址，用于发送通知消息
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")

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
        with open(HIST_FILE, encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        history = {}

def save_history():
    tmp = HIST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HIST_FILE)

fails = []          # 记录下载失败的项
used_names = set()  # 记录本次运行中用到的文件名，防止重名覆盖
new_cnt = 0         # 本次成功保存的新作品数量
skip_cnt = 0        # 本次跳过的已保存作品数量
refetch_cnt = 0     # 本次恢复 Cookie 后重新下载的高清作品数量
url_author_map = {} # 记录博主 URL 与昵称的映射关系
last_fetch_error = "" # 记录最近一次网络抓取失败的原因
last_wd_error = ""    # 记录最近一次 WebDAV 网盘上传失败的原因

# ------------------------------------------------------------------------------
# 3. 辅助功能函数与飞书播报系统（报警推送 & 标准战报 & Cookie提醒）
# ------------------------------------------------------------------------------

def push(title, content, retry=2):
    text = content.replace("<br>", "\n").replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
    lines = text.split("\n")
    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title,
                    "content": [[{"tag": "text", "text": line}] for line in lines if line.strip()]
                }
            }
        }
    }
    for _ in range(retry + 1):
        try:
            r = requests.post(FEISHU_WEBHOOK, json=payload, timeout=15)
            j = r.json()
            if r.status_code == 200 and j.get("code", j.get("StatusCode", -1)) == 0:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False

def p0(msg, detail="", err_type="UNKNOWN"):
    """
    【发送超详细紧急报警通知（P0级别）】
    当遇到严重不可逆故障时触发。
    提供故障时间、错误归类、根因诊断与逐步排查修复指引。
    """
    now_str = datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")
    title = f"🚨【抖音特工急报】{msg} 🚨"
    content = (
        f"⏰ <b>发生时间：</b>{now_str}<br>"
        f"🏷️ <b>故障类型：</b>{err_type}<br>"
        f"🔍 <b>根因诊断：</b><br>{detail.replace(chr(10), '<br>')}<br><br>"
        f"🛠️ <b>逐步排查与修复建议：</b><br>"
        f"  1. 🔑 登录 GitHub 仓库进入 Settings -> Secrets and variables -> Actions<br>"
        f"  2. 📝 检查并更新对应的 DOUYIN_COOKIE 或 DOUYIN_URL 密钥与变量<br>"
        f"  3. 🌐 确认抖音网页版（douyin.com）账号登录状态正常且无验证码弹窗<br>"
        f"  4. 🚀 重新点击 Actions -> Run workflow 手动验证运行结果<br><br>"
        f"⚡️ <b>别慌！</b>系统已保护性处理本次保存任务~ 🛡️"
    )
    push(title, content)

def get_cookie_expired_banner():
    """
    【Cookie 失效每日提醒 Banner】
    当 Cookie 失效时，在飞书通知顶部或底部附加醒目的每日提醒与获取 Cookie 指引。
    """
    return (
        "⚠️ <b>【重点提醒：抖音 Cookie 已失效/未配置】</b> ⚠️<br>"
        "💡 <i>当前处于降级抓取模式：只抓取页面初始刷新到的视频，不进行页面下翻（防止触发验证码风控），视频标题已自动标注清晰度。恢复 Cookie 后系统将自动重新抓取高清版原视频！</i><br><br>"
        "🔑 <b>【如何获取并恢复 Cookie？】</b><br>"
        "  • <b>电脑端：</b><br>"
        "    1. 在 Chrome/Edge 浏览器安装 <b>Cookie-Editor</b> 插件。<br>"
        "    2. 打开并登录抖音网页版 (douyin.com)。<br>"
        "    3. 点击 Cookie-Editor 插件，选择 Export -> Export Header String (或直接复制 Cookie 字符串)。<br>"
        "  • <b>手机端：</b><br>"
        "    1. 下载并打开 <b>狐猴浏览器 (Lemur Browser)</b>。<br>"
        "    2. 在内置微软 Extension 插件商店搜索并安装 <b>Cookie-Editor</b> 插件。<br>"
        "    3. 在狐猴浏览器中打开并登录抖音网页版，点击插件复制 Cookie。<br>"
        "  • <b>更新方式：</b>前往 GitHub 仓库 -> Settings -> Secrets -> 更新 <b>DOUYIN_COOKIE</b>。<br>"
    )

def generate_daily_report(author_stats, fails):
    """
    【生成统一标准飞书每日巡视战报】
    包含：所有监控博主的抓取状态（已抓取/未抓取到）、抓取的总作品数、各类型及成功抓取数、重抓数、跳过作品数、失败作品数，以及全局总成功数、跳过数和失败数。
    若 Cookie 失效，也会在卡片中附加每日提示 Banner。
    """
    now_str = datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")

    # 计算全局汇总数据
    total_authors = len(author_stats)
    fetched_authors_cnt = sum(1 for s in author_stats.values() if s["total_fetched"] > 0)
    unfetched_authors_cnt = total_authors - fetched_authors_cnt

    total_success = sum(s["success_cnt"] for s in author_stats.values())
    total_refetch = sum(s.get("refetch_cnt", 0) for s in author_stats.values())
    total_skipped = sum(s["skip_cnt"] for s in author_stats.values())
    total_failed = sum(s["fail_cnt"] for s in author_stats.values())

    title = "🤖【抖音云端特工巡逻战报】"

    # 组装各博主的统计明细
    author_blocks = []
    if author_stats:
        for nick, s in author_stats.items():
            if s["total_fetched"] > 0:
                types_parts = []
                for t_name, t_cnt in s["types"].items():
                    if t_cnt > 0:
                        types_parts.append(f"{t_name} {t_cnt} 个")
                types_str = "，".join(types_parts) if types_parts else "无（全部跳过或未保存）"
                status_tag = "🟢 已抓取"
            else:
                types_str = "无（未抓取到作品/无新动态）"
                status_tag = "⚠️ 未抓取到"

            disp_nick = f"{nick}（{status_tag}）"

            blk = (
                f"👤 <b>博主昵称：</b>{disp_nick}<br>"
                f"  • 🔍 抓取作品总数：<b>{s['total_fetched']}</b> 个<br>"
                f"  • 📦 抓取成功类型：<b>{types_str}</b><br>"
                f"  • 🔄 恢复重抓高清：<b>{s.get('refetch_cnt', 0)}</b> 个<br>"
                f"  • ⏭️ 跳过重复作品：<b>{s['skip_cnt']}</b> 个<br>"
                f"  • ⚠️ 抓取失败作品：<b>{s['fail_cnt']}</b> 个"
            )
            author_blocks.append(blk)
    else:
        author_blocks.append("👀 本轮巡视未配置或未捕获到任何博主作品动态~")

    summary_block = (
        f"📊 <b>【全局战况汇总】</b><br>"
        f"  • 👥 监控博主总数：<b>{total_authors}</b> 位（已抓取：<b>{fetched_authors_cnt}</b> 位，未抓取到：<b>{unfetched_authors_cnt}</b> 位）<br>"
        f"  • 🟢 一共新增保存：<b>{total_success}</b> 个作品<br>"
        f"  • ✨ 恢复高清重抓：<b>{total_refetch}</b> 个作品<br>"
        f"  • ⏭️ 一共跳过重复：<b>{total_skipped}</b> 个作品<br>"
        f"  • ❌ 一共抓取失败：<b>{total_failed}</b> 个作品"
    )

    content_lines = [
        "⚡ 报告长官！巡逻特工已完成新一轮抖音博主搜捕任务，成果丰硕！<br>",
        f"⏰ <b>巡视时间：</b>{now_str}<br>",
        "👥 <b>【各博主详细战果清单】</b><br>" + "<br><br>".join(author_blocks) + "<br>",
        f"{summary_block}<br>"
    ]

    if fails:
        content_lines.append("❌ <b>失败明细与诊断提示：</b>")
        for f in fails[:8]:
            content_lines.append(f"  • ⚠️ {f}")
        if len(fails) > 8:
            content_lines.append(f"  • ...等共 {len(fails)} 项异常")
        content_lines.append("💡 <i>提示：若频繁失败，可能是网络波动或文件大小超出限制，系统将在下一轮重试。</i><br>")

    content_lines.append("🫡 特工小队归位，随时待命迎接下一轮巡逻！✨")

    # 如果 Cookie 失效，在通知顶部附加每日提醒 Banner
    if is_cookie_invalid:
        content_lines.insert(0, get_cookie_expired_banner() + "<br>----------------------------------------<br>")

    return title, "<br>".join(content_lines)


# ------------------------------------------------------------------------------
# 辅助网盘操作与文件下载函数
# ------------------------------------------------------------------------------

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
    上传失败会自动重试最多 3 次，并记录错误原因。
    """
    global last_wd_error
    last_wd_error = "未知网盘写入错误"
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
                last_wd_error = ""
                return True
            elif r.status_code == 401:
                last_wd_error = "WebDAV 认证失败（账号或应用密码错误）"
            elif r.status_code == 507:
                last_wd_error = "WebDAV 网盘存储空间已满"
            elif r.status_code == 404:
                last_wd_error = "WebDAV 目标文件夹不存在或路径错误"
            else:
                last_wd_error = f"WebDAV 网盘响应 HTTP {r.status_code}"
        except requests.exceptions.Timeout:
            last_wd_error = "WebDAV 网盘上传超时（网络波动或上传服务过慢）"
        except requests.exceptions.ConnectionError:
            last_wd_error = "WebDAV 网盘连接失败（无法建立网络连接）"
        except Exception as e:
            last_wd_error = f"WebDAV 网盘上传异常（{e}）"
        time.sleep(5 * (i + 1))
    return False

def fetch(url, retry=3):
    """
    【从网上下载文件/视频/图片的数据】
    带伪装请求头，下载失败会自动重试，并记录错误原因。
    """
    global last_fetch_error
    last_fetch_error = "未知网络下载错误"
    for i in range(retry):
        try:
            r = requests.get(
                url,
                headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"},
                timeout=300,
                stream=True
            )
            if r.status_code == 200:
                last_fetch_error = ""
                return r.content
            elif r.status_code == 403:
                last_fetch_error = "HTTP 403 拒绝访问（资源防盗链或链接失效）"
            elif r.status_code == 404:
                last_fetch_error = "HTTP 404 资源未找到（作品可能已被博主删除或隐藏）"
            else:
                last_fetch_error = f"HTTP Status {r.status_code} 服务器响应异常"
        except requests.exceptions.Timeout:
            last_fetch_error = "网络请求超时（服务器响应过慢或连接超时）"
        except requests.exceptions.ConnectionError:
            last_fetch_error = "网络连接失败（无法建立与服务器的连接）"
        except Exception as e:
            last_fetch_error = f"网络请求发生异常（{e}）"
        time.sleep(5)
    return None

def fetch_to_tmp(url, retry=3):
    global last_fetch_error
    last_fetch_error = "未知网络下载错误"
    for i in range(retry):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"}, timeout=300, stream=True)
            if r.status_code == 200:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tmp")
                for chunk in r.iter_content(chunk_size=1024*1024):
                    tmp.write(chunk)
                tmp.close()
                last_fetch_error = ""
                return tmp.name
            elif r.status_code == 403:
                last_fetch_error = "HTTP 403 拒绝访问（视频链接已过期或防盗链限制）"
            elif r.status_code == 404:
                last_fetch_error = "HTTP 404 视频资源未找到（作品可能已被下架或删除）"
            else:
                last_fetch_error = f"HTTP Status {r.status_code} 服务器响应异常"
        except requests.exceptions.Timeout:
            last_fetch_error = "视频下载超时（网络波动或数据流过大）"
        except requests.exceptions.ConnectionError:
            last_fetch_error = "视频下载连接中断（无法连接视频服务器）"
        except Exception as e:
            last_fetch_error = f"视频下载发生异常（{e}）"
        time.sleep(5)
    return None

def wd_put_file(remote_path, local_path, retry=3):
    global last_wd_error
    last_wd_error = "未知网盘写入错误"
    for i in range(retry):
        try:
            with open(local_path, "rb") as f:
                r = requests.put(wd(remote_path), data=f, auth=(WD_USER, WD_PASS), timeout=600)
                if r.status_code in (200, 201, 204):
                    last_wd_error = ""
                    return True
                elif r.status_code == 401:
                    last_wd_error = "WebDAV 认证失败（账号或密码错误）"
                elif r.status_code == 507:
                    last_wd_error = "WebDAV 网盘存储空间不足"
                elif r.status_code == 404:
                    last_wd_error = "WebDAV 目标路径不存在"
                else:
                    last_wd_error = f"WebDAV 网盘响应 HTTP {r.status_code}"
        except requests.exceptions.Timeout:
            last_wd_error = "WebDAV 大文件上传超时"
        except requests.exceptions.ConnectionError:
            last_wd_error = "WebDAV 网盘连接断开"
        except Exception as e:
            last_wd_error = f"WebDAV 上传文件发生异常（{e}）"
        time.sleep(5*(i+1))
    return False

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
    global is_cookie_invalid
    try:
        if ("/aweme/v1/web/aweme/post/" in resp.url or "/aweme/v2/web/aweme/post/" in resp.url) and resp.status == 200:
            j = resp.json()
            st = j.get("status_code")
            api_status.append(st)
            if st != 0:
                if sum(1 for s in api_status if s != 0) >= 2:
                    is_cookie_invalid = True
                    print(f"[warn] API returned non-zero status code ({st}) twice, marking Cookie invalid")
                else:
                    print(f"[warn] API returned non-zero status code ({st}), single occurrence, not marking invalid yet")
            for it in j.get("aweme_list") or []:
                collected[it["aweme_id"]] = it
    except Exception as e:
        print(f"[warn] on_resp parse error: {e}")

def crawl():
    """
    【核心抓取逻辑：用无头浏览器打开抖音主页并根据 Cookie 状态决定抓取模式】
    - Cookie 有效：正常向下滚动以加载全量作品。
    - Cookie 失效：降级抓取模式！不进行页面下翻（防止触发验证码风控与弹窗），仅保留页面刷新时获取到的视频作品。
    """
    global is_cookie_invalid, url_author_map, collected, api_status

    if not COOKIE.strip():
        is_cookie_invalid = True
        print("[crawl] No DOUYIN_COOKIE configured, running in degraded crawl mode.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})

        # 写入 Cookie
        if COOKIE.strip():
            cookies = []
            for kv in COOKIE.split(";"):
                kv = kv.strip()
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    cookies.append({"name": k.strip(), "value": v.strip(), "domain": ".douyin.com", "path": "/"})
            ctx.add_cookies(cookies)

        # 会话预热：种下前置 Cookie，避免第一个目标 URL 冷启动失败
        warm = ctx.new_page()
        try:
            warm.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            warm.wait_for_timeout(6000)
        except Exception as e:
            print(f"[warn] warm-up page failed: {e}")
        finally:
            warm.close()

        for u in SHARE_URLS:
            before_keys = set(collected.keys())
            page = ctx.new_page()
            page.on("response", on_resp)
            try:
                print(f"[crawl] Opening user URL ({'degraded' if is_cookie_invalid else 'normal'}): {u}")
                page.goto(u, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_url("**/user/**", timeout=30000)
                except Exception:
                    pass
                time.sleep(5)

                body = page.content()
                if "passport" in page.url or ("登录" in body and len(body) < 50000):
                    is_cookie_invalid = True
                    print("[crawl] Redirected or login prompt detected, marking Cookie invalid")

                # 如果 Cookie 失效，绝对不下翻滚动，只提取页面首次刷新获取到的视频！
                if is_cookie_invalid:
                    print("[crawl] Cookie invalid: Skip page scrolling to avoid risk control popups.")
                else:
                    # 正常 Cookie 模式：定位滚动区域并滚动页面
                    page.mouse.move(720, 700)
                    page.evaluate("""() => { let b = null; for (const e of document.querySelectorAll('*')) { if (e.scrollHeight > e.clientHeight + 100 && e.clientHeight > 200) { if (!b || e.scrollHeight > b.scrollHeight) b = e; } } window.__sc = b || document.scrollingElement; }""")

                    empty = 0
                    while empty < 8 and len(collected) < MAX_PER_RUN * 3:
                        before = len(collected)
                        page.evaluate("if (window.__sc) { window.__sc.scrollTop = window.__sc.scrollHeight; } else { window.scrollTo(0, document.body.scrollHeight); }")
                        page.mouse.wheel(0, 3000)
                        page.wait_for_timeout(4000)
                        empty = 0 if len(collected) > before else empty + 1

                new_keys = set(collected.keys()) - before_keys
                if not new_keys:
                    print(f"[crawl] No items from {u}, reload once for cold-start retry")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(8000)
                        if not is_cookie_invalid:
                            for _ in range(3):
                                b2 = len(collected)
                                page.evaluate("if (window.__sc) { window.__sc.scrollTop = window.__sc.scrollHeight; } else { window.scrollTo(0, document.body.scrollHeight); }")
                                page.mouse.wheel(0, 3000)
                                page.wait_for_timeout(4000)
                                if len(collected) > b2:
                                    break
                    except Exception as e:
                        print(f"[warn] reload retry failed: {e}")
                    new_keys = set(collected.keys()) - before_keys

                if new_keys:
                    sample_it = collected[list(new_keys)[0]]
                    url_author_map[u] = author_of(sample_it)

                # 检查验证码风控
                if ("verify" in page.url) or ("captcha" in page.url):
                    p0("触发抖音验证码/风控限制",
                       detail=f"访问页面触发风控重定向：{page.url}\n可能是短时间内请求过于频繁。系统已自动保护性暂停保存任务。",
                       err_type="CAPTCHA_RISK_CONTROL")
                    browser.close(); sys.exit(5)
            except Exception as e:
                print(f"[warn] Failed to open/crawl URL {u}: {e}")
                fails.append(f"博主链接【{u}】页面打开或抓取失败（原因：{e}，可能是网络连接超时或抖音页面结构变动）")
            finally:
                try:
                    page.close()
                except Exception:
                    pass

        time.sleep(2)

        # 检查是否因为 Cookie 失效导致 API 异常
        if api_status and all(s not in (0,) for s in api_status):
            is_cookie_invalid = True
            print(f"[crawl] API statuses all non-zero ({api_status}), marked Cookie invalid")

        print(f"[diag] api={api_status} items={len(collected)} cookie_invalid={is_cookie_invalid}")
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

    _au = (item.get("author") or {})
    _av = _au.get("avatar_larger") or _au.get("avatar_medium") or _au.get("avatar_thumb") or {}
    av_uri = _av.get("uri") or ""
    av_url = re.sub(r"/\d+x\d+/", "/1080x1080/", (_av.get("url_list") or [""])[0])
    if av_uri and history.get("_avatars", {}).get(nick) != av_uri:
        stamp = datetime.now(BJ).strftime("%Y-%m-%d")
        if av_url and wd_put(f"{folder}/avatar_{stamp}.jpg", fetch(av_url, retry=1)):
            history.setdefault("_avatars", {})[nick] = av_uri

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

def process(item, is_degraded=False, is_refetch=False):
    """
    【处理单个作品的下载全流程】
    - is_degraded: 是否为 Cookie 失效降级模式抓取。若为 True，将在文件名与标题上加注清晰度（如 [720P_Cookie失效降级]）。
    - is_refetch: 是否为恢复 Cookie 后的重新抓取。若为 True，将下载 1080P 高清版并更新覆盖网盘文件。
    """
    global new_cnt, refetch_cnt
    hid = item["aweme_id"]

    cdate = datetime.fromtimestamp(int(item.get("create_time") or 0), BJ).strftime("%Y-%m-%d") if item.get("create_time") else DATE
    safe = re.sub(r"[^\w.-]+", "_", ((item.get("desc") or "")[:40]).strip()) or "untitled"

    images = item.get("images") or []
    video_info = item.get("video") or {}

    # 确定清晰度字符串与标签
    if is_degraded:
        if images:
            clarity_str = "原图_Cookie失效降级"
        else:
            clarity_str = "默认画质_Cookie失效降级"
    else:
        if images:
            first_img = images[0] if images else {}
            iw = first_img.get("width") or 0
            ih = first_img.get("height") or 0
            clarity_str = f"{min(iw, ih)}P" if (iw and ih) else "原图"
        else:
            brs = video_info.get("bit_rate") or []
            if brs:
                best = max(brs, key=lambda b: b.get("bit_rate", 0))
                bw = best.get("play_addr", {}).get("width") or video_info.get("width") or 0
                bh = best.get("play_addr", {}).get("height") or video_info.get("height") or 0
            else:
                bw = video_info.get("width") or 0
                bh = video_info.get("height") or 0
            clarity_str = f"{min(bw, bh)}P" if (bw and bh) else "1080P"

    clarity_tag = f"[{clarity_str}]"

    # 封面文件名不标注清晰度，作品文件名通通标注清晰度
    base_name = f"{cdate}_{safe}"
    cover_aid = base_name
    aid = f"{base_name}_{clarity_tag}"

    # 防止重名
    n = 1
    while aid in used_names:
        cover_aid = f"{base_name}({n})"
        aid = f"{base_name}_{clarity_tag}({n})"
        n += 1
    used_names.add(aid)

    desc = (item.get("desc") or "")[:40]
    files = []
    gear_info = None
    nick = author_of(item)
    folder = f"douyin/{nick}"

    wd_mkdir(folder)
    wd_mkdir(f"{folder}/封面")

    if images:
        # ---------------- 图集作品处理 ----------------
        for i, img in enumerate(images):
            urls = img.get("url_list") or img.get("download_url_list") or []
            if not urls:
                continue

            lv = ((img.get("video") or {}).get("play_addr") or {}).get("url_list") or []
            live_ok = False
            if lv:
                vd = fetch(lv[0])
                if vd and wd_put(f"{folder}/{aid}_img{i}_live.mp4", vd):
                    files.append(f"{folder}/{aid}_img{i}_live.mp4")
                    live_ok = True
                else:
                    cause = last_fetch_error if not vd else last_wd_error
                    fails.append(f"作品【{aid}】图{i} LivePhoto动图下载或保存失败（原因：{cause or '动图地址无效或网盘写入失败'}）")

            if not live_ok:
                data = fetch(re.sub(r"~tplv-[^?]+", "~tplv-dy-aweme-original:jpeg", urls[-1]), retry=1) or fetch(urls[-1])
                if data is None:
                    fails.append(f"作品【{aid}】图{i} 原图下载失败（原因：{last_fetch_error or '图集图片链接已失效或网络连接超时'}）")
                    continue
                path = f"{folder}/{aid}_img{i}.{guess_ext(urls[-1])}"
                if wd_put(path, data):
                    files.append(path)
                else:
                    fails.append(f"作品【{aid}】图{i} WebDAV网盘上传失败（原因：{last_wd_error or '网盘连接超时或存储权限不足'}）")
    else:
        # ---------------- 视频作品处理 ----------------
        video = item.get("video") or {}
        brs = video.get("bit_rate") or []
        url = None

        if brs:
            best = max(brs, key=lambda b: b.get("bit_rate", 0))
            url = ((best.get("play_addr") or {}).get("url_list") or [None])[0]
            bw = best.get("play_addr", {}).get("width") or video_info.get("width") or 0
            bh = best.get("play_addr", {}).get("height") or video_info.get("height") or 0
            gear_info = {
                "chosen_gear": best.get("gear_name"),
                "chosen_bitrate": best.get("bit_rate"),
                "resolution": f"{bw}x{bh}",
                "all_gears": [[b.get("gear_name"), b.get("bit_rate")] for b in brs]
            }
        print(f"[quality] {aid} (degraded={is_degraded}) -> {gear_info}")

        if not url:
            url = ((video.get("play_addr") or {}).get("url_list") or [None])[0]

        if url:
            tmp = fetch_to_tmp(url)
            if tmp is None:
                fails.append(f"视频【{aid}】网络数据抓取失败（原因：{last_fetch_error or '视频播放地址已失效或网络请求超时'}）")
            elif wd_put_file(f"{folder}/{aid}_video.mp4", tmp):
                files.append(f"{folder}/{aid}_video.mp4")
                os.unlink(tmp)
            else:
                fails.append(f"视频【{aid}】WebDAV网盘上传失败（原因：{last_wd_error or '网盘连接超时或存储空间不足'}）")
                os.unlink(tmp)
        else:
            fails.append(f"视频【{aid}】无有效下载链接（原因：视频可能为私密作品或受版权保护无法提取播放地址）")

    cover_ok = False
    cover_url, cover_ext = cover_of(item)
    if cover_url:
        cd = fetch(cover_url, retry=1)
        if cd and wd_put(f"{folder}/封面/{cover_aid}.{cover_ext}", cd):
            cover_ok = True

    if files:
        history[hid] = {
            "date": DATE,
            "files": len(files),
            "desc": desc,
            "cover": cover_ok,
            "degraded": is_degraded,
            "clarity": clarity_tag if is_degraded else "1080P原画",
            "aid": aid
        }
        if is_refetch:
            refetch_cnt += 1
        else:
            new_cnt += 1
        return True
    return False

# ------------------------------------------------------------------------------
# 7. 主程序入口与整体运行流程
# ------------------------------------------------------------------------------

def main():
    global new_cnt, skip_cnt, refetch_cnt

    # 1. 开始第一轮抓取
    crawl()

    # 如果第一轮没抓到任何作品，自动等待 30 秒后重试一次
    if not collected and not is_cookie_invalid:
        print("[warn] Empty first round, auto-retrying in 30s")
        time.sleep(30)
        crawl()

    items = list(collected.values())
    print(f"[info] Fetched {len(items)} items (Cookie Invalid: {is_cookie_invalid}), history has {len(history)} items")

    author_stats = {}
    for u in SHARE_URLS:
        label = url_author_map.get(u, u)
        if label not in author_stats:
            author_stats[label] = {
                "total_fetched": 0,
                "types": {"视频": 0, "图集": 0},
                "success_cnt": 0,
                "refetch_cnt": 0,
                "skip_cnt": 0,
                "fail_cnt": 0
            }

    for it in items:
        nick = author_of(it)
        if nick not in author_stats:
            author_stats[nick] = {
                "total_fetched": 0,
                "types": {"视频": 0, "图集": 0},
                "success_cnt": 0,
                "refetch_cnt": 0,
                "skip_cnt": 0,
                "fail_cnt": 0
            }
        author_stats[nick]["total_fetched"] += 1

    # 2. 遍历抓取到的作品列表，逐个对比历史记录并处理
    done = 0
    for it in items:
        if done >= MAX_PER_RUN:
            break
        hid = it.get("aweme_id")
        if not hid:
            continue

        nick = author_of(it)
        itype = "图集" if it.get("images") else "视频"

        housekeep(it) # 基础维护

        # 检查是否已在历史记录中
        hist_rec = history.get(hid)
        is_previously_degraded = isinstance(hist_rec, dict) and hist_rec.get("degraded") is True

        # 如果在 Cookie 正常状态下，发现之前降级抓取过的作品，则重新抓取高清版！
        if is_previously_degraded and not is_cookie_invalid:
            print(f"[refetch] Cookie restored! Re-fetching high quality for item: {hid}")
            try:
                if process(it, is_degraded=False, is_refetch=True):
                    done += 1
                    author_stats[nick]["refetch_cnt"] += 1
                    author_stats[nick]["types"][itype] = author_stats[nick]["types"].get(itype, 0) + 1
                    print(f"[refetch-ok] {hid} restored to full quality")
                else:
                    author_stats[nick]["fail_cnt"] += 1
            except Exception as e:
                fails.append(f"作品【{hid}】重新抓取高清发生异常（原因：{e}）")
                author_stats[nick]["fail_cnt"] += 1
            time.sleep(random.randint(2, 5))
            continue

        # 如果已经完全保存过且非降级模式需要恢复，则跳过
        if hid in history:
            skip_cnt += 1
            author_stats[nick]["skip_cnt"] += 1
            continue

        try:
            # 处理新作品（如果当前 Cookie 失效，则以降级模式保存并加注清晰度）
            if process(it, is_degraded=is_cookie_invalid, is_refetch=False):
                done += 1
                author_stats[nick]["success_cnt"] += 1
                author_stats[nick]["types"][itype] = author_stats[nick]["types"].get(itype, 0) + 1
                print(f"[ok] {hid} saved (degraded={is_cookie_invalid})")
            else:
                author_stats[nick]["fail_cnt"] += 1
        except Exception as e:
            fails.append(f"作品【{hid}】处理发生异常（原因：{e}）")
            author_stats[nick]["fail_cnt"] += 1

        time.sleep(random.randint(3, 8))

    # 3. 保存历史记录
    save_history()

    tot_success = sum(s["success_cnt"] for s in author_stats.values())
    tot_refetch = sum(s["refetch_cnt"] for s in author_stats.values())
    tot_skipped = sum(s["skip_cnt"] for s in author_stats.values())
    tot_failed = sum(s["fail_cnt"] for s in author_stats.values())
    print(f"[info] Run summary: Success {tot_success}, Refetched {tot_refetch}, Skipped {tot_skipped}, Failed {tot_failed}")

    # 4. 生成日报并推送
    title, content = generate_daily_report(author_stats, fails)
    push(title, content)

if __name__ == "__main__":
    main()
