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
        import linecache

        linecache.cache[self.name] = (
            len(self.src),
            None,
            [line + '\n' for line in self.src.splitlines()],
            self.name
        )
        # print(linecache.cache)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            from linecache import cache

            del cache[self.name]

            return False
        else:
            lines = self.src.splitlines()

            if exc_tb.tb_next is not None:
                exc_val.add_note(
                    "Exception occurred in dynamically generated code:\n"
                    f"{lines[exc_tb.tb_next.tb_lineno - 1]}\n\n"
                    f"Full source:\n{self.src}"
                )
        return False


@dataclasses.dataclass
class CodeGenerator:
    statements: list[tuple[int, Statement]] = dataclasses.field(default_factory=list)
    current_indent: int = 0
    consts: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    var_i: int = 0

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

    def add_statement(self, statement: Statement):
        self.statements.append((self.current_indent, statement))

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

    def blocks(self) -> list[tuple[int, list[Statement]]]:
        blocks = []
        current_indent = None

        for indent, statement in self.statements:
            if indent != current_indent:
                blocks.append((indent, []))

            blocks[-1][1].append(statement)
            current_indent = indent

        return blocks

    def to_str(self):
        lines = []

        for indent, statement in self.statements:
            lines.append("    " * indent + statement.to_str())

        return "\n".join(lines)

    def compile(self, name: str, in_var: str = "inp", out_var: str = "out") -> typing.Callable:
        src = self.to_str()

        code = compile(src, name, "exec", optimize=2)

        def func(data):
            with _TracebackSourceContextManager(src, name):
                scope = {in_var: data}
                exec(code, self.consts, scope)

                return scope[out_var]

        return func
