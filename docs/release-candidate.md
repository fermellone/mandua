# Mandu'a 0.1.0 — release candidate

This candidate delivers the deterministic Git-backed memory proof of concept: fourteen public
commands, explicit preview/apply writes, a versioned evidence result, a reproducible local demo,
and an eighteen-case conformance map. It is not a production service or a published package.

## Delivery checks

From a source checkout, use `make check`, `make demo`, `make tutorial-check`,
`make conformance-check`, and `make build`. The wheel is built from the source distribution,
so the packaged demo must also work outside the source checkout:

```console
uv run --no-project --isolated --with /absolute/path/to/mandua_memory-0.1.0-py3-none-any.whl mandua demo --format json
```

Use a physical installation/cache path. The demo intentionally rejects symbolic links in its
resource-directory ancestry; on macOS, `/private/tmp` is the physical spelling of `/tmp`.
The demo uses local Git transport, retains its report and bundle, and reports whether any network
access was observed. Package installation/build dependencies may need an initial download.

Final artifact hashes and the exact source revision belong in the accompanying release manifest,
generated after integration. A build made before the final source changes is not the delivery
artifact even if it has the same package version.

## Review and platform scope

The hardening work covers anomalous checkpoint reconciliation, isolated Git execution, concurrent
publication, interruption recovery, and bounded cleanup. The detailed evidence is in
[hardening-status.md](hardening-status.md). A new independent review completed, found three additional defects, and approved their
corrections after re-review and six independently executed regression tests. The earlier
service-interrupted attempt is not counted as approval. See the concurrency scope in the
hardening record: writes through old descriptors after the final check are outside the guarantee.

The reviewed source's complete 1,124-test suite passed on macOS with Python 3.13.7,
including clean installation and packaged-demo checks. The release manifest records the final
wheel's separate Python 3.11 and 3.13 installation/demo results. Linux and macOS with Python 3.11 are configured in CI;
Linux ARM64 was subsequently validated in Docker with Python 3.11.16 and Git 2.50.1:
all 1,124 tests and both source and installed-wheel demos passed, without network access.
One test fixture required `HEAD` capitalization on the case-sensitive filesystem; runtime code
was unchanged. Linux x86_64 subsequently passed the same 1,124 tests and both demos using
Docker `linux/amd64` emulation with the same Python and Git versions; no additional fixes
were needed. Git 2.39.5 did not pass the complete suite. External Linux CI and native x86_64
execution were unverified at that stage. Emulated timings are not native performance measurements.
Windows is outside the validated scope.

### GitHub validation — 2026-09-20

Commit `75c7e06` passed the complete 1,131-test suite, local demo, and seven tutorial tests
on both native Ubuntu x86_64 and macOS ARM64, using Python 3.11 and Git 2.55.0.
See [the successful CI run](https://github.com/fermellone/mandua/actions/runs/35500833794).
This closes the previously pending external-CI and native-x86_64 compatibility checks.

The current candidate includes an optional experimental [Jev adapter](../adapters/jev/README.md).
Its mock mode is local; configuring `TYPESAFE_API_KEY` enables external TypeSafe API calls.
The deterministic core and main demo remain independent of that adapter. Mock tests do not
establish live provider compatibility or production readiness.

## Publication boundary

Preparing artifacts and integrating local source do not publish them. A canonical public
repository, package-registry destination, tag, and public release require the operator's separate
publication decision. No registry or public repository URL is implied by these instructions.
