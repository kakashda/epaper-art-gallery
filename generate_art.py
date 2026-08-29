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

# Разрешение панели Seeed reTerminal E1002 — ровно 800x480 (7.3", Spectra 6).
OUTPUT_WIDTH = 800
OUTPUT_HEIGHT = 480

# «Безопасное поле» (safe area). На физическом e-paper пластиковая рамка (bezel)
# перекрывает крайние пиксели экрана, а превью SenseCraft HMI скругляет углы и
# тоже подрезает края. Из-за этого картинка «впритык» к краям выглядела как
# обрезанная/зумленная, а подпись у самого низа (margin=0) пряталась под рамкой.
# Поэтому и картину, и подпись держим не вплотную к краям, а внутри области,
# уменьшенной на этот процент с каждой стороны. 0.05 = 5% (≈40px по ширине,
# ≈24px по высоте) — рамка гарантированно ничего не срезает.
SAFE_INSET_FRAC = 0.05

# Плашка стоит в НИЖНЕМ ЛЕВОМ УГЛУ. Отступ слева маленький (почти к самому
# краю), а снизу держим ровно на границе зоны обрезки дисплея (~10% высоты),
# чтобы плашка была максимально в углу, но нижняя строка текста не срезалась.
CAPTION_LEFT_FRAC = 0.02    # ≈16px от левого края — плашка почти в углу
CAPTION_BOTTOM_FRAC = 0.10  # ≈48px от низа — сразу над зоной обрезки дисплея

# --- Главный файл под E1002 (SenseCraft HMI, ручная загрузка) ---------------
# E1002 — это полноцветный E Ink Spectra 6 (ACeP). Панель физически умеет
# показывать ТОЛЬКО 6 чистых цветов (без градаций серого). Поэтому мы сами
# готовим 6-цветное изображение с дизерингом и отдаём PNG (lossless), иначе
# JPEG размывает точки дизеринга в грязь.
OUTPUT_PNG = "current.png"

# Палитра E Ink Spectra 6: чёрный, белый, красный, жёлтый, зелёный, синий.
# Значения подобраны как «чистые» цвета пигментов панели.
SPECTRA6_PALETTE = [
    (0, 0, 0),        # чёрный
    (255, 255, 255),  # белый
    (200, 40, 40),    # красный
    (230, 200, 40),   # жёлтый
    (50, 130, 70),    # зелёный
    (45, 70, 150),    # синий
]

# JPEG-версии (для веб-превью / других экранов). Для самого E1002 не нужны,
# но пусть остаются, чтобы index.html и прочее не ломались.
OUTPUT_SIZES = [
    ("current.jpg", 800, 480),
    ("current_1600.jpg", 1600, 960),
    ("current_2560.jpg", 2560, 1536),
    ("current_3840.jpg", 3840, 2304),
]

OUTPUT_IMAGE = "current.jpg"
OUTPUT_HTML = "index.html"
LAST_UPDATE_FILE = "last_update.txt"
MUSEUM_HISTORY_FILE = "museum_history.txt"  # список последних показанных музеев
MUSEUM_HISTORY_SIZE = 8  # не повторять музей, если он был в последних N показах

ARTIST_HISTORY_FILE = "artist_history.txt"  # список последних показанных авторов
ARTIST_HISTORY_SIZE = 5  # не повторять автора, если он был в последних N показах

# Ключевые слова в НАЗВАНИИ, по которым работу считаем портретом. Wikidata
# отсекает портреты по жанру (P136), но SMK/V&A/Met/Cleveland жанр не отдают —
# поэтому дополнительно ловим портреты по названию на разных языках.
PORTRAIT_TITLE_WORDS = (
    "portrait", "portræt", "portrett", "portret", "porträt", "bildnis",
    "self-portrait", "selvportræt", "selbstbildnis", "selfportrait",
    "head of a", "bust of", "effigy of",
    "портрет", "автопортрет",
)

# Вероятность ПРОПУСТИТЬ портрет (0.0 = все портреты проходят, 1.0 = все блокируются).
# 0.75 = пропускаем 75% портретов, т.е. примерно 1 из 4 портретов попадёт в показ —
# портреты БУДУТ, но их в ~4 раза меньше чем сюжетных/исторических картин.
PORTRAIT_SKIP_PROBABILITY = 0.75

# Сетевые параметры
HTTP_TIMEOUT = 30
MAX_RETRIES = 3          # число повторов на один HTTP-запрос
RETRY_BACKOFF = 2.0      # базовая задержка между повторами (секунды)
MAX_CANDIDATES = 25      # сколько объектов пробуем у одного источника
EURO_QUERY_ATTEMPTS = 8  # сколько художников перебирает европейский источник, пока не найдёт картины
MIN_IMAGE_BYTES = 5000   # меньший размер почти наверняка означает страницу-ошибку
# ================================================

