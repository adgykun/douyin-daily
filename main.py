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
COOKIE = os.environ.get("DOUYIN_COOKIE", "")

def parse_douyin_urls(raw_text):
    """
    【解析并清洗博主主页链接】
    支持单条/多条链接，自动从分享文字、空格或杂质字符中提取合法 HTTP/HTTPS 网址，
    兼容逗号（中英文）、分号（中英文）、换行、空格等多种分隔符。
    """
    if not raw_text:
        return []
    # 使用正则表达式匹配出所有 http:// 或 https:// 链接
    found = re.findall(r'https?://[^\s,\n\r，;；"\'<>（）()]+', raw_text)
    urls = []
    for u in found:
        # 移除参数 query 及末尾常见的标点或符号
        clean_u = u.split("?")[0].rstrip(".,;:;，；\"'()（）")
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
        history = json.load(open(HIST_FILE, encoding="utf-8"))
    except Exception:
        history = {}

fails = []       # 记录下载失败的项
used_names = set() # 记录本次运行中用到的文件名，防止重名覆盖
new_cnt = 0      # 本次成功保存的新作品数量
skip_cnt = 0     # 本次跳过的已保存作品数量

# ------------------------------------------------------------------------------
# 3. 辅助功能函数与飞书播报系统（报警推送 & 一周不重样酷炫日报）
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
                json={"msg_type": "text", "content": {"text": text[:3500]}},
                timeout=15
            )
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
    当遇到 Cookie 失效、触发验证码风控等严重问题时触发。
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
        f"⚡️ <b>别慌！</b>系统已为你自动熔断暂停本次保存，保护账号不被封禁~ 🛡️"
    )
    push(title, content)

# ------------------------------------------------------------------------------
# 10 套充满丰富 Emoji 表情与热情的随机飞书通知模板
# ------------------------------------------------------------------------------

