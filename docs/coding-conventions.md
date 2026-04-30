# Coding Conventions

Project-wide conventions captured as we hit decisions worth remembering. Update when a new convention is established or an existing one changes.

---

## Configuration loading: `.env` is the source of truth

All `load_dotenv()` calls must use `override=True`. The `.env` file is the source of truth for configuration; shell environment values must not silently shadow it. This was discovered after a stale shell `ANTHROPIC_API_KEY` caused a confusing 401 error in `scripts/discover.py` during the Phase 3 E2E setup.

```python
load_dotenv(_ROOT / ".env", override=True)
```

### Where `load_dotenv` belongs

`src/` library code must NEVER call `load_dotenv()` directly — only top-level entry points load it. Library code reads from `os.environ.get()` and trusts the caller has loaded `.env` appropriately. This keeps the library testable and the dotenv loading path explicit.

Recognized entry points (the only places `load_dotenv` may be called):

  * `scripts/*.py` — at the top of `main()`, before any code that reads `os.environ`
  * `tests/conftest.py` — at module import, so all tests see the same `.env` values that production scripts do. Without this, pytest invocations would pick up stale shell values and silently shadow `.env`. Discovered in Phase 4 step 11 when an integration test authenticated with a placeholder shell `ANTHROPIC_API_KEY` and failed with HTTP 401, despite the on-disk `.env` having a valid key.
