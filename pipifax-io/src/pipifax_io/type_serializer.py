import abc
import collections
import dataclasses
import types
import typing

from . import code_generator
from . import serializable_errors


class TypeHintError(serializable_errors.SerializationError):
    pass


type TypeHint = object


@dataclasses.dataclass(frozen=True, slots=True)
class Label:
    value: str


@dataclasses.dataclass(frozen=True, slots=True)
class SerializeAs:
    value: TypeHint


def read_type_hint(type_: TypeHint) -> tuple[TypeHint, tuple[TypeHint, ...]]:
    if type_ is None:
        type_ = types.NoneType

    base_type = typing.get_origin(type_)
    base_type = type_ if base_type is None else base_type
    args = typing.get_args(type_)

    args = tuple((types.NoneType if t is None else t) for t in args)

    return base_type, args


class SerializerDeserializerCodegenSuper(abc.ABC):
    codegen: code_generator.CodeGenerator

    def __init__(self):
        self.labels: dict[str, type] = {}

    @abc.abstractmethod
    def is_scalar(self, type_: TypeHint) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    def scalar(self, type_: TypeHint, in_var: str, out_var: str):
        raise NotImplementedError

    @abc.abstractmethod
    def tuple_(self, class_: type, in_vars: list[str], out_var: str):
        raise NotImplementedError

    @abc.abstractmethod
    def mapping(self, class_: type, key_type: TypeHint, value_type: TypeHint, in_var: str, out_var: str):
        raise NotImplementedError

    @abc.abstractmethod
    def collection(self, class_: type, element_type: TypeHint, in_var: str, out_var: str):
        raise NotImplementedError

    @abc.abstractmethod
    def union(self, args: tuple[TypeHint, ...], in_var: str, out_var: str):
        raise NotImplementedError

    def get_real_origin(self, type_: TypeHint) -> TypeHint:
        origin, args = read_type_hint(type_)

        if isinstance(type_, Label):
            return self.get_real_origin(self.labels[type_.value])

        if origin is typing.Annotated:
            return self.get_real_origin(args[0])

        return type_ if origin is None else origin

    def annotated(self, annotated_type: TypeHint, annotation: TypeHint, in_var: str, out_var: str):
        if isinstance(annotation, Label):
            # if not isinstance(annotated_type, type):
            #     raise TypeHintError(f"Label can only be applied to concrete types, not to {annotated_type!r}.")

            self.labels[annotation.value] = annotated_type

            arg_name, ret_name = self.codegen.get_vars(2)
            self.codegen.begin_toplevel_function(f"process_type_annotation_{annotation.value}", [arg_name])
            self.any(annotated_type, arg_name, ret_name)
            self.codegen.end_toplevel_function(ret_name)

            self.any(annotation, in_var, out_var)
            return

        elif isinstance(annotation, SerializeAs):
            self.any(annotation.value, in_var, out_var)
            return

        self.any(annotated_type, in_var, out_var)

    def any(self, type_: TypeHint, in_var: str, out_var: str):
        self.codegen.comment(repr(type_))

        if isinstance(type_, Label):
            # noinspection PyUnresolvedReferences
            self.codegen.assign(out_var, f"process_type_annotation_{type_.value}({in_var})")
            return

        base_type, args = read_type_hint(type_)

        if base_type is typing.Annotated:
            annotated_type, *others, annotation = args

            if others:
                annotated_type = typing.Annotated[annotated_type, *others]

            self.annotated(annotated_type, annotation, in_var, out_var)
            return

        if self.is_scalar(type_):
            self.scalar(type_, in_var, out_var)
            return

        if base_type in (typing.Union, types.UnionType):
            self.union(args, in_var, out_var)
            return

        if base_type is typing.Literal:
            if all(self.is_scalar(type(arg)) for arg in args):
                self.scalar(type_, in_var, out_var)
                return

            raise TypeHintError("Literal types are not supported for serialization. Annotate with SerializeAs.")

        if not isinstance(base_type, type):
            raise TypeHintError(f"Serialization of {type_!r} not supported.")

        base_type: type

        self.type_(type_, base_type, args, in_var, out_var)

    def type_(self, type_: TypeHint, base_type: type, args: tuple[TypeHint, ...], in_var: str, out_var: str):
        if issubclass(base_type, collections.abc.Mapping):
            if len(args) != 2:
                raise serializable_errors.SerializationError(
                    f"Serialization type {type_} of a mapping must specify key and value types."
                )

            self.mapping(base_type, args[0], args[1], in_var, out_var)
            return

        if issubclass(base_type, collections.abc.Collection) and not issubclass(base_type, (str, bytes)):
            if base_type is tuple and not (len(args) == 2 and args[1] is Ellipsis):
                args_vars = self.codegen.get_vars(len(args))

                for i, (arg_var, arg_type) in enumerate(zip(args_vars, args)):
                    tmp = self.codegen.assign_new(f"{in_var}[{i}]")
                    self.any(arg_type, tmp, arg_var)

                self.tuple_(
                    base_type, args_vars, out_var
                )
                return
            else:
                if args[-1] is Ellipsis:
                    args = args[:-1]

                if len(args) != 1:
                    raise serializable_errors.SerializationError(
                        f"Serialization of sequence type {type_} must specify exactly one element type."
                    )

                self.collection(base_type, args[0], in_var, out_var)
                return

        raise serializable_errors.SerializationError(f"Serialization of {type_!r} not supported.")


