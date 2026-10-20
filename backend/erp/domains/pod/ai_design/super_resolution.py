from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from erp.core.config import PROJECT_ROOT

from .image_size_limit import ImageSizeLimitError, MAX_OUTPUT_BYTES, limit_image_file_size


APP_DIR = PROJECT_ROOT
MODEL_NAME = "realesrgan-x4plus"
NATIVE_SCALE = 4
MAX_X4_INTERMEDIATE_PIXELS = 32_000_000
LARGE_SAFE_JPEG_PIXELS = 50_000_000
SUPPORTED_OUTPUT_SCALES = {0, 2, 4}


class SuperResolutionError(ValueError):
    pass


def find_realesrgan_executable() -> Path | None:
    candidates = [
        APP_DIR / "tools" / "realesrgan-ncnn-vulkan" / "realesrgan-ncnn-vulkan.exe",
        APP_DIR / "tools" / "realesrgan-ncnn-vulkan.exe",
        APP_DIR / "realesrgan-ncnn-vulkan.exe",
    ]
    if os.environ.get('ERP_REALESRGAN_EXE'):candidates.insert(0,Path(os.environ['ERP_REALESRGAN_EXE']))
    command = shutil.which("realesrgan-ncnn-vulkan")
    if command:
        candidates.append(Path(command))
    return next((path.resolve() for path in candidates if path.is_file()), None)


def upscale_with_realesrgan(
    content: bytes,
    extension: str,
    output_scale: int,
    *,
    executable: Path | None = None,
) -> tuple[bytes, dict[str, Any]]:
    # Concurrency is controlled by the caller's dedicated super-resolution
    # worker pool.  Keeping a global lock here would make that setting inert.
    return _upscale_with_realesrgan(content, extension, output_scale, executable=executable)


