#!/bin/bash
# ============================================================================
# build-deps.sh —— 构建 CPython 需要的 iOS 原生依赖
#
# CPython 的 ssl / curses / readline / ffi 模块需要原生库。
# Procursus 提供了运行时库（libssl3 等），但**没有开发头文件**，
# 所以这里从源码交叉编译到统一的 --prefix。
#
# 产物：$PREFIX/{include,lib}
#
# 用法：
#   bash build-deps.sh <iPhoneOS.sdk 路径> <输出前缀>
# ============================================================================
set -euo pipefail

SDK="${1:?用法: build-deps.sh <sdk> <prefix>}"
PREFIX="${2:?用法: build-deps.sh <sdk> <prefix>}"

TARGET="arm64-apple-ios15.0"
MIN_IOS="15.0"
JOBS="${JOBS:-$(sysctl -n hw.ncpu 2>/dev/null || echo 4)}"
WORK="${WORK:-$(pwd)/deps-work}"

log()  { printf '\033[1;34m[deps]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[err]\033[0m %s\n' "$*" >&2; exit 1; }

[ -d "$SDK" ] || die "SDK 不存在：$SDK"
mkdir -p "$WORK" "$PREFIX"

CC="clang -target $TARGET -isysroot $SDK -miphoneos-version-min=$MIN_IOS"
export CC
export CFLAGS="-O2 -fPIC"
export LDFLAGS="-L$PREFIX/lib"

# ---------------------------------------------------------------- 通用
fetch_and_extract() {
    local url="$1" name="$2"
    local archive="$WORK/$(basename "$url")"
    if [ ! -f "$archive" ]; then
        log "下载 $name"
        curl -sSL --retry 3 -m 600 -o "$archive" "$url" || die "下载失败：$url"
    fi
    if [ ! -d "$WORK/$name" ]; then
        log "解包 $name"
        tar xf "$archive" -C "$WORK"
    fi
}

# ---------------------------------------------------------------- 1. zlib
# iOS 自带 libz，但头文件在 SDK 里，直接用，不单独编
log "zlib: 使用 iOS SDK 自带"

# ---------------------------------------------------------------- 2. OpenSSL
build_openssl() {
    local ver="3.2.1"
    local dir="openssl-$ver"
    if [ -f "$PREFIX/lib/libssl.a" ]; then
        log "OpenSSL 已存在，跳过"
        return
    fi
    fetch_and_extract "https://www.openssl.org/source/$dir.tar.gz" "$dir"
    log "配置 OpenSSL $ver"
    (
        cd "$WORK/$dir"
        ./Configure ios64-cross no-shared no-tests no-asm \
            --prefix="$PREFIX" \
            --openssldir=/var/jb/etc/ssl \
            CC="$CC" 2>&1 | tail -3
        log "编译 OpenSSL"
        make -j"$JOBS" 2>&1 | tail -3
        make install_sw 2>&1 | tail -3
    ) || die "OpenSSL 构建失败"
}

# ---------------------------------------------------------------- 3. ncurses
build_ncurses() {
    local ver="6.4"
    local dir="ncurses-$ver"
    if [ -f "$PREFIX/lib/libncursesw.a" ]; then
        log "ncurses 已存在，跳过"
        return
    fi
    fetch_and_extract "https://invisible-mirror.net/archives/ncurses/$dir.tar.gz" "$dir"
    log "配置 ncurses $ver"
    (
        cd "$WORK/$dir"
        ./configure --host=arm-apple-darwin \
            --prefix="$PREFIX" \
            --with-shared --without-debug --without-ada \
            --enable-widec --without-manpages \
            --disable-db-install 2>&1 | tail -3
        log "编译 ncurses"
        make -j"$JOBS" 2>&1 | tail -3
        make install 2>&1 | tail -3
    ) || die "ncurses 构建失败"
}

# ---------------------------------------------------------------- 4. libffi
build_libffi() {
    local ver="3.4.6"
    local dir="libffi-$ver"
    if [ -f "$PREFIX/lib/libffi.a" ]; then
        log "libffi 已存在，跳过"
        return
    fi
    fetch_and_extract "https://github.com/libffi/libffi/releases/download/v$ver/$dir.tar.gz" "$dir"
    log "配置 libffi $ver"
    (
        cd "$WORK/$dir"
        ./configure --host=arm-apple-darwin \
            --prefix="$PREFIX" \
            --enable-static --disable-shared 2>&1 | tail -3
        log "编译 libffi"
        make -j"$JOBS" 2>&1 | tail -3
        make install 2>&1 | tail -3
    ) || die "libffi 构建失败"
}

# ---------------------------------------------------------------- 5. readline
build_readline() {
    local ver="8.2"
    local dir="readline-$ver"
    if [ -f "$PREFIX/lib/libreadline.a" ]; then
        log "readline 已存在，跳过"
        return
    fi
    fetch_and_extract "https://ftp.gnu.org/gnu/readline/$dir.tar.gz" "$dir"
    log "配置 readline $ver"
    (
        cd "$WORK/$dir"
        # 避免和 ncurses 冲突：readline 自带的 termcap
        ./configure --host=arm-apple-darwin \
            --prefix="$PREFIX" \
            --with-curses --enable-static --disable-shared \
            bash_cv_wcwidth_broken=no 2>&1 | tail -3
        log "编译 readline"
        make -j"$JOBS" 2>&1 | tail -3
        make install 2>&1 | tail -3
    ) || die "readline 构建失败"
}

# ---------------------------------------------------------------- 6. xz (liblzma)
build_xz() {
    local ver="5.4.4"
    local dir="xz-$ver"
    if [ -f "$PREFIX/lib/liblzma.a" ]; then
        log "xz 已存在，跳过"
        return
    fi
    fetch_and_extract "https://tukaani.org/xz/$dir.tar.gz" "$dir"
    log "配置 xz $ver"
    (
        cd "$WORK/$dir"
        ./configure --host=arm-apple-darwin \
            --prefix="$PREFIX" \
            --enable-static --disable-shared --disable-xz --disable-xzdec \
            --disable-lzmadec --disable-lzmainfo --disable-scripts \
            --disable-doc 2>&1 | tail -3
        log "编译 xz"
        make -j"$JOBS" 2>&1 | tail -3
        make install 2>&1 | tail -3
    ) || die "xz 构建失败"
}

# ---------------------------------------------------------------- 7. sqlite3
build_sqlite() {
    local ver="3440200"  # 3.44.2
    local dir="sqlite-autoconf-$ver"
    if [ -f "$PREFIX/lib/libsqlite3.a" ]; then
        log "sqlite 已存在，跳过"
        return
    fi
    fetch_and_extract "https://www.sqlite.org/2023/sqlite-autoconf-$ver.tar.gz" "$dir"
    log "配置 sqlite $ver"
    (
        cd "$WORK/$dir"
        ./configure --host=arm-apple-darwin \
            --prefix="$PREFIX" \
            --enable-static --disable-shared 2>&1 | tail -3
        log "编译 sqlite"
        make -j"$JOBS" 2>&1 | tail -3
        make install 2>&1 | tail -3
    ) || die "sqlite 构建失败"
}

# ---------------------------------------------------------------- 执行
log "构建依赖到 $PREFIX"
build_openssl
build_ncurses
build_libffi
build_readline
build_xz
build_sqlite

log "完成。产物："
ls -1 "$PREFIX/lib"/*.a 2>/dev/null | sed 's|^|  |' || log "  (无静态库)"
