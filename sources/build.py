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
            die(f'命令失败（{proc.returncode}）：{" ".join(str(a) for a in argv[:4])}\n{detail}')
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

        # iOS 没有 _scproxy（macOS 代理探测模块）
        setup_local = ios / 'Modules/Setup.local'
        setup_local.touch(exist_ok=True)
        text = setup_local.read_text()
        if '\n_scproxy\n' not in text:
            with open(setup_local, 'a') as f:
                f.write('\n*disabled*\n_scproxy\n')

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
