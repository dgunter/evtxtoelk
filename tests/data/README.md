# Test fixtures

All `.evtx` files here are the public samples shipped with
[python-evtx](https://github.com/williballenthin/python-evtx/tree/master/tests/data)
and are used unchanged (MD5s match upstream).

| File | Records | Origin |
| --- | --- | --- |
| `system.evtx` | 1601 | [plaso test data](https://github.com/log2timeline/plaso/tree/1e2fa282efa2f839e1f179a3e98dbf922b5dbbc7/test_data) |
| `security.evtx` | 2261 | Carlos Dias, contributed to python-evtx |
| `issue_38.evtx` | 1 | python-evtx issue #38 |
| `dns_log_malformed.evtx` | 1 readable, 4 corrupt | python-evtx issue #37 |
