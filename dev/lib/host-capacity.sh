#!/usr/bin/env bash
# host-capacity.sh — the single definition site for "can this host actually hold a Kind cluster".
#
# WHY THIS IS A LIBRARY AND NOT A COMMENT IN up.sh
#   Both resources below were discovered the same way: a cluster refused to work, the error named
#   something else, and hours went into the wrong layer. Each fix was then written into whichever
#   script happened to be in hand, which is how the memory check briefly existed in two files with
#   two different floors. `invariants-gate.py check_cluster_creating_scripts_assert_host_capacity`
#   fails any script that runs `kind create cluster` without calling assert_host_capacity, so there
#   is one floor, one message, and one place to add the third resource when it bites (V-MET-013).
#
# THE POINT OF MEASURING TWO THINGS AND NAMING THE REST
#   A preflight grown one incident at a time only ever measures the PREVIOUS incident. The memory
#   check was written after LSN-026; the very next new cluster failed on inotify while the memory
#   check printed 5758Mi of headroom — a green line that is not neutral, because it actively sends
#   you to look at size when size is fine. That is LSN-027. Hence the closing note: this says what
#   it checked AND what it did not, so nobody reads two green numbers as "the host is fine".
#
# Usage (source it):
#   . "$(dirname "$0")/../lib/host-capacity.sh"
#   assert_host_capacity            # prints its findings; exits 2 if the host cannot hold a cluster

HOST_NEED_FREE_MI="${HOST_NEED_FREE_MI:-2560}"
HOST_NEED_INOTIFY="${HOST_NEED_INOTIFY:-512}"
HOST_PROBE_IMAGE="${HOST_PROBE_IMAGE:-${KIND_IMAGE:-kindest/node:v1.31.2}}"
ALLOW_TIGHT_MEMORY="${ALLOW_TIGHT_MEMORY:-0}"

# _check_memory — two agent pods (~2.7Gi requested each) plus a control plane. The floor is
# deliberately below the sum of requests: Kind nodes report the whole VM as allocatable, so the
# scheduler places the pods regardless, and what kills the host is real usage rather than requests.
_check_memory() {
  local total_b total_mi used_mi free_mi
  total_b="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
  total_mi=$((total_b / 1048576))
  used_mi="$(docker stats --no-stream --format '{{.MemUsage}}' 2>/dev/null |
    awk -F'/' '{gsub(/[^0-9.KMGi]/,"",$1); u=$1;
                if (u ~ /GiB|G/) {gsub(/[^0-9.]/,"",u); s+=u*1024}
                else if (u ~ /MiB|M/) {gsub(/[^0-9.]/,"",u); s+=u}
                else {gsub(/[^0-9.]/,"",u); s+=u/1024}}
         END {printf "%d", s}')"
  free_mi=$((total_mi - ${used_mi:-0}))
  echo "   memory: ${total_mi}Mi total · ${used_mi:-0}Mi in use by containers · ${free_mi}Mi headroom (want >= ${HOST_NEED_FREE_MI}Mi)"
  [ "$free_mi" -ge "$HOST_NEED_FREE_MI" ] && return 0
  [ "$ALLOW_TIGHT_MEMORY" = "1" ] && { echo "   ALLOW_TIGHT_MEMORY=1 — proceeding anyway." >&2; return 0; }
  cat >&2 <<EOF

REFUSING: ${free_mi}Mi of headroom, want >= ${HOST_NEED_FREE_MI}Mi.

  This is how LSN-026 happened: under memory pressure kube-scheduler and kube-controller-manager
  crash-looped, fixture pods never left Pending, and three unrelated security properties reported
  FAIL when nothing was wrong with any of them.

  Free memory:
      kind get clusters      # anything here that is not kube-agents-dev is stale — delete it
  or raise the VM (Colima):
      colima stop && colima start --cpu 6 --memory 12
  or override deliberately:
      ALLOW_TIGHT_MEMORY=1 \$0
EOF
  exit 2
}

# _check_inotify — THE RESOURCE THAT ACTUALLY RAN OUT the one time the memory check said green.
# Every Kind node's containerd opens fsnotify watchers; past the instance ceiling (default 128) the
# CRI plugin never registers. What makes it worth a preflight rather than a comment is how it
# surfaces: kubelet cannot reach a CRI that never came up, so kubeadm fails at `wait-control-plane`
# with an HTTP timeout on /healthz — an error whose entire vocabulary is slow-and-small, which sends
# you to memory, where the line above has already certified everything is fine.
_check_inotify() {
  local limit
  limit="$(docker run --rm --entrypoint cat "$HOST_PROBE_IMAGE" \
    /proc/sys/fs/inotify/max_user_instances 2>/dev/null | tr -dc '0-9')"
  if [ -z "$limit" ]; then
    echo "   inotify: could not read max_user_instances — proceeding, but if the control plane times"
    echo "   out at wait-control-plane, check the node's containerd log for 'too many open files'."
    return 0
  fi
  echo "   inotify: max_user_instances=$limit (want >= $HOST_NEED_INOTIFY)"
  [ "$limit" -ge "$HOST_NEED_INOTIFY" ] && return 0
  cat >&2 <<EOF

REFUSING: fs.inotify.max_user_instances is $limit, want >= $HOST_NEED_INOTIFY.

  Each Kind node consumes inotify instances and containerd's CRI plugin will not start without one.
  The error you would get is NOT about inotify -- kubeadm reports 'wait-control-plane' timing out on
  /healthz, because kubelet cannot reach a CRI that never came up. That is a diagnosis-eater, so
  this refuses instead of letting you have it.

  Raise it in the Docker VM (Colima):
      colima ssh -- sudo sysctl -w fs.inotify.max_user_instances=$HOST_NEED_INOTIFY
  That lasts until the VM restarts -- a host that passed yesterday can refuse today. To persist,
  add to ~/.colima/default/colima.yaml:
      provision:
        - mode: system
          script: sysctl -w fs.inotify.max_user_instances=$HOST_NEED_INOTIFY
EOF
  exit 2
}

assert_host_capacity() {
  echo "== host preflight =="
  _check_memory
  _check_inotify
  # Say what this is and is not evidence about. Two green numbers read as "the host is fine", and
  # that reading is what cost an hour looking at memory while inotify was the limit (LSN-027).
  echo "   NOT checked: inotify watches, PIDs, file descriptors, disk. If the control plane still"
  echo "   times out at wait-control-plane, re-create with 'kind create --retain' and read the"
  echo "   node's containerd log — the resource that ran out is named there and nowhere further up."
}
