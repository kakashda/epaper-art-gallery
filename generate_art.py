# generate_art.py
import io
import math
import os
import random
import re
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter, ImageEnhance

# ================== НАСТРОЙКИ ==================
REFRESH_INTERVAL_MINUTES = 2
OUTPUT_WIDTH = 800      # итоговое разрешение для e-paper (ширина)
OUTPUT_HEIGHT = 480     # итоговое разрешение для e-paper (высота)

OUTPUT_IMAGE = "current.jpg"
OUTPUT_HTML = "index.html"
LAST_UPDATE_FILE = "last_update.txt"

# Сетевые параметры
HTTP_TIMEOUT = 30
MAX_RETRIES = 3          # число повторов на один HTTP-запрос
RETRY_BACKOFF = 2.0      # базовая задержка между повторами (секунды)
MAX_CANDIDATES = 25      # сколько объектов пробуем у одного источника
MIN_IMAGE_BYTES = 5000   # меньший размер почти наверняка означает страницу-ошибку
# ================================================

# Реалистичный браузерный User-Agent помогает обойти простые анти-бот проверки
# и корректно пройти content-negotiation на CDN музеев.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Заголовки для API-запросов (JSON)
API_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json",
}

# Заголовки для скачивания изображений.
# Явно перечисляем конкретные типы, чтобы избежать 406 Not Acceptable
# на некоторых CDN-узлах, которые не любят обобщённый "image/*".
IMG_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "image/avif,image/webp,image/apng,image/jpeg,image/png,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ==================================================================
#                         HTTP-хелперы
# ==================================================================

def _request_with_retries(url, *, params=None, headers=None, label="request"):
    """GET с повторами и экспоненциальной задержкой.

    Возвращает объект Response с кодом 200 либо бросает исключение
    после исчерпания попыток. Повторяем только временные/анти-бот
    ошибки (403, 406, 408, 429, 5xx) и сетевые сбои.
    """
    retryable_status = {403, 406, 408, 425, 429, 500, 502, 503, 504}
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=HTTP_TIMEOUT,
            )
            status = response.status_code
            print(f"[{label}] GET {response.url} -> HTTP {status} (попытка {attempt})")

            if status == 200:
                return response

            last_error = requests.HTTPError(
                f"HTTP {status} for url: {response.url}", response=response
            )

            if status not in retryable_status:
                # Не временная ошибка — повторять смысла нет.
                print(f"[{label}] Неповторяемый статус {status}, прекращаем попытки.")
                raise last_error

            print(f"[{label}] Тело ответа (первые 200 символов): {response.text[:200]!r}")

        except requests.RequestException as error:
            last_error = error
            print(f"[{label}] Сетевая ошибка на попытке {attempt}: {error}")

        if attempt < MAX_RETRIES:
            delay = RETRY_BACKOFF * attempt + random.uniform(0, 0.8)
            print(f"[{label}] Ждём {delay:.1f}s перед повтором...")
            time.sleep(delay)

    raise last_error if last_error else RuntimeError(f"[{label}] Запрос не удался.")


def fetch_json(url, params=None, label="json"):
    """Загружает и парсит JSON с повторами."""
    response = _request_with_retries(url, params=params, headers=API_HEADERS, label=label)
    try:
        return response.json()
    except ValueError as error:
        print(f"[{label}] Ответ не является валидным JSON: {response.text[:300]!r}")
        raise RuntimeError(f"Некорректный JSON от API: {error}") from error


def download_image_bytes(url, label="image"):
    """Скачивает изображение с повторами и валидирует, что это настоящая картинка.

    Возвращает bytes либо бросает исключение.
    """
    referer = None
    if "metmuseum.org" in url:
        referer = "https://www.metmuseum.org/"
    elif "clevelandart.org" in url:
        referer = "https://www.clevelandart.org/"

    headers = dict(IMG_HEADERS)
    if referer:
        headers["Referer"] = referer

    response = _request_with_retries(url, headers=headers, label=label)
    content = response.content

    if len(content) < MIN_IMAGE_BYTES:
        raise RuntimeError(
            f"[{label}] Слишком маленький ответ ({len(content)} байт) — вероятно, не изображение."
        )

    content_type = (response.headers.get("Content-Type") or "").lower()
    if content_type and "image" not in content_type:
        raise RuntimeError(
            f"[{label}] Неверный Content-Type: {content_type!r} (ожидали изображение)."
        )

    # Финальная проверка — реально ли это декодируется как изображение.
    try:
        Image.open(io.BytesIO(content)).verify()
    except Exception as error:
        raise RuntimeError(f"[{label}] Данные не распознаны как изображение: {error}") from error

    return content


