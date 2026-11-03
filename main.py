# -*- coding: utf-8 -*-
"""flet build 的应用入口：flet 打包模板默认从 main.py 启动。

真正实现见 music_player_flet.py（ApiClient 网络层 + MusicApp 界面层）。
"""
import flet as ft

from music_player_flet import main

if __name__ == "__main__":
    ft.app(target=main)
