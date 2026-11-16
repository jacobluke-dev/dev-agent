# TASK-123: Add a greeting function

Implement a small `greet(name: str) -> str` function in the example project's
existing Python package. Follow the package's existing layout and coding style.

## Acceptance criteria

- `greet("Ada")` returns `"Hello, Ada!"`.
- Surrounding whitespace is removed from the supplied name.
- An empty or whitespace-only name raises `ValueError`.
- Add tests for the normal, whitespace and empty-name cases.
- Keep the change scoped to this function and its tests.

The human reviewer will inspect the retained worktree and the recorded checks.
