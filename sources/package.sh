#!/bin/bash
# ============================================================================
# package.sh —— 把 staging 打成 rootless deb
#
# 用法：bash package.sh <stage 目录> <输出 deb>
#
# stage 目录结构应为：<stage>/var/jb/usr/local/python3.15/...
# ============================================================================
set -euo pipefail

STAGE="${1:?用法: package.sh <stage> <out.deb>}"
OUT="${2:?用法: package.sh <stage> <out.deb>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"

log() { printf '\033[1;34m[pkg]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[err]\033[0m %s\n' "$*" >&2; exit 1; }

[ -d "$STAGE/var/jb" ] || die "stage 里没有 var/jb：$STAGE"

# ---------------------------------------------------------------- 1. DEBIAN
log "生成 DEBIAN 控制文件"
mkdir -p "$STAGE/DEBIAN"
cp "$REPO/control" "$STAGE/DEBIAN/control"

# 算 Installed-Size
SIZE=$(du -sk "$STAGE/var" | awk '{print $1}')
sed -i.bak "s/^Version:.*/&/" "$STAGE/DEBIAN/control" 2>/dev/null || true
rm -f "$STAGE/DEBIAN/control.bak"
grep -q "^Installed-Size:" "$STAGE/DEBIAN/control" \
    && sed -i.bak "s/^Installed-Size:.*/Installed-Size: $SIZE/" "$STAGE/DEBIAN/control" \
    || echo "Installed-Size: $SIZE" >> "$STAGE/DEBIAN/control"
rm -f "$STAGE/DEBIAN/control.bak"

# ---------------------------------------------------------------- 2. postinst
cat > "$STAGE/DEBIAN/postinst" <<'POST'
#!/bin/sh
set -e
JB=/var/jb
PY="$JB/usr/local/python3.15"

# 让 python3 / pip3 能从 PATH 找到
for d in "$JB/usr/local/bin" "$JB/usr/bin"; do
    mkdir -p "$d" 2>/dev/null || true
done

link() {
    target="$1"; linkpath="$2"
    [ -e "$target" ] || return 0
    if [ -L "$linkpath" ] || [ -e "$linkpath" ]; then
        rm -f "$linkpath" 2>/dev/null || true
    fi
    ln -s "$target" "$linkpath" 2>/dev/null || true
}

link "$PY/bin/python3" "$JB/usr/local/bin/python3"
link "$PY/bin/python3" "$JB/usr/local/bin/python"
link "$PY/bin/python3" "$JB/usr/local/bin/python3.15"
link "$PY/bin/pip3"    "$JB/usr/local/bin/pip3"
link "$PY/bin/pip3"    "$JB/usr/local/bin/pip"

# 证书（pip 联网需要）
if [ -d /etc/ssl/certs ]; then
    mkdir -p "$JB/etc/ssl" 2>/dev/null || true
fi

echo ""
echo "Python 已安装到 $PY"
echo "  命令：python3 / pip3"
echo "  验证：python3 -V && pip3 --version"
echo ""
exit 0
POST
chmod 755 "$STAGE/DEBIAN/postinst"

cat > "$STAGE/DEBIAN/prerm" <<'PRE'
#!/bin/sh
JB=/var/jb
for f in python3 python python3.15 pip3 pip; do
    rm -f "$JB/usr/local/bin/$f" 2>/dev/null || true
done
echo "Python 已卸载（site-packages 保留在 /var/jb/usr/local/python3.15）"
exit 0
PRE
chmod 755 "$STAGE/DEBIAN/prerm"

# ---------------------------------------------------------------- 3. 打包
log "打包（xz）"
mkdir -p "$(dirname "$OUT")"
python3 "$HERE/mkdeb.py" "$STAGE" "$OUT" --compression xz

[ -f "$OUT" ] || die "打包失败"
log "产物：$OUT ($(du -h "$OUT" | awk '{print $1}'))"
