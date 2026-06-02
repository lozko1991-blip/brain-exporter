#!/usr/bin/env python3
"""
Brain API → XML Exporter v4.0
─────────────────────────────────────────────────────────────────
Підтримує КІЛЬКА фідів (feeds.json) — по одному XML на маркетплейс,
кожен зі своїми категоріями та своєю націнкою (% + грн).

Режими:
  full  — повна вигрузка: товари + характеристики + фото + повний опис
  quick — тільки ціни і наявність (швидко), бере дані з кешу

Запуск:
  EXPORT_MODE=full  python export.py
  EXPORT_MODE=quick python export.py

Що змінилось проти v3:
  • Кілька фідів з feeds.json (fallback на старий config.json)
  • Націнка задається per-feed (percent + fixed), а не зашита в коді
  • ВИПРАВЛЕНО опис: тепер качаємо повний `description` через /product/
  • ВИПРАВЛЕНО наявність: рахуємо реальні залишки зі `stocks`
"""

import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from xml.dom.minidom import parseString
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx

# ══════════════════════════════════════════════════════════════════
API_BASE   = "http://api.brain.com.ua"
OUTPUT_DIR = Path("output")
CACHE_FILE = Path("products_cache.json")
CATS_FILE  = Path("categories.json")
FEEDS_FILE = Path("feeds.json")
OUTPUT_DIR.mkdir(exist_ok=True)

# Префікс для ID товарів і категорій у XML — щоб уникнути колізій
# з товарами інших постачальників на маркетплейсі. "br" = Brain.
ID_PREFIX = "br"

def pid_ext(pid) -> str:
    """ID товару для XML: br12345"""
    return f"{ID_PREFIX}{pid}"

def cid_ext(cid) -> str:
    """ID категорії для XML: br1181"""
    return f"{ID_PREFIX}{cid}"


# ══════════════════════════════════════════════════════════════════
#  ЛОГУВАННЯ
# ══════════════════════════════════════════════════════════════════

def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ══════════════════════════════════════════════════════════════════
#  КОНФІГ + ФІДИ
# ══════════════════════════════════════════════════════════════════

def load_base_config() -> dict:
    """Спільні налаштування: логін, мова, режим, назва магазину."""
    cfg = {}
    if Path("config.json").exists():
        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))

    if os.environ.get("BRAIN_LOGIN"):    cfg["login"]     = os.environ["BRAIN_LOGIN"]
    if os.environ.get("BRAIN_PASSWORD"): cfg["password"]  = os.environ["BRAIN_PASSWORD"]
    if os.environ.get("LANG"):           cfg["lang"]      = os.environ["LANG"]
    if os.environ.get("SHOP_NAME"):      cfg["shop_name"] = os.environ["SHOP_NAME"]
    if os.environ.get("SHOP_URL"):       cfg["shop_url"]  = os.environ["SHOP_URL"]
    if os.environ.get("EXPORT_MODE"):    cfg["mode"]      = os.environ["EXPORT_MODE"]

    cfg.setdefault("lang",      "ua")
    cfg.setdefault("shop_name", "Мій магазин")
    cfg.setdefault("shop_url",  "https://example.com.ua")
    cfg.setdefault("mode",      "quick")
    return cfg


def load_feeds(base: dict) -> list:
    """
    Повертає список фідів. Кожен фід:
      {
        "id": "rozetka",                 # → output/rozetka.xml
        "name": "Rozetka",               # назва магазину в XML
        "category_ids": [1181, 1191],    # вибрані категорії
        "markup_percent": 20,            # +20%
        "markup_fixed": 50,              # +50 грн
        "lang": "ua"                     # (опц.) перевизначає базову мову
      }
    Якщо feeds.json немає — будуємо один фід зі старого config.json.
    """
    if FEEDS_FILE.exists():
        data = json.loads(FEEDS_FILE.read_text(encoding="utf-8"))
        feeds = data.get("feeds", data) if isinstance(data, dict) else data
        out = []
        for f in feeds:
            out.append({
                "id":             str(f.get("id") or f.get("name") or "feed").strip(),
                "name":           f.get("name") or base["shop_name"],
                "category_ids":   [int(x) for x in f.get("category_ids", [])],
                "markup_percent": float(f.get("markup_percent", 0) or 0),
                "markup_fixed":   float(f.get("markup_fixed", 0) or 0),
                "lang":           f.get("lang") or base["lang"],
            })
        log(f"📑 feeds.json: {len(out)} фід(ів)")
        return out

    # ── Fallback: старий config.json як один фід ──
    cfg = {}
    if Path("config.json").exists():
        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
    log("📑 feeds.json не знайдено — використовую config.json як один фід")
    return [{
        "id":             "catalog",
        "name":           base["shop_name"],
        "category_ids":   [int(x) for x in cfg.get("category_ids", [])],
        "markup_percent": float(cfg.get("markup_percent", 0) or 0),
        "markup_fixed":   float(cfg.get("markup_fixed", 0) or 0),
        "lang":           base["lang"],
    }]


