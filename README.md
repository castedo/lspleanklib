Lspleanklib
===========

This repository is online at both:

* [gitlab.com](https://gitlab.com/castedo/lspleanklib) for active development
* [github.com](https://github.com/castedo/lspleanklib) as a mirror


For installation instructions and additional information,
visit [lean.castedo.com/lspleank](https://lean.castedo.com/lspleank).


Lspleanklib is a low-level library that includes two CLI utilities:

* `lspleank`: an LSP server to run inside an editor process that also acts as an [Leank LSP](./docs/leank-lsp.md) client
* `lakelspout`: a stdio LSP proxy that adapts Lake LSP to [Leank LSP](./docs/leank-lsp.md)

LSP communication between an LSP-compatible editor, these two utilities, and `lake serve` is:
```
[[ Editor (LSP client) ]]
       ⇵
    standard
      LSP
       ⇵
[[ lspleank (both standard LSP server & Leank LSP client) ]]
       ⇵
   Leank-flavor
      LSP
       ⇵
[[ lakelspout (both Leank LSP server & Lake LSP client) ]]
       ⇵
   Lake-flavor
      LSP
       ⇵
[[ lake serve ]]
```

Lspleanklib also implements functionality for:

* LSP (Language Server Protocol)
* JSON-RPC
* Multiplexing multiple Lake workspace sessions into a single unified editor LSP session
* Connecting (indirectly) to `lake serve` via local UNIX domain sockets
* Running `lake serve` outside the editor process
* Proper reading of stdin with Python asyncio

The high-level application [Webleank](https://gitlab.com/castedo/webleank/) uses `lspleanklib`.


Leank LSP Reference
-------------------

[Leank LSP Reference](docs/leank-lsp.md)


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

Runs as a [Leank LSP](./docs/leank-lsp.md) server.
By default, `lake serve` will be adapted.
The optional command following `--` can be used as an alternative to `lake serve`.


### CLI interaction examples

The following command runs a Lake-flavor LSP server:
```
lake serve
```

The following command runs a [Leank-flavor LSP](./docs/leank-lsp.md) server that is also a Lake LSP client to the previous command:
```
lakelspout stdio -- lake serve
```
which is functionally equivalent to:
```
lakelspout stdio
```

The following command runs a standard LSP server that is a Leank LSP client to the previous command:
```
lspleank stdio -- lakelspout stdio -- lake serve
```
which is functionally equivalent to:
```
lspleank stdio -- lakelspout stdio
```
which is functionally equivalent to:
```
lspleank lake
```
