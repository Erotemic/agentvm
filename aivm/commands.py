"""Centralized command orchestration, logging, and approval handling.

This module is the long-term home for subprocess execution in ``aivm``.
It organizes command output around user-meaningful plans and intent
contexts while still preserving raw command visibility for deeper
debugging.

The main entry point is :class:`CommandManager`, which coordinates
command submission, grouped plan execution, and privileged-operation
approval prompts.
"""

from __future__ import annotations

import inspect
import os
import shlex
import subprocess
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from sys import version_info
from typing import Literal, Sequence

if version_info >= (3, 11):
    from types import TracebackType
else:
    from types import TracebackType

from loguru import logger

log = logger

# TODO: The current command role model is too coarse.
# don't execute on this, this needs more thought. Current ideas written here.
#
# Right now roles mostly collapse into "read" vs "modify", but in practice
# there are at least two separate axes we care about:
#
# 1. privilege boundary
#    - unprivileged
#    - privileged / sudo
#
# 2. effect boundary
#    - read / inspect
#    - system write
#    - user-file write
#
# These should not be conflated. Some operations are unprivileged but still
# deserve explicit approval because they modify user-owned files (for example
# SSH config or other dotfiles). Other operations are privileged but read-only
# and may be safe to auto-approve in some contexts.
#
# Future design:
# - replace the coarse role model with a richer action / approval model
# - allow step-level approval policy to be explicit instead of only inferred
# - distinguish "touches user files" from "touches system state"
# - distinguish "requires sudo" from "is a write"

CommandRole = Literal['read', 'modify']


def shell_join(cmd: Sequence[str]) -> str:
    """Render a command sequence as a shell-escaped string.

    This is intended for logging and preview output rather than direct
    execution. Each element is converted to ``str`` and quoted with
    :func:`shlex.quote`.

    Args:
        cmd: Command tokens to render.

    Returns:
        A shell-escaped command line string.
    """
    return ' '.join(shlex.quote(str(c)) for c in cmd)


@dataclass(frozen=True)
class CommandResult:
    """Immutable result of one executed command.

    Attributes:
        code: Process exit status.
        stdout: Captured standard output text.
        stderr: Captured standard error text.
    """

    code: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    """Error raised when a checked command finishes unsuccessfully.

    The exception retains both the original command and the normalized
    :class:`CommandResult` so callers can inspect exit status and any
    captured output.

    Attributes:
        cmd: The command that failed.
        result: The normalized result object for the failed command.
    """

    def __init__(self, cmd: Sequence[str] | str, result: CommandResult) -> None:
        self.cmd = cmd
        self.result = result
        super().__init__(
            f'Command failed (code={result.code}): {cmd}\n{result.stderr}'.strip()
        )


@dataclass(frozen=True)
class IntentFrame:
    """One entry in the manager's intent stack.

    Intent frames describe *why* the caller is traversing a command tree.
    Visible frames are surfaced in breadcrumbs and plan previews to help a
    human understand the current operation at a glance.

    Attributes:
        title: Short human-readable title for this context.
        why: Optional longer explanation for the context.
        role: Default command role implied by this context.
        visible: If True, include this frame in rendered breadcrumbs.
    """

    title: str
    why: str = ''
    role: CommandRole = 'modify'
    visible: bool = True


@dataclass
class CommandSpec:
    """Normalized specification for a queued command.

    A ``CommandSpec`` stores the execution parameters associated with one
    command submission. These specs are later previewed, approved, and
    executed by :class:`CommandManager`.

    Attributes:
        cmd: Command tokens to execute.
        sudo: If True, execute through ``sudo`` when needed.
        role: Optional explicit command role. When omitted, the role is
            inferred from the surrounding intent context.
        check: If True, raise :class:`CommandError` on non-zero exit.
        capture: If True, capture stdout and stderr.
        text: If True, run the subprocess in text mode.
        input_text: Optional standard input text to send to the process.
        env: Optional process environment override.
        timeout: Optional timeout in seconds.
        summary: Short human-facing summary shown in previews.
        detail: Optional longer preview detail.
        submitted_by: Caller provenance string captured at submission time.
    """

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
    """Lazy handle for one submitted command.

    Handles are returned from :meth:`CommandManager.submit`. They let the
    caller defer execution until a later flush, or force execution on
    demand by asking for the result.

    Attributes:
        manager: The owning command manager.
        command_id: Monotonic identifier assigned at submission time.
    """

    manager: 'CommandManager'
    command_id: int
    _result: CommandResult | None = None
    _executed: bool = False

    def done(self) -> bool:
        """Return True if this command has already been executed."""
        return self._executed

    def result(self) -> CommandResult:
        """Return the command result, executing through this handle if needed.

        If the command has not run yet, this method asks the manager to
        flush execution through this handle's command id before returning
        the cached result.

        Returns:
            The normalized command result.
        """
        if not self._executed:
            self.manager.flush_through(self.command_id)
        assert self._result is not None
        return self._result

    @property
    def stdout(self) -> str:
        """Return captured standard output for this command."""
        return self.result().stdout

    @property
    def stderr(self) -> str:
        """Return captured standard error for this command."""
        return self.result().stderr

    @property
    def returncode(self) -> int:
        """Return the process exit status for this command."""
        return self.result().code

    @property
    def code(self) -> int:
        """Alias for :attr:`returncode`."""
        return self.result().code

    def _set_result(self, result: CommandResult) -> None:
        """Record the result of execution on this handle."""
        self._result = result
        self._executed = True