# ==================================================================
#                    Источники изображений (провайдеры)
# ==================================================================

# Знаменитейшие европейские мастера — «светила» мировой живописи.
# Их работы (в открытом доступе / public domain) есть в коллекциях The Met
# и Cleveland Museum of Art. Каждый запуск выбираем случайного мастера,
# чтобы галерея показывала самые впечатляющие европейские шедевры.
FAMOUS_ARTISTS = [
    "Vincent van Gogh", "Claude Monet", "Rembrandt van Rijn",
    "Johannes Vermeer", "Pierre-Auguste Renoir", "Edgar Degas",
    "Paul Cezanne", "Edouard Manet", "Camille Pissarro", "Paul Gauguin",
    "Georges Seurat", "J. M. W. Turner", "Eugene Delacroix",
    "Francisco Goya", "Diego Velazquez", "El Greco", "Titian",
    "Raphael", "Sandro Botticelli", "Pieter Bruegel the Elder",
    "Peter Paul Rubens", "Caravaggio", "Nicolas Poussin",
    "Jean-Honore Fragonard", "Camille Corot", "Gustave Courbet",
    "Jean-Francois Millet", "Anthony van Dyck", "Frans Hals",
    "Canaletto", "Henri de Toulouse-Lautrec", "Henri Rousseau",
    "Gustav Klimt", "Albrecht Durer", "Hans Holbein the Younger",
    "Nicolas de Stael", "Georges de La Tour", "Jean-Baptiste-Simeon Chardin",
]

# Резервные тематические запросы (если по конкретному мастеру ничего не нашли) —
# тоже смещены в сторону европейской классической живописи.
FALLBACK_TERMS = [
    "European painting", "old master painting", "impressionism painting",
    "baroque painting", "renaissance painting", "portrait oil painting",
    "landscape oil painting",
]

# Европейские отделы The Met — сильно повышают шанс попасть на «светил».
MET_EUROPEAN_DEPARTMENTS = {
    "European Paintings",
    "European Sculpture and Decorative Arts",
    "Robert Lehman Collection",
    "The Cloisters",
    "Modern and Contemporary Art",
    "Drawings and Prints",
}


