# Setting up, and choosing models and effort

openfarm has three jobs, and each can use a different model at a different effort.

| Job | What it does | Runs on |
| --- | --- | --- |
| **Architect** | Asks the questions, writes the spec and the plans, answers builders, proposes fixes | Claude or Codex, through your own sign-in |
| **Reviewer** | Shapes each task before it is built, coaches a stuck builder, reviews the result | The same sign-in as the Architect |
| **Builders** | Write the tests and the code, and the tests that try to break it | Any address that speaks the OpenAI chat API |

## The setup helper

```bash
openfarm setup
```

It asks, in order:

1. **Who plans and reviews**: `claude` or `codex`. It says whether that sign-in is ready and offers to sign in.
2. **Who builds**: a preset (`openrouter`, `fireworks`, `together`, `ollama`) or `custom` with your own address,
   then the API key, then whether to ask the provider not to keep what is sent.
3. **Which model builds**: it asks the address for its list. With a long list, type part of a name to narrow it.
4. **Model and effort for each job**. Enter keeps what is shown. An empty model means the provider's default,
   and an effort left unchosen leaves each project's own setting in place.

Pressing Enter at every question changes nothing, so it is safe to run again later.

Every answer is also a flag. With `--yes` nothing is asked: the flags are used and everything else is kept.
`--json` does the same and prints what is now set.

```bash
openfarm setup --yes --frontier claude --preset openrouter --api-key "$KEY" \
    --builder-model qwen/qwen3-coder --architect-effort high --builder-effort medium
openfarm setup --yes --reviewer-effort low      # change one thing, keep the rest
openfarm setup --project                        # models and efforts for this project only
```

| Flag | Meaning |
| --- | --- |
| `--frontier claude\|codex` | Who plans and reviews |
| `--preset NAME`, `--endpoint URL` | Where the builders' requests go |
| `--api-key KEY`, `--key-file PATH` | The builders' key. It is written to a file only you can read |
| `--no-zdr` | Let the provider keep what is sent (the default asks it not to) |
| `--architect-model`, `--reviewer-model`, `--builder-model` | The model for a job |
| `--architect-effort`, `--reviewer-effort`, `--builder-effort` | The effort for a job |
| `--project` | Save models and efforts for this project instead of for every project |
| `--yes` | Ask nothing |

The key is never printed and never stored in `providers.toml`.

## Choosing later

```bash
openfarm models                                  # what is in use, and where each choice comes from
openfarm models set architect --effort high      # for this project
openfarm models set builder --model acme/coder --effort medium --global
openfarm models clear reviewer                   # back to what applied before
openfarm models catalogue                        # the models and efforts each job can be given
```

In the app: `Ctrl+K`, "Models and effort". Pick "this project" or "every project" at the top, fill in what you
want to choose, and Save. "List the models on offer" asks the builders' address for its list, after which the
model fields complete a name as you type.

## Where a choice is kept, and which one wins

| Place | File | Wins over |
| --- | --- | --- |
| This project | `.openfarm/models.toml` in the project | everything below |
| Every project | `models.toml` beside the global `providers.toml` (`~/.config/openfarm/`) | the project's `config.toml` |
| The project's config | `[micromanager]` and `[model]` in `.openfarm/config.toml` | nothing |

A model and an effort are looked up separately, so a project can take its model from the global file and its
effort from its own. `openfarm models` shows where each one came from.

The two `models.toml` files are small and written whole by openfarm:

```toml
[architect]
model = "opus"
effort = "high"

[builder]
model = "deepseek/deepseek-v4.1-flash"
effort = "medium"
```

`config.toml` is never rewritten. Delete a `models.toml`, or use `openfarm models clear`, to go back.

## Effort

- Architect and reviewer: `low`, `medium`, `high`, `xhigh`, `max`.
- Builders: `low`, `medium`, `high`, `max`. This is the effort of the first try. A retry climbs from there, so
  `medium` gives `medium`, `high`, `max`.

## When a change takes effect

From the next step. A build that is already running finishes with what it started with.

## Not tested

- Model names are passed to the provider as typed. openfarm lists `opus`, `sonnet` and `haiku` for Claude
  because Claude's own command line takes them; other names are not checked before a call is made.
- A different model for the Architect than for the reviewer has only been run in tests, not in a live build.
