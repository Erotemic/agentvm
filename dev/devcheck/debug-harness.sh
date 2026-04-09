#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_NAME="debug-harness.sh"
SCRIPT_VERSION="20260409T000012"

VM_NAME="${VM_NAME:-aivm-2404}"
TOKEN="${TOKEN:-/mnt/aivm-persistent/hostcode-geowatch-5f1a05ef}"
TOKEN_NAME="$(basename "$TOKEN")"
PERSIST_ROOT="${PERSIST_ROOT:-/var/lib/libvirt/aivm/${VM_NAME}/persistent-root}"
HOST_TOKEN_PATH="${HOST_TOKEN_PATH:-${PERSIST_ROOT}/${TOKEN_NAME}}"
GUEST_SSH="${GUEST_SSH:-ssh agent@10.77.0.195}"

RUN_EXPENSIVE="${RUN_EXPENSIVE:-0}"
RUN_STRACE="${RUN_STRACE:-0}"
WATCH_INTERVAL="${WATCH_INTERVAL:-0.05}"
FD_SAMPLE_COUNT="${FD_SAMPLE_COUNT:-200}"
FD_HISTO_SAMPLES="${FD_HISTO_SAMPLES:-400}"
HOT_PID_STRACE_SECS="${HOT_PID_STRACE_SECS:-1}"

STAMP="$(date +%Y%m%dT%H%M%S)"
OUTDIR="${OUTDIR:-aivm-report-${VM_NAME}-${STAMP}}"
RAW="${OUTDIR}/raw"
HOST="${RAW}/host"
GUEST="${RAW}/guest"

mkdir -p "$HOST" "$GUEST"

CURRENT_MD=""
FD_SAMPLER_PID=""
QEMU_PID=""
HOT_VFS_PID=""
HOT_VFS_COUNT=""
VFS_PIDS=()

log() {
    printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2
}

begin_md() {
    CURRENT_MD="$1"
    : > "$CURRENT_MD"
}

md_heading() {
    printf '# %s\n\n' "$*" >> "$CURRENT_MD"
}

md_note() {
    printf '> %s\n\n' "$*" >> "$CURRENT_MD"
}

md_cmd() {
    local label="$1"
    shift
    {
        printf '### %s\n\n' "$label"
        printf '```text\n'
        printf '$'
        printf ' %q' "$@"
        printf '\n'
        "$@" 2>&1 || true
        printf '```\n\n'
    } >> "$CURRENT_MD"
}

md_sh() {
    local label="$1"
    local cmd="$2"
    {
        printf '### %s\n\n' "$label"
        printf '```text\n'
        printf '$ %s\n' "$cmd"
        bash -lc "$cmd" 2>&1 || true
        printf '```\n\n'
    } >> "$CURRENT_MD"
}

append_file() {
    local report="$1"
    local title="$2"
    local file="$3"
    if [[ -f "$file" ]]; then
        {
            printf '\n## %s\n\n' "$title"
            printf '```text\n'
            cat "$file"
            printf '\n```\n'
        } >> "$report"
    fi
}

append_tail() {
    local report="$1"
    local title="$2"
    local file="$3"
    local n="${4:-80}"
    if [[ -f "$file" ]]; then
        {
            printf '\n## %s\n\n' "$title"
            printf '```text\n'
            tail -n "$n" "$file"
            printf '\n```\n'
        } >> "$report"
    fi
}

select_vfs_pids() {
    pgrep -a virtiofsd | awk -v pat="$PERSIST_ROOT" '$0 ~ pat {print $1}'
}

select_qemu_pid() {
    pgrep -f "qemu-system.*${VM_NAME}" | head -n1 || true
}

fd_count_for_pid() {
    local pid="$1"
    if [[ -d "/proc/$pid/fd" ]]; then
        sudo ls "/proc/$pid/fd" 2>/dev/null | wc -l | tr -d ' '
    else
        echo "NA"
    fi
}

refresh_hot_pid() {
    local pid
    local count
    HOT_VFS_PID=""
    HOT_VFS_COUNT=""
    mapfile -t VFS_PIDS < <(select_vfs_pids)
    QEMU_PID="$(select_qemu_pid)"
    for pid in "${VFS_PIDS[@]}"; do
        count="$(fd_count_for_pid "$pid")"
        if [[ "$count" =~ ^[0-9]+$ ]]; then
            if [[ -z "$HOT_VFS_COUNT" || "$count" -gt "$HOT_VFS_COUNT" ]]; then
                HOT_VFS_PID="$pid"
                HOT_VFS_COUNT="$count"
            fi
        fi
    done
}

