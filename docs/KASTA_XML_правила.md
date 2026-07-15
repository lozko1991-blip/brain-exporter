# Інструкція KASTA: завантаження контенту XML-формат

> Джерело: офіційний PDF KASTA "Інструкція з завантаження контенту XML формат"
> Збережено: 2026-06-14

---

## 1. Структура XML-файлу

```xml
<?xml version="1.0" encoding="UTF-8"?>
<yml_catalog date="2022-07-20 14:58">
  <shop>
    <currencies>
      <currency id="UAH" rate="1"/>
    </currencies>
    <categories>
      <category id="12345" rz_id="32635505">Назва категорії</category>
    </categories>
    <offers>
      <offer id="19305" available="true">
        ...
      </offer>
    </offers>
  </shop>
</yml_catalog>
```

### Теги структури

| Тег | Обов'язковий | Опис |
|-----|:---:|------|
| `<yml_catalog date="...">` | ✅ | Корінь YML. Атрибут `date` — дата генерації XML, формат `YYYY-MM-DD hh:mm` |
| `<shop>` | ✅ | Контейнер магазину |
| `<currencies>` | ✅ | Валюти. `currency id="UAH" rate="1"` |
| `<categories>` | ✅ | Список категорій. Кожна має унікальний `id`. Закривається `</categories>` |
| `<offers>` | ✅ | Список товарів. Відкривається після `</categories>` |

---

## 2. Категорії (`<categories>`)

```xml
<categories>
  <category id="12345" rz_id="32635505">Жіночі сукні</category>
</categories>
```

