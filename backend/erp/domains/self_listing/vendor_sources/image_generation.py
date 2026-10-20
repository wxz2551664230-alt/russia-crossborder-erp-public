from erp.core.ai_concurrency import async_post as ai_async_post
import hashlib
import asyncio
import aiohttp
import base64
import json
import mimetypes
import os
import queue
import random
import re
import ssl
import tempfile
import threading
import time
import argparse
import tkinter as tk
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageOps

try:
    import certifi
except Exception:
    certifi = None


# =========================
# 基础配置
# =========================

APP_NAME = "OZON AI 图片生成器 - 品类策略版"
CONFIG_PATH = Path.home() / ".ozon_ai_image_gui_category_config.json"

DEFAULT_API_HOST = "api.gpt.ge"
DEFAULT_CHAT_PATH = "/v1/chat/completions"
DEFAULT_IMAGE_EDIT_PATH = "/v1/images/edits"

DEFAULT_ANALYSIS_MODEL = "gpt-5.5"
DEFAULT_IMAGE_MODEL = "gpt-image-2"
DEFAULT_IMAGE_GENERATION_METHOD = "image_edits"
IMAGE_GENERATION_METHODS = {
    "chat_completions": "Chat Completions 图像生成",
    "image_edits": "Images Edits 图生图",
}

DEFAULT_SKU_PREFIX = "SKU"
DEFAULT_OUT_SUBFOLDER = "_generated"

DEFAULT_ASPECT_TEXT = "strict 3:4 vertical portrait, full-frame composition, no letterbox or white padding"
DEFAULT_RES_TEXT = "1792x2400"
DEFAULT_GPT_IMAGE_2_VERTICAL_SIZE = "1792x2400"

MAX_PRODUCT_IMAGES_FOR_ANALYSIS = 8
MAX_PRODUCT_IMAGES_FOR_GENERATION = 2

DEFAULT_MAX_CONCURRENT_REQUESTS = 4
DEFAULT_FOLDER_CONCURRENCY = 2
DEFAULT_MAX_IMAGE_MB = 8
DEFAULT_TIMEOUT = 600
DEFAULT_RETRIES = 5
DEFAULT_FAILED_TASK_RERUNS = 3
GPT_GE_MAX_EDIT_IMAGES = 4
GPT_GE_MAX_EDIT_IMAGE_BYTES = 3_900_000
GPT_GE_MAX_EDIT_PROMPT_CHARS = 1000
DEFAULT_MAX_EDIT_PROMPT_CHARS = 3000
REFERENCE_SHEET_SIZE = 1600
REFERENCE_SHEET_MARGIN = 24
REFERENCE_SHEET_GAP = 16

IMAGE_PLAN_MIN_COUNT = 1
IMAGE_PLAN_MAX_COUNT = 12
DEFAULT_IMAGE_PLAN_COUNT = 8

GLOBAL_IMAGE_RULES = """
Vertical 3:4 Ozon marketplace product image.
True portrait composition from edge to edge, not a horizontal/landscape poster placed inside a vertical canvas.
No letterbox, pillarbox, white top/bottom padding, empty border bands, pasted screenshot frame, or landscape content strip.
Russian text only when text is used. No Chinese text.
No logo, watermark, QR code, brand mark, marketplace UI, or OZON/Ozon word.
Do not invent unverified specifications, functions, materials, capacities, or accessories.
Preserve product identity, signature design elements, color family, material, and recognizable proportions.
Re-render the product as a real three-dimensional object integrated into the new scene; never paste the source product as a flat cutout.
Allow physically necessary rotation, perspective, articulation, opening/closing, drape, bending, contact, occlusion, and conservative completion of unseen surfaces when appropriate to the product.
Improve composition, lighting, background, and graphic design without replacing the product with a different design.
For hero and lifestyle images, use a believable everyday environment appropriate to the product's actual use.
Treat the reference background, surface, lighting, text, props, and composition as disposable; reference only the product itself.
Use clean neutral daylight, accurate white balance, fresh surfaces, crisp product edges, and very few functional props.
No cold empty studio, showroom pedestal, muddy beige/gray cast, yellow haze, dirty-looking surface, irrelevant decorative clutter, or repeated generic poster furniture.
""".strip()

STRICT_VERTICAL_FRAME_RULE = """
Compose as a real portrait 3:4 marketplace image using the full vertical frame.
Do not create a wide horizontal poster and center it on a tall white canvas.
Do not add white bands above or below the designed image.
The product, background, graphics, and any Russian text must be arranged naturally for the vertical canvas itself.
""".strip()


# =========================
# 风格预设
# =========================

STYLE_PRESETS = {
    "自动": {
        "description": "根据商品图片自动分析风格、颜色、构图和背景。",
        "prompt_ru": ""
    },
    "极简白底": {
        "description": "干净、明亮、白底、少装饰，适合通用电商主图。",
        "prompt_ru": "чистый минимализм, белый или светло-серый фон, мягкие тени, современный каталог"
    },
    "家居生活": {
        "description": "适合家居、收纳、装饰、厨房、日用品。",
        "prompt_ru": "уютный домашний интерьер, естественный свет, реальные бытовые детали, теплая атмосфера"
    },
    "高级质感": {
        "description": "适合礼品、装饰、高客单、质感类产品。",
        "prompt_ru": "премиальная предметная съемка без промо, глубокие мягкие тени, дорогие фактуры"
    },
    "自然真实": {
        "description": "强调真实生活感，不像棚拍。",
        "prompt_ru": "максимально реалистичная фотография, повседневная среда, натуральный свет, живые детали"
    },
    "俄区电商海报": {
        "description": "适合带俄文卖点的 OZON 主图海报。",
        "prompt_ru": "современный маркетплейс, аккуратные русские текстовые блоки, чистая инфографика"
    },
    "科技清爽": {
        "description": "适合电子、工具、功能性产品。",
        "prompt_ru": "технологичный чистый дизайн, холодная светлая палитра, синие акценты, четкие линии"
    }
}


# =========================
# 图片模板
# =========================

IMAGE_TYPE_TEMPLATES = {
    "主图海报": {
        "filename": "cover_poster",
        "prompt": "Чистый выразительный постер для главного изображения: товар крупный и естественно встроен в правдоподобную повседневную сцену, короткий русский заголовок и не более двух лаконичных преимуществ. Без шаблонных плашек и студийного подиума."
    },
    "真实场景图": {
        "filename": "scene_real",
        "prompt": "Реалистичная сцена использования товара в повседневной обстановке."
    },
    "第二场景图": {
        "filename": "scene_alt",
        "prompt": "Альтернативная сцена применения товара в другой реальной ситуации."
    },
    "细节特写图": {
        "filename": "detail_closeup",
        "prompt": "Крупный план ключевых деталей: материал, фактура, конструкция, края, крепления."
    },
    "尺寸结构图": {
        "filename": "dimensions",
        "prompt": (
            "Обязательная инфографика размеров: показать товар целиком, нанести хорошо видимые "
            "размерные линии со стрелками и числовые значения с единицами измерения. "
            "Точно перенести размеры из DIMENSION REFERENCE, не менять цифры и единицы. "
            "Если точных размеров на референсе нет, не выдумывать их."
        )
    },
    "卖点信息图": {
        "filename": "benefits",
        "prompt": "Инфографика с 3-5 нейтральными преимуществами товара на русском языке."
    },
    "对比图": {
        "filename": "comparison",
        "prompt": "Сравнительное изображение: товар и условный обычный аналог, показать удобство или конструктивные преимущества."
    },
    "使用步骤图": {
        "filename": "steps",
        "prompt": "Изображение с 3-4 простыми шагами использования товара, русские нейтральные подписи."
    },
    "包装/配件图": {
        "filename": "bundle",
        "prompt": "Изображение комплектации, упаковки или аксессуаров товара, аккуратная раскладка."
    },
    "模特展示图": {
        "filename": "model_show",
        "prompt": "Реалистичная сцена с человеком: модель носит, держит или демонстрирует товар для понимания масштаба и посадки."
    },
    "顶视平铺图": {
        "filename": "flatlay",
        "prompt": "Вид сверху: товар разложен на натуральной поверхности с уместными предметами."
    },
    "氛围生活图": {
        "filename": "lifestyle",
        "prompt": "Атмосферная lifestyle фотография товара в красивой, но реалистичной среде."
    },
    "容量展示图": {
        "filename": "capacity",
        "prompt": "Показать вместимость, что помещается внутри, аккуратную организацию и порядок."
    },
    "使用前后图": {
        "filename": "before_after",
        "prompt": "Показать эффект до и после использования товара, без агрессивной рекламы."
    },
    "接口功能图": {
        "filename": "ports_features",
        "prompt": "Показать функциональные элементы, интерфейсы, разъемы, кнопки или важные зоны товара."
    },
    "材质面料图": {
        "filename": "material_texture",
        "prompt": "Показать материал, ткань, фактуру, швы, поверхность или качество исполнения крупным планом."
    }
}


# =========================
# 品类策略
# =========================

def is_dimension_template(tpl_name: str) -> bool:
    return IMAGE_TYPE_TEMPLATES.get(tpl_name, {}).get("filename") == "dimensions"


def template_filename(tpl_name: str) -> str:
    return IMAGE_TYPE_TEMPLATES.get(tpl_name, {}).get("filename", "")


def template_prompt_policy(tpl_name: str) -> dict:
    filename = template_filename(tpl_name)
    if filename == "cover_poster":
        return {
            "max_benefits": 2,
            "max_steps": 0,
            "include_headline": True,
            "include_subheadline": False,
            "include_comparison": False,
            "include_scene": True,
            "include_detail": False,
            "layout_rule": (
                "Create a clean editorial marketplace poster built on a believable everyday scene. "
                "Use one short Russian headline and at most two compact verified benefits. "
                "Choose typography placement and hierarchy for this exact product; do not default to a huge top title, "
                "two rounded pills, repeated icon badges, rigid geometric panels, or a studio podium."
            ),
        }
    if filename == "dimensions":
        return {
            "max_benefits": 0,
            "max_steps": 0,
            "include_headline": False,
            "include_subheadline": False,
            "include_comparison": False,
            "include_scene": False,
            "include_detail": False,
            "layout_rule": (
                "Create only a clean dimension/structure diagram. Use measurement lines, arrows, "
                "and copied numeric labels when a dimension reference is available. Do not add "
                "selling points, scene text, decorative badges, or extra marketing copy."
            ),
        }
    if filename in {"benefits", "comparison", "steps", "before_after"}:
        return {
            "max_benefits": 3,
            "max_steps": 3 if filename == "steps" else 0,
            "include_headline": True,
            "include_subheadline": False,
            "include_comparison": filename in {"comparison", "before_after"},
            "include_scene": False,
            "include_detail": True,
            "layout_rule": (
                "Use a clean, balanced infographic layout with limited copy. Keep text blocks short, "
                "align elements on a clear grid, and leave generous empty space around the product. "
                "Prefer 2-3 compact callouts instead of filling the page. Do not place a duplicate "
                "product photo, reference thumbnail, inset image, or picture-in-picture frame."
            ),
        }
    return {
        "max_benefits": 0,
        "max_steps": 0,
        "include_headline": False,
        "include_subheadline": False,
        "include_comparison": False,
        "include_scene": True,
        "include_detail": filename in {"detail_closeup", "material_texture", "ports_features"},
        "layout_rule": (
            "Create a polished realistic product image for this template, with one clear subject, "
            "natural composition, clean margins, and attractive negative space. Do not add headlines, "
            "benefit text, captions, badges, tables, poster-style text panels, duplicate product "
            "photos, reference thumbnails, inset images, or picture-in-picture frames. Let the product "
            "and scene/close-up carry the image."
        ),
    }


def build_sku_cover_filename(sku_path: Path) -> str:
    stem = (sku_path.stem or "SKU").strip()
    return f"{stem}.jpg"


def build_dimension_filename(index: int) -> str:
    return build_appendix_filename(index)


def build_appendix_filename(index: int) -> str:
    return f"附图({index}).jpg"


CATEGORY_PROFILES = {
    "自动识别": {
        "key": "auto",
        "description": "由 AI 和关键词自动判断品类策略。",
        "model_policy": "auto",
        "default_templates": {
            "主图海报": 1,
            "真实场景图": 1,
            "细节特写图": 1,
            "卖点信息图": 1,
            "氛围生活图": 1
        },
        "extra_prompt_ru": ""
    },
    "服装": {
        "key": "clothing",
        "description": "衣服、裤子、外套、裙子等。重点展示上身效果、版型、面料。",
        "model_policy": "strongly_required",
        "default_templates": {
            "主图海报": 1,
            "模特展示图": 2,
            "材质面料图": 2,
            "尺寸结构图": 1,
            "氛围生活图": 1
        },
        "extra_prompt_ru": (
            "Для одежды обязательно показать посадку на реальной модели. "
            "Естественная поза, хорошо виден крой, длина, ткань, швы и фактура. "
            "Не менять форму, цвет и детали изделия."
        )
    },
    "配饰": {
        "key": "fashion_accessory",
        "description": "包、帽子、围巾、首饰、眼镜、发饰等。重点展示佩戴效果和比例。",
        "model_policy": "recommended",
        "default_templates": {
            "主图海报": 1,
            "模特展示图": 2,
            "细节特写图": 2,
            "尺寸结构图": 1,
            "氛围生活图": 1
        },
        "extra_prompt_ru": (
            "Показать товар на человеке, в руках или в носке для понимания масштаба. "
            "Фокус на сочетании с одеждой, размере, материале и деталях."
        )
    },
    "家居装饰": {
        "key": "home_decor",
        "description": "摆件、花瓶、装饰画、香薰、灯饰等。重点是空间氛围和美感。",
        "model_policy": "avoid",
        "default_templates": {
            "主图海报": 1,
            "真实场景图": 2,
            "氛围生活图": 2,
            "细节特写图": 1,
            "尺寸结构图": 1
        },
        "extra_prompt_ru": (
            "Показать товар как декоративный элемент в красивом домашнем интерьере: "
            "гостиная, спальня, полка, стол, комод или прихожая. "
            "Не перегружать текстом. Главный акцент — эстетика, фактура и сочетание с интерьером."
        )
    },
    "工具": {
        "key": "tool",
        "description": "手动工具、电动工具、维修工具。重点是使用过程、结构和可靠性。",
        "model_policy": "hands_recommended",
        "default_templates": {
            "主图海报": 1,
            "使用步骤图": 1,
            "细节特写图": 2,
            "卖点信息图": 1,
            "对比图": 1,
            "模特展示图": 1,
            "尺寸结构图": 1
        },
        "extra_prompt_ru": (
            "Для инструмента важно показать реальное использование: рабочий стол, гараж, ремонт, руки человека. "
            "Показать хват, конструкцию, прочность, детали и процесс применения."
        )
    },
    "收纳": {
        "key": "storage",
        "description": "收纳盒、收纳包、衣柜/厨房/桌面收纳。重点是容量和前后对比。",
        "model_policy": "optional",
        "default_templates": {
            "主图海报": 1,
            "容量展示图": 2,
            "使用前后图": 1,
            "真实场景图": 1,
            "细节特写图": 1,
            "尺寸结构图": 1
        },
        "extra_prompt_ru": (
            "Для хранения показать вместимость, порядок, организацию пространства, до/после. "
            "Подходящие сцены: шкаф, кухня, ванная, стол, гардероб, офис."
        )
    },
    "厨房用品": {
        "key": "kitchen",
        "description": "锅具、刀具、砧板、保鲜盒、厨房小工具。重点是厨房使用过程。",
        "model_policy": "hands_recommended",
        "default_templates": {
            "主图海报": 1,
            "真实场景图": 2,
            "使用步骤图": 1,
            "细节特写图": 1,
            "尺寸结构图": 1,
            "卖点信息图": 1
        },
        "extra_prompt_ru": (
            "Показать товар на кухонной столешнице в реальном использовании. "
            "Можно добавить руки, продукты, процесс приготовления или хранения. "
            "Акцент на удобстве, чистоте и безопасности."
        )
    },
    "电子产品": {
        "key": "electronics",
        "description": "充电器、数据线、小家电、支架、智能设备。重点是功能、接口、参数信息。",
        "model_policy": "optional",
        "default_templates": {
            "主图海报": 1,
            "接口功能图": 2,
            "卖点信息图": 1,
            "真实场景图": 1,
            "尺寸结构图": 1,
            "对比图": 1
        },
        "extra_prompt_ru": (
            "Для электроники показать функции, разъемы, кнопки, сценарии использования, совместимость. "
            "Стиль чистый, технологичный, с понятной инфографикой."
        )
    },
    "美妆个护": {
        "key": "beauty",
        "description": "护肤、美妆、个护工具。重点是质感、使用场景、细节。",
        "model_policy": "recommended",
        "default_templates": {
            "主图海报": 1,
            "模特展示图": 1,
            "细节特写图": 2,
            "氛围生活图": 1,
            "卖点信息图": 1
        },
        "extra_prompt_ru": (
            "Показать чистую эстетичную сцену ухода за собой. "
            "Можно использовать руки, лицо без узнаваемости, ванную комнату, туалетный столик. "
            "Акцент на текстуре, чистоте и удобстве."
        )
    },
    "宠物用品": {
        "key": "pet",
        "description": "猫狗用品、宠物玩具、窝、碗、牵引等。重点展示宠物使用场景。",
        "model_policy": "pet_required",
        "default_templates": {
            "主图海报": 1,
            "真实场景图": 2,
            "细节特写图": 1,
            "卖点信息图": 1,
            "尺寸结构图": 1
        },
        "extra_prompt_ru": (
            "Показать товар рядом с кошкой или собакой в безопасной реальной ситуации. "
            "Акцент на размере, удобстве для питомца и домашнем использовании."
        )
    },
    "母婴用品": {
        "key": "baby",
        "description": "婴儿用品、儿童用品。注意安全、温和、真实使用环境。",
        "model_policy": "careful",
        "default_templates": {
            "主图海报": 1,
            "真实场景图": 1,
            "细节特写图": 2,
            "卖点信息图": 1,
            "尺寸结构图": 1
        },
        "extra_prompt_ru": (
            "Для детских товаров сцена должна быть безопасной, мягкой и реалистичной. "
            "Не показывать опасное использование. Акцент на материале, удобстве и заботе."
        )
    },
    "运动户外": {
        "key": "sports_outdoor",
        "description": "运动用品、户外装备。重点是户外/运动使用场景。",
        "model_policy": "recommended",
        "default_templates": {
            "主图海报": 1,
            "模特展示图": 1,
            "真实场景图": 2,
            "细节特写图": 1,
            "卖点信息图": 1
        },
        "extra_prompt_ru": (
            "Показать товар в активном использовании: спортзал, улица, поход, тренировка. "
            "Акцент на прочности, удобстве и реальном действии."
        )
    },
    "汽车用品": {
        "key": "car_accessory",
        "description": "车载用品、维修、清洁、内饰配件。重点是车内/车外使用场景。",
        "model_policy": "hands_recommended",
        "default_templates": {
            "主图海报": 1,
            "真实场景图": 2,
            "使用步骤图": 1,
            "细节特写图": 1,
            "对比图": 1,
            "卖点信息图": 1
        },
        "extra_prompt_ru": (
            "Показать товар в автомобиле или рядом с автомобилем. "
            "Сцены: салон, багажник, гараж, мойка, ремонт. "
            "Акцент на практичности и совместимости."
        )
    },
    "通用商品": {
        "key": "generic",
        "description": "不确定类型时使用的通用策略。",
        "model_policy": "auto",
        "default_templates": {
            "主图海报": 1,
            "真实场景图": 1,
            "细节特写图": 1,
            "卖点信息图": 1,
            "尺寸结构图": 1
        },
        "extra_prompt_ru": (
            "Универсальная товарная съемка: показать внешний вид, детали, применение и преимущества без промо."
        )
    }
}


# =========================
# 配置
# =========================