# Реалистичный браузерный User-Agent помогает обойти простые анти-бот проверки
# и корректно пройти content-negotiation на CDN музеев.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Wikimedia требует описательный User-Agent (иначе HTTP 429). См.
# https://meta.wikimedia.org/wiki/User-Agent_policy
WIKIMEDIA_UA = (
    "epaper-art-gallery/1.0 (https://github.com/kakashda/epaper-art-gallery; "
    "kakashda@users.noreply.github.com)"
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

    # Wikimedia (Commons) требует ОПИСАТЕЛЬНЫЙ User-Agent по своей политике —
    # обобщённый браузерный UA с облачных IP они throttl-ят (HTTP 429).
    if "wikimedia.org" in url or "wikipedia.org" in url:
        headers["User-Agent"] = WIKIMEDIA_UA

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
    # Мастера ДИНАМИЧНЫХ, повествовательных сцен: события, драма, движение,
    # история, войны, мифы, фантазия, ад/рай, видения и кошмары.
    "Peter Paul Rubens", "Eugene Delacroix", "Theodore Gericault",
    "Jacques-Louis David", "Caravaggio", "Tintoretto", "Paolo Veronese",
    "Hieronymus Bosch", "Pieter Bruegel the Elder", "William Blake",
    "Henry Fuseli", "Gustave Dore", "John Martin", "Francisco Goya",
    "Peter von Cornelius", "Benjamin West", "Jean-Leon Gerome",
    "Ilya Repin", "Vasily Surikov", "Karl Bryullov", "Rembrandt van Rijn",
]

# Wikidata QID знаменитых европейских мастеров. Через Wikidata мы получаем
# картины из МУЗЕЕВ ВСЕЙ ЕВРОПЫ (Прадо, Лувр, Уффици, Вена, Мюнхен, Ватикан,
# Португалия и т.д.) с указанием музея-хранителя и его страны.
ARTIST_QIDS = {
    "Q5582": "Vincent van Gogh", "Q296": "Claude Monet",
    "Q5598": "Rembrandt van Rijn", "Q41264": "Johannes Vermeer",
    "Q39931": "Pierre-Auguste Renoir", "Q46373": "Edgar Degas",
    "Q35548": "Paul Cezanne", "Q40599": "Edouard Manet",
    "Q134741": "Camille Pissarro", "Q37693": "Paul Gauguin",
    "Q34013": "Georges Seurat", "Q159758": "J. M. W. Turner",
    "Q33477": "Eugene Delacroix", "Q5432": "Francisco Goya",
    "Q297": "Diego Velazquez", "Q301": "El Greco", "Q47551": "Titian",
    "Q5597": "Raphael", "Q5669": "Sandro Botticelli",
    "Q43270": "Pieter Bruegel the Elder", "Q5599": "Peter Paul Rubens",
    "Q42207": "Caravaggio", "Q41554": "Nicolas Poussin",
    "Q127171": "Jean-Honore Fragonard", "Q148475": "Camille Corot",
    "Q34618": "Gustave Courbet", "Q148458": "Jean-Francois Millet",
    "Q150679": "Anthony van Dyck", "Q167654": "Frans Hals",
    "Q182664": "Canaletto", "Q82445": "Henri de Toulouse-Lautrec",
    "Q34661": "Gustav Klimt", "Q5580": "Albrecht Durer",
    "Q48319": "Hans Holbein the Younger", "Q203371": "Georges de La Tour",
    "Q207447": "Jean-Baptiste-Simeon Chardin", "Q762": "Leonardo da Vinci",
    "Q5592": "Michelangelo", "Q102272": "Jan van Eyck",
    "Q130531": "Hieronymus Bosch", "Q7814": "Giotto",
    "Q205863": "Jan Steen", "Q470551": "Nicolas de Stael",
    # Мистические/фантастические художники (для "адских сюжетов", видений, монстров)
    "Q41513": "William Blake",           # видения, Апокалипсис, мистика
    "Q154349": "Odilon Redon",          # символизм, монстры, фантазия
    "Q122382": "Henry Fuseli",          # "Кошмар", демоны
    "Q7751": "Giuseppe Arcimboldo",     # головы из фруктов/рыб, маньеризм
    "Q154338": "Matthias Grünewald",    # Изенгеймский алтарь, распятие с ужасами
    "Q6682": "Gustave Doré",            # иллюстратор "Ада" Данте (гравюры)
    # Мастера ДИНАМИЧНЫХ сцен: битвы, драма, движение, история, мифы, фантазия
    "Q184212": "Theodore Gericault",    # "Плот Медузы", катастрофы, движение
    "Q83155": "Jacques-Louis David",    # революция, античная драма, клятвы
    "Q9319": "Tintoretto",              # барочная динамика, вихревые композиции
    "Q9440": "Paolo Veronese",          # пиры, толпы, монументальные сцены
    "Q212499": "Jean-Leon Gerome",      # гладиаторы, восточные сцены, история
    "Q172911": "Ilya Repin",            # народные события, драма, движение
    "Q110228": "Vasily Surikov",        # русская история, казни, восстания
    "Q4768": "Karl Bryullov",           # "Последний день Помпеи", катастрофы
    "Q937096": "John Martin",           # апокалипсис, гибель городов, ад/рай
    "Q313498": "Benjamin West",         # исторические сражения, "Смерть Вольфа"
}

