"""
Справочник пород FCI для seed-скрипта.

Источники:
- FCI Nomenclature of Breeds (https://www.fci.be/en/nomenclature/)
- РКФ — национальные породы, не признанные FCI, помечены fci_number=None.

Формат записи: (group_number, code, name_ru, fci_number)

- group_number — номер группы FCI (1..10).
- code         — машинный slug (kebab-case), уникальный в пределах
                 animal_type 'dog'. Не зависит от языка.
- name_ru      — русское название (как обычно используется в РКФ-кругах).
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

Идемпотентность seed-скрипта (см. _get_or_create) гарантирует, что
повторный запуск после добавления пород не дублирует существующие.
"""

from __future__ import annotations


# Алиас типа: одна строка справочника пород. Используем во всех групповых
# списках — без этой явной аннотации pyright выводит "list[tuple[..., str]]"
# для групп, где все fci_number заполнены, и потом не разрешает конкат
# с группами, где есть None (list-параметр инвариантен в системе типов).
BreedRow = tuple[int, str, str, str | None]


# Группа 1: Овчарки и скотогонные собаки (кроме швейцарских пастушьих)
GROUP_1_SHEEPDOGS: list[BreedRow] = [
    (1, "australian-cattle-dog", "Австралийская пастушья собака", "287"),
    (1, "australian-kelpie", "Австралийский келпи", "293"),
    (1, "australian-shepherd", "Австралийская овчарка", "342"),
    (1, "bearded-collie", "Бородатый колли", "271"),
    (1, "beauceron", "Босерон", "44"),
    (1, "belgian-malinois", "Бельгийская овчарка малинуа", "15"),
    (1, "belgian-tervueren", "Бельгийская овчарка тервюрен", "15"),
    (1, "belgian-groenendael", "Бельгийская овчарка грюнендаль", "15"),
    (1, "belgian-laekenois", "Бельгийская овчарка лакенуа", "15"),
    (1, "bergamasco", "Бергамская овчарка", "194"),
    (1, "berger-picard", "Пикардийская овчарка", "176"),
    (1, "border-collie", "Бордер-колли", "297"),
    (1, "bouvier-des-flandres", "Фландрский бувье", "191"),
    (1, "bouvier-des-ardennes", "Арденнский бувье", "171"),
    (1, "briard", "Бриар", "113"),
    (1, "catalan-sheepdog", "Каталонская овчарка", "87"),
    (1, "collie-rough", "Колли длинношёрстный", "156"),
    (1, "collie-smooth", "Колли короткошёрстный", "296"),
    (1, "croatian-sheepdog", "Хорватская овчарка", "277"),
    (1, "dutch-shepherd", "Голландская овчарка", "223"),
    (1, "german-shepherd", "Немецкая овчарка", "166"),
    (1, "komondor", "Командор", "53"),
    (1, "kuvasz", "Кувас", "54"),
    (1, "mudi", "Муди", "238"),
    (1, "old-english-sheepdog", "Бобтейл (староанглийская овчарка)", "16"),
    (1, "polish-lowland-sheepdog", "Польская низинная овчарка (PON)", "251"),
    (1, "polish-tatra-sheepdog", "Польская подгалянская овчарка", "252"),
    (1, "portuguese-sheepdog", "Португальская овчарка", "93"),
    (1, "puli", "Пули", "55"),
    (1, "pumi", "Пуми", "56"),
    (1, "pyrenean-shepherd", "Пиренейская овчарка", "138"),
    (1, "saarloos-wolfdog", "Саарлоосская волчья собака", "311"),
    (1, "schapendoes", "Схапендус", "313"),
    (1, "shetland-sheepdog", "Шелти", "88"),
    (1, "slovak-cuvac", "Словацкий чувач", "142"),
    (1, "south-russian-shepherd", "Южнорусская овчарка", "326"),
    (1, "spanish-water-dog", "Испанская водяная собака", "336"),
    (1, "white-swiss-shepherd", "Белая швейцарская овчарка", "347"),
    (1, "czechoslovakian-wolfdog", "Чехословацкая волчья собака", "332"),
    (1, "majorca-shepherd", "Майоркская овчарка", "321"),
]


