# Contributing

Thanks for your interest in infoguana. Bug reports, feature ideas, and
PRs are welcome.

## Reporting issues

Open a GitHub issue with:

- What you ran and what you expected to happen
- What actually happened (full error / `docker compose logs infoguana`
  output if relevant)
- Your platform (Linux distro / macOS version / Windows + Docker
  Desktop version) and Compose version (`docker compose version`)

For security-sensitive reports, see [SECURITY.md](SECURITY.md) instead.

## Suggesting features

For anything bigger than a small fix, open an issue first to discuss
direction before you write code. infoguana's design is opinionated
(typed-graph notes, write-time tag suggestion, IDF-weighted BFS
retrieval), so a quick conversation up front saves both of us time.

## Development setup

```bash
git clone https://github.com/ryan-barry-99/infoguana.git
cd infoguana
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m app.main
```

The server runs on `http://localhost:8789/`. Drop a `.env` in the repo
root to override defaults — see [`.env.example`](.env.example).

For Docker-based development:

```bash
cp .env.example .env   # required: compose reads this file
docker compose up -d --build
docker compose logs -f infoguana
```

## Code style

- Match the existing style — no formatter is enforced, but the codebase
  is uniform.
- Type hints on function signatures.
- Docstrings on public functions / MCP tools (the docstrings are how
  agents discover what tools do).
- Don't add comments that just restate the code. Add comments when the
  *why* is non-obvious.
- Don't reference infoguana note IDs (`#NNN`) in code, commit messages,
  or PR descriptions — they're internal state that doesn't survive
  outside an agent session.

## Submitting a PR

- Branch off `main`.
- Keep the diff focused. Refactors and feature work belong in separate
  PRs.
- Describe *why*, not just *what*. Link the issue if there is one.
- Local check: `python -m app.main` boots without errors, and the
  endpoints you touched respond as expected.

## License

By contributing you agree your contribution is licensed under the
[MIT License](LICENSE).
