"""Exercise the portal's built icon URLs through the application router."""

import importlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

import anyio.to_thread
import httpx
from psycopg2 import pool
from starlette.routing import Mount


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend-web"


class OfflineThreadedConnectionPool:
    def __init__(self, *_args, **_kwargs):
        pass

    def getconn(self):
        raise AssertionError("database access is disabled in icon HTTP tests")

    def putconn(self, _connection):
        pass


async def run_file_operation_inline(func, *args, **kwargs):
    # This sandbox cannot start AnyIO worker threads. Keep Starlette's real
    # StaticFiles/FileResponse behavior while performing their file IO inline.
    kwargs.pop("abandon_on_cancel", None)
    kwargs.pop("cancellable", None)
    kwargs.pop("limiter", None)
    return func(*args, **kwargs)


class PortalModuleIconsHttpTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.runtime = Path(cls.temporary.name)
        cls.dist = cls.runtime / "dist"
        cls.icons = cls.dist / "module-icons"
        cls.icons.mkdir(parents=True)
        (cls.dist / "assets").mkdir()
        (cls.dist / "index.html").write_text(
            "<!doctype html><html><body>RemiHub Portal</body></html>",
            encoding="utf-8",
        )
        (cls.dist / "assets" / "app.js").write_text(
            "window.remihub = true;", encoding="utf-8"
        )
        shutil.copyfile(FRONTEND / "public" / "favicon.png", cls.dist / "favicon.png")
        shutil.copyfile(
            FRONTEND / "public" / "remihub-favicon.svg",
            cls.dist / "remihub-favicon.svg",
        )

        # The frontend renders these values as <img src={module.icon}>.
        app_source = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")
        cls.icon_urls = re.findall(r'icon: "(/module-icons/[^"/]+\.svg)"', app_source)
        if "src={module.icon}" not in app_source:
            raise AssertionError("landing-page cards no longer render module.icon")
        if len(cls.icon_urls) != 8 or len(set(cls.icon_urls)) != 8:
            raise AssertionError("expected eight distinct rendered module icon URLs")
        for url in cls.icon_urls:
            shutil.copyfile(FRONTEND / "public" / url.lstrip("/"), cls.dist / url.lstrip("/"))

        # Import the real application without reading external configuration or
        # opening a database connection. ASGITransport does not start its lifespan.
        config = cls.runtime / "config.ini"
        config.write_text(
            "[Database]\nuser = test\npassword = test\nhost = localhost\n"
            "port = 5432\ndatabase = test\n"
            "[Weather]\napi_key = test\napi_app_key = test\n",
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {
                "REMIHUB_CONFIG_FILE": str(config),
                "REMIHUB_DATABASE_CONFIG": str(config),
                "REMIHUB_ENV_FILE": str(cls.runtime / "missing.env"),
                "REMIHUB_LOG_DIR": str(cls.runtime / "logs"),
            },
        ), patch.object(pool, "ThreadedConnectionPool", OfflineThreadedConnectionPool):
            cls.main = importlib.import_module("backend.main")

        cls.mounts = {
            route.path: route.app
            for route in cls.main.app.routes
            if isinstance(route, Mount)
        }
        cls.directory_patches = [
            patch.object(cls.mounts["/module-icons"], "directory", str(cls.icons)),
            patch.object(cls.mounts["/module-icons"], "all_directories", [str(cls.icons)]),
            patch.object(cls.mounts["/assets"], "directory", str(cls.dist / "assets")),
            patch.object(
                cls.mounts["/assets"], "all_directories", [str(cls.dist / "assets")]
            ),
            patch.object(cls.mounts["/race"], "directory", str(cls.dist)),
            patch.object(cls.mounts["/race"], "all_directories", [str(cls.dist)]),
            patch.object(cls.main, "WEB_DIST_DIR", cls.dist),
        ]
        for directory_patch in cls.directory_patches:
            directory_patch.start()
            cls.addClassCleanup(directory_patch.stop)

    async def asyncSetUp(self):
        self.file_io_patch = patch.object(
            anyio.to_thread, "run_sync", run_file_operation_inline
        )
        self.file_io_patch.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.main.app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        self.file_io_patch.stop()

    async def test_all_rendered_module_icon_urls_deliver_svg_images(self):
        for url in self.icon_urls:
            with self.subTest(url=url):
                response = await self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "image/svg+xml")
                self.assertEqual(
                    response.content,
                    (FRONTEND / "public" / url.lstrip("/")).read_bytes(),
                )
                svg = ElementTree.fromstring(response.content)
                self.assertEqual(svg.tag, "{http://www.w3.org/2000/svg}svg")
                self.assertTrue(list(svg), "SVG contains no visible shapes")

    async def test_missing_icon_does_not_fall_back_to_html(self):
        response = await self.client.get("/module-icons/missing.svg")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("text/html", response.headers.get("content-type", ""))

    async def test_existing_web_routes_and_auth_boundary(self):
        for url, media_type in (
            ("/", "text/html"),
            ("/portal", "text/html"),
            ("/race/", "text/html"),
            ("/assets/app.js", "text/javascript"),
            ("/favicon.png", "image/png"),
            ("/remihub-favicon.svg", "image/svg+xml"),
        ):
            with self.subTest(url=url):
                response = await self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["content-type"].startswith(media_type))

        with patch.dict(os.environ, {"REMIHUB_AUTH_MODE": "required"}):
            self.assertEqual((await self.client.get("/auth/me")).status_code, 401)
            self.assertEqual((await self.client.get("/agent/cards")).status_code, 401)


if __name__ == "__main__":
    unittest.main()
