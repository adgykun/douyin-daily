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
    title = f"🚨【抖音特工急报】{msg}"
    content = (
        f"⏰ 发生时间：{now_str}<br>"
        f"🏷 故障类型：{err_type}<br>"
        f"🔍 根因诊断：<br>{detail.replace(chr(10), '<br>')}<br><br>"
        f"🛠 逐步排查与修复建议：<br>"
        f"1. 登录 GitHub 仓库进入 Settings -> Secrets and variables -> Actions<br>"
        f"2. 检查并更新对应的 DOUYIN_COOKIE 或 DOUYIN_URL 密钥与变量<br>"
        f"3. 确认抖音网页版（douyin.com）账号登录状态正常且无验证码弹窗<br>"
        f"4. 重新点击 Actions -> Run workflow 手动验证运行结果<br><br>"
        f"⚡️ 别慌！系统已为你自动熔断暂停本次保存，保护账号不被封禁~"
    )
    push(title, content)

# ------------------------------------------------------------------------------
# 一周 21 轮（7天 × 每天3轮）完全不重复的酷炫小调皮风格日报生成器
# ------------------------------------------------------------------------------

REPORT_PERSONAS = [
    # Index 0: 周一 早班 (08:17)
    {
        "title": "⚡️【特工小哥·周一早八能量加满】",
        "intro": "嘀！周一特工小哥打卡！开启本周第一波硬核巡逻~ ☕️",
        "active": "爽快！一大早就抓到了 {new_cnt} 个热乎乎的新作品！统统无压送入网盘金库！📦🚀",
        "silent": "巡逻完毕~ 博主们大概还没从周末梦里醒过来，今天没更新哦，网盘安然无事！😴",
        "closing": "退下继续监视去啦，加油打工人！💪"
    },
    # Index 1: 周一 午班 (12:23)
    {
        "title": "🍱【打工人续航站·周一午间巡检】",
        "intro": "一边干饭一边巡逻！周一中午的抖音捕手上线咯 🍗",
        "active": "干饭途中战果丰硕！拦截到 {new_cnt} 个新发作品，已打包码齐，请查收！🍣",
        "silent": "吃饱喝足，网盘里也是满满当当~ 本轮未发现新增作品，小弟继续打瞌睡~ 💤",
        "closing": "吃饱喝足准备午休，下午继续干！🍉"
    },
    # Index 2: 周一 晚班 (20:37)
    {
        "title": "🌙【夜行者电波·周一打卡下班】",
        "intro": "周一终于熬过头啦！夜行者特工为你带来今夜最后一波战报 🌃",
        "active": "夜幕降临，收获满满！把博主刚烤好的 {new_cnt} 个作品一股脑运回家啦 🍢",
        "silent": "今夜无风无浪，博主今晚静悄悄，小弟也去充充电咯，晚安！💤",
        "closing": "关机洗洗睡，明天又是新的一天~ ✨"
    },
    # Index 3: 周二 早班
    {
        "title": "🚀【星际航行·周二晨间引擎全开】",
        "intro": "哔哔！周二晨间巡航号已经升空，传感器全开！🛸",
        "active": "在抖音星系捕获到 {new_cnt} 颗高能新星作品！已成功降落到 WebDAV 基地！🌌",
        "silent": "星系一片祥和，未发现新星轨迹，基地仓储完好无损~ 📡",
        "closing": "巡航号保持轨道飞行中，随时待命！💫"
    },
    # Index 4: 周二 午班
    {
        "title": "🍉【吃瓜群众·周二午后前线】",
        "intro": "搬个小板凳！周二午后吃瓜巡逻队准时报道 🍉",
        "active": "小板凳没白搬！捕获 {new_cnt} 个爆款新动态，这就奉上大片！🍿",
        "silent": "吃瓜小分队环顾四周，博主今日按兵不动，瓜架十分安全~ 🍉",
        "closing": "撤走小板凳，去准备下午茶啦~ 🍰"
    },
    # Index 5: 周二 晚班
    {
        "title": "🏎️【极速飞车·周二夜间冲刺】",
        "intro": "漂移过弯！周二夜间极速搬运车队组团刷屏 🏎️💨",
        "active": "一脚油门下去，直接运回 {new_cnt} 个极品音画！这速度就问你酷不酷 😎",
        "silent": "赛道畅通无阻，博主今晚休息，车队回库保养咯 🏁",
        "closing": "尾灯闪烁，特工车队优雅归巢~ 🏆"
    },
    # Index 6: 周三 早班
    {
        "title": "🐫【周三驼峰日·黎明破晓行动】",
        "intro": "一周过半啦！周三驼峰行动小队闪亮登场 🐫⚡️",
        "active": "成功翻越周三山峰！顺便掏空博主主页，扛回 {new_cnt} 个硬货！🏋️",
        "silent": "驼峰山上风平浪静，没有发现新作品的痕迹，轻松过关！🏔️",
        "closing": "坚持住！周末已经在向我们招手啦~ 👋"
    },
    # Index 7: 周三 午班
    {
        "title": "☕️【下午茶特遣队·周三午间电波】",
        "intro": "来杯咖啡提提神！周三午间巡逻特遣队报道 ☕️🍰",
        "active": "配合冰美式，一口气吞下 {new_cnt} 个精彩作品！美味极了 🍩",
        "silent": "咖啡喝完了，博主还没有发新动态，网盘安安静静享受午后阳光~ ☕️",
        "closing": "咖啡因生效中，小弟神采奕奕~ ⚡️"
    },
    # Index 8: 周三 晚班
    {
        "title": "👾【赛博朋克·周三深夜极客】",
        "intro": "系统已接入网络矩阵... 周三赛博巡逻夜启动 👾💻",
        "active": "成功解密数据流！拦截并下载 {new_cnt} 个高清数据包！真香！⚡️",
        "silent": "数据矩阵暂无异常波动，博主节点未发包，网络保持清洁 🔌",
        "closing": "断开连接，小弟要进入休眠模式咯 🤖"
    },
    # Index 9: 周四 早班
    {
        "title": "🏄【黎明冲浪·周四晨间搜捕】",
        "intro": "周四的曙光照亮大海！冲浪特工踏浪而来 🏄‍♂️🌊",
        "active": "抓到了巨浪！成功抱回 {new_cnt} 个超棒的新视频/图集！🏄",
        "silent": "风平浪静，海面上没有新作品出没，准备回岸上晒太阳 🏖️",
        "closing": "脚踩冲浪板，随时准备迎接下一波热浪~ 🤙"
    },
    # Index 10: 周四 午班
    {
        "title": "🍗【疯狂星期四·V我50巡逻组】",
        "intro": "疯狂星期四！V我50，本特工帮你在网盘堆满视频 🍗🍟",
        "active": "今天不仅有原味鸡，更有 {new_cnt} 个热气腾腾的新备份！香爆了 🍗",
        "silent": "没等来V50，也没等来博主发新作品，小弟先去吃炸鸡咯 🍟",
        "closing": "肯德基门前集合，不见不散~ 🥤"
    },
    # Index 11: 周四 晚班
    {
        "title": "🌆【周末前夜哨所·周四晚间巡查】",
        "intro": "黎明前的曙光！再坚持一天就是周末！周四晚间巡查组上线 🌆",
        "active": "博主也在冲刺周末！今晚奉献了 {new_cnt} 个高分作品，全收下啦 🎁",
        "silent": "博主大概也在提前构思周末大招，今晚零新增，哨所平安无事 🏰",
        "closing": "哨所灯火通明，静候周五降临！✨"
    },
    # Index 12: 周五 早班
    {
        "title": "🎉【周末倒计时·周五晨间狂欢预热】",
        "intro": "周五啦！周五啦！连空气都是甜的！周五晨间小分队出动 🎉🎈",
        "active": "用 {new_cnt} 个崭新备份开启美好的周五！简直不要太快乐 🥳",
        "silent": "虽然还没抓到新作品，但周五的快乐丝毫减不了一分！网盘妥妥的 🎈",
        "closing": "快乐因子爆表，祝你今天心情美美哒~ 💖"
    },
    # Index 13: 周五 午班
    {
        "title": "🍹【快乐水特工·周五午间电波】",
        "intro": "喝口奶茶庆祝周五午后！快乐水特工闪亮巡查 🍹",
        "active": "快乐加倍！搞到了 {new_cnt} 个高清好货，网盘库存又涨啦 🧋",
        "silent": "奶茶喝完，博主还在憋大招，网盘静候周末盛宴 🍹",
        "closing": "吸一口珍珠，开启倒计时下班模式 ⏳"
    },
    # Index 14: 周五 晚班
    {
        "title": "💃【周末狂欢 Night·周五夜间爆破】",
        "intro": "下班！下课！周末狂欢 Party 正式开始！🥳💃",
        "active": "周五夜惊喜狂欢！疯狂扫货 {new_cnt} 个高能作品，存入金库！🍾",
        "silent": "博主也去嗨皮狂欢了，今晚零更新，网盘锁门打烊咯 🔒",
        "closing": "摇滚起来！开启周末狂欢模式！🎸"
    },
    # Index 15: 周六 早班
    {
        "title": "💤【睡到自然醒·周六懒人巡逻】",
        "intro": "伸个懒腰~ 周六阳光正好，懒人特工悠闲伸展 ☀️🛌",
        "active": "懒人也有大收获！床头一抓就是 {new_cnt} 个新鲜视频/图集！🛌",
        "silent": "大家都在睡懒觉，博主也不例外~ 零新增，继续躺平 😴",
        "closing": "翻个身继续做美梦去啦~ 💤"
    },
    # Index 16: 周六 午班
    {
        "title": "🍰【惬意下午茶·周六 midday 轻松搜搜】",
        "intro": "吃着甜点逛抖音！周六午后悠闲小分队报道 🍰☕️",
        "active": "下午茶配大片！顺利收入 {new_cnt} 个超酷作品，完美 🎨",
        "silent": "享受无忧无虑的周六午后，网盘里岁月静好，无新动态 ~ 🍰",
        "closing": "祝你度过一个惬意的周末下午~ 甜甜哒！🍡"
    },
    # Index 17: 周六 晚班
    {
        "title": "🍿【周末爆米花影院·周六夜间大片】",
        "intro": "灯光准备！爆米花就位！周六黄金档影院巡逻 🍿🎬",
        "active": "黄金档爆款连连！抱回 {new_cnt} 部精品大作，快去网盘刷片吧 🎬",
        "silent": "今夜无电影上映，博主休假中，爆米花我一个人独享啦 🍿",
        "closing": "电影散场，网盘金库门已锁好，晚安~ 🌙"
    },
    # Index 18: 周日 早班
    {
        "title": "🌿【 Sunday Chill·周日晨间清爽巡航】",
        "intro": "清晨的第一缕阳光！周日 Chill 巡逻小队上线 🌿🍵",
        "active": "收获清晨第一份美好！收纳了 {new_cnt} 个优质作品！🍵",
        "silent": "阳光万里，网盘无恙，今日无需搬运，静享周日时光 🌻",
        "closing": "大自然的气息真好，今天也要开开心心！🌈"
    },
    # Index 19: 周日 午班
    {
        "title": "🔋【电量满格·周日午后充电站】",
        "intro": "给心情充满电！周日午后电力特工满格复活 🔋⚡️",
        "active": "电量十足！一口气抓取 {new_cnt} 个作品，网盘能量爆发 ⚡️",
        "silent": "蓄力充电中，博主未发新作品，网盘电池百分百满格 🔋",
        "closing": "满电状态，随时准备应对各种挑战！⚡️"
    },
    # Index 20: 周日 晚班
    {
        "title": "🎒【收心大作战·周日深夜备战】",
        "intro": "周日晚间备战哨响！整理好心情迎接新一周 🎒💼",
        "active": "周日收官之战！拿下 {new_cnt} 个压轴作品，完美收尾本周 🏆",
        "silent": "本周最后一轮巡逻顺利完成！零新增，准备齐整，下周继续战斗！👊",
        "closing": "打卡完毕！下周我们不见不散！🚀"
    }
]

