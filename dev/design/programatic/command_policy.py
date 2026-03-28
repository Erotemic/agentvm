from __future__ import annotations

from itertools import product
from typing import Any, Dict, Iterable, List, Literal, Optional

import pandas as pd

STAR = '*'

PromptKind = Literal['none', 'file_update', 'plan', 'sudo']
QueueKind = Literal['none', 'loose', 'plan']
OpKind = Literal['file_update', 'command']
Role = Literal['read', 'modify']
BehaviorClass = Literal[
    'file_update_bypass',
    'file_update_noninteractive_error',
    'file_update_prompt',
    'plan_non_sudo_preview_only',
    'planned_sudo_autoapproved',
    'plan_noninteractive_error',
    'plan_prompt',
    'loose_non_sudo',
    'already_root',
    'loose_sudo_autoapproved',
    'loose_sudo_autoapproved_then_preauth',
    'loose_sudo_noninteractive_error',
    'loose_sudo_prompt',
    'loose_sudo_prompt_then_preauth',
]


# ----
# 1) Main behavior table
# ----
#
# Notes:
# - op_yes is the per-operation `yes=` argument to confirm_* methods.
# - In ordinary submit()/run() command flow:
#     * plan approval has no op_yes input
#     * loose sudo currently reaches confirm_sudo_scope(..., yes=False)
#   so op_yes mostly matters for direct confirm_* callers.
# - queue="none" is used for file_update rows.
#
# Compression keys off canonical outcome columns plus a stable behavior_class.
# A human-readable note is derived afterward so prose does not block wildcard
# compression.
#
# This is intentionally "current behavior", not intended policy.

BEHAVIOR_PREDICATE_COLS = [
    'op_kind',
    'queue',
    'sudo',
    'is_root',
    'stdin_tty',
    'op_yes',
    'manager_yes',
    'yes_sudo',
    'approve_all_remaining',
    'auth_required',
    'effective_role',
    'auto_approve_readonly_sudo',
]

BEHAVIOR_OUTCOME_KEY_COLS = [
    'render_plan_preview',
    'render_sudo_context',
    'prompt_kind',
    'noninteractive_error',
    'proceeds_without_prompt',
    'preauthenticate_without_prompt',
    'preauthenticate_if_prompt_approved',
    'sudo_auth_deferred_to_execute',
    'behavior_class',
]

BEHAVIOR_OUTCOME_AUX_COLS = [
    'note',
]

BEHAVIOR_OUTCOME_COLS = BEHAVIOR_OUTCOME_KEY_COLS + BEHAVIOR_OUTCOME_AUX_COLS


def note_for_behavior_class(behavior_class: BehaviorClass) -> str:
    mapping = {
        'file_update_bypass': 'file update bypassed',
        'file_update_noninteractive_error': 'file update requires confirmation but stdin is not interactive',
        'file_update_prompt': 'file update prompts with Continue? [y/N]',
        'plan_non_sudo_preview_only': 'planned non-sudo command; preview only',
        'planned_sudo_autoapproved': 'planned sudo auto-approved at step boundary',
        'plan_noninteractive_error': 'plan approval required but stdin is not interactive',
        'plan_prompt': 'Approve this step? [y]es/[a]ll/[s]how/[N]o',
        'loose_non_sudo': 'loose non-sudo command',
        'already_root': 'already root',
        'loose_sudo_autoapproved': 'loose sudo auto-approved',
        'loose_sudo_autoapproved_then_preauth': 'loose sudo auto-approved, then sudo -v preauth',
        'loose_sudo_noninteractive_error': 'loose sudo confirmation required but stdin is not interactive',
        'loose_sudo_prompt': 'Continue? [y]es/[a]ll/[N]o',
        'loose_sudo_prompt_then_preauth': 'Continue? [y]es/[a]ll/[N]o',
    }
    return mapping[behavior_class]


def with_behavior_note(row: Dict[str, Any]) -> Dict[str, Any]:
    new_row = dict(row)
    new_row['note'] = note_for_behavior_class(new_row['behavior_class'])
    return new_row