# Группа 2: Пинчеры и шнауцеры, молоссы, швейцарские пастушьи
GROUP_2_PINSCHERS_MOLOSSERS: list[BreedRow] = [
    (2, "affenpinscher", "Аффенпинчер", "186"),
    (2, "anatolian-shepherd", "Анатолийская овчарка (кангал)", "331"),
    (2, "appenzeller-sennenhund", "Аппенцеллер зенненхунд", "46"),
    (2, "atlas-mountain-dog", "Аиди (атласская овчарка)", "247"),
    (2, "bernese-mountain-dog", "Бернский зенненхунд", "45"),
    (2, "boerboel", "Бурбуль (южноафриканский)", "365"),
    (2, "bordeaux-dog", "Бордоский дог", "116"),
    (2, "boxer", "Боксёр", "144"),
    (2, "broholmer", "Бролхольмер", "315"),
    (2, "bullmastiff", "Бульмастиф", "157"),
    (2, "cane-corso", "Кане-корсо", "343"),
    (2, "caucasian-shepherd", "Кавказская овчарка", "328"),
    (2, "central-asian-shepherd", "Среднеазиатская овчарка (алабай)", "335"),
    (2, "doberman", "Доберман", "143"),
    (2, "dogo-argentino", "Аргентинский дог", "292"),
    (2, "dogo-canario", "Канарский дог (пресо канарио)", "346"),
    (2, "english-mastiff", "Английский мастиф", "264"),
    (2, "entlebucher-sennenhund", "Энтлебухер зенненхунд", "47"),
    (2, "estrela-mountain-dog", "Эштрельская овчарка", "173"),
    (2, "fila-brasileiro", "Фила бразилейро", "225"),
    (2, "german-pinscher", "Немецкий пинчер", "184"),
    (2, "great-dane", "Немецкий дог", "235"),
    (2, "great-pyrenees", "Пиренейская горная собака", "137"),
    (2, "greater-swiss-mountain-dog", "Большой швейцарский зенненхунд", "58"),
    (2, "hovawart", "Ховаварт", "190"),
    (2, "kangal", "Кангал", "331"),
    (2, "karst-shepherd", "Крашская овчарка", "278"),
    (2, "landseer", "Ландсир", "226"),
    (2, "leonberger", "Леонбергер", "145"),
    (2, "majorca-mastiff", "Майоркский мастиф (ка-де-бо)", "249"),
    (2, "miniature-pinscher", "Цвергпинчер", "185"),
    (2, "moscow-watchdog", "Московская сторожевая", None),
    (2, "neapolitan-mastiff", "Неаполитанский мастиф", "197"),
    (2, "newfoundland", "Ньюфаундленд", "50"),
    (2, "pyrenean-mastiff", "Пиренейский мастиф", "92"),
    (2, "rafeiro-do-alentejo", "Рафейру до Алентежу", "96"),
    (2, "rottweiler", "Ротвейлер", "147"),
    (2, "russian-black-terrier", "Русский чёрный терьер", "327"),
    (2, "saint-bernard", "Сенбернар", "61"),
    (2, "sarplaninac", "Шарпланинская овчарка", "41"),
    (2, "schnauzer-giant", "Ризеншнауцер", "181"),
    (2, "schnauzer-standard", "Миттельшнауцер", "182"),
    (2, "schnauzer-miniature", "Цвергшнауцер", "183"),
    (2, "shar-pei", "Шарпей", "309"),
    (2, "spanish-mastiff", "Испанский мастиф", "91"),
    (2, "tibetan-mastiff", "Тибетский мастиф", "230"),
    (2, "tornjak", "Торняк", "355"),
    (2, "tosa-inu", "Тоса-ину", "260"),
]


