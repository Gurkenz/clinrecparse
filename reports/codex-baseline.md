# Codex baseline

- repository_commit: `72812020aaf3d2355abfc936340ce583a59aa7c1`
- branch: `codex/clinrec-raw-bank-transactional-lifecycle`
- git_status: clean
- pytest_exit_code: 0
- pytest_test_count: 107
- ruff_exit_code: 0
- mypy_exit_code: 0
- known_preexisting_failures: none with `.venv\Scripts\python.exe`

Commands:

- `git status --short`: exit 0, no output
- `git rev-parse HEAD`: exit 0, `72812020aaf3d2355abfc936340ce583a59aa7c1`
- `git log -5 --oneline`: exit 0
- `python -m pytest`: exit 1 with system Python, `No module named pytest`
- `python -m ruff check .`: exit 1 with system Python, `No module named ruff`
- `python -m mypy src`: exit 1 with system Python, `No module named mypy`
- `.venv\Scripts\python.exe -m pytest`: exit 0, 107 passed
- `.venv\Scripts\python.exe -m ruff check .`: exit 0
- `.venv\Scripts\python.exe -m mypy src`: exit 0

