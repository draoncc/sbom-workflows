# Contributing: Commits and History

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <imperative summary>

<optional body: why, not what>

<optional footer: BREAKING CHANGE: ..., refs>
```

- Types: `feat`, `fix`, `docs`, `refactor`, `test`, `perf`, `build`, `ci`, `chore`, `style`, `revert`
- Summary: lowercase, imperative ("add", not "added"), no trailing period, ≤72 chars
- Breaking changes: `!` after type/scope (`feat(api)!: ...`) and a `BREAKING CHANGE:` footer
- One logical change per commit

## Linear history

Keep history linear. Never create merge commits.

- Update a branch with `git fetch && git rebase origin/main`, not `git merge main`; local `main` may be stale, and worktrees branch from `origin/main`
- Pull with `git pull --rebase`
- Integrate branches by rebase + fast-forward (`git merge --ff-only`) or squash/rebase-merge on the PR, never a merge commit
- Clean up WIP commits with `git rebase` (`--autosquash` with `fixup!` commits) before integrating; interactive `-i` is unavailable to agents
- After rebasing a pushed branch, use `git push --force-with-lease`, never `--force`; never force-push `main`
