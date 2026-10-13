# 05. Sandbox — nsjail isolation and the read/write split of `/workspace` and `/ext`

Code execution is one path only: a built-in tool that runs Python / shell inside an nsjail jail on the server. The jail has an empty network namespace (no loopback), a read-only rootfs, a per-user `/workspace` backed by a sparse loopback ext4 with a hard quota, a RAM-backed `/tmp`, and read-only bind mounts under `/ext` that carry the document corpus (`/ext/doc`, `/ext/doc-static`), a shared read-only area (`/ext/public`) and group areas (`/ext/groups/<name>`). Files are exchanged by tools that resolve references, never by the jail reaching the UI. The corpus is always read-only in the jail.

## 1. Why a server-side jail

- An in-browser interpreter (Pyodide) is not used: it is slow, long jobs silently break the session, and the system prompt fills up with workarounds for its limits. Server-side execution allows compound operations ("build it in Python, post-process it in the shell") and keeps the prompt short.
- The host sits on the hospital intranet, so what has to be protected is (1) reachability from user code to other intranet hosts, (2) privilege escalation, (3) reading other users' areas or host files.
- **Cutting the network is the core**: the jail's network namespace is empty, so the problem is removed structurally (not even loopback). The model is told "it does not reach", not "it is forbidden".
- **Thin wall and real wall**: the sandbox container itself is privileged (nsjail creates namespaces / mounts and the loopback device for the quota), but user code never runs in the container namespace, only inside the jail.
- **Isolation between users is the mount namespace**: only the current user's workspace is bound at run time, so other users' areas do not exist inside the jail.

## 2. Inside the jail

| Path | Nature |
|---|---|
| rootfs (`/usr` `/bin` `/opt/venv` …) | read-only bind. The Python venv, shell tools and Japanese fonts are baked into the image; nothing is installed after the host goes air-gapped |
| `/workspace` | per-user sparse loopback ext4, rw bind. Apparent capacity is the quota (default 10 GB); exceeding it yields `No space left on device` at the fs layer |
| `/tmp` | 256 MB tmpfs, cleared per execution |
| `/ext/<name>` | reference data placed by the deployment. Only three states: rw / ro / invisible |

Two execution modes share the same `/workspace`: **stateful** (`run_python` = a resident kernel per conversation; variables and imports survive across runs) and **stateless** (`run_python_once` / `run_shell` = one process per run). Each run has a wall-clock limit and a memory limit, and there is a global concurrency cap so that execution does not compete with inference for CPU.

## 3. Read/write split under `/ext`

Permissions as seen from the jail are **only three states: rw / ro / invisible**; no notion of "group permissions" is introduced, because a permission concept inside the sandbox confuses the model.

| Path | Regular user | Administrator | Contents |
|---|---|---|---|
| `/ext/doc/` | ro | ro | generated output of the document corpus (same paths as the doc tools; leaves are `.md`, directories carry `_index.md`) |
| `/ext/doc-static/` | ro | ro | figures, original PDFs, original CSV / xlsx |
| `/ext/public/` | ro | rw | shared with everyone (recipes etc.) |
| `/ext/groups/<group name>/` | rw for members, **invisible** to others | rw | group share; promotion to public is done by an administrator |

- **Under `/ext` the URL and the sandbox path are the same string.** A path found in the jail can be used verbatim as a tool's `path=`, as `url=`, and as a link in the reply.
- `/ext/doc` holds the substituted generated output, not the source; the tools, the browser and the jail all see the same thing (→ `03_knowledge_layer.md` §7). The source is mounted nowhere.
- The Web frontend (code name nucllei) does not bind `/ext/doc`; it proxies the same path to the document service (code name cage), authenticated GET / HEAD only. A group path returns 404 to non-members, the same as "does not exist".
- The contents of `/ext/public` and `/ext/groups` are **local to the production host**, never put into git or onto the delivery medium: procedures born inside the air gap cannot be returned to git, so they need a place that updates do not wipe.

## 4. Rules for `/workspace`

| Top level | Handling |
|---|---|
| `memory/` `recipes/` | kept: memory across conversations and procedures the user leaves |
| `imports/` | destination of attachments (`send_to_workspace`); auto-deleted after about a month |
| `exports/` | files added or changed here are collected after execution and attached to the chat; deleted after about a month |
| `trash/` | recycle bin: deleting = moving here; deleted after about a month, recoverable until then |
| `<YYYYMMDD>-<task name>/` | one folder per task; outputs, scripts and intermediate files all go here |

- Work happens in the task folder and only deliverables are copied into `exports/`; the three expiring directories never hold anything that must last.
- **No `rm`.** Removal under `/workspace` is a move to `trash/`; shared recipes under `/ext` are moved to `retired/`.
- **Show, then stop.** Before deleting, moving, restoring or writing under `/ext`, the model shows the absolute paths and sizes of the targets (the full text, if writing), asks for confirmation, and ends the turn. "Delete everything" or "no need to confirm" is not consent by itself; consent is agreement, in the next turn, to the list that was shown.
- Patient-specific values (names, patient IDs, dates, lab values) are never placed in any shared layer.

## 5. File exchange

The jail never accesses the UI (trust flows one way, UI → sandbox); the bridge is held by tools. `send_to_workspace(file_id=|url=)` brings an attachment or a URL resource into `/workspace/imports/` (for `/ext/…`, already visible, only the path is returned). `fetch_from_workspace(path=)` turns a file under `/workspace/…` into an attachment (for `/ext/…` only a link is returned, since the same string is the URL). The diff of `exports/` is collected automatically after each run and attached, returning a `file_id`. Collection has a per-file size limit because files travel as base64 inside JSON.

## 6. Documents are read-only

- `/ext/doc` and `/ext/doc-static` are passed to the jail only via `--bindmount_ro`.
- Documents are visible in the jail so that corpus-wide aggregation can be done in one go (→ `04_doc_tools.md` §5). Checking and quoting individual content is done with the doc tools, never from the raw files. The model is told that this is enforced **by mechanism**: the raw files carry front matter, and quotations and figure URLs can only be obtained from tool return values (a bare prohibition without a reason gets broken).
- Every user sees the same thing, so only data every user may read is placed there; that judgement belongs to the deployment.

## 7. Recipes (procedures left by users)

Procedures users create are called **recipes** (the word "skill" is reserved for system skills): one folder with a `RECIPE.md`, living in three layers (personal `/workspace/recipes/`, group `/ext/groups/<name>/recipes/`, site-wide `/ext/public/recipes/`) and listed with `grep` rather than an index file, which multiple writers would conflict on. The default save location is personal; a recipe uses only tools available to everyone who can see it.
