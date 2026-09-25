import os
import sys
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text
from dotenv import load_dotenv

console = Console()

ENV_EXAMPLE_CONTENT = """# ==============================================================================
# 抖音视频/图集自动备份与同步工具 - 本地配置文件 (.env)
#
# 【使用说明】
# 请填写以下配置参数。填写完成后，保存此文件。
# ==============================================================================

# 1. 抖音网页版登录 Cookie (必需/建议)
# 获取方法：电脑/手机浏览器登录 douyin.com，使用 Cookie-Editor 插件导出 Header String 格式
DOUYIN_COOKIE=""

# 2. 需要备份的抖音博主主页链接 (必需)
# 支持单个或多个链接，可直接粘贴抖音分享口令，多个链接请用空格或逗号隔开
DOUYIN_URL="https://www.douyin.com/user/MS4wLjABAAAA..."

# 3. WebDAV 网盘存储配置 (必需)
# 例如坚果云: https://dav.jianguoyun.com/dav
WEBDAV_URL="https://dav.jianguoyun.com/dav"
WEBDAV_USER="your_email@example.com"
WEBDAV_PASS="your_webdav_app_password"

# 4. 飞书机器人 Webhook 地址 (可选)
# 用于接收每日巡逻与运行战报推送
FEISHU_WEBHOOK=""

# 5. 每次运行最多抓取作品数 (默认 30)
MAX_PER_RUN="30"
"""

def print_welcome_banner():
    welcome_text = Text()
    welcome_text.append("🚀 抖音视频/图集自动备份与同步工具\n", style="bold cyan")
    welcome_text.append("   - Windows 本地命令行运行终端 (v1.0.0)\n", style="dim white")
    welcome_text.append("   - 包含网页自动抓取、1080P原画重抓、WebDAV同步与飞书推送", style="italic green")

    console.print(Panel(welcome_text, title="[bold yellow]欢迎使用[/bold yellow]", border_style="cyan", expand=False))

def check_and_load_env():
    env_path = ".env"
    env_example_path = ".env.example"

    if not os.path.exists(env_path):
        console.print("[bold yellow]⚠️ 未检测到本地配置文件 .env ![/bold yellow]")
        if not os.path.exists(env_example_path):
            with open(env_example_path, "w", encoding="utf-8") as f:
                f.write(ENV_EXAMPLE_CONTENT)
            console.print(f"[green]✨ 已自动为你生成配置文件模板: [bold]{env_example_path}[/bold][/green]")
        else:
            console.print(f"[dim]已存在配置文件模板: {env_example_path}[/dim]")

        console.print("\n[bold red]👉 请复制 .env.example 并重命名为 .env，或者直接编辑生成的配置文件填入参数后再重新运行此程序！[/bold red]")
        sys.exit(0)

    load_dotenv(dotenv_path=env_path, override=True)
    console.print("[bold green]✅ 成功加载 .env 配置文件[/bold green]")

def check_playwright_browser():
    console.print("[dim]🔍 正在检测 Playwright Chromium 浏览器内核状态...[/dim]")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            executable = p.chromium.executable_path
            if not os.path.exists(executable):
                raise FileNotFoundError(f"Chromium executable not found at: {executable}")
        console.print("[bold green]✅ Playwright Chromium 浏览器内核已就绪[/bold green]")
    except Exception as e:
        console.print(Panel(
            "[bold red]❌ 未检测到 Playwright Chromium 浏览器内核或初始化失败！[/bold red]\n\n"
            "首次运行或打包环境需要安装 Chromium 浏览器内核，请在命令行中运行以下命令进行安装：\n\n"
            "  [bold yellow]playwright install chromium[/bold yellow]\n\n"
            f"[dim]错误详情: {e}[/dim]",
            title="[bold red]环境缺失提醒[/bold red]",
            border_style="red"
        ))
        sys.exit(1)

def main_wrapper():
    print_welcome_banner()
    check_and_load_env()
    check_playwright_browser()

    console.print("\n[bold green]🚀 环境检测全部通过，即将启动备份主程序...[/bold green]\n" + "="*60 + "\n")

    try:
        from main import main as run_main
        run_main()
        console.print("\n" + "="*60 + "\n[bold green]🎉 备份任务执行完毕！[/bold green]")
    except KeyboardInterrupt:
        console.print("\n\n" + Panel(
            "[bold yellow]👋 接收到键盘中断信号 (Ctrl+C)，程序已优雅退出。[/bold yellow]",
            border_style="yellow",
            expand=False
        ))
        sys.exit(0)
    except Exception as e:
        console.print(f"\n[bold red]❌ 主程序运行发生异常: {e}[/bold red]")
        sys.exit(1)

if __name__ == "__main__":
    main_wrapper()
