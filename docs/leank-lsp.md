Leank LSP Technical Reference
=============================

This reference documents the "Leank-flavor LSP" as implemented by `lspleanklib` v0.3.
*Leank LSP* is a subset of both the [standard LSP (Language Server
Protocol)](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/)
and the LSP variant implemented by
[Lake](https://lean-lang.org/doc/reference/latest/Build-Tools-and-Distribution/Lake/).

See the [README.md](../README.md) for background information.


### LSP rootUri

The `initialize` request from an LSP client includes a `rootUri` value.
In Leank-flavored LSP, this is the sole determinant of which Lake workspace directory to use.
In Lake-flavored LSP, the `lake serve` command relies on the current directory or a
CLI parameter and does not use the LSP `rootUri` value.

Leank LSP is a per-Lake-workspace protocol.
It is designed for a single Lake workspace, is not used directly by editors, and is
independent of what an editor considers a workspace.
The `lspleank` program multiplexes one or more Leank LSP sessions into a single unified standard
LSP session, which appears as a single editor workspace to an LSP-compatible editor.


### Connection types

Lspleanklib supports two connection methods for *Leank LSP*:

* subprocess stdio (as does `lake serve`)
* a [UNIX domain socket](https://en.wikipedia.org/wiki/Unix_domain_socket) named
  `lspleank.sock` within a `lean` subdirectory inside the *user runtime directory*
  (see the [Runtime directory reference](#runtime-directory-reference) section).

The `lspleank stdio` and `lspleank lake` commands use subprocess stdio connections,
while the `lspleank connect` command connects to the user runtime directory socket.


Server capabilities (as of Lean 4.29)
-------------------------------------

Excluded
--------

The following Lake server capabilities are excluded and dropped by `lspleanklib` v0.3:

```
experimental
inlayHintProvider
semanticTokensProvider
```

The implementations of these server capabilities in `lake serve` are non-standard and
rely on special editor extension or plugin handling.

Included
--------

The following Lake server capabilities are passed along by `lspleanklib` v0.3:

```
callHierarchyProvider
codeActionProvider
colorProvider
completionProvider
declarationProvider
definitionProvider
documentHighlightProvider
documentSymbolProvider
foldingRangeProvider
hoverProvider
referencesProvider
renameProvider
signatureHelpProvider
textDocumentSync
typeDefinitionProvider
workspaceSymbolProvider
```

Excluded Lake server methods
----------------------------

The following LSP methods are filtered out of Lake LSP and excluded from Leank LSP:
```
$/lean/*
client/registerCapability
client/unregisterCapability
telemetry/*
workspace/applyEdit
workspace/inlayHint/*
workspace/semanticTokens/*
workspace/workspaceFolders
```

Most exclusions are to simplify Leank LSP, and in a few cases, to avoid
non-standard Lake-variant LSP.

Note that a proxy acting as both a Leank server and a Lake client can still make use of special
Lake LSP methods. This is, for example, what `webleank` does. The exclusions apply
only to communication between a Leank LSP client (such as `lspleank`) and a Leank
LSP server (such as `lakelspout`).


Runtime directory reference
---------------------------

Lspleanklib creates a `lean` subdirectory in the *user runtime directory*,
using the `platformdirs` Python package to determine its location, if installed.
Otherwise, Lspleanklib independently determines the location on Linux and macOS.
On Linux, this corresponds to the environment variable `XDG_RUNTIME_DIR`.
On macOS, this corresponds to `~/Library/Caches/TemporaryItems/`.
For more details, consult the [platformdirs
documentation](https://platformdirs.readthedocs.io/en/latest/platforms.html).
