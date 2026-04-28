# Coding Conventions

Project-wide conventions captured as we hit decisions worth remembering. Update when a new convention is established or an existing one changes.

---

## Configuration loading: `.env` is the source of truth

All `load_dotenv()` calls must use `override=True`. The `.env` file is the source of truth for configuration; shell environment values must not silently shadow it. This was discovered after a stale shell `ANTHROPIC_API_KEY` caused a confusing 401 error in `scripts/discover.py` during the Phase 3 E2E setup.

```python
load_dotenv(_ROOT / ".env", override=True)
```

### Where `load_dotenv` belongs

`src/` library code must NEVER call `load_dotenv()` directly — only top-level scripts load it. Library code reads from `os.environ.get()` and trusts the caller has loaded `.env` appropriately. This keeps the library testable and the dotenv loading path explicit.