copy_script_metadata() {
    local dest="$RAW/debug-harness-used.sh"
    cp "$0" "$dest" 2>/dev/null || cp "$SCRIPT_NAME" "$dest" 2>/dev/null || true
    if [[ -f "$dest" ]]; then
        sha256sum "$dest" > "$RAW/debug-harness-used.sha256" 2>/dev/null || true
    fi
}

remote_md_common() {
    cat <<'EOS'
set +e
export TOKEN TOKEN_NAME RUN_EXPENSIVE RUN_STRACE

cmd() {
    local label="$1"
    shift
    printf '### %s\n\n' "$label"
    printf '```text\n'
    printf '$'
    printf ' %q' "$@"
    printf '\n'
    "$@" 2>&1 || true
    printf '```\n\n'
}

shcmd() {
    local label="$1"
    local script="$2"
    printf '### %s\n\n' "$label"
    printf '```text\n'
    printf '$ %s\n' "$script"
    bash -lc "$script" 2>&1 || true
    printf '```\n\n'
}
EOS
}

remote_snapshot_body() {
    cat <<'EOS'
printf '# guest snapshot\n\n'
printf '> Baseline guest state around /mnt/aivm-persistent and the suspected token subtree.\n\n'
cmd 'date' date -Is
cmd 'findmnt for persistent root' findmnt -T /mnt/aivm-persistent -o TARGET,SOURCE,FSTYPE,OPTIONS
shcmd 'mount entries mentioning aivm-persistent' 'mount | grep aivm-persistent || true'
shcmd 'guest open-file limits' 'printf "soft=%s\nhard=%s\n" "$(ulimit -Sn)" "$(ulimit -Hn)"'
cmd 'guest file-nr' cat /proc/sys/fs/file-nr
cmd 'root listing' ls -ld /mnt/aivm-persistent
shcmd 'top-level entries' 'ls /mnt/aivm-persistent 2>&1 | head -n 200'
cmd 'token mount lookup' findmnt -T "$TOKEN" -o TARGET,SOURCE,FSTYPE,OPTIONS
cmd 'token path metadata' ls -ld "$TOKEN"
shcmd 'mount entries mentioning token' 'mount | grep -F "$TOKEN_NAME" || true'
shcmd 'token readlink' 'readlink -f "$TOKEN" || true'
EOS
}

remote_repro_body() {
    cat <<'EOS'
printf '# guest reproducer\n\n'
printf '> Focused checks on the suspected reproducer: the token subtree.\n\n'
cmd 'date' date -Is
cmd 'findmnt for persistent root' findmnt -T /mnt/aivm-persistent -o TARGET,SOURCE,FSTYPE,OPTIONS
cmd 'findmnt for token' findmnt -T "$TOKEN" -o TARGET,SOURCE,FSTYPE,OPTIONS
cmd 'token path metadata' ls -ld "$TOKEN"
shcmd 'token contents head' 'ls "$TOKEN" 2>&1 | head -n 50'
shcmd 'find token shallow' 'find "$TOKEN" -maxdepth 2 -mindepth 1 2>&1 | head -n 120'
shcmd 'stat token' 'stat "$TOKEN"'
shcmd 'stat token .git' 'stat "$TOKEN/.git"'
shcmd 'python os.listdir smoke test' 'python - <<"PY"
import os
p = os.environ["TOKEN"]
try:
    for i, name in enumerate(os.listdir(p)):
        print(i, name)
        if i >= 20:
            break
except Exception as ex:
    print(repr(ex))
PY'
shcmd 'python os.walk smoke test' 'python - <<"PY"
import os
root = os.environ["TOKEN"]
try:
    for i, (d, subdirs, files) in enumerate(os.walk(root)):
        print(f"dir={d!r} subdirs={len(subdirs)} files={len(files)}")
        if i >= 20:
            break
except Exception as ex:
    print(repr(ex))
PY'
shcmd 'ripgrep serial traversal' 'rg --threads 1 --files "$TOKEN" >/tmp/rg-serial.out 2>/tmp/rg-serial.err; rc=$?; echo rc=$rc; tail -n 50 /tmp/rg-serial.err'
shcmd 'ripgrep default traversal' 'rg --files "$TOKEN" >/tmp/rg-default.out 2>/tmp/rg-default.err; rc=$?; echo rc=$rc; tail -n 50 /tmp/rg-default.err'
shcmd 'find traversal' 'find "$TOKEN" -type f >/tmp/find.out 2>/tmp/find.err; rc=$?; echo rc=$rc; tail -n 50 /tmp/find.err'
if [[ "$RUN_STRACE" == "1" ]]; then
    shcmd 'strace on ripgrep default traversal' 'strace -f -e trace=openat,openat2,statx -o /tmp/rg.strace rg --files "$TOKEN" >/tmp/rg-default.out 2>/tmp/rg-default.err; rc=$?; echo rc=$rc; tail -n 120 /tmp/rg.strace'
fi
if [[ "$RUN_EXPENSIVE" == "1" ]]; then
    shcmd 'EXPENSIVE: lsof on persistent root' 'lsof +D /mnt/aivm-persistent 2>&1 | tee /tmp/aivm-persistent-lsof.txt'
else
    printf '> Skipping lsof +D /mnt/aivm-persistent (set RUN_EXPENSIVE=1 to enable).\n\n'
fi
shcmd 'guest kernel log tail' 'sudo dmesg -T | tail -n 120'
shcmd 'guest journalctl kernel tail' 'sudo journalctl -k -b --no-pager | tail -n 120'
EOS
}

