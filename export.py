#!/usr/bin/env python3
"""
Brain API → XML Exporter v3.0
Два режими:
  full  — повна вигрузка: товари + характеристики + фото (~40 хв)
  quick — тільки ціни і наявність (~3 хв), використовує кеш

Запуск:
  EXPORT_MODE=full  python export.py
  EXPORT_MODE=quick python export.py
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
API_BASE     = "http://api.brain.com.ua"
OUTPUT_DIR   = Path("output")
CACHE_FILE   = Path("products_cache.json")
CATS_FILE    = Path("categories.json")
OUTPUT_DIR.mkdir(exist_ok=True)


# ══════════════════════════════════════════════════════════════════
#  КОНФІГУРАЦІЯ
# ══════════════════════════════════════════════════════════════════

def load_config() -> dict:
    cfg = {}
    if Path("config.json").exists():
        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))

    if os.environ.get("BRAIN_LOGIN"):    cfg["login"]    = os.environ["BRAIN_LOGIN"]
    if os.environ.get("BRAIN_PASSWORD"): cfg["password"] = os.environ["BRAIN_PASSWORD"]
    if os.environ.get("LANG"):           cfg["lang"]     = os.environ["LANG"]
    if os.environ.get("SHOP_NAME"):      cfg["shop_name"] = os.environ["SHOP_NAME"]
    if os.environ.get("SHOP_URL"):       cfg["shop_url"]  = os.environ["SHOP_URL"]
    if os.environ.get("EXPORT_MODE"):    cfg["mode"]      = os.environ["EXPORT_MODE"]

    raw_ids = os.environ.get("CATEGORY_IDS", "").strip()
    if raw_ids:
        cfg["category_ids"] = [int(x) for x in raw_ids.split(",") if x.strip().isdigit()]

    cfg.setdefault("lang",         "ua")
    cfg.setdefault("shop_name",    "Мій магазин")
    cfg.setdefault("shop_url",     "https://example.com.ua")
    cfg.setdefault("output_file",  "output/catalog.xml")
    cfg.setdefault("category_ids", [])
    cfg.setdefault("mode",         "quick")
    return cfg


# ══════════════════════════════════════════════════════════════════
#  АВТОРИЗАЦІЯ
# ══════════════════════════════════════════════════════════════════

async def auth(client: httpx.AsyncClient, login: str, password: str) -> str:
    """Пароль передається як MD5 хеш — не у відкритому вигляді!"""
    md5_pass = hashlib.md5(password.encode("utf-8")).hexdigest()
    log(f"🔐 Авторизація: {login}")
    resp = await client.post(
        f"{API_BASE}/auth",
        data={"login": login, "password": md5_pass},
        timeout=20,
    )
    data = resp.json()
    if data.get("status") != 1:
        raise Exception(f"❌ Помилка авторизації: {data}")
    log(f"✅ Авторизовано, SID: {data['result'][:8]}...")
    return data["result"]


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
    result = {parent_id}
    for c in cats:
        if c["parentID"] == parent_id:
            result |= get_all_descendants(cats, c["categoryID"])
    return result


def save_categories_json(all_cats: list):
    """Зберігає дерево категорій для сайту (index.html)."""
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

    CATS_FILE.write_text(
        json.dumps({
            "generated":  datetime.now().strftime("%Y-%m-%d %H:%M"),
            "total":      len(all_cats),
            "categories": roots,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    log(f"📂 categories.json збережено ({len(all_cats)} категорій)")


# ══════════════════════════════════════════════════════════════════
#  ЗАВАНТАЖЕННЯ ТОВАРІВ
# ══════════════════════════════════════════════════════════════════

async def fetch_products_page(
    client: httpx.AsyncClient, sid: str, cat_id: int,
    lang: str, offset: int, limit: int = 100
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
                if isinstance(result, list):
                    return {"list": result, "count": offset + len(result) + (1 if len(result) == limit else 0)}
                elif isinstance(result, dict):
                    items = result.get("list") or result.get("products") or []
                    count = int(result.get("count") or result.get("total") or len(items))
                    return {"list": items, "count": count}
        except Exception as e:
            if attempt == 2:
                log(f"   ⚠️ Помилка кат.{cat_id} offset={offset}: {e}")
        await asyncio.sleep(1.5 ** attempt)
    return {"list": [], "count": 0}


async def fetch_options(client: httpx.AsyncClient, sid: str, pid: int, lang: str) -> list:
    try:
        r = await client.get(f"{API_BASE}/product_options/{pid}/{sid}?lang={lang}", timeout=15)
        d = r.json()
        if d.get("status") == 1:
            return d.get("result", [])
    except Exception:
        pass
    return []


async def fetch_pictures(client: httpx.AsyncClient, sid: str, pid: int) -> list:
    try:
        r = await client.get(f"{API_BASE}/product_pictures/{pid}/{sid}", timeout=15)
        d = r.json()
        if d.get("status") == 1:
            return d.get("result", {}).get("pictures", [])
    except Exception:
        pass
    return []


async def fetch_all_products_full(
    client: httpx.AsyncClient, sid: str, cat_ids: list, lang: str
) -> list:
    """
    FULL режим: завантажує всі товари + характеристики + фото.
    Зберігає кеш в products_cache.json для quick режиму.
    """
    pool: dict[int, dict] = {}

    # Фаза 1: базові дані
    log("\n📦 Фаза 1: базові дані товарів...")
    for i, cat_id in enumerate(cat_ids, 1):
        offset = 0
        cat_count = 0
        while True:
            result   = await fetch_products_page(client, sid, cat_id, lang, offset)
            products = result.get("list", [])
            if not products:
                break
            for p in products:
                pid = (p.get("productID") or p.get("product_id")
                       or p.get("ID") or p.get("id"))
                if pid:
                    pool[int(pid)] = p
                    cat_count += 1
            print(f"   [{i}/{len(cat_ids)}] Кат.{cat_id}: {offset + len(products)}", end="\r")
            if len(products) < 100:
                break
            offset += 100
            await asyncio.sleep(0.4)
        log(f"   [{i}/{len(cat_ids)}] Категорія {cat_id}: {cat_count} товарів")

    product_list = list(pool.values())
    log(f"\n✅ Фаза 1: {len(product_list)} унікальних товарів")

    # Фаза 2: характеристики + фото батчами
    log("📋 Фаза 2: характеристики та фото...")
    total    = len(product_list)
    batch_sz = 10

    for start in range(0, total, batch_sz):
        batch        = product_list[start:start + batch_sz]
        opts_results = await asyncio.gather(*[
            fetch_options(client, sid, p.get("productID") or p.get("id"), lang)
            for p in batch
        ])
        pics_results = await asyncio.gather(*[
            fetch_pictures(client, sid, p.get("productID") or p.get("id"))
            for p in batch
        ])
        for p, opts, pics in zip(batch, opts_results, pics_results):
            p["options"]  = opts
            p["pictures"] = pics

        done = min(start + batch_sz, total)
        pct  = int(done / total * 100)
        bar  = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"   [{bar}] {pct}% ({done}/{total})", end="\r")
        await asyncio.sleep(0.4)

    log(f"\n✅ Фаза 2 завершена")

    # Зберігаємо кеш для quick режиму
    save_cache(product_list)

    return product_list


async def fetch_prices_only(
    client: httpx.AsyncClient, sid: str, cat_ids: list, lang: str
) -> dict:
    """
    QUICK режим: завантажує тільки ціни і наявність.
    Повертає словник: productID → {price, price_uah, retail_price_uah, available}
    """
    log("\n⚡ Quick режим: завантаження цін і наявності...")
    prices = {}

    for i, cat_id in enumerate(cat_ids, 1):
        offset = 0
        while True:
            result   = await fetch_products_page(client, sid, cat_id, lang, offset)
            products = result.get("list", [])
            if not products:
                break
            for p in products:
                pid = (p.get("productID") or p.get("product_id")
                       or p.get("ID") or p.get("id"))
                if pid:
                    prices[int(pid)] = {
                        "price":             p.get("price", 0),
                        "price_uah":         p.get("price_uah", 0),
                        "retail_price_uah":  p.get("retail_price_uah", 0),
                        "recommendable_price": p.get("recommendable_price", 0),
                        "available":         p.get("available", {}),
                        "is_archive":        p.get("is_archive", 0),
                    }
            print(f"   [{i}/{len(cat_ids)}] Кат.{cat_id}: {offset + len(products)}", end="\r")
            if len(products) < 100:
                break
            offset += 100
            await asyncio.sleep(0.4)
        log(f"   [{i}/{len(cat_ids)}] Категорія {cat_id}: {len([p for p in prices])} товарів")

    log(f"✅ Ціни отримані: {len(prices)} товарів")
    return prices


# ══════════════════════════════════════════════════════════════════
#  КЕШ
# ══════════════════════════════════════════════════════════════════

def save_cache(products: list):
    """Зберігає повні дані товарів для quick режиму."""
    cache = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "count":     len(products),
        "products":  products,
    }
    CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False),
        encoding="utf-8"
    )
    size_mb = CACHE_FILE.stat().st_size / 1024 / 1024
    log(f"💾 Кеш збережено: {len(products)} товарів ({size_mb:.1f} МБ)")


def load_cache() -> list:
    """Завантажує кеш товарів."""
    if not CACHE_FILE.exists():
        return []
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        products = data.get("products", [])
        generated = data.get("generated", "?")
        log(f"📦 Кеш завантажено: {len(products)} товарів (створено {generated})")
        return products
    except Exception as e:
        log(f"⚠️ Помилка читання кешу: {e}")
        return []


def apply_prices_to_cache(products: list, prices: dict) -> list:
    """Оновлює ціни і наявність в кешованих товарах."""
    updated = 0
    for p in products:
        pid = int(p.get("productID") or p.get("id") or 0)
        if pid and pid in prices:
            new = prices[pid]
            p["price"]              = new["price"]
            p["price_uah"]          = new["price_uah"]
            p["retail_price_uah"]   = new["retail_price_uah"]
            p["recommendable_price"] = new["recommendable_price"]
            p["available"]          = new["available"]
            p["is_archive"]         = new["is_archive"]
            updated += 1
    log(f"🔄 Оновлено цін: {updated}/{len(products)} товарів")
    return products


# ══════════════════════════════════════════════════════════════════
#  XML ГЕНЕРАТОР
# ══════════════════════════════════════════════════════════════════

def safe(text) -> str:
    if not text:
        return ""
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(text)).strip()


def build_group_id(product: dict) -> str:
    articul = safe(str(product.get("articul") or product.get("product_code") or ""))
    if not articul:
        return ""
    group = re.sub(r'[-/_\s]*(XS|S|M|L|XL|XXL|XXXL|\d{2,3})$', '', articul, flags=re.I)
    group = re.sub(r'\(\d{2,3}\)$', '', group).strip()
    return group or articul


def build_xml(
    products: list, all_cats: list, needed_ids: set,
    output: Path, shop_name: str, shop_url: str, lang: str,
) -> dict:
    log("📝 Генерація XML (формат Rozetka YML)...")
    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    root = Element("yml_catalog")
    root.set("date", now)

    shop = SubElement(root, "shop")
    SubElement(shop, "name").text    = safe(shop_name)
    SubElement(shop, "company").text = safe(shop_name)
    SubElement(shop, "url").text     = safe(shop_url)

    cur = SubElement(SubElement(shop, "currencies"), "currency")
    cur.set("id", "UAH"); cur.set("rate", "1")

    # Категорії + батьки для ієрархії
    cat_map     = {c["categoryID"]: c for c in all_cats}
    full_needed = set(needed_ids)
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

    # Товари
    offers_el = SubElement(shop, "offers")
    added = skipped = 0

    for p in products:
        pid = (p.get("productID") or p.get("product_id")
               or p.get("ID") or p.get("id"))
        if not pid:
            skipped += 1
            continue
        pid = int(pid)

        # Ціна
        price = 0.0
        for f in ["price_uah", "price", "retail_price_uah", "recommendable_price"]:
            try:
                v = float(str(p.get(f) or 0).replace(",", "."))
                if v > 0:
                    price = v
                    break
            except Exception:
                pass
        if price <= 0:
            skipped += 1
            continue

        offer = SubElement(offers_el, "offer")
        offer.set("id", str(pid))
        offer.set("available", "true")

        group_id = build_group_id(p)
        if group_id:
            offer.set("group_id", group_id)

        name = safe(p.get("name_ua") or p.get("name") or "")
        SubElement(offer, "name").text       = name
        SubElement(offer, "price").text      = str(round(price, 2))
        SubElement(offer, "currencyId").text = "UAH"

        try:
            old = float(str(p.get("retail_price_uah") or p.get("recommendable_price") or 0).replace(",", "."))
            if old > price:
                SubElement(offer, "price_old").text = str(round(old, 2))
        except Exception:
            pass

        SubElement(offer, "categoryId").text = str(p.get("categoryID", ""))

        # Фото
        pics = p.get("pictures", [])
        if pics:
            for pic in pics:
                url = pic.get("large_image") or pic.get("full_image") or pic.get("medium_image")
                if url:
                    SubElement(offer, "picture").text = url
        else:
            for key in ["large_image", "full_image", "medium_image", "small_image"]:
                if p.get(key):
                    SubElement(offer, "picture").text = p[key]
                    break

        if p.get("vendor"):
            SubElement(offer, "vendor").text = safe(str(p["vendor"]))

        articul = p.get("articul") or p.get("product_code", "")
        if articul:
            SubElement(offer, "article").text = safe(str(articul))

        if p.get("warranty"):
            SubElement(offer, "warranty").text = f"{p['warranty']} міс."

        if p.get("country"):
            SubElement(offer, "country_of_origin").text = safe(str(p["country"]))

        if p.get("weight"):
            try:
                SubElement(offer, "weight").text = str(round(float(p["weight"]), 3))
            except Exception:
                pass

        avail = p.get("available", {})
        qty   = sum(avail.values()) if isinstance(avail, dict) else 0
        SubElement(offer, "stock_quantity").text = str(qty)

        desc = safe(p.get("brief_description") or p.get("description_ua") or "")
        if desc:
            SubElement(offer, "description_ua").text = f"<![CDATA[{desc}]]>"

        for opt in p.get("options", []):
            oname = safe(opt.get("OptionName") or opt.get("name_ua") or opt.get("name") or "")
            oval  = safe(opt.get("ValueName")  or opt.get("value_ua") or opt.get("value") or "")
            if oname and oval:
                param = SubElement(offer, "param")
                param.set("name", oname)
                param.text = oval

        if p.get("is_new") and str(p.get("is_new")) not in ("0", "False", "false"):
            SubElement(offer, "is_new").text = "true"

        added += 1

    # Запис файлу
    output.parent.mkdir(parents=True, exist_ok=True)
    raw    = tostring(root, encoding="unicode")
    try:
        pretty = parseString(f'<?xml version="1.0" encoding="UTF-8"?>{raw}').toprettyxml(indent="  ")
        lines  = pretty.splitlines()
        if lines[0].startswith("<?xml"):
            lines[0] = '<?xml version="1.0" encoding="UTF-8"?>'
        final = "\n".join(lines)
    except Exception:
        final = f'<?xml version="1.0" encoding="UTF-8"?>\n{raw}'

    output.write_text(final, encoding="utf-8")
    size_mb = output.stat().st_size / 1024 / 1024

    return {"offers": added, "skipped": skipped,
            "categories": len(cats_el), "size_mb": round(size_mb, 1)}


# ══════════════════════════════════════════════════════════════════
#  ЛОГУВАННЯ
# ══════════════════════════════════════════════════════════════════

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ══════════════════════════════════════════════════════════════════
#  ГОЛОВНА ФУНКЦІЯ
# ══════════════════════════════════════════════════════════════════

async def main():
    cfg = load_config()

    login     = cfg.get("login", "")
    password  = cfg.get("password", "")
    lang      = cfg.get("lang", "ua")
    cat_ids   = cfg.get("category_ids", [])
    shop_name = cfg.get("shop_name", "Мій магазин")
    shop_url  = cfg.get("shop_url",  "https://example.com.ua")
    output    = Path(cfg.get("output_file", "output/catalog.xml"))
    mode      = cfg.get("mode", "quick")

    if not login or not password:
        log("❌ Не задані BRAIN_LOGIN / BRAIN_PASSWORD")
        sys.exit(1)

    if not cat_ids:
        log("❌ Не вибрані категорії (category_ids порожній)")
        sys.exit(1)

    log("=" * 55)
    log(f"  Brain API → XML Exporter v3.0")
    log(f"  Режим: {'🚀 FULL (повна вигрузка)' if mode == 'full' else '⚡ QUICK (тільки ціни)'}")
    log(f"  Мова: {lang.upper()} | Категорій: {len(cat_ids)}")
    log("=" * 55)

    # Quick режим без кешу → автоматично full
    if mode == "quick" and not CACHE_FILE.exists():
        log("⚠️  Кеш не знайдено — перемикаємось на FULL режим")
        mode = "full"

    async with httpx.AsyncClient(
        timeout=30,
        limits=httpx.Limits(max_connections=15, max_keepalive_connections=10),
    ) as client:
        sid = await auth(client, login, password)

        try:
            all_cats = await fetch_categories(client, sid, lang)
            cat_map  = {c["categoryID"]: c for c in all_cats}

            # Зберігаємо категорії для сайту
            save_categories_json(all_cats)

            valid_ids = [cid for cid in cat_ids if cid in cat_map]
            if not valid_ids:
                log(f"❌ Категорії {cat_ids} не знайдені")
                sys.exit(1)

            needed = set()
            for cid in valid_ids:
                needed |= get_all_descendants(all_cats, cid)

            log(f"📋 Категорій для вигрузки (з нащадками): {len(needed)}")

            # ── FULL режим ────────────────────────────────────────
            if mode == "full":
                products = await fetch_all_products_full(client, sid, list(needed), lang)

            # ── QUICK режим ───────────────────────────────────────
            else:
                log("⚡ Quick: завантажуємо кеш + оновлюємо ціни...")
                products = load_cache()
                if not products:
                    log("⚠️  Кеш порожній — перемикаємось на FULL")
                    products = await fetch_all_products_full(client, sid, list(needed), lang)
                else:
                    prices   = await fetch_prices_only(client, sid, list(needed), lang)
                    products = apply_prices_to_cache(products, prices)

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
            log(f"✅ Готово! Режим: {mode.upper()}")
            log(f"   Товарів у XML:  {stats['offers']}")
            log(f"   Пропущено:      {stats['skipped']}")
            log(f"   Категорій:      {stats['categories']}")
            log(f"   Розмір файлу:   {stats['size_mb']} МБ")
            if stats["size_mb"] > 180:
                log("⚠️  Файл > 180 МБ — перевищує ліміт Prom.ua!")
            else:
                log(f"✅ Prom.ua ліміт OK ({stats['size_mb']} / 180 МБ)")
            log(f"   XML посилання:")
            log(f"   https://raw.githubusercontent.com/lozko1991-blip/brain-exporter/main/output/catalog.xml")
            log(f"{'=' * 55}")

        finally:
            await logout(client, sid)


if __name__ == "__main__":
    asyncio.run(main())
