"""
Patchwork fix for stacked bind mounts
"""

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

_OCTAL_ESCAPE_RE = re.compile(r'\\([0-7]{3})')


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
    def parse(cls, line: str) -> 'MountInfo':
        line = line.rstrip('\n')
        if ' - ' not in line:
            raise ValueError(f'mountinfo line missing separator: {line!r}')
        left, right = line.split(' - ', 1)
        left_parts = left.split()
        right_parts = right.split()
        if len(left_parts) < 6:
            raise ValueError(
                f'mountinfo line has too few left fields: {line!r}'
            )
        if len(right_parts) < 3:
            raise ValueError(
                f'mountinfo line has too few right fields: {line!r}'
            )
        return cls(
            mount_id=int(left_parts[0]),
            parent_id=int(left_parts[1]),
            st_dev=left_parts[2],
            root=unescape_mountinfo(left_parts[3]),
            mount_point=unescape_mountinfo(left_parts[4]),
            mount_options=left_parts[5],
            optional_fields=left_parts[6:],
            fstype=right_parts[0],
            source=unescape_mountinfo(right_parts[1]),
            super_options=' '.join(right_parts[2:]),
        )


def read_mountinfo(path: str = '/proc/self/mountinfo') -> list[MountInfo]:
    items: list[MountInfo] = []
    with open(path, 'r', encoding='utf-8') as file:
        for line in file:
            line = line.strip()
            if line:
                items.append(MountInfo.parse(line))
    return items


def likely_bind_like(m: MountInfo) -> bool:
    return m.root != '/' or m.source.startswith('/') or m.source.startswith('[')


def group_by_mountpoint(
    items: Iterable[MountInfo],
) -> dict[str, list[MountInfo]]:
    grouped: dict[str, list[MountInfo]] = defaultdict(list)
    for item in items:
        grouped[item.mount_point].append(item)
    for mount_point, group in grouped.items():
        group.sort(key=lambda x: x.mount_id)
    return grouped


@dataclass
class StackCandidate:
    mount_point: str
    count: int
    identity: tuple[str, str, str, str]
    chain: list[MountInfo]

    @property
    def root(self) -> str:
        return self.identity[0]

    @property
    def source(self) -> str:
        return self.identity[1]

    @property
    def fstype(self) -> str:
        return self.identity[2]

    @property
    def st_dev(self) -> str:
        return self.identity[3]


def find_identical_stacks(items: list[MountInfo]) -> list[StackCandidate]:
    grouped = group_by_mountpoint(items)
    found: list[StackCandidate] = []
    for mount_point, group in grouped.items():
        if len(group) < 2:
            continue
        if not any(likely_bind_like(g) for g in group):
            continue
        keys = {(g.root, g.source, g.fstype, g.st_dev) for g in group}
        if len(keys) != 1:
            continue
        found.append(
            StackCandidate(
                mount_point=mount_point,
                count=len(group),
                identity=next(iter(keys)),
                chain=group,
            )
        )
    found.sort(key=lambda s: (-s.count, s.mount_point))
    return found


def filter_candidates(
    stacks: list[StackCandidate],
    *,
    mountpoints: list[str] | None,
    prefix: str | None,
    regex: str | None,
    aivm_only: bool,
) -> list[StackCandidate]:
    out = []
    cre = re.compile(regex) if regex else None
    for stack in stacks:
        mp = stack.mount_point
        if mountpoints and mp not in mountpoints:
            continue
        if prefix and not mp.startswith(prefix):
            continue
        if cre and not cre.search(mp):
            continue
        if aivm_only and '/var/lib/libvirt/aivm/' not in mp:
            continue
        out.append(stack)
    return out


def current_stack_count(
    mount_point: str, mountinfo_path: str = '/proc/self/mountinfo'
) -> int:
    count = 0
    with open(mountinfo_path, 'r', encoding='utf-8') as file:
        needle = f' {mount_point} '
        for line in file:
            if needle in line:
                count += 1
    return count


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def summarize_fuser(mount_point: str) -> str:
    res = run(['sudo', 'fuser', '-v', mount_point])
    txt = ((res.stdout or '') + (res.stderr or '')).strip()
    return txt