# Страны, музеи которых считаем «европейскими/евразийскими» (по названию из Wikidata).
EUROPEAN_COUNTRIES = {
    "Spain", "France", "Italy", "Germany", "Austria", "Netherlands",
    "Belgium", "United Kingdom", "Kingdom of England", "Great Britain",
    "Portugal", "Switzerland", "Denmark", "Sweden", "Norway", "Finland",
    "Iceland", "Ireland", "Vatican City", "Holy See", "Russia",
    "Russian Empire", "Poland", "Czech Republic", "Czechia", "Slovakia",
    "Hungary", "Greece", "Romania", "Bulgaria", "Croatia", "Slovenia",
    "Serbia", "Ukraine", "Estonia", "Latvia", "Lithuania", "Luxembourg",
    "Monaco", "Malta", "Turkey", "Georgia", "Armenia", "Kazakhstan",
    "Kingdom of the Netherlands", "Kingdom of Italy", "German Empire",
    "Dutch Republic", "Republic of Venice", "Papal States",
}

# Резервные тематические запросы (если по конкретному мастеру ничего не нашли) —
# тоже смещены в сторону европейской классической живописи.
FALLBACK_TERMS = [
    "old master painting", "baroque painting", "romanticism painting",
    # ДИНАМИКА: события, действие, драма, движение
    "history painting", "battle painting", "shipwreck painting",
    "mythology painting", "biblical scene painting",
    # Фантазия, видения, ад и рай, кошмары
    "apocalypse painting", "hell and heaven painting", "vision painting",
    "allegory painting", "the last judgment painting",
    # Сюжеты из книг, истории, войн, городов
    "Dante Inferno painting", "siege city painting", "revolution painting",
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
            "medium": _clean_meta(obj.get("medium")),
            "culture": _clean_meta(obj.get("culture")),
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
        "fields": "id,title,creators,creation_date,images,type,technique,culture",
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
            "medium": _clean_meta(record.get("technique")),
            "culture": _clean_meta(record.get("culture")),
            "source": "Cleveland Museum of Art",
        }


def smk_candidates():
    """Генератор кандидатов из SMK — Statens Museum for Kunst (Копенгаген, Дания).

    Национальная галерея Дании, коллекция в открытом доступе (public domain).
    Это РЕАЛЬНО европейский музей (в отличие от The Met / Cleveland в США).
    """
    search_url = "https://api.smk.dk/api/v1/art/search/"
    # SMK — датский музей, держит не каждого мастера. Перебираем несколько
    # художников, пока не найдём картины, чтобы европейский источник почти
    # всегда что-то отдавал (и соотношение 9:1 реально соблюдалось).
    queries = random.sample(FAMOUS_ARTISTS, min(EURO_QUERY_ATTEMPTS, len(FAMOUS_ARTISTS)))
    items = []
    for query in queries:
        params = {
            "keys": query,
            "filters": "[has_image:true],[public_domain:true],[object_names:maleri]",
            "fields": "titles,image_native,artist,production_date,techniques",
            "rows": 100,
            "offset": 0,
        }
        try:
            data = fetch_json(search_url, params, "smk-search")
        except Exception as error:
            print(f"[smk] Поиск по '{query}' не удался: {error}")
            continue
        items = data.get("items") or []
        if items:
            print(f"[smk] Запрос '{query}': найдено {len(items)} картин.")
            break
        print(f"[smk] Поиск по '{query}' (maleri) ничего не вернул.")

    if not items:
        return

    random.shuffle(items)

    for item in items[:MAX_CANDIDATES]:
        image_url = item.get("image_native")
        if not image_url:
            continue

        titles = item.get("titles") or []
        title = _clean_meta(titles[0].get("title")) if titles else ""

        artists = item.get("artist") or []
        artist = _clean_meta(artists[0]) if artists else "Unknown artist"

        date = ""
        prod = item.get("production_date") or []
        if prod:
            date = _clean_meta(prod[0].get("period"))

        techniques = item.get("techniques") or []
        medium = _clean_meta(techniques[0]) if techniques else ""

        yield {
            "image_url": image_url,
            "title": title or "Untitled",
            "artist": artist or "Unknown artist",
            "date": date,
            "medium": medium,
            "culture": "",
            "source": "SMK — National Gallery of Denmark",
        }


def vam_candidates():
    """Генератор кандидатов из Victoria and Albert Museum (Лондон, Великобритания).

    Крупнейший европейский музей искусства и дизайна. Фильтруем строго до
    масляной живописи (kw_object_type=Oil painting), чтобы не попадали
    репродукции и печатная графика.
    """
    search_url = "https://api.vam.ac.uk/v2/objects/search"
    # Перебираем нескольких мастеров, пока не найдём масляную живопись.
    queries = random.sample(FAMOUS_ARTISTS, min(EURO_QUERY_ATTEMPTS, len(FAMOUS_ARTISTS)))
    records = []
    for query in queries:
        params = {
            "q": query,
            "kw_object_type": "Oil painting",
            "images_exist": 1,
            "page_size": 100,
            "page": 1,
        }
        try:
            data = fetch_json(search_url, params, "vam-search")
        except Exception as error:
            print(f"[vam] Поиск по '{query}' не удался: {error}")
            continue
        records = data.get("records") or []
        if records:
            print(f"[vam] Запрос '{query}': найдено {len(records)} картин.")
            break
        print(f"[vam] Поиск по '{query}' (Oil painting) ничего не вернул.")

    if not records:
        return

    random.shuffle(records)

    for rec in records[:MAX_CANDIDATES]:
        images = rec.get("_images") or {}
        base = images.get("_iiif_image_base_url")
        if not base:
            continue
        # IIIF: вписываем в 2000px по большей стороне — хорошее качество для 4K-версии.
        image_url = base.rstrip("/") + "/full/!2000,2000/0/default.jpg"

        maker = rec.get("_primaryMaker") or {}
        artist = _clean_meta(maker.get("name")) or "Unknown artist"

        yield {
            "image_url": image_url,
            "title": _clean_meta(rec.get("_primaryTitle")) or "Untitled",
            "artist": artist,
            "date": _clean_meta(rec.get("_primaryDate")),
            "medium": "oil painting",
            "culture": _clean_meta(rec.get("_primaryPlace")),
            "source": "Victoria and Albert Museum",
        }


