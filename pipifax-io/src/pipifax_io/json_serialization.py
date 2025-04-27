import base64
import collections.abc
import datetime
import json
import types
import typing

from . import code_generator
from . import serializable_errors
from . import type_serializer

try:
    import pydantic
except ImportError:
    pydantic = None
    pass

_SimpleJson = typing.Union[str, int, float, bool, None]
type JsonType = (
    _SimpleJson
    | collections.abc.Collection[JsonType]
    | collections.abc.Mapping[str, JsonType]
)

type _JsonDecodeType = (
    _SimpleJson
    | list[JsonType]
    | dict[str, JsonType]
)

type JsonSerializableValue = (
    _SimpleJson
    | HasJsonSerializationCodegen
    | collections.abc.Collection[JsonSerializableValue]
    | collections.abc.Mapping[JsonSerializableValue, JsonSerializableValue]
)


class HasJsonSerializationCodegen(typing.Protocol):
    @classmethod
    def _compile_json_serializer(
        cls,
        in_var: str,
        out_var: str,
        codegen: code_generator.CodeGenerator
    ) -> None:
        ...

    @classmethod
    def _compile_json_deserializer(
        cls,
        in_var: str,
        out_var: str,
        codegen: code_generator.CodeGenerator
    ) -> None:
        ...


class HasJsonSerializationCodegenSerializableMixin:
    def serialize_json(self: HasJsonSerializationCodegen) -> JsonType:
        if not hasattr(self.__class__, "_json_serializer_func"):
            self.__class__._json_serializer_func = compile_json_serializer(self.__class__)

        return self.__class__._json_serializer_func(self)

    @classmethod
    def deserialize_json(cls: type[HasJsonSerializationCodegen], data: JsonType) -> typing.Self:
        if not hasattr(cls, "_json_deserializer_func"):
            cls._json_deserializer_func = compile_json_deserializer(cls)

        # noinspection PyUnresolvedReferences
        return cls._json_deserializer_func(data)

    def serialize(self) -> bytes:
        return json.dumps(self.serialize_json()).encode("utf-8")

    @classmethod
    def deserialize(cls, data: bytes) -> typing.Self:
        return cls.deserialize_json(json.loads(data.decode("utf-8")))


def _serialize_json_type_blind[T: JsonSerializableValue](data: T) -> JsonType:
    if isinstance(data, _SimpleJson):
        return data

    if isinstance(data, bytes):
        return base64.b64encode(data).decode("utf-8")

    if hasattr(data, "serialize_json"):
        return data.serialize_json()

    if isinstance(data, collections.abc.Mapping):
        return {
            json.dumps(_serialize_json_type_blind(key)): _serialize_json_type_blind(value)
            for key, value in data.items()
        }

    if isinstance(data, collections.abc.Collection):
        return [_serialize_json_type_blind(item) for item in data]

    if isinstance(data, datetime.datetime):
        return data.isoformat()

    if pydantic is not None:
        if isinstance(data, pydantic.BaseModel):
            return data.model_dump(mode="json")
        elif isinstance(data, type(pydantic.BaseModel)):
            return data.model_json_schema(mode="validation")

    raise serializable_errors.SerializationError(f"Serialization of {data!r} not supported.")


def _issubclass_json_type(tp: type_serializer.TypeHint) -> bool:
    if tp in (str, int, float, bool, types.NoneType, None):
        return True
    elif tp in (bytes, bytearray):
        return False

    origin, args = type_serializer.read_type_hint(tp)

    if origin is typing.Union or origin is types.UnionType:
        return all(_issubclass_json_type(arg) for arg in args)

    if origin is JsonType:
        return True

    if not isinstance(origin, type):
        return False

    if origin and issubclass(origin, collections.abc.Mapping):
        if len(args) != 2:
            return False

        key_t, val_t = args
        return (key_t is str) and _issubclass_json_type(val_t)

    if origin and issubclass(origin, collections.abc.Collection):
        if not args:
            return False

        if origin is tuple:
            # homogeneous: Tuple[T, ...]
            if len(args) == 2 and args[1] is Ellipsis:
                return _issubclass_json_type(args[0])

            # fixed‐length: Tuple[T1, T2, ...]
            return all(_issubclass_json_type(a) for a in args)

        if len(args) != 1:
            return False

        return _issubclass_json_type(args[0])

    return False