def _upscale_with_realesrgan(
    content: bytes,
    extension: str,
    output_scale: int,
    *,
    executable: Path | None = None,
) -> tuple[bytes, dict[str, Any]]:
    if output_scale not in SUPPORTED_OUTPUT_SCALES:
        raise ValueError("AI 超分倍率仅支持关闭、2×或4×")
    if output_scale == 0:
        return content, {
            "super_resolution_enabled": False,
            "super_resolution_model": None,
            "super_resolution_native_scale": None,
            "super_resolution_output_scale": 1,
        }

    binary = (executable or find_realesrgan_executable())
    if binary is None or not binary.is_file():
        expected = APP_DIR / "tools" / "realesrgan-ncnn-vulkan" / "realesrgan-ncnn-vulkan.exe"
        raise SuperResolutionError(
            "未找到 Real-ESRGAN 运行时，无法执行 AI 超分。"
            f"请确认程序文件存在：{expected}"
        )
    models_dir = binary.parent / "models"
    model_param = models_dir / f"{MODEL_NAME}.param"
    model_bin = models_dir / f"{MODEL_NAME}.bin"
    if not model_param.is_file() or not model_bin.is_file():
        raise SuperResolutionError(f"Real-ESRGAN 模型文件不完整：{models_dir}")

    try:
        with Image.open(io.BytesIO(content)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            original_size = image.size
    except Exception as exc:
        raise SuperResolutionError("送入 AI 超分的数据不是有效图片") from exc

    engine_scale = NATIVE_SCALE
    projected_x4_pixels = original_size[0] * original_size[1] * NATIVE_SCALE * NATIVE_SCALE
    if projected_x4_pixels > MAX_X4_INTERMEDIATE_PIXELS:
        # Very large provider images make the x4plus runtime emit incomplete
        # PNG files on some GPUs.  In 2× mode the engine also renders its x4
        # tiles on an x2 grid, producing a shuffled mosaic.  Use a direct,
        # deterministic resize instead of invoking an unsafe native x4 pass.
        try:
            return _upscale_large_image_safely(image, extension, output_scale)
        except SuperResolutionError:
            raise
        except MemoryError as exc:
            raise SuperResolutionError("大图安全放大失败：可用内存不足") from exc
        except Exception as exc:
            raise SuperResolutionError(f"大图安全放大失败：{exc}") from exc

    alpha = _real_alpha_channel(image)
    if alpha is not None:
        rgba = image.convert("RGBA")
        model_input = Image.new("RGB", original_size, "black")
        model_input.paste(rgba.convert("RGB"), mask=alpha)
    else:
        model_input = image.convert("RGB")

    with tempfile.TemporaryDirectory(prefix="pod-realesrgan-") as folder:
        work_dir = Path(folder)
        input_path = work_dir / "input.png"
        native_output_path = work_dir / "output-x4.png"
        model_input.save(input_path, format="PNG", optimize=True)
        command = [
            str(binary),
            "-i",
            str(input_path),
            "-o",
            str(native_output_path),
            "-n",
            MODEL_NAME,
            "-s",
            str(engine_scale),
            "-m",
            str(models_dir),
            "-f",
            "png",
        ]
        completed = _run_realesrgan(command, binary.parent)
        if completed.returncode != 0 or not native_output_path.is_file():
            detail = (completed.stderr or completed.stdout or "未知错误").strip()
            raise SuperResolutionError(f"Real-ESRGAN 超分失败：{detail[-800:]}")

        try:
            with Image.open(native_output_path) as generated:
                upscaled_rgb = generated.convert("RGB")
                upscaled_rgb.load()
        except Exception as exc:
            output_bytes = native_output_path.stat().st_size if native_output_path.is_file() else 0
            raise SuperResolutionError(
                f"Real-ESRGAN 输出文件无法解析（运行倍率 {engine_scale}×，文件 {output_bytes} 字节）"
            ) from exc

    native_size = (original_size[0] * engine_scale, original_size[1] * engine_scale)
    if upscaled_rgb.size != native_size:
        raise SuperResolutionError(
            f"Real-ESRGAN 返回尺寸异常：预期 {native_size[0]}x{native_size[1]}，"
            f"实际 {upscaled_rgb.width}x{upscaled_rgb.height}"
        )

    if alpha is not None:
        native_alpha = alpha.resize(native_size, Image.Resampling.LANCZOS)
        native_image = Image.merge("RGBa", (*upscaled_rgb.split(), native_alpha)).convert("RGBA")
    else:
        native_image = upscaled_rgb

    target_size = (original_size[0] * output_scale, original_size[1] * output_scale)
    if target_size != native_size:
        if native_image.mode == "RGBA":
            final_image = native_image.convert("RGBa").resize(target_size, Image.Resampling.LANCZOS).convert("RGBA")
        else:
            final_image = native_image.resize(target_size, Image.Resampling.LANCZOS)
    else:
        final_image = native_image

    limited_content, file_size_info = _encode_and_limit(final_image, extension)
    return limited_content, {
        "super_resolution_enabled": True,
        "super_resolution_engine": "realesrgan-ncnn-vulkan",
        "super_resolution_model": MODEL_NAME,
        "super_resolution_input_size": f"{original_size[0]}x{original_size[1]}",
        "super_resolution_native_scale": engine_scale,
        "super_resolution_native_size": f"{native_size[0]}x{native_size[1]}",
        "super_resolution_output_scale": output_scale,
        "super_resolution_output_size": f"{target_size[0]}x{target_size[1]}",
        "saved_size": f"{target_size[0]}x{target_size[1]}",
        "super_resolution_downsample": "lanczos" if output_scale < engine_scale else "none",
        "super_resolution_alpha": "separate_lanczos_premultiplied_merge" if alpha is not None else "none",
        **file_size_info,
    }


def _upscale_large_image_safely(
    image: Image.Image,
    extension: str,
    output_scale: int,
) -> tuple[bytes, dict[str, Any]]:
    original_size = image.size
    requested_size = (original_size[0] * output_scale, original_size[1] * output_scale)
    alpha = _real_alpha_channel(image)
    alpha_strategy = "premultiplied_lanczos" if alpha is not None else "none"
    final_size = requested_size
    final_image = _resize_image_safely(image, final_size, alpha is not None)
    file_size_fallback = "none"
    try:
        limited_content, file_size_info = _encode_and_limit(final_image, extension)
    except SuperResolutionError as exc:
        if not isinstance(exc.__cause__, ImageSizeLimitError):
            raise
        limited_content = b""
        file_size_info: dict[str, Any] = {}
        # A 7680² indexed PNG is already about 29.5 MB before PNG overhead.
        # Reduce dimensions in small steps only when the exact target cannot
        # meet the configured 30 MB cap, instead of discarding the API result.
        for factor in (0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50):
            candidate_size = (
                max(original_size[0], int(requested_size[0] * factor)),
                max(original_size[1], int(requested_size[1] * factor)),
            )
            candidate = _resize_image_safely(image, candidate_size, alpha is not None)
            try:
                limited_content, file_size_info = _encode_and_limit(candidate, extension)
            except SuperResolutionError as retry_exc:
                if not isinstance(retry_exc.__cause__, ImageSizeLimitError):
                    raise
                continue
            final_size = candidate_size
            file_size_fallback = "dimension_reduced_to_meet_30mb"
            break
        if not limited_content:
            raise exc

    return limited_content, {
        "super_resolution_enabled": True,
        "super_resolution_engine": "pillow-lanczos",
        "super_resolution_model": None,
        "super_resolution_input_size": f"{original_size[0]}x{original_size[1]}",
        "super_resolution_native_scale": None,
        "super_resolution_native_size": f"{final_size[0]}x{final_size[1]}",
        "super_resolution_output_scale": output_scale,
        "super_resolution_requested_size": f"{requested_size[0]}x{requested_size[1]}",
        "super_resolution_output_size": f"{final_size[0]}x{final_size[1]}",
        "saved_size": f"{final_size[0]}x{final_size[1]}",
        "super_resolution_downsample": "direct_lanczos",
        "super_resolution_strategy": "large_input_safe_resize",
        "super_resolution_fallback_reason": (
            "avoid_x4_model_x2_tile_mosaic"
            if output_scale == 2
            else "avoid_x4_model_large_output_corruption"
        ),
        "super_resolution_file_size_fallback": file_size_fallback,
        "super_resolution_alpha": alpha_strategy,
        **file_size_info,
    }


def _resize_image_safely(
    image: Image.Image,
    size: tuple[int, int],
    has_alpha: bool,
) -> Image.Image:
    if has_alpha:
        return (
            image.convert("RGBA")
            .convert("RGBa")
            .resize(size, Image.Resampling.LANCZOS)
            .convert("RGBA")
        )
    return image.convert("RGB").resize(size, Image.Resampling.LANCZOS)


def _encode_and_limit(
    image: Image.Image,
    extension: str,
) -> tuple[bytes, dict[str, Any]]:
    format_name = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}.get(
        extension.lower(), "PNG"
    )
    if format_name == "JPEG" and image.width * image.height > LARGE_SAFE_JPEG_PIXELS:
        return _encode_large_jpeg_safely(image)

    output = io.BytesIO()
    if format_name == "JPEG":
        image.convert("RGB").save(output, format="JPEG", quality=95, subsampling=0)
    elif format_name == "WEBP":
        image.save(output, format="WEBP", lossless=image.mode == "RGBA", quality=100)
    else:
        image.save(output, format="PNG", optimize=True)

    try:
        return limit_image_file_size(output.getvalue(), extension, MAX_OUTPUT_BYTES)
    except ImageSizeLimitError as exc:
        raise SuperResolutionError(str(exc)) from exc