@dataclass
class PlannedCommand:
    """Command record stored inside a plan or loose-command queue.

    This ties together a submitted specification, its public handle, and
    the manager-assigned command id used to control partial flushing.
    """

    command_id: int
    spec: CommandSpec
    handle: CommandHandle


@dataclass
class CommandPlan:
    """Ordered group of commands previewed and executed as one step.

    Plans are usually created indirectly through :class:`PlanScope`. They
    collect related commands, present a plan preview, optionally request
    approval, and then execute in order.

    Attributes:
        title: Human-facing title for the step.
        why: Optional explanation of the step's purpose.
        approval_scope: Optional label describing the approval boundary.
        submitted_by: Caller provenance string for the plan.
        commands: Commands in submission order.
        approved: True once this plan has cleared approval.
        executed_upto: Highest command index already executed.
        closed: True once the plan lifecycle has ended.
        rendered_preview: True once the preview has been logged.
    """

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
        """Append one planned command to this plan."""
        self.commands.append(item)

    def is_empty(self) -> bool:
        """Return True if this plan contains no commands."""
        return not self.commands


class IntentScope:
    """Context manager that temporarily pushes an intent frame.

    Use this to describe the current user-visible task while building up a
    command tree. Nested scopes form the breadcrumb shown in plan previews.
    """

    def __init__(
        self,
        manager: 'CommandManager',
        title: str,
        *,
        why: str = '',
        role: CommandRole = 'modify',
        visible: bool = True,
    ) -> None:
        self.manager = manager
        self.frame = IntentFrame(
            title=title, why=why, role=role, visible=visible
        )

    def __enter__(self) -> 'IntentScope':
        """Push this scope's intent frame onto the manager."""
        self.manager.push_intent(self.frame)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        """Pop this scope's frame from the manager on exit."""
        self.manager.pop_intent(self.frame)
        return False