class JsonSerializerCodegen(type_serializer.SerializerCodegen):
    def is_scalar(self, type_: type_serializer.TypeHint) -> bool:
        return _issubclass_json_type(type_)

    def scalar(self, type_: type_serializer.TypeHint, in_var: str, out_var: str):
        self.codegen.assign(out_var, in_var)

    def tuple_(self, in_vars: list[str], out_var: str):
        self.codegen.assign(out_var, f"({', '.join(in_vars)},)")

    def mapping(self, key_type: type_serializer.TypeHint, value_type: type_serializer.TypeHint, in_var: str,
                out_var: str):
        base_class, _ = type_serializer.read_type_hint(key_type)
        if isinstance(base_class, type) and issubclass(base_class, str):
            key_var, value_var = self.codegen.get_vars(2)

            self.codegen.literal(f"{out_var} = {in_var}.copy()")
            self.codegen.literal(f"for {key_var}, {value_var} in {out_var}.items():")
            self.codegen.indent()
            self.any(value_type, value_var, value_var)
            self.codegen.assign(f"{out_var}[{key_var}]", value_var)
            self.codegen.dedent()
        else:
            super().mapping(key_type, value_type, in_var, out_var)

    def union(self, args: tuple[type_serializer.TypeHint, ...], in_var: str, out_var: str):
        if types.NoneType in args:
            self.codegen.literal(f"if {in_var} is not None:")
            self.codegen.indent()
            self.any(typing.Union[*[a for a in args if a is not types.NoneType]], in_var, out_var)
            self.codegen.dedent()
            self.codegen.literal("else:")
            self.codegen.indent()
            self.codegen.assign(out_var, in_var)
            self.codegen.dedent()
        else:
            super().union(args, in_var, out_var)

    def type_(self, type_: type_serializer.TypeHint, base_type: type, args: tuple[type_serializer.TypeHint, ...],
              in_var: str, out_var: str):
        if issubclass(base_type, bytes):
            self.codegen.ensure_import("base64")
            self.codegen.assign(out_var, f"base64.b64encode({in_var}).decode()")
            return

        if issubclass(base_type, datetime.datetime):
            self.codegen.assign(out_var, f"{in_var}.isoformat()")
            return

        if hasattr(base_type, "_compile_json_serializer"):
            base_type._compile_json_serializer(in_var, out_var, self)
            return

        if pydantic is not None:
            if issubclass(base_type, pydantic.BaseModel):
                self.codegen.assign(out_var, f"{in_var}.model_dump(mode='json')")
                return
            elif issubclass(base_type, type(pydantic.BaseModel)):
                self.codegen.assign(out_var, f"{in_var}.model_json_schema(mode='validation')")
                return

        super().type_(type_, base_type, args, in_var, out_var)


def _issubclass_json_decode_type(tp) -> bool:
    """
    Returns True if `tp` is one of:
      - str, int, float, bool, NoneType
      - Union[...] of the above (including X|Y syntax)
      - list[<valid>]
      - dict[str, <valid>]
    """
    origin, args = type_serializer.read_type_hint(tp)

    # Handle unions (both typing.Union[...] and X|Y)
    if origin is typing.Union or origin is types.UnionType:
        return all(_issubclass_json_decode_type(arg) for arg in args)

    # Simple JSON types
    if tp in (str, int, float, bool, type(None)):
        return True

    # list[...]
    if origin is list and len(args) == 1:
        return _issubclass_json_decode_type(args[0])

    # dict[str, ...]
    if origin is dict and len(args) == 2:
        key_type, val_type = args
        return key_type is str and _issubclass_json_decode_type(val_type)

    # Anything else is not allowed
    return False


