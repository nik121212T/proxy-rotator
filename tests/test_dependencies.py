"""Check that the documented runtime dependencies load together."""

import importlib
import unittest


class DependencyTests(unittest.TestCase):
    def test_runtime_dependencies_import(self) -> None:
        for module in (
            "fastapi",
            "uvicorn",
            "pydantic",
            "pydantic_settings",
            "jinja2",
            "aiosqlite",
            "aiohttp",
            "aiohttp_socks",
            "cryptography.fernet",
            "cachetools",
            "click",
        ):
            with self.subTest(module=module):
                importlib.import_module(module)