def generate_daily_report(new_cnt, skip_cnt, fails):
    """
    【生成一周不重样、酷酷的且带有调皮表情的飞书日报】
    根据当前星期（0-6）与当前时间段（早/午/晚）自动匹配 21 种独一无二的播报 Persona。
    """
    now = datetime.now(BJ)
    weekday = now.weekday() # 0 = 周一 ... 6 = 周日
    hour = now.hour

    # 判断当前时间的轮次 slot
    if hour < 11:
        slot = 0 # 早班
    elif hour < 17:
        slot = 1 # 午班
    else:
        slot = 2 # 晚班

    slot_index = (weekday * 3 + slot) % len(REPORT_PERSONAS)
    persona = REPORT_PERSONAS[slot_index]

    # 动态拼接标题与正文
    title = persona["title"]
    now_str = now.strftime("%Y-%m-%d %H:%M")

    status_narration = persona["active"].format(new_cnt=new_cnt) if new_cnt > 0 else persona["silent"]

    content_lines = [
        f"{persona['intro']}<br>",
        f"📅 巡视时间：{now_str}",
        f"📊 本轮战况：",
        f"  • 🟢 新增备份：<b>{new_cnt}</b> 个",
        f"  • ⏭️ 跳过重复：<b>{skip_cnt}</b> 个",
        f"  • ⚠️ 失败报错：<b>{len(fails)}</b> 个<br>",
        f"💬 特工说：{status_narration}<br>"
    ]

    # 如果有下载失败的项，添加极度详细的错误排查明细
    if fails:
        content_lines.append("❌ <b>失败明细与诊断提示：</b>")
        for f in fails[:8]:
            content_lines.append(f"  • {f}")
        if len(fails) > 8:
            content_lines.append(f"  • ...等共 {len(fails)} 项异常")
        content_lines.append("💡 <i>提示：若频繁失败，可能是网络波动或文件大小超出限制，系统将在下一轮重试。</i><br>")

    content_lines.append(f"✨ <i>{persona['closing']}</i>")
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
            fails.append(f"作品【{aid}】处理发生异常：{e}")

        # 随机暂停 3~8 秒，避免下载过快被抖音服务器封禁
        time.sleep(random.randint(3, 8))

    # 3. 将最新的历史记录保存回本地文件
    json.dump(history, open(HIST_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[info] This run: New {new_cnt}, Skipped {skip_cnt}, Failed {len(fails)}")

    # 4. 根据运行结果生成并发送飞书酷炫播报
    title, content = generate_daily_report(new_cnt, skip_cnt, fails)
    push(title, content)

if __name__ == "__main__":
    main()
