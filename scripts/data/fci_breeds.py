"""
Справочник пород FCI для seed-скрипта.

Источники:
- FCI Nomenclature of Breeds (https://www.fci.be/en/nomenclature/)
- РКФ — национальные породы, не признанные FCI, помечены fci_number=None.

Формат записи: (group_number, code, name_ru, name_en, fci_number)

- group_number — номер группы FCI (1..10).
- code         — машинный slug (kebab-case), уникальный в пределах
                 animal_type 'dog'. Не зависит от языка.
- name_ru      — русское название (как обычно используется в РКФ-кругах).
- name_en      — английское название по номенклатуре FCI (для пород вне
                 FCI — общепринятое английское). Отдаётся фронту при
                 Accept-Language: en (спека 2026-06-11).
- fci_number   — официальный номер стандарта FCI как строка.
                 None — порода не признана FCI (национальная,
                 экспериментальная, либо пока ожидает признания).
                 Строка может содержать буквы ("122a"): в FCI бывают
                 разделённые стандарты.

Решения:
- Список курируется вручную. Это компромисс: полный реестр FCI
  обновляется регулярно (~370 пород, новые признания каждый год),
  но для запуска платформы 250+ пород — это покрытие почти всех
  выставочных пород РКФ. Дальнейшее пополнение — через админку.
- Разделение бельгийской овчарки на 4 типа (малинуа, тервюрен,
  грюнендаль, лакенуа) — у FCI это один стандарт №15, но в РКФ
  они обычно выставляются как 4 отдельные породы. Делаем как в РКФ.
- Такса — формально один стандарт FCI №148, но РКФ ведёт 9 разновидностей
  (3 размера × 3 типа шерсти). Указываем разновидности отдельно.
- Если код или название добавляется со временем — добавляем в конец
  соответствующей группы, чтобы git diff был чище. Сортировка
  по алфавиту русского названия — не цель.
- name_en храним в ASCII (без диакритики: Vendeen, а не Vendéen) —
  меньше сюрпризов в поиске и URL; диакритику можно добавить позже
  отдельной правкой данных.

Идемпотентность seed-скрипта (см. _get_or_create) гарантирует, что
повторный запуск после добавления пород не дублирует существующие,
а недостающие name_en дозаполняет (upsert перевода).
"""

from __future__ import annotations


# Алиас типа: одна строка справочника пород. Используем во всех групповых
# списках — без этой явной аннотации pyright выводит "list[tuple[..., str]]"
# для групп, где все fci_number заполнены, и потом не разрешает конкат
# с группами, где есть None (list-параметр инвариантен в системе типов).
BreedRow = tuple[int, str, str, str, str | None]


# Группа 1: Овчарки и скотогонные собаки (кроме швейцарских пастушьих)
GROUP_1_SHEEPDOGS: list[BreedRow] = [
    (1, "australian-cattle-dog", "Австралийская пастушья собака", "Australian Cattle Dog", "287"),
    (1, "australian-kelpie", "Австралийский келпи", "Australian Kelpie", "293"),
    (1, "australian-shepherd", "Австралийская овчарка", "Australian Shepherd", "342"),
    (1, "bearded-collie", "Бородатый колли", "Bearded Collie", "271"),
    (1, "beauceron", "Босерон", "Beauceron", "44"),
    (1, "belgian-malinois", "Бельгийская овчарка малинуа", "Belgian Shepherd Dog (Malinois)", "15"),
    (1, "belgian-tervueren", "Бельгийская овчарка тервюрен", "Belgian Shepherd Dog (Tervueren)", "15"),
    (1, "belgian-groenendael", "Бельгийская овчарка грюнендаль", "Belgian Shepherd Dog (Groenendael)", "15"),
    (1, "belgian-laekenois", "Бельгийская овчарка лакенуа", "Belgian Shepherd Dog (Laekenois)", "15"),
    (1, "bergamasco", "Бергамская овчарка", "Bergamasco Shepherd Dog", "194"),
    (1, "berger-picard", "Пикардийская овчарка", "Picardy Sheepdog (Berger Picard)", "176"),
    (1, "border-collie", "Бордер-колли", "Border Collie", "297"),
    (1, "bouvier-des-flandres", "Фландрский бувье", "Bouvier des Flandres", "191"),
    (1, "bouvier-des-ardennes", "Арденнский бувье", "Bouvier des Ardennes", "171"),
    (1, "briard", "Бриар", "Briard", "113"),
    (1, "catalan-sheepdog", "Каталонская овчарка", "Catalan Sheepdog", "87"),
    (1, "collie-rough", "Колли длинношёрстный", "Collie Rough", "156"),
    (1, "collie-smooth", "Колли короткошёрстный", "Collie Smooth", "296"),
    (1, "croatian-sheepdog", "Хорватская овчарка", "Croatian Shepherd Dog", "277"),
    (1, "dutch-shepherd", "Голландская овчарка", "Dutch Shepherd Dog", "223"),
    (1, "german-shepherd", "Немецкая овчарка", "German Shepherd Dog", "166"),
    (1, "komondor", "Командор", "Komondor", "53"),
    (1, "kuvasz", "Кувас", "Kuvasz", "54"),
    (1, "mudi", "Муди", "Mudi", "238"),
    (1, "old-english-sheepdog", "Бобтейл (староанглийская овчарка)", "Old English Sheepdog (Bobtail)", "16"),
    (1, "polish-lowland-sheepdog", "Польская низинная овчарка (PON)", "Polish Lowland Sheepdog (PON)", "251"),
    (1, "polish-tatra-sheepdog", "Польская подгалянская овчарка", "Tatra Shepherd Dog", "252"),
    (1, "portuguese-sheepdog", "Португальская овчарка", "Portuguese Sheepdog", "93"),
    (1, "puli", "Пули", "Puli", "55"),
    (1, "pumi", "Пуми", "Pumi", "56"),
    (1, "pyrenean-shepherd", "Пиренейская овчарка", "Pyrenean Sheepdog", "138"),
    (1, "saarloos-wolfdog", "Саарлоосская волчья собака", "Saarloos Wolfdog", "311"),
    (1, "schapendoes", "Схапендус", "Dutch Schapendoes", "313"),
    (1, "shetland-sheepdog", "Шелти", "Shetland Sheepdog", "88"),
    (1, "slovak-cuvac", "Словацкий чувач", "Slovakian Chuvach", "142"),
    (1, "south-russian-shepherd", "Южнорусская овчарка", "South Russian Shepherd Dog", "326"),
    (1, "spanish-water-dog", "Испанская водяная собака", "Spanish Water Dog", "336"),
    (1, "white-swiss-shepherd", "Белая швейцарская овчарка", "White Swiss Shepherd Dog", "347"),
    (1, "czechoslovakian-wolfdog", "Чехословацкая волчья собака", "Czechoslovakian Wolfdog", "332"),
    (1, "majorca-shepherd", "Майоркская овчарка", "Majorca Shepherd Dog", "321"),
]


