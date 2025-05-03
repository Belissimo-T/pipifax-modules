import abc
import base64
import collections
import concurrent.futures
import contextlib
import dataclasses
import datetime
import functools
import itertools
import logging
import pathlib
import random
import threading
import time
import typing

import curl_cffi.requests
import curl_cffi.requests.exceptions
import urllib3

import pipifax_io.serializable
import pipifax_io.saferw
import pipifax_io.serializable_errors

__all__ = [
    "BasicAuth",
    "ProxyData",
    "Proxy",
    "Proxies",
    "ProxyFetcher",
    "ProxyProvider",
    "ProxiedSession",
    "RetryError",
    "ProxyBlockedError",
    "ProxyBrokenError"
]


@dataclasses.dataclass
class BasicAuth(pipifax_io.serializable.SimpleSerializable):
    login: str
    password: str


@dataclasses.dataclass
class ProxyData(pipifax_io.serializable.SimpleSerializable):
    __exclude_fields__: typing.ClassVar[set[str]] = {"_currently_in_use", "_last_yielded"}

    auth: BasicAuth | None = None
    score5: float = 1
    score25: float = 1
    score100: float = 1
    tries: int = 0

    last_worked: datetime.datetime | None = None
    # last_retried: datetime.datetime | None = None
    last_blocked: dict[str, tuple[datetime.datetime, int]] = dataclasses.field(default_factory=dict)
    last_used: dict[str, datetime.datetime] = dataclasses.field(default_factory=dict)
    last_used_global: datetime.datetime = datetime.datetime.min

    last_judged: datetime.datetime = datetime.datetime.min
    anonymity_level: str | None = None

    _last_yielded: float | None = None

    def to_proxy(self, scheme: str, url: str, port: int) -> "Proxy":
        return Proxy(scheme, url, port, self.auth, _proxy_data=self)