def wikidata_candidates():
    """Генератор кандидатов из Wikidata — картины И гравюры из МУЗЕЕВ ВСЕЙ ЕВРОПЫ.

    Берёт случайного знаменитого мастера и запрашивает его произведения (живопись,
    печатную графику, гравюры) вместе с музеем-хранителем (P195) и страной музея
    (P17). Оставляет только работы в европейских/евразийских музеях (Прадо, Лувр,
    Уффици, Вена, Мюнхен, Ватикан и т.д.). Изображения — с Wikimedia Commons
    в высоком разрешении.

    Типы произведений (P31):
      Q3305213 — живопись (paintings)
      Q11060274 — печатная графика (prints)
      Q11835431 — гравюры (engravings)

    Это позволяет показывать, например, иллюстрации Доре к "Аду" Данте,
    "Апокалипсис" Дюрера и другие мистические гравюры.
    """
    endpoint = "https://query.wikidata.org/sparql"
    qids = list(ARTIST_QIDS.keys())
    random.shuffle(qids)

    bindings = []
    chosen_qid = None
    for qid in qids[:EURO_QUERY_ATTEMPTS]:
        sparql = (
            "SELECT ?item ?itemLabel ?img ?inception ?collLabel ?countryLabel ?genreLabel WHERE { "
            "VALUES ?type { wd:Q3305213 wd:Q11060274 wd:Q11835431 } "
            "?item wdt:P31 ?type; wdt:P170 wd:%s; wdt:P18 ?img. "
            "OPTIONAL { ?item wdt:P195 ?coll. OPTIONAL { ?coll wdt:P17 ?country. } } "
            "OPTIONAL { ?item wdt:P571 ?inception. } "
            "OPTIONAL { ?item wdt:P136 ?genre. } "
            'SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } } '
            "LIMIT 120" % qid
        )
        try:
            data = fetch_json(endpoint, {"query": sparql, "format": "json"},
                              f"wikidata-{qid}")
        except Exception as error:
            print(f"[wikidata] Запрос по {qid} ({ARTIST_QIDS[qid]}) не удался: {error}")
            continue
        rows = data.get("results", {}).get("bindings", [])
        # оставляем только картины в европейских музеях
        euro_rows = [
            r for r in rows
            if r.get("countryLabel", {}).get("value", "") in EUROPEAN_COUNTRIES
        ]
        if euro_rows:
            bindings = euro_rows
            chosen_qid = qid
            print(f"[wikidata] {ARTIST_QIDS[qid]}: {len(euro_rows)} картин в музеях Европы "
                  f"(из {len(rows)} всего).")
            break
        print(f"[wikidata] {ARTIST_QIDS[qid]}: нет картин в европейских музеях "
              f"(всего {len(rows)}).")

    if not bindings:
        return

    random.shuffle(bindings)
    for row in bindings[:MAX_CANDIDATES]:
        img = row.get("img", {}).get("value")
        if not img:
            continue
        # Commons Special:FilePath — просим масштабированную версию (до 2600px),
        # чтобы не тянуть гигантские TIFF/оригиналы.
        sep = "&" if "?" in img else "?"
        image_url = f"{img}{sep}width=2600"

        museum = _clean_meta(row.get("collLabel", {}).get("value"))
        country = _clean_meta(row.get("countryLabel", {}).get("value"))
        source = museum or "European museum (Wikidata)"
        if museum and country:
            source = f"{museum}, {country}"

        yield {
            "image_url": image_url,
            "title": _clean_meta(row.get("itemLabel", {}).get("value")) or "Untitled",
            "artist": ARTIST_QIDS.get(chosen_qid, "Unknown artist"),
            "date": _clean_meta(row.get("inception", {}).get("value"))[:4],
            "medium": "oil painting",
            "culture": country,
            "source": source,
            "genre": _clean_meta(row.get("genreLabel", {}).get("value")),
        }