# Группа 2: Пинчеры и шнауцеры, молоссы, швейцарские пастушьи
GROUP_2_PINSCHERS_MOLOSSERS: list[BreedRow] = [
    (2, "affenpinscher", "Аффенпинчер", "Affenpinscher", "186"),
    (2, "anatolian-shepherd", "Анатолийская овчарка (кангал)", "Anatolian Shepherd Dog", "331"),
    (2, "appenzeller-sennenhund", "Аппенцеллер зенненхунд", "Appenzell Cattle Dog", "46"),
    (2, "atlas-mountain-dog", "Аиди (атласская овчарка)", "Atlas Mountain Dog (Aidi)", "247"),
    (2, "bernese-mountain-dog", "Бернский зенненхунд", "Bernese Mountain Dog", "45"),
    (2, "boerboel", "Бурбуль (южноафриканский)", "Boerboel", "365"),
    (2, "bordeaux-dog", "Бордоский дог", "Dogue de Bordeaux", "116"),
    (2, "boxer", "Боксёр", "German Boxer", "144"),
    (2, "broholmer", "Бролхольмер", "Broholmer", "315"),
    (2, "bullmastiff", "Бульмастиф", "Bullmastiff", "157"),
    (2, "cane-corso", "Кане-корсо", "Italian Cane Corso", "343"),
    (2, "caucasian-shepherd", "Кавказская овчарка", "Caucasian Shepherd Dog", "328"),
    (2, "central-asian-shepherd", "Среднеазиатская овчарка (алабай)", "Central Asia Shepherd Dog", "335"),
    (2, "doberman", "Доберман", "Dobermann", "143"),
    (2, "dogo-argentino", "Аргентинский дог", "Dogo Argentino", "292"),
    (2, "dogo-canario", "Канарский дог (пресо канарио)", "Presa Canario (Dogo Canario)", "346"),
    (2, "english-mastiff", "Английский мастиф", "Mastiff", "264"),
    (2, "entlebucher-sennenhund", "Энтлебухер зенненхунд", "Entlebuch Cattle Dog", "47"),
    (2, "estrela-mountain-dog", "Эштрельская овчарка", "Estrela Mountain Dog", "173"),
    (2, "fila-brasileiro", "Фила бразилейро", "Fila Brasileiro", "225"),
    (2, "german-pinscher", "Немецкий пинчер", "German Pinscher", "184"),
    (2, "great-dane", "Немецкий дог", "Great Dane", "235"),
    (2, "great-pyrenees", "Пиренейская горная собака", "Pyrenean Mountain Dog", "137"),
    (2, "greater-swiss-mountain-dog", "Большой швейцарский зенненхунд", "Great Swiss Mountain Dog", "58"),
    (2, "hovawart", "Ховаварт", "Hovawart", "190"),
    (2, "kangal", "Кангал", "Kangal Shepherd Dog", "331"),
    (2, "karst-shepherd", "Крашская овчарка", "Karst Shepherd Dog", "278"),
    (2, "landseer", "Ландсир", "Landseer", "226"),
    (2, "leonberger", "Леонбергер", "Leonberger", "145"),
    (2, "majorca-mastiff", "Майоркский мастиф (ка-де-бо)", "Majorca Mastiff (Ca de Bou)", "249"),
    (2, "miniature-pinscher", "Цвергпинчер", "Miniature Pinscher", "185"),
    (2, "moscow-watchdog", "Московская сторожевая", "Moscow Watchdog", None),
    (2, "neapolitan-mastiff", "Неаполитанский мастиф", "Neapolitan Mastiff", "197"),
    (2, "newfoundland", "Ньюфаундленд", "Newfoundland", "50"),
    (2, "pyrenean-mastiff", "Пиренейский мастиф", "Pyrenean Mastiff", "92"),
    (2, "rafeiro-do-alentejo", "Рафейру до Алентежу", "Rafeiro do Alentejo", "96"),
    (2, "rottweiler", "Ротвейлер", "Rottweiler", "147"),
    (2, "russian-black-terrier", "Русский чёрный терьер", "Russian Black Terrier", "327"),
    (2, "saint-bernard", "Сенбернар", "St. Bernard", "61"),
    (2, "sarplaninac", "Шарпланинская овчарка", "Sarplaninac", "41"),
    (2, "schnauzer-giant", "Ризеншнауцер", "Giant Schnauzer", "181"),
    (2, "schnauzer-standard", "Миттельшнауцер", "Standard Schnauzer", "182"),
    (2, "schnauzer-miniature", "Цвергшнауцер", "Miniature Schnauzer", "183"),
    (2, "shar-pei", "Шарпей", "Shar Pei", "309"),
    (2, "spanish-mastiff", "Испанский мастиф", "Spanish Mastiff", "91"),
    (2, "tibetan-mastiff", "Тибетский мастиф", "Tibetan Mastiff", "230"),
    (2, "tornjak", "Торняк", "Tornjak", "355"),
    (2, "tosa-inu", "Тоса-ину", "Tosa Inu", "260"),
]


