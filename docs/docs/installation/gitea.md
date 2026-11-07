---
title: "Gitea Integration"
sidebar_position: 8
---

## Run a Gitea webhook server

1. In Gitea create a new user and give it write access to the intended repositories, either as a collaborator or through an organization team. See [Gitea repository permissions](https://docs.gitea.com/usage/access-control/permissions/).

2. For the user from step 1. generate a personal access token under **User Settings → Applications**, with `write:repository` and `write:issue` scopes for reading pull requests and publishing descriptions, reviews, comments, and labels. See [Gitea API token permissions](https://docs.gitea.com/1.26/development/api-usage/#generating-and-listing-api-tokens).

3. Generate a random secret for your app, and save it for later (`webhook_secret`). For example, you can use:

    ```bash
    WEBHOOK_SECRET=$(python -c "import secrets; print(secrets.token_hex(10))")
    ```

    The webhook secret is required: if `GITEA.WEBHOOK_SECRET` is not configured, the server rejects every incoming webhook with HTTP 403.

4. Clone this repository:

    ```bash
    git clone https://github.com/xaker00/pr-agent.git
    cd pr-agent
    ```

5. Prepare variables and secrets. Skip this step if you plan on setting these as environment variables when running the agent:
    - In the configuration file/variables:
        - Set `config.git_provider` to "gitea"
    - In the secrets file/variables:
        - Set your AI model key in the respective section
        - In the [Gitea] section, set `personal_access_token` (with token from step 2) and `webhook_secret` (with secret from step 3)

6. Build a Docker image for the app and optionally push it to a Docker repository. We'll use Dockerhub as an example:

    ```bash
    docker build . -t pr-agent:gitea_app --target gitea_app -f docker/Dockerfile

    # Optional, to push it to your own Docker repository:
    docker tag pr-agent:gitea_app <your-registry>/pr-agent:gitea_app
    docker push <your-registry>/pr-agent:gitea_app
    ```

7. Set the environmental variables, the method depends on your docker runtime. Secrets can also be provided through a mounted `.secrets.toml` file. The supported secrets files are excluded from the Docker build context; do not bake secrets into the image.

    ```bash
    CONFIG__GIT_PROVIDER=gitea
    GITEA__PERSONAL_ACCESS_TOKEN=<personal_access_token>
    GITEA__WEBHOOK_SECRET=<webhook_secret>
    GITEA__URL=https://gitea.com # Or self host
    GITEA__WEB_URL=https://git.example.com # Optional: user-facing URL for links published in comments (see below)
    OPENAI__KEY=<your_openai_api_key>
    GITEA__SKIP_SSL_VERIFICATION=false
    GITEA__SSL_CA_CERT=/path/to/cacert.pem
    ```

    > **Note:** SSL verification can be disabled by setting `GITEA__SKIP_SSL_VERIFICATION=true`, although this is not
    > recommended.

    Links published in comments are built from `GITEA__WEB_URL` when set, else from `GITEA__URL`
    when it differs from the shipped default (`https://gitea.com`), else derived from the PR's
    `html_url` (which Gitea/Forgejo builds from its own `ROOT_URL`).
    Set `GITEA__WEB_URL` explicitly when `GITEA__URL` is an internal address users cannot browse
    (e.g. a Docker service name), or when the server's `ROOT_URL` is misconfigured.

8. Create a webhook in your Gitea repository. Set the URL to `http[s]://<PR_AGENT_HOSTNAME>/api/v1/gitea_webhooks`, the secret token to the generated secret from step 3, and enable pull request and pull request comment events. To run commands after new commits, also enable pull request synchronization events and set `gitea.handle_push_trigger = true`. The server handles normalized `pull_request` and `issue_comment` events, not standalone `push` events.

9. Test your installation by opening a pull request or commenting on a pull request using one of PR Agent's commands.

10. The webhook server runs under gunicorn with multiple worker processes. See [Sizing a self-hosted webhook server](./index.md#sizing-a-self-hosted-webhook-server) for the `GUNICORN_WORKERS` / `GUNICORN_MAX_WORKERS` knobs and memory guidance — worth reading before setting a memory limit.

## Incomplete pull-request files

PR-Agent stops the command when Gitea cannot supply complete, valid changed-file data, rather than analyzing a partial change set.

When `CONFIG.PUBLISH_OUTPUT` is enabled, PR-Agent attempts to post a **PR-Agent command was not run** notice with a Gitea-specific explanation. Publishing this notice is best effort; it may not appear if the provider cannot post it.

Retry the command. If the problem persists, inspect the pull request's changed files and diff in Gitea.

## Incremental reviews in this fork

The `xaker00/pr-agent` fork supports `/review -i` (CLI: `review -i`) on Gitea and Forgejo.
This extension is maintained in the fork; it is not part of the published upstream Docker image.
Build the fork to use it. No existing deployment is changed automatically.

The first run reviews the full PR. Subsequent runs select the greatest-ID eligible, complete
native review by the token's authenticated user, with a PR-Agent checkpoint matching that review's
commit ID and the current target SHA. Ordinary issue comments, other users' reviews, pending reviews,
failure messages, and partial review results cannot advance the checkpoint. Reading the authenticated
identity (`GET /user`) requires the token's user-read permission on servers enforcing scoped tokens.
Use a dedicated bot account: reviews published with that account's credentials are trusted.
Review IDs follow the extension's normal append-only publication order; a server reusing an older
pending review can cause an older checkpoint to be selected and changes to be reviewed again.
Review listing follows the advertised underlying record count, including privacy-filtered empty
pages. Missing/inconsistent counts or more than 1,000 underlying reviews cause full fallback.

Incremental model input contains the **net changes between the reviewed SHA and the captured head**,
not full-PR patches filtered by filename or concatenated commit patches. Complete comparison metadata
and parent links establish a linear history; UTF-8 file contents are read at immutable SHAs and used
to construct unified hunks. This also handles additions, deletions, and reverting an earlier PR change.
The usual ignore rules and review size/token limits still apply. A result omitting files or failed
chunks is published without a new checkpoint. An already-reviewed head or empty net delta makes no
model call. The existing incremental commit/time thresholds still apply.

Missing/unsupported APIs, incomplete file metadata, non-UTF-8 contents, changed target SHAs, merges,
rebases, and unreachable anchors conservatively trigger a full PR review, labeled as a fallback in
the published result. No timestamp-only anchor guessing is used. Requests use the configured API
origin and TLS settings and do not follow authenticated redirects; `GITEA.WEB_URL` is only for links.

Gitea review summaries are submitted native `COMMENT` reviews with an explicit head commit ID. The
checkpoint is included atomically in that review, not posted before a model result or in a separate
success comment. Unlike the generic issue-comment publisher, this path appends native reviews rather
than editing a persistent summary (`pr_reviewer.persistent_comment` does not replace old native reviews).
Before publication, the current head and target are checked again; an observed change fails the run
without advancing the checkpoint. This is not server-side compare-and-swap: a push after that check
can still accept a review attached to the captured older head. Its checkpoint remains that captured
SHA, and a changed target invalidates it on the next run. Incremental inline findings use
captured-head new-side line numbers and are
posted only when those lines also belong to the full PR diff; other findings remain in the summary.
The full-PR position index is reused within an incremental command for the captured head/target
pair, while revision checks still run before each inline publication attempt.
`/improve -i` is not enabled by this extension.