@dataclass
class AppConfig:
    api_key: str = ""
    root_dir: str = ""
    product_hint: str = ""

    api_host: str = DEFAULT_API_HOST
    chat_path: str = DEFAULT_CHAT_PATH
    analysis_api_key: str = ""
    analysis_api_host: str = ""
    analysis_chat_path: str = ""
    image_api_key: str = ""
    image_api_host: str = ""
    image_chat_path: str = ""
    image_generation_method: str = DEFAULT_IMAGE_GENERATION_METHOD
    image_edit_path: str = DEFAULT_IMAGE_EDIT_PATH
    analysis_model: str = DEFAULT_ANALYSIS_MODEL
    image_model: str = DEFAULT_IMAGE_MODEL
    image_size: str = DEFAULT_GPT_IMAGE_2_VERTICAL_SIZE
    image_quality: str = ""

    sku_prefix: str = DEFAULT_SKU_PREFIX
    out_subfolder: str = DEFAULT_OUT_SUBFOLDER

    aspect_text: str = DEFAULT_ASPECT_TEXT
    res_text: str = DEFAULT_RES_TEXT

    style_name: str = "自动"
    category_name: str = "自动识别"

    image_temperature: float = 0.075
    active_image_provider: str = ""
    image_providers: dict = None
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS
    folder_concurrency: int = DEFAULT_FOLDER_CONCURRENCY
    max_image_mb: int = DEFAULT_MAX_IMAGE_MB
    request_timeout: int = DEFAULT_TIMEOUT
    request_retries: int = DEFAULT_RETRIES
    failed_task_reruns: int = DEFAULT_FAILED_TASK_RERUNS

    generate_for_each_sku_cover: bool = True
    image_plan_count: int = DEFAULT_IMAGE_PLAN_COUNT
    generate_dimension_image: bool = True
    auto_detect_model: bool = True
    include_bundle_reference: bool = False
    skip_sku_text_detection: bool = False
    fast_mode: bool = False
    repair_only_missing: bool = False

    selected_templates: dict = None

    def __post_init__(self):
        if self.image_providers is None:
            self.image_providers = {}
        if self.selected_templates is None:
            self.selected_templates = CATEGORY_PROFILES["自动识别"]["default_templates"].copy()


def app_config_from_dict(data: dict) -> AppConfig:
    allowed_keys = {field.name for field in fields(AppConfig)}
    return AppConfig(**{key: value for key, value in dict(data or {}).items() if key in allowed_keys})


def load_config() -> AppConfig:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = app_config_from_dict(data)
            normalize_image_generation_config(cfg)
            return cfg
        except Exception as e:
            try:
                backup_path = CONFIG_PATH.with_name(
                    f"{CONFIG_PATH.stem}.broken-{time.strftime('%Y%m%d-%H%M%S')}{CONFIG_PATH.suffix}"
                )
                backup_path.write_bytes(CONFIG_PATH.read_bytes())
                print(f"损坏配置已备份到：{backup_path}")
            except Exception:
                pass
            print(f"配置文件损坏，已使用默认配置：{CONFIG_PATH}，错误：{e}")
            return AppConfig()
    cfg = AppConfig()
    normalize_image_generation_config(cfg)
    return cfg


def save_config(cfg: AppConfig):
    CONFIG_PATH.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")


# =========================
# 工具函数
# =========================

def wants_vertical_3x4(cfg: AppConfig | None) -> bool:
    if cfg is None:
        return False
    aspect_text = (cfg.aspect_text or "").lower()
    return "3:4" in aspect_text and (
        "vertical" in aspect_text or "portrait" in aspect_text or "竖" in aspect_text
    )


def normalized_target_size(cfg: AppConfig | None) -> tuple[int, int] | None:
    if cfg is None:
        return None
    target_size = parse_size_text(cfg.res_text)
    if not target_size:
        return None
    if wants_vertical_3x4(cfg):
        width, height = target_size
        if width > height:
            target_size = (height, width)
    return target_size


def effective_image_edit_size(cfg: AppConfig) -> str:
    size = (cfg.image_size or "").strip()

    if wants_vertical_3x4(cfg):
        if not size or size.lower() == "auto":
            if cfg.image_model != "gpt-image-2":
                return "auto"
            return DEFAULT_GPT_IMAGE_2_VERTICAL_SIZE

        match = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", size.lower())
        if match:
            width, height = int(match.group(1)), int(match.group(2))
            if width > height:
                width, height = height, width
            if cfg.image_model != "gpt-image-2":
                return f"{width}x{height}"
            if width <= 3840 and height <= 3840 and width % 16 == 0 and height % 16 == 0:
                return f"{width}x{height}"
            return DEFAULT_GPT_IMAGE_2_VERTICAL_SIZE

    return size or "auto"


def effective_image_quality(cfg: AppConfig) -> str:
    quality = str(getattr(cfg, "image_quality", "") or "").strip().lower()
    allowed = {"auto", "high", "medium", "low", "standard", "hd"}
    if quality in allowed:
        return quality
    return ""


def is_gpt_ge_endpoint(url: str) -> bool:
    return "api.gpt.ge" in str(url or "").lower()


def compact_gpt_ge_edit_prompt(prompt: str) -> str:
    text = str(prompt or "").strip()
    if len(text) <= GPT_GE_MAX_EDIT_PROMPT_CHARS:
        return text

    suffix = (
        "\n\nCritical: true portrait 3:4 full-frame composition. "
        "No horizontal poster strip, no letterbox, no white top/bottom padding."
    )
    keep = max(100, GPT_GE_MAX_EDIT_PROMPT_CHARS - len(suffix) - 6)
    return (text[:keep].rstrip() + "\n...\n" + suffix)[:GPT_GE_MAX_EDIT_PROMPT_CHARS]


def compact_image_edit_prompt(prompt: str, endpoint_url: str) -> str:
    """Keep image-edit prompts below provider limits without losing hard rules."""
    text = str(prompt or "").strip()
    if is_gpt_ge_endpoint(endpoint_url):
        return compact_gpt_ge_edit_prompt(text)

    limit = DEFAULT_MAX_EDIT_PROMPT_CHARS
    if len(text) <= limit:
        return text

    direct_text = ""
    direct_markers = (
        "Render the following short Russian text directly inside the final image.",
        "Russian text to render directly inside the image:",
    )
    direct_marker = next((value for value in direct_markers if value in text), "")
    if direct_marker:
        direct_text = direct_marker + text.rsplit(direct_marker, 1)[1].split("\n\n", 1)[0]
        direct_text = "\n\n[Exact approved Russian copy; preserve verbatim]\n" + direct_text.strip()
    marker = "\n\n[Long creative brief compressed; mandatory rules below are preserved.]\n\n"
    # The beginning describes the requested composition/product. The end contains
    # the strict frame, SKU colour, negative and reference-handling constraints.
    tail_size = min(950, max(650, limit // 3))
    reserved = direct_text[:500]
    head_size = limit - len(marker) - len(reserved) - tail_size
    return (
        text[:head_size].rstrip()
        + marker
        + reserved
        + "\n\n"
        + text[-tail_size:].lstrip()
    )[:limit]


def prepare_gpt_ge_edit_upload(path: Path) -> Path | None:
    path = Path(path)
    try:
        if path.stat().st_size <= GPT_GE_MAX_EDIT_IMAGE_BYTES:
            return None
    except Exception:
        return None

    with Image.open(path) as img:
        src = ImageOps.exif_transpose(img).convert("RGB")

    max_side = max(src.size)
    qualities = [92, 88, 84, 80, 76, 72, 68]
    scales = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5]

    last_tmp = None
    for scale in scales:
        target_side = max(512, int(max_side * scale))
        work = src.copy()
        if max(work.size) > target_side:
            work.thumbnail((target_side, target_side), Image.Resampling.LANCZOS)

        for quality in qualities:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            tmp_path = Path(tmp.name)
            tmp.close()
            work.save(tmp_path, format="JPEG", quality=quality, optimize=True)
            if last_tmp and last_tmp.exists():
                try:
                    last_tmp.unlink()
                except Exception:
                    pass
            last_tmp = tmp_path
            if tmp_path.stat().st_size <= GPT_GE_MAX_EDIT_IMAGE_BYTES:
                return tmp_path

    return last_tmp


def unique_existing_paths(paths: list[Path], exclude: set[str] | None = None) -> list[Path]:
    exclude = exclude or set()
    result = []
    seen = set()
    for path in paths or []:
        if not path:
            continue
        try:
            p = Path(path)
            key = str(p.resolve())
            if key in exclude or key in seen or not p.exists() or not p.is_file():
                continue
            seen.add(key)
            result.append(p)
        except Exception:
            continue
    return result


def split_evenly(items: list[Path], chunk_count: int) -> list[list[Path]]:
    chunk_count = max(1, min(chunk_count, len(items)))
    base = len(items) // chunk_count
    extra = len(items) % chunk_count
    chunks = []
    start = 0
    for idx in range(chunk_count):
        size = base + (1 if idx < extra else 0)
        chunks.append(items[start:start + size])
        start += size
    return [chunk for chunk in chunks if chunk]


def contact_sheet_grid(cell_count: int) -> tuple[int, int]:
    if cell_count <= 1:
        return 1, 1
    if cell_count <= 4:
        return 2, 2
    if cell_count <= 6:
        return 3, 2
    if cell_count <= 9:
        return 3, 3
    return 4, 3


def create_reference_contact_sheet(paths: list[Path], sheet_index: int, sheet_size: int = REFERENCE_SHEET_SIZE) -> Path:
    cols, rows = contact_sheet_grid(len(paths))
    margin = REFERENCE_SHEET_MARGIN
    gap = REFERENCE_SHEET_GAP
    canvas = Image.new("RGB", (sheet_size, sheet_size), (246, 247, 249))
    draw = ImageDraw.Draw(canvas)
    cell_w = (sheet_size - margin * 2 - gap * (cols - 1)) // cols
    cell_h = (sheet_size - margin * 2 - gap * (rows - 1)) // rows

    for idx, path in enumerate(paths[:cols * rows]):
        row = idx // cols
        col = idx % cols
        x0 = margin + col * (cell_w + gap)
        y0 = margin + row * (cell_h + gap)
        x1 = x0 + cell_w
        y1 = y0 + cell_h
        draw.rectangle((x0, y0, x1, y1), fill=(255, 255, 255), outline=(215, 220, 228), width=2)
        try:
            with Image.open(path) as img:
                src = ImageOps.exif_transpose(img).convert("RGB")
                src.thumbnail((cell_w - 12, cell_h - 12), Image.Resampling.LANCZOS)
                px = x0 + (cell_w - src.width) // 2
                py = y0 + (cell_h - src.height) // 2
                canvas.paste(src, (px, py))
        except Exception:
            draw.line((x0 + 12, y0 + 12, x1 - 12, y1 - 12), fill=(190, 80, 80), width=3)
            draw.line((x1 - 12, y0 + 12, x0 + 12, y1 - 12), fill=(190, 80, 80), width=3)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".refsheet{sheet_index}.jpg")
    tmp_path = Path(tmp.name)
    tmp.close()
    canvas.save(tmp_path, format="JPEG", quality=88, optimize=True)
    return tmp_path


def build_support_reference_uploads(
    support_paths: list[Path],
    hero_path: Path,
    max_uploads: int,
) -> tuple[list[Path], list[Path], int, int]:
    hero_key = str(Path(hero_path).resolve()) if hero_path else ""
    refs = unique_existing_paths(support_paths, exclude={hero_key} if hero_key else set())
    if max_uploads <= 0 or not refs:
        return [], [], len(refs), 0
    if len(refs) <= max_uploads:
        return refs, [], len(refs), 0

    chunks = split_evenly(refs, max_uploads)
    sheet_paths = [create_reference_contact_sheet(chunk, idx) for idx, chunk in enumerate(chunks, start=1)]
    return sheet_paths, sheet_paths, len(refs), len(sheet_paths)


def parse_size_text(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", str(value or "").lower())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def resize_saved_image_if_needed(path: Path, cfg: AppConfig):
    target_size = normalized_target_size(cfg)
    if not target_size:
        return

    with Image.open(path) as img:
        src = ImageOps.exif_transpose(img).convert("RGB")
        if src.size == target_size:
            return
        target_w, target_h = target_size
        target_ratio = target_w / target_h
        source_ratio = src.width / src.height
        ratio_delta = abs(source_ratio - target_ratio) / target_ratio
        if ratio_delta > 0.025:
            raise RuntimeError(
                "生成结果不是竖向 3:4，已拒绝保存，避免把横版图补白成竖图："
                f"原图 {src.width}x{src.height}，目标 {target_w}x{target_h}"
            )
        resized = src.resize(target_size, Image.Resampling.LANCZOS)
        resized.save(path, format="JPEG", quality=95, optimize=True)


def has_horizontal_letterbox_padding(img: Image.Image) -> bool:
    if img.width <= 0 or img.height <= 0:
        return False

    sample_w = min(320, img.width)
    sample_h = min(480, img.height)
    small = img.convert("RGB").resize((sample_w, sample_h), Image.Resampling.BILINEAR)
    pixels = small.load()

    def is_non_white(x: int, y: int) -> bool:
        r, g, b = pixels[x, y]
        return not (r >= 248 and g >= 248 and b >= 248)

    row_has_content = []
    for y in range(sample_h):
        non_white = sum(1 for x in range(sample_w) if is_non_white(x, y))
        row_has_content.append(non_white / sample_w >= 0.06)

    try:
        first_row = next(i for i, has_content in enumerate(row_has_content) if has_content)
        last_row = sample_h - 1 - next(
            i for i, has_content in enumerate(reversed(row_has_content)) if has_content
        )
    except StopIteration:
        return False

    top_blank_ratio = first_row / sample_h
    bottom_blank_ratio = (sample_h - 1 - last_row) / sample_h
    content_height_ratio = (last_row - first_row + 1) / sample_h

    content_rows = max(1, last_row - first_row + 1)
    col_has_content = []
    for x in range(sample_w):
        non_white = sum(1 for y in range(first_row, last_row + 1) if is_non_white(x, y))
        col_has_content.append(non_white / content_rows >= 0.04)

    try:
        first_col = next(i for i, has_content in enumerate(col_has_content) if has_content)
        last_col = sample_w - 1 - next(
            i for i, has_content in enumerate(reversed(col_has_content)) if has_content
        )
    except StopIteration:
        return False

    content_width_ratio = (last_col - first_col + 1) / sample_w
    return (
        top_blank_ratio >= 0.16
        and bottom_blank_ratio >= 0.10
        and (top_blank_ratio + bottom_blank_ratio) >= 0.30
        and content_height_ratio <= 0.70
        and content_width_ratio >= 0.70
    )


def validate_generated_image_file(path: Path, cfg: AppConfig | None = None):
    with Image.open(path) as img:
        img.verify()

    if cfg is None:
        return

    target_size = normalized_target_size(cfg)
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        if target_size and img.size != target_size:
            raise RuntimeError(f"图片尺寸不符合目标：{img.width}x{img.height}，目标 {target_size[0]}x{target_size[1]}")
        if wants_vertical_3x4(cfg):
            if img.width >= img.height:
                raise RuntimeError(f"图片不是竖版：{img.width}x{img.height}")
            if has_horizontal_letterbox_padding(img):
                raise RuntimeError("检测到横版内容被放进竖向白色画布，上下有大面积白边，已拒绝保存")


def write_image_bytes_atomic(out_path: Path, image_bytes: bytes, cfg: AppConfig | None = None):
    ensure_dir(out_path.parent)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_bytes(image_bytes)
    try:
        with Image.open(tmp_path) as img:
            img.verify()

        if cfg is not None:
            resize_saved_image_if_needed(tmp_path, cfg)

        validate_generated_image_file(tmp_path, cfg)

        tmp_path.replace(out_path)
    except Exception:
        try:
            tmp_path.unlink()
        except Exception:
            pass
        raise


def generated_image_file_is_valid(path: Path, cfg: AppConfig | None = None) -> bool:
    try:
        if not path.exists() or not path.is_file() or path.stat().st_size <= 0:
            return False
        validate_generated_image_file(path, cfg)
        return True
    except Exception:
        return False


def decode_image_base64(value: str) -> bytes:
    try:
        cleaned = re.sub(r"\s+", "", str(value or ""))
        cleaned += "=" * (-len(cleaned) % 4)
        return base64.b64decode(cleaned, validate=True)
    except Exception as e:
        raise RuntimeError(f"生图接口返回的 Base64 图片数据不完整或已损坏：{e}") from e


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def robust_parse_first_json(text: str, fallback=None):
    if fallback is None:
        fallback = {}
    if not text:
        return fallback

    text = text.strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.I)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except Exception:
        pass

    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                return obj
            except Exception:
                continue
    return fallback


def extract_text(resp_json: dict) -> str:
    try:
        content = resp_json["choices"][0]["message"].get("content", "")
    except Exception:
        return json.dumps(resp_json, ensure_ascii=False)

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        arr = []
        for item in content:
            if isinstance(item, dict):
                if "text" in item:
                    arr.append(str(item["text"]))
                elif item.get("type") == "text" and "content" in item:
                    arr.append(str(item["content"]))
        return "\n".join(arr)
    return str(content)


def file_to_data_url(path: Path) -> str:
    return DATA_URL_CACHE.get(path)


class DataUrlCache:
    def __init__(self):
        self.cache = {}

    def get(self, path: Path) -> str:
        path = Path(path)
        stat = path.stat()
        key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        if key not in self.cache:
            mime, _ = mimetypes.guess_type(str(path))
            if not mime:
                mime = "image/jpeg"
            b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
            self.cache[key] = f"data:{mime};base64,{b64}"
        return self.cache[key]


DATA_URL_CACHE = DataUrlCache()


async def bounded_gather(items, worker, limit, return_exceptions=True):
    sem = asyncio.Semaphore(max(1, int(limit or 1)))

    async def run_one(item):
        async with sem:
            return await worker(item)

    return await asyncio.gather(
        *(run_one(item) for item in items),
        return_exceptions=return_exceptions
    )