def file_update_behavior(
    *,
    stdin_tty: bool,
    op_yes: bool,
    manager_yes: bool,
    approve_all_remaining: bool,
) -> Dict[str, Any]:
    bypass = op_yes or manager_yes or approve_all_remaining
    if bypass:
        return {
            'render_plan_preview': False,
            'render_sudo_context': False,
            'prompt_kind': 'none',
            'noninteractive_error': False,
            'proceeds_without_prompt': True,
            'preauthenticate_without_prompt': False,
            'preauthenticate_if_prompt_approved': False,
            'sudo_auth_deferred_to_execute': False,
            'behavior_class': 'file_update_bypass',
        }
    if not stdin_tty:
        return {
            'render_plan_preview': False,
            'render_sudo_context': False,
            'prompt_kind': 'none',
            'noninteractive_error': True,
            'proceeds_without_prompt': False,
            'preauthenticate_without_prompt': False,
            'preauthenticate_if_prompt_approved': False,
            'sudo_auth_deferred_to_execute': False,
            'behavior_class': 'file_update_noninteractive_error',
        }
    return {
        'render_plan_preview': False,
        'render_sudo_context': False,
        'prompt_kind': 'file_update',
        'noninteractive_error': False,
        'proceeds_without_prompt': False,
        'preauthenticate_without_prompt': False,
        'preauthenticate_if_prompt_approved': False,
        'sudo_auth_deferred_to_execute': False,
        'behavior_class': 'file_update_prompt',
    }


def plan_command_behavior(
    *,
    sudo: bool,
    is_root: bool,
    stdin_tty: bool,
    manager_yes: bool,
    yes_sudo: bool,
    approve_all_remaining: bool,
    auth_required: bool,
    effective_role: Role,
    auto_approve_readonly_sudo: bool,
) -> Dict[str, Any]:
    # Current code always renders the plan preview before deciding approval.
    if not sudo:
        return {
            'render_plan_preview': True,
            'render_sudo_context': False,
            'prompt_kind': 'none',
            'noninteractive_error': False,
            'proceeds_without_prompt': True,
            'preauthenticate_without_prompt': False,
            'preauthenticate_if_prompt_approved': False,
            'sudo_auth_deferred_to_execute': False,
            'behavior_class': 'plan_non_sudo_preview_only',
        }

    # _needs_sudo_approval(role)
    needs_approval = True
    if is_root:
        needs_approval = False
    elif manager_yes or yes_sudo or approve_all_remaining:
        needs_approval = False
    elif auth_required:
        needs_approval = True
    elif effective_role == 'read' and auto_approve_readonly_sudo:
        needs_approval = False
    else:
        needs_approval = True

    # Important: plan path does not preauthenticate sudo. Any auth happens later
    # when _execute_one() actually runs sudo / sudo -n.
    if not needs_approval:
        return {
            'render_plan_preview': True,
            'render_sudo_context': False,
            'prompt_kind': 'none',
            'noninteractive_error': False,
            'proceeds_without_prompt': True,
            'preauthenticate_without_prompt': False,
            'preauthenticate_if_prompt_approved': False,
            'sudo_auth_deferred_to_execute': (not is_root),
            'behavior_class': 'planned_sudo_autoapproved',
        }

    if not stdin_tty:
        return {
            'render_plan_preview': True,
            'render_sudo_context': False,
            'prompt_kind': 'none',
            'noninteractive_error': True,
            'proceeds_without_prompt': False,
            'preauthenticate_without_prompt': False,
            'preauthenticate_if_prompt_approved': False,
            'sudo_auth_deferred_to_execute': False,
            'behavior_class': 'plan_noninteractive_error',
        }

    return {
        'render_plan_preview': True,
        'render_sudo_context': False,
        'prompt_kind': 'plan',
        'noninteractive_error': False,
        'proceeds_without_prompt': False,
        'preauthenticate_without_prompt': False,
        'preauthenticate_if_prompt_approved': False,
        'sudo_auth_deferred_to_execute': True,
        'behavior_class': 'plan_prompt',
    }