# Группа 3: Терьеры
GROUP_3_TERRIERS: list[BreedRow] = [
    (3, "airedale-terrier", "Эрдельтерьер", "7"),
    (3, "american-staffordshire-terrier", "Американский стаффордширский терьер", "286"),
    (3, "australian-terrier", "Австралийский терьер", "8"),
    (3, "bedlington-terrier", "Бедлингтон-терьер", "9"),
    (3, "border-terrier", "Бордер-терьер", "10"),
    (3, "bull-terrier", "Бультерьер", "11"),
    (3, "bull-terrier-miniature", "Миниатюрный бультерьер", "359"),
    (3, "cairn-terrier", "Керн-терьер", "4"),
    (3, "cesky-terrier", "Чешский терьер", "246"),
    (3, "dandie-dinmont-terrier", "Денди-динмонт-терьер", "168"),
    (3, "english-toy-terrier", "Английский той-терьер", "13"),
    (3, "fox-terrier-smooth", "Гладкошёрстный фокстерьер", "12"),
    (3, "fox-terrier-wire", "Жесткошёрстный фокстерьер", "169"),
    (3, "german-hunting-terrier", "Немецкий ягдтерьер", "103"),
    (3, "glen-of-imaal-terrier", "Глен оф Имаал терьер", "302"),
    (3, "irish-soft-coated-wheaten-terrier", "Мягкошёрстный пшеничный терьер", "40"),
    (3, "irish-terrier", "Ирландский терьер", "139"),
    (3, "jack-russell-terrier", "Джек-рассел-терьер", "345"),
    (3, "japanese-terrier", "Японский терьер", "259"),
    (3, "kerry-blue-terrier", "Керри-блю-терьер", "3"),
    (3, "lakeland-terrier", "Лейкленд-терьер", "70"),
    (3, "manchester-terrier", "Манчестер-терьер", "71"),
    (3, "norfolk-terrier", "Норфолк-терьер", "272"),
    (3, "norwich-terrier", "Норвич-терьер", "72"),
    (3, "parson-russell-terrier", "Парсон-рассел-терьер", "339"),
    (3, "scottish-terrier", "Скотчтерьер", "73"),
    (3, "sealyham-terrier", "Силихем-терьер", "74"),
    (3, "skye-terrier", "Скай-терьер", "75"),
    (3, "staffordshire-bull-terrier", "Стаффордширский бультерьер", "76"),
    (3, "welsh-terrier", "Вельштерьер", "78"),
    (3, "west-highland-white-terrier", "Вест-хайленд-уайт-терьер", "85"),
    (3, "yorkshire-terrier", "Йоркширский терьер", "86"),
    (3, "biewer-terrier", "Бивер-йоркширский терьер", None),
    (3, "russian-toy", "Русский той", "352"),
]


# Группа 4: Таксы (формально 1 стандарт FCI №148, но 9 разновидностей по
# типу шерсти и размеру — заводят как отдельные породы в РКФ).
GROUP_4_DACHSHUNDS: list[BreedRow] = [
    (4, "dachshund-standard-smooth", "Такса стандартная гладкошёрстная", "148"),
    (4, "dachshund-standard-longhaired", "Такса стандартная длинношёрстная", "148"),
    (4, "dachshund-standard-wirehaired", "Такса стандартная жесткошёрстная", "148"),
    (4, "dachshund-miniature-smooth", "Такса миниатюрная гладкошёрстная", "148"),
    (4, "dachshund-miniature-longhaired", "Такса миниатюрная длинношёрстная", "148"),
    (4, "dachshund-miniature-wirehaired", "Такса миниатюрная жесткошёрстная", "148"),
    (4, "dachshund-rabbit-smooth", "Такса кроличья гладкошёрстная", "148"),
    (4, "dachshund-rabbit-longhaired", "Такса кроличья длинношёрстная", "148"),
    (4, "dachshund-rabbit-wirehaired", "Такса кроличья жесткошёрстная", "148"),
]


