import os
import sys
import tempfile
import shutil
import argparse
from PIL import Image
import tkinter as tk
from tkinter import filedialog

# ================= 配置区域（请按需修改）=================
# MAIN_DIR 将通过对话框选择，无需在此填写
WATERMARK_FILENAME = "水印.png"             # 水印文件名（位于主文件夹下）
POSITION = "bottom-right"                   # 方位: top-left, top-right, bottom-left, bottom-right, center
MARGIN = 20                                 # 边距（像素）
SCALE = 0.2                                 # 水印宽度占图片短边的比例
OPACITY = 0.7                               # 水印透明度（0~1，1为不透明）
OUTPUT_MODE = "overwrite"                   # "overwrite"覆盖原图 / "suffix"添加后缀
SUFFIX = "_watermarked"                     # suffix模式时的文件名后缀

# 支持的图片扩展名
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}
# ======================================================

def add_watermark_to_image(img_path, watermark, pos, margin, scale, opacity, output_mode, suffix, explicit_out_path=None):
    """单张图片加水印（安全覆盖版）"""
    try:
        base = Image.open(img_path)
        base.load()  # 立即验证图片完整性
    except Exception as e:
        print(f"  无法打开或图片已损坏 [{img_path}]: {e}")
        return

    fmt = base.format
    # 暂时不保存EXIF，避免因损坏EXIF导致保存失败（如需保留可自行添加）
    # exif = base.info.get("exif", None)

    # 转换为RGBA处理
    if base.mode != "RGBA":
        base = base.convert("RGBA")
    bg_w, bg_h = base.size

    # 水印缩放
    wm = watermark.copy()
    short_side = min(bg_w, bg_h)
    target_w = max(10, int(short_side * scale))
    ratio = target_w / wm.width
    target_h = int(wm.height * ratio)
    wm = wm.resize((target_w, target_h), Image.LANCZOS)

    # 透明度处理
    if opacity < 1.0:
        if wm.mode != "RGBA":
            wm = wm.convert("RGBA")
        alpha = wm.split()[3]
        alpha = alpha.point(lambda p: int(p * opacity))
        wm.putalpha(alpha)

    # 计算位置
    wm_w, wm_h = wm.size
    pos_map = {
        "top-left": (margin, margin),
        "top-right": (bg_w - wm_w - margin, margin),
        "bottom-left": (margin, bg_h - wm_h - margin),
        "bottom-right": (bg_w - wm_w - margin, bg_h - wm_h - margin),
        "center": ((bg_w - wm_w) // 2, (bg_h - wm_h) // 2),
    }
    x, y = pos_map.get(pos, (margin, margin))

    # 合成水印
    watermark_layer = Image.new("RGBA", (bg_w, bg_h), (0, 0, 0, 0))
    watermark_layer.paste(wm, (x, y), wm if wm.mode == "RGBA" else None)
    watermarked = Image.alpha_composite(base, watermark_layer)

    # 格式兼容处理
    if fmt == "JPEG":
        background = Image.new("RGB", watermarked.size, (255, 255, 255))
        background.paste(watermarked, mask=watermarked.split()[3])
        final = background
    elif fmt == "PNG":
        final = watermarked
    else:
        final = watermarked.convert("RGB")

    # 输出路径
    if explicit_out_path:
        out_path = explicit_out_path
    elif output_mode == "overwrite":
        out_path = img_path
    else:
        root, ext = os.path.splitext(img_path)
        out_path = root + suffix + ext

    # 保存参数
    save_args = {}
    if fmt == "JPEG":
        save_args = {"quality": 100, "subsampling": 0}
    elif fmt == "PNG":
        save_args = {"compress_level": 3}
    elif fmt == "WEBP":
        save_args = {"lossless": True, "quality": 100}

    # ===== 安全保存核心（overwrite时先写临时文件，成功后再替换）=====
    if output_mode == "overwrite" and not explicit_out_path:
        dir_name = os.path.dirname(img_path)
        try:
            # 创建临时文件（与目标文件同目录，避免跨分区移动失败）
            with tempfile.NamedTemporaryFile(dir=dir_name, delete=False, suffix=".tmp") as tmp:
                tmp_path = tmp.name
            # 写入临时文件
            final.save(tmp_path, format=fmt, **save_args)
            # 写入成功，原子替换原文件
            shutil.move(tmp_path, img_path)
            print(f"  已处理 -> {img_path}")
        except Exception as e:
            print(f"  保存失败 [{img_path}]: {e}，原图未被修改")
            # 清理可能残留的临时文件
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    else:
        # suffix模式或独立输出目录直接保存
        try:
            out_dir = os.path.dirname(out_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            final.save(out_path, format=fmt, **save_args)
            print(f"  已处理 -> {out_path}")
        except Exception as e:
            print(f"  保存失败 [{out_path}]: {e}")

def process_one_level_subdirs(root_dir, watermark, watermark_path, pos, margin, scale, opacity, output_mode, suffix, target_subfolder=None, output_root=None, skip_suffix=None):
    """处理批量根目录、单个产品目录或直接选择的图片目录。"""
    try:
        entries = os.listdir(root_dir)
    except Exception as e:
        print(f"无法列出主文件夹内容: {e}")
        return 0

    processed = 0
    image_dirs = []

    def add_image_dir(path, label):
        if not path or not os.path.isdir(path):
            return
        abs_path = os.path.abspath(path)
        if any(os.path.abspath(existing[0]) == abs_path for existing in image_dirs):
            return
        image_dirs.append((path, label))

    if target_subfolder:
        target_name = str(target_subfolder).strip().strip("/\\")
        if os.path.basename(os.path.normpath(root_dir)) == target_name:
            add_image_dir(root_dir, os.path.basename(os.path.dirname(os.path.normpath(root_dir))) or target_name)
        add_image_dir(os.path.join(root_dir, target_name), os.path.basename(os.path.normpath(root_dir)))
        for entry in entries:
            sub_path = os.path.join(root_dir, entry)
            if os.path.isdir(sub_path):
                add_image_dir(os.path.join(sub_path, target_name), entry)
    else:
        add_image_dir(root_dir, os.path.basename(os.path.normpath(root_dir)))
        for entry in entries:
            sub_path = os.path.join(root_dir, entry)
            if os.path.isdir(sub_path):
                add_image_dir(sub_path, entry)

    for image_dir, label in image_dirs:
        print(f"\n进入图片文件夹: {label} -> {image_dir}")
        try:
            files = os.listdir(image_dir)
        except Exception as e:
            print(f"  无法读取文件夹 {image_dir}: {e}")
            continue

        for f in files:
            full_path = os.path.join(image_dir, f)
            if not os.path.isfile(full_path):
                continue   # 跳过子文件夹中的子文件夹

            if os.path.splitext(f)[1].lower() not in IMAGE_EXTENSIONS:
                continue

            if skip_suffix and os.path.splitext(f)[0].endswith(skip_suffix):
                print(f"  跳过已加水印文件: {full_path}")
                continue

            # 跳过水印文件自身（虽然水印在主文件夹，以防万一）
            if os.path.abspath(full_path) == os.path.abspath(watermark_path):
                print(f"  跳过水印自身: {full_path}")
                continue

            explicit_out_path = None
            if output_root:
                rel_path = os.path.relpath(full_path, root_dir)
                explicit_out_path = os.path.join(output_root, rel_path)

            print(f"  正在处理: {full_path}")
            add_watermark_to_image(
                full_path,
                watermark,
                pos,
                margin,
                scale,
                opacity,
                output_mode,
                suffix,
                explicit_out_path=explicit_out_path
            )
            processed += 1

    return processed

def process_watermark_directory(main_dir, watermark_filename=WATERMARK_FILENAME, position=POSITION,
                                margin=MARGIN, scale=SCALE, opacity=OPACITY,
                                output_mode=OUTPUT_MODE, suffix=SUFFIX,
                                target_subfolder=None, output_root=None, skip_suffix=None):
    """批量加水印。供 pipeline.py 或命令行调用。"""
    if not os.path.isdir(main_dir):
        raise FileNotFoundError(f"主文件夹不存在: {main_dir}")

    watermark_path = os.path.join(main_dir, watermark_filename)
    if not os.path.isfile(watermark_path):
        raise FileNotFoundError(f"水印图未找到: {watermark_path}")

    watermark = Image.open(watermark_path).convert("RGBA")

    print(f"主文件夹: {main_dir}")
    print(f"水印图片: {watermark_path}")
    if target_subfolder:
        print(f"只处理子目录: {target_subfolder}")
    if output_root:
        print(f"独立输出目录: {output_root}")
    print(f"水印位置: {position}, 透明度: {opacity}, 大小比例: {scale}, 输出模式: {output_mode}")

    count = process_one_level_subdirs(
        main_dir,
        watermark,
        watermark_path,
        position,
        margin,
        scale,
        opacity,
        output_mode,
        suffix,
        target_subfolder=target_subfolder,
        output_root=output_root,
        skip_suffix=skip_suffix
    )

    print(f"\n全部处理完成，处理图片数: {count}")
    return count

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="批量给产品图片添加水印")
    parser.add_argument("main_dir", nargs="?", help="包含产品子文件夹的主目录")
    parser.add_argument("--watermark-file", default=WATERMARK_FILENAME, help="主目录下的水印文件名")
    parser.add_argument("--target-subfolder", default=None, help="只处理每个产品目录下的指定子目录，例如 _generated")
    parser.add_argument("--output-root", default=None, help="把加水印图片输出到独立根目录，并保持产品目录结构")
    parser.add_argument("--skip-suffix", default=None, help="跳过文件名带有指定后缀的图片，例如 _watermarked")
    parser.add_argument("--position", default=POSITION, choices=["top-left", "top-right", "bottom-left", "bottom-right", "center"])
    parser.add_argument("--margin", type=int, default=MARGIN)
    parser.add_argument("--scale", type=float, default=SCALE)
    parser.add_argument("--opacity", type=float, default=OPACITY)
    parser.add_argument("--output-mode", default=OUTPUT_MODE, choices=["overwrite", "suffix"])
    parser.add_argument("--suffix", default=SUFFIX)
    parser.add_argument("--no-gui", action="store_true", help="未提供 main_dir 时不弹窗，直接报错")
    return parser.parse_args(argv)

def main():
    args = parse_args()

    if args.main_dir:
        try:
            process_watermark_directory(
                args.main_dir,
                watermark_filename=args.watermark_file,
                position=args.position,
                margin=args.margin,
                scale=args.scale,
                opacity=args.opacity,
                output_mode=args.output_mode,
                suffix=args.suffix,
                target_subfolder=args.target_subfolder,
                output_root=args.output_root,
                skip_suffix=args.skip_suffix
            )
            return
        except Exception as e:
            print(f"错误: {e}")
            sys.exit(1)

    if args.no_gui:
        print("错误: 使用 --no-gui 时必须提供 main_dir")
        sys.exit(1)

    # 弹出文件夹选择对话框
    root = tk.Tk()
    root.withdraw()  # 隐藏主窗口
    root.attributes('-topmost', True)  # 对话框置顶
    main_dir = filedialog.askdirectory(title="请选择主文件夹（包含水印图片及子文件夹）")
    root.destroy()

    if not main_dir:  # 用户取消选择
        print("未选择文件夹，程序退出。")
        sys.exit(0)

    print(f"已选择主文件夹: {main_dir}")

    if not os.path.isdir(main_dir):
        print(f"错误：主文件夹不存在 -> {main_dir}")
        sys.exit(1)

    watermark_path = os.path.join(main_dir, WATERMARK_FILENAME)
    if not os.path.isfile(watermark_path):
        print(f"错误：水印图未找到 -> {watermark_path}")
        sys.exit(1)

    try:
        watermark = Image.open(watermark_path).convert("RGBA")
    except Exception as e:
        print(f"无法读取水印图片: {e}")
        sys.exit(1)

    print(f"主文件夹: {main_dir}")
    print(f"水印图片: {watermark_path}")
    print(f"水印位置: {POSITION}, 透明度: {OPACITY}, 大小比例: {SCALE}")
    if OUTPUT_MODE == "overwrite":
        print("⚠️ 警告：将【覆盖原图】，本程序已采用安全写入，但仍建议提前备份重要图片！")
    else:
        print(f"将为图片添加后缀: {SUFFIX}")

    process_one_level_subdirs(main_dir, watermark, watermark_path, POSITION, MARGIN, SCALE, OPACITY, OUTPUT_MODE, SUFFIX)
    print("\n✅ 全部处理完成！")

if __name__ == "__main__":
    main()