def _clean_meta(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _is_painting(classification, medium):
    """Похоже ли произведение на живопись (а не гравюру/фото/мебель)."""
    text = f"{classification} {medium}".lower()
    keywords = ("painting", "oil", "tempera", "canvas", "panel", "fresco", "живопис")
    return any(k in text for k in keywords)


def met_candidates():
    """Генератор кандидатов из The Metropolitan Museum of Art.

    Ищет по имени конкретного знаменитого европейского мастера и отдаёт
    сначала живопись, затем остальные его работы. Формат словаря:
    {image_url, title, artist, date, source}.
    """
    query = random.choice(FAMOUS_ARTISTS + FALLBACK_TERMS)
    search_url = "https://collectionapi.metmuseum.org/public/collection/v1/search"
    object_url = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{}"

    try:
        data = fetch_json(
            search_url,
            {"hasImages": "true", "q": query},
            "met-search",
        )
    except Exception as error:
        print(f"[met] Поиск по '{query}' не удался: {error}")
        return

    object_ids = data.get("objectIDs") or []
    if not object_ids:
        print(f"[met] Поиск по '{query}' ничего не вернул.")
        return

    print(f"[met] Запрос '{query}': найдено {len(object_ids)} объектов.")
    random.shuffle(object_ids)

    deferred = []  # непейзажная живопись / прочее — отдаём во вторую очередь

    for i, object_id in enumerate(object_ids[:MAX_CANDIDATES], start=1):
        try:
            obj = fetch_json(object_url.format(object_id), label=f"met-obj-{i} ({object_id})")
        except Exception as error:
            print(f"[met] Не удалось получить объект {object_id}: {error}")
            continue

        if not obj.get("isPublicDomain"):
            continue

        image_url = obj.get("primaryImage") or obj.get("primaryImageSmall")
        if not image_url:
            continue

        candidate = {
            "image_url": image_url,
            "title": _clean_meta(obj.get("title")) or "Untitled",
            "artist": _clean_meta(obj.get("artistDisplayName")) or "Unknown artist",
            "date": _clean_meta(obj.get("objectDate")),
            "source": "The Met",
        }

        is_painting = _is_painting(obj.get("classification"), obj.get("medium"))
        is_european = (obj.get("department") in MET_EUROPEAN_DEPARTMENTS)

        # Живопись из европейских отделов — сразу, всё остальное — в резерв.
        if is_painting and is_european:
            yield candidate
        else:
            deferred.append(candidate)

    for candidate in deferred:
        yield candidate


def cleveland_candidates():
    """Генератор кандидатов из The Cleveland Museum of Art (Open Access, CC0).

    Ищет живопись конкретного европейского мастера (type=Painting).
    """
    query = random.choice(FAMOUS_ARTISTS + FALLBACK_TERMS)
    search_url = "https://openaccess-api.clevelandart.org/api/artworks/"

    params = {
        "q": query,
        "has_image": 1,
        "cc0": 1,
        "type": "Painting",
        "limit": 100,
        "fields": "id,title,creators,creation_date,images,type",
    }

    try:
        data = fetch_json(search_url, params, "cma-search")
    except Exception as error:
        print(f"[cma] Поиск по '{query}' не удался: {error}")
        return

    records = data.get("data") or []
    if not records:
        print(f"[cma] Поиск по '{query}' (Painting) ничего не вернул.")
        return

    print(f"[cma] Запрос '{query}': найдено {len(records)} картин.")
    random.shuffle(records)

    for record in records[:MAX_CANDIDATES]:
        images = record.get("images") or {}
        # print — самое крупное, web — среднее; берём максимально доступное качество.
        image_url = None
        for key in ("print", "web"):
            entry = images.get(key) or {}
            if entry.get("url"):
                image_url = entry["url"]
                break
        if not image_url:
            continue

        creators = record.get("creators") or []
        artist = "Unknown artist"
        if creators:
            artist = _clean_meta(creators[0].get("description") or creators[0].get("name")) \
                or "Unknown artist"
            # У Cleveland часто в description есть роль/годы в скобках — обрежем.
            artist = re.sub(r"\s*\(.*?\)\s*$", "", artist).strip() or "Unknown artist"

        yield {
            "image_url": image_url,
            "title": _clean_meta(record.get("title")) or "Untitled",
            "artist": artist,
            "date": _clean_meta(record.get("creation_date")),
            "source": "Cleveland Museum of Art",
        }


# Порядок провайдеров перемешиваем для разнообразия, но обходим все,
# пока не получим годное изображение.
PROVIDERS = [met_candidates, cleveland_candidates]


def get_random_artwork_bytes():
    """Пробует источники по очереди и возвращает (artwork_dict, image_bytes).

    Устойчив к 403/406 и прочим ошибкам: пропускает проблемные кандидаты
    и переходит к следующему источнику.
    """
    providers = PROVIDERS[:]
    random.shuffle(providers)

    last_error = None
    tried = 0

    for provider in providers:
        print(f"\n=== Источник: {provider.__name__} ===")
        for artwork in provider():
            tried += 1
            try:
                image_bytes = download_image_bytes(
                    artwork["image_url"], label=f"download ({artwork['source']})"
                )
                print(f"[ok] Изображение получено из {artwork['source']}: "
                      f"{artwork['title']} ({len(image_bytes)} байт)")
                return artwork, image_bytes
            except Exception as error:
                last_error = error
                print(f"[skip] {artwork['source']} — {error}")
                continue

    raise RuntimeError(
        f"Не удалось получить изображение ни из одного источника "
        f"(проверено кандидатов: {tried}). Последняя ошибка: {last_error}"
    )


# ==================================================================
#                      Обработка изображения
# ==================================================================

def clean_text(value, max_length):
    """Убирает лишние пробелы и переносы; ограничивает длину текста."""
    text = str(value or "")
    text = text.replace("$", "")
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"

    return text


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
        except Exception:
            pass
    return ImageFont.load_default()


def upscale_if_needed(img, target_w, target_h):
    """Апскейлит изображение до покрытия целевого размера, если оно меньше."""
    w, h = img.size
    if w >= target_w and h >= target_h:
        return img

    factor = max(target_w / w, target_h / h)
    new_w = max(int(math.ceil(w * factor)), target_w)
    new_h = max(int(math.ceil(h * factor)), target_h)

    print(f"[upscale] исходный размер {w}x{h}, upscale -> {new_w}x{new_h} (factor {factor:.2f})")
    return img.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)