# Провайдеры разбиты по ГЕОГРАФИИ музея (а не по происхождению картины):
#   • Европа/Евразия: Wikidata (музеи ВСЕЙ Европы), SMK (Дания), V&A (Британия)
#   • США: The Met (Нью-Йорк), Cleveland (Огайо)
# Пользователь хочет соотношение ~9:1 в пользу европейских музеев/коллекций.
# Wikidata стоит первым: покрывает Прадо, Лувр, Уффици, Вену, Мюнхен, Ватикан и т.д.
EUROPEAN_PROVIDERS = [wikidata_candidates, smk_candidates, vam_candidates]
US_PROVIDERS = [met_candidates, cleveland_candidates]
EUROPEAN_SHARE = 0.9  # доля показов из европейских музеев

# Обратная совместимость: полный список источников.
PROVIDERS = EUROPEAN_PROVIDERS + US_PROVIDERS


def _museum_key(source):
    """Нормализует название музея для сравнения в истории.

    Для Wikidata `source` = «Музей, Страна» — берём часть до запятой, чтобы
    ключом был сам музей, а не связка музей+страна.
    """
    text = str(source or "").strip()
    text = text.split(",")[0].strip()          # «Prado, Spain» -> «Prado»
    text = re.sub(r"\s+", " ", text).lower()
    return text


def load_recent_museums():
    """Читает список недавно показанных музеев (ключи), старые — первыми."""
    try:
        lines = Path(MUSEUM_HISTORY_FILE).read_text(encoding="utf-8").splitlines()
        return [ln.strip() for ln in lines if ln.strip()]
    except Exception:
        return []


def save_recent_museum(source):
    """Добавляет музей в историю и подрезает её до MUSEUM_HISTORY_SIZE."""
    history = load_recent_museums()
    history.append(_museum_key(source))
    history = history[-MUSEUM_HISTORY_SIZE:]
    try:
        Path(MUSEUM_HISTORY_FILE).write_text("\n".join(history) + "\n", encoding="utf-8")
    except Exception as error:
        print(f"[history] не удалось сохранить историю музеев: {error}")


def _artist_key(artist):
    """Нормализует имя автора для сравнения в истории."""
    return re.sub(r"\s+", " ", str(artist or "").strip()).lower()


def load_recent_artists():
    """Читает список недавно показанных авторов (ключи), старые — первыми."""
    try:
        lines = Path(ARTIST_HISTORY_FILE).read_text(encoding="utf-8").splitlines()
        return [ln.strip() for ln in lines if ln.strip()]
    except Exception:
        return []


def save_recent_artist(artist):
    """Добавляет автора в историю и подрезает её до ARTIST_HISTORY_SIZE."""
    history = load_recent_artists()
    history.append(_artist_key(artist))
    history = history[-ARTIST_HISTORY_SIZE:]
    try:
        Path(ARTIST_HISTORY_FILE).write_text("\n".join(history) + "\n", encoding="utf-8")
    except Exception as error:
        print(f"[history] не удалось сохранить историю авторов: {error}")


def _looks_like_portrait(artwork):
    """True, если работа выглядит как портрет — по жанру (Wikidata) или названию."""
    genre = str(artwork.get("genre") or "").lower()
    if "portrait" in genre:   # 'portrait', 'self-portrait' из Wikidata P136
        return True
    title = str(artwork.get("title") or "").lower()
    return any(word in title for word in PORTRAIT_TITLE_WORDS)


def get_random_artwork_bytes():
    """Пробует источники по очереди и возвращает (artwork_dict, image_bytes).

    Справедливое распределение: музей, показанный в последних
    MUSEUM_HISTORY_SIZE картинах, пропускается — так один и тот же музей
    (например, SMK или Met) не идёт подряд/через один, и картины равномерно
    чередуются между разными музеями Европы и США.

    Устойчивость: если ВСЕ доступные кандидаты оказались из недавних музеев
    (например, временно работает только один источник), второй проход
    игнорирует историю, чтобы экран никогда не остался пустым.
    """
    euro = EUROPEAN_PROVIDERS[:]
    us = US_PROVIDERS[:]
    random.shuffle(euro)
    random.shuffle(us)

    if random.random() < EUROPEAN_SHARE:
        providers = euro + us
        print(f"[выбор] Приоритет: европейские музеи (доля {EUROPEAN_SHARE:.0%})")
    else:
        providers = us + euro
        print(f"[выбор] Приоритет: музеи США (доля {1 - EUROPEAN_SHARE:.0%})")

    recent = set(load_recent_museums())
    recent_artists = set(load_recent_artists())
    print(f"[выбор] Недавние музеи (пропускаются): {sorted(recent) or '—'}")
    print(f"[выбор] Недавние авторы (пропускаются): {sorted(recent_artists) or '—'}")

    last_error = None
    tried = 0

    # Проход 1 — строгий: свежий музей, свежий автор, БЕЗ портретов.
    # Проход 2 — резерв: игнорируем историю и фильтр портретов, лишь бы экран
    # не остался пустым (например, когда временно работает один источник).
    for enforce_fairness in (True, False):
        if not enforce_fairness:
            print("\n[выбор] Строгий проход не дал результата — резервный проход "
                  "без фильтров истории/портретов.")
        for provider in providers:
            print(f"\n=== Источник: {provider.__name__} (fairness={enforce_fairness}) ===")
            for artwork in provider():
                if enforce_fairness:
                    if _museum_key(artwork["source"]) in recent:
                        print(f"[fair-skip] {artwork['source']} — музей недавно показан")
                        continue
                    if _artist_key(artwork.get("artist")) in recent_artists:
                        print(f"[fair-skip] {artwork.get('artist')} — автор недавно показан")
                        continue
                    if (_looks_like_portrait(artwork)
                            and random.random() < PORTRAIT_SKIP_PROBABILITY):
                        print(f"[fair-skip] «{artwork.get('title')}» — портрет "
                              f"(пропуск с вероятностью {PORTRAIT_SKIP_PROBABILITY:.0%})")
                        continue
                tried += 1
                try:
                    image_bytes = download_image_bytes(
                        artwork["image_url"], label=f"download ({artwork['source']})"
                    )
                    print(f"[ok] Изображение получено из {artwork['source']}: "
                          f"{artwork['title']} ({len(image_bytes)} байт)")
                    save_recent_museum(artwork["source"])
                    save_recent_artist(artwork.get("artist"))
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


