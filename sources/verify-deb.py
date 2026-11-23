#!/usr/bin/env python3
"""
verify-deb.py —— 校验 dsh-ios deb 的结构与内容完整性。

用法：python3 verify-deb.py <path/to.deb>

检查项：
  1. ar 归档结构（debian-binary / control.tar.gz / data.tar.gz）
  2. control 必需字段
  3. 必需文件存在
  4. 零原生模块（.node / .dylib）
  5. 可执行位正确
  6. 无 macOS 残留（.app/Contents/MacOS、dsoffice 等）
"""
import io
import sys
import tarfile

REQUIRED_CONTROL = [
    "Package", "Version", "Architecture", "Maintainer", "Description",
]

# deb 内的路径都带 rootless 前缀 var/jb/
REQUIRED_FILES = [
    "var/jb/usr/bin/dsh",
    "var/jb/usr/bin/dsh-doctor",
    "var/jb/usr/bin/dsh-url",
    "var/jb/usr/lib/dsh/bin/node",
    "var/jb/usr/lib/dsh/ios-image-shim.mjs",
    "var/jb/usr/lib/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js",
    "var/jb/etc/dsh/dsh.env",
    "var/jb/Library/LaunchDaemons/com.twoyears.dsh.plist",
]

EXECUTABLES = [
    "var/jb/usr/bin/dsh",
    "var/jb/usr/bin/dsh-doctor",
    "var/jb/usr/bin/dsh-url",
    "var/jb/usr/lib/dsh/bin/node",
]

# GUI 相关（可选，缺了只警告）
OPTIONAL_APP = [
    "var/jb/Applications/DSH.app/DSH",
    "var/jb/Applications/DSH.app/Info.plist",
]

FORBIDDEN_SUFFIX = (".node", ".dylib", ".dll", ".so")


def fail(msg):
    print(f"  \033[31m✗\033[0m {msg}")
    return 1


def ok(msg):
    print(f"  \033[32m✓\033[0m {msg}")
    return 0


def iter_ar(data: bytes):
    """逐个产出 ar 归档的 (name, content)。"""
    if data[:8] != b"!<arch>\n":
        raise SystemExit("不是 ar 归档")
    pos = 8
    while pos + 60 <= len(data):
        hdr = data[pos:pos + 60]
        if hdr[58:60] != b"`\n":
            break  # 头部损坏或结束
        name = hdr[0:16].decode("ascii", "replace").strip().rstrip("/")
        size_field = hdr[48:58].decode("ascii", "replace").strip()
        try:
            size = int(size_field)
        except ValueError:
            break
        start = pos + 60
        yield name, data[start:start + size]
        # 数据 2 字节对齐
        pos = start + size + (size % 2)


