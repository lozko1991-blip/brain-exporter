#!/usr/bin/env python3
"""
Brain API → XML Exporter
Формат: Rozetka YML (підходить також для Prom.ua, Hotline, Google Shopping)
Групування варіантів: по артикулу (розміри, кольори як окремі offers з однаковим group_id)

Запуск вручну:
  BRAIN_LOGIN=email BRAIN_PASSWORD=pass python export.py

Через GitHub Actions — всі параметри беруться з ENV та config.json
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from xml.dom.minidom import parseString
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx

# ══════════════════════════════════════════════════════════════════
#  КОНФІГУРАЦІЯ
# ══════════════════════════════════════════════════════════════════

API_BASE   = "http://api.brain.com.ua"
OUTPUT_DIR = Path("output")
CONFIG     = Path("config.json")

def load_config() -> dict:
    """Завантажує config.json, env змінні мають пріоритет."""
    cfg = {}
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))

    # ENV перекриває config.json
    if os.environ.get("BRAIN_LOGIN"):
        cfg["login"] = os.environ["BRAIN_LOGIN"]
    if os.environ.get("BRAIN_PASSWORD"):
        cfg["password"] = os.environ["BRAIN_PASSWORD"]
    if os.environ.get("CATEGORY_IDS"):
        raw = os.environ["CATEGORY_IDS"]
        cfg["category_ids"] = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    if os.environ.get("LANG"):
        cfg["lang"] = os.environ["LANG"]
    if os.environ.get("SHOP_NAME"):
        cfg["shop_name"] = os.environ["SHOP_NAME"]
    if os.environ.get("SHOP_URL"):
        cfg["shop_url"] = os.environ["SHOP_URL"]

    # Дефолти
    cfg.setdefault("lang",      "ua")
    cfg.setdefault("shop_name", "Мій магазин")
    cfg.setdefault("shop_url",  "https://example.com.ua")
    cfg.setdefault("category_ids", [])

    return cfg


# ══════════════════════════════════════════════════════════════════
#  АВТОРИЗАЦІЯ
# ══════════════════════════════════════════════════════════════════

async def auth(client: httpx.AsyncClient, login: str, password: str) -> str:
    """
    Авторизація в Brain API.
    ВАЖЛИВО: пароль передається як MD5 хеш, не у відкритому вигляді!
    """
    md5_pass = hashlib.md5(password.encode("utf-8")).hexdigest()
    log(f"🔐 Авторизація: {login}")

    resp = await client.post(
        f"{API_BASE}/auth",
        data={"login": login, "password": md5_pass},
        timeout=20,
    )
    data = resp.json()
    if data.get("status") != 1:
        raise Exception(f"Помилка авторизації Brain API: {data}")

    sid = data["result"]
    log(f"✅ Авторизовано, SID: {sid[:8]}...")
    return sid


async def logout(client: httpx.AsyncClient, sid: str):
    try:
        await client.get(f"{API_BASE}/logout/{sid}", timeout=10)
        log("👋 Сесію закрито")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
#  КАТЕГОРІЇ
# ══════════════════════════════════════════════════════════════════

async def fetch_categories(client: httpx.AsyncClient, sid: str, lang: str) -> list:
    log("📂 Завантаження категорій...")
    resp = await client.get(f"{API_BASE}/categories/{sid}?lang={lang}", timeout=20)
    cats = resp.json().get("result", [])
    log(f"   Знайдено: {len(cats)} категорій")
    return cats


def get_all_descendants(cats: list, parent_id: int) -> set:
    """Рекурсивно збирає ID категорії + всіх нащадків."""
    result = {parent_id}
    for c in cats:
        if c["parentID"] == parent_id:
            result |= get_all_descendants(cats, c["categoryID"])
    return result


# ══════════════════════════════════════════════════════════════════
#  ТОВАРИ — двофазне завантаження
# ══════════════════════════════════════════════════════════════════

async def fetch_products_page(
    client: httpx.AsyncClient, sid: str, cat_id: int, lang: str,
    offset: int, limit: int = 100
) -> dict:
    url = (
        f"{API_BASE}/products/{cat_id}/{sid}"
        f"?lang={lang}&limit={limit}&offset={offset}"
    )
    for attempt in range(3):
        try:
            resp = await client.get(url, timeout=30)
            data = resp.json()

            if data.get("status") == 1:
                result = data["result"]

                # Brain API повертає result як список товарів напряму:
                # {"status":1, "result": [{"productID":123,...}, ...]}
                # count береться з окремого поля або обчислюється
                if isinstance(result, list):
                    count = data.get("count", offset + len(result))
                    # Якщо повернулось рівно limit — можливо є ще сторінки
                    if len(result) == limit:
                        count = max(count, offset + limit + 1)
                    return {"list": result, "count": count}

                # Якщо раптом dict — шукаємо список
                elif isinstance(result, dict):
                    items = (result.get("list") or result.get("products")
                             or result.get("items") or [])
                    count = int(result.get("count") or result.get("total") or len(items))
                    return {"list": items, "count": count}

        except Exception as e:
            if attempt == 2:
                log(f"   ⚠️ Помилка категорія {cat_id} offset={offset}: {e}")
        await asyncio.sleep(1.5 ** attempt)
    return {"list": [], "count": 0}


async def fetch_options(client: httpx.AsyncClient, sid: str, pid: int, lang: str) -> list:
    """Характеристики товару (розмір, колір, матеріал тощо)."""
    try:
        r = await client.get(
            f"{API_BASE}/product_options/{pid}/{sid}?lang={lang}", timeout=15
        )
        d = r.json()
        if d.get("status") == 1:
            return d.get("result", [])
    except Exception:
        pass
    return []


async def fetch_pictures(client: httpx.AsyncClient, sid: str, pid: int) -> list:
    """Всі фотографії товару."""
    try:
        r = await client.get(f"{API_BASE}/product_pictures/{pid}/{sid}", timeout=15)
        d = r.json()
        if d.get("status") == 1:
            return d.get("result", {}).get("pictures", [])
    except Exception:
        pass
    return []


async def fetch_all_products(
    client: httpx.AsyncClient, sid: str,
    cat_ids: list, lang: str
) -> list:
    """
    Двофазне завантаження:
    Фаза 1 — базові дані всіх товарів (швидко, по 100 шт)
    Фаза 2 — характеристики + фото батчами (паралельно по 10)
    Захист від дублів: словник по productID
    """
    pool: dict[int, dict] = {}

    # ── Фаза 1: базові дані ─────────────────────────────────────
    log("\n📦 Фаза 1: базові дані товарів...")
    total_cats = len(cat_ids)

    for i, cat_id in enumerate(cat_ids, 1):
        offset = 0
        cat_total = 0

        while True:
            result   = await fetch_products_page(client, sid, cat_id, lang, offset)
            products = result.get("list", [])
            count    = result.get("count", 0)
            if products:
                cat_total += len(products)

            # DEBUG: показуємо сирі дані першого товару першої категорії
            if products and i == 1 and offset == 0:
                log(f"🔍 DEBUG сирі дані першого товару:")
                for k, v in products[0].items():
                    log(f"   {k} = {repr(v)[:100]}")

            for p in products:
                # Шукаємо ID товару в різних можливих полях
                pid = (p.get("productID") or p.get("product_id")
                       or p.get("ID") or p.get("id"))
                if pid:
                    pool[int(pid)] = p  # завжди додаємо, без фільтра is_archive

            fetched = offset + len(products)
            print(f"   [{i}/{total_cats}] Кат.{cat_id}: {fetched}", end="\r")

            # Зупиняємось якщо отримали менше ніж limit (остання сторінка)
            if len(products) < 100:
                break
            offset += 100
            await asyncio.sleep(0.4)  # не більше 3 запитів/сек

        log(f"   [{i}/{total_cats}] Категорія {cat_id}: {cat_total} товарів")

    product_list = list(pool.values())
    log(f"\n✅ Фаза 1: {len(product_list)} унікальних товарів")

    # DEBUG: показуємо поля першого товару щоб зрозуміти структуру API
    if product_list:
        sample = product_list[0]
        log(f"🔍 DEBUG — поля першого товару (productID={sample.get('productID')}):")
        for key, val in sample.items():
            if key not in ("options", "pictures"):
                log(f"   {key} = {repr(val)[:80]}")

    # ── Фаза 2: характеристики + фото ───────────────────────────
    log("📋 Фаза 2: характеристики та фото (паралельно по 10)...")
    total    = len(product_list)
    batch_sz = 10

    for start in range(0, total, batch_sz):
        batch = product_list[start:start + batch_sz]

        # Паралельний запит характеристик і фото для batch_sz товарів
        opts_tasks = [fetch_options(client, sid, p["productID"], lang) for p in batch]
        pics_tasks = [fetch_pictures(client, sid, p["productID"])      for p in batch]

        opts_results = await asyncio.gather(*opts_tasks)
        pics_results = await asyncio.gather(*pics_tasks)

        for p, opts, pics in zip(batch, opts_results, pics_results):
            p["options"]  = opts
            p["pictures"] = pics

        done = min(start + batch_sz, total)
        pct  = int(done / total * 100)
        bar  = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"   [{bar}] {pct}% ({done}/{total})", end="\r")

        # Пауза між батчами щоб не перегрівати API
        await asyncio.sleep(0.5)

    log(f"\n✅ Фаза 2 завершена")
    return product_list


# ══════════════════════════════════════════════════════════════════
#  XML ГЕНЕРАТОР — формат Rozetka YML
# ══════════════════════════════════════════════════════════════════

def safe(text) -> str:
    """Очищає текст від символів заборонених в XML."""
    if not text:
        return ""
    # Видаляємо non-printable символи (крім tab, newline, carriage return)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', str(text))
    return text.strip()


def get_param_value(options: list, names: list) -> str:
    """Шукає значення характеристики по можливих назвах."""
    names_lower = [n.lower() for n in names]
    for opt in options:
        opt_name = (opt.get("OptionName") or opt.get("name", "")).lower()
        if any(n in opt_name for n in names_lower):
            return opt.get("ValueName") or opt.get("value", "")
    return ""


def build_group_id(product: dict) -> str:
    """
    Групування варіантів товару для Rozetka/Prom.
    Товари з однаковим group_id відображаються як варіанти одного товару
    (наприклад, різні розміри одного кросівка).

    Логіка: артикул без розмірного суфіксу = group_id
    Якщо артикул відсутній — використовуємо назву без розміру.
    """
    articul = safe(str(product.get("articul") or product.get("product_code") or ""))

    if not articul:
        return ""

    # Видаляємо типові розмірні суфікси: -42, /XL, _M, (44) тощо
    group = re.sub(r'[-/_\s]*(XS|S|M|L|XL|XXL|XXXL|\d{2,3})$', '', articul, flags=re.I)
    group = re.sub(r'\(\d{2,3}\)$', '', group).strip()

    return group if group != articul else articul


def build_xml(
    products:   list,
    all_cats:   list,
    needed_ids: set,
    output:     Path,
    shop_name:  str,
    shop_url:   str,
    lang:       str,
) -> dict:
    """
    Генерує YML/XML файл формату Rozetka.
    Структура сумісна також з Prom.ua, Hotline, Google Shopping.

    Групування варіантів:
    - <param name="Розмір"> → значення розміру
    - <param name="Колір">  → значення кольору
    - group_id              → однакове для всіх варіантів одного товару
    """
    log("📝 Генерація XML (формат Rozetka YML)...")
    now  = datetime.now().strftime("%Y-%m-%d %H:%M")

    root = Element("yml_catalog")
    root.set("date", now)

    shop = SubElement(root, "shop")
    SubElement(shop, "name").text    = safe(shop_name)
    SubElement(shop, "company").text = safe(shop_name)
    SubElement(shop, "url").text     = safe(shop_url)

    # Валюти
    currencies = SubElement(shop, "currencies")
    uah = SubElement(currencies, "currency")
    uah.set("id", "UAH"); uah.set("rate", "1")

    # Категорії — потрібні + батьки для правильної ієрархії
    cat_map      = {c["categoryID"]: c for c in all_cats}
    full_needed  = set(needed_ids)

    # Додаємо всіх батьків
    for cid in list(needed_ids):
        pid = cat_map.get(cid, {}).get("parentID", 1)
        while pid and pid != 1 and pid in cat_map:
            full_needed.add(pid)
            pid = cat_map.get(pid, {}).get("parentID", 1)

    cats_el = SubElement(shop, "categories")
    for cat in all_cats:
        if cat["categoryID"] not in full_needed:
            continue
        el = SubElement(cats_el, "category")
        el.set("id", str(cat["categoryID"]))
        if cat.get("parentID", 1) != 1:
            el.set("parentId", str(cat["parentID"]))
        el.text = safe(cat["name"])

    # ── Товари (offers) ───────────────────────────────────────────
    offers_el = SubElement(shop, "offers")
    added = skipped = 0

    for p in products:
        pid = (p.get("productID") or p.get("product_id")
               or p.get("ID") or p.get("id"))
        if not pid:
            skipped += 1
            continue
        pid = int(pid)

        # Ціна — перебираємо всі можливі поля Brain API
        price = 0.0
        for price_field in ["price_uah", "price", "recommendable_price",
                            "retail_price_uah", "retail_price", "opt_price"]:
            val = p.get(price_field)
            if val:
                try:
                    price = float(str(val).replace(",", "."))
                    if price > 0:
                        break
                except Exception:
                    pass

        # Якщо ціни немає — ставимо 0 але НЕ пропускаємо товар
        # (деякі товари можуть бути без ціни але з наявністю)

        offer = SubElement(offers_el, "offer")
        offer.set("id",        str(pid))
        offer.set("available", "true")

        # Групування варіантів (розміри/кольори одного товару)
        group_id = build_group_id(p)
        if group_id:
            offer.set("group_id", group_id)

        # ── Основні поля ──────────────────────────────────────────

        # Назва (Rozetka вимагає name_ua для українського контенту)
        name    = safe(p.get("name_ua") or p.get("name", ""))
        name_ru = safe(p.get("name", ""))

        SubElement(offer, "name").text    = name if lang == "ua" else name_ru
        if name and name_ru and name != name_ru:
            SubElement(offer, "name_ua").text = name

        # Ціна
        SubElement(offer, "price").text      = str(round(price, 2))
        SubElement(offer, "currencyId").text = "UAH"

        # Стара ціна (рекомендована роздрібна)
        try:
            old = float(str(
                p.get("retail_price_uah") or p.get("recommendable_price") or 0
            ).replace(",", "."))
            if old > price:
                SubElement(offer, "price_old").text = str(round(old, 2))
        except Exception:
            pass

        # Категорія
        SubElement(offer, "categoryId").text = str(p.get("categoryID", ""))

        # ── Фотографії ───────────────────────────────────────────
        # Rozetka: перше фото = головне, решта — галерея
        pics = p.get("pictures", [])
        if pics:
            for pic in pics:
                url = (pic.get("large_image") or pic.get("full_image")
                       or pic.get("medium_image"))
                if url:
                    SubElement(offer, "picture").text = safe(url)
        else:
            # Fallback на поля з основного запиту
            for key in ["large_image", "full_image", "medium_image", "small_image"]:
                if p.get(key):
                    SubElement(offer, "picture").text = safe(p[key])
                    break

        # ── Виробник і артикул ────────────────────────────────────
        if p.get("vendor"):
            SubElement(offer, "vendor").text = safe(str(p["vendor"]))

        articul = p.get("articul") or p.get("product_code", "")
        if articul:
            SubElement(offer, "article").text = safe(str(articul))

        # ── Додаткові поля ────────────────────────────────────────
        if p.get("warranty"):
            SubElement(offer, "warranty").text = f"{p['warranty']} міс."

        if p.get("country"):
            SubElement(offer, "country_of_origin").text = safe(str(p["country"]))

        if p.get("weight"):
            try:
                SubElement(offer, "weight").text = str(round(float(p["weight"]), 3))
            except Exception:
                pass

        # Наявність (сума по всіх складах)
        avail = p.get("available", {})
        qty   = sum(avail.values()) if isinstance(avail, dict) else 0
        SubElement(offer, "stock_quantity").text = str(qty)

        # ── Опис ─────────────────────────────────────────────────
        desc    = safe(p.get("brief_description") or p.get("description_ua") or "")
        desc_ru = safe(p.get("description") or "")

        if desc:
            d = SubElement(offer, "description_ua")
            d.text = f"<![CDATA[{desc}]]>"
        if desc_ru and desc_ru != desc:
            d2 = SubElement(offer, "description")
            d2.text = f"<![CDATA[{desc_ru}]]>"

        # ── Характеристики (params) ───────────────────────────────
        # Brain API повертає: OptionName + ValueName
        # Rozetka формат: <param name="Назва">Значення</param>
        options = p.get("options", [])

        # Визначаємо розмір і колір окремо (для групування)
        size  = get_param_value(options, ["розмір", "size", "розм"])
        color = get_param_value(options, ["колір", "цвет", "color"])

        # Всі характеристики
        for opt in options:
            opt_name = safe(opt.get("OptionName") or opt.get("name_ua") or opt.get("name") or "")
            opt_val  = safe(opt.get("ValueName")  or opt.get("value_ua") or opt.get("value") or "")

            if not opt_name or not opt_val:
                continue

            param = SubElement(offer, "param")
            param.set("name", opt_name)
            param.text = opt_val

        # Новинка
        if p.get("is_new"):
            SubElement(offer, "is_new").text = "true"

        added += 1

    # ── Запис файлу ───────────────────────────────────────────────
    output.parent.mkdir(parents=True, exist_ok=True)

    raw_xml = tostring(root, encoding="unicode")
    try:
        pretty = parseString(
            f'<?xml version="1.0" encoding="UTF-8"?>{raw_xml}'
        ).toprettyxml(indent="  ", encoding=None)

        lines = pretty.splitlines()
        if lines[0].startswith("<?xml"):
            lines[0] = '<?xml version="1.0" encoding="UTF-8"?>'
        final = "\n".join(lines)
    except Exception:
        final = f'<?xml version="1.0" encoding="UTF-8"?>\n{raw_xml}'

    output.write_text(final, encoding="utf-8")

    size_mb = output.stat().st_size / 1024 / 1024
    return {
        "offers":     added,
        "skipped":    skipped,
        "categories": len(cats_el),
        "size_mb":    round(size_mb, 1),
    }


# ══════════════════════════════════════════════════════════════════
#  ЗБЕРЕЖЕННЯ КАТЕГОРІЙ ДЛЯ САЙТУ
# ══════════════════════════════════════════════════════════════════

def save_categories_json(all_cats: list):
    """
    Будує дерево категорій і зберігає в categories.json
    щоб сайт (index.html) міг показувати реальні категорії Brain.
    """
    # Будуємо дерево
    cat_map = {c["categoryID"]: {
        "categoryID": c["categoryID"],
        "parentID":   c["parentID"],
        "name":       c["name"],
        "children":   []
    } for c in all_cats}

    roots = []
    for c in all_cats:
        node = cat_map[c["categoryID"]]
        pid  = c.get("parentID", 1)
        if pid == 1 or pid not in cat_map:
            roots.append(node)
        else:
            cat_map[pid]["children"].append(node)

    output = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total":     len(all_cats),
        "categories": roots
    }

    path = Path("categories.json")
    path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    log(f"📂 categories.json збережено ({len(all_cats)} категорій)")


# ══════════════════════════════════════════════════════════════════
#  ЛОГУВАННЯ
# ══════════════════════════════════════════════════════════════════

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ══════════════════════════════════════════════════════════════════
#  ГОЛОВНА ФУНКЦІЯ
# ══════════════════════════════════════════════════════════════════

async def main():
    cfg = load_config()

    login    = cfg.get("login", "")
    password = cfg.get("password", "")
    lang     = cfg.get("lang", "ua")
    cat_ids  = cfg.get("category_ids", [])
    shop_name = cfg.get("shop_name", "Мій магазин")
    shop_url  = cfg.get("shop_url",  "https://example.com.ua")
    output    = Path(cfg.get("output_file", "output/catalog.xml"))

    if not login or not password:
        log("❌ Не задані BRAIN_LOGIN / BRAIN_PASSWORD")
        sys.exit(1)

    if not cat_ids:
        log("❌ Не вибрані категорії (category_ids порожній в config.json та ENV)")
        sys.exit(1)

    log("=" * 55)
    log(f"  Brain API → XML Exporter")
    log(f"  Мова: {lang.upper()} | Категорій: {len(cat_ids)}")
    log("=" * 55)

    async with httpx.AsyncClient(
        timeout=30,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    ) as client:
        sid = await auth(client, login, password)

        try:
            all_cats  = await fetch_categories(client, sid, lang)
            cat_map   = {c["categoryID"]: c for c in all_cats}

            # Зберігаємо всі категорії в categories.json для сайту
            save_categories_json(all_cats)

            # Перевіряємо що вибрані категорії існують
            valid_ids = [cid for cid in cat_ids if cid in cat_map]
            if not valid_ids:
                log(f"❌ Жодна з категорій {cat_ids} не знайдена в Brain API")
                sys.exit(1)

            # Розгортаємо до всіх нащадків
            needed = set()
            for cid in valid_ids:
                needed |= get_all_descendants(all_cats, cid)

            log(f"📋 Категорій для вигрузки (з нащадками): {len(needed)}")
            for cid in valid_ids:
                name = cat_map.get(cid, {}).get("name", "?")
                desc_count = len(get_all_descendants(all_cats, cid))
                log(f"   ✓ [{cid}] {name} ({desc_count} кат.)")

            # Завантаження товарів
            products = await fetch_all_products(client, sid, list(needed), lang)

            # Генерація XML
            stats = build_xml(
                products   = products,
                all_cats   = all_cats,
                needed_ids = needed,
                output     = output,
                shop_name  = shop_name,
                shop_url   = shop_url,
                lang       = lang,
            )

            log(f"\n{'=' * 55}")
            log(f"✅ Готово!")
            log(f"   Товарів у XML:  {stats['offers']}")
            log(f"   Пропущено:      {stats['skipped']} (архів/без ціни)")
            log(f"   Категорій:      {stats['categories']}")
            log(f"   Розмір файлу:   {stats['size_mb']} МБ")
            log(f"   Файл:           {output}")

            if stats["size_mb"] > 180:
                log("⚠️  Файл > 180 МБ — перевищує ліміт Prom.ua!")
            else:
                log(f"✅ Prom.ua ліміт OK ({stats['size_mb']} / 180 МБ)")

            log(f"   Постійне посилання на файл:")
            log(f"   https://raw.githubusercontent.com/ВАШ_АКАУНТ/ВАШ_РЕПО/main/output/catalog.xml")
            log(f"{'=' * 55}")

        finally:
            await logout(client, sid)


if __name__ == "__main__":
    asyncio.run(main())
