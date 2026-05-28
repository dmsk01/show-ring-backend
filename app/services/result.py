"""
Сервис результатов выставки (этап 7).

Бизнес-логика ввода оценок и присвоения титулов.

Ключевое — транзакционность. Присвоение результата делает три вещи
атомарно:
1. UPSERT в show_results (один результат на ShowEntry).
2. INSERT в dog_titles для каждого выданного титула.
3. Обновление titles_cache в show_results.

Если любая часть падает — откатывается всё. Иначе можно получить
"результат без титулов" или "титулы без результата".
"""

from __future__ import annotations

import uuid
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.reference import ShowClass, ShowRank
from app.models.result import DogTitle, ShowResult
from app.models.show import Show, ShowEntry, ShowStatus
from app.repositories import result as repo
from app.repositories import show as show_repo
from app.services import show_rules


# ---------------------------------------------------------------------
# Авторизация: судья/организатор/admin
# ---------------------------------------------------------------------


def _can_modify_results(
    show: Show, user_id: uuid.UUID, is_admin: bool
) -> bool:
    """
    Право ввода/правки результатов:
    - admin всегда может,
    - организатор выставки может,
    - судьи (любой из назначенных) могут — но на этом этапе детальную
      проверку "именно этот судья назначен на этот ринг" не делаем.
      Достаточно "пользователь — судья, назначенный хоть куда".

    Полная "правильный судья на правильном ринге" проверка — TODO,
    нужна модель ring↔entry (этап 8/9 при разработке расписания).
    """
    if is_admin or show.organizer_id == user_id:
        return True
    # Любой судья этой выставки.
    return any(j.judge_id == user_id for j in show.judges)


async def _ensure_can_edit(
    db: AsyncSession,
    show_entry_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
) -> tuple[Show, ShowEntry]:
    """
    Проверки контекста: запись существует, выставка в правильном статусе,
    пользователь имеет право вводить результаты.
    Возвращает (show, entry).
    """
    ctx = await repo.get_entry_context(db, show_entry_id)
    if ctx is None:
        raise ValueError("entry_not_found")
    entry, _dog, _breed = ctx
    # Подгружаем выставку с judges (нужно для _can_modify_results).
    show = await show_repo.get_show_with_relations(db, entry.show_id)
    if show is None:
        raise ValueError("not_found")
    if show.status not in (ShowStatus.in_progress, ShowStatus.registration_closed):
        # Результаты можно вводить, когда регистрация закрыта или
        # выставка идёт. До этого — рано, после publish — поздно.
        raise ValueError("show_not_in_progress")
    if not _can_modify_results(show, user_id, is_admin):
        raise ValueError("forbidden")
    return show, entry


# ---------------------------------------------------------------------
# Ввод результата в ринге класса
# ---------------------------------------------------------------------


async def _apply_class_titles(
    db: AsyncSession,
    *,
    result: ShowResult,
    entry: ShowEntry,
    show: Show,
    awards: Iterable[show_rules.TitleAward],
) -> list[show_rules.TitleAward]:
    """
    Создаёт строки в dog_titles для каждого титула и обновляет
    titles_cache на результате. Возвращает список фактически выданных
    титулов (некоторые могут отсеяться на UNIQUE-конфликте, если
    тот же титул на той же выставке уже был).

    Имена titles_cache формирует фронт-дружелюбно: список объектов с
    code и name.
    """
    granted: list[show_rules.TitleAward] = []
    for award in awards:
        # Идемпотентность: если титул уже есть (например, при повторном
        # сохранении результата) — пропускаем без ошибки. UNIQUE
        # (dog_id, title_id, show_id) гарантирует, что в БД не появится
        # дубликат, мы просто не хотим, чтобы 409 ломал транзакцию.
        existing = await _find_existing_title(
            db, dog_id=entry.dog_id, title_id=award.title_id, show_id=show.id
        )
        if existing is None:
            await repo.create_dog_title(
                db,
                dog_id=entry.dog_id,
                title_id=award.title_id,
                show_id=show.id,
                judge_id=result.judge_id,
                date_earned=show.date_start,
            )
        granted.append(award)
    # titles_cache держит ВСЕ титулы по этой записи (включая выданные
    # ранее BIG/BIS, если уже были). Поэтому сначала читаем текущий.
    current = list(result.titles_cache or [])
    seen_codes = {item["code"] for item in current}
    for award in granted:
        if award.code not in seen_codes:
            current.append({"code": award.code, "name": award.name})
            seen_codes.add(award.code)
    result.titles_cache = current
    return granted


