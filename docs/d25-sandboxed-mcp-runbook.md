# D25 sandboxed MCP runbook

This runbook uses the copyable v3 example at
[`docs/examples/d25-sandboxed-mcp.example.json`](examples/d25-sandboxed-mcp.example.json).
It is for the pinned local Docker image recorded in
`tests/fixtures/d25_mcp_server/provenance.json`:
`sha256:adcd84ab9f9dc91e5c3eebe9fa32329545f73ab0b891ecd6def4232740cc4300`.
The top-level sandbox runner uses the separately pinned D8 Python test image
`sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a`;
the two image identities are intentionally not interchangeable.

## Before running

From `v2/`, make the package importable and ensure Docker is running:

```powershell
$env:PYTHONPATH = 'src'
python -B -m koawa_agent_v2.runtime.cli doctor --config docs/examples/d25-sandboxed-mcp.example.json
```

`doctor` is read-only. It checks the repository, database path, provider-key
presence, and Docker/image readiness. The example names the provider key with
`KOAWA_PROVIDER_KEY`; its value must be supplied by the operator's environment,
never copied into JSON. A missing provider key is expected to make that check
fail, but is not a reason to add a secret to the MCP environment.

## Run

The provider-backed CLI run is:

```powershell
python -B -m koawa_agent_v2.runtime.cli run --config docs/examples/d25-sandboxed-mcp.example.json --task 'Exercise the configured filesystem MCP read-only tool, then report the result.'
```

The MCP server is created only after the normal activation ticket is consumed.
Its container contract is fixed by trusted runtime code: exact image digest,
`network=none`, read-only rootfs, non-root `65532:65532`, `cap-drop ALL`,
`no-new-privileges`, bounded CPU/memory/PIDs/tmpfs, zero host mounts, and an
empty environment. A tag or a host executable path is not a substitute.

## Cleanup and recovery

Normal shutdown stops and removes the exact allocation-owned container as part
of endpoint termination. Do not use a broad `docker rm` or remove containers by
name alone. For a read-only audit of any managed containers, use:

```powershell
docker container ls --all --filter 'label=io.koawa.v2.managed=true'
docker container ls --all --filter 'label=koawa.managed=mcp-sandbox'
```

If a run is interrupted, preserve the reported allocation/container identity
and let the allocation reaper reconcile it; an object whose labels or inspect
identity cannot be proved is reported, not deleted. Do not manually delete an
uncertain object or switch the MCP to `host_trusted` as a fallback.