# ══════════════════════════════════════════════════════════════════
#  АВТОРИЗАЦІЯ
# ══════════════════════════════════════════════════════════════════

async def auth(client: httpx.AsyncClient, login: str, password: str) -> str:
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


def get_all_descendants(cats_by_parent: dict, parent_id: int) -> set:
    """Усі нащадки категорії (включно з нею). Ітеративно — без рекурсії."""
    result = set()
    stack = [parent_id]
    while stack:
        cur = stack.pop()
        if cur in result:
            continue
        result.add(cur)
        stack.extend(cats_by_parent.get(cur, []))
    return result


def save_categories_json(all_cats: list):
    """Дерево категорій для адмінки (index.html)."""
    cat_map = {c["categoryID"]: {
        "categoryID": c["categoryID"],
        "parentID":   c["parentID"],
        "name":       c["name"],
        "children":   [],
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
        encoding="utf-8",
    )
    log(f"📂 categories.json збережено ({len(all_cats)} категорій)")


# ══════════════════════════════════════════════════════════════════
#  ЗАВАНТАЖЕННЯ ТОВАРІВ
# ══════════════════════════════════════════════════════════════════

async def fetch_products_page(
    client: httpx.AsyncClient, sid: str, cat_id: int,
    lang: str, offset: int, limit: int = 100
) -> list:
    url = (f"{API_BASE}/products/{cat_id}/{sid}"
           f"?lang={lang}&limit={limit}&offset={offset}")
    for attempt in range(3):
        try:
            resp = await client.get(url, timeout=30)
            data = resp.json()
            if data.get("status") == 1:
                result = data["result"]
                if isinstance(result, list):
                    return result
                if isinstance(result, dict):
                    return result.get("list") or result.get("products") or []
        except Exception as e:
            if attempt == 2:
                log(f"   ⚠️ Кат.{cat_id} offset={offset}: {e}")
        await asyncio.sleep(1.5 ** attempt)
    return []


async def fetch_product_full(client: httpx.AsyncClient, sid: str, pid: int, lang: str) -> dict:
    """Повна картка товару — ДАЄ повний `description` (список /products його не містить)."""
    try:
        r = await client.get(f"{API_BASE}/product/{pid}/{sid}?lang={lang}", timeout=20)
        d = r.json()
        if d.get("status") == 1 and isinstance(d.get("result"), dict):
            return d["result"]
    except Exception:
        pass
    return {}


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
            result = d.get("result", [])
            if isinstance(result, list):
                return result
            if isinstance(result, dict):
                return result.get("pictures", [])
    except Exception:
        pass
    return []


async def fetch_all_products_full(
    client: httpx.AsyncClient, sid: str, cat_ids: list, lang: str
) -> list:
    """FULL: базові дані + (повна картка з описом) + характеристики + фото. Кешуємо."""
    pool: dict[int, dict] = {}

    # Фаза 1: базові дані (список товарів)
    log("\n📦 Фаза 1: базові дані товарів...")
    skipped_oos = 0
    for i, cat_id in enumerate(cat_ids, 1):
        offset = 0
        cat_count = 0
        while True:
            products = await fetch_products_page(client, sid, cat_id, lang, offset)
            if not products:
                break
            for p in products:
                pid = (p.get("productID") or p.get("product_id")
                       or p.get("ID") or p.get("id"))
                if not pid:
                    continue
                # Пропускаємо відсутні ще ДО завантаження описів/фото — це головна
                # економія часу. Обережно: фільтруємо лише коли дані про наявність
                # реально присутні у відповіді списку.
                is_archive = str(p.get("is_archive", "0")) not in ("0", "False", "false", "")
                has_stock_field = ("stocks" in p) or ("stocks_expected" in p)
                if is_archive or (has_stock_field and stock_qty(p) == 0):
                    skipped_oos += 1
                    continue
                p["categoryID"] = p.get("categoryID", cat_id)
                pool[int(pid)] = p
                cat_count += 1
            print(f"   [{i}/{len(cat_ids)}] Кат.{cat_id}: {offset + len(products)}", end="\r")
            if len(products) < 100:
                break
            offset += 100
            await asyncio.sleep(0.4)
        log(f"   [{i}/{len(cat_ids)}] Категорія {cat_id}: {cat_count} в наявності")

    product_list = list(pool.values())
    log(f"\n✅ Фаза 1: {len(product_list)} товарів у наявності "
        f"(пропущено відсутніх: {skipped_oos})")

    # Фаза 2: повна картка (опис) + характеристики + фото, паралельно по 3
    log("📋 Фаза 2: опис + характеристики + фото (паралельно по 3)...")
    total    = len(product_list)
    batch_sz = 3  # 3 товари × 3 запити = 9 req / 2 сек ≈ ліміт Brain

    for start in range(0, total, batch_sz):
        batch = product_list[start:start + batch_sz]
        pids  = [int(p.get("productID") or p.get("id")) for p in batch]

        all_results = await asyncio.gather(*[
            asyncio.gather(
                fetch_product_full(client, sid, pid, lang),
                fetch_options(client, sid, pid, lang),
                fetch_pictures(client, sid, pid),
            )
            for pid in pids
        ])

        for p, (full, opts, pics) in zip(batch, all_results):
            # Повна картка містить description, country, weight, stocks тощо
            if full:
                for k, v in full.items():
                    # не затираємо ціни/наявність зі списку, якщо у повній порожньо
                    if v not in (None, "", []):
                        p[k] = v
            p["options"]  = opts
            p["pictures"] = pics

        done = min(start + batch_sz, total)
        pct  = int(done / total * 100)
        bar  = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"   [{bar}] {pct}% ({done}/{total})", end="\r")
        await asyncio.sleep(2)

    log(f"\n✅ Фаза 2 завершена")
    save_cache(product_list)
    return product_list