def process_image_to_canvas(source_image_bytes):
    """Обработка исходного изображения (upscale -> fit -> sharpen)."""
    source = Image.open(io.BytesIO(source_image_bytes))
    # Корректно обрабатываем ориентацию по EXIF и любые цветовые режимы.
    source = ImageOps.exif_transpose(source).convert("RGB")

    source = upscale_if_needed(source, OUTPUT_WIDTH, OUTPUT_HEIGHT)

    canvas = ImageOps.fit(
        source,
        (OUTPUT_WIDTH, OUTPUT_HEIGHT),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    ).convert("RGB")

    # Повышаем локальную чёткость и слегка контраст — лучше смотрится на e-paper.
    final = canvas.filter(ImageFilter.UnsharpMask(radius=1.2, percent=130, threshold=3))
    try:
        final = ImageEnhance.Sharpness(final).enhance(1.08)
        final = ImageEnhance.Contrast(final).enhance(1.04)
        final = ImageEnhance.Color(final).enhance(1.03)
    except Exception:
        pass

    return final


def create_image(artwork, image_bytes):
    final_image = process_image_to_canvas(image_bytes)

    overlay = Image.new("RGBA", final_image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    title = clean_text(artwork.get("title") or "Untitled", 48)
    artist = clean_text(artwork.get("artist") or "Unknown artist", 42)
    year = clean_text(artwork.get("date"), 18)
    meta = f"{artist} · {year}" if year else artist

    title_font = get_font(17, bold=True)
    meta_font = get_font(13)

    padding_x = 10
    margin = 10
    box_height = 48

    title_box = draw.textbbox((0, 0), title, font=title_font)
    meta_box = draw.textbbox((0, 0), meta, font=meta_font)

    text_width = max(title_box[2] - title_box[0], meta_box[2] - meta_box[0])
    box_width = min(text_width + padding_x * 2, int(OUTPUT_WIDTH * 0.62))

    x1 = margin
    y1 = OUTPUT_HEIGHT - box_height - margin
    x2 = x1 + box_width
    y2 = y1 + box_height

    # Полупрозрачная плашка: не закрывает картину «наглухо», сквозь неё
    # просматривается изображение. Тёмный фон + белый текст с лёгкой тенью
    # остаются читаемыми даже поверх светлых участков картины.
    draw.rounded_rectangle((x1, y1, x2, y2), radius=6, fill=(0, 0, 0, 110))

    # Мягкая тень под текстом — гарантирует читаемость на любом фоне.
    shadow = (0, 0, 0, 160)
    draw.text((x1 + padding_x + 1, y1 + 6), title, font=title_font, fill=shadow)
    draw.text((x1 + padding_x + 1, y1 + 28), meta, font=meta_font, fill=shadow)

    draw.text((x1 + padding_x, y1 + 5), title, font=title_font, fill=(255, 255, 255, 255))
    draw.text((x1 + padding_x, y1 + 27), meta, font=meta_font, fill=(235, 235, 235, 255))

    composite = Image.alpha_composite(final_image.convert("RGBA"), overlay).convert("RGB")
    composite.save(OUTPUT_IMAGE, "JPEG", quality=95, optimize=True)

    return title, meta


def write_html(version):
    """Страница немедленно открывает готовый JPG (без исполнения JS)."""
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <!-- Страница сама перезагружается каждые 2 минуты, чтобы дисплей подхватывал свежую картину. -->
  <meta http-equiv="refresh" content="120">
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

    artwork, image_bytes = get_random_artwork_bytes()
    title, meta = create_image(artwork, image_bytes)

    version = int(time.time())
    write_html(version)
    Path(LAST_UPDATE_FILE).write_text(str(version), encoding="utf-8")

    print(f"\nСоздана новая картина: {title} — {meta} [{artwork['source']}]")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Ошибка генерации: {error}")
        sys.exit(1)
