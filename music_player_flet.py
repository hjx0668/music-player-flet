# -*- coding: utf-8 -*-
"""
音乐搜索播放器（Flet 版 —— 手机 / 车机 APK 通用）
=====================================================
功能：
1. 搜索歌曲（酷我 / 网易云双平台，可"加载全部"自动翻页）
2. 三种播放方式：
   - ▶ 从第一首开始顺序播放
   - 🔀 随机播放
   - 点击列表中任意一首播放（播完后从下一首继续顺序连播）
3. 播放完当前歌曲后自动跳到下一首（顺序模式顺延 / 随机模式随机换曲）
4. 暂停/继续、停止、进度条拖动跳转
5. 某首歌取地址失败时自动跳过，继续下一首

运行（桌面/手机调试）：
    pip install "flet>=0.24,<0.29" flet-audio==0.1.0
    python music_player_flet.py

打包 APK（手机/车机安装）：
    flet build apk --split-per-abi --android-permissions android.permission.INTERNET=True
    生成 build/ 下的 apk 拷到手机或车机安装即可（车机需为 Android 系统）

注意：audio 控件自 flet 0.26 起拆到 flet-audio 包，移动端必须安装该包，
否则界面会报 "Unknown control: audio" 且无声。

说明：播放直接使用 Audio 组件流式播放网络地址，无需下载整首到本地。
"""

import json
import random
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

import flet as ft

# 兼容不同版本 flet：Audio 组件可能在 flet_audio 包中
try:
    from flet_audio import Audio as _AudioControl  # type: ignore
except ImportError:
    _AudioControl = getattr(ft, "Audio", None)
if _AudioControl is None:
    print("错误：未找到 Audio 播放组件。请安装 flet：pip install \"flet>=0.24,<0.29\"")
    sys.exit(1)

# ======================================================================
# 配置
# ======================================================================
BASE_URL = "https://flac.music.hi.cn"
API_URL = BASE_URL + "/ajax.php"
PAGE_SIZE = 20          # 网站每页大小
MAX_PAGES = 50          # 安全上限，防止无限翻页
REQ_TIMEOUT = 30        # 请求超时（秒）

# ======================================================================
# 主题（浅色明亮风格）
# 所有颜色/不透明度集中定义，界面代码里不再散落硬编码色值
# ======================================================================
C_BG = "#F7F9FC"            # 页面底色：极浅灰蓝
C_SURFACE = "#FFFFFF"       # 卡片/播放条/列表行底色：白色
C_SURFACE_ALT = "#F0F3F7"   # 次级表面：按钮、滑块轨道底
C_ACCENT = "#1E88E5"        # 强调色（清爽蓝）
C_TEXT = "#1F2430"          # 一级文字（主，深灰近黑）
C_TEXT_2 = "#5A6472"        # 二级文字（次，中灰）
C_TEXT_3 = "#9AA4B2"        # 三级文字（弱提示，浅灰）
C_DIVIDER = "#E3E8EF"       # 分隔线 / 滑块非活动轨道
# 歌词面板（白色 88% 半透明 + 深灰字，保证浅色主题下的对比度）
C_LYRIC_BG = "#FFFFFF"
C_LYRIC_BG_OPACITY = 0.88   # 面板不透明度 88%
C_LYRIC_CUR = C_ACCENT      # 当前行：强调蓝加粗放大
C_LYRIC_NEAR = "#1F2430"    # 相邻行：70% 深灰
C_LYRIC_FAR = "#1F2430"     # 其余行：40% 深灰
C_LYRIC_MSG = "#1F2430"     # 加载中/暂无歌词


def _op(color, alpha):
    """颜色 + 不透明度的简写（统一走 ft.Colors.with_opacity）"""
    return ft.Colors.with_opacity(alpha, color)


def _btn_style(primary=False):
    """统一按钮样式：主按钮强调蓝底白字，次按钮浅灰底 + 强调色文字/图标"""
    return ft.ButtonStyle(
        color="#FFFFFF" if primary else C_ACCENT,
        bgcolor=C_ACCENT if primary else C_SURFACE_ALT,
        icon_color="#FFFFFF" if primary else C_ACCENT,
        overlay_color=_op("#000000", 0.06),
        elevation=0,
        padding=ft.padding.symmetric(horizontal=12, vertical=10),
        shape=ft.RoundedRectangleBorder(radius=10),
        text_style=ft.TextStyle(size=14, weight=ft.FontWeight.W_500),
    )


def pick_best_quality(minfo):
    """从 minfo 列表中挑选最佳音质（极高320K -> 普通128K）。返回 dict 或 None"""
    for m in minfo:
        if m.get("format") == "mp3" and str(m.get("bitrate")) == "320":
            return m
    for m in minfo:
        if m.get("format") == "mp3":
            return m
    return None


def _evt_ms(value):
    """把事件里的时间值统一转成毫秒。
    兼容 Duration 对象 / timedelta / 数字 / 字符串 等不同 flet 版本的格式"""
    if value is None:
        return 0
    ims = getattr(value, "in_milliseconds", None)
    if callable(ims):
        try:
            return int(ims())
        except Exception:
            pass
    tsec = getattr(value, "total_seconds", None)
    if callable(tsec):
        try:
            return int(tsec() * 1000)
        except Exception:
            pass
    try:
        s = str(value).strip().split(",")[0]
        return int(float(s))
    except Exception:
        return 0


def fmt_time(seconds):
    """秒数格式化为 mm:ss"""
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# ======================================================================
# HTTP 客户端（与桌面版逻辑一致，仅保留搜索/取播放地址）
# ======================================================================
class ApiClient:
    """封装对 ajax.php 的请求，带 Cookie 保持与失败重试"""

    def __init__(self):
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"),
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
        """POST ajax.php?act=xxx，失败自动重试（指数退避）"""
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
                backoff = 2 ** attempt + random.uniform(0, 0.5)
                time.sleep(backoff)
        raise RuntimeError(f"请求失败（重试{retries}次）: {last_err}")

    def search(self, keyword, platform, page, size=PAGE_SIZE):
        """搜索，返回 (list, total)。限流时接口返回 code=0 但 list 为空，需检测重试"""
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
        """获取播放地址。song 为搜索结果单条 dict。失败自动重试"""
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
        """获取歌词（LRC 文本，带时间轴）。无歌词/失败返回空字符串，不抛异常"""
        for attempt in range(retries):
            try:
                params = {
                    "platform": platform,
                    "songid": song["id"],
                    "time": song.get("time"),
                    "sign": song.get("sign"),
                }
                print(f"[lyric] GET request attempt {attempt + 1} params={params}")
                result = self._post("getLyric", params)
                print(f"[lyric] return code={result.get('code')} "
                      f"msg={result.get('msg', '')!r} "
                      f"data_len={len(result.get('data') or '')}")
                if result.get("code") == 0:
                    data = result.get("data")
                    return data if isinstance(data, str) else ""
            except Exception as ex:
                print(f"[lyric] request exception: {ex!r}")
            time.sleep(1.5)
        return ""