async def fetch_prices_only(
    client: httpx.AsyncClient, sid: str, cat_ids: list, lang: str
) -> dict:
    """QUICK: тільки ціни і наявність. pid → {ціни, stocks, is_archive}."""
    log("\n⚡ Quick: завантаження цін і наявності...")
    prices = {}
    for i, cat_id in enumerate(cat_ids, 1):
        offset = 0
        while True:
            products = await fetch_products_page(client, sid, cat_id, lang, offset)
            if not products:
                break
            for p in products:
                pid = (p.get("productID") or p.get("product_id")
                       or p.get("ID") or p.get("id"))
                if pid:
                    prices[int(pid)] = {
                        "price":               p.get("price", 0),
                        "price_uah":           p.get("price_uah", 0),
                        "retail_price_uah":    p.get("retail_price_uah", 0),
                        "recommendable_price": p.get("recommendable_price", 0),
                        "stocks":              p.get("stocks", []),
                        "stocks_expected":     p.get("stocks_expected", {}),
                        "is_archive":          p.get("is_archive", 0),
                    }
            print(f"   [{i}/{len(cat_ids)}] Кат.{cat_id}: {offset + len(products)}", end="\r")
            if len(products) < 100:
                break
            offset += 100
            await asyncio.sleep(0.4)
        log(f"   [{i}/{len(cat_ids)}] Категорія {cat_id}: {len(prices)} товарів (накопич.)")
    log(f"✅ Ціни отримані: {len(prices)} товарів")
    return prices


# ══════════════════════════════════════════════════════════════════
#  КЕШ
# ══════════════════════════════════════════════════════════════════

