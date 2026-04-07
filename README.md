Lspleanklib
===========

This repository is online at both:

* [gitlab.com](https://gitlab.com/castedo/lspleanklib) for active development
* [github.com](https://github.com/castedo/lspleanklib) as a mirror


Lspleanklib is a low-level library that implements functionality used by
[Webleank](https://gitlab.com/castedo/webleank/), including:

* LSP (Language Server Protocol)
* JSON-RPC
* Multiplexing multiple `lake serve` workspace sessions into a single unified editor LSP session
* Connecting to `lake serve` via local UNIX domain sockets
* Running `lake serve` outside the editor process
* Proper reading of stdin with Python asyncio

There are no required dependencies on Linux and macOS. However, Windows requires the Python
package `platformdirs` to be installed. The pip install package specification
`lspleanklib[crossplatform]` will add `platformdirs` as a requirement.

This package also includes two CLI utilities for advanced usage:
* `lspleank`: an LSP server used by Webleank to run inside an editor process
* `lakelspout`: a stdio LSP proxy that adapts Lake LSP to Leank LSP

For more information, visit [Webleank](https://gitlab.com/castedo/webleank) and
[lean.castedo.com](https://lean.castedo.com).


CLI Reference
-------------

The `lspleank` and `lakelspout` programs provide low-level functionality.
For reference information on the high-level program `webleank`,
see [the Webleank README](https://gitlab.com/castedo/webleank).


### Lspleank

Lspleank is an LSP server. It runs as a subprocess, communicating with an editor via
stdio.

```
$ lspleank -h
usage: lspleank  [-h] [--version] {connect,lake,stdio} [-- external_command ...]

Stdio LSP server multiplexing one or more Lake LSP servers.

positional arguments:
  {connect,lake,stdio}
    connect             connect to an lspleank socket service after starting it with the external command
    lake                internally use lakelspout to run Leank LSP servers
    stdio               use the external command to run stdio Leank LSP servers

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
```

#### Subcommand `connect`

Runs as a Leank client (and LSP server), connecting to a Leank server via an *lspleank
socket*. The command following `--` is used to start a Leank server if needed.

#### Subcommand `lake`

Runs as an LSP client to `lake serve` (and as an LSP server to the editor).
The command following `--` may be used as an alternative to `lake serve`.
The command
```
lspleank lake
```
is functionally equivalent to
```
lspleank stdio -- lakelspout stdio
```

#### Subcommand `stdio`

Runs as a Leank client (and LSP server) and executes the command following `--`
as a stdio Leank server.


### Lakelspout

Lakelspout is a Leank service/server.
Editors use Leank clients such as `lspleank` or `webleank` to connect to `lakelspout`
processes.

```
$ lakelspout -h
usage: lakelspout  [-h] [--version] {stdio} [-- lake_serve_command ...]

Adapt a Lake LSP server as a Leank service/server.

positional arguments:
  {stdio}
    stdio               run as a stdio Leank LSP server

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
```

#### Subcommand `stdio`

Runs as a Leank LSP server.
By default, `lake serve` will be adapted.
The optional command following `--` can be used as an alternative to `lake serve`.


Leank LSP Reference
-------------------

Lspleanklib implements *Leank LSP*, a subset of standard LSP.
It serves as a simplified intermediary LSP between:

* the standard LSP expected by any LSP-compatible editor, and
* the non-standard LSP variant implemented by `lake serve`.

Lspleanklib supports two possible connection methods for *Leank LSP*:

* subprocess stdio (like `lake serve`), or
* a [UNIX domain socket](https://en.wikipedia.org/wiki/Unix_domain_socket) named
  `lspleank.sock` within a `lean` subdirectory inside the *user runtime directory*
  (see the [Runtime directory reference](#runtime-directory-reference) section).

The `lspleank stdio` and `lspleank lake` commands make subprocess stdio connections,
while the `lspleank connect` command connects to the user runtime directory socket.

A Leank LSP session is per Lake workspace and does not support "workspace" as defined by an
editor. The program `lspleank` multiplexes Leank LSP
sessions into a single unified standard LSP session, which appears as a single workspace to an
LSP-compatible editor.


Runtime directory reference
---------------------------

Lspleanklib creates a `lean` subdirectory in the *user runtime directory*,
using the `platformdirs` Python package to determine its location, if installed.
Otherwise, Lspleanklib independently determines the location on Linux and macOS.
On Linux, this corresponds to the environment variable `XDG_RUNTIME_DIR`.
On macOS, this corresponds to `~/Library/Caches/TemporaryItems/`.
For more details, consult the [platformdirs
documentation](https://platformdirs.readthedocs.io/en/latest/platforms.html).
