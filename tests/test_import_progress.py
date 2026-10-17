import asyncio
from uuid import uuid4

from cryptography.fernet import Fernet

from app.models import CheckResult
from app.pool import Pool, utc_now


async def test_import_reports_actual_completed_checks_before_commit(db):
    pool = await Pool.load(db, Fernet.generate_key())
    updates = []
    waiting = asyncio.Event()
    completed = asyncio.Event()

    async def verify(candidate):
        if candidate.host == "slow.invalid":
            await waiting.wait()
        else:
            completed.set()
        return CheckResult(proxy_id=1, ok=candidate.host != "slow.invalid", checked_at=utc_now())

    task = asyncio.create_task(
        pool.import_text(
            "fast.invalid:80\nslow.invalid:80",
            verify=verify,
            progress=lambda *values: updates.append(values),
        )
    )
    try:
        await asyncio.wait_for(completed.wait(), 2)
        await asyncio.sleep(0)
        assert ("testing", 2, 1, 0) in updates
        assert not pool.all()
        waiting.set()
        report = await task
        assert report.imported == 1 and report.failed == 1
        assert ("saving", 2, 2, 1) in updates
    finally:
        waiting.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_import_progress_api_tracks_totals_without_credentials(api):
    client, port = api
    identifier = str(uuid4())
    response = await client.post(
        "/api/proxies/import",
        headers={"X-Import-ID": identifier},
        json={"text": f"tester:synthetic-password@127.0.0.1:{port}"},
    )
    assert response.status_code == 200
    progress = await client.get("/api/proxies/imports/" + identifier)
    assert progress.status_code == 200
    assert progress.json()["stage"] == "done"
    assert progress.json()["completed"] == progress.json()["total"] == 1
    assert progress.json()["failed"] == 0
    assert "synthetic-password" not in progress.text and "127.0.0.1" not in progress.text
    assert (await client.get("/api/proxies/imports/" + str(uuid4()))).status_code == 404
    assert (
        await client.post(
            "/api/proxies/import",
            headers={"X-Import-ID": identifier},
            json={"text": "x.invalid:80"},
        )
    ).status_code == 409


async def test_explicit_protocol_override_is_applied_and_deduplicated(db):
    pool = await Pool.load(db, Fernet.generate_key())
    schemes = []

    async def verify(candidate):
        schemes.append(candidate.scheme)
        return CheckResult(proxy_id=1, ok=True, checked_at=utc_now())

    await pool.import_text("https://proxy.invalid:80", verify=verify)
    assert schemes == ["https"]  # Never silently downgrade an explicit TLS proxy.
    schemes.clear()
    result = await pool.import_text(
        "https://proxy.invalid:80\nhttp://proxy.invalid:80", scheme="http", verify=verify
    )
    assert schemes == ["http"]
    assert result.imported == 1 and result.skipped == 1


def test_import_tracking_is_bounded_and_expired_results_are_removed():
    from app.import_progress import ImportTracker

    tracker = ImportTracker()
    for i in range(100):
        progress = tracker.start(str(i))
        progress.update("done", 1, 1, 0)
    assert len(tracker.items) == 64
    for progress in tracker.items.values():
        progress.updated = -1000
    tracker.prune()
    assert not tracker.items