class PlanScope:
    """Context manager that groups submitted commands into one plan.

    On successful exit, the collected plan is finalized, previewed, and
    flushed. If the block raises an exception, the plan is aborted instead.
    """

    def __init__(
        self,
        manager: 'CommandManager',
        title: str,
        *,
        why: str = '',
        approval_scope: str = '',
    ) -> None:
        self.manager = manager
        self.plan = CommandPlan(
            title=title,
            why=why,
            approval_scope=approval_scope,
            submitted_by=manager.capture_submitter(),
        )

    def __enter__(self) -> CommandPlan:
        """Begin collecting commands into this scope's plan."""
        self.manager.begin_plan(self.plan)
        return self.plan

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        """Finish or abort the plan, then remove it from the manager."""
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
    """Central authority for command submission, execution, and approval.

    A command manager organizes subprocess execution around human-readable
    intent scopes and plans. Commands may be submitted either into the
    current open plan or as loose commands. Plans can be previewed and
    approved as a unit before execution.

    Args:
        yes: If True, auto-approve operations that would otherwise prompt.
        yes_sudo: If True, auto-approve privileged sudo operations.
        auto_approve_readonly_sudo: If True, allow read-only sudo commands
            to proceed without prompting when possible.

    Example:
        >>> # Submit one loose command outside of any step.
        >>> from aivm.commands import *  # NOQA
        >>> py = sys.executable
        >>> mgr = CommandManager(yes=True)
        >>> h = mgr.submit(
        ...     [py, '-c', 'print("virsh dominfo demo-vm")'],
        ...     summary='Run one ad hoc inspection command',
        ... )
        >>> print(h.stdout.strip())
        virsh dominfo demo-vm

    Example:
        >>> # Group several "privileged" actions into one readable step.
        >>> from aivm.commands import *  # NOQA
        >>> py = sys.executable
        >>> mgr = CommandManager(yes=True)
        >>> with mgr.intent(title='User specified intent title (e.g. prepare host)', role='modify'):
        ...     with mgr.step(
        ...         title='User specified step title. e.g. install host dependencies',
        ...         why='User specified why. e.g. prepare the machine for VM lifecycle operations',
        ...     ):
        ...         h1 = mgr.submit(
        ...             [py, '-c', 'print("sudo apt-get update -y")'],
        ...             summary='User summary, e.g. Refresh apt metadata',
        ...         )
        ...         h2 = mgr.submit(
        ...             [py, '-c', 'print("sudo apt-get install -y qemu-system libvirt-daemon-system")'],
        ...             summary='User summary, e.g. Install virtualization packages',
        ...         )
        ...         h3 = mgr.submit(
        ...             [py, '-c', 'print("sudo systemctl enable --now libvirtd")'],
        ...             summary='User summary, e.g. Enable libvirtd service',
        ...         )

    Example:
        >>> # Discover something in one step, then use it in a later step.
        >>> from aivm.commands import *  # NOQA
        >>> py = sys.executable
        >>> mgr = CommandManager(yes=True)
        >>> with mgr.intent('configure guest access', role='modify'):
        ...     with mgr.step(
        ...         'discover VM address',
        ...         why='later commands need the current guest IP',
        ...     ):
        ...         ip = mgr.submit(
        ...             [py, '-c', 'print("10.0.0.42")'],
        ...             summary='Read cached VM IP',
        ...         )
        >>> addr = ip.stdout.strip()
        >>> with mgr.intent('configure guest access', role='modify'):
        ...     with mgr.step(
        ...         'test SSH command composition',
        ...         why='show how later commands can consume earlier output',
        ...     ):
        ...         cmd = mgr.submit(
        ...             [py, '-c', f'print("ssh agent@{addr} sudo systemctl status ssh")'],
        ...             summary='Show the SSH command that would be run',
        ...         )

    Example:
        >>> # Use output from an earlier command inside the same step when the
        >>> # later command depends on it.
        >>> from aivm.commands import *  # NOQA
        >>> py = sys.executable
        >>> mgr = CommandManager(yes=True)
        >>> with mgr.intent('reconcile attachment', role='modify'):
        ...     with mgr.step(
        ...         'inspect then repair bind target',
        ...         why='the repair command depends on the detected source',
        ...     ):
        ...         current = mgr.submit(
        ...             [py, '-c', 'print("/old/source")'],
        ...             summary='Inspect current bind source',
        ...         )
        ...         found = current.stdout.strip()
        ...         repair = mgr.submit(
        ...             [py, '-c', f'print("sudo mount --bind /new/source /srv/target  # replacing {found}")'],
        ...             summary='Replace stale bind source',
        ...         )

    Example:
        >>> # xdoctest: +IGNORE_WANT
        >>> from aivm.commands import *  # NOQA
        >>> import sys
        >>> mgr = CommandManager(yes=True)
        >>> with mgr.intent(title='inspect runtime', role='read'):
        ...     h3 = mgr.submit(
        ...         [sys.executable, '-c', 'print("alpha")'],
        ...         summary='emit alpha',
        ...     )
        ...     with mgr.step(title='collect facts', why='demonstrate the command lifecycle') as plan:
        ...         h1 = mgr.submit(
        ...             [sys.executable, '-c', 'print("alpha")'],
        ...             summary='emit alpha',
        ...         )
        ...         h2 = mgr.submit(
        ...             [sys.executable, '-c', 'print("beta")'],
        ...             summary='emit beta',
        ...         )
        ...         # The plan is executed after the context ends.
    """

    def intent(
        self,
        title: str,
        *,
        why: str = '',
        role: CommandRole = 'modify',
        visible: bool = True,
    ) -> IntentScope:
        """
        Context manager that temporarily pushes an intent frame.

        Use this to mark some high level intent.
        """
        return IntentScope(self, title, why=why, role=role, visible=visible)

    def step(
        self,
        title: str,
        *,
        why: str = '',
        approval_scope: str = '',
    ) -> PlanScope:
        """
        Context manager that groups submitted commands into one plan

        Use this to group related commands.
        """
        # TODO: might want to rename this to StepScope
        return PlanScope(
            self,
            title,
            why=why,
            approval_scope=approval_scope,
        )

    @classmethod
    def current(cls) -> 'CommandManager':
        """Return the current context-local manager, creating one if needed."""
        current = _CURRENT_MANAGER.get()
        if current is None:
            current = cls()
            _CURRENT_MANAGER.set(current)
        return current

    @classmethod
    def activate(cls, manager: 'CommandManager') -> None:
        """Install ``manager`` as the current context-local manager."""
        _CURRENT_MANAGER.set(manager)

    @classmethod
    def reset_current(cls) -> None:
        """Clear the current context-local manager."""
        _CURRENT_MANAGER.set(None)

    def __init__(
        self,
        *,
        yes: bool = False,
        yes_sudo: bool = False,
        auto_approve_readonly_sudo: bool = True,
    ) -> None:
        self.yes = bool(yes)
        self.yes_sudo = bool(yes_sudo)
        self.auto_approve_readonly_sudo = bool(auto_approve_readonly_sudo)
        self.intent_stack: list[IntentFrame] = []
        self.plan_stack: list[CommandPlan] = []
        self._next_command_id = 0
        self._approve_all_remaining = False
        self._loose_commands: list[PlannedCommand] = []
        self._sudo_authentication_required: bool | None = None

    def push_intent(self, frame: IntentFrame) -> None:
        """Push one intent frame onto the active intent stack."""
        # TODO: probably a good public method, let the underlying scope handle it.
        self.intent_stack.append(frame)

    def pop_intent(self, frame: IntentFrame) -> None:
        """Remove ``frame`` from the active intent stack.

        The most recently pushed matching frame is removed. This tolerates
        mildly out-of-order cleanup to avoid leaking stale context.
        """
        # TODO: probably a good public method, let the underlying scope handle it.
        if self.intent_stack and self.intent_stack[-1] is frame:
            self.intent_stack.pop()
            return
        for idx in range(len(self.intent_stack) - 1, -1, -1):
            if self.intent_stack[idx] is frame:
                del self.intent_stack[idx]
                return

    def begin_plan(self, plan: CommandPlan) -> None:
        """Push an in-progress plan onto the plan stack."""
        # TODO: probably a good public method, let the underlying scope handle it.
        self.plan_stack.append(plan)

    def end_plan(self, plan: CommandPlan) -> None:
        """Remove ``plan`` from the active plan stack."""
        # TODO: probably a good public method, let the underlying scope handle it.
        if self.plan_stack and self.plan_stack[-1] is plan:
            self.plan_stack.pop()
            return
        for idx in range(len(self.plan_stack) - 1, -1, -1):
            if self.plan_stack[idx] is plan:
                del self.plan_stack[idx]
                return

    def abort_plan(self, plan: CommandPlan) -> None:
        """Mark ``plan`` as closed without executing its commands."""
        # TODO: probably a good public method, let the underlying scope handle it.
        plan.closed = True

    def finish_plan(self, plan: CommandPlan) -> None:
        """Finalize, approve, and execute a plan.

        Empty plans are simply marked closed. Non-empty plans are previewed,
        approved if needed, then flushed in order.
        """
        # TODO: probably a good public method, let the underlying scope handle it.
        if plan.is_empty():
            plan.closed = True
            return
        self._approve_plan_if_needed(plan)
        self._flush_plan(plan)
        plan.closed = True

    def current_plan(self) -> CommandPlan | None:
        """Return the currently active innermost plan, if any."""
        return self.plan_stack[-1] if self.plan_stack else None

    def capture_submitter(self) -> str:
        """Return a best-effort provenance string for the submitter.

        The returned value is formatted as ``module:function:lineno`` and is
        intended for debugging and log output. Frames within selected
        internal modules are skipped so the provenance points at the caller.
        """
        # TODO: We probably want the logger to just take care of this.  This is
        # too heavy handed.
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
        """Submit one command and return a handle to its eventual result.

        If a plan is currently open, the command is appended to that plan.
        Otherwise it is queued as a loose command. When ``eager`` is True,
        execution is flushed through this command immediately.

        Args:
            cmd: Command tokens to execute.
            sudo: If True, execute through ``sudo`` when needed.
            role: Optional explicit command role.
            check: If True, raise :class:`CommandError` on non-zero exit.
            capture: If True, capture stdout and stderr.
            text: If True, run the subprocess in text mode.
            input_text: Optional standard input text.
            env: Optional process environment override.
            timeout: Optional timeout in seconds.
            summary: Short human-facing summary for previews.
            detail: Optional longer preview detail.
            eager: If True, execute through this command immediately.

        Returns:
            A handle that can be used to inspect the eventual result.
        """
        # TODO: ergonomic ubelt style string acceptence? Might be better to
        # keep it type strict though. Don't do this one yet. Need to think
        # about it more.
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

    def run(
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
    ) -> CommandResult:
        """Submit one command and return its result immediately."""
        return self.submit(
            cmd,
            sudo=sudo,
            role=role,
            check=check,
            capture=capture,
            text=text,
            input_text=input_text,
            env=env,
            timeout=timeout,
            summary=summary,
            detail=detail,
            eager=True,
        ).result()

    def flush(self) -> None:
        """Flush pending execution for the current plan or loose queue."""
        # TODO: does this need to be public?
        if self.plan_stack:
            self._flush_plan(self.plan_stack[-1])
            return
        if self._loose_commands:
            self._flush_loose_commands()

    def flush_through(self, command_id: int) -> None:
        """Flush execution through the specified command id.

        This executes all earlier pending commands needed to reach the
        requested command, whether it lives inside an active plan or in the
        loose-command queue.

        Args:
            command_id: Identifier of the last command that must be run.
        """
        # TODO: does this need to be public?
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
        """Normalize a role string to ``'read'`` or ``'modify'``."""
        mode = str(role or 'modify').strip().lower()
        if mode == 'read':
            return 'read'
        return 'modify'

    def _effective_role(self, spec: CommandSpec) -> CommandRole:
        """Infer the effective role for a command specification."""
        if spec.role is not None:
            return self._normalize_role(spec.role)
        if spec.sudo and not spec.check:
            return 'read'
        for frame in reversed(self.intent_stack):
            if frame.role in {'read', 'modify'}:
                return frame.role
        return 'modify'

    def render_breadcrumb(self) -> str:
        """Render the visible intent stack as a breadcrumb string."""
        parts = [f.title for f in self.intent_stack if f.visible and f.title]
        return ' > '.join(parts)

    def _needs_sudo_approval(self, role: CommandRole) -> bool:
        """Return True if a sudo command with ``role`` needs confirmation."""
        if os.geteuid() == 0:
            return False
        if self.yes or self.yes_sudo or self._approve_all_remaining:
            return False
        if role == 'read' and self.auto_approve_readonly_sudo:
            return False
        if self.sudo_authentication_required():
            return True
        return True

    def sudo_authentication_required(self) -> bool:
        """Return True when the current user likely needs sudo auth."""
        if os.geteuid() == 0:
            self._sudo_authentication_required = False
            return False
        if self._sudo_authentication_required is not None:
            return self._sudo_authentication_required
        probe = subprocess.run(
            ['sudo', '-n', 'true'],
            capture_output=True,
            text=True,
        )
        self._sudo_authentication_required = probe.returncode != 0
        log.opt(depth=0).trace(
            'sudo auth probe returncode={} stderr={!r}',
            probe.returncode,
            (probe.stderr or '').strip(),
        )
        return self._sudo_authentication_required

    def _readonly_sudo_policy_note(self) -> str:
        if self.auto_approve_readonly_sudo:
            return (
                'Future read-only sudo commands are configured to auto-approve '
                'once authentication is ready.'
            )
        return (
            'Future read-only sudo commands are configured to keep asking for '
            'approval unless you choose [a]ll or enable auto approval in config.'
        )

    def _render_sudo_prompt_context(
        self,
        *,
        purpose: str,
        role: CommandRole,
        auth_required: bool,
        preview_cmds: Sequence[Sequence[str]] | None = None,
    ) -> None:
        local_log = log.opt(depth=0)
        local_log.info(
            'About to request sudo for {} host operations:',
            'read-only' if role == 'read' else 'state-changing',
        )
        local_log.info('  {}', purpose)
        if preview_cmds:
            local_log.info('  Planned sudo commands:')
            for idx, cmd in enumerate(preview_cmds, start=1):
                rendered = shell_join(['sudo', *(str(part) for part in cmd)])
                local_log.info('    {}. {}', idx, rendered)
        if auth_required:
            local_log.info(
                '  Sudo authentication appears to be required before the next command can run.'
            )
            local_log.info('  {}', self._readonly_sudo_policy_note())

    def _authenticate_sudo(self) -> None:
        """Refresh sudo credentials now so later commands do not surprise."""
        if os.geteuid() == 0:
            self._sudo_authentication_required = False
            return
        cmd = ['sudo', '-v']
        if not sys.stdin.isatty():
            cmd = ['sudo', '-n', '-v']
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            res = CommandResult(
                proc.returncode,
                proc.stdout or '',
                proc.stderr or '',
            )
            raise CommandError(cmd, res)
        self._sudo_authentication_required = False

    def confirm_sudo_scope(
        self,
        *,
        purpose: str,
        role: str = 'modify',
        yes: bool = False,
        preview_cmds: Sequence[Sequence[str]] | None = None,
    ) -> None:
        """Preflight sudo approval/authentication for an upcoming operation."""
        if os.geteuid() == 0:
            return
        eff_role = self._normalize_role(role)
        auth_required = self.sudo_authentication_required()
        auto_yes = bool(
            yes
            or self.yes
            or self.yes_sudo
            or self._approve_all_remaining
            or (eff_role == 'read' and self.auto_approve_readonly_sudo)
        )
        if auth_required:
            self._render_sudo_prompt_context(
                purpose=purpose,
                role=eff_role,
                auth_required=True,
                preview_cmds=preview_cmds,
            )
        if auto_yes:
            if auth_required:
                self._authenticate_sudo()
            return
        if not sys.stdin.isatty():
            raise RuntimeError(
                'Privileged host operations require confirmation, but stdin is not interactive. '
                'Re-run with --yes or --yes-sudo.'
            )
        if not auth_required:
            self._render_sudo_prompt_context(
                purpose=purpose,
                role=eff_role,
                auth_required=False,
                preview_cmds=preview_cmds,
            )
        ans = input('Continue? [y]es/[a]ll/[N]o: ').strip().lower()
        if ans in {'a', 'all'}:
            self._approve_all_remaining = True
        elif ans not in {'y', 'yes'}:
            raise RuntimeError('Aborted by user.')
        if auth_required:
            self._authenticate_sudo()

    def confirm_file_update(
        self,
        *,
        path: str | os.PathLike[str],
        purpose: str,
        yes: bool = False,
    ) -> None:
        """Confirm an update to a user-managed host file."""
        if yes or self.yes or self._approve_all_remaining:
            return
        if not sys.stdin.isatty():
            raise RuntimeError(
                'External host file updates require confirmation, but stdin is not interactive. '
                'Re-run with --yes.'
            )
        local_log = log.opt(depth=0)
        local_log.info('About to update a host file not managed by aivm:')
        local_log.info('  {}', os.fspath(path))
        local_log.info('  {}', purpose)
        ans = input('Continue? [y/N]: ').strip().lower()
        if ans not in {'y', 'yes'}:
            raise RuntimeError('Aborted by user.')

    def _plan_needs_approval(self, plan: CommandPlan) -> bool:
        """Return True if any command in ``plan`` requires approval."""
        return any(
            item.spec.sudo
            and self._needs_sudo_approval(self._effective_role(item.spec))
            for item in plan.commands
        )

    def _approve_plan_if_needed(self, plan: CommandPlan) -> None:
        """Render and approve ``plan`` if approval has not already occurred."""
        if plan.approved:
            return
        self._render_plan_preview(plan)
        readonly_autoapproved_sudo = [
            item
            for item in plan.commands
            if item.spec.sudo
            and self._effective_role(item.spec) == 'read'
            and self.auto_approve_readonly_sudo
        ]
        if (
            readonly_autoapproved_sudo
            and not self._plan_needs_approval(plan)
            and not (self.yes or self.yes_sudo or self._approve_all_remaining)
        ):
            if self.sudo_authentication_required():
                self._authenticate_sudo()
            plan.approved = True
            return
        if not self._plan_needs_approval(plan):
            plan.approved = True
            return
        if not sys.stdin.isatty():
            raise RuntimeError(
                'Privileged host operations require confirmation, but stdin is not interactive. '
                'Re-run with --yes or --yes-sudo.'
            )
        while True:
            ans = (
                input('Approve this step? [y]es/[a]ll/[s]how/[N]o: ')
                .strip()
                .lower()
            )
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
        """Log a concise preview of the commands contained in ``plan``."""
        if plan.rendered_preview:
            return
        breadcrumb = self.render_breadcrumb()
        local_log = log.opt(depth=0)
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
            command_label = (
                'command (read-only)' if role == 'read' else 'command'
            )
            local_log.info('     {}: {}', command_label, preview_cmd)
            if item.spec.detail:
                local_log.debug('     detail: {}', item.spec.detail)
            raw_cmd = self._raw_command(item.spec)
            if raw_cmd != preview_cmd:
                local_log.debug('     raw command: {}', raw_cmd)
            local_log.trace('     role={} capture={}', role, item.spec.capture)
        plan.rendered_preview = True

    def _render_plan_full_commands(self, plan: CommandPlan) -> None:
        """Log the full raw command lines for every item in ``plan``."""
        local_log = log.opt(depth=0)
        local_log.info('Full commands for step: {}', plan.title)
        for idx, item in enumerate(plan.commands, start=1):
            local_log.info('  {}. {}', idx, self._raw_command(item.spec))

    def _confirm_loose_sudo_command(self, spec: CommandSpec) -> None:
        """Confirm one sudo command that is not grouped into a plan."""
        if not spec.sudo or os.geteuid() == 0:
            return
        role = self._effective_role(spec)
        purpose = spec.summary.strip()
        if not purpose:
            breadcrumb = self.render_breadcrumb().strip()
            if breadcrumb:
                purpose = breadcrumb
            else:
                purpose = (
                    'Run a privileged host command without an explicit step.'
                )
        if not spec.summary.strip():
            log.opt(depth=0).info(
                '  This sudo command is not grouped into an explicit step. '
                'Wrap related work in mgr.intent(...) / mgr.step(...) for clearer previews and fewer prompts.'
            )
        self.confirm_sudo_scope(
            yes=False,
            purpose=purpose,
            role=role,
            preview_cmds=[list(spec.cmd)],
        )

    def _flush_plan(
        self,
        plan: CommandPlan,
        *,
        through_command_id: int | None = None,
    ) -> None:
        """Execute pending commands in ``plan`` in submission order."""
        for idx in range(plan.executed_upto + 1, len(plan.commands)):
            item = plan.commands[idx]
            res = self._execute_one(
                item.spec,
                ordinal=(idx + 1, len(plan.commands)),
                within_plan=True,
            )
            item.handle._set_result(res)
            plan.executed_upto = idx
            if (
                through_command_id is not None
                and item.command_id >= through_command_id
            ):
                break

    def _flush_loose_commands(
        self, *, through_command_id: int | None = None
    ) -> None:
        """Execute pending loose commands in FIFO order."""
        while self._loose_commands:
            item = self._loose_commands[0]
            res = self._execute_one(item.spec, within_plan=False)
            item.handle._set_result(res)
            self._loose_commands.pop(0)
            if (
                through_command_id is not None
                and item.command_id >= through_command_id
            ):
                break

    def _raw_command(self, spec: CommandSpec) -> str:
        """Return the full shell-rendered command that would be executed."""
        cmd = list(spec.cmd)
        if spec.sudo and os.geteuid() != 0:
            cmd = ['sudo', *cmd] if sys.stdin.isatty() else ['sudo', '-n', *cmd]
        return shell_join(cmd)

    def _preview_command(self, spec: CommandSpec, *, max_len: int = 160) -> str:
        """Return a shortened command preview suitable for logs.

        Long shell snippets and remote command tails are abbreviated to keep
        previews readable while still revealing the overall command shape.
        """
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
                elif idx == len(cmd) - 1 and 'ssh' in {
                    str(cmd[0]),
                    str(cmd[1]) if len(cmd) > 1 else '',
                }:
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

    def _execute_one(
        self,
        spec: CommandSpec,
        *,
        ordinal: tuple[int, int] | None = None,
        within_plan: bool = False,
    ) -> CommandResult:
        """Execute one command specification and normalize its result."""
        local_log = log.opt(depth=0)
        if spec.sudo and not within_plan:
            self._confirm_loose_sudo_command(spec)
        cmd = list(spec.cmd)
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
            res = CommandResult(
                124, stdout, (stderr + '\ncommand timed out').strip()
            )
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