def peel_stack(
    mount_point: str,
    *,
    keep_layers: int,
    verbose: bool = True,
) -> tuple[int, int, list[str]]:
    before = current_stack_count(mount_point)
    messages: list[str] = []
    current = before

    while current > keep_layers:
        if verbose:
            print(f'  attempting: sudo umount {mount_point}')
        res = run(['sudo', 'umount', mount_point])
        after = current_stack_count(mount_point)

        if res.returncode == 0 and after < current:
            messages.append(f'peeled one layer: {current} -> {after}')
            current = after
            continue

        err = ((res.stderr or '') + (res.stdout or '')).strip()
        if err:
            messages.append(f'umount failed: {err}')
        else:
            messages.append('umount failed with no output')

        holders = summarize_fuser(mount_point)
        if holders:
            messages.append('holders:\n' + holders)
        break

    return before, current, messages


def print_plan(stacks: list[StackCandidate], *, keep_layers: int) -> None:
    if not stacks:
        print('No matching identical stacked bind-like mountpoints found.')
        return
    print(
        f'Matched {len(stacks)} stack(s). Planned target: keep {keep_layers} layer(s) each.\n'
    )
    for stack in stacks:
        extra = max(0, stack.count - keep_layers)
        print(f'MOUNTPOINT: {stack.mount_point}')
        print(f'  current_layers: {stack.count}')
        print(f'  layers_to_remove: {extra}')
        print(f'  root:   {stack.root}')
        print(f'  source: {stack.source}')
        print(f'  fstype: {stack.fstype}')
        print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            'Reduce identical stacked bind-like mounts to a smaller number of layers '
            'while leaving at least one layer in place.'
        )
    )
    parser.add_argument('--mountinfo', default='/proc/self/mountinfo')
    parser.add_argument(
        '--mountpoint',
        action='append',
        default=None,
        help='Specific mountpoint to target. May be repeated.',
    )
    parser.add_argument(
        '--prefix',
        default=None,
        help='Only operate on mountpoints with this prefix.',
    )
    parser.add_argument(
        '--regex',
        default=None,
        help='Only operate on mountpoints matching this regex.',
    )
    parser.add_argument(
        '--aivm-only',
        action='store_true',
        help='Only operate on /var/lib/libvirt/aivm/... mountpoints.',
    )
    parser.add_argument(
        '--keep-layers',
        type=int,
        default=1,
        help='How many identical layers to keep. Default: 1',
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Actually perform sudo umount operations.',
    )
    parser.add_argument(
        '--verbose', action='store_true', help='Print each attempted umount.'
    )
    args = parser.parse_args()

    if args.keep_layers < 1:
        print('--keep-layers must be >= 1', file=sys.stderr)
        return 2

    try:
        items = read_mountinfo(args.mountinfo)
    except Exception as ex:
        print(f'Failed to read mountinfo: {ex}', file=sys.stderr)
        return 2

    stacks = find_identical_stacks(items)
    stacks = filter_candidates(
        stacks,
        mountpoints=args.mountpoint,
        prefix=args.prefix,
        regex=args.regex,
        aivm_only=args.aivm_only,
    )

    print_plan(stacks, keep_layers=args.keep_layers)

    if not stacks:
        return 1

    if not args.apply:
        print('Dry run only. Re-run with --apply to peel extra layers.')
        return 0

    print('Applying changes...\n')
    any_changed = False
    any_failed = False

    for stack in stacks:
        print(f'MOUNTPOINT: {stack.mount_point}')
        before, after, messages = peel_stack(
            stack.mount_point,
            keep_layers=args.keep_layers,
            verbose=args.verbose,
        )
        for msg in messages:
            print('  ' + msg.replace('\n', '\n  '))
        print(f'  result: {before} -> {after} layers')
        if after < before:
            any_changed = True
        if after > args.keep_layers:
            any_failed = True
        print()

    if any_failed:
        print(
            'Some mountpoints could not be reduced to the requested layer count.'
        )
        return 3
    if any_changed:
        print('All targeted mountpoints were reduced successfully.')
        return 0

    print('No changes were made.')
    return 4


if __name__ == '__main__':
    raise SystemExit(main())