# Группа 3: Терьеры
GROUP_3_TERRIERS: list[BreedRow] = [
    (3, "airedale-terrier", "Эрдельтерьер", "Airedale Terrier", "7"),
    (3, "american-staffordshire-terrier", "Американский стаффордширский терьер", "American Staffordshire Terrier", "286"),
    (3, "australian-terrier", "Австралийский терьер", "Australian Terrier", "8"),
    (3, "bedlington-terrier", "Бедлингтон-терьер", "Bedlington Terrier", "9"),
    (3, "border-terrier", "Бордер-терьер", "Border Terrier", "10"),
    (3, "bull-terrier", "Бультерьер", "Bull Terrier", "11"),
    (3, "bull-terrier-miniature", "Миниатюрный бультерьер", "Miniature Bull Terrier", "359"),
    (3, "cairn-terrier", "Керн-терьер", "Cairn Terrier", "4"),
    (3, "cesky-terrier", "Чешский терьер", "Cesky Terrier", "246"),
    (3, "dandie-dinmont-terrier", "Денди-динмонт-терьер", "Dandie Dinmont Terrier", "168"),
    (3, "english-toy-terrier", "Английский той-терьер", "English Toy Terrier (Black and Tan)", "13"),
    (3, "fox-terrier-smooth", "Гладкошёрстный фокстерьер", "Fox Terrier Smooth", "12"),
    (3, "fox-terrier-wire", "Жесткошёрстный фокстерьер", "Fox Terrier Wire", "169"),
    (3, "german-hunting-terrier", "Немецкий ягдтерьер", "German Hunting Terrier", "103"),
    (3, "glen-of-imaal-terrier", "Глен оф Имаал терьер", "Irish Glen of Imaal Terrier", "302"),
    (3, "irish-soft-coated-wheaten-terrier", "Мягкошёрстный пшеничный терьер", "Irish Soft Coated Wheaten Terrier", "40"),
    (3, "irish-terrier", "Ирландский терьер", "Irish Terrier", "139"),
    (3, "jack-russell-terrier", "Джек-рассел-терьер", "Jack Russell Terrier", "345"),
    (3, "japanese-terrier", "Японский терьер", "Japanese Terrier", "259"),
    (3, "kerry-blue-terrier", "Керри-блю-терьер", "Kerry Blue Terrier", "3"),
    (3, "lakeland-terrier", "Лейкленд-терьер", "Lakeland Terrier", "70"),
    (3, "manchester-terrier", "Манчестер-терьер", "Manchester Terrier", "71"),
    (3, "norfolk-terrier", "Норфолк-терьер", "Norfolk Terrier", "272"),
    (3, "norwich-terrier", "Норвич-терьер", "Norwich Terrier", "72"),
    (3, "parson-russell-terrier", "Парсон-рассел-терьер", "Parson Russell Terrier", "339"),
    (3, "scottish-terrier", "Скотчтерьер", "Scottish Terrier", "73"),
    (3, "sealyham-terrier", "Силихем-терьер", "Sealyham Terrier", "74"),
    (3, "skye-terrier", "Скай-терьер", "Skye Terrier", "75"),
    (3, "staffordshire-bull-terrier", "Стаффордширский бультерьер", "Staffordshire Bull Terrier", "76"),
    (3, "welsh-terrier", "Вельштерьер", "Welsh Terrier", "78"),
    (3, "west-highland-white-terrier", "Вест-хайленд-уайт-терьер", "West Highland White Terrier", "85"),
    (3, "yorkshire-terrier", "Йоркширский терьер", "Yorkshire Terrier", "86"),
    (3, "biewer-terrier", "Бивер-йоркширский терьер", "Biewer Terrier", None),
    (3, "russian-toy", "Русский той", "Russian Toy", "352"),
]


