# mcp_gateway

An approval gate for MCP tool calls. The agent inside the sandbox runs with
permissions switched off; every tool call it makes is confirmed here, on the
host, before it reaches the outside world.

## Why not Claude Code's own permissions

They are enforced inside the agent's process, in the same VM the agent
controls. A prompt injection that can write a file and run it can talk to the
MCP gateway directly and never pass through them. They are a guardrail against
mistakes, not a boundary against a compromised agent.

This gate sits outside the VM, on the leg that every MCP call must physically
traverse:

```
agent ──▶ sbx gateway ──▶ sandboxd ──▶ this daemon ──▶ MCP server
 (VM)                    (host)          (host)
```

Docker's own MCP policy engine already evaluates every call locally — you can
see its decisions in `sbx policy log` — but authoring rules for it requires an
organization with AI Governance. This is the missing half.

## Shape

Two processes, one socket between them:

```
        intercepted traffic                control socket (ws)
sbx ─────────────────────────▶ daemon ◀───────────────────────── cli
                                 │                               (or anything
                            decisions.json                        else that
                            audit.jsonl                           speaks JSON)
```

The daemon holds the call and owns the decision. The client only displays it.
That split is the whole point of the arrangement:

- **A held call outlives its audience.** Close the console mid-prompt, or lose
  the connection, and the call stays held. Reattach and it is handed back to
  you in the snapshot.
- **Several clients may attach at once.** Whoever answers first settles it; the
  others are told by a broadcast, so a second console can drop the prompt from
  its screen rather than showing a stale one.
- **No client attached is not an error.** The call simply waits, up to
  `timeout`, and no answer counts as a refusal.

Interception is mitmproxy used as a library, not as a separate `mitmdump`
process — one thing to start, one config, and TLS, ALPN and HTTP framing stay
somebody else's well-tested problem.

## How a sandbox is identified

Nothing in the outgoing request says which sandbox it came from: one daemon
serves them all, there is no per-sandbox header, and the session id is not
carried upstream. So the identity is put into the URL at registration time:

```
sbx mcp add linear-project1 --url https://mcp.linear.app/mcp-project1
```

The daemon reads `project1` back out of the path and rewrites the path to
`/mcp` before forwarding. Decisions are then stored per sandbox, not globally.

Registering a server does not yet hand it to anybody. That is a second move,
and there are two ways to make it depending on whether the sandbox exists:

```
sbx mcp load linear-project1 --sandbox project1                       # into an existing one
sbx run --name project1 --static-mcp linear-project1,github-project1  # at creation
```

`--static-mcp` takes registered server names, comma-separated.

Naming the sandbox after its suffix is a convention, not a requirement: the
daemon never learns the real name — nothing in the request carries it — so the
suffix is simply what the prompt and `decisions.json` will call this sandbox.
What *is* enforced is that the suffix appears in `policy.json`. An unrecognised
one leaves the call unattributable, so the `tools/call` is refused; the rest of
the session is forwarded untouched and 404s upstream on the un-rewritten path.

**This doubles as a canary.** The registered path does not exist upstream, so a
request that reaches the server without passing through the daemon gets a 404
and fails. There is no silent bypass: if the route breaks, MCP stops working.

The cost is worth knowing: sbx reads that 404 as "backend not authorized" and
parks the server until it is kicked with `sbx mcp load <server> --sandbox
<name>`. That is why a *refusal* from this gate is a normal JSON-RPC response
carrying `isError: true` rather than an HTTP error — the agent sees a failed
tool, the connection stays healthy.

## Setup

**1. mitmproxy's CA in the Windows root store.** The daemon uses the same
`~/.mitmproxy` CA that `mitmdump` does, so if you have trusted it before there
is nothing to do. Otherwise let the daemon start once to generate it, then:

```
certutil -addstore -f Root "%USERPROFILE%\.mitmproxy\mitmproxy-ca-cert.cer"
```

**2. Send sandboxd's traffic through it.** sandboxd honours the proxy
environment variables and passes them to the daemon it starts, so this is the
whole of it:

```powershell
$env:HTTPS_PROXY = "http://127.0.0.1:8080"
sbx daemon start -d
```

It is only needed on the shell that starts the daemon. `sbx mcp add` is the CLI
talking to the server itself, and its OAuth flow never touches the suffixed
path, so it works with the gate stopped altogether. `sbx mcp load` reaches
sandboxd over IPC, and sandboxd already has the proxy — which is why that one
*fails* while the gate is down.

It does mean the proxy sees *all* of sandboxd's traffic, not just MCP. Only the
hosts in `policy.json` have their TLS terminated; everything else is tunnelled
through untouched, which is both cheaper and none of the gate's business.

**3. Register servers with a sandbox suffix** and hand them over by either of
the two routes above. Never dynamic mode — there the agent gets `mcp-find` and
`mcp-add` and can attach servers of its own, with no suffix and no decisions
recorded, which defeats all of this.

**4. Copy the examples:**

```
mkdir data
cp examples/config.json data/config.json
cp examples/policy.json data/policy.json
```

`policy.json` is the one place hosts are named: the gate reads it, and it is
also what mitmproxy is told to intercept, so the two cannot drift apart.

## Running

```
uv run -m daemon        # the gate
uv run -m cli           # the console, in another window
```

Python 3.14, pinned in `.python-version`; uv fetches it if the host does not
have it, so there is nothing to install beyond uv itself.

The gate has to be up before sandboxd is, which is a window to keep alive, so
it can also run in the background:

```
uv run -m daemon -d       # start detached, return once it answers
uv run -m daemon --status
uv run -m daemon --stop
```

