"""Per-cluster indexes the classifier reads: nodes by name, pods by node (pure; ADR-0002)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field

from ttv.models import ClusterNode, ClusterPod, KubeSnapshot, ThanosSnapshot


def clean_hostname(raw: object) -> str | None:
    """
    Reduce a hostname the way server-scan does before storing `reported_name` (D10).

    Copied from server-scan `backend/app/infrastructure/openshift/records.py@defe172`.

    Args:
        raw (object): A hostname in any shape.

    Returns:
        str | None: Lowercased, stripped, without its DNS domain; None when empty.
    """
    if not isinstance(raw, str):
        return None
    host = raw.strip().lower().split(".", 1)[0]
    return host or None


def holder_ref(pod: ClusterPod) -> str:
    """
    The logical workload holding a pod's GPUs: its top owner, or the pod itself when bare.

    Args:
        pod (ClusterPod): The pod.

    Returns:
        str: `namespace/Kind/name`.
    """
    if pod.owners:
        top = pod.owners[-1]
        return f"{pod.namespace}/{top.kind}/{top.name}"
    return f"{pod.namespace}/Pod/{pod.name}"


@dataclass(frozen=True)
class ClusterView:
    """
    What the classifier knows about one observed target cluster in this scan.

    Attributes:
        name (str): Cluster name.
        kube (KubeSnapshot | None): Latest good kube read (may be from an earlier scan).
        thanos (ThanosSnapshot | None): Latest good Thanos read.
        unreachable (bool): A source of this cluster failed beyond `unknown_grace_scans`.
        reservation_namespaces (frozenset[str]): Namespaces of Run:ai reservation pods.
        nodes_by_clean (Mapping[str, tuple[ClusterNode, ...]]): Nodes by cleaned name.
        pods_by_node (Mapping[str, tuple[ClusterPod, ...]]): GPU-relevant pods by node name.
        uuids_by_clean (Mapping[str, frozenset[str]]): DCGM GPU UUIDs by cleaned node label.
        xid_by_clean (Mapping[str, Mapping[str, float]]): Non-zero last XID per UUID, by node.
        gpus_by_holder (Mapping[str, int]): Whole GPUs each logical workload holds cluster-wide.
    """

    name: str
    kube: KubeSnapshot | None
    thanos: ThanosSnapshot | None
    unreachable: bool = False
    reservation_namespaces: frozenset[str] = frozenset()
    nodes_by_clean: Mapping[str, tuple[ClusterNode, ...]] = field(default_factory=dict)
    pods_by_node: Mapping[str, tuple[ClusterPod, ...]] = field(default_factory=dict)
    uuids_by_clean: Mapping[str, frozenset[str]] = field(default_factory=dict)
    xid_by_clean: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    gpus_by_holder: Mapping[str, int] = field(default_factory=dict)

    def gpu_uuids(self, node: ClusterNode) -> frozenset[str]:
        """
        DCGM GPU UUIDs reporting for a node; `R` is the size of the set.

        Args:
            node (ClusterNode): The node.

        Returns:
            frozenset[str]: UUIDs (empty when DCGM reports nothing for it).
        """
        return self.uuids_by_clean.get(clean_hostname(node.name) or "", frozenset())


def build_cluster_view(
    name: str,
    kube: KubeSnapshot | None,
    thanos: ThanosSnapshot | None,
    *,
    unreachable: bool = False,
    reservation_namespaces: frozenset[str] = frozenset(),
) -> ClusterView:
    """
    Index one cluster's snapshots for the classifier.

    Args:
        name (str): Cluster name.
        kube (KubeSnapshot | None): Latest good kube read.
        thanos (ThanosSnapshot | None): Latest good Thanos read.
        unreachable (bool): A source failed beyond grace.
        reservation_namespaces (frozenset[str]): Run:ai reservation namespaces.

    Returns:
        ClusterView: The indexed view.
    """
    nodes: dict[str, list[ClusterNode]] = defaultdict(list)
    pods: dict[str, list[ClusterPod]] = defaultdict(list)
    held: dict[str, int] = defaultdict(int)
    for node in kube.nodes if kube else ():
        nodes[clean_hostname(node.name) or node.name].append(node)
    for pod in kube.pods if kube else ():
        pods[pod.node_name].append(pod)
        if pod.gpus and pod.namespace not in reservation_namespaces:
            held[holder_ref(pod)] += pod.gpus

    uuids: dict[str, set[str]] = defaultdict(set)
    xids: dict[str, dict[str, float]] = defaultdict(dict)
    for label, values in (thanos.gpu_uuids_by_node if thanos else {}).items():
        uuids[clean_hostname(label) or label].update(values)
    for label, values in (thanos.last_xid_by_node if thanos else {}).items():
        xids[clean_hostname(label) or label].update(values)

    return ClusterView(
        name=name,
        kube=kube,
        thanos=thanos,
        unreachable=unreachable,
        reservation_namespaces=reservation_namespaces,
        nodes_by_clean={k: tuple(v) for k, v in nodes.items()},
        pods_by_node={k: tuple(v) for k, v in pods.items()},
        uuids_by_clean={k: frozenset(v) for k, v in uuids.items()},
        xid_by_clean={k: dict(v) for k, v in xids.items()},
        gpus_by_holder=dict(held),
    )
