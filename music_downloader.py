# -*- coding: utf-8 -*-
"""
flac.music.hi.cn 音乐搜索下载工具
====================================
功能：
1. 搜索歌曲（酷我/网易云双平台）
2. 全部结果加载到列表（自动翻页，无需滚动）
3. 逐条/批量播放（内嵌播放优先，失败用系统播放器）
4. 逐条/批量下载（MP3 极高320K 优先，无则普通128K）
5. 歌词下载（默认勾选）
6. 自定义保存目录

使用：python music_downloader.py
依赖：仅 Python 标准库（tkinter 内置），无需 pip 安装任何包。
可选：pip install pygame 可获得更好的内嵌播放体验。
"""

import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:
    print("错误：需要 tkinter，请使用完整版 Python（python.org 安装包自带）。")
    sys.exit(1)

# ======================================================================
# 配置
# ======================================================================
BASE_URL = "https://flac.music.hi.cn"
API_URL = BASE_URL + "/ajax.php"
PAGE_SIZE = 20          # 网站每页大小
MAX_PAGES = 50          # 安全上限，防止无限翻页
REQ_TIMEOUT = 30        # 请求超时（秒）

# 配置文件：保存目录历史等设置，持久化在项目目录下
CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.json"
)


def load_config():
    """读取配置文件，返回 dict（不存在或损坏则返回空 dict）"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    """保存配置到文件"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def pick_best_quality(minfo):
    """从 minfo 列表中挑选最佳音质（用户策略：极高320K -> 普通128K，FLAC不参与）。
    返回 minfo 中的一项 dict，或 None"""
    for m in minfo:
        if m.get("format") == "mp3" and str(m.get("bitrate")) == "320":
            return m
    for m in minfo:
        if m.get("format") == "mp3":
            return m
    return None


# ======================================================================
# HTTP 客户端
# ======================================================================
class ApiClient:
    """封装对 ajax.php 的请求，带 Cookie 保持与失败重试"""

    def __init__(self):
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            ("Referer", BASE_URL + "/"),
            ("Content-Type", "application/x-www-form-urlencoded;charset=UTF-8"),
        ]
        self._last_req = 0.0
        self._req_lock = threading.Lock()

    def _throttle(self):
        """请求限速：每次请求间隔至少 0.8 秒，避免触发网站限流"""
        with self._req_lock:
            now = time.time()
            wait = 0.8 - (now - self._last_req)
            if wait > 0:
                time.sleep(wait)
            self._last_req = time.time()

    def _post(self, act, params, retries=3):
        """POST ajax.php?act=xxx，参数自动 URL 编码。失败自动重试（退避）"""
        last_err = None
        for attempt in range(retries):
            self._throttle()
            try:
                url = API_URL + "?act=" + act
                data = urllib.parse.urlencode(
                    {k: v for k, v in params.items() if v is not None}
                ).encode("utf-8")
                with self.opener.open(url, data=data, timeout=REQ_TIMEOUT) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                last_err = e
                # 指数退避：1s, 2s, 4s
                backoff = 2 ** attempt + random.uniform(0, 0.5)
                time.sleep(backoff)
        raise RuntimeError(f"请求失败（重试{retries}次）: {last_err}")

    def search(self, keyword, platform, page, size=PAGE_SIZE):
        """搜索，返回 (list, total) 或抛异常。
        注意：限流时接口返回 code=0 但 list 为空，需检测并重试"""
        for attempt in range(3):
            result = self._post("search", {
                "platform": platform, "keyword": keyword,
                "page": page, "size": size,
            })
            if result.get("code") != 0:
                raise RuntimeError("搜索失败：" + result.get("msg", "接口返回异常"))
            data = result.get("data") or {}
            items = data.get("list") or []
            total = int(data.get("total") or 0)
            # 限流特征：total > 0 但 list 为空
            if not items and total > 0 and page == 1:
                time.sleep(3 + attempt * 3)
                continue
            return items, total
        raise RuntimeError("搜索多次返回空列表，可能被限流，请稍后再试")

    def get_url(self, song, format_, bitrate, platform, retries=3):
        """获取播放/下载地址。song 为搜索结果单条 dict。失败自动重试"""
        last_err = None
        for attempt in range(retries):
            result = self._post("getUrl", {
                "platform": platform,
                "songid": song["id"],
                "format": format_,
                "bitrate": bitrate,
                "time": song.get("time"),
                "sign": song.get("sign"),
            })
            if result.get("code") == 0:
                data = result.get("data") or {}
                url = data.get("url")
                if url:
                    return url
                last_err = "接口未返回有效地址"
            else:
                last_err = "获取地址失败：" + result.get("msg", "接口返回异常")
            time.sleep(2 ** attempt + random.uniform(0, 0.5))
        raise RuntimeError(last_err)

    def get_lyric(self, song, platform, retries=2):
        """获取歌词文本。无歌词返回空字符串。失败自动重试"""
        for attempt in range(retries):
            result = self._post("getLyric", {
                "platform": platform,
                "songid": song["id"],
                "time": song.get("time"),
                "sign": song.get("sign"),
            })
            if result.get("code") == 0:
                data = result.get("data")
                return data if isinstance(data, str) else ""
            time.sleep(1.5)
        return ""


# ======================================================================
# 下载器
# ======================================================================
def sanitize_filename(name):
    """清理文件名中的非法字符"""
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:150] if name else "未知歌曲"


def safe_song_name(song):
    """生成 "歌名 - 歌手" 格式的文件名"""
    name = song.get("name") or "未知歌曲"
    artist = song.get("artist") or ""
    if artist:
        return sanitize_filename(name + " - " + artist)
    return sanitize_filename(name)


