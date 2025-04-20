import dataclasses
import datetime
import os
import random
import time
import typing
from pathlib import Path

import psutil


class CouldNotAcquireLockException(Exception):
    pass


@dataclasses.dataclass
class AcquiredLock:
    lock_file: Path

    def __enter__(self):
        return self.lock_file

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.lock_file.unlink(missing_ok=True)


@dataclasses.dataclass
class FilePidLock:
    file_path: Path

    def __post_init__(self):
        self.file_path = self.file_path.resolve()

    @staticmethod
    def is_lock_valid(
        file_path: Path
    ) -> typing.Literal[False] | tuple[int, int]:
        items = file_path.name.split("-", 3)
        if len(items) == 4:
            _, pid_str, create_time_str, timestamp_str = items
        else:
            return False

        try:
            locked_pid = int(pid_str)
            create_time_ns = int(create_time_str)
            timestamp_int = int(timestamp_str)
        except ValueError:
            return False

        if psutil.pid_exists(locked_pid) and int(psutil.Process(locked_pid).create_time() * 1e9) == create_time_ns:
            return locked_pid, timestamp_int
        else:
            return False

    def write_lock(self) -> tuple[Path, int]:
        timestamp = time.time_ns()
        pid = os.getpid()
        create_time_str = str(int(psutil.Process(pid).create_time() * 1e9))
        lock_file = self.file_path / f"{random.randbytes(16).hex()}-{pid}-{create_time_str}-{int(timestamp)}"
        lock_file.touch()
        return lock_file, timestamp

    def check_existing_locks(self, our_lock: tuple[str, int] | None = None):
        for lock_file in self.file_path.iterdir():
            if our_lock is not None and lock_file.name == our_lock[0]:
                continue

            lock_data = self.is_lock_valid(lock_file)

            if not lock_data:
                # remove invalid lock files
                lock_file.unlink(missing_ok=True)
                continue

            locked_pid, timestamp = lock_data

            if our_lock is not None and timestamp > our_lock[1]:
                # our lock is older than the existing lock
                lock_file.unlink(missing_ok=True)
                continue

            raise CouldNotAcquireLockException(
                f"Could not acquire lock at {self.file_path!s}. "
                f"Blocked by PID {lock_data[0]!s} since {datetime.datetime.fromtimestamp(lock_data[1] * 1e-9)!s}. "
                f"File: {lock_file.name!r}"
            )

    def acquire(self) -> AcquiredLock:
        self.file_path.mkdir(parents=True, exist_ok=True)

        self.check_existing_locks()

        file_path, timestamp = self.write_lock()

        self.check_existing_locks(our_lock=(file_path.name, timestamp))

        # redundancy check
        if not file_path.exists():
            raise CouldNotAcquireLockException("Could not acquire lock. Lock file vanished.")

        return AcquiredLock(file_path)


def main():
    lock = FilePidLock(Path("cache/lock2"))
    with lock.acquire():
        try:
            lock.acquire()
        except CouldNotAcquireLockException as e:
            print(e)
        else:
            raise AssertionError("Lock acquired twice.")

        print("Releasing lock.")

    try:
        with lock.acquire():
            print("Lock acquired again.")
    except CouldNotAcquireLockException as e:
        raise AssertionError("Could not acquire lock.") from e


if __name__ == "__main__":
    main()
