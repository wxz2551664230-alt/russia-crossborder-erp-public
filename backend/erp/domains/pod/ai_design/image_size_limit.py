from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageOps


MAX_OUTPUT_BYTES = 30_000_000


class ImageSizeLimitError(ValueError):
    pass


def limit_image_file_size(
    content: bytes,
    extension: str,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> tuple[bytes, dict[str, Any]]:
    if max_bytes <= 0:
        raise ValueError("文件大小上限必须大于 0")
    if len(content) <= max_bytes:
        return content, _details(len(content), max_bytes, "original")

    try:
        with Image.open(io.BytesIO(content)) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
    except Exception as exc:
        raise ImageSizeLimitError("无法压缩无效的图片数据") from exc

    normalized = extension.lower().lstrip(".")
    if normalized in {"jpg", "jpeg"}:
        result, method = _limit_jpeg(image, max_bytes)
    elif normalized == "webp":
        result, method = _limit_webp(image, max_bytes)
    elif normalized == "png":
        result, method = _limit_png(image, max_bytes)
    else:
        raise ImageSizeLimitError(f"不支持对 {extension} 格式执行 30 MB 限制")
    return result, _details(len(result), max_bytes, method)


def _limit_jpeg(image: Image.Image, max_bytes: int) -> tuple[bytes, str]:
    rgb = image.convert("RGB")
    for subsampling in (0, 2):
        result = _best_quality_encode(
            lambda output, quality: rgb.save(
                output,
                format="JPEG",
                quality=quality,
                subsampling=subsampling,
                optimize=True,
            ),
            max_bytes,
            minimum_quality=35,
            maximum_quality=95,
        )
        if result:
            content, quality = result
            return content, f"jpeg_quality_{quality}_subsampling_{subsampling}"
    raise _cannot_fit("JPG", max_bytes)


def _limit_webp(image: Image.Image, max_bytes: int) -> tuple[bytes, str]:
    working = image.convert("RGBA" if _has_real_alpha(image) else "RGB")
    result = _best_quality_encode(
        lambda output, quality: working.save(
            output,
            format="WEBP",
            quality=quality,
            method=6,
            exact=working.mode == "RGBA",
        ),
        max_bytes,
        minimum_quality=30,
        maximum_quality=95,
    )
    if not result:
        raise _cannot_fit("WebP", max_bytes)
    content, quality = result
    return content, f"webp_quality_{quality}"


def _limit_png(image: Image.Image, max_bytes: int) -> tuple[bytes, str]:
    has_alpha = _has_real_alpha(image)
    working = image.convert("RGBA" if has_alpha else "RGB")
    lossless = _encode_png(working)
    if len(lossless) <= max_bytes:
        return lossless, "png_lossless_max"

    rgb = working.convert("RGB")
    alpha = working.getchannel("A") if has_alpha else None
    for colors in (256, 192, 128, 96, 64, 48, 32, 16):
        quantized = rgb.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        if alpha is not None:
            encoded_image = quantized.convert("RGB").convert("RGBA")
            encoded_image.putalpha(alpha)
            method = f"png_rgb_{colors}_colors_alpha_lossless"
        else:
            encoded_image = quantized
            method = f"png_palette_{colors}_colors"
        bits = 4 if colors <= 16 else 8
        result = _encode_png(encoded_image, bits=bits if alpha is None else None)
        if len(result) <= max_bytes:
            return result, method
    raise _cannot_fit("PNG", max_bytes)


def _best_quality_encode(
    encode: Any,
    max_bytes: int,
    *,
    minimum_quality: int,
    maximum_quality: int,
) -> tuple[bytes, int] | None:
    low = minimum_quality
    high = maximum_quality
    best: tuple[bytes, int] | None = None
    while low <= high:
        quality = (low + high) // 2
        output = io.BytesIO()
        encode(output, quality)
        content = output.getvalue()
        if len(content) <= max_bytes:
            best = (content, quality)
            low = quality + 1
        else:
            high = quality - 1
    return best


def _encode_png(image: Image.Image, *, bits: int | None = None) -> bytes:
    output = io.BytesIO()
    options: dict[str, Any] = {"format": "PNG", "optimize": True, "compress_level": 9}
    if bits is not None:
        options["bits"] = bits
    image.save(output, **options)
    return output.getvalue()


def _has_real_alpha(image: Image.Image) -> bool:
    if "A" in image.getbands():
        alpha = image.getchannel("A")
    elif "transparency" in image.info:
        alpha = image.convert("RGBA").getchannel("A")
    else:
        return False
    return alpha.getextrema()[0] < 255


def _cannot_fit(format_name: str, max_bytes: int) -> ImageSizeLimitError:
    limit_mb = max_bytes / 1_000_000
    return ImageSizeLimitError(
        f"{format_name} 在保持当前像素尺寸和可用画质的前提下仍超过 {limit_mb:g} MB；"
        "请改用 2×超分或尺寸更小的原图"
    )


def _details(size: int, limit: int, method: str) -> dict[str, Any]:
    return {
        "file_size_limit_bytes": limit,
        "file_size_bytes": size,
        "file_size_mb": round(size / 1_000_000, 2),
        "file_size_compression": method,
    }