# ======================================================================
# 主程序（GUI 类，与网络逻辑类分离）
# ======================================================================
class MusicApp:
    PLATFORM_LABEL = {"kuwo": "酷我", "wyy": "网易云"}

    def __init__(self, page: ft.Page):
        self.page = page
        self.client = ApiClient()

        # ---- 播放状态 ----
        self.songs = []                    # 全部搜索结果
        self.songs_lock = threading.Lock()
        self.current_index = None          # 当前播放的歌曲索引
        self.current_song = None           # 当前播放的歌曲 dict（歌词拉取用）
        self.play_mode = "seq"             # "seq" 顺序 / "random" 随机
        self.search_platform = "kuwo"      # 当前列表对应的平台（酷我取地址可用；wyy 解析在网站端故障）
        self._fetch_stop = threading.Event()
        self._manual_stop = False          # 手动停止后忽略 on_ended
        self._paused_flag = False
        self._seeking = False              # 用户正在拖进度条
        self._duration_ms = 0
        self._fail_streak = 0              # 连续取地址失败计数（防死循环连播）
        self._want_play = False            # 待播放标志：媒体加载完成(on_loaded)后真正 play
        self._loaded_flag = False          # 客户端是否已回报 on_loaded（兜底计时判断用）
        self._play_seq = 0                 # 播放序号：切歌后让旧的兜底计时失效
        self.volume = 80                   # 应用内音量 0-100（持久化到 client_storage）
        self.muted = False                 # 静音标志（跨歌曲、跨启动保持）
        self._pre_mute_volume = 80         # 静音前的音量，恢复用
        self._mode = None                  # 布局模式：car/compact/extreme/normal/desktop_small
        self.row_controls = []             # 与 songs 一一对应的列表行控件
        self.row_bars = []                 # 与 row_controls 一一对应的"正在播放"左侧强调条
        self._seek_target = 0              # 进度条拖动目标（秒）
        self._seek_seq = 0                 # 进度条拖动序号：让过期的兜底 seek 失效

        # ---- 歌词状态 ----
        self.show_lyric = True             # 歌词开关（默认勾选，持久化）
        self._lyric_lines = []             # [(sec, text), ...] 按时间升序
        self._lyric_active = -1            # 当前行索引
        self._lyric_cache = {}             # (platform, songid) -> LRC 文本或 ""
        self._lyric_song_key = None        # 当前歌词对应的 (platform, songid)

        page.title = "音乐播放器 - flac.music.hi.cn"
        page.vertical_alignment = ft.MainAxisAlignment.START
        # 统一浅色主题：底色、表面、文字三级色全部走主题常量
        page.theme_mode = ft.ThemeMode.LIGHT
        page.bgcolor = C_BG
        page.padding = 12
        page.spacing = 8
        page.on_resize = self._on_page_resize

        self._build_ui()

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_ui(self):
        page = self.page

        # Audio 播放组件：首次播放时才创建（带真实 URL），
        # 因为 flet 客户端要求 Audio 加入页面时 src 不能为空。
        # 注意：flet 0.28 的 Audio 没有 on_ended 事件，
        # 播放结束需在 on_state_changed 中检测 completed 状态
        self.audio = None

        # ===== 搜索区（下拉 96 + 输入框自适应 + 搜索按钮固定宽，栅格整齐）=====
        self.platform_dd = ft.Dropdown(
            width=96,
            value="kuwo",  # 默认酷我：酷我取播放地址可用；wyy 的解析后端在网站端故障
            options=[
                ft.dropdown.Option("kuwo", "酷我"),
                ft.dropdown.Option("wyy", "网易云"),
            ],
            text_size=14,
            bgcolor=C_SURFACE,
            border_color=C_DIVIDER,
            focused_border_color=C_ACCENT,
            color=C_TEXT,
            border_radius=10,
        )
        self.keyword = ft.TextField(
            hint_text="歌手 / 歌曲名",
            expand=True,
            on_submit=self.do_search,
            text_size=14,
            bgcolor=C_SURFACE,
            border_color=C_DIVIDER,
            focused_border_color=C_ACCENT,
            color=C_TEXT,
            cursor_color=C_ACCENT,
            hint_style=ft.TextStyle(color=C_TEXT_3, size=14),
            content_padding=ft.padding.symmetric(horizontal=12, vertical=12),
            border_radius=10,
        )
        self.search_btn = ft.ElevatedButton(
            "搜索", width=72, on_click=self.do_search, style=_btn_style(primary=True))
        self.load_all_btn = ft.ElevatedButton(
            "加载全部", on_click=self.load_all_pages, style=_btn_style())

        search_row = ft.Row(
            [self.platform_dd, self.keyword, self.search_btn],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # ===== 播放控制区（同一行四个等距按钮；短标签保证小屏不换行）=====
        self.play_first_btn = ft.ElevatedButton(
            "播放", icon=ft.Icons.PLAY_ARROW, tooltip="从第一首开始顺序播放",
            on_click=self.on_play_from_first, style=_btn_style())
        self.random_btn = ft.ElevatedButton(
            "随机", icon=ft.Icons.SHUFFLE, tooltip="随机播放",
            on_click=self.on_play_random, style=_btn_style())
        self.stop_btn = ft.ElevatedButton(
            "停止", icon=ft.Icons.STOP, tooltip="停止播放",
            on_click=self.on_stop, style=_btn_style())
        self.pause_btn = ft.ElevatedButton(
            "暂停", icon=ft.Icons.PAUSE, on_click=self.on_pause_toggle,
            style=_btn_style())
        # 统一收集按钮，便于按设备模式调整高度
        self._buttons = [self.search_btn, self.load_all_btn, self.pause_btn,
                         self.play_first_btn, self.random_btn, self.stop_btn]
        # 歌词开关（默认勾选，状态持久化见 _load_prefs/_persist_lyric_pref）
        self.lyric_check = ft.Checkbox(
            label="歌词", value=True, on_change=self.on_lyric_toggle,
            label_style=ft.TextStyle(color=C_TEXT, size=14),
            active_color=C_ACCENT,
            check_color="#FFFFFF",
            shape=ft.RoundedRectangleBorder(radius=4),
            visual_density=ft.VisualDensity.COMPACT,
        )
        actions_row = ft.Row(
            [self.play_first_btn, self.random_btn, self.stop_btn, self.pause_btn],
            spacing=8,
        )
        # 次行：加载全部（左）+ 歌词开关（右），用弹性占位让两端对齐
        secondary_row = ft.Row(
            [self.load_all_btn, ft.Container(expand=True), self.lyric_check],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # ===== 歌曲列表 =====
        self.list_view = ft.ListView(
            expand=True,
            spacing=0,
            padding=ft.padding.symmetric(horizontal=2, vertical=4),
            auto_scroll=False,
            divider_thickness=1,   # 行间细分隔线（浅灰）
        )

        # ===== 底部播放条（标题/歌手两级 + 等宽时间靠右）=====
        self.title_text = ft.Text(
            "未播放", size=16, weight=ft.FontWeight.BOLD, color=C_TEXT,
            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
            expand=True,  # 长歌名省略号截断，不把时间挤出屏
        )
        self.artist_text = ft.Text(
            "选择歌曲后开始播放", size=12, color=C_TEXT_2,
            max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True,
        )
        self.time_text = ft.Text(
            "00:00 / 00:00", size=12, color=C_TEXT_2, width=104,
            text_align=ft.TextAlign.RIGHT, font_family="monospace",
        )
        self.slider = ft.Slider(
            min=0, max=100, value=0, expand=True,
            active_color=C_ACCENT, inactive_color=C_DIVIDER, thumb_color=C_ACCENT,
            overlay_color=_op(C_ACCENT, 0.15),
            on_change=self._on_slider_change,
            on_change_start=self._on_slider_start,
            on_change_end=self._on_slider_end,
        )

        # ---- 音量区（应用内增益；系统音量键不受影响）----
        self.mute_btn = ft.IconButton(
            icon=ft.Icons.VOLUME_UP,
            icon_size=24,
            icon_color=C_TEXT_2,
            tooltip="静音 / 恢复",
            on_click=self.on_mute_toggle,
        )
        self.vol_slider = ft.Slider(
            min=0, max=100, value=self.volume, expand=True, label="{value}%",
            active_color=C_ACCENT, inactive_color=C_DIVIDER, thumb_color=C_ACCENT,
            overlay_color=_op(C_ACCENT, 0.15),
            on_change=self.on_volume_change,
            on_change_end=self.on_volume_commit,
        )
        # 触屏友好：加大上下留白扩大点击/拖动区域
        self.vol_area = ft.Container(
            content=self.vol_slider,
            padding=ft.padding.symmetric(vertical=6),
            expand=True,
        )
        self.vol_label = ft.Text(
            f"{self.volume}%", size=11, color=C_TEXT_3,
            tooltip="应用内音量（不影响手机/车机系统音量键）")

        # 行1 标题+歌手 / 行2 进度条 / 行3 静音+音量滑块+百分比
        player_bar = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [ft.Column([self.title_text, self.artist_text],
                                   spacing=1, tight=True, expand=True),
                         self.time_text],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.slider,
                    ft.Row(
                        [self.mute_btn, self.vol_area, self.vol_label],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=2,
                tight=True,
            ),
            padding=ft.padding.symmetric(horizontal=12, vertical=10),
            bgcolor=C_SURFACE,
            border_radius=14,
            border=ft.border.all(1, C_DIVIDER),
        )
        self.player_bar = player_bar

        # ===== 状态栏 =====
        self.status_text = ft.Text(
            "就绪。输入歌手名或歌曲名后点击搜索。",
            size=12,
            color=C_TEXT_2,
        )

        page.add(
            search_row,
            actions_row,
            secondary_row,
            self.list_view,
            player_bar,
            self.status_text,
        )

        # ===== 歌词面板（深色半透明；整块面板都是拖动热区）=====
        # 结构：GestureDetector(负责 left/top 定位与手势) → Container(视觉层)
        # 触摸面板任意位置都能拖动，不再只有顶部把手能拖（Android 触屏根因）
        self._lyric_drag = {"on": False, "dx": 0, "dy": 0}
        self.lyric_col = ft.Column(
            spacing=4, tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER)
        self.lyric_box = ft.Container(
            width=400,
            height=172,
            border_radius=14,
            padding=ft.padding.symmetric(horizontal=16, vertical=12),
            bgcolor=_op(C_LYRIC_BG, C_LYRIC_BG_OPACITY),
            border=ft.border.all(1, _op(C_TEXT, 0.10)),
            shadow=ft.BoxShadow(blur_radius=16, color=_op(C_TEXT, 0.18),
                                offset=ft.Offset(0, 6)),
            content=ft.Column(
                [
                    # 顶部小横条：视觉提示"可拖动"
                    ft.Container(width=44, height=4, border_radius=2,
                                 bgcolor=_op(C_TEXT, 0.22)),
                    ft.Container(content=self.lyric_col, expand=True,
                                 alignment=ft.alignment.center),
                ],
                spacing=8,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        self.lyric_panel = ft.GestureDetector(
            left=24,
            top=400,
            drag_interval=4,
            mouse_cursor=ft.MouseCursor.MOVE,
            on_pan_start=self._on_lyric_pan_start,
            on_pan_update=self._on_lyric_pan_update,
            on_pan_end=self._on_lyric_pan_end,
            on_scale_start=self._on_lyric_pan_start,
            on_scale_update=self._on_lyric_scale_update,
            on_scale_end=self._on_lyric_pan_end,
            content=self.lyric_box,
            visible=self.show_lyric,
        )
        page.overlay.append(self.lyric_panel)

        # 启动时异步读取上次保存的音量/静音设置
        self._load_prefs()
        # 首次按当前窗口尺寸套用布局模式（含歌词面板尺寸/位置）
        self._apply_layout_mode()

    # ------------------------------------------------------------------
    # 自适应布局
    # ------------------------------------------------------------------
    def _on_page_resize(self, e=None):
        self._apply_layout_mode()

    def _detect_mode(self):
        """桌面平台一律 normal（字号/行高不动）；移动端按逻辑像素分档：
        - extreme: 高度<520（手机横屏 873x393、超矮车机）——保列表高度优先
        - car:     宽>=1000 且 高>=550（车机 1280x800 / 1024x600 横屏）——大字号大热区
        - compact: 宽<600（手机竖屏 393x873）——标准字号+48 热区
        - normal:  其余移动端尺寸
        另有 desktop_small：桌面窗口过小（高<520）时只收紧边距/隐藏次要标签，
        不动字号行高——与移动端字号档位是两回事。
        平台检测失败（None/未知）一律按桌面 normal 处理，不误判成车机。"""
        try:
            p = getattr(self.page, "platform", None)
            plat = p.value if hasattr(p, "value") else str(p or "")
        except Exception:
            plat = ""  # 枚举构造失败（属性为 None）→ 视为未知平台
        is_mobile = plat in ("android", "ios", "android_tv")
        w = self.page.width or 0
        h = self.page.height or 0
        if not is_mobile:
            # 桌面/未知平台：小窗只做空间收缩，字号档位保持 normal
            if w > 0 and h > 0 and h < 520:
                return "desktop_small"
            return "normal"
        if w <= 0 or h <= 0:
            return "compact"  # 移动端尺寸未知，先按手机竖屏处理
        if h < 520:
            return "extreme"
        if w >= 1000 and h >= 550:
            return "car"
        if w < 600:
            return "compact"
        return "normal"

    def _apply_layout_mode(self):
        """按设备模式套用 padding/字号/按钮高度，并重建列表行。
        桌面档（normal/desktop_small）不使用大字号；列表行重建仅发生在
        移动端 car 档进出的场合（行高/字号变化才需要重建）。"""
        page = self.page
        mode = self._detect_mode()
        if mode == self._mode:
            return
        prev = self._mode
        self._mode = mode

        # 页面/播放条内边距
        page.padding = {"car": 16, "compact": 8, "extreme": 8,
                        "desktop_small": 8}.get(mode, 12)
        if self.player_bar is not None:
            self.player_bar.padding = {"car": 12, "compact": 8, "extreme": 6,
                                       "desktop_small": 8}.get(mode, 10)

        # 按钮热区：车机 52，其余 46（小屏省纵向空间，热区仍足够）
        for b in self._buttons:
            b.height = 52 if mode == "car" else 46

        # 字号：只有移动端 car 档加大（桌面任何窗口尺寸都不动字号）
        car = mode == "car"
        self.title_text.size = 20 if car else 16
        self.artist_text.size = 15 if car else 12
        self.time_text.size = 15 if car else 12
        self.time_text.width = 122 if car else 104
        self.mute_btn.icon_size = 30 if car else 24
        self.keyword.text_size = 16 if car else 14
        self.platform_dd.width = 110 if car else 96
        self.platform_dd.text_size = 16 if car else 14
        self.search_btn.width = 88 if car else 72

        # 次要元素：极端矮屏与桌面小窗隐藏，保列表高度
        hide_extras = mode in ("extreme", "desktop_small")
        self.vol_label.visible = not hide_extras
        self.status_text.visible = not hide_extras

        # 歌词面板随模式调整尺寸并重置到播放条上方
        try:
            self._position_lyric_panel(reset=True)
        except Exception:
            pass

        try:
            page.update()
        except Exception:
            pass
        # 列表行高/字号只在移动端 car 档进出时变化 → 重建已显示的行
        if prev is not None and (prev == "car") != car:
            try:
                with self.songs_lock:
                    has_songs = bool(self.songs)
                if has_songs:
                    self._rebuild_list()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 音量控制（应用内增益，持久化优先 client_storage，手机/车机可用）
    # ------------------------------------------------------------------
    def _effective_volume(self):
        """当前实际音量（0.0-1.0）：静音时为 0"""
        return 0.0 if self.muted else self.volume / 100.0

    def _set_pause_visual(self, paused):
        """暂停按钮：图标+文字随状态切换（矢量图标，颜色随主题）"""
        self.pause_btn.text = "继续" if paused else "暂停"
        self.pause_btn.icon = ft.Icons.PLAY_ARROW if paused else ft.Icons.PAUSE

    def _set_mute_visual(self):
        """ muted / 0% → VOLUME_OFF，<50% → VOLUME_DOWN，其余 → VOLUME_UP"""
        if self.muted or self.volume <= 0:
            icon = ft.Icons.VOLUME_OFF
        elif self.volume < 50:
            icon = ft.Icons.VOLUME_DOWN
        else:
            icon = ft.Icons.VOLUME_UP
        self.mute_btn.icon = icon
        self.mute_btn.icon_color = C_TEXT_3 if self.muted else C_TEXT_2
        try:
            self.vol_label.value = f"{0 if self.muted else self.volume}%"
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 歌词显示（半透明面板，跟随播放进度高亮）
    # ------------------------------------------------------------------
    _LRC_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")

    def on_lyric_toggle(self, e):
        """勾选/取消歌词面板；状态持久化"""
        self.show_lyric = bool(self.lyric_check.value)
        self.lyric_panel.visible = self.show_lyric
        if self.show_lyric and self.current_song is not None:
            platform = self.platform_dd.value or "kuwo"
            key = (platform, self.current_song.get("id"))
            if self._lyric_song_key != key:
                self._fetch_lyric_async()
        try:
            self.page.run_task(self._persist_lyric_pref)
        except Exception:
            pass
        try:
            self.page.update()
        except Exception:
            pass

    async def _persist_lyric_pref(self):
        try:
            await self.page.client_storage.set_async(
                "show_lyric", bool(self.show_lyric))
        except Exception:
            pass  # client_storage 不可用时退化为内存默认值

    def _fetch_lyric_async(self):
        """后台线程拉歌词并解析；成功回填面板，失败显示暂无歌词。
        网络失败不抛异常、不中断播放"""
        song = self.current_song
        if song is None or not self.show_lyric:
            return
        platform = self.platform_dd.value or "kuwo"
        key = (platform, song.get("id"))
        self._lyric_song_key = key
        self._lyric_lines = []
        self._lyric_active = -1
        self._render_lyric_message("歌词加载中...")

        def worker():
            if key in self._lyric_cache:
                lrc = self._lyric_cache[key]
                print(f"[lyric] cache hit: {key} len={len(lrc)}")
            else:
                lrc = self.client.get_lyric(song, platform)
                self._lyric_cache[key] = lrc
            # 拉取期间用户已切歌 → 丢弃过期结果
            if self._lyric_song_key != key:
                print(f"[lyric] outdated result, discarded (current={self._lyric_song_key})")
                return
            lines = self._parse_lrc(lrc) if lrc else []
            print(f"[lyric] parse complete: {len(lines)} lines")
            if not lines:
                self._render_lyric_message("暂无歌词")
                return
            self._lyric_lines = lines
            self._lyric_active = -1
            self._render_lyric_message("")  # 清提示，等待首个 position 事件

        threading.Thread(target=worker, daemon=True).start()

    def _parse_lrc(self, lrc):
        """解析 LRC 为 [(sec, text)]，按时间排序（一行可带多个时间戳）"""
        lines = []
        for raw in str(lrc).splitlines():
            stamps = self._LRC_RE.findall(raw)
            if not stamps:
                continue
            text = self._LRC_RE.sub("", raw).strip()
            if not text:
                continue
            for mm, ss, frac in stamps:
                sec = int(mm) * 60 + int(ss)
                if frac:
                    sec += float("0." + frac)
                lines.append((sec, text))
        lines.sort(key=lambda t: t[0])
        return lines

    def _lyric_font_size(self):
        """歌词字号随设备分档：车机大字号"""
        return 18 if self._mode == "car" else 15

    def _render_lyric_message(self, msg):
        """面板占位提示（加载中/暂无歌词）；空串则清空待高亮"""
        size = self._lyric_font_size()
        if msg:
            self.lyric_col.controls = [ft.Text(
                msg, size=size,
                color=_op(C_LYRIC_MSG, 0.75),
                text_align=ft.TextAlign.CENTER)]
        else:
            self.lyric_col.controls = []
        try:
            self.page.update()
        except Exception:
            pass

    def _render_lyric_window(self):
        """渲染以当前行为中心的歌词窗口（上4行+当前+下4行）。
        浅色面板 + 深灰字体系：当前行强调蓝加粗放大，相邻行 70% 深灰，其余 40% 深灰。
        每次当前行索引变化都会整块重建 Text 样式（不是只改文字内容）"""
        if not self._lyric_lines:
            return
        act = self._lyric_active
        size = self._lyric_font_size()
        half = 4
        start = max(0, (act if act >= 0 else 0) - half)
        end = min(len(self._lyric_lines), start + half * 2 + 1)
        controls = []
        for i in range(start, end):
            _, txt = self._lyric_lines[i]
            if i == act:
                controls.append(ft.Text(
                    txt, size=size + 3, weight=ft.FontWeight.BOLD,
                    color=C_LYRIC_CUR, text_align=ft.TextAlign.CENTER,
                    max_lines=2, overflow=ft.TextOverflow.ELLIPSIS))
            elif abs(i - act) == 1:
                controls.append(ft.Text(
                    txt, size=size,
                    color=_op(C_LYRIC_NEAR, 0.70),
                    text_align=ft.TextAlign.CENTER,
                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS))
            else:
                controls.append(ft.Text(
                    txt, size=size,
                    color=_op(C_LYRIC_FAR, 0.40),
                    text_align=ft.TextAlign.CENTER,
                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS))
        self.lyric_col.controls = controls
        try:
            self.page.update()
        except Exception:
            pass

    def _sync_lyric(self, pos_sec):
        """按当前播放位置(秒)更新高亮行。单调前进 O(1)，seek 回跳线性重扫"""
        if not self.show_lyric or not self._lyric_lines:
            return
        lines = self._lyric_lines
        tol = 0.3
        if self._lyric_active >= 0:
            idx = self._lyric_active
            if pos_sec + tol >= lines[idx][0]:
                while idx + 1 < len(lines) and lines[idx + 1][0] <= pos_sec + tol:
                    idx += 1
            else:
                idx = -1  # seek 回跳 → 重扫
        else:
            idx = -1
        if idx < 0:
            idx = -1
            for i, (sec, _) in enumerate(lines):
                if sec <= pos_sec + tol:
                    idx = i
                else:
                    break
        if idx != self._lyric_active:
            # 索引变化 → 整块重建歌词样式（颜色/字重/字号都随之更新）
            print(f"[lyric] active line {self._lyric_active} -> {idx} @ {pos_sec:.1f}s")
            self._lyric_active = idx
            self._render_lyric_window()

    # ---- 歌词面板拖动（整块面板为热区）----
    def _on_lyric_pan_start(self, e):
        self._lyric_drag["on"] = True
        self._lyric_drag["dx"] = self.lyric_panel.left or 0
        self._lyric_drag["dy"] = self.lyric_panel.top or 0
        print(f"[drag] start at ({self._lyric_drag['dx']}, {self._lyric_drag['dy']})"
              f" event={type(e).__name__}")

    def _on_lyric_pan_update(self, e):
        ddx = getattr(e, "delta_x", 0) or 0
        ddy = getattr(e, "delta_y", 0) or 0
        self._lyric_drag_apply(ddx, ddy, "pan")

    def _on_lyric_scale_update(self, e):
        # ScaleUpdateEvent：鼠标拖拽走这里（pan 不认鼠标），取焦点位移
        ddx = getattr(e, "focal_point_delta_x", 0) or 0
        ddy = getattr(e, "focal_point_delta_y", 0) or 0
        self._lyric_drag_apply(ddx, ddy, "scale")

    def _lyric_drag_apply(self, ddx, ddy, src):
        """统一应用拖拽位移：累计 delta（不是起点+单次 delta，否则会弹回原位）；
        钳制边界每次读取实时窗口尺寸"""
        if not self._lyric_drag.get("on"):
            # 容错：个别平台 start 回调没触发但 update 已在派发 → 用当前坐标起步
            self._lyric_drag["on"] = True
            self._lyric_drag["dx"] = self.lyric_panel.left or 0
            self._lyric_drag["dy"] = self.lyric_panel.top or 0
            print(f"[drag] implicit start via {src}")
        dbg_key = f"n_{src}"
        self._lyric_drag[dbg_key] = self._lyric_drag.get(dbg_key, 0) + 1
        if self._lyric_drag[dbg_key] <= 3:
            print(f"[drag] {src} delta=({ddx}, {ddy})")
        page = self.page
        w = page.width or 0
        h = page.height or 0
        pw = self.lyric_box.width or 0
        ph = self.lyric_box.height or 0
        # 累计到位移状态里并同步钳制，再落到面板
        self._lyric_drag["dx"] = max(0, min((self._lyric_drag["dx"] or 0) + ddx,
                                            max(0, w - pw)))
        self._lyric_drag["dy"] = max(0, min((self._lyric_drag["dy"] or 0) + ddy,
                                            max(0, h - ph)))
        self.lyric_panel.left = self._lyric_drag["dx"]
        self.lyric_panel.top = self._lyric_drag["dy"]
        try:
            page.update()
        except Exception:
            pass

    def _on_lyric_pan_end(self, e):
        self._lyric_drag["on"] = False

    def _position_lyric_panel(self, reset=True):
        """按当前窗口/模式设置面板尺寸；reset=True 时置于播放条上方居中"""
        page = self.page
        w = page.width or 0
        h = page.height or 0
        if w <= 0 or h <= 0 or self.lyric_panel is None:
            return
        car = self._mode == "car"
        tight = self._mode in ("extreme", "desktop_small")
        pw = max(240, min(int(w * 0.72), 560))
        ph = 210 if car else (142 if tight else 176)
        self.lyric_box.width = pw
        self.lyric_box.height = ph
        if reset:
            self.lyric_panel.left = max(8, (w - pw) // 2)
            # 默认在播放条上方，不挡列表主体；矮屏再上移让位
            self.lyric_panel.top = max(8, h - ph - (160 if not tight else 128))
        else:
            self.lyric_panel.left = max(0, min(self.lyric_panel.left or 0, w - pw))
            self.lyric_panel.top = max(0, min(self.lyric_panel.top or 0, h - ph))

    def _apply_volume_to_audio(self):
        """把当前音量同步给 Audio 组件（懒创建/切歌后都要调用）"""
        if self.audio is not None:
            try:
                self.audio.volume = self._effective_volume()
            except Exception:
                pass

    def _persist_volume(self):
        """音量/静音持久化：优先 client_storage（桌面=本地文件，
        手机/车机=应用沙箱存储），API 不可用时退化为内存值"""
        async def _save():
            try:
                await self.page.client_storage.set_async("player_volume", self.volume)
                await self.page.client_storage.set_async("player_muted", self.muted)
            except Exception:
                pass
        try:
            self.page.run_task(_save)
        except Exception:
            pass  # 无 client_storage 时退化为内存值

    def _load_prefs(self):
        """启动时读取持久化的音量/静音设置并刷新 UI"""
        async def _load():
            try:
                v = await self.page.client_storage.get_async("player_volume")
                m = await self.page.client_storage.get_async("player_muted")
                if isinstance(v, (int, float)) and 0 <= v <= 100:
                    self.volume = int(v)
                if isinstance(m, bool):
                    self.muted = m
                    if self.muted:
                        self._pre_mute_volume = self.volume
            except Exception:
                pass  # 读不到就用默认值（80%，不静音）
            try:
                lv = await self.page.client_storage.get_async("show_lyric")
                if isinstance(lv, bool):
                    self.show_lyric = lv
            except Exception:
                pass  # 读不到就用默认值（默认勾选）
            try:
                self.vol_slider.value = 0 if self.muted else self.volume
                self._set_mute_visual()
                self.lyric_check.value = self.show_lyric
                self.lyric_panel.visible = self.show_lyric
                self._apply_volume_to_audio()
                self.page.update()
            except Exception:
                pass
        try:
            self.page.run_task(_load)
        except Exception:
            pass

    def on_volume_change(self, e=None):
        """拖动音量滑块（实时生效）：更新应用内音量并同步 Audio"""
        try:
            val = int(float(getattr(e, "control", e).value))
        except Exception:
            return
        self.volume = max(0, min(100, val))
        if self.muted and self.volume > 0:
            # 拖动音量 = 解除静音
            self.muted = False
            self._set_mute_visual()
        elif not self.muted:
            self._pre_mute_volume = self.volume
        # 图标随音量档位变化（OFF/DOWN/UP）+ 右侧百分比同步
        self._set_mute_visual()
        self._apply_volume_to_audio()
        try:
            self.page.update()
        except Exception:
            pass

    def on_volume_commit(self, e=None):
        """拖动结束后持久化"""
        self._persist_volume()

    def on_mute_toggle(self, e=None):
        """静音/恢复：记住静音前的音量"""
        if not self.muted:
            self._pre_mute_volume = self.volume
            self.muted = True
            self.vol_slider.value = 0
        else:
            self.muted = False
            self.volume = self._pre_mute_volume or 50
            self.vol_slider.value = self.volume
        self._set_mute_visual()
        self._apply_volume_to_audio()
        self._persist_volume()
        try:
            self.page.update()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 状态/界面小工具
    # ------------------------------------------------------------------
    def _set_status(self, text):
        self.status_text.value = text
        try:
            self.page.update()
        except Exception:
            pass

    def _update_highlight(self):
        """高亮当前播放行：浅蓝底（强调色 10%）+ 左侧强调色竖条"""
        for i, row in enumerate(self.row_controls):
            active = i == self.current_index
            # 正在播放：浅蓝底（强调色 10%）+ 左侧强调条
            row.bgcolor = _op(C_ACCENT, 0.10) if active else C_SURFACE
            if i < len(self.row_bars):
                self.row_bars[i].bgcolor = (
                    C_ACCENT if active else ft.Colors.TRANSPARENT)
        try:
            self.page.update()
        except Exception:
            pass

    def _guess_duration_ms(self, v):
        """搜索结果里的 duration 单位不确定（秒/毫秒），用阈值启发式判断"""
        n = _evt_ms(v)
        if n <= 0:
            return 0
        return n if n > 10000 else n * 1000

    # ------------------------------------------------------------------
    # 搜索与加载
    # ------------------------------------------------------------------
    def do_search(self, e=None):
        kw = (self.keyword.value or "").strip()
        if not kw:
            self._set_status("请输入歌手名或歌曲名")
            return
        platform = self.platform_dd.value or "kuwo"
        self.search_platform = platform
        # 换搜索时停掉当前播放并清空列表
        self._stop_playback(silent=True)
        self._clear_list()
        self.search_btn.disabled = True
        self._set_status(f"正在搜索「{kw}」({self.PLATFORM_LABEL.get(platform, platform)})...")

        def _work():
            other = "wyy" if platform == "kuwo" else "kuwo"
            chosen = platform
            try:
                try:
                    items, total = self.client.search(kw, platform, 1)
                except Exception as first_ex:
                    # 选中平台搜索失败/空列表（酷我源近期在网站端返回空列表）：
                    # 自动尝试另一平台
                    try:
                        items2, total2 = self.client.search(kw, other, 1)
                    except Exception:
                        raise first_ex
                    if not items2:
                        raise first_ex
                    chosen = other
                    items, total = items2, total2
                    self._set_status(
                        f"{self.PLATFORM_LABEL.get(platform, platform)}源暂不可用（限流），"
                        f"已自动切换到{self.PLATFORM_LABEL.get(chosen, chosen)}源"
                        f"（注意：网易云源的播放地址解析当前在网站端故障，可能无法播放）")

                with self.songs_lock:
                    self.songs = list(items)
                    self._fetch_stop.clear()
                self.search_platform = chosen
                self.current_index = None
                self._rebuild_list()
                self._set_status(
                    f"搜索完成（{self.PLATFORM_LABEL.get(chosen, chosen)}源），"
                    f"共 {total} 首，已显示 {len(items)} 首。"
                    f"点击「加载全部」获取完整列表。")
            except Exception as ex:
                self._set_status("搜索失败: " + str(ex))
            finally:
                self.search_btn.disabled = False
                try:
                    self.page.update()
                except Exception:
                    pass

        threading.Thread(target=_work, daemon=True).start()

    def load_all_pages(self, e=None):
        """自动翻页加载全部结果"""
        kw = (self.keyword.value or "").strip()
        if not kw:
            self._set_status("请先输入关键词搜索")
            return
        with self.songs_lock:
            if not self.songs:
                self.do_search()
                return
        self.load_all_btn.disabled = True
        self._fetch_stop.clear()
        self._set_status("正在加载全部结果（自动翻页）...")

        def _work():
            try:
                page = 2
                while not self._fetch_stop.is_set():
                    items, total = self.client.search(kw, self.search_platform, page)
                    if not items:
                        break
                    with self.songs_lock:
                        base = len(self.songs)
                        self.songs.extend(items)
                    self._append_rows(items, base)
                    self._set_status(
                        f"加载中：第 {page} 页，已加载 {base + len(items)} / 约 {total} 首")
                    page += 1
                    if page > MAX_PAGES:
                        break
                    time.sleep(1.5)  # 翻页间隔，降低限流风险
                self._set_status(f"全部加载完成，共 {len(self.songs)} 首。")
            except Exception as ex:
                self._set_status("加载失败: " + str(ex))
            finally:
                self.load_all_btn.disabled = False
                try:
                    self.page.update()
                except Exception:
                    pass

        threading.Thread(target=_work, daemon=True).start()

    def _clear_list(self):
        with self.songs_lock:
            self.songs = []
        self.row_controls = []
        self.row_bars = []
        self.current_index = None
        self.list_view.controls.clear()
        try:
            self.page.update()
        except Exception:
            pass

    def _rebuild_list(self):
        """搜索完成后重建整个列表"""
        self.list_view.controls.clear()
        self.row_controls = []
        self.row_bars = []
        with self.songs_lock:
            items = list(self.songs)
        self._append_rows(items, 0)

    def _append_rows(self, items, base_index):
        """把一批歌曲追加到列表。base_index 为这批歌曲在 songs 中的起始索引。
        行结构：强调条 | 序号 | 歌名/歌手·专辑 | 音质，四列固定栅格对齐"""
        new_rows = []
        new_bars = []
        # 车机模式：大字号、大行高，远距观看可读
        car = self._mode == "car"
        t_size = 17 if car else 14
        s_size = 13 if car else 12
        q_size = 13 if car else 12
        pad_v = 9 if car else 7
        bar_h = 40 if car else 34
        for i, song in enumerate(items):
            idx = base_index + i
            q = pick_best_quality(song.get("minfo") or [])
            quality = f"{q['format'].upper()} {q.get('bitrate')}K" if q else "无MP3"
            name = song.get("name", "?")
            artist = song.get("artist", "") or "未知歌手"
            album = song.get("album_name", "") or ""
            meta = f"{artist} · {album}" if album else artist
            bar = ft.Container(
                width=3, height=bar_h, border_radius=2,
                bgcolor=ft.Colors.TRANSPARENT)
            num = ft.Text(
                f"{idx + 1}", size=s_size, color=C_TEXT_3,
                width=26, text_align=ft.TextAlign.RIGHT)
            row = ft.Container(
                content=ft.Row(
                    [
                        bar,
                        num,
                        ft.Column(
                            [
                                ft.Text(
                                    name, size=t_size, color=C_TEXT,
                                    weight=ft.FontWeight.W_500,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                ft.Text(
                                    meta, size=s_size, color=C_TEXT_2,
                                    max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                            ],
                            spacing=2, tight=True, expand=True,
                        ),
                        ft.Text(
                            quality, size=q_size, color=C_TEXT_3,
                            width=84, text_align=ft.TextAlign.RIGHT),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.symmetric(horizontal=8, vertical=pad_v),
                border_radius=8,
                bgcolor=C_SURFACE,   # 白色行底，靠 ListView 分隔线分界
                on_click=lambda e, k=idx: self.on_row_click(k),
                ink=True,
            )
            new_rows.append(row)
            new_bars.append(bar)
        with self.songs_lock:
            need = len(self.songs)
        # 行控件同时进 row_controls（高亮用）与 list_view（显示用）
        added = []
        while len(self.row_controls) < need and new_rows:
            self.row_controls.append(new_rows.pop(0))
            self.row_bars.append(new_bars.pop(0))
            added.append(self.row_controls[-1])
        self.list_view.controls.extend(added)
        self._update_highlight()

    # ------------------------------------------------------------------
    # 播放控制
    # ------------------------------------------------------------------
    def on_play_from_first(self, e=None):
        """从第一首开始顺序播放"""
        self.play_mode = "seq"
        self._play_index(0)

    def on_play_random(self, e=None):
        """随机播放一首，之后每次播完继续随机"""
        with self.songs_lock:
            n = len(self.songs)
        if n == 0:
            self._set_status("列表为空，请先搜索")
            return
        self.play_mode = "random"
        self._play_index(random.randrange(n))

    def on_row_click(self, idx):
        """点击列表播放：播完后自动接下一首（顺序）"""
        self.play_mode = "seq"
        self._play_index(idx)

    def _play_index(self, idx):
        with self.songs_lock:
            n = len(self.songs)
        if n == 0:
            self._set_status("列表为空，请先搜索")
            return
        if not (0 <= idx < n):
            return
        self._manual_stop = False
        self.current_index = idx
        self._update_highlight()
        with self.songs_lock:
            song = self.songs[idx]
        threading.Thread(target=self._play_song, args=(song,), daemon=True).start()

    def _ensure_audio(self, url):
        """首次播放时创建 Audio 组件并加入 overlay；之后切歌只更换 src。
        autoplay=False：等 on_loaded（媒体真正加载完成）后再显式 play()，
        避免在加载完成前发 play 命令被客户端忽略导致无声"""
        if self.audio is None:
            self.audio = self._create_audio_control(url)
            self.page.overlay.append(self.audio)
        else:
            self.audio.src = url
            self._apply_volume_to_audio()  # 每次切歌复用组件时重申当前音量

    def _create_audio_control(self, url):
        """按运行平台选择实现：
        - Android / iOS：必须用 flet_audio 扩展（flet 0.26 起 audio 控件从核心包
          拆出，移动端客户端不再内置，用核心 ft.Audio 会报 "Unknown control: audio" 且无声）
        - 桌面：沿用 flet 核心 Audio（已长期验证可用）
        """
        plat = str(getattr(self.page, "platform", "") or "").lower()
        mobile = ("android" in plat) or ("ios" in plat)
        cls = _AudioControl if mobile else (getattr(ft, "Audio", None) or _AudioControl)
        print(f"[audio] platform={plat or '?'} using={getattr(cls, '__module__', '?')}")
        return cls(
            src=url,
            autoplay=False,
            volume=self._effective_volume(),  # 新建即带上当前音量/静音
            on_loaded=self._on_audio_loaded,
            on_duration_changed=self._on_duration_changed,
            on_position_changed=self._on_position_changed,
            on_state_changed=self._on_state_changed,
        )

    def _play_song(self, song):
        """后台线程：取播放地址并交给 Audio 组件播放"""
        self.current_song = song  # 供歌词拉取等使用
        name = song.get("name", "?")
        artist = song.get("artist", "")
        q = pick_best_quality(song.get("minfo") or [])
        if not q:
            self._set_status(f"「{name}」无可用音质，自动跳下一首")
            self._play_next()
            return
        self._set_status(f"正在获取播放地址: {name} - {artist}")
        try:
            url = self.client.get_url(
                song, q["format"], q.get("bitrate"), self.search_platform)
        except Exception as ex:
            self._fail_streak += 1
            if self._fail_streak >= 5:
                self._set_status(
                    f"连续 {self._fail_streak} 首获取地址失败，已停止自动连播。"
                    f"（网站解析后端可能临时故障，请稍后再试。最后错误: {ex}）")
                return
            self._set_status(
                f"获取「{name}」地址失败: {ex}，自动跳下一首"
                f"（若连续失败可能是网站解析后端临时故障）")
            self._play_next()
            return
        self._fail_streak = 0

        # 更新播放条（歌名主、歌手次，时间靠右独立显示）
        self.title_text.value = name
        self.artist_text.value = artist or "未知歌手"
        self._duration_ms = self._guess_duration_ms(song.get("duration"))
        self.slider.max = max(1, int(self._duration_ms / 1000)) if self._duration_ms else 100
        self.slider.value = 0
        self.slider.label = None          # 新歌不保留上一首的拖动气泡
        self._seeking = False
        self._seek_seq += 1               # 让上一首拖动的兜底 seek 失效
        self.time_text.value = f"00:00 / {fmt_time(self._duration_ms / 1000)}"
        self._paused_flag = False
        self._want_play = True
        self._loaded_flag = False          # 新歌：等待客户端 on_loaded 回报
        self._play_seq += 1                # 使上一首的兜底计时失效
        self._set_pause_visual(False)
        try:
            self._ensure_audio(url)
            self.page.update()
            # 不在这里直接 play()：等 on_loaded 回调触发后再播（见 _on_audio_loaded）
        except Exception as ex:
            self._set_status(f"播放失败: {ex}")
            return
        self._set_status(f"正在加载音频: {name} - {artist}")
        # 兜底：若客户端迟迟不回报 on_loaded（Android 上个别机型/编码会这样），
        # 3 秒后主动 play() 一次，并把结果写进状态栏，避免静默无声
        threading.Thread(
            target=self._play_fallback_check, args=(self._play_seq, name), daemon=True
        ).start()
        # 后台拉取并显示当前歌歌词（面板关闭时不拉）
        try:
            self._fetch_lyric_async()
        except Exception:
            pass

    def _play_next(self):
        """播完/跳过后自动接下一首：顺序模式顺延，随机模式随机换曲"""
        with self.songs_lock:
            n = len(self.songs)
        if n == 0 or self.current_index is None:
            return
        if self.play_mode == "random":
            if n == 1:
                idx = 0
            else:
                idx = self.current_index
                while idx == self.current_index:
                    idx = random.randrange(n)
        else:
            idx = self.current_index + 1
            if idx >= n:
                self._set_status("播放列表已播完。")
                return
        self._play_index(idx)

    def on_pause_toggle(self, e=None):
        """暂停/继续切换"""
        if self.audio is None or self.current_index is None:
            return
        try:
            if self._paused_flag:
                self.audio.resume()
            else:
                self.audio.pause()
        except Exception as ex:
            self._set_status(f"暂停/继续失败: {ex}")

    def on_stop(self, e=None):
        self._stop_playback(silent=False)

    def _stop_playback(self, silent=False):
        self._manual_stop = True
        self._seeking = False
        self._paused_flag = False
        self._want_play = False
        try:
            if self.audio is not None:
                self.audio.pause()
                self.audio.seek(0)
        except Exception:
            pass
        self.slider.value = 0
        self.time_text.value = "00:00 / 00:00"
        self._set_pause_visual(False)
        if not silent:
            self._set_status("已停止播放。")
        else:
            try:
                self.page.update()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Audio 事件
    # ------------------------------------------------------------------
    def _on_audio_loaded(self, e=None):
        """媒体加载完成 → 此时 play() 才会真正生效"""
        self._loaded_flag = True
        try:
            if self._want_play and self.audio is not None:
                self.audio.play()
                self._set_status("正在播放（客户端已确认加载）")
        except Exception as ex:
            self._set_status(f"启动播放失败: {ex}")

    def _play_fallback_check(self, seq, name):
        """兜底：3 秒内没收到 on_loaded 就主动 play()，并在状态栏说明结果"""
        time.sleep(3.0)
        if seq != self._play_seq or not self._want_play or self.audio is None:
            return                      # 已切歌/已停止/组件不存在
        if self._loaded_flag:
            return                      # on_loaded 正常触发，无需兜底
        try:
            self.audio.play()
            self._set_status(f"正在播放（未收到加载回调，已兜底启动: {name}）")
            print("[audio] fallback play() issued (no on_loaded within 3s)")
        except Exception as ex:
            self._set_status(f"兜底播放失败: {ex}")

    def _on_duration_changed(self, e):
        d = _evt_ms(getattr(e, "data", None))
        if d > 0:
            self._duration_ms = d
            self.slider.max = max(1, int(d / 1000))

    def _on_position_changed(self, e):
        pos = getattr(e, "position", None)
        dur = getattr(e, "duration", None)
        if pos is None:
            # 旧版 flet：e.data 形如 "12345" 或 "12345,67890"
            parts = str(getattr(e, "data", "") or "0").split(",")
            pos = parts[0]
            if dur is None and len(parts) > 1:
                dur = parts[1]
        pos_ms = _evt_ms(pos)
        dur_ms = _evt_ms(dur)
        if dur_ms > 0 and dur_ms != self._duration_ms:
            self._duration_ms = dur_ms
            self.slider.max = max(1, int(dur_ms / 1000))
        if self._seeking:
            return
        total_s = self._duration_ms / 1000.0
        try:
            self.slider.value = min(pos_ms / 1000.0, float(self.slider.max or 1))
        except Exception:
            pass
        self.time_text.value = f"{fmt_time(pos_ms / 1000)} / {fmt_time(total_s)}"
        try:
            self.page.update()
        except Exception:
            pass
        # 歌词高亮跟随（暂停时不产生 position 事件，高亮自然保持不动）
        self._pos_dbg = getattr(self, "_pos_dbg", 0) + 1
        if self._pos_dbg <= 3:
            print(f"[lyric] position event #{self._pos_dbg}: {pos_ms}ms")
        self._sync_lyric(pos_ms / 1000.0)

    def _on_state_changed(self, e):
        state = str(getattr(e, "state", None) or getattr(e, "data", "") or "").lower()
        print("[audio state]", state or "(empty)")  # 诊断输出：真实状态序列，无声时看这里
        if "completed" in state:
            # 播放自然结束 → 自动下一首
            # （flet 0.28 的 Audio 无 on_ended 事件，用 completed 状态检测）
            if not self._manual_stop:
                self._paused_flag = False
                self._set_pause_visual(False)
                try:
                    self.page.update()
                except Exception:
                    pass
                threading.Thread(target=self._play_next, daemon=True).start()
            return
        if "playing" in state:
            self._paused_flag = False
        elif any(k in state for k in ("paused", "stopped", "ended")):
            self._paused_flag = True
        self._set_pause_visual(self._paused_flag)
        try:
            self.page.update()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 进度条拖动跳转
    # ------------------------------------------------------------------
    def _on_slider_start(self, e=None):
        """开始拖动：立刻置位 _seeking，阻断 _on_position_changed 回写"""
        self._seeking = True
        print("[seek] drag start")

    def _on_slider_change(self, e=None):
        """拖动中：暂停位置同步，实时显示目标时间气泡，并启动兜底 seek"""
        self._seeking = True
        try:
            val = float(e.control.value or 0)
        except Exception:
            return
        self._seek_target = val
        self.slider.label = fmt_time(val)      # Slider 自带的拖动气泡
        self.time_text.value = (
            f"{fmt_time(val)} / {fmt_time(self._duration_ms / 1000)}")
        self._seek_seq += 1
        seq = self._seek_seq
        # 兜底：个别平台 on_change_end 不触发，0.8 秒无新拖动就自动 seek
        threading.Thread(
            target=self._seek_after_idle, args=(seq, val), daemon=True).start()
        try:
            self.page.update()
        except Exception:
            pass

    def _seek_after_idle(self, seq, val):
        time.sleep(0.8)
        if seq != self._seek_seq or not self._seeking:
            return                 # 已有新的拖动/已松手，本次作废
        print("[seek] on_change_end missing, fallback seek")
        self._perform_seek(val)

    def _on_slider_end(self, e=None):
        """松手：取消兜底并执行 seek"""
        self._seek_seq += 1
        try:
            val = float(e.control.value or 0)
        except Exception:
            val = self._seek_target or 0
        self._perform_seek(val)

    def _perform_seek(self, seconds):
        """执行跳转：清气泡、恢复位置同步、刷新时间与歌词高亮"""
        seconds = max(0.0, float(seconds or 0))
        self._seeking = False
        self.slider.label = None
        self.slider.value = min(seconds, float(self.slider.max or 1))
        self.time_text.value = (
            f"{fmt_time(seconds)} / {fmt_time(self._duration_ms / 1000)}")
        self._sync_lyric(seconds)              # 立即按目标位置高亮，不等下一个事件
        try:
            self.page.update()
        except Exception:
            pass
        if self.audio is None:
            return
        ms = int(seconds * 1000)
        try:
            self.audio.seek(ms)
            print(f"[seek] seek to {ms}ms")
        except Exception:
            try:
                from datetime import timedelta
                self.audio.seek(timedelta(milliseconds=ms))
                print(f"[seek] seek(timedelta) to {ms}ms")
            except Exception as ex:
                self._set_status(f"跳转失败: {ex}")


def main(page: ft.Page):
    MusicApp(page)


if __name__ == "__main__":
    ft.app(target=main)