# Группа 4: Таксы (формально 1 стандарт FCI №148, но 9 разновидностей по
# типу шерсти и размеру — заводят как отдельные породы в РКФ).
GROUP_4_DACHSHUNDS: list[BreedRow] = [
    (4, "dachshund-standard-smooth", "Такса стандартная гладкошёрстная", "Dachshund Standard Smooth-haired", "148"),
    (4, "dachshund-standard-longhaired", "Такса стандартная длинношёрстная", "Dachshund Standard Long-haired", "148"),
    (4, "dachshund-standard-wirehaired", "Такса стандартная жесткошёрстная", "Dachshund Standard Wire-haired", "148"),
    (4, "dachshund-miniature-smooth", "Такса миниатюрная гладкошёрстная", "Dachshund Miniature Smooth-haired", "148"),
    (4, "dachshund-miniature-longhaired", "Такса миниатюрная длинношёрстная", "Dachshund Miniature Long-haired", "148"),
    (4, "dachshund-miniature-wirehaired", "Такса миниатюрная жесткошёрстная", "Dachshund Miniature Wire-haired", "148"),
    (4, "dachshund-rabbit-smooth", "Такса кроличья гладкошёрстная", "Dachshund Rabbit Smooth-haired", "148"),
    (4, "dachshund-rabbit-longhaired", "Такса кроличья длинношёрстная", "Dachshund Rabbit Long-haired", "148"),
    (4, "dachshund-rabbit-wirehaired", "Такса кроличья жесткошёрстная", "Dachshund Rabbit Wire-haired", "148"),
]


# Группа 5: Шпицы и примитивные
GROUP_5_SPITZ_PRIMITIVE: list[BreedRow] = [
    (5, "akita", "Акита-ину", "Akita", "255"),
    (5, "alaskan-malamute", "Аляскинский маламут", "Alaskan Malamute", "243"),
    (5, "american-akita", "Американская акита", "American Akita", "344"),
    (5, "basenji", "Басенджи", "Basenji", "43"),
    (5, "canaan-dog", "Ханаанская собака", "Canaan Dog", "273"),
    (5, "chinook", "Чинук", "Chinook", "362"),
    (5, "chow-chow", "Чау-чау", "Chow Chow", "205"),
    (5, "cirneco-dell-etna", "Сицилийская борзая", "Cirneco dell'Etna", "199"),
    (5, "eurasier", "Евразиер", "Eurasier", "291"),
    (5, "finnish-lapphund", "Финский лаппхунд", "Finnish Lapphund", "189"),
    (5, "finnish-spitz", "Финский шпиц", "Finnish Spitz", "49"),
    (5, "german-spitz-keeshond", "Немецкий вольфшпиц (кеесхонд)", "German Wolfspitz (Keeshond)", "97"),
    (5, "german-spitz-large", "Немецкий большой шпиц", "German Spitz Large (Grossspitz)", "97"),
    (5, "german-spitz-medium", "Немецкий средний шпиц", "German Spitz Medium (Mittelspitz)", "97"),
    (5, "german-spitz-miniature", "Немецкий малый шпиц", "German Spitz Small (Kleinspitz)", "97"),
    (5, "german-spitz-pomeranian", "Немецкий миниатюрный шпиц (померанский)", "Pomeranian (Zwergspitz)", "97"),
    (5, "greenland-dog", "Гренландская собака", "Greenland Dog", "274"),
    (5, "hokkaido", "Хоккайдо", "Hokkaido", "261"),
    (5, "ibizan-hound", "Поденко ибиценко", "Ibizan Podenco", "89"),
    (5, "icelandic-sheepdog", "Исландская собака", "Icelandic Sheepdog", "289"),
    (5, "japanese-spitz", "Японский шпиц", "Japanese Spitz", "262"),
    (5, "jindo", "Корейский чиндо", "Korea Jindo Dog", "334"),
    (5, "kai", "Кай-кэн", "Kai", "317"),
    (5, "karelian-bear-dog", "Карельская медвежья собака", "Karelian Bear Dog", "48"),
    (5, "kishu", "Кисю", "Kishu", "318"),
    (5, "korean-jindo", "Корейский чиндо", "Korea Jindo Dog", "334"),
    (5, "lapponian-herder", "Лапландская оленегонная собака", "Lapponian Herder", "284"),
    (5, "mexican-hairless-xolo-standard", "Ксолоитцкуинтли стандартный", "Xoloitzcuintle Standard", "234"),
    (5, "mexican-hairless-xolo-miniature", "Ксолоитцкуинтли миниатюрный", "Xoloitzcuintle Miniature", "234"),
    (5, "mexican-hairless-xolo-intermediate", "Ксолоитцкуинтли промежуточный", "Xoloitzcuintle Intermediate", "234"),
    (5, "norwegian-buhund", "Норвежский бухунд", "Norwegian Buhund", "237"),
    (5, "norwegian-elkhound-black", "Норвежский лосиная лайка чёрная", "Norwegian Elkhound Black", "242"),
    (5, "norwegian-elkhound-grey", "Норвежский лосиная лайка серая", "Norwegian Elkhound Grey", "97"),
    (5, "norwegian-lundehund", "Норвежский лундехунд", "Norwegian Lundehund", "265"),
    (5, "peruvian-inca-orchid", "Перуанская голая собака", "Peruvian Hairless Dog", "310"),
    (5, "pharaoh-hound", "Фараонова собака", "Pharaoh Hound", "248"),
    (5, "podengo-portugues", "Португальский поденгу", "Portuguese Podengo", "94"),
    (5, "russian-european-laika", "Русско-европейская лайка", "Russian-European Laika", "304"),
    (5, "samoyed", "Самоедская собака", "Samoyed", "212"),
    (5, "shiba-inu", "Сиба-ину", "Shiba", "257"),
    (5, "shikoku", "Сикоку", "Shikoku", "319"),
    (5, "siberian-husky", "Сибирский хаски", "Siberian Husky", "270"),
    (5, "swedish-lapphund", "Шведский лаппхунд", "Swedish Lapphund", "135"),
    (5, "swedish-vallhund", "Шведский вальхунд", "Swedish Vallhund", "14"),
    (5, "thai-bangkaew", "Тайский бангкеу", "Thai Bangkaew Dog", "358"),
    (5, "thai-ridgeback", "Тайский риджбек", "Thai Ridgeback Dog", "338"),
    (5, "volpino-italiano", "Итальянский вольпино", "Italian Volpino", "195"),
    (5, "west-siberian-laika", "Западносибирская лайка", "West Siberian Laika", "306"),
    (5, "yakutian-laika", "Якутская лайка", "Yakutian Laika", "365"),
    (5, "east-siberian-laika", "Восточносибирская лайка", "East Siberian Laika", "305"),
]


