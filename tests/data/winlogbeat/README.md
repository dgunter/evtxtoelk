# Winlogbeat fidelity fixtures

Pairs of `<name>.evtx.gz` (the raw event log) and `<name>.golden.json.gz`
(the documents Winlogbeat's module produced from it) taken from the Beats
repository at tag `v9.5.3`, directories
`x-pack/winlogbeat/module/<module>/test/testdata/{collection,ingest}/`.
They are gzip-compressed here; the tests inflate them on the fly.

`tests/test_ecs_golden.py` parses each `.evtx` with python-evtx, maps the
records with `evtxtoelk.ecs.to_ecs`, and compares them with the golden
documents field by field, excluding only the fields an offline `.evtx` cannot
supply (the rendered `message`, manifest-resolved keyword/opcode/task names,
SID resolution, agent and ingest metadata). Beats is licensed under the Elastic
License 2.0 / SSPL; these files are test data and are not distributed in the
package.
