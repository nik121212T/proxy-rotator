from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet

from app.models import CheckResult
from app.parser import parse_line
from app.pool import Pool


async def test_exit_ip_changes_survive_reload_and_failed_checks(db):
    key = Fernet.generate_key()
    pool = await Pool.load(db, key)
    proxy = await pool.add(parse_line("tester:fake@host.invalid:80"))
    first = datetime.now(UTC)
    for i, ip in enumerate(["203.0.113.1", "203.0.113.2"]):
        await pool.apply_check(
            CheckResult(
                proxy_id=proxy.id,
                ok=True,
                exit_ip=ip,
                latency_ms=10,
                checked_at=first + timedelta(seconds=i),
            )
        )
    await pool.mark_fail(proxy.id, "timeout")
    observed = pool.exit_observations()[proxy.id]
    assert str(observed.ip) == "203.0.113.2"
    assert str(observed.previous_ip) == "203.0.113.1"
    assert observed.changed_at == first + timedelta(seconds=1)
    reloaded = await Pool.load(db, key)
    assert reloaded.exit_observations()[proxy.id] == observed
