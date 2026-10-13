# 06. Updates into the air gap — four kinds plus a manifest, delivered on media

After installation the host cannot reach the Internet, so updates travel on physical media. One delivery is one directory plus one manifest, holding four kinds of content: `images` (container images, one file per image), `codes` (a git bundle of the deployment repo), `deps` (an offline wheelhouse plus the package manager binary), and `docs` (a tarball of the document corpus, which never travels any other way because it contains in-hospital documents). The manifest is the only evidence that the four kinds belong to the same release; the loader refuses a directory without one. The inference environment (SGLang + torch + CUDA) is a fifth, named-only kind applied during planned downtime, and model weights are outside the mechanism altogether. Where a network path exists, images come from a registry and code from the git origin; the medium path is the one that always works.

## 1. Constraints

What the air gap forbids: `docker build`, `pip` / `uv install`, arbitrary egress. So images are built outside and carried in, code is carried via git, Python dependencies as wheels, documents as a tar.

Two routes: **if the network reaches**, images via `docker compose pull` from a registry and code via `git fetch` from origin; **if it does not**, the four kinds are bundled into one directory and carried on USB / SSD. **Document data always goes on media** — it contains in-hospital documents and original PDFs, so it can go neither into git nor into an image.

```
          [registry]                   [git origin]
               │ pull                       │ fetch
               ▼                            ▼
          [air-gapped GPU host] ◄── load ── [medium] ◄── save ── [release machine]
```

## 2. Four kinds plus a manifest

| Kind | Contents | Form on the medium | Usual route |
|---|---|---|---|
| `images` | container images referenced by compose | **1 image = 1 file** (`docker save`) | `docker compose pull` |
| `codes` | deployment repo (compose / scripts / material injected into the Web frontend) | `git bundle` | `git fetch` |
| `deps` | Python dependencies of the document service (code name cage) + the package manager binary | offline wheelhouse (with sha256) + binaries | `uv sync` on the host |
| `docs` | document corpus (sources + figures / originals + ingestion masters) | tar (complete snapshot) | **none** |
| `root-deps` (named only) | lock set of the inference environment (SGLang + torch + CUDA) | wheelhouse (about 4 GB) | `uv sync` on the host |
| model weights | — | **out of scope**: planned downtime + physical media to replace `models/` | — |

**One delivery = one directory + one manifest.** Image tags, the git commit, the document version and the wheels' Python only make sense as one set; carried as loose files they arrive with mismatched versions. The manifest is therefore the sole evidence that the four kinds belong to the same delivery, the loader refuses a directory without one, and images deliberately not carried are recorded so that an intended omission can be told from a missing item.

Design choices:

- **One file per image**: packing them into one archive shrinks only 1% through layer sharing (the base images differ). Kept separate, they can be thinned right before carrying, and a broken transfer means re-copying one file. By default only the images that changed in the release are carried.
- **Uncompressed tar + `.sha256`**: the contents are already-compressed layers, wheels and bundles, so gzip gains only 1%.
- **The inference environment is not a default kind**: it is heavy, and swapping torch / CUDA under a running SGLang breaks unpredictably, so it is applied by name only during a planned downtime.
- **No version tag for documents**: what is included is declared by each corpus's `_index.md` (version, retrieval date); which delivery it came in is held by the manifest. Extraction is a **replacement**, not an overlay, so deleted files propagate and rolling back to an older snapshot both work naturally.

## 3. Versioning

All version pins live in `.env`, so staging and production can run different versions without branching compose. The Web frontend and the sandbox are built by one CI workflow from the same commit and pinned by one variable, so there is no path where only one of them ships, and rollback is as a pair. Production pins date tags (only development machines and staging may follow `latest`); code is pinned by commit hash. Fetching and applying are separate, explicit operations.

## 4. pip inside the air gap: a wheelhouse behind an approval gate

A transparent PyPI proxy is **not** used (a new outbound, no pin, typosquats flowing in). The wheelhouse is built outside exactly as the lock says, collected in the same bare OS container with the same Python as the deployment target so that wheel selection and marker evaluation match. Inside the air gap the lock is applied offline with hashes required at both ends, so only files whose hash matches the lock are used. Packages that ship only as sdist are carried as sdist together with their build backend's wheel, because a wheel built on the medium would not match the lock's hash.

## 5. No content scanning step

**No kind has a step that scans the byte stream** (no ClamAV gate or similar). Scanning is treated as unnecessary, because everything coming in is **only what people wrote** — images are only the references written in compose, code is our own repo, deps follow the lock, docs are documents we made ourselves. There is no path by which something unknown enters on its own. This reasoning no longer holds if a pull-through proxy is inserted, a third-party registry is added to compose, or a medium received from outside is loaded.