def loose_command_behavior(
    *,
    sudo: bool,
    is_root: bool,
    stdin_tty: bool,
    op_yes: bool,
    manager_yes: bool,
    yes_sudo: bool,
    approve_all_remaining: bool,
    auth_required: bool,
    effective_role: Role,
    auto_approve_readonly_sudo: bool,
) -> Dict[str, Any]:
    if not sudo:
        return {
            'render_plan_preview': False,
            'render_sudo_context': False,
            'prompt_kind': 'none',
            'noninteractive_error': False,
            'proceeds_without_prompt': True,
            'preauthenticate_without_prompt': False,
            'preauthenticate_if_prompt_approved': False,
            'sudo_auth_deferred_to_execute': False,
            'behavior_class': 'loose_non_sudo',
        }

    if is_root:
        return {
            'render_plan_preview': False,
            'render_sudo_context': False,
            'prompt_kind': 'none',
            'noninteractive_error': False,
            'proceeds_without_prompt': True,
            'preauthenticate_without_prompt': False,
            'preauthenticate_if_prompt_approved': False,
            'sudo_auth_deferred_to_execute': False,
            'behavior_class': 'already_root',
        }

    # confirm_sudo_scope()
    auto_yes = bool(
        op_yes
        or manager_yes
        or yes_sudo
        or approve_all_remaining
        or (
            effective_role == 'read'
            and auto_approve_readonly_sudo
            and not auth_required
        )
    )

    # Current render behavior is asymmetrical:
    # - if auth_required: render sudo context BEFORE checking auto_yes
    # - if not auth_required: render sudo context only after the tty gate,
    #   and only if auto_yes is false
    render_sudo_context = False
    if auth_required:
        render_sudo_context = True
    elif not auto_yes and stdin_tty:
        render_sudo_context = True

    if auto_yes:
        return {
            'render_plan_preview': False,
            'render_sudo_context': render_sudo_context,
            'prompt_kind': 'none',
            'noninteractive_error': False,
            'proceeds_without_prompt': True,
            'preauthenticate_without_prompt': auth_required,
            'preauthenticate_if_prompt_approved': False,
            'sudo_auth_deferred_to_execute': False,
            'behavior_class': (
                'loose_sudo_autoapproved'
                if not auth_required
                else 'loose_sudo_autoapproved_then_preauth'
            ),
        }

    if not stdin_tty:
        return {
            'render_plan_preview': False,
            'render_sudo_context': render_sudo_context,
            'prompt_kind': 'none',
            'noninteractive_error': True,
            'proceeds_without_prompt': False,
            'preauthenticate_without_prompt': False,
            'preauthenticate_if_prompt_approved': False,
            'sudo_auth_deferred_to_execute': False,
            'behavior_class': 'loose_sudo_noninteractive_error',
        }

    return {
        'render_plan_preview': False,
        'render_sudo_context': render_sudo_context,
        'prompt_kind': 'sudo',
        'noninteractive_error': False,
        'proceeds_without_prompt': False,
        'preauthenticate_without_prompt': False,
        'preauthenticate_if_prompt_approved': auth_required,
        'sudo_auth_deferred_to_execute': False,
        'behavior_class': (
            'loose_sudo_prompt'
            if not auth_required
            else 'loose_sudo_prompt_then_preauth'
        ),
    }


