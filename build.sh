#!/usr/bin/env bash
# Wallpaper Changer SPK 一键打包
# 产物：dist/autowallpaper_noarch-dsm7_<version>-<build>.spk
# .spk = 未压缩 tar{ INFO, package.tgz, scripts/, conf/, PACKAGE_ICON*.PNG, LICENSE }
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PKG_NAME="autowallpaper"
VERSION="1.0.0"
BUILD="0010"
ARCH="epyc7002 x86_64"
ARCH_TAG="x86_64"
SPK_FILE="${ROOT}/dist/${PKG_NAME}_${ARCH_TAG}-dsm7_${VERSION}-${BUILD}.spk"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/stage"
mkdir -p "$STAGE/bin" "$STAGE/ui/images" "$STAGE/var" \
         "$WORK/scripts" "$WORK/conf" "$ROOT/dist"

echo "==> 校验源码"
python3 - "$ROOT" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
for rel in ("src/bin/wallpaper-daemon.py", "src/ui/api.cgi", "tests/test_local.py"):
    path = root / rel
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
print("python syntax OK")
PY
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$ROOT/spk/var-default-config.json"
for f in installer service-setup start-stop-status \
         preinst postinst preuninst postuninst preupgrade postupgrade; do
  [ -f "$ROOT/spk/scripts/$f" ] || { echo "缺少 spk/scripts/$f"; exit 1; }
  sh -n "$ROOT/spk/scripts/$f"
done

echo "==> 编译 setuid helper"
command -v gcc >/dev/null 2>&1 || { echo "缺少 gcc（用于编译 wallpaper-helper）"; exit 1; }
[ "$(uname -m)" = "x86_64" ] || { echo "当前构建机不是 x86_64，helper 需在 x86_64 上交叉/本机编译"; exit 1; }
gcc -O2 -static -s -Wall -Wextra -o "$WORK/wallpaper-helper" "$ROOT/src/bin/wallpaper-helper.c"
install -m 755 "$WORK/wallpaper-helper" "$STAGE/bin/wallpaper-helper"

echo "==> 组装 package.tgz 内容"
install -m 755 "$ROOT/src/bin/wallpaper-daemon.py" "$STAGE/bin/wallpaper-daemon.py"
install -m 755 "$ROOT/src/ui/api.cgi"              "$STAGE/ui/api.cgi"
install -m 644 "$ROOT/src/ui/index.html"           "$STAGE/ui/index.html"
install -m 644 "$ROOT/src/ui/config"               "$STAGE/ui/config"
install -m 644 "$ROOT/src/ui/dsm-wrapper.js"       "$STAGE/ui/dsm-wrapper.js"
install -m 644 "$ROOT/src/ui/images/"wallpaper_*.png "$STAGE/ui/images/"
install -m 644 "$ROOT/spk/var-default-config.json" "$STAGE/var/config.json"

echo "==> scripts / conf / 图标 / LICENSE"
for f in installer service-setup start-stop-status \
         preinst postinst preuninst postuninst preupgrade postupgrade; do
  install -m 755 "$ROOT/spk/scripts/$f" "$WORK/scripts/$f"
done
install -m 644 "$ROOT/spk/conf/privilege" "$WORK/conf/privilege"
install -m 644 "$ROOT/src/ui/images/wallpaper_64.png"  "$WORK/PACKAGE_ICON.PNG"
install -m 644 "$ROOT/src/ui/images/wallpaper_256.png" "$WORK/PACKAGE_ICON_256.PNG"
install -m 644 "$ROOT/LICENSE" "$WORK/LICENSE"

echo "==> 生成 package.tgz"
( cd "$STAGE" && tar czf "$WORK/package.tgz" bin ui var )

CHECKSUM="$(md5sum "$WORK/package.tgz" | cut -d' ' -f1)"
echo "==> package.tgz md5 = $CHECKSUM"

# INFO 只允许 key="value" 行，不能有注释/空行（DSM 解析器会拒绝安装）
cat > "$WORK/INFO" <<EOF
package="$PKG_NAME"
version="$VERSION-$BUILD"
description="Automatically change DSM login and desktop wallpapers from Bing daily image or a local folder."
description_chs="自动更换 DSM 登录壁纸与桌面壁纸，支持必应每日一图与本地图片。"
description_cht="自動更換 DSM 登入壁紙與桌面壁紙，支援 Bing 每日一圖與本機圖片。"
arch="$ARCH"
os_min_ver="7.2-64570"
displayname="Wallpaper Changer"
displayname_chs="壁纸自动更换"
displayname_cht="壁紙自動更換"
dsmuidir="ui"
dsmappname="MeowPerth.AutoWallpaper.AppInstance"
maintainer="MeowPerth"
maintainer_url="https://github.com/MeowPerth/WallpaperChange-DSM"
distributor="MeowPerth"
distributor_url="https://github.com/MeowPerth/WallpaperChange-DSM"
helpurl="https://github.com/MeowPerth/WallpaperChange-DSM#readme"
support_url="https://github.com/MeowPerth/WallpaperChange-DSM/issues"
startable="yes"
changelog="1.0.0-0010: 项目更名为 WallpaperChange-DSM，开发者/支持链接同步更新；无功能变更。"
checksum="$CHECKSUM"
EOF

if grep -Evq '^[A-Za-z_]+=".*"$' "$WORK/INFO"; then
  echo "ERROR: INFO 含非法行："
  grep -Env '^[A-Za-z_]+=".*"$' "$WORK/INFO"
  exit 1
fi

echo "==> 打包 .spk（未压缩 tar）"
( cd "$WORK" && tar cpf "$SPK_FILE" INFO package.tgz scripts conf \
    PACKAGE_ICON.PNG PACKAGE_ICON_256.PNG LICENSE )

echo "==> 产物：$SPK_FILE ($(du -h "$SPK_FILE" | cut -f1))"
tar tf "$SPK_FILE"