def process_image_to_canvas(source, target_w, target_h, enhance=True, align_top=False):
    """Готовит кадр заданного разрешения БЕЗ обрезки (contain + поля).

    Картина вписывается ЦЕЛИКОМ: масштабируется по меньшему коэффициенту (без
    искажения пропорций), а свободное место заполняется чёрными полями. Так
    ничего не срезается и геометрия оригинала сохраняется.

    `enhance` — если True (по умолчанию, для веб-JPEG), слегка повышаем
    чёткость/контраст/цвет. Если False (для PNG под e-paper), НЕ трогаем
    изображение вообще — только меняем размер, как просил пользователь.

    `align_top` — если True (для PNG), картина прижимается к ВЕРХУ кадра,
    чёрная полоса остаётся ТОЛЬКО внизу. Если False (по умолчанию, для JPG),
    картина центрируется по вертикали (letterbox сверху и снизу).

    `source` — уже открытое и приведённое к RGB изображение (PIL.Image).
    """
    src = source.convert("RGB")
    w, h = src.size

    # Картина заполняет ВЕСЬ кадр (contain, без дополнительных чёрных полей):
    # никакого лишнего отступа сверху/снизу — только естественный letterbox/
    # pillarbox там, где пропорции картины не совпадают с 800x480. Подпись при
    # этом всё равно держим выше зоны обрезки дисплея (см. draw_caption).
    factor = min(target_w / w, target_h / h)
    new_w = max(1, int(round(w * factor)))
    new_h = max(1, int(round(h * factor)))
    resized = src.resize((new_w, new_h), resample=Image.Resampling.LANCZOS)

    # Чёрный холст целевого размера. По горизонтали всегда центрируем (pillarbox).
    # По вертикали: если align_top=True (PNG), прижимаем к верху (letterbox только
    # снизу); если False (JPG), центрируем (letterbox сверху и снизу).
    canvas = Image.new("RGB", (target_w, target_h), (0, 0, 0))
    off_x = (target_w - new_w) // 2
    off_y = 0 if align_top else (target_h - new_h) // 2
    canvas.paste(resized, (off_x, off_y))

    if not enhance:
        # PNG под e-paper: НИКАКОЙ обработки — только изменённый размер.
        return canvas

    # Повышаем локальную чёткость и слегка контраст — лучше смотрится на e-paper.
    final = canvas.filter(ImageFilter.UnsharpMask(radius=1.2, percent=130, threshold=3))
    try:
        final = ImageEnhance.Sharpness(final).enhance(1.08)
        final = ImageEnhance.Contrast(final).enhance(1.04)
        final = ImageEnhance.Color(final).enhance(1.03)
    except Exception:
        pass

    return final


def build_caption_lines(artwork):
    """Собирает строки подписи: название, автор·год, техника·музей.

    Возвращает список кортежей (kind, text), где kind — 'title'|'meta'|'desc'.
    """
    title = clean_text(artwork.get("title") or "Untitled", 72)
    artist = clean_text(artwork.get("artist") or "Unknown artist", 56)
    year = clean_text(artwork.get("date"), 24)
    meta = f"{artist} · {year}" if year else artist

    # Третья строка — только МУЗЕЙ (где картина хранится сейчас).
    # Технику/материал ("oil painting", "tempera" и т.п.) НЕ показываем —
    # по просьбе пользователя оставляем название, автора, год и место хранения.
    source = clean_text(artwork.get("source"), 48)

    desc = source

    lines = [("title", title), ("meta", meta)]
    if desc:
        lines.append(("desc", desc))
    return lines


