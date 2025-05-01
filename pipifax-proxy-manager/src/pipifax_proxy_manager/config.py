import json
import pathlib
import tomllib

from pipifax_proxy_manager import *


__all__ = [
    "build_proxied_session"
]

from pipifax_proxy_manager import JudgeManager


def load_config_file(config_file_path: pathlib.Path) -> tuple[dict, pathlib.Path]:
    return tomllib.loads(config_file_path.read_text("utf-8")), config_file_path.resolve()


def _build_proxy_provider(config: dict, config_path: pathlib.Path) -> ProxyProvider:
    proxy_provider = ProxyProvider(
        cache_file=config_path.parent / config["proxy-provider"]["cache_file"],
    )
    from . import proxy_fetchers

    fetchers = proxy_fetchers.parse_sites_json(json.loads((config_path.parent / config["proxy-provider"]["fetcher_sites_json"]).read_text("utf-8")))
    proxy_provider.proxy_fetchers = fetchers

    proxy_provider.judge_mgr = JudgeManager.create(
        urls=json.loads((config_path.parent / config["proxy-provider"]["judges_json"]).read_text("utf-8")),
        ip_resolvers=json.loads((config_path.parent / config["proxy-provider"]["ip_resolvers_json"]).read_text("utf-8")),
    )

    for attr, val in config["proxy-provider"]["attrs"].items():
        setattr(proxy_provider, attr, val)

    proxy_provider.init()

    return proxy_provider


def _build_proxied_session(config: dict, config_path: pathlib.Path):
    proxy_provider = _build_proxy_provider(config, config_path)
    return ProxiedSession(
        proxy_provider=proxy_provider
    )


def build_proxied_session(config_file_path: pathlib.Path):
    config, config_path = load_config_file(config_file_path)
    return _build_proxied_session(config, config_path)