def build_behavior_rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    # file_update rows
    for stdin_tty, op_yes, manager_yes, approve_all_remaining in product(
        [False, True], repeat=4
    ):
        pred = {
            'op_kind': 'file_update',
            'queue': 'none',
            'sudo': False,
            'is_root': False,
            'stdin_tty': stdin_tty,
            'op_yes': op_yes,
            'manager_yes': manager_yes,
            'yes_sudo': False,  # irrelevant here; confirm_file_update ignores it
            'approve_all_remaining': approve_all_remaining,
            'auth_required': False,
            'effective_role': 'modify',
            'auto_approve_readonly_sudo': False,
        }
        rows.append(
            pred
            | file_update_behavior(
                stdin_tty=stdin_tty,
                op_yes=op_yes,
                manager_yes=manager_yes,
                approve_all_remaining=approve_all_remaining,
            )
        )

    # plan command rows
    for (
        sudo,
        is_root,
        stdin_tty,
        manager_yes,
        yes_sudo,
        approve_all_remaining,
        auth_required,
        effective_role,
        auto_approve_readonly_sudo,
    ) in product(
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        ['read', 'modify'],
        [False, True],
    ):
        # Canonicalize irrelevant fields for non-sudo plan commands
        if not sudo:
            is_root = False
            manager_yes = False
            yes_sudo = False
            approve_all_remaining = False
            auth_required = False
            effective_role = 'modify'
            auto_approve_readonly_sudo = False

        pred = {
            'op_kind': 'command',
            'queue': 'plan',
            'sudo': sudo,
            'is_root': is_root,
            'stdin_tty': stdin_tty,
            'op_yes': False,  # no per-command yes in plan approval path
            'manager_yes': manager_yes,
            'yes_sudo': yes_sudo,
            'approve_all_remaining': approve_all_remaining,
            'auth_required': auth_required,
            'effective_role': effective_role,
            'auto_approve_readonly_sudo': auto_approve_readonly_sudo,
        }
        rows.append(
            pred
            | plan_command_behavior(
                sudo=sudo,
                is_root=is_root,
                stdin_tty=stdin_tty,
                manager_yes=manager_yes,
                yes_sudo=yes_sudo,
                approve_all_remaining=approve_all_remaining,
                auth_required=auth_required,
                effective_role=effective_role,  # type: ignore[arg-type]
                auto_approve_readonly_sudo=auto_approve_readonly_sudo,
            )
        )

    # loose command rows
    for (
        sudo,
        is_root,
        stdin_tty,
        op_yes,
        manager_yes,
        yes_sudo,
        approve_all_remaining,
        auth_required,
        effective_role,
        auto_approve_readonly_sudo,
    ) in product(
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        [False, True],
        ['read', 'modify'],
        [False, True],
    ):
        if not sudo:
            is_root = False
            op_yes = False
            manager_yes = False
            yes_sudo = False
            approve_all_remaining = False
            auth_required = False
            effective_role = 'modify'
            auto_approve_readonly_sudo = False

        pred = {
            'op_kind': 'command',
            'queue': 'loose',
            'sudo': sudo,
            'is_root': is_root,
            'stdin_tty': stdin_tty,
            'op_yes': op_yes,
            'manager_yes': manager_yes,
            'yes_sudo': yes_sudo,
            'approve_all_remaining': approve_all_remaining,
            'auth_required': auth_required,
            'effective_role': effective_role,
            'auto_approve_readonly_sudo': auto_approve_readonly_sudo,
        }
        rows.append(
            pred
            | loose_command_behavior(
                sudo=sudo,
                is_root=is_root,
                stdin_tty=stdin_tty,
                op_yes=op_yes,
                manager_yes=manager_yes,
                yes_sudo=yes_sudo,
                approve_all_remaining=approve_all_remaining,
                auth_required=auth_required,
                effective_role=effective_role,  # type: ignore[arg-type]
                auto_approve_readonly_sudo=auto_approve_readonly_sudo,
            )
        )

    # De-duplicate rows created by canonicalization.
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for row in rows:
        key = tuple(
            (col, row[col])
            for col in (BEHAVIOR_PREDICATE_COLS + BEHAVIOR_OUTCOME_KEY_COLS)
        )
        if key not in seen:
            seen.add(key)
            deduped.append(row)

    return [with_behavior_note(row) for row in deduped]


behavior_rows: List[Dict[str, Any]] = build_behavior_rows()
behavior_df = pd.DataFrame(behavior_rows)


# ----
# 2) Detailed role-derivation table
# ----
#
# Mirrors _effective_role():
#   explicit spec.role
#   else (sudo and not check) -> read
#   else nearest intent role
#   else modify

ROLE_PREDICATE_COLS = [
    'spec_role',
    'spec_sudo',
    'spec_check',
    'intent_role',
]

ROLE_OUTCOME_COLS = [
    'effective_role',
    'role_source',
]


def infer_effective_role(
    *,
    spec_role: Optional[str],
    spec_sudo: bool,
    spec_check: bool,
    intent_role: Optional[str],
) -> Dict[str, Any]:
    if spec_role is not None:
        eff = 'read' if str(spec_role).strip().lower() == 'read' else 'modify'
        return {'effective_role': eff, 'role_source': 'explicit_spec_role'}
    if spec_sudo and not spec_check:
        return {'effective_role': 'read', 'role_source': 'sudo_and_not_check'}
    if intent_role in {'read', 'modify'}:
        return {'effective_role': intent_role, 'role_source': 'intent_stack'}
    return {'effective_role': 'modify', 'role_source': 'default_modify'}


