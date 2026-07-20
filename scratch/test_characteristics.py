import sys
from pathlib import Path

# Додаємо кореневу директорію до шляхів пошуку
sys.path.append(str(Path(__file__).resolve().parents[1]))

import export

def test_char_mapping():
    print("[~] Loading Kasta characteristics config...")
    cfg = export.load_kasta_characteristics_config()
    
    assert len(cfg.get("season_map", {})) > 0, "Season map is empty!"
    assert len(cfg.get("pattern_map", {})) > 0, "Pattern map is empty!"
    assert len(cfg.get("pattern_keywords", {})) > 0, "Pattern keywords are empty!"
    print("[+] Configuration successfully loaded.")
    
    # ── Тестові кейси для розмірів ──
    size_tests = [
        # (Вхідний розмір, Очікуваний вихід)
        ("152 см", "152 см"),         # Точний збіг зросту
        ("76 см", "74 см"),           # Округлення до меншого
        ("106 см", "104 см"),         # Округлення до меншого
        ("12 років", "12р."),         # Роки
        ("3 місяці", "3м."),          # Місяці
        ("12-18 міс", "12м."),        # Діапазон місяців
        ("6 років", "6р."),           # Роки
        ("рост 76", "74 см"),         # Текст + число
        
        # ── Тести запобіжника для дорослих розмірів ──
        ("42", "42"),                 # Дорослий розмір 42 (має залишитися 42, не стати "42 см")
        ("42 см", "42 см"),           # Дитячий зріст 42 см (має залишитися "42 см")
        ("104", "104 см")             # Дитячий зріст 104 без "см" (>= 80, стає "104 см")
    ]
    
    print("\n--- RUNNING SIZE STANDARDIZATION TESTS ---")
    failed_sizes = 0
    for val, expected in size_tests:
        res = export.standardize_kasta_size(val)
        status = "PASSED" if res == expected else "FAILED"
        print(f"  - Size: '{val}' -> Result: '{res}' (Expected: '{expected}') | {status}")
        if res != expected:
            failed_sizes += 1
            
    assert failed_sizes == 0, f"[-] {failed_sizes} size tests FAILED!"
    
    # ── Тести характеристик товару 1 ──
    prod_1 = {
        "name_ua": "Термоштани дитячі Sevim",
        "description": "Чудові дитячі кальсони з бавовни та еластану",
        "options": [
            {"OptionName": "Сезон", "ValueName": "осінь весна зима"},
            {"OptionName": "Візерунок", "ValueName": "абстракція"},
            {"OptionName": "Склад матеріалу", "ValueName": "62 % Termal, 35 % вискоза, 3 % еластан"},
            {"OptionName": "Застібка", "ValueName": "без застібки"},
            {"OptionName": "Декорування", "ValueName": "з аплікацією"},
            {"OptionName": "Зріст", "ValueName": "76 см"}  # Додаємо зріст для перевірки мапінгу
        ]
    }
    
    res_1 = export.standardize_kasta_characteristics(prod_1, cfg)
    
    # Валідація товару 1
    names_1 = [opt["name"] for opt in res_1]
    vals_1 = [opt["value"] for opt in res_1]
    
    assert "Сезонність" in names_1
    assert vals_1[names_1.index("Сезонність")] == "Демісезон"
    assert "Візерунок" in names_1
    assert vals_1[names_1.index("Візерунок")] == "Абстрактний"
    assert names_1.count("Матеріал") == 2
    assert "Віскоза" in vals_1
    assert "Еластан" in vals_1
    assert "Застібка" in names_1
    assert vals_1[names_1.index("Застібка")] == "Без застібки"
    assert "Декор" in names_1
    assert vals_1[names_1.index("Декор")] == "Аплікація"
    
    # ── Тести кольорів з комами ──
    color_cfg = {"allowed_colors": ["блакитний", "кремовий", "індиго", "комбінований"], "color_map": {}}
    assert export.standardize_kasta_color("блакитний,кремовий", color_cfg) == "блакитний"
    assert export.standardize_kasta_color("рожевий, фіолетовий", color_cfg) == "комбінований"
    
    # ── Тести санітара описів (від російських літер) ──
    raw_desc = "Набор детской одежды с рисунком животных"
    clean_desc = export.sanitize_ukrainian_description(raw_desc, "Ковдра")
    assert "ы" not in clean_desc and "э" not in clean_desc and "ъ" not in clean_desc
    assert "набір" in clean_desc or "дитячого" in clean_desc or clean_desc == "Ковдра"
    
    # ── Тести уточнення назв категорій ──
    cat_map = {8145: {"categoryID": 8145, "name": "Боді", "parentID": 8138}, 8138: {"categoryID": 8138, "name": "Одяг", "parentID": 7456}}
    enhanced = export.enhance_kasta_cat_name(8145, "Боді", cat_map)
    assert enhanced == "Боді для малюків"
    
    # ── Тести розпізнавання статі з назви/опису ──
    assert export.detect_gender({"name": "Боді для дівчинки Sevim", "options": []}) == "girl"
    assert export.detect_gender({"name": "Кофта для хлопчика з капюшоном", "options": []}) == "boy"
    
    print("\n[+] All characteristics, size, color, description, category name, and gender fallback tests PASSED successfully!")

if __name__ == "__main__":
    test_char_mapping()