@dataclasses.dataclass
class Proxy:
    scheme: str
    host: str
    port: int
    auth: BasicAuth | None = None

    _proxy_data: ProxyData = None

    @property
    def _key(self) -> tuple[str, str, int]:
        return self.scheme, self.host, self.port

    def to_str(self) -> str:
        if self.scheme.startswith("http") and self.auth is not None:
            return f"{self.scheme}://{self.auth.login}:{self.auth.password}@{self.host}:{self.port}"
        else:
            return f"{self.scheme}://{self.host}:{self.port}"

    def to_str_no_auth(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @classmethod
    def from_str(cls, s: str) -> "Proxy":
        url = urllib3.util.url.parse_url(s)
        if url.auth is not None:
            auth = BasicAuth(*url.auth.split(":"))
        else:
            auth = None

        return cls(url.scheme, url.host, url.port, auth)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._last_yielded = None


@dataclasses.dataclass
class Proxies(pipifax_io.serializable.SimpleSerializable):
    proxies: dict[tuple[str, str, int], ProxyData] = dataclasses.field(default_factory=dict)

    def _get_proxy_data(self, scheme: str, url: str, port: int) -> ProxyData:
        return self.proxies[scheme, url, port]

    def contains_proxy(self, scheme: str, url: str, port: int) -> bool:
        return (scheme, url, port) in self.proxies

    def add_proxy(self, proxy: Proxy):
        if not self.contains_proxy(*proxy._key):
            self.proxies[proxy._key] = ProxyData(proxy.auth)

    def __len__(self):
        return len(self.proxies)


class ProxyFetcher(abc.ABC):
    @abc.abstractmethod
    def fetch_proxies(self, proxy_provider: "ProxyProvider | None") -> list[Proxy]:
        pass

    def get_name(self) -> str:
        return self.__class__.__name__


class _BreakException(Exception):
    pass


class ProxyProvider:
    def __init__(self, cache_file: pathlib.Path):
        self._logger = logging.getLogger(self.__class__.__name__)
        self.cache_file = cache_file

        self.proxies: Proxies = Proxies()

        self.proxy_fetchers: list[ProxyFetcher] = []
        self.judge_mgr: JudgeManager | None = None
        # self.delete_after: datetime.timedelta = datetime.timedelta(days=20)

        self.blocked_tries = 8
        self.score_window_size = 50

        self.fetch_interval = 10000
        self._last_fetched = datetime.datetime.now()
        self.rejudge_interval = 60 * 60 * 24
        self._last_rejudge_checked = datetime.datetime.now()

        self.save_interval = 60
        self.score_random_choice_exponent = 8
        self.shard_interval = 200
        self.shard_threshold = 0.10
        self.shard_length = 200
        self.shard_choices = 5
        self.scoring_min_tries = 3
        self.scoring_untested_score = 1
        self.min_yield_delay = 0.5

        self.do_rechoose_shard = False
        self.executor = concurrent.futures.ThreadPoolExecutor()
        self.rejudge_workers = 5
        self.rejudge_executor = None

        self._store_lock = threading.Lock()
        self._proxies_lock = threading.Lock()
        self._update_save_lock = threading.Lock()
        self._rejudge_lock = threading.Lock()
        self._shard_lock = threading.Lock()
        self._fetch_lock = threading.Lock()

        # TODO: auto delete service entries

        self._i = 0

    def init(self):
        self.rejudge_executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.rejudge_workers)
        self.load_proxies()
        self._shard()
        self._update_save()
        if len(self.proxies.proxies) == 0:
            self.fetch_proxies()
        self.rejudge_proxies()

    def _get_proxy_effective_score(self, proxy_data: ProxyData) -> float:
        if proxy_data.tries < self.scoring_min_tries:
            return self.scoring_untested_score

        s_since_last_used = (datetime.datetime.now() - proxy_data.last_used_global).total_seconds()

        # TODO: maybe continuous?
        if s_since_last_used < 30 or (proxy_data.score5 > proxy_data.score25):
            return proxy_data.score5
        elif s_since_last_used < 60 * 5:  # or (proxy_data.score25 > proxy_data.score100):
            return proxy_data.score25
        else:
            return proxy_data.score100

    def load_proxies(self):
        file = self.cache_file.resolve()
        self._logger.debug(f"* Loading proxies from: {file}")
        try:
            self.proxies = Proxies.deserialize(pipifax_io.saferw.safe_read_bytes(file))
        except FileNotFoundError:
            self._logger.info("=> No proxies cached yet.")
        except pipifax_io.serializable_errors.SerializationError:
            self._logger.error("=> Invalid proxy cache file!", exc_info=True)
        else:
            self._logger.info(f"=> Loaded {len(self.proxies)} proxies.")

    def store_proxies(self):
        with self._store_lock:
            self._logger.debug(f"* Storing proxies at {str(self.cache_file)!r}.")

            pipifax_io.saferw.safe_write_bytes(self.cache_file, self.proxies.serialize())

    def add_proxy(self, proxy: Proxy, _no_save: bool = False):
        self._logger.debug(f"=> Adding {proxy.to_str()} to proxy pool.")
        with self._proxies_lock:
            self.proxies.add_proxy(proxy)

        if not _no_save:
            self._update_save()

    def fetch_proxies(self):
        a = self._fetch_lock.acquire(blocking=False)
        if not a:
            self._logger.warning("Fetch lock already acquired. Skipping fetching. (%s)", self._i)
            return

        try:

            self._logger.info("* Fetching proxies. (%s)", self._i)
            tot = 0
            tot_new = 0
            errs = 0

            for proxy_fetcher in self.proxy_fetchers:
                self._logger.info(f"=> {proxy_fetcher.get_name()!r}...")
                try:
                    # TODO give self to proxy fetcher?
                    _proxies = proxy_fetcher.fetch_proxies(None)
                except Exception as e:
                    self._logger.error("=> Error fetching proxies.", exc_info=True)
                    errs += 1
                    continue
                else:
                    tot += len(_proxies)

                    if not _proxies:
                        self._logger.info(f"=> {proxy_fetcher.get_name()!r} returned no proxies.")
                        continue

                    f_new = 0

                    for proxy in _proxies:
                        if self.contains_proxy(proxy):
                            continue

                        if proxy.scheme not in ("https", "socks4", "socks5"):
                            self._logger.debug(f" => Proxy {proxy.to_str()} has invalid scheme.")
                            continue

                        self.add_proxy(proxy, _no_save=True)

                        f_new += 1

                    self._logger.info(f" -> Got {f_new} / {len(_proxies)} = {f_new / len(_proxies):.2%} new proxies.")

                    tot_new += f_new

            self._logger.info(f"=> Fetched {tot} proxies. {tot_new} new. {errs} errors.")

            self.store_proxies()
        finally:
            self._fetch_lock.release()

    def iterate_proxies(
        self,
        reason: str | None = None,
        rotation_rate: float | None = None,
        blocked_grace: float | None = 60 * 5,
        anonymity_level: typing.Literal["elite", "anonymous"] | None = None,
    ) -> typing.Generator[Proxy, None, None]:
        """

        :param blocked_grace: If a proxy was marked blocked less than `blocked_grace` seconds ago, it will not be used.
        """
        assert blocked_grace is None or blocked_grace > 0

        i = 0
        for p in self._iterate_proxies(reason, rotation_rate, blocked_grace, anonymity_level):
            self._logger.log(
                logging.DEBUG - 2,
                "* Yielding proxy. "
                "Score: %.2f | %.2f | %.2f . "
                "Tries: %d. "
                "%s",

                p._proxy_data.score100,
                p._proxy_data.score25,
                p._proxy_data.score5,
                p._proxy_data.tries,
                p.to_str()
            )
            yield p
            i += 1

            if i % 20 == 0:
                self._logger.warning(f"Yielded %s proxies.", i)

    def _shard(self):
        a = self._shard_lock.acquire(blocking=False)
        if not a:
            self._logger.warning("Shard lock already acquired. Skipping sharding.")
            return

        try:
            self._logger.debug(" * Sharding proxies. (%s)", self._i)
            new_shards = [Proxies() for _ in range(len(self.proxies.proxies) // self.shard_length + 1)]
            shard_i = 0
            n_proxies_untested = 0

            sorted_proxies = sorted(
                self.proxies.proxies.items(),
                key=lambda x: self._get_proxy_effective_score(x[1]),
                reverse=True
            )

            for i, (key, val) in enumerate(sorted_proxies):
                if self._get_proxy_effective_score(val) < self.shard_threshold:
                    self._logger.debug(" => Stopping sharding at %s.", i + 1)
                    break

                if val.tries < 3:
                    n_proxies_untested += 1

                new_shards[shard_i].proxies[key] = val

                shard_i += 1
                shard_i %= len(new_shards)

            self.shards = new_shards
            # self.do_rechoose_shard = n_proxies_untested > self.shard_length / 2

            self._logger.debug(" => Sharded into %d shards.", len(new_shards))
        finally:
            self._shard_lock.release()

    def _iterate_proxies(
        self,
        reason: str | None,
        rotation_rate: float | None,
        blocked_grace: float | None,
        anonymity_level: typing.Literal["elite", "anonymous"] | None = None,
    ):
        t0 = time.perf_counter()

        assert not (reason is not None is blocked_grace)

        # tracks how many times a proxy was yielded
        proxies_n: dict[tuple[str, str, int], int] = collections.defaultdict(int)

        while True:
            shard = random.choice(self.shards)

            proxies = shard.proxies.copy()

            # proxies, cum_weights = self._get_cached_cum_weights()
            i = 0
            while proxies and i < self.shard_choices:
                try:
                    # if self.do_rechoose_shard:
                    #     choices = list(proxies.items())
                    # else:
                    t1 = time.perf_counter()
                    choices = random.choices(
                        population=list(proxies.items()),
                        weights=[self._get_proxy_effective_score(p) ** self.score_random_choice_exponent for p in
                                 proxies.values()],
                        k=self.shard_choices * 2
                    )
                    t2 = time.perf_counter()
                    # self._logger.debug(f"Random choice took %.2fms.", (t2 - t1) * 1000)
                    proxy_data: ProxyData
                except ValueError:
                    # total weight == 0
                    break

                for p_addr, proxy_data in choices:
                    now_ts = time.time()
                    now = datetime.datetime.now()

                    do_exclude_proxy = False

                    if (
                        proxy_data._last_yielded is not None
                        and (now_ts - proxy_data._last_yielded) < self.min_yield_delay
                    ):
                        continue

                    with contextlib.suppress(_BreakException):
                        wrong_anonymity = (
                            (anonymity_level == "elite" and proxy_data.anonymity_level != "elite")
                            or (anonymity_level == "anonymous"
                                and proxy_data.anonymity_level not in ("elite", "anonymous"))
                        )
                        if wrong_anonymity:
                            do_exclude_proxy = True
                            raise _BreakException

                        if proxies_n[p_addr] >= 3:
                            do_exclude_proxy = True
                            raise _BreakException

                        needs_rotate = (
                            rotation_rate is not None
                            and (
                                now - proxy_data.last_used.get(reason, datetime.datetime.min)
                            ).total_seconds() < rotation_rate
                        )
                        if needs_rotate:
                            do_exclude_proxy = True
                            raise _BreakException

                        if reason is not None:
                            try:
                                last_blocked, tries = proxy_data.last_blocked[reason]
                            except KeyError:
                                pass
                            else:
                                if (
                                    tries >= self.blocked_tries
                                ) and (now - last_blocked).total_seconds() < blocked_grace:
                                    do_exclude_proxy = True
                                    raise _BreakException

                    if do_exclude_proxy:
                        del proxies[p_addr]
                        break

                    proxies_n[p_addr] += 1
                    proxy_data._last_yielded = now_ts

                    _dt = time.perf_counter() - t0
                    self._logger.log(logging.DEBUG - 3, "Proxy Yield took %.2fms. %s", _dt * 1000,
                                     self.do_rechoose_shard)

                    yield proxy_data.to_proxy(*p_addr)

                    # if self.do_rechoose_shard:
                    #     return

                i += 1

    def _log_error(self, func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception:
                self._logger.error(f"Error while executing {func}", exc_info=True)

        return wrapper

    def _update_save(self):
        t1 = time.perf_counter()

        now = datetime.datetime.now()
        with self._update_save_lock:
            self._i = i = self._i + 1

            if i % 20 == 0:
                if (now - self._last_fetched).total_seconds() > self.fetch_interval:
                    self._last_fetched = now
                    self.executor.submit(self._log_error(self.fetch_proxies))

                if (now - self._last_rejudge_checked).total_seconds() > self.rejudge_interval / 2:
                    self._last_rejudge_checked = now
                    self.executor.submit(self._log_error(self.rejudge_proxies))

        if i % self.save_interval == 0:
            self.executor.submit(self._log_error(self.store_proxies))

        if i % self.shard_interval == 0:
            self._shard()

        t2 = time.perf_counter()
        self._logger.log(logging.DEBUG - 3, f"Update save took %.2fms.", (t2 - t1) * 1000)

    def _rejudge(self, p_addr, proxy_data: ProxyData):
        proxy = proxy_data.to_proxy(*p_addr)
        status = self.judge_mgr.judge(self, proxy)
        proxy_data.anonymity_level = status
        proxy_data.last_judged = datetime.datetime.now()

    def rejudge_proxies(self):
        a = self._rejudge_lock.acquire(blocking=False)
        if not a:
            self._logger.warning("Rejudge lock already acquired. Skipping rejudging. (%s)", self._i)
            return

        try:
            self._logger.info("Rejudging proxies.")
            now = datetime.datetime.now()
            self.judge_mgr.create_judges()

            for p_addr, proxy in self.proxies.proxies.items():
                if proxy.tries > self.scoring_min_tries and proxy.last_worked is None:
                    continue
                elif self._get_proxy_effective_score(proxy) < self.shard_threshold:
                    continue

                if (now - proxy.last_judged).total_seconds() > self.rejudge_interval:
                    # self._logger.debug(f" => Rejudging {p_addr}.")
                    self.rejudge_executor.submit(self._log_error(self._rejudge), p_addr, proxy)
        finally:
            self._rejudge_lock.release()

    @typing.overload
    def proxy_feedback(self, proxy: Proxy, worked: bool, blocked: typing.Literal[True], reason: str):
        pass

    @typing.overload
    def proxy_feedback(self, proxy: Proxy, worked: bool, blocked: typing.Literal[False] = False,
                       reason: str | None = None):
        pass

    def proxy_feedback(self, proxy: Proxy, worked: bool, blocked: bool = False, reason: str | None = None):
        self._logger.log(
            logging.DEBUG - 1,
            f"* Proxy feedback: %s. Worked: %s. Blocked: %s. Reason: %s.",
            proxy.to_str(),
            worked,
            blocked,
            reason
        )

        assert not (blocked is None is reason), "When marking a proxy as blocked, a reason must be provided."

        with self._proxies_lock:
            _proxy = proxy._proxy_data

            if blocked:
                if reason in _proxy.last_blocked:
                    _, tries = _proxy.last_blocked[reason]
                else:
                    tries = 0

                _proxy.last_blocked[reason] = (datetime.datetime.now(), tries + 1)
            else:
                if reason is not None:
                    try:
                        del _proxy.last_blocked[reason]
                    except KeyError:
                        pass

            _proxy.tries += 1

            _proxy.score5 = (_proxy.score5 * min(_proxy.tries, 5) + worked) / (min(_proxy.tries, 5) + 1)
            _proxy.score25 = (_proxy.score25 * min(_proxy.tries, 25) + worked) / (min(_proxy.tries, 25) + 1)
            _proxy.score100 = (_proxy.score100 * min(_proxy.tries, 100) + worked) / (min(_proxy.tries, 100) + 1)

            _proxy.last_used_global = now = datetime.datetime.now()

            if worked:
                # TODO: last worked per reason
                _proxy.last_worked = _proxy.last_used[reason] = now

        self._update_save()

    def contains_proxy(self, proxy: Proxy) -> bool:
        return self.proxies.contains_proxy(*proxy._key)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.rejudge_executor.shutdown(wait=False, cancel_futures=True)

    def __del__(self):
        self.store_proxies()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.store_proxies()


class RetryError(Exception):
    pass


class ProxyBlockedError(RetryError):
    pass


class ProxyBrokenError(RetryError):
    pass


class ProxiedSession:
    def __init__(
        self,
        proxy_provider: ProxyProvider,
        ignore_ssl: bool = True,
    ):
        self.proxy_provider = proxy_provider
        self.ignore_ssl = ignore_ssl

    def request[T](
        self,
        curl_kwargs: dict[str, typing.Any],
        handler: typing.Callable[[concurrent.futures.Future[curl_cffi.requests.Response], Proxy, int], T],
        anonymity_level: typing.Literal["elite", "anonymous"] | None = None,
        preferred_proxy: Proxy | None = None,
        rotation_rate: float | None = None,
        blocked_grace: float | None = 60 * 5,
        reason: str | None = None,
    ) -> T:
        curl_kwargs = dict(timeout=5) | curl_kwargs
        t0 = time.perf_counter()

        preferred_proxy_it = itertools.repeat(preferred_proxy, 3) if preferred_proxy is not None else []

        endpoint = urllib3.util.url.parse_url(curl_kwargs["url"])
        reason = reason if reason is not None else f"{endpoint.scheme}://{endpoint.netloc}{endpoint.path or ''}"

        proxy_it = self.proxy_provider.iterate_proxies(
            reason=reason,
            rotation_rate=rotation_rate,
            blocked_grace=blocked_grace,
            anonymity_level=anonymity_level
        )

        for _proxy_i, proxy in enumerate(itertools.chain(preferred_proxy_it, proxy_it)):
            fut = concurrent.futures.Future()
            try:
                t1 = time.perf_counter()

                with proxy:
                    # noinspection PyTypeChecker
                    fut.set_result(
                        curl_cffi.requests.request(
                            proxy=proxy.to_str_no_auth(),
                            proxy_auth=(proxy.auth.login, proxy.auth.password) if proxy.auth is not None else None,
                            verify=not self.ignore_ssl,
                            **curl_kwargs,
                        )
                    )

                total = time.perf_counter() - t1
                if total > curl_kwargs["timeout"] + 1:
                    logging.getLogger(self.__class__.__name__).warning(f"Request took {total:.2f}s.")
            except curl_cffi.requests.exceptions.Timeout:
                self.proxy_provider.proxy_feedback(proxy, False, False, reason)
                continue
            except curl_cffi.requests.exceptions.ProxyError:
                self.proxy_provider.proxy_feedback(proxy, False, False, reason)
                continue
            except curl_cffi.requests.exceptions.ConnectionError:
                self.proxy_provider.proxy_feedback(proxy, False, False, reason)
                continue
            except curl_cffi.requests.exceptions.TooManyRedirects:
                self.proxy_provider.proxy_feedback(proxy, False, False, reason)
                continue
            except curl_cffi.requests.exceptions.IncompleteRead:
                self.proxy_provider.proxy_feedback(proxy, False, False, reason)
                continue
            except Exception as e:
                fut.set_exception(e)

            if not fut.done():
                raise AssertionError("Future not done.")

            try:
                out = handler(fut, proxy, _proxy_i)
            except ProxyBrokenError:
                self.proxy_provider.proxy_feedback(proxy, False, False, reason)
                continue
            except ProxyBlockedError:
                self.proxy_provider.proxy_feedback(proxy, True, True, reason)
                continue
            except RetryError:
                continue
            except Exception:
                raise
            else:
                self.proxy_provider.proxy_feedback(proxy, True, False, reason)
                logging.getLogger(self.__class__.__name__).log(
                    logging.DEBUG - 1,
                    "Request took %s proxy tries and %.3f s.",
                    _proxy_i + 1, time.perf_counter() - t0
                )
                return out

    def __enter__(self):
        self.proxy_provider.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self.proxy_provider.__exit__(exc_type, exc_val, exc_tb)


class JudgeCreateError(Exception):
    """Could not create Judge instance."""


@dataclasses.dataclass
class Judge:
    url: str
    no_proxy_response: dict[str, str]

    @classmethod
    def create(cls, url: str):
        logger = logging.getLogger(__class__.__name__ + "[" + url + "]")

        # logger.info(f"Testing.")
        usr, passwd = random.randbytes(2).hex(), random.randbytes(2).hex()
        cust_header, cust_header_val = random.randbytes(2).hex(), random.randbytes(2).hex()
        basic_auth = base64.b64encode(f"{usr}:{passwd}".encode()).decode()
        try:
            response = curl_cffi.requests.get(
                url,
                # impersonate="chrome",
                timeout=5,
                headers={
                    "Authorization": basic_auth,
                    cust_header: cust_header_val,
                }
            )
        except curl_cffi.requests.exceptions.RequestException as e:
            logger.info(f"Failed (Exception)")
            raise JudgeCreateError from e

        if response.status_code != 200:
            logger.info(f"Failed. (status code %d)", response.status_code)
            raise JudgeCreateError(f"Judge {url} returned status code {response.status_code}.")

        text = response.text.lower()

        for fragment in (basic_auth, cust_header, cust_header_val):
            if fragment.lower() not in text:
                logger.info(f"Failed. (missing fragments)")
                raise JudgeCreateError(f"Judge {url} returned text containing {fragment}.")

        logger.info(f"Works!")
        return cls(
            url=url,
            no_proxy_response={
                "via": text.count("via"),
                # "x-forwarded-for": text.count("x-forwarded-for"),
                "forwarded": text.count("forwarded"),
                "proxy": text.count("proxy"),
            }
        )

    def judge(
        self,
        proxy_provider: ProxyProvider,
        proxy: Proxy,
        curr_ips: set[str]
    ) -> typing.Literal["elite", "anonymous", "transparent"] | None:
        logger = logging.getLogger(__class__.__name__).getChild(self.url)
        # elite: server does not know that it is a proxy
        # anonymous: server knows that it is a proxy, but does not know the IP
        # transparent: server knows that it is a proxy and the IP
        logger.log(logging.DEBUG - 1, f"Judging proxy {proxy.to_str()}...")
        for _ in range(3):
            try:
                response = curl_cffi.requests.get(
                    url=self.url,
                    # impersonate="chrome",
                    proxy=proxy.to_str_no_auth(),
                    proxy_auth=(proxy.auth.login, proxy.auth.password) if proxy.auth is not None else None,
                    verify=False,
                    timeout=5,
                    # auth=("HELLO", "HELLO_PASSWORD")
                )
                break
            except curl_cffi.requests.exceptions.Timeout:
                proxy_provider.proxy_feedback(proxy, False, False, self.url)
            except curl_cffi.requests.exceptions.ProxyError:
                proxy_provider.proxy_feedback(proxy, False, False, self.url)
            except curl_cffi.requests.exceptions.ConnectionError:
                proxy_provider.proxy_feedback(proxy, False, False, self.url)
            except curl_cffi.requests.exceptions.TooManyRedirects:
                proxy_provider.proxy_feedback(proxy, False, False, self.url)
            except curl_cffi.requests.exceptions.IncompleteRead:
                proxy_provider.proxy_feedback(proxy, False, False, self.url)
            except Exception as e:
                logger.error("An exception occurred while judging.", exc_info=True)
        else:
            # logger.deb("Failed.")
            return None

        proxy_provider.proxy_feedback(proxy, True, False, self.url)

        if response.status_code != 200:
            return None

        text = response.text.lower()

        elite = anonymous = True

        for key, times in self.no_proxy_response.items():
            this_times = text.count(key)
            if this_times > times:
                elite = False
                break

        if any(ip in text for ip in curr_ips):
            anonymous = elite = False

        if elite:
            # logger.info("Proxy is elite.")
            return "elite"
        elif anonymous:
            # logger.info("Proxy is anonymous.")
            return "anonymous"
        else:
            # logger.info("Proxy is transparent.")
            return "transparent"


@dataclasses.dataclass
class JudgeManager:
    urls: list[str]
    ip_resolvers: list[str]
    judges: list[Judge]
    lock: threading.Lock
    curr_judge_i: int = 0
    curr_ips: set[str] = None

    def create_judges(self):
        logger = logging.getLogger(__class__.__name__)
        logger.info("(Re)Creating judges.")
        with self.lock:
            new_judges = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                judge_futures = [executor.submit(Judge.create, url) for url in self.urls]
                ip_futures = [executor.submit(curl_cffi.requests.get, url, timeout=5, impersonate="chrome") for url in
                              self.ip_resolvers]
                for future in concurrent.futures.as_completed(judge_futures):
                    try:
                        judge = future.result()
                        new_judges.append(judge)
                    except JudgeCreateError:
                        continue

                # ip address
                ip_addrs = []
                for future in concurrent.futures.as_completed(ip_futures):
                    try:
                        resp = future.result()
                        if resp.status_code == 200 and resp.text.strip():
                            ip_addrs.append(resp.text.strip())

                        logger.info(ip_addrs[-1])
                    except curl_cffi.requests.exceptions.RequestException:
                        continue

                if not ip_addrs:
                    logger.critical("No IP address resolvers returned a valid IP address.")
                    self.curr_ips = set()
                else:
                    self.curr_ips = set(ip_addrs)
                    logger.info(f"IP addresses: {self.curr_ips}")

            self.judges = new_judges

    @classmethod
    def create(cls, urls: list[str], ip_resolvers: list[str]):
        return cls(
            urls=urls,
            ip_resolvers=ip_resolvers,
            judges=[],
            lock=threading.Lock(),
        )

    def judge(
        self,
        proxy_provider: ProxyProvider,
        proxy: Proxy
    ) -> typing.Literal["elite", "anonymous", "transparent"] | None:
        with self.lock:
            judge = self.judges[self.curr_judge_i]

            self.curr_judge_i += 1
            self.curr_judge_i %= len(self.judges)

        return judge.judge(proxy_provider, proxy, self.curr_ips)