def build_role_rows() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for spec_role, spec_sudo, spec_check, intent_role in product(
        [None, 'read', 'modify'],
        [False, True],
        [False, True],
        [None, 'read', 'modify'],
    ):
        pred = {
            'spec_role': spec_role,
            'spec_sudo': spec_sudo,
            'spec_check': spec_check,
            'intent_role': intent_role,
        }
        rows.append(
            pred
            | infer_effective_role(
                spec_role=spec_role,
                spec_sudo=spec_sudo,
                spec_check=spec_check,
                intent_role=intent_role,
            )
        )
    return rows


role_rows: List[Dict[str, Any]] = build_role_rows()
role_df = pd.DataFrame(role_rows)


# ----
# 3) Greedy wildcard compression helper
# ----
#
# This is intentionally simple:
# - it only merges rows with identical outcomes
# - it only merges rows that differ in exactly one predicate
# - it repeats until no more merges are possible
#
# It is greedy, so it does not guarantee the globally minimal cube cover,
# but it is good enough to expose "this predicate doesn't matter here".


def _dedupe_rows(
    rows: Iterable[Dict[str, Any]],
    *,
    cols: List[str],
) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for row in rows:
        key = tuple((c, row[c]) for c in cols)
        if key not in seen:
            seen.add(key)
            out.append(dict(row))
    return out


def compress_rows_with_wildcards(
    rows: List[Dict[str, Any]],
    *,
    predicate_cols: List[str],
    outcome_cols: List[str],
    star: str = STAR,
) -> List[Dict[str, Any]]:
    rows = _dedupe_rows(rows, cols=predicate_cols + outcome_cols)

    changed = True
    while changed:
        changed = False
        used = [False] * len(rows)
        merged_rows: List[Dict[str, Any]] = []

        for i, left in enumerate(rows):
            if used[i]:
                continue

            merged = False
            for j in range(i + 1, len(rows)):
                if used[j]:
                    continue
                right = rows[j]

                # outcomes must match exactly
                if any(left[c] != right[c] for c in outcome_cols):
                    continue

                diffs = [c for c in predicate_cols if left[c] != right[c]]
                if len(diffs) != 1:
                    continue

                diff_col = diffs[0]
                if left[diff_col] == star or right[diff_col] == star:
                    continue

                new_row = dict(left)
                new_row[diff_col] = star
                merged_rows.append(new_row)
                used[i] = True
                used[j] = True
                changed = True
                merged = True
                break

            if not merged and not used[i]:
                merged_rows.append(left)

        rows = _dedupe_rows(merged_rows, cols=predicate_cols + outcome_cols)

    return rows


behavior_compact_rows = [
    with_behavior_note(row)
    for row in compress_rows_with_wildcards(
        behavior_rows,
        predicate_cols=BEHAVIOR_PREDICATE_COLS,
        outcome_cols=BEHAVIOR_OUTCOME_KEY_COLS,
    )
]

role_compact_rows = compress_rows_with_wildcards(
    role_rows,
    predicate_cols=ROLE_PREDICATE_COLS,
    outcome_cols=ROLE_OUTCOME_COLS,
)

from pprint import pformat

print(
    f'behavior_compact_rows = {pformat(behavior_compact_rows, sort_dicts=False)}'
)

behavior_compact_df = pd.DataFrame(behavior_compact_rows)
role_compact_df = pd.DataFrame(role_compact_rows)

# Ensure predicates are on the left and outcomes are on the right
behavior_df = behavior_df[BEHAVIOR_PREDICATE_COLS + BEHAVIOR_OUTCOME_COLS]
behavior_compact_df = behavior_compact_df[
    BEHAVIOR_PREDICATE_COLS + BEHAVIOR_OUTCOME_COLS
]

role_df = role_df[ROLE_PREDICATE_COLS + ROLE_OUTCOME_COLS]
role_compact_df = role_compact_df[ROLE_PREDICATE_COLS + ROLE_OUTCOME_COLS]


# ----
# 4) A few useful inspection snippets
# ----