class JsonDeserializerCodegen(type_serializer.DeserializerCodegen):
    def is_scalar(self, type_: type_serializer.TypeHint) -> bool:
        return _issubclass_json_decode_type(type_)

    def scalar(self, type_: type_serializer.TypeHint, in_var: str, out_var: str):
        self.codegen.assign(out_var, in_var)

    def tuple_(self, class_: type, in_vars: list[str], out_var: str):
        class_var = self.codegen.get_const(class_)
        self.codegen.assign(out_var, f"{class_var}(({', '.join(in_vars)},))")

    def mapping(self, class_: type, key_type: type_serializer.TypeHint, value_type: type_serializer.TypeHint,
                in_var: str, out_var: str):
        base_class, _ = type_serializer.read_type_hint(key_type)
        if isinstance(base_class, type) and issubclass(base_class, str):
            key_var, value_var = self.codegen.get_vars(2)

            self.codegen.literal(f"{out_var} = {in_var}.copy()")
            self.codegen.literal(f"for {key_var}, {value_var} in {out_var}.items():")
            self.codegen.indent()
            self.any(value_type, value_var, value_var)
            self.codegen.assign(f"{out_var}[{key_var}]", value_var)
            self.codegen.dedent()
        else:
            super().mapping(class_, key_type, value_type, in_var, out_var)

    def union(self, args: tuple[type_serializer.TypeHint, ...], in_var: str, out_var: str):
        if types.NoneType in args:
            self.codegen.literal(f"if {in_var} is not None:")
            self.codegen.indent()
            self.any(typing.Union[*[a for a in args if a is not types.NoneType]], in_var, out_var)
            self.codegen.dedent()
            self.codegen.literal("else:")
            self.codegen.indent()
            self.codegen.assign(out_var, in_var)
            self.codegen.dedent()
        else:
            super().union(args, in_var, out_var)

    def type_(self, type_: type_serializer.TypeHint, base_type: type, args: tuple[type_serializer.TypeHint, ...],
              in_var: str, out_var: str):
        if issubclass(base_type, bytes):
            self.codegen.ensure_import("base64")
            self.codegen.assign(out_var, f"base64.b64decode({in_var}.encode())")
            return

        if issubclass(base_type, datetime.datetime):
            datetime_const = self.codegen.get_const(datetime.datetime)
            self.codegen.assign(out_var, f"{datetime_const}.fromisoformat({in_var})")
            return

        if hasattr(base_type, "_compile_json_deserializer"):
            base_type._compile_json_deserializer(in_var, out_var, self)
            return

        if pydantic is not None:
            if issubclass(base_type, pydantic.BaseModel):
                base_type_var = self.codegen.get_const(base_type)
                self.codegen.assign(out_var, f"{base_type_var}.model_validate({in_var})")
                return

        super().type_(type_, base_type, args, in_var, out_var)


def compile_json_serializer[T: JsonSerializableValue](
    type_: type[T]
) -> typing.Callable[[T], JsonType]:
    codegen = code_generator.CodeGenerator()
    serializer_codegen = JsonSerializerCodegen(codegen)
    serializer_codegen.any(
        type_,
        "inp",
        "out",
    )
    func = codegen.compile(
        name=f"<pipifax_io compiled json serialization for {repr(type_)}>",
        in_var="inp",
        out_var="out",
    )

    def wrapper(data):
        try:
            return func(data)
        except Exception as e:
            raise serializable_errors.SerializationError from e

    return wrapper


def compile_json_deserializer[T: JsonSerializableValue](
    type_: type[T]
) -> typing.Callable[[JsonType], T]:
    codegen = code_generator.CodeGenerator()
    deserializer_codegen = JsonDeserializerCodegen(codegen)
    deserializer_codegen.any(
        type_,
        "inp",
        "out",
    )

    func = codegen.compile(
        name=f"<pipifax_io compiled json deserialization for {repr(type_)}>"
    )

    def wrapper(data):
        try:
            return func(data)
        except Exception as e:
            raise serializable_errors.SerializationError from e

    return wrapper


def serialize_json[T: JsonSerializableValue](data: T, type_: type[T] | None) -> JsonType:
    if type_ is None:
        return _serialize_json_type_blind(data)
    else:
        return compile_json_serializer(type_)(data)


def deserialize_json[T: JsonSerializableValue](
    data: JsonType,
    type_: type[T]
) -> T:
    return compile_json_deserializer(type_)(data)


def main():
    codegen = code_generator.CodeGenerator()
    JsonSerializerCodegen(
        codegen=codegen,
    ).any(
        type_=dict[int, str] | None | bytes,
        # type_=list[bytes | None],
        in_var="in_var",
        out_var="out_var",
    )

    print(codegen.to_str())
    print(codegen.consts)
    asd = codegen.compile("ASD", "in_var", "out_var")
    print(asd({2: "Hello"}))
    print(asd(None))
    print(asd(b"HELLOW"))

    codegen = code_generator.CodeGenerator()
    JsonDeserializerCodegen(
        codegen=codegen,
    ).any(
        type_=dict[int, str] | None | bytes,
        # type_=list[bytes | None],
        in_var="in_var",
        out_var="out_var",
    )

    print(codegen.to_str())
    print(codegen.consts)
    asd = codegen.compile("ASD", "in_var", "out_var")
    print(asd((0, [(2, 'Hello')])))
    print(asd(None))
    print(asd((1, 'SEVMTE9X')))


if __name__ == "__main__":
    main()
