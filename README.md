# 抖音博主作品自动备份与同步工具 🚀

欢迎使用本工具！这是一个专为抖音设计的**自动备份与同步脚本**。即使你**完全不懂代码**，也可以通过这篇教程轻松学会如何使用它。

---

## 💡 用大白话告诉你：这个脚本是怎么运行的？

你可以把这个脚本想象成一个**24小时不休息的“数字搬运工”**。它的工作流程非常简单直观：

1. **自动打开抖音（模拟真人浏览）**
   脚本会在后台启动一个“隐形浏览器”（Playwright），就像你平时在电脑上打开网页一样，自动进入你指定的抖音博主主页。

2. **自动刷视频（滚动页面）**
   它会像你刷抖音一样，自动往下滑动页面，让博主最新发布的作品不断加载出来。

3. **捕获高清数据（智能提取）**
   在页面加载的同时，脚本会在后台悄悄拦截抖音的数据接口，从中挑选出最高画质的视频（最高支持尝试获取 1080P 超高清）或者高清无水印原图图集。如果是动图（Live Photo），它也会自动保存动图视频。

4. **上传到你的网盘（WebDAV 存储）**
   下载好视频、图片、封面和博主头像后，脚本会按照“抖音/博主名字/作品”的目录结构，把文件安全地上传到你的个人网盘（如坚果云、Alist、NAS 等 WebDAV 网盘）。

5. **记录历史，绝不重复下载**
   脚本每次成功保存作品后，都会在一个叫做 `history.json` 的小本本上做记号。下次再运行时，已经下载过的作品就会自动跳过，既省时又省流量。

6. **飞书机器人汇报**
   工作完成后，它会在飞书群里给你发一条消息，告诉你今天新增备份了几个作品、跳过了几个作品，让你一目了然！

---

## 🔗 博主主页 URL 示例配置

在配置变量 `DOUYIN_URL` 时，你可以填入你想备份的博主主页链接（支持配置多个，可以用逗号或换行隔开）。

以下是原本项目中配置的博主主页示例链接：

```text
https://www.douyin.com/user/MS4wLjABAAAA64tdxMeXyrQVJXAx5aE8Fk7NtU3stoQhwsqv-wP_SerqGiuQfLgeOtUhU1Tna07l
https://www.douyin.com/user/MS4wLjABAAAAkj57HrJK_90RfPHSJ0SxoBcJHqiM9ivTdCSXTDmP38_OZUGQjqDl4xIh2uaJpu5f
https://www.douyin.com/user/MS4wLjABAAAA2UKy3mj6WwWGjPIn-XBv9CEWDGFbJwU5vpS9VuyvBEk
```

> **如何获取博主主页链接？**
> 打开电脑版抖音网页（https://www.douyin.com ），搜索并进入你喜欢的博主主页，复制浏览器地址栏里的网址即可！

---

## 🛠️ 详细使用指南

下面为你一步步讲解如何配置并运行这个项目。

### 第一步：准备必要信息

在运行脚本前，你需要准备好以下 5 种参数（环境变量）：

| 环境变量名称 | 说明 | 示例 |
| :--- | :--- | :--- |
| `DOUYIN_COOKIE` | 抖音账号登录凭证（获取方法见下文） | `passport_csrf_token=...; sessionid=...` |
| `DOUYIN_URL` | 目标博主的主页链接（多个用逗号或换行分隔） | 上文提到的示例链接 |
| `WEBDAV_URL` | 你的 WebDAV 网盘服务器地址 | `https://dav.jianguoyun.com/dav` |
| `WEBDAV_USER` | WebDAV 网盘账号 | `your_email@example.com` |
| `WEBDAV_PASS` | WebDAV 网盘密码/应用密码 | `your_webdav_password` |
| `FEISHU_WEBHOOK` | 飞书自定义机器人的 Webhook 链接 | `https://open.feishu.cn/open-apis/bot/v2/hook/...` |
| `MAX_PER_RUN` | （可选）单次运行最多下载的新作品数，默认 `30` | `30` |

---

### 💡 如何获取抖音 `DOUYIN_COOKIE`？

1. 用电脑浏览器（建议 Chrome 或 Edge）打开 [抖音网页版](https://www.douyin.com) 并登录你的账号。
2. 按键盘上的 `F12` 键（或者右键点击网页选择“检查”），打开开发者工具。
3. 点击顶部的 **“网络” (Network)** 选项卡。
4. 刷新一下抖音页面（按 `F5`），然后在网络请求列表中找到任意一个以 `www.douyin.com` 开头的请求。
5. 点击该请求，在右侧的 **“标头” (Headers)** -> **“请求标头” (Request Headers)** 里找到 `Cookie`。
6. 复制 `Cookie:` 后面的全部文本，这就是你的 `DOUYIN_COOKIE`。

---

### 第二步：运行项目

本项目支持在**本地电脑**或者 **GitHub Actions（定时自动运行）** 中执行。

#### 方式 A：在本地电脑运行

1. **安装 Python 3.8+** 和 **ffmpeg**（用于画质检测）。
2. **安装依赖包**：
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```
3. **设置环境变量**（以 Linux / macOS 为例）：
   ```bash
   export DOUYIN_COOKIE="你的Cookie"
   export DOUYIN_URL="https://www.douyin.com/user/MS4wLjABAAAA64tdxMeXyrQVJXAx5aE8Fk7NtU3stoQhwsqv-wP_SerqGiuQfLgeOtUhU1Tna07l"
   export WEBDAV_URL="你的WebDAV地址"
   export WEBDAV_USER="你的WebDAV账号"
   export WEBDAV_PASS="你的WebDAV密码"
   export FEISHU_WEBHOOK="你的飞书Webhook"
   export MAX_PER_RUN="30"
   ```
4. **运行脚本**：
   ```bash
   python main.py
   ```

#### 方式 B：使用 GitHub Actions 云端定时自动运行（推荐 🌟）

1. 将本项目代码 Fork 或推送到你的 GitHub 仓库。
2. 打开仓库设置：`Settings` -> `Secrets and variables` -> `Actions`。
3. 点击 **New repository secret**，依次添加上述环境变量（`DOUYIN_COOKIE`, `DOUYIN_URL`, `WEBDAV_URL`, `WEBDAV_USER`, `WEBDAV_PASS`, `FEISHU_WEBHOOK`）。
4. 在 GitHub Actions 中开启定时任务，脚本就会每天定时自动帮你备份博主的更新！

---

## ❓ 常见问题与提示

* **收到飞书提醒“Cookie suspected invalid”？**
  说明你的抖音 Cookie 已经失效或者过期了，重新按照教程获取最新的 Cookie 并更新环境变量即可。
* **收到飞书提醒“Triggered Douyin captcha risk control”？**
  说明触发了抖音的验证码或风控机制，系统会自动暂停本次下载，防止账号受影响。稍等一段时间后再试即可。
* **保存的文件在哪？**
  文件会自动储存在你的 WebDAV 网盘中，路径结构为：`douyin/博主昵称/作品文件名.mp4` 和 `douyin/博主昵称/封面/`。

---
