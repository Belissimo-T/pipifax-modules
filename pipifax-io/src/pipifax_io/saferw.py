import pathlib

__all__ = [
    "safe_write_bytes",
    "safe_write_text",
    "safe_read_bytes",
    "safe_read_text",
]


def safe_write_bytes(path: pathlib.Path, data: bytes):
    temp_path = path.with_name(f"{path.name}._saferwtmp")  # safe write temp

    with temp_path.open("wb") as f:
        f.write(data)

    temp_path.replace(path)


def safe_write_text(path: pathlib.Path, data: str, encoding="utf-8"):
    safe_write_bytes(path, data.encode(encoding))


def safe_read_bytes(path: pathlib.Path) -> bytes:
    # temp_path = _get_temp_filename(path)

    return path.read_bytes()

    # try:
    #     with path.open("rb") as f:
    #         return f.read()
    # except FileNotFoundError as original_error:
    #     try:
    #         with temp_path.open("rb") as f:
    #             data = f.read()
    #
    #         temp_path.replace(path)
    #
    #         return data
    #     except FileNotFoundError:
    #         raise original_error


def safe_read_text(path: pathlib.Path, encoding="utf-8") -> str:
    return safe_read_bytes(path).decode(encoding)
