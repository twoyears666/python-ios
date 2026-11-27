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
set -uo pipefail
# 注意：不开 set -e。可选模块（ncurses/readline）失败要能继续，
# set -e 会让子 shell 里的 make 失败直接退出整个脚本，|| 捕获不到。

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
# 不自己编！用 Procursus 的 libssl-dev（5.2 MB，含 139 个头文件 + libssl.a/libcrypto.a）
# 好处：
#   1. 版本与运行时库一致（3.2.1），不会 ABI 不匹配
#   2. 省掉 10 分钟编译
#   3. iOS 交叉编译 openssl 本身坑很多（ucontext/asm/dso）
build_openssl_from_procursus() {
    local url="https://apt.procurs.us/pool/main/iphoneos-arm64/1700/openssl/libssl-dev_3.2.1_iphoneos-arm.deb"
    if [ -f "$PREFIX/lib/libssl.a" ]; then
        log "OpenSSL 已存在，跳过"
        return
    fi
    local deb="$WORK/libssl-dev.deb"
    if [ ! -f "$deb" ]; then
        log "下载 Procursus libssl-dev"
        curl -sSL --retry 3 -m 300 -o "$deb" "$url" || die "下载 libssl-dev 失败"
    fi

    log "解包 libssl-dev"
    local extract="$WORK/libssl-dev"
    rm -rf "$extract"; mkdir -p "$extract"
    (
        cd "$extract"
        ar x "$deb" 2>/dev/null || die "ar 解包失败"
        local dataf
        dataf=$(ls data.tar.* 2>/dev/null | head -1)
        [ -n "$dataf" ] || die "找不到 data.tar.*"
        case "$dataf" in
            *.zst) zstd -dc "$dataf" | tar xf - ;;
            *.xz)  tar xJf "$dataf" ;;
            *.gz)  tar xzf "$dataf" ;;
            *)     die "未知压缩格式：$dataf" ;;
        esac
    ) || die "解包 data.tar 失败"

    # 只拷头文件和静态库（不要 .dylib，避免和运行时冲突）
    log "安装头文件和静态库到 $PREFIX"
    mkdir -p "$PREFIX/include" "$PREFIX/lib"
    cp -R "$extract/usr/include/openssl" "$PREFIX/include/" 2>/dev/null || die "拷头文件失败"
    for lib in libssl.a libcrypto.a; do
        [ -f "$extract/usr/lib/$lib" ] && cp "$extract/usr/lib/$lib" "$PREFIX/lib/"
    done
    log "OpenSSL: $(ls -1 "$PREFIX/lib"/lib{ssl,crypto}.a 2>/dev/null | wc -l | tr -d ' ') 个静态库, $(ls -1 "$PREFIX/include/openssl"/*.h 2>/dev/null | wc -l | tr -d ' ') 个头文件"
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

    # ncurses 的 tinfo/lib_baudrate.c 在 iOS 上编不过：
    #   sys/ttydev.h      —— BSD 遗留头，iOS 没有
    #   sys/ioctl_compat.h —— sys/ioctl.h 内部引用的，iOS 也没有
    #
    # 补丁：把这两处 include 都替换成 iOS 有的头，并用 iOS 的 Bxxx 常量。
    local baud="$WORK/$dir/ncurses/tinfo/lib_baudrate.c"
    if [ -f "$baud" ]; then
        log "打补丁：lib_baudrate.c 适配 iOS"
        # 去掉 sys/ttydev.h（不存在）
        sed -i.bak 's|^#include <sys/ttydev.h>|/* iOS: 无 sys/ttydev.h */|' "$baud" 2>/dev/null || true
        # sys/ioctl.h 在 iOS 上会引 ioctl_compat.h，也不存在 → 整行去掉，
        # 只保留 sys/termios.h（iOS 有）
        sed -i.bak2 's|^#include <sys/ioctl.h>|#include <sys/termios.h>|' "$baud" 2>/dev/null || true
    fi

    # iOS 的 <termios.h> 不定义 B0/B50/.../B38400 这些波特率常量
    # （Darwin 桌面版有，iOS 版被裁掉了），而 ncurses 的
    # tinfo/lib_baudrate.c 用它们建 speed 表，缺一个就整表编不过。
    #
    # 用 -include 在编译前把常量补上，比 sed 改源码稳：
    # 不依赖 include 顺序，也不怕 ncurses 换版本后行号变化。
    local baudhdr="$WORK/ios-termios-baud.h"
    cat > "$baudhdr" <<'BAUD'
/* iOS <termios.h> 缺少的波特率常量，数值与 Darwin 桌面版一致。
   用 #ifndef 包住，真有的平台不会被覆盖。 */