- **`id`** — унікальний ID категорії в межах вашого XML
- **`rz_id`** — ID категорії на Rozetka (необов'язковий, але допомагає KASTA автоматично зіставити категорію)
- Батьківські категорії задаються атрибутом `parentId`

---

## 3. Товар (`<offer>`)

```xml
<offer id="19305" available="true">
  <currencyId>UAH</currencyId>
  <categoryId>12345</categoryId>
  <price>900</price>
  <old_price>1700</old_price>
  <price_promo>700</price_promo>
  <picture>https://cdn.kasta.ua/image/.../photo.jpeg</picture>
  <vendor>Brand Name</vendor>
  <article>SKU-001</article>
  <name_ua>Назва товару укр</name_ua>
  <name_ru>Название товара рус</name_ru>
  <description_ua>Опис товару укр (до 5000 символів)</description_ua>
  <param name="Розмір">M</param>
  <param name="Колір">червоний</param>
</offer>
```

---

## 4. Атрибути `<offer>`

| Атрибут | Обов'язковий | Правила |
|---------|:---:|---------|
| `id` | ✅ | Унікальний ID товару. Лише `Aa-Zz`, `0-9`, без пробілів. Не змінювати після публікації |
| `available` | ✅ | `true` = є в наявності (stock=10), `false` = нема (stock=0) |

---

## 5. Ціни — НАЙВАЖЛИВІШЕ

### Три цінові теги

| Тег | Назва | Правило |
|-----|-------|---------|
| `<price>` | Ціна продажу | **Обов'язкова.** Ціна, за якою виставляється товар |
| `<old_price>` або `<price_old>` | Стара ціна (закреслена) | Необов'язкова. **СТРОГО > price** |
| `<price_promo>` або `<promo_price>` або `<promo_new_price>` | Акційна ціна | Необов'язкова. **СТРОГО ≤ price**. Акційна ціна покупця |

### Правила цін (офіційні, з PDF)

1. **`<price>` + `<old_price>`** — товар зі знижкою. KASTA показує: ~~old_price~~ → price
2. **Лише `<price>`** — звичайна ціна без знижки
3. **`<old_price>` без `<price>`** або `price = 0` — **помилка PRICE_ERROR**
4. **`<price_old>` / `<old_price>` ≤ `<price>`** — **помилка PRICE_ERROR**
5. **`<price_promo>`** ≤ `<price>` — акційна ціна, що замінює price під час акції
6. **Будь-яке значення = 0** (крім stock) — помилка

### Наш прайс ISSA PLUS (3 значення)

```xml
<price>871</price>                  <!-- ціна продажу = srcPrice × 1.5 + 50 -->
<old_price>1153</old_price>         <!-- стара ціна = srcOldPrice × 1.5 + 50, строго > price -->
<price_promo>827</price_promo>      <!-- акційна = price × 0.95 (−5%), строго ≤ price -->
```

---

## 6. Залишок (stock)

| Варіант | Результат |
|---------|-----------|
| `available="true"` (без stock-тега) | stock = 10 |
| `available="false"` (без stock-тега) | stock = 0 |
| `<stock_quantity>100</stock_quantity>` + `available="true"` | stock = 100 |
| `<stock_quantity>100</stock_quantity>` + `available="false"` | stock = 0 |
| `<stock_quantity>0</stock_quantity>` + `available="true"` | stock = 0 |

Альтернативні теги залишку: `<stock_quantity>`, `<quantity_in_stock>`, `<stock>`

---

## 7. Зображення (`<picture>`)

```xml
<picture>https://site.ua/images/product-123.jpeg</picture>
<picture>https://site.ua/images/product-123-2.jpeg</picture>
```

- **Мінімум 1, максимум 20** фото на товар
- **Мінімальна ширина**: 1035 px (рекомендовано 1440 px)
- **Формат**: JPEG (рекомендовано)
- Перший `<picture>` = головне фото
- Зображення повинні повертати HTTP 200
- Не змінювати URL зображення без потреби (HUB запам'ятовує URL — зміна URL = повторне завантаження)

### IP-адреси KASTA (для whitelist на вашому сервері)

```
18.196.145.164
3.121.252.59
52.59.104.195
3.126.0.144
```

### User-Agent KASTA

```
Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/74.0.3729.169 Safari/537.36
Apache-HttpClient/4.5.5 (Java/22.0.2)
```

---

## 8. Назва товару

| Тег | Призначення | Пріоритет |
|-----|-------------|-----------|
| `<name_ua>` | Українська назва | 1-й (основний) |
| `<name_ru>` | Російська назва | 2-й |
| `<name>` | Автовизначення мови | Якщо немає name_ua / name_ru |

**Правила:**
- Якщо є `name_ua` → KASTA використовує його
- Якщо є тільки `name` → визначає мову автоматично
- Не дублювати бренд у `<vendor>`, `<param name="Бренд">` і назві товару одночасно

---

## 9. Артикул / SKU

Рівнозначні теги (будь-який один):
```xml
<article>SKU-001</article>
<vendorCode>SKU-001</vendorCode>
<param name="Артикул">SKU-001</param>
```

Ідентифікація товару в KASTA = `vendor` + `article`

---

## 10. Розміри (`<param name="Розмір">`)

```xml
<!-- Варіант 1: Розмір Kasta -->
<param name="Розмір Kasta">48</param>
<param name="Розмір Kasta (max)">50</param>

<!-- Варіант 2: числовий -->
<param name="Розмір">42</param>

<!-- Варіант 3: буквений -->
<param name="Розмір">M</param>

<!-- Варіант 4: комбінований -->
<param name="Розмір">42/152</param>
```

**Важливо:** якщо розмір задається діапазоном (44-46), KASTA розбиває його на окремі значення. Краще вказувати окремі `<param>` для кожного розміру у різних `<offer>`.

---

## 11. Опис (`<description>`)

```xml
<description_ua>Опис укр мовою (до 5000 символів)</description_ua>
<description>Опис (якщо мова не визначена)</description>
```

- Максимум **5000 символів**
- Не дублювати характеристики у описі
- Не використовувати HTML-теги (крім дозволених)

---

## 12. Характеристики (`<param>`)

```xml
<param name="Колір">червоний</param>
<param name="Розмір">42</param>
<param name="Склад, матеріал">92% бавовна, 8% еластан</param>
<param name="Висота, мм">306</param>
```

- Назва param = назва характеристики
- Числові значення без одиниць або з одиницями в назві param
- Розміри: `%1x%2x%3` → KASTA перетворює на `10x11x12`
- Роздільники розмірів: `×`, `x`, `-`, `:`

---

## 13. Категорія (`<categoryId>`)

```xml
<categoryId>12345</categoryId>
```

- Значення = `id` з блоку `<categories>`
- Якщо не знайдено → **CATEGORY_EXTRACTION_ERROR**

---

## 14. Ліміти XML-файлу

| Параметр | Ліміт |
|----------|-------|
| Розмір файлу | 1 GB |
| Timeout завантаження | 45 хвилин |
| Timeout підключення | 1 хвилина |
| Максимум товарів | 200 000 |
| Максимум фото на товар | 20 |

---

## 15. Коди помилок імпорту

| Код | Причина | Рішення |
|-----|---------|---------|
| `PRICE_ERROR` | `old_price` ≤ `price`, або `price = 0` | Перевірити що old_price > price |
| `OFFER_PICTURES_ERROR` | Зображення повертає 404/500 | Перевірити URL фото |
| `SIZE_NOT_PROVIDED` | Відсутній param з розміром | Додати `<param name="Розмір">` |
| `SIZE_NOT_FOUND` | Розмір не знайдено в каталозі KASTA | Перевірити формат розміру |
| `CATEGORY_EXTRACTION_ERROR` | Невалідний `categoryId` | Перевірити відповідність з `<categories>` |
| `FIELD_PARSE_ERROR` | Помилка парсингу поля | Перевірити формат конкретного поля |
| `INVALID_IMPORT_LOAD_SKU` | Дублікат або невалідний SKU | Перевірити унікальність `offer id` |
| `STOPWORDS_SKIP` | Стоп-слово у назві/бренді | Прибрати заборонені слова |
| `LANG_EXTRACTION_ERROR` | Неправильна мова у полях | Використати `name_ua` / `name_ru` |
| `CHARACTERISTICS_MATCH_ERROR` | Характеристики не відповідають категорії | Перевірити назви param |
| `CHARACTERISTICS_NOT_ENOUGH` | Недостатньо характеристик | Додати обов'язкові param |
| `VALUE_ERROR` | Неправильний тип значення | Перевірити числові поля |
| `IMAGES_AT_MAX` | Більше 20 фото на товар | Залишити максимум 20 |
| `UNKNOWN_ERROR` | Невідома помилка | Звернутись до moderator@kasta.ua |

---

## 16. Стоп-слова (заборонені у назві/бренді)

Не можна використовувати у `<name>`, `<vendor>` та `<param>`:

**Конкуренти:** rozetka, prom, expres, express, meest, kasta та ін.  
**Маркетплейси:** /, ., ., ,  
**Інші:** (повний список надає moderator@kasta.ua)

---

## 17. Статуси оновлення (успішні)

| Код | Значення |
|-----|---------|
| `SKIP_NOT_NEW` | SKU вже є, пропускаємо |
| `SKIP_NO_CHANGES` | Немає змін |
| `OFFER_PICTURES_WAIT` | Фото в черзі на завантаження |
| `NAME_TRUNCATED` | Назва обрізана |
| `DID_UPDATE_STOCK` | Залишок оновлено |
| `DID_UPDATE_PRICE` | Ціну оновлено |
| `DID_SET_STOCK_0` | Залишок встановлено в 0 |

---

## 18. Зміна зображень (важливо!)

HUB запам'ятовує зображення за URL. При зміні фото:

1. **Змінити URL** (`?v=2` або нова назва файлу) → HUB завантажить нове фото
2. **Не змінювати URL** → HUB використає старе фото з кешу

```xml
<!-- Стара версія -->
<picture>https://site.ua/images/product-123.jpg</picture>
<!-- Нова версія (HUB підхопить зміну) -->
<picture>https://site.ua/images/product-123.jpg?v=2</picture>
```

---

## 19. Приклад повного `<offer>` для ISSA PLUS

```xml
<offer id="1434102" available="true" group_id="61047">
  <currencyId>UAH</currencyId>
  <categoryId>25</categoryId>
  <price>871</price>
  <old_price>1153</old_price>
  <price_promo>827</price_promo>
  <picture>https://issaplus.com/wa-data/public/.../149682.601x0.jpg</picture>
  <picture>https://issaplus.com/wa-data/public/.../149683.601x0.jpg</picture>
  <vendor>ISSA PLUS</vendor>
  <article>10833_yellow</article>
  <name_ua>Жіноча сукня ISSA PLUS 10833 S жовта</name_ua>
  <description_ua>Жіноча сукня з 100% бавовни.</description_ua>
  <param name="Розмір">S</param>
  <param name="Колір">жовтий</param>
  <param name="Склад">100% хлопок</param>
  <param name="Матеріал">Котон</param>
</offer>
```

---

## 20. Контакти для питань

- **Email:** moderator@kasta.ua
- **Тема листа:** `new brand: [Назва бренду]`
- **Необхідні дані:** назва бренду, посилання на оригінальний сайт, артикул товару