def file_to_data_url_uncached(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        mime = "image/jpeg"
    b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def list_images(folder: Path, out_subfolder: str, max_image_mb: int) -> list[Path]:
    exts = ["*.jpg", "*.jpeg", "*.png", "*.webp"]
    paths = []
    for ext in exts:
        paths += list(folder.glob(ext))

    result = []
    for p in paths:
        try:
            if out_subfolder in p.parts:
                continue
            size_mb = p.stat().st_size / 1024 / 1024
            if size_mb <= max_image_mb:
                result.append(p)
        except Exception:
            pass
    return sorted(result)


def split_sku_hero_and_others(paths: list[Path], sku_prefix: str):
    sku = []
    others = []
    pref = sku_prefix.lower()
    for p in paths:
        if p.name.lower().startswith(pref):
            sku.append(p)
        else:
            others.append(p)
    return sku, others


def discover_product_folders(root: Path, cfg: AppConfig) -> list[Path]:
    imgs = list_images(root, cfg.out_subfolder, cfg.max_image_mb)
    sku, _ = split_sku_hero_and_others(imgs, cfg.sku_prefix)
    if sku:
        return [root]

    folders = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        if p.name == cfg.out_subfolder or p.name.startswith("_"):
            continue
        imgs = list_images(p, cfg.out_subfolder, cfg.max_image_mb)
        sku, _ = split_sku_hero_and_others(imgs, cfg.sku_prefix)
        if sku:
            folders.append(p)
    return folders


def inspect_product_generation(product_folder: Path, cfg: AppConfig) -> dict:
    """Inspect one product without calling AI so a batch can resume safely."""
    imgs = list_images(product_folder, cfg.out_subfolder, cfg.max_image_mb)
    sku_paths, _ = split_sku_hero_and_others(imgs, cfg.sku_prefix)
    out_dir = product_folder / cfg.out_subfolder
    plan_path = out_dir / "image_plan.json"
    plan_images = []
    if plan_path.is_file():
        try:
            saved_plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan_images = list(saved_plan.get("images") or [])
        except (OSError, ValueError, TypeError):
            plan_images = []

    missing = []
    if cfg.generate_for_each_sku_cover:
        missing.extend(
            build_sku_cover_filename(path)
            for path in sku_paths
            if not generated_image_file_is_valid(
                out_dir / build_sku_cover_filename(path), cfg,
            )
        )

    if not plan_images:
        return {
            "complete": False,
            "missing": missing,
            "reason": "missing_plan",
            "sku_count": len(sku_paths),
        }

    main_item = next(
        (
            item for item in plan_images
            if "hero" in str(item.get("image_type", "")).lower()
            or "main" in str(item.get("id", "")).lower()
        ),
        plan_images[0],
    )
    appendix_items = [
        item for item in plan_images
        if not (cfg.generate_for_each_sku_cover and item is main_item)
    ]
    missing.extend(
        build_appendix_filename(index)
        for index, _item in enumerate(appendix_items, start=1)
        if not generated_image_file_is_valid(
            out_dir / build_appendix_filename(index), cfg,
        )
    )
    return {
        "complete": bool(sku_paths) and not missing,
        "missing": missing,
        "reason": "missing_outputs" if missing else "complete",
        "sku_count": len(sku_paths),
    }


def infer_category_name(product_hint: str, category_en: str = "", product_name_zh: str = "") -> str:
    text = f"{product_hint} {category_en} {product_name_zh}".lower()

    rules = [
        ("服装", ["衣服", "服装", "t-shirt", "shirt", "dress", "pants", "coat", "jacket", "skirt", "clothing", "apparel"]),
        ("配饰", ["包", "帽", "围巾", "手套", "首饰", "项链", "耳环", "戒指", "眼镜", "accessory", "bag", "hat", "jewelry"]),
        ("家居装饰", ["装饰", "摆件", "花瓶", "香薰", "灯饰", "装饰画", "雕塑", "decor", "decoration", "vase"]),
        ("工具", ["工具", "扳手", "钳", "钻", "锯", "tool", "repair", "wrench", "pliers", "drill"]),
        ("收纳", ["收纳", "整理", "置物", "storage", "organizer", "box", "bag"]),
        ("厨房用品", ["厨房", "锅", "刀", "砧板", "保鲜", "kitchen", "pan", "knife", "cutting", "container"]),
        ("电子产品", ["电子", "充电", "数据线", "耳机", "支架", "灯", "电器", "charger", "cable", "electronic", "device"]),
        ("美妆个护", ["美妆", "护肤", "化妆", "洗脸", "美容", "beauty", "makeup", "skin", "care"]),
        ("宠物用品", ["宠物", "猫", "狗", "pet", "cat", "dog"]),
        ("母婴用品", ["婴儿", "儿童", "宝宝", "母婴", "baby", "kids", "children"]),
        ("运动户外", ["运动", "户外", "健身", "露营", "sports", "outdoor", "fitness", "camping"]),
        ("汽车用品", ["汽车", "车载", "车内", "后备箱", "car", "auto", "vehicle"])
    ]

    for name, kws in rules:
        if any(k.lower() in text for k in kws):
            return name

    return "通用商品"


def get_effective_category_profile(cfg: AppConfig, product_info: dict) -> tuple[str, dict]:
    if cfg.category_name and cfg.category_name != "自动识别":
        return cfg.category_name, CATEGORY_PROFILES[cfg.category_name]

    inferred = infer_category_name(
        cfg.product_hint,
        product_info.get("category_en", ""),
        product_info.get("product_name_zh", "")
    )
    return inferred, CATEGORY_PROFILES.get(inferred, CATEGORY_PROFILES["通用商品"])


# =========================
# API 客户端
# =========================

def build_api_url(host: str, path: str) -> str:
    host = str(host or "").strip().rstrip("/")
    path = "/" + str(path or "").strip().lstrip("/")
    if not host.startswith(("http://", "https://")):
        host = "https://" + host
    return host + path

class ApiClient:
    def __init__(self, cfg: AppConfig, log_func):
        self.cfg = cfg
        self.log = log_func
        self.analysis_url = build_api_url(
            cfg.analysis_api_host or cfg.api_host,
            cfg.analysis_chat_path or cfg.chat_path,
        )
        self.image_chat_url = build_api_url(
            cfg.image_api_host or cfg.api_host,
            cfg.image_chat_path or cfg.chat_path,
        )
        self.image_edit_url = build_api_url(
            cfg.image_api_host or cfg.api_host,
            cfg.image_edit_path,
        )
        self.analysis_api_key = cfg.analysis_api_key or cfg.api_key
        self.image_api_key = cfg.image_api_key or cfg.api_key
        self.semaphore = asyncio.Semaphore(cfg.max_concurrent_requests)

    async def chat_async(
        self,
        session: aiohttp.ClientSession,
        model: str,
        messages: list,
        max_tokens: int = 1600,
        timeout: int = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        extra_payload: dict | None = None,
        api_kind: str = "analysis",
    ):
        last_err = None
        timeout = int(getattr(self.cfg, "request_timeout", timeout) or timeout)
        retries = int(getattr(self.cfg, "request_retries", retries) or retries)

        for attempt in range(retries):
            try:
                payload = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens
                }
                if extra_payload:
                    payload.update(extra_payload)

                request_url = self.image_chat_url if api_kind == "image" else self.analysis_url
                request_key = self.image_api_key if api_kind == "image" else self.analysis_api_key

                async with self.semaphore:
                    async with ai_async_post("image" if api_kind == "image" else "text", session,
                        request_url,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {request_key}",
                            "Content-Type": "application/json; charset=utf-8"
                        },
                        timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as resp:
                        raw = await resp.text()

                        if resp.status != 200:
                            if resp.status in [408, 409, 425, 429, 500, 502, 503, 504]:
                                raise RuntimeError(f"HTTP {resp.status}: {raw[:1200]}")
                            raise Exception(f"不可重试错误 HTTP {resp.status}: {raw[:1200]}")

                        try:
                            j = json.loads(raw)
                        except Exception:
                            raise RuntimeError(f"响应不是合法 JSON: {raw[:1200]}")

                        if "error" in j:
                            raise RuntimeError(str(j["error"]))

                        return j

            except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as e:
                last_err = e
                sleep_s = min(2 ** attempt, 20)
                self.log(
                    f"  [超时 {attempt + 1}/{retries}] 请求超过 {timeout}s，等待 {sleep_s}s 后重试。"
                    "如持续超时，请调大请求超时秒。"
                )
                await asyncio.sleep(sleep_s)
            except Exception as e:
                if "不可重试错误" in str(e):
                    raise
                last_err = e
                sleep_s = min(2 ** attempt, 20)
                self.log(f"  [重试 {attempt + 1}/{retries}] 请求失败，等待 {sleep_s}s：{e}")
                await asyncio.sleep(sleep_s)

        raise RuntimeError(f"Request failed after {retries} retries: {last_err}")

    async def image_edit_async(
        self,
        session: aiohttp.ClientSession,
        prompt: str,
        image_paths: list[Path],
        timeout: int = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
    ):
        last_err = None
        timeout = int(getattr(self.cfg, "request_timeout", timeout) or timeout)
        retries = int(getattr(self.cfg, "request_retries", retries) or retries)

        for attempt in range(retries):
            file_handles = []
            temp_upload_paths = []
            try:
                is_gpt_ge = is_gpt_ge_endpoint(self.image_edit_url)
                upload_image_paths = list(image_paths or [])
                if is_gpt_ge:
                    upload_image_paths = upload_image_paths[:GPT_GE_MAX_EDIT_IMAGES]

                form = aiohttp.FormData()
                for path in upload_image_paths:
                    upload_path = path
                    upload_filename = path.name
                    if is_gpt_ge:
                        prepared_path = prepare_gpt_ge_edit_upload(path)
                        if prepared_path is not None:
                            upload_path = prepared_path
                            upload_filename = f"{path.stem}.jpg"
                            temp_upload_paths.append(prepared_path)
                    fh = upload_path.open("rb")
                    file_handles.append(fh)
                    form.add_field(
                        "image",
                        fh,
                        filename=upload_filename,
                        content_type=mimetypes.guess_type(upload_filename)[0] or "application/octet-stream",
                    )
                request_prompt = compact_image_edit_prompt(prompt, self.image_edit_url)
                if len(request_prompt) < len(str(prompt or "")):
                    self.log(
                        f"    [提示词自动压缩] {len(str(prompt or ''))} -> "
                        f"{len(request_prompt)} 字符"
                    )
                form.add_field("prompt", request_prompt)
                form.add_field("model", self.cfg.image_model)
                form.add_field("n", "1")
                image_size = effective_image_edit_size(self.cfg)
                if image_size:
                    form.add_field("size", image_size)
                image_quality = effective_image_quality(self.cfg)
                if image_quality:
                    form.add_field("quality", image_quality)
                if self.cfg.image_model != "gpt-image-1":
                    form.add_field("response_format", "b64_json")

                async with self.semaphore:
                    async with ai_async_post("image", session,
                        self.image_edit_url,
                        usage_model=self.cfg.image_model,
                        data=form,
                        headers={"Authorization": f"Bearer {self.image_api_key}"},
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as resp:
                        raw = await resp.text()
                        if resp.status != 200:
                            if resp.status in [408, 409, 425, 429, 500, 502, 503, 504]:
                                raise RuntimeError(f"HTTP {resp.status}: {raw[:1200]}")
                            raise Exception(f"不可重试错误 HTTP {resp.status}: {raw[:1200]}")
                        try:
                            j = json.loads(raw)
                        except Exception:
                            raise RuntimeError(f"响应不是合法 JSON: {raw[:1200]}")
                        if "error" in j:
                            raise RuntimeError(str(j["error"]))
                        return j
            except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as e:
                last_err = e
                sleep_s = min(2 ** attempt, 20)
                self.log(
                    f"  [超时 {attempt + 1}/{retries}] 生图请求超过 {timeout}s，等待 {sleep_s}s 后重试。"
                    "如持续超时，请调大请求超时秒。"
                )
                await asyncio.sleep(sleep_s)
            except Exception as e:
                if "不可重试错误" in str(e):
                    raise
                last_err = e
                sleep_s = min(2 ** attempt, 20)
                self.log(f"  [重试 {attempt + 1}/{retries}] 生图请求失败，等待 {sleep_s}s: {e}")
                await asyncio.sleep(sleep_s)
            finally:
                for fh in file_handles:
                    try:
                        fh.close()
                    except Exception:
                        pass
                for tmp_path in temp_upload_paths:
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass

        raise RuntimeError(f"Image edit request failed after {retries} retries: {last_err}")


# =========================
# AI 分析
# =========================

async def detect_has_heavy_text(client: ApiClient, session, img_data_url: str) -> dict:
    system = "你是图像审核员，判断图片上是否有大量宣传文字/大字版面。只输出JSON。"
    user = [
        {
            "type": "text",
            "text": (
                "输出JSON：{\"has_heavy_text\": true/false, \"confidence\": 0.0}。"
                "如果存在大块文字信息栏、多行中文、广告语、参数版式、卖点图层，则 true。只输出JSON。"
            )
        },
        {"type": "image_url", "image_url": {"url": img_data_url}}
    ]
    resp = await client.chat_async(
        session,
        client.cfg.analysis_model,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=200
    )
    return robust_parse_first_json(extract_text(resp), {"has_heavy_text": False, "confidence": 0.0})


async def find_clean_base_hero(client: ApiClient, session, sku_paths: list[Path]) -> Path:
    if getattr(client.cfg, "skip_sku_text_detection", False):
        client.log(f"  [1/8] 跳过 SKU 主图文字检测，使用：{sku_paths[0].name}")
        return sku_paths[0]

    client.log(f"  [1/8] 检测 SKU 主图文字，共 {len(sku_paths)} 张...")
    scored = []

    for p in sku_paths:
        try:
            info = await detect_has_heavy_text(client, session, file_to_data_url(p))
            heavy = bool(info.get("has_heavy_text"))
            conf = float(info.get("confidence") or 0)
        except Exception as e:
            client.log(f"    文字检测失败，默认可用：{p.name}，{e}")
            heavy = False
            conf = 0.0
        scored.append((heavy, -conf, p))

    scored.sort(key=lambda x: (x[0], x[1]))
    client.log(f"  [OK] 基础 HERO：{scored[0][2].name}")
    return scored[0][2]


async def detect_has_model(client: ApiClient, session, img_data_url: str) -> bool:
    system = "判断图片中是否有真人、真人手部或模特展示商品。只回答 true 或 false。"
    user = [
        {"type": "text", "text": "Есть ли человек, рука человека или модель, демонстрирующая товар? Ответь только true или false."},
        {"type": "image_url", "image_url": {"url": img_data_url}}
    ]
    resp = await client.chat_async(
        session,
        client.cfg.analysis_model,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=10
    )
    return "true" in extract_text(resp).lower()


async def detect_any_model(client: ApiClient, session, paths: list[Path], max_check: int = 5) -> bool:
    for p in paths[:max_check]:
        try:
            if await detect_has_model(client, session, file_to_data_url(p)):
                return True
        except Exception as e:
            client.log(f"    模特检测失败：{p.name}，{e}")
    return False


async def analyze_product_universal(client: ApiClient, session, hero_url: str, other_urls: list[str], product_hint: str) -> dict:
    system = "You are a Russian e-commerce creative director for OZON. Return strict JSON only."

    user_text = f"""
已知商品提示：{product_hint}

根据商品图识别商品信息，并返回严格 JSON：
{{
  "category_en": "...",
  "product_name_ru": "...",
  "product_name_zh": "...",
  "target_audience_ru": "...",
  "use_scene_ru": "...",
  "scene_background_ru": "...",
  "headline_ru": "...",
  "subheadline_ru": "...",
  "benefits_ru": ["...", "...", "...", "...", "..."],
  "feature_callouts_ru": ["...", "...", "..."],
  "detail_focus_ru": "...",
  "comparison_angle_ru": "...",
  "usage_steps_ru": ["...", "...", "..."],
  "physical_behavior": "rigid|flexible|articulated|wearable|openable|mixed",
  "allowed_transformations": ["...", "..."],
  "identity_anchors": ["...", "..."],
  "scene_integration_ru": "...",
  "bundle_possible": true,
  "confidence": 0.0
}}

要求：
1. 优先参考商品提示。
2. headline_ru 是功能概括，不是促销。
3. 禁止 скидка, акция, sale, best, premium。
4. 文案适合 российского маркетплейса, не упоминайте OZON.。
5. 判断商品的物理属性。allowed_transformations 说明为了真实佩戴或使用可以发生的弯曲、垂坠、开合、旋转、透视和遮挡；identity_anchors 只保留识别这款商品所必需的设计特征。
6. scene_integration_ru 说明商品如何与人体、桌面、容器或环境真实接触，避免像平面贴图覆盖在场景上。
7. 只输出 JSON。
"""

    content = [{"type": "text", "text": user_text}]
    content.append({"type": "image_url", "image_url": {"url": hero_url}})
    for u in other_urls[:MAX_PRODUCT_IMAGES_FOR_ANALYSIS - 1]:
        content.append({"type": "image_url", "image_url": {"url": u}})

    fallback = {
        "category_en": "generic_product",
        "product_name_ru": "Товар",
        "product_name_zh": product_hint or "商品",
        "target_audience_ru": "для дома",
        "use_scene_ru": "использование товара по назначению",
        "scene_background_ru": "реалистичная сцена использования товара",
        "headline_ru": "УДОБНОЕ И ПРАКТИЧНОЕ ИСПОЛЬЗОВАНИЕ",
        "subheadline_ru": "ПОДХОДИТ ДЛЯ ЕЖЕДНЕВНЫХ ЗАДАЧ",
        "benefits_ru": ["Удобная конструкция", "Практичное использование", "Надежные материалы", "Компактное хранение"],
        "feature_callouts_ru": ["Качественные материалы", "Продуманная конструкция", "Удобно в использовании"],
        "detail_focus_ru": "показать материал и ключевые детали",
        "comparison_angle_ru": "сравнить удобство с обычным аналогом",
        "usage_steps_ru": ["Подготовьте товар", "Разместите в нужном месте", "Используйте по назначению"],
        "physical_behavior": "mixed",
        "allowed_transformations": ["естественный ракурс и перспектива", "физически правдоподобный контакт и частичное перекрытие"],
        "identity_anchors": ["основной силуэт", "цвет и материал", "характерные детали конструкции"],
        "scene_integration_ru": "перерисовать товар как реальный объемный объект с естественным контактом, тенями и перспективой",
        "bundle_possible": False,
        "confidence": 0.0
    }

    resp = await client.chat_async(
        session,
        client.cfg.analysis_model,
        [{"role": "system", "content": system}, {"role": "user", "content": content}],
        max_tokens=1300
    )

    parsed = robust_parse_first_json(extract_text(resp), fallback)
    for k, v in fallback.items():
        if k not in parsed:
            parsed[k] = v
    return parsed


async def analyze_visual_style(client: ApiClient, session, hero_url: str, other_urls: list[str], category_en: str, product_hint: str) -> dict:
    cfg = client.cfg

    if cfg.style_name != "自动":
        preset = STYLE_PRESETS[cfg.style_name]
        return {
            "palette": {
                "name": cfg.style_name,
                "primary": "#2E5BFF",
                "secondary": "#F3F6FA",
                "accent": "#FFD54F",
                "bg_description_ru": preset["prompt_ru"]
            },
            "style_keywords_ru": [cfg.style_name, "реалистичный", "чистый"],
            "composition_advice_ru": "товар крупно, композиция понятная, свободное пространство, без текстовых блоков если шаблон этого не требует",
            "lighting_advice_ru": "естественный или мягкий коммерческий свет",
            "decor_elements_ru": "минимальные элементы, не отвлекающие от товара",
            "preset_prompt_ru": preset["prompt_ru"]
        }

    system = "You are a marketplace lifestyle photography director. Return strict JSON only."
    user_text = f"""
Product hint: {product_hint}
Product category: {category_en}

Analyze the sold product, not the existing photo design. Ignore the reference background, color cast,
surface, lighting, typography, props, and composition. Choose a fresh, clean, neutral-daylight everyday
environment based on how this exact product is actually used. Never recommend muddy beige/gray grading,
yellow haze, an empty studio, a showroom podium, foreground plants, dried flowers, or decorative gold props.

Return JSON:
{{
  "palette": {{
    "name": "...",
    "primary": "#RRGGBB",
    "secondary": "#RRGGBB",
    "accent": "#RRGGBB",
    "bg_description_ru": "..."
  }},
  "style_keywords_ru": ["...", "..."],
  "composition_advice_ru": "...",
  "lighting_advice_ru": "...",
  "decor_elements_ru": "...",
  "preset_prompt_ru": ""
}}
Only JSON.
"""

    content = [{"type": "text", "text": user_text}, {"type": "image_url", "image_url": {"url": hero_url}}]
    for u in other_urls[:4]:
        content.append({"type": "image_url", "image_url": {"url": u}})

    fallback = {
        "palette": {
            "name": "Marketplace Clean",
            "primary": "#2E5BFF",
            "secondary": "#EAF0FF",
            "accent": "#FFD54F",
            "bg_description_ru": "чистый светлый фон"
        },
        "style_keywords_ru": ["чистый", "современный", "реалистичный"],
        "composition_advice_ru": "крупный товар в центре, свободное пространство, без текстовых блоков если шаблон этого не требует",
        "lighting_advice_ru": "чистый нейтральный дневной свет, точный баланс белого, без желтой дымки",
        "decor_elements_ru": "только 1-3 функциональных бытовых предмета; без растений, сухоцветов, золота и подиумов",
        "preset_prompt_ru": ""
    }

    resp = await client.chat_async(
        session,
        cfg.analysis_model,
        [{"role": "system", "content": system}, {"role": "user", "content": content}],
        max_tokens=500
    )

    parsed = robust_parse_first_json(extract_text(resp), fallback)
    for k, v in fallback.items():
        if k not in parsed:
            parsed[k] = v
    return parsed


async def detect_color_material_from_image(client: ApiClient, session, hero_url: str) -> dict:
    system = "Ты эксперт по точному распознаванию цвета товара. Верни только JSON."
    user = [
        {
            "type": "text",
            "text": (
                "Определи цвет именно корпуса/ткани/поверхности продаваемого товара, а не фон, "
                "освещение, упаковку, декор или цветовой рефлекс. Укажи точный основной оттенок.\n"
                "Верни JSON:\n"
                "{\n"
                "  \"color_ru\": \"...\",\n"
                "  \"material_ru\": \"...\",\n"
                "  \"finish_ru\": \"...\",\n"
                "  \"confidence\": 0.0\n"
                "}"
            )
        },
        {"type": "image_url", "image_url": {"url": hero_url}}
    ]
    fallback = {"color_ru": "", "material_ru": "", "finish_ru": "", "confidence": 0.0}

    resp = await client.chat_async(
        session,
        client.cfg.analysis_model,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=220
    )
    return robust_parse_first_json(extract_text(resp), fallback)


def build_sku_color_lock_rule(sku_path: Path, color_info: dict) -> str:
    color = (color_info.get("color_ru") or "").strip()
    material = (color_info.get("material_ru") or "").strip()
    finish = (color_info.get("finish_ru") or "").strip()
    detected = ", ".join(value for value in (color, material, finish) if value)
    detected_line = detected or "определяется только по PRIMARY SKU reference"

    return f"""
- Это главное изображение строго для SKU: {sku_path.name}.
- Единственный источник цвета товара: первая загруженная картинка PRIMARY SKU reference.
- Цвет/материал/фактура текущего SKU: {detected_line}.
- Покажи только этот цвет товара. Не смешивай его с другими цветовыми вариантами.
- Игнорируй любые названия цветов из общего описания товара, палитры стиля, фона и декора, если они отличаются от PRIMARY SKU reference.
- Цветовая палитра дизайна относится только к фону, тексту и графическим элементам; она не должна перекрашивать товар.
- Не добавляй цветной свет, рефлексы или фильтры, меняющие оттенок товара.
- Сохрани исходные hue, saturation, brightness, материал и отделку товара максимально точно.
""".strip()


async def find_bundle_reference(client: ApiClient, session, other_paths: list[Path]) -> Path | None:
    if not other_paths:
        return None

    system = "Pick the best image that looks like accessories/bundle/packaging. Return JSON only."
    items = [(i, p, file_to_data_url(p)) for i, p in enumerate(other_paths[:12])]

    user = [{"type": "text", "text": "Choose best accessories/bundle/packaging image. Return {\"best_index\": -1}"}]
    for i, p, u in items:
        user.append({"type": "text", "text": f"IMAGE {i}: {p.name}"})
        user.append({"type": "image_url", "image_url": {"url": u}})

    try:
        resp = await client.chat_async(
            session,
            client.cfg.analysis_model,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=100
        )
        j = robust_parse_first_json(extract_text(resp), {"best_index": -1})
        idx = int(j.get("best_index", -1))
        if 0 <= idx < len(items):
            return items[idx][1]
    except Exception as e:
        client.log(f"    查找配件参考失败：{e}")

    return None


async def find_dimension_reference(
    client: ApiClient,
    session,
    other_paths: list[Path],
) -> tuple[Path | None, list[str]]:
    if not other_paths:
        return None, []
    unique_paths = []
    seen_paths = set()
    for path in other_paths:
        key = str(path.resolve())
        if key not in seen_paths:
            seen_paths.add(key)
            unique_paths.append(path)

    filename_keywords = (
        "尺寸", "规格", "参数", "大小", "长宽高", "size", "sizes",
        "dimension", "dimensions", "measurement", "measurements",
    )
    filename_match = next(
        (p for p in unique_paths if any(word in p.stem.casefold() for word in filename_keywords)),
        None,
    )
    candidate_paths = []
    if filename_match:
        candidate_paths.append(filename_match)
    candidate_paths.extend(p for p in unique_paths if p != filename_match)
    items = [(i, p, file_to_data_url(p)) for i, p in enumerate(candidate_paths[:12])]
    system = (
        "You inspect product reference images. Find the image that contains real dimension "
        "annotations: numeric measurements, units, arrows, dimension lines, or a size diagram. "
        "Transcribe visible measurement labels exactly. Return strict JSON only."
    )
    user = [{
        "type": "text",
        "text": (
            "Return JSON: {\"best_index\": -1, \"dimension_labels\": [], "
            "\"has_exact_dimensions\": false, \"confidence\": 0.0}. "
            "Do not infer or calculate dimensions. Copy only text visibly printed in the image."
        ),
    }]
    for i, p, url in items:
        user.append({"type": "text", "text": f"IMAGE {i}: {p.name}"})
        user.append({"type": "image_url", "image_url": {"url": url}})

    try:
        resp = await client.chat_async(
            session,
            client.cfg.analysis_model,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=350,
        )
        result = robust_parse_first_json(extract_text(resp), {})
        idx = int(result.get("best_index", -1))
        raw_labels = result.get("dimension_labels") or []
        if isinstance(raw_labels, str):
            raw_labels = [raw_labels]
        labels = [
            str(value).strip()
            for value in raw_labels
            if str(value).strip()
        ]
        if 0 <= idx < len(items):
            return items[idx][1], labels
    except Exception as exc:
        client.log(f"    查找尺寸参考失败：{exc}")

    return filename_match, []


async def inspect_dimension_output(
    client: ApiClient,
    session,
    image_path: Path,
    expected_labels: list[str] | None = None,
) -> dict:
    expected_text = ", ".join(expected_labels or []) or "not provided"
    content = [
        {
            "type": "text",
            "text": (
                "Inspect this generated dimension infographic. Return strict JSON: "
                "{\"has_dimension_lines\": false, \"has_numeric_measurements\": false, "
                "\"product_visible\": false, \"is_polished_marketplace_image\": false, "
                "\"has_bad_gray_sketch_style\": false, \"visible_measurements\": [], \"confidence\": 0.0}. "
                "Dimension lines must be visible arrows/guide lines connected to product edges. "
                "A polished marketplace image must be bright, full-color or clean white-background, realistic, and readable. "
                "Set has_bad_gray_sketch_style=true for dark gray vignettes, monochrome sketches/renders, embossed giant text, distorted Cyrillic, or low-quality poster styling. "
                f"Expected reference labels, if available: {expected_text}."
            ),
        },
        {"type": "image_url", "image_url": {"url": file_to_data_url(image_path)}},
    ]
    fallback = {
        "has_dimension_lines": False,
        "has_numeric_measurements": False,
        "product_visible": False,
        "is_polished_marketplace_image": False,
        "has_bad_gray_sketch_style": False,
        "visible_measurements": [],
        "confidence": 0.0,
    }
    try:
        resp = await client.chat_async(
            session,
            client.cfg.analysis_model,
            [{"role": "user", "content": content}],
            max_tokens=250,
        )
        return robust_parse_first_json(extract_text(resp), fallback)
    except Exception as exc:
        client.log(f"    尺寸图质量检查失败：{exc}")
        return fallback


def contains_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", str(text or "")))


def clamp_image_plan_count(value) -> int:
    try:
        count = int(value)
    except Exception:
        count = IMAGE_PLAN_MIN_COUNT
    return max(IMAGE_PLAN_MIN_COUNT, min(IMAGE_PLAN_MAX_COUNT, count))


def desired_image_plan_count(cfg: AppConfig) -> int:
    return clamp_image_plan_count(getattr(cfg, "image_plan_count", DEFAULT_IMAGE_PLAN_COUNT))


def build_light_product_context(cfg: AppConfig) -> dict:
    return {
        "category_en": "",
        "product_name_ru": "",
        "product_name_zh": cfg.product_hint or "",
        "target_audience_ru": "",
        "use_scene_ru": "",
        "scene_background_ru": "",
        "headline_ru": "",
        "subheadline_ru": "",
        "benefits_ru": [],
        "feature_callouts_ru": [],
        "detail_focus_ru": "",
        "comparison_angle_ru": "",
        "usage_steps_ru": [],
        "bundle_possible": False,
        "confidence": 0.0,
    }


def build_light_style_context(cfg: AppConfig) -> dict:
    style_name = cfg.style_name if cfg.style_name in STYLE_PRESETS else "自动"
    preset = STYLE_PRESETS.get(style_name, STYLE_PRESETS["自动"])
    return {
        "palette": {
            "name": style_name,
            "primary": "",
            "secondary": "",
            "accent": "",
            "bg_description_ru": preset.get("prompt_ru", ""),
        },
        "style_keywords_ru": [style_name] if style_name != "自动" else [],
        "composition_advice_ru": "",
        "lighting_advice_ru": "",
        "decor_elements_ru": "",
        "preset_prompt_ru": preset.get("prompt_ru", ""),
    }


def category_profile_for_planner(category_profile: dict) -> dict:
    return {
        "strategy_hint": category_profile.get("extra_prompt_ru") or category_profile.get("description", ""),
        "model_policy": category_profile.get("model_policy", "auto"),
    }


def build_verified_facts(product_info: dict, color_info: dict, dimension_labels: list[str], cfg: AppConfig) -> list[str]:
    facts = []

    def add(value):
        text = str(value or "").strip()
        if text and not contains_chinese(text) and text not in facts:
            facts.append(text)

    add(product_info.get("product_name_ru"))
    add(product_info.get("headline_ru"))
    add(product_info.get("subheadline_ru"))
    add(product_info.get("use_scene_ru"))
    add(product_info.get("detail_focus_ru"))
    add(product_info.get("comparison_angle_ru"))
    for value in dimension_labels or []:
        add(value)
    for key in ("benefits_ru", "feature_callouts_ru", "usage_steps_ru"):
        values = product_info.get(key) or []
        if isinstance(values, str):
            values = [values]
        for value in values:
            add(value)
    color_parts = [
        color_info.get("color_ru", ""),
        color_info.get("material_ru", ""),
        color_info.get("finish_ru", ""),
    ]
    add(" ".join(part for part in color_parts if part).strip())
    if cfg.product_hint:
        add(cfg.product_hint)
    return facts[:18]


def image_type_from_template_name(template_name: str) -> str:
    mapping = {
        "主图海报": "hero_product",
        "真实场景图": "lifestyle_scene",
        "第二场景图": "alternate_scene",
        "细节特写图": "detail_closeup",
        "尺寸结构图": "dimension_infographic",
        "卖点信息图": "benefit_infographic",
        "对比图": "comparison_infographic",
        "使用步骤图": "steps_infographic",
        "包装/配件图": "bundle_display",
        "模特展示图": "model_demo",
        "顶视平铺图": "flatlay",
        "氛围生活图": "lifestyle_mood",
        "容量展示图": "capacity_display",
        "使用前后图": "before_after",
        "接口功能图": "ports_features",
        "材质面料图": "material_texture",
    }
    return mapping.get(template_name, "product_image")


def append_direct_text_requirement(
    prompt: str,
    text_lines_ru: list[str],
    needs_text_in_image: bool,
) -> str:
    lines = [str(line).strip() for line in (text_lines_ru or []) if str(line).strip() and not contains_chinese(str(line))]
    if not needs_text_in_image and not lines:
        return prompt
    if not lines:
        return prompt
    return prompt.rstrip() + "\n\n" + (
        "Render the following short Russian text directly inside the final image. "
        "Use clean modern typography and keep it readable on mobile. Do not add extra unverified text:\n"
        + "\n".join(f"- {line}" for line in lines[:5])
    )


def build_sku_cover_text_lines(product_info: dict) -> list[str]:
    candidates = []

    def add(value):
        text = str(value or "").strip()
        if text and not contains_chinese(text) and text not in candidates:
            candidates.append(text)

    add(product_info.get("headline_ru"))
    benefits = product_info.get("benefits_ru") or []
    if isinstance(benefits, str):
        benefits = [benefits]
    for value in benefits[:2]:
        add(value)
    if not candidates:
        add(product_info.get("product_name_ru"))
    callouts = product_info.get("feature_callouts_ru") or []
    if isinstance(callouts, str):
        callouts = [callouts]
    for value in callouts[:2]:
        add(value)
    return candidates[:3]


def prepare_sku_cover_plan_item(image_item: dict, product_info: dict) -> dict:
    plan_item = dict(image_item or {})
    text_lines = normalize_text_overlay_lines(plan_item.get("text_lines_ru") or [])
    if not text_lines:
        text_lines = build_sku_cover_text_lines(product_info)

    base_prompt = str(plan_item.get("image2_prompt") or "").strip()
    design_style = str(plan_item.get("design_style") or "product-specific clean editorial poster").strip()
    layout_rule = str(plan_item.get("layout_direction") or "Choose an asymmetric product-specific layout with a clear mobile hierarchy.").strip()
    use_scene = str(product_info.get("use_scene_ru") or "").strip()
    scene_background = str(product_info.get("scene_background_ru") or "").strip()
    physical_behavior = str(product_info.get("physical_behavior") or "mixed").strip()
    transformations = product_info.get("allowed_transformations") or []
    identity_anchors = product_info.get("identity_anchors") or []
    scene_integration = str(product_info.get("scene_integration_ru") or "").strip()
    poster_prompt = f"""
Create a clean, high-conversion portrait 3:4 product poster with an authentic everyday-life foundation and refined Russian typography.
The analysis planner has creative authority over the scene, palette, camera, hierarchy, and typography. Follow its product-specific concept instead of applying a generic template.
Planner design style: {design_style}
Planner layout direction: {layout_rule}
Suggested real use: {use_scene}
Suggested environment: {scene_background}
The product must remain large, accurate, clearly recognizable, visually dominant, and occupy roughly 60-78% of the useful frame.
Use a real, recognizable everyday environment appropriate to the product. It should feel inhabited and useful, not like an empty photo studio, showroom pedestal, or graphic advertising set.
Treat the reference background and composition as disposable. Preserve the product's identity anchors, not its exact photographed pose.
Physical behavior: {physical_behavior}
Allowed scene-adaptation transformations: {json.dumps(transformations, ensure_ascii=False)}
Identity anchors to preserve: {json.dumps(identity_anchors, ensure_ascii=False)}
Required physical integration: {scene_integration}
Re-render the product as a three-dimensional object inside the new scene. Use realistic perspective, gravity, contact, occlusion, cast shadows, reflections, drape, bending, articulation, or opening when physically appropriate. Never paste a flat cutout over a person or background.
Use clean neutral daylight, accurate white balance, fresh whites, crisp product edges, and only a few functional props. The scene must look tidy, airy, hygienic, and visually calm.
Avoid muddy beige or gray color casts, yellow haze, smoky shadows, dirty-looking surfaces, visual noise, irrelevant foreground leaves, dried flowers, marble podiums, gold ornaments, and random props.
Do not reuse the generic beauty-ad layout with a huge title across the top and two rounded feature pills underneath.
Do not force beige, pink, cosmetic-table styling, dried flowers, gold arcs, or beauty props unless the product itself genuinely requires them.
Avoid generic marketplace templates. Preserve breathing room and a clear mobile hierarchy.
Render one short Russian headline and at most one additional compact verified benefit. Typography must support the composition, not dominate it.
Do not use a giant product-name heading, two identical rounded pills, repeated icon badges, dense copy, or more than two text groups.
Keep important product details and text away from outer edges where marketplace badges may overlap them.
""".strip()
    if base_prompt:
        plan_item["image2_prompt"] = poster_prompt + "\n\nCreative brief from planner:\n" + base_prompt
    else:
        plan_item["image2_prompt"] = poster_prompt
    plan_item["image_type"] = "editorial_lifestyle_poster"
    plan_item["design_style"] = design_style
    plan_item["layout_direction"] = layout_rule
    plan_item["needs_text_in_image"] = bool(text_lines)
    plan_item["text_lines_ru"] = text_lines[:2]
    return plan_item


def build_fallback_image_plan(
    product_info: dict,
    style_guide: dict,
    color_info: dict,
    category_profile: dict,
    dimension_labels: list[str],
    cfg: AppConfig,
) -> dict:
    product_name = product_info.get("product_name_ru") or product_info.get("product_name_zh") or cfg.product_hint or "product"
    target_count = desired_image_plan_count(cfg)
    include_dimension = bool(getattr(cfg, "generate_dimension_image", True))
    verified_facts = build_verified_facts(product_info, color_info, dimension_labels, cfg)
    ordered_templates = ["尺寸结构图"] if include_dimension and target_count == 1 else ["主图海报"]
    if include_dimension and target_count > 1:
        ordered_templates.append("尺寸结构图")
    fillers = ["真实场景图", "细节特写图", "容量展示图", "卖点信息图", "使用前后图", "氛围生活图", "使用步骤图", "包装/配件图"]
    filler_idx = 0
    while len(ordered_templates) < target_count:
        name = fillers[filler_idx % len(fillers)]
        if name != "尺寸结构图" or include_dimension:
            ordered_templates.append(name)
        filler_idx += 1
    ordered_templates = ordered_templates[:target_count]
    palette = style_guide.get("palette", {})
    visible_desc = ", ".join(
        value for value in [
            color_info.get("color_ru", ""),
            color_info.get("material_ru", ""),
            color_info.get("finish_ru", ""),
        ]
        if value
    ) or "exactly as shown in the product reference"

    images = []
    for idx, template_name in enumerate(ordered_templates, start=1):
        image_type = image_type_from_template_name(template_name)
        text_lines_ru = []
        needs_text_in_image = False
        text_instruction = ""
        if image_type == "dimension_infographic":
            text_lines_ru = [label for label in dimension_labels if not contains_chinese(label)]
            labels_text = ", ".join(text_lines_ru) if text_lines_ru else "only exact visible measurements from the reference, no invented numbers"
            needs_text_in_image = True
            text_instruction = (
                f"Generate readable Russian dimension labels directly inside the image. "
                f"Use these exact labels when available: {labels_text}. "
            )
        elif "infographic" in image_type or image_type in {"capacity_display", "before_after"}:
            text_lines_ru = verified_facts[:4]
            needs_text_in_image = bool(text_lines_ru)
            if needs_text_in_image:
                text_instruction = "Generate short readable Russian labels directly inside the image. "

        prompt = (
            f"Create a clean vertical 3:4 marketplace image for {product_name}. "
            f"Image purpose: {template_name}. Product appearance: {visible_desc}. "
            f"Use a {palette.get('name', 'clean marketplace')} visual style with realistic lighting. "
            "Keep the product accurate and prominent. "
            f"{text_instruction}"
        )
        prompt = append_direct_text_requirement(
            prompt,
            text_lines_ru,
            needs_text_in_image,
        )

        images.append({
            "id": f"{idx:02d}_{image_type}",
            "filename": f"{idx:02d}_{image_type}.jpg",
            "purpose": template_name,
            "buyer_question": "clarify product value and reduce purchase hesitation",
            "image_type": image_type,
            "design_style": "clean modern Ozon marketplace design",
            "layout_direction": "balanced vertical 3:4 composition with clear product hierarchy and intentional negative space",
            "background_direction": "tasteful background chosen for this product, with soft shadows and enough contrast around product edges",
            "generation_mode": "image_only",
            "needs_text_overlay": False,
            "needs_text_in_image": needs_text_in_image,
            "image2_prompt": prompt,
            "text_lines_ru": text_lines_ru,
            "text_overlay": [],
            "reference_priority": ["primary_sku", "dimension_reference" if image_type == "dimension_infographic" else "supporting_references"],
            "qc_rules": [
                "product must match reference",
                "no Chinese text",
                "no logo or watermark",
                "do not invent product features",
            ],
        })

    return {
        "sku": "ALL_SKU",
        "platform": "Ozon",
        "product_type": product_info.get("category_en") or cfg.product_hint or "generic product",
        "target_buyer": product_info.get("target_audience_ru") or "",
        "visual_strategy": category_profile.get("description") or "dynamic marketplace image plan",
        "image_count": len(images),
        "images": images,
    }


def normalize_text_overlay_lines(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, (str, int, float)):
        candidates = [value]
    elif isinstance(value, dict):
        candidates = [value.get("text") or value.get("label") or ""]
    elif isinstance(value, list):
        candidates = []
        for item in value:
            if isinstance(item, dict):
                candidates.append(item.get("text") or item.get("label") or "")
            else:
                candidates.append(item)
    else:
        candidates = []
    return [
        str(line).strip()
        for line in candidates
        if str(line).strip() and not contains_chinese(str(line))
    ][:8]


def normalize_image_plan(
    raw_plan: dict,
    product_info: dict,
    style_guide: dict,
    color_info: dict,
    category_profile: dict,
    dimension_labels: list[str],
    cfg: AppConfig,
) -> dict:
    if not isinstance(raw_plan, dict):
        raw_plan = {}

    fallback = build_fallback_image_plan(product_info, style_guide, color_info, category_profile, dimension_labels, cfg)
    images = raw_plan.get("images")
    if not isinstance(images, list) or not images:
        raw_plan = fallback
        images = raw_plan["images"]

    include_dimension = bool(getattr(cfg, "generate_dimension_image", True))
    verified_facts = build_verified_facts(product_info, color_info, dimension_labels, cfg)
    normalized = []
    for idx, item in enumerate(images, start=1):
        if not isinstance(item, dict):
            continue
        image_type = str(item.get("image_type") or "product_image").strip() or "product_image"
        dimension_check_text = " ".join(
            str(item.get(key) or "").lower()
            for key in ("image_type", "id", "purpose")
        )
        if not include_dimension and any(word in dimension_check_text for word in ("dimension", "size", "measurement", "尺寸", "规格")):
            continue
        item_id = str(item.get("id") or f"{idx:02d}_{image_type}").strip()
        item_id = re.sub(r"[^A-Za-z0-9_-]+", "_", item_id).strip("_") or f"{idx:02d}_{image_type}"

        prompt = str(item.get("image2_prompt") or "").strip()
        if not prompt:
            prompt = fallback["images"][min(idx - 1, len(fallback["images"]) - 1)]["image2_prompt"]

        text_lines_ru = normalize_text_overlay_lines(item.get("text_lines_ru") or item.get("text_overlay"))
        needs_text_in_image = bool(item.get("needs_text_in_image")) or bool(text_lines_ru)
        if "dimension" in image_type.lower():
            dimension_text = [label for label in (dimension_labels or []) if not contains_chinese(label)]
            if dimension_text:
                text_lines_ru = text_lines_ru or dimension_text
            needs_text_in_image = True
        elif needs_text_in_image and not text_lines_ru:
            text_lines_ru = verified_facts[:4]
        prompt = append_direct_text_requirement(prompt, text_lines_ru, needs_text_in_image)

        qc_rules = item.get("qc_rules") or []
        if isinstance(qc_rules, str):
            qc_rules = [qc_rules]
        qc_rules = [str(rule).strip() for rule in qc_rules if str(rule).strip()]

        normalized.append({
            "id": item_id,
            "filename": str(item.get("filename") or f"{idx:02d}_{image_type}.jpg").strip(),
            "purpose": str(item.get("purpose") or image_type).strip(),
            "buyer_question": str(item.get("buyer_question") or "").strip(),
            "image_type": image_type,
            "design_style": str(item.get("design_style") or "").strip(),
            "layout_direction": str(item.get("layout_direction") or "").strip(),
            "background_direction": str(item.get("background_direction") or "").strip(),
            "generation_mode": "image_only",
            "needs_text_overlay": False,
            "needs_text_in_image": needs_text_in_image,
            "image2_prompt": prompt,
            "text_lines_ru": text_lines_ru,
            "text_overlay": [],
            "reference_priority": item.get("reference_priority") or ["primary_sku"],
            "qc_rules": qc_rules or [
                "product must match reference",
                "no Chinese text",
                "no logo or watermark",
                "do not invent product features",
            ],
        })

    if not any("hero" in item["image_type"] or "main" in item["id"] for item in normalized):
        normalized.insert(0, fallback["images"][0])

    count = desired_image_plan_count(cfg)
    if include_dimension and not any("dimension" in item["image_type"].lower() for item in normalized):
        dimension_item = next(
            (item for item in fallback["images"] if "dimension" in item["image_type"].lower()),
            None,
        )
        if dimension_item:
            insert_at = 0 if count == 1 or not normalized else 1
            normalized.insert(insert_at, dimension_item)

    normalized = normalized[:count]
    fallback_idx = 0
    while len(normalized) < count:
        candidate = dict(fallback["images"][fallback_idx % len(fallback["images"])])
        fallback_idx += 1
        if not include_dimension and "dimension" in candidate["image_type"].lower():
            continue
        if any(candidate["id"] == item["id"] for item in normalized):
            candidate["id"] = f"{len(normalized) + 1:02d}_{candidate['image_type']}"
        normalized.append(candidate)

    plan = {
        "sku": str(raw_plan.get("sku") or "ALL_SKU"),
        "platform": "Ozon",
        "product_type": str(raw_plan.get("product_type") or product_info.get("category_en") or cfg.product_hint or "generic product"),
        "target_buyer": str(raw_plan.get("target_buyer") or product_info.get("target_audience_ru") or ""),
        "visual_strategy": str(raw_plan.get("visual_strategy") or category_profile.get("description") or ""),
        "image_count": len(normalized),
        "images": normalized,
    }
    return plan


async def generate_image_plan(
    client,
    session,
    hero_url,
    other_urls,
    product_info,
    style_guide,
    color_info,
    category_profile,
    dimension_labels,
    cfg,
) -> dict:
    desired_count = desired_image_plan_count(cfg)
    include_dimension = bool(getattr(cfg, "generate_dimension_image", True))
    planner_profile = category_profile_for_planner(category_profile)
    verified_facts = build_verified_facts(product_info, color_info, dimension_labels, cfg)
    fallback = build_fallback_image_plan(product_info, style_guide, color_info, category_profile, dimension_labels, cfg)

    system = (
        "You are an expert Ozon marketplace creative director. "
        "Think like an e-commerce designer who plans a coherent image set for mobile shoppers. "
        "Return strict JSON only, no Markdown."
    )
    user_text = f"""
Create an image_plan JSON for this product, suitable for Ozon e-commerce.
Look at the attached product images directly. You are the planning brain. First infer the product identity and best visual strategy from the product itself:
- what the product mainly sells;
- what buyers may worry about;
- what must be explained with text;
- what should be shown visually without text;
- which images drive click, trust, specification clarity, use imagination, or final purchase confidence.
Then plan exactly {desired_count} vertical 3:4 images as one coherent set.

Hard constraints:
- image_count must be exactly {desired_count}.
- Must include a dimension/specification image: {str(include_dimension).lower()}.
- Image ratio: vertical 3:4.
- Compose every image as a true portrait 3:4 design from the start. Never plan a wide horizontal poster, landscape banner, or content strip inside a tall white canvas.
- No letterbox/pillarbox, no white top/bottom padding bands, no empty border bands.
- All visible text must be Russian. No Chinese text.
- Do not write OZON/Ozon, logo, watermark, QR code, or marketplace UI.
- Do not invent unverified specs or claims. Exact dimensions/capacity/material must come from verified facts or references.
- image-2 generates the whole final image, including any Russian text. The program will not overlay text later.
- Use "generation_mode": "image_only", "needs_text_overlay": false, and "text_overlay": [] for every item.
- Use "needs_text_in_image": true only when the image truly needs direct Russian text generated by image-2.
- Put exact short Russian text in "text_lines_ru" when needs_text_in_image is true.
- The SKU main/hero image must be a polished graphic product poster built on a clean believable lifestyle or real-use scene. Use one short Russian headline and at most one compact verified benefit.

Creative direction:
- Make the set useful for a mobile buyer in 3 seconds.
- Before choosing the cover, silently consider at least three materially different concepts for scene, camera angle, product placement, text hierarchy, and palette; select the concept that best fits this exact product. Do not output the rejected concepts.
- Let the recognized product determine the art direction. Different product categories must not collapse into one repeated poster template.
- Vary hierarchy, product placement, camera distance, background structure, typography placement, and use of text according to the product and buyer question.
- Never default to a beauty/cosmetics visual language. Do not use beige-pink dressing-table scenes, dried flowers, gold arcs, huge top headings, or two rounded feature pills unless the actual product and strategy specifically justify them.
- Keep the product clearly dominant while showing enough real environment to make its use immediately understandable. Do not manufacture an empty area merely to hold text.
- After the cover, use one buyer benefit or one question per image. Do not repeat the same headline, composition, background, or two-badge layout across the gallery.
- Category expression matters: storage and bags explain capacity/organization; tools explain verified power, kit, and use; kitchen goods show function in context; electronics clarify verified controls and compatibility; material-led goods show texture and construction.
- Every image must have deliberate but natural art direction, not a plain auto-generated product snapshot or an artificial studio poster.
- For each image choose a design_style, layout_direction, and background_direction suitable for that product and image purpose.
- Use tasteful composition: clear hierarchy, natural depth, clean neutral daylight, accurate white balance, fresh surfaces, soft realistic shadows, and strong product-edge clarity.
- Infographic images should feel like polished marketplace design with clean callouts, arrows, or icons where useful.
- The main image should show one strong product hero inside a credible daily routine or home/work environment, combined with restrained product-specific Russian typography.
- The cover must not default to a giant product noun across the top, two rounded benefit pills, repeated icon badges, or the same product-right/text-left composition.
- Do not make every image text-heavy. Hero/lifestyle/detail/scene images may have little or no text.
- If a chosen image is an infographic, comparison, size/spec, steps, or summary image, it usually needs short readable Russian text.
- Keep Russian copy short: 1 headline or 2-4 compact labels.
- Avoid generic empty backgrounds, cold studio staging, showroom podiums, giant headings, repeated two-pill layouts, repeated icon rows, irrelevant foreground leaves, dried flowers, gold ornaments, excessive props, muddy beige/gray casts, yellow haze, dirty-looking surfaces, dull catalog snapshots, and crowded poster layouts.

User product hint:
{cfg.product_hint}

Product analysis, including physical behavior, allowed scene adaptation, identity anchors, and contact requirements:
{json.dumps(product_info, ensure_ascii=False, indent=2)}

Optional style hint:
{json.dumps(style_guide, ensure_ascii=False, indent=2)}

Detected product color/material:
{json.dumps(color_info, ensure_ascii=False, indent=2)}

Category strategy reference only, not a template command:
{json.dumps(planner_profile, ensure_ascii=False, indent=2)}

Verified facts and safe Russian text candidates. Use these for exact claims and labels:
{json.dumps(verified_facts, ensure_ascii=False, indent=2)}

Dimension labels visible in references, if any:
{json.dumps(dimension_labels or [], ensure_ascii=False, indent=2)}

Return exactly this JSON structure:
{{
  "sku": "ALL_SKU",
  "platform": "Ozon",
  "product_type": "...",
  "target_buyer": "...",
  "visual_strategy": "...",
  "image_count": {desired_count},
  "images": [
    {{
      "id": "01_main",
      "filename": "01_main.jpg",
      "purpose": "conversion purpose",
      "buyer_question": "buyer doubt this image solves",
      "image_type": "hero_product",
      "design_style": "specific visual style for this image",
      "layout_direction": "composition and hierarchy instructions",
      "background_direction": "background, props, lighting, shadow, color mood",
      "generation_mode": "image_only",
      "needs_text_overlay": false,
      "needs_text_in_image": true,
      "image2_prompt": "Final English prompt for image-2. Include exact Russian text to render when this is an infographic.",
      "text_lines_ru": ["short Russian headline", "short Russian benefit"],
      "text_overlay": [],
      "reference_priority": ["primary_sku"],
      "qc_rules": ["product must match reference", "no Chinese text"]
    }}
  ]
}}
Only JSON.
"""
    content = [{"type": "text", "text": user_text}]
    content.append({"type": "text", "text": "PRIMARY SKU/product reference image:"})
    content.append({"type": "image_url", "image_url": {"url": hero_url}})
    for idx, url in enumerate(other_urls[:MAX_PRODUCT_IMAGES_FOR_ANALYSIS - 1], start=1):
        content.append({"type": "text", "text": f"Additional product reference image {idx}:"})
        content.append({"type": "image_url", "image_url": {"url": url}})
    try:
        resp = await client.chat_async(
            session,
            cfg.analysis_model,
            [{"role": "system", "content": system}, {"role": "user", "content": content}],
            max_tokens=3200,
        )
        raw_plan = robust_parse_first_json(extract_text(resp), fallback)
    except Exception as exc:
        client.log(f"    image_plan 生成失败，使用本地兜底计划：{exc}")
        raw_plan = fallback

    return normalize_image_plan(
        raw_plan,
        product_info,
        style_guide,
        color_info,
        category_profile,
        dimension_labels,
        cfg,
    )


# =========================
# 图片生成
# =========================

def build_prompt(
    template_name: str,
    product_info: dict,
    style_guide: dict,
    color_info: dict,
    cfg: AppConfig,
    effective_category_name: str,
    category_profile: dict,
    color_variant_rule_ru: str = ""
) -> str:
    template = IMAGE_TYPE_TEMPLATES[template_name]

    product_name_ru = product_info.get("product_name_ru") or "Товар"
    headline = product_info.get("headline_ru") or "ФУНКЦИОНАЛЬНОЕ РЕШЕНИЕ"
    subheadline = product_info.get("subheadline_ru") or "ДЛЯ ПОВСЕДНЕВНОГО ИСПОЛЬЗОВАНИЯ"
    use_scene = product_info.get("use_scene_ru") or "использование товара"
    scene_bg = product_info.get("scene_background_ru") or "реалистичная сцена"
    detail = product_info.get("detail_focus_ru") or "ключевые детали"
    comparison = product_info.get("comparison_angle_ru") or "удобство использования"
    steps = product_info.get("usage_steps_ru") or []
    benefits = product_info.get("benefits_ru") or []

    palette = style_guide.get("palette", {})
    style_keywords = ", ".join(style_guide.get("style_keywords_ru", []))
    composition = style_guide.get("composition_advice_ru", "")
    lighting = style_guide.get("lighting_advice_ru", "")
    decor = style_guide.get("decor_elements_ru", "")
    preset_prompt = style_guide.get("preset_prompt_ru", "")

    visible_desc = "、".join([
        x for x in [
            (color_info.get("color_ru") or "").strip(),
            (color_info.get("material_ru") or "").strip(),
            (color_info.get("finish_ru") or "").strip()
        ] if x
    ]) or "как на изображении товара"

    policy = template_prompt_policy(template_name)
    benefits_text = "\n".join([f"- {b}" for b in benefits[:policy["max_benefits"]]])
    steps_text = "\n".join([f"- {s}" for s in steps[:policy["max_steps"]]])

    category_rule = category_profile.get("extra_prompt_ru", "")
    model_policy = category_profile.get("model_policy", "auto")

    info_lines = [
        f"- Product name: {product_name_ru}",
        f"- Product appearance: {visible_desc}. Preserve its identity anchors while adapting pose and geometry naturally to the scene.",
    ]
    if policy["include_headline"]:
        info_lines.append(f"- Optional short title idea: {headline}")
    if policy["include_subheadline"]:
        info_lines.append(f"- Optional subtitle idea: {subheadline}")
    if policy["include_scene"]:
        info_lines.extend([
            f"- Use scene: {use_scene}",
            f"- Background/setting: {scene_bg}",
        ])
    if policy["include_detail"]:
        info_lines.append(f"- Detail focus: {detail}")
    if policy["include_comparison"]:
        info_lines.append(f"- Comparison angle: {comparison}")
    product_info_text = "\n".join(info_lines)

    benefits_section = f"\nUseful short benefits, only if this template needs text:\n{benefits_text}\n" if benefits_text else ""
    steps_section = f"\nUsage steps, only if this template is a steps infographic:\n{steps_text}\n" if steps_text else ""

    return f"""
Create one marketplace product image.

Image type: {template_name}
Template instruction:
{template["prompt"]}

Category: {effective_category_name}
Model/person policy: {model_policy}
Category-specific rule:
{category_rule}

Color variant rule:
{color_variant_rule_ru}

Product information for this image:
{product_info_text}
{benefits_section}{steps_section}
Visual style, use lightly:
- Style keywords: {style_keywords}
- Composition hint: {composition}
- Lighting: {lighting}
- Decor: {decor}
- Background/text/graphic colors only, never recolor the product: primary {palette.get("primary")}, secondary {palette.get("secondary")}, accent {palette.get("accent")}
- Background: {palette.get("bg_description_ru")}
- Preset: {preset_prompt}

Layout density rule:
{policy["layout_rule"]}

Strict vertical frame rule:
{STRICT_VERTICAL_FRAME_RULE}

General rules:
- If any visible text is used, it must be Russian only.
- Keep copy neutral and informational.
- Never add discounts, promotions, sale, best, premium, brand names, logos, watermarks, or Chinese text.
- Do not invent exact dimensions or specifications unless they are visible in the references.
- Preserve the product's color family, material, recognizable proportions, and signature design details; allow physically necessary pose, perspective, drape, bending, opening, articulation, contact, and occlusion.
- Re-render the product into the scene with coherent depth, contact shadows, reflections, and overlap. Never paste it as a flat cutout.
- Never apply style/background/decor colors to the product itself.
- Avoid colored lighting or reflections that change the perceived product color.
- The image must look realistic and suitable for a product card.

Format: {cfg.aspect_text}, {cfg.res_text}.

NEGATIVE:
promo, sale, discount, акция, скидка, best, premium, OZON, ozon, logo, brand, watermark,
Chinese text, wrong product, distorted color, meaningless text, extra logos, dense text blocks,
busy layout, cramped poster, crowded callouts, oversized typography, duplicate product photo,
reference thumbnail, inset image, picture-in-picture, collage frame, pasted reference image.
""".strip()


async def generate_image(
    client: ApiClient,
    session,
    prompt: str,
    hero_path: Path,
    other_paths: list[Path],
    bundle_path: Path | None,
    sku_variant_paths: list[Path] | None = None,
    template_name: str = ""
):
    """
    hero_url：
        当前这张图的主颜色参考图。
        对于逐 SKU 主图，就是当前 SKU。
        对于附图，就是轮换分配后的某一个颜色 SKU。

    sku_variant_urls：
        同款商品的其他颜色参考。
        不要求每张图全部展示，只用于保持颜色真实和允许适当展示变体。
    """

    if sku_variant_paths is None:
        sku_variant_paths = []

    strict_sku_cover = template_name == "主图海报"
    if strict_sku_cover:
        # SKU covers must be conditioned by exactly one product image. Extra references
        # can leak another variant's color into the generated product.
        other_paths = []
        bundle_path = None
        sku_variant_paths = []

    hero_url = file_to_data_url(hero_path)
    other_urls = [file_to_data_url(p) for p in other_paths]
    bundle_url = file_to_data_url(bundle_path) if bundle_path else None
    sku_variant_urls = [file_to_data_url(p) for p in sku_variant_paths]

    if client.cfg.image_generation_method == "image_edits":
        output_size = effective_image_edit_size(client.cfg)
        output_size_text = f"exact size {output_size}" if output_size and output_size != "auto" else "the requested vertical 3:4 size"
        edit_prompt = f"""
Output: true portrait vertical 3:4 canvas, {output_size_text}.
{STRICT_VERTICAL_FRAME_RULE}

{prompt}

Reference image order:
1. PRIMARY SKU reference: use its product identity, selected color, material, recognizable proportions, and signature design details. Ignore and replace its background, surface, lighting, props, text, and composition. Adapt pose and geometry when required for physically realistic wearing or use.
2+. Optional references or supporting reference sheets: dimensions, details, accessories, or variants when provided.
Use references as product facts only. Do not paste them as thumbnails, inset images, grid sheets, collage frames, or duplicated photos.
""".strip()
        if template_name == "尺寸结构图":
            edit_prompt += """

Dimension image rule: if measurements are shown, copy only visible values from the dimension reference. Do not invent or recalculate numbers.
""".strip()
        if strict_sku_cover:
            edit_prompt += """

SKU color rule: show only the selected SKU color from the primary reference. Do not recolor the product or mix variants.
SKU cover scene rule: create a fresh believable daily-use environment. Do not imitate the reference background or turn a simple reference background into a cold studio poster.
""".strip()

        temp_reference_sheets = []
        try:
            upload_paths = [hero_path]
            hero_key = str(hero_path.resolve())
            if template_name in {"主图海报", "卖点信息图", "对比图"}:
                variant_limit = 3
            else:
                variant_limit = 1

            variant_uploads = unique_existing_paths(sku_variant_paths[:variant_limit], exclude={hero_key})
            upload_paths.extend(variant_uploads)

            support_paths = []
            if bundle_path:
                support_paths.append(bundle_path)
            support_paths.extend(other_paths)
            max_support_uploads = max(0, GPT_GE_MAX_EDIT_IMAGES - len(upload_paths))
            support_uploads, temp_reference_sheets, support_count, sheet_count = build_support_reference_uploads(
                support_paths,
                hero_path,
                max_support_uploads,
            )
            if sheet_count:
                edit_prompt += f"""

Supporting reference sheet rule: the non-SKU supporting references were combined into {sheet_count} contact sheet image(s) from {support_count} original photos.
Use these sheets only to understand product details, accessories, scale, packaging, and use cases. Never recreate the sheet grid or paste small reference tiles into the final image.
""".strip()
                client.log(f"    附图参考较多：{support_count} 张非 SKU 附图已拼接成 {sheet_count} 张参考拼图")
            upload_paths.extend(support_uploads)

            unique_paths = []
            seen = set()
            for path in upload_paths:
                key = str(path.resolve())
                if key not in seen:
                    seen.add(key)
                    unique_paths.append(path)
            return await client.image_edit_async(session, edit_prompt, unique_paths[:GPT_GE_MAX_EDIT_IMAGES])
        finally:
            for tmp_path in temp_reference_sheets:
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    output_size = normalized_target_size(client.cfg)
    output_size_text = f"{output_size[0]}x{output_size[1]}" if output_size else (client.cfg.res_text or "vertical 3:4")
    generation_prompt = f"""
Output: true portrait vertical 3:4 canvas, final size {output_size_text}.
{STRICT_VERTICAL_FRAME_RULE}

{prompt}
""".strip()

    content = [
        {"type": "text", "text": generation_prompt},
        {
            "type": "text",
            "text": (
                "PRIMARY SKU reference: use it for product identity, selected color, material, recognizable proportions, and signature details. "
                "Re-render it with physically realistic pose, perspective, contact, occlusion, shadow, and reflection. "
                "Do not paste the reference as a flat cutout, thumbnail, inset, collage, or duplicated product photo."
            )
        },
        {"type": "image_url", "image_url": {"url": hero_url}}
    ]

    if template_name == "尺寸结构图":
        content.append({
            "type": "text",
            "text": (
                "Dimension reference follows. If measurements are shown, use only visible values from the reference; do not invent numbers."
            ),
        })

    if sku_variant_urls:
        content.append({
            "type": "text",
            "text": (
                "Other color variant references. Use only if the planned image naturally needs variant information."
            )
        })

        for idx, u in enumerate(sku_variant_urls[:8], start=1):
            content.append({"type": "text", "text": f"Other color variant {idx}:"})
            content.append({"type": "image_url", "image_url": {"url": u}})

    if bundle_url:
        content.append({"type": "text", "text": "Bundle/accessories reference:"})
        content.append({"type": "image_url", "image_url": {"url": bundle_url}})

    for u in other_urls[:MAX_PRODUCT_IMAGES_FOR_GENERATION - 1]:
        content.append({"type": "image_url", "image_url": {"url": u}})

    extra_payload = {}

    if client.cfg.image_temperature is not None and client.cfg.image_model == "nano-banana-2":
        extra_payload["temperature"] = client.cfg.image_temperature
        extra_payload["seed"] = random.randint(1, 2147483647)

    return await client.chat_async(
        session,
        client.cfg.image_model,
        [{"role": "user", "content": content}],
        max_tokens=1400,
        extra_payload=extra_payload,
        api_kind="image",
    )


def template_name_from_plan_item(image_item: dict) -> str:
    image_type = str(image_item.get("image_type") or "").lower()
    item_id = str(image_item.get("id") or "").lower()
    purpose = str(image_item.get("purpose") or "").lower()
    text = f"{image_type} {item_id} {purpose}"
    if "dimension" in text or "size" in text or "尺寸" in text:
        return "尺寸结构图"
    if "benefit" in text or "卖点" in text:
        return "卖点信息图"
    if "comparison" in text or "before_after" in text or "对比" in text:
        return "对比图"
    if "step" in text or "步骤" in text:
        return "使用步骤图"
    if "model" in text or "模特" in text:
        return "模特展示图"
    if "detail" in text or "material" in text or "texture" in text or "细节" in text or "材质" in text:
        return "细节特写图"
    if "capacity" in text or "容量" in text:
        return "容量展示图"
    if "bundle" in text or "package" in text or "配件" in text or "包装" in text:
        return "包装/配件图"
    if "hero" in text or "main" in text or "cover" in text or "主图" in text:
        return "主图海报"
    return "真实场景图"


def build_plan_item_prompt(
    image_item: dict,
    color_variant_rule_ru: str = "",
    dimension_ref: Path | None = None,
    dimension_labels: list[str] | None = None,
) -> str:
    prompt = str(image_item.get("image2_prompt") or "").strip()
    if not prompt:
        prompt = "Create a clean vertical 3:4 marketplace product image that accurately matches the reference product."

    text_lines = image_item.get("text_lines_ru") or image_item.get("text_overlay") or []
    text_lines = normalize_text_overlay_lines(text_lines)
    text_lines_block = "\n".join(f"- {line}" for line in text_lines)

    parts = [prompt]
    if text_lines:
        parts.extend([
            "",
            "Russian text to render directly inside the image:",
            text_lines_block,
        ])

    if dimension_ref:
        labels_text = ", ".join(dimension_labels or []) or "copy visible measurements exactly from DIMENSION REFERENCE"
        parts.extend([
            "",
            f"Dimension reference: {dimension_ref.name}. Use only visible measurements from that reference: {labels_text}. Do not invent measurements.",
            "Dimension image quality rules: make a bright full-color polished marketplace infographic, not a gray sketch, not a monochrome render, not a dark vignette poster. Preserve the reference product as a realistic glossy transparent plastic product with its original red cross and white handle. Use thin clean measurement lines and small readable labels. Do not use oversized embossed text, fake 3D lettering, distorted Cyrillic, or decorative shadow-heavy typography.",
        ])

    if color_variant_rule_ru:
        parts.extend(["", color_variant_rule_ru])

    parts.extend(["", STRICT_VERTICAL_FRAME_RULE, "", GLOBAL_IMAGE_RULES])
    return "\n".join(str(part) for part in parts).strip()


async def generate_image_from_plan_item(client, session, image_item, hero_path, other_paths, bundle_path=None, dimension_ref=None, dimension_labels=None, sku_variant_paths=None, color_variant_rule_ru=""):
    from erp.core.ai_reuse import fingerprint, async_result_cache, read_json, write_json
    from erp.core.ai_usage import scope
    paths=[hero_path, *other_paths, bundle_path, dimension_ref, *(sku_variant_paths or [])]
    sources=[(str(p.resolve()), hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths if p is not None]
    cfg=client.cfg
    key=fingerprint({"version":1,"task":scope.get().get("job_id",str(cfg.root_dir)),"config":fingerprint(vars(cfg)),"item":image_item,"sources":sources,"labels":dimension_labels,"color":color_variant_rule_ru})
    async with async_result_cache("self-listing-image-receipts",key) as receipt:
        saved=read_json(receipt)
        if saved is not None:
            return saved
        value=await _generate_image_from_plan_item(client,session,image_item,hero_path,other_paths,bundle_path,dimension_ref,dimension_labels,sku_variant_paths,color_variant_rule_ru)
        write_json(receipt,value)
        return value


async def _generate_image_from_plan_item(
    client: ApiClient,
    session,
    image_item: dict,
    hero_path: Path,
    other_paths: list[Path],
    bundle_path: Path | None = None,
    dimension_ref: Path | None = None,
    dimension_labels: list[str] | None = None,
    sku_variant_paths: list[Path] | None = None,
    color_variant_rule_ru: str = "",
):
    template_name = template_name_from_plan_item(image_item)
    auxiliary_candidates = [p for p in other_paths if p.resolve() != hero_path.resolve()]
    if template_name == "尺寸结构图":
        selected_others = []
        if dimension_ref:
            selected_others.append(dimension_ref)
        selected_others.extend(auxiliary_candidates)
        selected_bundle_ref = None
    else:
        selected_others = auxiliary_candidates
        selected_bundle_ref = bundle_path

    final_prompt = build_plan_item_prompt(
        image_item,
        color_variant_rule_ru=color_variant_rule_ru,
        dimension_ref=dimension_ref if template_name == "尺寸结构图" else None,
        dimension_labels=dimension_labels or [],
    )
    return await generate_image(
        client,
        session,
        final_prompt,
        hero_path,
        selected_others,
        selected_bundle_ref,
        sku_variant_paths=sku_variant_paths or [],
        template_name=template_name,
    )


async def download_url_to_file(session, url: str, out_path: Path, cfg: AppConfig | None = None):
    async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
        if r.status == 200:
            write_image_bytes_atomic(out_path, await r.read(), cfg)
        else:
            raw = await r.text()
            raise RuntimeError(f"下载图片失败：{r.status}，{raw[:500]}")


async def save_image_from_response(resp_json: dict, out_path: Path, session, cfg: AppConfig | None = None):
    ensure_dir(out_path.parent)

    content = extract_text(resp_json)

    m = re.search(r"(data:image\/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\n\r]+)", content)
    if m:
        b64 = m.group(1).split(",", 1)[1]
        write_image_bytes_atomic(out_path, decode_image_base64(b64), cfg)
        return

    if "data" in resp_json and isinstance(resp_json["data"], list) and resp_json["data"]:
        first = resp_json["data"][0]
        if isinstance(first, dict):
            if first.get("b64_json"):
                write_image_bytes_atomic(out_path, decode_image_base64(first["b64_json"]), cfg)
                return
            if first.get("base64"):
                b64 = first["base64"]
                if b64.startswith("data:image"):
                    b64 = b64.split(",", 1)[1]
                write_image_bytes_atomic(out_path, decode_image_base64(b64), cfg)
                return
            if first.get("url"):
                await download_url_to_file(session, first["url"], out_path, cfg)
                return

    raw = json.dumps(resp_json, ensure_ascii=False)
    m = re.search(r"(https?://[^\s\"\\)]+)", raw)
    if m:
        await download_url_to_file(session, m.group(1), out_path, cfg)
        return

    raise RuntimeError("No image found in response.")


# =========================
# 主处理流程
# =========================

async def process_one_product_folder(
    client: ApiClient,
    session,
    product_folder: Path,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
):
    cfg = client.cfg

    async def should_stop():
        await wait_if_paused(pause_event)
        return stop_event is not None and stop_event.is_set()

    if await should_stop():
        return

    imgs = list_images(product_folder, cfg.out_subfolder, cfg.max_image_mb)
    if not imgs:
        client.log(f"[跳过] {product_folder.name}：没有图片")
        return

    sku_paths, other_paths = split_sku_hero_and_others(imgs, cfg.sku_prefix)
    if not sku_paths:
        client.log(f"[跳过] {product_folder.name}：没有 {cfg.sku_prefix} 开头图片")
        return

    out_dir = product_folder / cfg.out_subfolder
    ensure_dir(out_dir)

    client.log(f"\n[开始] {product_folder.name}")
    client.log(f"  SKU 图：{len(sku_paths)}，其他图：{len(other_paths)}")

    if await should_stop():
        return
    if cfg.fast_mode:
        client.log("  [快速模式] 跳过 SKU 大字检测、真人检测和自动风格分析")
        base_hero_path = sku_paths[0]
    else:
        base_hero_path = await find_clean_base_hero(client, session, sku_paths)
    base_hero_url = file_to_data_url(base_hero_path)

    other_paths_limited = other_paths[:MAX_PRODUCT_IMAGES_FOR_ANALYSIS]
    other_urls = [file_to_data_url(p) for p in other_paths_limited]

    if await should_stop():
        return
    has_model = False
    if cfg.auto_detect_model and not cfg.fast_mode:
        client.log("  [2/8] 检测是否已有真人/手持图...")
        model_candidates = other_paths[:5] if other_paths else [base_hero_path]
        has_model = await detect_any_model(client, session, model_candidates)
        client.log(f"  [OK] 模特检测：{'有' if has_model else '无'}")

    if await should_stop():
        return
    client.log("  [3/8] 逐产品看图识别品类、用途、材质与购买关切点...")
    product_info = await analyze_product_universal(
        client, session, base_hero_url, other_urls, cfg.product_hint,
    )

    effective_category_name, category_profile = get_effective_category_profile(cfg, product_info)
    client.log(f"  [品类策略] {effective_category_name}：{category_profile.get('description', '')}")

    if await should_stop():
        return
    client.log("  [4/8] 根据商品图独立分析色板、构图、灯光与场景...")
    style_guide = await analyze_visual_style(
        client, session, base_hero_url, other_urls,
        str(product_info.get("category_en") or ""), cfg.product_hint,
    )
    client.log(f"  [OK] 风格提示：{style_guide.get('palette', {}).get('name', cfg.style_name)}")

    if await should_stop():
        return
    dimension_ref = None
    dimension_labels = []
    dimension_candidate_paths = [base_hero_path] + other_paths
    needs_dimension_reference = bool(getattr(cfg, "generate_dimension_image", True)) and bool(dimension_candidate_paths)
    if needs_dimension_reference:
        client.log("  [5/8] 查找尺寸参考图并读取标注...")
        dimension_ref, dimension_labels = await find_dimension_reference(client, session, dimension_candidate_paths)
        client.log(
            f"  [OK] 尺寸参考：{dimension_ref.name if dimension_ref else '未找到'}；"
            f"识别标注：{', '.join(dimension_labels) if dimension_labels else '无'}"
        )

    client.log("  [5/8] 查找配件/包装参考图...")
    bundle_ref = None
    if cfg.include_bundle_reference:
        bundle_ref = await find_bundle_reference(client, session, other_paths)
    client.log(f"  [OK] 配件参考：{bundle_ref.name if bundle_ref else '无'}")

    meta = {
        "product_folder": str(product_folder),
        "product_hint": cfg.product_hint,
        "base_hero": str(base_hero_path),
        "has_model": has_model,
        "selected_category": cfg.category_name,
        "effective_category": effective_category_name,
        "category_profile": category_profile,
        "product_info": product_info,
        "style_guide": style_guide,
        "image_plan_count": desired_image_plan_count(cfg),
        "generate_dimension_image": bool(getattr(cfg, "generate_dimension_image", True)),
        "selected_templates": cfg.selected_templates,
        "bundle_ref": str(bundle_ref) if bundle_ref else "",
        "dimension_ref": str(dimension_ref) if dimension_ref else "",
        "dimension_labels": dimension_labels,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    client.log(f"  [6/8] 分析 SKU 颜色材质，共 {len(sku_paths)} 张...")

    async def analyze_color(p: Path):
        if await should_stop():
            return p.name, {}
        try:
            info = await detect_color_material_from_image(client, session, file_to_data_url(p))
            (out_dir / f"{p.stem}_color.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            return p.name, info
        except Exception as e:
            client.log(f"    颜色分析失败：{p.name}，{e}")
            return p.name, {}

    color_paths = sku_paths[:1] if cfg.fast_mode else sku_paths
    color_results = await bounded_gather(color_paths, analyze_color, cfg.max_concurrent_requests)
    color_infos = dict(color_results)
    if cfg.fast_mode and len(sku_paths) > 1:
        client.log("    快速模式仅分析第一张 SKU；其余 SKU 颜色只以各自原图为准，不复用第一张颜色。")

    base_color_info = color_infos.get(base_hero_path.name) or next(iter(color_infos.values()), {})
    if await should_stop():
        return

    client.log("  [7/8] 生成动态 image_plan.json...")
    image_plan = await generate_image_plan(
        client,
        session,
        base_hero_url,
        other_urls,
        product_info,
        style_guide,
        base_color_info,
        category_profile,
        dimension_labels,
        cfg,
    )
    (out_dir / "image_plan.json").write_text(
        json.dumps(image_plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    meta["image_plan"] = image_plan
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    client.log(f"  [OK] image_plan：{image_plan.get('image_count', 0)} 张")

    plan_images = list(image_plan.get("images") or [])
    if not plan_images:
        client.log("  [警告] image_plan 为空，跳过生成")
        return

    main_item = next(
        (
            item for item in plan_images
            if "hero" in str(item.get("image_type", "")).lower()
            or "main" in str(item.get("id", "")).lower()
        ),
        plan_images[0],
    )

    if cfg.generate_for_each_sku_cover:
        client.log("  [8/9] 按 image_plan 为每个 SKU 生成主图...")

        async def gen_cover(p: Path):
            if await should_stop():
                return
            out_path = out_dir / build_sku_cover_filename(p)
            if generated_image_file_is_valid(out_path, cfg):
                client.log(f"    已存在，跳过：{out_path.name}")
                return
            if out_path.exists():
                client.log(f"    已有文件无效，将重新生成：{out_path.name}")

            sku_color_info = color_infos.get(p.name, {})
            plan_item = prepare_sku_cover_plan_item(main_item, product_info)
            plan_item["actual_output_filename"] = out_path.name
            resp = await generate_image_from_plan_item(
                client,
                session,
                plan_item,
                p,
                [],
                None,
                dimension_ref=None,
                dimension_labels=[],
                sku_variant_paths=[],
                color_variant_rule_ru=build_sku_color_lock_rule(p, sku_color_info),
            )

            if stop_event is not None and stop_event.is_set():
                return
            await save_image_from_response(resp, out_path, session, cfg)
            client.log(f"    保存：{out_path.name}")

        results = await bounded_gather(sku_paths, gen_cover, cfg.max_concurrent_requests)
        for r in results:
            if isinstance(r, Exception):
                client.log(f"    主图生成失败：{r}")
        failed_cover_paths = [
            p for p in sku_paths
            if not generated_image_file_is_valid(out_dir / build_sku_cover_filename(p), cfg)
        ]
        failed_task_reruns = min(
            DEFAULT_FAILED_TASK_RERUNS,
            max(0, int(getattr(cfg, "failed_task_reruns", DEFAULT_FAILED_TASK_RERUNS) or 0)),
        )
        for rerun_round in range(1, failed_task_reruns + 1):
            if not failed_cover_paths or await should_stop():
                break
            client.log(f"    [失败补跑 {rerun_round}/{failed_task_reruns}] 主图缺失/无效 {len(failed_cover_paths)} 张，开始重跑")
            results = await bounded_gather(failed_cover_paths, gen_cover, cfg.max_concurrent_requests)
            for r in results:
                if isinstance(r, Exception):
                    client.log(f"    主图补跑失败：{r}")
            failed_cover_paths = [
                p for p in failed_cover_paths
                if not generated_image_file_is_valid(out_dir / build_sku_cover_filename(p), cfg)
            ]
        if failed_cover_paths:
            names = ", ".join(p.name for p in failed_cover_paths)
            client.log(f"    [仍失败] 主图仍有 {len(failed_cover_paths)} 张未生成成功：{names}")
            raise RuntimeError(f"主图生成不完整：{names}")
    else:
        client.log("  [8/9] 跳过逐 SKU 主图")

    client.log("  [9/9] 按 image_plan 生成附图...")

    appendix_items = []
    for item in plan_images:
        if cfg.generate_for_each_sku_cover and item is main_item:
            continue
        appendix_items.append(item)

    jobs = []
    for appendix_index, item in enumerate(appendix_items, start=1):
        jobs.append((item, appendix_index, build_appendix_filename(appendix_index)))

    async def gen_plan_appendix(image_item: dict, idx: int, filename: str):
        if await should_stop():
            return
        out_path = out_dir / filename
        template_name = template_name_from_plan_item(image_item)

        if out_path.exists():
            if not generated_image_file_is_valid(out_path, cfg):
                client.log(f"    已有文件无效，将重新生成：{filename}")
            elif template_name != "尺寸结构图":
                client.log(f"    已存在，跳过：{filename}")
                return
            else:
                existing_check = await inspect_dimension_output(
                    client,
                    session,
                    out_path,
                    dimension_labels,
                )
                if (
                    existing_check.get("has_dimension_lines")
                    and existing_check.get("has_numeric_measurements")
                    and existing_check.get("product_visible")
                    and existing_check.get("is_polished_marketplace_image")
                    and not existing_check.get("has_bad_gray_sketch_style")
                ):
                    client.log(f"    已存在且尺寸标注合格，跳过：{filename}")
                    return
                client.log(f"    已有尺寸图不合格，将重新生成：{filename}")

        assigned_sku_path = base_hero_path
        assigned_hero_path = base_hero_path

        color_info = color_infos.get(assigned_sku_path.name, {})
        color_variant_rule_ru = f"""
- Все изображения этой серии должны использовать один и тот же основной SKU: {assigned_sku_path.name}.
- Показывать только цвет PRIMARY SKU reference; не добавлять и не смешивать другие цветовые варианты.
- Не придумывать новые цвета и не менять оттенок товара.
- Дополнительная референсная картинка используется только для деталей/ракурса и не может менять цвет, форму или материал товара.
""".strip()

        client.log(
            f"    生成附图：{filename}，计划项：{image_item.get('id', idx)}，"
            f"类型：{image_item.get('image_type', '')}，主颜色参考：{assigned_sku_path.name}"
        )

        max_attempts = 2 if template_name == "尺寸结构图" and dimension_ref else 1
        attempt_item = dict(image_item)
        attempt_item["actual_output_filename"] = filename
        for attempt in range(1, max_attempts + 1):
            resp = await generate_image_from_plan_item(
                client,
                session,
                attempt_item,
                assigned_hero_path,
                other_paths,
                bundle_path=bundle_ref,
                dimension_ref=dimension_ref,
                dimension_labels=dimension_labels,
                sku_variant_paths=[],
                color_variant_rule_ru=color_variant_rule_ru,
            )

            if stop_event is not None and stop_event.is_set():
                return
            await save_image_from_response(resp, out_path, session, cfg)

            if template_name != "尺寸结构图" or not dimension_ref:
                client.log(f"    保存：{filename}")
                return

            check = await inspect_dimension_output(client, session, out_path, dimension_labels)
            dimension_ok = (
                check.get("has_dimension_lines")
                and check.get("has_numeric_measurements")
                and check.get("product_visible")
                and check.get("is_polished_marketplace_image")
                and not check.get("has_bad_gray_sketch_style")
            )
            if dimension_ok:
                visible = ", ".join(str(v) for v in (check.get("visible_measurements") or []))
                client.log(f"    保存：{filename}，尺寸标注检查通过：{visible or '已检测到'}")
                return

            if attempt < max_attempts:
                client.log(f"    尺寸标注缺失，第 {attempt + 1} 次重新生成：{filename}")
                attempt_item = dict(attempt_item)
                attempt_item["image2_prompt"] = str(attempt_item.get("image2_prompt") or "") + "\n\n" + """
CORRECTION AFTER QUALITY CHECK:
The previous output failed because dimension numbers or dimension arrows were missing.
This retry MUST visibly include clear dimension guide lines and arrowheads connected to the product.
Use only measurements from the dimension reference. Render Russian labels directly inside the image.
Do not return a plain product photo.
""".strip()
            else:
                client.log(f"    警告：{filename} 重试后仍未通过尺寸标注检查，请人工确认")

    async def run_plan_job(job):
        return await gen_plan_appendix(*job)

    results = await bounded_gather(jobs, run_plan_job, cfg.max_concurrent_requests)

    for r in results:
        if isinstance(r, Exception):
            client.log(f"    plan 附图生成失败：{r}")
    failed_jobs = [
        job for job in jobs
        if not generated_image_file_is_valid(out_dir / job[2], cfg)
    ]
    failed_task_reruns = min(
        DEFAULT_FAILED_TASK_RERUNS,
        max(0, int(getattr(cfg, "failed_task_reruns", DEFAULT_FAILED_TASK_RERUNS) or 0)),
    )
    for rerun_round in range(1, failed_task_reruns + 1):
        if not failed_jobs or await should_stop():
            break
        client.log(f"    [失败补跑 {rerun_round}/{failed_task_reruns}] 附图缺失/无效 {len(failed_jobs)} 张，开始重跑")
        results = await bounded_gather(failed_jobs, run_plan_job, cfg.max_concurrent_requests)
        for r in results:
            if isinstance(r, Exception):
                client.log(f"    附图补跑失败：{r}")
        failed_jobs = [
            job for job in failed_jobs
            if not generated_image_file_is_valid(out_dir / job[2], cfg)
        ]
    if failed_jobs:
        names = ", ".join(job[2] for job in failed_jobs)
        client.log(f"    [仍失败] 附图仍有 {len(failed_jobs)} 张未生成成功：{names}")
        raise RuntimeError(f"附图生成不完整：{names}")

    client.log(f"[完成] {product_folder.name}\n")

async def wait_if_paused(pause_event=None):
    while pause_event is not None and pause_event.is_set():
        await asyncio.sleep(0.2)


async def run_generation_async(cfg: AppConfig, log_func, stop_event: threading.Event, pause_event=None):
    root = Path(cfg.root_dir)
    if not root.exists():
        raise RuntimeError(f"Image root does not exist: {root}")

    product_folders = discover_product_folders(root, cfg)
    if not product_folders:
        raise RuntimeError(f"No product folders found under: {root}")

    original_count = len(product_folders)
    pending_folders = []
    completed_count = 0
    for product_folder in product_folders:
        state = inspect_product_generation(product_folder, cfg)
        if state["complete"]:
            completed_count += 1
            log_func(f"[预检跳过] {product_folder.name}：计划图片已全部存在且文件有效")
            continue
        pending_folders.append(product_folder)
        if state["missing"]:
            log_func(f"[预检待补] {product_folder.name}: {', '.join(state['missing'])}")
        else:
            log_func(f"[预检待处理] {product_folder.name}：尚无可用的生图计划")

    mode_label = "补跑" if bool(getattr(cfg, "repair_only_missing", False)) else "断点预检"
    log_func(
        f"[{mode_label}] 已检查 {original_count} 个产品："
        f"跳过已完成 {completed_count} 个，待生成/补跑 {len(pending_folders)} 个"
    )
    product_folders = pending_folders
    if not product_folders:
        log_func(f"[{mode_label}] 所有目标图片均已存在，未调用 AI 接口")
        return

    if not (cfg.analysis_api_key or cfg.api_key).strip():
        raise RuntimeError("Analysis API Key is required")
    if not (cfg.image_api_key or cfg.api_key).strip():
        raise RuntimeError("Image API Key is required")

    log_func(f"========== Start image generation: {len(product_folders)} products ==========")
    log_func(f"Config: {CONFIG_PATH}")
    log_func(f"Category: {cfg.category_name}")
    log_func(f"Style: {cfg.style_name}")
    log_func(f"Generation: {IMAGE_GENERATION_METHODS.get(cfg.image_generation_method, cfg.image_generation_method)} / {cfg.image_model}")
    log_func(f"Analysis API: {build_api_url(cfg.analysis_api_host or cfg.api_host, cfg.analysis_chat_path or cfg.chat_path)} / {cfg.analysis_model}")
    image_path = cfg.image_edit_path if cfg.image_generation_method == "image_edits" else (cfg.image_chat_path or cfg.chat_path)
    log_func(f"Image API: {build_api_url(cfg.image_api_host or cfg.api_host, image_path)} / {cfg.image_model}")
    if cfg.image_generation_method == "image_edits":
        log_func(f"Image edit request size: {effective_image_edit_size(cfg)}; final save size: {cfg.res_text}")
    log_func(f"Image plan count: {desired_image_plan_count(cfg)}")
    log_func(f"Generate dimension image: {bool(getattr(cfg, 'generate_dimension_image', True))}")

    client = ApiClient(cfg, log_func)

    if certifi:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
    else:
        ssl_context = ssl.create_default_context()

    connector = aiohttp.TCPConnector(
        ssl=ssl_context,
        limit=cfg.max_concurrent_requests * 2
    )

    async with aiohttp.ClientSession(connector=connector) as session:
        folder_queue = asyncio.Queue()
        for product_folder in product_folders:
            folder_queue.put_nowait(product_folder)

        async def folder_worker():
            while True:
                try:
                    pf = folder_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

                try:
                    if stop_event.is_set():
                        continue

                    await wait_if_paused(pause_event)

                    if stop_event.is_set():
                        continue

                    await process_one_product_folder(
                        client,
                        session,
                        pf,
                        stop_event=stop_event,
                        pause_event=pause_event,
                    )
                except Exception as e:
                    folder_failures.append((pf.name, str(e)))
                    log_func(f"[failed] {pf.name}: {e}\n")
                finally:
                    folder_queue.task_done()

        worker_count = min(max(1, cfg.folder_concurrency), len(product_folders))
        folder_failures = []
        workers = [asyncio.create_task(folder_worker()) for _ in range(worker_count)]
        await folder_queue.join()
        await asyncio.gather(*workers)

        if folder_failures:
            summary = "；".join(f"{name}: {message}" for name, message in folder_failures[:10])
            raise RuntimeError(f"{len(folder_failures)} 个产品文件夹生图不完整：{summary}")

    log_func("========== Image generation complete ==========")


def run_generation(cfg: AppConfig, log_func=print, pause_event=None, stop_event=None):
    """Run image generation without the Tk GUI. Can be imported by pipeline.py."""
    stop_event = stop_event or threading.Event()
    return asyncio.run(run_generation_async(cfg, log_func, stop_event, pause_event=pause_event))


def load_config_from_file(path=None) -> AppConfig:
    if not path:
        return load_config()

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    cfg = app_config_from_dict(data)
    normalize_image_generation_config(cfg)
    return cfg


def normalize_image_generation_config(cfg: AppConfig):
    if str(cfg.analysis_chat_path or "").endswith("/images/edits"):
        cfg.analysis_chat_path = DEFAULT_CHAT_PATH
    if str(cfg.image_chat_path or "").endswith("/images/edits"):
        cfg.image_chat_path = DEFAULT_CHAT_PATH
    if str(cfg.image_edit_path or "").endswith("/chat/completions"):
        cfg.image_edit_path = DEFAULT_IMAGE_EDIT_PATH
    if cfg.image_model == "gpt-image-2" and cfg.image_generation_method == "image_edits":
        cfg.image_edit_path = cfg.image_edit_path or DEFAULT_IMAGE_EDIT_PATH


def save_config_to_file(cfg: AppConfig, path=None):
    if not path:
        save_config(cfg)
        return

    config_path = Path(path)
    config_path.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")


def parse_templates_json(value):
    if not value:
        return None

    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError("--templates-json 必须是 JSON 对象，例如 {\"主图海报\": 1}")
    return data


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="批量生成 OZON 商品图片")
    parser.add_argument("--config", help="读取指定 JSON 配置文件；不填则读取默认 GUI 配置")
    parser.add_argument("--root-dir", help="图片根目录，目录下每个一级子文件夹视为一个产品")
    parser.add_argument("--api-key", help="API Key")
    parser.add_argument("--product-hint", help="产品提示/品类提示")
    parser.add_argument("--api-host", help="API Host，例如 api.gpt.ge")
    parser.add_argument("--chat-path", help="Chat API Path，例如 /v1/chat/completions")
    parser.add_argument("--analysis-api-key", help="独立分析 API Key")
    parser.add_argument("--analysis-api-host", help="独立分析 Base URL")
    parser.add_argument("--analysis-chat-path", help="独立分析 Chat API Path")
    parser.add_argument("--image-api-key", help="独立生图 API Key")
    parser.add_argument("--image-api-host", help="独立生图 Base URL")
    parser.add_argument("--image-chat-path", help="独立生图 Chat API Path")
    parser.add_argument("--image-generation-method", choices=list(IMAGE_GENERATION_METHODS.keys()), help="生图方式")
    parser.add_argument("--image-edit-path", help="Images Edits API Path，例如 /v1/images/edits")
    parser.add_argument("--analysis-model", help="分析模型")
    parser.add_argument("--image-model", help="生图模型")
    parser.add_argument("--image-size", help="Images Edits 尺寸，例如 auto 或 1024x1024")
    parser.add_argument("--image-quality", help="Images Edits 图片质量")
    parser.add_argument("--sku-prefix", help="SKU 图片文件名前缀")
    parser.add_argument("--out-subfolder", help="生成图片输出子目录")
    parser.add_argument("--style-name", help="风格名称")
    parser.add_argument("--category-name", help="品类策略名称")
    parser.add_argument("--templates-json", help="模板数量 JSON，例如 {\"主图海报\": 1}")
    parser.add_argument("--image-plan-count", type=int, help="动态 image_plan 精确生成张数")
    parser.add_argument("--max-concurrent-requests", type=int)
    parser.add_argument("--folder-concurrency", type=int)
    parser.add_argument("--max-image-mb", type=int)
    parser.add_argument("--request-timeout", type=int)
    parser.add_argument("--request-retries", type=int)
    parser.add_argument("--failed-task-reruns", type=int)
    parser.add_argument("--image-temperature", type=float)
    parser.add_argument("--generate-for-each-sku-cover", choices=["true", "false"])
    parser.add_argument("--generate-dimension-image", choices=["true", "false"])
    parser.add_argument("--auto-detect-model", choices=["true", "false"])
    parser.add_argument("--include-bundle-reference", choices=["true", "false"])
    parser.add_argument("--skip-sku-text-detection", choices=["true", "false"])
    parser.add_argument("--fast-mode", choices=["true", "false"])
    parser.add_argument("--save-config", action="store_true", help="运行前保存当前参数到配置文件")
    parser.add_argument("--no-gui", action="store_true", help="未提供 --root-dir 时不打开 GUI")
    return parser.parse_args(argv)


def str_to_bool(value):
    if value is None:
        return None
    return value.lower() == "true"


def config_from_args(args) -> AppConfig:
    cfg = load_config_from_file(args.config)

    overrides = {
        "root_dir": args.root_dir,
        "api_key": args.api_key,
        "product_hint": args.product_hint,
        "api_host": args.api_host,
        "chat_path": args.chat_path,
        "analysis_api_key": args.analysis_api_key,
        "analysis_api_host": args.analysis_api_host,
        "analysis_chat_path": args.analysis_chat_path,
        "image_api_key": args.image_api_key,
        "image_api_host": args.image_api_host,
        "image_chat_path": args.image_chat_path,
        "image_generation_method": args.image_generation_method,
        "image_edit_path": args.image_edit_path,
        "analysis_model": args.analysis_model,
        "image_model": args.image_model,
        "image_size": args.image_size,
        "image_quality": args.image_quality,
        "sku_prefix": args.sku_prefix,
        "out_subfolder": args.out_subfolder,
        "style_name": args.style_name,
        "category_name": args.category_name,
        "max_concurrent_requests": args.max_concurrent_requests,
        "folder_concurrency": args.folder_concurrency,
        "max_image_mb": args.max_image_mb,
        "request_timeout": args.request_timeout,
        "request_retries": args.request_retries,
        "failed_task_reruns": args.failed_task_reruns,
        "image_temperature": args.image_temperature,
        "image_plan_count": args.image_plan_count,
    }

    for key, value in overrides.items():
        if value is not None:
            setattr(cfg, key, value)

    templates = parse_templates_json(args.templates_json)
    if templates is not None:
        cfg.selected_templates = templates

    bool_overrides = {
        "generate_for_each_sku_cover": str_to_bool(args.generate_for_each_sku_cover),
        "generate_dimension_image": str_to_bool(args.generate_dimension_image),
        "auto_detect_model": str_to_bool(args.auto_detect_model),
        "include_bundle_reference": str_to_bool(args.include_bundle_reference),
        "skip_sku_text_detection": str_to_bool(args.skip_sku_text_detection),
        "fast_mode": str_to_bool(args.fast_mode),
    }
    for key, value in bool_overrides.items():
        if value is not None:
            setattr(cfg, key, value)

    return cfg


def run_cli(args):
    cfg = config_from_args(args)
    if args.save_config:
        save_config_to_file(cfg, args.config)
    return run_generation(cfg, print)


# =========================
# GUI
# =========================

class OzonImageGui:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1220x860")

        self.cfg = load_config()
        self.log_queue = queue.Queue()
        self.worker_thread = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()

        self.template_vars = {}
        self.template_count_vars = {}

        self.create_widgets()
        self.load_cfg_to_ui()
        self.root.after(100, self.poll_log_queue)

    def make_scrollable_frame(self, parent):
        """
        创建可滚动区域，返回内部 frame。
        用于配置项很多时避免把日志区挤没。
        """
        container = ttk.Frame(parent)

        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=canvas.yview)

        scroll_frame = ttk.Frame(canvas)

        scroll_window = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")

        def on_frame_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def on_canvas_configure(event):
            canvas.itemconfigure(scroll_window, width=event.width)

        scroll_frame.bind("<Configure>", on_frame_configure)
        canvas.bind("<Configure>", on_canvas_configure)

        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_mousewheel(event):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_mousewheel(event):
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", _bind_mousewheel)
        canvas.bind("<Leave>", _unbind_mousewheel)

        return container, scroll_frame


    def create_widgets(self):
        self.root.geometry("1280x900")
        self.root.minsize(1100, 720)

        main = ttk.Frame(self.root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        # 上下可拖动分栏：上面配置，下面日志
        paned = ttk.PanedWindow(main, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)

        top_area = ttk.Frame(paned)
        bottom_area = ttk.Frame(paned)

        paned.add(top_area, weight=4)
        paned.add(bottom_area, weight=1)

        # 顶部用 Notebook 分页，避免一个页面太长
        notebook = ttk.Notebook(top_area)
        notebook.pack(fill=tk.BOTH, expand=True)

        basic_tab_container, basic_tab = self.make_scrollable_frame(notebook)
        advanced_tab_container, advanced_tab = self.make_scrollable_frame(notebook)
        templates_tab_container, templates_tab = self.make_scrollable_frame(notebook)

        notebook.add(basic_tab_container, text="基础配置")
        notebook.add(advanced_tab_container, text="品类 / 风格 / 高级")
        notebook.add(templates_tab_container, text="图片模板")

        # =========================
        # 基础配置 Tab
        # =========================

        basic = ttk.LabelFrame(basic_tab, text="基础配置", padding=10)
        basic.pack(fill=tk.X, padx=8, pady=8)

        row = 0

        ttk.Label(basic, text="图片目录：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.root_dir_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.root_dir_var, width=90).grid(row=row, column=1, sticky=tk.EW, pady=5)
        ttk.Button(basic, text="选择", command=self.choose_root_dir).grid(row=row, column=2, padx=5)
        row += 1

        ttk.Label(basic, text="API Key：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.api_key_var = tk.StringVar()
        self.api_key_entry = ttk.Entry(basic, textvariable=self.api_key_var, width=90, show="*")
        self.api_key_entry.grid(row=row, column=1, sticky=tk.EW, pady=5)
        ttk.Button(basic, text="显示/隐藏", command=self.toggle_api_key_show).grid(row=row, column=2, padx=5)
        row += 1

        ttk.Label(basic, text="产品类型/提示：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.product_hint_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.product_hint_var, width=90).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, pady=5
        )
        row += 1

        ttk.Label(basic, text="API Host：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.api_host_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.api_host_var, width=90).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, pady=5
        )
        row += 1

        ttk.Label(basic, text="Chat Path：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.chat_path_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.chat_path_var, width=90).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, pady=5
        )
        row += 1

        ttk.Label(basic, text="分析 API Key：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.analysis_api_key_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.analysis_api_key_var, width=90, show="*").grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, pady=5
        )
        row += 1

        ttk.Label(basic, text="分析 Base URL：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.analysis_api_host_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.analysis_api_host_var, width=90).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, pady=5
        )
        row += 1

        ttk.Label(basic, text="分析 Chat Path：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.analysis_chat_path_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.analysis_chat_path_var, width=90).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, pady=5
        )
        row += 1

        ttk.Label(basic, text="生图 API Key：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.image_api_key_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.image_api_key_var, width=90, show="*").grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, pady=5
        )
        row += 1

        ttk.Label(basic, text="生图 Base URL：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.image_api_host_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.image_api_host_var, width=90).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, pady=5
        )
        row += 1

        ttk.Label(basic, text="生图 Chat Path：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.image_chat_path_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.image_chat_path_var, width=90).grid(
            row=row, column=1, sticky=tk.EW, pady=5
        )
        ttk.Button(basic, text="填入 micuapi 参数", command=self.apply_micuapi_provider).grid(
            row=row, column=2, padx=5
        )
        row += 1

        ttk.Label(basic, text="生图方式：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.image_generation_method_var = tk.StringVar()
        ttk.Combobox(
            basic,
            textvariable=self.image_generation_method_var,
            values=list(IMAGE_GENERATION_METHODS.keys()),
            state="readonly",
            width=28,
        ).grid(row=row, column=1, sticky=tk.W, pady=5)
        row += 1

        ttk.Label(basic, text="Images Edits Path：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.image_edit_path_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.image_edit_path_var, width=90).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, pady=5
        )
        row += 1

        ttk.Label(basic, text="分析模型：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.analysis_model_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.analysis_model_var, width=90).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, pady=5
        )
        row += 1

        ttk.Label(basic, text="图片模型：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.image_model_var = tk.StringVar()
        ttk.Combobox(
            basic,
            textvariable=self.image_model_var,
            values=["gpt-image-2", "gpt-image-1.5", "gpt-image-1", "nano-banana-2"],
        ).grid(
            row=row, column=1, columnspan=2, sticky=tk.EW, pady=5
        )
        row += 1

        ttk.Label(basic, text="图片尺寸：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.image_size_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.image_size_var, width=20).grid(row=row, column=1, sticky=tk.W, pady=5)
        row += 1

        ttk.Label(basic, text="图片质量：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.image_quality_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.image_quality_var, width=20).grid(row=row, column=1, sticky=tk.W, pady=5)
        row += 1

        ttk.Label(basic, text="SKU 前缀：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.sku_prefix_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.sku_prefix_var, width=20).grid(row=row, column=1, sticky=tk.W, pady=5)
        row += 1

        ttk.Label(basic, text="输出文件夹：").grid(row=row, column=0, sticky=tk.W, pady=5)
        self.out_subfolder_var = tk.StringVar()
        ttk.Entry(basic, textvariable=self.out_subfolder_var, width=20).grid(row=row, column=1, sticky=tk.W, pady=5)
        row += 1

        basic.columnconfigure(1, weight=1)

        # =========================
        # 高级配置 Tab
        # =========================

        advanced = ttk.LabelFrame(advanced_tab, text="品类、风格与高级配置", padding=10)
        advanced.pack(fill=tk.X, padx=8, pady=8)

        r = 0

        ttk.Label(advanced, text="产品品类：").grid(row=r, column=0, sticky=tk.W, pady=5)
        self.category_var = tk.StringVar()
        self.category_combo = ttk.Combobox(
            advanced,
            textvariable=self.category_var,
            values=list(CATEGORY_PROFILES.keys()),
            state="readonly",
            width=30
        )
        self.category_combo.grid(row=r, column=1, sticky=tk.W, pady=5)
        self.category_combo.bind("<<ComboboxSelected>>", self.update_category_desc)

        ttk.Button(
            advanced,
            text="按品类推荐模板",
            command=self.apply_category_templates
        ).grid(row=r, column=2, padx=5, pady=5)
        r += 1

        self.category_desc_var = tk.StringVar()
        ttk.Label(
            advanced,
            textvariable=self.category_desc_var,
            wraplength=760,
            foreground="#555"
        ).grid(row=r, column=0, columnspan=3, sticky=tk.W, pady=5)
        r += 1

        ttk.Label(advanced, text="生成风格：").grid(row=r, column=0, sticky=tk.W, pady=5)
        self.style_var = tk.StringVar()
        self.style_combo = ttk.Combobox(
            advanced,
            textvariable=self.style_var,
            values=list(STYLE_PRESETS.keys()),
            state="readonly",
            width=30
        )
        self.style_combo.grid(row=r, column=1, sticky=tk.W, pady=5)
        self.style_combo.bind("<<ComboboxSelected>>", self.update_style_desc)
        r += 1

        self.style_desc_var = tk.StringVar()
        ttk.Label(
            advanced,
            textvariable=self.style_desc_var,
            wraplength=760,
            foreground="#555"
        ).grid(row=r, column=0, columnspan=3, sticky=tk.W, pady=5)
        r += 1

        ttk.Separator(advanced, orient=tk.HORIZONTAL).grid(
            row=r, column=0, columnspan=3, sticky=tk.EW, pady=10
        )
        r += 1

        ttk.Label(advanced, text="画幅描述：").grid(row=r, column=0, sticky=tk.W, pady=5)
        self.aspect_text_var = tk.StringVar()
        ttk.Entry(advanced, textvariable=self.aspect_text_var, width=40).grid(
            row=r, column=1, columnspan=2, sticky=tk.W, pady=5
        )
        r += 1

        ttk.Label(advanced, text="分辨率描述：").grid(row=r, column=0, sticky=tk.W, pady=5)
        self.res_text_var = tk.StringVar()
        ttk.Entry(advanced, textvariable=self.res_text_var, width=40).grid(
            row=r, column=1, columnspan=2, sticky=tk.W, pady=5
        )
        r += 1

        ttk.Label(advanced, text="图片温度：").grid(row=r, column=0, sticky=tk.W, pady=5)
        self.temperature_var = tk.StringVar()
        ttk.Entry(advanced, textvariable=self.temperature_var, width=12).grid(row=r, column=1, sticky=tk.W, pady=5)
        r += 1

        ttk.Label(advanced, text="生成图片数量：").grid(row=r, column=0, sticky=tk.W, pady=5)
        self.image_plan_count_var = tk.IntVar()
        ttk.Spinbox(
            advanced,
            from_=IMAGE_PLAN_MIN_COUNT,
            to=IMAGE_PLAN_MAX_COUNT,
            textvariable=self.image_plan_count_var,
            width=10
        ).grid(row=r, column=1, sticky=tk.W, pady=5)
        r += 1

        ttk.Label(advanced, text="API 并发数：").grid(row=r, column=0, sticky=tk.W, pady=5)
        self.concurrent_var = tk.IntVar()
        ttk.Spinbox(advanced, from_=1, to=20, textvariable=self.concurrent_var, width=10).grid(
            row=r, column=1, sticky=tk.W, pady=5
        )
        r += 1

        ttk.Label(advanced, text="产品目录并发：").grid(row=r, column=0, sticky=tk.W, pady=5)
        self.folder_concurrent_var = tk.IntVar()
        ttk.Spinbox(advanced, from_=1, to=10, textvariable=self.folder_concurrent_var, width=10).grid(
            row=r, column=1, sticky=tk.W, pady=5
        )
        r += 1

        ttk.Label(advanced, text="最大图片 MB：").grid(row=r, column=0, sticky=tk.W, pady=5)
        self.max_image_mb_var = tk.IntVar()
        ttk.Spinbox(advanced, from_=1, to=50, textvariable=self.max_image_mb_var, width=10).grid(
            row=r, column=1, sticky=tk.W, pady=5
        )
        r += 1

        ttk.Separator(advanced, orient=tk.HORIZONTAL).grid(
            row=r, column=0, columnspan=3, sticky=tk.EW, pady=10
        )
        r += 1

        self.generate_for_each_sku_cover_var = tk.BooleanVar()
        ttk.Checkbutton(
            advanced,
            text="为每个 SKU 单独生成主图海报",
            variable=self.generate_for_each_sku_cover_var
        ).grid(row=r, column=0, columnspan=3, sticky=tk.W, pady=5)
        r += 1

        self.generate_dimension_image_var = tk.BooleanVar()
        ttk.Checkbutton(
            advanced,
            text="生成计划中必须包含尺寸图",
            variable=self.generate_dimension_image_var
        ).grid(row=r, column=0, columnspan=3, sticky=tk.W, pady=5)
        r += 1

        self.auto_detect_model_var = tk.BooleanVar()
        ttk.Checkbutton(
            advanced,
            text="自动检测真人/手持/模特图",
            variable=self.auto_detect_model_var
        ).grid(row=r, column=0, columnspan=3, sticky=tk.W, pady=5)
        r += 1

        self.include_bundle_reference_var = tk.BooleanVar()
        ttk.Checkbutton(
            advanced,
            text="尝试使用配件/包装/套装参考图",
            variable=self.include_bundle_reference_var
        ).grid(row=r, column=0, columnspan=3, sticky=tk.W, pady=5)
        r += 1

        self.fast_mode_var = tk.BooleanVar()
        ttk.Checkbutton(
            advanced,
            text="快速模式：减少分析请求，只分析第一张 SKU 颜色",
            variable=self.fast_mode_var
        ).grid(row=r, column=0, columnspan=3, sticky=tk.W, pady=5)
        r += 1

        ttk.Label(
            advanced,
            text="并发说明：目录并发控制同时处理的产品数；所有目录共享最大并发请求，不会相乘。",
        ).grid(row=r, column=0, columnspan=3, sticky=tk.W, pady=5)
        r += 1

        advanced.columnconfigure(1, weight=1)

        # =========================
        # 模板 Tab
        # =========================

        templates_frame = ttk.LabelFrame(templates_tab, text="历史模板配置（当前动态 image_plan 不再受模板约束）", padding=10)
        templates_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        ttk.Label(
            templates_frame,
            text="当前版本只使用“生成图片数量”和“是否生成尺寸图”作为硬约束；这里的模板勾选仅保留用于旧配置兼容。",
            foreground="#A05A00",
            wraplength=900,
        ).grid(row=0, column=0, columnspan=4, sticky=tk.W, padx=5, pady=(0, 8))

        ttk.Label(templates_frame, text="启用", font=("Microsoft YaHei", 9, "bold")).grid(
            row=1, column=0, sticky=tk.W, padx=5, pady=3
        )
        ttk.Label(templates_frame, text="图片类型", font=("Microsoft YaHei", 9, "bold")).grid(
            row=1, column=1, sticky=tk.W, padx=5, pady=3
        )
        ttk.Label(templates_frame, text="数量", font=("Microsoft YaHei", 9, "bold")).grid(
            row=1, column=2, sticky=tk.W, padx=5, pady=3
        )
        ttk.Label(templates_frame, text="说明", font=("Microsoft YaHei", 9, "bold")).grid(
            row=1, column=3, sticky=tk.W, padx=5, pady=3
        )

        for idx, (name, tpl) in enumerate(IMAGE_TYPE_TEMPLATES.items(), start=2):
            enable_var = tk.BooleanVar()
            count_var = tk.IntVar(value=1)

            self.template_vars[name] = enable_var
            self.template_count_vars[name] = count_var

            ttk.Checkbutton(
                templates_frame,
                variable=enable_var
            ).grid(row=idx, column=0, sticky=tk.W, padx=5, pady=3)

            ttk.Label(
                templates_frame,
                text=name
            ).grid(row=idx, column=1, sticky=tk.W, padx=5, pady=3)

            ttk.Spinbox(
                templates_frame,
                from_=0,
                to=20,
                textvariable=count_var,
                width=6
            ).grid(row=idx, column=2, sticky=tk.W, padx=5, pady=3)

            prompt_preview = tpl["prompt"]
            if len(prompt_preview) > 120:
                prompt_preview = prompt_preview[:120] + "..."

            ttk.Label(
                templates_frame,
                text=prompt_preview,
                foreground="#555",
                wraplength=780
            ).grid(row=idx, column=3, sticky=tk.W, padx=5, pady=3)

        templates_frame.columnconfigure(3, weight=1)

        # =========================
        # 按钮区域，固定在日志上方
        # =========================

        btn_frame = ttk.Frame(bottom_area)
        btn_frame.pack(fill=tk.X, pady=(0, 6))

        ttk.Button(
            btn_frame,
            text="保存配置",
            command=self.save_ui_config
        ).pack(side=tk.LEFT, padx=5)

        ttk.Button(
            btn_frame,
            text="加载配置",
            command=self.reload_config
        ).pack(side=tk.LEFT, padx=5)

        ttk.Button(
            btn_frame,
            text="全选模板",
            command=self.select_all_templates
        ).pack(side=tk.LEFT, padx=5)

        ttk.Button(
            btn_frame,
            text="清空模板",
            command=self.clear_templates
        ).pack(side=tk.LEFT, padx=5)

        ttk.Button(
            btn_frame,
            text="按品类推荐模板",
            command=self.apply_category_templates
        ).pack(side=tk.LEFT, padx=5)

        self.start_btn = ttk.Button(
            btn_frame,
            text="开始生成",
            command=self.start_generation
        )
        self.start_btn.pack(side=tk.RIGHT, padx=5)

        self.stop_btn = ttk.Button(
            btn_frame,
            text="停止",
            command=self.stop_generation,
            state=tk.DISABLED
        )
        self.stop_btn.pack(side=tk.RIGHT, padx=5)

        self.pause_btn = ttk.Button(
            btn_frame,
            text="暂停",
            command=self.toggle_pause_generation,
            state=tk.DISABLED
        )
        self.pause_btn.pack(side=tk.RIGHT, padx=5)

        # =========================
        # 日志区域，固定显示在底部
        # =========================

        log_frame = ttk.LabelFrame(bottom_area, text="运行日志", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_frame, height=10, wrap=tk.WORD)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.log_text.configure(yscrollcommand=scroll.set)


    # =========================
    # GUI 事件与配置
    # =========================

    def choose_root_dir(self):
        path = filedialog.askdirectory()
        if path:
            self.root_dir_var.set(path)

    def toggle_api_key_show(self):
        current = self.api_key_entry.cget("show")
        if current == "*":
            self.api_key_entry.configure(show="")
        else:
            self.api_key_entry.configure(show="*")

    def apply_micuapi_provider(self):
        self.analysis_api_host_var.set("https://www.micuapi.ai")
        self.analysis_chat_path_var.set("/v1/chat/completions")
        self.image_api_host_var.set("https://www.micuapi.ai")
        self.image_chat_path_var.set("/v1/chat/completions")
        self.image_edit_path_var.set("/v1/images/edits")
        self.analysis_model_var.set("gpt-5.5")
        self.image_model_var.set("gpt-image-2")
        self.image_generation_method_var.set("image_edits")

    def update_category_desc(self, event=None):
        name = self.category_var.get()
        profile = CATEGORY_PROFILES.get(name, CATEGORY_PROFILES["自动识别"])
        desc = profile.get("description", "")
        model_policy = profile.get("model_policy", "auto")
        self.category_desc_var.set(f"{desc}  模特策略：{model_policy}")

    def update_style_desc(self, event=None):
        name = self.style_var.get()
        preset = STYLE_PRESETS.get(name, STYLE_PRESETS["自动"])
        self.style_desc_var.set(preset.get("description", ""))

    def apply_category_templates(self):
        name = self.category_var.get() or "自动识别"
        profile = CATEGORY_PROFILES.get(name, CATEGORY_PROFILES["自动识别"])
        defaults = profile.get("default_templates", {})

        for tpl_name in IMAGE_TYPE_TEMPLATES:
            count = int(defaults.get(tpl_name, 0))
            self.template_vars[tpl_name].set(count > 0)
            if count > 0:
                self.template_count_vars[tpl_name].set(count)
            else:
                self.template_count_vars[tpl_name].set(1)

        self.log(f"已按品类「{name}」推荐模板：{defaults}")

    def select_all_templates(self):
        for name in IMAGE_TYPE_TEMPLATES:
            self.template_vars[name].set(True)
            if self.template_count_vars[name].get() <= 0:
                self.template_count_vars[name].set(1)

    def clear_templates(self):
        for name in IMAGE_TYPE_TEMPLATES:
            self.template_vars[name].set(False)

    def load_cfg_to_ui(self):
        cfg = self.cfg

        self.root_dir_var.set(cfg.root_dir)
        self.api_key_var.set(cfg.api_key)
        self.product_hint_var.set(cfg.product_hint)

        self.api_host_var.set(cfg.api_host)
        self.chat_path_var.set(cfg.chat_path)
        self.analysis_api_key_var.set(cfg.analysis_api_key)
        self.analysis_api_host_var.set(cfg.analysis_api_host)
        self.analysis_chat_path_var.set(cfg.analysis_chat_path)
        self.image_api_key_var.set(cfg.image_api_key)
        self.image_api_host_var.set(cfg.image_api_host)
        self.image_chat_path_var.set(cfg.image_chat_path)
        self.image_generation_method_var.set(cfg.image_generation_method)
        self.image_edit_path_var.set(cfg.image_edit_path)
        self.analysis_model_var.set(cfg.analysis_model)
        self.image_model_var.set(cfg.image_model)
        self.image_size_var.set(cfg.image_size)
        self.image_quality_var.set(cfg.image_quality)

        self.sku_prefix_var.set(cfg.sku_prefix)
        self.out_subfolder_var.set(cfg.out_subfolder)

        if cfg.category_name not in CATEGORY_PROFILES:
            cfg.category_name = "自动识别"
        self.category_var.set(cfg.category_name)
        self.update_category_desc()

        if cfg.style_name not in STYLE_PRESETS:
            cfg.style_name = "自动"
        self.style_var.set(cfg.style_name)
        self.update_style_desc()

        self.aspect_text_var.set(cfg.aspect_text)
        self.res_text_var.set(cfg.res_text)

        self.temperature_var.set(str(cfg.image_temperature))
        self.image_plan_count_var.set(desired_image_plan_count(cfg))
        self.concurrent_var.set(cfg.max_concurrent_requests)
        self.folder_concurrent_var.set(cfg.folder_concurrency)
        self.max_image_mb_var.set(cfg.max_image_mb)

        self.generate_for_each_sku_cover_var.set(cfg.generate_for_each_sku_cover)
        self.generate_dimension_image_var.set(bool(getattr(cfg, "generate_dimension_image", True)))
        self.auto_detect_model_var.set(cfg.auto_detect_model)
        self.include_bundle_reference_var.set(cfg.include_bundle_reference)
        self.fast_mode_var.set(cfg.fast_mode)

        selected = cfg.selected_templates or {}

        for tpl_name in IMAGE_TYPE_TEMPLATES:
            count = int(selected.get(tpl_name, 0))
            self.template_vars[tpl_name].set(count > 0)
            if count > 0:
                self.template_count_vars[tpl_name].set(count)
            else:
                self.template_count_vars[tpl_name].set(1)

    def collect_ui_config(self) -> AppConfig:
        selected_templates = {}

        for tpl_name in IMAGE_TYPE_TEMPLATES:
            if self.template_vars[tpl_name].get():
                try:
                    count = int(self.template_count_vars[tpl_name].get())
                except Exception:
                    count = 1

                if count > 0:
                    selected_templates[tpl_name] = count

        try:
            image_temperature = float(self.temperature_var.get())
        except Exception:
            image_temperature = 0.075

        try:
            max_concurrent_requests = int(self.concurrent_var.get())
        except Exception:
            max_concurrent_requests = DEFAULT_MAX_CONCURRENT_REQUESTS

        try:
            folder_concurrency = int(self.folder_concurrent_var.get())
        except Exception:
            folder_concurrency = DEFAULT_FOLDER_CONCURRENCY

        try:
            max_image_mb = int(self.max_image_mb_var.get())
        except Exception:
            max_image_mb = DEFAULT_MAX_IMAGE_MB

        try:
            image_plan_count = clamp_image_plan_count(self.image_plan_count_var.get())
        except Exception:
            image_plan_count = DEFAULT_IMAGE_PLAN_COUNT

        cfg = AppConfig(
            api_key=self.api_key_var.get().strip(),
            root_dir=self.root_dir_var.get().strip(),
            product_hint=self.product_hint_var.get().strip(),

            api_host=self.api_host_var.get().strip() or DEFAULT_API_HOST,
            chat_path=self.chat_path_var.get().strip() or DEFAULT_CHAT_PATH,
            analysis_api_key=self.analysis_api_key_var.get().strip(),
            analysis_api_host=self.analysis_api_host_var.get().strip().rstrip("/"),
            analysis_chat_path=self.analysis_chat_path_var.get().strip(),
            image_api_key=self.image_api_key_var.get().strip(),
            image_api_host=self.image_api_host_var.get().strip().rstrip("/"),
            image_chat_path=self.image_chat_path_var.get().strip(),
            image_generation_method=self.image_generation_method_var.get().strip() or DEFAULT_IMAGE_GENERATION_METHOD,
            image_edit_path=self.image_edit_path_var.get().strip() or DEFAULT_IMAGE_EDIT_PATH,
            analysis_model=self.analysis_model_var.get().strip() or DEFAULT_ANALYSIS_MODEL,
            image_model=self.image_model_var.get().strip() or DEFAULT_IMAGE_MODEL,
            image_size=self.image_size_var.get().strip(),
            image_quality=self.image_quality_var.get().strip(),

            sku_prefix=self.sku_prefix_var.get().strip() or DEFAULT_SKU_PREFIX,
            out_subfolder=self.out_subfolder_var.get().strip() or DEFAULT_OUT_SUBFOLDER,

            aspect_text=self.aspect_text_var.get().strip() or DEFAULT_ASPECT_TEXT,
            res_text=self.res_text_var.get().strip() or DEFAULT_RES_TEXT,

            style_name=self.style_var.get().strip() or "自动",
            category_name=self.category_var.get().strip() or "自动识别",

            image_temperature=image_temperature,
            max_concurrent_requests=max_concurrent_requests,
            folder_concurrency=folder_concurrency,
            max_image_mb=max_image_mb,
            image_plan_count=image_plan_count,

            generate_for_each_sku_cover=bool(self.generate_for_each_sku_cover_var.get()),
            generate_dimension_image=bool(self.generate_dimension_image_var.get()),
            auto_detect_model=bool(self.auto_detect_model_var.get()),
            include_bundle_reference=bool(self.include_bundle_reference_var.get()),
            fast_mode=bool(self.fast_mode_var.get()),

            selected_templates=selected_templates
        )

        return cfg

    def save_ui_config(self):
        try:
            cfg = self.collect_ui_config()
            save_config(cfg)
            self.cfg = cfg
            messagebox.showinfo("成功", f"配置已保存：\n{CONFIG_PATH}")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def reload_config(self):
        try:
            self.cfg = load_config()
            self.load_cfg_to_ui()
            messagebox.showinfo("成功", "配置已重新加载")
        except Exception as e:
            messagebox.showerror("加载失败", str(e))

    # =========================
    # 日志
    # =========================

    def log(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {msg}")

    def poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
        except queue.Empty:
            pass

        self.root.after(100, self.poll_log_queue)

    # =========================
    # 任务控制
    # =========================

    def start_generation(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("提示", "任务正在运行中")
            return

        try:
            cfg = self.collect_ui_config()
        except Exception as e:
            messagebox.showerror("配置错误", str(e))
            return

        if not (cfg.analysis_api_key or cfg.api_key):
            messagebox.showerror("配置错误", "分析 API Key 不能为空")
            return

        if not (cfg.image_api_key or cfg.api_key):
            messagebox.showerror("配置错误", "生图 API Key 不能为空")
            return

        if not cfg.root_dir:
            messagebox.showerror("配置错误", "图片目录不能为空")
            return

        if desired_image_plan_count(cfg) <= 0:
            messagebox.showerror("配置错误", "生成图片数量必须大于 0")
            return

        try:
            save_config(cfg)
        except Exception as e:
            messagebox.showwarning("提醒", f"配置保存失败，但仍会继续运行：{e}")

        self.cfg = cfg
        self.stop_event.clear()
        self.pause_event.clear()

        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.pause_btn.configure(state=tk.NORMAL, text="暂停")

        self.log_text.delete("1.0", tk.END)
        self.log("任务启动")
        self.log(f"图片目录：{cfg.root_dir}")
        self.log(f"产品品类：{cfg.category_name}")
        self.log(f"生成风格：{cfg.style_name}")
        self.log(f"生图方式：{IMAGE_GENERATION_METHODS.get(cfg.image_generation_method, cfg.image_generation_method)} / {cfg.image_model}")
        self.log(f"生成图片数量：{desired_image_plan_count(cfg)}；尺寸图：{'生成' if cfg.generate_dimension_image else '不生成'}")

        def worker():
            try:
                asyncio.run(
                    run_generation_async(
                        cfg,
                        self.log,
                        self.stop_event,
                        pause_event=self.pause_event,
                    )
                )
            except Exception as e:
                self.log(f"[严重错误] {e}")
            finally:
                self.log("任务结束")
                self.root.after(0, self.on_worker_finished)

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def stop_generation(self):
        self.stop_event.set()
        self.pause_event.clear()
        self.log("已请求停止。当前正在进行的 API 请求可能需要等待完成。")
        self.stop_btn.configure(state=tk.DISABLED)
        self.pause_btn.configure(state=tk.DISABLED, text="暂停")

    def toggle_pause_generation(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.configure(text="暂停")
            self.log("已继续任务")
        else:
            self.pause_event.set()
            self.pause_btn.configure(text="继续")
            self.log("已请求暂停。当前 API 请求完成后将在下一个安全点暂停。")

    def on_worker_finished(self):
        self.pause_event.clear()
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.pause_btn.configure(state=tk.DISABLED, text="暂停")


# =========================
# 程序入口
# =========================

def main():
    args = parse_args()

    if args.root_dir:
        try:
            run_cli(args)
            return
        except Exception as e:
            print(f"错误: {e}")
            raise SystemExit(1)

    if args.no_gui:
        print("错误: 使用 --no-gui 时必须提供 --root-dir")
        raise SystemExit(1)

    root = tk.Tk()
    app = OzonImageGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
