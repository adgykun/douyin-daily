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

fails = []          # 记录下载失败的项
used_names = set()  # 记录本次运行中用到的文件名，防止重名覆盖
new_cnt = 0         # 本次成功保存的新作品数量
skip_cnt = 0        # 本次跳过的已保存作品数量
refetch_cnt = 0     # 本次恢复 Cookie 后重新下载的高清作品数量

# ------------------------------------------------------------------------------
# 3. 辅助功能函数与飞书播报系统（报警推送 & 20套随机常规模板 & 10套全部跳过模板 & Cookie提醒）
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

# ------------------------------------------------------------------------------
# 20 套充满丰富 Emoji 表情与热情的随机飞书通知模板
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
            "  • 🔄 恢复重抓高清：<b>{refetch_cnt}</b> 个<br>"
            "  • ⏭️ 跳过重复作品：<b>{skip_cnt}</b> 个<br>"
            "  • ⚠️ 抓取失败作品：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "📊 <b>【全局战况汇总】</b><br>"
            "  • 🟢 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • ✨ 恢复高清重抓：<b>{total_refetch}</b> 个作品<br>"
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
            "  • 🌟 修复高码轨道：<b>{refetch_cnt}</b> 个<br>"
            "  • 🌀 避开重复轨道：<b>{skip_cnt}</b> 个<br>"
            "  • 💥 异常丢包作品：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "🌌 <b>【星系采风总结算】</b><br>"
            "  • 🌟 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🛰️ 高清修复作品：<b>{total_refetch}</b> 个<br>"
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
            "  • ⚡ 升级无损节点：<b>{refetch_cnt}</b> 个<br>"
            "  • 🔒 缓存命中跳过：<b>{skip_cnt}</b> 个<br>"
            "  • ❌ 校验失败作品：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "🖥️ <b>【矩阵总结算】</b><br>"
            "  • ⚡️ 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🔋 重写高清数据：<b>{total_refetch}</b> 个<br>"
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
            "  • 🍲 升级升级大餐：<b>{refetch_cnt}</b> 道<br>"
            "  • 🍱 之前尝过跳过：<b>{skip_cnt}</b> 道<br>"
            "  • 🍳 上菜失败数量：<b>{fail_cnt}</b> 道"
        ),
        "summary_fmt": (
            "🥤 <b>【外卖总账单】</b><br>"
            "  • 😋 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🍲 升配高清套餐：<b>{total_refetch}</b> 个<br>"
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
            "  • 🏎️ 换装超级引擎：<b>{refetch_cnt}</b> 个<br>"
            "  • ⛽ 弯道避让重复：<b>{skip_cnt}</b> 个<br>"
            "  • 🛑 抛锚失败作品：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "🥇 <b>【车队总成绩】</b><br>"
            "  • 🎉 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🏆 重新冲线高清：<b>{total_refetch}</b> 个<br>"
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
            "  • 💎 换替换高清档：<b>{refetch_cnt}</b> 个<br>"
            "  • 📁 已有存档跳过：<b>{skip_cnt}</b> 个<br>"
            "  • ⚠️ 归档失败作品：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "📈 <b>【网盘金库汇总】</b><br>"
            "  • ✅ 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • ✨ 自动升级高清：<b>{total_refetch}</b> 个<br>"
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
            "  • 🐚 捞取无损珍珠：<b>{refetch_cnt}</b> 个<br>"
            "  • 🐚 沙滩旧贝跳过：<b>{skip_cnt}</b> 个<br>"
            "  • 🦈 意外脱钩失败：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "🏖️ <b>【海滩收货总计】</b><br>"
            "  • 🟢 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🌊 重新打捞高清：<b>{total_refetch}</b> 个<br>"
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
            "  • 🎞️ 修复蓝光无损：<b>{refetch_cnt}</b> 部<br>"
            "  • 🎞️ 已经放映跳过：<b>{skip_cnt}</b> 部<br>"
            "  • ❌ 胶片损坏失败：<b>{fail_cnt}</b> 部"
        ),
        "summary_fmt": (
            "🍿 <b>【票房总盘点】</b><br>"
            "  • 🎉 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🎥 升级蓝光重抓：<b>{total_refetch}</b> 个<br>"
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
            "  • ⚡ 强效满血重刷：<b>{refetch_cnt}</b> 个<br>"
            "  • 🔌 满电跳过作品：<b>{skip_cnt}</b> 个<br>"
            "  • 🪫 断电失败作品：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "⚡ <b>【总电量汇总量】</b><br>"
            "  • 🟢 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🔋 高清重刷充电：<b>{total_refetch}</b> 个<br>"
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
            "  • 🎨 刷新绚丽画质：<b>{refetch_cnt}</b> 个<br>"
            "  • 🎀 之前存过跳过：<b>{skip_cnt}</b> 个<br>"
            "  • 🌧️ 偶遇小雨失败：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "💖 <b>【彩虹宝库总结】</b><br>"
            "  • 🌈 一共抓取成功：<b>{total_success}</b> 个作品<br>"
            "  • 🌈 重新补全彩虹：<b>{total_refetch}</b> 个<br>"
            "  • 🎀 一共跳过重复：<b>{total_skipped}</b> 个作品<br>"
            "  • ☔ 一共抓取失败：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🎉 祝您今天每一天都充满七彩阳光！🌟"
    },
    # 模板 11
    {
        "title": "🕵️‍♂️【黑客帝国·代码流备份简报】",
        "intro": "🕶️ [Matrix Code] 探针线程已从抖音服务器提取完毕最新数据流：",
        "author_fmt": (
            "🕹️ <b>目标节点：</b>{nick}<br>"
            "  • 📡 捕获封包：<b>{total_fetched}</b> 个<br>"
            "  • 💾 解析载荷：<b>{types_str}</b><br>"
            "  • ⚡ 无损重构：<b>{refetch_cnt}</b> 个<br>"
            "  • 🛡️ 校验重复：<b>{skip_cnt}</b> 个<br>"
            "  • ⚠️ 丢包丢失：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "🖥️ <b>【矩阵链路总结】</b><br>"
            "  • 🟩 成功注入网盘：<b>{total_success}</b> 个作品<br>"
            "  • 🟩 重新高精渲染：<b>{total_refetch}</b> 个作品<br>"
            "  • 🟨 自动忽略重复：<b>{total_skipped}</b> 个作品<br>"
            "  • 🟥 异常中断请求：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🕶️ 敲下 Enter，退出母体，准备下一次同步... 💊"
    },
    # 模板 12
    {
        "title": "☕【咖啡馆小歇·创作搜罗简讯】",
        "intro": "☕ 拿上一杯热美式，特工为您送上边喝咖啡边搜罗到的博主新风采：",
        "author_fmt": (
            "☕ <b>灵感博主：</b>{nick}<br>"
            "  • 📖 浏览作品：<b>{total_fetched}</b> 个<br>"
            "  • 🍰 享用类型：<b>{types_str}</b><br>"
            "  • ☕ 升级原磨浓香：<b>{refetch_cnt}</b> 个<br>"
            "  • 🍪 之前品尝过：<b>{skip_cnt}</b> 个<br>"
            "  • 🥀 打翻咖啡：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "🍰 <b>【咖啡馆总账单】</b><br>"
            "  • ☕ 满意打包：<b>{total_success}</b> 个作品<br>"
            "  • ☕ 重新特调高清：<b>{total_refetch}</b> 个作品<br>"
            "  • 🍪 避免重复打卡：<b>{total_skipped}</b> 个作品<br>"
            "  • 🥐 意外缺货：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🍰 享受惬意时光，期待下一次灵感碰撞！✨"
    },
    # 模板 13
    {
        "title": "🐱【猫咪巡逻队·萌系云端战果】",
        "intro": "🐱 喵呜~ 喵喵特工队踩着肉垫帮您巡视了抖音领地，抓到了好东西：",
        "author_fmt": (
            "🐾 <b>巡视博主：</b>{nick}<br>"
            "  • 🐾 爪子扑到：<b>{total_fetched}</b> 个<br>"
            "  • 🐟 叼回小黄鱼：<b>{types_str}</b><br>"
            "  • 🐾 重新打磨利爪：<b>{refetch_cnt}</b> 个<br>"
            "  • 🧶 旧毛线球跳过：<b>{skip_cnt}</b> 个<br>"
            "  • 🙀 溜走的小鱼：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "🐟 <b>【猫粮仓库盘点】</b><br>"
            "  • 😻 成功运回仓库：<b>{total_success}</b> 个作品<br>"
            "  • 🐾 升级超级大鱼：<b>{total_refetch}</b> 个作品<br>"
            "  • 🧶 跳过已有玩具：<b>{total_skipped}</b> 个作品<br>"
            "  • 🙀 抓捕失败：<b>{total_failed}</b> 个作品"
        ),
        "closing": "💤 伸个懒腰，小猫咪要抱抱去晒太阳啦~ ☀️"
    },
    # 模板 14
    {
        "title": "🎮【头号玩家·游戏通关通报】",
        "intro": "🎮 Level Up! 搬运副本已通关，装备与奖励已全额放入您的网盘背包：",
        "author_fmt": (
            "👾 <b>BOSS博主：</b>{nick}<br>"
            "  • 🗡️ 掉落宝箱：<b>{total_fetched}</b> 个<br>"
            "  • 🛡️ 拾取装备类型：<b>{types_str}</b><br>"
            "  • 💎 锻造史诗高清：<b>{refetch_cnt}</b> 个<br>"
            "  • 🎒 背包已满跳过：<b>{skip_cnt}</b> 个<br>"
            "  • ☠️ 掉落失败：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "🏆 <b>【副本通关结算】</b><br>"
            "  • 💎 获得稀有图鉴：<b>{total_success}</b> 个作品<br>"
            "  • 🗡️ 升级传说高清：<b>{total_refetch}</b> 个作品<br>"
            "  • 🎒 过滤重复道具：<b>{total_skipped}</b> 个作品<br>"
            "  • 👾 副本未掉落：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🕹️ 存盘成功，随时准备开启下一局刷图！💥"
    },
    # 模板 15
    {
        "title": "🚚【顺丰速运·云端冷链专列简报】",
        "intro": "📦 嘀！您的专属抖音内容冷链运输车已顺畅抵达目的地网盘站：",
        "author_fmt": (
            "🏭 <b>发货厂家：</b>{nick}<br>"
            "  • 📦 装车件数：<b>{total_fetched}</b> 件<br>"
            "  • 🚚 签收类型：<b>{types_str}</b><br>"
            "  • 📦 重新包装真原画：<b>{refetch_cnt}</b> 件<br>"
            "  • 🔂 重复包裹拦截：<b>{skip_cnt}</b> 件<br>"
            "  • 💥 运输损耗：<b>{fail_cnt}</b> 件"
        ),
        "summary_fmt": (
            "📮 <b>【物流总签收单】</b><br>"
            "  • 📦 成功入库：<b>{total_success}</b> 个作品<br>"
            "  • 🚚 高清原画升级：<b>{total_refetch}</b> 个作品<br>"
            "  • 🔂 过滤重复单号：<b>{total_skipped}</b> 个作品<br>"
            "  • ❌ 异常退单：<b>{total_failed}</b> 个作品"
        ),
        "closing": "📦 感谢使用专线速运，祝您生活愉快！🌟"
    },
    # 模板 16
    {
        "title": "🛸【外星科技·量子传输回传报告】",
        "intro": "📡 收到来自地球抖音频道的量子纠缠信号，数据传输正常：",
        "author_fmt": (
            "🛸 <b>地球信号源：</b>{nick}<br>"
            "  • 📡 接收波动：<b>{total_fetched}</b> 次<br>"
            "  • 🌌 解码光子：<b>{types_str}</b><br>"
            "  • 💫 量子重构高清：<b>{refetch_cnt}</b> 次<br>"
            "  • 🛰️ 过滤坍缩重复：<b>{skip_cnt}</b> 次<br>"
            "  • ☄️ 空间风暴干扰：<b>{fail_cnt}</b> 次"
        ),
        "summary_fmt": (
            "🌌 <b>【量子传输总览】</b><br>"
            "  • 🌟 成功纠缠存储：<b>{total_success}</b> 个作品<br>"
            "  • 💫 高清无损重构：<b>{total_refetch}</b> 个作品<br>"
            "  • 🛰️ 避开相干重复：<b>{total_skipped}</b> 个作品<br>"
            "  • ☄️ 信号衰减丢失：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🛸 传输信道保持稳定，等待下次波动... 💫"
    },
    # 模板 17
    {
        "title": "🎨【艺术画廊·展品珍藏日报】",
        "intro": "🖼️ 欢迎来到云端艺术馆，本期从抖音搜罗到的新画作已入库展出：",
        "author_fmt": (
            "🎨 <b>艺术家：</b>{nick}<br>"
            "  • 🖌️ 创作画作：<b>{total_fetched}</b> 幅<br>"
            "  • 🖼️ 展出类型：<b>{types_str}</b><br>"
            "  • 💎 换置无损原作：<b>{refetch_cnt}</b> 幅<br>"
            "  • 🏛️ 画廊已有跳过：<b>{skip_cnt}</b> 幅<br>"
            "  • 🥀 画框受损：<b>{fail_cnt}</b> 幅"
        ),
        "summary_fmt": (
            "🏛️ <b>【画廊馆藏总结】</b><br>"
            "  • 🖼️ 成功珍藏：<b>{total_success}</b> 个作品<br>"
            "  • 🎨 重新装裱高清：<b>{total_refetch}</b> 个作品<br>"
            "  • 🏛️ 跳过已有馆藏：<b>{total_skipped}</b> 个作品<br>"
            "  • 🥀 运输遗失：<b>{total_failed}</b> 个作品"
        ),
        "closing": "✨ 漫步艺术长廊，感受生活的美好！🌹"
    },
    # 模板 18
    {
        "title": "🎪【奇幻马戏团·精彩演出简讯】",
        "intro": "🎪 精彩绝伦！奇幻马戏团特工巡演结束，为您带回精彩幕后花絮：",
        "author_fmt": (
            "🤹 <b>主演博主：</b>{nick}<br>"
            "  • 🎪 演出节目：<b>{total_fetched}</b> 个<br>"
            "  • 精彩收录：<b>{types_str}</b><br>"
            "  • 🎪 升级特写高清：<b>{refetch_cnt}</b> 个<br>"
            "  • 🎟️ 已经看过了：<b>{skip_cnt}</b> 个<br>"
            "  • ❌ 道具失误：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "🎟️ <b>【演出票房汇总】</b><br>"
            "  • 🎈 成功打卡：<b>{total_success}</b> 个作品<br>"
            "  • ✨ 升级高清特写：<b>{total_refetch}</b> 个作品<br>"
            "  • 🎟️ 避开重复节目：<b>{total_skipped}</b> 个作品<br>"
            "  • 🎪 节目取消：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🎪 谢幕致意，期待下一场大秀精彩上演！👏"
    },
    # 模板 19
    {
        "title": "🚢【大航海时代·宝藏猎人寻宝记】",
        "intro": "⚓ 扬帆起航！宝藏猎人号驶向抖音大海，为您打捞回了珍贵宝箱：",
        "author_fmt": (
            "🏴‍☠️ <b>岛屿博主：</b>{nick}<br>"
            "  • 🗺️ 发现宝箱：<b>{total_fetched}</b> 个<br>"
            "  • 🪙 宝物类型：<b>{types_str}</b><br>"
            "  • 💎 换取纯金重抓：<b>{refetch_cnt}</b> 个<br>"
            "  • 🪙 纯金金币已有：<b>{skip_cnt}</b> 个<br>"
            "  • 🌊 沉入海底：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "💎 <b>【航海金库结清】</b><br>"
            "  • 🪙 成功搬运入库：<b>{total_success}</b> 个作品<br>"
            "  • 💎 升级纯金原画：<b>{total_refetch}</b> 个作品<br>"
            "  • ⚓ 跳过重复金币：<b>{total_skipped}</b> 个作品<br>"
            "  • 🌊 触礁遗失：<b>{total_failed}</b> 个作品"
        ),
        "closing": "⚓ 离港远航，向着伟大的航路继续进发！🌊"
    },
    # 模板 20
    {
        "title": "🌸【二次元·云端搜集日常】",
        "intro": "🌸 酱酱~ 魔法特工为你带回了超多心动的博主更新萌系作品哦：",
        "author_fmt": (
            "🌸 <b>宝藏博主：</b>{nick}<br>"
            "  • 📜 发现更新：<b>{total_fetched}</b> 个<br>"
            "  • 🎀 萌系类型：<b>{types_str}</b><br>"
            "  • 💖 魔法升级高清：<b>{refetch_cnt}</b> 个<br>"
            "  • 🎀 以前收集过：<b>{skip_cnt}</b> 个<br>"
            "  • 🌧️ 魔法失效：<b>{fail_cnt}</b> 个"
        ),
        "summary_fmt": (
            "💖 <b>【心动百宝箱】</b><br>"
            "  • 🌸 成功收藏：<b>{total_success}</b> 个作品<br>"
            "  • 💖 高清无损魔法：<b>{total_refetch}</b> 个作品<br>"
            "  • 🎀 自动跳过重复：<b>{total_skipped}</b> 个作品<br>"
            "  • 🌧️ 收集失败：<b>{total_failed}</b> 个作品"
        ),
        "closing": "🌸 今天也要保持满满元气，加油鸭！(๑•̀ㅂ•́)و✧"
    }
]

# ------------------------------------------------------------------------------
# 10 套针对【全部作品跳过】场景的随机飞书通知模板
# ------------------------------------------------------------------------------

ALL_SKIPPED_TEMPLATES = [
    # 跳过模板 1
    {
        "title": "☕【云端特工·平静巡逻报】",
        "intro": "☕ 报告长官！特工巡逻了一圈，发现您关注的博主均未发布新动态，所有已知作品已全额在网盘安稳归档~",
        "closing": "😌 库内万无一失，特工小队继续静默守护！✨"
    },
    # 跳过模板 2
    {
        "title": "🛡️【金库安防·零增量巡检简报】",
        "intro": "🔒 尊敬的主人！今日巡检完毕，网盘金库中的作品已被完美保护，本次巡逻发现的作品全部为已备份记录，无需重复下载！",
        "closing": "🎩 尽职尽责守护您的每一份美好记忆~ 💖"
    },
    # 跳过模板 3
    {
        "title": "🛸【星际采风号·静轨观测日志】",
        "intro": "🌌 哔哔！采风号在抖音星系扫描一圈，未探测到新的光子能量波动，所有观测到的作品均在缓存金库中！",
        "closing": "🛰️ 采风号保持静轨挂机，等待下一次新星爆发！💫"
    },
    # 跳过模板 4
    {
        "title": "🍕【特工美食·满腹安心简讯】",
        "intro": "🍣 叮咚！特工外卖小哥上线查看，博主主页的所有美食作品您之前都已经品尝过啦，本次零重复打卡！",
        "closing": "🍩 肚子饱饱，去喝杯奶茶歇会儿啦~ 🍧"
    },
    # 跳过模板 5
    {
        "title": "🏎️【极速车队·巡航安全简报】",
        "intro": "🏁 轰隆隆！搬运车队全速巡航一圈，未发现任何新发弯道作品，所有动态均已在网盘车库停放妥当！",
        "closing": "🏆 车队保持最佳竞技状态，随时准备再次发车！🏎️"
    },
    # 跳过模板 6
    {
        "title": "💻【赛博矩阵·无新数据包通告】",
        "intro": "🤖 [SYSTEM NORMAL] 数据矩阵无新数据包产生，本地缓存 100% 命中，无需额外拉取带宽！",
        "closing": "🔌 系统继续处于绿色低碳休眠模式... 🤖"
    },
    # 跳过模板 7
    {
        "title": "🏖️【海滩冲浪·风平浪静战报】",
        "intro": "🏄‍♂️ 踏浪特工在抖音大潮中巡视了一番，海面风平浪静，所有贝壳作品之前都已经收入囊中啦！",
        "closing": "🤙 躺在沙滩椅上喝口椰汁，等下一阵大浪！☀️"
    },
    # 跳过模板 8
    {
        "title": "🍿【私人影院·无新片上映提醒】",
        "intro": "🎬 影院经理播报：本期巡视各大导演出品库，暂无未上映的新片，旧片库均已在网盘高清备齐！",
        "closing": "🥤 拿着爆米花复习一下经典好片吧~ ✨"
    },
    # 跳过模板 9
    {
        "title": "🔋【满电特工·状态完好战报】",
        "intro": "⚡ 叮！满电特工队巡视完毕，所有博主动态均在蓄电池中妥善保存，本次巡逻全量跳过重复！",
        "closing": "🔌 电量满格，时刻准备迎接最新震撼作品！⚡"
    },
    # 跳过模板 10
    {
        "title": "🌈【彩虹小助手·安宁无忧盘点】",
        "intro": "🎈 嗨喽！彩虹小助手为你带来清爽报告：博主们今天很安静，宝库里的每一份美好都在闪闪发光，没有新作品需要搬运哦~",
        "closing": "🎉 祝您今天也是平平安安、心情舒畅的一天！🌸"
    }
]

def generate_daily_report(author_stats, fails):
    """
    【从 20 套常规模板或 10 套全部跳过模板中随机抽取 1 套，生成超详细报表】
    包含：博主名称、抓取的总作品数、各类型及成功抓取数、重抓数、跳过作品数、失败作品数，以及全局总成功数、跳过数和失败数。
    若 Cookie 失效，也会在卡片中附加每日提示 Banner。
    """
    now_str = datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")

    # 计算全局汇总数据
    total_success = sum(s["success_cnt"] for s in author_stats.values())
    total_refetch = sum(s.get("refetch_cnt", 0) for s in author_stats.values())
    total_skipped = sum(s["skip_cnt"] for s in author_stats.values())
    total_failed = sum(s["fail_cnt"] for s in author_stats.values())

    # 判断是否属于“全部跳过”场景（有抓到/检查到作品，但成功数=0且跳过数>0）
    is_all_skipped = (total_success == 0 and total_skipped > 0)

    if is_all_skipped:
        tmpl = random.choice(ALL_SKIPPED_TEMPLATES)
        title = tmpl["title"]
        content_lines = [
            f"{tmpl['intro']}<br>",
            f"⏰ <b>巡视时间：</b>{now_str}<br>",
            f"📊 <b>【检查汇总】</b><br>"
            f"  • ⏭️ 一共跳过已备份重复作品：<b>{total_skipped}</b> 个<br>"
            f"  • 🟢 一共新增保存：<b>0</b> 个<br>"
            f"  • ❌ 一共失败作品：<b>{total_failed}</b> 个<br>"
        ]
        if fails:
            content_lines.append("❌ <b>失败明细：</b>")
            for f in fails[:5]:
                content_lines.append(f"  • ⚠️ {f}")
        content_lines.append(f"<br>✨ <i>{tmpl['closing']}</i>")
    else:
        tmpl = random.choice(NOTIFICATION_TEMPLATES)
        title = tmpl["title"]

        # 组装各博主的统计明细
        author_blocks = []
        if author_stats:
            for nick, s in author_stats.items():
                types_parts = []
                for t_name, t_cnt in s["types"].items():
                    if t_cnt > 0:
                        types_parts.append(f"{t_name} {t_cnt} 个")
                types_str = "，".join(types_parts) if types_parts else "无（未抓取到新类型作品）"

                blk = tmpl["author_fmt"].format(
                    nick=nick,
                    total_fetched=s["total_fetched"],
                    types_str=types_str,
                    refetch_cnt=s.get("refetch_cnt", 0),
                    skip_cnt=s["skip_cnt"],
                    fail_cnt=s["fail_cnt"]
                )
                author_blocks.append(blk)
        else:
            author_blocks.append("👀 本轮巡视未捕获到任何博主作品动态~")

        summary_block = tmpl["summary_fmt"].format(
            total_success=total_success,
            total_refetch=total_refetch,
            total_skipped=total_skipped,
            total_failed=total_failed
        )

        content_lines = [
            f"{tmpl['intro']}<br>",
            f"⏰ <b>巡视时间：</b>{now_str}<br>",
            "👥 <b>【各博主详细战果】</b><br>" + "<br><br>".join(author_blocks) + "<br>",
            f"{summary_block}<br>"
        ]

        if fails:
            content_lines.append("❌ <b>失败明细与诊断提示：</b>")
            for f in fails[:8]:
                content_lines.append(f"  • ⚠️ {f}")
            if len(fails) > 8:
                content_lines.append(f"  • ...等共 {len(fails)} 项异常")
            content_lines.append("💡 <i>提示：若频繁失败，可能是网络波动或文件大小超出限制，系统将在下一轮重试。</i><br>")

        content_lines.append(f"✨ <i>{tmpl['closing']}</i>")

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
    global is_cookie_invalid
    try:
        if "/aweme/v1/web/aweme/post/" in resp.url and resp.status == 200:
            j = resp.json()
            st = j.get("status_code")
            api_status.append(st)
            if st != 0:
                is_cookie_invalid = True
                print(f"[warn] API returned non-zero status code ({st}), marking Cookie invalid")
            for it in j.get("aweme_list") or []:
                collected[it["aweme_id"]] = it
    except Exception:
        pass

def crawl():
    """
    【核心抓取逻辑：用无头浏览器打开抖音主页并根据 Cookie 状态决定抓取模式】
    - Cookie 有效：正常向下滚动以加载全量作品。
    - Cookie 失效：降级抓取模式！不进行页面下翻（防止触发验证码风控与弹窗），仅保留页面刷新时获取到的视频作品。
    """
    global is_cookie_invalid

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

        page = ctx.new_page()
        page.on("response", on_resp)

        for u in SHARE_URLS:
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
                    continue

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
            except Exception as e:
                print(f"[warn] Failed to open/crawl URL {u}: {e}")

        # 检查验证码风控
        if ("verify" in page.url) or ("captcha" in page.url):
            p0("触发抖音验证码/风控限制",
               detail=f"访问页面触发风控重定向：{page.url}\n可能是短时间内请求过于频繁。系统已自动保护性暂停保存任务。",
               err_type="CAPTCHA_RISK_CONTROL")
            browser.close(); sys.exit(5)

        time.sleep(2)

        # 检查是否因为 Cookie 失效导致 API 异常
        if api_status and all(s not in (0,) for s in api_status):
            is_cookie_invalid = True
            print(f"[crawl] API statuses all non-zero ({api_status}), marked Cookie invalid")

        # 仅在非 Cookie 降级模式且依然没有收集到作品时才补充滚动
        if not is_cookie_invalid:
            empty_rounds = 0
            while len(collected) < MAX_PER_RUN * 3 and empty_rounds < 6:
                before = len(collected)
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(2000 + random.randint(500, 2000))
                empty_rounds = empty_rounds + 1 if len(collected) == before else 0

        _b = page.content()
        _cap = ("验证码" in _b) or ("captcha" in _b.lower())
        print(f"[diag] bodylen={len(_b)} api={api_status} items={len(collected)} cap={_cap} cookie_invalid={is_cookie_invalid}")
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

    # 清晰度与标注后缀
    video_info = item.get("video") or {}
    vw = video_info.get("width") or 0
    vh = video_info.get("height") or 0
    res_str = f"{min(vw, vh)}P" if (vw and vh) else "720P"

    clarity_tag = f"[{res_str}_Cookie失效降级]" if is_degraded else ""

    aid = f"{cdate}_{safe}{('_' + clarity_tag) if clarity_tag else ''}"

    # 防止重名
    n = 1
    while aid in used_names:
        aid = f"{cdate}_{safe}{('_' + clarity_tag) if clarity_tag else ''}({n})"
        n += 1
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

            lv = ((img.get("video") or {}).get("play_addr") or {}).get("url_list") or []
            live_ok = False
            if lv:
                vd = fetch(lv[0])
                if vd and wd_put(f"{folder}/{aid}_img{i}_live.mp4", vd):
                    files.append(f"{folder}/{aid}_img{i}_live.mp4")
                    live_ok = True
                else:
                    fails.append(f"作品【{aid}】图{i} LivePhoto动图下载或保存失败")

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

        if brs:
            best = max(brs, key=lambda b: b.get("bit_rate", 0))
            url = ((best.get("play_addr") or {}).get("url_list") or [None])[0]
            gear_info = {
                "chosen_gear": best.get("gear_name"),
                "chosen_bitrate": best.get("bit_rate"),
                "resolution": f"{video.get('width')}x{video.get('height')}",
                "all_gears": [[b.get("gear_name"), b.get("bit_rate")] for b in brs]
            }
        print(f"[quality] {aid} (degraded={is_degraded}) -> {gear_info}")

        if not url:
            url = ((video.get("play_addr") or {}).get("url_list") or [None])[0]

        if url:
            # 在非降级且 Cookie 正常时尝试 1080P
            if not is_degraded:
                t = try1080(video, gear_info, fetch)
                vd, gear_info = t[0] or fetch(url), t[1]
            else:
                vd = fetch(url)

            if vd is None:
                fails.append(f"视频【{aid}】网络数据抓取失败")
            elif wd_put(f"{folder}/{aid}_video.mp4", vd):
                files.append(f"{folder}/{aid}_video.mp4")
            else:
                fails.append(f"视频【{aid}】WebDAV网盘上传失败")

    cover_ok = False
    cover_url, cover_ext = cover_of(item)
    if cover_url:
        cd = fetch(cover_url, retry=1)
        if cd and wd_put(f"{folder}/封面/{aid}.{cover_ext}", cd):
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
                fails.append(f"作品【{hid}】重新抓取高清发生异常：{e}")
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
            fails.append(f"作品【{hid}】处理发生异常：{e}")
            author_stats[nick]["fail_cnt"] += 1

        time.sleep(random.randint(3, 8))

    # 3. 保存历史记录
    json.dump(history, open(HIST_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

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