# The row you called out:
loose_readonly_cold = behavior_df.query(
    "op_kind == 'command' and queue == 'loose' and sudo == True "
    "and is_root == False and auth_required == True and effective_role == 'read'"
).sort_values(
    [
        'manager_yes',
        'yes_sudo',
        'approve_all_remaining',
        'auto_approve_readonly_sudo',
        'stdin_tty',
        'op_yes',
    ]
)

# Matching plan-side case:
plan_readonly_cold = behavior_df.query(
    "op_kind == 'command' and queue == 'plan' and sudo == True "
    "and is_root == False and auth_required == True and effective_role == 'read'"
).sort_values(
    [
        'manager_yes',
        'yes_sudo',
        'approve_all_remaining',
        'auto_approve_readonly_sudo',
        'stdin_tty',
    ]
)

# Warm-auth readonly loose sudo:
loose_readonly_warm = behavior_df.query(
    "op_kind == 'command' and queue == 'loose' and sudo == True "
    "and is_root == False and auth_required == False and effective_role == 'read'"
).sort_values(
    [
        'manager_yes',
        'yes_sudo',
        'approve_all_remaining',
        'auto_approve_readonly_sudo',
        'stdin_tty',
        'op_yes',
    ]
)

# --- pretty terminal dumps with pandas formatting + Rich color ---

from rich.console import Console

console = Console()

pd.set_option('display.max_rows', 200)
pd.set_option('display.max_columns', 50)
pd.set_option('display.width', 200)
pd.set_option('display.max_colwidth', 80)


def show_df(
    title, df, *, sort_by=None, columns=None, max_rows=None, style='cyan'
):
    if sort_by:
        df = df.sort_values(sort_by)
    if columns:
        df = df[columns]
    if max_rows is not None:
        df = df.head(max_rows)
    console.rule(title, style='bold yellow')
    console.print(df.to_string(index=False), style=style, markup=False)
    console.print()


# Core columns that are usually worth looking at
main_cols = BEHAVIOR_PREDICATE_COLS + BEHAVIOR_OUTCOME_COLS

show_df(
    f'All behavior rows ({len(behavior_df)})',
    behavior_df,
    sort_by=[
        'op_kind',
        'queue',
        'sudo',
        'is_root',
        'auth_required',
        'effective_role',
        'auto_approve_readonly_sudo',
        'op_yes',
        'manager_yes',
        'yes_sudo',
        'approve_all_remaining',
        'stdin_tty',
    ],
    columns=main_cols,
    style='bright_cyan',
)

show_df(
    f'Compressed behavior rows ({len(behavior_compact_df)})',
    behavior_compact_df,
    sort_by=[
        'op_kind',
        'queue',
        'sudo',
        'is_root',
        'auth_required',
        'effective_role',
        'auto_approve_readonly_sudo',
        'behavior_class',
    ],
    columns=main_cols,
    style='bright_green',
)

show_df(
    f'Role derivation rows ({len(role_df)})',
    role_df,
    sort_by=['spec_role', 'spec_sudo', 'spec_check', 'intent_role'],
    style='magenta',
)

show_df(
    f'Compressed role derivation rows ({len(role_compact_df)})',
    role_compact_df,
    sort_by=['spec_role', 'spec_sudo', 'spec_check', 'intent_role'],
    style='bright_magenta',
)

# --- subsets you said are most interesting ---

show_df(
    'Subset: loose sudo + read + cold auth',
    loose_readonly_cold,
    columns=main_cols,
    style='bold bright_red',
)

show_df(
    'Subset: plan sudo + read + cold auth',
    plan_readonly_cold,
    columns=main_cols,
    style='bold red',
)

show_df(
    'Subset: loose sudo + read + warm auth',
    loose_readonly_warm,
    columns=main_cols,
    style='bold bright_yellow',
)

# Helpful grouped summary
grouped = (
    behavior_df.groupby(
        [
            'queue',
            'sudo',
            'is_root',
            'auth_required',
            'effective_role',
            'behavior_class',
            'prompt_kind',
        ],
        dropna=False,
    )
    .size()
    .reset_index(name='n')
    .sort_values(
        [
            'queue',
            'sudo',
            'is_root',
            'auth_required',
            'effective_role',
            'behavior_class',
            'prompt_kind',
        ]
    )
)

show_df(
    'Grouped summary',
    grouped,
    style='bright_blue',
)
