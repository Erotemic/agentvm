"""Centralized command orchestration, logging, and approval handling.

This module is the long-term home for subprocess execution in ``aivm``.
It organizes command output around user-meaningful plans/steps, while still
preserving raw command visibility for deeper debugging.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import inspect
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal, Optional, Sequence

from loguru import logger

log = logger

CommandRole = Literal['read', 'modify']


def shell_join(cmd: Sequence[str]) -> str:
    return ' '.join(shlex.quote(str(c)) for c in cmd)


@dataclass(frozen=True)
class CommandResult:
    code: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    def __init__(self, cmd: Sequence[str] | str, result: CommandResult):
        self.cmd = cmd
        self.result = result
        super().__init__(
            f'Command failed (code={result.code}): {cmd}\n{result.stderr}'.strip()
        )


@dataclass(frozen=True)
class IntentFrame:
    title: str
    why: str = ''
    role: CommandRole = 'modify'
    visible: bool = True


@dataclass
class CommandSpec:
    cmd: Sequence[str]
    sudo: bool = False
    role: CommandRole | None = None
    check: bool = True
    capture: bool = True
    text: bool = True
    input_text: str | None = None
    env: dict[str, str] | None = None
    timeout: float | None = None
    summary: str = ''
    detail: str = ''
    submitted_by: str = ''


@dataclass
class CommandHandle:
    manager: 'CommandManager'
    command_id: int
    _result: CommandResult | None = None
    _executed: bool = False

    def done(self) -> bool:
        return self._executed

    def result(self) -> CommandResult:
        if not self._executed:
            self.manager.flush_through(self.command_id)
        assert self._result is not None
        return self._result

    @property
    def stdout(self) -> str:
        return self.result().stdout

    @property
    def stderr(self) -> str:
        return self.result().stderr

    @property
    def returncode(self) -> int:
        return self.result().code

    @property
    def code(self) -> int:
        return self.result().code

    def _set_result(self, result: CommandResult) -> None:
        self._result = result
        self._executed = True


@dataclass
class PlannedCommand:
    command_id: int
    spec: CommandSpec
    handle: CommandHandle


@dataclass
class CommandPlan:
    title: str
    why: str = ''
    approval_scope: str = ''
    submitted_by: str = ''
    commands: list[PlannedCommand] = field(default_factory=list)
    approved: bool = False
    executed_upto: int = -1
    closed: bool = False
    rendered_preview: bool = False

    def add(self, item: PlannedCommand) -> None:
        self.commands.append(item)

    def is_empty(self) -> bool:
        return not self.commands


@dataclass(frozen=True)
class CompatSudoIntent:
    yes: bool
    purpose: str
    action: CommandRole = 'modify'
    sticky: bool = False


class IntentScope:
    def __init__(
        self,
        manager: 'CommandManager',
        title: str,
        *,
        why: str = '',
        role: CommandRole = 'modify',
        visible: bool = True,
    ):
        self.manager = manager
        self.frame = IntentFrame(
            title=title, why=why, role=role, visible=visible
        )

    def __enter__(self) -> 'IntentScope':
        self.manager.push_intent(self.frame)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.manager.pop_intent(self.frame)
        return False


class PlanScope:
    def __init__(
        self,
        manager: 'CommandManager',
        title: str,
        *,
        why: str = '',
        approval_scope: str = '',
    ):
        self.manager = manager
        self.plan = CommandPlan(
            title=title,
            why=why,
            approval_scope=approval_scope,
            submitted_by=manager.capture_submitter(),
        )

    def __enter__(self) -> CommandPlan:
        self.manager.begin_plan(self.plan)
        return self.plan

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self.manager.finish_plan(self.plan)
            else:
                self.manager.abort_plan(self.plan)
        finally:
            self.manager.end_plan(self.plan)
        return False


_CURRENT_MANAGER: ContextVar['CommandManager | None'] = ContextVar(
    'aivm_current_command_manager', default=None
)


class CommandManager:
    """Central authority for subprocess execution and sudo approval UX."""

    @classmethod
    def current(cls) -> 'CommandManager':
        current = _CURRENT_MANAGER.get()
        if current is None:
            current = cls()
            _CURRENT_MANAGER.set(current)
        return current

    @classmethod
    def activate(cls, manager: 'CommandManager') -> None:
        _CURRENT_MANAGER.set(manager)

    @classmethod
    def reset_current(cls) -> None:
        _CURRENT_MANAGER.set(None)

    def __init__(
        self,
        *,
        yes: bool = False,
        yes_sudo: bool = False,
        auto_approve_readonly_sudo: bool = True,
    ):
        self.yes = bool(yes)
        self.yes_sudo = bool(yes_sudo)
        self.auto_approve_readonly_sudo = bool(auto_approve_readonly_sudo)
        self.intent_stack: list[IntentFrame] = []
        self.plan_stack: list[CommandPlan] = []
        self._next_command_id = 0
        self._approve_all_remaining = False
        self._compat_sudo_intent: CompatSudoIntent | None = None
        self._loose_commands: list[PlannedCommand] = []

    def push_intent(self, frame: IntentFrame) -> None:
        self.intent_stack.append(frame)

    def pop_intent(self, frame: IntentFrame) -> None:
        if self.intent_stack and self.intent_stack[-1] is frame:
            self.intent_stack.pop()
            return
        for idx in range(len(self.intent_stack) - 1, -1, -1):
            if self.intent_stack[idx] is frame:
                del self.intent_stack[idx]
                return

    def begin_plan(self, plan: CommandPlan) -> None:
        self.plan_stack.append(plan)

    def end_plan(self, plan: CommandPlan) -> None:
        if self.plan_stack and self.plan_stack[-1] is plan:
            self.plan_stack.pop()
            return
        for idx in range(len(self.plan_stack) - 1, -1, -1):
            if self.plan_stack[idx] is plan:
                del self.plan_stack[idx]
                return

    def abort_plan(self, plan: CommandPlan) -> None:
        plan.closed = True

    def finish_plan(self, plan: CommandPlan) -> None:
        if plan.is_empty():
            plan.closed = True
            return
        self._approve_plan_if_needed(plan)
        self._flush_plan(plan)
        plan.closed = True

    def current_plan(self) -> CommandPlan | None:
        return self.plan_stack[-1] if self.plan_stack else None

    def compat_arm_sudo_intent(
        self,
        *,
        yes: bool,
        purpose: str,
        action: str = 'modify',
        sticky: bool = False,
    ) -> None:
        role = self._normalize_role(action)
        self._compat_sudo_intent = CompatSudoIntent(
            yes=bool(yes), purpose=str(purpose), action=role, sticky=bool(sticky)
        )
        if sticky:
            self._approve_all_remaining = True

    def compat_clear_sudo_intent(self) -> None:
        self._compat_sudo_intent = None
        self._approve_all_remaining = False

    def compat_auto_yes(self) -> bool:
        return bool(self._approve_all_remaining)

    def capture_submitter(self) -> str:
        frame = inspect.currentframe()
        if frame is None:
            return ''
        try:
            cur = frame.f_back
            while cur is not None:
                mod = inspect.getmodule(cur)
                mod_name = mod.__name__ if mod is not None else ''
                if mod_name not in {'aivm.commands', 'aivm.util'}:
                    return (
                        f'{mod_name or "(unknown)"}:'
                        f'{cur.f_code.co_name}:{cur.f_lineno}'
                    )
                cur = cur.f_back
        finally:
            del frame
        return ''

    def submit(
        self,
        cmd: Sequence[str],
        *,
        sudo: bool = False,
        role: CommandRole | None = None,
        check: bool = True,
        capture: bool = True,
        text: bool = True,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        summary: str = '',
        detail: str = '',
        eager: bool = False,
    ) -> CommandHandle:
        spec = CommandSpec(
            cmd=tuple(str(c) for c in cmd),
            sudo=bool(sudo),
            role=role,
            check=bool(check),
            capture=bool(capture),
            text=bool(text),
            input_text=input_text,
            env=env,
            timeout=timeout,
            summary=summary.strip(),
            detail=detail.strip(),
            submitted_by=self.capture_submitter(),
        )
        handle = CommandHandle(manager=self, command_id=self._next_command_id)
        planned = PlannedCommand(
            command_id=self._next_command_id, spec=spec, handle=handle
        )
        self._next_command_id += 1

        plan = self.current_plan()
        if plan is not None:
            plan.add(planned)
            if eager:
                self.flush_through(planned.command_id)
            return handle

        self._loose_commands.append(planned)
        if eager:
            self.flush_through(planned.command_id)
        return handle

    def flush(self) -> None:
        if self.plan_stack:
            self._flush_plan(self.plan_stack[-1])
            return
        if self._loose_commands:
            self._flush_loose_commands()

    def flush_through(self, command_id: int) -> None:
        for plan in reversed(self.plan_stack):
            if any(item.command_id == command_id for item in plan.commands):
                self._approve_plan_if_needed(plan)
                self._flush_plan(plan, through_command_id=command_id)
                return
        if self._loose_commands:
            self._flush_loose_commands(through_command_id=command_id)
            return
        raise RuntimeError(f'Unknown command handle id: {command_id}')

    def _normalize_role(self, role: str | None) -> CommandRole:
        mode = str(role or 'modify').strip().lower()
        if mode not in {'read', 'modify'}:
            mode = 'modify'
        return mode  # type: ignore[return-value]

    def _effective_role(self, spec: CommandSpec) -> CommandRole:
        if spec.role is not None:
            return self._normalize_role(spec.role)
        if spec.sudo and not spec.check:
            return 'read'
        for frame in reversed(self.intent_stack):
            if frame.role in {'read', 'modify'}:
                return frame.role
        return 'modify'

    def render_breadcrumb(self) -> str:
        parts = [f.title for f in self.intent_stack if f.visible and f.title]
        return ' > '.join(parts)

    def _needs_sudo_approval(self, role: CommandRole) -> bool:
        if os.geteuid() == 0:
            return False
        if self.yes or self.yes_sudo or self._approve_all_remaining:
            return False
        if role == 'read' and self.auto_approve_readonly_sudo:
            return False
        return True

    def _plan_needs_approval(self, plan: CommandPlan) -> bool:
        return any(
            item.spec.sudo and self._needs_sudo_approval(self._effective_role(item.spec))
            for item in plan.commands
        )

    def _approve_plan_if_needed(self, plan: CommandPlan) -> None:
        if plan.approved:
            return
        self._render_plan_preview(plan)
        if not self._plan_needs_approval(plan):
            plan.approved = True
            return
        if not sys.stdin.isatty():
            raise RuntimeError(
                'Privileged host operations require confirmation, but stdin is not interactive. '
                'Re-run with --yes or --yes-sudo.'
            )
        while True:
            ans = input(
                'Approve this step? [y]es/[a]ll/[s]how/[N]o: '
            ).strip().lower()
            if ans in {'s', 'show'}:
                self._render_plan_full_commands(plan)
                continue
            if ans in {'a', 'all'}:
                self._approve_all_remaining = True
                plan.approved = True
                return
            if ans in {'y', 'yes'}:
                plan.approved = True
                return
            raise RuntimeError('Aborted by user.')

    def _render_plan_preview(self, plan: CommandPlan) -> None:
        if plan.rendered_preview:
            return
        breadcrumb = self.render_breadcrumb()
        local_log = log.opt(depth=2)
        local_log.info('Step: {}', plan.title)
        if plan.submitted_by:
            local_log.info('Submitted by: {}', plan.submitted_by)
        if breadcrumb:
            local_log.info('Context: {}', breadcrumb)
        if plan.why:
            local_log.info('Why: {}', plan.why)
        local_log.info('Planned commands: {}', len(plan.commands))
        for idx, item in enumerate(plan.commands, start=1):
            summary = item.spec.summary or shell_join(item.spec.cmd)
            role = self._effective_role(item.spec)
            preview_cmd = self._preview_command(item.spec)
            local_log.info('  {}. {}', idx, summary)
            local_log.info('     command: {}', preview_cmd)
            if item.spec.detail:
                local_log.debug('     detail: {}', item.spec.detail)
            raw_cmd = self._raw_command(item.spec)
            if raw_cmd != preview_cmd:
                local_log.debug('     raw command: {}', raw_cmd)
            local_log.trace('     role={} capture={}', role, item.spec.capture)
        plan.rendered_preview = True

    def _render_plan_full_commands(self, plan: CommandPlan) -> None:
        local_log = log.opt(depth=2)
        local_log.info('Full commands for step: {}', plan.title)
        for idx, item in enumerate(plan.commands, start=1):
            local_log.info('  {}. {}', idx, self._raw_command(item.spec))

    def _flush_plan(
        self,
        plan: CommandPlan,
        *,
        through_command_id: int | None = None,
    ) -> None:
        for idx in range(plan.executed_upto + 1, len(plan.commands)):
            item = plan.commands[idx]
            res = self._execute_one(
                item.spec,
                ordinal=(idx + 1, len(plan.commands)),
                within_plan=True,
            )
            item.handle._set_result(res)
            plan.executed_upto = idx
            if through_command_id is not None and item.command_id >= through_command_id:
                break

    def _flush_loose_commands(
        self, *, through_command_id: int | None = None
    ) -> None:
        while self._loose_commands:
            item = self._loose_commands[0]
            res = self._execute_one(item.spec, within_plan=False)
            item.handle._set_result(res)
            self._loose_commands.pop(0)
            if through_command_id is not None and item.command_id >= through_command_id:
                break

    def _raw_command(self, spec: CommandSpec) -> str:
        cmd = list(spec.cmd)
        if spec.sudo and os.geteuid() != 0:
            cmd = ['sudo', *cmd] if sys.stdin.isatty() else ['sudo', '-n', *cmd]
        return shell_join(cmd)

    def _preview_command(self, spec: CommandSpec, *, max_len: int = 160) -> str:
        cmd = list(spec.cmd)
        if spec.sudo and os.geteuid() != 0:
            cmd = ['sudo', *cmd] if sys.stdin.isatty() else ['sudo', '-n', *cmd]
        display_parts: list[str] = []
        for idx, part in enumerate(cmd):
            text = str(part)
            prev = str(cmd[idx - 1]) if idx > 0 else ''
            prev2 = str(cmd[idx - 2]) if idx > 1 else ''
            if len(text) > 80:
                if prev == '-lc' and prev2 in {'bash', 'sh'}:
                    text = '<shell script omitted>'
                elif idx == len(cmd) - 1 and 'ssh' in {str(cmd[0]), str(cmd[1]) if len(cmd) > 1 else ''}:
                    text = '<remote command omitted>'
                else:
                    text = text[:57] + '...'
            display_parts.append(shlex.quote(text))
        preview_cmd = ' '.join(display_parts)
        if len(preview_cmd) <= max_len:
            return preview_cmd
        if max_len <= 3:
            return preview_cmd[:max_len]
        return preview_cmd[: max_len - 3] + '...'

    def _ensure_compat_sudo_ready(
        self, spec: CommandSpec, *, within_plan: bool = False
    ) -> None:
        if not spec.sudo or os.geteuid() == 0:
            return
        if within_plan:
            return
        intent = self._compat_sudo_intent
        if intent is None:
            return
        role = self._effective_role(spec)
        auto_yes = bool(
            self.yes
            or self.yes_sudo
            or self._approve_all_remaining
            or intent.yes
            or (role == 'read' and self.auto_approve_readonly_sudo)
        )
        if auto_yes:
            return
        local_log = log.opt(depth=3)
        local_log.info(
            'About to run privileged {} host operations via sudo:',
            'read-only' if role == 'read' else 'state-changing',
        )
        local_log.info('  {}', intent.purpose)
        if not sys.stdin.isatty():
            raise RuntimeError(
                'Privileged host operations require confirmation, but stdin is not interactive. '
                'Re-run with --yes or --yes-sudo.'
            )
        ans = input('Continue? [y]es/[a]ll/[N]o: ').strip().lower()
        if ans in {'a', 'all'}:
            self._approve_all_remaining = True
            self._compat_sudo_intent = CompatSudoIntent(
                yes=True,
                purpose=intent.purpose,
                action=intent.action,
                sticky=True,
            )
            return
        if ans not in {'y', 'yes'}:
            raise RuntimeError('Aborted by user.')

    def _execute_one(
        self,
        spec: CommandSpec,
        *,
        ordinal: tuple[int, int] | None = None,
        within_plan: bool = False,
    ) -> CommandResult:
        local_log = log.opt(depth=3)
        cmd = list(spec.cmd)
        self._ensure_compat_sudo_ready(spec, within_plan=within_plan)
        if spec.sudo and os.geteuid() != 0:
            cmd = ['sudo', *cmd] if sys.stdin.isatty() else ['sudo', '-n', *cmd]

        run_line = shell_join(cmd)

        # Keep mutating or privileged work visible at INFO while leaving
        # unprivileged plumbing at DEBUG unless a plan preview already framed it.
        if spec.sudo:
            logger = local_log.info
        else:
            logger = local_log.debug

        if within_plan and ordinal is not None:
            current, total = ordinal
            if spec.submitted_by:
                logger(
                    'RUN [{}/{}]: {} (submitted_by={})',
                    current,
                    total,
                    run_line,
                    spec.submitted_by,
                )
            else:
                logger('RUN [{}/{}]: {}', current, total, run_line)
        elif spec.check:
            if spec.submitted_by:
                local_log.info(
                    'RUN: {} (submitted_by={})',
                    run_line,
                    spec.submitted_by,
                )
            else:
                local_log.info('RUN: {}', run_line)
        else:
            if spec.submitted_by:
                logger(
                    'RUN: {} (submitted_by={})',
                    run_line,
                    spec.submitted_by,
                )
            else:
                logger('RUN: {}', run_line)

        try:
            proc = subprocess.run(
                cmd,
                input=spec.input_text if spec.input_text is not None else None,
                capture_output=spec.capture,
                text=spec.text,
                env=spec.env,
                timeout=spec.timeout,
            )
            res = CommandResult(
                proc.returncode,
                proc.stdout or '',
                proc.stderr or '',
            )
        except subprocess.TimeoutExpired as ex:
            stdout = ex.stdout or ''
            stderr = ex.stderr or ''
            if not isinstance(stdout, str):
                stdout = stdout.decode(errors='replace')
            if not isinstance(stderr, str):
                stderr = stderr.decode(errors='replace')
            res = CommandResult(124, stdout, (stderr + '\ncommand timed out').strip())
            local_log.warning(
                'Command timed out after {}s cmd={}',
                spec.timeout,
                run_line,
            )
            if spec.check:
                raise CommandError(cmd, res) from ex
            return res

        local_log.trace(
            'Command result code={} stdout_len={} stderr_len={}',
            res.code,
            len(res.stdout),
            len(res.stderr),
        )
        if spec.check and res.code != 0:
            local_log.error(
                'Command failed code={} cmd={} stderr={} stdout={}',
                res.code,
                run_line,
                res.stderr.strip(),
                res.stdout.strip(),
            )
            raise CommandError(cmd, res)
        return res
