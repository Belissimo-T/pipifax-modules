import time
import typing

import urllib3

from pipifax_proxy_manager import *


# class CachedTimoutMixin:
#     def __init__(self, file: pathlib.Path, timeout: int):
#         self._file = file
#         self._timeout = timeout
#         try:
#             self._last_time = float(self._file.read_text("utf-8"))
#         except (OSError, ValueError):
#             logging.getLogger(__name__).warning("Failed to read last time from file at %s.", self._file,
#                                                 exc_info=True)
#             self._last_time = time.time()
#
#     def doit(self) -> bool:
#         now = time.time()
#         if now - self._last_time > self._timeout:
#             try:
#                 self._file.parent.mkdir(parents=True, exist_ok=True)
#                 self._file.write_text(str(now), "utf-8")
#             except OSError:
#                 logging.getLogger(__name__).critical("Failed to write last time to file at %s.", self._file,
#                                                      exc_info=True)
#             return True
#         else:
#             return False

class CachedTimoutMixin:
    def __init__(self, timeout: int):
        self._timeout = timeout
        self._last_time = 0

    def doit(self) -> bool:
        now = time.time()
        if now - self._last_time > self._timeout:
            self._last_time = now
            return True
        else:
            return False


class UrlProxyFetcher(ProxyFetcher, CachedTimoutMixin):
    def __init__(self, url: str, fmt: typing.Literal["url", "socks4", "socks5", "https"]):
        super().__init__(60 * 10)
        self.url = url
        self.fmt = fmt

    def fetch_proxies(self, proxy_provider: ProxyProvider | None) -> list[Proxy]:
        if not self.doit():
            return []

        response = urllib3.request("get", self.url)

        proxies = response.data.decode("utf-8").splitlines()

        if self.fmt == "url":
            return [Proxy.from_str(proxy) for proxy in proxies]
        else:
            return [
                Proxy(
                    scheme=self.fmt,
                    host=(parts := proxy_str.split(":", 1))[0],
                    port=int(parts[1]),
                    auth=None
                )
                for proxy_str in proxies
            ]

    def get_name(self):
        return f"({self.fmt}) {self.url}"


def parse_sites_json(data: dict) -> list[UrlProxyFetcher]:
    out = []

    for fmt, urls in data.items():
        for url in urls:
            out.append(UrlProxyFetcher(url, fmt))

    return out
