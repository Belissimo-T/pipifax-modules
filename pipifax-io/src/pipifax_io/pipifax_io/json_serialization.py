import base64
import collections.abc
import datetime
import json
import types
import typing

from pipifax_io import dyn_codegen

try:
    import pydantic
except ImportError:
    pass

_SimpleJson = typing.Union[str, int, float, bool, None]
type JsonType = (
    _SimpleJson
    | collections.abc.Collection["JsonType"]
    | collections.abc.Mapping[str, "JsonType"]
)


class _HasSerializationCodegenProto(typing.Protocol):
    @classmethod
    def _compile_json_serializer(
        cls,
        in_var: str,
        out_var: str,
        codegen: dyn_codegen.CodeGenerator
    ) -> None:
        ...

    @classmethod
    def _compile_json_deserializer(
        cls,
        in_var: str,
        out_var: str,
        codegen: dyn_codegen.CodeGenerator
    ) -> None:
        ...


type JsonSerializableValue = (
    _SimpleJson,
    _HasSerializationCodegenProto,
    collections.abc.Collection[JsonSerializableValue],
    collections.abc.Mapping[JsonSerializableValue, JsonSerializableValue],
)


class SerializationError(Exception):
    pass


class DeserializationError(Exception):
    pass


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

    raise SerializationError(f"Serialization of {data!r} not supported.")


def issubclass_json_type(tp: typing.Any) -> bool:
    if tp in (str, int, float, bool, types.NoneType):
        return True
    elif tp in (bytes, bytearray):
        return False

    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    if origin is typing.Union or origin is types.UnionType:
        return all(issubclass_json_type(arg) for arg in args)

    if origin and issubclass(origin, collections.abc.Mapping):
        if len(args) != 2:
            return False

        key_t, val_t = args
        return (key_t is str) and issubclass_json_type(val_t)

    if origin and issubclass(origin, collections.abc.Collection):
        if not args:
            return False

        if origin is tuple:
            # homogeneous: Tuple[T, ...]
            if len(args) == 2 and args[1] is Ellipsis:
                return issubclass_json_type(args[0])

            # fixed‐length: Tuple[T1, T2, ...]
            return all(issubclass_json_type(a) for a in args)

        if len(args) != 1:
            return False

        return issubclass_json_type(args[0])

    return False


def _compile_json_serializer[T: JsonSerializableValue](
    type_: type[T],
    in_var: str,
    out_var: str,
    codegen: dyn_codegen.CodeGenerator
):
    # TODO fix unions

    if issubclass_json_type(type_):
        codegen.assign(out_var, in_var)
        return

    args = typing.get_args(type_)

    # remove None from union types
    if type(type_) in (typing.Union, types.UnionType):
        if types.NoneType in args:
            args = tuple(arg for arg in args if arg is not types.NoneType)
            type_ = typing.Union[args]

    base_class = typing.get_origin(type_)
    base_class = type_ if base_class is None else base_class
    args = typing.get_args(type_)

    if not isinstance(base_class, type):
        raise SerializationError(f"Serialization of {type_!r} not supported.")

    if issubclass(base_class, _SimpleJson):
        codegen.assign(out_var, in_var)
        return

    if issubclass(base_class, collections.abc.Mapping):
        if len(args) != 2:
            raise DeserializationError(
                f"Serialization type {type_} of a mapping must specify key and value types."
            )

        key_var, value_var = codegen.get_vars()

        if issubclass(args[0], str):
            codegen.literal(f"{out_var} = {in_var}.copy()")
            codegen.literal(f"for {key_var}, {value_var} in {out_var}.items():")
            codegen.indent()
            _compile_json_serializer(args[0], value_var, value_var, codegen)
            codegen.assign(f"{out_var}[{key_var}]", value_var)
            codegen.dedent()
            return
        else:
            key_type, value_type = args

            codegen.literal(f"{out_var} = {{}}")
            codegen.literal(f"for {key_var}, {value_var} in {in_var}.items():")
            codegen.indent()
            _compile_json_serializer(key_type, key_var, key_var, codegen)
            _compile_json_serializer(value_type, value_var, value_var, codegen)
            codegen.assign(f"{out_var}[{key_var}]", value_var)
            codegen.dedent()
            return

    if issubclass(base_class, collections.abc.Collection):
        if base_class is tuple and not (len(args) == 2 and args[1] is Ellipsis):
            out = []
            for i, elem_type in enumerate(args):
                var = codegen.get_var()
                _compile_json_serializer(elem_type, f"{out_var}[{i}]", var, codegen)
                out.append(var)

            codegen.assign(out_var, f"({', '.join(out)},)")
            return
        else:
            if args[-1] is Ellipsis:
                args = args[:-1]

            if len(args) != 1:
                raise DeserializationError(
                    f"Serialization mutable sequence type {type_} must specify exactly one element type."
                )

            elem_type, = args

            elem_i, elem_var = codegen.get_vars(2)

            codegen.assign(out_var, f"list({in_var})")
            codegen.literal(f"for {elem_i}, {elem_var} in enumerate({out_var}):")
            codegen.indent()
            _compile_json_serializer(elem_type, elem_var, elem_var, codegen)
            codegen.assign(f"{out_var}[{elem_i}]", elem_var)
            codegen.dedent()
            return

    if issubclass(base_class, bytes):
        codegen.ensure_import("base64")
        codegen.assign(out_var, f"base64.b64decode({in_var}.encode('utf-8'))")
        return

    if issubclass(base_class, datetime.datetime):
        codegen.assign(out_var, f"{in_var}.isoformat()")
        return

    if hasattr(base_class, "_compile_json_serializer"):
        base_class._compile_json_serializer(in_var, out_var, codegen)
        return

    # if issubclass(base_class, JsonSerializable):
    #     return f"{out_var} = {in_var}.serialize_json()"

    if pydantic is not None:
        if issubclass(base_class, pydantic.BaseModel):
            codegen.assign(out_var, f"{in_var}.model_dump(mode='json')")
            return
        elif issubclass(base_class, type(pydantic.BaseModel)):
            codegen.assign(out_var, f"{in_var}.model_json_schema(mode='validation')")
            return

    raise SerializationError(f"Serialization of {type_!r} not supported.")