#ifndef B0
#define B0      0
#define B50     50
#define B75     75
#define B110    110
#define B134    134
#define B150    150
#define B200    200
#define B300    300
#define B600    600
#define B1200   1200
#define B1800   1800
#define B2400   2400
#define B4800   4800
#define B9600   9600
#define B19200  19200
#define B38400  38400
#define B7200   7200
#define B14400  14400
#define B28800  28800
#define B57600  57600
#define B76800  76800
#define B115200 115200
#define B230400 230400
#endif
BAUD
    local ncflags="$CFLAGS -include $baudhdr"

    log "配置 ncurses $ver"
    if (
        cd "$WORK/$dir" || exit 1
        CFLAGS="$ncflags" ./configure --host=arm-apple-darwin \
            --prefix="$PREFIX" \
            --without-shared --without-debug --without-ada \
            --enable-widec --without-manpages --without-progs \
            --disable-db-install || exit 1
        log "编译 ncurses"
        make -j"$JOBS" || exit 1
        make install || exit 1
    ); then
        log "ncurses 构建成功"
    else
        log "⚠ ncurses 构建失败（curses 模块将不可用，其余不受影响）"
        # 清理可能装了一半的头文件，避免 CPython configure 误判为可用
        rm -rf "$PREFIX/include/ncurses" "$PREFIX/include/ncursesw" 2>/dev/null || true
        rm -f "$PREFIX/include/curses.h" "$PREFIX/include/panel.h" 2>/dev/null || true
        rm -f "$PREFIX/lib/libncursesw.a" "$PREFIX/lib/libpanel.a" 2>/dev/null || true
        log "  已清理残缺的 ncurses 头文件/库"
        return 0
    fi
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

    # libffi 的 configure 会调 `$CC -print-multi-os-directory`，clang 不支持。
    # ac_cv 变量覆盖不可靠（不同版本变量名不同），改用 CC 包装脚本：
    # 拦截这个选项，直接返回空。
    local ccwrap="$WORK/cc-libffi.sh"
    cat > "$ccwrap" <<WRAP
#!/bin/sh
for arg in "\$@"; do
    case "\$arg" in
        -print-multi-os-directory) exit 0 ;;
    esac
done
exec $CC "\$@"
WRAP
    chmod +x "$ccwrap"

    if (
        cd "$WORK/$dir" || exit 1
        CC="$ccwrap" \
        ./configure --host=arm-apple-darwin \
            --prefix="$PREFIX" \
            --enable-static --disable-shared || exit 1
        log "编译 libffi"
        make -j"$JOBS" || exit 1
        make install || exit 1
    ); then
        log "libffi 构建成功"
    else
        die "libffi 构建失败（ctypes 必需）"
    fi
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
    if (
        cd "$WORK/$dir" || exit 1
        # 避免和 ncurses 冲突：readline 自带的 termcap
        ./configure --host=arm-apple-darwin \
            --prefix="$PREFIX" \
            --with-curses --enable-static --disable-shared \
            bash_cv_wcwidth_broken=no || exit 1
        log "编译 readline"
        make -j"$JOBS" || exit 1
        make install || exit 1
    ); then
        log "readline 构建成功"
    else
        log "⚠ readline 构建失败（readline 模块将不可用）"
        return 0
    fi
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
            --disable-doc
        log "编译 xz"
        make -j"$JOBS"
        make install
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
    if (
        cd "$WORK/$dir" || exit 1
        # 只编静态库，不编 sqlite3 命令行 shell。
        # shell.c 用了 system()，iOS SDK 把该函数标记为 unavailable。
        ./configure --host=arm-apple-darwin \
            --prefix="$PREFIX" \
            --enable-static --disable-shared \
            --disable-readline \
            CFLAGS="-O2 -fPIC -DSQLITE_OMIT_LOAD_EXTENSION" || exit 1
        log "编译 sqlite（仅 libsqlite3.a，跳过 shell）"
        # 只构建库目标，不碰 sqlite3 可执行文件
        make -j"$JOBS" libsqlite3.la || exit 1
        make install-libLTLIBRARIES install-includeHEADERS || exit 1
    ); then
        log "sqlite 构建成功"
    else
        die "sqlite 构建失败（sqlite3 模块必需）"
    fi
}

# ---------------------------------------------------------------- 执行
log "构建依赖到 $PREFIX"
build_openssl_from_procursus
build_ncurses
build_libffi
build_readline
build_xz
build_sqlite

log "完成。静态库产物："
for lib in libssl.a libcrypto.a libffi.a liblzma.a libsqlite3.a libncursesw.a libreadline.a; do
    if [ -f "$PREFIX/lib/$lib" ]; then
        printf '  ✅ %s\n' "$lib"
    else
        printf '  ⚠ %s（缺失，对应模块不可用）\n' "$lib"
    fi
done
echo
log "必需：libssl.a libcrypto.a libffi.a liblzma.a libsqlite.a"
for req in libssl.a libcrypto.a libffi.a liblzma.a libsqlite3.a; do
    [ -f "$PREFIX/lib/$req" ] || die "缺少必需库：$req"
done
log "必需库齐全"
