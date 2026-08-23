import io
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ================== НАСТРОЙКИ ==================
REFRESH_INTERVAL_MINUTES = 5
OUTPUT_WIDTH = 800
OUTPUT_HEIGHT = 480

OUTPUT_IMAGE = "current.jpg"
OUTPUT_HTML = "index.html"
LAST_UPDATE_FILE = "last_update.txt"

# Art Institute of Chicago
AIC_API = "https://api.artic.edu/api/v1/artworks"
AIC_IIIF = "https://www.artic.edu/iiif/2/{image_id}/full/843,/0/default.jpg"
# ================================================

HEADERS = {
    "User-Agent": "epaper-art-gallery/1.0"
}

def clean_text(value, max_length):
    """Убирает лишние пробелы и переносы; ограничивает длину текста."""
    text = str(value or "")
    text = text.replace("$", "")
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"

    return text

def short_artist(value):
    """У artist_display может быть несколько строк — берём первую."""
    lines = [
        line.strip()
        for line in str(value or "").splitlines()
        if line.strip()
    ]
    return clean_text(lines[0] if lines else "Unknown artist", 42)

def should_update():
    """Не создаёт новую картину до истечения заданного интервала."""
    if os.environ.get("FORCE_UPDATE") == "1":
        return True

    path = Path(LAST_UPDATE_FILE)

    if not path.exists():
        return True

    try:
        last_update = float(path.read_text(encoding="utf-8").strip())
        elapsed_seconds = time.time() - last_update
        return elapsed_seconds >= REFRESH_INTERVAL_MINUTES * 60
    except Exception:
        return True

def get_random_artwork():
    """Получает случайную работу с изображением из каталога Art Institute of Chicago."""
    # ВАЖНО: total_pages считаем с тем же limit=100, что и в основном запросе,
    # иначе номер случайной страницы выходит далеко за пределы реального каталога.
    first_response = requests.get(
        AIC_API,
        params={
            "page": 1,
            "limit": 100,
            "fields": "id",
        },
        headers=HEADERS,
        timeout=20,
    )
    first_response.raise_for_status()

    pagination = first_response.json().get("pagination", {})
    total_pages = int(pagination.get("total_pages", 1))

    # Некоторые записи не имеют изображения: пробуем до 12 страниц.
    for _ in range(12):
        page = random.randint(1, max(1, total_pages))

        response = requests.get(
            AIC_API,
            params={
                "page": page,
                "limit": 100,
                "fields": "id,title,artist_display,date_display,image_id",
            },
            headers=HEADERS,
            timeout=20,
        )
        response.raise_for_status()

        candidates = [
            item
            for item in response.json().get("data", [])
            if item.get("image_id")
        ]

        if candidates:
            return random.choice(candidates)

    raise RuntimeError(
        "Не удалось получить запись с изображением из каталога."
    )

def get_font(size, bold=False):
    candidates = [
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]

    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            pass

    return ImageFont.load_default()

def create_image(artwork):
    image_id = artwork["image_id"]
    image_url = AIC_IIIF.format(image_id=image_id)

    image_response = requests.get(
        image_url,
        headers=HEADERS,
        timeout=45,
    )
    image_response.raise_for_status()

    source = Image.open(
        io.BytesIO(image_response.content)
    ).convert("RGB")

    # Заполняет весь экран 800x480; края могут немного обрезаться.
    canvas = ImageOps.fit(
        source,
        (OUTPUT_WIDTH, OUTPUT_HEIGHT),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    ).convert("RGBA")

    title = clean_text(artwork.get("title") or "Untitled", 48)
    artist = short_artist(artwork.get("artist_display"))
    year = clean_text(artwork.get("date_display"), 18)
    meta = f"{artist} · {year}" if year else artist

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    title_font = get_font(17, bold=True)
    meta_font = get_font(13)

    padding_x = 10
    margin = 10
    box_height = 48

    title_box = draw.textbbox((0, 0), title, font=title_font)
    meta_box = draw.textbbox((0, 0), meta, font=meta_font)

    text_width = max(
        title_box[2] - title_box[0],
        meta_box[2] - meta_box[0],
    )
    box_width = min(
        text_width + padding_x * 2,
        int(OUTPUT_WIDTH * 0.62),
    )

    x1 = margin
    y1 = OUTPUT_HEIGHT - box_height - margin
    x2 = x1 + box_width
    y2 = y1 + box_height

    # Маленькая светлая плашка — не перекрывает значительную часть картины.
    draw.rounded_rectangle(
        (x1, y1, x2, y2),
        radius=4,
        fill=(255, 255, 255, 215),
    )

    draw.text(
        (x1 + padding_x, y1 + 5),
        title,
        font=title_font,
        fill=(0, 0, 0, 255),
    )
    draw.text(
        (x1 + padding_x, y1 + 27),
        meta,
        font=meta_font,
        fill=(45, 45, 45, 255),
    )

    final_image = Image.alpha_composite(canvas, overlay).convert("RGB")
    final_image.save(OUTPUT_IMAGE, "JPEG", quality=92, optimize=True)

    return title, meta

def write_html(version):
    """Страница не исполняет JS: она немедленно открывает готовый JPG."""
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Art Gallery</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: #fff;
    }}
    img {{
      display: block;
      width: 100vw;
      height: 100vh;
      object-fit: contain;
    }}
  </style>
</head>
<body>
  <img src="current.jpg?v={version}" alt="Artwork">
</body>
</html>
"""
    Path(OUTPUT_HTML).write_text(html, encoding="utf-8")

def main():
    if not should_update():
        print("Интервал ещё не истёк: генерация пропущена.")
        return

    artwork = get_random_artwork()
    title, meta = create_image(artwork)

    version = int(time.time())
    write_html(version)
    Path(LAST_UPDATE_FILE).write_text(str(version), encoding="utf-8")

    print(f"Создана новая картина: {title} — {meta}")

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Ошибка генерации: {error}")
        sys.exit(1)