NOTIFICATION_TEMPLATES = [
    # 模板 1
    {
        "title": "🤖【抖音云端特工巡逻战报】",
        "intro": "⚡ 报告长官！巡逻特工已完成新一轮抖音博主搜捕任务，成果丰硕！",
        "author_fmt": (
            "👤 <b>博主昵称：</b>{nick}<br>"
            "  • 🔍 抓取作品总数：<b>{total_fetched}</b> 个<br>"
            "  • 📦 抓取成功类型：<b>{types_str}</b><br>"
            "  • ⏭️ 跳过重复作品：<b>{skip_cnt}</b> 个<br>"
            "  • ⚠️ 抓取失败作品：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "📊 <b>【全局战况汇总】</b><br>"
            "  • 🟢 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • ⏭️ 一共跳过重复：<b>{total_skipped}</b> 个作品<br>"
            "  • ❌ 一共抓取失败：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🫡 特工小队归位，随时待命迎接下一轮巡逻！✨"
    },
    # 模板 2
    {
        "title": "🚀【星际航行·采风号搜捕日志】",
        "intro": "🛸 哔哔！星际采风号飞船穿梭抖音星系，为您带来最新观测报告：",
        "author_fmt": (
            "🪐 <b>目标博主：</b>{nick}<br>"
            "  • 📡 探测作品总数：<b>{total_fetched}</b> 个<br>"
            "  • 💎 成功捕获类型：<b>{types_str}</b><br>"
            "  • 🌀 避开重复轨道：<b>{skip_cnt}</b> 个<br>"
            "  • 💥 异常丢包作品：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "🌌 <b>【星系采风总结算】</b><br>"
            "  • 🌟 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🌀 一共跳过重复：<b>{total_skipped}</b> 个作品<br>"
            "  • ☄️ 一共抓取失败：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🛰️ 采风号进入蓄能状态，下一站准时启航！💫"
    },
    # 模板 3
    {
        "title": "👾【赛博朋克·数据矩阵抓取报告】",
        "intro": "💻 [SYSTEM OK] 抖音节点数据爬取与解密已完成，数据链路接入成功：",
        "author_fmt": (
            "🤖 <b>节点博主：</b>{nick}<br>"
            "  • 🔌 拦截数据包：<b>{total_fetched}</b> 个<br>"
            "  • 💾 解密成功类型：<b>{types_str}</b><br>"
            "  • 🔒 缓存命中跳过：<b>{skip_cnt}</b> 个<br>"
            "  • ❌ 校验失败作品：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "🖥️ <b>【矩阵总结算】</b><br>"
            "  • ⚡️ 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🛡️ 一共跳过重复：<b>{total_skipped}</b> 个作品<br>"
            "  • ⚠️ 一共抓取失败：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🔌 节点断开，系统已切入休眠节电模式... 🤖"
    },
    # 模板 4
    {
        "title": "🍕【特工美食快送·新鲜作品派送单】",
        "intro": "🍱 叮咚！您关注的博主最新作品“热乎套餐”已全速送达，请签收：",
        "author_fmt": (
            "👨‍🍳 <b>主厨博主：</b>{nick}<br>"
            "  • 📜 本期出菜作品：<b>{total_fetched}</b> 道<br>"
            "  • 🍲 成功上桌类型：<b>{types_str}</b><br>"
            "  • 🍱 之前尝过跳过：<b>{skip_cnt}</b> 道<br>"
            "  • 🍳 上菜失败数量：<b>{fail_cnt}</b> 道"
        ),
        "summary_fmt": (
            "🥤 <b>【外卖总账单】</b><br>"
            "  • 😋 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🥡 一共跳过重复：<b>{total_skipped}</b> 个作品<br>"
            "  • 🥣 一共抓取失败：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🍩 祝您用餐愉快，小哥先去吃零食啦~ 🍧"
    },
    # 模板 5
    {
        "title": "🏎️【极速飞车·博主动态快讯】",
        "intro": "🏎️💨 轰隆隆！极速搬运车队以 200km/h 的速度冲过终点线，战果大公开：",
        "author_fmt": (
            "🏁 <b>赛道博主：</b>{nick}<br>"
            "  • 🚩 发现动态作品：<b>{total_fetched}</b> 个<br>"
            "  • 🏆 极速冲线类型：<b>{types_str}</b><br>"
            "  • ⛽ 弯道避让重复：<b>{skip_cnt}</b> 个<br>"
            "  • 🛑 抛锚失败作品：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "🥇 <b>【车队总成绩】</b><br>"
            "  • 🎉 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🏎️ 一共跳过重复：<b>{total_skipped}</b> 个作品<br>"
            "  • 🔧 一共抓取失败：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🏆 奖杯已收入囊中，车队回库保养等下一场！🏁"
    },
    # 模板 6
    {
        "title": "🏆【数字搬运金牌特工·巡检简报】",
        "intro": "💼 尊敬的主人，您的专属金牌数字搬运官为您呈上最新的巡检与备份报告：",
        "author_fmt": (
            "🌟 <b>创作者：</b>{nick}<br>"
            "  • 🔍 检索到作品：<b>{total_fetched}</b> 个<br>"
            "  • 📦 归档成功类型：<b>{types_str}</b><br>"
            "  • 📁 已有存档跳过：<b>{skip_cnt}</b> 个<br>"
            "  • ⚠️ 归档失败作品：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "📈 <b>【网盘金库汇总】</b><br>"
            "  • ✅ 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 📁 一共跳过重复：<b>{total_skipped}</b> 个作品<br>"
            "  • 🚨 一共抓取失败：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🎩 随时待命为您服务，愿您今天心情舒畅！💖"
    },
    # 模板 7
    {
        "title": "🏖️【海滩冲浪小分队·作品搜捕日报】",
        "intro": "🏄‍♂️ 踏浪而来！冲浪特工在抖音大潮中抓到了不少新鲜货，速来看：",
        "author_fmt": (
            "🌴 <b>冲浪博主：</b>{nick}<br>"
            "  • 🌊 巨浪卷入作品：<b>{total_fetched}</b> 个<br>"
            "  • 🏄 抱回岸上类型：<b>{types_str}</b><br>"
            "  • 🐚 沙滩旧贝跳过：<b>{skip_cnt}</b> 个<br>"
            "  • 🦈 意外脱钩失败：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "🏖️ <b>【海滩收货总计】</b><br>"
            "  • 🟢 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🐚 一共跳过重复：<b>{total_skipped}</b> 个作品<br>"
            "  • 🌊 一共抓取失败：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🤙 晒个日光浴，准备下一次踏浪搜捕！☀️"
    },
    # 模板 8
    {
        "title": "🍿【爆米花私人影院·更新动向指南】",
        "intro": "🎬 欢迎光临私人影院！本期新片上架与放映清单已为您整理妥当：",
        "author_fmt": (
            "🎬 <b>导演/博主：</b>{nick}<br>"
            "  • 📽️ 提交影片总数：<b>{total_fetched}</b> 部<br>"
            "  • 🍿 上映成功类型：<b>{types_str}</b><br>"
            "  • 🎞️ 已经放映跳过：<b>{skip_cnt}</b> 部<br>"
            "  • ❌ 胶片损坏失败：<b>{fail_cnt}</b> 部"
        ),
        "summary_fmt": (
            "🍿 <b>【票房总盘点】</b><br>"
            "  • 🎉 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🎞️ 一共跳过重复：<b>{total_skipped}</b> 个作品<br>"
            "  • 📽️ 一共抓取失败：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🥤 拿好爆米花，快去网盘开启刷片模式吧！✨"
    },
    # 模板 9
    {
        "title": "🔋【满电特工队·云端同步情报】",
        "intro": "⚡ 叮！电池已充满 100%！满电特工队为您送上云端同步最新战况：",
        "author_fmt": (
            "💡 <b>高能博主：</b>{nick}<br>"
            "  • 🔋 侦测到信号：<b>{total_fetched}</b> 个<br>"
            "  • ⚡ 成功充电类型：<b>{types_str}</b><br>"
            "  • 🔌 满电跳过作品：<b>{skip_cnt}</b> 个<br>"
            "  • 🪫 断电失败作品：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "⚡ <b>【总电量汇总量】</b><br>"
            "  • 🟢 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🔋 一共跳过重复：<b>{total_skipped}</b> 个作品<br>"
            "  • 🪫 一共抓取失败：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🔌 电量充足，小队随时准备接管任务！⚡"
    },
    # 模板 10
    {
        "title": "🌈【彩虹云端小助手·博主更新大盘点】",
        "intro": "🎈 嗨喽！彩虹小助手闪亮登场~ 为您送上今天最绚丽的云端作品大盘点：",
        "author_fmt": (
            "🌺 <b>宝藏博主：</b>{nick}<br>"
            "  • 🎈 收集到新动态：<b>{total_fetched}</b> 个<br>"
            "  • 🎁 存入网盘类型：<b>{types_str}</b><br>"
            "  • 🎀 之前存过跳过：<b>{skip_cnt}</b> 个<br>"
            "  • 🌧️ 偶遇小雨失败：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "💖 <b>【彩虹宝库总结】</b><br>"
            "  • 🌈 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🎀 一共跳过重复：<b>{total_skipped}</b> 个作品<br>"
            "  • ☔ 一共抓取失败：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🎉 祝您今天每一天都充满七彩阳光！🌟"
    }
]

def generate_daily_report(author_stats, fails):
    """
    【从 10 个充满调皮表情的飞书通知模板中随机抽取 1 套，生成超详细报表】
    包含：博主名称、抓取的总作品数、各类型及成功抓取数、跳过作品数、失败作品数，以及全局总成功数、跳过数和失败数。
    """
    tmpl = random.choice(NOTIFICATION_TEMPLATES)
    now_str = datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")

    # 计算全局汇总数据
    total_success = sum(s["success_cnt"] for s in author_stats.values())
    total_skipped = sum(s["skip_cnt"] for s in author_stats.values())
    total_failed = sum(s["fail_cnt"] for s in author_stats.values())

    # 组装各博主的统计明细
    author_blocks = []
    if author_stats:
        for nick, s in author_stats.items():
            # 格式化作品类型与抓取数量
            types_parts = []
            for t_name, t_cnt in s["types"].items():
                if t_cnt > 0:
                    types_parts.append(f"{t_name} {t_cnt} 个")
            types_str = "，".join(types_parts) if types_parts else "无（未抓取到新类型作品）"

            blk = tmpl["author_fmt"].format(
                nick=nick,
                total_fetched=s["total_fetched"],
                types_str=types_str,
                skip_cnt=s["skip_cnt"],
                fail_cnt=s["fail_cnt"]
            )
            author_blocks.append(blk)
    else:
        author_blocks.append("👀 本轮巡视未捕获到任何博主作品动态~")

    # 组装全局汇总信息
    summary_block = tmpl["summary_fmt"].format(
        total_success=total_success,
        total_skipped=total_skipped,
        total_failed=total_failed
    )

    content_lines = [
        f"{tmpl['intro']}<br>",
        f"⏰ <b>巡视时间：</b>{now_str}<br>",
        "👥 <b>【各博主详细战果】</b><br>" + "<br><br>".join(author_blocks) + "<br>",
        f"{summary_block}<br>"
    ]

    # 如果存在失败异常，列出失败明细与诊断
    if fails:
        content_lines.append("❌ <b>失败明细与诊断提示：</b>")
        for f in fails[:8]:
            content_lines.append(f"  • ⚠️ {f}")
        if len(fails) > 8:
            content_lines.append(f"  • ...等共 {len(fails)} 项异常")
        content_lines.append("💡 <i>提示：若频繁失败，可能是网络波动或文件大小超出限制，系统将在下一轮重试。</i><br>")

    content_lines.append(f"✨ <i>{tmpl['closing']}</i>")
    return tmpl["title"], "<br>".join(content_lines)

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
            try:
                print(f"[crawl] Opening user URL: {u}")
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
                    page.evaluate("if (window.__sc) { window.__sc.scrollTop = window.__sc.scrollHeight; } else { window.scrollTo(0, document.body.scrollHeight); }")
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(4000)
                    empty = 0 if len(collected) > before else empty + 1
            except Exception as e:
                print(f"[warn] Failed to open/crawl URL {u}: {e}")

        # 检查是否触碰到了风险控制（验证码页面）
        if ("verify" in page.url) or ("captcha" in page.url):
            p0("触发抖音验证码/风控限制",
               detail=f"访问页面触发风控重定向：{page.url}\n可能是短时间内请求过于频繁或 IP 触发拦截。系统已自动暂停保存任务。",
               err_type="CAPTCHA_RISK_CONTROL")
            browser.close(); sys.exit(5)

        time.sleep(2)

        # 检查 Cookie 是否过期（接口返回非 0 状态码）
        if api_status and all(s not in (0,) for s in api_status):
            p0("抖音 Cookie 疑似失效",
               detail=f"抖音后台 API 返回异常状态码：{api_status[0]}\n通常表示登录凭证 DOUYIN_COOKIE 已过期或失效，请在浏览器中重新登录并获取最新 Cookie。",
               err_type="COOKIE_EXPIRED")
            browser.close(); sys.exit(2)

        # 检查是否被弹出了强制登录框
        if not collected:
            body = page.content()
            if "passport" in page.url or ("登录" in body and len(body) < 50000):
                p0("抖音 Cookie 疑似失效（触发登录拦截）",
                   detail=f"打开页面被拦截重定向至登录页（{page.url}）。请打开浏览器重新登录抖音账号，并在 GitHub Secrets 中更新 DOUYIN_COOKIE 环境变量。",
                   err_type="LOGIN_INTERCEPTED")
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
                    fails.append(f"作品【{aid}】图{i} LivePhoto动图下载或保存失败")

            # 如果不是动图，下载原图
            if not live_ok:
                data = fetch(re.sub(r"~tplv-[^?]+", "~tplv-dy-aweme-original:jpeg", urls[-1]), retry=1) or fetch(urls[-1])
                if data is None:
                    fails.append(f"作品【{aid}】图{i} 原图下载失败"); continue
                path = f"{folder}/{aid}_img{i}.{guess_ext(urls[-1])}"
                if wd_put(path, data):
                    files.append(path)
                else:
                    fails.append(f"作品【{aid}】图{i} WebDAV网盘上传失败")
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
                fails.append(f"视频【{aid}】网络数据抓取失败")
            elif wd_put(f"{folder}/{aid}_video.mp4", vd):
                files.append(f"{folder}/{aid}_video.mp4")
            else:
                fails.append(f"视频【{aid}】WebDAV网盘上传失败")

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
        p0("抓取作品列表为空",
           detail="后台未拦截到任何作品数据，但本地已有历史备份记录。\n可能原因：\n1. 抖音网页版结构发生变动\n2. 账号触发了隐形风控，作品列表无法正常渲染\n3. DOUYIN_URL 配置的博主主页无法访问",
           err_type="EMPTY_CRAWL_WITH_HISTORY")
        sys.exit(3)
    if not items and not history:
        p0("首次运行未获取到任何作品",
           detail="系统首次运行未获取到任何作品。\n请检查环境变量配置：\n1. DOUYIN_URL 是否为有效博主主页链接或分享口令\n2. DOUYIN_COOKIE 是否包含正确的登录 Cookie",
           err_type="EMPTY_CRAWL_FIRST_RUN")
        sys.exit(4)

    # 初始化各博主统计数据结构
    # author_stats = { nick: {"total_fetched": 0, "types": {"视频": 0, "图集": 0}, "success_cnt": 0, "skip_cnt": 0, "fail_cnt": 0} }
    author_stats = {}

    for it in items:
        nick = author_of(it)
        if nick not in author_stats:
            author_stats[nick] = {
                "total_fetched": 0,
                "types": {"视频": 0, "图集": 0},
                "success_cnt": 0,
                "skip_cnt": 0,
                "fail_cnt": 0
            }
        author_stats[nick]["total_fetched"] += 1

    # 2. 遍历抓取到的作品列表，逐个对比历史记录并下载
    done = 0
    for it in items:
        if done >= MAX_PER_RUN:
            break
        aid = it.get("aweme_id")
        if not aid:
            continue

        nick = author_of(it)
        itype = "图集" if it.get("images") else "视频"

        housekeep(it) # 基础维护（更新博主头像、封面）

        # 如果这个作品已经下载过，直接跳过
        if aid in history:
            skip_cnt += 1
            author_stats[nick]["skip_cnt"] += 1
            continue

        try:
            # 处理并下载新作品
            if process(it):
                done += 1
                author_stats[nick]["success_cnt"] += 1
                author_stats[nick]["types"][itype] = author_stats[nick]["types"].get(itype, 0) + 1
                print(f"[ok] {aid} saved")
            else:
                author_stats[nick]["fail_cnt"] += 1
        except Exception as e:
            fails.append(f"作品【{aid}】处理发生异常：{e}")
            author_stats[nick]["fail_cnt"] += 1

        # 随机暂停 3~8 秒，避免下载过快被抖音服务器封禁
        time.sleep(random.randint(3, 8))

    # 3. 将最新的历史记录保存回本地文件
    json.dump(history, open(HIST_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    tot_success = sum(s["success_cnt"] for s in author_stats.values())
    tot_skipped = sum(s["skip_cnt"] for s in author_stats.values())
    tot_failed = sum(s["fail_cnt"] for s in author_stats.values())
    print(f"[info] This run: New {tot_success}, Skipped {tot_skipped}, Failed {tot_failed}")

    # 4. 从 10 套飞书通知模板里随机选择 1 套，生成超详细战报并推送
    title, content = generate_daily_report(author_stats, fails)
    push(title, content)

if __name__ == "__main__":
    main()