def read_member(data: bytes, name: bytes) -> bytes:
    """从 ar 归档里取出一个成员的内容。"""
    want = name.decode("ascii").rstrip("/")
    for n, content in iter_ar(data):
        if n == want:
            return content
    raise SystemExit(f"ar 归档里找不到 {want}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    errors = 0

    print(f"=== 校验 {path} ===")
    with open(path, "rb") as fh:
        data = fh.read()

    # ---- 1. ar 结构 ----
    print("\n[1] ar 归档结构")
    if data[:8] != b"!<arch>\n":
        errors += fail("不是 ar 归档（缺少 !<arch> 魔数）")
        return 1
    errors += ok("ar 魔数正确")
    present = [n for n, _ in iter_ar(data)]
    for want in ("debian-binary", "control.tar", "data.tar"):
        hits = [n for n in present if n == want or n.startswith(want + ".")]
        if hits:
            errors += ok(f"含 {hits[0]}")
        else:
            errors += fail(f"缺少 {want}*")

    # ---- 2. control ----
    print("\n[2] control 字段")
    ctrl_name = next(n for n, _ in iter_ar(data) if n.startswith("control.tar"))
    ctrl_tar = read_member(data, ctrl_name.encode())
    ctrl = tarfile.open(fileobj=io.BytesIO(ctrl_tar), mode="r:*")
    # control.tar 内的路径不带 DEBIAN/ 前缀（规范就是 ./control）
    ctrl_member = None
    for cand in ("./control", "control", "./DEBIAN/control", "DEBIAN/control"):
        try:
            ctrl.extractfile(cand)
            ctrl_member = cand
            break
        except KeyError:
            continue
    if ctrl_member is None:
        errors += fail("control.tar 里没有 control 文件")
        return 1
    if ctrl_member in ("./DEBIAN/control", "DEBIAN/control"):
        errors += fail(f"control 路径不规范（{ctrl_member}），应为 ./control")
    else:
        errors += ok(f"control 路径规范（{ctrl_member}）")
    ctrl_text = ctrl.extractfile(ctrl_member).read().decode("utf-8")

    fields = {}
    for line in ctrl_text.splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()

    for f in REQUIRED_CONTROL:
        if f in fields:
            errors += ok(f"{f}: {fields[f][:60]}")
        else:
            errors += fail(f"缺少字段 {f}")

    if fields.get("Architecture") not in ("iphoneos-arm64", "iphoneos-arm"):
        errors += fail(f"Architecture 不是 iOS: {fields.get('Architecture')}")
    else:
        errors += ok("Architecture 是 iOS")

    # control 脚本可执行
    for script in ("postinst", "prerm"):
        found = None
        for cand in (f"./{script}", script, f"./DEBIAN/{script}"):
            try:
                found = ctrl.getmember(cand)
                break
            except KeyError:
                continue
        if found is None:
            errors += fail(f"缺少 {script}")
        elif found.mode & 0o111:
            errors += ok(f"{script} 可执行")
        else:
            errors += fail(f"{script} 不可执行 (mode={oct(found.mode)})")

    # ---- 3. data 内容 ----
    print("\n[3] data 内容")
    data_name = next(n for n, _ in iter_ar(data) if n.startswith("data.tar"))
    data_tar = read_member(data, data_name.encode())
    dt = tarfile.open(fileobj=io.BytesIO(data_tar), mode="r:*")
    members = {m.name.lstrip("./"): m for m in dt.getmembers()}
    names = list(members.keys())
    errors += ok(f"共 {len(names)} 个条目")

    for f in REQUIRED_FILES:
        if f in members:
            errors += ok(f)
        else:
            errors += fail(f"缺失 {f}")

    # ---- 4. 零原生模块 ----
    print("\n[4] 原生模块检查")
    native = [n for n in names if n.endswith(FORBIDDEN_SUFFIX)]
    if native:
        errors += fail(f"发现 {len(native)} 个原生模块:")
        for n in native[:5]:
            print(f"       {n}")
    else:
        errors += ok("0 个 .node/.dylib/.so")

    # ---- 5. 可执行位 ----
    print("\n[5] 可执行位")
    for f in EXECUTABLES:
        m = members.get(f)
        if not m:
            continue  # 上面已报缺失
        if m.mode & 0o111:
            errors += ok(f"{f} ({oct(m.mode)})")
        else:
            errors += fail(f"{f} 不可执行 (mode={oct(m.mode)})")

    # ---- 6. macOS 残留 ----
    print("\n[6] macOS 残留检查")
    macos = [n for n in names
             if ".app/Contents/MacOS/" in n
             or n.endswith("/dsoffice")
             or n.endswith("/spawn-helper")
             or "libreoffice-kit-darwin" in n]
    if macos:
        errors += fail(f"发现 {len(macos)} 个 macOS 残留:")
        for n in macos[:5]:
            print(f"       {n}")
    else:
        errors += ok("无 macOS 二进制残留")

    # ---- 7. GUI（可选）----
    print("\n[7] GUI 检查")
    has_app = all(f in members for f in OPTIONAL_APP)
    if has_app:
        errors += ok("原生 App 已包含")
        app_exe = members["var/jb/Applications/DSH.app/DSH"]
        if app_exe.mode & 0o111:
            errors += ok("DSH.app/DSH 可执行")
        else:
            errors += fail("DSH.app/DSH 不可执行")
    else:
        print("  \033[33m!\033[0m 原生 App 未包含（仅 CLI 包）")

    # ---- 汇总 ----
    print()
    if errors == 0:
        print("\033[32m=== 全部通过 ===\033[0m")
        return 0
    print(f"\033[31m=== {errors} 项失败 ===\033[0m")
    return 1


if __name__ == "__main__":
    sys.exit(main())
