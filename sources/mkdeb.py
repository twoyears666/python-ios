#!/usr/bin/env python3
"""
mkdeb.py —— 纯 Python 生成 .deb（ar + tar.gz），不依赖 dpkg-deb。

deb 格式 = ar 归档，成员顺序固定：
    debian-binary      "2.0\n"
    control.tar.gz     DEBIAN/ 下所有控制文件
    data.tar.gz        实际文件系统树

用法：
    mkdeb.py <root-dir> <output.deb> [--compression gzip|xz|zstd]

root-dir 下应包含：
    DEBIAN/control, DEBIAN/postinst, DEBIAN/prerm, ...
    以及要安装的文件树（如 var/jb/...）
"""
import io
import os
import sys
import tarfile
import gzip
import lzma
import stat
import time

AR_MAGIC = b"!<arch>\n"


def _ar_member(name: str, data: bytes, mtime: int, mode: int = 0o100644) -> bytes:
    """构造一个 ar 成员。名字需 <=16 字节且以 / 结尾。"""
    if len(name) > 16:
        raise ValueError(f"ar 成员名过长: {name}")
    # 注意：不能给名字加 "/" 后缀。GNU ar 用 "/" 标记长文件名，
    # 但 BSD ar 与 iOS 的 dpkg/Sileo 解析器不认这个约定，
    # 会把成员名读成 "debian-binary/" 从而报 "cannot read"。
    hdr = (
        name.ljust(16).encode("ascii")
        + str(mtime).ljust(12).encode("ascii")
        + str(0).ljust(6).encode("ascii")      # uid
        + str(0).ljust(6).encode("ascii")      # gid
        + oct(mode)[2:].ljust(8).encode("ascii")
        + str(len(data)).ljust(10).encode("ascii")
        + b"`\n"
    )
    body = data
    if len(body) % 2 == 1:                      # ar 要求 2 字节对齐
        body += b"\n"
    return hdr + body


def _tar_bytes(root: str, include: str | None = None, mtime: int = 0) -> bytes:
    """把 root 打包成 tar.gz，保留权限位（关键：可执行位与符号链接）。"""
    buf = io.BytesIO()
    # 用 gzip 包裹，mtime 固定以保证可复现
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=mtime, compresslevel=9) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tf:
            entries = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                filenames.sort()
                rel_dir = os.path.relpath(dirpath, root)
                if rel_dir == ".":
                    rel_dir = ""
                if include is not None:
                    if rel_dir and not (rel_dir == include or rel_dir.startswith(include + os.sep)):
                        continue
                if rel_dir:
                    entries.append(("d", rel_dir, os.path.join(root, rel_dir)))
                for fn in filenames:
                    rel = os.path.join(rel_dir, fn) if rel_dir else fn
                    if include is not None and not (rel == include or rel.startswith(include + os.sep)):
                        continue
                    entries.append(("f", rel, os.path.join(root, rel)))

            # 目录在前，保证解包顺序
            for kind, rel, full in sorted(entries, key=lambda e: (e[1].count(os.sep), e[1])):
                st = os.lstat(full)
                ti = tarfile.TarInfo(name="./" + rel.replace(os.sep, "/"))
                ti.mtime = mtime
                ti.uid = 0
                ti.gid = 0
                ti.uname = "root"
                ti.gname = "wheel"
                if kind == "d" or stat.S_ISDIR(st.st_mode):
                    ti.type = tarfile.DIRTYPE
                    ti.mode = 0o755
                    ti.size = 0
                    tf.addfile(ti)
                elif stat.S_ISLNK(st.st_mode):
                    ti.type = tarfile.SYMTYPE
                    ti.linkname = os.readlink(full)
                    ti.mode = 0o777
                    ti.size = 0
                    tf.addfile(ti)
                elif stat.S_ISREG(st.st_mode):
                    ti.type = tarfile.REGTYPE
                    ti.mode = st.st_mode & 0o7777
                    ti.size = st.st_size
                    with open(full, "rb") as fh:
                        tf.addfile(ti, fh)
                else:
                    continue
    return buf.getvalue()


