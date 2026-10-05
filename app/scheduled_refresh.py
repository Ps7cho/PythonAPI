"""UTC-boundary jobs that catch up to the current period after downtime."""
import logging
from datetime import date, datetime, timedelta, timezone
from math import ceil
from random import Random
from threading import Lock

from sqlalchemy import select, text

from app.database import SessionLocal
from app.models import QuestTemplate, ScheduledJob, Shop, ShopRotation, ShopTable, WeaponType


JOBS = {'raid_daily': 'daily', 'raid_weekly': 'weekly', 'shop_daily': 'daily'}
_sqlite_lock = Lock()


def utc_now():
    return datetime.now(timezone.utc)


def period_window(cadence, now=None):
    now = (now or utc_now()).astimezone(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if cadence == 'weekly':
        start -= timedelta(days=start.weekday())
    end = start + timedelta(days=7 if cadence == 'weekly' else 1)
    return start.date().isoformat(), end


def seconds_until_next_refresh(now=None):
    """Return the delay until the nearest UTC cadence boundary."""
    now = (now or utc_now()).astimezone(timezone.utc)
    deadline = min(period_window(cadence, now)[1] for cadence in set(JOBS.values()))
    return max(0.0, (deadline - now).total_seconds())


def refresh_worker(stop, retry_seconds=30):
    """Catch up at startup, then wait for the next daily or weekly deadline."""
    while not stop.is_set():
        failed = False
        for key in JOBS:
            try:
                run_due_jobs(jobs=(key,))
            except Exception:
                failed = True
                logging.getLogger(__name__).exception(
                    'Scheduled refresh %s failed; it will retry after a short delay.', key)
        delay = retry_seconds if failed else seconds_until_next_refresh()
        stop.wait(delay)


def rotated_stock(items, seed, offset=0):
    """Keep catalog staples and cycle through roughly 60% of the other choices."""
    staples = [item for item in items if item.get('always_stocked')]
    rotating = [item for item in items if not item.get('always_stocked')]
    if not rotating:
        return staples
    Random(seed).shuffle(rotating)
    count = min(len(rotating) - 1, max(1, ceil(len(rotating) * .6))) if len(rotating) > 1 else 1
    chosen = [rotating[(offset + index) % len(rotating)] for index in range(count)]
    return [*staples, *chosen]


def _refresh_shops(db, period, resets_at):
    for shop in db.scalars(select(Shop).order_by(Shop.slug)):
        if db.get(ShopRotation, (shop.slug, period)):
            continue
        tables = list(db.scalars(select(ShopTable).where(ShopTable.shop_slug == shop.slug)
                                 .order_by(ShopTable.slug)))
        stock = {table.slug: rotated_stock(_stock_pool(db, table), table.slug,
                 date.fromisoformat(period).toordinal()) for table in tables}
        db.add(ShopRotation(shop_slug=shop.slug, period=period,
                            resets_at=resets_at.replace(tzinfo=None), stock=stock))


def _stock_pool(db, table):
    if table.items:
        return table.items
    if table.category != 'weapons' or table.shop_slug != 'mosswood-market':
        return []
    # The legacy Mosswood table can be empty because weapon types are seeded after
    # its migration. Build its pool from the live weapon catalog in that case.
    from app.shop import WEAPON_PRICES
    return [{'item_type': 'weapon', 'slug': weapon.slug, 'name': weapon.name,
             'price': WEAPON_PRICES[weapon.slug], 'tags': weapon.tags,
             'base_damage': 12 + list(WEAPON_PRICES).index(weapon.slug) * 2,
             'always_stocked': weapon.slug in ('sword', 'axe')}
            for weapon in db.scalars(select(WeaponType).where(WeaponType.slug.in_(WEAPON_PRICES))
                                     .order_by(WeaponType.name))]


def _refresh_raids(db, cadence, now):
    from app.journeys import build_plan
    for template in db.scalars(select(QuestTemplate).order_by(QuestTemplate.slug)):
        raw = template.journey or {}
        if (raw.get('raid') or {}).get('cadence') == cadence:
            build_plan(db, template, now=now)


def run_due_jobs(now=None, jobs=None):
    """Run each job once for the current UTC period; skipped periods are not replayed."""
    now = (now or utc_now()).astimezone(timezone.utc)
    requested = set(jobs) if jobs is not None else set(JOBS)
    completed = []
    with _sqlite_lock:
        for index, (key, cadence) in enumerate(JOBS.items()):
            if key not in requested:
                continue
            period, resets_at = period_window(cadence, now)
            with SessionLocal.begin() as db:
                if db.bind.dialect.name == 'postgresql':
                    db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': 74829110 + index})
                row = db.get(ScheduledJob, key)
                if row and row.period >= period and row.next_run_at > now.replace(tzinfo=None):
                    continue
                if key == 'shop_daily':
                    _refresh_shops(db, period, resets_at)
                else:
                    _refresh_raids(db, cadence, now)
                if row is None:
                    db.add(ScheduledJob(key=key, period=period, last_run_at=now.replace(tzinfo=None),
                                        next_run_at=resets_at.replace(tzinfo=None)))
                else:
                    row.period = period
                    row.last_run_at = now.replace(tzinfo=None)
                    row.next_run_at = resets_at.replace(tzinfo=None)
                completed.append(key)
    return completed


def current_shop_stock(db, shop_slug, now=None):
    period, _ = period_window('daily', now)
    return db.get(ShopRotation, (shop_slug, period))