def save_cache(products: list):
    CACHE_FILE.write_text(
        json.dumps({
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "count":     len(products),
            "products":  products,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    size_mb = CACHE_FILE.stat().st_size / 1024 / 1024
    log(f"💾 Кеш збережено: {len(products)} товарів ({size_mb:.1f} МБ)")


def load_cache() -> list:
    if not CACHE_FILE.exists():
        return []
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        products = data.get("products", [])
        log(f"📦 Кеш завантажено: {len(products)} товарів (створено {data.get('generated','?')})")
        return products
    except Exception as e:
        log(f"⚠️ Помилка читання кешу: {e}")
        return []


def apply_prices_to_cache(products: list, prices: dict) -> list:
    updated = gone = 0
    for p in products:
        pid = int(p.get("productID") or p.get("id") or 0)
        if pid and pid in prices:
            new = prices[pid]
            p["price"]               = new["price"]
            p["price_uah"]           = new["price_uah"]
            p["retail_price_uah"]    = new["retail_price_uah"]
            p["recommendable_price"] = new["recommendable_price"]
            p["stocks"]              = new["stocks"]
            p["stocks_expected"]     = new["stocks_expected"]
            p["is_archive"]          = new["is_archive"]
            updated += 1
        else:
            # Товару більше немає у свіжому списку категорії → вважаємо відсутнім.
            # Фільтр наявності в build_feed_xml його приховає.
            p["stocks"] = []
            p["is_archive"] = 1
            gone += 1
    log(f"🔄 Оновлено цін: {updated}/{len(products)} (зникли з наявності: {gone})")
    return products


# ══════════════════════════════════════════════════════════════════
#  ДОПОМІЖНІ
# ══════════════════════════════════════════════════════════════════

def safe(text) -> str:
    if not text:
        return ""
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(text)).strip()


def base_price(p: dict) -> float:
    """
    Базова закупівельна ціна в ГРИВНІ (ДО націнки).
    ВАЖЛИВО: поле `price` у Brain — ціна у валюті постачальника (USD/EUR),
    тому для гривневого XML його НЕ використовуємо, інакше ціна буде у ~37х занижена.
    Беремо лише гривневі поля.
    """
    for f in ["price_uah", "retail_price_uah", "recommendable_price"]:
        try:
            v = float(str(p.get(f) or 0).replace(",", "."))
            if v > 0:
                return v
        except Exception:
            pass
    return 0.0


def stock_qty(p: dict) -> int:
    """
    Чи є товар у наявності (і скільки умовно).
    У Brain `stocks` — це масив ID складів, де товар присутній (напр. [1,2,3]),
    а НЕ кількість штук. Точну кількість API не віддає.
    Тому: товар у наявності ⇢ повертаємо умовну кількість для маркетплейсу.
    Повертає 0, якщо товару немає на жодному складі.
    """
    stocks = p.get("stocks", [])
    if isinstance(stocks, list):
        # є хоч один склад → в наявності. Кількість невідома → ставимо запас.
        return 99 if len(stocks) > 0 else 0
    if isinstance(stocks, dict):
        total = sum(int(v) for v in stocks.values() if str(v).isdigit())
        return total if total > 0 else (99 if stocks else 0)
    if isinstance(stocks, (int, float)):
        return int(stocks)
    return 0


def build_group_id(product: dict) -> str:
    """
    Групування варіантів. Brain не дає офіційного поля,
    тож обережно: ріжемо лише явні суфікси розмірів одягу/взуття.
    Числові суфікси НЕ ріжемо (вони часто = модель, а не розмір).
    """
    articul = safe(str(product.get("articul") or product.get("product_code") or ""))
    if not articul:
        return ""
    group = re.sub(r'[-/_\s]+(XS|S|M|L|XL|XXL|XXXL|XXXXL)$', '', articul, flags=re.I)
    return group if group and group != articul else ""


# ══════════════════════════════════════════════════════════════════
#  XML ГЕНЕРАТОР (один фід)
# ══════════════════════════════════════════════════════════════════

def build_feed_xml(
    products: list, all_cats: list, feed: dict,
    shop_url: str, output: Path,
) -> dict:
    cat_map     = {c["categoryID"]: c for c in all_cats}
    by_parent: dict[int, list] = {}
    for c in all_cats:
        by_parent.setdefault(c.get("parentID", 1), []).append(c["categoryID"])

    # Які категорії потрібні цьому фіду (вибрані + усі нащадки)
    needed = set()
    for cid in feed["category_ids"]:
        if cid in cat_map:
            needed |= get_all_descendants(by_parent, cid)

    lang = feed["lang"]
    mp   = feed["markup_percent"]
    mf   = feed["markup_fixed"]
    log(f"📝 Фід '{feed['id']}': категорій={len(needed)}, націнка +{mp}% +{mf}грн")

    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    root = Element("yml_catalog"); root.set("date", now)
    shop = SubElement(root, "shop")
    SubElement(shop, "name").text    = safe(feed["name"])
    SubElement(shop, "company").text = safe(feed["name"])
    SubElement(shop, "url").text     = safe(shop_url)
    cur = SubElement(SubElement(shop, "currencies"), "currency")
    cur.set("id", "UAH"); cur.set("rate", "1")

    # Категорії + усі батьки для коректної ієрархії
    full_needed = set(needed)
    for cid in list(needed):
        pid = cat_map.get(cid, {}).get("parentID", 1)
        while pid and pid != 1 and pid in cat_map:
            full_needed.add(pid)
            pid = cat_map.get(pid, {}).get("parentID", 1)

    cats_el = SubElement(shop, "categories")
    n_cats = 0
    for cat in all_cats:
        if cat["categoryID"] not in full_needed:
            continue
        el = SubElement(cats_el, "category")
        el.set("id", cid_ext(cat["categoryID"]))
        if cat.get("parentID", 1) != 1:
            el.set("parentId", cid_ext(cat["parentID"]))
        el.text = safe(cat["name"])
        n_cats += 1

    offers_el = SubElement(shop, "offers")
    added = skipped = 0

    for p in products:
        pid = (p.get("productID") or p.get("product_id") or p.get("ID") or p.get("id"))
        if not pid:
            skipped += 1; continue
        pid = int(pid)

        # фільтр по категоріях фіда
        if p.get("categoryID") and int(p["categoryID"]) not in full_needed:
            skipped += 1; continue

        bp = base_price(p)
        if bp <= 0:
            skipped += 1; continue

        # ── ФІЛЬТР НАЯВНОСТІ: у XML потрапляють ТІЛЬКИ товари в наявності ──
        # (архівні та з нульовим залишком пропускаємо повністю)
        is_archive = str(p.get("is_archive", "0")) not in ("0", "False", "false", "")
        qty = stock_qty(p)
        if is_archive or qty == 0:
            skipped += 1; continue

        offer = SubElement(offers_el, "offer")
        offer.set("id", pid_ext(pid))

        gid = build_group_id(p)
        if gid:
            offer.set("group_id", f"{ID_PREFIX}{gid}")

        SubElement(offer, "name").text = safe(p.get("name") or "")

        # ── ЦІНА З НАЦІНКОЮ ФІДА: bp × (1 + %/100) + грн ──
        sell = round(bp * (1 + mp / 100) + mf, 0)
        SubElement(offer, "price").text      = str(int(sell))
        SubElement(offer, "currencyId").text = "UAH"

        # стара ціна — рекомендована роздрібна, якщо вона вища
        try:
            rec = float(str(p.get("retail_price_uah") or p.get("recommendable_price") or 0).replace(",", "."))
        except Exception:
            rec = 0
        if rec > sell:
            SubElement(offer, "price_old").text = str(int(rec))

        if p.get("categoryID"):
            SubElement(offer, "categoryId").text = cid_ext(p["categoryID"])

        # ── ФОТО ──
        pics = p.get("pictures", [])
        if pics:
            for pic in sorted(pics, key=lambda x: x.get("priority", 99)):
                url = pic.get("full_image") or pic.get("large_image") or pic.get("medium_image")
                if url and "no-photo" not in url:
                    SubElement(offer, "picture").text = url
        else:
            for key in ["full_image", "large_image", "medium_image", "small_image"]:
                if p.get(key) and "no-photo" not in str(p.get(key)):
                    SubElement(offer, "picture").text = p[key]; break

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

        # ── НАЯВНІСТЬ (товар, що дійшов сюди, завжди в наявності) ──
        offer.set("available", "true")
        SubElement(offer, "stock_quantity").text = str(qty)

        # ── ОПИС: повний `description`, fallback на короткий ──
        desc = safe(p.get("description") or p.get("brief_description") or "")
        if desc:
            d = SubElement(offer, "description")
            d.text = f"__CDATA_OPEN__{desc}__CDATA_CLOSE__"

        # ── ХАРАКТЕРИСТИКИ ──
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

    # запис
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = tostring(root, encoding="unicode")
    try:
        pretty = parseString(f'<?xml version="1.0" encoding="UTF-8"?>{raw}').toprettyxml(indent="  ")
        lines = pretty.splitlines()
        if lines[0].startswith("<?xml"):
            lines[0] = '<?xml version="1.0" encoding="UTF-8"?>'
        final = "\n".join(lines)
    except Exception:
        final = f'<?xml version="1.0" encoding="UTF-8"?>\n{raw}'

    # реальні CDATA: ElementTree екранує весь текст (включно з HTML у описі).
    # Розгортаємо плейсхолдери у справжні CDATA і знімаємо екранування ВСЕРЕДИНІ них.
    def _unescape_cdata(m):
        inner = m.group(1)
        inner = (inner.replace("&lt;", "<").replace("&gt;", ">")
                      .replace("&quot;", '"').replace("&apos;", "'")
                      .replace("&amp;", "&"))
        return f"<![CDATA[{inner}]]>"

    final = final.replace("&lt;![CDATA[", "__CDATA_OPEN__").replace("]]&gt;", "__CDATA_CLOSE__")
    final = re.sub(r'__CDATA_OPEN__(.*?)__CDATA_CLOSE__', _unescape_cdata, final, flags=re.S)

    output.write_text(final, encoding="utf-8")
    size_mb = output.stat().st_size / 1024 / 1024
    return {"offers": added, "skipped": skipped, "categories": n_cats, "size_mb": round(size_mb, 2)}


# ══════════════════════════════════════════════════════════════════
#  ГОЛОВНА ФУНКЦІЯ
# ══════════════════════════════════════════════════════════════════

async def main():
    base  = load_base_config()
    feeds = load_feeds(base)

    login    = base.get("login", "")
    password = base.get("password", "")
    mode     = base.get("mode", "quick")
    shop_url = base.get("shop_url", "https://example.com.ua")

    if not login or not password:
        log("❌ Не задані BRAIN_LOGIN / BRAIN_PASSWORD"); sys.exit(1)

    # усі категорії, потрібні хоч одному фіду
    all_selected = set()
    for f in feeds:
        all_selected |= set(f["category_ids"])
    if not all_selected:
        log("❌ Жоден фід не має вибраних категорій"); sys.exit(1)

    lang = base["lang"]
    log("=" * 55)
    log(f"  Brain API → XML Exporter v4.0")
    log(f"  Режим: {'🚀 FULL' if mode == 'full' else '⚡ QUICK'} | Фідів: {len(feeds)}")
    log("=" * 55)

    if mode == "quick" and not CACHE_FILE.exists():
        log("⚠️  Кеш не знайдено — перемикаємось на FULL")
        mode = "full"

    async with httpx.AsyncClient(
        timeout=30,
        limits=httpx.Limits(max_connections=15, max_keepalive_connections=10),
    ) as client:
        sid = await auth(client, login, password)
        try:
            all_cats = await fetch_categories(client, sid, lang)
            cat_map  = {c["categoryID"]: c for c in all_cats}
            save_categories_json(all_cats)

            by_parent: dict[int, list] = {}
            for c in all_cats:
                by_parent.setdefault(c.get("parentID", 1), []).append(c["categoryID"])

            # повний набір категорій (вибрані + нащадки) для завантаження товарів
            needed = set()
            for cid in all_selected:
                if cid in cat_map:
                    needed |= get_all_descendants(by_parent, cid)
            log(f"📋 Категорій для завантаження (з нащадками): {len(needed)}")

            if mode == "full":
                products = await fetch_all_products_full(client, sid, list(needed), lang)
            else:
                log("⚡ Quick: кеш + оновлення цін...")
                products = load_cache()
                if not products:
                    log("⚠️  Кеш порожній → FULL")
                    products = await fetch_all_products_full(client, sid, list(needed), lang)
                else:
                    prices   = await fetch_prices_only(client, sid, list(needed), lang)
                    products = apply_prices_to_cache(products, prices)

            # ── будуємо XML для КОЖНОГО фіда ──
            log(f"\n{'─' * 55}")
            results = []
            for f in feeds:
                out = OUTPUT_DIR / f"{f['id']}.xml"
                stats = build_feed_xml(products, all_cats, f, shop_url, out)
                stats["id"] = f["id"]; stats["file"] = str(out)
                results.append(stats)
                warn = "  ⚠️ >180МБ!" if stats["size_mb"] > 180 else ""
                log(f"  ✅ {f['id']}.xml — товарів {stats['offers']}, "
                    f"категорій {stats['categories']}, {stats['size_mb']}МБ{warn}")

            log(f"\n{'=' * 55}")
            log(f"✅ Готово! Режим: {mode.upper()} | Фідів: {len(results)}")
            log(f"   Постійні посилання (заміни АКАУНТ/РЕПО):")
            for r in results:
                log(f"   • {r['id']}: "
                    f"https://raw.githubusercontent.com/АКАУНТ/РЕПО/main/output/{r['id']}.xml")
            log(f"{'=' * 55}")

        finally:
            await logout(client, sid)


if __name__ == "__main__":
    asyncio.run(main())
