import dataclasses
import pathlib
import typing

from . import saferw, type_serializer, json_serialization


class Serializable(typing.Protocol):
    def serialize(self) -> bytes:
        pass

    @classmethod
    def deserialize(cls, data: bytes) -> typing.Self:
        pass


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


class SimpleSerializable(json_serialization.HasJsonSerializationCodegenSerializableMixin):
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
        serializer: type_serializer.SerializerCodegen
    ) -> None:
        out = []
        fields = cls._get_serialize_fields()

        for field_name, field_type in fields.items():
            tmp = serializer.codegen.get_var()
            out.append((field_name, tmp))

            serializer.codegen.assign(tmp, f"{in_var}.{field_name}")

            serializer.any(field_type, tmp, tmp)

        serializer.codegen.assign(out_var, "{" + ", ".join([f"{name!r}: {v}" for name, v in out]) + "}")

        serializer.any(
            dict[str, json_serialization.JsonType],
            in_var=out_var,
            out_var=out_var,
        )

    @classmethod
    def _compile_json_deserializer(
        cls,
        in_var: str,
        out_var: str,
        deserializer: type_serializer.DeserializerCodegen
    ) -> None:
        cls_var = deserializer.codegen.get_const(cls)

        fields = cls._get_serialize_fields()

        tmp = deserializer.codegen.get_var()
        deserializer.codegen.assign(tmp, f"{cls_var}.__new__({cls_var})")

        for field_name, field_type in fields.items():
            tmp2 = deserializer.codegen.get_var()
            deserializer.any(field_type, f"{in_var}[{field_name!r}]", tmp2)

            deserializer.codegen.literal(f"object.__setattr__({tmp}, {field_name!r}, {tmp2})")

        deserializer.codegen.literal(f"{tmp}.__deserialize_init__()")

        deserializer.codegen.assign(out_var, tmp)


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