def build(root: str, out: str, compression: str = "xz") -> None:
    if not os.path.isdir(os.path.join(root, "DEBIAN")):
        raise SystemExit(f"缺少 {root}/DEBIAN 目录")

    # 防呆：deb 的根下只应有 DEBIAN/ 和文件系统树（通常是 var/）。
    # 出现裸文件或意料外的顶层目录，多半是测试目录被污染了。
    allowed = {"DEBIAN", "var", "usr", "etc", "Library", "Applications", "System", "private"}
    unexpected = [e for e in os.listdir(root)
                  if e not in allowed and not e.startswith(".")]
    if unexpected:
        raise SystemExit(
            f"{root} 下有意料外的顶层条目: {unexpected}\n"
            f"  deb 根只应有 DEBIAN/ 和文件系统树（var/usr/etc/...）。\n"
            f"  请检查构建目录是否被污染。")

    control = os.path.join(root, "DEBIAN")
    for req in ("control",):
        if not os.path.isfile(os.path.join(control, req)):
            raise SystemExit(f"缺少 {control}/{req}")

    # ---- control.tar ----
    # 规范：control.tar 里直接是 ./control、./postinst、./md5sums、./，
    # 不带 DEBIAN/ 前缀（DEBIAN/ 只是构建目录名）。
    # 带前缀会让 dpkg/Sileo 找不到 ./control → "could not be read"。
    import hashlib

    ctrl_buf = io.BytesIO()
    if compression == "gzip":
        ctrl_comp = gzip.GzipFile(fileobj=ctrl_buf, mode="wb", mtime=0, compresslevel=9)
    else:
        ctrl_comp = lzma.LZMAFile(ctrl_buf, mode="wb", preset=9)

    with ctrl_comp as comp:
        with tarfile.open(fileobj=comp, mode="w", format=tarfile.GNU_FORMAT) as tf:
            # 根目录项（官方 deb 有，Sileo 兼容性更好）
            root_ti = tarfile.TarInfo(name="./")
            root_ti.type = tarfile.DIRTYPE
            root_ti.mode = 0o755
            root_ti.mtime = 0
            root_ti.uid = root_ti.gid = 0
            root_ti.uname = "root"
            root_ti.gname = "root"
            tf.addfile(root_ti)

            names = sorted(os.listdir(control))
            for fn in names:
                full = os.path.join(control, fn)
                if not os.path.isfile(full):
                    continue
                st = os.lstat(full)
                ti = tarfile.TarInfo(name="./" + fn)
                ti.mtime = 0
                ti.uid = ti.gid = 0
                ti.uname = "root"
                ti.gname = "root"
                ti.mode = st.st_mode & 0o7777
                ti.size = st.st_size
                with open(full, "rb") as fh:
                    tf.addfile(ti, fh)

            # md5sums（官方 deb 都有；dpkg 用它校验完整性）
            if "md5sums" not in names:
                md5_lines = []
                for dirpath, _, filenames in os.walk(root):
                    rel_dir = os.path.relpath(dirpath, root)
                    if rel_dir == ".":
                        rel_dir = ""
                    # 跳过 DEBIAN/
                    if rel_dir == "DEBIAN" or rel_dir.startswith("DEBIAN" + os.sep):
                        continue
                    for f in sorted(filenames):
                        full = os.path.join(dirpath, f)
                        if os.path.islink(full) or not os.path.isfile(full):
                            continue
                        rel = (rel_dir + "/" + f) if rel_dir else f
                        h = hashlib.md5()
                        with open(full, "rb") as fh:
                            for chunk in iter(lambda: fh.read(1 << 20), b""):
                                h.update(chunk)
                        md5_lines.append(f"{h.hexdigest()}  {rel}")
                md5_data = ("\n".join(md5_lines) + "\n").encode()
                ti = tarfile.TarInfo(name="./md5sums")
                ti.mtime = 0
                ti.uid = ti.gid = 0
                ti.uname = "root"
                ti.gname = "root"
                ti.mode = 0o644
                ti.size = len(md5_data)
                tf.addfile(ti, io.BytesIO(md5_data))

    ctrl_data = ctrl_buf.getvalue()

    # data.tar.* 排除 DEBIAN/
    data_buf = io.BytesIO()
    if compression == "gzip":
        opener = lambda f: gzip.GzipFile(fileobj=f, mode="wb", mtime=0, compresslevel=9)
    elif compression == "xz":
        opener = lambda f: lzma.LZMAFile(f, mode="wb", preset=9)
    else:
        raise SystemExit(f"不支持的压缩: {compression}")

    with opener(data_buf) as comp:
        with tarfile.open(fileobj=comp, mode="w", format=tarfile.GNU_FORMAT) as tf:
            entries = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                filenames.sort()
                if dirpath == root:
                    dirnames[:] = [d for d in dirnames if d != "DEBIAN"]
                rel_dir = os.path.relpath(dirpath, root)
                if rel_dir == ".":
                    rel_dir = ""
                if rel_dir:
                    entries.append(("d", rel_dir, dirpath))
                for fn in filenames:
                    rel = os.path.join(rel_dir, fn) if rel_dir else fn
                    entries.append(("f", rel, os.path.join(root, rel)))
            for kind, rel, full in sorted(entries, key=lambda e: (e[1].count(os.sep), e[1])):
                st = os.lstat(full)
                ti = tarfile.TarInfo(name="./" + rel.replace(os.sep, "/"))
                ti.mtime = 0
                ti.uid = ti.gid = 0
                ti.uname = "root"
                ti.gname = "wheel"
                if stat.S_ISDIR(st.st_mode):
                    ti.type = tarfile.DIRTYPE
                    ti.mode = 0o755
                    ti.size = 0
                    tf.addfile(ti)
                elif stat.S_ISLNK(st.st_mode):
                    ti.type = tarfile.SYMTYPE
                    ti.linkname = os.readlink(full)
                    ti.mode = 0o777
                    ti.size = 0
                    tf.addfile(ti)
                elif stat.S_ISREG(st.st_mode):
                    ti.type = tarfile.REGTYPE
                    ti.mode = st.st_mode & 0o7777
                    ti.size = st.st_size
                    with open(full, "rb") as fh:
                        tf.addfile(ti, fh)
    data_data = data_buf.getvalue()

    suffix = {"gzip": "gz", "xz": "xz"}[compression]
    now = int(time.time())
    members = [
        _ar_member("debian-binary", b"2.0\n", now),
        _ar_member(f"control.tar.{suffix}", ctrl_data, now),
        _ar_member(f"data.tar.{suffix}", data_data, now),
    ]
    with open(out, "wb") as fh:
        fh.write(AR_MAGIC)
        for m in members:
            fh.write(m)

    total = os.path.getsize(out)
    print(f"[mkdeb] {out}")
    print(f"[mkdeb]   control.tar.{suffix}: {len(ctrl_data):,} B")
    print(f"[mkdeb]   data.tar.{suffix}:    {len(data_data):,} B")
    print(f"[mkdeb]   总计:             {total:,} B ({total/1048576:.2f} MiB)")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    root, out = sys.argv[1], sys.argv[2]
    comp = "xz"
    if "--compression" in sys.argv:
        comp = sys.argv[sys.argv.index("--compression") + 1]
    build(root, out, comp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