# Группа 6: Гончие и ищейки
GROUP_6_SCENTHOUNDS: list[BreedRow] = [
    (6, "alpine-dachsbracke", "Альпийский таксообразный бракк", "Alpine Dachsbracke", "254"),
    (6, "american-foxhound", "Американский фоксхаунд", "American Foxhound", "303"),
    (6, "ariegeois", "Арьежский бракк", "Ariegeois", "20"),
    (6, "artois-hound", "Артуанская гончая", "Artois Hound", "28"),
    (6, "basset-artesien-normand", "Артезиано-нормандский бассет", "Norman Artesian Basset", "34"),
    (6, "basset-bleu-de-gascogne", "Голубой гасконский бассет", "Blue Gascony Basset", "35"),
    (6, "basset-fauve-de-bretagne", "Бретонский палевый бассет", "Fawn Brittany Basset", "36"),
    (6, "basset-hound", "Бассет-хаунд", "Basset Hound", "163"),
    (6, "bavarian-mountain-hound", "Баварская горная гончая", "Bavarian Mountain Scent Hound", "217"),
    (6, "beagle", "Бигль", "Beagle", "161"),
    (6, "beagle-harrier", "Бигль-харьер", "Beagle Harrier", "290"),
    (6, "billy", "Бильи", "Billy", "25"),
    (6, "black-and-tan-coonhound", "Чёрно-подпалый кунхаунд", "Black and Tan Coonhound", "300"),
    (6, "bloodhound", "Бладхаунд (сен-юбер)", "Bloodhound", "84"),
    (6, "blue-gascony-griffon", "Голубой гасконский гриффон", "Blue Gascony Griffon", "32"),
    (6, "bosnian-coarse-haired-hound", "Боснийская грубошёрстная гончая", "Bosnian Coarse-haired Hound (Barak)", "155"),
    (6, "briquet-griffon-vendeen", "Бракк-гриффон вандейский", "Briquet Griffon Vendeen", "19"),
    (6, "chien-de-saint-hubert", "Сенбернарская гончая", "Chien de Saint-Hubert", "84"),
    (6, "dachsbracke", "Таксообразный бракк", "Dachsbracke", "254"),
    (6, "dalmatian", "Далматин", "Dalmatian", "153"),
    (6, "deutsche-bracke", "Немецкая гончая", "German Hound (Deutsche Bracke)", "299"),
    (6, "drever", "Древер", "Drever", "130"),
    (6, "dunker", "Норвежская гончая (дункер)", "Norwegian Hound (Dunker)", "203"),
    (6, "english-foxhound", "Английский фоксхаунд", "English Foxhound", "159"),
    (6, "finnish-hound", "Финская гончая", "Finnish Hound", "51"),
    (6, "grand-anglo-francais", "Большой англо-французский гончий", "Great Anglo-French Hound", "21"),
    (6, "grand-bleu-de-gascogne", "Большая голубая гасконская гончая", "Great Gascony Blue", "22"),
    (6, "grand-griffon-vendeen", "Большой вандейский гриффон", "Grand Griffon Vendeen", "282"),
    (6, "griffon-fauve-de-bretagne", "Бретонский палевый гриффон", "Fawn Brittany Griffon", "66"),
    (6, "griffon-nivernais", "Ниверне-гриффон", "Griffon Nivernais", "17"),
    (6, "hamiltonstovare", "Гончая Гамильтона", "Hamiltonstovare", "132"),
    (6, "hanoverian-scenthound", "Ганноверская гончая", "Hanoverian Scent Hound", "213"),
    (6, "harrier", "Харьер", "Harrier", "295"),
    (6, "hellenic-hound", "Эллинская гончая", "Hellenic Hound", "214"),
    (6, "hygenhund", "Хюген (гончая Хюгена)", "Hygen Hound", "266"),
    (6, "istrian-coarse-haired-hound", "Истринская жесткошёрстная гончая", "Istrian Coarse-haired Hound", "152"),
    (6, "istrian-shorthaired-hound", "Истринская гладкошёрстная гончая", "Istrian Short-haired Hound", "151"),
    (6, "italian-segugio-shorthaired", "Итальянский сегуджо гладкошёрстный", "Italian Short-haired Segugio", "337"),
    (6, "italian-segugio-wirehaired", "Итальянский сегуджо жесткошёрстный", "Italian Coarse-haired Segugio", "198"),
    (6, "montenegrin-mountain-hound", "Черногорская гончая", "Montenegrin Mountain Hound", "275"),
    (6, "norwegian-elkhound", "Норвежский эльгхунд", "Norwegian Elkhound", "242"),
    (6, "otterhound", "Оттерхаунд", "Otterhound", "294"),
    (6, "petit-basset-griffon-vendeen", "Малый вандейский бассет-гриффон", "Petit Basset Griffon Vendeen", "67"),
    (6, "petit-bleu-de-gascogne", "Малая голубая гасконская гончая", "Small Blue Gascony", "31"),
    (6, "poitevin", "Пуатвинская гончая", "Poitevin", "24"),
    (6, "polish-hound", "Польская гончая", "Polish Hound", "52"),
    (6, "polish-hunting-dog", "Польский огар", "Polish Hunting Dog", "354"),
    (6, "porcelaine", "Порселен", "Porcelaine", "30"),
    (6, "posavac-hound", "Посавская гончая", "Posavac Hound", "154"),
    (6, "redbone-coonhound", "Рыжий кунхаунд", "Redbone Coonhound", "366"),
    (6, "rhodesian-ridgeback", "Родезийский риджбек", "Rhodesian Ridgeback", "146"),
    (6, "russian-hound", "Русская гончая", "Russian Hound", None),
    (6, "russian-piebald-hound", "Русская пегая гончая", "Russian Piebald Hound", None),
    (6, "schillerstovare", "Гончая Шиллера", "Schillerstovare", "131"),
    (6, "schweizer-laufhund", "Швейцарская гончая", "Schweizer Laufhund", "59"),
    (6, "serbian-hound", "Сербская гончая", "Serbian Hound", "150"),
    (6, "serbian-tricolour-hound", "Сербская трёхцветная гончая", "Serbian Tricolour Hound", "229"),
    (6, "slovak-hound", "Словацкий копов", "Slovakian Hound (Kopov)", "244"),
    (6, "small-griffon-vendeen", "Малый вандейский гриффон", "Small Griffon Vendeen", "19"),
    (6, "smalandsstovare", "Смоландская гончая", "Smalandsstovare", "129"),
    (6, "spanish-hound", "Испанский сабуэсо", "Spanish Hound (Sabueso Espanol)", "204"),
    (6, "styrian-coarse-haired-hound", "Штирийский жесткошёрстный бракк", "Styrian Coarse-haired Hound", "62"),
    (6, "swiss-hound", "Швейцарский лауфхунд", "Swiss Hound", "59"),
    (6, "transylvanian-hound", "Трансильванская гончая", "Transylvanian Hound", "241"),
    (6, "tyrolean-hound", "Тирольский бракк", "Tyrolean Hound", "68"),
    (6, "welsh-hound", "Уэльская гончая", "Welsh Hound", None),
    (6, "westphalian-dachsbracke", "Вестфальская гончая", "Westphalian Dachsbracke", "100"),
    (6, "yugoslavian-mountain-hound", "Югославская горная гончая", "Yugoslavian Mountain Hound", "279"),
]