# Группа 5: Шпицы и примитивные
GROUP_5_SPITZ_PRIMITIVE: list[BreedRow] = [
    (5, "akita", "Акита-ину", "255"),
    (5, "alaskan-malamute", "Аляскинский маламут", "243"),
    (5, "american-akita", "Американская акита", "344"),
    (5, "basenji", "Басенджи", "43"),
    (5, "canaan-dog", "Ханаанская собака", "273"),
    (5, "chinook", "Чинук", "362"),
    (5, "chow-chow", "Чау-чау", "205"),
    (5, "cirneco-dell-etna", "Сицилийская борзая", "199"),
    (5, "eurasier", "Евразиер", "291"),
    (5, "finnish-lapphund", "Финский лаппхунд", "189"),
    (5, "finnish-spitz", "Финский шпиц", "49"),
    (5, "german-spitz-keeshond", "Немецкий вольфшпиц (кеесхонд)", "97"),
    (5, "german-spitz-large", "Немецкий большой шпиц", "97"),
    (5, "german-spitz-medium", "Немецкий средний шпиц", "97"),
    (5, "german-spitz-miniature", "Немецкий малый шпиц", "97"),
    (5, "german-spitz-pomeranian", "Немецкий миниатюрный шпиц (померанский)", "97"),
    (5, "greenland-dog", "Гренландская собака", "274"),
    (5, "hokkaido", "Хоккайдо", "261"),
    (5, "ibizan-hound", "Поденко ибиценко", "89"),
    (5, "icelandic-sheepdog", "Исландская собака", "289"),
    (5, "japanese-spitz", "Японский шпиц", "262"),
    (5, "jindo", "Корейский чиндо", "334"),
    (5, "kai", "Кай-кэн", "317"),
    (5, "karelian-bear-dog", "Карельская медвежья собака", "48"),
    (5, "kishu", "Кисю", "318"),
    (5, "korean-jindo", "Корейский чиндо", "334"),
    (5, "lapponian-herder", "Лапландская оленегонная собака", "284"),
    (5, "mexican-hairless-xolo-standard", "Ксолоитцкуинтли стандартный", "234"),
    (5, "mexican-hairless-xolo-miniature", "Ксолоитцкуинтли миниатюрный", "234"),
    (5, "mexican-hairless-xolo-intermediate", "Ксолоитцкуинтли промежуточный", "234"),
    (5, "norwegian-buhund", "Норвежский бухунд", "237"),
    (5, "norwegian-elkhound-black", "Норвежский лосиная лайка чёрная", "242"),
    (5, "norwegian-elkhound-grey", "Норвежский лосиная лайка серая", "97"),
    (5, "norwegian-lundehund", "Норвежский лундехунд", "265"),
    (5, "peruvian-inca-orchid", "Перуанская голая собака", "310"),
    (5, "pharaoh-hound", "Фараонова собака", "248"),
    (5, "podengo-portugues", "Португальский поденгу", "94"),
    (5, "russian-european-laika", "Русско-европейская лайка", "304"),
    (5, "samoyed", "Самоедская собака", "212"),
    (5, "shiba-inu", "Сиба-ину", "257"),
    (5, "shikoku", "Сикоку", "319"),
    (5, "siberian-husky", "Сибирский хаски", "270"),
    (5, "swedish-lapphund", "Шведский лаппхунд", "135"),
    (5, "swedish-vallhund", "Шведский вальхунд", "14"),
    (5, "thai-bangkaew", "Тайский бангкеу", "358"),
    (5, "thai-ridgeback", "Тайский риджбек", "338"),
    (5, "volpino-italiano", "Итальянский вольпино", "195"),
    (5, "west-siberian-laika", "Западносибирская лайка", "306"),
    (5, "yakutian-laika", "Якутская лайка", "365"),
    (5, "east-siberian-laika", "Восточносибирская лайка", "305"),
]