async def _find_existing_title(
    db: AsyncSession,
    *,
    dog_id: uuid.UUID,
    title_id: uuid.UUID,
    show_id: uuid.UUID,
) -> DogTitle | None:
    from sqlalchemy import select

    stmt = select(DogTitle).where(
        DogTitle.dog_id == dog_id,
        DogTitle.title_id == title_id,
        DogTitle.show_id == show_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def upsert_class_result(
    db: AsyncSession,
    *,
    show_entry_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
    grade_id: uuid.UUID | None,
    placement: int | None,
    critique: str | None,
) -> ShowResult:
    """
    Создаёт или обновляет результат в ринге класса.

    Шаги (всё в одной транзакции):
    1. Валидация контекста (запись/статус/право).
    2. UPSERT show_results.
    3. Вычисление титулов через show_rules.compute_class_titles.
    4. INSERT dog_titles, обновление titles_cache.
    5. Установка флага is_class_winner.

    Возвращает обновлённый ShowResult.
    """
    show, entry = await _ensure_can_edit(db, show_entry_id, user_id, is_admin)

    # Подгрузим контекст ринга: класс, ранг, оценка, animal_type.
    cls = await db.get(ShowClass, entry.show_class_id)
    if cls is None:
        raise ValueError("show_class_not_found")
    rank = await db.get(ShowRank, show.rank_id)
    if rank is None:
        raise ValueError("show_rank_not_found")
    grade = (
        await repo.get_grade(db, grade_id) if grade_id is not None else None
    )
    if grade_id is not None and grade is None:
        raise ValueError("grade_not_found")

    # UPSERT — если результат уже был, обновляем поля; иначе создаём.
    result = await repo.get_result_by_entry(db, show_entry_id)
    if result is None:
        result = await repo.create_result(
            db,
            show_entry_id=show_entry_id,
            judge_id=user_id,
            grade_id=grade_id,
            placement=placement,
            critique=critique,
        )
    else:
        # Только не-None поля обновляем — позволяет частичный update
        # из роутера.
        if grade_id is not None:
            result.grade_id = grade_id
        if placement is not None:
            result.placement = placement
        if critique is not None:
            result.critique = critique
        result.judge_id = user_id

    # Вычисляем титулы класса.
    awards = await show_rules.compute_class_titles(
        db,
        animal_type_id=cls.animal_type_id,
        show_class=cls,
        show_rank=rank,
        grade=grade,
        placement=result.placement,
    )

    # CW — если в awards есть TITLE_CW. Снимаем флаг, если титул больше
    # не выдан (например, исправили оценку).
    result.is_class_winner = any(
        a.code == show_rules.TITLE_CW for a in awards
    )

    await _apply_class_titles(
        db, result=result, entry=entry, show=show, awards=awards
    )

    await db.commit()
    await db.refresh(result)
    return result


# ---------------------------------------------------------------------
# Лучшие на разных уровнях
# ---------------------------------------------------------------------


async def set_best_of_breed(
    db: AsyncSession,
    *,
    show_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
    breed_id: uuid.UUID,
    winner_entry_id: uuid.UUID,
    best_male_entry_id: uuid.UUID | None,
    best_female_entry_id: uuid.UUID | None,
    best_junior_entry_id: uuid.UUID | None,
    best_veteran_entry_id: uuid.UUID | None,
) -> ShowResult:
    """
    Выбор ЛПП по породе. Сервис проставляет соответствующие флаги
    на результаты указанных записей и выдаёт титулы BOB / CACIB
    (на CACIB-выставках).
    """
    show = await show_repo.get_show_with_relations(db, show_id)
    if show is None:
        raise ValueError("not_found")
    if not _can_modify_results(show, user_id, is_admin):
        raise ValueError("forbidden")

    rank = await db.get(ShowRank, show.rank_id)
    if rank is None:
        raise ValueError("show_rank_not_found")

    # Снимаем флаги BOB/best_male/best_female/best_junior/best_veteran
    # с прежних победителей в этой породе (если кто-то был).
    #
    # ИСПРАВЛЕНО (bug_019 ultrareview): при re-election BOB ex-winner
    # сохранял флаги is_best_in_group и is_best_in_show, потому что
    # этот reset их не трогал. Дальше set_best_in_group искал prev
    # BIG-winner'ов через `is_best_of_breed=True` — ex-BOB (теперь
    # False) в выборку не попадал, и его BIG-флаг оставался навсегда.
    # Аналогично — для BIS через is_best_in_group.
    # Каскадом снимаем все ancestor-флаги, иначе ломается инвариант
    # BIS ⊆ BIG ⊆ BOB → дубли в analytics, PDF-каталоге, дашборде.
    #
    # ИСПРАВЛЕНО (bug_209 audit 2026-05-28): for_update=True добавляет
    # SELECT … FOR UPDATE OF show_results. Без этого два параллельных
    # PUT BOB на одну породу читали один и тот же набор ex-победителей,
    # каждый писал нового → один из сбросов терялся, оставалось два
    # BOB-флага. Лок держится до db.commit() ниже — критическая секция
    # сериализована по этой выборке (по сути, по паре show_id+breed_id).
    existing = await repo.list_results_by_breed(
        db, show_id, breed_id, for_update=True
    )
    for r in existing:
        r.is_best_of_breed = False
        r.is_best_male = False
        r.is_best_female = False
        r.is_best_junior = False
        r.is_best_veteran = False
        r.is_best_in_group = False
        r.is_best_in_show = False

    # Утилита для проставления флага одной записи.
    async def _set_flag_on(entry_id: uuid.UUID, attr: str) -> ShowResult:
        # Валидация: запись принадлежит выставке и породе.
        ctx = await repo.get_entry_context(db, entry_id)
        if ctx is None:
            raise ValueError("entry_not_found")
        entry, dog, _breed = ctx
        if entry.show_id != show_id or dog.breed_id != breed_id:
            raise ValueError("entry_breed_mismatch")
        result = await repo.get_result_by_entry(db, entry_id)
        if result is None:
            raise ValueError("result_not_found")
        setattr(result, attr, True)
        return result

    winner_result = await _set_flag_on(winner_entry_id, "is_best_of_breed")
    # Сам BOB по умолчанию — это и best_male/best_female (если пол собаки
    # известен), но клиент может явно указать обоих "лучших" в породе.
    if best_male_entry_id is not None:
        await _set_flag_on(best_male_entry_id, "is_best_male")
    if best_female_entry_id is not None:
        await _set_flag_on(best_female_entry_id, "is_best_female")
    if best_junior_entry_id is not None:
        await _set_flag_on(best_junior_entry_id, "is_best_junior")
    if best_veteran_entry_id is not None:
        await _set_flag_on(best_veteran_entry_id, "is_best_veteran")

    # Выдаём BOB победителю.
    bob_award = await show_rules.get_best_of_breed_titles(
        db,
        animal_type_id=(
            # animal_type у winner_result определяется через породу собаки.
            (await _resolve_animal_type(db, winner_entry_id))
        ),
        show_rank=rank,
        is_bob=True,
        is_best_male=winner_result.is_best_male,
        is_best_female=winner_result.is_best_female,
    )
    winner_entry_ctx = await repo.get_entry_context(db, winner_entry_id)
    assert winner_entry_ctx is not None  # выше проверили в _set_flag_on
    winner_entry, _wdog, _wbreed = winner_entry_ctx
    await _apply_class_titles(
        db,
        result=winner_result,
        entry=winner_entry,
        show=show,
        awards=bob_award,
    )

    await db.commit()
    await db.refresh(winner_result)
    return winner_result


async def _resolve_animal_type(
    db: AsyncSession, entry_id: uuid.UUID
) -> uuid.UUID:
    """Достаёт animal_type_id для собаки через её породу."""
    ctx = await repo.get_entry_context(db, entry_id)
    if ctx is None:
        raise ValueError("entry_not_found")
    _entry, _dog, breed = ctx
    return breed.animal_type_id


async def set_best_in_group(
    db: AsyncSession,
    *,
    show_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
    breed_group_id: uuid.UUID,
    winner_entry_id: uuid.UUID,
) -> ShowResult:
    """
    Выбор BIG для группы FCI. Победитель должен быть BOB в одной из пород
    этой группы.
    """
    show = await show_repo.get_show_with_relations(db, show_id)
    if show is None:
        raise ValueError("not_found")
    if not _can_modify_results(show, user_id, is_admin):
        raise ValueError("forbidden")

    # Снимаем флаг BIG с предыдущих победителей этой группы.
    # ИСПРАВЛЕНО (bug_019 ultrareview): также сбрасываем
    # is_best_in_show — иначе при re-election BIG ex-winner сохранял
    # BIS-флаг, и инвариант BIS ⊆ BIG ломался.
    # NB: list_results_by_group фильтрует по is_best_of_breed=True;
    # если ex-BIG уже потерял BOB (через set_best_of_breed выше),
    # его BIG-флаг сбрасывается там же — здесь рассчитываем на
    # «обычный» сценарий, когда BOB не менялся.
    # ИСПРАВЛЕНО (bug_209 audit 2026-05-28): for_update=True — лок до
    # commit'а сериализует параллельные set_best_in_group по одной группе.
    prev = await repo.list_results_by_group(
        db, show_id, breed_group_id, for_update=True
    )
    for r in prev:
        r.is_best_in_group = False
        r.is_best_in_show = False

    ctx = await repo.get_entry_context(db, winner_entry_id)
    if ctx is None:
        raise ValueError("entry_not_found")
    entry, _dog, breed = ctx
    if entry.show_id != show_id:
        raise ValueError("entry_show_mismatch")
    if breed.breed_group_id != breed_group_id:
        raise ValueError("entry_group_mismatch")

    winner = await repo.get_result_by_entry(db, winner_entry_id)
    if winner is None:
        raise ValueError("result_not_found")
    if not winner.is_best_of_breed:
        # BIG присуждается только BOB-победителю породы.
        raise ValueError("winner_must_be_bob")
    winner.is_best_in_group = True

    big_award = await show_rules.get_big_title(db, breed.animal_type_id)
    if big_award is not None:
        await _apply_class_titles(
            db,
            result=winner,
            entry=entry,
            show=show,
            awards=[big_award],
        )

    await db.commit()
    await db.refresh(winner)
    return winner


async def set_best_in_show(
    db: AsyncSession,
    *,
    show_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
    winner_entry_id: uuid.UUID,
) -> ShowResult:
    """
    Выбор BIS — главного победителя выставки. Победитель должен быть
    BIG-победителем своей группы.
    """
    show = await show_repo.get_show_with_relations(db, show_id)
    if show is None:
        raise ValueError("not_found")
    if not _can_modify_results(show, user_id, is_admin):
        raise ValueError("forbidden")

    # Снимаем BIS с предыдущего победителя выставки.
    # ИСПРАВЛЕНО (bug_209 audit 2026-05-28): for_update=True для
    # сериализации параллельных set_best_in_show на одной выставке.
    prev = await repo.list_bob_results_for_show(
        db, show_id, for_update=True
    )
    for r in prev:
        r.is_best_in_show = False

    ctx = await repo.get_entry_context(db, winner_entry_id)
    if ctx is None:
        raise ValueError("entry_not_found")
    entry, _dog, breed = ctx
    if entry.show_id != show_id:
        raise ValueError("entry_show_mismatch")

    winner = await repo.get_result_by_entry(db, winner_entry_id)
    if winner is None:
        raise ValueError("result_not_found")
    if not winner.is_best_in_group:
        raise ValueError("winner_must_be_big")
    winner.is_best_in_show = True

    bis_award = await show_rules.get_bis_title(db, breed.animal_type_id)
    if bis_award is not None:
        await _apply_class_titles(
            db,
            result=winner,
            entry=entry,
            show=show,
            awards=[bis_award],
        )

    await db.commit()
    await db.refresh(winner)
    return winner


# ---------------------------------------------------------------------
# Публикация
# ---------------------------------------------------------------------


async def publish_results(
    db: AsyncSession,
    *,
    show_id: uuid.UUID,
    user_id: uuid.UUID,
    is_admin: bool,
) -> Show:
    """
    Публикация результатов = смена статуса выставки на completed.

    Дополнительно (для будущего этапа 9 — события) генерирует событие
    show.results_published. На этапе 7 событие пока не отправляем —
    оставляем точку расширения комментарием.
    """
    show = await show_repo.get_show(db, show_id)
    if show is None:
        raise ValueError("not_found")
    if not (is_admin or show.organizer_id == user_id):
        raise ValueError("forbidden")
    if not show_rules.is_transition_allowed(
        show.status, ShowStatus.completed
    ):
        raise ValueError("invalid_status_transition")
    show.status = ShowStatus.completed
    await db.commit()
    await db.refresh(show)
    # TODO (этап 9): publish event "show.results_published" в RabbitMQ.
    return show
