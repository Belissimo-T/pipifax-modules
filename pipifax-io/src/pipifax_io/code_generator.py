import dataclasses
import typing


@dataclasses.dataclass
class LiteralStatement:
    stmt: str

    def to_str(self):
        return self.stmt


@dataclasses.dataclass
class AssignmentStatement:
    left: str
    right: str

    def to_str(self):
        return f"{self.left} = {self.right}"


type Statement = LiteralStatement | AssignmentStatement


@dataclasses.dataclass(frozen=True, slots=True)
class _TracebackSourceContextManager:
    src: str
    name: str

    def __enter__(self):
        # import linecache
        # 
        # linecache.cache[self.name] = (
        #     len(self.src),
        #     None,
        #     [line + '\n' for line in self.src.splitlines()],
        #     self.name
        # )
        # print(linecache.cache)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            lines = self.src.splitlines()

            exc_val.add_note(
                "Exception occurred in dynamically generated code.\n"
                # f"{lines[exc_tb.tb_next.tb_lineno - 1]}\n\n"
                f"Full source:\n{self.src}"
            )
            return False


@dataclasses.dataclass
class CodeGenerator:
    functions: dict[str, tuple[list[tuple[int, Statement]], int]] = dataclasses.field(
        default_factory=lambda: {None: ([], 0)})
    current_function_stack: list[str] = dataclasses.field(default_factory=lambda: [None])
    consts: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    var_i: int = 0

    @property
    def current_function(self) -> str:
        return self.current_function_stack[-1]

    @property
    def current_indent(self):
        return self.functions[self.current_function][1]

    @current_indent.setter
    def current_indent(self, value: int):
        self.functions[self.current_function] = (self.functions[self.current_function][0], value)

    @property
    def current_statements(self) -> list[tuple[int, Statement]]:
        return self.functions[self.current_function][0]

    def get_var(self) -> str:
        try:
            return f"var{self.var_i}"
        finally:
            self.var_i += 1

    def get_vars(self, n: int = 1) -> list[str]:
        return [self.get_var() for _ in range(n)]

    def get_const(self, val: typing.Any) -> str:
        var = self.get_var()
        self.consts[var] = val
        return var

    def ensure_import(self, module: str):
        self.consts[module] = __import__(module)

    def indent(self):
        self.current_indent += 1

    def dedent(self):
        self.literal("...")
        self.current_indent -= 1

    def begin_toplevel_function(self, name: str, args: list[str]):
        self.current_function_stack.append(name)
        self.functions[name] = ([], 0)
        self.literal(f"def {name}({', '.join(args)}):")
        self.indent()

    def end_toplevel_function(self, return_var: str | None):
        if return_var is not None:
            self.literal(f"return {return_var}")

        self.current_function_stack.pop()

    def add_statement(self, statement: Statement):
        self.current_statements.append((self.current_indent, statement))

    def assign(self, left: str, right: str):
        if left == right:
            left = "# " + left

        self.add_statement(
            AssignmentStatement(left, right)
        )

    def assign_new(self, expr: str) -> str:
        new = self.get_var()
        self.assign(new, expr)
        return new

    def literal(self, *statements: str):
        for statement in statements:
            self.add_statement(
                LiteralStatement(statement)
            )

    def comment(self, *statements: str):
        for statement in statements:
            self.add_statement(
                LiteralStatement("# " + statement)
            )

    # def blocks(self) -> list[tuple[int, list[Statement]]]:
    #     blocks = []
    #     current_indent = None
    # 
    #     for indent, statement in self.statements:
    #         if indent != current_indent:
    #             blocks.append((indent, []))
    # 
    #         blocks[-1][1].append(statement)
    #         current_indent = indent
    # 
    #     return blocks

    # def optimize(self):
    #     new_blocks = []
    #
    #     for indent, statements in self.blocks():
    #         for statement in statements:
    #
    #             match statement:
    #                 case AssignmentStatement(left=left, right=right):
    #                     pass

    def to_str(self) -> tuple[str, str]:
        lines = []

        for name, (statements, _) in self.functions.items():
            if name is None:
                continue

            for indent, statement in statements:
                lines.append("    " * indent + statement.to_str())

            lines.append("")
            lines.append("")

        return (
            "\n".join(lines),
            "\n".join(
                [
                    "    " * indent + statement.to_str()

                    for indent, statement in self.functions[None][0]
                ]
            )
        )

    def compile(self, name: str, in_var: str = "inp", out_var: str = "out") -> typing.Callable:
        src_funcs, src_main = self.to_str()

        # print(f"COMPILED {name}:\n{src}\n")

        globals_ = self.consts.copy()

        code_funcs = compile(src_funcs, name, "exec", optimize=2)
        exec(code_funcs, globals_, globals_)

        code_main = compile(src_main, name, "exec", optimize=2)

        def func(data):
            with _TracebackSourceContextManager(src_funcs + "\n\n" + src_main, name):
                scope = {in_var: data}

                exec(code_main, globals_, scope)

                return scope[out_var]

        return func