run_remote_script() {
    local outfile="$1"
    local script_body="$2"
    local rc=0
    {
        printf '$ %s bash -s --\n' "$GUEST_SSH"
        bash -lc "$GUEST_SSH bash -s --" 2>&1 <<EOF_REMOTE || rc=$?
export TOKEN=$(printf '%q' "$TOKEN")
export TOKEN_NAME=$(printf '%q' "$TOKEN_NAME")
export RUN_EXPENSIVE=$(printf '%q' "$RUN_EXPENSIVE")
export RUN_STRACE=$(printf '%q' "$RUN_STRACE")
$(remote_md_common)
${script_body}
EOF_REMOTE
        printf '\nremote_rc=%s\n' "$rc"
    } > "$outfile"
    return 0
}

collect_guest_snapshot() {
    run_remote_script "$GUEST/guest-snapshot.md" "$(remote_snapshot_body)"
}

collect_guest_repro() {
    run_remote_script "$GUEST/guest-repro.md" "$(remote_repro_body)"
}

sample_fd_targets() {
    local pid="$1"
    local count="$2"
    local outfile="$3"
    local idx
    local last
    if [[ -z "$pid" || -z "$count" || ! "$count" =~ ^[0-9]+$ || "$count" -le 0 ]]; then
        printf 'no hot virtiofsd pid selected\n' > "$outfile"
        return 0
    fi
    last=$((count - 1))
    {
        for idx in 0 1 2 3 4 5 10 100 1000 10000 100000 200000 500000 900000 "$last"; do
            if [[ "$idx" =~ ^[0-9]+$ && "$idx" -ge 0 && "$idx" -lt "$count" ]]; then
                printf 'fd=%s\t' "$idx"
                sudo readlink "/proc/$pid/fd/$idx" 2>&1 || true
            fi
        done
    } > "$outfile"
}

sample_fdinfo() {
    local pid="$1"
    local count="$2"
    local outfile="$3"
    local idx
    local last
    if [[ -z "$pid" || -z "$count" || ! "$count" =~ ^[0-9]+$ || "$count" -le 0 ]]; then
        printf 'no hot virtiofsd pid selected
' > "$outfile"
        return 0
    fi
    last=$((count - 1))
    {
        for idx in 0 1 2 3 4 5 10 100 1000 10000 100000 200000 500000 900000 "$last"; do
            if [[ "$idx" =~ ^[0-9]+$ && "$idx" -ge 0 && "$idx" -lt "$count" ]]; then
                if sudo test -e "/proc/$pid/fdinfo/$idx"; then
                    printf '===== fdinfo %s =====
' "$idx"
                    sudo sed -n '1,20p' "/proc/$pid/fdinfo/$idx" 2>&1 || true
                fi
            fi
        done
    } > "$outfile"
}


