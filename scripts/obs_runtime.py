#!/usr/bin/env python3
"""Assemble an isolated loader directory from the official OBS SDK's existing libraries."""
import argparse
from pathlib import Path
import platform


def prepare(source, library, destination, machine=None):
    """Link one architecture's SDK dependencies without replacing system or AWS transport libraries."""
    machine = machine or platform.machine()
    architectures = {"x86_64": ("linux", "linux_64"), "aarch64": ("arm", "aarch64")}
    if machine not in architectures:
        raise ValueError("OBS runtime assembly supports Linux x86_64 and aarch64")
    source, library = source.resolve(), library.resolve()
    native, logging = architectures[machine]
    roots = [source/"build/script/Provider/build"/native,
             source/"platform/libboundscheck/lib"/native,
             source/"platform/eSDK_LogAPI_V2.1.10/C"/logging]
    if not library.is_file() or any(not root.is_dir() for root in roots):
        raise ValueError("missing OBS SDK library or bundled dependency directories")
    files = {"libeSDKOBS.so": library}
    for root in roots:
        for candidate in sorted(root.rglob("*.so*")):
            if candidate.is_file():
                if candidate.name in files and files[candidate.name].resolve() != candidate.resolve():
                    raise ValueError(f"ambiguous OBS dependency: {candidate.name}")
                files[candidate.name] = candidate.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    for name, file in files.items():
        (destination/name).symlink_to(file)
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = prepare(args.source, args.library, args.output)
    print(f"Prepared {count} library links in {args.output.resolve()}")


if __name__ == "__main__":
    main()
