import collections.abc
import dataclasses
import typing


@dataclasses.dataclass(frozen=True, slots=True)
class _GeneratorLengthHint:
    length: int


class _LengthedGenerator(collections.abc.Generator):
    def __init__(self, generator: typing.Generator, length: int):
        self._generator = generator
        self._length = length

    def __len__(self):
        return self._length

    def send(self, value) -> typing.Any:
        return self._generator.send(value)

    def throw(self, typ, value=None, traceback=None) -> typing.Any:
        return self._generator.throw(typ, value, traceback)


def generator_length[** P, Y, S, R](generator_func: typing.Callable[P, typing.Generator[Y, S, R]]) -> typing.Callable[
    P, _LengthedGenerator[Y, S, R]]:
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> typing.Generator[Y, S, R]:
        it = generator_func(*args, **kwargs)
        _length_hint = next(it, None)

        if isinstance(_length_hint, _GeneratorLengthHint):
            return _LengthedGenerator(it, _length_hint.length)
        else:
            return it

    return wrapper


def length_hint(length: int):
    return _GeneratorLengthHint(length)
