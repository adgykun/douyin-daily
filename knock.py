import os
import subprocess


def try1080(video, gi, fetch):
    uri = (video.get("play_addr") or {}).get("uri") or ""
    if not uri or "1080" in str((gi or {}).get("chosen_gear")):
        return None, gi
    cand = "https://www.douyin.com/aweme/v1/play/?video_id=" + uri + "&ratio=1080p&line=0"
    cd = fetch(cand)
    if not cd or len(cd) < 100000:
        return None, gi
    open("_cand.mp4", "wb").write(cd)
    result = (None, gi)
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,bit_rate", "-of", "csv=p=0", "_cand.mp4"], capture_output=True, text=True, timeout=60).stdout.strip()
        parts = (out.split(",") + ["0", "0", "0"])[:3]
        cb = int(float(parts[2] or 0))
        if int(parts[1] or 0) >= 1080 and (cb == 0 or cb > int(gi.get("chosen_bitrate") or 0)):
            result = (cd, {"chosen_gear": "play_1080p_verified", "chosen_bitrate": cb or int(gi.get("chosen_bitrate") or 0), "resolution": parts[0] + "x" + parts[1], "all_gears": gi.get("all_gears")})
    except Exception as e:
        print("[quality] probe failed:", e)
    try:
        os.remove("_cand.mp4")
    except Exception:
        pass
    return result