def draw_caption(image, lines, scale, flush_bottom_left=False):
    """Рисует полупрозрачную плашку с подписью, масштабируя всё под разрешение.

    `scale` = высота_кадра / базовая_высота (480). Шрифты, отступы и радиус
    скругления умножаются на scale, чтобы плашка выглядела одинаково на всех
    форматах — от 800x480 до 4K.

    `flush_bottom_left` — если True (для JPG), плашка прижата в самый нижний
    левый угол БЕЗ отступов. Если False (для PNG под e-paper E1002), плашка
    приподнята над зоной обрезки дисплея (CAPTION_BOTTOM_FRAC) и чуть отступает
    слева (CAPTION_LEFT_FRAC).
    """
    image = image.convert("RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    W, H = image.size

    if flush_bottom_left:
        # JPG: плашка в самом низу, вплотную к левому-нижнему углу — без отступов.
        margin_x = 0
        margin_y = 0
    else:
        # PNG (e-paper E1002): слева чуть отступаем (CAPTION_LEFT_FRAC), снизу
        # держим над зоной обрезки дисплея (CAPTION_BOTTOM_FRAC), чтобы нижняя
        # строка текста не срезалась физическим краем панели.
        margin_x = max(int(round(CAPTION_LEFT_FRAC * W)), 6)
        margin_y = max(int(round(CAPTION_BOTTOM_FRAC * H)), 12)
    pad_x = max(int(round(12 * scale)), 8)
    pad_y = max(int(round(9 * scale)), 6)
    gap = max(int(round(5 * scale)), 3)
    radius = max(int(round(6 * scale)), 4)
    shadow_off = max(int(round(scale)), 1)

    fonts = {
        "title": get_font(max(int(round(20 * scale)), 12), bold=True),
        "meta": get_font(max(int(round(15 * scale)), 10)),
        "desc": get_font(max(int(round(13 * scale)), 9)),
    }

    # Замеряем каждую строку.
    measured = []
    max_text_w = 0
    total_h = 0
    for kind, text in lines:
        font = fonts[kind]
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        measured.append((kind, text, font, text_w, text_h, bbox[1]))
        max_text_w = max(max_text_w, text_w)
        total_h += text_h
    total_h += gap * (len(measured) - 1)

    box_w = min(max_text_w + pad_x * 2, int(W * 0.72))
    box_h = total_h + pad_y * 2
    x1 = margin_x
    y1 = H - box_h - margin_y
    x2 = x1 + box_w
    y2 = y1 + box_h

    # Полупрозрачная чёрная плашка под текстом — в 2 раза прозрачнее прежней
    # (alpha 200 -> 100): картина хорошо просматривается сквозь неё. Читаемость
    # текста держит ЧЁРНАЯ ОБВОДКА вокруг белых букв.
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=(0, 0, 0, 100))

    stroke = max(int(round(2 * scale)), 2)  # толщина чёрной обводки букв
    cursor_y = y1 + pad_y
    for kind, text, font, _tw, text_h, top in measured:
        text_x = x1 + pad_x
        text_y = cursor_y - top  # выравниваем реальный верх глифов по cursor_y
        fill = (255, 255, 255, 255) if kind == "title" else (238, 238, 238, 255)
        draw.text(
            (text_x, text_y), text, font=font, fill=fill,
            stroke_width=stroke, stroke_fill=(0, 0, 0, 255),
        )
        cursor_y += text_h + gap

    return Image.alpha_composite(image, overlay).convert("RGB")


def _build_palette_image():
    """Создаёт PIL-палитру (P-mode) из SPECTRA6_PALETTE для quantize()."""
    pal_img = Image.new("P", (1, 1))
    flat = []
    for r, g, b in SPECTRA6_PALETTE:
        flat.extend([r, g, b])
    # PIL требует 256 цветов в палитре — добиваем последним цветом.
    flat.extend(flat[-3:] * (256 - len(SPECTRA6_PALETTE)))
    pal_img.putpalette(flat)
    return pal_img


def _apply_gamma(img, gamma):
    """Гамма-коррекция через LUT. gamma>1 — притемняет средние тона."""
    if gamma == 1.0:
        return img
    lut = [min(255, int((i / 255.0) ** gamma * 255 + 0.5)) for i in range(256)]
    return img.point(lut * 3)


def _crush_tones(img, black_thr, white_thr):
    """«Давление» крайних тонов в ЧИСТЫЙ чёрный/белый (только PIL, без numpy).

    Самый заметный источник «снега» — тёмные, но не чёрные зоны (тёмно-бордовый
    фон, тени платья): панель Spectra 6 не умеет тёмные оттенки, поэтому дизеринг
    собирает их из чёрного + красных/зелёных точек. Если всё, что темнее порога,
    принудительно сделать чистым чёрным (а очень светлое — чистым белым), эти
    точки исчезают, и картинка выглядит гораздо чище.
    """
    lum = img.convert("L")
    black_mask = lum.point(lambda p: 255 if p < black_thr else 0)
    white_mask = lum.point(lambda p: 255 if p > white_thr else 0)
    black_img = Image.new("RGB", img.size, (0, 0, 0))
    white_img = Image.new("RGB", img.size, (255, 255, 255))
    img = Image.composite(black_img, img, black_mask)
    img = Image.composite(white_img, img, white_mask)
    return img


def _warm_shift(img, rmul=1.12, gmul=0.92, bmul=0.95):
    """Тёплый сдвиг каналов ПЕРЕД квантованием к 6 цветам.

    У Spectra 6 нет коричневого/бежевого/тёплого-серого. Тёплые песчаные стены
    (типичны для венецианской/европейской архитектуры) при nearest-color попадали
    в ЗЕЛЁНЫЙ и картинка выглядела болотной. Лёгкий подъём красного и приглушение
    зелёного/синего смещают такие тона в ЖЁЛТЫЙ — стены выглядят как тёплый камень,
    а не как трава. Значения подобраны мягкими, чтобы небо/вода не краснели.
    """
    r, g, b = img.split()
    r = r.point(lambda p: min(255, int(p * rmul + 0.5)))
    g = g.point(lambda p: min(255, int(p * gmul + 0.5)))
    b = b.point(lambda p: min(255, int(p * bmul + 0.5)))
    return Image.merge("RGB", (r, g, b))