sample_fd_histogram() {
    local pid="$1"
    local count="$2"
    local outfile="$3"
    local samples="$4"
    if [[ -z "$pid" || -z "$count" || ! "$count" =~ ^[0-9]+$ || "$count" -le 0 ]]; then
        printf 'no hot virtiofsd pid selected
' > "$outfile"
        return 0
    fi
    sudo python3 - "$pid" "$count" "$PERSIST_ROOT" "$samples" <<'PY' > "$outfile"
import os, sys, collections
pid = sys.argv[1]
count = int(sys.argv[2])
persist_root = sys.argv[3]
samples = int(sys.argv[4])
if count <= 0:
    print('no hot virtiofsd pid selected')
    raise SystemExit(0)
step = max(1, count // max(1, samples))
hits = collections.Counter()
examples = {}
scanned = 0
missing = 0
for fd in range(0, count, step):
    path = f'/proc/{pid}/fd/{fd}'
    try:
        target = os.readlink(path)
    except OSError:
        missing += 1
        continue
    scanned += 1
    label = target
    if target.startswith(persist_root + '/'):
        rel = target[len(persist_root) + 1:]
        label = rel.split('/', 1)[0] if rel else '.'
    elif target.startswith('socket:'):
        label = 'socket:'
    elif target.startswith('anon_inode:'):
        label = 'anon_inode:'
    elif target.startswith('pipe:'):
        label = 'pipe:'
    elif target.startswith('/'):
        parts = [p for p in target.split('/') if p]
        label = '/'.join(parts[:2]) if len(parts) >= 2 else target
    else:
        label = target.split(':', 1)[0]
    hits[label] += 1
    examples.setdefault(label, target)
print(f'scanned={scanned}')
print(f'missing={missing}')
for key, val in hits.most_common(40):
    print(f'{val}	{key}	{examples.get(key, "")}')
PY
}


sample_fd_listing() {
    local pid="$1"
    local count="$2"
    local outfile="$3"
    local samples="$4"
    if [[ -z "$pid" || -z "$count" || ! "$count" =~ ^[0-9]+$ || "$count" -le 0 ]]; then
        printf 'no hot virtiofsd pid selected
' > "$outfile"
        return 0
    fi
    sudo python3 - "$pid" "$count" "$samples" <<'PY' > "$outfile"
import os, sys
pid = sys.argv[1]
count = int(sys.argv[2])
samples = int(sys.argv[3])
step = max(1, count // max(1, samples))
printed = 0
for fd in range(0, count, step):
    path = f'/proc/{pid}/fd/{fd}'
    try:
        target = os.readlink(path)
    except OSError:
        continue
    print(f'{fd}	{target}')
    printed += 1
print(f'printed={printed}')
PY
}



sample_hot_pid_mount_ids() {
    if [[ -z "${HOT_VFS_PID:-}" ]]; then
        printf 'no hot virtiofsd pid selected
' > "$HOST/hot-pid-mountids.txt"
        return 0
    fi
    python3 - "$HOST/hot-pid-fdinfo.txt" > "$HOST/hot-pid-mountids.txt" <<'PY'
import collections, pathlib, re, sys
path = pathlib.Path(sys.argv[1])
counts = collections.Counter()
for line in path.read_text().splitlines():
    m = re.match(r'^mnt_id:\s*(\d+)$', line)
    if m:
        counts[m.group(1)] += 1
if not counts:
    print('sampled_mount_ids=0')
else:
    print(f'sampled_mount_ids={sum(counts.values())}')
    for mid, count in counts.most_common():
        print(f'{count}	mnt_id={mid}')
PY
}

trace_hot_pid_file_activity() {
    if [[ -z "${HOT_VFS_PID:-}" ]]; then
        printf 'no hot virtiofsd pid selected
' > "$HOST/hot-pid-strace.txt"
        return 0
    fi
    if ! command -v strace >/dev/null 2>&1; then
        printf 'strace not installed
' > "$HOST/hot-pid-strace.txt"
        return 0
    fi
    sudo timeout "$HOT_PID_STRACE_SECS" strace -ff -tt -e trace=openat,openat2,statx,close -p "$HOT_VFS_PID" -o "$HOST/hot-pid-strace" >/dev/null 2>&1 || true
    {
        ls -1 "$HOST"/hot-pid-strace* 2>/dev/null | sort | while read -r f; do
            printf '===== %s =====
' "$(basename "$f")"
            sed -n '1,120p' "$f"
            printf '
'
        done
    } > "$HOST/hot-pid-strace.txt"
}

collect_host_snapshot() {
    begin_md "$HOST/host-snapshot.md"
    md_heading "host snapshot"
    md_note "Host-side state for persistent-root, the suspected token subtree, and the relevant virtiofs/qemu processes."

    md_cmd "date" date -Is
    md_sh  "virtiofsd pgrep" 'pgrep -a virtiofsd || true'
    md_sh  "persistent-root virtiofsd candidates" "pgrep -a virtiofsd | grep -F '$PERSIST_ROOT' || true"
    md_sh  "qemu pgrep for VM" "pgrep -a -f 'qemu-system.*${VM_NAME}' || true"
    md_sh  "findmnt under persistent-root" "sudo findmnt -R '$PERSIST_ROOT' -o TARGET,SOURCE,FSTYPE,OPTIONS || true"
    md_cmd "host token path metadata" sudo ls -ld "$HOST_TOKEN_PATH"
    md_sh  "mount entries mentioning token" "sudo mount | grep -F '$TOKEN_NAME' || true"
    md_sh  "mount entries mentioning persistent-root" "sudo mount | grep -F '$PERSIST_ROOT' || true"
    md_sh  "persistent-root top-level entries" "sudo find '$PERSIST_ROOT' -maxdepth 1 -mindepth 1 -printf '%M %u %g %TY-%Tm-%Td %TT %p\n' | sort || true"
    md_sh  "host df -ih for persistent-root" "df -ih '$PERSIST_ROOT' '$HOST_TOKEN_PATH' || true"

    refresh_hot_pid

    md_sh "persistent-root virtiofsd fd counts" "for pid in ${VFS_PIDS[*]:-}; do count=\$(sudo ls /proc/\$pid/fd 2>/dev/null | wc -l | tr -d ' '); echo PID=\$pid count=\$count; done"
    if [[ -n "$HOT_VFS_PID" ]]; then
        echo "HOT_VFS_PID=$HOT_VFS_PID" > "$HOST/hot-pid.txt"
        echo "HOT_VFS_COUNT=$HOT_VFS_COUNT" >> "$HOST/hot-pid.txt"
        sample_fd_targets "$HOT_VFS_PID" "$HOT_VFS_COUNT" "$HOST/hot-pid-fd-targets.txt"
        sample_fdinfo "$HOT_VFS_PID" "$HOT_VFS_COUNT" "$HOST/hot-pid-fdinfo.txt"
        sample_fd_histogram "$HOT_VFS_PID" "$HOT_VFS_COUNT" "$HOST/hot-pid-histogram.txt" "$FD_HISTO_SAMPLES"
        sample_fd_listing "$HOT_VFS_PID" "$HOT_VFS_COUNT" "$HOST/hot-pid-sampled-listing.txt" "$FD_SAMPLE_COUNT"
        sample_hot_pid_mount_ids
        trace_hot_pid_file_activity
        md_sh "hot persistent-root virtiofsd proc status" "sudo sed -n '1,120p' /proc/$HOT_VFS_PID/status || true"
        md_sh "hot persistent-root virtiofsd limits" "sudo sed -n '1,200p' /proc/$HOT_VFS_PID/limits || true"
        md_sh "hot persistent-root virtiofsd cwd/exe/root" "sudo readlink /proc/$HOT_VFS_PID/cwd; sudo readlink /proc/$HOT_VFS_PID/exe; sudo readlink /proc/$HOT_VFS_PID/root"
        md_sh "hot persistent-root virtiofsd fd targets spot-check" "cat '$HOST/hot-pid-fd-targets.txt'"
        md_sh "hot persistent-root virtiofsd fdinfo spot-check" "sed -n '1,240p' '$HOST/hot-pid-fdinfo.txt'"
        md_sh "hot persistent-root sampled path prefix histogram" "cat '$HOST/hot-pid-histogram.txt'"
        md_sh "hot persistent-root sampled fd listing" "sed -n '1,240p' '$HOST/hot-pid-sampled-listing.txt'"
        if [[ "$RUN_EXPENSIVE" == "1" ]]; then
            md_sh "EXPENSIVE: lsof hot persistent-root virtiofsd" "sudo lsof -p '$HOT_VFS_PID' 2>&1 | head -n 300"
        else
            md_note "Skipping lsof -p for hot virtiofsd (set RUN_EXPENSIVE=1 to enable)."
        fi
    fi

    if [[ "$RUN_EXPENSIVE" == "1" ]]; then
        md_sh "EXPENSIVE: lsof on persistent-root" "sudo lsof +D '$PERSIST_ROOT' 2>&1 | tee '$HOST/aivm-persistent-host-lsof.txt'"
    else
        md_note "Skipping host lsof +D (set RUN_EXPENSIVE=1 to enable)."
    fi

    md_sh "libvirt journal tail" 'sudo journalctl -b -u libvirtd -u virtqemud --no-pager | tail -n 200'
    md_sh "host dmesg tail" 'sudo dmesg -T | tail -n 120'
}

start_fd_sampler() {
    local outfile="$HOST/fd-samples.tsv"
    refresh_hot_pid

    {
        printf 'PERSIST_ROOT=%s\n' "$PERSIST_ROOT"
        printf 'VIRTIOFSD_PIDS=%s\n' "${VFS_PIDS[*]:-}"
        printf 'QEMU_PID=%s\n' "${QEMU_PID:-}"
        printf 'HOT_VFS_PID=%s\n' "${HOT_VFS_PID:-}"
        printf 'HOT_VFS_COUNT=%s\n' "${HOT_VFS_COUNT:-}"
    } > "$HOST/pid-selection.txt"

    if (( ${#VFS_PIDS[@]} == 0 )) && [[ -z "${QEMU_PID:-}" ]]; then
        printf 'no matching virtiofsd/qemu pids found\n' > "$outfile"
        return 0
    fi

    rm -f "$OUTDIR/.stop-fd-sampling"

    (
        printf 'ts\tvirtiofsd_total\tvirtiofsd_detail\tqemu\n'
        while [[ ! -e "$OUTDIR/.stop-fd-sampling" ]]; do
            local_total=0
            details=()
            for pid in "${VFS_PIDS[@]}"; do
                count="$(fd_count_for_pid "$pid")"
                details+=("${pid}:${count}")
                if [[ "$count" =~ ^[0-9]+$ ]]; then
                    local_total=$((local_total + count))
                fi
            done
            qcount="NA"
            if [[ -n "${QEMU_PID:-}" ]]; then
                qcount="$(fd_count_for_pid "$QEMU_PID")"
            fi
            printf '%s\t%s\t%s\t%s\n' \
                "$(date +%Y-%m-%dT%H:%M:%S.%3N%z)" \
                "$local_total" \
                "$(IFS=,; echo "${details[*]:-}")" \
                "$qcount"
            sleep "$WATCH_INTERVAL"
        done
    ) > "$outfile" &
    FD_SAMPLER_PID="$!"
}

stop_fd_sampler() {
    if [[ -n "${FD_SAMPLER_PID:-}" ]]; then
        : > "$OUTDIR/.stop-fd-sampling"
        wait "$FD_SAMPLER_PID" || true
        rm -f "$OUTDIR/.stop-fd-sampling"
        FD_SAMPLER_PID=""
    fi
}

render_report() {
    local report="$OUTDIR/report.md"
    local host_token_exists="no"
    local guest_token_visible="unknown"
    local rg_serial_rc="NA"
    local rg_default_rc="NA"
    local find_rc="NA"

    refresh_hot_pid
    [[ -e "$HOST_TOKEN_PATH" ]] && host_token_exists="yes"
    grep -qF "$TOKEN" "$GUEST/guest-snapshot.md" 2>/dev/null && guest_token_visible="yes" || true
    rg_serial_rc="$(grep -m1 -o 'rc=[0-9]\+' "$GUEST/guest-repro.md" 2>/dev/null | cut -d= -f2 || true)"
    rg_default_rc="$(grep -o 'rc=[0-9]\+' "$GUEST/guest-repro.md" 2>/dev/null | sed -n '2p' | cut -d= -f2 || true)"
    find_rc="$(grep -o 'rc=[0-9]\+' "$GUEST/guest-repro.md" 2>/dev/null | sed -n '3p' | cut -d= -f2 || true)"
    [[ -n "$rg_serial_rc" ]] || rg_serial_rc="NA"
    [[ -n "$rg_default_rc" ]] || rg_default_rc="NA"
    [[ -n "$find_rc" ]] || find_rc="NA"

    {
        printf '# aivm attachment debug report\n\n'
        printf 'Generated: `%s`\n\n' "$(date -Is)"
        printf 'VM_NAME: `%s`\n\n' "$VM_NAME"
        printf 'TOKEN: `%s`\n\n' "$TOKEN"
        printf 'PERSIST_ROOT: `%s`\n\n' "$PERSIST_ROOT"
        printf 'GUEST_SSH: `%s`\n\n' "$GUEST_SSH"
        printf 'SCRIPT_VERSION: `%s`\n\n' "$SCRIPT_VERSION"
        if [[ -f "$RAW/debug-harness-used.sha256" ]]; then
            printf 'SCRIPT_SHA256: `%s`\n\n' "$(awk '{print $1}' "$RAW/debug-harness-used.sha256")"
        fi

        printf '## Quick findings\n\n'
        printf -- '- host_token_exists: `%s`\n' "$host_token_exists"
        printf -- '- guest_token_visible: `%s`\n' "$guest_token_visible"
        printf -- '- selected_virtiofsd_pids: `%s`\n' "${VFS_PIDS[*]:-(none)}"
        printf -- '- selected_qemu_pid: `%s`\n' "${QEMU_PID:-none}"
        printf -- '- hot_virtiofsd_pid: `%s`\n' "${HOT_VFS_PID:-none}"
        printf -- '- hot_virtiofsd_fd_count: `%s`\n' "${HOT_VFS_COUNT:-none}"
        printf -- '- rg_serial_rc: `%s`\n' "$rg_serial_rc"
        printf -- '- rg_default_rc: `%s`\n' "$rg_default_rc"
        printf -- '- find_rc: `%s`\n' "$find_rc"
        printf '\n'

        printf '## Notes\n\n'
        printf -- '- FD sampling targets only `virtiofsd` processes whose command line includes `%s`.\n' "$PERSIST_ROOT"
        printf -- '- The guest probe now exports `TOKEN` and `TOKEN_NAME` into the remote environment before nested `bash -lc` calls.\n'
        printf -- '- `tar.gz` is the only archive format emitted by this script.\n'
        printf '\n'
    } > "$report"

    append_file "$report" "Harness script used" "$RAW/debug-harness-used.sh"
    append_file "$report" "Harness script sha256" "$RAW/debug-harness-used.sha256"
    append_file "$report" "Host snapshot" "$HOST/host-snapshot.md"
    append_file "$report" "Guest snapshot" "$GUEST/guest-snapshot.md"
    append_file "$report" "Guest reproducer" "$GUEST/guest-repro.md"
    append_file "$report" "PID selection" "$HOST/pid-selection.txt"
    append_tail "$report" "Host FD samples (tail)" "$HOST/fd-samples.tsv" 80
    append_file "$report" "Hot virtiofsd FD targets" "$HOST/hot-pid-fd-targets.txt"
    append_file "$report" "Hot virtiofsd fdinfo" "$HOST/hot-pid-fdinfo.txt"
    append_file "$report" "Hot virtiofsd sampled path histogram" "$HOST/hot-pid-histogram.txt"
    append_file "$report" "Hot virtiofsd sampled FD listing" "$HOST/hot-pid-sampled-listing.txt"
    append_file "$report" "Hot virtiofsd sampled mount IDs" "$HOST/hot-pid-mountids.txt"
    append_file "$report" "Hot virtiofsd short strace" "$HOST/hot-pid-strace.txt"
}

pack_bundle() {
    tar -czf "${OUTDIR}.tar.gz" "$OUTDIR"
}

cleanup() {
    stop_fd_sampler || true
}
trap cleanup EXIT

main() {
    copy_script_metadata
    log "collecting host snapshot"
    collect_host_snapshot
    log "collecting guest snapshot via ${GUEST_SSH}"
    collect_guest_snapshot
    log "sampling host FDs during guest repro"
    start_fd_sampler
    collect_guest_repro
    stop_fd_sampler
    copy_script_metadata
    log "rendering report"
    render_report
    log "packing bundle"
    pack_bundle
    log "done"
    printf '\n%s complete\nreport dir: %s\nbundle: %s.tar.gz\n' "$SCRIPT_NAME" "$OUTDIR" "$OUTDIR"
}

main "$@"