def download_file(url, save_dir, filename, log_cb=None, should_cancel=None):
    """下载文件到 save_dir/filename。log_cb(text) 用于回调进度。
    should_cancel() 返回 True 时中止下载并抛 RuntimeError"""
    path = os.path.join(save_dir, filename)
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]
    with opener.open(url, timeout=REQ_TIMEOUT) as resp, open(path, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if should_cancel and should_cancel():
                raise RuntimeError("下载已取消")
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if log_cb and total:
                pct = int(done * 100 / total)
                log_cb(f"{filename}: {pct}% ({done // 1024}KB / {total // 1024}KB)")
    return path


# ======================================================================
# 播放器
# ======================================================================
class Player:
    """播放控制：pyaudio + numpy 精确播放，支持暂停和进度条拖动跳转。
    音频经 pygame 解码为 PCM 数组后由 pyaudio 回调播放，
    跳转/暂停完全可控（不依赖 SDL_mixer 的 MP3 seek 支持）。
    pyaudio/numpy 不可用时回退到 pygame.mixer.music。

    快速切歌防护：
    - 每次 play() 分配一个代数 gen，工作线程在下载完成、解码完成、
      开播、收尾四个点校验代数，被新歌取代则立即放弃；
    - 停止/清流期间回调返回静音并跳过被弃置的 PCM；
    - 临时文件名加随机后缀避免同名歌曲撞车。"""

    def __init__(self):
        # pygame 用于解码音频（sndarray 转 PCM 数组）
        self._pygame = None
        try:
            import pygame  # type: ignore
            self._pygame = pygame
            pygame.mixer.init()
        except Exception:
            self._pygame = None
        # pyaudio 用于播放（支持 seek）
        self._pa = None
        self._pa_obj = None
        try:
            import pyaudio  # type: ignore
            self._pa = pyaudio
            self._pa_obj = pyaudio.PyAudio()
        except Exception:
            self._pa = None
            self._pa_obj = None
        # numpy 用于音频数据处理
        self._np = None
        try:
            import numpy as np  # type: ignore
            self._np = np
        except Exception:
            self._np = None
        # 播放状态
        self._stream = None
        self._pcm = None       # numpy 数组 (samples, channels) float32
        self._freq = None      # 采样率
        self._channels = None  # 声道数
        self._pos = 0          # 当前播放位置（采样索引）
        self._playing = False
        self._paused = False
        self._stopped = False
        self._gen = 0          # 播放代数，每次 play() 递增
        self._lock = threading.Lock()  # 保护共享播放状态
        self._decode_lock = threading.Lock()  # 串行化 SDL_mixer 解码
        self._mode = "pa" if (self._pa and self._np and self._pygame) else "pygame"
        self._current_tmp = None
        self._current_title = ""
        self._current_duration = 0
        # 临时文件目录：项目目录下的"临时"文件夹
        self._tmp_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "临时"
        )
        os.makedirs(self._tmp_dir, exist_ok=True)

    @property
    def using_pygame(self):
        """内嵌播放是否可用（pyaudio 优先，pygame 兜底）"""
        return self._pa is not None or self._pygame is not None

    # ---- 播放状态查询（供进度条轮询） ----

    def is_active(self):
        """音乐已加载（播放中或暂停中）"""
        with self._lock:
            if self._mode == "pa":
                return self._playing or self._paused
        if self._mode == "pygame" and self._pygame is not None:
            return self._pygame.mixer.music.get_busy()
        return False

    def is_playing(self):
        """是否正在内嵌播放（暂停状态不算）"""
        with self._lock:
            if self._mode == "pa":
                return self._playing
        if self._mode == "pygame" and self._pygame is not None:
            return self._pygame.mixer.music.get_busy() and not self._paused
        return False

    @property
    def is_paused(self):
        with self._lock:
            return self._paused

    def pause(self):
        """暂停播放。校验代数，避免作废线程把新歌标记为暂停"""
        with self._lock:
            gen = self._gen
            if self._mode == "pa":
                if self._playing and self._stream:
                    stream = self._stream
                else:
                    return False
            else:
                # 锁内不能调用 is_playing()（会重入死锁），直接读标志
                if (self._pygame is not None
                        and self._pygame.mixer.music.get_busy()
                        and not self._paused):
                    try:
                        self._pygame.mixer.music.pause()
                        self._paused = True
                        return True
                    except Exception:
                        return False
                return False
        # 锁外停流：回调线程需要此锁，持锁调用 stop_stream 会互相阻塞
        try:
            stream.stop_stream()
        except Exception:
            return False
        with self._lock:
            if gen != self._gen:
                return False  # 期间已切歌/停止，状态已重置
            self._playing = False
            self._paused = True
        return True

    def unpause(self):
        """继续播放。校验代数，避免作废线程恢复已停止的播放"""
        with self._lock:
            gen = self._gen
            if self._mode == "pa":
                if self._paused and self._stream:
                    self._playing = True
                    self._paused = False
                    stream = self._stream
                else:
                    return False
            else:
                if self._pygame is not None and self._paused:
                    try:
                        self._pygame.mixer.music.unpause()
                        self._paused = False
                        return True
                    except Exception:
                        return False
                return False
        # 锁外启流：回调会在 PortAudio 线程触发并需要此锁
        try:
            stream.start_stream()
        except Exception:
            with self._lock:
                if gen == self._gen:
                    self._playing = False
                    self._paused = True
            return False
        with self._lock:
            if gen != self._gen:
                return False  # 期间已切歌/停止
        return True

    def get_position(self):
        """当前播放位置（秒），未播放返回 0"""
        with self._lock:
            if self._mode == "pa":
                if self._playing or self._paused:
                    return self._pos / self._freq if self._freq else 0.0
                return 0.0
        if self._mode == "pygame" and self._pygame is not None and self.is_active():
            return self._pygame.mixer.music.get_pos() / 1000.0
        return 0.0

    def get_length(self):
        """当前歌曲总时长（秒）"""
        with self._lock:
            if self._mode == "pa" and self._pcm is not None:
                return self._pcm.shape[0] / self._freq if self._freq else 0.0
        if self._mode == "pygame" and self._pygame is not None:
            try:
                length = self._pygame.mixer.music.get_length()
                if length > 0:
                    return length
            except Exception:
                pass
        return self.current_duration

    def seek(self, seconds):
        """跳转到指定位置（秒）。播放中或暂停时均可。成功返回 True"""
        with self._lock:
            if self._pa is not None:
                if not (self._playing or self._paused):
                    return False
                self._pos = max(0, int(seconds * self._freq))
                need_reopen = self._playing and self._stream is not None
                stream = self._stream
                pa_path = True
            else:
                pa_path = False
        if pa_path:
            if need_reopen:
                # 锁外关闭旧流：回调线程需要此锁
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
                new_stream = self._open_stream()
                # 期间可能被 stop() 关闭：捕获引用再操作，避免 None
                if new_stream is not None:
                    try:
                        new_stream.start_stream()
                    except Exception:
                        pass
            # 暂停时跳转：只更新位置，保持暂停
            return True
        if self._pygame is not None and self.is_active():
            try:
                self._pygame.mixer.music.set_pos(max(0, int(seconds)))
                return True
            except Exception:
                return False
        return False

    def _callback(self, in_data, frame_count, time_info, status):
        """pyaudio 回调：提供音频数据。
        被弃置/停止期间返回静音，避免读到新歌的 PCM 造成串音。
        回调内必须自己兜住异常：抛出去会导致 PortAudio 停流无声。"""
        try:
            with self._lock:
                ch = self._channels or 2
                if not self._playing or self._pcm is None:
                    return (b'\x00\x00\x00\x00' * frame_count * ch,
                            self._pa.paContinue)
                end = self._pos + frame_count
                if end >= self._pcm.shape[0]:
                    self._playing = False
                    return (b'\x00\x00\x00\x00' * frame_count * ch,
                            self._pa.paComplete)
                chunk = self._pcm[self._pos:end]
                self._pos = end
                return (chunk.tobytes(), self._pa.paContinue)
        except Exception:
            # 兜底：任何异常都返回静音，绝不让异常穿透回调
            ch = self._channels or 2
            return (b'\x00\x00\x00\x00' * frame_count * ch, self._pa.paContinue)

    def _open_stream(self):
        """打开 pyaudio 输出流，设置为当前流并返回"""
        stream = self._pa_obj.open(
            format=self._pa.paFloat32,
            channels=self._channels,
            rate=self._freq,
            output=True,
            stream_callback=self._callback
        )
        self._stream = stream
        return stream

    def _close_stream(self):
        """关闭当前 pyaudio 输出流。
        调用方须保证当前流属于自己（工作线程先校验代数；
        play()/stop() 递增代数后立即调用，天然归属自己）"""
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass

    @property
    def current_title(self):
        return self._current_title

    @property
    def current_duration(self):
        return self._current_duration

    def _remove_file(self, path):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    def cleanup(self):
        """清理临时目录中的所有文件（程序退出时调用）"""
        try:
            for name in os.listdir(self._tmp_dir):
                self._remove_file(os.path.join(self._tmp_dir, name))
        except OSError:
            pass

    def stop(self):
        """停止播放。递增代数使在途工作线程作废，等待其自行退出"""
        with self._lock:
            self._gen += 1
            self._stopped = True
            self._paused = False
            self._playing = False
            if self._mode == "pygame" and self._pygame is not None:
                # 持锁 stop：与工作线程的 load+play 串行
                try:
                    self._pygame.mixer.music.stop()
                except Exception:
                    pass
        self._close_stream()
        with self._lock:
            self._pcm = None
            tmp = self._current_tmp
            self._current_tmp = None
        self._remove_file(tmp)

    def play(self, url, title="", on_done=None, status_cb=None, duration=None):
        """下载音频到临时文件后播放。立即返回，播放完毕回调 on_done()。
        status_cb(text) 在后台线程中被调用，用于更新界面状态
        duration 为歌曲时长（秒），用于回退模式下估算清理时机"""
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._stopped = False
            self._current_title = title or ""
            self._current_duration = int(duration) if duration else 0
            self._pcm = None
            self._pos = 0
            self._playing = False
            self._paused = False
        # 关闭上一首歌的输出流（锁外执行：音频回调线程需要此锁）
        self._close_stream()
        if self._mode == "pygame" and self._pygame is not None:
            # 回退模式：mixer.music 是全局通道。持锁 stop 与旧工作线程的
            # load+play 串行，避免 stop 与 load 交叉导致状态错乱
            with self._lock:
                try:
                    self._pygame.mixer.music.stop()
                except Exception:
                    pass

        def _do():
            tmp = None
            try:
                suffix = ".flac" if ".flac" in url.lower() else ".mp3"
                safe_title = sanitize_filename(title) if title else "audio"
                # 随机后缀：同一秒切同名歌曲时不会写同一个文件
                tmp = os.path.join(
                    self._tmp_dir,
                    f"{safe_title}_{int(time.time())}_{uuid.uuid4().hex[:8]}{suffix}",
                )
                with self._lock:
                    self._current_tmp = tmp
                if status_cb:
                    status_cb("正在下载音频到 临时/ 文件夹...")
                try:
                    download_file(url, os.path.dirname(tmp), os.path.basename(tmp),
                                  should_cancel=lambda: gen != self._gen)
                except RuntimeError:
                    # 下载期间被新歌取代：静默作废
                    self._remove_file(tmp)
                    with self._lock:
                        if self._current_tmp == tmp:
                            self._current_tmp = None
                    return

                # 下载期间用户可能已切到下一首：旧线程作废
                with self._lock:
                    if gen != self._gen:
                        return

                if self._mode == "pa":
                    # pyaudio + numpy 方案：解码为 PCM 数组后回调播放
                    # 解码持 _decode_lock：SDL_mixer 非线程安全，
                    # 新旧线程同时 Sound() 会崩溃或产出损坏数据
                    with self._decode_lock:
                        sound = self._pygame.mixer.Sound(tmp)
                        pcm = self._np.array(self._pygame.sndarray.array(sound))
                        freq, fmt, ch = self._pygame.mixer.get_init()
                    if pcm.ndim == 1:
                        pcm = pcm.reshape(-1, 1)  # 单声道转 (samples, 1)
                    pcm = pcm.astype(self._np.float32) / 32768.0
                    with self._lock:
                        if gen != self._gen:
                            return
                        self._pcm = pcm
                        self._freq = freq
                        self._channels = ch
                        self._pos = 0
                        self._open_stream()
                        stream = self._stream
                        self._playing = True
                        self._paused = False
                    if gen != self._gen or stream is None:
                        # 开流后被取代：流由新 play()/stop() 负责关闭
                        return
                    try:
                        stream.start_stream()
                    except Exception:
                        if gen == self._gen:
                            raise  # 真错误，交给外层处理
                        return  # 被停止/取代，静默退出
                    if status_cb:
                        status_cb("正在播放（内嵌 pyaudio）...")
                    # 等待播放结束或停止（暂停时继续等待，不退出）。
                    # 注意：不能持锁 sleep，否则音频回调拿不到锁会爆音
                    finished_naturally = False
                    while True:
                        with self._lock:
                            if gen != self._gen:
                                # 已被新歌取代：新 play() 已接管流与状态，
                                # 本线程不得再触碰共享状态
                                break
                            if (not self._playing and not self._paused) or self._stopped:
                                finished_naturally = not self._stopped
                                break
                        time.sleep(0.3)
                    if gen != self._gen:
                        # 被取代：只清理自己的临时文件，流/PCM 归新线程管
                        self._remove_file(tmp)
                        with self._lock:
                            if self._current_tmp == tmp:
                                self._current_tmp = None
                        return
                    self._close_stream()
                    with self._lock:
                        self._playing = False
                        self._paused = False
                        self._pcm = None
                    self._remove_file(tmp)
                    with self._lock:
                        if self._current_tmp == tmp:
                            self._current_tmp = None
                    if finished_naturally and on_done:
                        on_done()
                else:
                    # 回退：pygame.mixer.music
                    # 持锁 load+play：与 play() 的 stop() 串行化，
                    # 并与代数检查构成原子操作，防止旧线程覆盖新歌
                    with self._lock:
                        if gen != self._gen:
                            return
                        self._pygame.mixer.music.load(tmp)
                        self._pygame.mixer.music.play()
                    if status_cb:
                        status_cb("正在播放（内嵌）...")
                    # 等待播放结束或停止；代数变化时立即退出
                    while True:
                        with self._lock:
                            if gen != self._gen or self._stopped:
                                break
                        if not self._pygame.mixer.music.get_busy():
                            break
                        time.sleep(0.3)
                    self._remove_file(tmp)
                    with self._lock:
                        if self._current_tmp == tmp:
                            self._current_tmp = None
                    if gen == self._gen and not self._stopped and on_done:
                        on_done()
            except Exception as e:
                self._remove_file(tmp)
                with self._lock:
                    if self._current_tmp == tmp:
                        self._current_tmp = None
                if status_cb:
                    status_cb("播放失败: " + str(e))
        threading.Thread(target=_do, daemon=True).start()
        return True


