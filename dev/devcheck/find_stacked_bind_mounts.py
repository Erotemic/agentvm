#!/usr/bin/env python3
"""
This searches for stacked bind mounts that can happen in older versions of aivm
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Iterable


_OCTAL_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


def unescape_mountinfo(value: str) -> str:
    return _OCTAL_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), value)


@dataclass
class MountInfo:
    mount_id: int
    parent_id: int
    st_dev: str
    root: str
    mount_point: str
    mount_options: str
    optional_fields: list[str]
    fstype: str
    source: str
    super_options: str

    @classmethod
    def parse(cls, line: str) -> "MountInfo":
        line = line.rstrip("\n")
        if " - " not in line:
            raise ValueError(f"mountinfo line missing separator: {line!r}")

        left, right = line.split(" - ", 1)
        left_parts = left.split()
        right_parts = right.split()

        if len(left_parts) < 6:
            raise ValueError(f"mountinfo line has too few left fields: {line!r}")
        if len(right_parts) < 3:
            raise ValueError(f"mountinfo line has too few right fields: {line!r}")

        mount_id = int(left_parts[0])
        parent_id = int(left_parts[1])
        st_dev = left_parts[2]
        root = unescape_mountinfo(left_parts[3])
        mount_point = unescape_mountinfo(left_parts[4])
        mount_options = left_parts[5]
        optional_fields = left_parts[6:]

        fstype = right_parts[0]
        source = unescape_mountinfo(right_parts[1])
        super_options = " ".join(right_parts[2:])

        return cls(
            mount_id=mount_id,
            parent_id=parent_id,
            st_dev=st_dev,
            root=root,
            mount_point=mount_point,
            mount_options=mount_options,
            optional_fields=optional_fields,
            fstype=fstype,
            source=source,
            super_options=super_options,
        )


def read_mountinfo(path: str = "/proc/self/mountinfo") -> list[MountInfo]:
    items: list[MountInfo] = []
    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            items.append(MountInfo.parse(line))
    return items


def likely_bind_like(m: MountInfo) -> bool:
    # Heuristic:
    # - root is not "/" OR
    # - source looks like a path OR
    # - mountpoint repeats with same fstype/source/root pattern
    return m.root != "/" or m.source.startswith("/") or m.source.startswith("[")


def group_by_mountpoint(items: Iterable[MountInfo]) -> dict[str, list[MountInfo]]:
    grouped: dict[str, list[MountInfo]] = defaultdict(list)
    for item in items:
        grouped[item.mount_point].append(item)
    for mount_point in grouped:
        grouped[mount_point].sort(key=lambda x: x.mount_id)
    return grouped


def build_parent_map(items: Iterable[MountInfo]) -> dict[int, MountInfo]:
    return {item.mount_id: item for item in items}


def describe_chain(items: list[MountInfo]) -> list[dict]:
    return [
        {
            "mount_id": item.mount_id,
            "parent_id": item.parent_id,
            "st_dev": item.st_dev,
            "root": item.root,
            "mount_point": item.mount_point,
            "fstype": item.fstype,
            "source": item.source,
            "mount_options": item.mount_options,
            "optional_fields": item.optional_fields,
            "super_options": item.super_options,
        }
        for item in items
    ]


def find_suspicious_stacks(
    items: list[MountInfo],
    *,
    only_bind_like: bool = True,
) -> list[dict]:
    grouped = group_by_mountpoint(items)
    suspicious: list[dict] = []

    for mount_point, group in grouped.items():
        if len(group) < 2:
            continue

        bind_like_count = sum(1 for g in group if likely_bind_like(g))
        same_root = len({g.root for g in group}) == 1
        same_source = len({(g.source, g.fstype, g.st_dev) for g in group}) == 1

        if only_bind_like and bind_like_count == 0:
            continue

        suspicious.append(
            {
                "mount_point": mount_point,
                "count": len(group),
                "bind_like_count": bind_like_count,
                "same_root": same_root,
                "same_source": same_source,
                "chain": describe_chain(group),
            }
        )

    suspicious.sort(key=lambda x: (-x["count"], x["mount_point"]))
    return suspicious


def print_human_report(stacks: list[dict]) -> None:
    if not stacks:
        print("No suspicious stacked mountpoints found.")
        return

    print(f"Found {len(stacks)} suspicious stacked mountpoint(s).\n")
    for stack in stacks:
        print(f"MOUNTPOINT: {stack['mount_point']}")
        print(f"  layers:          {stack['count']}")
        print(f"  bind_like_count: {stack['bind_like_count']}")
        print(f"  same_root:       {stack['same_root']}")
        print(f"  same_source:     {stack['same_source']}")
        print("  chain:")
        for item in stack["chain"]:
            print(
                "    - "
                f"id={item['mount_id']} parent={item['parent_id']} "
                f"dev={item['st_dev']} fstype={item['fstype']} "
                f"root={item['root']} source={item['source']}"
            )
        print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scan /proc/self/mountinfo for suspicious stacked mountpoints, "
            "with a focus on repeated bind-like mounts at the same target."
        )
    )
    parser.add_argument(
        "--mountinfo",
        default="/proc/self/mountinfo",
        help="Path to a mountinfo file to inspect. Default: /proc/self/mountinfo",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a human-readable report.",
    )
    parser.add_argument(
        "--all-stacked",
        action="store_true",
        help="Report any repeated mountpoint, not only bind-like ones.",
    )
    parser.add_argument(
        "--mountpoint",
        default=None,
        help="Only report a specific mountpoint path.",
    )
    args = parser.parse_args()

    try:
        items = read_mountinfo(args.mountinfo)
    except Exception as ex:
        print(f"Failed to read mountinfo: {ex}", file=sys.stderr)
        return 2

    stacks = find_suspicious_stacks(
        items,
        only_bind_like=not args.all_stacked,
    )

    if args.mountpoint is not None:
        stacks = [s for s in stacks if s["mount_point"] == args.mountpoint]

    if args.json:
        print(json.dumps(stacks, indent=2, sort_keys=True))
    else:
        print_human_report(stacks)

    return 0 if stacks else 1


if __name__ == "__main__":
    raise SystemExit(main())
