__all__ = ["Generic", "GenericMetaclass"]


class GenericMetaclass(type):
    def __init__(cls, name: str, bases: tuple[type, ...], namespace: dict[str, object]):
        super().__init__(name, bases, namespace)

    def __getitem__[T](self: T, item) -> T:
        # self = the generic class that wants to be subscripted
        if not isinstance(item, tuple):
            item = item,

        # return a new class with the given generic arguments saved and a subclass of self
        return type(self.__name__, (self,), {"__generic_args__": item, "__base_class__": self})

    def __repr__(self: type["Generic"]):
        if self.__base_class__ is None:
            base_class = self
        else:
            base_class = self.__base_class__

        f_generic_args = f"[{', '.join(map(repr, self.__generic_args__))}]" if self.__generic_args__ else ""

        return f"{base_class.__name__}" + f_generic_args


class Generic[*Ts](metaclass=GenericMetaclass):
    __generic_args__: tuple[*Ts] = ()
    __base_class__: type = None


def main():
    class MyGeneric(Generic):
        def __init__(self, a: int):
            self.a = a

        def what_am_i(self):
            print(self.__base_class__, self.__class__, self.__generic_args__)

    my_generic1 = MyGeneric[int](1)
    my_generic1.what_am_i()

    my_generic2 = MyGeneric["This a generic arg", bool](2)
    my_generic2.what_am_i()


if __name__ == '__main__':
    main()
