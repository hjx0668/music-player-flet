# 云端打包 APK（GitHub Actions）

无需本地安装 Android SDK / Flutter，用 GitHub 免费云服务器构建。

## 步骤

1. 把本目录推到 GitHub **public** 仓库（含 `.github/workflows/build-apk.yml`）
2. 仓库页 → **Actions** → 选 **Build APK** → **Run workflow**
3. 等 15~30 分钟，进该次运行页面底部 **Artifacts** 下载 `music-player-apk`
4. 解压得到 `apk` 目录，里面的
   `music-player-arm64-v8a-release.apk` 装手机/现代车机，
   `armeabi-v7a` 版装老车机

## 说明

- flet 版本锁定在 `pyproject.toml` 的 dependencies（0.28.3）。
  **注意**：flet 0.28 起命令行工具在独立的 `flet-cli` 包里，
  两个包都要声明，否则构建时找不到 `flet build` 命令。
- 构建入口是 `main.py`（转发到 `music_player_flet.py`）。
- 产物为 debug 签名，可直接安装；以后上架商店再换正式签名。
- 改了代码后重新 Run workflow 即可出新包。