# Группа 7: Легавые (pointing dogs)
GROUP_7_POINTERS: list[BreedRow] = [
    (7, "ariege-pointer", "Арьежский бракк", "Ariege Pointing Dog", "177"),
    (7, "auvergne-pointer", "Овернский бракк", "Auvergne Pointer (Braque d'Auvergne)", "180"),
    (7, "blue-picardy-spaniel", "Голубой пикардийский спаниель", "Blue Picardy Spaniel", "106"),
    (7, "bohemian-wirehaired-pointing-griffon", "Чешский фоусек", "Bohemian Wire-haired Pointing Griffon (Cesky Fousek)", "245"),
    (7, "bourbonnais-pointer", "Бракк дю Бурбонне", "Bourbonnais Pointing Dog", "179"),
    (7, "bracco-italiano", "Итальянский бракк", "Italian Pointing Dog (Bracco Italiano)", "202"),
    (7, "brittany-spaniel", "Бретонский эпаньоль", "Brittany Spaniel", "95"),
    (7, "burgos-pointer", "Бургосский бракк (Pachón)", "Burgos Pointing Dog", "90"),
    (7, "drentse-patrijshond", "Дрентская куропаточная собака", "Drentsche Partridge Dog", "224"),
    (7, "english-pointer", "Английский пойнтер", "English Pointer", "1"),
    (7, "english-setter", "Английский сеттер", "English Setter", "2"),
    (7, "epagneul-bleu-de-picardie", "Голубой пикардийский эпаньоль", "Epagneul Bleu de Picardie", "106"),
    (7, "french-pointer-gascogne", "Французский бракк гасконский", "French Pointing Dog (Gascogne type)", "133"),
    (7, "french-pointer-pyrenees", "Французский бракк пиренейский", "French Pointing Dog (Pyrenean type)", "134"),
    (7, "french-spaniel", "Французский эпаньоль", "French Spaniel", "175"),
    (7, "german-longhaired-pointer", "Немецкая длинношёрстная легавая", "German Long-haired Pointing Dog", "117"),
    (7, "german-pointer-shorthaired", "Курцхаар", "German Short-haired Pointing Dog (Kurzhaar)", "119"),
    (7, "german-pointer-wirehaired", "Дратхаар", "German Wire-haired Pointing Dog (Drahthaar)", "98"),
    (7, "gordon-setter", "Шотландский сеттер (гордон)", "Gordon Setter", "6"),
    (7, "hungarian-vizsla-shorthaired", "Венгерская выжла короткошёрстная", "Hungarian Short-haired Vizsla", "57"),
    (7, "hungarian-vizsla-wirehaired", "Венгерская выжла жесткошёрстная", "Hungarian Wire-haired Vizsla", "239"),
    (7, "irish-red-and-white-setter", "Ирландский красно-белый сеттер", "Irish Red and White Setter", "330"),
    (7, "irish-setter", "Ирландский сеттер", "Irish Red Setter", "120"),
    (7, "italian-spinone", "Итальянский спиноне", "Italian Spinone", "165"),
    (7, "korthals-griffon", "Жесткошёрстный гриффон (Кортальс)", "Wire-haired Pointing Griffon (Korthals)", "107"),
    (7, "large-munsterlander", "Большой мюнстерлендер", "Large Munsterlander", "118"),
    (7, "old-danish-pointer", "Старый датский пойнтер", "Old Danish Pointing Dog", "281"),
    (7, "picardy-spaniel", "Пикардийский эпаньоль", "Picardy Spaniel", "108"),
    (7, "pont-audemer-spaniel", "Эпаньоль Понт-Одемер", "Pont-Audemer Spaniel", "114"),
    (7, "portuguese-pointer", "Португальская легавая", "Portuguese Pointing Dog", "187"),
    (7, "pudelpointer", "Пудельпойнтер", "Pudelpointer", "216"),
    (7, "saint-germain-pointer", "Бракк Сен-Жермен", "Saint Germain Pointer", "115"),
    (7, "slovakian-rough-haired-pointer", "Словацкий жесткошёрстный пойнтер", "Slovakian Wire-haired Pointing Dog", "320"),
    (7, "small-munsterlander", "Малый мюнстерлендер", "Small Munsterlander", "102"),
    (7, "stabyhoun", "Стабихаун", "Stabyhoun", "222"),
    (7, "weimaraner-shorthaired", "Веймаранер короткошёрстный", "Weimaraner Short-haired", "99"),
    (7, "weimaraner-longhaired", "Веймаранер длинношёрстный", "Weimaraner Long-haired", "99"),
]


