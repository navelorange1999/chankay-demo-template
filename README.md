# Chankay demo template

Use **Use this template** to create an independent, trusted demo repository. The included rotation study is a dependency-free example. Edit the three files in `site/` or replace `scripts/build-demo.sh` with your project's build and verification steps. Each demo builds in its own repository.

## Connect a project

1. Choose an available, lowercase, hyphen-separated slug. The starter uses the repository name by default. Register the same slug in the site's Payload **Demos** collection. Its published record and gateway setting control when the public URL is served.
2. In the demo repository, create the GitHub Actions environment **demo-release**. Restrict deployment branches to the release branch. Protect this branch and review changes to workflows, scripts and dependency locks before merging. Grant environment administration and release-workflow editing only to trusted maintainers.
3. Set environment variable `R2_ENDPOINT_URL` to the R2 account's HTTPS S3 endpoint. Add `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` as environment secrets. Provision Object Read & Write access scoped to the **chankay-demo-assets** bucket. Only the publisher step receives this credential. Its R2 grant covers that entire bucket, including other projects and shell objects. Every repository trusted with this grant is a bucket-level publisher. Use a separately controlled broker or isolated bucket if maintainers require storage-enforced project isolation.
4. For a different slug, configure environment variable `DEMO_SLUG` and pass the same `slug` input in the caller workflow. For a different release branch, set `DEMO_RELEASE_BRANCH`, update the caller's push trigger, and restrict the GitHub environment to that branch. Defaults are the repository name and its default branch. The provided push trigger selects `main`. The optional `locale` input accepts `en` (default) and `zh-CN`.
5. Keep the reusable workflow pinned to a reviewed full commit SHA. The caller grants `contents: read` and `actions: read`. Protect this workflow and preserve its filename when migrating an existing demo so the GitHub workflow run number continues to increase. A push to the selected branch or a manual run on it publishes. A pull request only builds.

## Static output

The build runs with Node.js 22, Python 3 and the variables `DEMO_SLUG`, `DEMO_RELEASE` (full commit SHA) and `DEMO_LOCALE`. Write ordinary static files to `dist/`. Include `index.html`, a head marker `<meta name="chankay-shell" content="v1">`, and body containers with `data-shell-slot="header"` and `data-shell-slot="footer"` in that order. Keep application mounts outside these containers. The gateway adds shared shell content on request. Each HTML page has its own shell markers.

Use `/<slug>/_r/<sha>/` for demo asset URLs. The publisher resolves HTML references to scripts, stylesheets, icons, preloads and media, plus manifest icons, and verifies their release prefix and membership in the artifact. Canonical and navigation links can point to public pages. The example builds canonical and asset URLs from validated inputs.

Run the starter locally from a Git checkout:

```sh
DEMO_SLUG=rotation-study DEMO_RELEASE="$(git rev-parse HEAD)" bash scripts/build-demo.sh
python3 -m unittest discover -s tests -v
```

The release format supports up to 256 files, 8 MiB per file, 24 MiB total and a 64 KiB inventory. Supported extensions are defined in `publisher/release.py`. A supplied `release.json` must match the independently generated locale, path, size, SHA-256 and content type of every file. Verified legacy inventories retain their original bytes.

## Release protocol

The first GitHub-hosted job checks out the demo source, runs its build and uploads `dist/`. The publisher runs on a fresh runner, checks out the central workflow's own repository and commit, verifies the artifact's repository, workflow run, commit, size and digest, then reads files directly from the ZIP as data. It validates file paths, sizes, MIME types and shell markup before accessing R2.

Each asset is conditionally created in `projects/<slug>/releases/<sha>/` and read back. Existing bytes and content types must match. The verified inventory is written last. The publisher then conditionally updates `projects/<slug>/current.json` using its previous ETag or a create-only precondition. Its `run` watermark rejects stale promotions. Failed partial uploads can be retried, and releases with unchanged bytes are idempotent. To roll back, revert the app change and publish the resulting reviewed commit in a later run.

A green build proves that the app produced a GitHub artifact. A successful publish step verifies its immutable R2 files and current pointer. After the Payload route is enabled, check the release header and the rendered shell and app at `https://demos.chankay.com/<slug>/`.

The release uses the hosted runner's AWS CLI and GitHub CLI. [Cloudflare R2 conditional write semantics](https://developers.cloudflare.com/r2/api/s3/conditional-operations/) and [GitHub reusable workflow identity](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#job-context) document the storage and checkout guarantees.
