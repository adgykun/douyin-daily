# ==============================================================================
# knock.py - 尝试获取抖音 1080P 超高清视频的辅助模块
#
# 【这段代码是干嘛的？】
# 当我们在抖音拿到的默认视频画质不够高（比如只有720P或更低）时，
# 这个脚本会尝试拼凑出 1080P 高清视频的下载地址，把视频下载下来，
# 并调用系统工具（ffprobe）检测它到底是不是真正的 1080P 高清视频。
# ==============================================================================

import os
import subprocess


def try1080(video, gi, fetch):
    """
    【尝试获取 1080P 画质视频】
    参数说明：
    - video: 抖音返回的视频元数据字典
    - gi: 当前已选择的视频画质/码率信息（gear_info）
    - fetch: 用于网络下载文件的函数

    返回值：
    - (视频二进制数据, 更新后的画质信息)
    """
    # 从视频数据里拿到视频的唯一标识 ID (uri)
    uri = (video.get("play_addr") or {}).get("uri") or ""

    # 如果拿不到视频 ID，或者当前已经拿到了 1080P 画质，就不需要再折腾了，直接返回空
    if not uri or "1080" in str((gi or {}).get("chosen_gear")):
        return None, gi

    # 尝试拼接 1080P 超高清视频的请求链接
    cand = "https://www.douyin.com/aweme/v1/play/?video_id=" + uri + "&ratio=1080p&line=0"

    # 下载这个候选的高清视频
    cd = fetch(cand)
    # 如果下载失败，或者下载下来的文件太小（小于 100KB），说明没拿到真正视频，直接放弃
    if not cd or len(cd) < 100000:
        return None, gi

    # 把下载下来的视频临时存到本地临时文件 _cand.mp4 中
    open("_cand.mp4", "wb").write(cd)
    result = (None, gi)

    try:
        # 使用 ffprobe 命令行工具读取这个临时视频文件的分辨率（长宽）和码率信息
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,bit_rate", "-of", "csv=p=0", "_cand.mp4"],
            capture_output=True,
            text=True,
            timeout=60
        ).stdout.strip()

        # 解析 ffprobe 输出的结果：宽度, 高度, 码率
        parts = (out.split(",") + ["0", "0", "0"])[:3]
        cb = int(float(parts[2] or 0))

        # 判断：如果视频的高度大于等于 1080，并且码率符合预期，说明确实是 1080P 超高清！
        if int(parts[1] or 0) >= 1080 and (cb == 0 or cb > int(gi.get("chosen_bitrate") or 0)):
            # 记录成功拿到的高清视频数据和对应的信息
            result = (cd, {
                "chosen_gear": "play_1080p_verified",
                "chosen_bitrate": cb or int(gi.get("chosen_bitrate") or 0),
                "resolution": parts[0] + "x" + parts[1],
                "all_gears": gi.get("all_gears")
            })
    except Exception as e:
        # 如果电脑里没装 ffprobe 或者解析报错，打印提示信息
        print("[quality] probe failed:", e)

    # 任务完成后删除临时文件，保持目录整洁
    try:
        os.remove("_cand.mp4")
    except Exception:
        pass

    return result