# ======================================================================
# 主程序界面
# ======================================================================
class MusicApp:
    PLATFORM_LABEL = {"kuwo": "酷我", "wyy": "网易云"}

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("音乐搜索下载 - flac.music.hi.cn----黄江雄制作")
        self.root.geometry("1000x680")
        self.root.minsize(860, 560)

        # 后台队列（界面更新统一走主线程）
        self.task_queue = []
        self.queue_lock = threading.Lock()

        self.client = ApiClient()
        self.player = Player()
        self.songs = []                    # 全部搜索结果
        self.songs_lock = threading.Lock()
        self.row_map = []                  # 行ID -> songs索引
        self.fetch_stop = threading.Event()

        # 从配置文件恢复保存目录和历史路径
        cfg = load_config()
        self.dir_history = cfg.get("save_dir_history") or []
        last_dir = cfg.get("save_dir")
        self.save_dir = last_dir if last_dir else os.path.join(
            os.path.expanduser("~"), "Music")
        # 当前路径若不在历史中则置顶
        if not self.dir_history or self.dir_history[0] != self.save_dir:
            self.dir_history.insert(0, self.save_dir)
        self.dir_history = self.dir_history[:10]  # 最多保留10条

        # 歌词窗状态（Canvas 重绘，与播放进度同步）
        self.lyric_lines = []          # [(time_sec|None, text), ...]
        self.lyric_active_idx = -1     # 当前高亮行在 lyric_lines 中的索引
        self.lyric_target_scroll = 0.0 # 自动滚动目标（像素）
        self.lyric_scroll = 0.0        # 当前歌词内容纵向偏移（像素）
        self.lyric_dragging = False    # 是否正在拖动横线同步
        self._drag_last_y = 0          # 拖动时上一帧鼠标 y
        self.lyric_line_h = 30         # 每行高度（像素）
        self.lyric_ref_y = 80          # 同步横线在画布中的 y 坐标
        self._lyric_anim_on = False    # 歌词自动滚动动画是否在跑
        self.lyric_placeholder = "（未播放）"
        self._last_lyric_pos = None    # 上一次播放/轮询位置，供暂停后预览
        self.current_lyric_raw = None  # 当前歌曲原始 LRC 文本
        self.current_lyric_title = ""
        self.current_song = None       # 当前播放歌曲 dict
        self.current_platform = None   # 当前平台
        # 歌词视觉样式
        self.LYRIC_FONT = ("Microsoft YaHei", 11)
        self.LYRIC_FONT_ACTIVE = ("Microsoft YaHei", 13, "bold")
        self.lyric_offset_var = tk.DoubleVar(value=0.0)  # 用户手动偏移（秒）

        self._build_ui()
        self._poll_queue()
        self._poll_progress()

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # ===== 顶部：搜索区 =====
        top = ttk.LabelFrame(self.root, text="搜索", padding=10)
        top.pack(fill=tk.X, **pad)

        ttk.Label(top, text="平台：").pack(side=tk.LEFT)
        self.platform_var = tk.StringVar(value="kuwo")
        ttk.Combobox(top, textvariable=self.platform_var, width=8, state="readonly",
                     values=["kuwo", "wyy"]).pack(side=tk.LEFT, padx=(0, 12))

        self.keyword_var = tk.StringVar()
        self.entry_kw = ttk.Entry(top, textvariable=self.keyword_var, width=40)
        self.entry_kw.pack(side=tk.LEFT, padx=(0, 8))
        self.entry_kw.bind("<Return>", lambda e: self.do_search())

        self.btn_search = ttk.Button(top, text="搜索", command=self.do_search)
        self.btn_search.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_load_all = ttk.Button(top, text="加载全部", command=self.load_all_pages)
        self.btn_load_all.pack(side=tk.LEFT)

        # ===== 中部：左列表 + 歌词窗 + 右操作面板 =====
        mid = ttk.Frame(self.root)
        mid.pack(fill=tk.BOTH, expand=True, **pad)

        # 左：歌曲列表（适当缩小列宽，腾出空间给歌词窗）
        list_frame = ttk.Frame(mid)
        list_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # 关键：不让 Treeview 按"最长行文本"撑大列表框宽度（否则选长名字歌曲时列表框忽大忽小）。
        # 列表框尺寸完全由中部布局分配（撑满剩余空间），与歌曲内容无关；歌名列 stretch 仍自动填满。
        list_frame.pack_propagate(False)

        cols = ("idx", "name", "artist", "album", "quality")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("idx", text="#")
        self.tree.heading("name", text="歌名")
        self.tree.heading("artist", text="歌手")
        self.tree.heading("album", text="专辑")
        self.tree.heading("quality", text="音质")
        self.tree.column("idx", width=40, anchor=tk.CENTER, stretch=tk.NO)
        self.tree.column("name", width=150, stretch=tk.YES)
        self.tree.column("artist", width=90, stretch=tk.NO)
        self.tree.column("album", width=110, stretch=tk.NO)
        self.tree.column("quality", width=80, anchor=tk.CENTER, stretch=tk.NO)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        vsb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # 中：歌词窗口（Canvas 重绘，支持「拖动横线」对齐演唱 + 随时间高亮）
        # 横向锁定在 width=180（只纵向拉伸），避免 Canvas 默认宽度把框撑大、遮挡右侧按钮
        lyric_box = ttk.LabelFrame(mid, text="歌词", padding=6)
        lyric_box.config(width=180)
        lyric_box.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0), expand=False)

        # 画布区：歌词逐行绘制在此，横线作为同步基准，可上下拖动
        lyric_canvas_frame = ttk.Frame(lyric_box)
        lyric_canvas_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.lyric_canvas = tk.Canvas(
            lyric_canvas_frame, background="#ffffff",
            width=160,
            relief=tk.FLAT, borderwidth=0, highlightthickness=0,
            cursor="sb_v_double_arrow",
        )
        self.lyric_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        # 拖动歌词 = 调整偏移；窗口尺寸变化时重绘
        self.lyric_canvas.bind("<Button-1>", self._on_lyric_press)
        self.lyric_canvas.bind("<B1-Motion>", self._on_lyric_drag)
        self.lyric_canvas.bind("<ButtonRelease-1>", self._on_lyric_release)
        self.lyric_canvas.bind("<Configure>", lambda e: self._render_lyrics())

        # 底部控制条：偏移读数 + 重置按钮（不再用数字输入框）
        ctrl = ttk.Frame(lyric_box)
        ctrl.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
        ttk.Label(ctrl, text="拖动歌词对齐演唱", font=("Microsoft YaHei", 8),
                  foreground="#999").pack(side=tk.LEFT)
        self.lyric_offset_label = ttk.Label(
            ctrl, text="偏移 +0.0s", font=("Microsoft YaHei", 8), foreground="#999")
        self.lyric_offset_label.pack(side=tk.LEFT, padx=(4, 0))
        ttk.Button(ctrl, text="↺", width=3,
                   command=self._reset_lyric_offset).pack(side=tk.RIGHT)

        self.tree.tag_configure("normal", background="white")
        self.tree.tag_configure("alt", background="#f5f5f5")
        self.tree.tag_configure("playing", background="#fff3cd")
        self.tree.bind("<Double-1>", lambda e: self.play_selected())

        # 右：操作面板
        right = ttk.LabelFrame(mid, text="操作", padding=10)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))

        ttk.Button(right, text="▶ 播放选中", command=self.play_selected).pack(fill=tk.X, pady=3)
        self.btn_pause = ttk.Button(right, text="⏸ 暂停", command=self.toggle_pause)
        self.btn_pause.pack(fill=tk.X, pady=3)
        ttk.Button(right, text="■ 停止", command=self.stop_play).pack(fill=tk.X, pady=3)

        # 显示歌词开关（默认勾选）
        self.show_lyric_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="显示歌词", variable=self.show_lyric_var,
                        command=self._on_toggle_lyric).pack(anchor=tk.W, pady=(3, 1))

        # 播放进度条区域
        player_box = ttk.LabelFrame(right, text="播放", padding=8)
        player_box.pack(fill=tk.X, pady=(4, 6))
        # 歌曲名
        self.player_title_var = tk.StringVar(value="未播放")
        self.player_title_label = ttk.Label(
            player_box, textvariable=self.player_title_var,
            anchor=tk.W, wraplength=160)
        self.player_title_label.pack(fill=tk.X)
        # 进度条
        self.progress_var = tk.DoubleVar(value=0)
        self.progress = ttk.Progressbar(
            player_box, variable=self.progress_var, maximum=100,
            mode="determinate"
        )
        self.progress.pack(fill=tk.X, pady=(4, 2))
        # 点击/拖动进度条跳转
        self.progress.bind("<Button-1>", self._on_progress_press)
        self.progress.bind("<B1-Motion>", self._on_progress_drag)
        # 时间显示
        self.time_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(player_box, textvariable=self.time_var,
                  anchor=tk.W).pack(fill=tk.X)

        ttk.Button(right, text="⬇ 下载选中", command=self.download_selected).pack(fill=tk.X, pady=3)

        self.lyric_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="下载歌词(默认)", variable=self.lyric_var).pack(
            anchor=tk.W, pady=3)

        # 浏览按钮放在"保存目录:"文字右边
        btn_row = ttk.Frame(right)
        btn_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(btn_row, text="保存目录：").pack(side=tk.LEFT)
        ttk.Button(btn_row, text="浏览...", command=self.choose_dir, width=7).pack(
            side=tk.LEFT, padx=(4, 0))
        # 下拉框单独一行：可手动输入，也可从历史路径中选择
        dir_row = ttk.Frame(right)
        dir_row.pack(fill=tk.X, pady=2)
        self.dir_var = tk.StringVar(value=self.save_dir)
        self.dir_combo = ttk.Combobox(
            dir_row, textvariable=self.dir_var,
            values=self.dir_history, state="normal"
        )
        self.dir_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # 选择历史路径后立即生效并持久化
        self.dir_combo.bind("<<ComboboxSelected>>", lambda e: self._on_dir_changed())
        self.dir_combo.bind("<FocusOut>", lambda e: self._on_dir_changed())

        info = ttk.LabelFrame(right, text="音质策略", padding=8)
        info.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(info, text="下载优先极高 320K\n无 320K 则普通 128K",
                  justify=tk.LEFT, foreground="#555").pack(anchor=tk.W)

        # 锁定右操作面板宽度：以初次构建时的自然宽度为准（此时标题为占位短文本"未播放"）。
        # 之后播放长歌名也不会把面板撑宽、挤小左侧列表框；标题超宽时自动换行而非撑大。
        right.update_idletasks()
        _right_w = right.winfo_reqwidth()
        right.config(width=_right_w)
        right.pack_propagate(False)
        self.player_title_label.config(wraplength=max(60, _right_w - 24))

        # 底部：状态栏
        self.status_var = tk.StringVar(value="就绪。输入歌手名或歌曲名后点击搜索。")
        ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN,
                  anchor=tk.W, padding=(8, 4)).pack(fill=tk.X, side=tk.BOTTOM)

        # 首次绘制歌词占位（画布布局完成后 <Configure> 会再次触发）
        self._render_lyrics()

    # ------------------------------------------------------------------
    # 播放进度条
    # ------------------------------------------------------------------
    def _fmt_time(self, seconds):
        """秒数格式化为 mm:ss"""
        seconds = max(0, int(seconds or 0))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def _on_progress_press(self, event):
        """按下进度条：跳转到对应位置"""
        self._seek_at(event.x)

    def _on_progress_drag(self, event):
        """拖动进度条：实时跳转"""
        self._seek_at(event.x)

    def _seek_at(self, x):
        """根据进度条上的 x 坐标跳转（播放中或暂停时均可）"""
        if not self.player.is_active():
            return
        bar = self.progress
        total = bar.winfo_width()
        if total <= 0:
            return
        ratio = max(0.0, min(1.0, x / total))
        length = self.player.get_length()
        if length > 0:
            self.player.seek(ratio * length)

    def _poll_progress(self):
        """轮询更新进度条和时间显示（每 500ms）"""
        if self.player.is_active():
            pos = self.player.get_position()
            length = self.player.get_length()
            if length > 0:
                self.progress_var.set(pos * 100 / length)
            self.time_var.set(f"{self._fmt_time(pos)} / {self._fmt_time(length)}")
            # 同步歌词高亮当前行
            self._update_lyric(pos)
            # 更新暂停按钮文字
            if self.player.is_paused:
                self.btn_pause.config(text="▶ 继续")
            elif self.player.is_playing():
                self.btn_pause.config(text="⏸ 暂停")
            self.btn_pause.config(state=tk.NORMAL)
        else:
            self.btn_pause.config(state=tk.DISABLED)
            if not self.player.is_paused:
                self.btn_pause.config(text="⏸ 暂停")
        self.root.after(500, self._poll_progress)

    def toggle_pause(self):
        """暂停/继续切换"""
        if self.player.is_paused:
            self.player.unpause()
        elif self.player.is_playing():
            self.player.pause()

    def _reset_progress(self):
        """重置进度条显示"""
        self.progress_var.set(0)
        self.time_var.set("00:00 / 00:00")

    # ------------------------------------------------------------------
    # 歌词窗（Canvas 重绘：拖动横线同步 + 随时间高亮）
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_lrc(text):
        """解析 LRC 文本，返回 [(time_sec|None, text), ...]，按时间排序。
        无时间戳的行（如元信息）记为 None，仅作展示不参与高亮。
        自动应用 LRC 标准 [offset:xxx] 标签（毫秒，正值=歌词延后）。"""
        pat = re.compile(r"\[(\d{1,2}):(\d{1,2}(?:[.,]\d{1,3})?)\]")
        offset_ms = 0.0
        out = []
        for line in (text or "").splitlines():
            if not line.strip():
                continue
            m = re.match(r"\[offset:\s*(-?\d+(?:\.\d+)?)\s*\]", line.strip(), re.IGNORECASE)
            if m:
                offset_ms = float(m.group(1))
                continue
            stamps = pat.findall(line)
            if not stamps:
                out.append((None, line.strip()))
                continue
            txt = pat.sub("", line).strip()
            if not txt:
                continue
            for mm, ss in stamps:
                t = int(mm) * 60 + float(ss.replace(",", "."))
                out.append((t, txt))
        offset = offset_ms / 1000.0
        # 应用 offset：时间戳整体平移（不影响无时间戳行）
        out = [(t + offset if t is not None else None, txt) for (t, txt) in out]
        out.sort(key=lambda x: (x[0] if x[0] is not None else 1e9, x[1]))
        return out

    def _fetch_lyric(self, song, platform):
        """后台拉取歌词后回主线程渲染（带失败兜底）"""
        def _work():
            try:
                raw = self.client.get_lyric(song, platform)
            except Exception:
                raw = ""
            self._queue_task(
                self._set_lyric, raw,
                f"{song.get('name')} - {song.get('artist')}")
        threading.Thread(target=_work, daemon=True).start()

    def _set_lyric(self, raw, title):
        """将歌词解析为行列表并渲染（主线程调用）"""
        self.current_lyric_raw = raw
        self.current_lyric_title = title
        # 不在此重置手动偏移：同一首歌重新拉取时保留用户微调；
        # 换歌时的清零在 _do_play 里统一做（LRC [offset:] 已在解析时应用）
        self.lyric_lines = []
        self.lyric_active_idx = -1
        if not raw or not raw.strip():
            self.lyric_placeholder = "（暂无歌词）"
            self.lyric_scroll = 0.0
            self._render_lyrics()
            return
        parsed = self._parse_lrc(raw)
        if not parsed:
            self.lyric_placeholder = raw[:60]
            self.lyric_scroll = 0.0
            self._render_lyrics()
            return
        self.lyric_lines = [(t, text) for t, text in parsed]
        self.lyric_scroll = 0.0
        self.lyric_active_idx = -1
        if self.player.is_active():
            self._update_lyric(self.player.get_position())
        else:
            self._render_lyrics()

    def _clear_lyric(self):
        """清空歌词窗（停止/播放结束时调用）"""
        self.lyric_lines = []
        self.lyric_active_idx = -1
        self.lyric_placeholder = "（未播放）"
        self.lyric_scroll = 0.0
        self.lyric_offset_var.set(0.0)
        self._update_offset_label()
        self._lyric_anim_on = False
        self._render_lyrics()

    def _lyric_ref_y(self):
        """同步横线在画布中的 y 坐标（随画布高度自适应）"""
        h = self.lyric_canvas.winfo_height() or 200
        return max(40, h * 0.42)

    def _render_lyrics(self):
        """在画布上重绘歌词：当前行高亮，横线作为同步基准可拖动"""
        cv = self.lyric_canvas
        if cv is None:
            return
        cv.delete("all")
        w = cv.winfo_width() or 200
        h = cv.winfo_height() or 200
        if w <= 1 or h <= 1:
            return
        ref = self._lyric_ref_y()
        self.lyric_ref_y = ref
        # 干净白底
        cv.create_rectangle(0, 0, w, h, fill="#ffffff", outline="")
        # 空歌词占位
        if not self.lyric_lines:
            cv.create_text(w / 2, h / 2, text=self.lyric_placeholder,
                           fill="#bbbbbb", font=self.LYRIC_FONT, anchor=tk.CENTER)
            return
        active = self.lyric_active_idx
        # 当前行高亮背景带（淡红）
        if active >= 0:
            cv.create_rectangle(4, ref - self.lyric_line_h / 2,
                                w - 4, ref + self.lyric_line_h / 2,
                                fill="#fdecea", outline="")
        # 同步横线 + 左侧小三角把手
        cv.create_line(4, ref, w - 4, ref, fill="#e74c3c", width=2)
        cv.create_polygon(0, ref - 5, 0, ref + 5, 7, ref, fill="#e74c3c")
        # 逐行绘制（距离当前行越远越淡，形成聚焦感）
        for i, (t, text) in enumerate(self.lyric_lines):
            cy = i * self.lyric_line_h + self.lyric_line_h / 2 - self.lyric_scroll
            if cy < -self.lyric_line_h or cy > h + self.lyric_line_h:
                continue
            if i == active:
                color, font = "#e74c3c", self.LYRIC_FONT_ACTIVE
            else:
                d = abs(i - active) if active >= 0 else 2
                gray = max(120, 205 - (d - 1) * 32) if d >= 1 else 205
                color = "#%02x%02x%02x" % (gray, gray, gray)
                font = self.LYRIC_FONT
            disp = text if len(text) <= 18 else text[:17] + "…"
            cv.create_text(w / 2, cy, text=disp, fill=color,
                           font=font, anchor=tk.CENTER)

    def _update_lyric(self, pos):
        """根据播放位置高亮当前歌词行并自动滚动（每 500ms 调用）"""
        if not self.show_lyric_var.get() or not self.lyric_lines:
            return
        # 记住原始位置，供暂停/停止后拖动横线时即时预览
        self._last_lyric_pos = pos
        # 叠加用户手动偏移，补偿歌词与演唱的固有偏差
        pos += self.lyric_offset_var.get()
        active = -1
        for i, (t, text) in enumerate(self.lyric_lines):
            if t is None:
                continue
            if t <= pos:
                active = i
            else:
                break
        self.lyric_active_idx = active
        if active >= 0:
            self.lyric_target_scroll = active * self.lyric_line_h + \
                self.lyric_line_h / 2 - self._lyric_ref_y()
        if self.lyric_dragging:
            # 拖动中：不自动滚动，按当前拖动位置直接渲染
            self._render_lyrics()
            return
        self._render_lyrics()
        self._ensure_lyric_anim()

    def _ensure_lyric_anim(self):
        """确保歌词自动滚动动画在运行（仅播放/暂停且非拖动时）"""
        if not getattr(self, "_lyric_anim_on", False):
            self._lyric_anim_on = True
            self._lyric_anim_step()

    def _lyric_anim_step(self):
        """平滑地把歌词滚动到目标位置（当前行对齐横线）"""
        if not getattr(self, "_lyric_anim_on", False):
            return
        if self.lyric_dragging or self.lyric_active_idx < 0 or not self.player.is_active():
            self._lyric_anim_on = False
            return
        diff = self.lyric_target_scroll - self.lyric_scroll
        if abs(diff) < 0.5:
            self.lyric_scroll = self.lyric_target_scroll
            self._render_lyrics()
            self._lyric_anim_on = False
            return
        self.lyric_scroll += diff * 0.25
        self._render_lyrics()
        self.root.after(33, self._lyric_anim_step)

    def _on_lyric_press(self, event):
        """按下：进入拖动同步模式（暂停自动滚动）"""
        if not self.lyric_lines:
            return
        self.lyric_dragging = True
        self._drag_last_y = event.y
        self._lyric_anim_on = False

    def _on_lyric_drag(self, event):
        """拖动：歌词随鼠标上下移动；横线处的那一行即“正在唱”的行，
        据此反推偏移量并实时高亮。"""
        if not self.lyric_dragging or not self.lyric_lines:
            return
        dy = event.y - self._drag_last_y
        self._drag_last_y = event.y
        # 鼠标下移 -> 内容下移 -> scroll 减小
        self.lyric_scroll -= dy
        self._clamp_lyric_scroll()
        # 计算此刻落在横线处的歌词行索引
        content_y_at_ref = self.lyric_scroll + self.lyric_ref_y
        idx = int(round((content_y_at_ref - self.lyric_line_h / 2) / self.lyric_line_h))
        idx = max(0, min(len(self.lyric_lines) - 1, idx))
        # 若该行为无时间戳的元信息行，向外就近找一个有时间的行
        t_line = self.lyric_lines[idx][0]
        if t_line is None:
            for d in range(1, len(self.lyric_lines)):
                for j in (idx - d, idx + d):
                    if 0 <= j < len(self.lyric_lines) and \
                            self.lyric_lines[j][0] is not None:
                        idx, t_line = j, self.lyric_lines[j][0]
                        break
                else:
                    continue
                break
        # 播放/暂停中：把该行对齐到当前播放时间，算出偏移量
        if self.player.is_active() and t_line is not None:
            pos = self.player.get_position()
            self.lyric_offset_var.set(round(t_line - pos, 2))
            self._update_offset_label()
        self.lyric_active_idx = idx
        self._render_lyrics()

    def _on_lyric_release(self, event):
        """松开：退出拖动，恢复自动滚动到当前行"""
        if not self.lyric_dragging:
            return
        self.lyric_dragging = False
        self._ensure_lyric_anim()

    def _clamp_lyric_scroll(self):
        """限制 scroll，使横线始终落在某一行歌词上"""
        if not self.lyric_lines:
            self.lyric_scroll = 0.0
            return
        lo = self.lyric_line_h / 2 - self.lyric_ref_y
        hi = (len(self.lyric_lines) - 1) * self.lyric_line_h + \
            self.lyric_line_h / 2 - self.lyric_ref_y
        self.lyric_scroll = max(lo, min(hi, self.lyric_scroll))

    def _on_toggle_lyric(self):
        """勾选框切换：勾选则显示当前歌曲歌词，取消则清空"""
        if self.show_lyric_var.get():
            if self.current_lyric_raw is not None:
                self._set_lyric(self.current_lyric_raw, self.current_lyric_title)
            elif self.player.is_active() and self.current_song is not None:
                self._fetch_lyric(self.current_song, self.current_platform)
        else:
            self.lyric_lines = []
            self.lyric_active_idx = -1
            self.lyric_placeholder = "（已关闭）"
            self._lyric_anim_on = False
            self._render_lyrics()

    def _reset_lyric_offset(self):
        """把手动偏移归零，并按当前/上次播放位置重算高亮"""
        self.lyric_offset_var.set(0.0)
        self._update_offset_label()
        if self.player.is_active():
            self._update_lyric(self.player.get_position())
        elif self._last_lyric_pos is not None:
            self._update_lyric(self._last_lyric_pos)
        else:
            self._render_lyrics()

    def _update_offset_label(self):
        """刷新底部偏移读数"""
        v = self.lyric_offset_var.get()
        sign = "+" if v >= 0 else "-"
        self.lyric_offset_label.config(text=f"偏移 {sign}{abs(v):.1f}s")

    # ------------------------------------------------------------------
    # 后台任务队列（避免阻塞界面）
    # ------------------------------------------------------------------
    def _queue_task(self, fn, *args, **kwargs):
        with self.queue_lock:
            self.task_queue.append((fn, args, kwargs))

    def _poll_queue(self):
        with self.queue_lock:
            tasks = self.task_queue[:]
            self.task_queue.clear()
        for fn, args, kwargs in tasks:
            try:
                fn(*args, **kwargs)
            except Exception as e:
                self._set_status("出错: " + str(e))
        self.root.after(150, self._poll_queue)

    def _set_status(self, text):
        self.status_var.set(text)

    def _on_error(self, msg):
        self._set_status(msg)
        messagebox.showerror("错误", msg)

    # ------------------------------------------------------------------
    # 搜索与加载
    # ------------------------------------------------------------------
    def do_search(self):
        keyword = self.keyword_var.get().strip()
        platform = self.platform_var.get()
        if not keyword:
            messagebox.showwarning("提示", "请输入歌手名或歌曲名")
            return
        self.btn_search.config(state=tk.DISABLED)
        self._clear_tree()
        self._set_status(f"正在搜索「{keyword}」({self.PLATFORM_LABEL.get(platform, platform)})...")

        def _work():
            try:
                items, total = self.client.search(keyword, platform, 1)
                with self.songs_lock:
                    self.songs = list(items)
                    self.fetch_stop.clear()
                self._add_rows(items, base_index=0)
                self._set_status(
                    f"搜索完成，共 {total} 首，已显示 {len(items)} 首。"
                    f"点击「加载全部」获取完整列表。")
            except Exception as e:
                self._queue_task(self._on_error, "搜索失败: " + str(e))
            finally:
                self._queue_task(self.btn_search.config, state=tk.NORMAL)
        threading.Thread(target=_work, daemon=True).start()

    def load_all_pages(self):
        """自动翻页加载全部结果"""
        keyword = self.keyword_var.get().strip()
        platform = self.platform_var.get()
        if not keyword:
            return
        if not self.songs:
            self.do_search()
            return
        self.btn_load_all.config(state=tk.DISABLED)
        self.fetch_stop.clear()
        self._set_status("正在加载全部结果（自动翻页）...")

        def _work():
            try:
                page = 2
                while not self.fetch_stop.is_set():
                    items, total = self.client.search(keyword, platform, page)
                    if not items:
                        break
                    with self.songs_lock:
                        base = len(self.songs)
                        self.songs.extend(items)
                    self._add_rows(items, base_index=base)
                    self._set_status(
                        f"加载中：第 {page} 页，已加载 {len(self.songs)} / 约 {total} 首")
                    page += 1
                    if page > MAX_PAGES:
                        break
                    # 翻页间隔，降低限流风险
                    time.sleep(1.5)
                self._set_status(f"全部加载完成，共 {len(self.songs)} 首。")
            except Exception as e:
                self._queue_task(self._on_error, "加载失败: " + str(e))
            finally:
                self._queue_task(self.btn_load_all.config, state=tk.NORMAL)
        threading.Thread(target=_work, daemon=True).start()

    def _clear_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.row_map = []

    def _add_rows(self, items, base_index):
        """插入一批行到树视图。base_index 为这批歌曲在 songs 中的起始索引。
        注意：tkinter 非线程安全，通过队列在主线程执行"""
        rows_data = []
        for i, song in enumerate(items):
            q = pick_best_quality(song.get("minfo") or [])
            if q:
                quality = f"{q['format'].upper()} {q.get('bitrate')}K"
            else:
                quality = "无MP3"
            rows_data.append((song, quality, base_index + i))
        self._queue_task(self._insert_rows, rows_data)

    def _insert_rows(self, rows_data):
        """在主线程中执行行插入"""
        for song, quality, song_idx in rows_data:
            tag = "alt" if len(self.tree.get_children()) % 2 else "normal"
            row_id = self.tree.insert(
                "", tk.END,
                values=(len(self.tree.get_children()) + 1, song.get("name", "?"),
                        song.get("artist", ""), song.get("album_name", ""), quality),
                tags=(tag,),
            )
            self.row_map.append((row_id, song_idx))

    # ------------------------------------------------------------------
    # 播放
    # ------------------------------------------------------------------
    def _song_of_row(self, row_id):
        for rid, idx in self.row_map:
            if rid == row_id:
                with self.songs_lock:
                    if idx < len(self.songs):
                        return self.songs[idx]
                return None
        return None

    def play_selected(self):
        sel = self.tree.selection()
        if not sel:
            self._set_status("请先在列表中选择一首歌。")
            return
        song = self._song_of_row(sel[0])
        if not song:
            return
        self._set_status(f"正在获取播放地址: {song.get('name')} - {song.get('artist')}")
        platform = self.platform_var.get()

        def _work():
            try:
                q = pick_best_quality(song.get("minfo") or [])
                if not q:
                    raise RuntimeError("该歌曲无可用音质")
                url = self.client.get_url(song, q["format"], q.get("bitrate"), platform)
                self._queue_task(self._do_play, url, song)
            except Exception as e:
                self._queue_task(self._on_error, "播放失败: " + str(e))
        threading.Thread(target=_work, daemon=True).start()

    def _do_play(self, url, song):
        self.player.stop()
        mode = "内嵌播放" if self.player.using_pygame else "系统播放器"
        self._set_status(f"准备播放: {song.get('name')} - {song.get('artist')} [{mode}]")

        # 记录当前歌曲，供歌词显示与勾选切换使用
        self.current_song = song
        self.current_platform = self.platform_var.get()
        self.current_lyric_raw = None
        # 换歌：清零上一首遗留的手动偏移，底部读数同步更新
        self.lyric_offset_var.set(0.0)
        self._update_offset_label()

        def _status(text):
            self._queue_task(self._set_status, text)

        def _on_done():
            # 播放自然结束：重置界面状态
            self._queue_task(self._set_status, "播放结束")
            self._queue_task(self._reset_progress)
            self._queue_task(self.player_title_var.set, "未播放")
            self._queue_task(self.btn_pause.config, text="⏸ 暂停", state=tk.DISABLED)
            self._queue_task(self._clear_lyric)

        # 显示歌曲名并启动进度轮询
        self.player_title_var.set(f"{song.get('name')} - {song.get('artist')}")
        self._reset_progress()
        self.btn_pause.config(text="⏸ 暂停", state=tk.NORMAL)

        # 加载歌词（默认勾选则自动获取并显示，随进度高亮）
        if self.show_lyric_var.get():
            self._fetch_lyric(song, self.current_platform)

        self.player.play(
            url, song.get("name", ""),
            duration=song.get("duration"),
            on_done=_on_done,
            status_cb=_status,
        )

    def stop_play(self):
        self.player.stop()
        self.player_title_var.set("未播放")
        self._reset_progress()
        self.btn_pause.config(text="⏸ 暂停", state=tk.DISABLED)
        self._set_status("已停止播放。")
        self._clear_lyric()

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    def choose_dir(self):
        """浏览选择保存目录：弹窗选择后加入历史并持久化"""
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or os.path.expanduser("~"))
        if d:
            self.dir_var.set(d)
            self._on_dir_changed()

    def _on_dir_changed(self):
        """保存目录变化：更新历史记录并持久化到配置文件"""
        path = self.dir_var.get().strip()
        if not path:
            return
        # 去重并置顶，最多保留10条
        self.dir_history = [p for p in self.dir_history if p != path]
        self.dir_history.insert(0, path)
        self.dir_history = self.dir_history[:10]
        # 更新下拉框选项（保留当前输入值）
        if hasattr(self, "dir_combo"):
            self.dir_combo.configure(values=self.dir_history)
        # 持久化
        save_config({"save_dir": path, "save_dir_history": self.dir_history})

    def download_selected(self):
        sel = self.tree.selection()
        if not sel:
            self._set_status("请先在列表中选择要下载的歌曲。")
            return
        song_list = [self._song_of_row(r) for r in sel]
        song_list = [s for s in song_list if s]
        if not song_list:
            return

        save_dir = self.dir_var.get().strip()
        if not save_dir:
            messagebox.showwarning("提示", "请先设置保存目录。")
            return
        if not os.path.isdir(save_dir):
            try:
                os.makedirs(save_dir, exist_ok=True)
            except OSError as e:
                messagebox.showerror("错误", "无法创建目录: " + str(e))
                return

        want_lyric = self.lyric_var.get()
        platform = self.platform_var.get()
        total = len(song_list)
        self._set_status(f"准备下载 {total} 首到 {save_dir}")

        def _work():
            for i, song in enumerate(song_list, 1):
                q = pick_best_quality(song.get("minfo") or [])
                if not q:
                    self._queue_task(self._set_status,
                                     f"[{i}/{total}] {song.get('name')} 无可用音质，跳过")
                    continue
                try:
                    self._queue_task(self._set_status,
                                     f"[{i}/{total}] 获取地址: {song.get('name')}")
                    url = self.client.get_url(song, q["format"], q.get("bitrate"), platform)
                    filename = safe_song_name(song) + "." + q["format"]

                    def log(text):
                        self._queue_task(self._set_status, f"[{i}/{total}] {text}")

                    download_file(url, save_dir, filename, log_cb=log)
                    result = f"[{i}/{total}] 完成: {filename}"

                    # 歌词
                    if want_lyric:
                        try:
                            lyric = self.client.get_lyric(song, platform)
                            if lyric:
                                lrc_name = safe_song_name(song) + ".lrc"
                                with open(os.path.join(save_dir, lrc_name), "w",
                                          encoding="utf-8") as f:
                                    f.write(lyric)
                                result += f" + {lrc_name}"
                            else:
                                result += "（无歌词）"
                        except Exception as le:
                            result += f"（歌词失败: {le}）"
                    self._queue_task(self._set_status, result)
                except Exception as e:
                    self._queue_task(self._set_status,
                                     f"[{i}/{total}] 失败 {song.get('name')}: {e}")
            self._queue_task(self._set_status, f"全部下载任务结束，共 {total} 首。")
        threading.Thread(target=_work, daemon=True).start()

    def run(self):
        # 窗口关闭时清理临时文件
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        try:
            self.player.stop()
            self.player.cleanup()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    app = MusicApp()
    app.run()