# Группа 6: Гончие и ищейки
GROUP_6_SCENTHOUNDS: list[BreedRow] = [
    (6, "alpine-dachsbracke", "Альпийский таксообразный бракк", "254"),
    (6, "american-foxhound", "Американский фоксхаунд", "303"),
    (6, "ariegeois", "Арьежский бракк", "20"),
    (6, "artois-hound", "Артуанская гончая", "28"),
    (6, "basset-artesien-normand", "Артезиано-нормандский бассет", "34"),
    (6, "basset-bleu-de-gascogne", "Голубой гасконский бассет", "35"),
    (6, "basset-fauve-de-bretagne", "Бретонский палевый бассет", "36"),
    (6, "basset-hound", "Бассет-хаунд", "163"),
    (6, "bavarian-mountain-hound", "Баварская горная гончая", "217"),
    (6, "beagle", "Бигль", "161"),
    (6, "beagle-harrier", "Бигль-харьер", "290"),
    (6, "billy", "Бильи", "25"),
    (6, "black-and-tan-coonhound", "Чёрно-подпалый кунхаунд", "300"),
    (6, "bloodhound", "Бладхаунд (сен-юбер)", "84"),
    (6, "blue-gascony-griffon", "Голубой гасконский гриффон", "32"),
    (6, "bosnian-coarse-haired-hound", "Боснийская грубошёрстная гончая", "155"),
    (6, "briquet-griffon-vendeen", "Бракк-гриффон вандейский", "19"),
    (6, "chien-de-saint-hubert", "Сенбернарская гончая", "84"),
    (6, "dachsbracke", "Таксообразный бракк", "254"),
    (6, "dalmatian", "Далматин", "153"),
    (6, "deutsche-bracke", "Немецкая гончая", "299"),
    (6, "drever", "Древер", "130"),
    (6, "dunker", "Норвежская гончая (дункер)", "203"),
    (6, "english-foxhound", "Английский фоксхаунд", "159"),
    (6, "finnish-hound", "Финская гончая", "51"),
    (6, "grand-anglo-francais", "Большой англо-французский гончий", "21"),
    (6, "grand-bleu-de-gascogne", "Большая голубая гасконская гончая", "22"),
    (6, "grand-griffon-vendeen", "Большой вандейский гриффон", "282"),
    (6, "griffon-fauve-de-bretagne", "Бретонский палевый гриффон", "66"),
    (6, "griffon-nivernais", "Ниверне-гриффон", "17"),
    (6, "hamiltonstovare", "Гончая Гамильтона", "132"),
    (6, "hanoverian-scenthound", "Ганноверская гончая", "213"),
    (6, "harrier", "Харьер", "295"),
    (6, "hellenic-hound", "Эллинская гончая", "214"),
    (6, "hygenhund", "Хюген (гончая Хюгена)", "266"),
    (6, "istrian-coarse-haired-hound", "Истринская жесткошёрстная гончая", "152"),
    (6, "istrian-shorthaired-hound", "Истринская гладкошёрстная гончая", "151"),
    (6, "italian-segugio-shorthaired", "Итальянский сегуджо гладкошёрстный", "337"),
    (6, "italian-segugio-wirehaired", "Итальянский сегуджо жесткошёрстный", "198"),
    (6, "montenegrin-mountain-hound", "Черногорская гончая", "275"),
    (6, "norwegian-elkhound", "Норвежский эльгхунд", "242"),
    (6, "otterhound", "Оттерхаунд", "294"),
    (6, "petit-basset-griffon-vendeen", "Малый вандейский бассет-гриффон", "67"),
    (6, "petit-bleu-de-gascogne", "Малая голубая гасконская гончая", "31"),
    (6, "poitevin", "Пуатвинская гончая", "24"),
    (6, "polish-hound", "Польская гончая", "52"),
    (6, "polish-hunting-dog", "Польский огар", "354"),
    (6, "porcelaine", "Порселен", "30"),
    (6, "posavac-hound", "Посавская гончая", "154"),
    (6, "redbone-coonhound", "Рыжий кунхаунд", "366"),
    (6, "rhodesian-ridgeback", "Родезийский риджбек", "146"),
    (6, "russian-hound", "Русская гончая", None),
    (6, "russian-piebald-hound", "Русская пегая гончая", None),
    (6, "schillerstovare", "Гончая Шиллера", "131"),
    (6, "schweizer-laufhund", "Швейцарская гончая", "59"),
    (6, "serbian-hound", "Сербская гончая", "150"),
    (6, "serbian-tricolour-hound", "Сербская трёхцветная гончая", "229"),
    (6, "slovak-hound", "Словацкий копов", "244"),
    (6, "small-griffon-vendeen", "Малый вандейский гриффон", "19"),
    (6, "smalandsstovare", "Смоландская гончая", "129"),
    (6, "spanish-hound", "Испанский сабуэсо", "204"),
    (6, "styrian-coarse-haired-hound", "Штирийский жесткошёрстный бракк", "62"),
    (6, "swiss-hound", "Швейцарский лауфхунд", "59"),
    (6, "transylvanian-hound", "Трансильванская гончая", "241"),
    (6, "tyrolean-hound", "Тирольский бракк", "68"),
    (6, "welsh-hound", "Уэльская гончая", None),
    (6, "westphalian-dachsbracke", "Вестфальская гончая", "100"),
    (6, "yugoslavian-mountain-hound", "Югославская горная гончая", "279"),
]