# Группа 8: Ретриверы, спаниели и водяные собаки
GROUP_8_RETRIEVERS_SPANIELS: list[BreedRow] = [
    (8, "american-cocker-spaniel", "Американский кокер-спаниель", "American Cocker Spaniel", "167"),
    (8, "american-water-spaniel", "Американский водяной спаниель", "American Water Spaniel", "301"),
    (8, "barbet", "Барбе", "Barbet (French Water Dog)", "105"),
    (8, "boykin-spaniel", "Спаниель Бойкина", "Boykin Spaniel", "368"),
    (8, "chesapeake-bay-retriever", "Чесапик-бей ретривер", "Chesapeake Bay Retriever", "263"),
    (8, "clumber-spaniel", "Кламбер-спаниель", "Clumber Spaniel", "109"),
    (8, "cocker-spaniel-english", "Английский кокер-спаниель", "English Cocker Spaniel", "5"),
    (8, "curly-coated-retriever", "Курчавошёрстный ретривер", "Curly Coated Retriever", "110"),
    (8, "english-springer-spaniel", "Английский спрингер-спаниель", "English Springer Spaniel", "125"),
    (8, "field-spaniel", "Филд-спаниель", "Field Spaniel", "123"),
    (8, "flat-coated-retriever", "Прямошёрстный ретривер", "Flat Coated Retriever", "121"),
    (8, "frisian-water-dog", "Фризская водяная собака (вэттерхаун)", "Frisian Water Dog (Wetterhoun)", "221"),
    (8, "golden-retriever", "Золотистый ретривер", "Golden Retriever", "111"),
    (8, "irish-water-spaniel", "Ирландский водяной спаниель", "Irish Water Spaniel", "124"),
    (8, "kooikerhondje", "Коикерхондье", "Nederlandse Kooikerhondje", "314"),
    (8, "labrador-retriever", "Лабрадор-ретривер", "Labrador Retriever", "122"),
    (8, "lagotto-romagnolo", "Лаготто-романьоло", "Lagotto Romagnolo", "298"),
    (8, "nova-scotia-duck-tolling-retriever", "Новошотландский толлер", "Nova Scotia Duck Tolling Retriever", "312"),
    (8, "portuguese-water-dog", "Португальская водяная собака", "Portuguese Water Dog", "37"),
    (8, "russian-spaniel", "Русский охотничий спаниель", "Russian Hunting Spaniel", None),
    (8, "sussex-spaniel", "Суссекс-спаниель", "Sussex Spaniel", "127"),
    (8, "welsh-springer-spaniel", "Вельш-спрингер-спаниель", "Welsh Springer Spaniel", "126"),
]


