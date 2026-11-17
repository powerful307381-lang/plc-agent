# -*- coding: utf-8 -*-
"""
模块名称: package_skill
功能说明:
  1. 清理工作区内的 __pycache__、.pytest_cache 及临时编译缓存；
  2. 将 eplan-to-tia-lad 技能工程纯净打包为 dist/eplan-to-tia-lad-release.zip；
  3. 强制排除 .git/、__pycache__/、.pytest_cache/、dist/、test_out/、build/、.venv/、venv/
     以及 *.pyc、*.pyo、*.tmp、*.bak、*.zap* 等非发布文件；
  4. 所有归档路径统一使用 POSIX 斜杠（/）与 UTF-8 文件名编码。
"""

import argparse
import fnmatch
import io
import os
import shutil
import sys
import zipfile
from pathlib import Path
from typing import List, Tuple

SKILL_ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "dist",
    "build",
    "test_out",
    ".venv",
    "venv",
    "env",
    ".vscode",
    ".idea",
}

EXCLUDED_FILE_PATTERNS = (
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.tmp",
    "*.bak",
    "*.zap*",
    ".DS_Store",
    "Thumbs.db",
)


def clean_caches(root: Path = SKILL_ROOT) -> List[str]:
    """清理工作区内的 __pycache__、.pytest_cache 等缓存目录及字节码残留"""
    removed: List[str] = []
    cache_dir_names = {"__pycache__", ".pytest_cache"}
    for current_root, dirs, files in os.walk(root, topdown=True):
        cur_path = Path(current_root)
        if ".git" in cur_path.parts:
            dirs[:] = []
            continue
        for d in list(dirs):
            if d in cache_dir_names:
                target = cur_path / d
                shutil.rmtree(target, ignore_errors=True)
                removed.append(target.relative_to(root).as_posix())
                dirs.remove(d)
            elif d == ".git":
                dirs.remove(d)
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                target = cur_path / f
                try:
                    target.unlink()
                    removed.append(target.relative_to(root).as_posix())
                except OSError:
                    pass
    return sorted(removed)


def _is_excluded(rel_path: Path) -> bool:
    """判断相对路径是否命中排除目录或排除文件名模式"""
    for part in rel_path.parts[:-1]:
        if part in EXCLUDED_DIRS:
            return True
    name = rel_path.name
    if name in EXCLUDED_DIRS:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDED_FILE_PATTERNS)


def collect_release_files(root: Path = SKILL_ROOT) -> List[Path]:
    """收集需要纳入开源发布压缩包的全部纯净文件（按 POSIX 路径排序）"""
    selected: List[Path] = []
    for current_root, dirs, files in os.walk(root, topdown=True):
        cur_path = Path(current_root)
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS)
        for fname in sorted(files):
            full_path = cur_path / fname
            rel_path = full_path.relative_to(root)
            if not _is_excluded(rel_path):
                selected.append(full_path)
    selected.sort(key=lambda p: p.relative_to(root).as_posix())
    return selected


def build_release_zip(
    root: Path = SKILL_ROOT,
    output_zip: Path = None,
    wrap_root_dir: bool = True,
) -> Tuple[Path, List[str], List[str]]:
    """
    清理缓存并生成纯净的 release zip 包。
    返回: (zip_path, cleaned_items, archived_posix_paths)
    """
    cleaned = clean_caches(root)
    if output_zip is None:
        output_zip = root / "dist" / "eplan-to-tia-lad-release.zip"
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    if output_zip.exists():
        output_zip.unlink()

    files = collect_release_files(root)
    archived_paths: List[str] = []
    prefix = root.name if wrap_root_dir else ""

    with zipfile.ZipFile(output_zip, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for file_path in files:
            rel_posix = file_path.relative_to(root).as_posix()
            arc_name = f"{prefix}/{rel_posix}" if prefix else rel_posix
            zinfo = zipfile.ZipInfo(filename=arc_name)
            st = file_path.stat()
            import time
            lt = time.localtime(st.st_mtime)
            zinfo.date_time = (max(lt.tm_year, 1980), lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec)
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            # 显式置位 UTF-8 文件名标志 (bit 11 = 0x800)
            zinfo.flag_bits |= 0x800
            zinfo.external_attr = 0o644 << 16
            zf.writestr(zinfo, file_path.read_bytes())
            archived_paths.append(arc_name)

    return output_zip, cleaned, archived_paths


def main() -> int:
    if hasattr(sys.stdout, "buffer") and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    parser = argparse.ArgumentParser(description="EPLAN-to-TIA-LAD 技能开源纯净打包工具")
    parser.add_argument(
        "--output",
        default="",
        help="输出 zip 路径（默认为 dist/eplan-to-tia-lad-release.zip）",
    )
    parser.add_argument(
        "--no-wrap-dir",
        action="store_true",
        help="不在压缩包内套一层 eplan-to-tia-lad/ 根目录",
    )
    args = parser.parse_args()

    out_path = Path(args.output) if args.output else None
    zip_path, cleaned, archived = build_release_zip(
        SKILL_ROOT,
        output_zip=out_path,
        wrap_root_dir=not args.no_wrap_dir,
    )
    size_bytes = zip_path.stat().st_size
    size_kb = size_bytes / 1024.0

    print(f"=== 打包完成 ===")
    print(f"清理缓存项: {len(cleaned)} 个")
    for c in cleaned:
        print(f"  - [CLEANED] {c}")
    print(f"输出文件: {zip_path}")
    print(f"压缩包体积: {size_bytes:,} bytes ({size_kb:.2f} KB)")
    print(f"归档文件总数: {len(archived)} 个")
    for item in archived:
        print(f"  - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