def _quantize_to_palette(img):
    """Снап к 6 цветам панели БЕЗ дизеринга (nearest color), без обработки тонов."""
    palette_img = _build_palette_image()
    quantized = img.convert("RGB").quantize(
        palette=palette_img,
        dither=Image.Dither.NONE,
    )
    return quantized.convert("RGB")


def to_spectra6(rgb_image):
    """Готовит изображение под панель Spectra 6: чистит шум, усиливает цвет,
    квантует к 6 цветам БЕЗ дизеринга.

    БЕЗ дизеринга текст и границы остаются чёткими. Главное новшество —
    MedianFilter ПЕРЕД квантованием: он убирает мелкий шум текстуры холста в
    средних тонах (небо, кожа, стены), из-за которого после снапа к 6 цветам
    появлялась «крупа» из бело-жёлто-зелёных точек.

    Возвращает RGB-изображение только из 6 цветов панели (nearest color)."""
    img = rgb_image.convert("RGB")

    try:
        img = ImageOps.autocontrast(img, cutoff=1)
        # E-paper — ОТРАЖАЮЩАЯ панель без подсветки: тёмные полотна на ней «тонут»
        # и выглядят грязно. Поэтому НЕ притемняем, а слегка ПОДНИМАЕМ средние
        # тона (gamma < 1) и яркость — детали в тенях становятся видны.
        img = _apply_gamma(img, 0.90)                    # поднимаем тени/средние
        img = ImageEnhance.Brightness(img).enhance(1.04)  # чуть светлее
        img = ImageEnhance.Color(img).enhance(1.55)      # насыщенность
        img = ImageEnhance.Contrast(img).enhance(1.12)   # мягкий контраст
        # Тёплый сдвиг: песчаные стены -> жёлтый, а не зелёный (см. _warm_shift).
        img = _warm_shift(img, rmul=1.12, gmul=0.92, bmul=0.95)
        # ГЛАВНОЕ против «крупы»: медианный фильтр убирает мелкий шум текстуры
        # холста в средних тонах (небо, кожа, стены). Именно этот шум после
        # квантования к 6 цветам «прыгал» бело-жёлто-зелёными точками. Подпись
        # рисуется ПОЗЖЕ, поэтому сглаживание её не касается — текст остаётся чётким.
        img = img.filter(ImageFilter.MedianFilter(size=3))
        img = ImageEnhance.Sharpness(img).enhance(1.4)   # вернуть чёткость краёв
        # Только совсем чёрное -> чёрное, совсем белое -> белое.
        img = _crush_tones(img, black_thr=12, white_thr=248)
    except Exception:
        pass

    return _quantize_to_palette(img)


def create_image(artwork, image_bytes):
    """Генерирует:
      - current.png — ГЛАВНЫЙ файл под E1002 (6 цветов Spectra 6, дизеринг, PNG);
      - current*.jpg — веб-превью/прочие экраны (обычный полноцветный JPEG).
    """
    source = Image.open(io.BytesIO(image_bytes))
    # Корректно обрабатываем ориентацию по EXIF и любые цветовые режимы.
    source = ImageOps.exif_transpose(source).convert("RGB")

    lines = build_caption_lines(artwork)

    # --- Главный файл под E1002 (PNG): ТОЛЬКО изменение размера + подпись ---
    # По просьбе пользователя изображение НЕ обрабатываем вообще: никакой
    # цветокоррекции, резкости, гаммы и НИКАКОГО снапа к 6 цветам. Только
    # вписываем в 800x480 (enhance=False) и рисуем сверху прозрачную плашку.
    # Преобразование цвета под палитру панели делает сама прошивка дисплея.
    base = process_image_to_canvas(source, OUTPUT_WIDTH, OUTPUT_HEIGHT, 
                                    enhance=False, align_top=True)
    png_image = draw_caption(base, lines, scale=1.0)   # прозрачная плашка сверху
    png_image.save(OUTPUT_PNG, "PNG", optimize=True)
    print(f"[save] {OUTPUT_PNG}: {OUTPUT_WIDTH}x{OUTPUT_HEIGHT} (только resize, без обработки)")

    # --- JPEG-версии (полноцветные, для веба и других дисплеев) ---
    for filename, width, height in OUTPUT_SIZES:
        canvas = process_image_to_canvas(source, width, height)
        composite = draw_caption(canvas, lines, scale=height / OUTPUT_HEIGHT,
                                 flush_bottom_left=True)
        quality = 95 if filename == OUTPUT_IMAGE else 90
        composite.save(filename, "JPEG", quality=quality, optimize=True)
        print(f"[save] {filename}: {width}x{height} (q{quality})")

    # Для лога/возврата — читабельные строки.
    title = lines[0][1]
    meta = lines[1][1] if len(lines) > 1 else ""
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
