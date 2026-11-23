"""Download Stratechery's public RSS with an isolated Firefox session."""

import json
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree


def download_feed(url: str) -> str:
    with TemporaryDirectory(prefix="stratechery-firefox-") as directory:
        root = Path(directory)
        profile = root / "profile"
        downloads = root / "downloads"
        profile.mkdir()
        downloads.mkdir()
        preferences = {
            "browser.download.folderList": 2,
            "browser.download.dir": str(downloads),
            "browser.download.useDownloadDir": True,
            "browser.helperApps.neverAsk.saveToDisk": (
                "application/rss+xml,application/xml,text/xml,application/octet-stream"
            ),
            "network.proxy.type": 0,
        }
        (profile / "user.js").write_text(
            "".join(
                f"user_pref({json.dumps(key)}, {json.dumps(value)});\n"
                for key, value in preferences.items()
            ),
            encoding="utf-8",
        )
        try:
            browser = subprocess.Popen(
                ["firefox", "--headless", "--no-remote", "--profile", str(profile), url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("未找到 Firefox 可执行文件") from exc

        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                for file in downloads.iterdir():
                    if file.suffix == ".part":
                        continue
                    try:
                        text = file.read_text(encoding="utf-8")
                        feed = ElementTree.fromstring(text)
                    except (FileNotFoundError, UnicodeError, ElementTree.ParseError):
                        continue
                    if feed.tag == "rss" and feed.find("./channel/item") is not None:
                        return text
                if browser.poll() is not None:
                    raise RuntimeError(f"Firefox 提前退出，退出码 {browser.returncode}")
                time.sleep(0.25)
            raise RuntimeError("Firefox 在 25 秒内未下载到包含文章的 RSS")
        finally:
            if browser.poll() is None:
                browser.terminate()
                try:
                    browser.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    browser.kill()
                    browser.wait()
