import sys
import types
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Provide a tiny redis.asyncio stub so the cache module can import cleanly
# in lightweight test environments without optional dependencies installed.
fake_redis_asyncio = types.ModuleType("redis.asyncio")
fake_pydantic_settings = types.ModuleType("pydantic_settings")


class _RedisFactory:
    @staticmethod
    def from_url(_url):
        return None


fake_redis_asyncio.Redis = _RedisFactory
fake_redis_package = types.ModuleType("redis")
fake_redis_package.asyncio = fake_redis_asyncio


class _BaseSettings:
    def __init__(self, **kwargs):
        for name, value in self.__class__.__dict__.items():
            if name.startswith("_") or callable(value) or isinstance(value, property):
                continue
            setattr(self, name, kwargs.get(name, value))


class _SettingsConfigDict(dict):
    pass


fake_pydantic_settings.BaseSettings = _BaseSettings
fake_pydantic_settings.SettingsConfigDict = _SettingsConfigDict
sys.modules.setdefault("redis", fake_redis_package)
sys.modules.setdefault("redis.asyncio", fake_redis_asyncio)
sys.modules.setdefault("pydantic_settings", fake_pydantic_settings)

from app.services import cache as cache_service
from app.services import reservations as reservation_service


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, ttl, value):
        self.values[key] = value


class FakeResult:
    def __init__(self, scalar_value=None, row=None):
        self._scalar_value = scalar_value
        self._row = row

    def scalar(self):
        return self._scalar_value

    def fetchone(self):
        return self._row


class FakeSession:
    def __init__(self, result):
        self.result = result
        self.last_query = None
        self.last_params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query, params):
        self.last_query = str(query)
        self.last_params = params
        return self.result

    async def close(self):
        return None


class RevenueIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_key_is_tenant_scoped(self):
        fake_redis = FakeRedis()

        with patch.object(cache_service, "redis_client", fake_redis):
            with patch("app.services.reservations.calculate_total_revenue", new=AsyncMock(return_value={
                "property_id": "prop-001",
                "tenant_id": "tenant-a",
                "total": "2250.00",
                "raw_total": "2250.000",
                "currency": "USD",
                "count": 4,
            })) as calc_mock:
                first = await cache_service.get_revenue_summary("prop-001", "tenant-a")
                second = await cache_service.get_revenue_summary("prop-001", "tenant-b")

        self.assertEqual(first["tenant_id"], "tenant-a")
        self.assertEqual(second["tenant_id"], "tenant-a")
        self.assertEqual(calc_mock.await_count, 2)
        self.assertEqual(calc_mock.await_args_list[0].args, ("prop-001", "tenant-a"))
        self.assertEqual(calc_mock.await_args_list[1].args, ("prop-001", "tenant-b"))
        self.assertIn("revenue:tenant-a:prop-001", fake_redis.values)
        self.assertIn("revenue:tenant-b:prop-001", fake_redis.values)

    async def test_total_revenue_preserves_raw_amount_and_rounds_display_amount(self):
        fake_row = SimpleNamespace(
            total_revenue=Decimal("99.999"),
            reservation_count=2,
            currency="USD",
        )
        fake_session = FakeSession(FakeResult(row=fake_row))

        with patch.object(reservation_service.db_pool, "initialize", new=AsyncMock()):
            reservation_service.db_pool.session_factory = object()
            with patch.object(reservation_service.db_pool, "get_session", return_value=fake_session):
                result = await reservation_service.calculate_total_revenue("prop-001", "tenant-a")

        self.assertEqual(result["total"], "100.00")
        self.assertEqual(result["raw_total"], "99.999")
        self.assertEqual(result["count"], 2)

    async def test_monthly_revenue_query_uses_tenant_and_month_filters(self):
        fake_session = FakeSession(FakeResult(scalar_value=Decimal("1250.000")))

        total = await reservation_service.calculate_monthly_revenue(
            property_id="prop-001",
            tenant_id="tenant-a",
            month=3,
            year=2024,
            db_session=fake_session,
        )

        self.assertEqual(total, Decimal("1250.000"))
        self.assertEqual(fake_session.last_params["tenant_id"], "tenant-a")
        self.assertEqual(fake_session.last_params["month"], 3)
        self.assertEqual(fake_session.last_params["year"], 2024)
        self.assertIn("timezone(p.timezone, r.check_in_date)", fake_session.last_query)


if __name__ == "__main__":
    unittest.main()