def _compile_json_deserializer[T: JsonSerializableValue](
    type_: type[T],
    in_var: str,
    out_var: str,
    codegen: dyn_codegen.CodeGenerator,
):
    if issubclass_json_type(type_):
        codegen.assign(out_var, in_var)
        return

    args = typing.get_args(type_)

    # remove None from union types
    if type(type_) in (typing.Union, types.UnionType):
        if types.NoneType in args:
            args = tuple(arg for arg in args if arg is not types.NoneType)
            type_ = typing.Union[args]

    base_class = typing.get_origin(type_)
    base_class = type_ if base_class is None else base_class
    args = typing.get_args(type_)

    if not isinstance(base_class, type):
        raise DeserializationError(f"Deserialization of {type_!r} not supported.")

    if issubclass(base_class, _SimpleJson):
        codegen.assign(out_var, in_var)
        return

    if issubclass(base_class, collections.abc.Mapping):
        if len(args) != 2:
            raise DeserializationError(
                f"Deserialization type {type_} of a mapping must specify key and value types."
            )
        base_class_name = codegen.get_const(base_class)

        key_type, value_type = args
        key_var, value_var = codegen.get_vars(2)

        codegen.assign(out_var, f"{base_class_name}()")
        codegen.literal(f"for {key_var}, {value_var} in {in_var}.items():")
        codegen.indent()
        _compile_json_deserializer(key_type, key_var, key_var, codegen)
        _compile_json_deserializer(value_type, value_var, value_var, codegen)
        codegen.assign(f"{out_var}[{key_var}]", value_var)
        codegen.dedent()
        return

    if issubclass(base_class, collections.abc.Collection):
        if base_class is tuple and not (len(args) == 2 and args[1] is Ellipsis):
            if base_class is not tuple:
                base_class_var = codegen.get_const(base_class)

            out = []
            for i, elem_type in enumerate(args):
                var = codegen.get_var()
                _compile_json_deserializer(elem_type, f"{in_var}[{i}]", var, codegen)
                out.append(var)

            codegen.assign(out_var, f"({', '.join(out)},)")
            if base_class is not tuple:
                codegen.literal(f"{out_var} = {base_class_var}({out_var})")

            return
        else:
            if args[-1] is Ellipsis:
                args = args[:-1]

            if len(args) != 1:
                raise DeserializationError(
                    f"Deserialization collection type {type_} must specify exactly one element type."
                )

            base_class_name = codegen.get_const(base_class)

            elem_type, = args
            elem_var = codegen.get_var()

            codegen.assign(out_var, "[]")
            codegen.literal(f"for {elem_var} in {in_var}:")
            codegen.indent()
            _compile_json_deserializer(elem_type, elem_var, elem_var, codegen)
            codegen.literal(f"{out_var}.append({elem_var})")
            codegen.dedent()
            codegen.assign(out_var, f"{base_class_name}({out_var})")
            return

    if issubclass(base_class, bytes):
        codegen.ensure_import("base64")
        codegen.assign(out_var, f"base64.b64decode({in_var}.encode('utf-8'))")
        return

    if issubclass(base_class, datetime.datetime):
        codegen.ensure_import("datetime")
        codegen.assign(out_var, f"datetime.datetime.fromisoformat({in_var})")
        return

    if hasattr(base_class, "_compile_json_deserializer"):
        base_class._compile_json_deserializer(in_var, out_var, codegen)
        return

    # if issubclass(base_class, JsonSerializable):
    #     base_class_name = next(var_name_gen)
    #     consts[base_class_name] = base_class
    #
    #     return f"{out_var} = {base_class_name}.deserialize_json({in_var})"

    if pydantic is not None:
        if issubclass(base_class, pydantic.BaseModel):
            # return f"{out_var} = {base_class.__name__}.model_validate({in_var})"
            codegen.assign(out_var, f"{base_class.__name__}.model_validate({in_var})")
            return

    raise DeserializationError(f"Deserialization of {type_!r} not supported.")


def compile_json_serializer[T: JsonSerializableValue](
    type_: type[T]
) -> typing.Callable[[T], JsonType]:
    codegen = dyn_codegen.CodeGenerator()
    _compile_json_serializer(
        type_,
        "inp",
        "out",
        codegen,
    )

    return codegen.compile(
        name=f"<pipifax_io compiled json serialization for {repr(type_)}>"
    )


def compile_json_deserializer[T: JsonSerializableValue](
    type_: type[T]
) -> typing.Callable[[JsonType], T]:
    codegen = dyn_codegen.CodeGenerator()
    _compile_json_deserializer(
        type_,
        "inp",
        "out",
        codegen,
    )

    return codegen.compile(
        name=f"<pipifax_io compiled json deserialization for {repr(type_)}>"
    )


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