# Группа 7: Легавые (pointing dogs)
GROUP_7_POINTERS: list[BreedRow] = [
    (7, "ariege-pointer", "Арьежский бракк", "177"),
    (7, "auvergne-pointer", "Овернский бракк", "180"),
    (7, "blue-picardy-spaniel", "Голубой пикардийский спаниель", "106"),
    (7, "bohemian-wirehaired-pointing-griffon", "Чешский фоусек", "245"),
    (7, "bourbonnais-pointer", "Бракк дю Бурбонне", "179"),
    (7, "bracco-italiano", "Итальянский бракк", "202"),
    (7, "brittany-spaniel", "Бретонский эпаньоль", "95"),
    (7, "burgos-pointer", "Бургосский бракк (Pachón)", "90"),
    (7, "drentse-patrijshond", "Дрентская куропаточная собака", "224"),
    (7, "english-pointer", "Английский пойнтер", "1"),
    (7, "english-setter", "Английский сеттер", "2"),
    (7, "epagneul-bleu-de-picardie", "Голубой пикардийский эпаньоль", "106"),
    (7, "french-pointer-gascogne", "Французский бракк гасконский", "133"),
    (7, "french-pointer-pyrenees", "Французский бракк пиренейский", "134"),
    (7, "french-spaniel", "Французский эпаньоль", "175"),
    (7, "german-longhaired-pointer", "Немецкая длинношёрстная легавая", "117"),
    (7, "german-pointer-shorthaired", "Курцхаар", "119"),
    (7, "german-pointer-wirehaired", "Дратхаар", "98"),
    (7, "gordon-setter", "Шотландский сеттер (гордон)", "6"),
    (7, "hungarian-vizsla-shorthaired", "Венгерская выжла короткошёрстная", "57"),
    (7, "hungarian-vizsla-wirehaired", "Венгерская выжла жесткошёрстная", "239"),
    (7, "irish-red-and-white-setter", "Ирландский красно-белый сеттер", "330"),
    (7, "irish-setter", "Ирландский сеттер", "120"),
    (7, "italian-spinone", "Итальянский спиноне", "165"),
    (7, "korthals-griffon", "Жесткошёрстный гриффон (Кортальс)", "107"),
    (7, "large-munsterlander", "Большой мюнстерлендер", "118"),
    (7, "old-danish-pointer", "Старый датский пойнтер", "281"),
    (7, "picardy-spaniel", "Пикардийский эпаньоль", "108"),
    (7, "pont-audemer-spaniel", "Эпаньоль Понт-Одемер", "114"),
    (7, "portuguese-pointer", "Португальская легавая", "187"),
    (7, "pudelpointer", "Пудельпойнтер", "216"),
    (7, "saint-germain-pointer", "Бракк Сен-Жермен", "115"),
    (7, "slovakian-rough-haired-pointer", "Словацкий жесткошёрстный пойнтер", "320"),
    (7, "small-munsterlander", "Малый мюнстерлендер", "102"),
    (7, "stabyhoun", "Стабихаун", "222"),
    (7, "weimaraner-shorthaired", "Веймаранер короткошёрстный", "99"),
    (7, "weimaraner-longhaired", "Веймаранер длинношёрстный", "99"),
]


# Группа 8: Ретриверы, спаниели и водяные собаки
GROUP_8_RETRIEVERS_SPANIELS: list[BreedRow] = [
    (8, "american-cocker-spaniel", "Американский кокер-спаниель", "167"),
    (8, "american-water-spaniel", "Американский водяной спаниель", "301"),
    (8, "barbet", "Барбе", "105"),
    (8, "boykin-spaniel", "Спаниель Бойкина", "368"),
    (8, "chesapeake-bay-retriever", "Чесапик-бей ретривер", "263"),
    (8, "clumber-spaniel", "Кламбер-спаниель", "109"),
    (8, "cocker-spaniel-english", "Английский кокер-спаниель", "5"),
    (8, "curly-coated-retriever", "Курчавошёрстный ретривер", "110"),
    (8, "english-springer-spaniel", "Английский спрингер-спаниель", "125"),
    (8, "field-spaniel", "Филд-спаниель", "123"),
    (8, "flat-coated-retriever", "Прямошёрстный ретривер", "121"),
    (8, "frisian-water-dog", "Фризская водяная собака (вэттерхаун)", "221"),
    (8, "golden-retriever", "Золотистый ретривер", "111"),
    (8, "irish-water-spaniel", "Ирландский водяной спаниель", "124"),
    (8, "kooikerhondje", "Коикерхондье", "314"),
    (8, "labrador-retriever", "Лабрадор-ретривер", "122"),
    (8, "lagotto-romagnolo", "Лаготто-романьоло", "298"),
    (8, "nova-scotia-duck-tolling-retriever", "Новошотландский толлер", "312"),
    (8, "portuguese-water-dog", "Португальская водяная собака", "37"),
    (8, "russian-spaniel", "Русский охотничий спаниель", None),
    (8, "sussex-spaniel", "Суссекс-спаниель", "127"),
    (8, "welsh-springer-spaniel", "Вельш-спрингер-спаниель", "126"),
]