# Группа 9: Декоративные и собаки-компаньоны
GROUP_9_COMPANIONS: list[BreedRow] = [
    (9, "bichon-frise", "Бишон фризе", "Bichon Frise", "215"),
    (9, "bichon-havanais", "Гаванский бишон (хаванез)", "Havanese (Bichon Havanais)", "250"),
    (9, "bolognese", "Болоньез", "Bolognese", "196"),
    (9, "boston-terrier", "Бостон-терьер", "Boston Terrier", "140"),
    (9, "cavalier-king-charles-spaniel", "Кавалер-кинг-чарльз-спаниель", "Cavalier King Charles Spaniel", "136"),
    (9, "chihuahua-smoothhaired", "Чихуахуа гладкошёрстный", "Chihuahua Smooth-haired", "218"),
    (9, "chihuahua-longhaired", "Чихуахуа длинношёрстный", "Chihuahua Long-haired", "218"),
    (9, "chinese-crested-hairless", "Китайская хохлатая голая", "Chinese Crested Dog Hairless", "288"),
    (9, "chinese-crested-powderpuff", "Китайская хохлатая пуховая", "Chinese Crested Dog Powderpuff", "288"),
    (9, "coton-de-tulear", "Котон-де-тулеар", "Coton de Tulear", "283"),
    (9, "french-bulldog", "Французский бульдог", "French Bulldog", "101"),
    (9, "havanese", "Гаванский бишон (хаванез)", "Havanese", "250"),
    (9, "japanese-chin", "Японский хин", "Japanese Chin", "206"),
    (9, "king-charles-spaniel", "Кинг-чарльз-спаниель", "King Charles Spaniel", "128"),
    (9, "kromfohrlander", "Кромфорлендер", "Kromfohrlander", "192"),
    (9, "lhasa-apso", "Лхаса-апсо", "Lhasa Apso", "227"),
    (9, "lowchen", "Лёвхен (малая львиная собака)", "Lowchen (Little Lion Dog)", "233"),
    (9, "maltese", "Мальтезе (мальтийская болонка)", "Maltese", "65"),
    (9, "papillon", "Папильон (континентальный той-спаниель)", "Papillon", "77"),
    (9, "pekingese", "Пекинес", "Pekingese", "207"),
    (9, "petit-brabancon", "Малый брабансон", "Petit Brabancon", "82"),
    (9, "phalene", "Фален", "Phalene", "77"),
    (9, "poodle-standard", "Пудель большой (стандартный)", "Poodle Standard", "172"),
    (9, "poodle-medium", "Пудель средний", "Poodle Medium", "172"),
    (9, "poodle-miniature", "Пудель малый (миниатюрный)", "Poodle Miniature", "172"),
    (9, "poodle-toy", "Пудель той", "Poodle Toy", "172"),
    (9, "pug", "Мопс", "Pug", "253"),
    (9, "russian-toy-smoothhaired", "Русский той гладкошёрстный", "Russian Toy Smooth-haired", "352"),
    (9, "russian-toy-longhaired", "Русский той длинношёрстный", "Russian Toy Long-haired", "352"),
    (9, "shih-tzu", "Ши-тцу", "Shih Tzu", "208"),
    (9, "small-brabant-griffon", "Малый брабансон (пти-брабансон)", "Small Brabant Griffon", "82"),
    (9, "tibetan-spaniel", "Тибетский спаниель", "Tibetan Spaniel", "231"),
    (9, "tibetan-terrier", "Тибетский терьер", "Tibetan Terrier", "209"),
    (9, "english-bulldog", "Английский бульдог", "English Bulldog", "149"),
    (9, "brussels-griffon", "Брюссельский гриффон", "Griffon Bruxellois", "80"),
    (9, "belgian-griffon", "Бельгийский гриффон", "Griffon Belge", "81"),
    (9, "continental-toy-spaniel", "Континентальный той-спаниель", "Continental Toy Spaniel", "77"),
]


# Группа 10: Борзые
GROUP_10_SIGHTHOUNDS: list[BreedRow] = [
    (10, "afghan-hound", "Афганская борзая", "Afghan Hound", "228"),
    (10, "azawakh", "Азавак", "Azawakh", "307"),
    (10, "borzoi", "Русская псовая борзая", "Borzoi (Russian Hunting Sighthound)", "193"),
    (10, "chart-polski", "Польский хорт (хорт польски)", "Polish Greyhound (Chart Polski)", "333"),
    (10, "deerhound", "Шотландская оленья борзая (дирхаунд)", "Deerhound", "164"),
    (10, "galgo-espanol", "Галго эспаньол", "Spanish Greyhound (Galgo Espanol)", "285"),
    (10, "greyhound", "Грейхаунд", "Greyhound", "158"),
    (10, "hungarian-greyhound", "Венгерская борзая (мадьяр агар)", "Hungarian Greyhound (Magyar Agar)", "240"),
    (10, "irish-wolfhound", "Ирландский волкодав", "Irish Wolfhound", "160"),
    (10, "italian-greyhound", "Итальянский левретто", "Italian Greyhound", "200"),
    (10, "magyar-agar", "Мадьяр агар (венгерская борзая)", "Magyar Agar", "240"),
    (10, "saluki", "Салюки (персидская борзая)", "Saluki", "269"),
    (10, "sloughi", "Слюги (арабская борзая)", "Sloughi (Arabian Greyhound)", "188"),
    (10, "south-russian-steppe-hound", "Южнорусская степная борзая", "South Russian Steppe Hound", None),
    (10, "taigan", "Тайган (киргизская борзая)", "Taigan (Kyrgyz Sighthound)", None),
    (10, "whippet", "Уиппет", "Whippet", "162"),
    (10, "russian-hortaya-borzaya", "Хортая борзая", "Hortaya Borzaya", None),
]


# Сводный список — экспортируется наружу. Порядок групп от 1 до 10
# сохраняется для предсказуемого вывода в логах seed-скрипта.
FCI_BREEDS: list[BreedRow] = (
    GROUP_1_SHEEPDOGS
    + GROUP_2_PINSCHERS_MOLOSSERS
    + GROUP_3_TERRIERS
    + GROUP_4_DACHSHUNDS
    + GROUP_5_SPITZ_PRIMITIVE
    + GROUP_6_SCENTHOUNDS
    + GROUP_7_POINTERS
    + GROUP_8_RETRIEVERS_SPANIELS
    + GROUP_9_COMPANIONS
    + GROUP_10_SIGHTHOUNDS
)
