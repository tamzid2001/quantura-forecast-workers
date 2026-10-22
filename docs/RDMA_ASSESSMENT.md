# RDMA applicability — quantura-forecast-workers

- Classification: **Class C**.
- Status: **RDMA NOT APPLICABLE**.
- Workload: quantura forecast workers.
- Assessment: Current workflow is quantura forecast workers; source review found no distributed collective/message-passing execution or demonstrated private-fabric bottleneck. Existing API/provider protocols remain unchanged.
- Environment: **D — macOS lacks DOCA 2.2/Linux/BlueField and native headers**.
- Hardware verification: **NOT RUN — HARDWARE UNAVAILABLE**.

No native DOCA, EFA, RoCE, MPI or NCCL backend is enabled by this change. A Python
loopback model, where present in legacy source, is a software test double, not a
DOCA binding. Its passing tests cannot establish RDMA implementation or throughput.
The shared `quantura-fleet-core` package provides explicit capability gates and a
mutual-TLS TCP artifact transport. TCP does not implement remote reads, writes,
atomics or GPU direct access. Application protocols and scientific semantics remain
unchanged. No chargeable infrastructure was created.

DOCA 2.2 uses the context/work-queue API and is beta. Version 2.2.0 supports
BlueField-2; BlueField-3 requires a deliberate migration to 2.2.1 or later. Native
integration is blocked until exact SDK headers, samples, device/peer capabilities,
build and two-peer tests are available. See the fleet core documentation for the
separate EFA/libfabric and Google Cloud RoCE/Falcon prerequisites.

References: [DOCA RDMA 2.2](https://docs.nvidia.com/doca/archive/doca-v2.2.0/rdma-programming-guide/index.html),
[DOCA Core 2.2](https://docs.nvidia.com/doca/archive/doca-v2.2.0/doca-core-programming-guide/index.html),
[release notes](https://docs.nvidia.com/doca/archive/doca-v2.2.0/release-notes/index.html),
[AWS EFA](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/efa.html),
[Google RDMA profiles](https://docs.cloud.google.com/vpc/docs/rdma-network-profiles).