# Группа 9: Декоративные и собаки-компаньоны
GROUP_9_COMPANIONS: list[BreedRow] = [
    (9, "bichon-frise", "Бишон фризе", "215"),
    (9, "bichon-havanais", "Гаванский бишон (хаванез)", "250"),
    (9, "bolognese", "Болоньез", "196"),
    (9, "boston-terrier", "Бостон-терьер", "140"),
    (9, "cavalier-king-charles-spaniel", "Кавалер-кинг-чарльз-спаниель", "136"),
    (9, "chihuahua-smoothhaired", "Чихуахуа гладкошёрстный", "218"),
    (9, "chihuahua-longhaired", "Чихуахуа длинношёрстный", "218"),
    (9, "chinese-crested-hairless", "Китайская хохлатая голая", "288"),
    (9, "chinese-crested-powderpuff", "Китайская хохлатая пуховая", "288"),
    (9, "coton-de-tulear", "Котон-де-тулеар", "283"),
    (9, "french-bulldog", "Французский бульдог", "101"),
    (9, "havanese", "Гаванский бишон (хаванез)", "250"),
    (9, "japanese-chin", "Японский хин", "206"),
    (9, "king-charles-spaniel", "Кинг-чарльз-спаниель", "128"),
    (9, "kromfohrlander", "Кромфорлендер", "192"),
    (9, "lhasa-apso", "Лхаса-апсо", "227"),
    (9, "lowchen", "Лёвхен (малая львиная собака)", "233"),
    (9, "maltese", "Мальтезе (мальтийская болонка)", "65"),
    (9, "papillon", "Папильон (континентальный той-спаниель)", "77"),
    (9, "pekingese", "Пекинес", "207"),
    (9, "petit-brabancon", "Малый брабансон", "82"),
    (9, "phalene", "Фален", "77"),
    (9, "poodle-standard", "Пудель большой (стандартный)", "172"),
    (9, "poodle-medium", "Пудель средний", "172"),
    (9, "poodle-miniature", "Пудель малый (миниатюрный)", "172"),
    (9, "poodle-toy", "Пудель той", "172"),
    (9, "pug", "Мопс", "253"),
    (9, "russian-toy-smoothhaired", "Русский той гладкошёрстный", "352"),
    (9, "russian-toy-longhaired", "Русский той длинношёрстный", "352"),
    (9, "shih-tzu", "Ши-тцу", "208"),
    (9, "small-brabant-griffon", "Малый брабансон (пти-брабансон)", "82"),
    (9, "tibetan-spaniel", "Тибетский спаниель", "231"),
    (9, "tibetan-terrier", "Тибетский терьер", "209"),
    (9, "english-bulldog", "Английский бульдог", "149"),
    (9, "brussels-griffon", "Брюссельский гриффон", "80"),
    (9, "belgian-griffon", "Бельгийский гриффон", "81"),
    (9, "continental-toy-spaniel", "Континентальный той-спаниель", "77"),
]


# Группа 10: Борзые
GROUP_10_SIGHTHOUNDS: list[BreedRow] = [
    (10, "afghan-hound", "Афганская борзая", "228"),
    (10, "azawakh", "Азавак", "307"),
    (10, "borzoi", "Русская псовая борзая", "193"),
    (10, "chart-polski", "Польский хорт (хорт польски)", "333"),
    (10, "deerhound", "Шотландская оленья борзая (дирхаунд)", "164"),
    (10, "galgo-espanol", "Галго эспаньол", "285"),
    (10, "greyhound", "Грейхаунд", "158"),
    (10, "hungarian-greyhound", "Венгерская борзая (мадьяр агар)", "240"),
    (10, "irish-wolfhound", "Ирландский волкодав", "160"),
    (10, "italian-greyhound", "Итальянский левретто", "200"),
    (10, "magyar-agar", "Мадьяр агар (венгерская борзая)", "240"),
    (10, "saluki", "Салюки (персидская борзая)", "269"),
    (10, "sloughi", "Слюги (арабская борзая)", "188"),
    (10, "south-russian-steppe-hound", "Южнорусская степная борзая", None),
    (10, "taigan", "Тайган (киргизская борзая)", None),
    (10, "whippet", "Уиппет", "162"),
    (10, "russian-hortaya-borzaya", "Хортая борзая", None),
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