The daemon can run without the console attached; calls queue up and are handed
over when a console appears. A held call looks like this:

```
  sandbox : project1
  server  : mcp.linear.app
  tool    : save_issue
  args    : {"team": "Development", "title": "..."}
  [y] once  [a] always  [n] deny >
```

`a` records the answer in `decisions.json` and that combination stops asking.
Tools listed in `always_ask` are only ever offered `y`/`n`, because one name
there covers unbounded behaviour.

## Files

All of these live in `data/`, which is gitignored whole:

| file | written by | what it is |
| -- | -- | -- |
| `config.json` | you | ports, paths — the wiring |
| `policy.json` | you | hosts, sandboxes, always_ask, timeout |
| `decisions.json` | the daemon | remembered "always" answers |
| `secret.txt` | the daemon | control-socket token, generated on first run |
| `tokens.json` | you, optional | a bearer token per host, attached on the way out |
| `audit.jsonl` | the daemon | every call held and every decision taken |
| `daemon.pid` | the daemon | whom `--stop` stops, written when started with `-d` |
| `daemon.log` | the daemon | its console output when started with `-d` |

The split is by whether a setting can change under a running process.
`policy.json` is consulted on every call, so it is re-read whenever it changes
— edit it and the next call obeys, no restart. A clock checks it as well as the
traffic, because the list of hosts to intercept is read when a connection
opens: a newly added host would not be intercepted, so no call would arrive to
prompt the re-read that would have allowed it. An edit that does not parse
leaves the last good policy in force, with one line in the log.

`config.json` is the wiring the process bound to at startup: changing a port
means restarting it. So is `tokens.json` — a credential that could change under
a running daemon would be one more moment where it is on disk being read.

## Authenticating without OAuth

`sbx mcp add` authenticates remote servers by OAuth and nothing else, which is
awkward for a server like GitHub's that has no dynamic client registration:
you would have to register an OAuth application, pass `--client-id`, store a
client secret, and accept scopes as coarse as `repo`.

The gate is already terminating TLS for that host and already rewriting the
path, so it can attach the credential instead:

```json
{"api.githubcopilot.com": "github_pat_..."}
```

This is not the same as `sbx secret set`. sbx substitutes stored secrets on
its own egress, but the MCP gateway is served by a different path and does not
go through that substitution — a server registered with a placeholder gets the
placeholder, fails, and is parked. The gate sits further out still, past
everything sbx controls, which is why it can do this at all.

`sbx` then sees a server that never answers 401 and registers it with no OAuth
at all. The token never enters the sandbox and never enters the sbx credential
store — it is on the host, in one file, read once at startup. A fine-grained
personal access token also scopes better than an OAuth app can.

It is attached only to a request that carried a known sandbox suffix. Without
one the gate cannot say whose call it is, and an anonymous caller is not one to
lend a credential to.

Decisions are keyed by **(sandbox, host, tool)**. The host is part of the key
because tool names are not unique across MCP servers: `get_issue` exists in
both Linear and GitHub, and an answer about one must not apply to the other.

## The control socket

`ws://127.0.0.1:8765`, JSON in both directions, one message per frame.
Authenticate with `Authorization: Bearer <contents of secret.txt>`. The message
shapes live in `protocol/__init__.py`, shared by both ends so they cannot
drift; the vocabulary is `StrEnum`, so every name there is also the literal
string on the wire.

The daemon sends `hello` (with the backlog of everything currently held),
`request`, `resolved` (carrying whether it was a client, a timeout, or the
agent giving up), `decisions`, `event` and `error`. A client sends `decide`,
`list_decisions` and `forget`. Writing another client — a tray icon, a phone
notifier — means speaking those nine messages and nothing else.

## What is and is not gated

Only `tools/call`. `initialize`, `tools/list`, notifications and the OAuth
endpoints pass untouched — they carry no side effects, and blocking them would
just break the connection. They are reported on the control socket as
`passthrough` events, so the console can show traffic without holding it.

Unknown tools are refused-by-default in the sense that they are asked about:
there is no allowlist to maintain, `decisions.json` fills itself as you work.

Request bodies are buffered and parsed, because a gate has to read what it
approves; anything larger than 1 MB is refused rather than waved through — as a
JSON-RPC server error (-32000), since nothing was parsed and so nothing shows
the request itself to be malformed.
Responses are never buffered — MCP answers over `text/event-stream` and may
hold a stream open indefinitely.

## Limits

- **Remote (HTTP) MCP servers only.** A local stdio server does not cross the
  network and is invisible here; give those a read-only credential instead.
- **JSON-RPC batches are refused, not inspected.** MCP dropped batching in its
  2025-06-18 revision, so one body is one call. A JSON array arriving anyway is
  refused rather than forwarded: it is the one shape whose `tools/call` could
  otherwise slip past ungated.
- **sandboxd now depends on this daemon being up.** Its traffic all goes
  through the proxy, so if the gate is down, image pulls stop too, not just
  MCP.
- **A token in `tokens.json` is reachable from inside the sandbox.** All of
  sandboxd's traffic goes through the proxy, the sandbox's own included, so an
  agent that speaks to a gated host with a valid sandbox suffix gets the token
  attached. `tools/call` is still held for approval; the other MCP methods are
  not. Give the token no more than the gate is worth.
- **Everything rests on sbx trusting the system certificate store.** If a
  future build pins certificates, interception stops working — loudly, which is
  the point of the canary.

## Tests

```
uv run pytest
```

The end-to-end tests are not mocked: they issue a throwaway CA, run a real
HTTPS server, start the real daemon, and drive it through the real proxy with a
real HTTP client.
