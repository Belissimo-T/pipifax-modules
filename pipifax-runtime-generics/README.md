# pipifax-runtime-generics

```py
from pipifax_generics import Generic


class MyGeneric(Generic):
    def __init__(self, a: int):
        self.a = a

    def what_am_i(self):
        print(self.__base_class__, self.__generic_args__)


def main():
    my_generic1 = MyGeneric[int](1)
    my_generic1.what_am_i()
    
    my_generic2 = MyGeneric["This a generic arg", bool](2)
    my_generic2.what_am_i()


if __name__ == '__main__':
    main()
```

Output:
```
MyGeneric (<class 'int'>,)
MyGeneric ('This a generic arg', <class 'bool'>)
```