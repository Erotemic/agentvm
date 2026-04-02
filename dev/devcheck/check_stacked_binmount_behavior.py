#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BIND_LAYERS = int(os.environ.get('BIND_LAYERS', '5'))
KEEP = os.environ.get('KEEP', '0') == '1'
BASE_PARENT = os.environ.get('BASE_DIR', '/tmp')


def run(cmd, *, check=True, capture=True, cwd=None):
    if isinstance(cmd, (list, tuple)):
        display = ' '.join(map(str, cmd))
    else:
        display = str(cmd)
    print(f'$ {display}')
    result = subprocess.run(
        cmd,
        check=False,
        text=True,
        capture_output=capture,
        cwd=cwd,
    )
    if capture:
        if result.stdout:
            print(
                result.stdout, end='' if result.stdout.endswith('\n') else '\n'
            )
        if result.stderr:
            print(
                result.stderr,
                end='' if result.stderr.endswith('\n') else '\n',
                file=sys.stderr,
            )
    if check and result.returncode != 0:
        raise RuntimeError(f'command failed ({result.returncode}): {display}')
    return result


def capture_cmd(cmd, *, cwd=None):
    return subprocess.run(
        cmd,
        check=False,
        text=True,
        capture_output=True,
        cwd=cwd,
    )


def readlink(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError as ex:
        return f'<readlink failed: {ex}>'


def realpath(path: str | Path) -> str:
    try:
        return os.path.realpath(os.fspath(path))
    except OSError as ex:
        return f'<realpath failed: {ex}>'


def grep_mountinfo(alias: Path) -> list[str]:
    lines = []
    with open('/proc/self/mountinfo', 'r') as file:
        for line in file:
            if f' {alias} ' in line:
                lines.append(line.rstrip('\n'))
    return lines


def count_mount_layers(alias: Path) -> int:
    return len(grep_mountinfo(alias))


def show_mounts(alias: Path) -> None:
    print('\n---- mountinfo for alias ----')
    lines = grep_mountinfo(alias)
    print('\n'.join(lines) if lines else '<none>')
    print('---- findmnt -R --target alias ----')
    run(['findmnt', '-R', '--target', str(alias)], check=False)


def proc_cwd_report(pid: int) -> dict:
    pwdx_res = capture_cmd(['pwdx', str(pid)])
    lsof_res = capture_cmd(['lsof', '-a', '-p', str(pid), '-d', 'cwd'])
    return {
        'pid': pid,
        'pwdx': (pwdx_res.stdout or pwdx_res.stderr).strip(),
        'readlink': readlink(Path(f'/proc/{pid}/cwd')),
        'realpath': realpath(f'/proc/{pid}/cwd'),
        'lsof': (lsof_res.stdout or lsof_res.stderr).strip(),
    }


def show_proc(label: str, pid: int) -> dict:
    print(f'\n== {label} (pid={pid}) ==')
    run(['ps', '-o', 'pid,ppid,cmd=', '-p', str(pid)], check=False)
    report = proc_cwd_report(pid)
    print(report['pwdx'])
    print(report['readlink'])
    print(report['realpath'])
    if report['lsof']:
        print(report['lsof'])
    return report


def spawn_sleep_in_dir(path: Path) -> subprocess.Popen:
    return subprocess.Popen(['sleep', '10000'], cwd=path)


def terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def alias_seen_in_lsof_plus_d(alias: Path, pids: list[int]) -> tuple[bool, str]:
    expr = '|'.join(str(p) for p in pids)
    res = capture_cmd(
        [
            'bash',
            '-lc',
            f"sudo lsof +D {alias!s} | egrep '({expr})|COMMAND' || true",
        ]
    )
    out = (res.stdout or '') + (res.stderr or '')
    matched = any(str(pid) in out for pid in pids)
    return matched, out.strip()


def concludes_expected_behavior(
    *,
    src: Path,
    alias: Path,
    before_src_report: dict,
    after_src_report: dict,
    alias_report: dict,
    layer_count: int,
    lsof_plus_d_hits: bool,
) -> tuple[bool, list[str]]:
    evidence = []

    src_before_ok = before_src_report['readlink'] == str(
        src
    ) and before_src_report['realpath'] == str(src)
    src_after_ok = after_src_report['readlink'] == str(
        src
    ) and after_src_report['realpath'] == str(src)
    alias_proc_ok = alias_report['readlink'] == str(alias) and alias_report[
        'realpath'
    ] == str(alias)
    stacked_ok = layer_count >= 2

    if stacked_ok:
        evidence.append(
            f'Observed stacked bind layers at alias target: {layer_count}'
        )
    if src_before_ok:
        evidence.append(
            'Process started in SRC before stacking still reports cwd via SRC'
        )
    if src_after_ok:
        evidence.append(
            'Process started in SRC after stacking still reports cwd via SRC'
        )
    if alias_proc_ok:
        evidence.append('Process started in ALIAS reports cwd via ALIAS')
    if lsof_plus_d_hits:
        evidence.append(
            'lsof +D ALIAS surfaces the tracked processes through the alias tree'
        )

    confirmed = (
        stacked_ok
        and src_before_ok
        and src_after_ok
        and alias_proc_ok
        and lsof_plus_d_hits
    )
    return confirmed, evidence


def print_evidence_once(title: str, evidence: list[str]) -> None:
    if not evidence:
        return
    print(f'\n{title}')
    for item in evidence:
        print(f'- {item}')


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix='aivm-cwd-exp-', dir=BASE_PARENT))
    src = base / 'src'
    alias = base / 'alias'
    src.mkdir(parents=True, exist_ok=True)
    alias.mkdir(parents=True, exist_ok=True)
    (src / 'marker.txt').write_text('hello\n')

    p_src_1 = None
    p_alias_1 = None
    p_src_2 = None

    print(f'BASE={base}')
    print(f'SRC={src}')
    print(f'ALIAS={alias}')

    try:
        print('\nSTEP 1: no mount yet')
        run(
            [
                'bash',
                '-lc',
                f"cd {src!s} && echo 'shell in SRC before mount:' && pwd && pwd -P",
            ]
        )

        print('\nSTEP 2: create first bind mount')
        run(['sudo', 'mount', '--bind', str(src), str(alias)])
        show_mounts(alias)

        print('\nSTEP 3: start one process from SRC and one from ALIAS')
        p_src_1 = spawn_sleep_in_dir(src)
        p_alias_1 = spawn_sleep_in_dir(alias)
        time.sleep(0.2)
        src_before_report = show_proc(
            'started in SRC after 1 bind', p_src_1.pid
        )
        alias_report = show_proc('started in ALIAS after 1 bind', p_alias_1.pid)

        print('\nSTEP 4: stack more binds on same alias target')
        for _ in range(BIND_LAYERS):
            run(['sudo', 'mount', '--bind', str(src), str(alias)])
        show_mounts(alias)

        print('\nSTEP 5: start another process from SRC after stacked binds')
        p_src_2 = spawn_sleep_in_dir(src)
        time.sleep(0.2)
        src_after_report = show_proc(
            'started in SRC after stacked binds', p_src_2.pid
        )

        print('\nSTEP 6: compare names and inode identity')
        run(['stat', '-Lc', 'dev=%D ino=%i %n', str(src), str(alias)])
        run(
            [
                'stat',
                '-Lc',
                'dev=%D ino=%i %n',
                f'/proc/{p_src_1.pid}/cwd',
                f'/proc/{p_alias_1.pid}/cwd',
                f'/proc/{p_src_2.pid}/cwd',
            ]
        )

        print('\nSTEP 7: does lsof +D on alias see the tracked processes?')
        lsof_hits, lsof_text = alias_seen_in_lsof_plus_d(
            alias, [p_src_1.pid, p_alias_1.pid, p_src_2.pid]
        )
        if lsof_text:
            print(lsof_text)

        print('\nSTEP 8: summarize outcome')
        layer_count = count_mount_layers(alias)
        print(f'Duplicate bind layers present: {layer_count}')
        print('PID started in SRC after stacking:')
        run(['pwdx', str(p_src_2.pid)], check=False)

        confirmed, evidence = concludes_expected_behavior(
            src=src,
            alias=alias,
            before_src_report=src_before_report,
            after_src_report=src_after_report,
            alias_report=alias_report,
            layer_count=layer_count,
            lsof_plus_d_hits=lsof_hits,
        )

        print_evidence_once('Observed evidence', evidence)

        print('\nConclusion')
        if confirmed:
            print(
                'Confirmed: repeated bind mounts stack on the alias target, while '
                'processes started in SRC still report SRC as cwd. '
                'At the same time, lsof +D ALIAS can surface those processes '
                'through the alias tree.'
            )
        else:
            print(
                'Unexpected result: this run did not match the earlier hypothesis cleanly. '
                'Review the evidence above before drawing conclusions.'
            )

        return 0
    finally:
        if KEEP:
            print('\nKEEP=1, leaving experiment state in place:')
            print(f'  BASE={base}')
            print(f'  SRC={src}')
            print(f'  ALIAS={alias}')
            for name, proc in [
                ('p_src_1', p_src_1),
                ('p_alias_1', p_alias_1),
                ('p_src_2', p_src_2),
            ]:
                if proc is not None:
                    print(f'  {name}={proc.pid}')
            return

        for proc in [p_src_1, p_alias_1, p_src_2]:
            if proc is not None:
                terminate_process(proc)

        while count_mount_layers(alias) > 0:
            res = subprocess.run(
                ['sudo', 'umount', str(alias)],
                check=False,
                text=True,
                capture_output=True,
            )
            if res.returncode != 0:
                if res.stderr:
                    print(
                        res.stderr,
                        end='' if res.stderr.endswith('\n') else '\n',
                        file=sys.stderr,
                    )
                if res.stdout:
                    print(
                        res.stdout,
                        end='' if res.stdout.endswith('\n') else '\n',
                    )
                break

        shutil.rmtree(base, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
