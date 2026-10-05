from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.database import SessionLocal
from app.models import RaidRotation, ScheduledJob, ShopRotation, ShopTable
from app.raids import rotation_info
from app.scheduled_refresh import (period_window, refresh_worker, run_due_jobs,
                                   rotated_stock, seconds_until_next_refresh)


def test_utc_boundaries_and_daily_stock_rotation():
    sunday = datetime(2030, 1, 6, 23, 59, tzinfo=timezone.utc)
    daily, daily_end = period_window('daily', sunday)
    weekly, weekly_end = period_window('weekly', sunday)
    assert daily == '2030-01-06' and daily_end.isoformat() == '2030-01-07T00:00:00+00:00'
    assert weekly == '2029-12-31' and weekly_end.isoformat() == '2030-01-07T00:00:00+00:00'
    items = [{'slug': 'staple', 'always_stocked': True}] + [{'slug': str(i)} for i in range(10)]
    stock = rotated_stock(items, 'first-period')
    assert len(stock) == 7 and stock[0]['slug'] == 'staple'
    assert rotated_stock(items, 'first-period') == stock
    assert rotated_stock(items, 'first-period', 1) != stock
    assert rotated_stock(items[:3], 'supplies', 0) != rotated_stock(items[:3], 'supplies', 1)


def test_scheduler_waits_for_nearest_utc_deadline():
    now = datetime(2030, 1, 6, 23, 59, 30, tzinfo=timezone.utc)
    assert seconds_until_next_refresh(now) == 30


def test_refresh_worker_sleeps_until_deadline(monkeypatch):
    import app.scheduled_refresh as scheduler

    calls = []

    class StopAfterWait:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            calls.append(('wait', seconds))
            self.stopped = True

    monkeypatch.setattr(scheduler, 'run_due_jobs',
                        lambda jobs: calls.append(('run', jobs[0])))
    monkeypatch.setattr(scheduler, 'utc_now', lambda: datetime(
        2030, 1, 6, 23, 59, 30, tzinfo=timezone.utc))

    refresh_worker(StopAfterWait())

    assert calls == [('run', key) for key in scheduler.JOBS] + [('wait', 30)]


def test_refresh_worker_uses_short_retry_after_failure(monkeypatch):
    import app.scheduled_refresh as scheduler

    waits = []

    class StopAfterWait:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            waits.append(seconds)
            self.stopped = True

    def fail_one_job(jobs):
        if jobs[0] == 'raid_daily':
            raise RuntimeError('database unavailable')

    monkeypatch.setattr(scheduler, 'run_due_jobs', fail_one_job)
    refresh_worker(StopAfterWait(), retry_seconds=17)
    assert waits == [17]


def test_scheduler_catches_up_once_after_missed_days(client):
    first = datetime.now(timezone.utc).replace(hour=1, minute=0, second=0, microsecond=0) + timedelta(days=10)
    second = first + timedelta(days=3)
    with SessionLocal() as db:
        saved = {row.key: (row.period, row.last_run_at, row.next_run_at)
                 for row in db.scalars(select(ScheduledJob))}
    try:
        assert set(run_due_jobs(first)) == {'raid_daily', 'raid_weekly', 'shop_daily'}
        assert run_due_jobs(first) == []
        with SessionLocal() as db:
            period, end = period_window('daily', first)
            rotation = db.get(ShopRotation, ('mosswood-market', period))
            assert rotation and rotation.resets_at == end.replace(tzinfo=None)
            gear = rotation.stock['mosswood-market-gear']
            assert any(item['slug'] == 'leather-coat' for item in gear)
            assert len(gear) < len(db.get(ShopTable, 'mosswood-market-gear').items)
            assert db.get(RaidRotation, rotation_info('daily-raid', 'daily', first)['key'])
        due = run_due_jobs(second)
        assert 'shop_daily' in due and 'raid_daily' in due
        assert run_due_jobs(second) == []
        with SessionLocal() as db:
            assert db.get(ScheduledJob, 'shop_daily').period == period_window('daily', second)[0]
            assert db.get(ShopRotation, ('mosswood-market', period_window('daily', second)[0]))
    finally:
        with SessionLocal.begin() as db:
            for key, previous in saved.items():
                row = db.get(ScheduledJob, key)
                row.period, row.last_run_at, row.next_run_at = previous
            for moment in (first, second):
                db.execute(delete(ShopRotation).where(ShopRotation.period == period_window('daily', moment)[0]))
                for slug, cadence in [('daily-raid', 'daily'), ('weekly-raid', 'weekly')]:
                    key = rotation_info(slug, cadence, moment)['key']
                    db.execute(delete(RaidRotation).where(RaidRotation.key == key))


def test_shop_only_sells_current_stock(client):
    catalog = client.get('/api/shop/villages')
    assert catalog.status_code == 200
    shop = catalog.json()[0]['shops'][0]
    assert shop['resets_at'] and shop['server_time']
    shown = {item['slug'] for table in shop['tables'] for item in table['items']}
    with SessionLocal() as db:
        unavailable = next(item for table in db.scalars(select(ShopTable))
                           for item in table.items if item['slug'] not in shown)
    hero = client.post('/api/adventurers', json={'name': 'Shop rotation buyer'}).json()
    from uuid import UUID
    from app.models import Adventurer
    with SessionLocal.begin() as db:
        db.get(Adventurer, UUID(hero['id'])).gold = 1000
    response = client.post('/api/shop/purchase', json={'adventurer_id': hero['id'],
                           'item_type': unavailable['item_type'], 'item_slug': unavailable['slug']})
    assert response.status_code == 409
