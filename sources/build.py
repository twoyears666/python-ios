#!/usr/bin/env python3
"""
build.py —— 交叉编译 CPython for iOS（rootless 越狱）

## 为什么需要两阶段

CPython 的构建过程本身需要跑 Python（生成解析器、冻结模块等）。
目标平台是 iOS，不能直接跑，所以：

  阶段 1：在宿主机（macOS runner）编一个 host python
  阶段 2：用 host python 作为 --with-build-python，
          交叉编译出 arm64-apple-ios 的 python

## 关键参数

  --host=arm-apple-darwin          目标三元组
  --build=<host triplet>           构建机三元组（config.guess 得出）
  --with-build-python=<host>/python.exe
  --with-openssl=<deps>            iOS 原生 openssl
  --without-ensurepip              自己塞 pip wheel

  编译器：clang -target arm64-apple-ios15.0 -isysroot <iPhoneOS.sdk>

## 交叉编译时禁用的探测

iOS 上这些系统调用不存在或被沙箱禁止，configure 探测会误判：

  ac_cv_func_getentropy=no      iOS 无
  ac_cv_func_system=no          iOS 沙箱禁
  ac_cv_func_sendfile=no        iOS 无
  ac_cv_func_clock_settime=no   iOS 禁
  ac_cv_file__dev_ptmx=yes      有
  ac_cv_file__dev_ptc=no        无

## 用法

  python3 build.py --sdk <path> --deps <prefix> [--jobs N] [--skip-build]

  --sdk   iPhoneOS SDK 路径（xcrun --sdk iphoneos --show-sdk-path）
  --deps  iOS 原生依赖前缀（含 include/ 和 lib/）
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = '3.15.2'
PIP_VERSION = '26.3'
SOURCE_URL = f'https://www.python.org/ftp/python/{VERSION}/Python-{VERSION}.tar.xz'
SOURCE_SHA256 = ''  # 首次运行后填入，见 --print-sha

PREFIX = '/var/jb/usr/local/python3.15'
IOS_TARGET = 'arm64-apple-ios15.0'
MIN_IOS = '15.0'

ROOT = Path(__file__).resolve().parent
WORK = ROOT / 'work'


def log(msg):
    print(f'\033[1;34m[build]\033[0m {msg}', flush=True)


def die(msg):
    print(f'\033[1;31m[err]\033[0m {msg}', file=sys.stderr)
    sys.exit(1)


def execute(argv, cwd=None, env=None, logfile=None):
    """跑一个命令，输出同时写日志文件。"""
    log(' '.join(str(a) for a in argv[:3]) + (' …' if len(argv) > 3 else ''))
    out = open(logfile, 'w') if logfile else None
    try:
        proc = subprocess.run(
            [str(a) for a in argv],
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=out or subprocess.PIPE,
            stderr=subprocess.STDOUT if out else subprocess.PIPE,
            text=out is None,
        )
        if proc.returncode != 0:
            detail = '' if out else (proc.stdout or '')[-3000:]
            tail = ''
            if logfile:
                try:
                    content = Path(logfile).read_text(errors='replace')
                    tail = '\n--- 日志尾部（' + str(logfile) + '）---\n' + content[-6000:]
                except Exception:
                    pass
            die(f'命令失败（{proc.returncode}）：{" ".join(str(a) for a in argv[:4])}\n{detail}{tail}')
        return proc
    finally:
        if out:
            out.close()


def fetch(url, dest):
    """下载并校验。"""
    if dest.exists():
        log(f'已存在，跳过下载：{dest.name}')
        return
    log(f'下载 {url}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as r, open(dest, 'wb') as f:
        shutil.copyfileobj(r, f)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sdk', required=True, help='iPhoneOS SDK 路径')
    ap.add_argument('--deps', required=True, help='iOS 原生依赖前缀')
    ap.add_argument('--jobs', type=int, default=os.cpu_count() or 4)
    ap.add_argument('--skip-build', action='store_true', help='只打包，跳过编译')
    ap.add_argument('--print-sha', action='store_true', help='只下载并打印源码 SHA256')
    args = ap.parse_args()

    sdk = Path(args.sdk)
    deps = Path(args.deps)
    if not sdk.is_dir():
        die(f'SDK 不存在：{sdk}')
    if not deps.is_dir():
        die(f'依赖前缀不存在：{deps}')

    WORK.mkdir(exist_ok=True)

    # ---------------------------------------------------------------- 1. 源码
    archive = WORK / f'Python-{VERSION}.tar.xz'
    fetch(SOURCE_URL, archive)
    actual = sha256(archive)
    if args.print_sha:
        print(f'SOURCE_SHA256 = "{actual}"')
        return
    if SOURCE_SHA256 and actual != SOURCE_SHA256:
        die(f'源码 SHA256 不匹配\n  期望 {SOURCE_SHA256}\n  实际 {actual}')
    log(f'源码 SHA256 {actual[:16]}… 校验通过')

    src = WORK / f'Python-{VERSION}'
    if not src.is_dir():
        log('解包源码')
        with tarfile.open(archive) as t:
            t.extractall(WORK, filter='data')

    # ---------------------------------------------------------------- 2. pip wheel
    stdlib_site = None  # 稍后确定
    pip_wheel = WORK / f'pip-{PIP_VERSION}-py3-none-any.whl'
    if not pip_wheel.exists():
        log(f'下载 pip {PIP_VERSION}')
        subprocess.run([sys.executable, '-m', 'pip', 'download',
                        f'pip=={PIP_VERSION}', '--no-deps', '--only-binary=:all:',
                        '-d', str(WORK)], check=True)

    # ---------------------------------------------------------------- 3. 交叉编译环境
    compiler = f'clang -target {IOS_TARGET} -isysroot {sdk} -miphoneos-version-min={MIN_IOS}'
    native_env = dict(
        os.environ,
        CC=compiler,
        CFLAGS='-O2 -g0',
        CPPFLAGS=f'-I{deps}/include -I{deps}/include/ncursesw',
        LDFLAGS=f'-L{deps}/lib -Wl,-rpath,/var/jb/usr/lib',
        PKG_CONFIG='/usr/bin/false',
        LIBFFI_CFLAGS=f'-I{deps}/include',
        LIBFFI_LIBS=f'-L{deps}/lib -lffi',
        # iOS 上不存在的系统调用：让 configure 别误判
        ac_cv_file__dev_ptmx='yes',
        ac_cv_file__dev_ptc='no',
        ac_cv_func_getentropy='no',
        ac_cv_func_system='no',
        ac_cv_func_sendfile='no',
        ac_cv_func_clock_settime='no',
        # 让 curses 探测失败 —— ncurses 在 iOS 上编不出来，
        # 但它的残缺头文件还在 $PREFIX/include 里，configure 会误判为可用，
        # 导致 _cursesmodule 编译时 WINDOW/PANEL 未定义。
        # 这两个变量让 configure 认为 curses 不可用，跳过该模块。
        ac_cv_header_curses_h='no',
        ac_cv_header_ncurses_h='no',
        ac_cv_lib_ncursesw_initscr='no',
        ac_cv_lib_ncurses_initscr='no',
        ac_cv_lib_panel_new_panel='no',
        ac_cv_search_initscr='no',
    )

    host = WORK / 'host'
    ios = WORK / 'ios'
    host.mkdir(exist_ok=True)
    ios.mkdir(exist_ok=True)

    shared_link = compiler + f' -bundle -bundle_loader {ios}/python.exe'
    make_args = ['make', f'-j{args.jobs}', 'LDSHARED=' + shared_link]

    if not args.skip_build:
        # ---- 阶段 1：host python ----
        if not (host / 'Makefile').exists():
            log('阶段 1/2：配置 host python')
            execute([src / 'configure',
                     f'--prefix={WORK}/host-install',
                     '--without-ensurepip',
                     '--disable-test-modules'], host, logfile=WORK / 'host-configure.log')
        log('阶段 1/2：编译 host python')
        execute(['make', f'-j{args.jobs}'], host, logfile=WORK / 'host-build.log')

        build_triplet = subprocess.check_output(
            [str(src / 'config.guess')], text=True).strip()
        log(f'构建机三元组 {build_triplet}')

        # ---- 阶段 2：交叉编译 iOS ----
        log('阶段 2/2：配置 iOS python')
        execute([src / 'configure', '-C',
                 '--host=arm-apple-darwin',
                 f'--build={build_triplet}',
                 f'--with-build-python={host}/python.exe',
                 f'--prefix={PREFIX}',
                 '--without-ensurepip',
                 '--disable-ipv6',
                 '--disable-test-modules',
                 '--without-system-libmpdec',
                 f'--with-openssl={deps}'], ios, native_env,
                logfile=WORK / 'ios-configure.log')

        # 禁用 iOS 上编不过的模块
        #
        # _scproxy    macOS 专属（代理探测）。iOS SDK 里
        #             kSCPropNetProxiesGopherProxy / SOCKS* 都标了
        #             API_UNAVAILABLE(ios)，编到就硬报错。
        # _curses*    ncurses 在 iOS 上编不出来，且 agent 用不到。
        #
        # 只写 Modules/Setup.local 的 *disabled* 不够可靠：实测同样的
        # 写法出现过一次生效、一次不生效（_scproxy 还是被编了）。
        # Modules/Setup.stdlib 才是 makesetup 生成 Modules/Setup 的来源，
        # 所以两处都改，并且把 Setup.stdlib 里对应的行直接注释掉。
        needed = ['_scproxy', '_curses', '_curses_panel']

        setup_local = ios / 'Modules/Setup.local'
        setup_local.touch(exist_ok=True)
        text = setup_local.read_text()
        missing = [m for m in needed if f'\n{m}\n' not in text]
        if missing:
            with open(setup_local, 'a') as f:
                f.write('\n*disabled*\n' + '\n'.join(missing) + '\n')
            log(f'禁用模块（Setup.local）: {", ".join(missing)}')

        # 注意：ios 是 out-of-tree 构建目录，这里的 Setup.stdlib 是
        # configure 生成的副本，make 有可能按时间戳重新生成、把改动冲掉。
        # 所以它只能算兜底，真正保证 _scproxy 编得过的是下面换空实现。
        stdlib = ios / 'Modules/Setup.stdlib'
        if stdlib.is_file():
            out, changed = [], []
            for line in stdlib.read_text().splitlines(keepends=True):
                stripped = line.strip()
                name = stripped.split(None, 1)[0] if stripped else ''
                if name in needed and not stripped.startswith('#'):
                    out.append('# iOS 禁用: ' + line)
                    changed.append(name)
                else:
                    out.append(line)
            if changed:
                stdlib.write_text(''.join(out))
                log(f'禁用模块（Setup.stdlib）: {", ".join(changed)}')

        # _scproxy 换成空实现。
        #
        # 它是 macOS 专属的代理探测模块，iOS SDK 把
        # kSCPropNetProxiesGopherProxy / SOCKS* 全标成 API_UNAVAILABLE(ios)，
        # 编到就硬报错。上面 Setup.local + Setup.stdlib 两处禁用都试过，
        # 这个模块依然会被编 —— CPython 的模块清单不止一处来源。
        #
        # 换成空模块最省事：能编过，urllib 里
        # `from _scproxy import _get_proxy_settings` 拿不到符号，
        # 会回退到读环境变量，和「模块不存在」的行为一致。
        # 注意：必须改**源码树** src 里的文件。
        # ios 是 out-of-tree 构建目录，Modules/_scproxy.c 是从 srcdir 编的；
        # 之前写成 ios/Modules/_scproxy.c，那里根本没有这个文件，
        # 于是什么都没改，编译时用的还是原版。
        scproxy = src / 'Modules/_scproxy.c'
        if scproxy.is_file() and 'iOS 空实现' not in scproxy.read_text(errors='ignore'):
            scproxy.write_text(
                '/* iOS 空实现：iOS SDK 不支持 SystemConfiguration 的代理探测。\n'
                '   原文件是 macOS 专属，见 build.py 里的说明。 */\n'
                '#include "Python.h"\n\n'
                'static struct PyModuleDef scproxy_module = {\n'
                '    PyModuleDef_HEAD_INIT, "_scproxy", NULL, -1, NULL\n'
                '};\n\n'
                'PyMODINIT_FUNC\nPyInit__scproxy(void)\n{\n'
                '    return PyModule_Create(&scproxy_module);\n}\n'
            )
            log('_scproxy.c 已替换为空实现')

        log('阶段 2/2：编译 iOS python')
        execute(make_args, ios, native_env, logfile=WORK / 'ios-build.log')

    # ---------------------------------------------------------------- 4. 打包
    log('安装到 staging')
    stage = WORK / 'pkg'
    if stage.exists():
        shutil.rmtree(stage)
    execute([*make_args, f'DESTDIR={stage}', 'altinstall'], ios, native_env,
            logfile=WORK / 'ios-install.log')

    pyroot = stage / PREFIX.lstrip('/')
    stdlib = pyroot / f'lib/python{VERSION[:4]}'
    if not stdlib.is_dir():
        die(f'stdlib 目录不存在：{stdlib}')

    site = stdlib / 'site-packages'
    site.mkdir(exist_ok=True)
    log(f'嵌入 pip {PIP_VERSION}')
    with zipfile.ZipFile(pip_wheel) as z:
        z.extractall(site)

    # ---------------------------------------------------------------- 5. 包装
    bin_dir = pyroot / 'bin'
    bin_dir.mkdir(parents=True, exist_ok=True)

    # python3 / python3.15 软链
    real = bin_dir / f'python{VERSION[:4]}'
    if not real.exists():
        # altinstall 可能生成 python3.15
        cand = list(bin_dir.glob('python3.*'))
        if cand:
            real = cand[0]
        else:
            die('找不到 python 可执行文件')

    for name in ('python3', 'python'):
        link = bin_dir / name
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(real.name)

    # pip 启动脚本
    pip_sh = bin_dir / 'pip3'
    pip_sh.write_text(f'''#!/bin/sh
exec {PREFIX}/bin/python3 -m pip "$@"
''')
    pip_sh.chmod(0o755)

    # ---------------------------------------------------------------- 6. 报告
    size = sum(f.stat().st_size for f in pyroot.rglob('*') if f.is_file())
    log(f'staging 完成：{size / 1048576:.1f} MiB')
    log(f'  {pyroot}')
    log(f'  可执行：{[p.name for p in bin_dir.iterdir() if p.is_file() or p.is_symlink()]}')

    # 输出给后续打包步骤
    (WORK / 'stage-path.txt').write_text(str(stage))


if __name__ == '__main__':
    main()