def _encode_large_jpeg_safely(image: Image.Image) -> tuple[bytes, dict[str, Any]]:
    """Encode a very large JPEG without the optimizer's full-image memory spike."""
    rgb = image.convert("RGB")
    for quality in (90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35):
        output = io.BytesIO()
        rgb.save(output, format="JPEG", quality=quality, subsampling=2)
        content = output.getvalue()
        if len(content) <= MAX_OUTPUT_BYTES:
            return content, {
                "file_size_limit_bytes": MAX_OUTPUT_BYTES,
                "file_size_bytes": len(content),
                "file_size_mb": round(len(content) / 1_000_000, 2),
                "file_size_compression": f"jpeg_quality_{quality}_subsampling_2_large_image_safe",
            }

    try:
        raise ImageSizeLimitError(
            "JPG 在保持当前像素尺寸和可用画质的前提下仍超过 30 MB；"
            "请改用 2×超分或尺寸更小的原图"
        )
    except ImageSizeLimitError as exc:
        raise SuperResolutionError(str(exc)) from exc


def _real_alpha_channel(image: Image.Image) -> Image.Image | None:
    if "A" in image.getbands():
        alpha = image.getchannel("A")
    elif "transparency" in image.info:
        alpha = image.convert("RGBA").getchannel("A")
    else:
        return None
    return alpha if alpha.getextrema()[0] < 255 else None


def calculate_super_resolution_size(size: str, output_scale: int) -> str:
    if output_scale not in SUPPORTED_OUTPUT_SCALES:
        raise ValueError("AI 超分倍率仅支持关闭、2×或4×")
    try:
        width_text, height_text = size.lower().split("x", maxsplit=1)
        width, height = int(width_text), int(height_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("基础尺寸格式无效") from exc
    scale = output_scale or 1
    return f"{width * scale}x{height * scale}"


def _run_realesrgan(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise SuperResolutionError(f"无法启动 Real-ESRGAN：{exc}") from exc
