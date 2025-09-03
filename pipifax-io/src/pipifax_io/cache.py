import abc
import contextlib
import contextvars
import dataclasses
import hashlib
import inspect
import json
import pathlib
import types
import typing
import functools

import bson

from .saferw import safe_read_bytes, safe_write_bytes
from .json_serialization import (
    serialize_json, JsonSerializableValue, compile_json_serializer, compile_json_deserializer
)


class DeleteCachedFile(Exception):
    pass


class SimpleCache(abc.ABC):
    @abc.abstractmethod
    def lookup(self, key: str) -> bytes:
        pass

    @abc.abstractmethod
    def store(self, key: str, data: bytes):
        pass

    @abc.abstractmethod
    def delete(self, key: str):
        pass


class GetAttrDict(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError as e:
            e.add_note(f"{item!r} is not an argument to the function.")
            raise AttributeError from e

    def __setattr__(self, key, value):
        self[key] = value


@dataclasses.dataclass
class FileSystemCache(SimpleCache):
    file_path: pathlib.Path

    def lookup(self, key: str) -> bytes:
        return safe_read_bytes(self.file_path / key)

    def store(self, key: str, data: bytes):
        file_path = self.file_path / key
        file_path.parent.mkdir(exist_ok=True, parents=True)
        safe_write_bytes(file_path, data)

    def delete(self, key: str):
        (self.file_path / key).unlink(missing_ok=True)


def _decorator_can_take_noargs[**P, R](decorator: typing.Callable[P, R]) -> typing.Callable[P, R]:
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        if len(args) == 1 and isinstance(args[0], types.FunctionType):
            return decorator()(args[0])
        else:
            return decorator(*args, **kwargs)

    return wrapper


def make_key_func(
    key_args: typing.Collection[str] | None = None,
    version=0,
    key_func: typing.Callable[[GetAttrDict], JsonSerializableValue] | None = None,
    ext: str = "",
) -> typing.Callable[[typing.Callable, dict[str, typing.Any]], str]:
    key_func = (lambda x: x) if key_func is None else key_func

    def key(func, call_args: dict[str, typing.Any]) -> str:
        if key_args is None:
            key_args_ = call_args
        else:
            key_args_ = {k: call_args[k] for k in key_args}

        json_data = serialize_json(key_func(GetAttrDict(key_args_)), None)
        hash_ = hashlib.sha256(bson.dumps({"data": json_data})).hexdigest()[0:16]
        # hash_ = hashlib.sha256(repr({"data": json_data}).encode()).hexdigest()[0:16]

        if func.__module__ == "__main__":
            module_name = "__main__-" + hashlib.sha256(func.__code__.co_filename.encode("utf-8")).hexdigest()[0:16]
        else:
            module_name = func.__module__

        return f"{module_name}.{func.__qualname__}/{version}-{hash_}{ext}"

    # noinspection PyTypeChecker
    return key


@_decorator_can_take_noargs
def cache[**P, R](
    # self,
    serialize: typing.Callable[[R], bytes],
    deserialize: typing.Callable[[bytes], R],
    # typing.Callable is the decorated function
    key: typing.Callable[[typing.Callable, dict[str, typing.Any]], str] | None = None,
) -> typing.Callable[[typing.Callable[P, R]], typing.Callable[P, R]]:
    key = make_key_func() if key is None else key

    def decorator(func: typing.Callable[P, R]) -> typing.Callable[P, R]:
        def wrapper(
            *args: P.args,
            __only_cached: bool = False,
            __disable_cache_lookup: bool = False,
            __do_remove_cached: bool = False,
            **kwargs: P.kwargs
        ) -> R:
            simple_cache = CURRENT_SIMPLE_CACHE_CONTEXT_VAR.get()

            if simple_cache is None:
                raise RuntimeError("No cache set in current context.")

            call_args = inspect.getcallargs(func, *args, **kwargs)

            cache_key = key(func, call_args)

            if __do_remove_cached:
                simple_cache.delete(cache_key)
                return

            assert not (__only_cached and __disable_cache_lookup)

            try:
                if __disable_cache_lookup:
                    raise FileNotFoundError

                return deserialize(simple_cache.lookup(cache_key))
            except FileNotFoundError as e:
                e.add_note(
                    f"Cache miss."
                    # f"key={cache_key!r}, "
                    # f"func={func.__qualname__!r}, "
                    # f"args={args!r}, "
                    # f"kwargs={kwargs!r}"
                )
                if __only_cached:
                    raise

                out = func(*args, **kwargs)

            serialized = serialize(out)

            simple_cache.store(cache_key, serialized)

            return out

        return wrapper

    return decorator


@_decorator_can_take_noargs
def cache_auto[**P, R, S: JsonSerializableValue](
    serialize_type: type[S] | None = None,
    version=0,
    key_args: typing.Collection[str] | None = None,
    key_func: typing.Callable[[GetAttrDict], JsonSerializableValue] | None = None,
    serialize: typing.Callable[[R], S] | None = None,
    deserialize: typing.Callable[[S], R] | None = None,
    file_extension: str | None = None,
) -> typing.Callable[[typing.Callable[P, R]], typing.Callable[P, R]]:
    assert not (key_args is not None is not key_func), "Specify either key_args or key_func."

    def decorator(func: typing.Callable[P, R]) -> typing.Callable[P, R]:
        return_type = inspect.get_annotations(func).get("return", None)

        if serialize is not None or deserialize is not None:
            assert serialize_type is not None, "Specify serialize_type when using custom serialize or deserialize."
            serialize_type_ = serialize_type
        else:
            assert not (isinstance(return_type, str) and serialize_type is None), \
                "Could not determine return type. Specify via serialize_type=."
            serialize_type_ = return_type if serialize_type is None else serialize_type

            assert serialize_type_ is not None, "Must specify a return or serialize_type."

        if serialize_type_ is str:
            serialize_ = (
                (lambda x: x.encode("utf-8"))
                if serialize is None else serialize
            )
            deserialize_ = (
                (lambda x: x.decode("utf-8"))
                if deserialize is None else deserialize
            )
            ext = ".txt"
        elif serialize_type_ is bytes:
            serialize_ = (
                (lambda x: x)
                if serialize is None else serialize
            )
            deserialize_ = (
                (lambda x: x)
                if deserialize is None else deserialize
            )
            ext = ""
        else:
            json_serialize_func = compile_json_serializer(serialize_type_)
            json_deserialize_func = compile_json_deserializer(serialize_type_)
            serialize_ = (
                (lambda x: json.dumps(json_serialize_func(x)).encode("utf-8"))
                if serialize is None else serialize
            )
            deserialize_ = (
                (lambda x: json_deserialize_func(json.loads(x.decode("utf-8"))))
                if deserialize is None else deserialize
            )
            ext = ".json"

        if file_extension is not None:
            ext = file_extension

        key = make_key_func(
            key_args=key_args,
            version=version,
            key_func=key_func,
            ext=ext,
        )

        # noinspection PyTypeChecker
        return cache(
            key=key,
            serialize=serialize_,
            deserialize=deserialize_
        )(func)

    return decorator


CURRENT_SIMPLE_CACHE_CONTEXT_VAR: contextvars.ContextVar[SimpleCache | None] = (
    contextvars.ContextVar('pipifax_io.cache.SimpleCache.current', default=None)
)


@contextlib.contextmanager
def set_simple_cache(simple_cache: SimpleCache):
    token = CURRENT_SIMPLE_CACHE_CONTEXT_VAR.set(simple_cache)

    try:
        yield
    finally:
        CURRENT_SIMPLE_CACHE_CONTEXT_VAR.reset(token)


def use_cache(simple_cache: SimpleCache):
    def decorator[**P, R](func: typing.Callable[P, R]) -> typing.Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with set_simple_cache(simple_cache):
                return func(*args, **kwargs)

        return wrapper

    return decorator