class SerializerCodegen(SerializerDeserializerCodegenSuper, abc.ABC):
    def __init__(self, codegen: code_generator.CodeGenerator):
        self.codegen = codegen
        super().__init__()

    def mapping(self, _, key_type: TypeHint, value_type: TypeHint, in_var: str, out_var: str):
        self.codegen.assign(in_var, f"list({in_var}.items())")
        self.any(list[tuple[key_type, value_type]], in_var, out_var)

    def collection(self, _, element_type: TypeHint, in_var: str, out_var: str):
        elem_i, elem_var = self.codegen.get_vars(2)
        self.codegen.assign(out_var, f"list({in_var})")
        self.codegen.literal(f"for {elem_i}, {elem_var} in enumerate({out_var}):")
        self.codegen.indent()
        self.any(element_type, elem_var, elem_var)
        self.codegen.assign(f"{out_var}[{elem_i}]", elem_var)
        self.codegen.dedent()

    def union(self, args: tuple[TypeHint, ...], in_var: str, out_var: str):
        base_types = [self.get_real_origin(arg) for arg in args]

        for i, base_type in enumerate(base_types):
            for j, base_type2 in enumerate(base_types):
                if i == j:
                    continue

                if base_type is base_type2:
                    raise serializable_errors.SerializationError(
                        "Cannot serialize union of two generic types that have the same origin."
                    )

        for i, (arg, origin) in enumerate(zip(args, base_types)):
            # type_identifier = i
            type_identifier = origin.__name__

            type_var = self.codegen.get_const(origin)

            self.codegen.literal(f"{'el' if i != 0 else ''}if isinstance({in_var}, {type_var}):")
            self.codegen.indent()
            tmp = self.codegen.assign_new(f"({type_identifier!r}, {in_var})")
            self.any(tuple[int, arg], tmp, out_var)
            self.codegen.dedent()

        self.codegen.literal("else:")
        self.codegen.indent()
        self.codegen.literal(
            f"raise {self.codegen.get_const(serializable_errors.SerializationError)}("
            f"f'Unexpected type for union: {{type({in_var})!r}} of {{{in_var}!r}}'"
            f")"
        )
        self.codegen.dedent()


class DeserializerCodegen(SerializerDeserializerCodegenSuper, abc.ABC):
    def __init__(self, codegen: code_generator.CodeGenerator):
        self.codegen = codegen
        super().__init__()

    def mapping(self, class_: type, key_type: TypeHint, value_type: TypeHint, in_var: str, out_var: str):
        class_var = self.codegen.get_const(class_)
        tmp, key_var, value_var = self.codegen.get_vars(3)

        self.codegen.assign(tmp, f"{class_var}()")
        self.codegen.literal(f"for {key_var}, {value_var} in {in_var}:")
        self.codegen.indent()
        self.any(key_type, key_var, key_var)
        self.any(value_type, value_var, value_var)
        self.codegen.assign(f"{tmp}[{key_var}]", value_var)
        self.codegen.dedent()
        self.codegen.assign(out_var, tmp)

    def collection(self, class_: type, element_type: TypeHint, in_var: str, out_var: str):
        class_var = self.codegen.get_const(class_)

        elem_i, elem_var = self.codegen.get_vars(2)
        self.codegen.literal(f"for {elem_i}, {elem_var} in enumerate({in_var}):")
        self.codegen.indent()
        self.any(element_type, elem_var, elem_var)
        self.codegen.assign(f"{in_var}[{elem_i}]", elem_var)
        self.codegen.assign(out_var, f"{class_var}({in_var})")
        self.codegen.dedent()

    def union(self, args: tuple[TypeHint, ...], in_var: str, out_var: str):
        base_types = [self.get_real_origin(arg) for arg in args]

        for i, (arg, origin) in enumerate(zip(args, base_types)):
            # type_identifier = i
            type_identifier = origin.__name__

            self.codegen.literal(f"{'el' if i != 0 else ''}if {in_var}[0] == {type_identifier!r}:")
            self.codegen.indent()
            tmp = self.codegen.assign_new(f"{in_var}[1]")
            self.any(arg, tmp, out_var)
            self.codegen.dedent()

        self.codegen.literal("else:")
        self.codegen.indent()
        self.codegen.literal(
            f"raise {self.codegen.get_const(serializable_errors.SerializationError)}("
            f"f'Unexpected type identifier for union: {{{in_var}[0]!r}} in {{{in_var}!r}}. '"
            f")"
        )
        self.codegen.dedent()
