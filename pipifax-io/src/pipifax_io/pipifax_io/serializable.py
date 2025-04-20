import abc
import dataclasses
import json
import pathlib
import typing

from . import saferw, dyn_codegen
from .json_serialization import (
    _compile_json_serializer, _compile_json_deserializer, JsonType,
    serialize_json, deserialize_json
)


class Serializable(abc.ABC):
    def serialize(self) -> bytes:
        ...

    @classmethod
    def deserialize(cls, data: bytes) -> typing.Self:
        ...


class JsonSerializable(Serializable, abc.ABC):
    def serialize_json(self) -> JsonType:
        ...

    @classmethod
    def deserialize_json(cls, data: JsonType) -> typing.Self:
        ...

    def serialize(self) -> bytes:
        return json.dumps(self.serialize_json()).encode("utf-8")

    @classmethod
    def deserialize(cls, data: bytes) -> typing.Self:
        return cls.deserialize_json(json.loads(data.decode("utf-8")))


class HasSerializationCodegen(JsonSerializable, abc.ABC):
    @classmethod
    @abc.abstractmethod
    def _compile_json_serializer(
        cls,
        in_var: str,
        out_var: str,
        codegen: dyn_codegen.CodeGenerator
    ) -> None:
        ...

    @classmethod
    @abc.abstractmethod
    def _compile_json_deserializer(
        cls,
        in_var: str,
        out_var: str,
        codegen: dyn_codegen.CodeGenerator
    ) -> None:
        ...

    def serialize_json(self) -> JsonType:
        return serialize_json(self, self.__class__)

    @classmethod
    def deserialize_json(cls, data: JsonType) -> typing.Self:
        return deserialize_json(data, cls)


# Type hinting this correctly is impossible due to a lack of intersection types
# @typing.overload
# def easy_serializable(
#     *,
#     exclude_fields: set[str] | None = None,
#     serialize_fields: dict[str, type] | None = None
# ) -> typing.Callable[[type], type]:
#     pass
#
#
# @typing.overload
# def easy_serializable(
#     __type,
# ) -> type:
#     pass
#
#
# def easy_serializable[T](
#     __type: T = None,
#     *,
#     exclude_fields: set[str] | None = None,
#     serialize_fields: dict[str, type] | None = None
# ):
#     if __type is not None:
#         assert exclude_fields is None is serialize_fields
#
#         return easy_serializable()(__type)
#
#     serialize_fields = serialize_fields if serialize_fields is not None else {}
#     exclude_fields = exclude_fields if exclude_fields is not None else set()
#
#     def decorator(class_: type):
#         serialize_fields_final = class_.__dataclass_fields__ | serialize_fields  # type: ignore
#         for f in exclude_fields:
#             serialize_fields_final.pop(f, None)
#
#         def serialize_json(self):
#             return {
#                 field: _serialize_json(getattr(self, field, type_)) for field, type_ in
#                 serialize_fields_final.items()
#             }
#
#         def deserialize_json(cls, data: dict):
#             out = cls(**{
#                 field: _deserialize_json(data[field], type_)
#                 for field, type_ in serialize_fields_final.items()
#             })
#             if hasattr(out, "__deserialize_init__"):
#                 out.__deserialize_init__()
#
#             return out
#
#         return type(class_.__name__, (class_, JsonSerializable), {
#             "serialize_json": serialize_json,
#             "deserialize_json": classmethod(deserialize_json),
#             "__serialize_fields__": serialize_fields_final
#         })
#
#     return decorator


class SimpleSerializable(HasSerializationCodegen):
    def __deserialize_init__(self):
        pass

    @staticmethod
    def __easy_serializable_migrate__(data: dict) -> dict:
        return data

    @classmethod
    def _get_serialize_fields(cls) -> dict[str, type]:
        # noinspection PyTypeChecker,PyDataclass
        dataclass_fields = dataclasses.fields(cls)

        out = (
            {
                field.name: field.type
                for field in dataclass_fields
                if field._field_type is not dataclasses._FIELD_CLASSVAR
            }
            | getattr(cls, "__serialize_fields__", {})
        )

        for field in getattr(cls, "__exclude_fields__", set()):
            out.pop(field, None)

        return out

    @classmethod
    def _compile_json_serializer(
        cls,
        in_var: str,
        out_var: str,
        codegen: dyn_codegen.CodeGenerator
    ) -> None:
        out = []
        fields = cls._get_serialize_fields()

        for field_name, field_type in fields.items():
            tmp = codegen.get_var()
            out.append(tmp)

            codegen.assign(tmp, f"{in_var}.{field_name}")

            _compile_json_serializer(field_type, tmp, tmp, codegen)

        codegen.assign(out_var, f"({', '.join(out)},)")

    @classmethod
    def _compile_json_deserializer(
        cls,
        in_var: str,
        out_var: str,
        codegen: dyn_codegen.CodeGenerator
    ) -> None:
        cls_var = codegen.get_const(cls)

        fields = cls._get_serialize_fields()

        tmp = codegen.get_var()
        codegen.assign(tmp, f"{cls_var}.__new__({cls_var})")

        for i, (field_name, field_type) in enumerate(fields.items()):
            tmp2 = codegen.get_var()
            _compile_json_deserializer(field_type, f"{in_var}[{i}]", tmp2, codegen)

            codegen.literal(f"object.__setattr__({tmp}, {field_name!r}, {tmp2})")

        codegen.literal(f"{tmp}.__deserialize_init__()")

        codegen.assign(out_var, tmp)


class DataStore[T: Serializable]:
    data: T


@dataclasses.dataclass
class FileSystemStore[T: Serializable](DataStore[T]):
    data: T
    path: pathlib.Path

    @classmethod
    def open[L: Serializable](cls, type_: type[L], path: pathlib.Path) -> "FileSystemStore[L]":
        try:
            serialized_data = saferw.safe_read_bytes(path)
        except FileNotFoundError:
            data = type_()
        else:
            data = type_.deserialize(serialized_data)

        return cls(data, path)

    def save(self, path: pathlib.Path | None = None):
        saferw.safe_write_bytes(self.path if path is None else path, self.data.serialize())
