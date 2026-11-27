# Python for iOS（rootless 越狱）

原生 CPython + pip，为越狱 iOS/iPadOS 交叉编译，打成 rootless `.deb`。

包名：`com.twoyears.python`

## 为什么独立仓库

DeepSeek Harness 的 macOS 版内置 Python 3.12，但那是 **macOS 平台**的二进制
（`platform=1`），且 numpy/pandas/lxml/PIL 共 79 个 `.so` 全是 darwin 编译。
iOS 上跑不了，必须单独交叉编译。

独立仓库的好处：编译要 30-60 分钟，不拖累主项目的 CI。

## 安装后

```sh
python3 -V        # Python 3.15.x
pip3 --version    # pip 26.x
python3 -c "import ssl, sqlite3, zlib, lzma, bz2, curses, readline, ctypes"
```

路径：

```
/var/jb/usr/local/python3.15/bin/python3.15   真正的可执行文件
/var/jb/usr/local/python3.15/lib/python3.15/  stdlib + site-packages
/var/jb/usr/local/bin/python3                软链
```

## 依赖

`Depends: firmware (>= 15.0), ca-certificates`

就这两个。**没有** libssl / libncursesw / libffi / liblzma / libreadline /
libsqlite3 / libgdbm / libzstd / libbrotli —— 它们全是静态链进去的：

```
$ otool -L var/jb/usr/local/python3.15/bin/python3.15
  /System/Library/Frameworks/CoreFoundation.framework/CoreFoundation
  /usr/lib/libSystem.B.dylib
```

（之前 control 里写了 9 个系统库依赖，是错的。多写的依赖只会让
Sileo 因为「找不到某个包」直接拒绝安装。）

`ca-certificates` 留着是因为 ssl 要读系统 CA 才能校验 HTTPS。

## 构建

CI 用 `macos-15` runner（自带 iOS SDK）：

```sh
# 1. 构建 iOS 原生依赖（openssl/ncurses/libffi/readline/xz/sqlite）
JOBS=$(sysctl -n hw.ncpu) bash sources/build-deps.sh \
    "$(xcrun --sdk iphoneos --show-sdk-path)" "$PWD/deps"

# 2. 交叉编译 CPython
python3 sources/build.py --sdk "$SDK" --deps "$PWD/deps" --jobs 8

# 3. 打包
bash sources/package.sh "$(cat work/stage-path.txt)" \
    dist/com.twoyears.python_3.15.2-1+rootless_iphoneos-arm64.deb
```

## 技术要点

### 两阶段编译

CPython 构建过程本身要跑 Python，目标平台跑不了，所以：

1. **阶段 1**：在 macOS 上编一个 host python
2. **阶段 2**：用它作 `--with-build-python`，交叉编译 arm64-apple-ios

### 关键 configure 参数

```
--host=arm-apple-darwin
--with-build-python=<host>/python.exe
--with-openssl=<deps>
--without-ensurepip          # pip 用 wheel 直接塞
--disable-ipv6
--without-system-libmpdec
```

编译器：`clang -target arm64-apple-ios15.0 -isysroot <iPhoneOS.sdk>`

### iOS 探测修正

configure 在 iOS 上会误判，需要显式覆盖：

```sh
ac_cv_func_getentropy=no      # iOS 无
ac_cv_func_system=no          # iOS 沙箱禁
ac_cv_func_sendfile=no        # iOS 无
ac_cv_func_clock_settime=no   # iOS 禁
ac_cv_file__dev_ptmx=yes
ac_cv_file__dev_ptc=no
```

### 禁用 _scproxy

macOS 专属的代理探测模块。iOS SDK 把
`kSCPropNetProxiesGopherProxy` / `kSCPropNetProxiesSOCKS*`
全标成 `API_UNAVAILABLE(ios)`，编到就硬报错。

只写 `Modules/Setup.local` 的 `*disabled*` **不可靠** ——
实测同一份代码一次生效一次不生效。`Modules/Setup.stdlib` 才是
makesetup 生成 `Modules/Setup` 的来源，所以两处都改。

最后仍然会被编到，于是把 `Modules/_scproxy.c` 直接换成空模块：
能编过，`from _scproxy import _get_proxy_settings` 拿不到符号，
urllib 回退到读环境变量 —— 与「模块不存在」行为一致。

### 不构建 ncurses

iOS 的 `<termios.h>` 把 `B0`/`B50`/…/`B38400` 这些波特率常量裁掉了，
ncurses 的 `tinfo/lib_baudrate.c` 用它们建 speed 表，直接
`error: use of undeclared identifier 'B0'`。

试过两种补法（`CFLAGS=-include`、把常量块插进源码），行号都位移了、
说明改动确实生效，但 B0 依然 undeclared，说明还有别的编译路径在起作用。
继续折腾收益极低，因为**没有依赖方**：

- readline 8.x 自带 termcap 回退，不链 ncurses 也能编（实测通过）
- Python 3.15 的交互式行编辑用内置 `_pyrepl`，不需要 GNU readline
- `curses` 模块本来就在 build.py 里禁用（agent 用不到）

所以 `build-deps.sh` 里只留一个 `check_ncurses`，把结论打印出来。

## 已知限制

- **没有 `import readline`**（`libreadline.a` 编出来了，但 CPython 的
  `Modules/readline.c` 没链上）。不影响使用：Python 3.13+ 的交互式行编辑
  由内置 `_pyrepl` 提供，`input()` 也走它。
- **没有 `curses`**（见上「不构建 ncurses」）
- **`_scproxy` 是空模块**（见上「禁用 _scproxy」），
  `urllib` 的代理探测回退到读环境变量

- **numpy / pandas / lxml 需要单独编译**（它们是 C 扩展，不是纯 Python）
- 纯 Python 包（`pip install requests` 等）可以直接用
- 交叉编译的扩展模块需要 `-bundle -bundle_loader` 链接，普通 `pip install` 编不了

## 结构

```
sources/
├── build.py          交叉编译 CPython（两阶段）
├── build-deps.sh     构建 iOS 原生依赖
├── package.sh        打包 deb
├── mkdeb.py          纯 Python deb 生成器
└── verify-deb.py     deb 校验
control               DEBIAN/control 模板
```
